"""Tests for the Telegram Markdown -> HTML formatter."""

from __future__ import annotations

from pincer.channels.telegram_formatters import markdown_to_telegram_html


class TestMarkdownToTelegramHtml:
    def test_bold_conversion(self) -> None:
        assert markdown_to_telegram_html("**bold text**") == "<b>bold text</b>"

    def test_header_h1(self) -> None:
        assert markdown_to_telegram_html("# Hello World") == "<b>Hello World</b>"

    def test_header_h2(self) -> None:
        assert markdown_to_telegram_html("## Section Title") == "<b>Section Title</b>"

    def test_header_h3(self) -> None:
        assert markdown_to_telegram_html("### Deep Section") == "<b>Deep Section</b>"

    def test_italic_asterisk(self) -> None:
        assert markdown_to_telegram_html("*italic*") == "<i>italic</i>"

    def test_italic_underscore(self) -> None:
        assert markdown_to_telegram_html("_italic_") == "<i>italic</i>"

    def test_strikethrough(self) -> None:
        assert markdown_to_telegram_html("~~gone~~") == "<s>gone</s>"

    def test_link_conversion(self) -> None:
        result = markdown_to_telegram_html("[OpenAI](https://openai.com)")
        assert result == '<a href="https://openai.com">OpenAI</a>'

    def test_bullet_dash(self) -> None:
        assert markdown_to_telegram_html("- item one") == "• item one"

    def test_bullet_asterisk(self) -> None:
        assert markdown_to_telegram_html("* item two") == "• item two"

    def test_code_block_with_language(self) -> None:
        code = "```python\nprint(hello)\n```"
        result = markdown_to_telegram_html(code)
        assert result == '<pre><code class="language-python">print(hello)\n</code></pre>'

    def test_code_block_no_language(self) -> None:
        code = "```\nsome code\n```"
        result = markdown_to_telegram_html(code)
        assert result == "<pre><code>some code\n</code></pre>"

    def test_inline_code_preserved(self) -> None:
        result = markdown_to_telegram_html("Use `print()` to output")
        assert result == "Use <code>print()</code> to output"

    def test_escapes_raw_ampersand(self) -> None:
        assert markdown_to_telegram_html("Tom & Jerry") == "Tom &amp; Jerry"

    def test_escapes_raw_angle_brackets(self) -> None:
        assert markdown_to_telegram_html("if x < y > z") == "if x &lt; y &gt; z"

    def test_code_content_escaped(self) -> None:
        result = markdown_to_telegram_html("`a < b`")
        assert result == "<code>a &lt; b</code>"

    def test_mixed_content(self) -> None:
        text = "## Summary\n\n**Key points:**\n- Point 1\n- Point 2\n\n[More info](https://example.com)"
        result = markdown_to_telegram_html(text)
        assert "<b>Summary</b>" in result
        assert "<b>Key points:</b>" in result
        assert "• Point 1" in result
        assert '<a href="https://example.com">More info</a>' in result

    def test_plain_text_unchanged(self) -> None:
        text = "Just a simple sentence with no markdown."
        assert markdown_to_telegram_html(text) == text

    def test_incomplete_bold_span_stays_literal_no_unclosed_tag(self) -> None:
        # Simulates a chunk boundary cutting a **bold** span mid-way.
        result = markdown_to_telegram_html("this is **unterminated bold")
        assert "<b>" not in result
        assert "**unterminated bold" in result

    def test_incomplete_code_fence_stays_literal_no_unclosed_tag(self) -> None:
        result = markdown_to_telegram_html("```python\nprint('hi')")
        assert "<pre>" not in result
        assert "<code>" not in result
