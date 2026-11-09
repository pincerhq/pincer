"""LLM prices, from the `[pricing."<model>"]` section of pincer.toml / pincer.local.toml.

Prices live in configuration only (issue #357): the repository's pincer.toml ships
list prices, and an entry in pincer.local.toml replaces the shipped entry for that
model as a whole. A model resolves to its rates by the exact name Pincer sends.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pincer.config.toml_loader import read_toml_raw

if TYPE_CHECKING:
    from collections.abc import Iterable

#: The rates every entry must carry, USD per million tokens.
RATE_FIELDS = ("input", "cache_write", "cache_write_1h", "cache_read", "output")
_OPTIONAL_FIELDS = ("source", "long_context")
#: `long_context`: the prompt size (tokens) above which its rates replace the entry's own
_LONG_CONTEXT_FIELDS = ("above", *RATE_FIELDS)
_CONFIG_FILES = ("pincer.toml", "pincer.local.toml")
# `[pricing.provider."<name>"]`: per-provider settings, not a model entry
_PROVIDER_KEY = "provider"
_PER_TOKEN = 1_000_000
# The entry `paste_block` writes: `nan` rates, so an unedited paste is still refused naming the field
_PASTE_LINES = (
    "input = nan           # USD per million tokens, fill in",
    "cache_write = nan     # USD per million tokens, fill in (5-minute cache write)",
    "cache_write_1h = nan  # USD per million tokens, fill in (1-hour cache write)",
    "cache_read = nan      # USD per million tokens, fill in",
    "output = nan          # USD per million tokens, fill in",
    'source = ""           # the pricing page and the date you read it',
)


@dataclass(frozen=True)
class Rates:
    """One model's prices, USD per million tokens."""

    input: float
    cache_write: float
    cache_write_1h: float
    cache_read: float
    output: float
    source: str = ""
    long_context: LongContext | None = None


@dataclass(frozen=True)
class LongContext:
    """The rates that replace an entry's own when the whole prompt is above `above` tokens."""

    above: int
    rates: Rates


class PricingError(ValueError):
    """Invalid `[pricing]` configuration; `errors` lists every problem found."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__(
            "Invalid [pricing] entries in pincer.toml / pincer.local.toml:\n" + "\n".join(f"  - {e}" for e in errors)
        )


class UnpricedModelError(LookupError):
    """A model with no `[pricing]` entry: its cost is unknown, never guessed."""

    def __init__(self, model: str) -> None:
        self.model = model
        super().__init__(f"no [pricing] entry for model {model!r}")


@dataclass(frozen=True)
class PriceTable:
    """The validated `[pricing]` entries, keyed by exact model name."""

    entries: dict[str, Rates] = field(default_factory=dict)
    # Provider name as configured (the name a call is recorded under) → factor on every rate
    multipliers: dict[str, float] = field(default_factory=dict)

    def rates(self, model: str) -> Rates | None:
        return self.entries.get(model)

    def __contains__(self, model: object) -> bool:
        return model in self.entries

    def cost(self, provider: str, model: str, input_tokens: int, output_tokens: int) -> float:
        """USD for one call served by `provider`. The prompt is `input_tokens` until the cache
        token types are priced (slice 3), which then counts writes and reads in it as well."""
        rates = self._resolve(provider, model, input_tokens)
        return (input_tokens * rates.input + output_tokens * rates.output) / _PER_TOKEN

    def _resolve(self, provider: str, model: str, prompt_tokens: int) -> Rates:
        """The rates one call is priced at: the entry's long tier when the prompt is above
        its threshold, times the multiplier of the provider that served it."""
        rates = self.entries.get(model)
        if rates is None:
            raise UnpricedModelError(model)
        long = rates.long_context
        if long is not None and prompt_tokens > long.above:
            rates = long.rates
        factor = self.multipliers.get(provider, 1.0)
        return replace(rates, **{f: getattr(rates, f) * factor for f in RATE_FIELDS})


def _config_dir() -> Path:
    return Path.cwd()


def _merged_section(config_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """The model entries and the `[pricing.provider]` tables of both files; a pincer.local.toml
    entry (model or provider) replaces the shipped one whole."""
    section: dict[str, Any] = {}
    providers: dict[str, Any] = {}
    errors: list[str] = []
    for name in _CONFIG_FILES:
        try:
            raw = read_toml_raw(config_dir / name) or {}
        except ValueError as e:  # tomllib.TOMLDecodeError
            errors.append(f"{name}: {e}")
            continue
        pricing = raw.get("pricing", {})
        if not isinstance(pricing, dict):
            errors.append(f"{name}: [pricing] must be a table of model entries")
            continue
        pricing = dict(pricing)
        provider_tables = pricing.pop(_PROVIDER_KEY, {})
        if isinstance(provider_tables, dict):
            providers.update(provider_tables)
        else:
            errors.append(f"{name}: pricing.{_PROVIDER_KEY} must be a table of provider settings")
        section.update(pricing)
    if errors:
        raise PricingError(errors)
    return section, providers


def _is_rate(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def _split_name(model: str, entry: dict[str, Any]) -> str | None:
    """`[pricing.gpt-5.4-mini]` (unquoted) parses as nested tables; the dotted name it was, if so."""
    parts = [model]
    while not any(f in entry for f in RATE_FIELDS):
        if len(entry) != 1:
            return None
        ((key, inner),) = entry.items()
        if not isinstance(inner, dict):
            return None
        parts.append(key)
        entry = inner
    return ".".join(parts) if len(parts) > 1 else None


def _entry_errors(model: str, entry: object) -> list[str]:
    where = f'pricing."{model}"'
    if not isinstance(entry, dict):
        return [f"{where}: must be a table of rates"]
    dotted = _split_name(model, entry)
    if dotted is not None:
        return [f'pricing.{dotted}: the model name was split at its dots; quote it: [pricing."{dotted}"]']
    errors = _rate_errors(where, entry, RATE_FIELDS, RATE_FIELDS + _OPTIONAL_FIELDS)
    if "source" in entry and not isinstance(entry["source"], str):
        errors.append(f"{where}: source must be text")
    if "long_context" in entry:
        errors += _long_context_errors(f"{where}.long_context", entry["long_context"])
    return errors


def _long_context_errors(where: str, long: object) -> list[str]:
    if not isinstance(long, dict):
        return [f"{where}: must be a table: {{ above = <tokens>, input = ..., output = ... }}"]
    errors = _rate_errors(where, long, _LONG_CONTEXT_FIELDS, _LONG_CONTEXT_FIELDS)
    above = long.get("above")
    if "above" in long and not (isinstance(above, int) and not isinstance(above, bool) and above > 0):
        errors.append(f"{where}: above must be a whole number of tokens > 0, got {above!r}")
    return errors


def _rate_errors(where: str, table: dict[str, Any], required: tuple[str, ...], allowed: tuple[str, ...]) -> list[str]:
    """Missing fields (all in one line), rates that are not numbers >= 0, and unknown fields."""
    errors: list[str] = []
    missing = [f for f in required if f not in table]
    if missing:
        errors.append(f"{where}: missing {', '.join(missing)}")
    errors += [
        f"{where}: {f} must be a number >= 0, got {table[f]!r}"
        for f in RATE_FIELDS
        if f in table and not _is_rate(table[f])
    ]
    errors += [f"{where}: unknown field {key!r}" for key in table if key not in allowed]
    return errors


def _provider_errors(name: str, table: object) -> list[str]:
    where = f'pricing.{_PROVIDER_KEY}."{name}"'
    if not isinstance(table, dict):
        return [f"{where}: must be a table, e.g. multiplier = 1.1"]
    if "multiplier" not in table:
        errors = [f"{where}: missing multiplier"]
    elif not (_is_rate(table["multiplier"]) and table["multiplier"] > 0):
        errors = [f"{where}: multiplier must be a number > 0, got {table['multiplier']!r}"]
    else:
        errors = []
    return errors + [f"{where}: unknown field {key!r}" for key in table if key != "multiplier"]


def load_price_table(config_dir: Path | None = None) -> PriceTable:
    """The merged `[pricing]` section of pincer.toml and pincer.local.toml in `config_dir` (default: cwd).

    Raises `PricingError` listing every invalid entry; a valid table holds every entry.
    """
    section, providers = _merged_section(config_dir or _config_dir())
    errors = [error for model, entry in section.items() for error in _entry_errors(model, entry)]
    errors += [error for name, table in providers.items() for error in _provider_errors(name, table)]
    if errors:
        raise PricingError(errors)
    return PriceTable(
        {model: _rates(entry) for model, entry in section.items()},
        {name: float(table["multiplier"]) for name, table in providers.items()},
    )


def _rates(entry: dict[str, Any]) -> Rates:
    long = entry.get("long_context")
    return Rates(
        **{f: float(entry[f]) for f in RATE_FIELDS},
        source=entry.get("source", ""),
        long_context=None if long is None else LongContext(long["above"], _rates(long)),
    )


def paste_block(models: Iterable[str]) -> str:
    """A `[pricing]` entry per model to paste and fill in. The rates are `nan` on purpose: an
    entry pasted without filling them in is still refused, naming the field, never priced at 0."""
    return "\n".join(f'[pricing."{model}"]\n' + "\n".join(_PASTE_LINES) + "\n" for model in models)
