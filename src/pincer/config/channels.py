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
    voice_recording_enabled: bool = Field(default=False, description="Enable call recording")
    voice_consent_mode: str = Field(
        default="one_party",
        description="Recording consent: one_party | two_party | none",
    )
    voice_outbound_enabled: bool = Field(default=False, description="Enable outbound calling")
    voice_outbound_max_daily: int = Field(default=10, ge=1, le=100, description="Max outbound calls/day")
    voice_retry_attempts: int = Field(
        default=0,
        ge=0,
        le=3,
        description="Automatic redial attempts after a failed outbound call (0 = none; reserved, not yet acted on)",
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
        default="3days.ai",
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
