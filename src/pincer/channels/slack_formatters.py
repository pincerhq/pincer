"""
Formatting utilities for the Slack channel.

Converts standard Markdown (used internally by the agent) to Slack's
mrkdwn format, and builds Block Kit payloads for approval buttons.
"""

from __future__ import annotations

import re
from typing import Any


def markdown_to_mrkdwn(text: str) -> str:
    """Convert standard Markdown to Slack mrkdwn format.

    Key differences from standard Markdown:
    - Bold: **text** → *text*
    - Italic: *text* → _text_ (only bare single-asterisk italics)
    - Code blocks: ```lang\\n...``` → ```\\n...``` (strip language hint)
    - Headers: # Title → *Title* (bold, Slack has no native headers)
    - Links: [text](url) → <url|text>
    - Lists: - item → • item
    """
    # Preserve code blocks first (so we don't mangle content inside them)
    code_blocks: list[str] = []

    def save_code(m: re.Match[str]) -> str:
        code_blocks.append(m.group(0))
        return f"\x00CODE{len(code_blocks) - 1}\x00"

    text = re.sub(r"```[\s\S]*?```", save_code, text)

    # Preserve inline code
    inline_codes: list[str] = []

    def save_inline(m: re.Match[str]) -> str:
        inline_codes.append(m.group(0))
        return f"\x00INLINE{len(inline_codes) - 1}\x00"

    text = re.sub(r"`[^`\n]+`", save_inline, text)

    # Bold: **text** → placeholder (must happen before italic/header to avoid double-conversion)
    bold_items: list[str] = []

    def save_bold(m: re.Match[str]) -> str:
        bold_items.append(m.group(1))
        return f"\x00BOLD{len(bold_items) - 1}\x00"

    text = re.sub(r"\*\*(.+?)\*\*", save_bold, text)

    # Headers → bold placeholder  (# Title → *Title*)
    def save_header(m: re.Match[str]) -> str:
        bold_items.append(m.group(1))
        return f"\x00BOLD{len(bold_items) - 1}\x00"

    text = re.sub(r"^#{1,6}\s+(.+)$", save_header, text, flags=re.MULTILINE)

    # Italic: bare *text* (remaining single-asterisk) → _text_
    text = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"_\1_", text)

    # Links: [text](url) → <url|text>
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"<\2|\1>", text)

    # Unordered bullet lists: - item or * item → • item
    text = re.sub(r"^[ \t]*[-*]\s+", "• ", text, flags=re.MULTILINE)

    # Restore bold placeholders → *text*
    for i, content in enumerate(bold_items):
        text = text.replace(f"\x00BOLD{i}\x00", f"*{content}*")

    # Restore code blocks (strip language hint from opening fence)
    for i, block in enumerate(code_blocks):
        cleaned = re.sub(r"^```\w*\n", "```\n", block)
        text = text.replace(f"\x00CODE{i}\x00", cleaned)

    # Restore inline code
    for i, code in enumerate(inline_codes):
        text = text.replace(f"\x00INLINE{i}\x00", code)

    return text


def build_approval_blocks(
    tool_name: str,
    tool_args: dict[str, Any],
    approval_id: str,
) -> list[dict[str, Any]]:
    """Build Block Kit blocks for a tool-call approval request.

    Layout:
    ┌──────────────────────────────────────┐
    │ 🔐 Approval Required                 │
    │ Tool: shell_exec                     │
    │ Args:                                │
    │   command: rm -rf /tmp/foo           │
    │                                      │
    │ [✅ Approve]  [❌ Deny]              │
    └──────────────────────────────────────┘
    """
    args_lines = "\n".join(f"  {k}: {v}" for k, v in tool_args.items())
    if len(args_lines) > 1900:
        args_lines = args_lines[:1900] + "\n  ... (truncated)"

    description = f"🔐 *Approval Required*\n\n*Tool:* `{tool_name}`"
    if args_lines:
        description += f"\n*Args:*\n```{args_lines}```"

    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": description,
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "✅ Approve", "emoji": True},
                    "style": "primary",
                    "action_id": "pincer_approve",
                    "value": approval_id,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "❌ Deny", "emoji": True},
                    "style": "danger",
                    "action_id": "pincer_deny",
                    "value": approval_id,
                },
            ],
        },
    ]
