"""Tests for the live checks of scripts/smoke_llm.py (issue #345, slice 10).

Every provider here is served by a fake transport: nothing reaches the network.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
from anthropic import AsyncAnthropic
from anthropic_sse import ScriptedAnthropic, text, thinking, tool_use
from openai import AsyncOpenAI

from pincer.config import Settings
from pincer.llm.openai_common import OpenAICompatibleProvider

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "smoke_llm.py"
_VOICE = "anthropic:claude-haiku-5-5"
_SIG = "c2lnbmF0dXJlLW9uZS10d28tdGhyZWU="
_STEP1 = ([thinking("weighing the cities", _SIG), tool_use("tu_1", "get_weather", {"city": "Kyiv"})], "tool_use")
_ANSWER = ([text("12C and light rain in Kyiv.")], "end_turn")


class NetworkUsed(AssertionError):
    pass


def _no_network(*args: Any, **kwargs: Any) -> Any:
    raise NetworkUsed("a real HTTP transport was used")


@pytest.fixture(autouse=True)
def _guard_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fakes go through MockTransport; any real transport is a bug in the test or the script."""
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", _no_network)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", _no_network)


async def test_the_network_guard_is_live():
    # control: a real transport must raise, or every "sends nothing" claim below is vacuous
    async with httpx.AsyncClient() as client:
        with pytest.raises(NetworkUsed):
            await client.get("https://smoke-guard.invalid/")  # .invalid never resolves, even unguarded


@pytest.fixture(scope="module")
def smoke() -> Any:
    spec = importlib.util.spec_from_file_location("smoke_llm_checks_under_test", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up while the script loads
    spec.loader.exec_module(module)
    return module


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "telegram_bot_token": "123456:TEST",
        "anthropic_api_key": "sk-ant-test",
        "openai_api_key": "",
        "default_provider": "anthropic",
        "default_model": "claude-haiku-5-5",
        "summary_model": "claude-haiku-5-5",
        "reasoning_effort": "",
        "thinking": "",
        "voice_turn_model": "",
    }
    return Settings(_env_file=None, **{**base, **overrides})  # type: ignore[call-arg]


_COMPLETION = {
    "id": "c",
    "object": "chat.completion",
    "created": 0,
    "model": "x",
    "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "pong"}}],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
}


class ScriptedOpenAI:
    """An OpenAI provider answering from a script: "ok" is a completion, an int that HTTP error."""

    def __init__(self, settings: Settings, name: str, model: str, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.requests: list[dict[str, Any]] = []
        field = "openai_model" if name == "openai" else "openai_compatible_model"
        self.provider = OpenAICompatibleProvider(settings.model_copy(update={field: model}), name)
        self.provider._client = AsyncOpenAI(
            api_key="sk-test", http_client=httpx.AsyncClient(transport=httpx.MockTransport(self._handle))
        )

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content))
        scripted = self._responses.pop(0)
        if isinstance(scripted, int):
            return httpx.Response(scripted, json={"error": {"message": "scripted failure", "type": "invalid"}})
        return httpx.Response(200, json=_COMPLETION)


class FakeBuild:
    """`build=` for the script: each built provider answers from its own script,
    keyed by "provider:model", and records every request body."""

    def __init__(self, scripts: dict[str, list[Any]]) -> None:
        self._scripts = {key: list(responses) for key, responses in scripts.items()}
        self.fakes: dict[str, ScriptedAnthropic | ScriptedOpenAI] = {}
        self.order: list[dict[str, Any]] = []  # every request body, across providers, in send order

    def __call__(self, settings: Settings, provider: str, model: str) -> Any:
        key = f"{provider}:{model}"
        if provider in ("openai", settings.openai_compatible_provider):
            openai_fake = ScriptedOpenAI(settings, provider, model, self._scripts.get(key, []))
            self.fakes[key] = openai_fake
            openai_fake.provider._client = AsyncOpenAI(
                api_key="sk-test", http_client=httpx.AsyncClient(transport=self._ordered(openai_fake))
            )
            return openai_fake.provider
        fake = ScriptedAnthropic(settings, self._scripts.get(key, []))
        fake.provider._client = AsyncAnthropic(
            api_key="sk-ant-test", http_client=httpx.AsyncClient(transport=self._ordered(fake))
        )
        self.fakes[key] = fake
        return fake.provider

    def _ordered(self, fake: ScriptedAnthropic | ScriptedOpenAI) -> httpx.MockTransport:
        def handle(request: httpx.Request) -> httpx.Response:
            try:
                return fake._handle(request)
            finally:
                self.order.append(fake.requests[-1])

        return httpx.MockTransport(handle)

    def requests(self, key: str = _VOICE) -> list[dict[str, Any]]:
        return self.fakes[key].requests


def _run(smoke: Any, argv: list[str], scripts: dict[str, list[Any]], **settings: Any) -> tuple[int, FakeBuild]:
    build = FakeBuild(scripts)
    code = smoke.main(["--voice-model", _VOICE, *argv], settings=_settings(**settings), build=build)
    return code, build


def test_effort_low_is_sent_and_passes(smoke, capsys):
    code, build = _run(smoke, ["--check", "effort-low"], {_VOICE: [([text("pong")], "end_turn")]})

    out = capsys.readouterr().out
    assert code == 0
    assert "PASS  effort-low" in out
    [body] = build.requests()
    assert body["output_config"] == {"effort": "low"}
    assert body["max_tokens"] == 1024
    assert body["model"] == "claude-haiku-5-5"


def test_thinking_disabled_is_sent_and_passes_without_thinking(smoke, capsys):
    code, build = _run(smoke, ["--check", "thinking-disabled"], {_VOICE: [([text("pong")], "end_turn")]})

    assert code == 0
    assert "PASS  thinking-disabled" in capsys.readouterr().out
    [body] = build.requests()
    assert body["thinking"] == {"type": "disabled"}
    assert "output_config" not in body


def test_thinking_disabled_skips_a_model_that_cannot_turn_thinking_off(smoke, capsys):
    opus = "anthropic:claude-opus-5-5"
    build = FakeBuild({})
    code = smoke.main(["--voice-model", opus, "--check", "thinking-disabled"], settings=_settings(), build=build)

    assert code == 0
    assert "SKIP  thinking-disabled: disabled not supported by claude-opus-5-5; nothing to check" in (
        capsys.readouterr().out
    )
    assert build.requests(opus) == []


def test_thinking_disabled_fails_when_thinking_comes_back(smoke, capsys):
    reply = ([thinking("hmm", _SIG), text("pong")], "end_turn")
    code, _ = _run(smoke, ["--check", "thinking-disabled"], {_VOICE: [reply]})

    out = capsys.readouterr().out
    assert code == 1
    assert "FAIL  thinking-disabled: 1 thinking block(s) returned" in out
    assert "hmm" not in out and _SIG not in out


def _assistant_blocks(body: dict[str, Any]) -> list[dict[str, Any]]:
    """The content of the request's (only) assistant message."""
    [assistant] = [m for m in body["messages"] if m["role"] == "assistant"]
    return list(assistant["content"])


def test_tool_loop_sends_the_thinking_back_unchanged(smoke, capsys):
    code, build = _run(smoke, ["--check", "tool-loop-thinking"], {_VOICE: [_STEP1, _ANSWER]})

    out = capsys.readouterr().out
    assert code == 0
    assert "PASS  tool-loop-thinking: 1 thinking block(s) returned (signed)" in out
    assert _SIG not in out and "weighing" not in out
    first, second = build.requests()
    assert "thinking" not in first and "output_config" not in first  # production's default: nothing sent
    blocks = _assistant_blocks(second)
    assert blocks[0] == {"type": "thinking", "thinking": "weighing the cities", "signature": _SIG}
    assert blocks[1]["type"] == "tool_use"


def test_tool_loop_fails_when_no_thinking_comes_back(smoke, capsys):
    no_thinking = ([tool_use("tu_1", "get_weather", {"city": "Kyiv"})], "tool_use")
    code, build = _run(smoke, ["--check", "tool-loop-thinking"], {_VOICE: [no_thinking]})

    assert code == 1
    assert "FAIL  tool-loop-thinking: no thinking returned; replay unproven" in capsys.readouterr().out
    assert len(build.requests()) == 1


def _signature(body: dict[str, Any]) -> str:
    return str(_assistant_blocks(body)[0]["signature"])


def test_tampered_signature_rejected_with_400_passes(smoke, capsys):
    code, build = _run(smoke, ["--check", "signature-tamper"], {_VOICE: [_STEP1, _ANSWER, _STEP1, 400]})

    out = capsys.readouterr().out
    assert code == 0
    assert "PASS  tool-loop-thinking" in out  # the control runs first, unasked
    assert "PASS  signature-tamper: tampered signature rejected with 400" in out
    requests = build.requests()
    assert len(requests) == 4
    sent = _signature(requests[3])
    assert len(sent) == len(_SIG)
    assert sum(a != b for a, b in zip(sent, _SIG, strict=True)) == 1
    assert _assistant_blocks(requests[3])[0]["thinking"] == "weighing the cities"
    assert _signature(requests[1]) == _SIG  # the control sent it untouched


def test_tampered_signature_accepted_fails(smoke, capsys):
    code, _ = _run(smoke, ["--check", "signature-tamper"], {_VOICE: [_STEP1, _ANSWER, _STEP1, _ANSWER]})

    assert code == 1
    assert "FAIL  signature-tamper: tampered signature was accepted; expected a 400" in capsys.readouterr().out


def test_tamper_400_is_inconclusive_when_the_control_failed(smoke, capsys):
    no_thinking = ([tool_use("tu_1", "get_weather", {"city": "Kyiv"})], "tool_use")
    code, _ = _run(smoke, ["--check", "signature-tamper"], {_VOICE: [no_thinking, _STEP1, 400]})

    out = capsys.readouterr().out
    assert code == 1
    assert "FAIL  tool-loop-thinking" in out
    assert "FAIL  signature-tamper: inconclusive: control (tool-loop-thinking) did not pass" in out


def _first_user_text(body: dict[str, Any]) -> str:
    first = body["messages"][0]
    assert first["role"] == "user"
    content = first["content"]
    return content if isinstance(content, str) else "".join(b.get("text", "") for b in content)


def test_edited_history_rejected_with_400_passes(smoke, capsys):
    code, build = _run(smoke, ["--check", "edited-history"], {_VOICE: [_STEP1, _ANSWER, _STEP1, 400]})

    out = capsys.readouterr().out
    assert code == 0
    assert "PASS  edited-history: edited history rejected with 400" in out
    first, control, _, edited = build.requests()
    assert _first_user_text(edited) != _first_user_text(first)
    assert _first_user_text(control) == _first_user_text(first)
    assert _assistant_blocks(edited)[0] == _assistant_blocks(control)[0]  # the thinking is unchanged


def test_edited_history_accepted_fails(smoke, capsys):
    code, _ = _run(smoke, ["--check", "edited-history"], {_VOICE: [_STEP1, _ANSWER, _STEP1, _ANSWER]})

    assert code == 1
    assert "FAIL  edited-history: edited history was accepted; expected a 400" in capsys.readouterr().out


def test_edited_history_400_is_inconclusive_when_the_control_failed(smoke, capsys):
    code, _ = _run(smoke, ["--check", "edited-history"], {_VOICE: [_STEP1, 404, _STEP1, 400]})

    assert code == 1
    assert "FAIL  edited-history: inconclusive: control (tool-loop-thinking) did not pass" in capsys.readouterr().out


def test_stripped_continuation_with_adaptive_thinking_passes(smoke, capsys):
    code, build = _run(smoke, ["--check", "stripped-continuation"], {_VOICE: [_STEP1, _ANSWER]})

    out = capsys.readouterr().out
    assert code == 0
    assert "PASS  stripped-continuation: 1 thinking block(s) returned (signed), stripped" in out
    first, second = build.requests()
    assert [b["type"] for b in _assistant_blocks(second)] == ["tool_use"]
    assert second["thinking"] == {"type": "adaptive"}
    assert "thinking" not in first


def test_stripped_continuation_rejected_fails(smoke, capsys):
    code, _ = _run(smoke, ["--check", "stripped-continuation"], {_VOICE: [_STEP1, 400]})

    assert code == 1
    assert "FAIL  stripped-continuation: Anthropic stream error: 400" in capsys.readouterr().out


def test_model_switch_skipped_without_a_second_model(smoke, capsys):
    code, build = _run(smoke, ["--check", "model-switch"], {})

    assert code == 0
    assert "SKIP  model-switch: no --switch-model given" in capsys.readouterr().out
    assert build.requests() == []


def test_model_switch_sends_the_blocks_unchanged_to_the_second_model(smoke, capsys):
    switch = "anthropic:claude-sonnet-5-5"
    code, build = _run(
        smoke, ["--check", "model-switch", "--switch-model", switch], {_VOICE: [_STEP1], switch: [_ANSWER]}
    )

    assert code == 0
    assert "PASS  model-switch: 1 thinking block(s) returned (signed), sent unchanged to claude-sonnet-5-5" in (
        capsys.readouterr().out
    )
    assert len(build.requests()) == 1
    [second] = build.requests(switch)
    assert second["model"] == "claude-sonnet-5-5"
    assert _assistant_blocks(second)[0] == {"type": "thinking", "thinking": "weighing the cities", "signature": _SIG}


def test_cache_reads_reported_for_two_turns_before_and_after_the_strip(smoke, capsys):
    step1 = (*_STEP1, {"cache_read_input_tokens": 0, "cache_creation_input_tokens": 1500})
    answer = ([thinking("short", _SIG), text("12C in Kyiv.")], "end_turn", {"cache_read_input_tokens": 1401})
    turn2 = ([text("Lviv is cooler.")], "end_turn", {"cache_read_input_tokens": 1402})
    turn2_stripped = ([text("Lviv is cooler.")], "end_turn", {"cache_read_input_tokens": 1403})
    code, build = _run(smoke, ["--check", "cache-reads"], {_VOICE: [step1, answer, turn2, turn2_stripped]})

    out = capsys.readouterr().out
    assert code == 0
    assert "PASS  cache-reads: cache_read_tokens turn 1: 0, 1401; turn 2: 1402 unstripped, 1403 after the strip" in out
    requests = build.requests()
    assert len(requests) == 4
    for body in requests:
        assert len(body["tools"]) == 2
        assert body["tools"][-1]["cache_control"] == {"type": "ephemeral"}  # production's breakpoint

    def thinking_count(body: dict[str, Any]) -> int:
        return sum(
            1
            for m in body["messages"]
            if m["role"] == "assistant" and not isinstance(m["content"], str)
            for b in m["content"]
            if b["type"] == "thinking"
        )

    assert thinking_count(requests[2]) == 2  # both turn-1 responses' thinking, kept
    assert thinking_count(requests[3]) == 0  # the end-of-turn strip
    assert requests[3]["messages"][-1] == requests[2]["messages"][-1]


def test_cache_prefix_is_above_the_haiku_5_5_minimum(smoke):
    # chars/4 is the estimate the dry run prints; 512 tokens is Haiku 5.5's minimum cacheable prompt
    assert 1200 <= smoke.CACHE_PREFIX_TOKENS <= 2000


def test_forced_cutoff_reports_max_tokens(smoke, capsys):
    cut = ([thinking("long", _SIG), text("Kyiv has")], "max_tokens")
    code, build = _run(smoke, ["--check", "forced-cutoff"], {_VOICE: [cut]})

    assert code == 0
    assert "PASS  forced-cutoff: stop_reason='max_tokens'" in capsys.readouterr().out
    [body] = build.requests()
    assert body["max_tokens"] == 32
    assert [tool["name"] for tool in body["tools"]] == ["get_weather"]


def test_forced_cutoff_fails_on_any_other_stop_reason(smoke, capsys):
    code, _ = _run(smoke, ["--check", "forced-cutoff"], {_VOICE: [_STEP1]})

    assert code == 1
    assert "FAIL  forced-cutoff: stop_reason='tool_use', expected 'max_tokens'" in capsys.readouterr().out


_OPENAI_NAMES = ["openai-effort-low", "openai-disabled", "openai-xhigh", "openai-minimal", "openai-unrecognised"]
_OPENAI_CHECKS = [arg for name in _OPENAI_NAMES for arg in ("--check", name)]
_MINI = "azure-openai:gpt-5.4-mini"
_TERRA = "azure-openai:gpt-6.1-terra"
# An Azure deployment name: off the resolver's allow-list (gpt-6.1-terra joined it in issue #357)
_UNRECOGNISED = "azure-openai:voice-fast"


def _foundry_openai(**overrides: Any) -> dict[str, Any]:
    """Settings overrides: the OpenAI-compatible slot on Foundry's OpenAI endpoint, no OpenAI key."""
    return {
        "openai_compatible_provider": "azure-openai",
        "openai_compatible_base_url": "https://foundry.example.invalid/openai/v1",
        "openai_compatible_model": "gpt-5.4-mini",
        **overrides,
    }


def test_openai_checks_skip_without_an_openai_model(smoke, capsys):
    code, build = _run(smoke, _OPENAI_CHECKS, {}, **_foundry_openai())

    out = capsys.readouterr().out
    assert code == 0
    for name in _OPENAI_NAMES:
        assert f"SKIP  {name}: no --openai-model given" in out
    assert build.fakes == {}


def test_a_recognised_openai_model_gets_low_none_xhigh_and_minimal(smoke, capsys):
    scripts = {_MINI: ["ok", "ok", "ok", 400]}
    code, build = _run(smoke, [*_OPENAI_CHECKS, "--openai-model", _MINI], scripts, **_foundry_openai())

    out = capsys.readouterr().out
    assert code == 0
    assert "PASS  openai-effort-low: reasoning_effort=low accepted: 'pong'" in out
    assert "PASS  openai-disabled: reasoning_effort=none accepted: 'pong'" in out
    assert "PASS  openai-xhigh: reasoning_effort=xhigh accepted" in out
    assert "PASS  openai-minimal: rejected with 400: the table is right (Pincer never sends it)" in out
    assert "SKIP  openai-unrecognised: gpt-5.4-mini is recognised" in out
    bodies = build.fakes[_MINI].requests
    assert [b["reasoning_effort"] for b in bodies] == ["low", "none", "xhigh", "minimal"]
    assert {b["model"] for b in bodies} == {"gpt-5.4-mini"}
    assert {b["max_completion_tokens"] for b in bodies} == {512}


def test_an_unrecognised_openai_model_is_sent_without_reasoning_effort(smoke, capsys):
    code, build = _run(
        smoke, [*_OPENAI_CHECKS, "--openai-model", _UNRECOGNISED], {_UNRECOGNISED: ["ok"]}, **_foundry_openai()
    )

    out = capsys.readouterr().out
    assert code == 0
    assert "PASS  openai-unrecognised: effort low dropped (unrecognised model), sent without it: 'pong'" in out
    for name in _OPENAI_NAMES[:4]:
        assert f"SKIP  {name}: voice-fast is not recognised; see openai-unrecognised" in out
    [body] = build.fakes[_UNRECOGNISED].requests
    assert body["model"] == "voice-fast"
    assert "reasoning_effort" not in body


def test_xhigh_and_minimal_skip_a_model_before_gpt_5_2(smoke, capsys):
    gpt51 = "azure-openai:gpt-5.1"
    argv = ["--check", "openai-xhigh", "--check", "openai-minimal", "--openai-model", gpt51]
    code, build = _run(smoke, argv, {}, **_foundry_openai())

    out = capsys.readouterr().out
    assert code == 0
    assert "SKIP  openai-xhigh: gpt-5.1 takes no xhigh (the resolver sends high)" in out
    assert "SKIP  openai-minimal: gpt-5.2 and later only" in out
    assert build.fakes[gpt51].requests == []


def test_openai_minimal_accepted_still_passes_and_says_so(smoke, capsys):
    code, _ = _run(smoke, ["--check", "openai-minimal", "--openai-model", _MINI], {_MINI: ["ok"]}, **_foundry_openai())

    assert code == 0
    assert "PASS  openai-minimal: accepted: the table is conservative (Pincer sends none)" in capsys.readouterr().out


def test_openai_minimal_fails_on_another_error(smoke, capsys):
    code, _ = _run(smoke, ["--check", "openai-minimal", "--openai-model", _MINI], {_MINI: [404]}, **_foundry_openai())

    assert code == 1
    assert "FAIL  openai-minimal: expected a 200 or a 400, got 404" in capsys.readouterr().out


_AZURE = "azure:claude-haiku-5-5"
_AZURE_URL = "https://foundry.example.invalid/anthropic"


def _azure_settings(**overrides: Any) -> Settings:
    """The owner's shape: the voice turn on an Anthropic-compatible slot, every key blank."""
    azure: dict[str, Any] = {
        "anthropic_api_key": "",
        "default_provider": "azure",
        "default_model": "claude-haiku-5-5",
        "anthropic_compatible_provider": "azure",
        "anthropic_compatible_base_url": _AZURE_URL,
        "anthropic_compatible_model": "claude-haiku-5-5",
        "voice_turn_model": _AZURE,
    }
    return _settings(**{**azure, **overrides})


def _never_build(*args: Any) -> Any:
    raise AssertionError("a real provider was built")


def test_dry_run_lists_every_request_and_sends_nothing(smoke, capsys, monkeypatch):
    monkeypatch.setattr(httpx.AsyncClient, "send", _no_network)
    monkeypatch.setattr(httpx.Client, "send", _no_network)

    code = smoke.main(["--dry-run"], settings=_azure_settings(), build=_never_build)

    out = capsys.readouterr().out
    assert code == 0
    lines = [line for line in out.splitlines() if line.startswith(_AZURE)]
    assert len(lines) == 19  # 4 basic + 15 voice (model-switch skipped); no OpenAI key: none
    assert sum("check=forced-cutoff " in line for line in lines) == 1
    assert "check=forced-cutoff max_tokens=32 effort=- thinking=- replay=no" in out
    assert "check=effort-low max_tokens=1024 effort=low thinking=- replay=no" in out
    assert "check=thinking-disabled max_tokens=1024 effort=- thinking=disabled replay=no" in out
    assert "check=stripped-continuation max_tokens=1024 effort=- thinking=adaptive replay=no" in out
    assert "check=signature-tamper max_tokens=1024 effort=- thinking=- replay=yes" in out
    assert "19 requests, largest max_tokens 1024" in out
    assert f"cache check tools prefix ~{smoke.CACHE_PREFIX_TOKENS} tokens" in out
    assert "bound: 40 requests" in out
    assert "nothing was sent" in out
    assert _AZURE_URL not in out


def _as_planned(body: dict[str, Any]) -> tuple[Any, ...]:
    """A recorded request body in the dry-run's terms."""
    effort = body.get("output_config", {}).get("effort") or body.get("reasoning_effort") or "-"
    replays = any(
        block.get("type") == "thinking"
        for message in body["messages"]
        if message["role"] == "assistant" and isinstance(message["content"], list)
        for block in message["content"]
    )
    max_tokens = body.get("max_tokens") or body.get("max_completion_tokens")
    return body["model"], max_tokens, effort, body.get("thinking", {}).get("type", "-"), replays


async def test_dry_run_plan_is_exactly_what_a_live_run_sends(smoke, capsys):
    switch, basic = "anthropic:claude-sonnet-5-5", "anthropic:claude-opus-5-5"
    cut = ([thinking("long", _SIG), text("Kyiv has")], "max_tokens")
    pong = ([text("pong")], "end_turn")
    scripts: dict[str, list[Any]] = {
        basic: [pong, pong, _STEP1, _ANSWER],
        _VOICE: [pong, pong, _STEP1, _ANSWER, _STEP1, 400, _STEP1, 400, _STEP1, _ANSWER, _STEP1]
        + [_STEP1, _ANSWER, _ANSWER, _ANSWER, cut],
        switch: [_ANSWER],
        _MINI: ["ok", "ok", "ok", 400],
        _UNRECOGNISED: ["ok"],
    }
    settings = _settings(**_foundry_openai())
    kwargs: dict[str, Any] = {
        "voice": ("anthropic", "claude-haiku-5-5"),
        "switch": ("anthropic", "claude-sonnet-5-5"),
        "openai": [("azure-openai", "gpt-5.4-mini"), ("azure-openai", "voice-fast")],
    }
    build = FakeBuild(scripts)

    planned = await smoke.plan_requests(settings, [("anthropic", "claude-opus-5-5")], **kwargs)
    failures = await smoke.run(settings, [("anthropic", "claude-opus-5-5")], build=build, **kwargs)

    assert failures == 0, capsys.readouterr().out
    assert len(planned) == len(build.order) == 4 + 17 + 4 + 1

    def sent(value: str) -> str:  # a dropped setting is absent from the body
        return "-" if value.startswith("dropped(") else value

    assert [(p.model, p.max_tokens, sent(p.effort), sent(p.thinking), p.replays_thinking) for p in planned] == [
        _as_planned(body) for body in build.order
    ]
    assert [p.effort for p in planned if p.model == "voice-fast"] == ["dropped(unrecognised model)"]


def _many_models(n: int) -> list[str]:
    return [arg for i in range(n) for arg in ("--model", f"anthropic:claude-model-{i}")]


def test_a_plan_over_the_cost_bound_is_refused_before_anything_is_built(smoke, capsys):
    code = smoke.main(["--voice-model", _VOICE, *_many_models(7)], settings=_settings(), build=_never_build)

    captured = capsys.readouterr()
    assert code == 2
    assert "Refusing to run: 43 requests" in captured.err
    assert captured.out == ""


def test_a_plan_at_the_cost_bound_runs(smoke, capsys):
    # 6 basic targets (24) + 15 voice = 39, under the bound of 40: it gets as far as building
    with pytest.raises(AssertionError, match="a real provider was built"):
        smoke.main(["--voice-model", _VOICE, *_many_models(6)], settings=_settings(), build=_never_build)


def test_dry_run_over_the_bound_says_it_would_be_refused(smoke, capsys):
    code = smoke.main(
        ["--dry-run", "--voice-model", _VOICE, *_many_models(7)], settings=_settings(), build=_never_build
    )

    assert code == 2
    assert "43 requests" in capsys.readouterr().out


def test_keys_and_base_urls_are_never_printed(smoke, capsys):
    secret = "sk-azure-secret-0123456789"
    settings = _azure_settings(anthropic_compatible_api_key=secret)

    def leaky_build(settings: Settings, provider: str, model: str) -> Any:
        raise ValueError(f"cannot reach {_AZURE_URL}/v1/messages with key {secret}")

    code = smoke.main(["--check", "effort-low"], settings=settings, build=leaky_build)

    out = capsys.readouterr().out
    assert code == 1
    assert "FAIL  provider setup: cannot reach <redacted>/v1/messages with key <redacted>" in out
    assert secret not in out and _AZURE_URL not in out


def test_the_basic_checks_still_run_on_each_model(smoke, capsys):
    basic = "anthropic:claude-opus-5-5"
    pong = ([text("pong")], "end_turn")
    argv = ["--model", basic, "--check", "complete", "--check", "stream", "--check", "tool round-trip"]
    code, build = _run(smoke, argv, {basic: [pong, pong, _STEP1, _ANSWER]})

    out = capsys.readouterr().out
    assert code == 0
    assert f"{basic}\n  PASS  complete: 'pong'" in out
    assert "PASS  stream: 'pong'" in out
    assert "PASS  tool round-trip: get_weather({'city': 'Kyiv'})" in out
    assert [body["max_tokens"] for body in build.requests(basic)] == [1024] * 4
    assert _VOICE not in build.fakes  # --check narrowed the run to the basic checks


_COLLISION = "provider name 'azure' is used by both compatible slots; the voice target would resolve to the OpenAI slot"


@pytest.mark.parametrize("dry_run", [False, True])
def test_a_voice_provider_named_like_both_slots_fails_before_sending(smoke, capsys, dry_run):
    settings = _azure_settings(
        openai_compatible_provider="azure",
        openai_compatible_base_url="https://foundry.example.invalid/openai/v1",
        openai_compatible_model="gpt-5.4-mini",
    )
    argv = ["--voice-model", _AZURE, "--check", "effort-low", *(["--dry-run"] if dry_run else [])]

    code = smoke.main(argv, settings=settings, build=_never_build)

    out = capsys.readouterr().out
    assert code == 1
    assert f"FAIL  voice target: {_COLLISION}" in out
    assert "check=effort-low" not in out


def test_voice_checks_skip_an_openai_wire_voice_target(smoke, capsys):
    settings = _azure_settings(
        openai_compatible_provider="azure-openai",
        openai_compatible_base_url="https://foundry.example.invalid/openai/v1",
        openai_compatible_model="gpt-5.4-mini",
    )
    argv = ["--voice-model", "azure-openai:gpt-5.4-mini", "--check", "effort-low", "--check", "forced-cutoff"]

    code = smoke.main(argv, settings=settings, build=_never_build)

    out = capsys.readouterr().out
    assert code == 0
    assert out.count("SKIP") == 2
    assert "SKIP  effort-low: azure-openai is OpenAI-wire; the voice checks are Anthropic-wire only" in out


def test_dry_run_of_the_live_gate_command(smoke, capsys):
    # the lead's live command: explicit targets only, never the default anthropic: ones
    settings = _azure_settings(anthropic_api_key="sk-ant-test", fallback_providers=["anthropic"], **_foundry_openai())
    assert ("anthropic", "claude-haiku-5-5") in smoke.default_targets(settings)  # precondition: there is one to leak
    argv = [
        "--dry-run",
        *("--model", _AZURE, "--voice-model", _AZURE, "--switch-model", "azure:claude-sonnet-5-5"),
        *("--openai-model", _MINI, "--openai-model", _TERRA),
    ]

    code = smoke.main(argv, settings=settings, build=_never_build)

    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if " check=" in line]
    assert code == 0
    assert not [line for line in lines if line.startswith("anthropic:")]
    assert sum(line.startswith(f"{_AZURE} check=") for line in lines) == 4 + 16
    assert sum(line.startswith("azure:claude-sonnet-5-5 check=model-switch") for line in lines) == 1
    assert sum(line.startswith(f"{_MINI} check=openai-") for line in lines) == 4
    # gpt-6.1-terra is recognised (issue #357): the four table checks, no unrecognised one
    assert [line.split(" thinking=")[0] for line in lines if line.startswith(f"{_TERRA} check=")] == [
        f"{_TERRA} check=openai-effort-low max_tokens=512 effort=low",
        f"{_TERRA} check=openai-disabled max_tokens=512 effort=none",
        f"{_TERRA} check=openai-xhigh max_tokens=512 effort=xhigh",
        f"{_TERRA} check=openai-minimal max_tokens=512 effort=minimal",
    ]
    assert "29 requests, largest max_tokens 1024" in out


def test_openai_disabled_skips_a_model_that_cannot_turn_reasoning_off(smoke, capsys):
    o3 = "azure-openai:o3"
    code, build = _run(smoke, ["--check", "openai-disabled", "--openai-model", o3], {}, **_foundry_openai())

    assert code == 0
    assert "SKIP  openai-disabled: disabled not supported by o3; nothing to check" in capsys.readouterr().out
    assert build.fakes[o3].requests == []


def test_thinking_disabled_skips_a_model_that_does_not_think_by_default(smoke, capsys):
    haiku45 = "anthropic:claude-haiku-4-5"
    build = FakeBuild({})
    code = smoke.main(["--voice-model", haiku45, "--check", "thinking-disabled"], settings=_settings(), build=build)

    assert code == 0
    assert "SKIP  thinking-disabled: claude-haiku-4-5 does not think by default; nothing to check" in (
        capsys.readouterr().out
    )
    assert build.requests(haiku45) == []


def test_a_dummy_key_does_not_mangle_the_output(smoke, capsys):
    # the lead neutralises keys with "x"; redacting every "x" would garble max_tokens and xhigh
    settings = _azure_settings(anthropic_compatible_api_key="x", **_foundry_openai(openai_compatible_api_key="x"))

    code = smoke.main(["--dry-run", "--openai-model", _MINI], settings=settings, build=_never_build)

    out = capsys.readouterr().out
    assert code == 0
    assert "check=openai-xhigh max_tokens=512 effort=xhigh" in out
    assert "<redacted>" not in out
