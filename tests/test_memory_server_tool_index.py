"""Tests for the tool index (tool_index_sync / tool_index_search) in the sqlite-vec memory MCP server."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import math
import struct
from pathlib import Path
from types import ModuleType

import pytest

_SERVER = Path(__file__).resolve().parents[1] / "src" / "sqlite-vec-memory-mcp" / "memory_server.py"


def _load_server() -> ModuleType:
    spec = importlib.util.spec_from_file_location("sqlite_vec_memory_server_under_test", _SERVER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


server = _load_server()


def _bag_of_words(text: str) -> bytes:
    """Deterministic stand-in for the embedding model: hashed bag of words, unit length."""
    vec = [0.0] * server._TOOL_EMBED_DIM
    for word in text.lower().replace(".", " ").split():
        vec[int(hashlib.sha256(word.encode()).hexdigest(), 16) % len(vec)] += 1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return struct.pack(f"{len(vec)}f", *(v / norm for v in vec))


@pytest.fixture
def embedded(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Fresh in-memory index per test; returns every text the fake model embedded."""
    seen: list[str] = []

    def fake_embed(texts: list[str]) -> list[bytes]:
        seen.extend(texts)
        return [_bag_of_words(t) for t in texts]

    monkeypatch.setattr(server, "_embed_tools", fake_embed)
    monkeypatch.setattr(server, "_tool_db", None)
    monkeypatch.setattr(server, "_tool_index_version", "")
    return seen


_TOOLS = [
    {"name": "make_phone_call", "text": "make phone call. Call a phone number"},
    {"name": "slack__post_message", "text": "slack post message. Post a message to a slack channel"},
    {"name": "google__search_drive_files", "text": "google search drive files. Search files on google drive"},
]


def test_sync_embeds_only_new_or_changed_and_deletes_removed(embedded: list[str]) -> None:
    stats = server.tool_index_sync(_TOOLS, "v1")
    assert stats == {"version": "v1", "added": 3, "updated": 0, "removed": 0, "unchanged": 0}
    assert len(embedded) == 3

    embedded.clear()
    changed = [
        _TOOLS[0],
        {"name": "slack__post_message", "text": "slack post message. Send a slack message to a channel"},
    ]
    stats = server.tool_index_sync(changed, "v2")
    assert stats == {"version": "v2", "added": 0, "updated": 1, "removed": 1, "unchanged": 1}
    assert embedded == [changed[1]["text"]]

    names = {r["name"] for r in server.tool_index_search("drive files", 10, "v2")["results"]}
    assert names == {"make_phone_call", "slack__post_message"}


def test_failed_sync_rolls_back_so_the_next_sync_re_embeds(
    embedded: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    server.tool_index_sync(_TOOLS, "v1")
    changed = [{"name": "make_phone_call", "text": "make phone call. Ring someone"}, *_TOOLS[1:]]
    good_embed = server._embed_tools
    # A vector of the wrong size: vec0 rejects the insert after the hash UPDATE and vector DELETE ran.
    monkeypatch.setattr(server, "_embed_tools", lambda texts: [struct.pack("2f", 1.0, 0.0) for _ in texts])

    with pytest.raises(Exception, match="(?i)dimension"):
        server.tool_index_sync(changed, "v2")

    monkeypatch.setattr(server, "_embed_tools", good_embed)
    assert server.tool_index_search("phone", 3, "v1")["stale"] is False  # still at v1, intact
    assert len(server.tool_index_search("phone", 3, "v1")["results"]) == 3
    stats = server.tool_index_sync(changed, "v2")
    assert stats["updated"] == 1  # the old hash survived, so the changed tool is re-embedded


def test_search_with_other_version_is_stale(embedded: list[str]) -> None:
    server.tool_index_sync(_TOOLS, "v1")
    assert server.tool_index_search("phone", 3, "v0") == {"stale": True, "results": []}
    # A fresh server (index lost on restart) is stale for any version.
    server._tool_index_version = ""
    assert server.tool_index_search("phone", 3, "v1")["stale"] is True


def test_search_orders_by_distance(embedded: list[str]) -> None:
    server.tool_index_sync(_TOOLS, "v1")
    result = server.tool_index_search("call a phone number", 3, "v1")
    assert result["stale"] is False
    names = [r["name"] for r in result["results"]]
    distances = [r["distance"] for r in result["results"]]
    assert names[0] == "make_phone_call"
    assert distances == sorted(distances)
    assert len(names) == 3


def test_search_caps_k_and_handles_empty_index_and_query(embedded: list[str]) -> None:
    server.tool_index_sync([], "empty")
    assert server.tool_index_search("phone", 5, "empty") == {"stale": False, "results": []}
    server.tool_index_sync(_TOOLS, "v1")
    assert len(server.tool_index_search("phone", 50, "v1")["results"]) == 3
    assert server.tool_index_search("   ", 5, "v1") == {"stale": False, "results": []}


def test_index_tools_are_marked_internal() -> None:
    tools = asyncio.run(server.mcp.list_tools())
    meta = {t.name: (t.meta or {}) for t in tools}
    assert meta["tool_index_sync"].get("pincer_internal") is True
    assert meta["tool_index_search"].get("pincer_internal") is True
    assert not meta["memory_store"].get("pincer_internal")
