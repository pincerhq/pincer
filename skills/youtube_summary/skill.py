"""YouTube summary skill: get video transcripts."""

import contextlib
import json
import re
import urllib.error
import urllib.request


def _extract_video_id(url: str) -> str | None:
    """Extract video ID from YouTube URL. Returns None for invalid."""
    if not url or not isinstance(url, str):
        return None
    url = url.strip()
    patterns = [
        r"(?:youtube\.com/watch\?v=)([a-zA-Z0-9_-]{11})",
        r"(?:youtu\.be/)([a-zA-Z0-9_-]{11})",
        r"(?:youtube\.com/embed/)([a-zA-Z0-9_-]{11})",
        r"(?:youtube\.com/v/)([a-zA-Z0-9_-]{11})",
        r"(?:youtube\.com/shorts/)([a-zA-Z0-9_-]{11})",
    ]
    for pat in patterns:
        m = re.search(pat, url)
        if m:
            return m.group(1)
    return None


def _fetch_transcript_fallback(video_id: str, language: str) -> tuple[str, str]:
    """Fallback: scrape captions from watch page or innertube. Returns (transcript, title)."""
    transcript = ""
    title = ""

    def try_innertube() -> bool:
        nonlocal transcript, title
        try:
            context = {
                "context": {
                    "client": {"clientName": "WEB", "clientVersion": "2.20231219.01.00"},
                }
            }
            body = json.dumps({"videoId": video_id, "context": context}).encode("utf-8")
            req = urllib.request.Request(
                "https://www.youtube.com/youtubei/v1/player",
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; rv:91.0) Gecko/20100101 Firefox/91.0",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
                playability = data.get("playabilityStatus", {})
                if playability.get("status") == "ERROR":
                    return False
                video_details = data.get("videoDetails", {})
                title = video_details.get("title", "")
                captions = data.get("captions", {}).get("playerCaptionsTracklistRenderer", {})
                tracks = captions.get("captionTracks", [])
                base_url = None
                for t in tracks:
                    if t.get("languageCode", "").startswith(language[:2]):
                        base_url = t.get("baseUrl")
                        break
                if not base_url and tracks:
                    base_url = tracks[0].get("baseUrl")
                if base_url:
                    cap_req = urllib.request.Request(
                        base_url + "&fmt=json3",
                        headers={"User-Agent": "Pincer/1.0"},
                    )
                    with urllib.request.urlopen(cap_req, timeout=10) as cap_resp:
                        cap_data = json.loads(cap_resp.read().decode())
                        events = cap_data.get("events", [])
                        parts = []
                        for ev in events:
                            for seg in ev.get("segs", []):
                                txt = seg.get("utf8", "").strip()
                                if txt:
                                    parts.append(txt)
                        transcript = " ".join(parts)
                    return True
        except Exception:
            pass
        return False

    def try_watch_page() -> bool:
        nonlocal transcript, title
        try:
            url = f"https://www.youtube.com/watch?v={video_id}"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; Pincer/1.0)"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                html = resp.read().decode(errors="replace")
            m = re.search(r"ytInitialPlayerResponse\s*=\s*(\{.+?\});\s*var", html)
            if not m:
                m = re.search(r"ytInitialPlayerResponse\s*=\s*(\{.+?\});", html, re.DOTALL)
            if m:
                data = json.loads(m.group(1))
                video_details = data.get("videoDetails", {})
                title = video_details.get("title", "")
                captions = data.get("captions", {}).get("playerCaptionsTracklistRenderer", {})
                tracks = captions.get("captionTracks", [])
                base_url = None
                for t in tracks:
                    if t.get("languageCode", "").startswith(language[:2]):
                        base_url = t.get("baseUrl")
                        break
                if not base_url and tracks:
                    base_url = tracks[0].get("baseUrl")
                if base_url:
                    cap_req = urllib.request.Request(
                        base_url + "&fmt=json3",
                        headers={"User-Agent": "Pincer/1.0"},
                    )
                    with urllib.request.urlopen(cap_req, timeout=10) as cap_resp:
                        cap_data = json.loads(cap_resp.read().decode())
                        events = cap_data.get("events", [])
                        parts = []
                        for ev in events:
                            for seg in ev.get("segs", []):
                                txt = seg.get("utf8", "").strip()
                                if txt:
                                    parts.append(txt)
                        transcript = " ".join(parts)
                    return True
        except Exception:
            pass
        return False

    try_innertube() or try_watch_page()
    return (transcript, title)


def get_transcript(url: str, language: str = "en") -> dict:
    """Get transcript for a YouTube video. Uses youtube_transcript_api if available, else fallback."""
    video_id = _extract_video_id(url)
    if not video_id:
        return {"error": "Invalid YouTube URL"}

    transcript = ""
    title = ""

    try:
        from youtube_transcript_api import YouTubeTranscriptApi

        transcript_list = None
        if hasattr(YouTubeTranscriptApi, "get_transcript"):
            with contextlib.suppress(Exception):
                transcript_list = YouTubeTranscriptApi.get_transcript(video_id)
        if transcript_list is None:
            try:
                api = YouTubeTranscriptApi()
                fetched = api.fetch(video_id, languages=[language[:2]])
                transcript_list = list(fetched) if hasattr(fetched, "__iter__") else []
            except Exception:
                transcript_list = []

        if transcript_list:
            parts = []
            for item in transcript_list:
                txt = item.get("text", "") if isinstance(item, dict) else getattr(item, "text", "") or str(item)
                if txt:
                    parts.append(txt)
            transcript = " ".join(parts)
    except ImportError:
        pass

    if not transcript:
        transcript, title = _fetch_transcript_fallback(video_id, language)

    if len(transcript) > 8000:
        transcript = transcript[:8000] + "..."

    return {
        "video_id": video_id,
        "title": title or None,
        "transcript": transcript,
        "language": language,
    }
