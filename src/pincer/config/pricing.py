"""LLM prices, from the `[pricing."<model>"]` section of pincer.toml / pincer.local.toml.

Prices live in configuration only (issue #357): the repository's pincer.toml ships
list prices, and an entry in pincer.local.toml replaces the shipped entry for that
model as a whole. A model resolves to its rates by the exact name Pincer sends.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pincer.config.toml_loader import read_toml_raw

if TYPE_CHECKING:
    from collections.abc import Iterable

#: The rates every entry must carry, USD per million tokens.
RATE_FIELDS = ("input", "cache_write", "cache_write_1h", "cache_read", "output")
_OPTIONAL_FIELDS = ("source",)
_CONFIG_FILES = ("pincer.toml", "pincer.local.toml")
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

    def rates(self, model: str) -> Rates | None:
        return self.entries.get(model)

    def __contains__(self, model: object) -> bool:
        return model in self.entries

    def cost(self, model: str, input_tokens: int, output_tokens: int) -> float:
        rates = self.entries.get(model)
        if rates is None:
            raise UnpricedModelError(model)
        return (input_tokens * rates.input + output_tokens * rates.output) / _PER_TOKEN


def _config_dir() -> Path:
    return Path.cwd()


def _merged_section(config_dir: Path) -> dict[str, Any]:
    """The `[pricing]` tables of both files; a pincer.local.toml entry replaces the shipped one whole."""
    section: dict[str, Any] = {}
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
        section.update(pricing)
    if errors:
        raise PricingError(errors)
    return section


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
    errors: list[str] = []
    missing = [f for f in RATE_FIELDS if f not in entry]
    if missing:
        errors.append(f"{where}: missing {', '.join(missing)}")
    errors += [
        f"{where}: {f} must be a number >= 0, got {entry[f]!r}"
        for f in RATE_FIELDS
        if f in entry and not _is_rate(entry[f])
    ]
    errors += [f"{where}: unknown field {key!r}" for key in entry if key not in RATE_FIELDS + _OPTIONAL_FIELDS]
    if "source" in entry and not isinstance(entry["source"], str):
        errors.append(f"{where}: source must be text")
    return errors


def load_price_table(config_dir: Path | None = None) -> PriceTable:
    """The merged `[pricing]` section of pincer.toml and pincer.local.toml in `config_dir` (default: cwd).

    Raises `PricingError` listing every invalid entry; a valid table holds every entry.
    """
    section = _merged_section(config_dir or _config_dir())
    errors = [error for model, entry in section.items() for error in _entry_errors(model, entry)]
    if errors:
        raise PricingError(errors)
    return PriceTable(
        {
            model: Rates(**{f: float(entry[f]) for f in RATE_FIELDS}, source=entry.get("source", ""))
            for model, entry in section.items()
        }
    )


def paste_block(models: Iterable[str]) -> str:
    """A `[pricing]` entry per model to paste and fill in. The rates are `nan` on purpose: an
    entry pasted without filling them in is still refused, naming the field, never priced at 0."""
    return "\n".join(f'[pricing."{model}"]\n' + "\n".join(_PASTE_LINES) + "\n" for model in models)
