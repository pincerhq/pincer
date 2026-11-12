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
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pincer.exceptions import SkillLoadError
from pincer.storage import clients
from pincer.tools.path_sandbox import confine_path

if TYPE_CHECKING:
    from cloudpathlib import CloudPath

    from pincer.config import Settings

logger = logging.getLogger(__name__)

type SkillPath = Path | CloudPath


def _build_client(scheme: str, settings: Settings, cache_dir: Path | None) -> Any:
    # A name of this module's own, so tests can patch the client skill roots get.
    try:
        return clients.build_client(scheme, settings, cache_dir)
    except clients.UnsupportedStorageScheme as e:
        raise SkillLoadError(f"Unsupported skills root scheme: {scheme}://") from e


def open_skills_root(location: str | Path, settings: Settings) -> SkillPath:
    """Turn a configured skills root into a `SkillPath`.

    Local paths become `Path`. Cloud URIs get a dedicated client built from the
    `skills_*` credential settings (never the cloudpathlib global default
    client), with the SDK's default credential chain covering anything unset.
    """
    if isinstance(location, Path) or not clients.is_remote_uri(location):
        return Path(location).expanduser()

    cache_root = settings.skills_cache_dir
    return clients.open_cloud_uri(
        location,
        settings,
        cache_root / "objects" if cache_root else None,
        error=SkillLoadError,
        what="skills root",
        build=_build_client,
    )


def open_usable_skills_root(location: str | Path, settings: Settings) -> SkillPath:
    """`open_skills_root`, plus proof that a remote root can actually be used.

    For a cloud URI this checks whether the root exists and, if it does, lists it
    once, so missing or wrong credentials, a missing bucket or an unreachable
    endpoint fail here, at startup, with what to configure. A root that does not
    exist yet is not an error (no skills yet): it is not listed, because on Azure
    accounts with hierarchical namespace (ADLS Gen2) listing a missing directory
    fails instead of returning nothing, and it is not created either — startup
    never writes to storage. A warning names the URI so a mistyped path is
    visible. Local paths are returned as-is: a missing local dir just has no skills.
    Startup calls this so a misconfigured remote root refuses to start.
    """
    root = open_skills_root(location, settings)
    if isinstance(root, Path):
        return root
    uri = str(location)
    hint = clients.STORAGE_HINTS.get(clients.uri_scheme(uri), "")
    try:
        # is_dir() is False for a missing root but still raises for bad credentials,
        # a missing bucket/container or an unreachable endpoint: with cloudpathlib
        # 0.26 a missing object falls back to a prefix listing on every backend
        # (S3 objects.filter, GCS list_blobs, Azure list_blobs), and that listing
        # fails when the bucket or container is not there.
        exists = root.is_dir()
        if exists:
            next(iter(root.iterdir()), None)
    except Exception as e:
        raise SkillLoadError(f"Skills root {uri} is not usable: {type(e).__name__}: {e}. {hint}") from e
    if not exists:
        logger.warning(
            "Skills root %s does not exist; starting with no user skills. If the path is right, it is "
            "created when the first skill is added",
            uri,
        )
    return root


def open_user_skills_root(settings: Settings) -> SkillPath | None:
    """`open_skills_root` for `settings.skills_dir`, logging instead of raising: a
    misconfigured remote root degrades to "bundled skills only". For code that
    builds an index lazily after startup; startup itself uses the strict
    `open_usable_skills_root`."""
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

    return clients.confine_cloud_path(base, requested, label=label)


def iter_skill_md(base: SkillPath) -> list[SkillPath]:
    """Candidate `<base>/<dir>/SKILL.md` paths, sorted.

    Local roots return only existing files. Cloud roots cost one non-recursive
    listing of the root's children — a `*/SKILL.md` glob would page through every
    object under the root, assets included. A cloud candidate may not exist (a
    folder without SKILL.md, or a stray file at the root); reading it raises
    FileNotFoundError, which discovery treats as "not a skill".
    """
    if isinstance(base, Path):
        return sorted(base.glob("*/SKILL.md"), key=str)
    return sorted((child / "SKILL.md" for child in base.iterdir()), key=str)


def _walk_relative(skill_dir: SkillPath) -> list[str]:
    """Relative paths of every file under `skill_dir`."""
    base_depth = len(skill_dir.parts)
    files: list[str] = []
    for dirpath, _dirnames, filenames in skill_dir.walk():
        rel_dir = dirpath.parts[base_depth:]
        for name in filenames:
            # Path.walk lists a symlink to a directory among the files; it is not one.
            if isinstance(dirpath, Path) and (dirpath / name).is_dir():
                continue
            files.append("/".join((*rel_dir, name)))
    return files


def walk_files(skill_dir: SkillPath) -> list[str]:
    """Relative paths of all files under `skill_dir`, excluding SKILL.md files."""
    return sorted(f for f in _walk_relative(skill_dir) if posixpath.basename(f) != "SKILL.md")


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
    local_dir = clients.cached_copy(skill_dir / "SKILL.md").parent
    for rel in remote_files:
        clients.cached_copy(skill_dir / rel)

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
