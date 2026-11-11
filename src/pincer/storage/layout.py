"""The layout of the storage root: the folders Pincer owns, and where the root is.

```
<storage root>/
├── skills/          user skills (the skills dir when PINCER_SKILLS_DIR is unset)
├── workspace/       everything the agent reads and writes
│   ├── uploads/     chat attachments (files and images)
│   ├── downloads/   Google Drive / Gmail downloads
│   ├── exec_output/ python_exec output
│   └── screenshots/ browser screenshots
└── <anything else>  other services
```

Pincer owns exactly `skills/` and `workspace/`. Everything else at the root
belongs to other services that share the bucket or folder: Pincer never
writes there (the storage browser still lets a user manage it).

The root is `PINCER_STORAGE_ROOT`. Unset, it is the bucket / container of an
explicit cloud `PINCER_SKILLS_DIR`, else `<data_dir>/storage`. For a cloud
root the bucket is the source of truth; local copies are only a cache.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from pincer.storage import clients

if TYPE_CHECKING:
    from pincer.config.core import CoreSettings

SKILLS = "skills"
WORKSPACE = "workspace"
WORKSPACE_SUBDIRS = ("uploads", "downloads", "exec_output", "screenshots")


def storage_root_location(settings: CoreSettings, skills_dir: str | Path | None) -> str | Path:
    """Where the storage root is, without touching it.

    `settings.storage_root` when set; else the bucket root of a cloud
    `skills_dir`; else `<data_dir>/storage`. Config passes only an explicitly
    configured skills dir (or None), never the default derived from this root.
    """
    if settings.storage_root:
        return settings.storage_root
    if skills_dir is not None and clients.is_remote_uri(skills_dir):
        return clients.bucket_root(str(skills_dir))
    return settings.data_dir / "storage"


def join(location: str | Path, name: str) -> str | Path:
    """`location/name`: a URI string for a cloud location, a Path for a local one."""
    if isinstance(location, str) and clients.is_remote_uri(location):
        return f"{location.rstrip('/')}/{name}"
    return Path(location).expanduser() / name
