"""Sprint 11 PR-1 — in-call tool policy unit tests (tiers, decide(), config parsing, doctor)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from pincer.exceptions import ConfigError
from pincer.voice import tool_policy as tp
from pincer.voice.engine import CallDirection, CallState


def _settings(**overrides):
    base = {
        "voice_tool_approval": "verbal",
        "voice_tool_approval_overrides": "",
        "voice_max_writes_per_call": 3,
        "voice_tools_extra": "",
        "voice_tool_timeout_s": 10,
        "voice_approval_timeout_s": 25,
        "voice_outbound_enabled": True,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _state(kind="appointment", direction=CallDirection.OUTBOUND, settings=None) -> CallState:
    state = CallState(call_sid="CA_policy", direction=direction, caller_number="+15550001111")
    state.metadata[tp.META_ALLOWED_TOOLS] = tp.allowed_tools_for_call(
        settings or _settings(), kind=kind, direction=str(direction)
    )
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
    settings = _settings(voice_tool_approval="user", voice_max_writes_per_call=0)
    state = _state(settings=settings)
    state.metadata[tp.META_WRITES_USED] = 99  # budget irrelevant for reads
    for name in ("google__list_events", "google__check_freebusy", "contact_lookup"):
        decision = tp.decide(name, state, settings)
        assert decision.action == "execute", name
        assert decision.reason == "tier_r"
        assert decision.tier == "R"


def test_not_in_call_scope_denied():
    settings = _settings()
    state = _state(kind="generic", settings=settings)  # generic: no calendar writes by default
    decision = tp.decide("google__create_event", state, settings)
    assert decision.action == "deny"
    assert decision.reason == "not_in_call_scope"


def test_write_budget():
    settings = _settings(voice_tool_approval="off", voice_max_writes_per_call=2)
    state = _state(settings=settings)
    assert tp.decide("google__create_event", state, settings).action == "execute"
    state.metadata[tp.META_WRITES_USED] = 2
    decision = tp.decide("google__create_event", state, settings)
    assert decision.action == "deny"
    assert decision.reason == "write_budget_exhausted"
    # 0 = writes disabled entirely
    zero = _settings(voice_tool_approval="off", voice_max_writes_per_call=0)
    state0 = _state(settings=zero)
    assert tp.decide("send_owner_message", state0, zero).reason == "write_budget_exhausted"


def test_override_precedence():
    settings = _settings(
        voice_tool_approval="verbal",
        voice_tool_approval_overrides="google__create_event:off,send_owner_message:user",
    )
    state = _state(settings=settings)
    assert tp.decide("google__create_event", state, settings, {"summary": "x"}).action == "execute"
    assert tp.decide("google__create_event", state, settings, {"summary": "x"}).reason == "mode_off"
    assert tp.decide("send_owner_message", state, settings, {"text": "hi"}).action == "need_user_approval"
    # global mode still applies to tools without an override
    assert tp.decide("memory_note", state, settings, {"note": "n"}).action == "need_verbal"


def test_override_tier_x_config_error():
    with pytest.raises(ConfigError, match="excluded from calls"):
        tp.validate_overrides("email_send:off")
    with pytest.raises(ConfigError, match="excluded from calls"):
        tp.validate_overrides("google__delete_event:verbal")
    # A Settings() object fails at construction — fail fast, not fallback
    from pincer.config import Settings

    with pytest.raises(ConfigError):
        Settings(anthropic_api_key="sk-ant-test", voice_tool_approval_overrides="make_phone_call:off")


def test_unknown_mode_config_error():
    with pytest.raises(ConfigError):
        tp.validate_approval_mode("sometimes")
    with pytest.raises(ConfigError):
        tp.parse_overrides("google__create_event:maybe")
    with pytest.raises(ConfigError):
        tp.parse_overrides("google__create_event")  # no mode
    from pincer.config import Settings

    with pytest.raises(ConfigError):
        Settings(anthropic_api_key="sk-ant-test", voice_tool_approval="sometimes")


def test_unknown_override_tool_is_ignored_not_fatal():
    # Parsing accepts it; runtime ignores it; doctor warns.
    overrides = tp.validate_overrides("no_such_tool:off")
    assert overrides == {"no_such_tool": "off"}
    assert tp.unknown_override_tools("no_such_tool:off,google__create_event:user") == ["no_such_tool"]


def test_fingerprint_invalidated_on_arg_change():
    settings = _settings(voice_tool_approval="verbal")
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
    settings = _settings(voice_tool_approval="verbal")
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


def test_generic_scope_has_reads_and_owner_writes_only():
    scope = tp.allowed_tools_for_call(_settings(), kind="generic")
    assert tp.tier_r_tools() <= scope
    assert {"send_owner_message", "memory_note"} <= scope
    assert "google__create_event" not in scope
    assert "google__update_event" not in scope


def test_inbound_excludes_memory_search():
    scope = tp.allowed_tools_for_call(_settings(), kind="generic", direction="inbound")
    assert "memory_search" not in scope
    assert "memory_search" in tp.allowed_tools_for_call(_settings(), kind="generic", direction="outbound")


def test_extra_widens_scope_but_never_the_allowlist():
    settings = _settings(voice_tools_extra="google__create_event, email_send, shell_exec, unknown_tool")
    scope = tp.allowed_tools_for_call(settings, kind="generic")
    assert "google__create_event" in scope
    for name in ("email_send", "shell_exec", "unknown_tool"):
        assert name not in scope
    assert tp.ignored_extra_tools(settings.voice_tools_extra) == ["email_send", "shell_exec", "unknown_tool"]


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


def test_doctor_autonomy_warns_on_off(tmp_path):
    from pincer.security.doctor import CheckStatus

    doc = _doctor(tmp_path)
    assert doc._check_voice_autonomy_on(_settings(voice_tool_approval="off")).status == CheckStatus.WARNING
    assert doc._check_voice_autonomy_on(_settings(voice_tool_approval="verbal")).status == CheckStatus.PASS
    per_tool = _settings(voice_tool_approval_overrides="google__create_event:off")
    assert doc._check_voice_autonomy_on(per_tool).status == CheckStatus.WARNING
    assert doc._check_voice_autonomy_on(_settings(voice_outbound_enabled=False)).status == CheckStatus.SKIPPED


def test_doctor_tier_x_reachable_self_test(tmp_path, monkeypatch):
    from pincer.security.doctor import CheckStatus

    doc = _doctor(tmp_path)
    assert doc._check_voice_tier_x_reachable(_settings()).status == CheckStatus.PASS
    assert doc._check_voice_tier_x_reachable(_settings(voice_tools_extra="email_send")).status == CheckStatus.WARNING
    # Simulate a regression in the filter: the check must go CRITICAL.
    monkeypatch.setattr(tp, "call_context_schemas", lambda schemas, allowed: list(schemas))
    assert doc._check_voice_tier_x_reachable(_settings()).status == CheckStatus.CRITICAL


def test_doctor_override_unknown_tool(tmp_path):
    from pincer.security.doctor import CheckStatus

    doc = _doctor(tmp_path)
    assert doc._check_voice_override_unknown_tool(_settings()).status == CheckStatus.PASS
    result = doc._check_voice_override_unknown_tool(_settings(voice_tool_approval_overrides="nope_tool:off"))
    assert result.status == CheckStatus.WARNING
    assert "nope_tool" in result.message
    ok = doc._check_voice_override_unknown_tool(_settings(voice_tool_approval_overrides="memory_note:user"))
    assert ok.status == CheckStatus.PASS
