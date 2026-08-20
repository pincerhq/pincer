"""
In-call tool policy (Sprint 11) — the single source of truth for what the
agent may do with tools while a stranger is on the phone.

Three tiers, no exceptions:

- **R** (read-only) executes in every mode, from any conversational phase,
  without approval.
- **W** (reversible writes) follows the configured approval mode — ``verbal``
  (the call partner confirms the exact commitment), ``user`` (the initiating
  Pincer user approves on their own channel), or ``off`` (autonomous, with a
  per-call write budget and post-call disclosure).
- **X** is never callable from call context. Not via overrides, not via
  ``PINCER_VOICE_TOOLS_EXTRA``, not by any configuration. Anything not listed
  in :data:`TIERS` is X — unknown means denied.

The tier table also drives schema filtering: the LLM only ever sees the
schemas of R+W tools that are additionally inside the call's purpose scope
(§5.3). "Invisible" beats "visible but refused" — a jailbroken model cannot
call a tool that was never in its tool array.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from pincer.exceptions import ConfigError

if TYPE_CHECKING:
    from pincer.voice.engine import CallState

logger = logging.getLogger(__name__)

TIER_R = "R"
TIER_W = "W"
TIER_X = "X"

# ── Tier table (single source of truth) ──────────────────────────────
#
# Keys are registry tool names. The Google calendar names are the ones the
# integration actually registers (`google__check_freebusy`, not the spec's
# shorthand `google__freebusy`); `contact_lookup`, `send_owner_message` and
# `memory_note` are the Pincer-owned call tools from voice/call_tools.py.
TIERS: dict[str, str] = {
    # ── Tier R: read-only, auto-execute in every mode, no approval ever
    "google__list_events": TIER_R,
    "google__check_freebusy": TIER_R,
    "contact_lookup": TIER_R,  # phone_contacts read
    # NOTE: outbound calls only; the inbound receptionist (Sprint 12 §9)
    # excludes it — enforced by allowed_tools_for_call (§5.3).
    "memory_search": TIER_R,
    "business_profile_lookup": TIER_R,
    # ── Tier W: reversible writes, follow the approval mode
    "google__create_event": TIER_W,
    "google__update_event": TIER_W,
    "send_owner_message": TIER_W,  # report/notify the initiating user's channel
    "memory_note": TIER_W,
    # ── Tier X: NEVER callable from call context (also not via overrides/extra)
    "email_send": TIER_X,
    "google__delete_event": TIER_X,
    "file_read": TIER_X,
    "file_write": TIER_X,
    "config_get": TIER_X,
    "config_set": TIER_X,
    "memory_dump": TIER_X,
    "do_not_call_add": TIER_X,
    "do_not_call_remove": TIER_X,
    "make_phone_call": TIER_X,  # no call-from-call chains in v1
    "schedule_appointment_call": TIER_X,
}

# Any tool not listed is EXCLUDED. Unknown = denied. MUST NOT default to allow.
DEFAULT_TIER = TIER_X

APPROVAL_MODES: tuple[str, ...] = ("verbal", "user", "off")

# Deny reason codes (§5.2) — stable strings: logged, persisted, metric labels.
REASON_TIER_X = "tier_x"
REASON_NOT_IN_SCOPE = "not_in_call_scope"
REASON_WRITE_BUDGET = "write_budget_exhausted"
REASON_APPROVAL_DENIED = "approval_denied"
REASON_APPROVAL_TIMEOUT = "approval_timeout"
REASON_TOOL_TIMEOUT = "tool_timeout"
REASON_TOOL_ERROR = "tool_error"
DENY_REASONS: tuple[str, ...] = (
    REASON_TIER_X,
    REASON_NOT_IN_SCOPE,
    REASON_WRITE_BUDGET,
    REASON_APPROVAL_DENIED,
    REASON_APPROVAL_TIMEOUT,
    REASON_TOOL_TIMEOUT,
    REASON_TOOL_ERROR,
)

# Per-purpose narrowing (§5.3). The appointment set is fixed; the generic set
# is "all Tier R + the two owner-facing writes" — calendar writes are NOT
# default on a generic call and must be added via PINCER_VOICE_TOOLS_EXTRA.
APPOINTMENT_CALL_TOOLS: frozenset[str] = frozenset(
    {
        "google__check_freebusy",
        "google__list_events",
        "google__create_event",
        "contact_lookup",
        "send_owner_message",
        "memory_note",
    }
)
GENERIC_CALL_WRITE_TOOLS: frozenset[str] = frozenset({"send_owner_message", "memory_note"})
# Sprint 12 §9: the inbound receptionist's EXACT tool set. Explicitly absent
# vs outbound: memory_search, contact_lookup, google__list_events (event
# contents must be unreachable — free/busy returns windows only).
INBOUND_RECEPTIONIST_TOOLS: frozenset[str] = frozenset(
    {"google__check_freebusy", "business_profile_lookup", "google__create_event", "send_owner_message"}
)
# Tools an inbound (receptionist) call never gets, even though their tier is R.
INBOUND_EXCLUDED_TOOLS: frozenset[str] = frozenset({"memory_search"})

# Metadata keys on CallState.metadata used by the policy
META_ALLOWED_TOOLS = "allowed_tools"
META_WRITES_USED = "writes_used"
META_VERBAL_CONFIRMED = "verbal_confirmed_action"
META_PENDING_ARGS = "pending_args"
# Per-call mode overrides (tool -> mode) set by trusted code, e.g. the Sprint 12
# receptionist maps PINCER_RECEPTIONIST_BOOKING_APPROVAL onto google__create_event.
META_MODE_OVERRIDES = "mode_overrides"


def tier_of(tool_name: str) -> str:
    """Tier for a tool name; unknown names are X."""
    return TIERS.get(tool_name, DEFAULT_TIER)


def tier_r_tools() -> frozenset[str]:
    return frozenset(name for name, tier in TIERS.items() if tier == TIER_R)


def tier_w_tools() -> frozenset[str]:
    return frozenset(name for name, tier in TIERS.items() if tier == TIER_W)


def callable_tools() -> frozenset[str]:
    """Every tool that may ever be visible in call context (R + W)."""
    return frozenset(name for name, tier in TIERS.items() if tier != TIER_X)


# ── Config parsing ───────────────────────────────────────────────────


def parse_overrides(raw: str) -> dict[str, str]:
    """Parse ``tool:mode,tool:mode`` into a dict. Malformed entries raise
    ConfigError (fail fast — never a silent fallback)."""
    overrides: dict[str, str] = {}
    for chunk in str(raw or "").split(","):
        entry = chunk.strip()
        if not entry:
            continue
        name, sep, mode = entry.partition(":")
        name, mode = name.strip(), mode.strip().lower()
        if not sep or not name or not mode:
            raise ConfigError(
                f"PINCER_VOICE_TOOL_APPROVAL_OVERRIDES entry {entry!r} is not 'tool_name:mode' "
                f"(modes: {', '.join(APPROVAL_MODES)})"
            )
        if mode not in APPROVAL_MODES:
            raise ConfigError(
                f"PINCER_VOICE_TOOL_APPROVAL_OVERRIDES: unknown mode {mode!r} for tool {name!r} "
                f"(allowed: {', '.join(APPROVAL_MODES)})"
            )
        overrides[name] = mode
    return overrides


def parse_csv(raw: str) -> list[str]:
    return [item.strip() for item in str(raw or "").split(",") if item.strip()]


def validate_approval_mode(mode: str) -> str:
    """Global mode string → normalized; unknown → ConfigError (fail fast)."""
    normalized = str(mode or "").strip().lower()
    if normalized not in APPROVAL_MODES:
        raise ConfigError(
            f"PINCER_VOICE_TOOL_APPROVAL={mode!r} is not a valid mode (allowed: {', '.join(APPROVAL_MODES)})"
        )
    return normalized


def validate_overrides(raw: str) -> dict[str, str]:
    """Parse + policy-check the overrides CSV.

    - unknown mode → ConfigError
    - override for a Tier X tool → ConfigError (the tier table cannot be
      widened by configuration)
    - override for a name not in the tier table → accepted here (doctor
      WARNING ``voice_override_unknown_tool``; ignored at runtime)
    """
    overrides = parse_overrides(raw)
    for name in overrides:
        if tier_of(name) == TIER_X and name in TIERS:
            raise ConfigError(f"tool {name} is excluded from calls and cannot be configured")
    return overrides


def unknown_override_tools(raw: str) -> list[str]:
    """Override entries naming tools outside the tier table (doctor WARNING)."""
    try:
        overrides = parse_overrides(raw)
    except ConfigError:
        return []
    return [name for name in overrides if name not in TIERS]


def ignored_extra_tools(raw: str) -> list[str]:
    """``PINCER_VOICE_TOOLS_EXTRA`` names that are not tier R/W (ignored)."""
    return [name for name in parse_csv(raw) if tier_of(name) == TIER_X]


def tool_overrides(settings: Any) -> dict[str, str]:
    """Resolved per-tool overrides from settings (lenient: a bad value here
    would already have failed at startup; at runtime we ignore garbage)."""
    try:
        return parse_overrides(str(getattr(settings, "voice_tool_approval_overrides", "") or ""))
    except ConfigError:
        return {}


def global_mode(settings: Any) -> str:
    mode = str(getattr(settings, "voice_tool_approval", "verbal") or "verbal").strip().lower()
    return mode if mode in APPROVAL_MODES else "verbal"


def resolve_mode(tool_name: str, settings: Any) -> str:
    """Override > global. Only meaningful for Tier W."""
    return tool_overrides(settings).get(tool_name, global_mode(settings))


def max_writes_per_call(settings: Any) -> int:
    try:
        return max(0, int(getattr(settings, "voice_max_writes_per_call", 3)))
    except (TypeError, ValueError):
        return 3


# ── Per-purpose narrowing (§5.3) ─────────────────────────────────────


def allowed_tools_for_call(
    settings: Any,
    *,
    kind: str = "generic",
    direction: str = "outbound",
) -> frozenset[str]:
    """The call's tool scope, computed once at call creation.

    ``kind``: ``"appointment"`` (Sprint 6 scheduling call) or ``"generic"``
    (make_phone_call / inbound). ``PINCER_VOICE_TOOLS_EXTRA`` widens the scope
    with R/W names only; X names are ignored (doctor WARNING).
    """
    if kind == "receptionist":
        # Fixed by spec; PINCER_VOICE_TOOLS_EXTRA does NOT widen the public line.
        return frozenset(name for name in INBOUND_RECEPTIONIST_TOOLS if tier_of(name) != TIER_X)
    if kind == "appointment":
        scope = set(APPOINTMENT_CALL_TOOLS)
    else:
        scope = set(tier_r_tools()) | set(GENERIC_CALL_WRITE_TOOLS)
    for name in parse_csv(str(getattr(settings, "voice_tools_extra", "") or "")):
        if tier_of(name) != TIER_X:
            scope.add(name)
    if str(direction).lower().endswith("inbound"):
        scope -= INBOUND_EXCLUDED_TOOLS
    # The scope can never contain an X tool, whatever the inputs were.
    return frozenset(name for name in scope if tier_of(name) != TIER_X)


def call_context_schemas(
    tool_schemas: list[dict[str, Any]],
    allowed_tools: frozenset[str] | set[str] | None,
) -> list[dict[str, Any]]:
    """The EXACT tool-schema set the LLM sees in call context: registry ∩
    (R+W) ∩ allowed_tools. Registry order is preserved."""
    allowed = set(allowed_tools or ())
    return [s for s in tool_schemas if tier_of(str(s.get("name", ""))) != TIER_X and s.get("name") in allowed]


# ── Fingerprints (§5.1) ──────────────────────────────────────────────


def canonical_json(args: dict[str, Any] | None) -> str:
    return json.dumps(args or {}, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def action_fingerprint(tool_name: str, args: dict[str, Any] | None) -> str:
    """``sha1(tool_name + canonical_json(args))[:12]`` — a verbal confirmation
    authorizes exactly these arguments; changing the time re-triggers VERIFY."""
    digest = hashlib.sha1((tool_name + canonical_json(args)).encode("utf-8"), usedforsecurity=False).hexdigest()
    return digest[:12]


def _action_fingerprint(tool_name: str, state: CallState) -> str:
    """Spec-named helper: fingerprint of the pending args stored on the call."""
    pending = state.metadata.get(META_PENDING_ARGS)
    return action_fingerprint(tool_name, pending if isinstance(pending, dict) else None)


# A verbal YES authorizes one action for a short window: it is cleared after
# execution, and a confirmation nobody acted on expires after this many caller
# turns (stale confirmations MUST NOT authorize anything later).
VERBAL_CONFIRMATION_TTL_TURNS = 2
META_TURN_NO = "turn_no"
META_VERBAL_CONFIRMED_TURN = "verbal_confirmed_turn"


def begin_turn(state: CallState) -> int:
    """Advance the per-call turn counter; expire a stale verbal confirmation."""
    turn = int(state.metadata.get(META_TURN_NO, 0) or 0) + 1
    state.metadata[META_TURN_NO] = turn
    confirmed_turn = state.metadata.get(META_VERBAL_CONFIRMED_TURN)
    if (
        state.metadata.get(META_VERBAL_CONFIRMED)
        and confirmed_turn is not None
        and turn - int(confirmed_turn) > VERBAL_CONFIRMATION_TTL_TURNS
    ):
        clear_verbal_confirmation(state)
    return turn


def set_verbal_confirmation(state: CallState, tool_name: str, args: dict[str, Any] | None) -> str:
    """Record that the call partner said YES to exactly (tool_name, args)."""
    fingerprint = action_fingerprint(tool_name, args)
    state.metadata[META_VERBAL_CONFIRMED] = fingerprint
    state.metadata[META_PENDING_ARGS] = dict(args or {})
    state.metadata[META_VERBAL_CONFIRMED_TURN] = int(state.metadata.get(META_TURN_NO, 0) or 0)
    return fingerprint


def clear_verbal_confirmation(state: CallState) -> None:
    state.metadata.pop(META_VERBAL_CONFIRMED, None)
    state.metadata.pop(META_PENDING_ARGS, None)
    state.metadata.pop(META_VERBAL_CONFIRMED_TURN, None)


# ── Decision (§5) ────────────────────────────────────────────────────


@dataclass
class ToolDecision:
    action: Literal["execute", "need_verbal", "need_user_approval", "deny"]
    reason: str  # stable code, see §5.2 (or tier_r / verbal / user / mode_off)
    tier: str
    mode: str  # resolved mode for W ("" for R/X)

    @property
    def is_execute(self) -> bool:
        return self.action == "execute"


def decide(
    tool_name: str,
    state: CallState,
    settings: Any,
    arguments: dict[str, Any] | None = None,
) -> ToolDecision:
    """The decision algorithm, exactly as specified (§5).

    ``arguments`` are the tool_use arguments being decided on; when omitted the
    pending args stored on the call are used for the fingerprint check.
    """
    # Step 1 — tier lookup; unknown → X
    tier = tier_of(tool_name)
    if tier == TIER_X:
        return ToolDecision("deny", REASON_TIER_X, tier, "")
    # Step 2 — per-purpose narrowing (§5.3)
    allowed = state.metadata.get(META_ALLOWED_TOOLS)
    if allowed is None or tool_name not in allowed:
        return ToolDecision("deny", REASON_NOT_IN_SCOPE, tier, "")
    # Step 3 — Tier R executes always
    if tier == TIER_R:
        return ToolDecision("execute", "tier_r", tier, "")
    # Step 4 — Tier W: write budget first
    if int(state.metadata.get(META_WRITES_USED, 0) or 0) >= max_writes_per_call(settings):
        return ToolDecision("deny", REASON_WRITE_BUDGET, tier, "")
    # Step 5 — resolve mode: per-call override (trusted code) > settings override > global
    call_overrides = state.metadata.get(META_MODE_OVERRIDES) or {}
    mode = str(call_overrides.get(tool_name) or "") if isinstance(call_overrides, dict) else ""
    if mode not in APPROVAL_MODES:
        mode = resolve_mode(tool_name, settings)
    if mode == "off":
        return ToolDecision("execute", "mode_off", tier, mode)
    if mode == "verbal":
        # verbal confirmation must have been captured THIS turn-cycle for THIS action
        expected = (
            action_fingerprint(tool_name, arguments) if arguments is not None else _action_fingerprint(tool_name, state)
        )
        ok = state.metadata.get(META_VERBAL_CONFIRMED) == expected
        return ToolDecision("execute" if ok else "need_verbal", "verbal", tier, mode)
    return ToolDecision("need_user_approval", "user", tier, mode)


# ── Startup validation (config + doctor) ─────────────────────────────


def validate_settings(settings: Any) -> list[str]:
    """Fail-fast validation of the Sprint 11 settings; returns doctor-level
    warnings (unknown override tools, ignored extras). Raises ConfigError on
    the hard errors (§3 parsing rules 1 and 3)."""
    validate_approval_mode(str(getattr(settings, "voice_tool_approval", "verbal") or "verbal"))
    validate_overrides(str(getattr(settings, "voice_tool_approval_overrides", "") or ""))
    warnings: list[str] = []
    unknown = unknown_override_tools(str(getattr(settings, "voice_tool_approval_overrides", "") or ""))
    if unknown:
        warnings.append("voice_override_unknown_tool: " + ", ".join(unknown))
    ignored = ignored_extra_tools(str(getattr(settings, "voice_tools_extra", "") or ""))
    if ignored:
        warnings.append("voice_tools_extra ignored (tier X): " + ", ".join(ignored))
    return warnings


__all__ = [
    "APPOINTMENT_CALL_TOOLS",
    "APPROVAL_MODES",
    "DEFAULT_TIER",
    "DENY_REASONS",
    "GENERIC_CALL_WRITE_TOOLS",
    "INBOUND_EXCLUDED_TOOLS",
    "INBOUND_RECEPTIONIST_TOOLS",
    "META_ALLOWED_TOOLS",
    "META_MODE_OVERRIDES",
    "META_PENDING_ARGS",
    "META_TURN_NO",
    "META_VERBAL_CONFIRMED",
    "META_VERBAL_CONFIRMED_TURN",
    "META_WRITES_USED",
    "TIERS",
    "TIER_R",
    "TIER_W",
    "TIER_X",
    "VERBAL_CONFIRMATION_TTL_TURNS",
    "ToolDecision",
    "action_fingerprint",
    "allowed_tools_for_call",
    "begin_turn",
    "call_context_schemas",
    "callable_tools",
    "canonical_json",
    "clear_verbal_confirmation",
    "decide",
    "global_mode",
    "ignored_extra_tools",
    "max_writes_per_call",
    "parse_overrides",
    "resolve_mode",
    "set_verbal_confirmation",
    "tier_of",
    "tier_r_tools",
    "tier_w_tools",
    "tool_overrides",
    "unknown_override_tools",
    "validate_approval_mode",
    "validate_overrides",
    "validate_settings",
]
