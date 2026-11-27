"""The startup price check: every model Pincer will call has a `[pricing]` entry (issue #357).

Each refusal is paired with its accepted twin, so a check that always refuses fails too.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest
from pydantic import SecretStr

from pincer.config import Settings
from pincer.llm.pricing_check import check_pricing, pricing_problems

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


def _with_multiplier(tmp_path: Path, provider: str) -> Path:
    _priced(tmp_path, "claude-a")
    (tmp_path / "pincer.local.toml").write_text(
        f'[pricing.provider."{provider}"]\nmultiplier = 1.1\n', encoding="utf-8"
    )
    return tmp_path


def test_a_multiplier_for_no_configured_provider_starts_and_doctor_warns(tmp_path: Path) -> None:
    from pincer.security.doctor import CheckResult, CheckStatus

    config = _with_multiplier(tmp_path, "foundy")
    assert pricing_problems(_settings(), config) is None  # never a reason to refuse
    assert _doctor_check(config, _settings()) == CheckResult(
        "llm_pricing",
        CheckStatus.WARNING,
        'Every model Pincer will call has a price: claude-a; but [pricing.provider."foundy"] names no '
        "configured provider, so its multiplier applies to nothing",
        fix_hint="Use a configured provider name: anthropic, openai",
        category="llm",
    )


def test_a_multiplier_for_a_configured_provider_passes(tmp_path: Path) -> None:
    from pincer.security.doctor import CheckResult, CheckStatus

    config = _with_multiplier(tmp_path, "foundry")
    settings = _settings(
        anthropic_compatible_provider="foundry", anthropic_compatible_base_url="https://example.invalid"
    )
    assert _doctor_check(config, settings) == CheckResult(
        "llm_pricing", CheckStatus.PASS, "Every model Pincer will call has a price: claude-a", category="llm"
    )


# ── gpt-6 on Chat Completions (slice 7, ruling Q13) ──────────────────────────
# Chat Completions takes function tools on gpt-6 only at reasoning_effort "none". Chat and
# voice send tools; the summary model does not.

_GPT6_FOOTER = (
    "Why: Pincer talks to OpenAI models through Chat Completions, which lets gpt-6 models use tools only "
    "with reasoning off. The summary model uses no tools, so PINCER_SUMMARY_REASONING_EFFORT still applies to it.\n"
    "To use reasoning with tools: add the endpoint's name to PINCER_RESPONSES_API_PROVIDERS if it serves "
    "/v1/responses (openai, azure and azure-openai already use it)."
)
_THEN = "Pincer then turns reasoning off for this model's tool requests."


def _gpt6(model: str = "gpt-6.1-terra", **overrides: object) -> Settings:
    """OpenAI as the default provider with a gpt-6 chat model, a priced summary model, voice off."""
    base: dict[str, object] = {
        "default_provider": "openai",
        "anthropic_api_key": "",
        "openai_api_key": "sk-test",
        "openai_model": model,
        "summary_model": "",
        "reasoning_effort": "",
        "thinking": "",
        "voice_reasoning_effort": "",
        "voice_thinking": "",
        "summary_reasoning_effort": "",
        "responses_api_providers": [],  # Chat Completions: the rules below; the Responses twins pass the default
    }
    base.update(overrides)
    return _settings(**base)


def _gpt6_priced(tmp_path: Path) -> Path:
    return _priced(tmp_path, "gpt-6.1-terra", "gpt-6-sol", "gpt-6-luna", "gpt-6.1-sol", "gpt-6-astra", "gpt-5-mini")


def test_a_chat_effort_on_gpt_6_refuses_and_unset_starts(tmp_path: Path) -> None:
    assert pricing_problems(_gpt6(reasoning_effort="high"), _gpt6_priced(tmp_path)) == (
        "1 gpt-6 setting won't work with tools:\n"
        "  - chat on openai, gpt-6.1-terra: PINCER_REASONING_EFFORT is high.\n"
        f"    Fix: unset PINCER_REASONING_EFFORT; {_THEN}\n" + _GPT6_FOOTER
    )
    assert pricing_problems(_gpt6(), _gpt6_priced(tmp_path)) is None


def test_a_voice_effort_on_gpt_6_refuses_and_unset_starts(tmp_path: Path) -> None:
    refused = _gpt6("gpt-5-mini", voice_enabled=True, voice_turn_model="gpt-6.1-terra", voice_reasoning_effort="high")
    assert pricing_problems(refused, _gpt6_priced(tmp_path)) == (
        "1 gpt-6 setting won't work with tools:\n"
        "  - voice on openai, gpt-6.1-terra: PINCER_VOICE_REASONING_EFFORT is high.\n"
        f"    Fix: unset PINCER_VOICE_REASONING_EFFORT; {_THEN}\n" + _GPT6_FOOTER
    )
    started = refused.model_copy(update={"voice_reasoning_effort": ""})
    assert pricing_problems(started, _gpt6_priced(tmp_path)) is None


def test_a_chat_effort_inherited_by_voice_names_the_voice_fix_and_default_starts(tmp_path: Path) -> None:
    inherited = _gpt6("gpt-5-mini", voice_enabled=True, voice_turn_model="gpt-6.1-terra", reasoning_effort="high")
    assert pricing_problems(inherited, _gpt6_priced(tmp_path)) == (
        "1 gpt-6 setting won't work with tools:\n"
        "  - voice on openai, gpt-6.1-terra: PINCER_REASONING_EFFORT is high, and voice inherits it.\n"
        "    Fix: set PINCER_VOICE_REASONING_EFFORT=default (or unset PINCER_REASONING_EFFORT); "
        f"{_THEN}\n" + _GPT6_FOOTER
    )
    default = inherited.model_copy(update={"voice_reasoning_effort": "default"})
    assert pricing_problems(default, _gpt6_priced(tmp_path)) is None


def test_an_effort_that_thinking_disabled_turns_to_none_starts(tmp_path: Path) -> None:
    """Refused on what would be sent: disabled replaces high with none (lead ruling 3, 2026-11-09)."""
    assert pricing_problems(_gpt6(reasoning_effort="high", thinking="disabled"), _gpt6_priced(tmp_path)) is None
    assert pricing_problems(_gpt6(reasoning_effort="high"), _gpt6_priced(tmp_path)) is not None


@pytest.mark.parametrize("model", ["gpt-6-astra", "gpt-6.1-sol"])
def test_a_gpt_6_model_with_no_none_refuses_for_chat_at_any_effort_and_starts_as_summary(
    tmp_path: Path, model: str
) -> None:
    line = (
        f"  - chat on openai, {model}: this model can't use tools at all, because its reasoning can't be turned off.\n"
        f"    Fix: use gpt-6.1-terra, gpt-6-sol, gpt-6-luna or a gpt-5 model for chat; {model} is fine as the "
        "summary model.\n"
    )
    for effort in ("", "low"):
        message = pricing_problems(_gpt6(model, reasoning_effort=effort), _gpt6_priced(tmp_path))
        assert message == "1 gpt-6 setting won't work with tools:\n" + line + _GPT6_FOOTER, effort
    summary = _gpt6("gpt-5-mini", summary_model=model, summary_reasoning_effort="high")
    assert pricing_problems(summary, _gpt6_priced(tmp_path)) is None


@pytest.mark.parametrize("model", ["gpt-6-astra", "gpt-6.1-sol"])
def test_a_gpt_6_model_with_no_none_refuses_for_voice_and_starts_as_summary(tmp_path: Path, model: str) -> None:
    voice = _gpt6("gpt-5-mini", voice_enabled=True, voice_turn_model=model)
    assert pricing_problems(voice, _gpt6_priced(tmp_path)) == (
        "1 gpt-6 setting won't work with tools:\n"
        f"  - voice on openai, {model}: this model can't use tools at all, because its reasoning can't be turned off.\n"
        f"    Fix: use gpt-6.1-terra, gpt-6-sol, gpt-6-luna or a gpt-5 model for voice; {model} is fine as the "
        "summary model.\n" + _GPT6_FOOTER
    )
    summary = voice.model_copy(update={"voice_turn_model": "", "summary_model": model})
    assert pricing_problems(summary, _gpt6_priced(tmp_path)) is None


def test_a_free_keyless_endpoint_is_checked_for_gpt_6_effort(tmp_path: Path) -> None:
    """Free endpoints skip the price check, not this one: the 400 comes from the model (lead ruling 4)."""
    free = _gpt6(
        "gpt-5-mini",
        default_provider="gw",
        openai_api_key="",
        openai_compatible_provider="gw",
        openai_compatible_base_url="http://localhost:9999/v1",
        openai_compatible_api_key="",
        openai_compatible_model="gpt-6.1-terra",
        reasoning_effort="low",
    )
    assert pricing_problems(free, _priced(tmp_path)) == (
        "1 gpt-6 setting won't work with tools:\n"
        "  - chat on gw, gpt-6.1-terra: PINCER_REASONING_EFFORT is low.\n"
        f"    Fix: unset PINCER_REASONING_EFFORT; {_THEN}\n" + _GPT6_FOOTER
    )
    assert pricing_problems(free.model_copy(update={"reasoning_effort": ""}), _priced(tmp_path)) is None


def test_doctor_reports_a_gpt_6_conflict_and_passes_its_twin(tmp_path: Path) -> None:
    from pincer.security.doctor import CheckResult, CheckStatus

    priced = _gpt6_priced(tmp_path)
    assert _doctor_check(priced, _gpt6(reasoning_effort="high")) == CheckResult(
        "llm_pricing",
        CheckStatus.CRITICAL,
        "gpt-6 settings that won't work with tools: chat on openai, gpt-6.1-terra: PINCER_REASONING_EFFORT is high; "
        "Pincer will not start",
        fix_hint=f"Fix: unset PINCER_REASONING_EFFORT; {_THEN}\n" + _GPT6_FOOTER,
        category="llm",
    )
    assert _doctor_check(priced, _gpt6()).status is CheckStatus.PASS


def test_doctor_reports_a_gpt_6_conflict_next_to_a_missing_price(tmp_path: Path) -> None:
    from pincer.security.doctor import CheckStatus

    result = _doctor_check(_priced(tmp_path, "gpt-5-mini"), _gpt6(reasoning_effort="high"))
    assert result.status is CheckStatus.CRITICAL
    assert result.message.startswith(
        "No price for 1 model Pincer will call: gpt-6.1-terra (chat on openai, summary on openai); "
    )
    assert (
        "gpt-6 settings that won't work with tools: chat on openai, gpt-6.1-terra: PINCER_REASONING_EFFORT is high;"
        in result.message
    )


def test_several_conflicts_are_listed_once_each_with_one_explanation(tmp_path: Path) -> None:
    settings = _gpt6(reasoning_effort="high", voice_enabled=True, voice_turn_model="gpt-6.1-sol")
    assert pricing_problems(settings, _gpt6_priced(tmp_path)) == (
        "2 gpt-6 settings won't work with tools:\n"
        "  - chat on openai, gpt-6.1-terra: PINCER_REASONING_EFFORT is high.\n"
        f"    Fix: unset PINCER_REASONING_EFFORT; {_THEN}\n"
        "  - voice on openai, gpt-6.1-sol: this model can't use tools at all, "
        "because its reasoning can't be turned off.\n"
        "    Fix: use gpt-6.1-terra, gpt-6-sol, gpt-6-luna or a gpt-5 model for voice; gpt-6.1-sol is fine as the "
        "summary model.\n" + _GPT6_FOOTER
    )


# ── the MCP sampling model (PR #366 review) ──────────────────────────────────

_SAMPLING_ON = '[mcp.server]\nenabled = true\n[mcp.server.sampling]\nenabled = true\nmodel = "claude-s"\n'


def _with_sampling(config: Path, toml: str = _SAMPLING_ON) -> Path:
    with (config / "pincer.toml").open("a", encoding="utf-8") as f:
        f.write(toml)
    return config


def test_an_unpriced_sampling_model_is_named(tmp_path: Path) -> None:
    assert _first_line(pricing_problems(_settings(), _with_sampling(_priced(tmp_path, "claude-a")))) == (
        "No price for 1 model Pincer will call:\n  - claude-s: sampling on anthropic"
    )


def test_a_priced_sampling_model_passes(tmp_path: Path) -> None:
    assert pricing_problems(_settings(), _with_sampling(_priced(tmp_path, "claude-a", "claude-s"))) is None


def test_a_sampling_model_is_not_checked_while_sampling_is_off(tmp_path: Path) -> None:
    sampling_off = _SAMPLING_ON.replace("sampling]\nenabled = true", "sampling]\nenabled = false")
    assert sampling_off != _SAMPLING_ON
    assert pricing_problems(_settings(), _with_sampling(_priced(tmp_path, "claude-a"), sampling_off)) is None


_SAMPLING_UNPRICED = "No price for 1 model Pincer will call:\n  - claude-s: sampling on anthropic"
_SAMPLING_SERVER_UNSET = _SAMPLING_ON.replace("[mcp.server]\nenabled = true\n", "")


@pytest.mark.parametrize("value", ["true", "1", "yes", "TRUE"])
def test_the_export_turned_on_by_env_alone_has_its_sampling_model_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """The documented Docker setup turns the server on with PINCER_MCP_SERVER_EXPORT_ENABLED only (#370)."""
    monkeypatch.setenv("PINCER_MCP_SERVER_EXPORT_ENABLED", value)
    config = _with_sampling(_priced(tmp_path, "claude-a"), _SAMPLING_SERVER_UNSET)
    assert _first_line(pricing_problems(_settings(), config)) == _SAMPLING_UNPRICED


@pytest.mark.parametrize(
    ("var", "value"),
    [
        ("PINCER_MCP_ENABLED", "false"),
        ("PINCER_MCP_ENABLED", "0"),
        ("PINCER_MCP_ENABLED", "No"),
        ("PINCER_MCP_SERVER_EXPORT_ENABLED", "false"),
        ("PINCER_MCP_SERVER_EXPORT_ENABLED", "off"),
    ],
)
def test_a_sampling_model_is_not_checked_when_env_turns_mcp_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, var: str, value: str
) -> None:
    """The runtime never samples then, so `pincer run` must not refuse over the model (#370)."""
    monkeypatch.setenv(var, value)
    assert pricing_problems(_settings(), _with_sampling(_priced(tmp_path, "claude-a"))) is None


def test_mcp_enabled_by_env_does_not_override_a_toml_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """As at runtime: PINCER_MCP_ENABLED can only turn MCP off."""
    monkeypatch.setenv("PINCER_MCP_ENABLED", "true")
    config = _with_sampling(_priced(tmp_path, "claude-a"), "[mcp]\nenabled = false\n" + _SAMPLING_ON)
    assert pricing_problems(_settings(), config) is None


@pytest.mark.parametrize(
    "env",
    [
        {},
        {"PINCER_MCP_ENABLED": "false"},
        {"PINCER_MCP_ENABLED": "true"},
        {"PINCER_MCP_SERVER_EXPORT_ENABLED": "true"},
        {"PINCER_MCP_SERVER_EXPORT_ENABLED": "false"},
        {"PINCER_MCP_SERVER_EXPORT_ENABLED": "1", "PINCER_MCP_ENABLED": "no"},
    ],
)
@pytest.mark.parametrize(
    "toml",
    [_SAMPLING_ON, _SAMPLING_SERVER_UNSET, "[mcp]\nenabled = false\n" + _SAMPLING_ON, "[mcp.server]\nenabled = true\n"],
    ids=["all-on", "server-unset", "mcp-off", "sampling-unset"],
)
def test_the_sampling_check_agrees_with_the_runtime_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env: dict[str, str], toml: str
) -> None:
    """The check samples exactly when the runtime's MCP config would, whichever switch decides."""
    from pincer.mcp.config import load_mcp_config, sampling_model

    monkeypatch.setattr("pincer.mcp.config._load_dotenv_into_environ", lambda: None)
    for var, value in env.items():
        monkeypatch.setenv(var, value)
    (tmp_path / "pincer.toml").write_text(toml, encoding="utf-8")
    runtime = load_mcp_config(tmp_path)
    samples = runtime.enabled and runtime.server.enabled and runtime.server.sampling.enabled
    assert sampling_model(tmp_path) == (runtime.server.sampling.model if samples else None)


@pytest.mark.parametrize("local", ["mcp = []\n", "mcp = {}\n", 'mcp = ""\n'])
def test_an_empty_local_mcp_value_is_ignored_as_at_runtime(tmp_path: Path, local: str) -> None:
    """The runtime skips an empty [mcp] in pincer.local.toml, so the check still sees pincer.toml's."""
    config = _with_sampling(_priced(tmp_path, "claude-a"))
    (config / "pincer.local.toml").write_text(local, encoding="utf-8")
    assert _first_line(pricing_problems(_settings(), config)) == _SAMPLING_UNPRICED


@pytest.mark.parametrize(
    "local",
    ["mcp = 1\n", '[[mcp.servers]]\ntransport = "stdio"\n'],
    ids=["mcp-not-a-table", "server-entry-without-name"],
)
def test_a_local_mcp_section_that_cant_be_merged_gives_no_sampling_model(tmp_path: Path, local: str) -> None:
    """The runtime's own load refuses it; the price check does not crash over it."""
    config = _with_sampling(_priced(tmp_path, "claude-a"))
    (config / "pincer.local.toml").write_text(local, encoding="utf-8")
    assert pricing_problems(_settings(), config) is None


@pytest.mark.parametrize(
    ("base", "local"),
    [
        (_SAMPLING_ON + '[[mcp.servers]]\nname = ["a"]\n', '[[mcp.servers]]\nname = "b"\n'),
        ("[mcp]\nserver = 5\n", "[mcp.server]\nenabled = true\n[mcp.server.sampling]\nenabled = true\n"),
    ],
    ids=["server-name-not-a-string", "server-not-a-table-under-a-local-table"],
)
def test_sections_that_cant_be_merged_are_logged_not_raised(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, base: str, local: str
) -> None:
    """`_merge_mcp_raw` would raise TypeError on these; the check logs them and skips sampling."""
    from pincer.mcp.config import sampling_model

    config = _with_sampling(_priced(tmp_path, "claude-a"), base)
    (config / "pincer.local.toml").write_text(local, encoding="utf-8")
    with caplog.at_level("WARNING", logger="pincer.mcp.config"):
        assert sampling_model(config) is None
        assert pricing_problems(_settings(), config) is None
    assert "can't be merged" in caplog.text


@pytest.mark.parametrize("kind", ["directory", "unreadable", "invalid-toml"])
def test_an_unreadable_local_toml_does_not_crash_the_check(tmp_path: Path, kind: str) -> None:
    """The sampling lookup leaves it to the MCP config's own load, like a malformed section; the
    price table, which can't be known without the file, refuses naming it (#370)."""
    from pincer.mcp.config import sampling_model

    config = _with_sampling(_priced(tmp_path, "claude-a"))
    local = config / "pincer.local.toml"
    if kind == "directory":
        local.mkdir()
    elif kind == "unreadable":
        if os.geteuid() == 0:
            pytest.skip("root reads a 0o000 file")
        local.write_text("[mcp]\n", encoding="utf-8")
        local.chmod(0o000)
    else:
        local.write_text("[mcp\n", encoding="utf-8")
    try:
        assert sampling_model(config) is None
        problems = pricing_problems(_settings(), config)
        assert problems is not None
        assert problems.startswith(
            "Invalid [pricing] entries in pincer.toml / pincer.local.toml:\n  - pincer.local.toml: "
        )
    finally:
        if local.is_file():
            local.chmod(0o644)


@pytest.mark.parametrize(
    "toml",
    [
        "[mcp]\nenabled = false\n" + _SAMPLING_ON,
        _SAMPLING_ON.replace("[mcp.server]\nenabled = true", "[mcp.server]\nenabled = false"),
        _SAMPLING_ON.replace("[mcp.server]\nenabled = true\n", ""),
    ],
    ids=["mcp-off", "server-off", "server-enabled-unset"],
)
def test_a_sampling_model_is_not_checked_when_the_runtime_would_never_sample(tmp_path: Path, toml: str) -> None:
    """`pincer mcp serve` exits unless [mcp.server] is enabled (default off) and [mcp] is on (default on):
    a leftover sampling table must not stop `pincer run` from starting."""
    assert toml != _SAMPLING_ON
    assert pricing_problems(_settings(), _with_sampling(_priced(tmp_path, "claude-a"), toml)) is None


@pytest.mark.parametrize(
    "toml",
    [
        '[mcp]\nserver = "x"\n',  # not a table
        "[mcp.server]\nenabled = true\nsampling = 1\n",  # not a table
        "[mcp.server]\nenabled = true\n[mcp.server.sampling]\nenabled = true\nmodel = 5\n",  # not a string
        '[mcp.server]\nenabled = true\n[mcp.server.sampling]\nenabled = true\nbudget_per_day = "x"\n',  # ValueError
    ],
    ids=[
        "server-not-a-table",
        "sampling-not-a-table",
        "model-not-a-string",
        "budget-not-a-number",
    ],
)
def test_a_malformed_mcp_section_does_not_crash_the_check(tmp_path: Path, toml: str) -> None:
    """The MCP config's own load reports it; the price check just has no sampling model."""
    assert pricing_problems(_settings(), _with_sampling(_priced(tmp_path, "claude-a"), toml)) is None


def test_the_price_check_does_not_load_the_working_directorys_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sentinel = "PINCER_TEST_DOTENV_SENTINEL"
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    (cwd / ".env").write_text(f"{sentinel}=1\n", encoding="utf-8")
    monkeypatch.chdir(cwd)
    monkeypatch.delenv(sentinel, raising=False)
    # conftest disarms every .env loader for all tests; re-arm the MCP one, as production has it
    monkeypatch.delenv("PYTHON_DOTENV_DISABLED", raising=False)
    monkeypatch.setattr("pincer.mcp.config._DOTENV_CANDIDATES", (".env", "../.env"))
    try:
        pricing_problems(_settings(), _with_sampling(_priced(tmp_path, "claude-a", "claude-s")))
        assert os.environ.get(sentinel) is None  # not `in os.environ`: its failure would print the whole env
    finally:
        os.environ.pop(sentinel, None)


def _dotenv_in_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    """A .env in the working directory, with the MCP .env candidates armed as in production."""
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    (cwd / ".env").write_text(text, encoding="utf-8")
    monkeypatch.chdir(cwd)
    monkeypatch.delenv("PYTHON_DOTENV_DISABLED", raising=False)
    monkeypatch.setattr("pincer.mcp.config._DOTENV_CANDIDATES", (".env", "../.env"))


def test_the_export_turned_on_in_env_file_alone_has_its_sampling_model_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`load_mcp_config` loads .env before `pincer mcp serve` decides, so the check reads it too
    (without copying it into os.environ)."""
    _dotenv_in_cwd(tmp_path, monkeypatch, "PINCER_MCP_SERVER_EXPORT_ENABLED=true\n")
    config = _with_sampling(_priced(tmp_path, "claude-a"), _SAMPLING_SERVER_UNSET)
    assert _first_line(pricing_problems(_settings(), config)) == _SAMPLING_UNPRICED
    assert os.environ.get("PINCER_MCP_SERVER_EXPORT_ENABLED") is None


def test_mcp_turned_off_in_env_file_skips_the_sampling_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _dotenv_in_cwd(tmp_path, monkeypatch, "PINCER_MCP_ENABLED=false\n")
    assert pricing_problems(_settings(), _with_sampling(_priced(tmp_path, "claude-a"))) is None


def test_a_disabled_dotenv_is_not_read_by_the_check_either(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With PYTHON_DOTENV_DISABLED the runtime never loads .env, so its switches don't count here."""
    _dotenv_in_cwd(tmp_path, monkeypatch, "PINCER_MCP_ENABLED=false\n")
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    assert _first_line(pricing_problems(_settings(), _with_sampling(_priced(tmp_path, "claude-a")))) == (
        _SAMPLING_UNPRICED
    )


def test_env_file_interpolation_resolves_as_at_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`${VAR}` in .env resolves against the process environment first, as `load_dotenv(override=False)`."""
    _dotenv_in_cwd(tmp_path, monkeypatch, "PINCER_TEST_MCP_ON=false\nPINCER_MCP_ENABLED=${PINCER_TEST_MCP_ON}\n")
    monkeypatch.setenv("PINCER_TEST_MCP_ON", "true")
    assert _first_line(pricing_problems(_settings(), _with_sampling(_priced(tmp_path, "claude-a")))) == (
        _SAMPLING_UNPRICED
    )
    assert os.environ.get("PINCER_MCP_ENABLED") is None  # the loader's additions are taken back


def test_the_process_environment_wins_over_the_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """As `load_dotenv(override=False)`: a variable already set is never replaced by .env."""
    _dotenv_in_cwd(tmp_path, monkeypatch, "PINCER_MCP_SERVER_EXPORT_ENABLED=true\n")
    monkeypatch.setenv("PINCER_MCP_SERVER_EXPORT_ENABLED", "false")
    assert pricing_problems(_settings(), _with_sampling(_priced(tmp_path, "claude-a"))) is None


def test_an_unreadable_mcp_config_is_logged(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """The check skips the sampling model, but says why instead of skipping it silently."""
    from pincer.mcp.config import sampling_model

    (tmp_path / "pincer.local.toml").mkdir()
    with caplog.at_level("WARNING", logger="pincer.mcp.config"):
        assert sampling_model(tmp_path) is None
    assert f"MCP config in {tmp_path} can't be read" in caplog.text
    assert "the price check skips the MCP sampling model" in caplog.text


def test_a_sampling_model_is_not_checked_without_an_anthropic_key(tmp_path: Path) -> None:
    settings = _settings(anthropic_api_key="", openai_api_key="sk-test", default_provider="openai", openai_model="m")
    assert pricing_problems(settings, _with_sampling(_priced(tmp_path, "claude-a", "m"))) is None


# ── gpt-6 on the Responses API (slice 8): nothing to refuse ──────────────────

_RESPONSES = {"responses_api_providers": ["openai", "azure", "azure-openai"]}


@pytest.mark.parametrize("effort", ["", "low", "high", "max"])
@pytest.mark.parametrize("model", ["gpt-6.1-terra", "gpt-6-sol", "gpt-6.1-sol", "gpt-6-astra"])
def test_a_listed_endpoint_starts_with_any_gpt_6_chat_effort(tmp_path: Path, model: str, effort: str) -> None:
    assert pricing_problems(_gpt6(model, reasoning_effort=effort, **_RESPONSES), _gpt6_priced(tmp_path)) is None


@pytest.mark.parametrize("model", ["gpt-6.1-sol", "gpt-6-astra"])
def test_a_listed_endpoint_starts_with_a_model_that_has_no_off_level_for_voice(tmp_path: Path, model: str) -> None:
    voice = _gpt6("gpt-5-mini", voice_enabled=True, voice_turn_model=model, voice_reasoning_effort="high", **_RESPONSES)

    assert pricing_problems(voice, _gpt6_priced(tmp_path)) is None


def test_only_the_endpoint_still_on_chat_completions_is_refused(tmp_path: Path) -> None:
    """Chat on `openai` (Responses) is fine; voice on `gw`, which is not listed, still has the old limit."""
    mixed = _gpt6(
        "gpt-6.1-terra",
        reasoning_effort="high",
        openai_compatible_provider="gw",
        openai_compatible_base_url="http://localhost:9999/v1",
        openai_compatible_api_key="gw-key",
        openai_compatible_model="gpt-5-mini",
        voice_enabled=True,
        voice_turn_model="gw:gpt-6-sol",
        **_RESPONSES,
    )
    report = check_pricing(mixed, _gpt6_priced(tmp_path))

    assert [(c.use.role, c.use.provider) for c in report.conflicts] == [("voice", "gw")]
    assert "#358" not in report.message()
