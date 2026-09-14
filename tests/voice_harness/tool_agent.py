"""Scripted *tool-using* stream agent for the in-call tool harness (Sprint 11).

The Sprint 5 FakeStreamAgent replays chunks; this one behaves like the real
agent loop around tools: every scripted tool call goes through the bound
in-call gate (``current_gate().run``) exactly as ``Agent.stream_voice_turn``
does, so the channel-level tests exercise the real policy, speech, and
approval flows. It is "LLM-compliant" in the two places the spec relies on
the model: after a ``[CONFIRMED]`` note it re-emits the pending tool call
with the same arguments, and after ``[DECLINED]`` it just acknowledges.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from pincer.core.agent import StreamChunk, StreamEventType
from pincer.voice.in_call_tools import current_gate


@dataclass
class Text:
    text: str


@dataclass
class Tool:
    name: str
    args: dict[str, Any] = field(default_factory=dict)
    result: str = "ok"
    is_error: bool = False
    delay_s: float = 0.0
    # Text the "model" says after seeing the tool result (dropped when the
    # gate suppresses LLM speech for the turn)
    then: str = ""


Step = Text | Tool


class ToolAgent:
    """Per-turn scripts of Text/Tool steps, routed through the in-call gate."""

    def __init__(self, turns: list[list[Step]], *, confirm_reply: str = "Done. Anything else?") -> None:
        self.turns = [list(t) for t in turns]
        self.calls: list[dict[str, Any]] = []
        self.gate_results: list[Any] = []
        self.executed: list[tuple[str, dict[str, Any]]] = []
        self.last_tool: Tool | None = None
        self.confirm_reply = confirm_reply
        self.pending_re_emit: Tool | None = None

    def _executor(self, step: Tool):
        async def _run() -> tuple[str, bool]:
            if step.delay_s:
                await asyncio.sleep(step.delay_s)
            self.executed.append((step.name, dict(step.args)))
            return step.result, step.is_error

        return _run

    async def _emit_tool(self, step: Tool):
        gate = current_gate()
        assert gate is not None, "ToolAgent requires a bound in-call gate"
        yield StreamChunk(StreamEventType.TOOL_START, step.name)
        result = await gate.run(step.name, dict(step.args), self._executor(step))
        self.gate_results.append(result)
        self.last_tool = step
        yield StreamChunk(StreamEventType.TOOL_DONE, step.name)
        if step.then and not result.suppress_llm_speech:
            yield StreamChunk(StreamEventType.TEXT, step.then)

    async def stream_voice_turn(self, **kwargs: Any):
        self.calls.append(kwargs)
        extra = str(kwargs.get("extra_system") or "")
        full = ""
        if "[CONFIRMED]" in extra and self.last_tool is not None:
            # A compliant model re-emits the confirmed tool call unchanged.
            step = Tool(
                self.last_tool.name,
                dict(self.last_tool.args),
                result=self.last_tool.result,
                is_error=self.last_tool.is_error,
                delay_s=self.last_tool.delay_s,
                then=self.confirm_reply,
            )
            async for chunk in self._emit_tool(step):
                if chunk.type == StreamEventType.TEXT:
                    full += chunk.content
                yield chunk
            yield StreamChunk(StreamEventType.DONE, full)
            return
        if "[DECLINED]" in extra:
            text = "Alright, nothing has been changed. Anything else I can do?"
            yield StreamChunk(StreamEventType.TEXT, text)
            yield StreamChunk(StreamEventType.DONE, text)
            return
        script = self.turns.pop(0) if self.turns else [Text("Is there anything else I can help with?")]
        for step in script:
            if isinstance(step, Text):
                full += step.text
                yield StreamChunk(StreamEventType.TEXT, step.text)
            else:
                async for chunk in self._emit_tool(step):
                    if chunk.type == StreamEventType.TEXT:
                        full += chunk.content
                    yield chunk
        yield StreamChunk(StreamEventType.DONE, full)
