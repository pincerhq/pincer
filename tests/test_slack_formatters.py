"""Tests for Slack mrkdwn formatter and Block Kit helpers."""

from __future__ import annotations

from pincer.channels.slack_formatters import build_approval_blocks, markdown_to_mrkdwn


class TestMarkdownToMrkdwn:
    def test_bold_conversion(self) -> None:
        assert markdown_to_mrkdwn("**bold text**") == "*bold text*"

    def test_header_h1(self) -> None:
        result = markdown_to_mrkdwn("# Hello World")
        assert result == "*Hello World*"

    def test_header_h2(self) -> None:
        result = markdown_to_mrkdwn("## Section Title")
        assert result == "*Section Title*"

    def test_header_h3(self) -> None:
        result = markdown_to_mrkdwn("### Deep Section")
        assert result == "*Deep Section*"

    def test_link_conversion(self) -> None:
        result = markdown_to_mrkdwn("[OpenAI](https://openai.com)")
        assert result == "<https://openai.com|OpenAI>"

    def test_bullet_dash(self) -> None:
        result = markdown_to_mrkdwn("- item one")
        assert result == "• item one"

    def test_bullet_asterisk(self) -> None:
        result = markdown_to_mrkdwn("* item two")
        assert result == "• item two"

    def test_code_block_preserved(self) -> None:
        code = "```python\nprint('hello')\n```"
        result = markdown_to_mrkdwn(code)
        # Language hint stripped, content preserved
        assert "print('hello')" in result
        assert result.startswith("```")

    def test_code_block_no_language(self) -> None:
        code = "```\nsome code\n```"
        result = markdown_to_mrkdwn(code)
        assert "some code" in result

    def test_inline_code_preserved(self) -> None:
        result = markdown_to_mrkdwn("Use `print()` to output")
        assert "`print()`" in result

    def test_mixed_content(self) -> None:
        text = "## Summary\n\n**Key points:**\n- Point 1\n- Point 2\n\n[More info](https://example.com)"
        result = markdown_to_mrkdwn(text)
        assert "*Summary*" in result
        assert "*Key points:*" in result
        assert "• Point 1" in result
        assert "<https://example.com|More info>" in result

    def test_plain_text_unchanged(self) -> None:
        text = "Just a simple sentence with no markdown."
        assert markdown_to_mrkdwn(text) == text

    def test_no_double_conversion(self) -> None:
        # Already-mrkdwn bold (*text*) should not be double-wrapped
        result = markdown_to_mrkdwn("**bold**")
        assert result.count("*") == 2  # exactly *bold*, not ***bold***


class TestBuildApprovalBlocks:
    def test_returns_list(self) -> None:
        blocks = build_approval_blocks("shell_exec", {"command": "ls"}, "approval-123")
        assert isinstance(blocks, list)

    def test_two_blocks(self) -> None:
        blocks = build_approval_blocks("shell_exec", {"command": "ls"}, "approval-123")
        assert len(blocks) == 2

    def test_section_block_has_tool_name(self) -> None:
        blocks = build_approval_blocks("email_send", {"to": "alice@example.com"}, "id-1")
        section = blocks[0]
        assert section["type"] == "section"
        assert "email_send" in section["text"]["text"]

    def test_actions_block_has_buttons(self) -> None:
        blocks = build_approval_blocks("shell_exec", {}, "id-2")
        actions = blocks[1]
        assert actions["type"] == "actions"
        elements = actions["elements"]
        assert len(elements) == 2

    def test_approve_button(self) -> None:
        blocks = build_approval_blocks("shell_exec", {}, "approval-xyz")
        approve_btn = blocks[1]["elements"][0]
        assert approve_btn["action_id"] == "pincer_approve"
        assert approve_btn["value"] == "approval-xyz"
        assert approve_btn["style"] == "primary"

    def test_deny_button(self) -> None:
        blocks = build_approval_blocks("shell_exec", {}, "approval-xyz")
        deny_btn = blocks[1]["elements"][1]
        assert deny_btn["action_id"] == "pincer_deny"
        assert deny_btn["value"] == "approval-xyz"
        assert deny_btn["style"] == "danger"

    def test_args_included_in_section(self) -> None:
        blocks = build_approval_blocks("email_send", {"to": "bob@test.com", "subject": "Hi"}, "id-3")
        section_text = blocks[0]["text"]["text"]
        assert "bob@test.com" in section_text
        assert "subject" in section_text

    def test_long_args_truncated(self) -> None:
        long_args = {"data": "x" * 3000}
        blocks = build_approval_blocks("tool", long_args, "id-4")
        section_text = blocks[0]["text"]["text"]
        assert "truncated" in section_text
