"""
In-call tool policy (Sprint 11) — the single source of truth for what the
agent may do with tools while a stranger is on the phone.

Three tiers, no exceptions:

- **R** (read-only) executes from any conversational phase, without approval.
- **W** (reversible writes) depends on who is on the phone. A **guest** (the
  remote number is no confirmed phone of an identity) always confirms the
  exact commitment out loud (``verbal``). A **registered user** follows the
  standard tool approval policy (``PINCER_APPROVAL_POLICY``): a tool that
  needs approval is confirmed out loud, any other runs directly. Trusted code
  may pin a mode per call (the receptionist's booking approval).
- **X** is never callable from call context. Not via
  ``PINCER_VOICE_GUESTS_TOOLS_ALLOWED``, not by any configuration. Anything
  not listed in :data:`TIERS` is X — unknown means denied.

The tier table also drives schema filtering: the LLM only ever sees the
schemas of R+W tools that are additionally inside the call's purpose scope
(§5.3). "Invisible" beats "visible but refused" — a jailbroken model cannot
call a tool that was never in its tool array.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from collections.abc import Callable

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
    # ── Tier W: reversible writes, approved per caller class (see decide())
    "google__create_event": TIER_W,
    "google__update_event": TIER_W,
    "send_owner_message": TIER_W,  # report/notify the initiating user's channel
    "memory_note": TIER_W,
    # ── Tier X: NEVER callable from call context (also not via the guest list)
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
    # Sprint 13 §8: thread lookup is a CHAT tool. On a call it would let the
    # person on the phone enumerate the user's open matters, so it is X — and
    # therefore never even appears in the call's tool array.
    "thread_lookup": TIER_X,
}

# Any tool not listed is EXCLUDED. Unknown = denied. MUST NOT default to allow.
DEFAULT_TIER = TIER_X

# Modes trusted code may pin per call (META_MODE_OVERRIDES).
APPROVAL_MODES: tuple[str, ...] = ("verbal", "user", "off")

# Mode recorded for a registered caller's write that the approval policy let
# run without confirmation (never configurable; see decide()).
MODE_POLICY = "policy"

# Who is on the phone (CallState.metadata[META_CALLER_CLASS]). Anything but
# "user" is a guest: an unknown class fails closed.
CALLER_USER = "user"
CALLER_GUEST = "guest"

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

# Per-purpose narrowing (§5.3). The appointment set is fixed. On a generic
# call a registered user gets every R+W tool; a guest gets "all Tier R + the
# two owner-facing writes", or exactly what PINCER_VOICE_GUESTS_TOOLS_ALLOWED
# lists when it is set.
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
META_CALLER_CLASS = "caller_class"
META_WRITES_USED = "writes_used"
META_VERBAL_CONFIRMED = "verbal_confirmed_action"
META_PENDING_ARGS = "pending_args"
# Per-call mode overrides (tool -> mode) set by trusted code, e.g. the Sprint 12
# receptionist maps PINCER_RECEPTIONIST_BOOKING_APPROVAL onto google__create_event.
META_MODE_OVERRIDES = "mode_overrides"


# Modes the inbound receptionist may pin onto google__create_event. "verbal"
# is deliberately absent: the receptionist's own deterministic yes/no step IS
# the verbal confirmation, and it never calls set_verbal_confirmation(), so a
# "verbal" override would make decide() answer need_verbal forever and every
# booking would fail with booking_failed. The config layer rejects the value at
# startup; this keeps the invariant next to the code that depends on it.
RECEPTIONIST_BOOKING_MODES: tuple[str, ...] = ("off", "user")


def receptionist_booking_mode(settings: Any) -> str:
    """Resolve the receptionist's booking-approval mode, fail-safe.

    "verbal" collapses to "off" — the caller already said yes out loud, which
    is exactly what the mode would be asking for. Anything unrecognised also
    yields "off" rather than a mode nobody can satisfy: a receptionist that
    silently declines every booking is worse than one that books after the
    caller's spoken confirmation.
    """
    mode = str(getattr(settings, "receptionist_booking_approval", "off") or "off").strip().lower()
    if mode in RECEPTIONIST_BOOKING_MODES:
        return mode
    logger.warning(
        "receptionist_booking_approval=%r cannot be honoured on an inbound call "
        "(allowed: %s) — booking after the caller's spoken yes instead",
        mode,
        ", ".join(RECEPTIONIST_BOOKING_MODES),
    )
    return "off"


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


def parse_csv(raw: str) -> list[str]:
    return [item.strip() for item in str(raw or "").split(",") if item.strip()]


def _entry_matches(entry: str) -> frozenset[str]:
    return frozenset(name for name, tier in TIERS.items() if tier != TIER_X and fnmatch.fnmatchcase(name, entry))


def match_tool_patterns(raw: str) -> frozenset[str]:
    """The R/W tools named by a CSV of tool names and fnmatch patterns.

    ``google__*`` expands to the calendar tools the tier table classes R or W;
    a Tier X or unlisted name never matches, whatever the pattern.
    """
    matched: set[str] = set()
    for entry in parse_csv(raw):
        matched |= _entry_matches(entry)
    return frozenset(matched)


def unmatched_guest_tool_entries(raw: str) -> list[str]:
    """``PINCER_VOICE_GUESTS_TOOLS_ALLOWED`` entries that match no R/W tool (doctor WARNING)."""
    return [entry for entry in parse_csv(raw) if not _entry_matches(entry)]


def caller_class_of(state: CallState) -> str:
    """``user`` for a registered caller, else ``guest`` (fail closed)."""
    return CALLER_USER if state.metadata.get(META_CALLER_CLASS) == CALLER_USER else CALLER_GUEST


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
    caller_class: str = CALLER_GUEST,
) -> frozenset[str]:
    """The call's tool scope, computed once at call creation.

    ``kind``: ``"receptionist"`` (Sprint 12 inbound line), ``"appointment"``
    (Sprint 6 scheduling call) or ``"generic"`` (make_phone_call / inbound).
    Receptionist and appointment calls have fixed sets. On a generic call a
    registered ``user`` gets every R+W tool; a guest gets the default generic
    set, or — when ``PINCER_VOICE_GUESTS_TOOLS_ALLOWED`` is set — exactly the
    R/W tools it matches, minus the inbound exclusions.
    """
    if kind == "receptionist":
        # Fixed by spec; the guest list does NOT widen the public line.
        return frozenset(name for name in INBOUND_RECEPTIONIST_TOOLS if tier_of(name) != TIER_X)
    if kind == "appointment":
        scope = set(APPOINTMENT_CALL_TOOLS)
    elif caller_class == CALLER_USER:
        scope = set(callable_tools())
    else:
        # Only a real CSV string replaces the default set; anything else (an
        # unset field on a settings stand-in) must not empty the guest scope.
        raw = getattr(settings, "voice_guests_tools_allowed", "")
        guest_list = raw if isinstance(raw, str) else ""
        if parse_csv(guest_list):
            scope = set(match_tool_patterns(guest_list))
        else:
            scope = set(tier_r_tools()) | set(GENERIC_CALL_WRITE_TOOLS)
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
    requires_approval: Callable[[str], bool] | None = None,
) -> ToolDecision:
    """The decision algorithm (§5).

    ``arguments`` are the tool_use arguments being decided on; when omitted the
    pending args stored on the call are used for the fingerprint check.
    ``requires_approval`` is the standard tool approval policy
    (``ToolRegistry.requires_approval``); it only decides for a registered
    caller. Without it, a registered caller confirms out loud as a guest does.
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
    # Step 5 — resolve mode: per-call override (trusted code) > caller class
    call_overrides = state.metadata.get(META_MODE_OVERRIDES) or {}
    mode = str(call_overrides.get(tool_name) or "") if isinstance(call_overrides, dict) else ""
    if mode not in APPROVAL_MODES:
        if caller_class_of(state) != CALLER_USER:
            # Step 6 — a guest's write is always confirmed out loud
            mode = "verbal"
        elif requires_approval is not None and not requires_approval(tool_name):
            # Step 7 — a registered caller follows the standard approval policy.
            # Its own mode: these writes ran without anyone confirming, so the
            # post-call report discloses them like `off` writes.
            return ToolDecision("execute", "policy_auto", tier, MODE_POLICY)
        else:
            mode = "verbal"
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


__all__ = [
    "APPOINTMENT_CALL_TOOLS",
    "APPROVAL_MODES",
    "CALLER_GUEST",
    "CALLER_USER",
    "DEFAULT_TIER",
    "DENY_REASONS",
    "GENERIC_CALL_WRITE_TOOLS",
    "INBOUND_EXCLUDED_TOOLS",
    "INBOUND_RECEPTIONIST_TOOLS",
    "RECEPTIONIST_BOOKING_MODES",
    "MODE_POLICY",
    "META_ALLOWED_TOOLS",
    "META_CALLER_CLASS",
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
    "caller_class_of",
    "canonical_json",
    "clear_verbal_confirmation",
    "decide",
    "match_tool_patterns",
    "max_writes_per_call",
    "parse_csv",
    "receptionist_booking_mode",
    "set_verbal_confirmation",
    "tier_of",
    "tier_r_tools",
    "tier_w_tools",
    "unmatched_guest_tool_entries",
]
