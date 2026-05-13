"""Tests for the security doctor."""

import os

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
    assert len(report.checks) == 43  # 31 original + 8 MCP + 3 MCP security + 1 WA neonize
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
    (src_dir / "bad.py").write_text('api_key = "sk-ant-abc123456789012345678901"\n')

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
    doc = SecurityDoctor()
    old = os.environ.pop("PINCER_DASHBOARD_HOST", None)
    try:
        result = doc._check_dashboard_not_exposed()
        assert result.status == CheckStatus.PASS
    finally:
        if old is not None:
            os.environ["PINCER_DASHBOARD_HOST"] = old


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

    (tmp_path / ".env").write_text(
        "PINCER_DASHBOARD_TOKEN=a-secure-32-char-token-for-test\n"
    )
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

    (tmp_path / ".env").write_text(
        "PINCER_TELEGRAM_BOT_TOKEN=123456:TEST\n"
        "PINCER_TELEGRAM_ALLOWED_USERS=[111,222]\n"
    )
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
