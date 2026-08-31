"""Tests for GmailProvider (Gmail awareness via Google API)."""

from __future__ import annotations

import base64
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from sova.awareness.base import ItemCategory
from sova.awareness.providers.gmail import (
    GmailProvider,
    _build_item,
    _categorize,
    _decode_base64,
    _extract_body,
    _extract_headers,
    _is_automated_sender,
    _is_ci_related,
    _parse_date,
    _strip_html,
)
from sova.config.models import AwarenessConfig


@pytest.fixture
def awareness_config() -> AwarenessConfig:
    return AwarenessConfig(
        enabled=True,
        providers=["gmail"],
        gmail_lookback_hours=24,
        gmail_ignore_labels=["SPAM", "TRASH"],
    )


@pytest.fixture
def gmail_provider(awareness_config: AwarenessConfig) -> GmailProvider:
    return GmailProvider(awareness_config)


def _b64(text: str) -> str:
    """Encode text as URL-safe base64 (matching Gmail API format)."""
    return base64.urlsafe_b64encode(text.encode()).decode()


def _make_message(
    msg_id: str = "msg1",
    thread_id: str = "thread1",
    sender: str = "Alice <alice@example.com>",
    to: str = "me@example.com",
    cc: str = "",
    subject: str = "Test Subject",
    date: str = "Mon, 28 Jul 2026 10:00:00 +0000",
    body_text: str = "Hello world",
    body_html: str = "",
    label_ids: list[str] | None = None,
    mime_type: str = "text/plain",
) -> dict:
    """Build a Gmail API message dict for testing."""
    if label_ids is None:
        label_ids = ["INBOX", "UNREAD"]

    headers = [
        {"name": "From", "value": sender},
        {"name": "To", "value": to},
        {"name": "Subject", "value": subject},
        {"name": "Date", "value": date},
    ]
    if cc:
        headers.append({"name": "Cc", "value": cc})

    if body_html and body_text:
        payload = {
            "mimeType": "multipart/alternative",
            "headers": headers,
            "parts": [
                {"mimeType": "text/plain", "body": {"data": _b64(body_text)}},
                {"mimeType": "text/html", "body": {"data": _b64(body_html)}},
            ],
        }
    elif body_html:
        payload = {
            "mimeType": "text/html",
            "headers": headers,
            "body": {"data": _b64(body_html)},
        }
    else:
        payload = {
            "mimeType": mime_type,
            "headers": headers,
            "body": {"data": _b64(body_text)},
        }

    return {
        "id": msg_id,
        "threadId": thread_id,
        "labelIds": label_ids,
        "payload": payload,
    }


# ---------------------------------------------------------------------------
# is_configured()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch("sova.awareness.providers.gmail._HAS_GOOGLE", False)
async def test_is_configured_no_google_libs(gmail_provider: GmailProvider) -> None:
    assert await gmail_provider.is_configured() is False


@pytest.mark.asyncio
@patch("sova.awareness.providers.gmail._HAS_GOOGLE", True)
@patch("sova.awareness.providers.gmail.authenticate_google")
async def test_is_configured_valid_creds(mock_auth: MagicMock, gmail_provider: GmailProvider) -> None:
    mock_auth.return_value = MagicMock()
    assert await gmail_provider.is_configured() is True


@pytest.mark.asyncio
@patch("sova.awareness.providers.gmail._HAS_GOOGLE", True)
@patch("sova.awareness.providers.gmail.authenticate_google", side_effect=FileNotFoundError("no creds"))
async def test_is_configured_auth_fails(mock_auth: MagicMock, gmail_provider: GmailProvider) -> None:
    assert await gmail_provider.is_configured() is False


# ---------------------------------------------------------------------------
# fetch_items()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch("sova.awareness.providers.gmail._HAS_GOOGLE", False)
async def test_fetch_no_google_libs(gmail_provider: GmailProvider) -> None:
    items = await gmail_provider.fetch_items()
    assert items == []


@pytest.mark.asyncio
@patch("sova.awareness.providers.gmail._HAS_GOOGLE", True)
@patch("sova.awareness.providers.gmail.authenticate_google", side_effect=Exception("auth fail"))
async def test_fetch_auth_failure(mock_auth: MagicMock, gmail_provider: GmailProvider) -> None:
    items = await gmail_provider.fetch_items()
    assert items == []


@pytest.mark.asyncio
@patch("sova.awareness.providers.gmail._HAS_GOOGLE", True)
@patch("sova.awareness.providers.gmail.build_service")
@patch("sova.awareness.providers.gmail.authenticate_google")
async def test_fetch_single_unread_human_email(
    mock_auth: MagicMock, mock_build: MagicMock, gmail_provider: GmailProvider
) -> None:
    mock_auth.return_value = MagicMock()
    service = MagicMock()
    mock_build.return_value = service

    msg = _make_message()
    service.users().messages().list().execute.return_value = {"messages": [{"id": "msg1", "threadId": "thread1"}]}
    service.users().messages().get().execute.return_value = msg

    items = await gmail_provider.fetch_items()

    assert len(items) == 1
    item = items[0]
    assert item.id == "gmail:msg1"
    assert item.provider == "gmail"
    assert item.category == ItemCategory.NEEDS_ATTENTION
    assert item.urgency == 1
    assert "Alice" in item.title
    assert "Test Subject" in item.title
    assert item.body == "Hello world"
    assert item.action_hint == "Unread email from a person"
    assert item.metadata["is_unread"] is True
    assert item.metadata["is_automated"] is False


@pytest.mark.asyncio
@patch("sova.awareness.providers.gmail._HAS_GOOGLE", True)
@patch("sova.awareness.providers.gmail.build_service")
@patch("sova.awareness.providers.gmail.authenticate_google")
async def test_fetch_automated_email_is_informational(
    mock_auth: MagicMock, mock_build: MagicMock, gmail_provider: GmailProvider
) -> None:
    mock_auth.return_value = MagicMock()
    service = MagicMock()
    mock_build.return_value = service

    msg = _make_message(sender="Slack <notifications@slack.com>", subject="New messages in #general")
    service.users().messages().list().execute.return_value = {"messages": [{"id": "msg1", "threadId": "thread1"}]}
    service.users().messages().get().execute.return_value = msg

    items = await gmail_provider.fetch_items()

    assert len(items) == 1
    assert items[0].category == ItemCategory.INFORMATIONAL
    assert items[0].urgency == 0
    assert items[0].metadata["is_automated"] is True


@pytest.mark.asyncio
@patch("sova.awareness.providers.gmail._HAS_GOOGLE", True)
@patch("sova.awareness.providers.gmail.build_service")
@patch("sova.awareness.providers.gmail.authenticate_google")
async def test_fetch_ci_email_is_filtered_out(
    mock_auth: MagicMock, mock_build: MagicMock, gmail_provider: GmailProvider
) -> None:
    mock_auth.return_value = MagicMock()
    service = MagicMock()
    mock_build.return_value = service

    msg = _make_message(sender="GitHub <noreply@github.com>", subject="[org/repo] CI passed")
    service.users().messages().list().execute.return_value = {"messages": [{"id": "msg1", "threadId": "thread1"}]}
    service.users().messages().get().execute.return_value = msg

    items = await gmail_provider.fetch_items()

    assert items == []


@pytest.mark.asyncio
@patch("sova.awareness.providers.gmail._HAS_GOOGLE", True)
@patch("sova.awareness.providers.gmail.build_service")
@patch("sova.awareness.providers.gmail.authenticate_google")
async def test_fetch_read_email_is_informational(
    mock_auth: MagicMock, mock_build: MagicMock, gmail_provider: GmailProvider
) -> None:
    mock_auth.return_value = MagicMock()
    service = MagicMock()
    mock_build.return_value = service

    msg = _make_message(label_ids=["INBOX"])
    service.users().messages().list().execute.return_value = {"messages": [{"id": "msg1", "threadId": "thread1"}]}
    service.users().messages().get().execute.return_value = msg

    items = await gmail_provider.fetch_items()

    assert len(items) == 1
    assert items[0].category == ItemCategory.INFORMATIONAL
    assert items[0].metadata["is_unread"] is False


@pytest.mark.asyncio
@patch("sova.awareness.providers.gmail._HAS_GOOGLE", True)
@patch("sova.awareness.providers.gmail.build_service")
@patch("sova.awareness.providers.gmail.authenticate_google")
async def test_fetch_empty_inbox(mock_auth: MagicMock, mock_build: MagicMock, gmail_provider: GmailProvider) -> None:
    mock_auth.return_value = MagicMock()
    service = MagicMock()
    mock_build.return_value = service

    service.users().messages().list().execute.return_value = {}

    items = await gmail_provider.fetch_items()

    assert items == []


@pytest.mark.asyncio
@patch("sova.awareness.providers.gmail._HAS_GOOGLE", True)
@patch("sova.awareness.providers.gmail.build_service")
@patch("sova.awareness.providers.gmail.authenticate_google")
async def test_fetch_thread_dedup(mock_auth: MagicMock, mock_build: MagicMock, gmail_provider: GmailProvider) -> None:
    mock_auth.return_value = MagicMock()
    service = MagicMock()
    mock_build.return_value = service

    service.users().messages().list().execute.return_value = {
        "messages": [
            {"id": "msg1", "threadId": "thread1"},
            {"id": "msg2", "threadId": "thread1"},
            {"id": "msg3", "threadId": "thread2"},
        ]
    }

    msg1 = _make_message(msg_id="msg1", thread_id="thread1")
    msg3 = _make_message(msg_id="msg3", thread_id="thread2", subject="Other thread")

    def get_message_side_effect(*args, **kwargs):
        mock = MagicMock()
        call_msg_id = kwargs.get("id") or (args[0] if args else None)
        if call_msg_id == "msg1":
            mock.execute.return_value = msg1
        elif call_msg_id == "msg3":
            mock.execute.return_value = msg3
        return mock

    service.users().messages().get.side_effect = get_message_side_effect

    items = await gmail_provider.fetch_items()

    assert len(items) == 2


@pytest.mark.asyncio
@patch("sova.awareness.providers.gmail._HAS_GOOGLE", True)
@patch("sova.awareness.providers.gmail.build_service")
@patch("sova.awareness.providers.gmail.authenticate_google")
async def test_fetch_message_error_skipped(
    mock_auth: MagicMock, mock_build: MagicMock, gmail_provider: GmailProvider
) -> None:
    mock_auth.return_value = MagicMock()
    service = MagicMock()
    mock_build.return_value = service

    service.users().messages().list().execute.return_value = {"messages": [{"id": "msg1", "threadId": "thread1"}]}
    service.users().messages().get().execute.side_effect = Exception("API error")

    items = await gmail_provider.fetch_items()

    assert items == []


@pytest.mark.asyncio
@patch("sova.awareness.providers.gmail._HAS_GOOGLE", True)
@patch("sova.awareness.providers.gmail.build_service")
@patch("sova.awareness.providers.gmail.authenticate_google")
async def test_fetch_list_error_returns_empty(
    mock_auth: MagicMock, mock_build: MagicMock, gmail_provider: GmailProvider
) -> None:
    mock_auth.return_value = MagicMock()
    service = MagicMock()
    mock_build.return_value = service

    service.users().messages().list().execute.side_effect = Exception("API error")

    items = await gmail_provider.fetch_items()

    assert items == []


@pytest.mark.asyncio
@patch("sova.awareness.providers.gmail._HAS_GOOGLE", True)
@patch("sova.awareness.providers.gmail.build_service")
@patch("sova.awareness.providers.gmail.authenticate_google")
async def test_fetch_uses_since_parameter(
    mock_auth: MagicMock, mock_build: MagicMock, gmail_provider: GmailProvider
) -> None:
    mock_auth.return_value = MagicMock()
    service = MagicMock()
    mock_build.return_value = service

    service.users().messages().list().execute.return_value = {}

    since = datetime(2026, 7, 28, 12, 0, 0, tzinfo=timezone.utc)
    await gmail_provider.fetch_items(since=since)

    call_kwargs = service.users().messages().list.call_args
    query = call_kwargs.kwargs.get("q") or call_kwargs[1].get("q", "")
    epoch = int(since.timestamp())
    assert f"after:{epoch}" in query


@pytest.mark.asyncio
@patch("sova.awareness.providers.gmail._HAS_GOOGLE", True)
@patch("sova.awareness.providers.gmail.build_service")
@patch("sova.awareness.providers.gmail.authenticate_google")
async def test_fetch_ignore_labels_in_query(
    mock_auth: MagicMock, mock_build: MagicMock, gmail_provider: GmailProvider
) -> None:
    mock_auth.return_value = MagicMock()
    service = MagicMock()
    mock_build.return_value = service

    service.users().messages().list().execute.return_value = {}

    await gmail_provider.fetch_items()

    call_kwargs = service.users().messages().list.call_args
    query = call_kwargs.kwargs.get("q") or call_kwargs[1].get("q", "")
    assert '-label:"SPAM"' in query
    assert '-label:"TRASH"' in query


# ---------------------------------------------------------------------------
# Header extraction
# ---------------------------------------------------------------------------


def test_extract_headers() -> None:
    msg = _make_message()
    headers = _extract_headers(msg)
    assert headers["from"] == "Alice <alice@example.com>"
    assert headers["subject"] == "Test Subject"
    assert headers["to"] == "me@example.com"


def test_extract_headers_empty() -> None:
    headers = _extract_headers({"payload": {}})
    assert headers == {}


# ---------------------------------------------------------------------------
# MIME body extraction
# ---------------------------------------------------------------------------


def test_extract_body_plain_text() -> None:
    payload = {
        "mimeType": "text/plain",
        "body": {"data": _b64("Plain text body")},
    }
    assert _extract_body(payload) == "Plain text body"


def test_extract_body_html() -> None:
    payload = {
        "mimeType": "text/html",
        "body": {"data": _b64("<p>Hello <b>world</b></p>")},
    }
    result = _extract_body(payload)
    assert "Hello" in result
    assert "world" in result
    assert "<p>" not in result


def test_extract_body_multipart_prefers_plain() -> None:
    payload = {
        "mimeType": "multipart/alternative",
        "parts": [
            {"mimeType": "text/plain", "body": {"data": _b64("Plain version")}},
            {"mimeType": "text/html", "body": {"data": _b64("<p>HTML version</p>")}},
        ],
    }
    assert _extract_body(payload) == "Plain version"


def test_extract_body_multipart_html_fallback() -> None:
    payload = {
        "mimeType": "multipart/alternative",
        "parts": [
            {"mimeType": "text/html", "body": {"data": _b64("<p>HTML only</p>")}},
        ],
    }
    result = _extract_body(payload)
    assert "HTML only" in result


def test_extract_body_nested_multipart() -> None:
    payload = {
        "mimeType": "multipart/mixed",
        "parts": [
            {
                "mimeType": "multipart/alternative",
                "parts": [
                    {"mimeType": "text/plain", "body": {"data": _b64("Nested plain")}},
                ],
            },
            {"mimeType": "application/pdf", "body": {"data": ""}},
        ],
    }
    assert _extract_body(payload) == "Nested plain"


def test_extract_body_empty_payload() -> None:
    assert _extract_body({}) == ""


def test_extract_body_no_parts_with_data() -> None:
    payload = {
        "mimeType": "application/octet-stream",
        "body": {"data": _b64("raw data")},
    }
    assert _extract_body(payload) == "raw data"


# ---------------------------------------------------------------------------
# Base64 / HTML helpers
# ---------------------------------------------------------------------------


def test_decode_base64_valid() -> None:
    encoded = _b64("Hello world")
    assert _decode_base64(encoded) == "Hello world"


def test_decode_base64_empty() -> None:
    assert _decode_base64("") == ""


def test_decode_base64_invalid() -> None:
    assert _decode_base64("!!!invalid!!!") == ""


def test_strip_html_basic() -> None:
    assert _strip_html("<p>Hello</p>") == "Hello"


def test_strip_html_entities() -> None:
    result = _strip_html("&amp; hello")
    assert "& hello" in result


def test_strip_html_collapse_whitespace() -> None:
    result = _strip_html("<p>Hello</p>  <p>World</p>")
    assert "Hello" in result
    assert "World" in result


def test_strip_html_removes_script_content() -> None:
    result = _strip_html("<script>alert(1)</script><p>Content</p>")
    assert "alert" not in result
    assert "Content" in result


def test_strip_html_removes_style_content() -> None:
    result = _strip_html("<style>body { color: red; }</style><p>Visible</p>")
    assert "color" not in result
    assert "Visible" in result


# ---------------------------------------------------------------------------
# Date parsing
# ---------------------------------------------------------------------------


def test_parse_date_rfc2822() -> None:
    dt = _parse_date("Mon, 28 Jul 2026 10:00:00 +0000")
    assert dt is not None
    assert dt.year == 2026
    assert dt.month == 7
    assert dt.day == 28


def test_parse_date_empty() -> None:
    assert _parse_date("") is None


def test_parse_date_invalid() -> None:
    assert _parse_date("not a date") is None


def test_parse_date_naive_gets_utc() -> None:
    dt = _parse_date("Mon, 28 Jul 2026 10:00:00 -0000")
    assert dt is not None
    assert dt.tzinfo is not None


# ---------------------------------------------------------------------------
# Automated sender detection
# ---------------------------------------------------------------------------


def test_automated_github() -> None:
    assert _is_automated_sender("GitHub <noreply@github.com>") is True


def test_automated_noreply_prefix() -> None:
    assert _is_automated_sender("noreply@some-company.com") is True
    assert _is_automated_sender("no-reply@example.com") is True


def test_automated_notifications_prefix() -> None:
    assert _is_automated_sender("notifications@example.com") is True


def test_human_sender() -> None:
    assert _is_automated_sender("Alice Smith <alice@example.com>") is False


def test_automated_empty() -> None:
    assert _is_automated_sender("") is False


def test_automated_jira() -> None:
    assert _is_automated_sender("JIRA <noreply@atlassian.net>") is True


def test_automated_sentry() -> None:
    assert _is_automated_sender("Sentry <noreply@sentry.io>") is True


def test_automated_subdomain_match() -> None:
    assert _is_automated_sender("GitHub <notifications@sub.github.com>") is True
    assert _is_automated_sender("JIRA <bot@mail.atlassian.net>") is True


# ---------------------------------------------------------------------------
# Categorization
# ---------------------------------------------------------------------------


def test_categorize_unread_human() -> None:
    result = _categorize(is_unread=True, is_automated=False)
    assert result is not None
    cat, urgency, hint = result
    assert cat == ItemCategory.NEEDS_ATTENTION
    assert urgency == 1
    assert hint != ""


def test_categorize_automated() -> None:
    result = _categorize(is_unread=True, is_automated=True)
    assert result is not None
    cat, urgency, hint = result
    assert cat == ItemCategory.INFORMATIONAL
    assert urgency == 0


def test_categorize_read_human() -> None:
    result = _categorize(is_unread=False, is_automated=False)
    assert result is not None
    cat, urgency, hint = result
    assert cat == ItemCategory.INFORMATIONAL
    assert urgency == 0


def test_categorize_ci_email_returns_none() -> None:
    result = _categorize(
        is_unread=True,
        is_automated=False,
        sender="GitHub <noreply@github.com>",
        subject="[org/repo] Build passed",
    )
    assert result is None


def test_categorize_ci_email_unread_still_filtered() -> None:
    result = _categorize(
        is_unread=True,
        is_automated=False,
        sender="dependabot[bot] <noreply@github.com>",
        subject="Bump lodash from 4.17.20 to 4.17.21",
    )
    assert result is None


def test_categorize_ci_wins_over_automated() -> None:
    result = _categorize(
        is_unread=True,
        is_automated=True,
        sender="noreply@github.com",
        subject="Build passed",
    )
    assert result is None


def test_categorize_non_ci_automated_still_informational() -> None:
    result = _categorize(
        is_unread=True,
        is_automated=True,
        sender="Slack <feedback@slack.com>",
        subject="You have new messages",
    )
    assert result is not None
    cat, urgency, _ = result
    assert cat == ItemCategory.INFORMATIONAL
    assert urgency == 0


# ---------------------------------------------------------------------------
# CI-related detection
# ---------------------------------------------------------------------------


def test_ci_related_github_noreply() -> None:
    assert _is_ci_related("GitHub <noreply@github.com>", "PR #42 merged") is True


def test_ci_related_github_notifications() -> None:
    assert _is_ci_related("GitHub <notifications@github.com>", "Review requested") is True


def test_ci_related_dependabot_in_sender() -> None:
    assert _is_ci_related("dependabot[bot]", "Bump pytest from 7.0 to 8.0") is True


def test_ci_related_coderabbit_in_subject() -> None:
    assert _is_ci_related("someone@example.com", "CodeRabbit Review: repo/pr#1") is True


def test_ci_related_sonarcloud_in_subject() -> None:
    assert _is_ci_related("noreply@sonarcloud.io", "Quality Gate passed") is True


def test_ci_related_jenkins_in_sender() -> None:
    assert _is_ci_related("Jenkins <ci@mycompany.com>", "Build #100 Success") is True


def test_ci_related_github_actions_in_subject() -> None:
    assert _is_ci_related("someone@example.com", "GitHub Actions: workflow run completed") is True


def test_ci_related_bot_suffix() -> None:
    assert _is_ci_related("renovate[bot] <noreply@renovatebot.com>", "Update deps") is True


def test_ci_related_case_insensitive() -> None:
    assert _is_ci_related("JENKINS <CI@COMPANY.COM>", "BUILD PASSED") is True
    assert _is_ci_related("", "SONARCLOUD Analysis") is True


def test_ci_related_human_email() -> None:
    assert _is_ci_related("Alice <alice@example.com>", "Meeting tomorrow") is False


def test_ci_related_empty_strings() -> None:
    assert _is_ci_related("", "") is False


def test_ci_related_empty_sender() -> None:
    assert _is_ci_related("", "Regular subject line") is False


def test_ci_related_empty_subject() -> None:
    assert _is_ci_related("alice@example.com", "") is False


def test_ci_related_circleci() -> None:
    assert _is_ci_related("builds@circleci.com", "Build succeeded") is True


def test_ci_related_snyk() -> None:
    assert _is_ci_related("noreply@snyk.io", "New vulnerability found") is True


def test_ci_related_mergify() -> None:
    assert _is_ci_related("Mergify <noreply@mergify.com>", "PR auto-merged") is True


def test_ci_related_human_mentioning_jenkins() -> None:
    """Broad vendor names in subject only do not trigger CI filtering."""
    assert _is_ci_related("Alice <alice@example.com>", "Jenkins maintenance window") is False


def test_ci_related_human_mentioning_renovate() -> None:
    """Common English words in subject only do not trigger CI filtering."""
    assert _is_ci_related("Bob <bob@example.com>", "Time to renovate the build system") is False


def test_ci_related_human_mentioning_coveralls() -> None:
    assert _is_ci_related("Carol <carol@example.com>", "Order coveralls for the team") is False


# ---------------------------------------------------------------------------
# _build_item() direct tests
# ---------------------------------------------------------------------------


def test_build_item_empty_id() -> None:
    assert _build_item({}) is None
    assert _build_item({"id": ""}) is None


def test_build_item_ci_email_filtered() -> None:
    msg = _make_message(
        sender="GitHub <noreply@github.com>",
        subject="[org/repo] CI passed",
    )
    assert _build_item(msg) is None


def test_build_item_human_unread() -> None:
    msg = _make_message(
        msg_id="abc123",
        sender="Alice Smith <alice@example.com>",
        subject="Meeting notes",
        label_ids=["INBOX", "UNREAD"],
    )
    item = _build_item(msg)
    assert item is not None
    assert item.id == "gmail:abc123"
    assert item.provider == "gmail"
    assert item.category == ItemCategory.NEEDS_ATTENTION
    assert item.urgency == 1
    assert "Alice Smith" in item.title
    assert "Meeting notes" in item.title
    assert item.metadata["is_unread"] is True
    assert item.metadata["is_automated"] is False


def test_build_item_automated_sender() -> None:
    msg = _make_message(
        sender="Slack <notifications@slack.com>",
        subject="New messages",
        label_ids=["INBOX", "UNREAD"],
    )
    item = _build_item(msg)
    assert item is not None
    assert item.category == ItemCategory.INFORMATIONAL
    assert item.metadata["is_automated"] is True


def test_build_item_read_email() -> None:
    msg = _make_message(label_ids=["INBOX"])
    item = _build_item(msg)
    assert item is not None
    assert item.category == ItemCategory.INFORMATIONAL
    assert item.metadata["is_unread"] is False


def test_build_item_preserves_metadata() -> None:
    msg = _make_message(
        msg_id="meta1",
        sender="Bob <bob@example.com>",
        to="me@example.com",
        cc="team@example.com",
        subject="Review request",
    )
    item = _build_item(msg)
    assert item is not None
    assert item.metadata["from"] == "Bob <bob@example.com>"
    assert item.metadata["to"] == "me@example.com"
    assert item.metadata["cc"] == "team@example.com"
    assert item.metadata["subject"] == "Review request"
    assert item.source_url == "https://mail.google.com/mail/u/0/#inbox/meta1"
