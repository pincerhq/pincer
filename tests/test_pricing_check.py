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
    "When it changes: reasoning with tools comes with the Responses API (#358)."
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


def test_the_sampling_switch_alone_decides_whatever_the_server_switch_and_env_say(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pure read of the toml (lead ruling R): neither `[mcp.server] enabled` nor the env hides the model."""
    monkeypatch.setenv("PINCER_MCP_SERVER_EXPORT_ENABLED", "false")
    server_off = _SAMPLING_ON.replace("[mcp.server]\nenabled = true", "[mcp.server]\nenabled = false")
    assert server_off != _SAMPLING_ON
    for name, toml in (("server-on", _SAMPLING_ON), ("server-off", server_off)):
        (tmp_path / name).mkdir()
        assert _first_line(
            pricing_problems(_settings(), _with_sampling(_priced(tmp_path / name, "claude-a"), toml))
        ) == ("No price for 1 model Pincer will call:\n  - claude-s: sampling on anthropic"), name


def test_the_price_check_does_not_load_the_working_directorys_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

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


def test_a_sampling_model_is_not_checked_without_an_anthropic_key(tmp_path: Path) -> None:
    settings = _settings(anthropic_api_key="", openai_api_key="sk-test", default_provider="openai", openai_model="m")
    assert pricing_problems(settings, _with_sampling(_priced(tmp_path, "claude-a", "m"))) is None
