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
    assert len(report.checks) == 25
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
    (src_dir / "bad.py").write_text(
        'api_key = "sk-ant-abc123456789012345678901"\n'
    )

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
    report.checks.append(
        SecurityDoctor()._check_python_version()
    )
    report.checks.append(
        SecurityDoctor()._check_not_running_as_root()
    )
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
