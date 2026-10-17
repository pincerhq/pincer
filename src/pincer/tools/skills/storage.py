"""Storage layer for skill roots: local directories and cloud object storage.

Every skill operation (discovery, hashing, listing, reading, running scripts)
goes through `SkillPath`, which is either a local `pathlib.Path` or a
cloudpathlib `CloudPath`. The two share the pathlib API, so callers stay
backend-agnostic; only path confinement and script execution need to know
which one they hold.

Remote files are cached by cloudpathlib alone, under `<skills_cache_dir>/objects/`:
every read refreshes the cached copy when the bucket's object is newer, and
scripts run straight from those cached copies. There is no second file cache.

cloudpathlib is an optional dependency (the `remote-skills` extra) and is only
imported when a skills root is an `s3://`, `az://` or `gs://` URI.
"""

from __future__ import annotations

import logging
import posixpath
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pincer.exceptions import SkillLoadError
from pincer.tools.path_sandbox import SandboxDenied, confine_path

if TYPE_CHECKING:
    from cloudpathlib import CloudPath

    from pincer.config import Settings

logger = logging.getLogger(__name__)

type SkillPath = Path | CloudPath

_REMOTE_URI_RE = re.compile(r"^(s3|az|gs)://", re.IGNORECASE)
_ANY_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
_MISSING_EXTRA_MSG = (
    "Remote skill roots ({uri}) need cloudpathlib. Install it with: pip install 'pincer-agent[remote-skills]'"
)


def is_remote_uri(value: str | Path) -> bool:
    """True for `s3://`, `az://` and `gs://` URIs."""
    return isinstance(value, str) and bool(_REMOTE_URI_RE.match(value))


def _secret(value: Any) -> str | None:
    """Unwrap an optional SecretStr; None when unset or empty."""
    if value is None:
        return None
    raw = value.get_secret_value() if hasattr(value, "get_secret_value") else str(value)
    return raw or None


def _drop_none(**kwargs: Any) -> dict[str, Any]:
    return {k: v for k, v in kwargs.items() if v is not None}


def _build_client(scheme: str, settings: Settings, cache_dir: Path | None) -> Any:
    cache_kwargs = _drop_none(local_cache_dir=str(cache_dir) if cache_dir else None)

    if scheme == "s3":
        import boto3
        from cloudpathlib import S3Client

        session = boto3.Session(
            **_drop_none(
                aws_access_key_id=_secret(settings.skills_s3_access_key_id),
                aws_secret_access_key=_secret(settings.skills_s3_secret_access_key),
                aws_session_token=_secret(settings.skills_s3_session_token),
                profile_name=settings.skills_s3_profile or None,
                region_name=settings.skills_s3_region or None,
            )
        )
        return S3Client(
            boto3_session=session,
            **_drop_none(endpoint_url=settings.skills_s3_endpoint_url or None),
            **cache_kwargs,
        )

    if scheme == "az":
        from cloudpathlib import AzureBlobClient

        connection_string = _secret(settings.skills_azure_connection_string)
        if connection_string:
            return AzureBlobClient(connection_string=connection_string, **cache_kwargs)
        # With only account_url set, cloudpathlib falls back to DefaultAzureCredential.
        return AzureBlobClient(
            **_drop_none(
                account_url=settings.skills_azure_account_url or None,
                credential=_secret(settings.skills_azure_credential),
            ),
            **cache_kwargs,
        )

    if scheme == "gs":
        from cloudpathlib import GSClient

        credentials_file = settings.skills_gcs_credentials_file
        return GSClient(
            **_drop_none(
                application_credentials=str(credentials_file) if credentials_file else None,
                project=settings.skills_gcs_project or None,
            ),
            **cache_kwargs,
        )

    raise SkillLoadError(f"Unsupported skills root scheme: {scheme}://")


def open_skills_root(location: str | Path, settings: Settings) -> SkillPath:
    """Turn a configured skills root into a `SkillPath`.

    Local paths become `Path`. Cloud URIs get a dedicated client built from the
    `skills_*` credential settings (never the cloudpathlib global default
    client), with the SDK's default credential chain covering anything unset.
    """
    if isinstance(location, Path) or not is_remote_uri(location):
        return Path(location).expanduser()

    uri = location
    scheme = uri.split("://", 1)[0].lower()
    try:
        import cloudpathlib  # noqa: F401
    except ImportError as e:
        raise SkillLoadError(_MISSING_EXTRA_MSG.format(uri=uri)) from e

    cache_root = settings.skills_cache_dir
    try:
        client = _build_client(scheme, settings, cache_root / "objects" if cache_root else None)
        return client.CloudPath(uri)  # type: ignore[no-any-return]
    except SkillLoadError:
        raise
    except Exception as e:
        # SDK messages describe the failure (bad profile, malformed connection
        # string), never the secret values themselves.
        raise SkillLoadError(f"Cannot open skills root {uri}: {type(e).__name__}: {e}") from e


def open_user_skills_root(settings: Settings) -> SkillPath | None:
    """`open_skills_root` for `settings.skills_dir`, logging instead of raising so a
    misconfigured remote root degrades to "bundled skills only" rather than
    aborting startup."""
    try:
        return open_skills_root(settings.skills_dir, settings)
    except SkillLoadError as e:
        logger.error("User skills root disabled: %s", e)
        return None


def confine_skill_path(base: SkillPath, requested: str, label: str = "directory") -> SkillPath:
    """Resolve `requested` within `base`, raising SandboxDenied if it would escape.

    Local bases use the shared `confine_path` (symlink-aware resolve). Cloud
    paths have no real resolve, so the request is normalised lexically and
    anything absolute, home-relative, scheme-qualified or `..`-escaping is
    rejected outright.
    """
    if isinstance(base, Path):
        return confine_path(base, requested, label=label)

    denied = SandboxDenied(f"Access denied: path '{requested}' is outside {label} ({base}).")
    if requested.startswith(("~", "/", "\\")) or _ANY_SCHEME_RE.match(requested):
        raise denied
    normalized = posixpath.normpath(requested.replace("\\", "/"))
    if normalized == ".":
        return base
    if normalized == ".." or normalized.startswith("../"):
        raise denied
    return base / normalized


def iter_skill_md(base: SkillPath) -> list[SkillPath]:
    """Every `<base>/<dir>/SKILL.md`, sorted. One listing call on cloud roots."""
    return sorted(base.glob("*/SKILL.md"), key=str)


def _walk_relative(skill_dir: SkillPath) -> list[str]:
    """Relative paths of every file under `skill_dir`."""
    base_depth = len(skill_dir.parts)
    files: list[str] = []
    for dirpath, _dirnames, filenames in skill_dir.walk():
        rel_dir = dirpath.parts[base_depth:]
        files.extend("/".join((*rel_dir, name)) for name in filenames)
    return files


def walk_files(skill_dir: SkillPath) -> list[str]:
    """Relative paths of all files under `skill_dir`, excluding SKILL.md files."""
    return sorted(f for f in _walk_relative(skill_dir) if posixpath.basename(f) != "SKILL.md")


def _cached_copy(path: CloudPath) -> Path:
    """Refresh cloudpathlib's cached copy of `path` and return its local path."""
    from cloudpathlib.exceptions import OverwriteDirtyFileError, OverwriteNewerLocalError

    try:
        return Path(path.fspath)
    except (OverwriteNewerLocalError, OverwriteDirtyFileError):
        # Something (e.g. an unsandboxed script) modified the cached copy. The
        # bucket is the source of truth: drop the local edit and fetch again.
        path._local.unlink(missing_ok=True)
        return Path(path.fspath)


def localize_skill_dir(skill_dir: SkillPath) -> Path:
    """Return a local directory with the current contents of `skill_dir`.

    Local skill dirs are returned as-is. For cloud skill dirs this brings
    cloudpathlib's cache for the dir up to date with the bucket: every file is
    refreshed (re-downloaded only if the object is newer), and cached files
    whose object was deleted are removed, so a script never sees stale siblings.
    """
    if isinstance(skill_dir, Path):
        return skill_dir

    remote_files = _walk_relative(skill_dir)
    local_dir = _cached_copy(skill_dir / "SKILL.md").parent
    for rel in remote_files:
        _cached_copy(skill_dir / rel)

    keep = set(remote_files)
    for dirpath, _dirnames, filenames in local_dir.walk(top_down=False):
        for name in filenames:
            if (dirpath / name).relative_to(local_dir).as_posix() not in keep:
                (dirpath / name).unlink()
        if dirpath != local_dir and not any(dirpath.iterdir()):
            dirpath.rmdir()
    return local_dir


def clear_remote_cache(root: SkillPath | None) -> None:
    """Empty cloudpathlib's file cache for a cloud root; a no-op for local roots."""
    if root is not None and not isinstance(root, Path):
        root.client.clear_cache()
