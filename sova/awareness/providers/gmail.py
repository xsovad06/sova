"""GmailProvider: Gmail awareness via Google API.

Queries recent emails from the user's Gmail inbox, categorizes by
actionability (human vs automated, unread vs read), and surfaces
them as awareness items. Requires google-api-python-client; gracefully
skipped when not installed.
"""

from __future__ import annotations

import base64
import email.utils
import html
import re
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from sova.awareness import register_provider
from sova.awareness.base import AwarenessItem, AwarenessProvider, ItemCategory
from sova.utils.logging import get_logger

if TYPE_CHECKING:
    from googleapiclient.discovery import Resource

build_service = None  # type: ignore[assignment]
authenticate_google = None  # type: ignore[assignment]
_HAS_GOOGLE = False

try:
    from googleapiclient.discovery import build as build_service

    from sova.awareness.auth.google_oauth import authenticate_google

    _HAS_GOOGLE = True
except ImportError:
    pass

_log = get_logger(component="awareness.gmail")

_MAX_RESULTS = 50

_AUTOMATED_DOMAINS = frozenset(
    {
        "github.com",
        "noreply.github.com",
        "gitlab.com",
        "bitbucket.org",
        "jira.atlassian.com",
        "atlassian.net",
        "circleci.com",
        "travis-ci.com",
        "travis-ci.org",
        "jenkins.io",
        "sonarcloud.io",
        "dependabot.com",
        "renovatebot.com",
        "snyk.io",
        "codecov.io",
        "coveralls.io",
        "sentry.io",
        "pagerduty.com",
        "opsgenie.com",
        "slack.com",
        "linear.app",
        "notion.so",
        "vercel.com",
        "netlify.com",
        "heroku.com",
        "aws.amazon.com",
        "googlecloud.com",
    }
)

_AUTOMATED_PREFIXES = (
    "noreply",
    "no-reply",
    "donotreply",
    "do-not-reply",
    "notifications",
    "mailer-daemon",
    "postmaster",
    "bounce",
    "auto-",
    "automated",
)

_SCRIPT_STYLE_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


class GmailProvider(AwarenessProvider):
    """Awareness provider for Gmail via Google API."""

    name = "gmail"
    display_name = "Gmail"

    async def is_configured(self) -> bool:
        if not _HAS_GOOGLE:
            _log.debug("gmail.google_libs_missing")
            return False
        try:
            authenticate_google(self.config)
            return True
        except Exception:
            _log.debug("gmail.auth_failed", exc_info=True)
            return False

    async def fetch_items(
        self,
        since: datetime | None = None,
    ) -> list[AwarenessItem]:
        if not _HAS_GOOGLE:
            return []

        try:
            creds = authenticate_google(self.config)
        except Exception:
            _log.warning("gmail.auth_failed", exc_info=True)
            return []

        lookback_hours = self.config.gmail_lookback_hours
        if since is None:
            since = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)

        try:
            service = build_service("gmail", "v1", credentials=creds, cache_discovery=False)
            messages = _list_messages(service, since, self.config.gmail_ignore_labels)
        except Exception:
            _log.warning("gmail.list_failed", exc_info=True)
            return []

        items: list[AwarenessItem] = []
        seen_threads: set[str] = set()

        for msg_stub in messages:
            msg_id = msg_stub["id"]
            thread_id = msg_stub.get("threadId", msg_id)

            if thread_id in seen_threads:
                continue
            seen_threads.add(thread_id)

            try:
                msg = _get_message(service, msg_id)
            except Exception:
                _log.debug("gmail.get_message_failed", msg_id=msg_id, exc_info=True)
                continue

            item = _build_item(msg)
            if item is not None:
                items.append(item)

        return items


def _list_messages(
    service: Resource,
    since: datetime,
    ignore_labels: list[str],
) -> list[dict[str, Any]]:
    """List messages matching the query, up to _MAX_RESULTS."""
    epoch = int(since.timestamp())
    query = f"after:{epoch}"

    for label in ignore_labels:
        query += f' -label:"{label}"'

    result = service.users().messages().list(userId="me", q=query, maxResults=_MAX_RESULTS).execute()
    return result.get("messages", [])


def _get_message(service: Resource, msg_id: str) -> dict[str, Any]:
    """Fetch a single message with full payload."""
    return service.users().messages().get(userId="me", id=msg_id, format="full").execute()


def _build_item(msg: dict[str, Any]) -> AwarenessItem | None:
    """Convert a Gmail API message into an AwarenessItem."""
    msg_id: str = msg.get("id", "")
    if not msg_id:
        return None

    headers = _extract_headers(msg)
    sender = headers.get("from", "")
    subject = headers.get("subject", "(no subject)")
    date_str = headers.get("date", "")
    to_field = headers.get("to", "")
    cc_field = headers.get("cc", "")

    label_ids: list[str] = msg.get("labelIds", [])
    is_unread = "UNREAD" in label_ids

    body = _extract_body(msg.get("payload", {}))
    body_preview = body[:300] if body else ""

    timestamp = _parse_date(date_str)

    is_automated = _is_automated_sender(sender)

    category, urgency, action_hint = _categorize(
        is_unread=is_unread,
        is_automated=is_automated,
    )

    source_url = f"https://mail.google.com/mail/u/0/#inbox/{msg_id}"

    sender_name, _ = email.utils.parseaddr(sender)
    title = f"{sender_name}: {subject}" if sender_name else subject

    return AwarenessItem(
        id=f"gmail:{msg_id}",
        provider="gmail",
        category=category,
        title=title,
        body=body_preview,
        source_url=source_url,
        timestamp=timestamp,
        urgency=urgency,
        action_hint=action_hint,
        metadata={
            "from": sender,
            "to": to_field,
            "cc": cc_field,
            "subject": subject,
            "is_unread": is_unread,
            "is_automated": is_automated,
            "labels": label_ids,
        },
    )


def _extract_headers(msg: dict[str, Any]) -> dict[str, str]:
    """Extract common headers from the message payload."""
    headers: dict[str, str] = {}
    payload = msg.get("payload", {})
    for header in payload.get("headers", []):
        name = header.get("name", "").lower()
        if name in ("from", "to", "cc", "subject", "date"):
            headers[name] = header.get("value", "")
    return headers


def _extract_body(payload: dict[str, Any]) -> str:
    """Walk MIME tree to extract a readable text body.

    Prefers text/plain; falls back to text/html with tag stripping.
    """
    mime_type = payload.get("mimeType", "")

    if mime_type == "text/plain":
        data = payload.get("body", {}).get("data", "")
        return _decode_base64(data)

    if mime_type == "text/html":
        data = payload.get("body", {}).get("data", "")
        return _strip_html(_decode_base64(data))

    parts = payload.get("parts", [])
    if not parts:
        data = payload.get("body", {}).get("data", "")
        if data:
            return _decode_base64(data)
        return ""

    plain_text = ""
    html_text = ""
    for part in parts:
        part_mime = part.get("mimeType", "")
        if part_mime == "text/plain" and not plain_text:
            plain_text = _extract_body(part)
        elif part_mime == "text/html" and not html_text:
            html_text = _extract_body(part)
        elif part_mime.startswith("multipart/"):
            nested = _extract_body(part)
            if nested and not plain_text:
                plain_text = nested

    return plain_text or html_text


def _decode_base64(data: str) -> str:
    """Decode Gmail's URL-safe base64 encoded data."""
    if not data:
        return ""
    try:
        return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
    except Exception:
        return ""


def _strip_html(text: str) -> str:
    """Remove HTML tags and collapse whitespace."""
    text = _SCRIPT_STYLE_RE.sub("", text)
    text = html.unescape(text)
    text = _HTML_TAG_RE.sub(" ", text)
    text = _WHITESPACE_RE.sub(" ", text)
    return text.strip()


def _parse_date(date_str: str) -> datetime | None:
    """Parse an RFC 2822 email date header."""
    if not date_str:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(date_str)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except (ValueError, TypeError):
        return None


def _is_automated_sender(sender: str) -> bool:
    """Detect automated/bot senders by address patterns."""
    _, addr = email.utils.parseaddr(sender)
    addr = addr.lower()
    if not addr or "@" not in addr:
        return False

    local_part, domain = addr.split("@", 1)

    if domain in _AUTOMATED_DOMAINS or any(domain.endswith("." + d) for d in _AUTOMATED_DOMAINS):
        return True

    return any(local_part.startswith(prefix) for prefix in _AUTOMATED_PREFIXES)


def _categorize(
    *,
    is_unread: bool,
    is_automated: bool,
) -> tuple[ItemCategory, int, str]:
    if is_automated or not is_unread:
        return ItemCategory.INFORMATIONAL, 0, ""

    return ItemCategory.NEEDS_ATTENTION, 1, "Unread email from a person"


if _HAS_GOOGLE:
    register_provider("gmail", GmailProvider)
