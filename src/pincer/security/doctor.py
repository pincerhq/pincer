"""
Pincer Security Doctor — Runs 25+ security checks.

`pincer doctor` outputs a traffic-light report with pass/warning/critical checks
across secrets, access control, budget, filesystem, network, and runtime categories.
"""

from __future__ import annotations

import contextlib
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

from pincer.utils.csv_list import split_csv

if TYPE_CHECKING:
    from pincer.config import Settings
    from pincer.config.llm import ReasoningEffort, ThinkingMode
    from pincer.llm.model_params import ReasoningWire


#: The shared API tokens removed in favour of per-identity auth (issue #228).
#: Named here only so `pincer doctor` can point out a leftover one.
_REMOVED_TOKEN_VARS = ("PINCER_DASHBOARD_TOKEN", "PINCER_WEB_CHAT_TOKEN")
# Replaced by PINCER_VOICE_GUESTS_TOOLS_ALLOWED and the standard approval policy (#299).
_REMOVED_VOICE_TOOL_VARS = (
    "PINCER_VOICE_TOOLS_EXTRA",
    "PINCER_VOICE_TOOL_APPROVAL",
    "PINCER_VOICE_TOOL_APPROVAL_OVERRIDES",
)
# Below this voice token limit, a model that thinks may spend it all before speaking (issue #345)
_VOICE_THINKING_MIN_TOKENS = 512


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
        skills_dir: str | Path | None = None,
        production: bool = False,
    ) -> None:
        self.data_dir = data_dir or Path("data")
        self.config_dir = config_dir or Path(".")
        self.skills_dir = skills_dir or Path.home() / ".pincer" / "skills"
        # `pincer doctor --production` (Sprint 7, T7.3): extra deploy-gate
        # checks; the deploy script refuses to start on any CRITICAL.
        self.production = production
        # The identity roster, read once per `run_all` instead of once per
        # access check (each read opens its own loop and engine).
        self._roster_cache: dict[Path, tuple[bool, list[str]]] | None = None

    def _cfg(self, cfg: Settings | None) -> Settings:
        if cfg is None:
            from pincer.config import get_settings_relaxed

            return get_settings_relaxed()
        return cfg

    def run_all(self) -> DoctorReport:
        from pincer.config import get_settings_relaxed

        self._roster_cache = {}
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
        report.checks.append(self._check_identity_management(settings))
        report.checks.append(self._check_telegram_access_control(settings))
        report.checks.append(self._check_whatsapp_access_control(settings))
        report.checks.append(self._check_whatsapp_neonize_version())
        report.checks.append(self._check_discord_allowlist(settings))
        report.checks.append(self._check_dashboard_auth_token(settings))
        report.checks.append(self._check_stale_shared_tokens())
        # Budget (3 checks)
        report.checks.append(self._check_budget_limits(settings))
        report.checks.append(self._check_rate_limits(settings))
        report.checks.append(self._check_tool_call_limits(settings))
        # Filesystem (5 checks)
        report.checks.append(self._check_data_dir_permissions())
        report.checks.append(self._check_skills_dir_permissions(settings))
        report.checks.append(self._check_storage_root(settings))
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
        # Voice DACH compliance (3 checks, Sprint 0)
        report.checks.append(self._check_voice_dach_consent(settings))
        report.checks.append(self._check_voice_retention(settings))
        report.checks.append(self._check_voice_provider_regions(settings))
        report.checks.append(self._check_voice_twilio_edge(settings))
        # Voice ElevenLabs (1 check, Sprint 4)
        report.checks.append(self._check_voice_elevenlabs_voices(settings))
        report.checks.append(self._check_voice_language_pairing(settings))
        # Observability (2 checks, Sprint 9)
        report.checks.append(self._check_ops_alert_routing(settings))
        report.checks.append(self._check_voice_canary(settings))
        # Voice security hardening (4 checks, Sprint 8)
        report.checks.append(self._check_voice_webhook_signature(settings))
        report.checks.append(self._check_voice_ws_auth(settings))
        report.checks.append(self._check_voice_abuse_limits(settings))
        report.checks.append(self._check_voice_do_not_call_enforced())
        # Voice in-call tool execution (3 checks, Sprint 11 / #299)
        report.checks.append(self._check_voice_legacy_tool_vars())
        report.checks.append(self._check_voice_registered_phones(settings))
        report.checks.append(self._check_voice_tier_x_reachable(settings))
        # Inbound receptionist (2 checks, Sprint 12)
        report.checks.append(self._check_receptionist_profile(settings))
        report.checks.append(self._check_inbound_recording_consent(settings))
        # Live listen-in (1 check, Sprint 15)
        report.checks.append(self._check_listen_in_announce(settings))
        # Signal (3 checks, Sprint 7.5)
        report.checks.append(self._check_signal_phone_set(settings))
        report.checks.append(self._check_signal_api_local(settings))
        report.checks.append(self._check_signal_access_control(settings))
        # Runtime (5 checks)
        report.checks.append(self._check_not_running_as_root())
        report.checks.append(self._check_audit_logging_enabled(settings))
        report.checks.append(self._check_skill_sandbox_enabled(settings))
        report.checks.append(self._check_tool_approval_mode(settings))
        report.checks.append(self._check_schedule_action_types(settings))
        # Google Workspace (1 check, issue #297)
        report.checks.append(self._check_google_workspace(settings))
        # LLM effort and thinking (4 checks, issue #345)
        report.checks.append(self._check_llm_reasoning_default(settings))
        report.checks.append(self._check_llm_reasoning_voice(settings))
        report.checks.append(self._check_llm_reasoning_summary(settings))
        report.checks.append(self._check_voice_thinking_token_limit(settings))
        report.checks.append(self._check_llm_pricing(settings))
        report.checks.append(self._check_llm_compaction(settings))
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
        # Production deploy gate (7 checks, Sprint 7 T7.3 + Sprint 8) — only with --production
        if self.production:
            report.checks.append(self._check_prod_webhook_url(settings))
            report.checks.append(self._check_prod_no_tunnel(settings))
            report.checks.append(self._check_prod_auth_tokens(settings))
            report.checks.append(self._check_prod_dach_compliance(settings))
            report.checks.append(self._check_prod_environment_flag(settings))
            report.checks.append(self._check_prod_cors_origins(settings))
            report.checks.append(self._check_prod_voice_signatures(settings))
        self._roster_cache = None
        return report

    # ── Production deploy gate (Sprint 7, T7.3) ───────────

    _TUNNEL_MARKERS = ("ngrok", "trycloudflare", "loca.lt", "localtunnel", "serveo", "localhost.run")

    def _check_prod_webhook_url(self, cfg: Settings | None = None) -> CheckResult:
        """Production voice webhooks must be a real HTTPS domain — no tunnel,
        no localhost, no plain HTTP (Twilio signature + WSS relay depend on it)."""
        settings = self._cfg(cfg)
        url = str(getattr(settings, "voice_webhook_base_url", "") or "").strip().lower()
        problems: list[str] = []
        if not url.startswith("https://"):
            problems.append("not HTTPS")
        if any(marker in url for marker in self._TUNNEL_MARKERS):
            problems.append("tunnel domain")
        if "localhost" in url or "127.0.0.1" in url:
            problems.append("localhost")
        if problems:
            return CheckResult(
                name="prod_webhook_url",
                status=CheckStatus.CRITICAL,
                message=f"Voice webhook base URL unfit for production ({', '.join(problems)}): {url or '(empty)'}",
                fix_hint="Set PINCER_VOICE_WEBHOOK_BASE_URL=https://voice.<your-domain> behind the TLS proxy",
                category="production",
            )
        return CheckResult(
            name="prod_webhook_url",
            status=CheckStatus.PASS,
            message=f"Voice webhook base URL is production HTTPS: {url}",
            category="production",
        )

    def _check_prod_no_tunnel(self, cfg: Settings | None = None) -> CheckResult:
        """The built-in ngrok tunnel is a dev convenience — it must be off in
        production (latency, availability, and a moving public URL)."""
        settings = self._cfg(cfg)
        token = ""
        with contextlib.suppress(AttributeError):
            token = settings.ngrok_authtoken.get_secret_value()
        if token:
            return CheckResult(
                name="prod_no_tunnel",
                status=CheckStatus.CRITICAL,
                message="ngrok tunnel is configured (PINCER_NGROK_AUTHTOKEN set)",
                fix_hint="Remove PINCER_NGROK_AUTHTOKEN from the production environment",
                category="production",
            )
        return CheckResult(
            name="prod_no_tunnel",
            status=CheckStatus.PASS,
            message="No dev tunnel configured",
            category="production",
        )

    def _check_prod_auth_tokens(self, cfg: Settings | None = None) -> CheckResult:
        """Every HTTP surface must be authenticated in production."""
        settings = self._cfg(cfg)
        # `is True`, not truthiness: the API is default-deny, and only the
        # explicit dev flag opens it.
        if getattr(settings, "auth_disabled", False) is True:
            return CheckResult(
                name="prod_auth_tokens",
                status=CheckStatus.CRITICAL,
                message="API authentication is disabled (PINCER_AUTH_DISABLED=true): every /api route is open",
                fix_hint="Remove PINCER_AUTH_DISABLED from the production environment",
                category="production",
            )
        try:
            jwt_secret = str(settings.jwt_secret.get_secret_value() or "")
        except AttributeError:
            jwt_secret = ""
        if not jwt_secret:
            # Without it the secret is a file under data_dir: fine for one
            # container on a persistent volume, but a redeploy onto a fresh
            # disk signs everyone out and two replicas reject each other's
            # sessions.
            return CheckResult(
                name="prod_auth_tokens",
                status=CheckStatus.WARNING,
                message="PINCER_JWT_SECRET is not set; sessions are signed with a secret generated into the data dir",
                fix_hint="Generate one: python -c 'import secrets; print(secrets.token_urlsafe(48))'",
                category="production",
            )
        if len(jwt_secret) < 32:
            return CheckResult(
                name="prod_auth_tokens",
                status=CheckStatus.CRITICAL,
                message="PINCER_JWT_SECRET is too short (use 32+ chars)",
                fix_hint="Generate one: python -c 'import secrets; print(secrets.token_urlsafe(48))'",
                category="production",
            )
        return CheckResult(
            name="prod_auth_tokens",
            status=CheckStatus.PASS,
            message="API authentication enabled, JWT secret set",
            category="production",
        )

    def _check_prod_dach_compliance(self, cfg: Settings | None = None) -> CheckResult:
        """Sprint 0 compliance settings must be ACTIVE in production, not just
        available: two-party consent, retention purge, EU timezone."""
        settings = self._cfg(cfg)
        if not getattr(settings, "voice_enabled", False) and not getattr(settings, "voice_outbound_enabled", False):
            return CheckResult(
                name="prod_dach_compliance",
                status=CheckStatus.SKIPPED,
                message="Voice disabled — DACH compliance gate not applicable",
                category="production",
            )
        problems: list[str] = []
        if str(getattr(settings, "voice_consent_mode", "")) != "two_party":
            problems.append("voice_consent_mode != two_party")
        if int(getattr(settings, "voice_transcript_retention_days", 0) or 0) <= 0:
            problems.append("transcript retention disabled (keep-forever)")
        tz = str(getattr(settings, "voice_timezone", "") or getattr(settings, "timezone", "") or "")
        if not tz.startswith("Europe/"):
            problems.append(f"timezone not Europe/* ({tz or 'unset'})")
        if problems:
            return CheckResult(
                name="prod_dach_compliance",
                status=CheckStatus.CRITICAL,
                message="DACH compliance settings inactive: " + "; ".join(problems),
                fix_hint="PINCER_VOICE_CONSENT_MODE=two_party, PINCER_VOICE_TRANSCRIPT_RETENTION_DAYS=90, "
                "PINCER_VOICE_TIMEZONE=Europe/Berlin (see .env.production.example)",
                category="production",
            )
        return CheckResult(
            name="prod_dach_compliance",
            status=CheckStatus.PASS,
            message="Two-party consent, retention purge, and EU timezone active",
            category="production",
        )

    def _check_prod_environment_flag(self, cfg: Settings | None = None) -> CheckResult:
        """PINCER_ENVIRONMENT drives the CORS policy and the auth hardening —
        deploying with it unset silently keeps the developer defaults."""
        settings = self._cfg(cfg)
        env = str(getattr(settings, "environment", "") or "").strip().lower()
        if env != "production":
            return CheckResult(
                name="prod_environment_flag",
                status=CheckStatus.CRITICAL,
                message=f"PINCER_ENVIRONMENT is {env or '(unset)'}, not 'production' — "
                "localhost CORS origins are still allowed",
                fix_hint="Set PINCER_ENVIRONMENT=production in .env.production",
                category="production",
            )
        return CheckResult(
            name="prod_environment_flag",
            status=CheckStatus.PASS,
            message="Environment is production",
            category="production",
        )

    def _check_prod_cors_origins(self, cfg: Settings | None = None) -> CheckResult:
        """No localhost origin may be credential-allowed by a production API."""
        settings = self._cfg(cfg)
        from pincer.api.auth_guard import cors_origins

        origins = cors_origins(settings)
        local = [o for o in origins if "localhost" in o or "127.0.0.1" in o]
        if local:
            return CheckResult(
                name="prod_cors_origins",
                status=CheckStatus.CRITICAL,
                message=f"Localhost CORS origins allowed in production: {', '.join(local)}",
                fix_hint="Set PINCER_ENVIRONMENT=production and list real origins in "
                "PINCER_DASHBOARD_URL / PINCER_WEB_CHAT_URL / PINCER_CORS_EXTRA_ORIGINS",
                category="production",
            )
        if not origins:
            return CheckResult(
                name="prod_cors_origins",
                status=CheckStatus.WARNING,
                message="No CORS origins configured — a browser dashboard on another host cannot reach the API",
                fix_hint="Set PINCER_DASHBOARD_URL=https://<dashboard-host>",
                category="production",
            )
        return CheckResult(
            name="prod_cors_origins",
            status=CheckStatus.PASS,
            message=f"CORS restricted to production origins: {', '.join(origins)}",
            category="production",
        )

    def _check_prod_voice_signatures(self, cfg: Settings | None = None) -> CheckResult:
        """Webhook signature + WS token validation must be ON and usable."""
        settings = self._cfg(cfg)
        if not getattr(settings, "voice_enabled", False):
            return CheckResult(
                name="prod_voice_signatures",
                status=CheckStatus.SKIPPED,
                message="Voice disabled — webhook signature gate not applicable",
                category="production",
            )
        problems: list[str] = []
        if not getattr(settings, "voice_webhook_validate", True):
            problems.append("PINCER_VOICE_WEBHOOK_VALIDATE=false")
        if not getattr(settings, "voice_ws_auth_required", True):
            problems.append("PINCER_VOICE_WS_AUTH_REQUIRED=false")
        try:
            token = settings.twilio_auth_token.get_secret_value()
        except AttributeError:
            token = ""
        if not token:
            problems.append("PINCER_TWILIO_AUTH_TOKEN empty (validation silently no-ops)")
        if problems:
            return CheckResult(
                name="prod_voice_signatures",
                status=CheckStatus.CRITICAL,
                message="Voice webhooks are unauthenticated: " + "; ".join(problems),
                fix_hint="Leave PINCER_VOICE_WEBHOOK_VALIDATE / PINCER_VOICE_WS_AUTH_REQUIRED at their "
                "defaults (true) and set PINCER_TWILIO_AUTH_TOKEN",
                category="production",
            )
        return CheckResult(
            name="prod_voice_signatures",
            status=CheckStatus.PASS,
            message="Twilio signature + WebSocket token validation enforced",
            category="production",
        )

    # ── Observability (Sprint 9) ──────────────────────────

    def _check_ops_alert_routing(self, cfg: Settings | None = None) -> CheckResult:
        """T9.2: alerts must have somewhere to go.

        An alert system that fires into the void is worse than none — it looks
        healthy from every angle while nobody is being told anything.
        """
        settings = self._cfg(cfg)
        if not getattr(settings, "ops_alerts_enabled", True):
            return CheckResult(
                name="ops_alert_routing",
                status=CheckStatus.WARNING,
                message="Ops alerting is disabled (PINCER_OPS_ALERTS_ENABLED=false) — "
                "stuck calls and provider outages will not notify anyone",
                fix_hint="Remove PINCER_OPS_ALERTS_ENABLED=false",
                category="observability",
            )

        recipient = str(getattr(settings, "ops_user_id", "") or getattr(settings, "default_user_id", "") or "")
        email = str(getattr(settings, "ops_alert_email", "") or "")
        if not recipient and not email:
            return CheckResult(
                name="ops_alert_routing",
                status=CheckStatus.CRITICAL,
                message="Alerts are enabled but have no recipient — every alert will only reach the log",
                fix_hint="Set PINCER_OPS_USER_ID (and PINCER_OPS_CHANNEL), or PINCER_OPS_ALERT_EMAIL as a fallback",
                category="observability",
            )

        channel = str(getattr(settings, "ops_channel", "") or "")
        details = f"channel={channel or 'unset'}"
        if email:
            details += f", email fallback={email}"
        if not getattr(settings, "telemetry_dsn", None):
            return CheckResult(
                name="ops_alert_routing",
                status=CheckStatus.WARNING,
                message=f"Alerts route to {details}, but PINCER_TELEMETRY_DSN is unset — "
                "no metrics dashboard, only alerts and the CLI",
                fix_hint="Set PINCER_TELEMETRY_DSN to your OTLP collector for the Voice Ops dashboard",
                category="observability",
            )
        return CheckResult(
            name="ops_alert_routing",
            status=CheckStatus.PASS,
            message=f"Ops alerts route to {details}; metrics export configured",
            category="observability",
        )

    def _check_voice_canary(self, cfg: Settings | None = None) -> CheckResult:
        """T9.2: the canary is the probe that catches provider outages first."""
        settings = self._cfg(cfg)
        if not getattr(settings, "voice_enabled", False):
            return CheckResult(
                name="voice_canary",
                status=CheckStatus.SKIPPED,
                message="Voice disabled — canary not applicable",
                category="observability",
            )
        if not getattr(settings, "voice_canary_enabled", False):
            return CheckResult(
                name="voice_canary",
                status=CheckStatus.WARNING,
                message="Synthetic canary is off — a Twilio/STT/TTS outage will first be noticed "
                "by a customer rather than by us",
                fix_hint="Set PINCER_VOICE_CANARY_ENABLED=true and PINCER_VOICE_CANARY_NUMBER=<staging responder>",
                category="observability",
            )

        number = str(getattr(settings, "voice_canary_number", "") or "").strip()
        if not number:
            return CheckResult(
                name="voice_canary",
                status=CheckStatus.CRITICAL,
                message="Canary is enabled but PINCER_VOICE_CANARY_NUMBER is empty — it can never run",
                fix_hint="Set PINCER_VOICE_CANARY_NUMBER to a staging responder you control (never a customer)",
                category="observability",
            )

        # A canary that runs only inside quiet hours would be skipped forever.
        from pincer.voice.safety_gates import parse_quiet_hours

        quiet = parse_quiet_hours(str(getattr(settings, "voice_quiet_hours", "") or ""))
        overrides = str(getattr(settings, "voice_quiet_hours_override_users", "") or "")
        if quiet is not None and "pincer-canary" not in overrides:
            return CheckResult(
                name="voice_canary",
                status=CheckStatus.WARNING,
                message="Canary runs will be skipped during quiet hours — overnight coverage has a gap",
                fix_hint="Add pincer-canary to PINCER_VOICE_QUIET_HOURS_OVERRIDE_USERS "
                "(it dials a responder you own, not a customer)",
                category="observability",
            )
        return CheckResult(
            name="voice_canary",
            status=CheckStatus.PASS,
            message=f"Canary enabled ({getattr(settings, 'voice_canary_cron', '')}) against a configured target",
            category="observability",
        )

    # ── Voice security hardening (Sprint 8) ───────────────

    def _check_voice_webhook_signature(self, cfg: Settings | None = None) -> CheckResult:
        """T8.1: every /voice/* route validates X-Twilio-Signature."""
        settings = self._cfg(cfg)
        if not getattr(settings, "voice_enabled", False):
            return CheckResult(
                name="voice_webhook_signature",
                status=CheckStatus.SKIPPED,
                message="Voice disabled",
                category="voice",
            )
        if not getattr(settings, "voice_webhook_validate", True):
            return CheckResult(
                name="voice_webhook_signature",
                status=CheckStatus.CRITICAL,
                message="Twilio webhook signature validation is DISABLED — anyone can spoof calls, "
                "status callbacks, and TwiML requests",
                fix_hint="Remove PINCER_VOICE_WEBHOOK_VALIDATE=false",
                category="voice",
            )
        try:
            token = settings.twilio_auth_token.get_secret_value()
        except AttributeError:
            token = ""
        if not token:
            return CheckResult(
                name="voice_webhook_signature",
                status=CheckStatus.CRITICAL,
                message="Voice is enabled but PINCER_TWILIO_AUTH_TOKEN is empty — "
                "signature validation cannot run and every webhook is accepted",
                fix_hint="Set PINCER_TWILIO_AUTH_TOKEN from the Twilio console",
                category="voice",
            )
        max_age = int(getattr(settings, "voice_signature_max_age_s", 300) or 300)
        return CheckResult(
            name="voice_webhook_signature",
            status=CheckStatus.PASS,
            message=f"Twilio signature validation enforced (replay window {max_age}s)",
            category="voice",
        )

    def _check_voice_ws_auth(self, cfg: Settings | None = None) -> CheckResult:
        """T8.1: the relay/stream WebSocket upgrade is token-authenticated."""
        settings = self._cfg(cfg)
        if not getattr(settings, "voice_enabled", False):
            return CheckResult(
                name="voice_ws_auth",
                status=CheckStatus.SKIPPED,
                message="Voice disabled",
                category="voice",
            )
        if not getattr(settings, "voice_ws_auth_required", True):
            return CheckResult(
                name="voice_ws_auth",
                status=CheckStatus.CRITICAL,
                message="WebSocket auth is DISABLED — anyone can open /voice/relay and speak on a live call",
                fix_hint="Remove PINCER_VOICE_WS_AUTH_REQUIRED=false",
                category="voice",
            )
        return CheckResult(
            name="voice_ws_auth",
            status=CheckStatus.PASS,
            message="ConversationRelay / Media Streams upgrades require a signed token",
            category="voice",
        )

    def _check_voice_abuse_limits(self, cfg: Settings | None = None) -> CheckResult:
        """T8.3: the outbound abuse limits are actually set to something."""
        settings = self._cfg(cfg)
        if not getattr(settings, "voice_outbound_enabled", False):
            return CheckResult(
                name="voice_abuse_limits",
                status=CheckStatus.SKIPPED,
                message="Outbound calling disabled",
                category="voice",
            )
        from pincer.voice.safety_gates import parse_quiet_hours

        daily = int(getattr(settings, "voice_daily_call_limit", 0) or 0)
        cooldown = int(getattr(settings, "voice_target_cooldown_min", 0) or 0)
        quiet = parse_quiet_hours(str(getattr(settings, "voice_quiet_hours", "") or ""))

        gaps: list[str] = []
        if daily <= 0:
            gaps.append("no global daily cap (PINCER_VOICE_DAILY_CALL_LIMIT=0)")
        if cooldown <= 0:
            gaps.append("no per-target cooldown (PINCER_VOICE_TARGET_COOLDOWN_MIN=0)")
        if quiet is None:
            gaps.append("no quiet hours (PINCER_VOICE_QUIET_HOURS empty) — §7 UWG exposure")
        if gaps:
            return CheckResult(
                name="voice_abuse_limits",
                status=CheckStatus.WARNING if len(gaps) < 3 else CheckStatus.CRITICAL,
                message="Outbound abuse limits weakened: " + "; ".join(gaps),
                fix_hint="Restore the defaults: PINCER_VOICE_DAILY_CALL_LIMIT=20, "
                "PINCER_VOICE_TARGET_COOLDOWN_MIN=60, PINCER_VOICE_QUIET_HOURS=20:00-08:00",
                category="voice",
            )
        return CheckResult(
            name="voice_abuse_limits",
            status=CheckStatus.PASS,
            message=f"Outbound limits active: {daily}/day, {cooldown}min target cooldown, "
            f"quiet hours {getattr(settings, 'voice_quiet_hours', '')}",
            category="voice",
        )

    def _check_voice_do_not_call_enforced(self) -> CheckResult:
        """T8.3: prove the do-not-call list is consulted on the dial path.

        Source-level rather than config-level on purpose — the risk is a
        refactor that routes around `check_outbound_allowed`, which no amount
        of configuration would reveal.
        """
        import inspect

        try:
            from pincer.voice import outbound
            from pincer.voice.safety_gates import check_outbound_allowed

            dial_src = inspect.getsource(outbound.make_phone_call)
            gate_src = inspect.getsource(check_outbound_allowed)
        except Exception as e:  # pragma: no cover — source unavailable (frozen build)
            return CheckResult(
                name="voice_do_not_call",
                status=CheckStatus.WARNING,
                message=f"Could not verify the do-not-call enforcement path: {e}",
                category="voice",
            )

        problems: list[str] = []
        if "check_outbound_allowed" not in dial_src:
            problems.append("make_phone_call does not call check_outbound_allowed")
        if "is_do_not_call" not in gate_src:
            problems.append("check_outbound_allowed does not consult the do-not-call list")
        if "record_outbound_call" not in dial_src:
            problems.append("placed calls are not recorded (daily cap and cooldown would never trigger)")
        if problems:
            return CheckResult(
                name="voice_do_not_call",
                status=CheckStatus.CRITICAL,
                message="Outbound gate bypassed: " + "; ".join(problems),
                fix_hint="Every dial must go through voice.safety_gates.check_outbound_allowed",
                category="voice",
            )
        return CheckResult(
            name="voice_do_not_call",
            status=CheckStatus.PASS,
            message="Do-not-call list, daily cap, and target cooldown enforced on every outbound dial",
            category="voice",
        )

    # ── In-call tool execution (Sprint 11) ────────────────

    def _check_voice_legacy_tool_vars(self) -> CheckResult:
        """The voice approval variables were removed; a leftover one silently does nothing."""
        stale = self._removed_vars_set(_REMOVED_VOICE_TOOL_VARS)
        if stale:
            return CheckResult(
                name="voice_legacy_tool_vars",
                status=CheckStatus.WARNING,
                message=f"{', '.join(sorted(stale))} set but no longer used: guests confirm every write out loud, "
                "callers with a confirmed identity phone follow PINCER_APPROVAL_POLICY",
                fix_hint="Remove it. To change what guests may use on a call, set PINCER_VOICE_GUESTS_TOOLS_ALLOWED "
                "(tool names or patterns such as google__*) — it REPLACES the default guest set, so list the reads "
                "guests still need too",
                category="voice",
            )
        return CheckResult(
            name="voice_legacy_tool_vars",
            status=CheckStatus.PASS,
            message="No removed voice tool approval variables set",
            category="voice",
        )

    def _check_voice_registered_phones(self, cfg: Settings | None = None) -> CheckResult:
        """#299: with no confirmed phone configured, every call party is a guest,
        and a guest gets no calendar writes on a generic call by default."""
        settings = self._cfg(cfg)
        if not getattr(settings, "voice_enabled", False):
            return CheckResult(
                name="voice_registered_phones",
                status=CheckStatus.SKIPPED,
                message="Voice disabled",
                category="voice",
            )
        from pincer.config.identity import load_identity_map
        from pincer.services.identity import confirmed_phone_owners

        mode = str(getattr(settings, "identity_management", "update"))
        try:
            # Phones added with `pincer identity set --add-phone` count too.
            stored = confirmed_phone_owners(Path(settings.db_path))
        except Exception:
            stored = []  # no database or no identity tables yet
        try:
            entries = load_identity_map(self.config_dir, mode)
        except ValueError as e:
            return CheckResult(
                name="voice_registered_phones",
                status=CheckStatus.WARNING,
                message=f"Identity config is invalid, so no phone is confirmed: {e}",
                fix_hint="Fix the [identity] section in pincer.toml / pincer.local.toml",
                category="voice",
            )
        owners = sorted({uid for uid, entry in entries.items() if entry.phones} | set(stored))
        if not owners:
            return CheckResult(
                name="voice_registered_phones",
                status=CheckStatus.WARNING,
                message="No identity has a confirmed phone: everyone on a call is a guest, so the agent cannot "
                "create calendar events on a generic call",
                fix_hint='Add your number to your identity in pincer.toml: phones = ["+49…"], or run '
                "`pincer identity set <name> --add-phone +49…`; "
                "or allow guests, e.g. PINCER_VOICE_GUESTS_TOOLS_ALLOWED=google__*",
                category="voice",
            )
        return CheckResult(
            name="voice_registered_phones",
            status=CheckStatus.PASS,
            message="Confirmed phones configured for: " + ", ".join(owners),
            category="voice",
        )

    def _check_voice_tier_x_reachable(self, cfg: Settings | None = None) -> CheckResult:
        """§10.2: self-test — build the call-context schema set for every call
        kind and assert no Tier X tool is in it. FAIL (critical) if one is."""
        settings = self._cfg(cfg)
        from pincer.voice.tool_policy import (
            CALLER_GUEST,
            CALLER_USER,
            TIER_X,
            TIERS,
            allowed_tools_for_call,
            call_context_schemas,
            callable_tools,
            match_tool_patterns,
            unmatched_guest_tool_entries,
        )

        guest_list = str(getattr(settings, "voice_guests_tools_allowed", "") or "")
        # The probe registry: every known tool, what the guest list expands to,
        # its literal entries, and a few never-listed names (unknown must be
        # denied, never admitted).
        probe_names = set(TIERS) | set(callable_tools()) | {"shell_exec", "python_exec", "unknown__tool"}
        probe_names |= set(match_tool_patterns(guest_list)) | set(split_csv(guest_list))
        schemas = [{"name": name} for name in sorted(probe_names)]

        probes = [("receptionist", "inbound", CALLER_GUEST)]
        probes += [("appointment", direction, CALLER_GUEST) for direction in ("outbound", "inbound")]
        probes += [
            ("generic", direction, caller)
            for caller in (CALLER_USER, CALLER_GUEST)
            for direction in ("outbound", "inbound")
        ]
        leaks: list[str] = []
        for kind, direction, caller in probes:
            allowed = allowed_tools_for_call(settings, kind=kind, direction=direction, caller_class=caller)
            visible = {str(s["name"]) for s in call_context_schemas(schemas, allowed)}
            for name in sorted(visible):
                if TIERS.get(name, TIER_X) == TIER_X:
                    leaks.append(f"{name} ({kind}/{direction}/{caller})")
        if leaks:
            return CheckResult(
                name="voice_tier_x_reachable",
                status=CheckStatus.CRITICAL,
                message="Tier X tool(s) reachable from call context: " + ", ".join(leaks),
                fix_hint="pincer.voice.tool_policy must never admit a Tier X or unlisted tool — fix the code",
                category="voice",
            )
        unmatched = unmatched_guest_tool_entries(guest_list)
        if unmatched:
            return CheckResult(
                name="voice_tier_x_reachable",
                status=CheckStatus.WARNING,
                message="PINCER_VOICE_GUESTS_TOOLS_ALLOWED entries match no call tool and are ignored: "
                + ", ".join(unmatched),
                fix_hint="Only Tier R/W tools can be given to guests (see pincer.voice.tool_policy.TIERS); "
                "Tier X and unlisted tools never are",
                category="voice",
            )
        return CheckResult(
            name="voice_tier_x_reachable",
            status=CheckStatus.PASS,
            message="Call-context tool schemas contain no Tier X tool (all call kinds)",
            category="voice",
        )

    # ── Inbound receptionist (Sprint 12) ──────────────────

    def _check_receptionist_profile(self, cfg: Settings | None = None) -> CheckResult:
        """§4: with the receptionist enabled the business profile must load and validate."""
        settings = self._cfg(cfg)
        if not getattr(settings, "receptionist_enabled", False):
            return CheckResult(
                name="receptionist_profile",
                status=CheckStatus.SKIPPED,
                message="Receptionist disabled",
                category="voice",
            )
        from pincer.voice.receptionist.profile import ProfileError, load_business_profile

        path = str(getattr(settings, "business_profile", "") or "./business_profile.yaml")
        try:
            profile = load_business_profile(path)
        except ProfileError as e:
            return CheckResult(
                name="receptionist_profile",
                status=CheckStatus.CRITICAL,
                message=str(e),
                fix_hint="Fix the named field in the business profile (see docs/guides/receptionist-setup.md)",
                category="voice",
            )
        except Exception as e:  # pragma: no cover — unexpected loader failure
            return CheckResult(
                name="receptionist_profile",
                status=CheckStatus.CRITICAL,
                message=f"business profile could not be loaded: {e}",
                category="voice",
            )
        return CheckResult(
            name="receptionist_profile",
            status=CheckStatus.PASS,
            message=f"Business profile valid: {profile.business.name} ({path}), booking="
            f"{'on' if profile.booking.enabled else 'off'}, transfer={'on' if profile.transfer.enabled else 'off'}",
            category="voice",
        )

    def _check_inbound_recording_consent(self, cfg: Settings | None = None) -> CheckResult:
        """§3: PINCER_INBOUND_RECORDING=true requires the two-party announcement (FAIL otherwise)."""
        settings = self._cfg(cfg)
        if not getattr(settings, "receptionist_enabled", False):
            return CheckResult(
                name="inbound_recording_consent",
                status=CheckStatus.SKIPPED,
                message="Receptionist disabled",
                category="voice",
            )
        if not getattr(settings, "inbound_recording", False):
            return CheckResult(
                name="inbound_recording_consent",
                status=CheckStatus.PASS,
                message="Inbound receptionist calls are not recorded",
                category="voice",
            )
        mode = str(getattr(settings, "voice_consent_mode", "") or "")
        if mode != "two_party" or not getattr(settings, "voice_recording_enabled", False):
            return CheckResult(
                name="inbound_recording_consent",
                status=CheckStatus.CRITICAL,
                message="PINCER_INBOUND_RECORDING=true but the mandatory two-party recording announcement is not "
                f"configured (consent mode={mode or 'unset'}, recording_enabled="
                f"{getattr(settings, 'voice_recording_enabled', False)})",
                fix_hint="Set PINCER_VOICE_CONSENT_MODE=two_party and PINCER_VOICE_RECORDING_ENABLED=true, "
                "or disable PINCER_INBOUND_RECORDING",
                category="voice",
            )
        return CheckResult(
            name="inbound_recording_consent",
            status=CheckStatus.PASS,
            message="Inbound recording announces two-party consent before the greeting",
            category="voice",
        )

    def _check_listen_in_announce(self, cfg: Settings | None = None) -> CheckResult:
        """Sprint 15 §2.1: live monitoring must be announced. ANNOUNCE=false with
        ENABLED=true is a FAIL unless two-party recording is already announced
        (that announcement covers monitoring)."""
        settings = self._cfg(cfg)
        if getattr(settings, "listen_in_enabled", False) is not True:
            return CheckResult(
                name="listen_in_announce",
                status=CheckStatus.SKIPPED,
                message="Live listen-in disabled (no media fork)",
                category="voice",
            )
        if getattr(settings, "listen_in_announce", True):
            return CheckResult(
                name="listen_in_announce",
                status=CheckStatus.PASS,
                message="Live listen-in is announced in the call opening",
                category="voice",
            )
        mode = str(getattr(settings, "voice_consent_mode", "") or "")
        recording = bool(getattr(settings, "voice_recording_enabled", False))
        if mode == "two_party" and recording:
            return CheckResult(
                name="listen_in_announce",
                status=CheckStatus.PASS,
                message="Live listen-in not separately announced — covered by the active two-party "
                "recording announcement",
                category="voice",
            )
        return CheckResult(
            name="listen_in_announce",
            status=CheckStatus.CRITICAL,
            message="PINCER_LISTEN_IN_ENABLED=true with PINCER_LISTEN_IN_ANNOUNCE=false and no active "
            f"two-party recording announcement (consent mode={mode or 'unset'}, recording={recording}) — "
            "callers are monitored without being told",
            fix_hint="Remove PINCER_LISTEN_IN_ANNOUNCE=false, or set PINCER_VOICE_CONSENT_MODE=two_party "
            "and PINCER_VOICE_RECORDING_ENABLED=true, or disable PINCER_LISTEN_IN_ENABLED",
            category="voice",
        )

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

    def _identity_gate(self, cfg: Settings, name: str, category: str) -> str | CheckResult:
        """What closes the guest gate, or "" when it is open; the finding to
        report when the [identity] TOML does not load — `pincer run` refuses
        to start on the same error.

        The gate is on in `clionly`, when the TOML lists anyone, or when the
        database has a configured or hand-made identity with a channel (active or not).
        """
        from pincer.config.identity import load_identity_map

        mode = cfg.identity_management
        if mode == "clionly":
            return "PINCER_IDENTITY_MANAGEMENT=clionly"
        try:
            entries = load_identity_map(self.config_dir, mode)
        except ValueError as e:
            return CheckResult(
                name,
                CheckStatus.CRITICAL,
                f"Identity config is invalid: {e}",
                fix_hint="Fix the [identity] section in pincer.toml / pincer.local.toml; Pincer will not start with it",
                category=category,
            )
        if entries:
            return "the [identity] TOML"
        if self._db_roster(cfg)[0]:
            return "the identities in the database"
        return ""

    def _db_roster(self, cfg: Settings) -> tuple[bool, list[str]]:
        """(the database holds a gating roster, CLI-made identity ids); (False, []) when unreadable."""
        from pincer.services.identity import roster_summary

        db_path = Path(cfg.db_path)
        if self._roster_cache is not None and db_path in self._roster_cache:
            return self._roster_cache[db_path]
        try:
            summary = roster_summary(db_path) or (False, [])
        except Exception:
            # No database or no identity tables yet: no roster.
            summary = (False, [])
        if self._roster_cache is not None:
            self._roster_cache[db_path] = summary
        return summary

    def _check_identity_management(self, cfg: Settings | None = None) -> CheckResult:
        """#348: the removed PINCER_IDENTITY_MAP, and a TOML the mode ignores."""
        from pincer.config.identity import (
            LEGACY_IDENTITY_MAP_ENV,
            legacy_identity_map,
            legacy_identity_map_toml,
            toml_declares_identities,
        )

        cfg = self._cfg(cfg)
        legacy = legacy_identity_map(self.config_dir)
        if legacy is not None:
            return CheckResult(
                "identity_management",
                CheckStatus.CRITICAL,
                f"{LEGACY_IDENTITY_MAP_ENV} is set but no longer read — `pincer run` refuses to start",
                fix_hint="Move it into pincer.toml and remove it from the environment / .env:\n"
                + legacy_identity_map_toml(legacy),
                category="access",
            )
        mode = cfg.identity_management
        if mode == "clionly" and toml_declares_identities(self.config_dir):
            return CheckResult(
                "identity_management",
                CheckStatus.WARNING,
                "PINCER_IDENTITY_MANAGEMENT=clionly, so the [identity] section of pincer.toml is ignored",
                fix_hint="Manage identities with `pincer identity …` and remove the [identity] section, "
                "or set PINCER_IDENTITY_MANAGEMENT=update to apply it",
                category="access",
            )
        if mode == "sync":
            cli_ids = self._db_roster(cfg)[1]
            if cli_ids:
                return CheckResult(
                    "identity_management",
                    CheckStatus.PASS,
                    "PINCER_IDENTITY_MANAGEMENT=sync; identities made with the CLI are kept: " + ", ".join(cli_ids),
                    category="access",
                )
        return CheckResult(
            "identity_management",
            CheckStatus.PASS,
            f"PINCER_IDENTITY_MANAGEMENT={mode}",
            category="access",
        )

    def _check_telegram_access_control(self, cfg: Settings | None = None) -> CheckResult:
        cfg = self._cfg(cfg)
        if not cfg.telegram_bot_token.get_secret_value():
            return CheckResult(
                "telegram_access_control",
                CheckStatus.SKIPPED,
                "Telegram not configured",
                category="access",
            )
        gate = self._identity_gate(cfg, "telegram_access_control", "access")
        if isinstance(gate, CheckResult):
            return gate
        if gate:
            return CheckResult(
                "telegram_access_control",
                CheckStatus.PASS,
                f"Telegram access controlled by {gate}",
                category="access",
            )
        return CheckResult(
            "telegram_access_control",
            CheckStatus.CRITICAL,
            "Telegram bot has no identities configured — any Telegram user can message it",
            fix_hint=(
                "Add yourself to the [identity] section of pincer.toml (or `pincer identity create … --channel`), "
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
        gate = self._identity_gate(cfg, "whatsapp_access_control", "access")
        if isinstance(gate, CheckResult):
            return gate
        if gate:
            return CheckResult(
                "whatsapp_access_control",
                CheckStatus.PASS,
                f"WhatsApp access controlled by {gate} (covers DMs and group mentions)",
                category="access",
            )
        if cfg.whatsapp_self_chat_only:
            return CheckResult(
                "whatsapp_access_control",
                CheckStatus.WARNING,
                "WhatsApp is in self-chat-only mode, but group mentions/trigger-word messages "
                "are answered for any group member regardless of this setting, and no "
                "identities are configured to gate them",
                fix_hint=(
                    "Add identities to the [identity] section of pincer.toml (or with `pincer identity create`) "
                    "and PINCER_WHATSAPP_GUESTS_ALLOWED=false to restrict who can trigger Pincer "
                    "from a group; self-chat-only alone does not protect group chats."
                ),
                category="access",
            )
        if cfg.whatsapp_guests_allowed:
            return CheckResult(
                "whatsapp_access_control",
                CheckStatus.CRITICAL,
                "WhatsApp accepts DMs from anyone — PINCER_WHATSAPP_GUESTS_ALLOWED=true "
                "overrides the fail-closed default that applies with no identities configured",
                fix_hint=(
                    "Set PINCER_WHATSAPP_SELF_CHAT_ONLY=true, or add identities to the [identity] section of "
                    "pincer.toml (or with `pincer identity create`) and set PINCER_WHATSAPP_GUESTS_ALLOWED=false"
                ),
                category="access",
            )
        return CheckResult(
            "whatsapp_access_control",
            CheckStatus.WARNING,
            "WhatsApp DMs are blocked by default with no identities configured "
            "(fail-closed), but group mentions/trigger-word messages are still answered "
            "for any group member",
            fix_hint=(
                "Add identities to the [identity] section of pincer.toml (or with `pincer identity create`) and "
                "PINCER_WHATSAPP_GUESTS_ALLOWED=false to restrict who can trigger Pincer "
                "from a group"
            ),
            category="access",
        )

    # Minimum neonize release carrying a whatsmeow build WhatsApp still accepts.
    # Bump when upstream ships a fix for a fresh `err-client-outdated` wave.
    # 0.5.0+ also guards empty event payloads; 0.4.x panics in Go
    # (`index out of range [0] with length 0` in getBytesAndSize).
    _WA_NEONIZE_MIN = (0, 5, 2)

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
        """Somebody must be able to sign in to the API and the dashboard.

        There is no shared token any more: access is an identity with a
        password or an API key (`pincer identity set-password` / `api-key`).
        """
        from pincer.services.auth import count_identities_with_credentials

        cfg = self._cfg(cfg)
        if getattr(cfg, "auth_disabled", False) is True:
            return CheckResult(
                "dashboard_auth_token",
                CheckStatus.WARNING,
                "API authentication is disabled (PINCER_AUTH_DISABLED=true)",
                fix_hint="Local development only. Unset PINCER_AUTH_DISABLED anywhere reachable.",
                category="access",
            )
        try:
            count = count_identities_with_credentials(Path(cfg.db_path))
        except Exception as e:
            # SQLAlchemy appends the full statement after the first line.
            reason = (str(e).splitlines() or [type(e).__name__])[0]
            return CheckResult(
                "dashboard_auth_token",
                CheckStatus.SKIPPED,
                f"Could not read identity credentials: {reason}",
                category="access",
            )
        if not count:
            # A warning, not CRITICAL: with nobody able to sign in the API is
            # closed, which is safe. It is also what the deploy gate sees —
            # `scripts/deploy.sh` runs the doctor in a container without the
            # data volume, before the first identity can exist.
            return CheckResult(
                "dashboard_auth_token",
                CheckStatus.WARNING,
                "No identity can sign in: nobody has a password or an API key",
                fix_hint=(
                    "pincer identity create <name> "
                    "(or pincer identity set-password <name> for an identity that already exists)"
                ),
                category="access",
            )
        return CheckResult(
            "dashboard_auth_token",
            CheckStatus.PASS,
            f"{count} identit{'y' if count == 1 else 'ies'} can sign in to the API",
            category="access",
        )

    def _removed_vars_set(self, names: tuple[str, ...]) -> set[str]:
        """Which of `names` are set to a value, in the process environment or a project `.env`.

        `Settings` ignores unknown variables, so a removed one is only found here.
        """
        from dotenv import dotenv_values

        stale = {name for name in names if os.environ.get(name)}
        for env_file in (self.config_dir / ".env", self.config_dir.parent / ".env"):
            try:
                if env_file.is_file():
                    stale |= {name for name in names if dotenv_values(env_file).get(name)}
            except OSError:
                continue
        return stale

    def _check_stale_shared_tokens(self) -> CheckResult:
        """The shared API tokens were removed; a leftover one protects nothing.

        `Settings` ignores unknown variables, so these are looked for where
        they would be set: the process environment and the project `.env`.
        """
        stale = self._removed_vars_set(_REMOVED_TOKEN_VARS)
        if stale:
            return CheckResult(
                "stale_shared_tokens",
                CheckStatus.WARNING,
                f"{', '.join(sorted(stale))} set but no longer used: the API authenticates identities now",
                fix_hint="Remove it; give callers a password or key with `pincer identity set-password` / `api-key`",
                category="access",
            )
        return CheckResult(
            "stale_shared_tokens",
            CheckStatus.PASS,
            "No leftover shared API tokens",
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

    def _check_skills_dir_permissions(self, cfg: Settings | None = None) -> CheckResult:
        from pincer.storage.clients import is_remote_uri

        if isinstance(self.skills_dir, str) and is_remote_uri(self.skills_dir):
            return self._check_remote_skills_root(self.skills_dir, self._cfg(cfg))
        skills_dir = Path(self.skills_dir)
        if not skills_dir.exists():
            return CheckResult(
                "skills_dir_permissions",
                CheckStatus.SKIPPED,
                "Skills dir doesn't exist yet",
                category="filesystem",
            )
        mode = oct(skills_dir.stat().st_mode)[-3:]
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

    def _check_storage_root(self, cfg: Settings | None = None) -> CheckResult:
        """A local storage root must not contain the data dir: the storage
        browser would serve the JWT secret and the database to any identity."""
        from pincer.storage.clients import is_remote_uri
        from pincer.storage.root import data_dir_conflict

        settings = self._cfg(cfg)
        root = settings.storage_root
        if root is None or is_remote_uri(root):
            return CheckResult(
                "storage_root",
                CheckStatus.SKIPPED,
                "No local PINCER_STORAGE_ROOT set (the default, <data_dir>/storage, is safe)",
                category="filesystem",
            )
        resolved = Path(root).expanduser().resolve()
        problem = data_dir_conflict(resolved, Path(settings.data_dir).expanduser().resolve())
        if problem:
            return CheckResult(
                "storage_root",
                CheckStatus.CRITICAL,
                f"{problem}: the storage browser refuses to serve it",
                fix_hint="Point PINCER_STORAGE_ROOT at a folder outside PINCER_DATA_DIR, or unset it",
                category="filesystem",
            )
        return CheckResult(
            "storage_root",
            CheckStatus.PASS,
            f"Storage root {resolved} does not contain the data dir",
            category="filesystem",
        )

    def _check_remote_skills_root(self, uri: str, cfg: Settings) -> CheckResult:
        """Remote roots have no POSIX mode to check; verify the extra is installed
        and the root answers instead."""
        from pincer.exceptions import SkillLoadError
        from pincer.tools.skills.storage import open_skills_root

        try:
            root = open_skills_root(uri, cfg)
            reachable = root.exists()
        except SkillLoadError as e:
            return CheckResult(
                "skills_dir_permissions",
                CheckStatus.WARNING,
                str(e),
                fix_hint=(
                    "pip install 'pincer-agent[remote-skills]' and check PINCER_SKILLS_DIR "
                    "and the PINCER_STORAGE_* settings"
                ),
                category="filesystem",
            )
        except Exception as e:
            return CheckResult(
                "skills_dir_permissions",
                CheckStatus.WARNING,
                f"Remote skills root {uri} is unreachable: {type(e).__name__}",
                fix_hint="Check the PINCER_STORAGE_* credential settings and network access to the bucket",
                category="filesystem",
            )
        if not reachable:
            return CheckResult(
                "skills_dir_permissions",
                CheckStatus.WARNING,
                f"Remote skills root {uri} is reachable but empty or missing",
                fix_hint="Upload skill directories (each with a SKILL.md) under that prefix",
                category="filesystem",
            )
        return CheckResult(
            "skills_dir_permissions",
            CheckStatus.PASS,
            f"Remote skills root reachable: {uri}",
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

    # ── Voice DACH compliance (Sprint 0) ─────────────────

    def _check_voice_dach_consent(self, cfg: Settings | None = None) -> CheckResult:
        cfg = self._cfg(cfg)
        if not (cfg.voice_enabled or cfg.voice_outbound_enabled):
            return CheckResult(
                "voice_dach_consent",
                CheckStatus.SKIPPED,
                "Voice not enabled",
                category="voice",
            )
        number = (cfg.twilio_phone_number or "").strip()
        is_dach = number.startswith(("+49", "+43", "+41"))
        if cfg.voice_outbound_enabled and is_dach and cfg.voice_consent_mode == "one_party":
            return CheckResult(
                "voice_dach_consent",
                CheckStatus.WARNING,
                f"Outbound calling from DACH number {number[:4]}… with one_party consent "
                "(recording needs all-party consent, e.g. §201 StGB in Germany)",
                fix_hint="Set PINCER_VOICE_CONSENT_MODE=two_party for DACH deployments",
                category="voice",
            )
        if is_dach:
            return CheckResult(
                "voice_dach_consent",
                CheckStatus.PASS,
                f"DACH number with {cfg.voice_consent_mode} consent mode",
                category="voice",
            )
        return CheckResult(
            "voice_dach_consent",
            CheckStatus.PASS,
            "No DACH Twilio number configured",
            category="voice",
        )

    def _check_voice_retention(self, cfg: Settings | None = None) -> CheckResult:
        cfg = self._cfg(cfg)
        if not (cfg.voice_enabled or cfg.voice_outbound_enabled):
            return CheckResult(
                "voice_retention",
                CheckStatus.SKIPPED,
                "Voice not enabled",
                category="voice",
            )
        days = cfg.voice_transcript_retention_days
        if cfg.voice_recording_enabled and days <= 0:
            return CheckResult(
                "voice_retention",
                CheckStatus.WARNING,
                "Recording enabled but no retention window (transcripts kept forever)",
                fix_hint="Set PINCER_VOICE_TRANSCRIPT_RETENTION_DAYS (GDPR storage limitation)",
                category="voice",
            )
        if days > 0:
            return CheckResult(
                "voice_retention",
                CheckStatus.PASS,
                f"Transcripts purged after {days} day(s)",
                category="voice",
            )
        return CheckResult(
            "voice_retention",
            CheckStatus.PASS,
            "No retention window (recording disabled)",
            category="voice",
        )

    def _check_voice_provider_regions(self, cfg: Settings | None = None) -> CheckResult:
        cfg = self._cfg(cfg)
        if not (cfg.voice_enabled or cfg.voice_outbound_enabled):
            return CheckResult(
                "voice_provider_regions",
                CheckStatus.SKIPPED,
                "Voice not enabled",
                category="voice",
            )
        providers = []
        if cfg.twilio_account_sid:
            providers.append("Twilio (US default; EU/Ireland region configurable)")
        if cfg.deepgram_api_key.get_secret_value():
            providers.append("Deepgram STT (US default; EU endpoint available)")
        if cfg.elevenlabs_api_key.get_secret_value():
            providers.append("ElevenLabs TTS (US processing)")
        if not providers:
            return CheckResult(
                "voice_provider_regions",
                CheckStatus.PASS,
                "No external voice providers configured",
                category="voice",
            )
        return CheckResult(
            "voice_provider_regions",
            CheckStatus.PASS,
            "Data processing: " + "; ".join(providers),
            fix_hint="See docs/guides/dach-compliance.md for DPA/AVV guidance",
            category="voice",
        )

    def _check_voice_twilio_edge(self, cfg: Settings | None = None) -> CheckResult:
        """Hangup, transfer, DTMF and the outbound dial are Twilio REST calls,
        routed to the US unless an edge is set. Advice, never a failure: the
        default works, an EU edge only shortens the round-trip."""
        cfg = self._cfg(cfg)
        if not (cfg.voice_enabled or cfg.voice_outbound_enabled) or not cfg.twilio_account_sid:
            return CheckResult(
                "voice_twilio_edge",
                CheckStatus.SKIPPED,
                "Voice not enabled",
                category="voice",
            )
        edge = cfg.twilio_edge.strip()
        if edge:
            return CheckResult(
                "voice_twilio_edge",
                CheckStatus.PASS,
                f"Twilio REST calls go through the '{edge}' edge",
                category="voice",
            )
        return CheckResult(
            "voice_twilio_edge",
            CheckStatus.PASS,
            "Twilio edge not set: REST calls (hangup, transfer, dial) go to the US default",
            fix_hint="EU deployments benefit from PINCER_TWILIO_EDGE=frankfurt (or dublin)",
            category="voice",
        )

    def _check_voice_elevenlabs_voices(self, cfg: Settings | None = None) -> CheckResult:
        """Sprint 4: configured ElevenLabs voice IDs exist in the account, and
        the configured model speaks the supported languages."""
        cfg = self._cfg(cfg)
        if not (cfg.voice_enabled or cfg.voice_outbound_enabled):
            return CheckResult(
                "voice_elevenlabs_voices",
                CheckStatus.SKIPPED,
                "Voice not enabled",
                category="voice",
            )
        api_key = cfg.elevenlabs_api_key.get_secret_value()
        from pincer.voice.voices import VoiceLookupError, configured_voice_ids, voice_usable

        voice_ids = configured_voice_ids(cfg)
        if not api_key or not voice_ids:
            return CheckResult(
                "voice_elevenlabs_voices",
                CheckStatus.PASS,
                "No ElevenLabs voices configured (default/Google voice in use)",
                category="voice",
            )

        # English-only models break German calls even with a valid voice ID
        from pincer.voice.language import elevenlabs_model_for, supported_languages

        model = elevenlabs_model_for(cfg)
        english_only_models = {"eleven_flash_v2", "eleven_turbo_v2"}
        if model in english_only_models and any(lang != "en" for lang in supported_languages(cfg)):
            return CheckResult(
                "voice_elevenlabs_voices",
                CheckStatus.WARNING,
                f"Model {model} is English-only but non-English calls are enabled",
                fix_hint="Set PINCER_ELEVENLABS_MODEL=eleven_flash_v2_5 (multilingual, low latency)",
                category="voice",
            )

        missing = []
        for voice_id in sorted(voice_ids):
            try:
                if not voice_usable(api_key, voice_id):
                    missing.append(voice_id)
            except VoiceLookupError as e:
                return CheckResult(
                    "voice_elevenlabs_voices",
                    CheckStatus.WARNING,
                    f"Could not verify ElevenLabs voices: {e}",
                    category="voice",
                )
        if missing:
            return CheckResult(
                "voice_elevenlabs_voices",
                CheckStatus.WARNING,
                f"Voice ID(s) not usable by this ElevenLabs account: {', '.join(missing)}",
                fix_hint="Run `pincer voice list` and fix PINCER_ELEVENLABS_VOICE_ID / _EN / _DE",
                category="voice",
            )
        return CheckResult(
            "voice_elevenlabs_voices",
            CheckStatus.PASS,
            f"{len(voice_ids)} ElevenLabs voice ID(s) verified, model {model}",
            category="voice",
        )

    def _check_voice_language_pairing(self, cfg: Settings | None = None) -> CheckResult:
        """Each call language's voice is a voice *for* that language.

        ConversationRelay selects TTS by the call's language and accepts only a
        matching voice; given a mismatch it substitutes its own default voice
        and reports nothing — no Twilio alert, no error here. A German voice
        configured for English calls therefore passes every other check and
        still never reaches the caller's ear.
        """
        cfg = self._cfg(cfg)
        if not (cfg.voice_enabled or cfg.voice_outbound_enabled):
            return CheckResult(
                "voice_language_pairing",
                CheckStatus.SKIPPED,
                "Voice not enabled",
                category="voice",
            )
        if str(getattr(cfg, "voice_engine", "")).lower().strip() != "conversation_relay":
            return CheckResult(
                "voice_language_pairing",
                CheckStatus.SKIPPED,
                "Only ConversationRelay picks the voice by call language",
                category="voice",
            )
        if not cfg.elevenlabs_api_key.get_secret_value():
            return CheckResult(
                "voice_language_pairing",
                CheckStatus.SKIPPED,
                "No ElevenLabs key configured",
                category="voice",
            )

        from pincer.voice.voices import voice_language_mismatches

        mismatches = voice_language_mismatches(cfg)
        if mismatches:
            return CheckResult(
                "voice_language_pairing",
                CheckStatus.WARNING,
                "; ".join(mismatches.values()),
                fix_hint="Set PINCER_ELEVENLABS_VOICE_ID_EN / _DE / _UK to a voice in each language "
                "(`pincer voice list`)",
                category="voice",
            )
        return CheckResult(
            "voice_language_pairing",
            CheckStatus.PASS,
            "Every call language uses a voice in that language",
            category="voice",
        )

    # ── Google Workspace (issue #297) ─────────────────────

    def _check_google_workspace(self, cfg: Settings | None = None) -> CheckResult:
        """Whether a Google token is present with scopes — without it there are no calendar, Gmail or
        Drive tools. Read from the token file or env only: a revoked token still passes here."""
        from pincer.integrations import google
        from pincer.integrations.google.config import load_config

        settings = self._cfg(cfg)
        name, setup = "google_workspace_token", "Run `pincer google setup`"
        if not load_config().enabled:
            return CheckResult(name, CheckStatus.SKIPPED, "Google integration disabled in config", category="google")
        state = google.google_status(settings)
        if state["status"] == "active":
            services = ", ".join(state["enabled_services"])
            return CheckResult(
                name,
                CheckStatus.PASS,
                f"Google token found with scopes for: {services} (not checked against Google; "
                "a revoked token still shows here)",
                category="google",
            )
        if state["status"] == "partial":
            return CheckResult(
                name,
                CheckStatus.WARNING,
                f"Google token is missing scopes; only {', '.join(state['enabled_services'])} enabled",
                fix_hint=f"{setup} to re-consent",
                category="google",
            )
        return CheckResult(
            name,
            CheckStatus.SKIPPED,
            "No usable Google token — no calendar, Gmail or Drive tools",
            fix_hint=f"{setup} to connect",
            category="google",
        )

    # ── LLM effort and thinking (issue #345) ──────────────

    def _check_llm_compaction(self, cfg: Settings | None = None) -> CheckResult:
        """History compaction by prompt tokens (issue #357): the removed message-count
        summary setting, when still set, is reported as having no effect."""
        from pincer.core.compaction import SUMMARY_THRESHOLD_DEPRECATED

        cfg = self._cfg(cfg)
        if cfg.summary_threshold is not None:
            return CheckResult(
                "llm_compaction",
                CheckStatus.WARNING,
                SUMMARY_THRESHOLD_DEPRECATED,
                fix_hint="Remove PINCER_SUMMARY_THRESHOLD from .env",
                category="llm",
            )
        return CheckResult(
            "llm_compaction",
            CheckStatus.PASS,
            f"History compacted past {cfg.compact_at_tokens:,} prompt tokens, to about {cfg.compact_to_tokens:,}",
            category="llm",
        )

    def _check_llm_pricing(self, cfg: Settings | None = None) -> CheckResult:
        """Every model Pincer will call has a valid `[pricing]` entry (issue #357); the same
        check `pincer run` refuses to start on."""
        from pincer.config.pricing import paste_block
        from pincer.llm.pricing_check import GPT6_TOOLS_EXPLANATION, check_pricing

        report = check_pricing(self._cfg(cfg), self.config_dir)
        if report.errors:
            return CheckResult(
                "llm_pricing",
                CheckStatus.CRITICAL,
                f"Invalid [pricing] entries: {'; '.join(report.errors)}; Pincer will not start",
                fix_hint="Fix the [pricing] section of pincer.toml / pincer.local.toml",
                category="llm",
            )
        problems: list[str] = []
        hints: list[str] = []
        if report.missing:
            listed = ", ".join(
                f"{model} ({', '.join(f'{u.role} on {u.provider}' for u in uses)})"
                for model, uses in report.missing.items()
            )
            count = f"{len(report.missing)} model{'s' if len(report.missing) > 1 else ''}"
            problems.append(f"No price for {count} Pincer will call: {listed}")
            hints.append(
                "Add to pincer.local.toml and fill in the rates (USD per million tokens):\n"
                + paste_block(report.missing)
            )
        if report.conflicts:
            listed = " ".join(c.line() for c in report.conflicts).rstrip(".")  # the "; " join ends the sentence
            problems.append(f"gpt-6 settings that won't work with tools: {listed}")
            hints.append("\n".join([*(f"Fix: {c.fix}" for c in report.conflicts), GPT6_TOOLS_EXPLANATION]))
        if problems:
            return CheckResult(
                "llm_pricing",
                CheckStatus.CRITICAL,
                "; ".join(problems) + "; Pincer will not start",
                fix_hint="\n\n".join(hints),
                category="llm",
            )
        message = (
            f"Every model Pincer will call has a price: {', '.join(report.models)}"
            if report.models
            else "No model Pincer will call is billed (keyless endpoints only)"
        )
        if report.unknown_providers:
            from pincer.llm.router import known_provider_names

            named = ", ".join(f'[pricing.provider."{name}"]' for name in report.unknown_providers)
            return CheckResult(
                "llm_pricing",
                CheckStatus.WARNING,
                f"{message}; but {named} names no configured provider, so its multiplier applies to nothing",
                fix_hint=f"Use a configured provider name: {', '.join(sorted(known_provider_names(self._cfg(cfg))))}",
                category="llm",
            )
        return CheckResult("llm_pricing", CheckStatus.PASS, message, category="llm")

    def _check_llm_reasoning_default(self, cfg: Settings | None = None) -> CheckResult:
        """What the default provider's requests carry for PINCER_REASONING_EFFORT / PINCER_THINKING."""
        from pincer.llm.router import provider_default_model

        cfg = self._cfg(cfg)
        name = cfg.default_provider
        return _reasoning_report(
            "llm_reasoning_default",
            cfg,
            name,
            provider_default_model(cfg, name),
            cfg.reasoning_effort,
            cfg.thinking,
            with_tools=True,
        )

    def _check_llm_reasoning_voice(self, cfg: Settings | None = None) -> CheckResult:
        """The same for voice turns: the provider and model the call really runs on, and the
        voice settings, of which an empty one inherits the global setting."""
        from pincer.config.llm import effective_effort, effective_thinking
        from pincer.llm.router import plan_voice_turn

        cfg = self._cfg(cfg)
        if not (cfg.voice_enabled or cfg.voice_outbound_enabled):
            return CheckResult("llm_reasoning_voice", CheckStatus.SKIPPED, "Voice not enabled", category="llm")
        plan = plan_voice_turn(cfg)  # builds the client object only: nothing is sent
        return _reasoning_report(
            "llm_reasoning_voice",
            cfg,
            plan.provider,
            plan.effective_model,
            effective_effort(cfg.voice_reasoning_effort, cfg.reasoning_effort),
            effective_thinking(cfg.voice_thinking, cfg.thinking),
            ("PINCER_VOICE_REASONING_EFFORT", "PINCER_VOICE_THINKING"),
            with_tools=True,
        )

    def _check_llm_reasoning_summary(self, cfg: Settings | None = None) -> CheckResult:
        """The same for conversation summaries, on the model the summarizer asks for
        (as `LLMRouter.get_summarizer` picks it)."""
        from pincer.config.llm import effective_effort, effective_thinking
        from pincer.llm.router import provider_default_model, summary_target, wire_safe_model

        cfg = self._cfg(cfg)
        name, model = summary_target(cfg)
        return _reasoning_report(
            "llm_reasoning_summary",
            cfg,
            name,
            wire_safe_model(cfg, name, model) or provider_default_model(cfg, name),
            effective_effort(cfg.summary_reasoning_effort, cfg.reasoning_effort),
            effective_thinking(cfg.summary_thinking, cfg.thinking),
            ("PINCER_SUMMARY_REASONING_EFFORT", "PINCER_SUMMARY_THINKING"),
        )

    def _check_voice_thinking_token_limit(self, cfg: Settings | None = None) -> CheckResult:
        """Thinking and speech share the voice token limit: a model that thinks
        can spend a small limit before saying a word."""
        from pincer.config.llm import effective_effort, effective_thinking
        from pincer.llm.model_params import model_thinks, resolve_reasoning
        from pincer.llm.router import plan_voice_turn

        name = "voice_thinking_token_limit"
        cfg = self._cfg(cfg)
        if not (cfg.voice_enabled or cfg.voice_outbound_enabled):
            return CheckResult(name, CheckStatus.SKIPPED, "Voice not enabled", category="voice")
        plan = plan_voice_turn(cfg)  # builds the client object only: nothing is sent
        model = plan.effective_model
        wire = _reasoning_wire(cfg, plan.provider)
        thinks = None
        if wire is not None:
            effort = effective_effort(cfg.voice_reasoning_effort, cfg.reasoning_effort)
            thinking = effective_thinking(cfg.voice_thinking, cfg.thinking)
            # Voice turns carry tools: a gpt-6 model then gets none (issue #357)
            params = resolve_reasoning(wire, model, effort, thinking, with_tools=True)
            thinks = model_thinks(wire, model, params)
        limit = cfg.voice_max_response_tokens
        if thinks is None:
            return CheckResult(name, CheckStatus.PASS, f"Can't tell whether {model} thinks", category="voice")
        if thinks and limit < _VOICE_THINKING_MIN_TOKENS:
            return CheckResult(
                name,
                CheckStatus.WARNING,
                f"{model} thinks on voice turns, but the voice token limit is {limit}: "
                "thinking can use it up before the caller hears a word",
                fix_hint=(
                    f"Raise PINCER_VOICE_MAX_RESPONSE_TOKENS to at least {_VOICE_THINKING_MIN_TOKENS}, "
                    "or set PINCER_VOICE_THINKING=disabled where the model allows it"
                ),
                category="voice",
            )
        state = "thinks" if thinks else "doesn't think"
        return CheckResult(name, CheckStatus.PASS, f"{model} {state}; voice token limit {limit}", category="voice")

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
        gate = self._identity_gate(cfg, "signal_access_control", "signal")
        if isinstance(gate, CheckResult):
            return gate
        if gate:
            return CheckResult(
                "signal_access_control",
                CheckStatus.PASS,
                f"Signal access controlled by {gate}",
                category="signal",
            )
        return CheckResult(
            "signal_access_control",
            CheckStatus.WARNING,
            "Signal has no identities configured — any phone number can DM it",
            fix_hint=(
                "Add identities to the [identity] section of pincer.toml (or with `pincer identity create`), "
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

    def _check_schedule_action_types(self, cfg: Settings | None = None) -> CheckResult:
        """Issue #217: a schedule whose `action.type` has no handler in
        `run_scheduled_action` silently no-ops on every tick it comes due,
        with nothing but a log line to show for it. `run_scheduled_action`
        now self-disables such a schedule the first time it hits this case,
        but this check is the audit trail: it catches one that hasn't fired
        yet, and confirms a disabled one actually got cleaned up.
        """
        import asyncio
        import concurrent.futures
        import json

        from pincer.db.engine import _database_file, get_database_url, get_sync_url
        from pincer.services.scheduler import ScheduleService
        from pincer.tasks.actors import KNOWN_SCHEDULE_ACTION_TYPES

        settings = self._cfg(cfg)
        # Opening a SQLite file that isn't there creates it; a fresh install
        # has nothing to audit yet, so don't leave an empty database behind.
        database = _database_file(get_sync_url(settings.db_path))
        if database is not None and not database.exists():
            return CheckResult(
                "schedule_action_types",
                CheckStatus.SKIPPED,
                "No database yet",
                category="runtime",
            )

        def _read_rows() -> list[dict[str, Any]]:
            return asyncio.run(ScheduleService(get_database_url(settings.db_path)).list_all())

        try:
            # `run_all()` is also called from inside a running loop
            # (`GET /api/doctor`, `ga_gate.evaluate()`), where `asyncio.run`
            # refuses to start; give the query a loop of its own on a worker.
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                rows = _read_rows()
            else:
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                    rows = ex.submit(_read_rows).result()
        except Exception as e:
            # SQLAlchemy appends the full statement after the first line.
            reason = (str(e).splitlines() or [type(e).__name__])[0]
            return CheckResult(
                "schedule_action_types",
                CheckStatus.SKIPPED,
                f"Could not read schedules: {reason}",
                category="runtime",
            )

        unknown_enabled: list[str] = []
        unknown_disabled: list[str] = []
        for row in rows:
            action = json.loads(row["action"]) if isinstance(row["action"], str) else row["action"]
            action_type = action.get("type", "custom")
            if action_type in KNOWN_SCHEDULE_ACTION_TYPES:
                continue
            target = unknown_enabled if row["enabled"] else unknown_disabled
            target.append(f"{row['name']} ({action_type})")

        if unknown_enabled:
            return CheckResult(
                "schedule_action_types",
                CheckStatus.CRITICAL,
                f"{len(unknown_enabled)} enabled schedule(s) have no handler and are firing "
                f"no-ops every tick: {', '.join(unknown_enabled)}",
                fix_hint="Disable or delete these schedules, or register a handler for their action type",
                category="runtime",
            )
        if unknown_disabled:
            return CheckResult(
                "schedule_action_types",
                CheckStatus.WARNING,
                f"{len(unknown_disabled)} disabled schedule(s) reference an unknown action type "
                f"(already self-disabled, safe to delete): {', '.join(unknown_disabled)}",
                fix_hint="Delete these schedule rows once confirmed obsolete",
                category="runtime",
            )
        return CheckResult(
            "schedule_action_types",
            CheckStatus.PASS,
            "All schedules have a registered handler for their action type",
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
        """Report the live approval policy (``PINCER_APPROVAL_POLICY``).

        Note ``PINCER_TOOL_APPROVAL`` is legacy and unenforced — nothing reads
        it but this check used to.
        """
        cfg = self._cfg(cfg)
        mode = getattr(cfg, "approval_policy", "critical")
        from pincer.tools.approval import parse_tool_names

        never = sorted(parse_tool_names(getattr(cfg, "approval_never", None)))

        if mode == "none":
            return CheckResult(
                "tool_approval_mode",
                CheckStatus.WARNING,
                "Tool approval: none — no tool ever prompts, including sends, deletes and shell",
                fix_hint="Set PINCER_APPROVAL_POLICY=critical unless this is a trusted non-interactive deployment",
                category="runtime",
            )
        # ``never`` wins over every mode in ApprovalPolicy.requires_approval,
        # so it ungates tools under "all" just as it does under "critical".
        if never:
            msg = f"Tool approval: {mode}, with {len(never)} tool(s) force-ungated: {', '.join(never[:5])}"
            fix_hint = "Review PINCER_APPROVAL_NEVER — entries there bypass the approval policy"
            unconfined = [t for t in ("python_exec", "shell_exec") if t in never]
            if unconfined:
                names = " and ".join(unconfined)
                msg += f" — {names} {'have' if len(unconfined) > 1 else 'has'} no path confinement "
                msg += "(bypasses the file_read/file_write workspace sandbox)"
                fix_hint += (
                    f"; remove {names}, or accept that they can read/write anywhere the "
                    "process user can, unlike file_read/file_write"
                )
            return CheckResult(
                "tool_approval_mode",
                CheckStatus.WARNING,
                msg,
                fix_hint=fix_hint,
                category="runtime",
            )
        return CheckResult(
            "tool_approval_mode",
            CheckStatus.PASS,
            f"Tool approval: {mode}",
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


# ── LLM effort and thinking (issue #345) ──────────────────────────────────────


def _reasoning_wire(cfg: Settings, name: str) -> ReasoningWire | None:
    """The request format a provider name speaks, as the router builds it; None if unknown."""
    from pincer.llm.model_params import ReasoningWire
    from pincer.llm.router import is_openai_wire

    if is_openai_wire(cfg, name):
        return ReasoningWire.OPENAI
    if name == "anthropic" or (name and name == cfg.anthropic_compatible_provider):
        return ReasoningWire.ANTHROPIC
    return None


def _reasoning_report(
    check: str,
    cfg: Settings,
    provider: str,
    model: str,
    effort: ReasoningEffort,
    thinking: ThinkingMode,
    env: tuple[str, str] = ("PINCER_REASONING_EFFORT", "PINCER_THINKING"),
    *,
    with_tools: bool = False,
) -> CheckResult:
    """What a target's requests carry, from the resolver the providers use. No API call.

    ``env`` names the effort and thinking settings an operator changes for this target.
    ``with_tools``: the target's requests carry tools (chat and voice; summaries don't).
    """
    from pincer.llm.model_params import ReasoningWire, openai_tools_need_off, resolve_reasoning

    if not (effort or thinking):
        default = "model default"
        if with_tools and _reasoning_wire(cfg, provider) is ReasoningWire.OPENAI and openai_tools_need_off(model):
            default = "sends reasoning_effort none with tools (gpt-6 on Chat Completions)"
        return CheckResult(check, CheckStatus.PASS, f"{provider} {model}: nothing set; {default}", category="llm")
    wire = _reasoning_wire(cfg, provider)
    if wire is None:
        return CheckResult(
            check,
            CheckStatus.WARNING,
            f"Provider {provider!r} is not configured, so nothing can be resolved for {model}",
            fix_hint="Name a configured provider: openai, anthropic, or a PINCER_*_COMPATIBLE_PROVIDER",
            category="llm",
        )
    params = resolve_reasoning(wire, model, effort, thinking)
    sent = []
    sent_effort = params.fragment.get("output_config", {}).get("effort")
    if sent_effort:
        sent.append(
            f"effort {sent_effort}" if sent_effort == effort else f"effort {effort} → {sent_effort} (stepped down)"
        )
    sent_thinking = params.fragment.get("thinking", {}).get("type")
    if sent_thinking:
        sent.append(f"thinking {sent_thinking}")
    level = params.fragment.get("reasoning_effort")
    turned_off = thinking == "disabled" and not any(d.setting == "thinking" for d in params.dropped)
    if level and turned_off:
        # One knob on OpenAI: `disabled` sends the level that turns reasoning off, over any effort
        replaced = f" replaces effort {effort}" if effort else ""
        sent.append(f"reasoning_effort {level} (thinking disabled{replaced})")
    elif level:
        sent.append(
            f"reasoning_effort {level}" if level == effort else f"reasoning_effort {effort} → {level} (stepped down)"
        )
    if wire is ReasoningWire.OPENAI and thinking == "adaptive":
        sent.append("thinking adaptive ignored (OpenAI models have no such setting)")
    message = f"{provider} {model}: {', '.join(sent) or 'nothing sent'}"
    if not params.dropped:
        return CheckResult(check, CheckStatus.PASS, message, category="llm")
    labels = {"reasoning_effort": "effort", "thinking": "thinking"}
    notes = [f"{labels[d.setting]} {d.value} not sent: {d.reason}" for d in params.dropped]
    to_change = [env[0] if d.setting == "reasoning_effort" else env[1] for d in params.dropped]
    return CheckResult(
        check,
        CheckStatus.WARNING,
        "; ".join([message, *notes]),
        fix_hint=f"Change {' / '.join(dict.fromkeys(to_change))}, or use a model that takes the value",
        category="llm",
    )
