"""The startup price check: every model Pincer will call has a `[pricing]` entry (issue #357).

Each refusal is paired with its accepted twin, so a check that always refuses fails too.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from pydantic import SecretStr

from pincer.config import Settings
from pincer.llm.pricing_check import pricing_problems

if TYPE_CHECKING:
    from pathlib import Path

_RATES = "input = 1.0\ncache_write = 1.25\ncache_write_1h = 2.0\ncache_read = 0.1\noutput = 5.0\n"


def _settings(**overrides: object) -> Settings:
    """Explicit settings: no developer .env, one Anthropic key, voice off, every model pinned."""
    base: dict[str, object] = {
        "telegram_bot_token": "123456:TEST",
        "anthropic_api_key": "sk-ant-test",
        "openai_api_key": "",
        "default_provider": "anthropic",
        "fallback_providers": [],
        "default_model": "claude-a",
        "anthropic_model": "",
        "openai_model": "",
        "openai_compatible_provider": "",
        "openai_compatible_base_url": "",
        "openai_compatible_api_key": "",
        "openai_compatible_model": "",
        "anthropic_compatible_provider": "",
        "anthropic_compatible_base_url": "",
        "anthropic_compatible_api_key": "",
        "anthropic_compatible_model": "",
        "summary_provider": None,
        "summary_model": "claude-a",
        "voice_enabled": False,
        "voice_outbound_enabled": False,
        "voice_turn_model": "",
        "daily_budget_usd": 5.0,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[arg-type, call-arg]


def _priced(tmp_path: Path, *models: str) -> Path:
    (tmp_path / "pincer.toml").write_text("".join(f'[pricing."{m}"]\n{_RATES}\n' for m in models), encoding="utf-8")
    return tmp_path


def test_an_unpriced_chat_model_is_named_with_a_block_to_paste(tmp_path: Path) -> None:
    message = pricing_problems(
        _settings(default_model="claude-b", summary_model="claude-a"), _priced(tmp_path, "claude-a")
    )
    assert message == (
        "No price for 1 model Pincer will call:\n"
        "  - claude-b: chat on anthropic\n"
        "Prices are matched by the exact model name. Add the block below to pincer.local.toml "
        "(or pincer.toml) in the directory Pincer runs from, fill in the rates, and start again:\n"
        "\n"
        '[pricing."claude-b"]\n'
        "input = nan           # USD per million tokens, fill in\n"
        "cache_write = nan     # USD per million tokens, fill in (5-minute cache write)\n"
        "cache_write_1h = nan  # USD per million tokens, fill in (1-hour cache write)\n"
        "cache_read = nan      # USD per million tokens, fill in\n"
        "output = nan          # USD per million tokens, fill in\n"
        'source = ""           # the pricing page and the date you read it\n'
    )


def test_a_priced_chat_model_passes(tmp_path: Path) -> None:
    assert (
        pricing_problems(
            _settings(default_model="claude-b", summary_model="claude-a"), _priced(tmp_path, "claude-a", "claude-b")
        )
        is None
    )


def _first_line(message: str | None) -> str | None:
    return None if message is None else "\n".join(message.split("\n")[:2])


def test_a_provider_with_credentials_has_its_model_checked(tmp_path: Path) -> None:
    settings = _settings(openai_api_key="sk-test", openai_model="gpt-x")
    assert _first_line(pricing_problems(settings, _priced(tmp_path, "claude-a"))) == (
        "No price for 1 model Pincer will call:\n  - gpt-x: chat on openai"
    )


def test_a_model_setting_without_credentials_is_not_checked(tmp_path: Path) -> None:
    settings = _settings(openai_api_key="", openai_model="gpt-x")
    assert pricing_problems(settings, _priced(tmp_path, "claude-a")) is None


def test_a_keyed_compatible_endpoint_is_checked(tmp_path: Path) -> None:
    settings = _settings(
        openai_compatible_provider="azure",
        openai_compatible_base_url="https://res.openai.azure.com/openai/v1/",
        openai_compatible_api_key="key",
        openai_compatible_model="my-deployment",
    )
    assert _first_line(pricing_problems(settings, _priced(tmp_path, "claude-a"))) == (
        "No price for 1 model Pincer will call:\n  - my-deployment: chat on azure"
    )


def test_a_free_keyless_endpoint_is_exempt(tmp_path: Path) -> None:
    settings = _settings(
        default_provider="ollama",
        anthropic_api_key="",
        openai_compatible_provider="ollama",
        openai_compatible_base_url="http://localhost:11434/v1",
        openai_compatible_api_key="",
        openai_compatible_model="llama3.2",
        summary_model="llama3.2",
    )
    assert pricing_problems(settings, _priced(tmp_path)) is None


def test_a_keyless_anthropic_compatible_endpoint_is_exempt_and_keyed_is_checked(tmp_path: Path) -> None:
    keyless = _settings(
        anthropic_compatible_provider="foundry",
        anthropic_compatible_base_url="https://example.invalid/anthropic",
        anthropic_compatible_model="claude-deploy",
    )
    assert pricing_problems(keyless, _priced(tmp_path, "claude-a")) is None
    keyed = keyless.model_copy(update={"anthropic_compatible_api_key": SecretStr("key")})
    assert _first_line(pricing_problems(keyed, _priced(tmp_path, "claude-a"))) == (
        "No price for 1 model Pincer will call:\n  - claude-deploy: chat on foundry"
    )


def test_the_check_runs_whatever_the_budget(tmp_path: Path) -> None:
    for budget in (0.0, 5.0):
        settings = _settings(default_model="claude-b", daily_budget_usd=budget)
        assert _first_line(pricing_problems(settings, _priced(tmp_path, "claude-a"))) == (
            "No price for 1 model Pincer will call:\n  - claude-b: chat on anthropic"
        ), budget


def test_a_claude_id_never_sent_to_an_openai_wire_provider_is_not_checked(tmp_path: Path) -> None:
    # openai_model unset: the openai leaf would fall back to the claude default_model, which it never sends
    settings = _settings(openai_api_key="sk-test", openai_model="")
    assert pricing_problems(settings, _priced(tmp_path, "claude-a")) is None


def test_an_unpriced_summary_model_refuses_and_a_priced_one_passes(tmp_path: Path) -> None:
    settings = _settings(summary_model="claude-sum")
    assert _first_line(pricing_problems(settings, _priced(tmp_path, "claude-a"))) == (
        "No price for 1 model Pincer will call:\n  - claude-sum: summary on anthropic"
    )
    assert pricing_problems(settings, _priced(tmp_path, "claude-a", "claude-sum")) is None


def test_a_summary_model_on_a_provider_prefix_is_checked_there(tmp_path: Path) -> None:
    settings = _settings(openai_api_key="sk-test", openai_model="gpt-a", summary_model="openai:gpt-sum")
    assert _first_line(pricing_problems(settings, _priced(tmp_path, "claude-a", "gpt-a"))) == (
        "No price for 1 model Pincer will call:\n  - gpt-sum: summary on openai"
    )


def test_the_voice_model_is_checked_only_when_voice_is_on(tmp_path: Path) -> None:
    off = _settings(voice_turn_model="claude-voice")
    assert pricing_problems(off, _priced(tmp_path, "claude-a")) is None
    for flag in ("voice_enabled", "voice_outbound_enabled"):
        on = _settings(voice_turn_model="claude-voice", **{flag: True})
        assert _first_line(pricing_problems(on, _priced(tmp_path, "claude-a"))) == (
            "No price for 1 model Pincer will call:\n  - claude-voice: voice on anthropic"
        ), flag
        assert pricing_problems(on, _priced(tmp_path, "claude-a", "claude-voice")) is None


def test_every_missing_model_is_listed_once_with_every_use(tmp_path: Path) -> None:
    settings = _settings(
        default_model="claude-b", summary_model="claude-b", voice_enabled=True, voice_turn_model="claude-v"
    )
    message = pricing_problems(settings, _priced(tmp_path))
    assert message is not None
    assert message.split("\n")[:3] == [
        "No price for 2 models Pincer will call:",
        "  - claude-b: chat on anthropic, summary on anthropic",
        "  - claude-v: voice on anthropic",
    ]
    assert message.count('[pricing."claude-b"]') == 1
    assert message.count('[pricing."claude-v"]') == 1


def test_the_paste_block_pasted_unedited_still_refuses_naming_each_rate(tmp_path: Path) -> None:
    message = pricing_problems(_settings(default_model="claude-b", summary_model="claude-b"), _priced(tmp_path))
    assert message is not None
    block = message.split("\n\n", 1)[1]
    (tmp_path / "pincer.local.toml").write_text(block, encoding="utf-8")
    assert pricing_problems(_settings(default_model="claude-b", summary_model="claude-b"), tmp_path) == (
        "Invalid [pricing] entries in pincer.toml / pincer.local.toml:\n"
        '  - pricing."claude-b": input must be a number >= 0, got nan\n'
        '  - pricing."claude-b": cache_write must be a number >= 0, got nan\n'
        '  - pricing."claude-b": cache_write_1h must be a number >= 0, got nan\n'
        '  - pricing."claude-b": cache_read must be a number >= 0, got nan\n'
        '  - pricing."claude-b": output must be a number >= 0, got nan'
    )


def _doctor_check(tmp_path: Path, settings: Settings):  # type: ignore[no-untyped-def]
    from pincer.security.doctor import SecurityDoctor

    return SecurityDoctor(config_dir=tmp_path, data_dir=tmp_path)._check_llm_pricing(settings)


def test_doctor_reports_unpriced_models_with_the_block_to_paste(tmp_path: Path) -> None:
    from pincer.security.doctor import CheckResult, CheckStatus

    settings = _settings(
        default_model="claude-b", summary_model="claude-a", voice_enabled=True, voice_turn_model="claude-v"
    )
    assert _doctor_check(_priced(tmp_path, "claude-a"), settings) == CheckResult(
        "llm_pricing",
        CheckStatus.CRITICAL,
        "No price for 2 models Pincer will call: claude-b (chat on anthropic), claude-v (voice on anthropic); "
        "Pincer will not start",
        fix_hint="Add to pincer.local.toml and fill in the rates (USD per million tokens):\n"
        '[pricing."claude-b"]\n'
        "input = nan           # USD per million tokens, fill in\n"
        "cache_write = nan     # USD per million tokens, fill in (5-minute cache write)\n"
        "cache_write_1h = nan  # USD per million tokens, fill in (1-hour cache write)\n"
        "cache_read = nan      # USD per million tokens, fill in\n"
        "output = nan          # USD per million tokens, fill in\n"
        'source = ""           # the pricing page and the date you read it\n'
        "\n"
        '[pricing."claude-v"]\n'
        "input = nan           # USD per million tokens, fill in\n"
        "cache_write = nan     # USD per million tokens, fill in (5-minute cache write)\n"
        "cache_write_1h = nan  # USD per million tokens, fill in (1-hour cache write)\n"
        "cache_read = nan      # USD per million tokens, fill in\n"
        "output = nan          # USD per million tokens, fill in\n"
        'source = ""           # the pricing page and the date you read it\n',
        category="llm",
    )


def test_doctor_reports_invalid_entries(tmp_path: Path) -> None:
    from pincer.security.doctor import CheckResult, CheckStatus

    (tmp_path / "pincer.toml").write_text('[pricing."claude-a"]\ninput = -1\n', encoding="utf-8")
    assert _doctor_check(tmp_path, _settings()) == CheckResult(
        "llm_pricing",
        CheckStatus.CRITICAL,
        'Invalid [pricing] entries: pricing."claude-a": missing cache_write, cache_write_1h, cache_read, output; '
        'pricing."claude-a": input must be a number >= 0, got -1; Pincer will not start',
        fix_hint="Fix the [pricing] section of pincer.toml / pincer.local.toml",
        category="llm",
    )


@pytest.mark.parametrize(
    ("overrides", "priced", "message"),
    [
        (
            {"default_model": "claude-b", "summary_model": "claude-a"},
            ("claude-a", "claude-b"),
            "Every model Pincer will call has a price: claude-b, claude-a",
        ),
        ({}, ("claude-a",), "Every model Pincer will call has a price: claude-a"),
        (
            {
                "default_provider": "ollama",
                "anthropic_api_key": "",
                "openai_compatible_provider": "ollama",
                "openai_compatible_base_url": "http://localhost:11434/v1",
                "openai_compatible_model": "llama3.2",
                "summary_model": "llama3.2",
            },
            (),
            "No model Pincer will call is billed (keyless endpoints only)",
        ),
    ],
)
def test_doctor_passes_when_every_model_is_priced(
    tmp_path: Path, overrides: dict[str, object], priced: tuple[str, ...], message: str
) -> None:
    from pincer.security.doctor import CheckResult, CheckStatus

    assert _doctor_check(_priced(tmp_path, *priced), _settings(**overrides)) == CheckResult(
        "llm_pricing", CheckStatus.PASS, message, category="llm"
    )


def test_doctor_runs_the_pricing_check(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from pincer.security.doctor import SecurityDoctor

    settings = _settings()
    monkeypatch.setattr("pincer.config.get_settings_relaxed", lambda: settings)
    report = SecurityDoctor(config_dir=_priced(tmp_path, "claude-a"), data_dir=tmp_path).run_all()
    assert [(c.name, c.status.value) for c in report.checks if c.name == "llm_pricing"] == [("llm_pricing", "pass")]
