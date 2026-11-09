"""The startup price check: every model Pincer will call has a `[pricing]` entry (issue #357),
and no gpt-6 chat or voice setting asks Chat Completions for what it can't do with tools.

Run by the entry points that serve traffic (`pincer run`, its daemon, `pincer chat`, the
standalone API) and by `pincer doctor`; never on settings construction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from pincer.config.llm import NO_SETTING, call_effort, effective_effort, effective_thinking
from pincer.config.pricing import PricingError, load_price_table, paste_block
from pincer.llm.model_params import ReasoningWire, openai_tools_need_off, openai_tools_unusable, resolve_reasoning
from pincer.llm.router import (
    has_credentials,
    is_free_provider,
    is_openai_wire,
    known_provider_names,
    plan_voice_turn,
    provider_default_model,
    summary_target,
    wire_safe_model,
)

if TYPE_CHECKING:
    from pathlib import Path

    from pincer.config import Settings
    from pincer.config.llm import ReasoningEffort, ThinkingMode

# Why a gpt-6 setting is refused, and when that changes: plain words for an operator (owner, 2026-11-09)
GPT6_TOOLS_EXPLANATION = (
    "Why: Pincer talks to OpenAI models through Chat Completions, which lets gpt-6 models use tools only "
    "with reasoning off. The summary model uses no tools, so PINCER_SUMMARY_REASONING_EFFORT still applies to it.\n"
    "When it changes: reasoning with tools comes with the Responses API (#358)."
)
# gpt-6 models that take tools with reasoning off, suggested for a model that can't (model pages, 2026-11-09)
_GPT6_TOOL_MODELS = "gpt-6.1-terra, gpt-6-sol, gpt-6-luna or a gpt-5 model"


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
    return [use for use in _model_uses(settings) if not is_free_provider(settings, use.provider)]


def _model_uses(settings: Settings) -> list[ModelUse]:
    """`models_to_price` with free providers kept: every model Pincer will call."""
    uses: list[ModelUse] = []
    for name in sorted(known_provider_names(settings)):
        model = wire_safe_model(settings, name, provider_default_model(settings, name))
        if has_credentials(settings, name) and model:
            uses.append(ModelUse(name, model, "chat"))
    name, model = summary_target(settings)
    if has_credentials(settings, name):
        uses.append(
            ModelUse(name, wire_safe_model(settings, name, model) or provider_default_model(settings, name), "summary")
        )
    if settings.voice_enabled or settings.voice_outbound_enabled:
        plan = plan_voice_turn(settings)  # builds client objects only: nothing is sent
        if has_credentials(settings, plan.provider):
            uses.append(ModelUse(plan.provider, plan.effective_model, "voice"))
    return uses


@dataclass(frozen=True)
class EffortConflict:
    """A chat or voice model whose tool requests Chat Completions would reject (ruling Q13)."""

    use: ModelUse
    problem: str  # what is wrong, naming the setting or the model
    fix: str  # what the operator does about it

    def line(self) -> str:
        return f"{self.use.role} on {self.use.provider}, {self.use.model}: {self.problem}"


def effort_conflicts(settings: Settings) -> list[EffortConflict]:
    """Every gpt-6 chat or voice use whose requests carry tools at a reasoning effort other
    than none. Judged on what would be sent (an effort replaced by thinking=disabled is
    fine), on every provider with credentials, free ones included: the 400 comes from the
    model, not the bill. The summary model sends no tools and is never a conflict."""
    conflicts: list[EffortConflict] = []
    for use in _model_uses(settings):
        if use.role == "summary" or not is_openai_wire(settings, use.provider):
            continue
        if not openai_tools_need_off(use.model):
            continue
        if openai_tools_unusable(use.model):
            conflicts.append(
                EffortConflict(
                    use,
                    "this model can't use tools at all, because its reasoning can't be turned off.",
                    f"use {_GPT6_TOOL_MODELS} for {use.role}; {use.model} is fine as the summary model.",
                )
            )
            continue
        effort, thinking, problem, fix = _use_reasoning(settings, use.role)
        params = resolve_reasoning(ReasoningWire.OPENAI, use.model, effort, thinking, with_tools=True)
        if params.fragment.get("reasoning_effort") != "none":
            conflicts.append(EffortConflict(use, problem, fix))
    return conflicts


def _use_reasoning(settings: Settings, role: str) -> tuple[ReasoningEffort, ThinkingMode, str, str]:
    """The effort and thinking a chat or voice request carries, what sets the effort, and how to clear it.
    "none" is not a value these settings accept, so the fix is always to unset (or `default`)."""
    effort = settings.reasoning_effort
    then = "Pincer then turns reasoning off for this model's tool requests."
    if role != "voice":
        return (
            effort,
            settings.thinking,
            f"PINCER_REASONING_EFFORT is {effort}.",
            f"unset PINCER_REASONING_EFFORT; {then}",
        )
    thinking = effective_thinking(settings.voice_thinking, settings.thinking)
    if call_effort(settings.voice_reasoning_effort) is None:  # "" inherits the chat setting
        return (
            effort,
            thinking,
            f"PINCER_REASONING_EFFORT is {effort}, and voice inherits it.",
            f"set PINCER_VOICE_REASONING_EFFORT={NO_SETTING} (or unset PINCER_REASONING_EFFORT); {then}",
        )
    voice = effective_effort(settings.voice_reasoning_effort, effort)
    return voice, thinking, f"PINCER_VOICE_REASONING_EFFORT is {voice}.", f"unset PINCER_VOICE_REASONING_EFFORT; {then}"


@dataclass(frozen=True)
class PricingReport:
    """What the price check found: invalid entries (`errors`), or the models with no entry
    and the gpt-6 settings Chat Completions would reject (`conflicts`)."""

    errors: list[str]
    missing: dict[str, list[ModelUse]]
    models: list[str]  # every model checked, once, in the order Pincer uses them
    # `[pricing.provider."x"]` names matching no configured provider: their multiplier applies
    # to nothing. Worth a doctor warning, never a reason to refuse.
    unknown_providers: list[str] = field(default_factory=list)
    conflicts: list[EffortConflict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors and not self.missing and not self.conflicts

    def message(self) -> str:
        """The refusal: every problem, and for missing models the block to paste."""
        if self.errors:
            return str(PricingError(self.errors))
        parts = [self.conflicts_message()] if self.conflicts else []
        if self.missing:
            count = f"{len(self.missing)} model{'s' if len(self.missing) > 1 else ''}"
            listed = "\n".join(f"  - {model}: {_uses(uses)}" for model, uses in self.missing.items())
            parts.append(
                f"No price for {count} Pincer will call:\n{listed}\n"
                "Prices are matched by the exact model name. Add the block below to pincer.local.toml "
                "(or pincer.toml) in the directory Pincer runs from, fill in the rates, and start again:\n\n"
                + paste_block(self.missing)
            )
        return "\n\n".join(parts)

    def conflicts_message(self) -> str:
        """The gpt-6 part of the refusal: what and how to fix it per conflict, then why and when, once."""
        count = f"{len(self.conflicts)} gpt-6 setting{'s' if len(self.conflicts) > 1 else ''}"
        listed = "\n".join(f"  - {c.line()}\n    Fix: {c.fix}" for c in self.conflicts)
        return f"{count} won't work with tools:\n{listed}\n{GPT6_TOOLS_EXPLANATION}"


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
    return PricingReport(
        [], missing, list(dict.fromkeys(u.model for u in uses)), unknown, conflicts=effort_conflicts(settings)
    )


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
