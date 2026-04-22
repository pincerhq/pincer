"""
summarize_mcp — Web page and YouTube transcript MCP server built with fastmcp.

Transports:
    stdio (default):  python server.py --transport stdio
    HTTP:             python server.py --transport http [--host 0.0.0.0] [--port 8000]
        MCP endpoint → http://localhost:8000/mcp

    Or use the fastmcp CLI:
        fastmcp run server.py --transport http --port 8000
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import html
import json
import logging
import os
import re

import httpx
import uvicorn
from fastapi.responses import JSONResponse
from fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

mcp = FastMCP(
    name="summarize_mcp",
    instructions=(
        "Web page and YouTube transcript MCP server. "
        "Use summarize_url to fetch a URL and get clean readable text. "
        "Use get_youtube_transcript to get the transcript of a YouTube video."
    ),
)

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; rv:91.0) Gecko/20100101 "
        "Firefox/91.0"
}


def _strip_html(html_text: str) -> str:
    if not html_text:
        return ""
    text = html_text
    text = re.sub(r"<script[^>]*>[\s\S]*?</script>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<style[^>]*>[\s\S]*?</style>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _extract_title(html_text: str) -> str:
    m = re.search(r"<title[^>]*>([^<]*)</title>", html_text, re.IGNORECASE | re.DOTALL)
    if m:
        return _strip_html(f"<x>{m.group(1)}</x>")[:500]
    return ""


def _handle_error(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP error {exc.response.status_code}"
    if isinstance(exc, httpx.TimeoutException):
        return "Error: request timed out – please try again"
    return f"Error: {type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# YouTube helpers
# ---------------------------------------------------------------------------

_YT_ID_PATTERNS = [
    r"(?:youtube\.com/watch\?v=)([a-zA-Z0-9_-]{11})",
    r"(?:youtu\.be/)([a-zA-Z0-9_-]{11})",
    r"(?:youtube\.com/embed/)([a-zA-Z0-9_-]{11})",
    r"(?:youtube\.com/v/)([a-zA-Z0-9_-]{11})",
    r"(?:youtube\.com/shorts/)([a-zA-Z0-9_-]{11})",
]


def _extract_video_id(url: str) -> str | None:
    for pat in _YT_ID_PATTERNS:
        m = re.search(pat, url)
        if m:
            return m.group(1)
    return None


def _parse_caption_events(cap_data: dict) -> str:
    parts = []
    for ev in cap_data.get("events", []):
        for seg in ev.get("segs", []):
            txt = seg.get("utf8", "").strip()
            if txt:
                parts.append(txt)
    return " ".join(parts)


def _pick_caption_url(tracks: list, language: str) -> str | None:
    for t in tracks:
        if t.get("languageCode", "").startswith(language[:2]):
            return t.get("baseUrl")
    return tracks[0].get("baseUrl") if tracks else None


async def _fetch_captions(client: httpx.AsyncClient, base_url: str) -> str:
    r = await client.get(base_url + "&fmt=json3", headers=_HEADERS)
    r.raise_for_status()
    return _parse_caption_events(r.json())


async def _try_innertube(video_id: str, language: str) -> tuple[str, str]:
    payload = {
        "videoId": video_id,
        "context": {"client": {"clientName": "WEB", "clientVersion": "2.20231219.01.00"}},
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(
                "https://www.youtube.com/youtubei/v1/player",
                json=payload,
                headers={"Content-Type": "application/json", **_HEADERS},
            )
            r.raise_for_status()
            data = r.json()
        if data.get("playabilityStatus", {}).get("status") == "ERROR":
            return "", ""
        title = data.get("videoDetails", {}).get("title", "")
        tracks = data.get("captions", {}).get("playerCaptionsTracklistRenderer", {}).get("captionTracks", [])
        base_url = _pick_caption_url(tracks, language)
        if not base_url:
            return "", title
        async with httpx.AsyncClient(timeout=10.0) as client:
            transcript = await _fetch_captions(client, base_url)
        return transcript, title
    except Exception:
        return "", ""


async def _try_watch_page(video_id: str, language: str) -> tuple[str, str]:
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            r = await client.get(
                f"https://www.youtube.com/watch?v={video_id}",
                headers={"User-Agent": "Mozilla/5.0 (compatible; Pincer/1.0)"},
            )
            r.raise_for_status()
            page = r.text
        m = re.search(r"ytInitialPlayerResponse\s*=\s*(\{.+?\});\s*var", page)
        if not m:
            m = re.search(r"ytInitialPlayerResponse\s*=\s*(\{.+?\});", page, re.DOTALL)
        if not m:
            return "", ""
        data = json.loads(m.group(1))
        title = data.get("videoDetails", {}).get("title", "")
        tracks = data.get("captions", {}).get("playerCaptionsTracklistRenderer", {}).get("captionTracks", [])
        base_url = _pick_caption_url(tracks, language)
        if not base_url:
            return "", title
        async with httpx.AsyncClient(timeout=10.0) as client:
            transcript = await _fetch_captions(client, base_url)
        return transcript, title
    except Exception:
        return "", ""


async def _fetch_transcript_fallback(video_id: str, language: str) -> tuple[str, str]:
    transcript, title = await _try_innertube(video_id, language)
    if transcript:
        return transcript, title
    return await _try_watch_page(video_id, language)


def _get_transcript_via_api(video_id: str, language: str) -> list[dict]:
    """Run youtube_transcript_api (blocking) — called via asyncio.to_thread."""
    from youtube_transcript_api import YouTubeTranscriptApi  # type: ignore[import]

    with contextlib.suppress(Exception):
        result = YouTubeTranscriptApi.get_transcript(video_id)
        if result:
            return list(result)
    try:
        api = YouTubeTranscriptApi()
        fetched = api.fetch(video_id, languages=[language[:2]])
        return list(fetched) if hasattr(fetched, "__iter__") else []
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Input models
# ---------------------------------------------------------------------------


class SummarizeInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    url: str = Field(description="URL to fetch and extract text from (must start with http:// or https://)")
    max_length: int = Field(
        default=5000,
        description="Maximum characters of extracted text to return (default: 5000)",
        ge=100,
        le=100_000,
    )

    @field_validator("url")
    @classmethod
    def _valid_url(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("url cannot be empty")
        if not v.lower().startswith(("http://", "https://")):
            raise ValueError("URL must start with http:// or https://")
        return v


class YoutubeInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    url: str = Field(description="YouTube video URL (watch, youtu.be, embed, shorts)")
    language: str = Field(
        default="en",
        description="Preferred transcript language code (default: 'en'). Falls back to any available language.",
    )
    max_length: int = Field(
        default=8000,
        description="Maximum characters of transcript to return (default: 8000)",
        ge=100,
        le=200_000,
    )

    @field_validator("url")
    @classmethod
    def _valid_yt_url(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("url cannot be empty")
        if _extract_video_id(v) is None:
            raise ValueError("not a recognised YouTube URL")
        return v


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


@mcp.custom_route("/", methods=["GET"])
@mcp.custom_route("/health", methods=["GET"])
async def health_check(request: Request) -> JSONResponse:
    return JSONResponse(
        {
            "status": "healthy",
            "service": "summarize-mcp",
            "version": "0.1.0",
            "transport": "http",
        }
    )


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool
async def summarize_url(params: SummarizeInput) -> str:
    """Fetch a URL, strip HTML tags and scripts, and return clean readable text.

    Returns url, title, text (up to max_length characters), and length.
    The agent is responsible for summarizing the returned text.
    """
    try:
        async with httpx.AsyncClient(timeout=10.0, headers=_HEADERS, follow_redirects=True) as client:
            r = await client.get(params.url)
            r.raise_for_status()
            raw = r.text
    except Exception as exc:
        return json.dumps({"error": _handle_error(exc)})

    title = _extract_title(raw)
    text = _strip_html(raw)
    if len(text) > params.max_length:
        text = text[: params.max_length] + "..."

    return json.dumps(
        {
            "url": params.url,
            "title": title,
            "text": text,
            "length": len(text),
        }
    )


@mcp.tool
async def get_youtube_transcript(params: YoutubeInput) -> str:
    """Get the transcript of a YouTube video.

    Tries youtube_transcript_api first (if installed), then falls back to
    scraping the YouTube innertube API and watch page.
    Returns video_id, title, transcript (up to max_length characters), and language.
    """
    video_id = _extract_video_id(params.url)
    if not video_id:
        return json.dumps({"error": "Invalid YouTube URL"})

    transcript = ""
    title = ""

    try:
        items = await asyncio.to_thread(_get_transcript_via_api, video_id, params.language)
        if items:
            parts = []
            for item in items:
                txt = item.get("text", "") if isinstance(item, dict) else getattr(item, "text", "") or str(item)
                if txt:
                    parts.append(txt)
            transcript = " ".join(parts)
    except ImportError:
        pass

    if not transcript:
        transcript, title = await _fetch_transcript_fallback(video_id, params.language)

    if not transcript:
        return json.dumps({"error": "no transcript available for this video"})

    if len(transcript) > params.max_length:
        transcript = transcript[: params.max_length] + "..."

    return json.dumps(
        {
            "video_id": video_id,
            "title": title or None,
            "transcript": transcript,
            "language": params.language,
        }
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Summarize MCP server (fastmcp)")
    parser.add_argument(
        "--transport",
        choices=["http", "stdio"],
        default=os.environ.get("TRANSPORT", "stdio"),
        help="Transport: 'http' (streamable HTTP, default) or 'stdio'",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("HOST", "0.0.0.0"),
        help="Bind host for HTTP transport (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=os.environ.get("PORT", 8000),
        help="Bind port for HTTP transport (default: 8000)",
    )
    args = parser.parse_args()

    if args.transport == "http":
        print(f"Starting summarize_mcp · HTTP transport · {args.host}:{args.port}/mcp")
        app = mcp.http_app()
        app = CORSMiddleware(app=app, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
        uvicorn.run(app, host=args.host, port=args.port)
    else:
        mcp.run(transport="stdio")
