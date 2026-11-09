"""Prices come from the `[pricing]` section of pincer.toml / pincer.local.toml (issue #357)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from pincer.config.pricing import PriceTable, PricingError, Rates, UnpricedModelError, load_price_table

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
    assert shipped.cost("claude-opus-4-6", 1_000_000, 1_000_000) == pytest.approx(30.0)


def test_tests_read_their_own_pricing_never_the_repository_config() -> None:
    table = load_price_table()
    fixture = table.rates("test-fixture-model")
    assert fixture is not None
    assert fixture.input == 7.0
    assert table.rates("claude-opus-4-6") is None  # shipped in pincer.toml, not in the fixture


_CLAUDE_SOURCE = "https://platform.claude.com/docs/en/about-claude/pricing, fetched 2026-11-07"
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
    "claude-haiku-5-5": ((0.10, 0.125, 0.20, 0.01, 0.50), _CLAUDE_SOURCE),
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
        shipped.cost("claude-opus-4-5-latest", 10, 10)
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
