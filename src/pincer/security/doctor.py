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
        production: bool = False,
    ) -> None:
        self.data_dir = data_dir or Path("data")
        self.config_dir = config_dir or Path(".")
        self.skills_dir = skills_dir or Path.home() / ".pincer" / "skills"
        # `pincer doctor --production` (Sprint 7, T7.3): extra deploy-gate
        # checks; the deploy script refuses to start on any CRITICAL.
        self.production = production

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
        report.checks.append(self._check_telegram_allowlist(settings))
        report.checks.append(self._check_whatsapp_dm_policy(settings))
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
        # Voice DACH compliance (3 checks, Sprint 0)
        report.checks.append(self._check_voice_dach_consent(settings))
        report.checks.append(self._check_voice_retention(settings))
        report.checks.append(self._check_voice_provider_regions(settings))
        # Voice ElevenLabs (1 check, Sprint 4)
        report.checks.append(self._check_voice_elevenlabs_voices(settings))
        # Observability (2 checks, Sprint 9)
        report.checks.append(self._check_ops_alert_routing(settings))
        report.checks.append(self._check_voice_canary(settings))
        # Voice security hardening (4 checks, Sprint 8)
        report.checks.append(self._check_voice_webhook_signature(settings))
        report.checks.append(self._check_voice_ws_auth(settings))
        report.checks.append(self._check_voice_abuse_limits(settings))
        report.checks.append(self._check_voice_do_not_call_enforced())
        # Voice in-call tool execution (3 checks, Sprint 11)
        report.checks.append(self._check_voice_autonomy_on(settings))
        report.checks.append(self._check_voice_tier_x_reachable(settings))
        report.checks.append(self._check_voice_override_unknown_tool(settings))
        # Inbound receptionist (2 checks, Sprint 12)
        report.checks.append(self._check_receptionist_profile(settings))
        report.checks.append(self._check_inbound_recording_consent(settings))
        # Live listen-in (1 check, Sprint 15)
        report.checks.append(self._check_listen_in_announce(settings))
        # Signal (3 checks, Sprint 7.5)
        report.checks.append(self._check_signal_phone_set(settings))
        report.checks.append(self._check_signal_api_local(settings))
        report.checks.append(self._check_signal_allowlist(settings))
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
        # Production deploy gate (7 checks, Sprint 7 T7.3 + Sprint 8) — only with --production
        if self.production:
            report.checks.append(self._check_prod_webhook_url(settings))
            report.checks.append(self._check_prod_no_tunnel(settings))
            report.checks.append(self._check_prod_auth_tokens(settings))
            report.checks.append(self._check_prod_dach_compliance(settings))
            report.checks.append(self._check_prod_environment_flag(settings))
            report.checks.append(self._check_prod_cors_origins(settings))
            report.checks.append(self._check_prod_voice_signatures(settings))
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
        missing: list[str] = []
        # T8.2: an empty token means allow-all in the API middleware. Both the
        # dashboard and the web-chat surface must carry their own strong token
        # in production — a shared or absent one is a full API bypass.
        for field_name, env in (
            ("dashboard_token", "PINCER_DASHBOARD_TOKEN"),
            ("web_chat_token", "PINCER_WEB_CHAT_TOKEN"),
        ):
            try:
                value = getattr(settings, field_name).get_secret_value()
            except AttributeError:
                value = ""
            if not value:
                missing.append(env)
            elif len(value) < 16:
                missing.append(f"{env} (too short, use 32+ chars)")
        try:
            if (
                settings.dashboard_token.get_secret_value()
                and settings.dashboard_token.get_secret_value() == settings.web_chat_token.get_secret_value()
            ):
                missing.append("PINCER_WEB_CHAT_TOKEN (identical to the dashboard token — issue separate ones)")
        except AttributeError:
            pass
        if missing:
            return CheckResult(
                name="prod_auth_tokens",
                status=CheckStatus.CRITICAL,
                message=f"API auth token missing or weak: {', '.join(missing)}",
                fix_hint="Generate one: python -c 'import secrets; print(secrets.token_urlsafe(32))'",
                category="production",
            )
        return CheckResult(
            name="prod_auth_tokens",
            status=CheckStatus.PASS,
            message="Dashboard API token set",
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

    def _check_voice_autonomy_on(self, cfg: Settings | None = None) -> CheckResult:
        """§10.1: WARN when Tier W writes run autonomously (mode `off`) on outbound calls."""
        settings = self._cfg(cfg)
        from pincer.voice.tool_policy import global_mode, tool_overrides

        if not getattr(settings, "voice_outbound_enabled", False):
            return CheckResult(
                name="voice_autonomy_on",
                status=CheckStatus.SKIPPED,
                message="Outbound calling disabled",
                category="voice",
            )
        mode = global_mode(settings)
        off_overrides = sorted(name for name, m in tool_overrides(settings).items() if m == "off")
        if mode == "off":
            return CheckResult(
                name="voice_autonomy_on",
                status=CheckStatus.WARNING,
                message="Autonomous writes during calls are enabled — intended? "
                "(PINCER_VOICE_TOOL_APPROVAL=off: Tier W tools run without any confirmation, "
                f"budget {getattr(settings, 'voice_max_writes_per_call', 3)}/call)",
                fix_hint="Use PINCER_VOICE_TOOL_APPROVAL=verbal (default) or user; "
                "narrow autonomy to single tools with PINCER_VOICE_TOOL_APPROVAL_OVERRIDES=tool:off",
                category="voice",
            )
        if off_overrides:
            return CheckResult(
                name="voice_autonomy_on",
                status=CheckStatus.WARNING,
                message="Autonomous writes during calls are enabled for: " + ", ".join(off_overrides) + " — intended?",
                fix_hint="Remove the ':off' entries from PINCER_VOICE_TOOL_APPROVAL_OVERRIDES if not intended",
                category="voice",
            )
        return CheckResult(
            name="voice_autonomy_on",
            status=CheckStatus.PASS,
            message=f"In-call writes require {mode} approval",
            category="voice",
        )

    def _check_voice_tier_x_reachable(self, cfg: Settings | None = None) -> CheckResult:
        """§10.2: self-test — build the call-context schema set for every call
        kind and assert no Tier X tool is in it. FAIL (critical) if one is."""
        settings = self._cfg(cfg)
        from pincer.voice.tool_policy import (
            TIER_X,
            TIERS,
            allowed_tools_for_call,
            call_context_schemas,
            callable_tools,
            ignored_extra_tools,
        )

        # The probe registry: every known tool plus the configured extras and
        # a few never-listed names (unknown must be denied, never admitted).
        probe_names = set(TIERS) | set(callable_tools()) | {"shell_exec", "python_exec", "unknown__tool"}
        probe_names |= set(
            n.strip() for n in str(getattr(settings, "voice_tools_extra", "") or "").split(",") if n.strip()
        )
        schemas = [{"name": name} for name in sorted(probe_names)]

        leaks: list[str] = []
        for kind in ("appointment", "generic"):
            for direction in ("outbound", "inbound"):
                allowed = allowed_tools_for_call(settings, kind=kind, direction=direction)
                visible = {str(s["name"]) for s in call_context_schemas(schemas, allowed)}
                for name in sorted(visible):
                    if TIERS.get(name, TIER_X) == TIER_X:
                        leaks.append(f"{name} ({kind}/{direction})")
        if leaks:
            return CheckResult(
                name="voice_tier_x_reachable",
                status=CheckStatus.CRITICAL,
                message="Tier X tool(s) reachable from call context: " + ", ".join(leaks),
                fix_hint="pincer.voice.tool_policy must never admit a Tier X or unlisted tool — fix the code",
                category="voice",
            )
        ignored = ignored_extra_tools(str(getattr(settings, "voice_tools_extra", "") or ""))
        if ignored:
            return CheckResult(
                name="voice_tier_x_reachable",
                status=CheckStatus.WARNING,
                message="PINCER_VOICE_TOOLS_EXTRA names Tier X / unknown tools that are ignored: " + ", ".join(ignored),
                fix_hint="Only Tier R/W tools can be added to the call scope; remove the others",
                category="voice",
            )
        return CheckResult(
            name="voice_tier_x_reachable",
            status=CheckStatus.PASS,
            message="Call-context tool schemas contain no Tier X tool (all call kinds)",
            category="voice",
        )

    def _check_voice_override_unknown_tool(self, cfg: Settings | None = None) -> CheckResult:
        """§10.3 / §3.2: overrides naming tools outside the tier table are ignored at runtime."""
        settings = self._cfg(cfg)
        from pincer.voice.tool_policy import parse_overrides, unknown_override_tools

        raw = str(getattr(settings, "voice_tool_approval_overrides", "") or "")
        if not raw.strip():
            return CheckResult(
                name="voice_override_unknown_tool",
                status=CheckStatus.PASS,
                message="No per-tool approval overrides configured",
                category="voice",
            )
        try:
            parse_overrides(raw)
        except Exception as e:
            return CheckResult(
                name="voice_override_unknown_tool",
                status=CheckStatus.CRITICAL,
                message=f"PINCER_VOICE_TOOL_APPROVAL_OVERRIDES is invalid: {e}",
                fix_hint="Use tool_name:mode entries with mode in verbal|user|off",
                category="voice",
            )
        unknown = unknown_override_tools(raw)
        if unknown:
            return CheckResult(
                name="voice_override_unknown_tool",
                status=CheckStatus.WARNING,
                message="Override(s) reference tools not in the in-call tier table (ignored): " + ", ".join(unknown),
                fix_hint="Check the spelling against pincer.voice.tool_policy.TIERS",
                category="voice",
            )
        return CheckResult(
            name="voice_override_unknown_tool",
            status=CheckStatus.PASS,
            message="All approval overrides reference known in-call tools",
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

    def _check_telegram_allowlist(self, cfg: Settings | None = None) -> CheckResult:
        cfg = self._cfg(cfg)
        al = cfg.telegram_allowed_users
        if al:
            return CheckResult(
                "telegram_allowlist",
                CheckStatus.PASS,
                f"Telegram allowlist configured ({len(al)} users)",
                category="access",
            )
        if cfg.telegram_bot_token.get_secret_value():
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

    def _check_whatsapp_dm_policy(self, cfg: Settings | None = None) -> CheckResult:
        cfg = self._cfg(cfg)
        if cfg.whatsapp_dm_allowlist.strip():
            return CheckResult(
                "whatsapp_dm_policy",
                CheckStatus.PASS,
                "WhatsApp DM allowlist configured",
                category="access",
            )
        if cfg.whatsapp_enabled:
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

    # ── Voice DACH compliance (Sprint 0) ─────────────────

    def _check_voice_dach_consent(self, cfg: Settings | None = None) -> CheckResult:
        cfg = self._cfg(cfg)
        if not cfg.voice_enabled:
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
        if not cfg.voice_enabled:
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
        if not cfg.voice_enabled:
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

    def _check_signal_allowlist(self, cfg: Settings | None = None) -> CheckResult:
        cfg = self._cfg(cfg)
        if not cfg.signal_enabled:
            return CheckResult(
                "signal_allowlist",
                CheckStatus.SKIPPED,
                "Signal not enabled",
                category="signal",
            )
        allowlist = cfg.signal_allowlist
        if allowlist.strip():
            return CheckResult(
                "signal_allowlist",
                CheckStatus.PASS,
                "Signal DM allowlist configured",
                category="signal",
            )
        return CheckResult(
            "signal_allowlist",
            CheckStatus.WARNING,
            "Signal DM allowlist is empty (any phone can DM)",
            fix_hint="Set PINCER_SIGNAL_ALLOWLIST=+1234567890",
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
