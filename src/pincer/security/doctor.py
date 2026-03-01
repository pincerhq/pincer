"""
Pincer Security Doctor — Runs 25+ security checks.

`pincer doctor` outputs a traffic-light report with pass/warning/critical checks
across secrets, access control, budget, filesystem, network, and runtime categories.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class CheckStatus(str, Enum):
    PASS = "pass"
    WARNING = "warning"
    CRITICAL = "critical"
    SKIPPED = "skipped"


@dataclass
class CheckResult:
    name: str
    status: CheckStatus
    message: str
    fix_hint: str = ""
    category: str = "general"


@dataclass
class DoctorReport:
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def passed(self) -> int:
        return sum(1 for c in self.checks if c.status == CheckStatus.PASS)

    @property
    def warnings(self) -> int:
        return sum(1 for c in self.checks if c.status == CheckStatus.WARNING)

    @property
    def critical(self) -> int:
        return sum(1 for c in self.checks if c.status == CheckStatus.CRITICAL)

    @property
    def score(self) -> int:
        if not self.checks:
            return 0
        total = len([c for c in self.checks if c.status != CheckStatus.SKIPPED])
        return int((self.passed / max(total, 1)) * 100)

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "passed": self.passed,
            "warnings": self.warnings,
            "critical": self.critical,
            "checks": [
                {
                    "name": c.name,
                    "status": c.status.value,
                    "message": c.message,
                    "fix_hint": c.fix_hint,
                    "category": c.category,
                }
                for c in self.checks
            ],
        }


class SecurityDoctor:
    """Runs comprehensive security checks on a Pincer installation."""

    def __init__(
        self,
        data_dir: Path | None = None,
        config_dir: Path | None = None,
        skills_dir: Path | None = None,
    ) -> None:
        self.data_dir = data_dir or Path("data")
        self.config_dir = config_dir or Path(".")
        self.skills_dir = skills_dir or Path.home() / ".pincer" / "skills"

    def run_all(self) -> DoctorReport:
        report = DoctorReport()
        # Secrets (6 checks)
        report.checks.append(self._check_env_file_permissions())
        report.checks.append(self._check_api_keys_not_in_config())
        report.checks.append(self._check_api_keys_not_in_git())
        report.checks.append(self._check_env_file_exists())
        report.checks.append(self._check_gitignore_has_env())
        report.checks.append(self._check_no_hardcoded_secrets())
        # Access Control (4 checks)
        report.checks.append(self._check_telegram_allowlist())
        report.checks.append(self._check_whatsapp_dm_policy())
        report.checks.append(self._check_discord_allowlist())
        report.checks.append(self._check_dashboard_auth_token())
        # Budget (3 checks)
        report.checks.append(self._check_budget_limits())
        report.checks.append(self._check_rate_limits())
        report.checks.append(self._check_tool_call_limits())
        # Filesystem (4 checks)
        report.checks.append(self._check_data_dir_permissions())
        report.checks.append(self._check_skills_dir_permissions())
        report.checks.append(self._check_no_world_readable_secrets())
        report.checks.append(self._check_sqlite_not_world_readable())
        # Network (2 checks)
        report.checks.append(self._check_dashboard_not_exposed())
        report.checks.append(self._check_no_debug_mode())
        # Deps (2 checks)
        report.checks.append(self._check_python_version())
        report.checks.append(self._check_dependencies_up_to_date())
        # Runtime (4 checks)
        report.checks.append(self._check_not_running_as_root())
        report.checks.append(self._check_audit_logging_enabled())
        report.checks.append(self._check_skill_sandbox_enabled())
        report.checks.append(self._check_tool_approval_mode())
        return report

    # ── Secrets ───────────────────────────────────────────

    def _check_env_file_permissions(self) -> CheckResult:
        env_path = self.config_dir / ".env"
        if not env_path.exists():
            return CheckResult(
                "env_file_permissions",
                CheckStatus.SKIPPED,
                "No .env file found",
                category="secrets",
            )
        mode = oct(env_path.stat().st_mode)[-3:]
        if mode in ("600", "400"):
            return CheckResult(
                "env_file_permissions",
                CheckStatus.PASS,
                f".env permissions {mode} (owner only)",
                category="secrets",
            )
        return CheckResult(
            "env_file_permissions",
            CheckStatus.CRITICAL,
            f".env permissions {mode} — too permissive!",
            fix_hint="chmod 600 .env",
            category="secrets",
        )

    def _check_api_keys_not_in_config(self) -> CheckResult:
        patterns = [
            r"sk-[a-zA-Z0-9]{20,}",
            r"sk-ant-[a-zA-Z0-9]{20,}",
            r"\d+:[A-Za-z0-9_-]{35}",
        ]
        config_files = (
            list(self.config_dir.glob("*.toml"))
            + list(self.config_dir.glob("*.yaml"))
            + list(self.config_dir.glob("*.yml"))
            + list(self.config_dir.glob("*.json"))
        )
        exposed = []
        for f in config_files:
            if f.name == ".env":
                continue
            try:
                content = f.read_text()
                for p in patterns:
                    if re.search(p, content):
                        exposed.append(f.name)
                        break
            except Exception:
                continue
        if not exposed:
            return CheckResult(
                "api_keys_not_in_config",
                CheckStatus.PASS,
                "No API keys in config files",
                category="secrets",
            )
        return CheckResult(
            "api_keys_not_in_config",
            CheckStatus.CRITICAL,
            f"API keys found in: {', '.join(exposed)}",
            fix_hint="Move all API keys to .env",
            category="secrets",
        )

    def _check_api_keys_not_in_git(self) -> CheckResult:
        try:
            result = subprocess.run(
                ["git", "log", "--oneline", "-20", "--all", "-p"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                return CheckResult(
                    "api_keys_not_in_git",
                    CheckStatus.SKIPPED,
                    "Not a git repo",
                    category="secrets",
                )
            for p in [r"sk-[a-zA-Z0-9]{20,}", r"sk-ant-[a-zA-Z0-9]{20,}"]:
                if re.search(p, result.stdout):
                    return CheckResult(
                        "api_keys_not_in_git",
                        CheckStatus.CRITICAL,
                        "API keys in git history!",
                        fix_hint="Use git-filter-repo to remove",
                        category="secrets",
                    )
            return CheckResult(
                "api_keys_not_in_git",
                CheckStatus.PASS,
                "No API keys in git history",
                category="secrets",
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return CheckResult(
                "api_keys_not_in_git",
                CheckStatus.SKIPPED,
                "git not available",
                category="secrets",
            )

    def _check_env_file_exists(self) -> CheckResult:
        if (self.config_dir / ".env").exists():
            return CheckResult(
                "env_file_exists",
                CheckStatus.PASS,
                ".env file found",
                category="secrets",
            )
        return CheckResult(
            "env_file_exists",
            CheckStatus.WARNING,
            "No .env file — using env vars directly?",
            fix_hint="pincer init",
            category="secrets",
        )

    def _check_gitignore_has_env(self) -> CheckResult:
        gi = self.config_dir / ".gitignore"
        if not gi.exists():
            return CheckResult(
                "gitignore_has_env",
                CheckStatus.WARNING,
                "No .gitignore",
                fix_hint="Create with: .env\\ndata/\\n*.db",
                category="secrets",
            )
        if ".env" in gi.read_text():
            return CheckResult(
                "gitignore_has_env",
                CheckStatus.PASS,
                ".env in .gitignore",
                category="secrets",
            )
        return CheckResult(
            "gitignore_has_env",
            CheckStatus.CRITICAL,
            ".env NOT in .gitignore!",
            fix_hint="Add .env to .gitignore",
            category="secrets",
        )

    def _check_no_hardcoded_secrets(self) -> CheckResult:
        src_dir = self.config_dir / "src"
        if not src_dir.exists():
            return CheckResult(
                "no_hardcoded_secrets",
                CheckStatus.SKIPPED,
                "No src/ directory",
                category="secrets",
            )
        suspicious = 0
        for py in src_dir.rglob("*.py"):
            try:
                content = py.read_text()
                if re.search(
                    r'(api_key|secret|token|password)\s*=\s*["\'][a-zA-Z0-9_-]{20,}["\']',
                    content,
                    re.I,
                ):
                    suspicious += 1
            except Exception:
                continue
        if suspicious == 0:
            return CheckResult(
                "no_hardcoded_secrets",
                CheckStatus.PASS,
                "No hardcoded secrets in source",
                category="secrets",
            )
        return CheckResult(
            "no_hardcoded_secrets",
            CheckStatus.CRITICAL,
            f"{suspicious} file(s) with potential hardcoded secrets",
            fix_hint="Use os.environ or pydantic-settings",
            category="secrets",
        )

    # ── Access Control ────────────────────────────────────

    def _check_telegram_allowlist(self) -> CheckResult:
        al = os.environ.get("PINCER_TELEGRAM_ALLOWED_USERS", "")
        if al.strip():
            return CheckResult(
                "telegram_allowlist",
                CheckStatus.PASS,
                f"Telegram allowlist configured ({len(al.split(','))} users)",
                category="access",
            )
        if os.environ.get("PINCER_TELEGRAM_BOT_TOKEN"):
            return CheckResult(
                "telegram_allowlist",
                CheckStatus.CRITICAL,
                "Telegram bot has no allowlist!",
                fix_hint="Set PINCER_TELEGRAM_ALLOWED_USERS=your_id",
                category="access",
            )
        return CheckResult(
            "telegram_allowlist",
            CheckStatus.SKIPPED,
            "Telegram not configured",
            category="access",
        )

    def _check_whatsapp_dm_policy(self) -> CheckResult:
        allowlist = os.environ.get("PINCER_WHATSAPP_DM_ALLOWLIST", "")
        if allowlist.strip():
            return CheckResult(
                "whatsapp_dm_policy",
                CheckStatus.PASS,
                "WhatsApp DM allowlist configured",
                category="access",
            )
        if os.environ.get("PINCER_WHATSAPP_ENABLED", "").lower() == "true":
            return CheckResult(
                "whatsapp_dm_policy",
                CheckStatus.PASS,
                "WhatsApp in self-chat-only mode (no DM allowlist)",
                category="access",
            )
        return CheckResult(
            "whatsapp_dm_policy",
            CheckStatus.SKIPPED,
            "WhatsApp not configured",
            category="access",
        )

    def _check_discord_allowlist(self) -> CheckResult:
        if os.environ.get("PINCER_DISCORD_GUILD_ALLOWLIST"):
            return CheckResult(
                "discord_allowlist",
                CheckStatus.PASS,
                "Discord guild allowlist configured",
                category="access",
            )
        if os.environ.get("PINCER_DISCORD_BOT_TOKEN"):
            return CheckResult(
                "discord_allowlist",
                CheckStatus.WARNING,
                "Discord bot has no guild allowlist",
                fix_hint="Set PINCER_DISCORD_GUILD_ALLOWLIST",
                category="access",
            )
        return CheckResult(
            "discord_allowlist",
            CheckStatus.SKIPPED,
            "Discord not configured",
            category="access",
        )

    def _check_dashboard_auth_token(self) -> CheckResult:
        token = os.environ.get("PINCER_DASHBOARD_TOKEN", "")
        if token and len(token) >= 16:
            return CheckResult(
                "dashboard_auth_token",
                CheckStatus.PASS,
                "Dashboard auth token configured (16+ chars)",
                category="access",
            )
        if token:
            return CheckResult(
                "dashboard_auth_token",
                CheckStatus.WARNING,
                "Dashboard token too short",
                fix_hint='python -c "import secrets; print(secrets.token_hex(32))"',
                category="access",
            )
        return CheckResult(
            "dashboard_auth_token",
            CheckStatus.CRITICAL,
            "No dashboard auth token!",
            fix_hint="Set PINCER_DASHBOARD_TOKEN",
            category="access",
        )

    # ── Budget ────────────────────────────────────────────

    def _check_budget_limits(self) -> CheckResult:
        daily = os.environ.get("PINCER_DAILY_BUDGET_USD")
        if daily:
            return CheckResult(
                "budget_limits",
                CheckStatus.PASS,
                f"Daily budget: ${float(daily):.2f}",
                category="budget",
            )
        return CheckResult(
            "budget_limits",
            CheckStatus.WARNING,
            "No daily budget (default $5)",
            fix_hint="Set PINCER_DAILY_BUDGET_USD=5",
            category="budget",
        )

    def _check_rate_limits(self) -> CheckResult:
        if os.environ.get("PINCER_RATE_MESSAGES_PER_MIN"):
            return CheckResult(
                "rate_limits",
                CheckStatus.PASS,
                "Message rate limit configured",
                category="budget",
            )
        return CheckResult(
            "rate_limits",
            CheckStatus.WARNING,
            "Using default rate limits (30/min)",
            category="budget",
        )

    def _check_tool_call_limits(self) -> CheckResult:
        configured = sum(
            1
            for k in ["PINCER_RATE_TOOLS_PER_MIN", "PINCER_MAX_CONCURRENT_LLM"]
            if os.environ.get(k)
        )
        if configured == 2:
            return CheckResult(
                "tool_call_limits",
                CheckStatus.PASS,
                "Tool + concurrency limits configured",
                category="budget",
            )
        return CheckResult(
            "tool_call_limits",
            CheckStatus.WARNING,
            "Not all limits explicitly configured",
            category="budget",
        )

    # ── Filesystem ────────────────────────────────────────

    def _check_data_dir_permissions(self) -> CheckResult:
        if not self.data_dir.exists():
            return CheckResult(
                "data_dir_permissions",
                CheckStatus.SKIPPED,
                "Data dir doesn't exist yet",
                category="filesystem",
            )
        mode = oct(self.data_dir.stat().st_mode)[-3:]
        if mode in ("700", "750"):
            return CheckResult(
                "data_dir_permissions",
                CheckStatus.PASS,
                f"Data dir permissions: {mode}",
                category="filesystem",
            )
        return CheckResult(
            "data_dir_permissions",
            CheckStatus.WARNING,
            f"Data dir permissions: {mode}",
            fix_hint="chmod 700 data/",
            category="filesystem",
        )

    def _check_skills_dir_permissions(self) -> CheckResult:
        if not self.skills_dir.exists():
            return CheckResult(
                "skills_dir_permissions",
                CheckStatus.SKIPPED,
                "Skills dir doesn't exist yet",
                category="filesystem",
            )
        mode = oct(self.skills_dir.stat().st_mode)[-3:]
        if mode in ("700", "750", "755"):
            return CheckResult(
                "skills_dir_permissions",
                CheckStatus.PASS,
                f"Skills dir permissions: {mode}",
                category="filesystem",
            )
        return CheckResult(
            "skills_dir_permissions",
            CheckStatus.WARNING,
            f"Skills dir permissions: {mode}",
            fix_hint="chmod 750 ~/.pincer/skills/",
            category="filesystem",
        )

    def _check_no_world_readable_secrets(self) -> CheckResult:
        world_readable = []
        for pattern in ["*.db", "*.key", "*.pem", "*.env*", "*.secret"]:
            for d in [self.config_dir, self.data_dir]:
                if not d.exists():
                    continue
                for f in d.glob(pattern):
                    if f.is_file() and f.stat().st_mode & stat.S_IROTH:
                        world_readable.append(f.name)
        if not world_readable:
            return CheckResult(
                "no_world_readable_secrets",
                CheckStatus.PASS,
                "No world-readable sensitive files",
                category="filesystem",
            )
        return CheckResult(
            "no_world_readable_secrets",
            CheckStatus.CRITICAL,
            f"World-readable: {', '.join(world_readable)}",
            fix_hint="chmod 600 <file>",
            category="filesystem",
        )

    def _check_sqlite_not_world_readable(self) -> CheckResult:
        dbs = list(self.data_dir.glob("*.db")) if self.data_dir.exists() else []
        if not dbs:
            return CheckResult(
                "sqlite_not_world_readable",
                CheckStatus.SKIPPED,
                "No database files",
                category="filesystem",
            )
        exposed = [f.name for f in dbs if f.stat().st_mode & stat.S_IROTH]
        if not exposed:
            return CheckResult(
                "sqlite_not_world_readable",
                CheckStatus.PASS,
                f"All {len(dbs)} databases protected",
                category="filesystem",
            )
        return CheckResult(
            "sqlite_not_world_readable",
            CheckStatus.CRITICAL,
            f"World-readable DBs: {', '.join(exposed)}",
            fix_hint="chmod 600 data/*.db",
            category="filesystem",
        )

    # ── Network ───────────────────────────────────────────

    def _check_dashboard_not_exposed(self) -> CheckResult:
        host = os.environ.get("PINCER_DASHBOARD_HOST", "127.0.0.1")
        if host in ("127.0.0.1", "localhost", "::1"):
            return CheckResult(
                "dashboard_not_exposed",
                CheckStatus.PASS,
                f"Dashboard bound to {host}",
                category="network",
            )
        return CheckResult(
            "dashboard_not_exposed",
            CheckStatus.WARNING,
            f"Dashboard bound to {host} — network accessible",
            fix_hint="Set PINCER_DASHBOARD_HOST=127.0.0.1",
            category="network",
        )

    def _check_no_debug_mode(self) -> CheckResult:
        if os.environ.get("PINCER_DEBUG", "").lower() in ("true", "1"):
            return CheckResult(
                "no_debug_mode",
                CheckStatus.WARNING,
                "Debug mode ON",
                fix_hint="Set PINCER_DEBUG=false in production",
                category="network",
            )
        return CheckResult(
            "no_debug_mode",
            CheckStatus.PASS,
            "Debug mode OFF",
            category="network",
        )

    # ── Dependencies ──────────────────────────────────────

    def _check_python_version(self) -> CheckResult:
        v = sys.version_info
        if v >= (3, 11):
            return CheckResult(
                "python_version",
                CheckStatus.PASS,
                f"Python {v.major}.{v.minor}.{v.micro}",
                category="deps",
            )
        return CheckResult(
            "python_version",
            CheckStatus.WARNING,
            f"Python {v.major}.{v.minor} — recommend 3.11+",
            category="deps",
        )

    def _check_dependencies_up_to_date(self) -> CheckResult:
        try:
            result = subprocess.run(
                ["uv", "pip", "list", "--outdated", "--format=json"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0:
                outdated = (
                    json.loads(result.stdout) if result.stdout.strip() else []
                )
                critical = {"anthropic", "openai", "httpx", "cryptography"}
                crit_outdated = [
                    p
                    for p in outdated
                    if p.get("name", "").lower() in critical
                ]
                if crit_outdated:
                    names = ", ".join(p["name"] for p in crit_outdated)
                    return CheckResult(
                        "deps_up_to_date",
                        CheckStatus.WARNING,
                        f"Outdated: {names}",
                        fix_hint="uv sync --upgrade",
                        category="deps",
                    )
                return CheckResult(
                    "deps_up_to_date",
                    CheckStatus.PASS,
                    "Security deps up to date",
                    category="deps",
                )
        except Exception:
            pass
        return CheckResult(
            "deps_up_to_date",
            CheckStatus.SKIPPED,
            "Could not check deps",
            category="deps",
        )

    # ── Runtime ───────────────────────────────────────────

    def _check_not_running_as_root(self) -> CheckResult:
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            return CheckResult(
                "not_running_as_root",
                CheckStatus.CRITICAL,
                "Running as root!",
                fix_hint="useradd -m pincer && su pincer",
                category="runtime",
            )
        return CheckResult(
            "not_running_as_root",
            CheckStatus.PASS,
            f"Running as: {os.getenv('USER', 'unknown')}",
            category="runtime",
        )

    def _check_audit_logging_enabled(self) -> CheckResult:
        if os.environ.get("PINCER_AUDIT_DISABLED", "").lower() in ("true", "1"):
            return CheckResult(
                "audit_logging_enabled",
                CheckStatus.WARNING,
                "Audit logging disabled",
                fix_hint="Remove PINCER_AUDIT_DISABLED",
                category="runtime",
            )
        return CheckResult(
            "audit_logging_enabled",
            CheckStatus.PASS,
            "Audit logging enabled",
            category="runtime",
        )

    def _check_skill_sandbox_enabled(self) -> CheckResult:
        if os.environ.get("PINCER_SKILL_SANDBOX_DISABLED", "").lower() in (
            "true",
            "1",
        ):
            return CheckResult(
                "skill_sandbox_enabled",
                CheckStatus.CRITICAL,
                "Skill sandbox DISABLED!",
                fix_hint="Remove PINCER_SKILL_SANDBOX_DISABLED",
                category="runtime",
            )
        return CheckResult(
            "skill_sandbox_enabled",
            CheckStatus.PASS,
            "Skill sandbox enabled",
            category="runtime",
        )

    def _check_tool_approval_mode(self) -> CheckResult:
        mode = os.environ.get("PINCER_TOOL_APPROVAL", "auto")
        if mode in ("manual", "allowlist"):
            return CheckResult(
                "tool_approval_mode",
                CheckStatus.PASS,
                f"Tool approval: {mode}",
                category="runtime",
            )
        return CheckResult(
            "tool_approval_mode",
            CheckStatus.WARNING,
            f"Tool approval: {mode}",
            fix_hint="Set PINCER_TOOL_APPROVAL=allowlist",
            category="runtime",
        )
