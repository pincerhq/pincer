"""cloudpathlib clients for object storage, shared by every feature that reads it.

Remote skill roots (`pincer.tools.skills.storage`) and the storage browser
(`pincer.storage.browser`) both reach S3, Azure Blob and Google Cloud Storage
through here, configured by the same `storage_*` credential settings.

cloudpathlib is an optional dependency (the `remote-skills` extra); nothing
here imports it until a client is actually built.
"""

from __future__ import annotations

import posixpath
import re
from typing import TYPE_CHECKING, Any

from pincer.exceptions import PincerError
from pincer.tools.path_sandbox import SandboxDenied

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from cloudpathlib import CloudPath

    from pincer.config import Settings

_REMOTE_URI_RE = re.compile(r"^(s3|az|gs)://", re.IGNORECASE)
ANY_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
# What to configure when a remote root can't be used. SDK default credentials
# (IAM role / ~/.aws, managed identity / az login, ADC) count as configured too.
STORAGE_HINTS = {
    "s3": (
        "Configure PINCER_STORAGE_S3_ACCESS_KEY_ID and PINCER_STORAGE_S3_SECRET_ACCESS_KEY "
        "(or PINCER_STORAGE_S3_PROFILE), PINCER_STORAGE_S3_REGION, and PINCER_STORAGE_S3_ENDPOINT_URL "
        "for S3-compatible servers (MinIO, R2) — or provide AWS default credentials (IAM role, AWS_* env, ~/.aws)."
    ),
    "az": (
        "Configure PINCER_STORAGE_AZURE_CONNECTION_STRING, or PINCER_STORAGE_AZURE_ACCOUNT_URL with "
        "PINCER_STORAGE_AZURE_CREDENTIAL (account key or SAS token). With only the account URL, "
        "DefaultAzureCredential is used, so a managed / workload identity or `az login` must be available."
    ),
    "gs": (
        "Configure PINCER_STORAGE_GCS_CREDENTIALS_FILE (service-account JSON) and PINCER_STORAGE_GCS_PROJECT "
        "— or provide Application Default Credentials."
    ),
}


class UnsupportedStorageScheme(PincerError):
    """A URI scheme with no cloudpathlib client behind it."""


def is_remote_uri(value: str | Path) -> bool:
    """True for `s3://`, `az://` and `gs://` URIs."""
    return isinstance(value, str) and bool(_REMOTE_URI_RE.match(value))


def split_uri(uri: str) -> tuple[str, str]:
    """(lower-cased scheme, the rest without surrounding slashes): `S3://b/k/` → `("s3", "b/k")`."""
    scheme, _, rest = uri.partition("://")
    return scheme.lower(), rest.strip("/")


def uri_scheme(uri: str) -> str:
    """The lower-cased scheme of a URI: `S3://b/k` → `s3`."""
    return split_uri(uri)[0]


def bucket_root(uri: str) -> str:
    """The bucket or container a cloud URI lives in, as a root: `s3://b/k` → `s3://b/`."""
    scheme, rest = split_uri(uri)
    return f"{scheme}://{rest.split('/', 1)[0]}/"


def _secret(value: Any) -> str | None:
    """Unwrap an optional SecretStr; None when unset or empty."""
    if value is None:
        return None
    raw = value.get_secret_value() if hasattr(value, "get_secret_value") else str(value)
    return raw or None


def _drop_none(**kwargs: Any) -> dict[str, Any]:
    return {k: v for k, v in kwargs.items() if v is not None}


def build_client(scheme: str, settings: Settings, cache_dir: Path | None, file_cache_mode: Any = None) -> Any:
    """A cloudpathlib client for `scheme`, from the `storage_*` settings.

    Never the cloudpathlib global default client; the provider SDK's default
    credential chain covers anything left unset. `file_cache_mode` is passed
    through when given (cloudpathlib's default keeps cached copies).
    """
    cache_kwargs = _drop_none(
        local_cache_dir=str(cache_dir) if cache_dir else None,
        file_cache_mode=file_cache_mode,
    )

    if scheme == "s3":
        import boto3
        from cloudpathlib import S3Client

        session = boto3.Session(
            **_drop_none(
                aws_access_key_id=_secret(settings.storage_s3_access_key_id),
                aws_secret_access_key=_secret(settings.storage_s3_secret_access_key),
                aws_session_token=_secret(settings.storage_s3_session_token),
                profile_name=settings.storage_s3_profile or None,
                region_name=settings.storage_s3_region or None,
            )
        )
        return S3Client(
            boto3_session=session,
            **_drop_none(endpoint_url=settings.storage_s3_endpoint_url or None),
            **cache_kwargs,
        )

    if scheme == "az":
        from cloudpathlib import AzureBlobClient

        connection_string = _secret(settings.storage_azure_connection_string)
        if connection_string:
            return AzureBlobClient(connection_string=connection_string, **cache_kwargs)
        account_url = settings.storage_azure_account_url or None
        credential: Any = _secret(settings.storage_azure_credential)
        if account_url and credential is None:
            # cloudpathlib would connect anonymously here (credential=None), so build
            # the SDK's default chain ourselves: managed / workload identity, Azure CLI
            # login, environment credentials. It authenticates on first use.
            from azure.identity import DefaultAzureCredential

            credential = DefaultAzureCredential()
        # With neither a connection string nor an account URL, cloudpathlib raises
        # MissingCredentialsError (unless AZURE_STORAGE_CONNECTION_STRING is set).
        return AzureBlobClient(**_drop_none(account_url=account_url, credential=credential), **cache_kwargs)

    if scheme == "gs":
        from cloudpathlib import GSClient

        credentials_file = settings.storage_gcs_credentials_file
        return GSClient(
            **_drop_none(
                application_credentials=str(credentials_file) if credentials_file else None,
                project=settings.storage_gcs_project or None,
            ),
            **cache_kwargs,
        )

    raise UnsupportedStorageScheme(f"Unsupported storage scheme: {scheme}://")


def open_cloud_uri(
    uri: str,
    settings: Settings,
    cache_dir: Path | None,
    *,
    error: type[Exception],
    what: str,
    file_cache_mode: str | None = None,
    build: Callable[[str, Settings, Path | None], Any] | None = None,
) -> CloudPath:
    """`uri` as a CloudPath, on a client built from the `storage_*` settings.

    Every failure is raised as `error`, naming `what` is being opened (e.g.
    "skills root"): a missing cloudpathlib with what to install, anything else
    with what to configure for the scheme. SDK messages describe the failure
    (a bad profile, a malformed connection string), never the secret values.
    `build` stands in for `build_client`, for a caller that keeps a name of
    its own to patch in tests.
    """
    scheme = uri_scheme(uri)
    try:
        import cloudpathlib  # noqa: F401
    except ImportError as e:
        raise error(
            f"A cloud {what} ({uri}) needs cloudpathlib. Install it with: pip install 'pincer-agent[remote-skills]'"
        ) from e
    try:
        if build is not None:
            client = build(scheme, settings, cache_dir)
        else:
            client = build_client(scheme, settings, cache_dir, file_cache_mode=file_cache_mode)
        return client.CloudPath(uri)  # type: ignore[no-any-return]
    except error:
        raise
    except Exception as e:
        raise error(f"Cannot open {what} {uri}: {type(e).__name__}: {e}. {STORAGE_HINTS.get(scheme, '')}") from e


def confine_cloud_path(base: CloudPath, requested: str, label: str = "directory") -> CloudPath:
    """Resolve `requested` within the cloud path `base`, raising SandboxDenied if it would escape.

    Cloud paths have no real resolve, so the request is normalised lexically and
    anything absolute, home-relative, scheme-qualified or `..`-escaping is
    rejected outright.
    """
    denied = SandboxDenied(f"Access denied: path '{requested}' is outside {label} ({base}).")
    if requested.startswith(("~", "/", "\\")) or ANY_SCHEME_RE.match(requested):
        raise denied
    normalized = posixpath.normpath(requested.replace("\\", "/"))
    if normalized == ".":
        return base
    if normalized == ".." or normalized.startswith("../"):
        raise denied
    return base / normalized
