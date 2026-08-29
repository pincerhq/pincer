from __future__ import annotations

from pydantic import BaseModel, Field, SecretStr, field_validator


class ChannelSettings(BaseModel):
    # ── Telegram ─────────────────────────────────────────
    telegram_bot_token: SecretStr = Field(default=SecretStr(""), description="Telegram bot token")
    telegram_allowed_users: list[int] = Field(
        default_factory=list,
        description="Telegram user IDs allowed to use the bot (empty = allow all)",
    )

    # ── Discord ───────────────────────────────────────────
    discord_bot_token: SecretStr = Field(default=SecretStr(""), description="Discord bot token")
    discord_guild_allowlist: str = Field(
        default="",
        description="Comma-separated guild IDs allowed to use the bot (empty = allow all)",
    )

    # ── WhatsApp ─────────────────────────────────────────
    whatsapp_enabled: bool = Field(default=False, description="Enable WhatsApp channel")
    whatsapp_self_chat_only: bool = Field(
        default=True,
        description="When True, only self-chat and group mentions get a reply; DMs from others ignored.",
    )
    whatsapp_dm_allowlist: str = Field(
        default="",
        description="Comma-separated phone numbers allowed to DM; empty = self-chat and group mentions only.",
    )
    whatsapp_group_trigger: str = Field(
        default="pincer",
        description="Trigger word for group chat mentions",
    )
    whatsapp_show_progress: bool = Field(
        default=True,
        description="Edit an in-place status message while tools run (and show typing indicator).",
    )

    # ── Slack ─────────────────────────────────────────────
    slack_bot_token: SecretStr = Field(default=SecretStr(""), description="Slack Bot Token (xoxb-...)")
    slack_app_token: SecretStr = Field(
        default=SecretStr(""),
        description="Slack App-Level Token for Socket Mode (xapp-...)",
    )
    slack_user_allowlist: list[str] = Field(
        default_factory=list,
        description="Optional Slack user IDs allowed to use the bot (empty = allow all)",
    )

    # ── Microsoft Teams ───────────────────────────────────
    teams_app_id: str = Field(
        default="",
        description="Microsoft Teams / Azure Bot App (client) ID",
    )
    teams_app_tenant_id: str = Field(
        default="common",
        description="Microsoft Teams / Azure Bot App Tenant ID (common for personal accounts)",
    )
    teams_app_password: SecretStr = Field(
        default=SecretStr(""),
        description="Teams bot App Password (client secret)",
    )
    teams_port: int = Field(
        default=3978,
        ge=1,
        le=65535,
        description="Local port for the Teams bot HTTP server (/api/messages)",
    )
    teams_user_allowlist: list[str] = Field(
        default_factory=list,
        description="Optional Teams user AAD IDs / UPNs allowed to use the bot (empty = allow all)",
    )

    # ── Signal ────────────────────────────────────────────
    signal_enabled: bool = Field(default=False, description="Enable Signal channel")
    signal_api_url: str = Field(default="http://signal-api:8080", description="signal-cli-rest-api base URL")
    signal_pair_url: str = Field(
        default="http://127.0.0.1:8081",
        description="URL for browser-based pairing (host-facing); use when signal-api is in Docker",
    )
    signal_phone_number: str = Field(default="", description="Registered Signal phone number (E.164)")
    signal_allowlist: str = Field(
        default="",
        description="Comma-separated phone numbers allowed to DM; empty = allow all",
    )
    signal_group_reply: str = Field(
        default="mention_only",
        description="Group reply mode: mention_only | all | disabled",
    )
    signal_poll_interval: int = Field(default=2, ge=1, description="Poll interval in seconds")
    signal_receive_mode: str = Field(
        default="websocket",
        description="Receive mode: websocket | poll",
    )

    # ── Voice ─────────────────────────────────────────────
    voice_enabled: bool = Field(default=False, description="Enable voice calling channel")
    twilio_account_sid: str = Field(default="", description="Twilio Account SID")
    twilio_auth_token: SecretStr = Field(default=SecretStr(""), description="Twilio Auth Token")
    twilio_phone_number: str = Field(default="", description="Twilio phone number (E.164)")
    voice_engine: str = Field(
        default="conversation_relay",
        description="Voice engine: conversation_relay | media_streams",
    )
    voice_webhook_base_url: str = Field(default="", description="Public URL for Twilio webhooks")
    deepgram_api_key: SecretStr = Field(default=SecretStr(""), description="Deepgram STT API key")
    elevenlabs_api_key: SecretStr = Field(default=SecretStr(""), description="ElevenLabs TTS API key")
    elevenlabs_voice_id: str = Field(default="", description="ElevenLabs voice ID (fallback for all languages)")
    elevenlabs_voice_id_en: str = Field(default="", description="ElevenLabs voice for English calls")
    elevenlabs_voice_id_de: str = Field(default="", description="ElevenLabs voice for German calls")
    elevenlabs_voice_id_uk: str = Field(default="", description="ElevenLabs voice for Ukrainian calls")
    elevenlabs_model: str = Field(
        default="eleven_flash_v2_5",
        description="ElevenLabs TTS model (multilingual, low-latency by default)",
    )
    elevenlabs_stability: float = Field(default=0.5, ge=0.0, le=1.0, description="ElevenLabs voice stability")
    elevenlabs_similarity: float = Field(default=0.75, ge=0.0, le=1.0, description="ElevenLabs voice similarity boost")
    elevenlabs_speed: float = Field(default=1.0, ge=0.7, le=1.2, description="ElevenLabs speaking speed")
    elevenlabs_style: float = Field(
        default=0.0, ge=0.0, le=1.0, description="ElevenLabs style exaggeration (0 = off, lowest latency)"
    )
    elevenlabs_output_format: str = Field(
        default="ulaw_8000",
        description="ElevenLabs streaming output for Media Streams: ulaw_8000 (native telephony) | "
        "pcm_16000 (legacy resample fallback path)",
    )
    cr_tts_provider: str = Field(
        default="",
        description="ConversationRelay TTS provider: google | amazon | elevenlabs; "
        "empty = auto (elevenlabs when an ElevenLabs voice is configured, else google)",
    )
    voice_language: str = Field(default="en-US", description="Primary language for STT (legacy fallback)")
    voice_default_language: str = Field(
        default="en",
        description="Fallback call language when none is given or inferable (ISO 639-1)",
    )
    voice_supported_languages: str = Field(
        default="en,de,uk",
        description="Comma-separated languages accepted for the per-call language parameter",
    )
    voice_de_formality: str = Field(
        default="sie",
        description="German register: sie (formal, default) | du (informal)",
    )
    voice_max_call_duration: int = Field(default=600, ge=30, le=3600, description="Max call seconds")
    voice_max_hold_time: int = Field(default=300, ge=30, le=600, description="Max IVR hold seconds")
    voice_hangup_grace_s: float = Field(
        default=2.0,
        ge=0.0,
        le=10.0,
        description="Seconds to wait after the farewell has played before hanging up "
        "(the 'both said goodbye' window; the other party can still add something)",
    )
    voice_recording_enabled: bool = Field(default=False, description="Enable call recording")
    voice_consent_mode: str = Field(
        default="one_party",
        description="Recording consent: one_party | two_party | none",
    )
    voice_outbound_enabled: bool = Field(default=False, description="Enable outbound calling")
    demo_call_enabled: bool = Field(
        default=False,
        description=(
            "Let the public website request a demo call (POST /api/public/demo-call). "
            "Unauthenticated by design and rate-limited per number, per client and "
            "globally — leave off unless the marketing site needs it"
        ),
    )
    voice_outbound_max_daily: int = Field(default=10, ge=1, le=100, description="Max outbound calls/day")
    voice_retry_attempts: int = Field(
        default=2,
        ge=0,
        le=5,
        description="Automatic redial attempts after voicemail/no-answer on appointment scheduling calls (0 = none)",
    )
    voice_retry_delay_min: int = Field(
        default=30,
        ge=1,
        le=720,
        description="Minutes to wait before an appointment-call redial attempt",
    )
    # Latency (Sprint 5)
    voice_turn_model: str = Field(
        default="",
        description="Model for conversational voice turns. '' = default model; '<model>' = that model on the "
        "default provider; '<provider>:<model>' (e.g. 'openai:gpt-5-mini') = cross-provider tiering. "
        "Also switchable at runtime from the dashboard (PUT /api/voice/config).",
    )
    voice_max_response_tokens: int = Field(
        default=150,
        ge=32,
        le=1024,
        description="max_tokens per voice turn — short spoken replies keep latency and streaming granularity tight",
    )
    voice_stt_endpointing_ms: int = Field(
        default=0,
        ge=0,
        le=2000,
        description="Deepgram endpointing override in ms (0 = per-language default: en 300, de 400)",
    )
    voice_stt_utterance_end_ms: int = Field(
        default=0,
        ge=0,
        le=5000,
        description="Deepgram utterance_end_ms override (0 = per-language default: en 800, de 1000)",
    )
    # Appointment scheduling (Sprint 6)
    business_hours: str = Field(
        default="09:00-17:00",
        description="Business hours for proposed appointment slots (HH:MM-HH:MM, voice timezone)",
    )
    business_days: str = Field(
        default="mon,tue,wed,thu,fri",
        description="Business days for proposed appointment slots (comma-separated mon..sun)",
    )
    slot_buffer_min: int = Field(
        default=15,
        ge=0,
        le=120,
        description="Buffer minutes required around existing calendar events when proposing slots",
    )
    scheduling_calendar_id: str = Field(
        default="primary",
        description="Google Calendar ID used for free/busy and appointment events",
    )
    voice_stt_min_confidence: float = Field(
        default=0.55,
        ge=0.0,
        le=1.0,
        description="Below this STT confidence the agent asks the caller to repeat instead of acting (0 = disabled)",
    )
    voice_machine_detection: bool = Field(
        default=True,
        description="Twilio Answering Machine Detection on outbound calls (voicemail is reported, not conversed with)",
    )
    voice_auto_followup: bool = Field(
        default=False,
        description="Auto-execute post-call follow-up suggestions without a user turn "
        "(reserved; this build always proposes and waits for the user)",
    )
    voice_allowed_callers: str = Field(
        default="*",
        description="Comma-separated E.164 numbers allowed to call (or * for all)",
    )
    voice_filler_phrases: str = Field(
        default="",
        description="Custom filler phrases JSON array (empty = built-in)",
    )
    voice_consent_language: str = Field(
        default="",
        description="Language for consent/AI-disclosure announcements (ISO 639-1, e.g. 'de'); "
        "empty = follow call language, fallback 'en'",
    )
    voice_assistant_name: str = Field(
        default="Pincer",
        description="Name the assistant introduces itself with at call start (empty = skip introduction)",
    )
    voice_assistant_org: str = Field(
        default="",
        description="Organization named in the call introduction (empty = omitted)",
    )
    voice_assistant_owner: str = Field(
        default="",
        description="Person the assistant introduces itself as personal AI assistant of (empty = omitted)",
    )
    voice_intro_text: str = Field(
        default="",
        description="Verbatim override for the call introduction (bypasses name/org/owner)",
    )
    voice_transcript_retention_days: int = Field(
        default=90,
        ge=0,
        description="Days to keep call transcripts/recordings metadata (GDPR storage limitation); 0 = keep forever",
    )
    voice_timezone: str = Field(
        default="",
        description="IANA timezone for voice-facing time rendering (e.g. Europe/Berlin); empty = settings.timezone",
    )

    # ── Voice security & abuse prevention (Sprint 8) ──────
    voice_webhook_validate: bool = Field(
        default=True,
        description="Enforce X-Twilio-Signature validation on every /voice/* and "
        "/api/apps/twilio/* request (T8.1). Never disable in production.",
    )
    voice_ws_auth_required: bool = Field(
        default=True,
        description="Require a signed token on the ConversationRelay / Media Streams "
        "WebSocket upgrade; unsigned connects are refused before accept() (T8.1)",
    )
    voice_signature_max_age_s: int = Field(
        default=300,
        ge=30,
        le=3600,
        description="Replay guard: reject signed voice requests whose timestamp is older than this (seconds)",
    )
    # ── Live listen-in (Sprint 15) ─────────────────────
    listen_in_enabled: bool = Field(
        default=False,
        description="Let dashboard users listen to active calls live (listen-only). Adds a "
        "<Start><Stream> media fork to every call's TwiML; false = no fork at all",
    )
    listen_in_max_listeners: int = Field(
        default=2,
        ge=1,
        le=10,
        description="Maximum concurrent listeners per call",
    )
    listen_in_announce: bool = Field(
        default=True,
        description="Extend the call announcement with the monitoring notice "
        "('this call may be monitored for quality assurance'). false with listen-in "
        "enabled is a doctor FAIL unless two-party recording is already announced",
    )
    voice_daily_call_limit: int = Field(
        default=20,
        ge=0,
        le=1000,
        description="Hard server-side cap on outbound calls per day across ALL users and channels "
        "(0 = no global cap; the per-user limit still applies)",
    )
    voice_target_cooldown_min: int = Field(
        default=60,
        ge=0,
        le=10080,
        description="Minutes a target number is off-limits after a call to it (0 = no cooldown). "
        "Prevents hammering one number via retries or multiple channels.",
    )
    voice_quiet_hours: str = Field(
        default="20:00-08:00",
        description="No outbound calls during this local-time window (HH:MM-HH:MM in "
        "PINCER_VOICE_TIMEZONE); empty = no quiet hours. §7 UWG cold-calling sensitivity.",
    )
    voice_quiet_hours_override_users: str = Field(
        default="",
        description="Comma-separated user IDs allowed to place calls during quiet hours (emergency use)",
    )

    # ── In-call tool execution (Sprint 11) ────────────────
    voice_tool_approval: str = Field(
        default="verbal",
        description="Approval mode for Tier W (write) tools during a live call: "
        "verbal (call partner confirms the exact commitment) | user (initiating user approves on their "
        "channel) | off (autonomous within the write budget, disclosed in the post-call report)",
    )
    voice_tool_approval_overrides: str = Field(
        default="",
        description="Per-tool mode overrides, CSV of tool_name:mode "
        "(e.g. 'google__create_event:off,send_owner_message:user'). Tier X tools cannot be configured.",
    )
    voice_tool_timeout_s: int = Field(
        default=10,
        ge=3,
        le=60,
        description="Per-tool execution timeout during a call (seconds); a timeout defers to a post-call follow-up",
    )
    voice_approval_timeout_s: int = Field(
        default=25,
        ge=10,
        le=120,
        description="How long a 'user' mode approval may keep the call partner on hold (seconds)",
    )
    voice_max_writes_per_call: int = Field(
        default=3,
        ge=0,
        le=20,
        description="Tier W write budget per call (0 = writes disabled)",
    )
    voice_tools_extra: str = Field(
        default="",
        description="CSV of tool names added to the per-call tool scope (Tier R/W only; never widens the allowlist)",
    )

    # ── Inbound receptionist (Sprint 12) ──────────────────
    receptionist_enabled: bool = Field(
        default=False,
        description="Answer inbound calls as the AI receptionist of the business profile "
        "(false = today's generic inbound behaviour)",
    )
    business_profile: str = Field(
        default="./business_profile.yaml",
        description="Path to the business profile YAML (validated at startup when the receptionist is enabled)",
    )
    receptionist_booking_approval: str = Field(
        default="off",
        description="Approval for google__create_event on inbound calls: off (book immediately after the "
        "caller's yes) | user (owner approves on their channel while the caller holds). 'verbal' is rejected — "
        "the caller's yes IS the verbal step.",
    )
    inbound_max_concurrent: int = Field(
        default=3,
        ge=1,
        le=50,
        description="Active inbound calls above which the webhook answers with the busy line and hangs up",
    )
    inbound_recording: bool = Field(
        default=False,
        description="Record inbound receptionist calls — requires the two-party consent announcement (doctor FAIL)",
    )
    voice_blocklist: str = Field(
        default="",
        description="CSV of E.164 caller numbers that are declined with one neutral sentence",
    )

    # ── Call threads (Sprint 13) ──────────────────────────
    thread_match_window_days: int = Field(
        default=7,
        ge=0,
        le=365,
        description="Window in which an inbound call may be matched to an open thread by caller ID "
        "(0 = never match). Ambiguity never guesses: only EXACTLY one open match attaches.",
    )
    thread_inbound_context: str = Field(
        default="off",
        description="What a matched inbound call may know about its thread: off (nothing — the Sprint 12 "
        "receptionist behaviour) | ack (at most the neutral 'we have been in touch about this' line). "
        "Caller ID is spoofable, so subject, summary, dates and commitments are never spoken inbound.",
    )
    thread_autoclose_days: int = Field(
        default=30,
        ge=0,
        le=3650,
        description="Close a thread after this many days without activity (0 = never)",
    )

    # ── Email ─────────────────────────────────────────────
    email_imap_host: str = Field(default="", description="IMAP server host")
    email_imap_port: int = Field(default=993, description="IMAP server port")
    email_smtp_host: str = Field(default="", description="SMTP server host")
    email_smtp_port: int = Field(default=587, description="SMTP port (587=STARTTLS, 465=TLS)")
    email_username: str = Field(default="", description="Email account username")
    email_password: SecretStr = Field(default=SecretStr(""), description="Email account password")
    email_from: str = Field(default="", description="Override sender address")

    # ── Cross-Channel Identity ────────────────────────────
    identity_map: str = Field(
        default="",
        description="Cross-channel identity mapping, e.g. telegram:12345=whatsapp:491234567890",
    )
    default_user_id: str = Field(
        default="",
        description="Default pincer_user_id for proactive messages",
    )

    @field_validator("slack_user_allowlist", "teams_user_allowlist", mode="before")
    @classmethod
    def parse_slack_allowlist(cls, v: str | list[str]) -> list[str]:
        if isinstance(v, str):
            if not v.strip():
                return []
            return [uid.strip() for uid in v.split(",") if uid.strip()]
        return v

    @field_validator("voice_tool_approval", mode="before")
    @classmethod
    def validate_voice_tool_approval(cls, v: str) -> str:
        """Sprint 11 §3: an unknown mode is a startup ConfigError, never a fallback."""
        from pincer.voice.tool_policy import validate_approval_mode

        return validate_approval_mode(str(v))

    @field_validator("voice_tool_approval_overrides", mode="before")
    @classmethod
    def validate_voice_tool_overrides(cls, v: str) -> str:
        """Sprint 11 §3: malformed entries / unknown modes fail fast; a Tier X
        tool can never be configured ("excluded from calls")."""
        from pincer.voice.tool_policy import validate_overrides

        validate_overrides(str(v or ""))
        return str(v or "")

    @field_validator("thread_inbound_context", mode="before")
    @classmethod
    def validate_thread_inbound_context(cls, v: str) -> str:
        """Sprint 13 §4.3: only off|ack. An unknown value must fail at startup,
        never silently fall back to the more disclosing mode."""
        from pincer.exceptions import ConfigError

        mode = str(v or "off").strip().lower()
        if mode not in ("off", "ack"):
            raise ConfigError(f"PINCER_THREAD_INBOUND_CONTEXT={v!r} is not valid (allowed: off, ack)")
        return mode

    @field_validator("receptionist_booking_approval", mode="before")
    @classmethod
    def validate_receptionist_booking_approval(cls, v: str) -> str:
        """Sprint 12 §3: only off|user; verbal is meaningless on an inbound line."""
        from pincer.exceptions import ConfigError

        mode = str(v or "off").strip().lower()
        if mode not in ("off", "user"):
            raise ConfigError(
                f"PINCER_RECEPTIONIST_BOOKING_APPROVAL={v!r} is not valid (allowed: off, user; "
                "'verbal' is rejected — the caller's yes is the verbal step)"
            )
        return mode

    @field_validator("telegram_allowed_users", mode="before")
    @classmethod
    def parse_allowed_users(cls, v: list[int] | str) -> list[int]:
        if isinstance(v, int):
            return [v]
        elif isinstance(v, str):
            if not v.strip():
                return []
            return [int(uid.strip()) for uid in v.split(",") if uid.strip()]
        return v
