"""Approval policy: which declared-write tools actually interrupt the user."""

from __future__ import annotations

import ast
import pathlib

import pytest

from pincer.tools.approval import (
    APPROVAL_MODES,
    NON_CRITICAL_TOOLS,
    ApprovalPolicy,
    is_critical,
    parse_tool_names,
)
from pincer.tools.registry import ToolRegistry

SRC = pathlib.Path(__file__).resolve().parents[1] / "src"


async def _noop() -> str:
    return "ok"


def _registry(policy: ApprovalPolicy | None = None) -> ToolRegistry:
    reg = ToolRegistry(policy)
    reg.register(name="google__send_message", description="send", handler=_noop, require_approval=True)
    reg.register(name="google__create_event", description="book", handler=_noop, require_approval=True)
    reg.register(name="file_read", description="read", handler=_noop)
    return reg


# ── Classification ────────────────────────────────────────


def test_unknown_tools_are_critical() -> None:
    """Fail-closed: a tool nobody classified keeps its gate."""
    assert is_critical("some__brand_new_tool")
    assert is_critical("google__delete_event")


def test_listed_tools_are_not_critical() -> None:
    assert not is_critical("google__create_event")
    assert not is_critical("slack__set_channel_topic")


@pytest.mark.parametrize(
    "name",
    [
        "email_send",
        "google__send_message",
        "google__trash_file",
        "google__clear_sheet_values",
        "google__share_file",
        "google__remove_meet_member",
        "slack__post_message",
        "slack__delete_message",
        "slack__kick_from_channel",
        "outlook__forward_message",
        "onedrive__delete_file",
        "make_phone_call",
        "run_skill_script",
        "shell_exec",
        "send_owner_message",
    ],
)
def test_critical_tools_stay_gated(name: str) -> None:
    assert is_critical(name), f"{name} must keep its approval gate"


# Verbs whose tools must never be ungated. Matched as whole underscore-separated
# tokens, so the restorative inverses ("untrash", "unarchive") do not trip this.
_FORBIDDEN_VERBS = frozenset(
    {
        "delete",
        "trash",
        "empty",
        "kick",
        "disable",
        "revoke",
        "clear",
        "exec",
        "send",
        "reply",
        "forward",
        "post",
        "share",
        "invite",
    }
)

# Deliberate exemptions, each with a reason. Empty is the goal.
_VERB_GUARD_EXEMPT: frozenset[str] = frozenset()


def test_allowlist_excludes_destructive_verbs() -> None:
    """Guard against a careless addition to NON_CRITICAL_TOOLS.

    Anything that deletes, empties, sends outward or executes belongs behind
    the gate no matter how convenient it would be to skip it.
    """
    offenders = []
    for name in NON_CRITICAL_TOOLS:
        if name in _VERB_GUARD_EXEMPT:
            continue
        tokens = set(name.replace("__", "_").split("_"))
        if tokens & _FORBIDDEN_VERBS:
            offenders.append(name)
    assert sorted(offenders) == [], f"destructive/outward tools must not be ungated: {sorted(offenders)}"


def test_verb_guard_actually_catches_a_bad_entry() -> None:
    """The guard above is only useful if its matching works."""
    tokens = set("google__delete_event".replace("__", "_").split("_"))
    assert tokens & _FORBIDDEN_VERBS
    # ...and does not fire on the restorative inverse
    assert not set("google__untrash_message".replace("__", "_").split("_")) & _FORBIDDEN_VERBS


def test_meet_membership_and_moderation_stay_gated() -> None:
    """Not caught by the verb guard — assert the classification directly."""
    for name in (
        "google__add_meet_member",
        "google__remove_meet_member",
        "google__configure_meet_moderation",
        "google__configure_meet_artifacts",
        "google__end_active_conference",
        "slack__archive_channel",
        "slack__upload_file",
        "slack__update_message",
        "email_trash",
        "email_empty_folder",
    ):
        assert is_critical(name), f"{name} must keep its approval gate"


def test_allowlist_only_names_tools_that_declare_approval() -> None:
    """Every entry should correspond to a real `require_approval=True` registration.

    Keeps the list from silently rotting as tools are renamed.
    """
    declared: set[str] = set()
    for path in SRC.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            fname = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            if fname != "register":
                continue
            kw = {k.arg: k.value for k in node.keywords if k.arg}
            ra = kw.get("require_approval")
            if not (isinstance(ra, ast.Constant) and ra.value is True):
                continue
            name_node = kw.get("name") or (node.args[0] if node.args else None)
            if isinstance(name_node, ast.Constant) and isinstance(name_node.value, str):
                declared.add(name_node.value)

    stale = NON_CRITICAL_TOOLS - declared
    assert stale == set(), f"NON_CRITICAL_TOOLS names no longer registered with approval: {sorted(stale)}"


# ── Policy resolution ─────────────────────────────────────


def test_default_mode_is_critical() -> None:
    assert ApprovalPolicy().mode == "critical"
    assert "critical" in APPROVAL_MODES


def test_critical_mode_gates_sends_but_not_bookings() -> None:
    reg = _registry()
    assert reg.requires_approval("google__send_message") is True
    assert reg.requires_approval("google__create_event") is False


def test_reads_never_require_approval() -> None:
    reg = _registry()
    assert reg.requires_approval("file_read") is False


def test_mode_all_restores_declared_behaviour() -> None:
    reg = _registry(ApprovalPolicy(mode="all"))
    assert reg.requires_approval("google__send_message") is True
    assert reg.requires_approval("google__create_event") is True
    assert reg.requires_approval("file_read") is False


def test_mode_none_gates_nothing() -> None:
    reg = _registry(ApprovalPolicy(mode="none"))
    assert reg.requires_approval("google__send_message") is False
    assert reg.requires_approval("google__create_event") is False


def test_never_override_ungates_a_critical_tool() -> None:
    reg = _registry(ApprovalPolicy(never=frozenset({"google__send_message"})))
    assert reg.requires_approval("google__send_message") is False


def test_always_override_gates_a_non_critical_tool() -> None:
    reg = _registry(ApprovalPolicy(always=frozenset({"google__create_event"})))
    assert reg.requires_approval("google__create_event") is True


def test_always_override_can_gate_an_undeclared_tool() -> None:
    reg = _registry(ApprovalPolicy(always=frozenset({"file_read"})))
    assert reg.requires_approval("file_read") is True


def test_unknown_tool_name_is_not_approved() -> None:
    assert _registry().requires_approval("nope") is False


# ── Declared flag stays intact ────────────────────────────


def test_declares_approval_ignores_policy() -> None:
    """MCP export and audit read intent, not the local convenience policy."""
    reg = _registry(ApprovalPolicy(mode="none"))
    assert reg.declares_approval("google__create_event") is True
    assert reg.declares_approval("file_read") is False
    assert reg.requires_approval("google__create_event") is False


def test_set_approval_policy_swaps_behaviour() -> None:
    reg = _registry()
    assert reg.requires_approval("google__create_event") is False
    reg.set_approval_policy(ApprovalPolicy(mode="all"))
    assert reg.requires_approval("google__create_event") is True


# ── Settings plumbing ─────────────────────────────────────


def test_from_settings_reads_comma_separated_fields() -> None:
    class Cfg:
        approval_policy = "all"
        approval_always = "a, b"
        approval_never = "c"

    policy = ApprovalPolicy.from_settings(Cfg())
    assert policy.mode == "all"
    assert policy.always == frozenset({"a", "b"})
    assert policy.never == frozenset({"c"})


def test_from_settings_accepts_a_real_list_too() -> None:
    class Cfg:
        approval_policy = "critical"
        approval_always = ["a", "b"]
        approval_never: list[str] = []

    policy = ApprovalPolicy.from_settings(Cfg())
    assert policy.always == frozenset({"a", "b"})
    assert policy.never == frozenset()


def test_from_settings_tolerates_missing_and_bogus_fields() -> None:
    class Bare:
        pass

    assert ApprovalPolicy.from_settings(Bare()).mode == "critical"

    class Bogus:
        approval_policy = "banana"
        approval_always = None
        approval_never = 42

    policy = ApprovalPolicy.from_settings(Bogus())
    assert policy.mode == "critical"
    assert policy.always == frozenset()
    assert policy.never == frozenset()


def test_parse_tool_names_handles_whitespace_and_blanks() -> None:
    assert parse_tool_names(" a , , b ") == frozenset({"a", "b"})
    assert parse_tool_names("") == frozenset()
    assert parse_tool_names(None) == frozenset()


def test_settings_rejects_invalid_policy() -> None:
    from pydantic import ValidationError

    from pincer.config.tools import ToolSettings

    with pytest.raises(ValidationError):
        ToolSettings(approval_policy="banana")

    assert ToolSettings(approval_policy="none").approval_policy == "none"
    assert ToolSettings().approval_policy == "critical"


def test_overrides_round_trip_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The documented env form must actually load — comma-separated, not JSON."""
    from pincer.config import get_settings_relaxed

    monkeypatch.setenv("PINCER_APPROVAL_POLICY", "critical")
    monkeypatch.setenv("PINCER_APPROVAL_NEVER", "email_send, google__send_message")
    get_settings_relaxed.cache_clear()
    try:
        policy = ApprovalPolicy.from_settings(get_settings_relaxed())
        assert policy.never == frozenset({"email_send", "google__send_message"})
        assert policy.requires_approval("email_send", declared=True) is False
    finally:
        get_settings_relaxed.cache_clear()
