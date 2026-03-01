"""Summarize URL skill: fetch and clean web page text."""

import html
import re
import urllib.error
import urllib.request


def _strip_html(html_text: str) -> str:
    """Remove script/style, then all tags, decode entities, collapse whitespace."""
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
        raw = m.group(1)
        return _strip_html(f"<x>{raw}</x>")[:500]
    return ""


def summarize_url(url: str, max_length: int = 5000) -> dict:
    """Fetch URL, strip HTML, return clean text. Agent does the summarization."""
    if not url or not isinstance(url, str):
        return {"error": "url is required"}
    url = url.strip()
    if not url.lower().startswith(("http://", "https://")):
        return {"error": "URL must start with http:// or https://"}

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Pincer/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode(errors="replace")
    except urllib.error.URLError as e:
        return {"error": f"Failed to fetch: {e}"}
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP error: {e.code}"}

    title = _extract_title(raw)
    text = _strip_html(raw)
    if len(text) > max_length:
        text = text[:max_length] + "..."

    return {
        "url": url,
        "title": title,
        "text": text,
        "length": len(text),
    }
