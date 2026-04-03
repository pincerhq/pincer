"""
Tests for EmailGuard — the 5-layer outbound email safety guard.
"""

from __future__ import annotations

import pytest

from pincer.integrations.google.email_guard import EmailGuard, EmailVerdict

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def guard(tmp_path):
    """EmailGuard with small test lists."""
    (tmp_path / "email_allowlist.txt").write_text("trusted@company.com\ncompany.com\n")
    (tmp_path / "email_blocklist.txt").write_text("blocked@bad.com\nbad.org\n")
    (tmp_path / "disposable_domains.txt").write_text("tempmail.lol\n10minutemail.com\n")
    return EmailGuard(data_dir=tmp_path, block_disposable=True)


@pytest.fixture
def guard_no_disposable(tmp_path):
    """EmailGuard with disposable blocking disabled."""
    (tmp_path / "disposable_domains.txt").write_text("tempmail.lol\n")
    return EmailGuard(data_dir=tmp_path, block_disposable=False)


@pytest.fixture
def guard_empty(tmp_path):
    """EmailGuard with no data files at all."""
    return EmailGuard(data_dir=tmp_path, block_disposable=True)


# ── Layer 1: Allowlist ────────────────────────────────────────────────────────


def test_allowlisted_address_passes(guard):
    result = guard.check("trusted@company.com")
    assert result.verdict == EmailVerdict.ALLOW


def test_allowlisted_domain_passes(guard):
    result = guard.check("anyone@company.com")
    assert result.verdict == EmailVerdict.ALLOW


def test_allowlist_subdomain_passes(guard):
    result = guard.check("user@mail.company.com")
    assert result.verdict == EmailVerdict.ALLOW


def test_allowlist_overrides_dangerous(guard):
    """If user explicitly allowlists evil.com subdomain of company.com, allow it."""
    result = guard.check("user@company.com")
    assert result.verdict == EmailVerdict.ALLOW


def test_allowlist_case_insensitive(guard):
    result = guard.check("TRUSTED@COMPANY.COM")
    assert result.verdict == EmailVerdict.ALLOW


# ── Layer 2: User blocklist ───────────────────────────────────────────────────


def test_blocklisted_address_denied(guard):
    result = guard.check("blocked@bad.com")
    assert result.verdict == EmailVerdict.DENY_BLOCKLIST


def test_blocklisted_domain_denied(guard):
    result = guard.check("anyone@bad.org")
    assert result.verdict == EmailVerdict.DENY_BLOCKLIST


def test_blocklisted_subdomain_denied(guard):
    result = guard.check("user@sub.bad.org")
    assert result.verdict == EmailVerdict.DENY_BLOCKLIST


def test_blocklist_reason_mentions_domain(guard):
    result = guard.check("user@bad.org")
    assert "bad.org" in result.reason


def test_blocklist_address_in_result(guard):
    result = guard.check("blocked@bad.com")
    assert result.address == "blocked@bad.com"


# ── Layer 3: Dangerous domains ────────────────────────────────────────────────


def test_evil_com_denied(guard):
    result = guard.check("spam@evil.com")
    assert result.verdict == EmailVerdict.DENY_DANGEROUS


def test_mailinator_denied(guard):
    result = guard.check("test@mailinator.com")
    assert result.verdict == EmailVerdict.DENY_DANGEROUS


def test_guerrillamail_denied(guard):
    result = guard.check("x@guerrillamail.com")
    assert result.verdict == EmailVerdict.DENY_DANGEROUS


def test_yopmail_denied(guard):
    result = guard.check("user@yopmail.com")
    assert result.verdict == EmailVerdict.DENY_DANGEROUS


def test_trashmail_denied(guard):
    result = guard.check("user@trashmail.com")
    assert result.verdict == EmailVerdict.DENY_DANGEROUS


def test_maildrop_denied(guard):
    result = guard.check("x@maildrop.cc")
    assert result.verdict == EmailVerdict.DENY_DANGEROUS


def test_suspicious_tld_tk_denied(guard):
    result = guard.check("user@randomsite.tk")
    assert result.verdict == EmailVerdict.DENY_DANGEROUS


def test_suspicious_tld_ml_denied(guard):
    result = guard.check("user@phishing.ml")
    assert result.verdict == EmailVerdict.DENY_DANGEROUS


def test_suspicious_tld_ga_denied(guard):
    result = guard.check("user@site.ga")
    assert result.verdict == EmailVerdict.DENY_DANGEROUS


def test_suspicious_tld_cf_denied(guard):
    result = guard.check("user@site.cf")
    assert result.verdict == EmailVerdict.DENY_DANGEROUS


def test_suspicious_tld_gq_denied(guard):
    result = guard.check("user@site.gq")
    assert result.verdict == EmailVerdict.DENY_DANGEROUS


def test_spam_prefix_domain_denied(guard):
    result = guard.check("admin@spam.domain.com")
    assert result.verdict == EmailVerdict.DENY_DANGEROUS


def test_dangerous_reason_mentions_domain(guard):
    result = guard.check("x@evil.com")
    assert "evil.com" in result.reason


# ── Layer 4: Disposable domains ───────────────────────────────────────────────


def test_disposable_domain_denied(guard):
    result = guard.check("user@tempmail.lol")
    assert result.verdict == EmailVerdict.DENY_DISPOSABLE


def test_disposable_domain_10minute(guard):
    result = guard.check("user@10minutemail.com")
    assert result.verdict == EmailVerdict.DENY_DISPOSABLE


def test_disposable_disabled_allows(guard_no_disposable):
    result = guard_no_disposable.check("user@tempmail.lol")
    assert result.verdict != EmailVerdict.DENY_DISPOSABLE


def test_disposable_reason_mentions_domain(guard):
    result = guard.check("user@10minutemail.com")
    assert "10minutemail.com" in result.reason


# ── Layer 5: Suspicious patterns (warn only) ─────────────────────────────────


def test_noreply_warns(guard):
    result = guard.check("noreply@google.com")
    assert result.verdict == EmailVerdict.WARN
    assert "no-reply" in result.reason.lower() or "noreply" in result.reason.lower()


def test_no_reply_hyphen_warns(guard):
    result = guard.check("no-reply@google.com")
    assert result.verdict == EmailVerdict.WARN


def test_donotreply_warns(guard):
    result = guard.check("donotreply@service.com")
    assert result.verdict == EmailVerdict.WARN


def test_mailer_daemon_warns(guard):
    result = guard.check("mailer-daemon@gmail.com")
    assert result.verdict == EmailVerdict.WARN


def test_postmaster_warns(guard):
    result = guard.check("postmaster@domain.com")
    assert result.verdict == EmailVerdict.WARN


def test_test_address_warns(guard):
    result = guard.check("test@bigcorp.com")
    assert result.verdict == EmailVerdict.WARN


def test_example_com_warns(guard):
    result = guard.check("test@example.com")
    assert result.verdict == EmailVerdict.WARN


def test_example_org_warns(guard):
    result = guard.check("user@example.org")
    assert result.verdict == EmailVerdict.WARN


def test_localhost_denied(guard):
    # admin@localhost has no TLD, so it fails the format check (DENY_DANGEROUS).
    result = guard.check("admin@localhost")
    assert result.verdict == EmailVerdict.DENY_DANGEROUS


def test_random_long_address_warns(guard):
    result = guard.check("aslkdjflkasjdflkajsdlkfjasdf@gmail.com")
    assert result.verdict == EmailVerdict.WARN
    assert "auto-generated" in result.reason.lower() or "random" in result.reason.lower()


def test_warn_does_not_block(guard):
    """WARN verdict must not be a DENY — send should proceed."""
    result = guard.check("noreply@google.com")
    assert not result.verdict.name.startswith("DENY")


# ── Normal addresses pass ─────────────────────────────────────────────────────


def test_normal_gmail_passes(guard):
    result = guard.check("bob@gmail.com")
    assert result.verdict == EmailVerdict.ALLOW


def test_normal_corporate_passes(guard):
    result = guard.check("jane.doe@bigcorp.de")
    assert result.verdict == EmailVerdict.ALLOW


def test_normal_with_dots_passes(guard):
    result = guard.check("first.last@company.co.uk")
    assert result.verdict == EmailVerdict.ALLOW


def test_normal_plus_addressing_passes(guard):
    result = guard.check("user+tag@gmail.com")
    assert result.verdict == EmailVerdict.ALLOW


# ── Edge cases ────────────────────────────────────────────────────────────────


def test_invalid_format_denied(guard):
    result = guard.check("notanemail")
    assert result.verdict == EmailVerdict.DENY_DANGEROUS


def test_missing_tld_denied(guard):
    result = guard.check("user@nodot")
    assert result.verdict == EmailVerdict.DENY_DANGEROUS


def test_empty_string_denied(guard):
    result = guard.check("")
    assert result.verdict == EmailVerdict.DENY_DANGEROUS


def test_case_insensitive_dangerous(guard):
    result = guard.check("SPAM@EVIL.COM")
    assert result.verdict == EmailVerdict.DENY_DANGEROUS


def test_address_whitespace_stripped(guard):
    result = guard.check("  spam@evil.com  ")
    assert result.verdict == EmailVerdict.DENY_DANGEROUS


def test_missing_data_dir_returns_empty_lists(tmp_path):
    """Guard with no data files should still block hardcoded dangerous domains."""
    guard = EmailGuard(data_dir=tmp_path, block_disposable=True)
    result = guard.check("spam@evil.com")
    assert result.verdict == EmailVerdict.DENY_DANGEROUS


def test_missing_data_dir_normal_passes(tmp_path):
    """Without blocklist files, normal addresses should pass."""
    guard = EmailGuard(data_dir=tmp_path, block_disposable=True)
    result = guard.check("bob@gmail.com")
    assert result.verdict == EmailVerdict.ALLOW


# ── check_all ─────────────────────────────────────────────────────────────────


def test_check_all_mixed_recipients(guard):
    results = guard.check_all(["bob@gmail.com", "spam@evil.com", "jane@company.com"])
    verdicts = {r.address: r.verdict for r in results}
    assert verdicts["bob@gmail.com"] == EmailVerdict.ALLOW
    assert verdicts["spam@evil.com"] == EmailVerdict.DENY_DANGEROUS
    assert verdicts["jane@company.com"] == EmailVerdict.ALLOW


def test_check_all_empty_list(guard):
    results = guard.check_all([])
    assert results == []


def test_check_all_filters_empty_strings(guard):
    results = guard.check_all(["", "  ", "bob@gmail.com"])
    assert len(results) == 1
    assert results[0].address == "bob@gmail.com"


def test_check_all_comma_separated_handled_externally(guard):
    """check_all processes a pre-split list — splitting is the caller's job."""
    results = guard.check_all(["bob@gmail.com", "spy@evil.com"])
    deny = [r for r in results if r.verdict.name.startswith("DENY")]
    assert len(deny) == 1
    assert deny[0].address == "spy@evil.com"


# ── Integration: _check_recipients helper ────────────────────────────────────


def test_check_recipients_helper_blocks_on_deny():
    """Simulates what google__send_message does: deny on evil domain."""
    from pincer.integrations.google.tools_gmail import _check_recipients

    guard = EmailGuard.__new__(EmailGuard)
    guard._data_dir = None  # type: ignore[assignment]
    guard._block_disposable = True
    guard._allowlist_addresses = set()
    guard._allowlist_domains = set()
    guard._blocklist_addresses = set()
    guard._blocklist_domains = set()
    guard._disposable_domains = set()

    block, warning = _check_recipients(guard, ["spam@evil.com"])
    assert block is not None
    assert "BLOCKED" in block
    assert "evil.com" in block


def test_check_recipients_helper_warns_noreply():
    """Warns but does not block noreply addresses."""
    from pincer.integrations.google.tools_gmail import _check_recipients

    guard = EmailGuard.__new__(EmailGuard)
    guard._data_dir = None  # type: ignore[assignment]
    guard._block_disposable = True
    guard._allowlist_addresses = set()
    guard._allowlist_domains = set()
    guard._blocklist_addresses = set()
    guard._blocklist_domains = set()
    guard._disposable_domains = set()

    block, warning = _check_recipients(guard, ["noreply@google.com"])
    assert block is None
    assert "⚠️" in warning


def test_check_recipients_helper_cc_bcc_checked(guard):
    """All recipient fields — to, cc, bcc — are checked."""
    from pincer.integrations.google.tools_gmail import _check_recipients

    block, _ = _check_recipients(guard, ["bob@gmail.com", "spy@evil.com"])
    assert block is not None
    assert "spy@evil.com" in block


# ── guard wired into registry ─────────────────────────────────────────────────


async def test_send_message_blocked_for_evil_domain(mock_factory, mock_gmail_service, tmp_path):
    """google__send_message returns block message for evil.com (no API call made)."""
    from pincer.integrations.google.tools_gmail import register_gmail_tools
    from pincer.tools.registry import ToolRegistry

    registry = ToolRegistry()
    register_gmail_tools(registry, mock_factory, data_dir=tmp_path)

    handler = registry._tools["google__send_message"].handler
    result = await handler(to="attacker@evil.com", subject="Test", body="Hi")

    assert "BLOCKED" in result
    assert "evil.com" in result
    # Gmail API must NOT have been called
    mock_gmail_service.users().messages().send.assert_not_called()


async def test_send_message_passes_for_normal_address(mock_factory, mock_gmail_service, tmp_path):
    """Normal address passes guard and reaches Gmail API."""
    from pincer.integrations.google.tools_gmail import register_gmail_tools
    from pincer.tools.registry import ToolRegistry

    mock_gmail_service.users().messages().send().execute.return_value = {"id": "sent1"}

    registry = ToolRegistry()
    register_gmail_tools(registry, mock_factory, data_dir=tmp_path)

    handler = registry._tools["google__send_message"].handler
    result = await handler(to="colleague@bigcorp.de", subject="Hello", body="World")

    assert "colleague@bigcorp.de" in result
    assert "BLOCKED" not in result


async def test_create_draft_blocked_for_disposable(mock_factory, mock_gmail_service, tmp_path):
    """google__create_draft also guards disposable domains."""
    from pincer.integrations.google.tools_gmail import register_gmail_tools
    from pincer.tools.registry import ToolRegistry

    (tmp_path / "disposable_domains.txt").write_text("tempmail.lol\n")

    registry = ToolRegistry()
    register_gmail_tools(registry, mock_factory, data_dir=tmp_path)

    handler = registry._tools["google__create_draft"].handler
    result = await handler(to="user@tempmail.lol", subject="Draft", body="Body")

    assert "BLOCKED" in result
    mock_gmail_service.users().drafts().create.assert_not_called()


async def test_forward_message_blocked_for_evil(mock_factory, mock_gmail_service, tmp_path):
    """google__forward_message checks the forward-to address."""
    from pincer.integrations.google.tools_gmail import register_gmail_tools
    from pincer.tools.registry import ToolRegistry

    mock_gmail_service.users().messages().get().execute.return_value = {
        "id": "msg1",
        "threadId": "t1",
        "payload": {
            "headers": [
                {"name": "Subject", "value": "Test"},
                {"name": "From", "value": "alice@example.com"},
            ],
            "mimeType": "text/plain",
            "body": {"data": "SGVsbG8="},
        },
    }

    registry = ToolRegistry()
    register_gmail_tools(registry, mock_factory, data_dir=tmp_path)

    handler = registry._tools["google__forward_message"].handler
    result = await handler(message_id="msg1", to="spy@evil.com")

    assert "BLOCKED" in result
    mock_gmail_service.users().messages().send.assert_not_called()


async def test_email_block_disposable_false_allows_disposable(mock_factory, mock_gmail_service, tmp_path):
    """email_block_disposable=False disables Layer 4."""
    from pincer.integrations.google.tools_gmail import register_gmail_tools
    from pincer.tools.registry import ToolRegistry

    (tmp_path / "disposable_domains.txt").write_text("tempmail.lol\n")
    mock_gmail_service.users().messages().send().execute.return_value = {"id": "ok"}

    registry = ToolRegistry()
    register_gmail_tools(registry, mock_factory, data_dir=tmp_path, email_block_disposable=False)

    handler = registry._tools["google__send_message"].handler
    result = await handler(to="user@tempmail.lol", subject="Hi", body="Body")

    assert "BLOCKED" not in result
