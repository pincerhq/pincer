"""
Formatting utilities for the Telegram channel.

Converts standard Markdown (used internally by the agent) to Telegram's
HTML parse mode. HTML is used instead of Telegram's Markdown/MarkdownV2
modes because it only requires escaping three characters (&, <, >) instead
of MarkdownV2's ~18 reserved characters, so ordinary LLM prose is far less
likely to trip a parse error.
"""

from __future__ import annotations

import html
import re


def markdown_to_telegram_html(text: str) -> str:
    """Convert standard Markdown to Telegram HTML parse-mode syntax.

    Key differences from standard Markdown:
    - Bold: **text** -> <b>text</b>
    - Italic: *text* / _text_ -> <i>text</i>
    - Strikethrough: ~~text~~ -> <s>text</s>
    - Code blocks: ```lang\\n...``` -> <pre><code class="language-lang">...</code></pre>
    - Inline code: `text` -> <code>text</code>
    - Headers: # Title -> <b>Title</b> (Telegram HTML has no headers)
    - Links: [text](url) -> <a href="url">text</a>
    - Lists: - item -> • item

    Any incomplete markdown span (e.g. a chunk boundary cutting a **bold
    off before its closing **) simply fails to match and is left as
    escaped literal text, so this never produces an unclosed HTML tag.
    """
    # Preserve fenced code blocks first (so we don't mangle content inside them)
    code_blocks: list[str] = []

    def save_code(m: re.Match[str]) -> str:
        lang = m.group(1)
        body = m.group(2)
        cls = f' class="language-{lang}"' if lang else ""
        code_blocks.append(f"<pre><code{cls}>{html.escape(body)}</code></pre>")
        return f"\x00CODE{len(code_blocks) - 1}\x00"

    text = re.sub(r"```(\w*)\n?([\s\S]*?)```", save_code, text)

    # Preserve inline code
    inline_codes: list[str] = []

    def save_inline(m: re.Match[str]) -> str:
        inline_codes.append(f"<code>{html.escape(m.group(1))}</code>")
        return f"\x00INLINE{len(inline_codes) - 1}\x00"

    text = re.sub(r"`([^`\n]+)`", save_inline, text)

    # Escape everything else so raw &, <, > can't be mistaken for HTML,
    # and stray quotes can't break attribute values we add below.
    text = html.escape(text, quote=True)

    # Links: [text](url) -> <a href="url">text</a>
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', text)

    # Bold: **text** -> <b>text</b> (before italic/header to avoid double-conversion)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)

    # Headers -> bold (# Title -> <b>Title</b>)
    text = re.sub(r"^#{1,6}\s+(.+)$", r"<b>\1</b>", text, flags=re.MULTILINE)

    # Italic: bare *text* or _text_ (remaining single-marker) -> <i>text</i>
    text = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"<i>\1</i>", text)
    text = re.sub(r"(?<!_)_(?!_)(.+?)(?<!_)_(?!_)", r"<i>\1</i>", text)

    # Strikethrough: ~~text~~ -> <s>text</s>
    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text)

    # Unordered bullet lists: - item or * item -> • item
    text = re.sub(r"^[ \t]*[-*]\s+", "• ", text, flags=re.MULTILINE)

    # Restore code blocks and inline code
    for i, block in enumerate(code_blocks):
        text = text.replace(f"\x00CODE{i}\x00", block)
    for i, code in enumerate(inline_codes):
        text = text.replace(f"\x00INLINE{i}\x00", code)

    return text
