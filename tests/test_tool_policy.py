"""In-call tool policy unit tests (tiers, caller classes, decide(), guest patterns, doctor)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from pincer.voice import tool_policy as tp
from pincer.voice.engine import CallDirection, CallState


def _settings(**overrides):
    base = {
        "voice_max_writes_per_call": 3,
        "voice_guests_tools_allowed": "",
        "voice_tool_timeout_s": 10,
        "voice_approval_timeout_s": 25,
        "voice_outbound_enabled": True,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _state(kind="appointment", direction=CallDirection.OUTBOUND, settings=None, caller=tp.CALLER_GUEST) -> CallState:
    state = CallState(call_sid="CA_policy", direction=direction, caller_number="+15550001111")
    state.metadata[tp.META_CALLER_CLASS] = caller
    state.metadata[tp.META_ALLOWED_TOOLS] = tp.allowed_tools_for_call(
        settings or _settings(), kind=kind, direction=str(direction), caller_class=caller
    )
    return state


def _pin(state, **modes):
    """Pin per-call modes the way trusted code (the receptionist) does."""
    state.metadata[tp.META_MODE_OVERRIDES] = dict(modes)
    return state


# ── Tier table invariants ────────────────────────────────────────────


def test_default_tier_is_deny():
    assert tp.DEFAULT_TIER == tp.TIER_X
    assert tp.tier_of("anything_at_all") == tp.TIER_X
    assert tp.tier_of("shell_exec") == tp.TIER_X


def test_unknown_tool_denied():
    decision = tp.decide("totally_unknown_tool", _state(), _settings())
    assert decision.action == "deny"
    assert decision.reason == "tier_x"
    assert decision.tier == "X"


@pytest.mark.parametrize("name", ["email_send", "google__delete_event", "make_phone_call", "file_write"])
def test_tier_x_denied_even_when_in_scope(name):
    state = _state()
    # Even a (corrupted) scope containing the tool must not help: tier check is first.
    state.metadata[tp.META_ALLOWED_TOOLS] = frozenset({name})
    decision = tp.decide(name, state, _settings())
    assert decision.action == "deny"
    assert decision.reason == "tier_x"


def test_tier_r_always_executes():
    settings = _settings(voice_max_writes_per_call=0)
    state = _state(settings=settings)
    state.metadata[tp.META_WRITES_USED] = 99  # budget irrelevant for reads
    for name in ("google__list_events", "google__check_freebusy", "contact_lookup"):
        decision = tp.decide(name, state, settings)
        assert decision.action == "execute", name
        assert decision.reason == "tier_r"
        assert decision.tier == "R"


def test_not_in_call_scope_denied():
    settings = _settings()
    state = _state(kind="generic", settings=settings)  # generic guest: no calendar writes by default
    decision = tp.decide("google__create_event", state, settings)
    assert decision.action == "deny"
    assert decision.reason == "not_in_call_scope"


def test_write_budget():
    settings = _settings(voice_max_writes_per_call=2)
    state = _pin(_state(settings=settings), google__create_event="off")
    assert tp.decide("google__create_event", state, settings).action == "execute"
    state.metadata[tp.META_WRITES_USED] = 2
    decision = tp.decide("google__create_event", state, settings)
    assert decision.action == "deny"
    assert decision.reason == "write_budget_exhausted"
    # 0 = writes disabled entirely, for a registered caller too
    zero = _settings(voice_max_writes_per_call=0)
    state0 = _state(kind="generic", settings=zero, caller=tp.CALLER_USER)
    assert tp.decide("send_owner_message", state0, zero, None, lambda _n: False).reason == "write_budget_exhausted"


def test_per_call_override_wins():
    settings = _settings()
    state = _pin(_state(settings=settings), google__create_event="off", send_owner_message="user")
    assert tp.decide("google__create_event", state, settings, {"summary": "x"}).action == "execute"
    assert tp.decide("google__create_event", state, settings, {"summary": "x"}).reason == "mode_off"
    assert tp.decide("send_owner_message", state, settings, {"text": "hi"}).action == "need_user_approval"
    # a guest's tools without an override confirm out loud
    assert tp.decide("memory_note", state, settings, {"note": "n"}).action == "need_verbal"


# ── Caller classes ───────────────────────────────────────────────────


@pytest.mark.parametrize("requires", [None, lambda _n: False, lambda _n: True])
def test_guest_write_always_needs_verbal(requires):
    settings = _settings(voice_guests_tools_allowed="google__*,memory_note")
    state = _state(kind="generic", settings=settings)
    decision = tp.decide("google__create_event", state, settings, {"summary": "Arzt"}, requires)
    assert decision.action == "need_verbal"
    assert decision.mode == "verbal"


def test_guest_read_in_scope_runs_without_asking():
    settings = _settings(voice_guests_tools_allowed="google__list_events")
    state = _state(kind="generic", settings=settings)
    assert tp.decide("google__list_events", state, settings).action == "execute"
    # …but only reads the guest list allows
    assert tp.decide("contact_lookup", state, settings).reason == "not_in_call_scope"


def test_registered_user_follows_approval_policy():
    from pincer.tools.approval import ApprovalPolicy

    policy = ApprovalPolicy()  # "critical": calendar writes run, sending is gated

    def requires(name: str) -> bool:
        return policy.requires_approval(name, declared=True)

    settings = _settings()
    state = _state(kind="generic", settings=settings, caller=tp.CALLER_USER)
    create = tp.decide("google__create_event", state, settings, {"summary": "Arzt"}, requires)
    assert (create.action, create.reason, create.mode) == ("execute", "policy_auto", tp.MODE_POLICY)
    send = tp.decide("send_owner_message", state, settings, {"text": "hi"}, requires)
    assert send.action == "need_verbal"


def test_registered_user_without_policy_confirms_out_loud():
    settings = _settings()
    state = _state(kind="generic", settings=settings, caller=tp.CALLER_USER)
    assert tp.decide("google__create_event", state, settings, {"summary": "x"}).action == "need_verbal"


def test_unknown_caller_class_is_a_guest():
    settings = _settings()
    state = _state(kind="generic", settings=settings, caller=tp.CALLER_USER)
    state.metadata[tp.META_CALLER_CLASS] = "owner?"
    decision = tp.decide("google__create_event", state, settings, {"summary": "x"}, lambda _n: False)
    assert decision.action == "need_verbal"
    assert tp.caller_class_of(CallState(call_sid="CA", direction=CallDirection.INBOUND, caller_number="+1")) == "guest"


def test_fingerprint_invalidated_on_arg_change():
    settings = _settings()
    state = _state(settings=settings)
    args = {"summary": "Beratung", "start": "2026-08-18T14:00:00+02:00", "end": "2026-08-18T14:30:00+02:00"}
    tp.begin_turn(state)
    tp.set_verbal_confirmation(state, "google__create_event", args)
    assert tp.decide("google__create_event", state, settings, dict(args)).action == "execute"
    # Same fingerprint regardless of key order
    reordered = {"end": args["end"], "start": args["start"], "summary": args["summary"]}
    assert tp.decide("google__create_event", state, settings, reordered).action == "execute"
    # Changing the time re-triggers VERIFY
    changed = {**args, "start": "2026-08-18T15:00:00+02:00"}
    assert tp.decide("google__create_event", state, settings, changed).action == "need_verbal"
    # A different tool with the same args is not authorized either
    assert tp.decide("google__update_event", _state(settings=settings), settings, dict(args)).reason in (
        "not_in_call_scope",
        "verbal",
    )


def test_stale_verbal_confirmation_expires():
    settings = _settings()
    state = _state(settings=settings)
    args = {"note": "call partner prefers mornings"}
    tp.begin_turn(state)  # turn 1
    tp.set_verbal_confirmation(state, "memory_note", args)
    assert tp.decide("memory_note", state, settings, args).action == "execute"
    tp.begin_turn(state)  # turn 2 — still valid
    tp.begin_turn(state)  # turn 3 — still valid (ttl = 2 turns after confirmation)
    assert tp.decide("memory_note", state, settings, args).action == "execute"
    tp.begin_turn(state)  # turn 4 — expired
    assert tp.decide("memory_note", state, settings, args).action == "need_verbal"
    assert tp.META_VERBAL_CONFIRMED not in state.metadata


def test_clear_verbal_confirmation_after_execution():
    settings = _settings()
    state = _state(settings=settings)
    tp.set_verbal_confirmation(state, "memory_note", {"note": "x"})
    tp.clear_verbal_confirmation(state)
    assert tp.decide("memory_note", state, settings, {"note": "x"}).action == "need_verbal"


# ── Per-purpose narrowing & schema filtering ─────────────────────────


def test_appointment_scope_exact():
    scope = tp.allowed_tools_for_call(_settings(), kind="appointment")
    assert scope == tp.APPOINTMENT_CALL_TOOLS


def test_generic_guest_scope_has_reads_and_owner_writes_only():
    scope = tp.allowed_tools_for_call(_settings(), kind="generic")
    assert scope == (tp.tier_r_tools() - tp.CALL_OPT_IN_TOOLS) | tp.GENERIC_CALL_WRITE_TOOLS
    assert "google__create_event" not in scope
    assert "google__update_event" not in scope


def test_generic_registered_user_gets_every_callable_tool():
    for direction in ("outbound", "inbound"):
        scope = tp.allowed_tools_for_call(_settings(), kind="generic", direction=direction, caller_class="user")
        assert scope == tp.callable_tools()
        assert "google__create_event" in scope
        assert "google__delete_event" not in scope
        assert "websearch__search" in scope


def test_websearch_is_tier_r():
    assert tp.tier_of("websearch__search") == tp.TIER_R


@pytest.mark.parametrize("kind", ["generic", "appointment", "receptionist"])
@pytest.mark.parametrize("direction", ["outbound", "inbound"])
def test_websearch_is_not_in_any_default_guest_scope(kind, direction):
    assert "websearch__search" not in tp.allowed_tools_for_call(_settings(), kind=kind, direction=direction)


@pytest.mark.parametrize("direction", ["outbound", "inbound"])
def test_registered_user_gets_websearch_and_it_runs_as_tier_r(direction):
    settings = _settings()
    state = _state(kind="generic", direction=direction, settings=settings, caller=tp.CALLER_USER)
    assert "websearch__search" in state.metadata[tp.META_ALLOWED_TOOLS]
    decision = tp.decide("websearch__search", state, settings, {"query": "Benzinpreis Bünde"})
    assert (decision.action, decision.reason) == ("execute", "tier_r")


@pytest.mark.parametrize("entry", ["websearch__search", "websearch__*"])
@pytest.mark.parametrize("direction", ["outbound", "inbound"])
def test_websearch_joins_the_guest_scope_through_the_guest_list(entry, direction):
    settings = _settings(voice_guests_tools_allowed=f"google__list_events,{entry}")
    state = _state(kind="generic", direction=direction, settings=settings)
    assert "websearch__search" in state.metadata[tp.META_ALLOWED_TOOLS]
    assert tp.unmatched_guest_tool_entries(settings.voice_guests_tools_allowed) == []
    decision = tp.decide("websearch__search", state, settings, {"query": "Benzinpreis Bünde"})
    assert (decision.action, decision.reason) == ("execute", "tier_r")


@pytest.mark.parametrize("caller", [tp.CALLER_GUEST, tp.CALLER_USER])
def test_websearch_never_reaches_appointment_calls_or_the_receptionist(caller):
    settings = _settings(voice_guests_tools_allowed="websearch__search")
    assert "websearch__search" not in tp.allowed_tools_for_call(settings, kind="appointment", caller_class=caller)
    for direction in ("outbound", "inbound"):
        scope = tp.allowed_tools_for_call(settings, kind="receptionist", direction=direction, caller_class=caller)
        assert "websearch__search" not in scope


def test_inbound_excludes_memory_search():
    scope = tp.allowed_tools_for_call(_settings(), kind="generic", direction="inbound")
    assert "memory_search" not in scope
    assert "memory_search" in tp.allowed_tools_for_call(_settings(), kind="generic", direction="outbound")
    guest = _settings(voice_guests_tools_allowed="memory_*")
    assert "memory_search" not in tp.allowed_tools_for_call(guest, kind="generic", direction="inbound")


def test_google_pattern_expands_to_rw_calendar_tools_only():
    assert tp.match_tool_patterns("google__*") == {
        "google__list_events",
        "google__check_freebusy",
        "google__create_event",
        "google__update_event",
    }
    assert "google__delete_event" not in tp.match_tool_patterns("google__*,google__delete_event,*")


def test_guest_list_replaces_the_default_scope():
    settings = _settings(voice_guests_tools_allowed="google__*, send_owner_message")
    scope = tp.allowed_tools_for_call(settings, kind="generic")
    assert scope == tp.match_tool_patterns("google__*") | {"send_owner_message"}
    assert "memory_note" not in scope  # replaced, not widened
    assert "contact_lookup" not in scope


def test_guest_list_never_widens_the_allowlist():
    settings = _settings(voice_guests_tools_allowed="google__create_event, email_send, shell_exec, unknown_tool, *")
    scope = tp.allowed_tools_for_call(settings, kind="generic")
    assert scope == tp.callable_tools()
    for name in ("email_send", "shell_exec", "unknown_tool", "google__delete_event"):
        assert name not in scope
    assert tp.unmatched_guest_tool_entries(settings.voice_guests_tools_allowed) == [
        "email_send",
        "shell_exec",
        "unknown_tool",
    ]


def test_fixed_scopes_ignore_caller_and_guest_list():
    settings = _settings(voice_guests_tools_allowed="google__*")
    for caller in ("user", "guest"):
        assert tp.allowed_tools_for_call(settings, kind="appointment", caller_class=caller) == tp.APPOINTMENT_CALL_TOOLS
        assert (
            tp.allowed_tools_for_call(settings, kind="receptionist", direction="inbound", caller_class=caller)
            == tp.INBOUND_RECEPTIONIST_TOOLS
        )


def test_call_context_tool_schemas_exact():
    registry = [
        {"name": n}
        for n in (
            "shell_exec",
            "google__list_events",
            "google__check_freebusy",
            "google__create_event",
            "google__delete_event",
            "email_send",
            "contact_lookup",
            "send_owner_message",
            "memory_note",
            "memory_search",
            "make_phone_call",
            "some_mcp__tool",
        )
    ]
    scope = tp.allowed_tools_for_call(_settings(), kind="appointment")
    visible = [s["name"] for s in tp.call_context_schemas(registry, scope)]
    assert visible == [
        "google__list_events",
        "google__check_freebusy",
        "google__create_event",
        "contact_lookup",
        "send_owner_message",
        "memory_note",
    ]
    # Registry order preserved, no X tool, nothing outside scope.
    assert "memory_search" not in visible  # not in the appointment scope
    assert tp.call_context_schemas(registry, None) == []


# ── Doctor checks ────────────────────────────────────────────────────


def _doctor(tmp_path):
    from pincer.security.doctor import SecurityDoctor

    return SecurityDoctor(data_dir=tmp_path, config_dir=tmp_path, skills_dir=tmp_path)


def test_doctor_legacy_voice_vars_warn(tmp_path, monkeypatch):
    from pincer.security.doctor import CheckStatus

    for name in ("PINCER_VOICE_TOOLS_EXTRA", "PINCER_VOICE_TOOL_APPROVAL", "PINCER_VOICE_TOOL_APPROVAL_OVERRIDES"):
        monkeypatch.delenv(name, raising=False)
    doc = _doctor(tmp_path)
    assert doc._check_voice_legacy_tool_vars().status == CheckStatus.PASS
    monkeypatch.setenv("PINCER_VOICE_TOOLS_EXTRA", "google__create_event")
    result = doc._check_voice_legacy_tool_vars()
    assert result.status == CheckStatus.WARNING
    assert "PINCER_VOICE_TOOLS_EXTRA" in result.message
    monkeypatch.delenv("PINCER_VOICE_TOOLS_EXTRA")
    (tmp_path / ".env").write_text("PINCER_VOICE_TOOL_APPROVAL=off\n")
    assert "PINCER_VOICE_TOOL_APPROVAL" in doc._check_voice_legacy_tool_vars().message


def test_doctor_tier_x_reachable_self_test(tmp_path, monkeypatch):
    from pincer.security.doctor import CheckStatus

    doc = _doctor(tmp_path)
    assert doc._check_voice_tier_x_reachable(_settings()).status == CheckStatus.PASS
    assert doc._check_voice_tier_x_reachable(_settings(voice_guests_tools_allowed="google__*")).status == (
        CheckStatus.PASS
    )
    assert doc._check_voice_tier_x_reachable(_settings(voice_guests_tools_allowed="websearch__search")).status == (
        CheckStatus.PASS
    )
    unmatched = doc._check_voice_tier_x_reachable(_settings(voice_guests_tools_allowed="google__*,email_send"))
    assert unmatched.status == CheckStatus.WARNING
    assert "email_send" in unmatched.message
    # Simulate a regression in the filter: the check must go CRITICAL.
    monkeypatch.setattr(tp, "call_context_schemas", lambda schemas, allowed: list(schemas))
    assert doc._check_voice_tier_x_reachable(_settings()).status == CheckStatus.CRITICAL


def test_doctor_warns_when_no_identity_has_phones(tmp_path):
    from pincer.security.doctor import CheckStatus

    doc = _doctor(tmp_path)
    voice = _settings(voice_enabled=True, identity_map="")
    assert doc._check_voice_registered_phones(_settings(voice_enabled=False)).status == CheckStatus.SKIPPED
    assert doc._check_voice_registered_phones(voice).status == CheckStatus.WARNING
    (tmp_path / "pincer.toml").write_text(
        '[identity.owner]\nphones = ["+491512345678"]\n[[identity.owner.channels]]\n'
        'channel = "telegram"\nchannel_user_id = 1\n'
    )
    result = doc._check_voice_registered_phones(voice)
    assert result.status == CheckStatus.PASS
    assert "owner" in result.message


def test_removed_settings_are_gone():
    from pincer.config import Settings

    fields = Settings.model_fields
    assert "voice_guests_tools_allowed" in fields
    for name in ("voice_tools_extra", "voice_tool_approval", "voice_tool_approval_overrides"):
        assert name not in fields
