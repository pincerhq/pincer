"""Tests for the security doctor."""

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
    assert len(report.checks) == 42  # 31 original + 7 MCP + 3 MCP security + 1 WA neonize
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
    fake.__version__ = "0.3.16.post0"  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "neonize", fake)

    result = SecurityDoctor()._check_whatsapp_neonize_version()
    assert result.status == CheckStatus.PASS
    assert "0.3.16" in result.message


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


def test_dashboard_auth_token_pass_from_dotenv_only(tmp_path, monkeypatch):
    """Root-cause regression for #117.

    PINCER_DASHBOARD_TOKEN set only in .env (not in shell) must make
    _check_dashboard_auth_token return PASS, not CRITICAL.
    """
    from pincer.config import get_settings_relaxed

    (tmp_path / ".env").write_text("PINCER_DASHBOARD_TOKEN=a-secure-32-char-token-for-test\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PINCER_DASHBOARD_TOKEN", raising=False)

    get_settings_relaxed.cache_clear()
    try:
        doc = SecurityDoctor(config_dir=tmp_path, data_dir=tmp_path)
        result = doc._check_dashboard_auth_token()
        assert result.status == CheckStatus.PASS
    finally:
        get_settings_relaxed.cache_clear()


def test_telegram_allowlist_pass_from_dotenv_only(tmp_path, monkeypatch):
    """Telegram allowlist configured only in .env must show as configured."""
    from pincer.config import get_settings_relaxed

    (tmp_path / ".env").write_text("PINCER_TELEGRAM_BOT_TOKEN=123456:TEST\nPINCER_TELEGRAM_ALLOWED_USERS=[111,222]\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PINCER_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("PINCER_TELEGRAM_ALLOWED_USERS", raising=False)

    get_settings_relaxed.cache_clear()
    try:
        doc = SecurityDoctor(config_dir=tmp_path, data_dir=tmp_path)
        result = doc._check_telegram_allowlist()
        assert result.status == CheckStatus.PASS
    finally:
        get_settings_relaxed.cache_clear()


# ── Additional coverage: access control checks ────────────────────────────────


def test_telegram_allowlist_critical_no_list():
    from unittest.mock import MagicMock

    doc = SecurityDoctor()
    cfg = MagicMock()
    cfg.telegram_allowed_users = []
    cfg.telegram_bot_token.get_secret_value.return_value = "123456:TOKEN"
    result = doc._check_telegram_allowlist(cfg)
    assert result.status == CheckStatus.CRITICAL


def test_telegram_allowlist_skipped_not_configured():
    from unittest.mock import MagicMock

    doc = SecurityDoctor()
    cfg = MagicMock()
    cfg.telegram_allowed_users = []
    cfg.telegram_bot_token.get_secret_value.return_value = ""
    result = doc._check_telegram_allowlist(cfg)
    assert result.status == CheckStatus.SKIPPED


def test_whatsapp_dm_policy_pass_with_allowlist():
    from unittest.mock import MagicMock

    doc = SecurityDoctor()
    cfg = MagicMock()
    cfg.whatsapp_dm_allowlist = "+1234567890"
    result = doc._check_whatsapp_dm_policy(cfg)
    assert result.status == CheckStatus.PASS


def test_whatsapp_dm_policy_pass_self_chat_only():
    from unittest.mock import MagicMock

    doc = SecurityDoctor()
    cfg = MagicMock()
    cfg.whatsapp_dm_allowlist = "  "
    cfg.whatsapp_enabled = True
    result = doc._check_whatsapp_dm_policy(cfg)
    assert result.status == CheckStatus.PASS


def test_whatsapp_dm_policy_skipped_not_configured():
    from unittest.mock import MagicMock

    doc = SecurityDoctor()
    cfg = MagicMock()
    cfg.whatsapp_dm_allowlist = ""
    cfg.whatsapp_enabled = False
    result = doc._check_whatsapp_dm_policy(cfg)
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


def test_dashboard_auth_token_short_warning():
    from unittest.mock import MagicMock

    doc = SecurityDoctor()
    cfg = MagicMock()
    cfg.dashboard_token.get_secret_value.return_value = "tooshort"
    result = doc._check_dashboard_auth_token(cfg)
    assert result.status == CheckStatus.WARNING


def test_dashboard_auth_token_critical_missing():
    from unittest.mock import MagicMock

    doc = SecurityDoctor()
    cfg = MagicMock()
    cfg.dashboard_token.get_secret_value.return_value = ""
    result = doc._check_dashboard_auth_token(cfg)
    assert result.status == CheckStatus.CRITICAL


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


def test_tool_approval_mode_pass_manual():
    from unittest.mock import MagicMock

    doc = SecurityDoctor()
    cfg = MagicMock()
    cfg.tool_approval = "manual"
    result = doc._check_tool_approval_mode(cfg)
    assert result.status == CheckStatus.PASS


def test_tool_approval_mode_pass_allowlist():
    from unittest.mock import MagicMock

    doc = SecurityDoctor()
    cfg = MagicMock()
    cfg.tool_approval = "allowlist"
    result = doc._check_tool_approval_mode(cfg)
    assert result.status == CheckStatus.PASS


def test_tool_approval_mode_warning_auto():
    from unittest.mock import MagicMock

    doc = SecurityDoctor()
    cfg = MagicMock()
    cfg.tool_approval = "auto"
    result = doc._check_tool_approval_mode(cfg)
    assert result.status == CheckStatus.WARNING


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


def test_signal_allowlist_skipped():
    from unittest.mock import MagicMock

    doc = SecurityDoctor()
    cfg = MagicMock()
    cfg.signal_enabled = False
    result = doc._check_signal_allowlist(cfg)
    assert result.status == CheckStatus.SKIPPED


def test_signal_allowlist_pass():
    from unittest.mock import MagicMock

    doc = SecurityDoctor()
    cfg = MagicMock()
    cfg.signal_enabled = True
    cfg.signal_allowlist = "+1234567890"
    result = doc._check_signal_allowlist(cfg)
    assert result.status == CheckStatus.PASS


def test_signal_allowlist_warning_empty():
    from unittest.mock import MagicMock

    doc = SecurityDoctor()
    cfg = MagicMock()
    cfg.signal_enabled = True
    cfg.signal_allowlist = ""
    result = doc._check_signal_allowlist(cfg)
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
