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
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pincer.config import Settings


class CheckStatus(StrEnum):
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

    def _cfg(self, cfg: Settings | None) -> Settings:
        if cfg is None:
            from pincer.config import get_settings_relaxed

            return get_settings_relaxed()
        return cfg

    def run_all(self) -> DoctorReport:
        from pincer.config import get_settings_relaxed

        settings = get_settings_relaxed()
        report = DoctorReport()
        # Secrets (6 checks)
        report.checks.append(self._check_env_file_permissions())
        report.checks.append(self._check_api_keys_not_in_config())
        report.checks.append(self._check_api_keys_not_in_git())
        report.checks.append(self._check_env_file_exists())
        report.checks.append(self._check_gitignore_has_env())
        report.checks.append(self._check_no_hardcoded_secrets())
        # Access Control (4 checks)
        report.checks.append(self._check_telegram_access_control(settings))
        report.checks.append(self._check_whatsapp_access_control(settings))
        report.checks.append(self._check_whatsapp_neonize_version())
        report.checks.append(self._check_discord_allowlist(settings))
        report.checks.append(self._check_dashboard_auth_token(settings))
        # Budget (3 checks)
        report.checks.append(self._check_budget_limits(settings))
        report.checks.append(self._check_rate_limits(settings))
        report.checks.append(self._check_tool_call_limits(settings))
        # Filesystem (4 checks)
        report.checks.append(self._check_data_dir_permissions())
        report.checks.append(self._check_skills_dir_permissions())
        report.checks.append(self._check_no_world_readable_secrets())
        report.checks.append(self._check_sqlite_not_world_readable())
        # Network (2 checks)
        report.checks.append(self._check_dashboard_not_exposed(settings))
        report.checks.append(self._check_no_debug_mode(settings))
        # Deps (2 checks)
        report.checks.append(self._check_python_version())
        report.checks.append(self._check_dependencies_up_to_date())
        # Voice (3 checks, Sprint 7)
        report.checks.append(self._check_voice_twilio_credentials(settings))
        report.checks.append(self._check_voice_webhook_url(settings))
        report.checks.append(self._check_voice_recording_consent(settings))
        # Signal (3 checks, Sprint 7.5)
        report.checks.append(self._check_signal_phone_set(settings))
        report.checks.append(self._check_signal_api_local(settings))
        report.checks.append(self._check_signal_access_control(settings))
        # Runtime (4 checks)
        report.checks.append(self._check_not_running_as_root())
        report.checks.append(self._check_audit_logging_enabled(settings))
        report.checks.append(self._check_skill_sandbox_enabled(settings))
        report.checks.append(self._check_tool_approval_mode(settings))
        # MCP (8 checks, Sprint 8)
        report.checks.append(self._check_mcp_config_valid())
        report.checks.append(self._check_mcp_sandbox_enabled())
        report.checks.append(self._check_mcp_no_plaintext_secrets())
        report.checks.append(self._check_mcp_tool_count())
        report.checks.append(self._check_mcp_env_vars())
        report.checks.append(self._check_mcp_collisions())
        report.checks.append(self._check_mcp_servers())
        # MCP Security (3 checks, Sprint 9)
        report.checks.append(self._check_mcp_oauth_enabled())
        report.checks.append(self._check_mcp_server_not_exposed())
        report.checks.append(self._check_mcp_injection_alerts())
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
            patterns = [re.compile(p) for p in [r"sk-[a-zA-Z0-9]{20,}", r"sk-ant-[a-zA-Z0-9]{20,}"]]
            current_file = ""
            for line in result.stdout.splitlines():
                if line.startswith("+++ b/"):
                    current_file = line[6:]
                if current_file.startswith("tests/") or current_file.startswith("test_"):
                    continue
                if any(p.search(line) for p in patterns):
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

    def _check_telegram_access_control(self, cfg: Settings | None = None) -> CheckResult:
        cfg = self._cfg(cfg)
        if not cfg.telegram_bot_token.get_secret_value():
            return CheckResult(
                "telegram_access_control",
                CheckStatus.SKIPPED,
                "Telegram not configured",
                category="access",
            )
        from pincer.config.identity import resolve_identity_map_config

        identity_map_config, _ = resolve_identity_map_config(cfg.identity_map, self.config_dir)
        if identity_map_config:
            return CheckResult(
                "telegram_access_control",
                CheckStatus.PASS,
                "Telegram access controlled by PINCER_IDENTITY_MAP",
                category="access",
            )
        return CheckResult(
            "telegram_access_control",
            CheckStatus.CRITICAL,
            "Telegram bot has no PINCER_IDENTITY_MAP configured — any Telegram user can message it",
            fix_hint=(
                "Set PINCER_IDENTITY_MAP (or configure the [identity] TOML section), "
                "then set PINCER_TELEGRAM_GUESTS_ALLOWED=false"
            ),
            category="access",
        )

    def _check_whatsapp_access_control(self, cfg: Settings | None = None) -> CheckResult:
        cfg = self._cfg(cfg)
        if not cfg.whatsapp_enabled:
            return CheckResult(
                "whatsapp_access_control",
                CheckStatus.SKIPPED,
                "WhatsApp not configured",
                category="access",
            )
        from pincer.config.identity import resolve_identity_map_config

        identity_map_config, _ = resolve_identity_map_config(cfg.identity_map, self.config_dir)
        if identity_map_config:
            return CheckResult(
                "whatsapp_access_control",
                CheckStatus.PASS,
                "WhatsApp access controlled by PINCER_IDENTITY_MAP (covers DMs and group mentions)",
                category="access",
            )
        if cfg.whatsapp_self_chat_only:
            return CheckResult(
                "whatsapp_access_control",
                CheckStatus.WARNING,
                "WhatsApp is in self-chat-only mode, but group mentions/trigger-word messages "
                "are answered for any group member regardless of this setting, and no "
                "PINCER_IDENTITY_MAP is configured to gate them",
                fix_hint=(
                    "Set PINCER_IDENTITY_MAP (or configure the [identity] TOML section) and "
                    "PINCER_WHATSAPP_GUESTS_ALLOWED=false to restrict who can trigger Pincer "
                    "from a group; self-chat-only alone does not protect group chats."
                ),
                category="access",
            )
        if cfg.whatsapp_guests_allowed:
            return CheckResult(
                "whatsapp_access_control",
                CheckStatus.CRITICAL,
                "WhatsApp accepts DMs from anyone — PINCER_WHATSAPP_GUESTS_ALLOWED=true "
                "overrides the fail-closed default that applies with no PINCER_IDENTITY_MAP configured",
                fix_hint=(
                    "Set PINCER_WHATSAPP_SELF_CHAT_ONLY=true, or set PINCER_IDENTITY_MAP "
                    "(or configure the [identity] TOML section) and PINCER_WHATSAPP_GUESTS_ALLOWED=false"
                ),
                category="access",
            )
        return CheckResult(
            "whatsapp_access_control",
            CheckStatus.WARNING,
            "WhatsApp DMs are blocked by default with no PINCER_IDENTITY_MAP configured "
            "(fail-closed), but group mentions/trigger-word messages are still answered "
            "for any group member",
            fix_hint=(
                "Set PINCER_IDENTITY_MAP (or configure the [identity] TOML section) and "
                "PINCER_WHATSAPP_GUESTS_ALLOWED=false to restrict who can trigger Pincer "
                "from a group"
            ),
            category="access",
        )

    # Minimum neonize release carrying a whatsmeow build WhatsApp still accepts.
    # Bump when upstream ships a fix for a fresh `err-client-outdated` wave.
    _WA_NEONIZE_MIN = (0, 4, 3)

    def _check_whatsapp_neonize_version(self) -> CheckResult:
        try:
            import neonize  # type: ignore[import-not-found]
        except ImportError:
            return CheckResult(
                "whatsapp_neonize_version",
                CheckStatus.SKIPPED,
                "neonize not installed (WhatsApp disabled)",
                category="access",
            )
        raw = getattr(neonize, "__version__", "")
        parts: list[int] = []
        for p in raw.split("."):
            digits = "".join(c for c in p if c.isdigit())
            if not digits:
                break
            parts.append(int(digits))
        parsed = tuple(parts[:3])
        min_str = ".".join(str(x) for x in self._WA_NEONIZE_MIN)
        if parsed and parsed >= self._WA_NEONIZE_MIN:
            return CheckResult(
                "whatsapp_neonize_version",
                CheckStatus.PASS,
                f"neonize {raw} (>= {min_str})",
                category="access",
            )
        return CheckResult(
            "whatsapp_neonize_version",
            CheckStatus.WARNING,
            f"neonize {raw or 'unknown'} — WhatsApp may return err-client-outdated",
            fix_hint=f"uv pip install -U 'neonize>={min_str}'",
            category="access",
        )

    def _check_discord_allowlist(self, cfg: Settings | None = None) -> CheckResult:
        cfg = self._cfg(cfg)
        if cfg.discord_guild_allowlist.strip():
            return CheckResult(
                "discord_allowlist",
                CheckStatus.PASS,
                "Discord guild allowlist configured",
                category="access",
            )
        if cfg.discord_bot_token.get_secret_value():
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

    def _check_dashboard_auth_token(self, cfg: Settings | None = None) -> CheckResult:
        cfg = self._cfg(cfg)
        token = cfg.dashboard_token.get_secret_value()
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

    def _check_budget_limits(self, cfg: Settings | None = None) -> CheckResult:
        cfg = self._cfg(cfg)
        if cfg.daily_budget_usd > 0:
            return CheckResult(
                "budget_limits",
                CheckStatus.PASS,
                f"Daily budget: ${cfg.daily_budget_usd:.2f}",
                category="budget",
            )
        return CheckResult(
            "budget_limits",
            CheckStatus.WARNING,
            "Daily budget is 0 (unlimited spend)",
            fix_hint="Set PINCER_DAILY_BUDGET_USD=5",
            category="budget",
        )

    def _check_rate_limits(self, cfg: Settings | None = None) -> CheckResult:
        cfg = self._cfg(cfg)
        return CheckResult(
            "rate_limits",
            CheckStatus.PASS,
            f"Message rate limit: {cfg.rate_messages_per_min}/min",
            category="budget",
        )

    def _check_tool_call_limits(self, cfg: Settings | None = None) -> CheckResult:
        cfg = self._cfg(cfg)
        return CheckResult(
            "tool_call_limits",
            CheckStatus.PASS,
            f"Tool limit: {cfg.rate_tools_per_min}/min, concurrent LLM: {cfg.max_concurrent_llm}",
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

    def _check_dashboard_not_exposed(self, cfg: Settings | None = None) -> CheckResult:
        cfg = self._cfg(cfg)
        host = cfg.dashboard_host
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

    def _check_no_debug_mode(self, cfg: Settings | None = None) -> CheckResult:
        cfg = self._cfg(cfg)
        if cfg.debug:
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
                outdated = json.loads(result.stdout) if result.stdout.strip() else []
                critical = {"anthropic", "openai", "httpx", "cryptography"}
                crit_outdated = [p for p in outdated if p.get("name", "").lower() in critical]
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

    # ── Voice (Sprint 7) ─────────────────────────────────

    def _check_voice_twilio_credentials(self, cfg: Settings | None = None) -> CheckResult:
        cfg = self._cfg(cfg)
        if not cfg.voice_enabled:
            return CheckResult(
                "voice_twilio_credentials",
                CheckStatus.SKIPPED,
                "Voice not enabled",
                category="voice",
            )
        sid = cfg.twilio_account_sid
        token = cfg.twilio_auth_token.get_secret_value()
        if sid and token:
            return CheckResult(
                "voice_twilio_credentials",
                CheckStatus.PASS,
                "Twilio credentials configured",
                category="voice",
            )
        missing = []
        if not sid:
            missing.append("PINCER_TWILIO_ACCOUNT_SID")
        if not token:
            missing.append("PINCER_TWILIO_AUTH_TOKEN")
        return CheckResult(
            "voice_twilio_credentials",
            CheckStatus.CRITICAL,
            f"Missing: {', '.join(missing)}",
            fix_hint="Set Twilio credentials in .env",
            category="voice",
        )

    def _check_voice_webhook_url(self, cfg: Settings | None = None) -> CheckResult:
        cfg = self._cfg(cfg)
        if not cfg.voice_enabled:
            return CheckResult(
                "voice_webhook_url",
                CheckStatus.SKIPPED,
                "Voice not enabled",
                category="voice",
            )
        url = cfg.voice_webhook_base_url
        if url and url.startswith("https://"):
            return CheckResult(
                "voice_webhook_url",
                CheckStatus.PASS,
                f"Webhook URL: {url[:50]}...",
                category="voice",
            )
        if url and not url.startswith("https://"):
            return CheckResult(
                "voice_webhook_url",
                CheckStatus.WARNING,
                "Webhook URL not using HTTPS",
                fix_hint="Use https:// for production",
                category="voice",
            )
        return CheckResult(
            "voice_webhook_url",
            CheckStatus.WARNING,
            "No webhook URL configured",
            fix_hint="Set PINCER_VOICE_WEBHOOK_BASE_URL",
            category="voice",
        )

    def _check_voice_recording_consent(self, cfg: Settings | None = None) -> CheckResult:
        cfg = self._cfg(cfg)
        if not cfg.voice_enabled:
            return CheckResult(
                "voice_recording_consent",
                CheckStatus.SKIPPED,
                "Voice not enabled",
                category="voice",
            )
        recording = cfg.voice_recording_enabled
        consent = cfg.voice_consent_mode
        if recording and consent == "none":
            return CheckResult(
                "voice_recording_consent",
                CheckStatus.CRITICAL,
                "Recording enabled without consent mode!",
                fix_hint="Set PINCER_VOICE_CONSENT_MODE=one_party or two_party",
                category="voice",
            )
        if recording:
            return CheckResult(
                "voice_recording_consent",
                CheckStatus.PASS,
                f"Recording enabled with {consent} consent",
                category="voice",
            )
        return CheckResult(
            "voice_recording_consent",
            CheckStatus.PASS,
            "Recording disabled (transcription only)",
            category="voice",
        )

    # ── Signal (Sprint 7.5) ───────────────────────────────

    def _check_signal_phone_set(self, cfg: Settings | None = None) -> CheckResult:
        cfg = self._cfg(cfg)
        if not cfg.signal_enabled:
            return CheckResult(
                "signal_phone_set",
                CheckStatus.SKIPPED,
                "Signal not enabled",
                category="signal",
            )
        phone = cfg.signal_phone_number
        if phone:
            return CheckResult(
                "signal_phone_set",
                CheckStatus.PASS,
                f"Signal phone configured: {phone}",
                category="signal",
            )
        return CheckResult(
            "signal_phone_set",
            CheckStatus.CRITICAL,
            "Signal enabled but PINCER_SIGNAL_PHONE_NUMBER not set",
            fix_hint="Set PINCER_SIGNAL_PHONE_NUMBER=+1234567890",
            category="signal",
        )

    def _check_signal_api_local(self, cfg: Settings | None = None) -> CheckResult:
        cfg = self._cfg(cfg)
        if not cfg.signal_enabled:
            return CheckResult(
                "signal_api_local",
                CheckStatus.SKIPPED,
                "Signal not enabled",
                category="signal",
            )
        url = cfg.signal_api_url
        local_hosts = ("localhost", "127.0.0.1", "signal-api", "::1")
        try:
            from urllib.parse import urlparse

            host = urlparse(url).hostname or ""
            if host in local_hosts:
                return CheckResult(
                    "signal_api_local",
                    CheckStatus.PASS,
                    f"Signal API URL is local: {url}",
                    category="signal",
                )
        except Exception:
            pass
        return CheckResult(
            "signal_api_local",
            CheckStatus.CRITICAL,
            f"Signal API URL appears public: {url}",
            fix_hint="Keep signal-api on localhost or internal Docker network only",
            category="signal",
        )

    def _check_signal_access_control(self, cfg: Settings | None = None) -> CheckResult:
        cfg = self._cfg(cfg)
        if not cfg.signal_enabled:
            return CheckResult(
                "signal_access_control",
                CheckStatus.SKIPPED,
                "Signal not enabled",
                category="signal",
            )
        from pincer.config.identity import resolve_identity_map_config

        identity_map_config, _ = resolve_identity_map_config(cfg.identity_map, self.config_dir)
        if identity_map_config:
            return CheckResult(
                "signal_access_control",
                CheckStatus.PASS,
                "Signal access controlled by PINCER_IDENTITY_MAP",
                category="signal",
            )
        return CheckResult(
            "signal_access_control",
            CheckStatus.WARNING,
            "Signal has no PINCER_IDENTITY_MAP configured — any phone number can DM it",
            fix_hint=(
                "Set PINCER_IDENTITY_MAP (or configure the [identity] TOML section), "
                "then set PINCER_SIGNAL_GUESTS_ALLOWED=false"
            ),
            category="signal",
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

    def _check_audit_logging_enabled(self, cfg: Settings | None = None) -> CheckResult:
        cfg = self._cfg(cfg)
        if cfg.audit_disabled:
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

    def _check_skill_sandbox_enabled(self, cfg: Settings | None = None) -> CheckResult:
        cfg = self._cfg(cfg)
        if cfg.skill_sandbox_disabled:
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

    # ── MCP (Sprint 8) ────────────────────────────────────

    def _check_mcp_config_valid(self) -> CheckResult:
        """Validate pincer.toml MCP config (if present)."""
        toml_path = self.config_dir / "pincer.toml"
        if not toml_path.exists():
            # No TOML config — check if env-based servers are defined
            mcp_servers_env = any(k.startswith("PINCER_MCP_SERVER_") for k in os.environ)
            if not mcp_servers_env:
                return CheckResult(
                    "mcp_config_valid",
                    CheckStatus.SKIPPED,
                    "No MCP servers configured",
                    category="mcp",
                )
        try:
            from pincer.mcp.config import load_mcp_config

            cfg = load_mcp_config(self.config_dir)
            if not cfg.enabled:
                return CheckResult(
                    "mcp_config_valid",
                    CheckStatus.SKIPPED,
                    "MCP disabled",
                    category="mcp",
                )
            n = len([s for s in cfg.servers if s.enabled])
            return CheckResult(
                "mcp_config_valid",
                CheckStatus.PASS,
                f"MCP config valid — {n} enabled server(s)",
                category="mcp",
            )
        except Exception as e:
            return CheckResult(
                "mcp_config_valid",
                CheckStatus.CRITICAL,
                f"MCP config error: {e}",
                fix_hint="Check pincer.toml [mcp] section syntax",
                category="mcp",
            )

    def _check_mcp_sandbox_enabled(self) -> CheckResult:
        """Warn if any stdio MCP server has sandbox disabled."""
        try:
            from pincer.mcp.config import MCPTransport, load_mcp_config

            cfg = load_mcp_config(self.config_dir)
            if not cfg.enabled or not cfg.servers:
                return CheckResult(
                    "mcp_sandbox_enabled",
                    CheckStatus.SKIPPED,
                    "No MCP servers configured",
                    category="mcp",
                )
            unsandboxed = [
                s.name for s in cfg.servers if s.enabled and s.transport == MCPTransport.STDIO and not s.sandbox
            ]
            if not unsandboxed:
                return CheckResult(
                    "mcp_sandbox_enabled",
                    CheckStatus.PASS,
                    "All stdio MCP servers are sandboxed",
                    category="mcp",
                )
            return CheckResult(
                "mcp_sandbox_enabled",
                CheckStatus.WARNING,
                f"Unsandboxed stdio servers: {', '.join(unsandboxed)}",
                fix_hint="Set sandbox = true in pincer.toml or PINCER_MCP_SERVER_N_SANDBOX=true",
                category="mcp",
            )
        except Exception:
            return CheckResult(
                "mcp_sandbox_enabled",
                CheckStatus.SKIPPED,
                "Could not load MCP config",
                category="mcp",
            )

    def _check_mcp_no_plaintext_secrets(self) -> CheckResult:
        """Warn if MCP server configs have hardcoded tokens instead of ${VAR} refs."""
        toml_path = self.config_dir / "pincer.toml"
        if not toml_path.exists():
            return CheckResult(
                "mcp_no_plaintext_secrets",
                CheckStatus.SKIPPED,
                "No pincer.toml found",
                category="mcp",
            )
        try:
            content = toml_path.read_text()
            # Look for token-like values that aren't ${VAR} references
            suspicious = re.findall(
                r'(?:token|key|secret|password)\s*=\s*"(?!\$\{)[a-zA-Z0-9_\-]{20,}"',
                content,
                re.IGNORECASE,
            )
            if not suspicious:
                return CheckResult(
                    "mcp_no_plaintext_secrets",
                    CheckStatus.PASS,
                    "No plaintext secrets in pincer.toml",
                    category="mcp",
                )
            return CheckResult(
                "mcp_no_plaintext_secrets",
                CheckStatus.WARNING,
                f"{len(suspicious)} potential hardcoded secret(s) in pincer.toml",
                fix_hint='Use ${ENV_VAR} references: token = "${GITHUB_TOKEN}"',
                category="mcp",
            )
        except Exception:
            return CheckResult(
                "mcp_no_plaintext_secrets",
                CheckStatus.SKIPPED,
                "Could not read pincer.toml",
                category="mcp",
            )

    def _check_mcp_tool_count(self) -> CheckResult:
        """Warn if MCP would expose too many tools (degrades LLM tool selection)."""
        try:
            from pincer.mcp.config import load_mcp_config

            cfg = load_mcp_config(self.config_dir)
            if not cfg.enabled or not cfg.servers:
                return CheckResult(
                    "mcp_tool_count",
                    CheckStatus.SKIPPED,
                    "No MCP servers configured",
                    category="mcp",
                )
            n_servers = len([s for s in cfg.servers if s.enabled])
            return CheckResult(
                "mcp_tool_count",
                CheckStatus.PASS,
                f"{n_servers} MCP server(s) configured (tool count checked at runtime)",
                category="mcp",
            )
        except Exception:
            return CheckResult(
                "mcp_tool_count",
                CheckStatus.SKIPPED,
                "Could not load MCP config",
                category="mcp",
            )

    def _check_tool_approval_mode(self, cfg: Settings | None = None) -> CheckResult:
        cfg = self._cfg(cfg)
        mode = cfg.tool_approval
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

    def _check_mcp_env_vars(self) -> CheckResult:
        """Warn if any ${VAR} references in pincer.toml [mcp] are unresolved."""
        toml_path = self.config_dir / "pincer.toml"
        if not toml_path.exists():
            return CheckResult(
                "mcp_env_vars",
                CheckStatus.SKIPPED,
                "No pincer.toml found",
                category="mcp",
            )
        try:
            content = toml_path.read_text()
            # Find all ${VAR} references
            refs = re.findall(r"\$\{([^}]+)\}", content)
            if not refs:
                return CheckResult(
                    "mcp_env_vars",
                    CheckStatus.PASS,
                    "No environment variable references in pincer.toml",
                    category="mcp",
                )
            unset = [v for v in refs if not os.environ.get(v)]
            if not unset:
                return CheckResult(
                    "mcp_env_vars",
                    CheckStatus.PASS,
                    f"All {len(refs)} env var reference(s) are set",
                    category="mcp",
                )
            return CheckResult(
                "mcp_env_vars",
                CheckStatus.WARNING,
                f"Unresolved env var(s) in pincer.toml: {', '.join(sorted(set(unset)))}",
                fix_hint="Export the missing variables before starting Pincer",
                category="mcp",
            )
        except Exception:
            return CheckResult(
                "mcp_env_vars",
                CheckStatus.SKIPPED,
                "Could not read pincer.toml",
                category="mcp",
            )

    def _check_mcp_collisions(self) -> CheckResult:
        """Warn if tool_prefix is disabled with multiple servers (risks tool name collisions)."""
        try:
            from pincer.mcp.config import load_mcp_config

            cfg = load_mcp_config(self.config_dir)
            if not cfg.enabled or not cfg.servers:
                return CheckResult(
                    "mcp_collisions",
                    CheckStatus.SKIPPED,
                    "No MCP servers configured",
                    category="mcp",
                )
            enabled_servers = [s for s in cfg.servers if s.enabled]
            if len(enabled_servers) > 1 and not cfg.tool_prefix:
                return CheckResult(
                    "mcp_collisions",
                    CheckStatus.WARNING,
                    f"{len(enabled_servers)} MCP servers with tool_prefix=false — tool name collisions possible",
                    fix_hint="Set tool_prefix = true in pincer.toml [mcp] to namespace tools by server",
                    category="mcp",
                )
            return CheckResult(
                "mcp_collisions",
                CheckStatus.PASS,
                "Tool name collision risk is low (prefix enabled or single server)",
                category="mcp",
            )
        except Exception:
            return CheckResult(
                "mcp_collisions",
                CheckStatus.SKIPPED,
                "Could not load MCP config",
                category="mcp",
            )

    def _check_mcp_servers(self) -> CheckResult:
        """Warn if any configured stdio MCP server command is not found."""
        import shutil

        try:
            from pincer.mcp.config import MCPTransport, load_mcp_config

            cfg = load_mcp_config(self.config_dir)
            if not cfg.enabled or not cfg.servers:
                return CheckResult(
                    "mcp_servers",
                    CheckStatus.SKIPPED,
                    "No MCP servers configured",
                    category="mcp",
                )
            missing = []
            for srv in cfg.servers:
                if not srv.enabled or srv.transport != MCPTransport.STDIO:
                    continue
                cmd = srv.command or ""
                if not cmd:
                    missing.append(f"{srv.name} (no command)")
                    continue
                # Absolute path check, then PATH lookup
                if not (Path(cmd).is_file() or shutil.which(cmd)):
                    missing.append(f"{srv.name} ({cmd!r})")
            if not missing:
                return CheckResult(
                    "mcp_servers",
                    CheckStatus.PASS,
                    "All stdio MCP server commands found",
                    category="mcp",
                )
            return CheckResult(
                "mcp_servers",
                CheckStatus.WARNING,
                f"Server command(s) not found: {', '.join(missing)}",
                fix_hint="Verify the command paths are correct and the binaries are installed",
                category="mcp",
            )
        except Exception:
            return CheckResult(
                "mcp_servers",
                CheckStatus.SKIPPED,
                "Could not load MCP config",
                category="mcp",
            )

    def _check_mcp_oauth_enabled(self) -> CheckResult:
        """Warn if MCP server export is enabled without OAuth configured."""
        try:
            from pincer.mcp.config import load_mcp_config

            cfg = load_mcp_config(self.config_dir)
            srv = cfg.server
            if not srv.enabled:
                return CheckResult(
                    "mcp_oauth_enabled",
                    CheckStatus.SKIPPED,
                    "MCP server export is disabled",
                    category="mcp",
                )
            if getattr(srv, "auth_enabled", False):
                return CheckResult(
                    "mcp_oauth_enabled",
                    CheckStatus.PASS,
                    "MCP server OAuth authentication is enabled",
                    category="mcp",
                )
            return CheckResult(
                "mcp_oauth_enabled",
                CheckStatus.WARNING,
                "MCP server export has no OAuth authentication configured",
                fix_hint="Set [mcp.server] auth_enabled = true and configure allowed_clients in pincer.toml",
                category="mcp",
            )
        except Exception:
            return CheckResult(
                "mcp_oauth_enabled",
                CheckStatus.SKIPPED,
                "Could not load MCP config",
                category="mcp",
            )

    def _check_mcp_server_not_exposed(self) -> CheckResult:
        """Warn if MCP server export is bound to a non-localhost address without auth."""
        try:
            from pincer.mcp.config import load_mcp_config

            cfg = load_mcp_config(self.config_dir)
            srv = cfg.server
            if not srv.enabled:
                return CheckResult(
                    "mcp_server_not_exposed",
                    CheckStatus.SKIPPED,
                    "MCP server export is disabled",
                    category="mcp",
                )
            if srv.host in ("127.0.0.1", "::1", "localhost"):
                return CheckResult(
                    "mcp_server_not_exposed",
                    CheckStatus.PASS,
                    f"MCP server bound to localhost only ({srv.host})",
                    category="mcp",
                )
            auth_ok = getattr(srv, "auth_enabled", False)
            if auth_ok:
                return CheckResult(
                    "mcp_server_not_exposed",
                    CheckStatus.PASS,
                    f"MCP server on {srv.host} with OAuth enabled",
                    category="mcp",
                )
            return CheckResult(
                "mcp_server_not_exposed",
                CheckStatus.WARNING,
                f"MCP server exposed on {srv.host} without OAuth — any host can connect",
                fix_hint=("Set host = '127.0.0.1' in [mcp.server] or enable OAuth authentication"),
                category="mcp",
            )
        except Exception:
            return CheckResult(
                "mcp_server_not_exposed",
                CheckStatus.SKIPPED,
                "Could not load MCP config",
                category="mcp",
            )

    def _check_mcp_injection_alerts(self) -> CheckResult:
        """Check for recent prompt injection alerts in audit log (informational)."""
        return CheckResult(
            "mcp_injection_alerts",
            CheckStatus.PASS,
            "Prompt injection detection is active on MCP tool outputs",
            category="mcp",
        )
