"""News skill: headlines, search, and RSS feeds."""

import json
import os
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET


def _truncate(s: str, max_len: int = 200) -> str:
    if not s:
        return ""
    s = (s or "").strip()
    if len(s) <= max_len:
        return s
    return s[: max_len - 3] + "..."


def get_headlines(
    category: str = "general",
    country: str = "us",
    count: int = 5,
) -> dict:
    """Get top headlines from NewsAPI."""
    api_key = os.environ.get("NEWSAPI_KEY") or os.environ.get("PINCER_NEWSAPI_KEY")
    if not api_key:
        return {"error": "NewsAPI requires NEWSAPI_KEY or PINCER_NEWSAPI_KEY to be set"}

    params = urllib.parse.urlencode({
        "country": country,
        "category": category,
        "pageSize": min(count, 100),
        "apiKey": api_key,
    })
    url = f"https://newsapi.org/v2/top-headlines?{params}"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            if data.get("status") != "ok":
                return {"error": data.get("message", "NewsAPI error")}
            articles = []
            for a in data.get("articles", [])[:count]:
                articles.append({
                    "title": a.get("title", ""),
                    "source": a.get("source", {}).get("name", ""),
                    "description": _truncate(a.get("description") or "", 200),
                    "url": a.get("url", ""),
                })
            return {"articles": articles}
    except urllib.error.HTTPError as e:
        body = e.read().decode() if e.fp else ""
        return {"error": f"API error: {e.code} {body[:200]}"}
    except (urllib.error.URLError, json.JSONDecodeError) as e:
        return {"error": f"Network or parse error: {e}"}


def search_news(query: str, count: int = 5) -> dict:
    """Search news via NewsAPI everything endpoint."""
    api_key = os.environ.get("NEWSAPI_KEY") or os.environ.get("PINCER_NEWSAPI_KEY")
    if not api_key:
        return {"error": "NewsAPI requires NEWSAPI_KEY or PINCER_NEWSAPI_KEY to be set"}

    params = urllib.parse.urlencode({
        "q": query,
        "pageSize": min(count, 100),
        "apiKey": api_key,
    })
    url = f"https://newsapi.org/v2/everything?{params}"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            if data.get("status") != "ok":
                return {"error": data.get("message", "NewsAPI error")}
            articles = []
            for a in data.get("articles", [])[:count]:
                articles.append({
                    "title": a.get("title", ""),
                    "source": a.get("source", {}).get("name", ""),
                    "description": _truncate(a.get("description") or "", 200),
                    "url": a.get("url", ""),
                })
            return {"articles": articles}
    except urllib.error.HTTPError as e:
        body = e.read().decode() if e.fp else ""
        return {"error": f"API error: {e.code} {body[:200]}"}
    except (urllib.error.URLError, json.JSONDecodeError) as e:
        return {"error": f"Network or parse error: {e}"}


def _text(elem: ET.Element | None, tag: str, default: str = "") -> str:
    if elem is None:
        return default
    child = elem.find(tag)
    if child is not None and child.text:
        return (child.text or "").strip()
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    for prefix, uri in ns.items():
        child = elem.find(f"{{{uri}}}{tag}")
        if child is not None and child.text:
            return (child.text or "").strip()
    return default


def read_rss(url: str, count: int = 5) -> dict:
    """Parse RSS 2.0 or Atom feed. Uses xml.etree.ElementTree only."""
    if not url or not url.strip():
        return {"error": "URL is required"}
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Pincer/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode(errors="replace")
    except urllib.error.URLError as e:
        return {"error": f"Failed to fetch feed: {e}"}
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP error: {e.code}"}

    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        return {"error": f"Invalid XML: {e}"}

    feed_title = ""
    items = []

    def ns(tag: str) -> str:
        for prefix, uri in [
            ("atom", "http://www.w3.org/2005/Atom"),
            ("dc", "http://purl.org/dc/elements/1.1/"),
            ("media", "http://search.yahoo.com/mrss/"),
        ]:
            if tag.startswith(prefix + ":"):
                return "{" + uri + "}" + tag.split(":", 1)[1]
        return tag

    is_atom = "Atom" in root.tag or root.tag.endswith("}feed")
    if "rss" in root.tag or root.tag == "rss" or is_atom:
        if not is_atom:
            channel = root.find("channel")
            if channel is not None:
                feed_title = _text(channel, "title")
        else:
            t = root.find("{http://www.w3.org/2005/Atom}title")
            if t is not None and t.text:
                feed_title = (t.text or "").strip()

        if not is_atom:
            for item in root.iter("item"):
                if len(items) >= count:
                    break
                title = _text(item, "title")
                link = _text(item, "link")
                desc = _text(item, "description") or _text(item, "content") or _text(item, ns("dc:description"))
                items.append({
                    "title": title,
                    "link": link,
                    "description": _truncate(desc, 200),
                })
        else:
            for entry in root.iter("{http://www.w3.org/2005/Atom}entry"):
                if len(items) >= count:
                    break
                title = _text(entry, "{http://www.w3.org/2005/Atom}title")
                link_el = entry.find("{http://www.w3.org/2005/Atom}link[@rel='alternate']")
                if link_el is None:
                    link_el = entry.find("{http://www.w3.org/2005/Atom}link")
                link = link_el.get("href", "") if link_el is not None else ""
                desc = _text(entry, "{http://www.w3.org/2005/Atom}summary") or _text(entry, "{http://www.w3.org/2005/Atom}content")
                items.append({
                    "title": title,
                    "link": link,
                    "description": _truncate(desc, 200),
                })

    return {"feed_title": feed_title, "items": items}
