"""A photo sent in chat stays in the conversation (issue #358, slice 3)."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import re
from pathlib import Path
from typing import Any

import pytest
from anthropic_sse import ScriptedAnthropic, text
from PIL import Image

from pincer.cli._attachments import describe_image_attachments
from pincer.core.agent import Agent
from pincer.core.session import SessionManager
from pincer.llm.base import ImageContent, LLMMessage, MessageRole
from pincer.storage.workspace import Workspace
from pincer.tools.registry import ToolRegistry


def jpeg(width: int, height: int, *, seed: int = 0) -> bytes:
    """A real JPEG with some structure, so its bytes depend on its pixels."""
    image = Image.new("RGB", (width, height))
    pixels = image.load()
    assert pixels is not None
    for x in range(0, width, 8):
        for y in range(0, height, 8):
            pixels[x, y] = ((x + seed) % 256, (y * 3) % 256, (x + y) % 256)
    out = io.BytesIO()
    image.save(out, "JPEG", quality=90)
    return out.getvalue()


def image_blocks(request: dict[str, Any]) -> list[tuple[int, dict[str, Any]]]:
    """Every image block of the request with the index of the message holding it."""
    return [
        (index, block)
        for index, message in enumerate(request["messages"])
        if isinstance(message["content"], list)
        for block in message["content"]
        if block["type"] == "image"
    ]


@pytest.fixture
def registry() -> ToolRegistry:
    return ToolRegistry()


def _replies(count: int) -> list[Any]:
    return [([text(f"Reply {n}.")], "end_turn") for n in range(count)]


async def test_turns_two_to_four_carry_the_photo_of_turn_one_in_the_same_position(
    settings, session_manager, cost_tracker, registry
):
    photo = jpeg(1000, 800)
    async with ScriptedAnthropic(settings, _replies(4)) as llm:
        agent = Agent(settings, llm.provider, session_manager, cost_tracker, registry)
        await agent.handle_message("user1", "test", "What is this?", images=[(photo, "image/jpeg")], memory=False)
        for follow_up in ("And now?", "Again?", "Once more?"):
            await agent.handle_message("user1", "test", follow_up, memory=False)

    assert len(llm.requests) == 4
    sent = [image_blocks(request) for request in llm.requests]
    assert [len(blocks) for blocks in sent] == [1, 1, 1, 1]  # precondition: turn 1 sent it, and so did the rest
    for blocks in sent:
        (position, block) = blocks[0]
        assert position == 0  # the first message of the conversation, each time
        assert block["source"]["media_type"] == "image/jpeg"
        assert base64.b64decode(block["source"]["data"]) == photo


async def test_a_photo_is_stored_once_as_a_ref_and_survives_a_reload(
    settings, session_manager, cost_tracker, registry, tmp_path
):
    photo = jpeg(1000, 800)
    digest = hashlib.sha256(photo).hexdigest()
    async with ScriptedAnthropic(settings, _replies(3)) as llm:
        agent = Agent(settings, llm.provider, session_manager, cost_tracker, registry)
        await agent.handle_message("user1", "test", "What is this?", images=[(photo, "image/jpeg")], memory=False)
        await agent.handle_message("user1", "test", "And now?", memory=False)

        row = await session_manager._store.load("test:user1", user_id="user1", channel="test", legacy_fallback=False)
        assert row is not None
        stored = json.loads(row["messages"])
        assert stored[0].get("images") == [{"sha256": digest, "media_type": "image/jpeg"}]
        assert base64.b64encode(photo).decode() not in row["messages"]  # the bytes live in their own table
        assert [(i.sha256, i.media_type, i.data) for i in row["images"]] == [(digest, "image/jpeg", photo)]

        reloaded = SessionManager(tmp_path / "test.db")
        await reloaded.initialize()
        try:
            agent = Agent(settings, llm.provider, reloaded, cost_tracker, registry)
            await agent.handle_message("user1", "test", "Still there?", memory=False)
        finally:
            await reloaded.close()

    assert len(llm.requests) == 3
    assert len(image_blocks(llm.requests[1])) == 1  # precondition: the cached session still held it
    reloaded_blocks = image_blocks(llm.requests[2])
    assert len(reloaded_blocks) == 1
    ((position, block),) = reloaded_blocks
    assert position == 0
    assert base64.b64decode(block["source"]["data"]) == photo


async def _stored_photos(session_manager, session_id: str, user_id: str, channel: str):
    row = await session_manager._store.load(session_id, user_id=user_id, channel=channel, legacy_fallback=False)
    assert row is not None
    return row["images"]


async def test_chat_prepares_a_big_photo_once_and_sends_what_it_stores(
    settings, session_manager, cost_tracker, registry
):
    raw = jpeg(4000, 3000)
    async with ScriptedAnthropic(settings, _replies(2)) as llm:
        agent = Agent(settings, llm.provider, session_manager, cost_tracker, registry)
        await agent.handle_message("user1", "test", "What is this?", images=[(raw, "image/jpeg")], memory=False)
        await agent.handle_message("user1", "test", "And now?", memory=False)

    assert Image.open(io.BytesIO(raw)).size == (4000, 3000)  # precondition
    sent = [base64.b64decode(blocks[0][1]["source"]["data"]) for blocks in map(image_blocks, llm.requests)]
    assert [Image.open(io.BytesIO(data)).size for data in sent] == [(2576, 1932), (2576, 1932)]
    assert sent[0] == sent[1]
    (stored,) = await _stored_photos(session_manager, "test:user1", "user1", "test")
    assert (stored.data, stored.sha256) == (sent[0], hashlib.sha256(sent[0]).hexdigest())
    assert stored.sha256 != hashlib.sha256(raw).hexdigest()


async def test_streaming_chat_prepares_a_big_photo_once_and_sends_what_it_stores(
    settings, session_manager, cost_tracker, registry
):
    raw = jpeg(4000, 3000)
    async with ScriptedAnthropic(settings, _replies(2)) as llm:
        agent = Agent(settings, llm.provider, session_manager, cost_tracker, registry)
        _ = [
            c
            async for c in agent.handle_message_stream(
                "user1", "web", "What is this?", images=[(raw, "image/jpeg")], memory=False
            )
        ]
        _ = [c async for c in agent.handle_message_stream("user1", "web", "And now?", memory=False)]

    sent = [base64.b64decode(blocks[0][1]["source"]["data"]) for blocks in map(image_blocks, llm.requests)]
    assert [Image.open(io.BytesIO(data)).size for data in sent] == [(2576, 1932), (2576, 1932)]
    (stored,) = await _stored_photos(session_manager, "web:user1", "user1", "web")
    assert stored.data == sent[0]


def test_a_message_keeps_refs_to_its_photos_and_finds_them_again_by_hash():
    shown = ImageContent.from_bytes(b"photo bytes", "image/png")
    message = LLMMessage(role=MessageRole.USER, content="look", images=[shown])

    stored = message.to_dict()

    assert stored["images"] == [{"sha256": hashlib.sha256(b"photo bytes").hexdigest(), "media_type": "image/png"}]
    assert LLMMessage.from_dict(stored, {shown.sha256: shown}).images == [shown]
    assert LLMMessage.from_dict(stored).images == []  # no store to resolve it from: left out
    assert "images" not in LLMMessage(role=MessageRole.USER, content="plain").to_dict()


def test_a_photo_is_never_in_a_repr():
    shown = ImageContent.from_bytes(b"photo bytes", "image/png")

    assert shown.data not in repr(shown)
    assert shown.data not in repr(LLMMessage(role=MessageRole.USER, content="look", images=[shown]))


def test_an_image_built_from_text_that_is_not_base64_has_no_hash_and_is_not_stored():
    """MCP sampling builds `ImageContent` from whatever the server sent; it must not raise."""
    odd = ImageContent(data="not base64 !!", media_type="image/png")

    shown = ImageContent.from_bytes(b"photo bytes", "image/png")
    assert odd.sha256 == ""
    mixed = LLMMessage(role=MessageRole.USER, content="x", images=[odd, shown]).to_dict()
    assert mixed["images"] == [shown.to_ref()]
    assert "images" not in LLMMessage(role=MessageRole.USER, content="x", images=[odd]).to_dict()
    assert (
        ImageContent(data=base64.b64encode(b"abc").decode(), media_type="image/png").sha256
        == hashlib.sha256(b"abc").hexdigest()
    )


def test_the_workspace_keeps_the_original_photo_and_its_note_as_before(tmp_path: Path):
    """Characterization: resizing happens in the agent, after `describe_image_attachments` saved the
    bytes as they arrived. (`cli/run.py` hands both the same list; neither file is touched by slice 3.)"""
    raw = jpeg(4000, 3000)
    workspace = Workspace(tmp_path, tmp_path)

    [note] = describe_image_attachments(workspace, [(raw, "image/jpeg")])

    saved = [path for _rel, path in workspace.files() if _rel.startswith("uploads/")]
    assert [path.read_bytes() for path in saved] == [raw]
    assert re.fullmatch(
        rf"\[Image: image_[0-9a-f]{{8}}\.jpg\] saved to {re.escape(str(saved[0]))} \({len(raw)} bytes, image/jpeg\)\.\n"
        r"If the user wants text read/extracted from this image, call the OCR tool "
        rf"with file_path='{re.escape(str(saved[0]))}' — do not try to transcribe it yourself\.",
        note,
    )
