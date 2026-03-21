"""Translate skill: LibreTranslate API."""

import json
import urllib.error
import urllib.request


def translate_text(text: str, target: str, source: str = "auto") -> dict:
    """Translate text via LibreTranslate."""
    if not text or not target:
        return {"error": "text and target are required"}

    url = "https://libretranslate.com/translate"
    body = json.dumps(
        {
            "q": text,
            "source": source,
            "target": target,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            translated = data.get("translatedText", "")
            detected = data.get("detectedLanguage", {}).get("language") if source == "auto" else source
            return {
                "translated_text": translated,
                "source_language": detected or source,
                "target_language": target,
            }
    except urllib.error.HTTPError as e:
        body_err = e.read().decode() if e.fp else ""
        return {"error": f"API error: {e.code} {body_err[:200]}"}
    except (urllib.error.URLError, json.JSONDecodeError) as e:
        return {"error": f"Network or parse error: {e}"}


def list_languages() -> dict:
    """List supported languages from LibreTranslate."""
    url = "https://libretranslate.com/languages"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            languages = [
                {"code": item.get("code", ""), "name": item.get("name", "")} for item in data if isinstance(item, dict)
            ]
            return {"languages": languages}
    except urllib.error.HTTPError as e:
        body_err = e.read().decode() if e.fp else ""
        return {"error": f"API error: {e.code} {body_err[:200]}"}
    except (urllib.error.URLError, json.JSONDecodeError) as e:
        return {"error": f"Network or parse error: {e}"}
