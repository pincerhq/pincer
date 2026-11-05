"""Tests for the security doctor."""

import asyncio
from types import SimpleNamespace

import pytest

from pincer.security.doctor import CheckStatus, DoctorReport, SecurityDoctor


@pytest.fixture
def doctor_env(tmp_path):
    """Create a minimal doctor environment."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    return SecurityDoctor(
        data_dir=data_dir,
        config_dir=config_dir,
        skills_dir=skills_dir,
    )


def test_run_all_returns_report(doctor_env):
    report = doctor_env.run_all()
    assert isinstance(report, DoctorReport)
    # 31 original + 7 MCP + 3 MCP security + 1 WA neonize + 3 voice DACH
    # + 1 ElevenLabs + 4 voice security (Sprint 8) + 2 observability (Sprint 9)
    # + 3 in-call tool execution (Sprint 11, #299) + 2 receptionist (Sprint 12)
    # + 1 live listen-in announce gate (Sprint 15) + 1 schedule action types (issue #217)
    # + 1 leftover shared API tokens (issue #228) + 1 Google Workspace token (issue #297)
    # + 1 identity management (issue #348) + 4 LLM effort and thinking (issue #345)
    assert len(report.checks) == 69

    assert 0 <= report.score <= 100


def test_env_file_permissions_pass(tmp_path):
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    env_file = config_dir / ".env"
    env_file.write_text("KEY=value")
    env_file.chmod(0o600)

    doc = SecurityDoctor(config_dir=config_dir, data_dir=tmp_path)
    result = doc._check_env_file_permissions()
    assert result.status == CheckStatus.PASS


def test_env_file_permissions_critical(tmp_path):
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    env_file = config_dir / ".env"
    env_file.write_text("KEY=value")
    env_file.chmod(0o644)

    doc = SecurityDoctor(config_dir=config_dir, data_dir=tmp_path)
    result = doc._check_env_file_permissions()
    assert result.status == CheckStatus.CRITICAL


def test_env_file_missing_skipped(tmp_path):
    doc = SecurityDoctor(config_dir=tmp_path, data_dir=tmp_path)
    result = doc._check_env_file_permissions()
    assert result.status == CheckStatus.SKIPPED


def test_gitignore_has_env_pass(tmp_path):
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    (config_dir / ".gitignore").write_text(".env\ndata/\n")

    doc = SecurityDoctor(config_dir=config_dir, data_dir=tmp_path)
    result = doc._check_gitignore_has_env()
    assert result.status == CheckStatus.PASS


def test_gitignore_missing_env_critical(tmp_path):
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    (config_dir / ".gitignore").write_text("*.pyc\n")

    doc = SecurityDoctor(config_dir=config_dir, data_dir=tmp_path)
    result = doc._check_gitignore_has_env()
    assert result.status == CheckStatus.CRITICAL


def test_no_hardcoded_secrets_pass(tmp_path):
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    src_dir = config_dir / "src"
    src_dir.mkdir()
    (src_dir / "app.py").write_text('api_key = os.environ["KEY"]\n')

    doc = SecurityDoctor(config_dir=config_dir, data_dir=tmp_path)
    result = doc._check_no_hardcoded_secrets()
    assert result.status == CheckStatus.PASS


def test_no_hardcoded_secrets_critical(tmp_path):
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    src_dir = config_dir / "src"
    src_dir.mkdir()
    fake_key = "sk-ant-" + "a" * 24
    (src_dir / "bad.py").write_text(f'api_key = "{fake_key}"\n')

    doc = SecurityDoctor(config_dir=config_dir, data_dir=tmp_path)
    result = doc._check_no_hardcoded_secrets()
    assert result.status == CheckStatus.CRITICAL


def test_python_version_pass():
    doc = SecurityDoctor()
    result = doc._check_python_version()
    assert result.status == CheckStatus.PASS


def test_not_running_as_root():
    doc = SecurityDoctor()
    result = doc._check_not_running_as_root()
    # In test environment, we should not be root
    assert result.status == CheckStatus.PASS


def test_dashboard_not_exposed_default():
    from unittest.mock import MagicMock

    doc = SecurityDoctor()
    cfg = MagicMock()
    cfg.dashboard_host = "127.0.0.1"
    result = doc._check_dashboard_not_exposed(cfg)
    assert result.status == CheckStatus.PASS


def test_report_score():
    report = DoctorReport()
    report.checks.append(SecurityDoctor()._check_python_version())
    report.checks.append(SecurityDoctor()._check_not_running_as_root())
    assert report.score == 100
    assert report.passed == 2
    assert report.critical == 0


def test_report_to_dict():
    doc = SecurityDoctor()
    report = doc.run_all()
    d = report.to_dict()
    assert "score" in d
    assert "checks" in d
    assert isinstance(d["checks"], list)
    assert all("name" in c and "status" in c for c in d["checks"])


def test_sqlite_world_readable(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_file = data_dir / "test.db"
    db_file.write_text("")
    db_file.chmod(0o644)

    doc = SecurityDoctor(data_dir=data_dir, config_dir=tmp_path)
    result = doc._check_sqlite_not_world_readable()
    assert result.status in (CheckStatus.PASS, CheckStatus.CRITICAL)


# ── New MCP checks (Sprint 3) ─────────────────────────────────────────────────


def test_mcp_env_vars_skipped_no_toml(tmp_path):
    doc = SecurityDoctor(config_dir=tmp_path)
    result = doc._check_mcp_env_vars()
    assert result.status == CheckStatus.SKIPPED


def test_mcp_env_vars_pass_no_refs(tmp_path):
    toml = tmp_path / "pincer.toml"
    toml.write_text("[mcp]\nenabled = true\n")
    doc = SecurityDoctor(config_dir=tmp_path)
    result = doc._check_mcp_env_vars()
    assert result.status == CheckStatus.PASS


def test_mcp_env_vars_pass_all_set(tmp_path, monkeypatch):
    toml = tmp_path / "pincer.toml"
    toml.write_text(
        "[mcp]\nenabled = true\n\n[[mcp.servers]]\n"
        'name = "s"\ntransport = "stdio"\ncommand = "echo"\n'
        'env = {TOKEN = "${MY_TOKEN}"}\n'
    )
    monkeypatch.setenv("MY_TOKEN", "abc123")
    doc = SecurityDoctor(config_dir=tmp_path)
    result = doc._check_mcp_env_vars()
    assert result.status == CheckStatus.PASS


def test_mcp_env_vars_warning_unset(tmp_path, monkeypatch):
    toml = tmp_path / "pincer.toml"
    toml.write_text(
        "[mcp]\nenabled = true\n\n[[mcp.servers]]\n"
        'name = "s"\ntransport = "stdio"\ncommand = "echo"\n'
        'env = {TOKEN = "${MISSING_VAR_XYZ}"}\n'
    )
    monkeypatch.delenv("MISSING_VAR_XYZ", raising=False)
    doc = SecurityDoctor(config_dir=tmp_path)
    result = doc._check_mcp_env_vars()
    assert result.status == CheckStatus.WARNING
    assert "MISSING_VAR_XYZ" in result.message


def test_mcp_collisions_skipped_no_servers(tmp_path):
    doc = SecurityDoctor(config_dir=tmp_path)
    result = doc._check_mcp_collisions()
    assert result.status == CheckStatus.SKIPPED


def test_mcp_collisions_pass_single_server(tmp_path):
    toml = tmp_path / "pincer.toml"
    toml.write_text(
        "[mcp]\nenabled = true\ntool_prefix = false\n\n"
        '[[mcp.servers]]\nname = "s"\ntransport = "stdio"\ncommand = "echo"\n'
    )
    doc = SecurityDoctor(config_dir=tmp_path)
    result = doc._check_mcp_collisions()
    assert result.status == CheckStatus.PASS


def test_mcp_collisions_warning_multi_no_prefix(tmp_path):
    toml = tmp_path / "pincer.toml"
    toml.write_text(
        "[mcp]\nenabled = true\ntool_prefix = false\n\n"
        '[[mcp.servers]]\nname = "a"\ntransport = "stdio"\ncommand = "echo"\n\n'
        '[[mcp.servers]]\nname = "b"\ntransport = "stdio"\ncommand = "echo"\n'
    )
    doc = SecurityDoctor(config_dir=tmp_path)
    result = doc._check_mcp_collisions()
    assert result.status == CheckStatus.WARNING


def test_mcp_collisions_pass_multi_with_prefix(tmp_path):
    toml = tmp_path / "pincer.toml"
    toml.write_text(
        "[mcp]\nenabled = true\ntool_prefix = true\n\n"
        '[[mcp.servers]]\nname = "a"\ntransport = "stdio"\ncommand = "echo"\n\n'
        '[[mcp.servers]]\nname = "b"\ntransport = "stdio"\ncommand = "echo"\n'
    )
    doc = SecurityDoctor(config_dir=tmp_path)
    result = doc._check_mcp_collisions()
    assert result.status == CheckStatus.PASS


def test_mcp_servers_skipped_no_servers(tmp_path):
    doc = SecurityDoctor(config_dir=tmp_path)
    result = doc._check_mcp_servers()
    assert result.status == CheckStatus.SKIPPED


def test_mcp_servers_pass_command_exists(tmp_path):
    toml = tmp_path / "pincer.toml"
    toml.write_text('[mcp]\nenabled = true\n\n[[mcp.servers]]\nname = "s"\ntransport = "stdio"\ncommand = "echo"\n')
    doc = SecurityDoctor(config_dir=tmp_path)
    result = doc._check_mcp_servers()
    assert result.status == CheckStatus.PASS


def test_mcp_servers_warning_command_missing(tmp_path):
    toml = tmp_path / "pincer.toml"
    toml.write_text(
        "[mcp]\nenabled = true\n\n[[mcp.servers]]\n"
        'name = "s"\ntransport = "stdio"\ncommand = "nonexistent-binary-xyzzy-99"\n'
    )
    doc = SecurityDoctor(config_dir=tmp_path)
    result = doc._check_mcp_servers()
    assert result.status == CheckStatus.WARNING
    assert "nonexistent-binary-xyzzy-99" in result.message


def test_mcp_servers_skips_http_transport(tmp_path):
    """HTTP servers don't have a local command to check."""
    toml = tmp_path / "pincer.toml"
    toml.write_text(
        "[mcp]\nenabled = true\n\n[[mcp.servers]]\n"
        'name = "s"\ntransport = "streamable-http"\nurl = "http://localhost:8000"\n'
    )
    doc = SecurityDoctor(config_dir=tmp_path)
    result = doc._check_mcp_servers()
    assert result.status == CheckStatus.PASS


def test_whatsapp_neonize_version_passes_on_recent(monkeypatch):
    """A neonize version at or above the minimum is a PASS."""
    import sys
    from types import ModuleType

    fake = ModuleType("neonize")
    fake.__version__ = "0.6.1"  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "neonize", fake)

    result = SecurityDoctor()._check_whatsapp_neonize_version()
    assert result.status == CheckStatus.PASS
    assert "0.5.2" in result.message


def test_whatsapp_neonize_version_warns_on_old(monkeypatch):
    import sys
    from types import ModuleType

    fake = ModuleType("neonize")
    fake.__version__ = "0.3.14"  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "neonize", fake)

    result = SecurityDoctor()._check_whatsapp_neonize_version()
    assert result.status == CheckStatus.WARNING
    assert "err-client-outdated" in result.message
    assert "neonize" in result.fix_hint


def test_whatsapp_neonize_version_skipped_when_missing(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def blocked_import(name, *a, **kw):
        if name == "neonize":
            raise ImportError("blocked for test")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", blocked_import)
    result = SecurityDoctor()._check_whatsapp_neonize_version()
    assert result.status == CheckStatus.SKIPPED


# ── Regression: #117 — doctor reads .env via settings, not os.environ ────────


def test_stale_shared_tokens_are_found_in_dotenv_only(tmp_path, monkeypatch):
    """Same root cause as #117: a variable set only in .env (not in the shell)
    must still be seen. The shared tokens were removed in #228; one left in
    .env protects nothing and the operator should be told."""
    (tmp_path / ".env").write_text("PINCER_DASHBOARD_TOKEN=a-secure-32-char-token-for-test\n")
    monkeypatch.delenv("PINCER_DASHBOARD_TOKEN", raising=False)
    monkeypatch.delenv("PINCER_WEB_CHAT_TOKEN", raising=False)

    result = SecurityDoctor(config_dir=tmp_path, data_dir=tmp_path)._check_stale_shared_tokens()
    assert result.status == CheckStatus.WARNING
    assert "PINCER_DASHBOARD_TOKEN" in result.message
    assert "PINCER_WEB_CHAT_TOKEN" not in result.message
    assert "a-secure-32-char-token-for-test" not in result.message


def test_stale_shared_tokens_are_found_in_the_environment(tmp_path, monkeypatch):
    monkeypatch.delenv("PINCER_DASHBOARD_TOKEN", raising=False)
    monkeypatch.setenv("PINCER_WEB_CHAT_TOKEN", "leftover")
    result = SecurityDoctor(config_dir=tmp_path, data_dir=tmp_path)._check_stale_shared_tokens()
    assert result.status == CheckStatus.WARNING
    assert "PINCER_WEB_CHAT_TOKEN" in result.message


def test_no_stale_shared_tokens_passes(tmp_path, monkeypatch):
    monkeypatch.delenv("PINCER_DASHBOARD_TOKEN", raising=False)
    monkeypatch.delenv("PINCER_WEB_CHAT_TOKEN", raising=False)
    result = SecurityDoctor(config_dir=tmp_path, data_dir=tmp_path)._check_stale_shared_tokens()
    assert result.status == CheckStatus.PASS


def test_telegram_access_control_pass_from_dotenv_only(tmp_path, monkeypatch):
    """A Telegram token configured only in .env must show as configured."""
    from pincer.config import get_settings_relaxed
    from pincer.config.main import _RelaxedSettings

    (tmp_path / ".env").write_text("PINCER_TELEGRAM_BOT_TOKEN=123456:TEST\n")
    _toml_identity(tmp_path)
    monkeypatch.chdir(tmp_path)
    # Opt back in to dotenv reading, which conftest turns off for every test.
    monkeypatch.setitem(_RelaxedSettings.model_config, "env_file", tmp_path / ".env")
    monkeypatch.delenv("PINCER_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("PINCER_IDENTITY_MAP", raising=False)

    get_settings_relaxed.cache_clear()
    try:
        doc = SecurityDoctor(config_dir=tmp_path, data_dir=tmp_path)
        result = doc._check_telegram_access_control()
        assert result.status == CheckStatus.PASS
    finally:
        get_settings_relaxed.cache_clear()


# ── Additional coverage: access control checks ────────────────────────────────


def _toml_identity(config_dir, channel: str = "telegram", channel_user_id: str = "111") -> None:
    (config_dir / "pincer.toml").write_text(
        f'[identity.alice]\n\n[[identity.alice.channels]]\nchannel = "{channel}"\n'
        f'channel_user_id = "{channel_user_id}"\n'
    )


def _access_cfg(tmp_path, mode: str = "update"):
    from unittest.mock import MagicMock

    cfg = MagicMock()
    cfg.identity_management = mode
    cfg.db_path = tmp_path / "no-such.db"
    cfg.telegram_bot_token.get_secret_value.return_value = "123456:TOKEN"
    return cfg


def test_telegram_access_control_critical_no_identity_map(tmp_path):
    doc = SecurityDoctor(config_dir=tmp_path)
    cfg = _access_cfg(tmp_path)
    cfg.telegram_bot_token.get_secret_value.return_value = "123456:TOKEN"
    result = doc._check_telegram_access_control(cfg)
    assert result.status == CheckStatus.CRITICAL


@pytest.mark.parametrize(
    "check", ["_check_telegram_access_control", "_check_whatsapp_access_control", "_check_signal_access_control"]
)
def test_access_control_reports_an_identity_map_that_does_not_load(tmp_path, check):
    """`pincer run` refuses to start on it; the doctor must say so, not crash."""
    cfg = _access_cfg(tmp_path)
    cfg.whatsapp_enabled = True
    cfg.signal_enabled = True

    (tmp_path / "pincer.toml").write_text("""
[identity.me]
[[identity.me.channels]]
channel = "telegram"
channel_user_id = 1
""")
    result = getattr(SecurityDoctor(config_dir=tmp_path), check)(cfg)
    assert result.status == CheckStatus.CRITICAL
    assert "identity key 'me' is reserved" in result.message


@pytest.mark.parametrize(
    "check", ["_check_telegram_access_control", "_check_whatsapp_access_control", "_check_signal_access_control"]
)
def test_access_control_counts_clionly_and_a_database_roster(tmp_path, check):
    """#348: the gate is on in clionly, and with identities kept only in the database."""
    from pincer.services.identity import IdentityService

    cfg = _access_cfg(tmp_path, mode="clionly")
    cfg.whatsapp_enabled = True
    cfg.signal_enabled = True
    doc = SecurityDoctor(config_dir=tmp_path)
    clionly = getattr(doc, check)(cfg)
    assert clionly.status == CheckStatus.PASS
    assert "clionly" in clionly.message

    cfg.identity_management = "update"
    cfg.db_path = tmp_path / "pincer.db"
    assert getattr(doc, check)(cfg).status != CheckStatus.PASS

    async def add_roster() -> None:
        from pincer.db.engine import dispose_engines

        try:
            service = await IdentityService.for_path(cfg.db_path)
            await service.create_profile("alice", channels=[("telegram", "111")])
        finally:
            await dispose_engines()

    asyncio.run(add_roster())
    from_db = getattr(doc, check)(cfg)
    assert from_db.status == CheckStatus.PASS
    assert "database" in from_db.message


def test_the_identity_roster_is_read_once_per_run(tmp_path, monkeypatch):
    from pincer.services import identity as identity_service

    calls = []
    monkeypatch.setattr(identity_service, "roster_summary", lambda path: calls.append(path) or (False, ["cli1"]))
    monkeypatch.delenv("PINCER_IDENTITY_MAP", raising=False)
    cfg = _access_cfg(tmp_path, mode="sync")
    cfg.whatsapp_enabled = True
    cfg.signal_enabled = True
    doc = SecurityDoctor(config_dir=tmp_path)

    doc._roster_cache = {}  # what run_all sets up
    for check in ("_check_identity_management", "_check_telegram_access_control", "_check_signal_access_control"):
        getattr(doc, check)(cfg)
    doc._check_whatsapp_access_control(cfg)
    assert len(calls) == 1

    doc._roster_cache = None  # outside a run, every check reads afresh
    doc._check_telegram_access_control(cfg)
    assert len(calls) == 2


def test_identity_management_is_critical_while_the_old_env_var_is_set(tmp_path, monkeypatch):
    monkeypatch.setenv("PINCER_IDENTITY_MAP", "john@telegram:1=whatsapp:+4911,telegram:2")
    result = SecurityDoctor(config_dir=tmp_path)._check_identity_management(_access_cfg(tmp_path))
    assert result.status == CheckStatus.CRITICAL
    assert "no longer read" in result.message
    assert "[identity.john]" in result.fix_hint
    assert "[identity.person_1]" in result.fix_hint

    monkeypatch.delenv("PINCER_IDENTITY_MAP")
    (tmp_path / ".env").write_text("PINCER_IDENTITY_MAP=jane@telegram:3\n")
    from_dotenv = SecurityDoctor(config_dir=tmp_path)._check_identity_management(_access_cfg(tmp_path))
    assert from_dotenv.status == CheckStatus.CRITICAL
    assert "[identity.jane]" in from_dotenv.fix_hint


def test_identity_management_warns_when_clionly_ignores_the_toml(tmp_path, monkeypatch):
    monkeypatch.delenv("PINCER_IDENTITY_MAP", raising=False)
    doc = SecurityDoctor(config_dir=tmp_path)
    assert doc._check_identity_management(_access_cfg(tmp_path, "clionly")).status == CheckStatus.PASS
    _toml_identity(tmp_path)
    ignored = doc._check_identity_management(_access_cfg(tmp_path, "clionly"))
    assert ignored.status == CheckStatus.WARNING
    assert "ignored" in ignored.message
    assert doc._check_identity_management(_access_cfg(tmp_path, "update")).status == CheckStatus.PASS


def test_telegram_access_control_pass_with_identity_map(tmp_path):
    doc = SecurityDoctor(config_dir=tmp_path)
    cfg = _access_cfg(tmp_path)
    _toml_identity(tmp_path)
    cfg.telegram_bot_token.get_secret_value.return_value = "123456:TOKEN"
    result = doc._check_telegram_access_control(cfg)
    assert result.status == CheckStatus.PASS


def test_telegram_access_control_skipped_not_configured(tmp_path):
    doc = SecurityDoctor(config_dir=tmp_path)
    cfg = _access_cfg(tmp_path)
    cfg.telegram_bot_token.get_secret_value.return_value = ""
    result = doc._check_telegram_access_control(cfg)
    assert result.status == CheckStatus.SKIPPED


def test_whatsapp_access_control_warning_self_chat_only_no_identity_map(tmp_path):
    """self-chat-only alone doesn't gate group mentions, so without an identity
    map this should warn rather than pass."""
    doc = SecurityDoctor(config_dir=tmp_path)
    cfg = _access_cfg(tmp_path)
    cfg.whatsapp_enabled = True
    cfg.whatsapp_self_chat_only = True
    cfg.whatsapp_guests_allowed = False
    result = doc._check_whatsapp_access_control(cfg)
    assert result.status == CheckStatus.WARNING


def test_whatsapp_access_control_pass_self_chat_only_with_identity_map(tmp_path):
    doc = SecurityDoctor(config_dir=tmp_path)
    cfg = _access_cfg(tmp_path)
    cfg.whatsapp_enabled = True
    cfg.whatsapp_self_chat_only = True
    _toml_identity(tmp_path)
    result = doc._check_whatsapp_access_control(cfg)
    assert result.status == CheckStatus.PASS


def test_whatsapp_access_control_pass_with_identity_map(tmp_path):
    doc = SecurityDoctor(config_dir=tmp_path)
    cfg = _access_cfg(tmp_path)
    cfg.whatsapp_enabled = True
    cfg.whatsapp_self_chat_only = False
    _toml_identity(tmp_path)
    result = doc._check_whatsapp_access_control(cfg)
    assert result.status == CheckStatus.PASS


def test_whatsapp_access_control_critical_open_to_anyone(tmp_path):
    """guests_allowed=true overrides the fail-closed-without-map DM default."""
    doc = SecurityDoctor(config_dir=tmp_path)
    cfg = _access_cfg(tmp_path)
    cfg.whatsapp_enabled = True
    cfg.whatsapp_self_chat_only = False
    cfg.whatsapp_guests_allowed = True
    result = doc._check_whatsapp_access_control(cfg)
    assert result.status == CheckStatus.CRITICAL


def test_whatsapp_access_control_warning_no_map_dm_fail_closed(tmp_path):
    """No map, self_chat_only=False, guests not allowed → DMs fail closed by
    default now, so this is a WARNING (ungated group mentions remain), not CRITICAL."""
    doc = SecurityDoctor(config_dir=tmp_path)
    cfg = _access_cfg(tmp_path)
    cfg.whatsapp_enabled = True
    cfg.whatsapp_self_chat_only = False
    cfg.whatsapp_guests_allowed = False
    result = doc._check_whatsapp_access_control(cfg)
    assert result.status == CheckStatus.WARNING


def test_whatsapp_access_control_skipped_not_configured(tmp_path):
    from unittest.mock import MagicMock

    doc = SecurityDoctor(config_dir=tmp_path)
    cfg = MagicMock()
    cfg.whatsapp_enabled = False
    result = doc._check_whatsapp_access_control(cfg)
    assert result.status == CheckStatus.SKIPPED


def test_discord_allowlist_pass():
    from unittest.mock import MagicMock

    doc = SecurityDoctor()
    cfg = MagicMock()
    cfg.discord_guild_allowlist = "123456789"
    result = doc._check_discord_allowlist(cfg)
    assert result.status == CheckStatus.PASS


def test_discord_allowlist_warning_token_no_list():
    from unittest.mock import MagicMock

    doc = SecurityDoctor()
    cfg = MagicMock()
    cfg.discord_guild_allowlist = "  "
    cfg.discord_bot_token.get_secret_value.return_value = "Nz.bot.token"
    result = doc._check_discord_allowlist(cfg)
    assert result.status == CheckStatus.WARNING


def test_discord_allowlist_skipped():
    from unittest.mock import MagicMock

    doc = SecurityDoctor()
    cfg = MagicMock()
    cfg.discord_guild_allowlist = ""
    cfg.discord_bot_token.get_secret_value.return_value = ""
    result = doc._check_discord_allowlist(cfg)
    assert result.status == CheckStatus.SKIPPED


def _auth_cfg(tmp_path, *, auth_disabled=False):
    from unittest.mock import MagicMock

    cfg = MagicMock()
    cfg.auth_disabled = auth_disabled
    cfg.db_path = tmp_path / "pincer.db"
    return cfg


def test_api_sign_in_warns_on_a_fresh_install(tmp_path):
    """Nobody can sign in, so the API is closed: safe, hence not CRITICAL —
    the deploy gate runs before the first identity can be created."""
    cfg = _auth_cfg(tmp_path)
    result = SecurityDoctor()._check_dashboard_auth_token(cfg)
    assert result.status == CheckStatus.WARNING
    assert "pincer identity set-password" in result.fix_hint
    assert not cfg.db_path.exists()  # looking must not create the database


def test_api_sign_in_warns_when_no_identity_has_credentials(tmp_path):
    from pincer.db.engine import ensure_schema_current

    cfg = _auth_cfg(tmp_path)
    ensure_schema_current(cfg.db_path)
    result = SecurityDoctor()._check_dashboard_auth_token(cfg)
    assert result.status == CheckStatus.WARNING


def test_api_sign_in_pass_when_an_identity_has_credentials(tmp_path):
    from conftest import seed_identity_with_api_key

    cfg = _auth_cfg(tmp_path)
    seed_identity_with_api_key(cfg.db_path)
    result = SecurityDoctor()._check_dashboard_auth_token(cfg)
    assert result.status == CheckStatus.PASS
    assert "1 identity" in result.message


async def test_api_sign_in_check_works_inside_a_running_loop(tmp_path):
    """`GET /api/doctor` runs the checks from the server's event loop."""
    import asyncio

    from conftest import seed_identity_with_api_key

    cfg = _auth_cfg(tmp_path)
    await asyncio.to_thread(seed_identity_with_api_key, cfg.db_path)
    assert SecurityDoctor()._check_dashboard_auth_token(cfg).status == CheckStatus.PASS


def test_api_sign_in_warns_when_auth_is_disabled(tmp_path):
    result = SecurityDoctor()._check_dashboard_auth_token(_auth_cfg(tmp_path, auth_disabled=True))
    assert result.status == CheckStatus.WARNING
    assert "PINCER_AUTH_DISABLED" in result.message


# ── Budget checks ─────────────────────────────────────────────────────────────


def test_budget_limits_pass():
    from unittest.mock import MagicMock

    doc = SecurityDoctor()
    cfg = MagicMock()
    cfg.daily_budget_usd = 5.0
    result = doc._check_budget_limits(cfg)
    assert result.status == CheckStatus.PASS
    assert "5.00" in result.message


def test_budget_limits_warning_zero():
    from unittest.mock import MagicMock

    doc = SecurityDoctor()
    cfg = MagicMock()
    cfg.daily_budget_usd = 0
    result = doc._check_budget_limits(cfg)
    assert result.status == CheckStatus.WARNING


def test_rate_limits_pass():
    from unittest.mock import MagicMock

    doc = SecurityDoctor()
    cfg = MagicMock()
    cfg.rate_messages_per_min = 10
    result = doc._check_rate_limits(cfg)
    assert result.status == CheckStatus.PASS


def test_tool_call_limits_pass():
    from unittest.mock import MagicMock

    doc = SecurityDoctor()
    cfg = MagicMock()
    cfg.rate_tools_per_min = 20
    cfg.max_concurrent_llm = 3
    result = doc._check_tool_call_limits(cfg)
    assert result.status == CheckStatus.PASS


# ── Filesystem checks ─────────────────────────────────────────────────────────


def test_data_dir_permissions_skipped_missing(tmp_path):
    doc = SecurityDoctor(data_dir=tmp_path / "nonexistent", config_dir=tmp_path)
    result = doc._check_data_dir_permissions()
    assert result.status == CheckStatus.SKIPPED


def test_data_dir_permissions_pass_700(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    d.chmod(0o700)
    doc = SecurityDoctor(data_dir=d, config_dir=tmp_path)
    result = doc._check_data_dir_permissions()
    assert result.status == CheckStatus.PASS


def test_data_dir_permissions_warning_755(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    d.chmod(0o755)
    doc = SecurityDoctor(data_dir=d, config_dir=tmp_path)
    result = doc._check_data_dir_permissions()
    assert result.status == CheckStatus.WARNING


def test_skills_dir_permissions_skipped_missing(tmp_path):
    doc = SecurityDoctor(skills_dir=tmp_path / "nonexistent", config_dir=tmp_path)
    result = doc._check_skills_dir_permissions()
    assert result.status == CheckStatus.SKIPPED


def test_skills_dir_permissions_pass_755(tmp_path):
    d = tmp_path / "skills"
    d.mkdir()
    d.chmod(0o755)
    doc = SecurityDoctor(skills_dir=d, config_dir=tmp_path)
    result = doc._check_skills_dir_permissions()
    assert result.status == CheckStatus.PASS


def test_skills_dir_permissions_warning(tmp_path):
    d = tmp_path / "skills"
    d.mkdir()
    d.chmod(0o777)
    doc = SecurityDoctor(skills_dir=d, config_dir=tmp_path)
    result = doc._check_skills_dir_permissions()
    assert result.status == CheckStatus.WARNING


def test_skills_dir_remote_root_reachable(tmp_path):
    from unittest.mock import MagicMock, patch

    root = MagicMock()
    root.exists.return_value = True
    doc = SecurityDoctor(skills_dir="s3://bucket/team", config_dir=tmp_path)
    with patch("pincer.tools.skills.storage.open_skills_root", return_value=root) as opener:
        result = doc._check_skills_dir_permissions(MagicMock())
    assert opener.call_args.args[0] == "s3://bucket/team"
    assert result.status == CheckStatus.PASS
    assert "s3://bucket/team" in result.message


def test_skills_dir_remote_root_empty(tmp_path):
    from unittest.mock import MagicMock, patch

    root = MagicMock()
    root.exists.return_value = False
    doc = SecurityDoctor(skills_dir="gs://bucket/team", config_dir=tmp_path)
    with patch("pincer.tools.skills.storage.open_skills_root", return_value=root):
        result = doc._check_skills_dir_permissions(MagicMock())
    assert result.status == CheckStatus.WARNING


def test_skills_dir_remote_root_unreachable(tmp_path):
    from unittest.mock import MagicMock, patch

    root = MagicMock()
    root.exists.side_effect = ConnectionError("no route")
    doc = SecurityDoctor(skills_dir="az://container/team", config_dir=tmp_path)
    with patch("pincer.tools.skills.storage.open_skills_root", return_value=root):
        result = doc._check_skills_dir_permissions(MagicMock())
    assert result.status == CheckStatus.WARNING
    assert "unreachable" in result.message


def test_skills_dir_remote_root_missing_extra(tmp_path, monkeypatch):
    import sys
    from unittest.mock import MagicMock

    monkeypatch.setitem(sys.modules, "cloudpathlib", None)
    doc = SecurityDoctor(skills_dir="s3://bucket/team", config_dir=tmp_path)
    result = doc._check_skills_dir_permissions(MagicMock())
    assert result.status == CheckStatus.WARNING
    assert "remote-skills" in result.message


def test_no_world_readable_secrets_pass(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    db = data / "pincer.db"
    db.write_text("data")
    db.chmod(0o600)
    doc = SecurityDoctor(config_dir=tmp_path, data_dir=data)
    result = doc._check_no_world_readable_secrets()
    assert result.status == CheckStatus.PASS


def test_no_world_readable_secrets_critical(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    db = data / "pincer.db"
    db.write_text("data")
    db.chmod(0o644)
    doc = SecurityDoctor(config_dir=tmp_path, data_dir=data)
    result = doc._check_no_world_readable_secrets()
    assert result.status == CheckStatus.CRITICAL


def test_sqlite_not_world_readable_pass(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    db = data / "pincer.db"
    db.write_text("")
    db.chmod(0o600)
    doc = SecurityDoctor(data_dir=data, config_dir=tmp_path)
    result = doc._check_sqlite_not_world_readable()
    assert result.status == CheckStatus.PASS


def test_sqlite_not_world_readable_skipped_no_db(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    doc = SecurityDoctor(data_dir=data, config_dir=tmp_path)
    result = doc._check_sqlite_not_world_readable()
    assert result.status == CheckStatus.SKIPPED


# ── Network checks ────────────────────────────────────────────────────────────


def test_dashboard_not_exposed_warning():
    from unittest.mock import MagicMock

    doc = SecurityDoctor()
    cfg = MagicMock()
    cfg.dashboard_host = "0.0.0.0"
    result = doc._check_dashboard_not_exposed(cfg)
    assert result.status == CheckStatus.WARNING


def test_no_debug_mode_pass():
    from unittest.mock import MagicMock

    doc = SecurityDoctor()
    cfg = MagicMock()
    cfg.debug = False
    result = doc._check_no_debug_mode(cfg)
    assert result.status == CheckStatus.PASS


def test_no_debug_mode_warning():
    from unittest.mock import MagicMock

    doc = SecurityDoctor()
    cfg = MagicMock()
    cfg.debug = True
    result = doc._check_no_debug_mode(cfg)
    assert result.status == CheckStatus.WARNING


# ── Env file exists ───────────────────────────────────────────────────────────


def test_env_file_exists_pass(tmp_path):
    (tmp_path / ".env").write_text("KEY=val")
    doc = SecurityDoctor(config_dir=tmp_path, data_dir=tmp_path)
    result = doc._check_env_file_exists()
    assert result.status == CheckStatus.PASS


def test_env_file_exists_warning_missing(tmp_path):
    doc = SecurityDoctor(config_dir=tmp_path, data_dir=tmp_path)
    result = doc._check_env_file_exists()
    assert result.status == CheckStatus.WARNING


# ── API keys in config ────────────────────────────────────────────────────────


def test_api_keys_not_in_config_critical(tmp_path):
    toml = tmp_path / "config.toml"
    fake_key = "sk-ant-" + "b" * 24
    toml.write_text(f'anthropic_key = "{fake_key}"\n')
    doc = SecurityDoctor(config_dir=tmp_path, data_dir=tmp_path)
    result = doc._check_api_keys_not_in_config()
    assert result.status == CheckStatus.CRITICAL


def test_api_keys_not_in_config_pass_env_ref(tmp_path):
    toml = tmp_path / "config.toml"
    toml.write_text('anthropic_key = "${ANTHROPIC_API_KEY}"\n')
    doc = SecurityDoctor(config_dir=tmp_path, data_dir=tmp_path)
    result = doc._check_api_keys_not_in_config()
    assert result.status == CheckStatus.PASS


# ── Gitignore ─────────────────────────────────────────────────────────────────


def test_gitignore_missing_warning(tmp_path):
    doc = SecurityDoctor(config_dir=tmp_path, data_dir=tmp_path)
    result = doc._check_gitignore_has_env()
    assert result.status == CheckStatus.WARNING


# ── Runtime checks ────────────────────────────────────────────────────────────


def test_audit_logging_enabled_pass():
    from unittest.mock import MagicMock

    doc = SecurityDoctor()
    cfg = MagicMock()
    cfg.audit_disabled = False
    result = doc._check_audit_logging_enabled(cfg)
    assert result.status == CheckStatus.PASS


def test_audit_logging_enabled_warning_disabled():
    from unittest.mock import MagicMock

    doc = SecurityDoctor()
    cfg = MagicMock()
    cfg.audit_disabled = True
    result = doc._check_audit_logging_enabled(cfg)
    assert result.status == CheckStatus.WARNING


def test_skill_sandbox_enabled_pass():
    from unittest.mock import MagicMock

    doc = SecurityDoctor()
    cfg = MagicMock()
    cfg.skill_sandbox_disabled = False
    result = doc._check_skill_sandbox_enabled(cfg)
    assert result.status == CheckStatus.PASS


def test_skill_sandbox_enabled_critical():
    from unittest.mock import MagicMock

    doc = SecurityDoctor()
    cfg = MagicMock()
    cfg.skill_sandbox_disabled = True
    result = doc._check_skill_sandbox_enabled(cfg)
    assert result.status == CheckStatus.CRITICAL


def _approval_cfg(policy="critical", never=""):
    """Plain namespace, not MagicMock: a MagicMock attribute is not a real name list."""
    return SimpleNamespace(approval_policy=policy, approval_never=never)


def test_tool_approval_mode_pass_critical():
    doc = SecurityDoctor()
    result = doc._check_tool_approval_mode(_approval_cfg("critical"))
    assert result.status == CheckStatus.PASS


def test_tool_approval_mode_pass_all():
    doc = SecurityDoctor()
    result = doc._check_tool_approval_mode(_approval_cfg("all"))
    assert result.status == CheckStatus.PASS


def test_tool_approval_mode_warning_none():
    doc = SecurityDoctor()
    result = doc._check_tool_approval_mode(_approval_cfg("none"))
    assert result.status == CheckStatus.WARNING
    assert "none" in result.message


def test_tool_approval_mode_warning_on_never_overrides():
    doc = SecurityDoctor()
    result = doc._check_tool_approval_mode(_approval_cfg("critical", "email_send"))
    assert result.status == CheckStatus.WARNING
    assert "email_send" in result.message
    assert "path confinement" not in result.message


def test_tool_approval_mode_warning_shell_exec_bypasses_sandbox():
    doc = SecurityDoctor()
    result = doc._check_tool_approval_mode(_approval_cfg("critical", "shell_exec"))
    assert result.status == CheckStatus.WARNING
    assert "shell_exec" in result.message
    assert "file_read/file_write workspace sandbox" in result.message


def test_tool_approval_mode_warning_python_exec_bypasses_sandbox():
    doc = SecurityDoctor()
    result = doc._check_tool_approval_mode(_approval_cfg("critical", "python_exec"))
    assert result.status == CheckStatus.WARNING
    assert "python_exec has no path confinement" in result.message
    assert "file_read/file_write workspace sandbox" in result.message


def test_tool_approval_mode_warning_names_both_unconfined_tools():
    doc = SecurityDoctor()
    result = doc._check_tool_approval_mode(_approval_cfg("critical", "shell_exec,python_exec"))
    assert result.status == CheckStatus.WARNING
    assert "python_exec and shell_exec have no path confinement" in result.message


def test_tool_approval_mode_warning_on_never_under_all():
    """``never`` wins over mode "all" too, so it must not report PASS."""
    doc = SecurityDoctor()
    result = doc._check_tool_approval_mode(_approval_cfg("all", "shell_exec"))
    assert result.status == CheckStatus.WARNING
    assert "Tool approval: all" in result.message
    assert "file_read/file_write workspace sandbox" in result.message


def test_tool_approval_mode_shell_exec_note_keeps_other_never_entries():
    doc = SecurityDoctor()
    result = doc._check_tool_approval_mode(_approval_cfg("critical", "shell_exec,email_send"))
    assert result.status == CheckStatus.WARNING
    assert "email_send" in result.message
    assert "shell_exec" in result.message
    assert "file_read/file_write workspace sandbox" in result.message


# ── Voice checks ──────────────────────────────────────────────────────────────


def test_voice_twilio_credentials_skipped():
    from unittest.mock import MagicMock

    doc = SecurityDoctor()
    cfg = MagicMock()
    cfg.voice_enabled = False
    result = doc._check_voice_twilio_credentials(cfg)
    assert result.status == CheckStatus.SKIPPED


def test_voice_twilio_credentials_pass():
    from unittest.mock import MagicMock

    doc = SecurityDoctor()
    cfg = MagicMock()
    cfg.voice_enabled = True
    cfg.twilio_account_sid = "ACtest"
    cfg.twilio_auth_token.get_secret_value.return_value = "auth_token"
    result = doc._check_voice_twilio_credentials(cfg)
    assert result.status == CheckStatus.PASS


def test_voice_twilio_credentials_critical_missing():
    from unittest.mock import MagicMock

    doc = SecurityDoctor()
    cfg = MagicMock()
    cfg.voice_enabled = True
    cfg.twilio_account_sid = ""
    cfg.twilio_auth_token.get_secret_value.return_value = ""
    result = doc._check_voice_twilio_credentials(cfg)
    assert result.status == CheckStatus.CRITICAL
    assert "PINCER_TWILIO_ACCOUNT_SID" in result.message


def test_voice_webhook_url_skipped():
    from unittest.mock import MagicMock

    doc = SecurityDoctor()
    cfg = MagicMock()
    cfg.voice_enabled = False
    result = doc._check_voice_webhook_url(cfg)
    assert result.status == CheckStatus.SKIPPED


def test_voice_webhook_url_pass_https():
    from unittest.mock import MagicMock

    doc = SecurityDoctor()
    cfg = MagicMock()
    cfg.voice_enabled = True
    cfg.voice_webhook_base_url = "https://example.ngrok.io"
    result = doc._check_voice_webhook_url(cfg)
    assert result.status == CheckStatus.PASS


def test_voice_webhook_url_warning_http():
    from unittest.mock import MagicMock

    doc = SecurityDoctor()
    cfg = MagicMock()
    cfg.voice_enabled = True
    cfg.voice_webhook_base_url = "http://example.ngrok.io"
    result = doc._check_voice_webhook_url(cfg)
    assert result.status == CheckStatus.WARNING


def test_voice_webhook_url_warning_missing():
    from unittest.mock import MagicMock

    doc = SecurityDoctor()
    cfg = MagicMock()
    cfg.voice_enabled = True
    cfg.voice_webhook_base_url = ""
    result = doc._check_voice_webhook_url(cfg)
    assert result.status == CheckStatus.WARNING


def test_voice_recording_consent_skipped():
    from unittest.mock import MagicMock

    doc = SecurityDoctor()
    cfg = MagicMock()
    cfg.voice_enabled = False
    result = doc._check_voice_recording_consent(cfg)
    assert result.status == CheckStatus.SKIPPED


def test_voice_recording_consent_critical_no_consent():
    from unittest.mock import MagicMock

    doc = SecurityDoctor()
    cfg = MagicMock()
    cfg.voice_enabled = True
    cfg.voice_recording_enabled = True
    cfg.voice_consent_mode = "none"
    result = doc._check_voice_recording_consent(cfg)
    assert result.status == CheckStatus.CRITICAL


def test_voice_recording_consent_pass_with_mode():
    from unittest.mock import MagicMock

    doc = SecurityDoctor()
    cfg = MagicMock()
    cfg.voice_enabled = True
    cfg.voice_recording_enabled = True
    cfg.voice_consent_mode = "one_party"
    result = doc._check_voice_recording_consent(cfg)
    assert result.status == CheckStatus.PASS


def test_voice_recording_consent_pass_disabled():
    from unittest.mock import MagicMock

    doc = SecurityDoctor()
    cfg = MagicMock()
    cfg.voice_enabled = True
    cfg.voice_recording_enabled = False
    result = doc._check_voice_recording_consent(cfg)
    assert result.status == CheckStatus.PASS


# ── Signal checks ─────────────────────────────────────────────────────────────


def test_signal_phone_set_skipped():
    from unittest.mock import MagicMock

    doc = SecurityDoctor()
    cfg = MagicMock()
    cfg.signal_enabled = False
    result = doc._check_signal_phone_set(cfg)
    assert result.status == CheckStatus.SKIPPED


def test_signal_phone_set_pass():
    from unittest.mock import MagicMock

    doc = SecurityDoctor()
    cfg = MagicMock()
    cfg.signal_enabled = True
    cfg.signal_phone_number = "+1234567890"
    result = doc._check_signal_phone_set(cfg)
    assert result.status == CheckStatus.PASS


def test_signal_phone_set_critical_missing():
    from unittest.mock import MagicMock

    doc = SecurityDoctor()
    cfg = MagicMock()
    cfg.signal_enabled = True
    cfg.signal_phone_number = ""
    result = doc._check_signal_phone_set(cfg)
    assert result.status == CheckStatus.CRITICAL


def test_signal_api_local_skipped():
    from unittest.mock import MagicMock

    doc = SecurityDoctor()
    cfg = MagicMock()
    cfg.signal_enabled = False
    result = doc._check_signal_api_local(cfg)
    assert result.status == CheckStatus.SKIPPED


def test_signal_api_local_pass_localhost():
    from unittest.mock import MagicMock

    doc = SecurityDoctor()
    cfg = MagicMock()
    cfg.signal_enabled = True
    cfg.signal_api_url = "http://localhost:8080"
    result = doc._check_signal_api_local(cfg)
    assert result.status == CheckStatus.PASS


def test_signal_api_local_critical_public():
    from unittest.mock import MagicMock

    doc = SecurityDoctor()
    cfg = MagicMock()
    cfg.signal_enabled = True
    cfg.signal_api_url = "http://api.example.com:8080"
    result = doc._check_signal_api_local(cfg)
    assert result.status == CheckStatus.CRITICAL


def test_signal_access_control_skipped(tmp_path):
    from unittest.mock import MagicMock

    doc = SecurityDoctor(config_dir=tmp_path)
    cfg = MagicMock()
    cfg.signal_enabled = False
    result = doc._check_signal_access_control(cfg)
    assert result.status == CheckStatus.SKIPPED


def test_signal_access_control_pass_with_identity_map(tmp_path):
    doc = SecurityDoctor(config_dir=tmp_path)
    cfg = _access_cfg(tmp_path)
    cfg.signal_enabled = True
    _toml_identity(tmp_path, "signal", "+1234567890")
    result = doc._check_signal_access_control(cfg)
    assert result.status == CheckStatus.PASS


def test_signal_access_control_warning_no_identity_map(tmp_path):
    doc = SecurityDoctor(config_dir=tmp_path)
    cfg = _access_cfg(tmp_path)
    cfg.signal_enabled = True
    result = doc._check_signal_access_control(cfg)
    assert result.status == CheckStatus.WARNING


# ── MCP security checks ───────────────────────────────────────────────────────


def test_mcp_no_plaintext_secrets_skipped_no_toml(tmp_path):
    doc = SecurityDoctor(config_dir=tmp_path)
    result = doc._check_mcp_no_plaintext_secrets()
    assert result.status == CheckStatus.SKIPPED


def test_mcp_no_plaintext_secrets_pass(tmp_path):
    (tmp_path / "pincer.toml").write_text(
        '[mcp]\nenabled = true\n\n[[mcp.servers]]\nname = "s"\nenv = {TOKEN = "${GITHUB_TOKEN}"}\n'
    )
    doc = SecurityDoctor(config_dir=tmp_path)
    result = doc._check_mcp_no_plaintext_secrets()
    assert result.status == CheckStatus.PASS


def test_mcp_no_plaintext_secrets_warning(tmp_path):
    (tmp_path / "pincer.toml").write_text(
        '[mcp]\nenabled = true\n\n[[mcp.servers]]\nname = "s"\nenv = {token = "abcdefghijklmnopqrstuvwxyz"}\n'
    )
    doc = SecurityDoctor(config_dir=tmp_path)
    result = doc._check_mcp_no_plaintext_secrets()
    assert result.status == CheckStatus.WARNING


def test_mcp_injection_alerts_always_pass():
    doc = SecurityDoctor()
    result = doc._check_mcp_injection_alerts()
    assert result.status == CheckStatus.PASS
    assert "injection" in result.message.lower()


# ── Voice DACH checks (Sprint 0) ──────────────────────────────────────────────


def _voice_cfg(**overrides):
    from unittest.mock import MagicMock

    cfg = MagicMock()
    cfg.voice_enabled = True
    cfg.voice_outbound_enabled = False
    cfg.voice_recording_enabled = False
    cfg.voice_consent_mode = "two_party"
    cfg.voice_transcript_retention_days = 90
    cfg.twilio_phone_number = "+4915212345678"
    cfg.twilio_account_sid = ""
    cfg.deepgram_api_key.get_secret_value.return_value = ""
    cfg.elevenlabs_api_key.get_secret_value.return_value = ""
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


def test_voice_dach_consent_skipped_when_disabled():
    result = SecurityDoctor()._check_voice_dach_consent(_voice_cfg(voice_enabled=False))
    assert result.status == CheckStatus.SKIPPED


def test_voice_dach_consent_warns_on_outbound_only_deployment():
    """voice_enabled=False must not skip the check the outbound flag needs —
    an outbound-only DACH deployment is exactly the scenario it exists for."""
    cfg = _voice_cfg(voice_enabled=False, voice_outbound_enabled=True, voice_consent_mode="one_party")
    result = SecurityDoctor()._check_voice_dach_consent(cfg)
    assert result.status == CheckStatus.WARNING


def test_voice_retention_runs_on_outbound_only_deployment():
    cfg = _voice_cfg(
        voice_enabled=False,
        voice_outbound_enabled=True,
        voice_recording_enabled=True,
        voice_transcript_retention_days=0,
    )
    result = SecurityDoctor()._check_voice_retention(cfg)
    assert result.status == CheckStatus.WARNING


def test_voice_provider_regions_runs_on_outbound_only_deployment():
    cfg = _voice_cfg(voice_enabled=False, voice_outbound_enabled=True, twilio_account_sid="AC123")
    result = SecurityDoctor()._check_voice_provider_regions(cfg)
    assert result.status == CheckStatus.PASS
    assert "Twilio" in result.message


def test_voice_dach_consent_warns_on_one_party_outbound():
    cfg = _voice_cfg(voice_outbound_enabled=True, voice_consent_mode="one_party")
    result = SecurityDoctor()._check_voice_dach_consent(cfg)
    assert result.status == CheckStatus.WARNING
    assert "two_party" in result.fix_hint


def test_voice_dach_consent_passes_with_two_party():
    cfg = _voice_cfg(voice_outbound_enabled=True, voice_consent_mode="two_party")
    result = SecurityDoctor()._check_voice_dach_consent(cfg)
    assert result.status == CheckStatus.PASS


def test_voice_dach_consent_passes_non_dach_number():
    cfg = _voice_cfg(voice_outbound_enabled=True, voice_consent_mode="one_party", twilio_phone_number="+14155551234")
    result = SecurityDoctor()._check_voice_dach_consent(cfg)
    assert result.status == CheckStatus.PASS


def test_voice_retention_warns_recording_without_window():
    cfg = _voice_cfg(voice_recording_enabled=True, voice_transcript_retention_days=0)
    result = SecurityDoctor()._check_voice_retention(cfg)
    assert result.status == CheckStatus.WARNING


def test_voice_retention_passes_with_window():
    result = SecurityDoctor()._check_voice_retention(_voice_cfg(voice_recording_enabled=True))
    assert result.status == CheckStatus.PASS
    assert "90" in result.message


def test_voice_provider_regions_lists_configured():
    cfg = _voice_cfg(twilio_account_sid="AC123")
    cfg.deepgram_api_key.get_secret_value.return_value = "dg-key"
    result = SecurityDoctor()._check_voice_provider_regions(cfg)
    assert result.status == CheckStatus.PASS
    assert "Twilio" in result.message
    assert "Deepgram" in result.message


def test_voice_provider_regions_none_configured():
    result = SecurityDoctor()._check_voice_provider_regions(_voice_cfg())
    assert result.status == CheckStatus.PASS
    assert "No external" in result.message


def test_voice_twilio_edge_unset_is_advice_not_a_failure():
    result = SecurityDoctor()._check_voice_twilio_edge(_voice_cfg(twilio_account_sid="AC123", twilio_edge=""))
    assert result.status == CheckStatus.PASS
    assert "not set" in result.message
    assert "frankfurt" in result.fix_hint


def test_voice_twilio_edge_set():
    result = SecurityDoctor()._check_voice_twilio_edge(_voice_cfg(twilio_account_sid="AC123", twilio_edge="frankfurt"))
    assert result.status == CheckStatus.PASS
    assert "'frankfurt' edge" in result.message
    assert result.fix_hint == ""


def test_voice_twilio_edge_skipped_without_voice():
    cfg = _voice_cfg(voice_enabled=False, voice_outbound_enabled=False, twilio_account_sid="AC123", twilio_edge="")
    assert SecurityDoctor()._check_voice_twilio_edge(cfg).status == CheckStatus.SKIPPED


# ── Production deploy gate (Sprint 7, T7.3) ──────────────────────────


def _prod_cfg(**overrides):
    from unittest.mock import MagicMock

    cfg = MagicMock()
    cfg.voice_enabled = True
    cfg.voice_outbound_enabled = True
    cfg.voice_webhook_base_url = "https://voice.example.com"
    cfg.voice_consent_mode = "two_party"
    cfg.voice_transcript_retention_days = 90
    cfg.voice_timezone = "Europe/Berlin"
    cfg.timezone = "Europe/Berlin"
    cfg.ngrok_authtoken.get_secret_value.return_value = ""
    cfg.auth_disabled = False
    cfg.jwt_secret.get_secret_value.return_value = "j" * 48
    cfg.twilio_auth_token.get_secret_value.return_value = "c" * 32
    # Sprint 8 production gate
    cfg.environment = "production"
    cfg.dashboard_url = "https://dashboard.example.com"
    cfg.web_chat_url = ""
    cfg.cors_extra_origins = ""
    cfg.voice_webhook_validate = True
    cfg.voice_ws_auth_required = True
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


def test_production_mode_adds_gate_checks(doctor_env):
    baseline = len(doctor_env.run_all().checks)
    doctor_env.production = True
    prod = doctor_env.run_all()
    assert len(prod.checks) == baseline + 7  # Sprint 7 (4) + Sprint 8 (3)
    assert {c.category for c in prod.checks} >= {"production"}


def test_prod_webhook_https_passes():
    result = SecurityDoctor(production=True)._check_prod_webhook_url(_prod_cfg())
    assert result.status == CheckStatus.PASS


@pytest.mark.parametrize(
    "url",
    [
        "http://voice.example.com",  # plain HTTP
        "https://abc123.ngrok-free.app",  # tunnel
        "https://tame-cat.trycloudflare.com",  # tunnel
        "https://localhost:8080",  # localhost
        "",  # unset
    ],
)
def test_prod_webhook_rejects_dev_urls(url):
    result = SecurityDoctor(production=True)._check_prod_webhook_url(_prod_cfg(voice_webhook_base_url=url))
    assert result.status == CheckStatus.CRITICAL


def test_prod_no_tunnel_critical_when_ngrok_configured():
    cfg = _prod_cfg()
    cfg.ngrok_authtoken.get_secret_value.return_value = "tok_123"
    result = SecurityDoctor(production=True)._check_prod_no_tunnel(cfg)
    assert result.status == CheckStatus.CRITICAL


def test_prod_no_tunnel_passes_when_absent():
    result = SecurityDoctor(production=True)._check_prod_no_tunnel(_prod_cfg())
    assert result.status == CheckStatus.PASS


def test_prod_auth_disabled_is_critical():
    result = SecurityDoctor(production=True)._check_prod_auth_tokens(_prod_cfg(auth_disabled=True))
    assert result.status == CheckStatus.CRITICAL
    assert "PINCER_AUTH_DISABLED" in result.message


def test_prod_missing_jwt_secret_is_a_warning():
    cfg = _prod_cfg()
    cfg.jwt_secret.get_secret_value.return_value = ""
    result = SecurityDoctor(production=True)._check_prod_auth_tokens(cfg)
    assert result.status == CheckStatus.WARNING
    assert "PINCER_JWT_SECRET" in result.message


def test_prod_short_jwt_secret_is_critical():
    cfg = _prod_cfg()
    cfg.jwt_secret.get_secret_value.return_value = "short"
    result = SecurityDoctor(production=True)._check_prod_auth_tokens(cfg)
    assert result.status == CheckStatus.CRITICAL
    assert "too short" in result.message


def test_prod_auth_tokens_pass():
    result = SecurityDoctor(production=True)._check_prod_auth_tokens(_prod_cfg())
    assert result.status == CheckStatus.PASS


def test_prod_dach_compliance_pass():
    result = SecurityDoctor(production=True)._check_prod_dach_compliance(_prod_cfg())
    assert result.status == CheckStatus.PASS


@pytest.mark.parametrize(
    ("override", "expected_fragment"),
    [
        ({"voice_consent_mode": "one_party"}, "two_party"),
        ({"voice_transcript_retention_days": 0}, "retention"),
        ({"voice_timezone": "America/New_York", "timezone": "America/New_York"}, "Europe"),
    ],
)
def test_prod_dach_compliance_critical_on_weakened_setting(override, expected_fragment):
    result = SecurityDoctor(production=True)._check_prod_dach_compliance(_prod_cfg(**override))
    assert result.status == CheckStatus.CRITICAL
    assert expected_fragment in result.message + result.fix_hint


def test_prod_dach_compliance_skipped_when_voice_off():
    cfg = _prod_cfg(voice_enabled=False, voice_outbound_enabled=False)
    result = SecurityDoctor(production=True)._check_prod_dach_compliance(cfg)
    assert result.status == CheckStatus.SKIPPED


# ── Sprint 8 production gate ──────────────────────────────


def test_prod_environment_flag_pass():
    result = SecurityDoctor(production=True)._check_prod_environment_flag(_prod_cfg())
    assert result.status == CheckStatus.PASS


def test_prod_environment_flag_critical_when_not_production():
    result = SecurityDoctor(production=True)._check_prod_environment_flag(_prod_cfg(environment="development"))
    assert result.status == CheckStatus.CRITICAL


def test_prod_cors_origins_pass():
    result = SecurityDoctor(production=True)._check_prod_cors_origins(_prod_cfg())
    assert result.status == CheckStatus.PASS


def test_prod_cors_origins_critical_on_localhost():
    result = SecurityDoctor(production=True)._check_prod_cors_origins(
        _prod_cfg(cors_extra_origins="http://localhost:3000")
    )
    assert result.status == CheckStatus.CRITICAL


def test_prod_voice_signatures_pass():
    result = SecurityDoctor(production=True)._check_prod_voice_signatures(_prod_cfg())
    assert result.status == CheckStatus.PASS


@pytest.mark.parametrize(
    "override",
    [
        {"voice_webhook_validate": False},
        {"voice_ws_auth_required": False},
    ],
)
def test_prod_voice_signatures_critical_when_disabled(override):
    result = SecurityDoctor(production=True)._check_prod_voice_signatures(_prod_cfg(**override))
    assert result.status == CheckStatus.CRITICAL


def test_prod_voice_signatures_critical_without_twilio_token():
    cfg = _prod_cfg()
    cfg.twilio_auth_token.get_secret_value.return_value = ""
    result = SecurityDoctor(production=True)._check_prod_voice_signatures(cfg)
    assert result.status == CheckStatus.CRITICAL


# ── Sprint 8 voice security checks (always run) ───────────


def test_voice_webhook_signature_critical_when_validation_off():
    result = SecurityDoctor()._check_voice_webhook_signature(_prod_cfg(voice_webhook_validate=False))
    assert result.status == CheckStatus.CRITICAL


def test_voice_webhook_signature_pass():
    assert SecurityDoctor()._check_voice_webhook_signature(_prod_cfg()).status == CheckStatus.PASS


def test_voice_ws_auth_critical_when_disabled():
    result = SecurityDoctor()._check_voice_ws_auth(_prod_cfg(voice_ws_auth_required=False))
    assert result.status == CheckStatus.CRITICAL


def test_voice_abuse_limits_pass_with_defaults():
    cfg = _prod_cfg(voice_daily_call_limit=20, voice_target_cooldown_min=60, voice_quiet_hours="20:00-08:00")
    assert SecurityDoctor()._check_voice_abuse_limits(cfg).status == CheckStatus.PASS


def test_voice_abuse_limits_critical_when_all_disabled():
    cfg = _prod_cfg(voice_daily_call_limit=0, voice_target_cooldown_min=0, voice_quiet_hours="")
    assert SecurityDoctor()._check_voice_abuse_limits(cfg).status == CheckStatus.CRITICAL


def test_voice_do_not_call_enforced_on_the_dial_path():
    """Guards against a refactor that routes make_phone_call around the gate."""
    assert SecurityDoctor()._check_voice_do_not_call_enforced().status == CheckStatus.PASS


# ── Sprint 9 observability checks ─────────────────────────


def _obs_cfg(**overrides):
    from unittest.mock import MagicMock

    cfg = MagicMock()
    cfg.ops_alerts_enabled = True
    cfg.ops_user_id = "ops"
    cfg.ops_channel = "telegram"
    cfg.ops_alert_email = ""
    cfg.default_user_id = ""
    cfg.telemetry_dsn = "https://token@otlp.example.com"
    cfg.voice_enabled = True
    cfg.voice_canary_enabled = True
    cfg.voice_canary_number = "+4915100000001"
    cfg.voice_canary_cron = "0 */6 * * *"
    cfg.voice_quiet_hours = "20:00-08:00"
    cfg.voice_quiet_hours_override_users = "pincer-canary"
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


def test_ops_alert_routing_pass():
    assert SecurityDoctor()._check_ops_alert_routing(_obs_cfg()).status == CheckStatus.PASS


def test_ops_alert_routing_critical_without_a_recipient():
    """An alert system firing into the void looks healthy from every angle."""
    result = SecurityDoctor()._check_ops_alert_routing(_obs_cfg(ops_user_id="", default_user_id="", ops_alert_email=""))
    assert result.status == CheckStatus.CRITICAL


def test_ops_alert_routing_accepts_email_only():
    result = SecurityDoctor()._check_ops_alert_routing(
        _obs_cfg(ops_user_id="", default_user_id="", ops_alert_email="ops@example.com")
    )
    assert result.status == CheckStatus.PASS


def test_ops_alert_routing_warns_without_telemetry():
    result = SecurityDoctor()._check_ops_alert_routing(_obs_cfg(telemetry_dsn=None))
    assert result.status == CheckStatus.WARNING


def test_ops_alert_routing_warns_when_disabled():
    result = SecurityDoctor()._check_ops_alert_routing(_obs_cfg(ops_alerts_enabled=False))
    assert result.status == CheckStatus.WARNING


def test_voice_canary_pass():
    assert SecurityDoctor()._check_voice_canary(_obs_cfg()).status == CheckStatus.PASS


def test_voice_canary_skipped_when_voice_off():
    assert SecurityDoctor()._check_voice_canary(_obs_cfg(voice_enabled=False)).status == CheckStatus.SKIPPED


def test_voice_canary_warns_when_disabled():
    result = SecurityDoctor()._check_voice_canary(_obs_cfg(voice_canary_enabled=False))
    assert result.status == CheckStatus.WARNING
    assert "customer" in result.message


def test_voice_canary_critical_without_a_target():
    """Enabled but unable to ever run is worse than off — it reads as covered."""
    result = SecurityDoctor()._check_voice_canary(_obs_cfg(voice_canary_number=""))
    assert result.status == CheckStatus.CRITICAL


def test_voice_canary_warns_about_quiet_hours_gap():
    result = SecurityDoctor()._check_voice_canary(_obs_cfg(voice_quiet_hours_override_users=""))
    assert result.status == CheckStatus.WARNING
    assert "quiet hours" in result.message


# ── Schedule action type check (issue #217) ───────────────


def _sched_cfg(tmp_path):
    return SimpleNamespace(db_path=tmp_path / "pincer.db")


def _seed_schedules(tmp_path, *rows):
    """rows: (name, action_dict, enabled) tuples. Always ensures the table exists,
    so even the zero-schedule case reflects a real (empty) query, not a missing table."""
    from pincer.scheduler.cron import CronScheduler

    async def _seed():
        store = CronScheduler(tmp_path / "pincer.db")
        await store.ensure_table()
        for name, action, enabled in rows:
            sid = await store.add(name, "* * * * *", action, "usr_test")
            if not enabled:
                await store.toggle(sid, False, "usr_test")

    asyncio.run(_seed())


def test_schedule_action_types_pass_with_no_schedules(tmp_path):
    _seed_schedules(tmp_path)
    result = SecurityDoctor()._check_schedule_action_types(_sched_cfg(tmp_path))
    assert result.status == CheckStatus.PASS


def test_schedule_action_types_pass_with_known_type(tmp_path):
    _seed_schedules(tmp_path, ("morning", {"type": "briefing"}, True))
    result = SecurityDoctor()._check_schedule_action_types(_sched_cfg(tmp_path))
    assert result.status == CheckStatus.PASS


def test_schedule_action_types_critical_when_enabled_unknown(tmp_path):
    """The live version of issue #217: an enabled schedule firing no-ops every tick."""
    _seed_schedules(tmp_path, ("weird", {"type": "webhook_retry"}, True))
    result = SecurityDoctor()._check_schedule_action_types(_sched_cfg(tmp_path))
    assert result.status == CheckStatus.CRITICAL
    assert "webhook_retry" in result.message


async def test_schedule_action_types_runs_inside_a_running_loop(tmp_path):
    """`GET /api/doctor` and `ga_gate.evaluate()` call `run_all()` from a running loop."""
    await asyncio.to_thread(_seed_schedules, tmp_path, ("weird", {"type": "webhook_retry"}, True))
    result = SecurityDoctor()._check_schedule_action_types(_sched_cfg(tmp_path))
    assert result.status == CheckStatus.CRITICAL
    assert "webhook_retry" in result.message


def test_schedule_action_types_skips_without_creating_database(tmp_path):
    result = SecurityDoctor()._check_schedule_action_types(_sched_cfg(tmp_path))
    assert result.status == CheckStatus.SKIPPED
    assert result.message == "No database yet"
    assert not (tmp_path / "pincer.db").exists()


def test_schedule_action_types_skip_message_omits_sql(tmp_path):
    (tmp_path / "pincer.db").touch()
    result = SecurityDoctor()._check_schedule_action_types(_sched_cfg(tmp_path))
    assert result.status == CheckStatus.SKIPPED
    assert "SELECT" not in result.message
    assert "\n" not in result.message


def test_schedule_action_types_warns_when_only_disabled_unknown(tmp_path):
    """Already self-disabled by `run_scheduled_action` — stale, not actively firing."""
    _seed_schedules(tmp_path, ("weird", {"type": "webhook_retry"}, False))
    result = SecurityDoctor()._check_schedule_action_types(_sched_cfg(tmp_path))
    assert result.status == CheckStatus.WARNING
    assert "webhook_retry" in result.message


def test_google_workspace_check_reads_the_token_of_the_settings_it_is_given(monkeypatch, tmp_path):
    """The check follows the doctor's cfg pattern: the cfg's oauth dir decides, not the process settings."""
    from pydantic import SecretStr

    from pincer.integrations.google.config import GoogleIntegrationConfig

    monkeypatch.setattr(
        "pincer.integrations.google.config.load_config",
        lambda: GoogleIntegrationConfig(enabled=True, services=["calendar"]),
    )

    def _cfg(oauth_dir):
        return SimpleNamespace(
            google_token_json=SecretStr(""), google_credentials_json=SecretStr(""), google_oauth_dir=lambda: oauth_dir
        )

    with_token, without = tmp_path / "with", tmp_path / "without"
    with_token.mkdir()
    without.mkdir()
    (with_token / "google_workspace_token.json").write_text("{}")

    found = SecurityDoctor()._check_google_workspace(_cfg(with_token))
    missing = SecurityDoctor()._check_google_workspace(_cfg(without))

    assert (found.status, missing.status) == (CheckStatus.PASS, CheckStatus.SKIPPED)
    # Present, not proven valid: a revoked token passes this check too.
    assert "covers" not in found.message
    assert found.message.startswith("Google token found with scopes for: calendar")
    assert "not checked against Google" in found.message


@pytest.mark.parametrize(
    ("enabled", "status", "expected", "hint"),
    [
        (
            True,
            {"status": "disabled", "enabled_services": [], "missing_scopes": [], "source": None},
            CheckStatus.SKIPPED,
            "pincer google setup",
        ),
        (
            True,
            {"status": "partial", "enabled_services": ["gmail"], "missing_scopes": ["x"], "source": "file"},
            CheckStatus.WARNING,
            "pincer google setup",
        ),
        (
            True,
            {"status": "active", "enabled_services": ["calendar"], "missing_scopes": [], "source": "file"},
            CheckStatus.PASS,
            "",
        ),
        (
            False,
            {"status": "disabled", "enabled_services": [], "missing_scopes": [], "source": None},
            CheckStatus.SKIPPED,
            "",
        ),
    ],
)
def test_google_workspace_check(monkeypatch, enabled, status, expected, hint):
    """No token: calendar tools are off, and the doctor says how to connect (issue #297)."""
    from pincer.integrations.google.config import GoogleIntegrationConfig

    monkeypatch.setattr(
        "pincer.integrations.google.config.load_config", lambda: GoogleIntegrationConfig(enabled=enabled)
    )
    monkeypatch.setattr("pincer.integrations.google.google_status", lambda settings=None: status)
    result = SecurityDoctor()._check_google_workspace()
    assert result.status == expected
    assert result.category == "google"
    assert (hint in result.fix_hint) if hint else result.fix_hint == ""


# ── LLM effort and thinking (issue #345) ──────────────────────────────────────


def _llm_cfg(**update):
    """Explicit settings: every field the reasoning checks read is pinned, so the
    developer's .env never decides a result."""
    from pydantic import SecretStr

    from pincer.config import Settings

    base = Settings(
        anthropic_api_key="sk-ant-test",  # type: ignore[arg-type]
        telegram_bot_token="123456:TEST",  # type: ignore[arg-type]
        _env_file=None,  # type: ignore[call-arg]
    )
    pinned = {
        "default_provider": "anthropic",
        "default_model": "claude-haiku-5-5",
        "anthropic_model": "",
        "openai_model": "",
        "openai_api_key": "",
        "openai_compatible_provider": "",
        "anthropic_compatible_provider": "",
        "reasoning_effort": "",
        "thinking": "",
        "voice_enabled": True,
        "voice_turn_model": "",
        "voice_reasoning_effort": "",
        "voice_thinking": "",
        "voice_max_response_tokens": 1024,
        "summary_provider": None,
        "summary_model": "claude-haiku-5-5",
        "summary_reasoning_effort": "",
        "summary_thinking": "",
    }
    merged = {**pinned, **update}
    # model_copy doesn't validate: keys must already be SecretStr, as the providers read them
    merged["openai_api_key"] = SecretStr(merged["openai_api_key"])
    return base.model_copy(update=merged)


def test_llm_reasoning_default_shows_what_is_sent():
    result = SecurityDoctor()._check_llm_reasoning_default(_llm_cfg(reasoning_effort="low", thinking="adaptive"))

    assert result.status == CheckStatus.PASS
    assert result.category == "llm"
    assert "anthropic claude-haiku-5-5" in result.message
    assert "effort low" in result.message
    assert "thinking adaptive" in result.message


def test_llm_reasoning_with_nothing_set_says_so():
    result = SecurityDoctor()._check_llm_reasoning_default(_llm_cfg())

    assert result.status == CheckStatus.PASS
    assert result.message == "anthropic claude-haiku-5-5: nothing set; the model runs on its own defaults"


def test_llm_reasoning_shows_a_step_down():
    """Step-downs only log at DEBUG at request time; the doctor is where they show."""
    result = SecurityDoctor()._check_llm_reasoning_default(
        _llm_cfg(default_model="claude-opus-4-5", reasoning_effort="xhigh")
    )

    assert result.status == CheckStatus.PASS
    assert result.message == "anthropic claude-opus-4-5: effort xhigh → high (stepped down)"


@pytest.mark.parametrize(
    ("update", "message"),
    [
        (  # Haiku 4.5 takes no effort
            {"default_model": "claude-haiku-4-5-20251001", "reasoning_effort": "low"},
            "anthropic claude-haiku-4-5-20251001: nothing sent; effort low not sent: not supported by this model",
        ),
        (  # a deployment name Pincer can't place
            {"default_model": "voice-fast", "reasoning_effort": "low", "thinking": "adaptive"},
            "anthropic voice-fast: nothing sent; effort low not sent: unrecognised model; "
            "thinking adaptive not sent: unrecognised model",
        ),
        (  # disabled is rejected together with xhigh/max
            {"reasoning_effort": "xhigh", "thinking": "disabled"},
            "anthropic claude-haiku-5-5: effort xhigh; thinking disabled not sent: not allowed at effort xhigh or max",
        ),
    ],
    ids=["unsupported", "unrecognised", "invalid-combination"],
)
def test_llm_reasoning_warns_about_a_dropped_setting(update, message):
    result = SecurityDoctor()._check_llm_reasoning_default(_llm_cfg(**update))

    assert result.status == CheckStatus.WARNING
    assert result.message == message
    assert "PINCER_REASONING_EFFORT" in result.fix_hint or "PINCER_THINKING" in result.fix_hint


@pytest.mark.parametrize(
    ("update", "message"),
    [
        (  # one knob on OpenAI: disabled wins over the effort, without a warning
            {"reasoning_effort": "low", "thinking": "disabled"},
            "openai gpt-5.1: reasoning_effort none (thinking disabled replaces effort low)",
        ),
        ({"reasoning_effort": "max"}, "openai gpt-5.1: reasoning_effort max → high (stepped down)"),
        (
            {"thinking": "adaptive"},
            "openai gpt-5.1: thinking adaptive ignored (OpenAI models have no such setting)",
        ),
    ],
    ids=["disabled-replaces-effort", "step-down", "adaptive-ignored"],
)
def test_llm_reasoning_on_openai_shows_reasoning_effort(update, message):
    cfg = _llm_cfg(default_provider="openai", openai_api_key="sk-test", openai_model="gpt-5.1", **update)

    result = SecurityDoctor()._check_llm_reasoning_default(cfg)

    assert result.status == CheckStatus.PASS
    assert result.message == message


@pytest.mark.parametrize(
    ("update", "message"),
    [
        ({"voice_reasoning_effort": "low", "reasoning_effort": "high"}, "anthropic claude-haiku-5-5: effort low"),
        ({"reasoning_effort": "medium"}, "anthropic claude-haiku-5-5: effort medium"),  # empty inherits the global
    ],
    ids=["own-value", "inherits-global"],
)
def test_llm_reasoning_voice_uses_the_voice_settings(update, message):
    result = SecurityDoctor()._check_llm_reasoning_voice(_llm_cfg(**update))

    assert result.status == CheckStatus.PASS
    assert result.message == message


@pytest.mark.parametrize(
    ("check", "update"),
    [
        ("_check_llm_reasoning_voice", {"voice_reasoning_effort": "none", "voice_thinking": "none"}),
        ("_check_llm_reasoning_summary", {"summary_reasoning_effort": "none", "summary_thinking": "none"}),
    ],
    ids=["voice", "summary"],
)
def test_llm_reasoning_none_opts_out_of_the_global_settings(check, update):
    cfg = _llm_cfg(reasoning_effort="high", thinking="adaptive", **update)

    result = getattr(SecurityDoctor(), check)(cfg)

    assert result.status == CheckStatus.PASS
    assert result.message == "anthropic claude-haiku-5-5: nothing set; the model runs on its own defaults"


def test_voice_thinking_token_limit_check_honours_none():
    """Opus 4.7 thinks only when asked: the global `adaptive` would make it think, a `none` voice value doesn't."""
    base = {"voice_turn_model": "claude-opus-4-7", "thinking": "adaptive", "voice_max_response_tokens": 150}

    inherits = SecurityDoctor()._check_voice_thinking_token_limit(_llm_cfg(**base))
    opts_out = SecurityDoctor()._check_voice_thinking_token_limit(_llm_cfg(voice_thinking="none", **base))

    assert inherits.status == CheckStatus.WARNING  # precondition: the global setting does make it think
    assert opts_out.status == CheckStatus.PASS


def test_llm_reasoning_voice_on_another_provider_uses_its_wire():
    """The voice turn plan's effective provider and model: here OpenAI while the default is Anthropic."""
    cfg = _llm_cfg(openai_api_key="sk-test", voice_turn_model="openai:gpt-5.1", voice_reasoning_effort="low")

    result = SecurityDoctor()._check_llm_reasoning_voice(cfg)

    assert result.message == "openai gpt-5.1: reasoning_effort low"


def test_llm_reasoning_summary_resolves_against_the_summary_model():
    """The provider defaults to Haiku 5.5, but summaries ask for Sonnet 5.5, whose disabled is between_tools."""
    cfg = _llm_cfg(summary_model="claude-sonnet-5-5", summary_thinking="disabled", thinking="adaptive")

    result = SecurityDoctor()._check_llm_reasoning_summary(cfg)

    assert result.status == CheckStatus.PASS
    assert result.message == "anthropic claude-sonnet-5-5: thinking between_tools"


def test_llm_reasoning_summary_on_an_openai_provider_uses_its_own_model():
    """A claude-* summary_model on an OpenAI-wire summary provider is replaced by that provider's model."""
    cfg = _llm_cfg(
        openai_api_key="sk-test",
        openai_model="gpt-5.1",
        summary_provider="openai",
        summary_model="claude-haiku-4-5-20251001",
        summary_reasoning_effort="low",
    )

    result = SecurityDoctor()._check_llm_reasoning_summary(cfg)

    assert result.message == "openai gpt-5.1: reasoning_effort low"


@pytest.mark.parametrize(
    ("update", "status"),
    [
        ({"voice_max_response_tokens": 150}, CheckStatus.WARNING),  # Haiku 5.5 thinks by default
        ({"voice_max_response_tokens": 600}, CheckStatus.PASS),
        ({"voice_max_response_tokens": 150, "voice_thinking": "disabled"}, CheckStatus.PASS),
        # between_tools: no extended thinking, only short progress notes (Sonnet 5.5)
        (
            {"voice_max_response_tokens": 150, "default_model": "claude-sonnet-5-5", "voice_thinking": "disabled"},
            CheckStatus.PASS,
        ),
        # gpt-5.1 defaults to reasoning_effort none; gpt-5 to medium
        (
            {"voice_max_response_tokens": 150, "openai_api_key": "sk-test", "voice_turn_model": "openai:gpt-5.1"},
            CheckStatus.PASS,
        ),
        (
            {"voice_max_response_tokens": 150, "openai_api_key": "sk-test", "voice_turn_model": "openai:gpt-5"},
            CheckStatus.WARNING,
        ),
    ],
    ids=["thinks-150", "thinks-600", "disabled-150", "between-tools-150", "gpt-5.1-150", "gpt-5-150"],
)
def test_voice_thinking_token_limit(update, status):
    result = SecurityDoctor()._check_voice_thinking_token_limit(_llm_cfg(**update))

    assert result.status == status
    assert result.category == "voice"
    if status == CheckStatus.WARNING:
        assert "PINCER_VOICE_MAX_RESPONSE_TOKENS" in result.fix_hint


_LLM_REASONING_CHECKS = (
    "_check_llm_reasoning_default",
    "_check_llm_reasoning_voice",
    "_check_llm_reasoning_summary",
    "_check_voice_thinking_token_limit",
)


def test_llm_reasoning_checks_never_fail_the_deploy_gate():
    """Informational: `pincer doctor --production` exits non-zero on CRITICAL, so odd
    settings give a warning at most, never CRITICAL and never an exception."""
    cfg = _llm_cfg(
        default_provider="mystery",
        default_model="voice-fast",
        voice_turn_model="ghost:model-x",
        summary_provider="nope",
        summary_model="whatever",
        reasoning_effort="max",
        thinking="disabled",
        voice_max_response_tokens=32,
    )

    results = [getattr(SecurityDoctor(), check)(cfg) for check in _LLM_REASONING_CHECKS]

    assert [r.status for r in results] == [
        CheckStatus.WARNING,  # default: provider not configured
        CheckStatus.WARNING,  # voice: falls back to the default, also not configured
        CheckStatus.WARNING,  # summary: provider not configured
        CheckStatus.PASS,  # limit: can't tell whether an unknown model thinks
    ]
    assert "not configured" in results[0].message


def test_llm_reasoning_checks_make_no_network_call(monkeypatch):
    """No API call: any HTTP request through httpx (both SDKs use it) raises here."""
    import httpx

    def no_network(*_args, **_kwargs):
        raise AssertionError("the doctor made a network call")

    monkeypatch.setattr(httpx.Client, "send", no_network)
    monkeypatch.setattr(httpx.AsyncClient, "send", no_network)
    with pytest.raises(AssertionError, match="network call"):  # the guard is live
        httpx.Client().get("https://example.invalid")
    # Voice and summary on another provider than the default: the voice plan builds that leaf
    cfg = _llm_cfg(
        openai_api_key="sk-test",
        voice_turn_model="openai:gpt-5",
        summary_provider="openai",
        summary_model="gpt-5.1",
        reasoning_effort="low",
        voice_max_response_tokens=150,
    )

    results = [getattr(SecurityDoctor(), check)(cfg) for check in _LLM_REASONING_CHECKS]

    assert [r.message.split(":")[0] for r in results[:3]] == [
        "anthropic claude-haiku-5-5",
        "openai gpt-5",
        "openai gpt-5.1",
    ]
    assert results[3].status == CheckStatus.WARNING  # gpt-5 thinks by default at 150


def test_llm_reasoning_voice_is_skipped_when_voice_is_off():
    result = SecurityDoctor()._check_llm_reasoning_voice(_llm_cfg(voice_enabled=False, voice_outbound_enabled=False))

    assert result.status == CheckStatus.SKIPPED
