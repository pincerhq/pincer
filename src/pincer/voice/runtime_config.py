"""
Runtime-adjustable voice settings (Sprint 5 follow-up).

The dashboard's telephony page lets the user switch the voice turn model
(e.g. default Sonnet ↔ Claude Haiku ↔ GPT mini) without touching .env or
restarting. Changes are applied to the LIVE settings objects immediately and
persisted here (``data_dir/voice_runtime.json``) so they survive restarts;
an explicit PINCER_VOICE_TURN_MODEL in the environment is only the initial
default — the stored override wins.

Kept deliberately tiny: one file, one known key, defensive reads.
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

RUNTIME_FILE = "voice_runtime.json"

# "" | "<model>" | "<provider>:<model>" — provider names are lowercase slugs;
# model ids never contain a colon themselves.
TURN_MODEL_RE = re.compile(r"^$|^(?:[a-z][a-z0-9_-]{1,31}:)?[A-Za-z0-9._-]{1,80}$")


def _path(settings: Any) -> Path | None:
    data_dir = getattr(settings, "data_dir", None)
    return (data_dir / RUNTIME_FILE) if data_dir else None


def load_overrides(settings: Any) -> dict[str, Any]:
    path = _path(settings)
    if path is None or not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        logger.warning("Unreadable %s — ignoring runtime overrides", path)
        return {}


def apply_overrides(settings: Any) -> None:
    """Apply persisted overrides onto a live Settings object (startup hook)."""
    overrides = load_overrides(settings)
    model = overrides.get("voice_turn_model")
    if isinstance(model, str) and TURN_MODEL_RE.match(model):
        settings.voice_turn_model = model
        logger.info("Voice turn model from runtime override: %r", model or "(default)")


def save_turn_model(settings: Any, model: str) -> None:
    path = _path(settings)
    if path is None:
        return
    overrides = load_overrides(settings)
    overrides["voice_turn_model"] = model
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(overrides, indent=2) + "\n", encoding="utf-8")
