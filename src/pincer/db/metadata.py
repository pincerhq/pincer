"""The metadata every table model registers on — Alembic's `target_metadata`."""

from __future__ import annotations

from sqlmodel import SQLModel

import pincer.models  # noqa: F401 - registers every table model on the metadata

metadata = SQLModel.metadata
