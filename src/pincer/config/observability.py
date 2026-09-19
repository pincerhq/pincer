"""Observability, alerting, and SLO configuration (Sprint 9).

Three groups of numbers, deliberately kept apart because they answer different
questions:

* **Alert thresholds** (`alert_*`) — "wake someone up / tell someone". Tuned for
  the pilot's traffic volume, expected to move as we learn.
* **SLO targets** (`slo_*`) — "what we promise ourselves". These are *measured*
  commitments; the error budget is computed against them. They move only by
  deliberate decision, not by tuning.
* **Routing** (`ops_*`) — where alerts land.

Every threshold is configurable because a pilot with 5 calls a day and a
customer with 500 need different windows for the same rule.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ObservabilitySettings(BaseModel):
    # ── Ops alert routing (T9.2) ──────────────────────────
    ops_alerts_enabled: bool = Field(
        default=True,
        description="Evaluate golden-signal alert rules and deliver alerts to the ops channel",
    )
    ops_user_id: str = Field(
        default="",
        description="User ID that receives ops alerts and digests (empty = fall back to default_user_id)",
    )
    ops_channel: str = Field(
        default="telegram",
        description="Channel ops alerts are delivered on (dogfood: Pincer delivers its own alerts)",
    )
    ops_alert_email: str = Field(
        default="",
        description="Fallback email for alerts when channel delivery fails (empty = log only)",
    )
    ops_alert_scan_interval_min: int = Field(
        default=5,
        ge=1,
        le=60,
        description="How often the alert scanner evaluates the golden-signal rules (minutes)",
    )
    ops_alert_repeat_min: int = Field(
        default=60,
        ge=1,
        description="Minimum minutes between repeat notifications for the same firing alert",
    )
    ops_digest_cron: str = Field(
        default="0 9 * * 1",
        description="Cron for the weekly failure digest (default Monday 09:00 in the voice timezone)",
    )

    # ── Alert thresholds (T9.2 golden signals) ────────────
    alert_call_success_min: float = Field(
        default=0.85,
        ge=0.0,
        le=1.0,
        description="Notify when the terminated-call success rate falls below this over its window",
    )
    alert_call_success_window_h: int = Field(default=2, ge=1, le=168, description="Call success rate window (hours)")
    alert_call_success_min_volume: int = Field(
        default=5,
        ge=1,
        description="Minimum terminated calls in the window before the success-rate rule can fire "
        "(one failed call out of one must not page anyone)",
    )
    alert_booking_success_min: float = Field(
        default=0.70,
        ge=0.0,
        le=1.0,
        description="Notify when the appointment booking success rate falls below this",
    )
    alert_booking_window_h: int = Field(default=24, ge=1, le=336, description="Booking success rate window (hours)")
    alert_booking_min_volume: int = Field(default=3, ge=1, description="Minimum booking attempts before firing")
    alert_latency_p95_max_s: float = Field(
        default=2.5,
        gt=0,
        description="Notify when p95 voice-to-voice turn latency exceeds this over its window",
    )
    alert_latency_window_h: int = Field(default=1, ge=1, le=168, description="Latency window (hours)")
    alert_latency_min_turns: int = Field(default=10, ge=1, description="Minimum turns before the latency rule fires")
    alert_stuck_call_grace_s: int = Field(
        default=60,
        ge=0,
        description="Seconds past voice_max_call_duration before a call counts as stuck (pages immediately)",
    )
    alert_cost_p95_multiplier: float = Field(
        default=2.0,
        gt=1.0,
        description="Notify when p95 cost per call exceeds this multiple of the 7-day baseline",
    )
    alert_cost_baseline_days: int = Field(default=7, ge=1, le=90, description="Cost baseline window (days)")
    alert_cost_min_calls: int = Field(default=10, ge=1, description="Minimum priced calls before the cost rule fires")
    alert_disk_free_min_pct: float = Field(
        default=10.0,
        ge=0.0,
        le=100.0,
        description="Page when free disk on the data volume drops below this percentage (SQLite and backups share it)",
    )

    # ── SLO targets (T9.5) ────────────────────────────────
    slo_call_attempt_success: float = Field(
        default=0.99,
        ge=0.0,
        le=1.0,
        description="SLO: fraction of call attempts that succeed, excluding callee no-answer/busy",
    )
    slo_latency_p95_s: float = Field(default=2.0, gt=0, description="SLO: p95 voice-to-voice turn latency (seconds)")
    slo_report_delivery_s: float = Field(
        default=30.0,
        gt=0,
        description="SLO: seconds from hangup to the post-call report reaching the initiating user",
    )
    slo_availability: float = Field(
        default=0.995, ge=0.0, le=1.0, description="SLO: monthly availability of the voice service"
    )
    slo_error_budget_freeze_pct: float = Field(
        default=50.0,
        ge=0.0,
        le=100.0,
        description="Feature-freeze rule: burning more than this share of the monthly error budget "
        "mid-month stops feature work until the budget recovers",
    )

    # ── Unit prices for the per-call cost record (T9.1) ───
    #
    # Approximate list prices; they are configuration because every account has
    # its own rates (volume tier, region, plan). The *structure* of the cost
    # record is what matters — override these with your real numbers or the
    # cost-per-call signal is precise but wrong.
    price_twilio_outbound_per_min: float = Field(
        default=0.10, ge=0.0, description="Twilio outbound PSTN price per minute (USD) — DE mobile list price"
    )
    price_twilio_inbound_per_min: float = Field(
        default=0.0085, ge=0.0, description="Twilio inbound PSTN price per minute (USD)"
    )
    price_conversationrelay_per_min: float = Field(
        default=0.06,
        ge=0.0,
        description="Twilio ConversationRelay add-on per minute (USD) — covers its bundled STT/TTS, "
        "so the separate STT/TTS prices below do not apply on that engine",
    )
    price_deepgram_per_min: float = Field(
        default=0.0077, ge=0.0, description="Deepgram streaming STT price per minute (USD), media_streams engine only"
    )
    price_elevenlabs_per_1k_chars: float = Field(
        default=0.05,
        ge=0.0,
        description="ElevenLabs TTS price per 1000 synthesized characters (USD), Flash tier",
    )

    # ── Synthetic canary (T9.2) ───────────────────────────
    voice_canary_enabled: bool = Field(
        default=False,
        description="Run the synthetic loop call that exercises STT+LLM+TTS end to end",
    )
    voice_canary_number: str = Field(
        default="",
        description="E.164 number the canary calls (a staging responder, never a customer)",
    )
    voice_canary_cron: str = Field(
        default="0 */6 * * *",
        description="Cron for the canary call (default every 6 hours)",
    )
    voice_canary_timeout_s: int = Field(
        default=180,
        ge=30,
        le=900,
        description="Seconds to wait for the canary call to connect and converse before declaring it failed",
    )
    voice_canary_min_turns: int = Field(
        default=1,
        ge=0,
        description="Turns the canary must complete for the run to count as healthy "
        "(0 = connecting is enough; 1+ proves STT and the LLM answered)",
    )

    # ── Telephony telemetry (call/turn traces and latency metrics) ──
    #
    # Separate from the golden signals above on purpose: those answer "is voice
    # healthy", these answer "why was THIS turn slow". The first is a handful of
    # numbers per hour, the second is tens of rows per turn, so it gets its own
    # sampling, its own retention, and its own bounded exporter.
    telephony_telemetry_enabled: bool = Field(
        default=True,
        description="Record correlated call/turn telemetry (events, spans, latency metrics) to the database",
    )
    telephony_telemetry_sample_rate: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description="Fraction of calls whose turn-level detail is recorded. Head-based and per call, so a "
        "sampled call is complete; lifecycle events are recorded for every call regardless",
    )
    telephony_telemetry_queue_size: int = Field(
        default=4096,
        ge=64,
        description="Bounded export queue. When it is full, records are DROPPED and counted rather than "
        "blocking the audio pipeline — the drop count is reported by /api/telephony/health",
    )
    telephony_telemetry_batch_size: int = Field(
        default=256, ge=1, description="Maximum records written per export batch"
    )
    telephony_telemetry_flush_interval_s: float = Field(
        default=0.5, gt=0.0, le=30.0, description="Maximum seconds a record waits in the queue before export"
    )
    telephony_telemetry_retention_days: int = Field(
        default=30,
        ge=0,
        description="Days of telephony events/spans/turn metrics to keep (0 = keep forever). Technical "
        "telemetry only; transcripts and recordings keep their own, stricter retention",
    )
    telephony_min_samples: int = Field(
        default=20,
        ge=1,
        description="Percentiles computed over fewer samples than this are flagged as under-sampled in the "
        "dashboard and cannot fire an alert",
    )

    # ── Telephony alert thresholds ────────────────────────────────
    #
    # Thresholds start from the SLO targets above and from the baselines
    # measured on the pilot; `pincer telephony baseline` prints the observed
    # percentiles these should be set from.
    alert_response_latency_p95_ms: float = Field(
        default=2000.0,
        gt=0,
        description="Fire when p95 response latency (caller speech end -> first response audio SENT) "
        "exceeds this over its window",
    )
    alert_response_latency_window_min: int = Field(
        default=30, ge=1, le=1440, description="Response-latency alert window (minutes)"
    )
    alert_connection_rate_min: float = Field(
        default=0.90, ge=0.0, le=1.0, description="Fire when the connection rate falls below this"
    )
    alert_technical_failure_rate_max: float = Field(
        default=0.05, ge=0.0, le=1.0, description="Fire when the technical failure rate exceeds this"
    )
    alert_unexpected_disconnect_rate_max: float = Field(
        default=0.02, ge=0.0, le=1.0, description="Fire when the unexpected-disconnect rate exceeds this"
    )
    alert_stage_timeout_max: int = Field(
        default=3,
        ge=1,
        description="Fire when STT/LLM/TTS/tool timeouts in the window exceed this count",
    )
    alert_audio_queue_p95_ms: float = Field(
        default=250.0,
        gt=0,
        description="Fire when p95 outbound audio queue residence exceeds this — a growing queue means "
        "we are producing audio faster than we can ship it",
    )
    alert_telemetry_coverage_min: float = Field(
        default=0.95,
        ge=0.0,
        le=1.0,
        description="Fire when the share of emitted telemetry records that reached storage falls below this",
    )
    alert_telephony_window_min: int = Field(
        default=60, ge=1, le=1440, description="Default evaluation window for telephony rate alerts (minutes)"
    )
    alert_telephony_min_calls: int = Field(
        default=10, ge=1, description="Minimum calls in the window before a telephony rate alert can fire"
    )
