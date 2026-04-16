"""Bot and user token management for the Slack integration.

Token resolution order (highest priority first):
  1. Environment variables  SLACK_BOT_TOKEN / SLACK_USER_TOKEN
  2. pincer.toml  [integrations.slack]
  3. ~/.pincer/slack_tokens.json  (written by ``pincer setup-slack``)
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

_TOKEN_FILE = "slack_tokens.json"


@dataclass
class SlackTokens:
    """Holds bot and optional user tokens."""

    bot_token: str = ""
    user_token: str = ""


def _token_path() -> Path:
    """Return the path to the persisted token cache file."""
    try:
        from pincer.config import get_settings_relaxed

        settings = get_settings_relaxed()
        return settings.data_dir / _TOKEN_FILE
    except Exception:
        return Path.home() / ".pincer" / _TOKEN_FILE


def load_tokens() -> SlackTokens:
    """Load tokens: env vars > pincer.toml > token file."""
    # 1. Environment variables (highest priority)
    bot = os.environ.get("SLACK_BOT_TOKEN", "")
    user = os.environ.get("SLACK_USER_TOKEN", "")
    if bot:
        return SlackTokens(bot_token=bot, user_token=user)

    # 2. pincer.toml
    try:
        from pincer.integrations.slack.config import load_config

        cfg = load_config()
        if cfg.bot_token:
            return SlackTokens(bot_token=cfg.bot_token, user_token=cfg.user_token)
    except Exception as exc:
        logger.debug("Could not load Slack config from toml: %s", exc)

    # 3. Token file
    path = _token_path()
    if path.exists():
        try:
            data = json.loads(path.read_text())
            return SlackTokens(
                bot_token=data.get("bot_token", ""),
                user_token=data.get("user_token", ""),
            )
        except Exception as exc:
            logger.warning("Could not read Slack token file %s: %s", path, exc)

    return SlackTokens()


def save_tokens(bot_token: str, user_token: str = "") -> Path:
    """Persist tokens to the token cache file (mode 0600)."""
    path = _token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"bot_token": bot_token, "user_token": user_token}
    path.write_text(json.dumps(data, indent=2))
    path.chmod(0o600)
    return path


async def validate_bot_token(bot_token: str) -> dict[str, str]:
    """Call auth.test to validate a bot token.

    Returns a dict with workspace/user info on success.
    Raises RuntimeError if the token is invalid or slack-sdk is not installed.
    """
    try:
        from slack_sdk.web.async_client import AsyncWebClient
    except ImportError as exc:
        raise RuntimeError(
            "slack-sdk not installed. Run: uv pip install 'pincer-agent[slack]'"
        ) from exc

    client = AsyncWebClient(token=bot_token)
    try:
        resp = await client.auth_test()
    except Exception as exc:
        raise RuntimeError(f"Slack auth.test failed: {exc}") from exc

    return {
        "workspace": str(resp.get("team", "")),
        "bot_user": str(resp.get("user", "")),
        "team_id": str(resp.get("team_id", "")),
        "user_id": str(resp.get("user_id", "")),
    }
