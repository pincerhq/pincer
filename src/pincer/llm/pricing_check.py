"""The startup price check: every model Pincer will call has a `[pricing]` entry (issue #357).

Run by the entry points that serve traffic (`pincer run`, its daemon, `pincer chat`, the
standalone API) and by `pincer doctor`; never on settings construction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from pincer.config.pricing import PricingError, load_price_table, paste_block
from pincer.llm.router import (
    has_credentials,
    is_free_provider,
    known_provider_names,
    plan_voice_turn,
    provider_default_model,
    summary_target,
    wire_safe_model,
)

if TYPE_CHECKING:
    from pathlib import Path

    from pincer.config import Settings


@dataclass(frozen=True)
class ModelUse:
    """A model Pincer will call: which provider serves it, and what for."""

    provider: str
    model: str
    role: str  # "chat" | "summary" | "voice"


def models_to_price(settings: Settings) -> list[ModelUse]:
    """Every model Pincer will call that costs money: the chat model of every provider with
    credentials, the summary model, and the voice turn model when voice is on. A free
    provider (a keyless compatible endpoint) is left out, and so is a claude-* id an
    OpenAI-wire provider would never send. `daily_budget_usd` plays no part."""

    def billed(name: str) -> bool:
        return has_credentials(settings, name) and not is_free_provider(settings, name)

    uses: list[ModelUse] = []
    for name in sorted(known_provider_names(settings)):
        model = wire_safe_model(settings, name, provider_default_model(settings, name))
        if billed(name) and model:
            uses.append(ModelUse(name, model, "chat"))
    name, model = summary_target(settings)
    if billed(name):
        uses.append(
            ModelUse(name, wire_safe_model(settings, name, model) or provider_default_model(settings, name), "summary")
        )
    if settings.voice_enabled or settings.voice_outbound_enabled:
        plan = plan_voice_turn(settings)  # builds client objects only: nothing is sent
        if billed(plan.provider):
            uses.append(ModelUse(plan.provider, plan.effective_model, "voice"))
    return uses


@dataclass(frozen=True)
class PricingReport:
    """What the price check found: invalid entries (`errors`), or the models with no entry."""

    errors: list[str]
    missing: dict[str, list[ModelUse]]
    models: list[str]  # every model checked, once, in the order Pincer uses them
    # `[pricing.provider."x"]` names matching no configured provider: their multiplier applies
    # to nothing. Worth a doctor warning, never a reason to refuse.
    unknown_providers: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors and not self.missing

    def message(self) -> str:
        """The refusal: every problem, and for missing models the block to paste."""
        if self.errors:
            return str(PricingError(self.errors))
        count = f"{len(self.missing)} model{'s' if len(self.missing) > 1 else ''}"
        listed = "\n".join(f"  - {model}: {_uses(uses)}" for model, uses in self.missing.items())
        return (
            f"No price for {count} Pincer will call:\n{listed}\n"
            "Prices are matched by the exact model name. Add the block below to pincer.local.toml "
            "(or pincer.toml) in the directory Pincer runs from, fill in the rates, and start again:\n\n"
            + paste_block(self.missing)
        )


def _uses(uses: list[ModelUse]) -> str:
    return ", ".join(f"{u.role} on {u.provider}" for u in uses)


def check_pricing(settings: Settings, config_dir: Path | None = None) -> PricingReport:
    """Load the `[pricing]` section of `config_dir` (default: cwd) and check every model in
    `models_to_price` against it."""
    try:
        table = load_price_table(config_dir)
    except PricingError as e:
        return PricingReport(e.errors, {}, [])
    uses = models_to_price(settings)
    missing: dict[str, list[ModelUse]] = {}
    for use in uses:
        if use.model not in table:
            missing.setdefault(use.model, []).append(use)
    unknown = sorted(set(table.multipliers) - known_provider_names(settings))
    return PricingReport([], missing, list(dict.fromkeys(u.model for u in uses)), unknown)


def pricing_problems(settings: Settings, config_dir: Path | None = None) -> str | None:
    """None when the `[pricing]` section is valid and prices every model in `models_to_price`;
    otherwise the message to refuse with."""
    report = check_pricing(settings, config_dir)
    return None if report.ok else report.message()


def voice_turn_unusable(settings: Settings, value: str, config_dir: Path | None = None) -> str | None:
    """Why `value` (a voice turn model, as `voice_turn_model`) can't be switched to at runtime;
    None when it can. It can't when its provider can't be built, or when the model it would
    call has no `[pricing]` entry: a runtime switch must not bypass the startup check."""
    plan = plan_voice_turn(settings, value)  # builds client objects only: nothing is sent
    if plan.unusable is not None:
        return plan.unusable
    if not has_credentials(settings, plan.provider) or is_free_provider(settings, plan.provider):
        return None
    try:
        table = load_price_table(config_dir)
    except PricingError as e:
        return str(e)
    if plan.effective_model in table:
        return None
    return (
        f"no [pricing] entry for model {plan.effective_model!r}; "
        f'add [pricing."{plan.effective_model}"] to pincer.local.toml'
    )
