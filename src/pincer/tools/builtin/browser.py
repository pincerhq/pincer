"""
Browser automation tool using Playwright.

Provides browsing, screenshotting, and interaction capabilities.
Auto-installs Chromium on first use if not already present.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_browser: Any = None
_playwright: Any = None
_install_attempted = False

MAX_TEXT_LENGTH = 6000


async def _ensure_browser() -> Any:
    """Lazily launch a shared browser instance, installing Chromium if needed."""
    global _browser, _playwright, _install_attempted

    if _browser and _browser.is_connected():
        return _browser

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return None

    if not _install_attempted:
        _install_attempted = True
        try:
            subprocess.run(
                [sys.executable, "-m", "playwright", "install", "chromium"],
                check=True,
                capture_output=True,
                timeout=120,
            )
            logger.info("Playwright Chromium installed successfully")
        except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
            logger.warning("Playwright browser install failed; assuming already installed")

    _playwright = await async_playwright().start()
    _browser = await _playwright.chromium.launch(headless=True)
    return _browser


def _extract_text(html: str) -> str:
    """Extract readable text from HTML, stripping tags and excessive whitespace."""
    text = re.sub(r"<script[^>]*>[\s\S]*?</script>", "", html, flags=re.IGNORECASE)
    text = re.sub(r"<style[^>]*>[\s\S]*?</style>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"&#\d+;", "", text)
    text = re.sub(r"&\w+;", "", text)
    text = re.sub(r"\s+", " ", text)
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    return "\n".join(lines).strip()


async def browse(url: str) -> str:
    """
    Navigate to a URL and return the page's readable text content.

    url: The full URL to navigate to (must start with http:// or https://)
    """
    browser = await _ensure_browser()
    if browser is None:
        return (
            "Error: Playwright is not installed. "
            "Install with: pip install 'pincer-agent[browser]' && playwright install chromium"
        )

    page = await browser.new_page()
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(1)

        title = await page.title()
        html = await page.content()
        text = _extract_text(html)

        if len(text) > MAX_TEXT_LENGTH:
            text = text[:MAX_TEXT_LENGTH] + "\n...[content truncated]"

        header = f"Title: {title}\nURL: {url}\n\n"
        return header + text
    except Exception as e:
        return f"Error browsing {url}: {type(e).__name__}: {e}"
    finally:
        await page.close()


async def screenshot(url: str, context: dict[str, Any] | None = None) -> str:
    """
    Navigate to a URL and take a screenshot, saving it to the workspace.

    url: The full URL to screenshot (must start with http:// or https://)
    """
    browser = await _ensure_browser()
    if browser is None:
        return (
            "Error: Playwright is not installed. "
            "Install with: pip install 'pincer-agent[browser]' && playwright install chromium"
        )

    page = await browser.new_page(viewport={"width": 1280, "height": 900})
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(2)

        screenshot_bytes = await page.screenshot(full_page=False)

        workspace = Path.home() / ".pincer" / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        import time

        filename = f"screenshot_{int(time.time())}.png"
        filepath = workspace / filename
        filepath.write_bytes(screenshot_bytes)

        b64 = base64.b64encode(screenshot_bytes).decode()
        return (
            f"Screenshot saved to: {filepath}\n"
            f"Size: {len(screenshot_bytes)} bytes\n"
            f"Base64 preview (first 100 chars): {b64[:100]}..."
        )
    except Exception as e:
        return f"Error screenshotting {url}: {type(e).__name__}: {e}"
    finally:
        await page.close()


async def close_browser() -> None:
    """Shut down the shared browser instance."""
    global _browser, _playwright
    if _browser:
        await _browser.close()
        _browser = None
    if _playwright:
        await _playwright.stop()
        _playwright = None
