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
- **Principal** (issue #252): on an outbound call the owner placed, the
  agent acts on the OWNER's authority. Its writes are approved by the owner
  (approval card) or by the owner's approval policy — never by the person on
  the phone — and the owner may grant extra R/W tools for that one call.
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

from pincer.utils.csv_list import split_csv

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
    # The owner's memory (voice/call_tools.py). NOT `memory_search`: that name
    # is the MCP export's, and a call-only tool under it would be exported as
    # an always-failing `pincer_memory_search` (#252). Outbound calls only; the
    # inbound receptionist (Sprint 12 §9) excludes it — allowed_tools_for_call.
    "call_memory_search": TIER_R,
    "business_profile_lookup": TIER_R,
    # Web search (issue #300): not in the guest default, see CALL_OPT_IN_TOOLS.
    "websearch__search": TIER_R,
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

# On whose authority the agent acts (CallState.metadata[META_PRINCIPAL]).
# "owner" is set only by trusted code (`_resolve_principal`) for an outbound
# generic call with a tracked initiating user; anything else, a missing or
# forged value included, is "remote" (fail closed).
PRINCIPAL_OWNER = "owner"
PRINCIPAL_REMOTE = "remote"

# Owner-addressed writes that need no approval on an owner call: the message
# goes to the very person who would answer the approval card, so the card
# would only ask the owner whether to tell them something.
OWNER_SELF_TOOLS: frozenset[str] = frozenset({"send_owner_message"})

# Deny reason codes (§5.2) — stable strings: logged, persisted, metric labels.
REASON_TIER_X = "tier_x"
REASON_NOT_IN_SCOPE = "not_in_call_scope"
REASON_WRITE_BUDGET = "write_budget_exhausted"
REASON_APPROVAL_DENIED = "approval_denied"
REASON_APPROVAL_TIMEOUT = "approval_timeout"
REASON_TOOL_TIMEOUT = "tool_timeout"
REASON_TOOL_ERROR = "tool_error"
# The model asked for a call it already made this turn (same tool, same args).
REASON_REPEATED_CALL = "repeated_call"
DENY_REASONS: tuple[str, ...] = (
    REASON_TIER_X,
    REASON_NOT_IN_SCOPE,
    REASON_WRITE_BUDGET,
    REASON_APPROVAL_DENIED,
    REASON_APPROVAL_TIMEOUT,
    REASON_TOOL_TIMEOUT,
    REASON_TOOL_ERROR,
    REASON_REPEATED_CALL,
)

# Per-purpose narrowing (§5.3). The appointment set is fixed. On a generic
# call a registered user gets every R+W tool; a guest gets "all Tier R except
# CALL_OPT_IN_TOOLS + the two owner-facing writes", or exactly what
# PINCER_VOICE_GUESTS_TOOLS_ALLOWED lists when it is set.
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
# Tier R/W tools left out of the guest default set: a registered user gets
# them, a guest only when PINCER_VOICE_GUESTS_TOOLS_ALLOWED lists them.
CALL_OPT_IN_TOOLS: frozenset[str] = frozenset({"websearch__search"})
# Sprint 12 §9: the inbound receptionist's EXACT tool set. Explicitly absent
# vs outbound: call_memory_search, contact_lookup, google__list_events (event
# contents must be unreachable — free/busy returns windows only).
INBOUND_RECEPTIONIST_TOOLS: frozenset[str] = frozenset(
    {"google__check_freebusy", "business_profile_lookup", "google__create_event", "send_owner_message"}
)
# Tools an inbound (receptionist) call never gets, even though their tier is R.
INBOUND_EXCLUDED_TOOLS: frozenset[str] = frozenset({"call_memory_search"})

# Metadata keys on CallState.metadata used by the policy
META_ALLOWED_TOOLS = "allowed_tools"
META_CALLER_CLASS = "caller_class"
META_WRITES_USED = "writes_used"
META_VERBAL_CONFIRMED = "verbal_confirmed_action"
META_PENDING_ARGS = "pending_args"
# Per-call mode overrides (tool -> mode) set by trusted code, e.g. the Sprint 12
# receptionist maps PINCER_RECEPTIONIST_BOOKING_APPROVAL onto google__create_event.
META_MODE_OVERRIDES = "mode_overrides"
# Issue #252: whose authority the call acts on (PRINCIPAL_*), and the tools the
# owner granted for this call at dial time (make_phone_call allow_tools).
META_PRINCIPAL = "principal"
META_OWNER_GRANTS = "owner_tool_grants"
# The initiating owner, recorded on the pre-registered outbound state by
# make_phone_call (trusted code), for a relay setup that beats call tracking.
META_OWNER_USER = "owner_user_id"
META_OWNER_CHANNEL = "owner_channel"
META_OWNER_PINCER_ID = "owner_pincer_user_id"
META_OWNER_SESSION = "owner_session_key"
# The callee's identity on an outbound call (a confirmed phone), and whether
# it is the identity that placed the call: the owner calling their own phone
# keeps the full R/W scope; any other registered callee gets the owner's
# default set + grants (#252).
META_REMOTE_USER = "remote_pincer_user_id"
META_REMOTE_IS_OWNER = "remote_is_owner"


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


def _guest_entries(raw: object) -> list[str]:
    """Entries of a guest-tools CSV; a non-str (a settings stand-in) is coerced, never raised on."""
    return split_csv(raw if isinstance(raw, str) else str(raw or ""))


def _entry_matches(entry: str) -> frozenset[str]:
    return frozenset(name for name, tier in TIERS.items() if tier != TIER_X and fnmatch.fnmatchcase(name, entry))


def match_tool_patterns(raw: str) -> frozenset[str]:
    """The R/W tools named by a CSV of tool names and fnmatch patterns.

    ``google__*`` expands to the calendar tools the tier table classes R or W;
    a Tier X or unlisted name never matches, whatever the pattern.
    """
    matched: set[str] = set()
    for entry in _guest_entries(raw):
        matched |= _entry_matches(entry)
    return frozenset(matched)


def unmatched_guest_tool_entries(raw: str) -> list[str]:
    """``PINCER_VOICE_GUESTS_TOOLS_ALLOWED`` entries that match no R/W tool (doctor WARNING)."""
    return [entry for entry in _guest_entries(raw) if not _entry_matches(entry)]


def validate_tool_grants(
    names: object, available: frozenset[str] | set[str] | None = None
) -> tuple[frozenset[str], list[str]]:
    """Split owner-requested tool grants into (granted, rejected).

    Each entry must be an exact tool name (no patterns: ``*`` would grant
    every R/W tool at once) that the tier table classes R or W. Tier X,
    unknown and MCP-only names are rejected — the caller reports them, never
    drops them silently. With ``available`` (the registered tools), a grant
    must also name a tool this deployment actually has: granting a calendar
    write without the calendar connected would let the agent promise a
    booking it cannot make.

    ``names`` is a list/tuple of names or a CSV string; ``None`` means no
    grants. Any other type raises ``TypeError`` — a mapping or a bare ``True``
    must never pass as "no grants" while the agent believes it granted one.
    """
    if names is None:
        entries: list[str] = []
    elif isinstance(names, str):
        entries = split_csv(names)
    elif isinstance(names, (list, tuple)):
        entries = [s for n in names if (s := str(n or "").strip())]
    else:
        raise TypeError(f"tool grants must be a list of tool names, not {type(names).__name__}")
    granted: set[str] = set()
    rejected: list[str] = []
    for entry in entries:
        grantable = TIERS.get(entry) in (TIER_R, TIER_W) and (available is None or entry in available)
        if grantable:
            granted.add(entry)
        else:
            rejected.append(entry)
    return frozenset(granted), rejected


def principal_of(state: CallState) -> str:
    """``owner`` only when trusted code set it; anything else is ``remote``."""
    return PRINCIPAL_OWNER if state.metadata.get(META_PRINCIPAL) == PRINCIPAL_OWNER else PRINCIPAL_REMOTE


def owner_grants_of(state: CallState) -> frozenset[str]:
    """The owner's per-call grants, re-filtered against the tier table."""
    raw = state.metadata.get(META_OWNER_GRANTS)
    if not isinstance(raw, (list, tuple, set, frozenset)):
        return frozenset()
    return frozenset(str(n) for n in raw if tier_of(str(n)) != TIER_X)


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
    principal: str = PRINCIPAL_REMOTE,
    owner_grants: frozenset[str] | set[str] = frozenset(),
    remote_is_owner: bool = False,
) -> frozenset[str]:
    """The call's tool scope, computed once at call creation.

    ``kind``: ``"receptionist"`` (Sprint 12 inbound line), ``"appointment"``
    (Sprint 6 scheduling call) or ``"generic"`` (make_phone_call / inbound).
    Receptionist and appointment calls have fixed sets. On a generic call a
    registered ``user`` gets every R+W tool; a guest gets the default generic
    set (without ``CALL_OPT_IN_TOOLS``), or — when ``PINCER_VOICE_GUESTS_TOOLS_ALLOWED`` is set — exactly the
    R/W tools it matches, minus the inbound exclusions.

    An outbound generic call the owner placed (``principal="owner"``) gets
    the default guest set plus ``owner_grants`` (R/W only), whoever answers —
    another registered user on the line does not widen it to every R/W tool.
    Only when the callee is the owner themselves (``remote_is_owner``: a
    registered callee whose identity placed the call) is it the full scope.
    ``PINCER_VOICE_GUESTS_TOOLS_ALLOWED`` does not apply to it, and the
    grants never apply to any other kind of call.
    """
    if kind == "receptionist":
        # Fixed by spec; the guest list does NOT widen the public line.
        return frozenset(name for name in INBOUND_RECEPTIONIST_TOOLS if tier_of(name) != TIER_X)
    if kind == "appointment":
        scope = set(APPOINTMENT_CALL_TOOLS)
    elif principal == PRINCIPAL_OWNER and not str(direction).lower().endswith("inbound"):
        # Checked BEFORE the registered-caller branch: on a call the owner
        # placed, another registered user answering must not widen the scope
        # to every R/W tool — the tools act for the owner, so the owner's
        # default set and grants are the whole scope. The owner answering
        # their own phone (#299) is the one exception.
        if caller_class == CALLER_USER and remote_is_owner:
            scope = set(callable_tools())
        else:
            scope = (set(tier_r_tools()) - CALL_OPT_IN_TOOLS) | set(GENERIC_CALL_WRITE_TOOLS)
            scope |= {name for name in owner_grants if tier_of(name) in (TIER_R, TIER_W)}
    elif caller_class == CALLER_USER:
        scope = set(callable_tools())
    else:
        # Only a real CSV string replaces the default set; anything else (an
        # unset field on a settings stand-in) must not empty the guest scope.
        raw = getattr(settings, "voice_guests_tools_allowed", "")
        guest_list = raw if isinstance(raw, str) else ""
        if split_csv(guest_list):
            scope = set(match_tool_patterns(guest_list))
        else:
            scope = (set(tier_r_tools()) - CALL_OPT_IN_TOOLS) | set(GENERIC_CALL_WRITE_TOOLS)
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

    On an owner call (``principal_of(state) == "owner"``) the person on the
    phone never approves anything: a write runs when the owner's approval
    policy lets it (or it is in ``OWNER_SELF_TOOLS``), and otherwise waits
    for the owner's approval card. Never ``need_verbal``.
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
    if mode not in APPROVAL_MODES and principal_of(state) == PRINCIPAL_OWNER:
        # Step 5b — the owner's authority: the owner's policy, or the owner's card.
        if tool_name in OWNER_SELF_TOOLS:
            return ToolDecision("execute", "owner_self", tier, MODE_POLICY)
        if requires_approval is not None and not requires_approval(tool_name):
            return ToolDecision("execute", "owner_policy", tier, MODE_POLICY)
        return ToolDecision("need_user_approval", "owner", tier, "user")
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
    "CALL_OPT_IN_TOOLS",
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
    "META_OWNER_CHANNEL",
    "META_OWNER_GRANTS",
    "META_OWNER_PINCER_ID",
    "META_OWNER_SESSION",
    "META_REMOTE_IS_OWNER",
    "META_REMOTE_USER",
    "META_OWNER_USER",
    "META_PRINCIPAL",
    "OWNER_SELF_TOOLS",
    "PRINCIPAL_OWNER",
    "PRINCIPAL_REMOTE",
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
    "owner_grants_of",
    "principal_of",
    "receptionist_booking_mode",
    "set_verbal_confirmation",
    "tier_of",
    "tier_r_tools",
    "tier_w_tools",
    "unmatched_guest_tool_entries",
    "validate_tool_grants",
]
