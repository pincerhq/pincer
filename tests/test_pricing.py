"""Prices come from the `[pricing]` section of pincer.toml / pincer.local.toml (issue #357)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from pincer.config.pricing import (
    PriceTable,
    PricingError,
    Rates,
    TokenUsage,
    UnpricedModelError,
    load_price_table,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def shipped(tmp_path: Path) -> PriceTable:
    """The repository's pincer.toml alone: never the developer's pincer.local.toml."""
    shutil.copy(REPO_ROOT / "pincer.toml", tmp_path / "pincer.toml")
    return load_price_table(tmp_path)


def test_the_shipped_section_prices_claude_opus_4_6_and_haiku_5_5(shipped: PriceTable) -> None:
    opus = shipped.rates("claude-opus-4-6")
    haiku = shipped.rates("claude-haiku-5-5")
    assert opus is not None
    assert haiku is not None
    assert (opus.input, opus.output) == (5.0, 25.0)
    assert (haiku.input, haiku.output) == (0.10, 0.50)
    assert shipped.cost(
        "anthropic", "claude-opus-4-6", TokenUsage(input_tokens=1_000_000, output_tokens=1_000_000)
    ) == pytest.approx(30.0)


def test_tests_read_their_own_pricing_never_the_repository_config() -> None:
    table = load_price_table()
    fixture = table.rates("test-fixture-model")
    assert fixture is not None
    assert fixture.input == 7.0
    assert table.rates("claude-opus-4-6") is None  # shipped in pincer.toml, not in the fixture


_CLAUDE_SOURCE = "https://platform.claude.com/docs/en/about-claude/pricing, fetched 2026-11-07"
# Rows given their long-prompt tier in slice 2 were re-read on 2026-11-09
_CLAUDE_SOURCE_LONG = "https://platform.claude.com/docs/en/about-claude/pricing, fetched 2026-11-09"
_OPENAI_SOURCE = "https://developers.openai.com/api/docs/pricing (Standard), fetched 2026-11-09"

# input / 5-minute write / 1-hour write / cache read / output, USD per million tokens. Claude: the
# slice's table from Anthropic's pricing page. OpenAI: input / write 30m / write 12h / cached / output
# from OpenAI's pricing page, with writes = input where OpenAI charges none.
_SHIPPED: dict[str, tuple[tuple[float, float, float, float, float], str]] = {
    **dict.fromkeys(["claude-fable-5-1", "claude-mythos-5-1"], ((10, 12.5, 20, 0.25, 50), _CLAUDE_SOURCE)),
    **dict.fromkeys(["claude-fable-5", "claude-mythos-5"], ((10, 12.5, 20, 1, 50), _CLAUDE_SOURCE)),
    "claude-opus-5-5": ((4, 5, 8, 0.20, 20), _CLAUDE_SOURCE),
    **dict.fromkeys(
        [
            "claude-opus-5",
            "claude-opus-4-8",
            "claude-opus-4-7",
            "claude-opus-4-6",
            "claude-opus-4-5",
            "claude-opus-4-5-20251101",
        ],
        ((5, 6.25, 10, 0.50, 25), _CLAUDE_SOURCE),
    ),
    "claude-sonnet-5-5": ((2, 2.5, 4, 0.10, 10), _CLAUDE_SOURCE),
    "claude-sonnet-5": ((2, 2.5, 4, 0.20, 10), _CLAUDE_SOURCE),
    **dict.fromkeys(
        ["claude-sonnet-4-6", "claude-sonnet-4-5", "claude-sonnet-4-5-20250929"],
        ((3, 3.75, 6, 0.30, 15), _CLAUDE_SOURCE),
    ),
    "claude-haiku-5-5": ((0.10, 0.125, 0.20, 0.01, 0.50), _CLAUDE_SOURCE_LONG),
    **dict.fromkeys(["claude-haiku-4-5", "claude-haiku-4-5-20251001"], ((1, 1.25, 2, 0.10, 5), _CLAUDE_SOURCE)),
    "gpt-6-astra": ((10, 12.5, 25, 1, 50), _OPENAI_SOURCE),
    "gpt-6.1-sol": ((2, 2.5, 5, 0.10, 10), _OPENAI_SOURCE),
    "gpt-6-sol": ((2, 2.5, 5, 0.20, 10), _OPENAI_SOURCE),
    "gpt-6.1-terra": ((1, 1.25, 2.5, 0.05, 5), _OPENAI_SOURCE),
    "gpt-6-luna": ((0.10, 0.125, 0.25, 0.01, 0.50), _OPENAI_SOURCE),
    "gpt-5.4": ((2.5, 2.5, 2.5, 0.25, 15), _OPENAI_SOURCE),
    "gpt-5.4-mini": ((0.75, 0.75, 0.75, 0.075, 4.5), _OPENAI_SOURCE),
    "gpt-5.2": ((1.75, 1.75, 1.75, 0.175, 14), _OPENAI_SOURCE),
    "gpt-5.1": ((1.25, 1.25, 1.25, 0.125, 10), _OPENAI_SOURCE),
    "gpt-5": ((1.25, 1.25, 1.25, 0.125, 10), _OPENAI_SOURCE),
    "gpt-5-mini": ((0.25, 0.25, 0.25, 0.025, 2), _OPENAI_SOURCE),
    "gpt-5-nano": ((0.05, 0.05, 0.05, 0.005, 0.40), _OPENAI_SOURCE),
    "gpt-4.1": ((2, 2, 2, 0.50, 8), _OPENAI_SOURCE),
    "gpt-4.1-mini": ((0.40, 0.40, 0.40, 0.10, 1.60), _OPENAI_SOURCE),
    "gpt-4o": ((2.5, 2.5, 2.5, 1.25, 10), _OPENAI_SOURCE),
    "gpt-4o-mini": ((0.15, 0.15, 0.15, 0.075, 0.60), _OPENAI_SOURCE),
    "o3": ((2, 2, 2, 0.50, 8), _OPENAI_SOURCE),
    "o3-pro": ((20, 20, 20, 20, 80), _OPENAI_SOURCE),  # no cached-input price listed
}


# The long-prompt tier (Q6/Q17): threshold in prompt tokens, then the rates in the order above.
# Claude: Haiku 5.5 only ("for prompts over 100,000 tokens"). OpenAI: "Long context: >272K input
# tokens" on every shipped model the page gives a long column; gpt-5.4 charges no writes.
_SHIPPED_LONG: dict[str, tuple[int, tuple[float, float, float, float, float]]] = {
    "claude-haiku-5-5": (100_000, (0.50, 0.625, 1, 0.05, 2.50)),
    "gpt-6-astra": (272_000, (20, 25, 50, 2, 75)),
    "gpt-6.1-sol": (272_000, (4, 5, 10, 0.20, 15)),
    "gpt-6-sol": (272_000, (4, 5, 10, 0.40, 15)),
    "gpt-6.1-terra": (272_000, (2, 2.5, 5, 0.10, 7.50)),
    "gpt-6-luna": (272_000, (0.20, 0.25, 0.50, 0.02, 0.75)),
    "gpt-5.4": (272_000, (5, 5, 5, 0.50, 22.50)),
}


def test_every_shipped_long_prompt_tier_resolves_to_its_own_rates(shipped: PriceTable) -> None:
    tiers = {model: rates.long_context for model, rates in shipped.entries.items() if rates.long_context}
    assert sorted(tiers) == sorted(_SHIPPED_LONG)
    for model, (above, rates) in _SHIPPED_LONG.items():
        long = tiers[model]
        got = long.rates
        assert long.above == above, model
        assert (got.input, got.cache_write, got.cache_write_1h, got.cache_read, got.output) == pytest.approx(rates), (
            model
        )


def test_every_shipped_model_resolves_to_its_own_rates(shipped: PriceTable) -> None:
    assert sorted(shipped.entries) == sorted(_SHIPPED)
    for model, (rates, source) in _SHIPPED.items():
        got = shipped.rates(model)
        assert got is not None, model
        assert (got.input, got.cache_write, got.cache_write_1h, got.cache_read, got.output) == pytest.approx(rates), (
            model
        )
        assert got.source == source, model


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


_FULL = "input = 1.0\ncache_write = 1.25\ncache_write_1h = 2.0\ncache_read = 0.1\noutput = 5.0\n"


def test_a_local_entry_replaces_the_shipped_entry_whole(tmp_path: Path) -> None:
    _write(tmp_path / "pincer.toml", f'[pricing."m-1"]\n{_FULL}source = "shipped page"\n\n[pricing."m-2"]\n{_FULL}')
    _write(
        tmp_path / "pincer.local.toml",
        '[pricing."m-1"]\ninput = 9.0\ncache_write = 9.0\ncache_write_1h = 9.0\ncache_read = 9.0\noutput = 9.0\n',
    )
    table = load_price_table(tmp_path)
    assert table.rates("m-1") == Rates(input=9.0, cache_write=9.0, cache_write_1h=9.0, cache_read=9.0, output=9.0)
    assert table.rates("m-1").source == ""  # not inherited from the shipped entry
    assert table.rates("m-2") == Rates(input=1.0, cache_write=1.25, cache_write_1h=2.0, cache_read=0.1, output=5.0)


def test_resolution_is_by_exact_name_only(shipped: PriceTable) -> None:
    assert shipped.rates("claude-opus-4-5") is not None
    assert shipped.rates("claude-opus-4") is None  # never the price of claude-opus-4-5, or of any other opus
    for spelling in ("claude-opus-4-5-latest", "Claude-Opus-4-5", "claude-opus-4.5", " claude-opus-4-5", "opus"):
        assert shipped.rates(spelling) is None, spelling
        assert spelling not in shipped
    with pytest.raises(UnpricedModelError) as raised:
        shipped.cost("anthropic", "claude-opus-4-5-latest", TokenUsage(input_tokens=10, output_tokens=10))
    assert raised.value.model == "claude-opus-4-5-latest"


@pytest.mark.parametrize(
    ("entry", "error"),
    [
        (
            "input = 1.0\ncache_write = 1.0\ncache_write_1h = 1.0\noutput = 1.0\n",
            'pricing."m": missing cache_read',
        ),
        ("input = 1.0\noutput = 1.0\n", 'pricing."m": missing cache_write, cache_write_1h, cache_read'),
        (_FULL.replace("input = 1.0", 'input = "1.0"'), "pricing.\"m\": input must be a number >= 0, got '1.0'"),
        (_FULL.replace("output = 5.0", "output = -5.0"), 'pricing."m": output must be a number >= 0, got -5.0'),
        (
            _FULL.replace("cache_read = 0.1", "cache_read = true"),
            'pricing."m": cache_read must be a number >= 0, got True',
        ),
        (_FULL.replace("input = 1.0", "input = nan"), 'pricing."m": input must be a number >= 0, got nan'),
        (_FULL.replace("input = 1.0", "input = inf"), 'pricing."m": input must be a number >= 0, got inf'),
        (_FULL + "cache_reed = 0.1\n", "pricing.\"m\": unknown field 'cache_reed'"),
        (_FULL + "source = 3\n", 'pricing."m": source must be text'),
    ],
)
def test_an_invalid_entry_is_refused_naming_the_field(tmp_path: Path, entry: str, error: str) -> None:
    _write(tmp_path / "pincer.toml", f'[pricing."m"]\n{entry}')
    with pytest.raises(PricingError) as raised:
        load_price_table(tmp_path)
    assert raised.value.errors == [error]


def test_an_unquoted_model_name_with_dots_is_explained(tmp_path: Path) -> None:
    _write(tmp_path / "pincer.toml", f"[pricing.gpt-5.4-mini]\n{_FULL}")
    with pytest.raises(PricingError) as raised:
        load_price_table(tmp_path)
    assert raised.value.errors == [
        'pricing.gpt-5.4-mini: the model name was split at its dots; quote it: [pricing."gpt-5.4-mini"]'
    ]


def test_an_entry_that_is_not_a_table_is_refused(tmp_path: Path) -> None:
    _write(tmp_path / "pincer.toml", '[pricing]\n"m" = 3.0\n')
    with pytest.raises(PricingError) as raised:
        load_price_table(tmp_path)
    assert raised.value.errors == ['pricing."m": must be a table of rates']


def test_every_invalid_entry_is_reported_at_once_in_both_files(tmp_path: Path) -> None:
    _write(tmp_path / "pincer.toml", f'[pricing."ok"]\n{_FULL}\n[pricing."a"]\ninput = 1.0\n')
    _write(tmp_path / "pincer.local.toml", f'[pricing."b"]\n{_FULL.replace("output = 5.0", "output = -1")}')
    with pytest.raises(PricingError) as raised:
        load_price_table(tmp_path)
    assert raised.value.errors == [
        'pricing."a": missing cache_write, cache_write_1h, cache_read, output',
        'pricing."b": output must be a number >= 0, got -1',
    ]
    assert str(raised.value) == (
        "Invalid [pricing] entries in pincer.toml / pincer.local.toml:\n"
        '  - pricing."a": missing cache_write, cache_write_1h, cache_read, output\n'
        '  - pricing."b": output must be a number >= 0, got -1'
    )


def test_a_pricing_file_that_is_not_toml_is_refused(tmp_path: Path) -> None:
    _write(tmp_path / "pincer.local.toml", '[pricing."m"\ninput = 1\n')
    with pytest.raises(PricingError) as raised:
        load_price_table(tmp_path)
    assert len(raised.value.errors) == 1
    assert raised.value.errors[0].startswith("pincer.local.toml: ")


# Every model the repository's default config and docs tell an owner to run, and where. A model
# missing from the shipped section would make an upgraded install refuse to start. Not listed:
# CHANGELOG history, the effort-rule examples in .env.example and model_params (o1, o1-pro, o4-mini,
# "gpt-5.x-pro": families, not models OpenAI still prices), and retired Claude models (none are named
# any more outside the MCP sampling default, which is never metered).
_NAMED_IN_DEFAULTS_AND_DOCS = {
    "claude-haiku-4-5-20251001": "Settings.default_model / summary_model, .env.example, cli/init.py, api/voice.py",
    "claude-haiku-4-5": ".env.example, docs/voice-latency-log.md",
    "claude-haiku-5-5": ".env.example, scripts/smoke_llm.py, docs/core-components/cli.md",
    "claude-sonnet-5-5": "scripts/smoke_llm.py, README.md, docs/",
    "claude-sonnet-4-5-20250929": "README.md, docs/core-components/cli.md",
    "claude-sonnet-4-5": "docs/voice-latency-log.md",
    "gpt-4o": "README.md, cli/init.py, docs/core-components/cli.md",
    "gpt-4o-mini": "api/voice.py turn-model choices, docs/core-components/cli.md",
    "gpt-5-mini": "config/channels.py, api/voice.py turn-model choices, .env.example",
    "gpt-5.4-mini": ".env.example, scripts/smoke_llm.py",
    "gpt-5.1": ".env.example, scripts/smoke_llm.py",
    "gpt-5.2": ".env.example, scripts/smoke_llm.py",
}


@pytest.mark.parametrize("model", sorted(_NAMED_IN_DEFAULTS_AND_DOCS))
def test_every_model_named_in_defaults_and_docs_is_shipped(shipped: PriceTable, model: str) -> None:
    assert model in shipped, _NAMED_IN_DEFAULTS_AND_DOCS[model]


def test_the_default_settings_models_are_shipped(shipped: PriceTable) -> None:
    from pincer.config.llm import LLMSettings
    from pincer.config.tools import ToolSettings

    assert LLMSettings.model_fields["default_model"].default in shipped
    assert ToolSettings.model_fields["summary_model"].default in shipped


def test_a_pricing_key_that_is_not_a_table_is_refused(tmp_path: Path) -> None:
    _write(tmp_path / "pincer.toml", "pricing = 3\n")
    with pytest.raises(PricingError) as raised:
        load_price_table(tmp_path)
    assert raised.value.errors == ["pincer.toml: [pricing] must be a table of model entries"]


# ── Slice 2: long-prompt rates and the provider multiplier ─────────────────────


@pytest.mark.parametrize(
    ("prompt_tokens", "expected"),
    [(100_000, 0.0105), (100_001, 0.0525005)],  # base 0.10/0.50 at the threshold, long 0.50/2.50 above it
)
def test_haiku_5_5_switches_to_its_long_rates_only_above_100k(
    shipped: PriceTable, prompt_tokens: int, expected: float
) -> None:
    assert shipped.cost(
        "anthropic", "claude-haiku-5-5", TokenUsage(input_tokens=prompt_tokens, output_tokens=1_000)
    ) == pytest.approx(expected, abs=1e-12)


@pytest.mark.parametrize(
    ("prompt_tokens", "expected"),
    [(272_000, 0.277), (272_001, 0.551502)],  # base 1/5 at the threshold, long 2/7.50 above it
)
def test_gpt_6_1_terra_switches_to_its_long_rates_only_above_272k(
    shipped: PriceTable, prompt_tokens: int, expected: float
) -> None:
    assert shipped.cost(
        "azure", "gpt-6.1-terra", TokenUsage(input_tokens=prompt_tokens, output_tokens=1_000)
    ) == pytest.approx(expected, abs=1e-12)


def _with_providers(tmp_path: Path, providers: str) -> PriceTable:
    shutil.copy(REPO_ROOT / "pincer.toml", tmp_path / "pincer.toml")
    _write(tmp_path / "pincer.local.toml", providers)
    return load_price_table(tmp_path)


@pytest.mark.parametrize(
    ("multiplier", "prompt_tokens", "expected"),
    [
        (1.1, 100_001, 0.0525005 * 1.1),  # Data Zone: the long tier scales too
        (0.8, 100_000, 0.0105 * 0.8),  # a negotiated discount, base tier
        (0.8, 100_001, 0.0525005 * 0.8),
    ],
)
def test_a_provider_multiplier_scales_whichever_tier_applies(
    tmp_path: Path, multiplier: float, prompt_tokens: int, expected: float
) -> None:
    table = _with_providers(tmp_path, f'[pricing.provider."foundry"]\nmultiplier = {multiplier}\n')
    assert table.cost(
        "foundry", "claude-haiku-5-5", TokenUsage(input_tokens=prompt_tokens, output_tokens=1_000)
    ) == pytest.approx(expected, abs=1e-12)
    assert table.cost(
        "anthropic", "claude-haiku-5-5", TokenUsage(input_tokens=prompt_tokens, output_tokens=1_000)
    ) == pytest.approx(expected / multiplier, abs=1e-12)


def test_a_local_provider_table_replaces_only_that_provider(tmp_path: Path) -> None:
    _write(
        tmp_path / "pincer.toml",
        f'[pricing."m"]\n{_FULL}\n[pricing.provider."zone"]\nmultiplier = 1.1\n\n'
        '[pricing.provider."deal"]\nmultiplier = 0.9\n',
    )
    _write(tmp_path / "pincer.local.toml", '[pricing.provider."zone"]\nmultiplier = 1.2\n')
    table = load_price_table(tmp_path)
    assert table.multipliers == {"zone": 1.2, "deal": 0.9}
    assert "provider" not in table  # the provider section is never a model entry


@pytest.mark.parametrize(
    ("provider", "error"),
    [
        ("multiplier = 0\n", 'pricing.provider."azure": multiplier must be a number > 0, got 0'),
        ("multiplier = -1.1\n", 'pricing.provider."azure": multiplier must be a number > 0, got -1.1'),
        ('multiplier = "1.1"\n', "pricing.provider.\"azure\": multiplier must be a number > 0, got '1.1'"),
        ("multiplier = true\n", 'pricing.provider."azure": multiplier must be a number > 0, got True'),
        ("multiplier = nan\n", 'pricing.provider."azure": multiplier must be a number > 0, got nan'),
        ("multiplier = inf\n", 'pricing.provider."azure": multiplier must be a number > 0, got inf'),
        ("", 'pricing.provider."azure": missing multiplier'),  # an empty table is a typo, not 1.0
        ("multiplier = 1.1\nfactor = 2\n", "pricing.provider.\"azure\": unknown field 'factor'"),
    ],
)
def test_an_invalid_provider_multiplier_is_refused_naming_the_field(tmp_path: Path, provider: str, error: str) -> None:
    _write(tmp_path / "pincer.toml", f'[pricing."m"]\n{_FULL}\n[pricing.provider."azure"]\n{provider}')
    with pytest.raises(PricingError) as raised:
        load_price_table(tmp_path)
    assert raised.value.errors == [error]


@pytest.mark.parametrize(
    ("toml", "error"),
    [
        ('[pricing.provider]\n"azure" = 1.1\n', 'pricing.provider."azure": must be a table, e.g. multiplier = 1.1'),
        ("[pricing]\nprovider = 1.1\n", "pincer.toml: pricing.provider must be a table of provider settings"),
    ],
)
def test_a_provider_section_of_the_wrong_shape_is_refused(tmp_path: Path, toml: str, error: str) -> None:
    _write(tmp_path / "pincer.toml", toml)
    with pytest.raises(PricingError) as raised:
        load_price_table(tmp_path)
    assert raised.value.errors == [error]


def test_provider_and_model_errors_are_reported_together(tmp_path: Path) -> None:
    _write(tmp_path / "pincer.toml", '[pricing."a"]\ninput = 1.0\n')
    _write(tmp_path / "pincer.local.toml", '[pricing.provider."azure"]\nmultiplier = 0\n')
    with pytest.raises(PricingError) as raised:
        load_price_table(tmp_path)
    assert raised.value.errors == [
        'pricing."a": missing cache_write, cache_write_1h, cache_read, output',
        'pricing.provider."azure": multiplier must be a number > 0, got 0',
    ]


_LONG = "input = 2.0, cache_write = 2.5, cache_write_1h = 4.0, cache_read = 0.2, output = 10.0"


@pytest.mark.parametrize(
    ("long_context", "error"),
    [
        ("3", 'pricing."m".long_context: must be a table: { above = <tokens>, input = ..., output = ... }'),
        (
            "{ above = 100, input = 2.0 }",
            'pricing."m".long_context: missing cache_write, cache_write_1h, cache_read, output',
        ),
        (f"{{ {_LONG} }}", 'pricing."m".long_context: missing above'),
        (
            f"{{ above = 0, {_LONG} }}",
            'pricing."m".long_context: above must be a whole number of tokens > 0, got 0',
        ),
        (
            f"{{ above = 1.5, {_LONG} }}",
            'pricing."m".long_context: above must be a whole number of tokens > 0, got 1.5',
        ),
        (
            f"{{ above = true, {_LONG} }}",
            'pricing."m".long_context: above must be a whole number of tokens > 0, got True',
        ),
        (
            f"{{ above = 100, {_LONG.replace('output = 10.0', 'output = -1')} }}",
            'pricing."m".long_context: output must be a number >= 0, got -1',
        ),
        (f'{{ above = 100, {_LONG}, source = "x" }}', "pricing.\"m\".long_context: unknown field 'source'"),
    ],
)
def test_an_invalid_long_context_is_refused_naming_the_field(tmp_path: Path, long_context: str, error: str) -> None:
    _write(tmp_path / "pincer.toml", f'[pricing."m"]\n{_FULL}long_context = {long_context}\n')
    with pytest.raises(PricingError) as raised:
        load_price_table(tmp_path)
    assert raised.value.errors == [error]


def test_a_one_hour_cache_write_is_priced_at_its_own_rate(shipped: PriceTable) -> None:
    """Sonnet 5.5 writes at 2.50 (5-minute) and 4 (1-hour) per MTok."""
    one_hour = TokenUsage(cache_write_1h_tokens=1_000)
    five_minute = TokenUsage(cache_write_5m_tokens=1_000)
    assert shipped.cost("anthropic", "claude-sonnet-5-5", one_hour) == pytest.approx(0.004, abs=1e-12)
    assert shipped.cost("anthropic", "claude-sonnet-5-5", five_minute) == pytest.approx(0.0025, abs=1e-12)


@pytest.mark.parametrize(
    ("usage", "expected"),
    [
        # 1,000 uncached + 99,000 read = exactly 100,000: base 0.10 / 0.01 / 0.50
        (TokenUsage(input_tokens=1_000, cache_read_tokens=99_000, output_tokens=1_000), 0.00159),
        # 1,000 uncached + 100,000 read = 101,000: long 0.50 / 0.05 / 2.50, though uncached is tiny
        (TokenUsage(input_tokens=1_000, cache_read_tokens=100_000, output_tokens=1_000), 0.008),
        # writes count too: 1,000 + 50,000 5m + 50,000 1h = 101,000 → 0.50 / 0.625 / 1 / 2.50
        (
            TokenUsage(
                input_tokens=1_000, cache_write_5m_tokens=50_000, cache_write_1h_tokens=50_000, output_tokens=1_000
            ),
            0.08425,
        ),
    ],
)
def test_the_long_context_threshold_counts_the_whole_prompt(
    shipped: PriceTable, usage: TokenUsage, expected: float
) -> None:
    assert shipped.cost("anthropic", "claude-haiku-5-5", usage) == pytest.approx(expected, abs=1e-12)


def test_a_provider_multiplier_scales_the_cache_rates_too(tmp_path: Path) -> None:
    """The benchmark oracle ($0.1093765) on a Data Zone deployment."""
    table = _with_providers(tmp_path, '[pricing.provider."foundry"]\nmultiplier = 1.1\n')
    oracle = TokenUsage(input_tokens=36, cache_write_5m_tokens=27_559, cache_read_tokens=281_470, output_tokens=1_226)
    assert table.cost("foundry", "claude-sonnet-5-5", oracle) == pytest.approx(0.12031415, abs=1e-12)
    assert table.cost("anthropic", "claude-sonnet-5-5", oracle) == pytest.approx(0.1093765, abs=1e-12)
