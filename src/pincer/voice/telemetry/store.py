"""
Persistence for telephony telemetry.

Schema is owned by Alembic revisions 0010 and 0016; this module only reads and
writes, both through `pincer.services.telemetry`.

Three properties the write path must have, because the data arrives from an
unreliable world:

* **Idempotent.** Twilio retries status callbacks for minutes. Events carry a
  deterministic id for anything provider-sourced, and every insert is
  ``INSERT OR IGNORE`` / upsert — a duplicate is a no-op, not a second row on
  the timeline.
* **Order-independent.** Nothing depends on arrival order. Rows are ordered at
  read time by ``(ts_utc, seq)``; a late event lands in its right place.
* **Partial-tolerant.** A call row is written as soon as the call exists, and
  every later write is an upsert of the fields that are known. A call that dies
  mid-setup still has a row, its events, and whatever spans closed — which is
  precisely when someone needs them.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pincer.db.engine import get_database_url
from pincer.services.telemetry import TelemetryService

if TYPE_CHECKING:
    from collections.abc import Sequence

    from pincer.voice.telemetry.records import TelemetryEvent, TelemetrySpan

logger = logging.getLogger(__name__)

CALL_FIELDS = (
    "provider_call_id",
    "trace_id",
    "direction",
    "provider",
    "engine",
    "transport",
    "codec",
    "sample_rate",
    "model",
    "language",
    "tenant_id",
    "environment",
    "app_version",
    "from_number_masked",
    "to_number_masked",
    "registered_at",
    "dialed_at",
    "answered_at",
    "media_open_at",
    "ended_at",
    "status",
    "outcome",
    "failure_category",
    "termination_reason",
    "failure_code",
    "duration_ms",
    "setup_ms",
    "media_establish_ms",
    "turn_count",
    "tool_count",
    "error_count",
    "timeout_count",
    "retry_count",
    "interruption_count",
    "reconnect_count",
    "sampled",
    "sample_rate_used",
    "coverage",
    "config_json",
    "updated_at",
)

TURN_FIELDS = (
    "call_id",
    "turn_no",
    "trigger",
    "started_at",
    "first_audio_at",
    "engine",
    "model",
    "language",
    "streamed",
    "response_latency_ms",
    "response_latency_source",
    "endpointing_ms",
    "stt_first_partial_ms",
    "stt_final_ms",
    "agent_queue_ms",
    "agent_prep_ms",
    "llm_ttft_ms",
    "llm_total_ms",
    "tool_total_ms",
    "tts_first_audio_ms",
    "tts_total_ms",
    "audio_queue_ms",
    "total_ms",
    "tool_calls",
    "tool_retries",
    "tool_timeouts",
    "interrupted",
    "cancelled",
    "error",
    "bottleneck_stage",
    "bottleneck_ms",
    "critical_path",
    "complete",
    "created_at",
)


def service(db_path: str | Path) -> TelemetryService:
    """Telemetry on the database `db_path` configures (or `PINCER_DATABASE_URL` overrides)."""
    return TelemetryService(get_database_url(Path(str(db_path))))


class SqlSink:
    """The recorder's durable sink.

    A batch is one unit of work. Slow is survivable — the recorder queues and
    drops when it has to; what must not happen is the audio path waiting.
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)
        self._service: TelemetryService | None = None

    async def _store(self) -> TelemetryService:
        if self._service is None:
            self._service = service(self._db_path)
        return self._service

    async def write(self, events: Sequence[TelemetryEvent], spans: Sequence[TelemetrySpan]) -> None:
        if not events and not spans:
            return
        telemetry = await self._store()
        await telemetry.write_rows([event.to_row() for event in events], [span.to_row() for span in spans])

    async def close(self) -> None:
        self._service = None


#: The name this sink had when it was SQLite-only.
SqliteSink = SqlSink


# ── call + turn upserts (not on the audio path) ──────────────────────


async def upsert_call(db_path: str | Path, call_id: str, **fields: Any) -> None:
    """Create or update the per-call row with whatever is known right now."""
    known = {k: v for k, v in fields.items() if k in CALL_FIELDS and v is not None}
    if not known:
        return
    await service(db_path).upsert_call(call_id, known)


async def save_turn(db_path: str | Path, turn_id: str, **fields: Any) -> None:
    """Write a completed turn's derived latencies."""
    known = {k: v for k, v in fields.items() if k in TURN_FIELDS and v is not None}
    if not known:
        return
    await service(db_path).upsert_turn(turn_id, known)


def loads(raw: Any, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default
