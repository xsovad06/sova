"""Notification system for human-in-the-loop workflows.

Sends desktop, Slack, email, webhook and ntfy notifications for agent events.
Each carries a NotificationKind; ntfy alone filters by kind.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import smtplib
import sys
from collections.abc import Coroutine
from email.message import EmailMessage
from enum import StrEnum
from pathlib import Path
from typing import Any

import httpx

from sova.config.context import resolve_project_dir
from sova.config.models import NotificationConfig
from sova.llm.keyring_store import resolve_ntfy_token
from sova.utils.logging import get_logger
from sova.utils.shell import run

log = get_logger(component="ipc.notifications")

_background_tasks: set[asyncio.Task[None]] = set()

# Unknown ntfy_events entries already warned about, so one typo logs once per process, not on every notify().
_warned_unknown_events: set[str] = set()

_ICON_PATH = Path(__file__).parent.parent.parent / "assets" / "agent-icon.png"

_HTTP_TIMEOUT_SECONDS = 30


class NotificationKind(StrEnum):
    """What a notification means to the person receiving it (drives the ntfy filter, priority and tags)."""

    NEEDS_HUMAN = "needs_human"
    FAILED = "failed"
    FINISHED = "finished"
    INFO = "info"


_KIND_VALUES = frozenset(NotificationKind)

# ntfy JSON priority is an integer: 3 is the default, 4 is high.
_NTFY_PRIORITY: dict[NotificationKind, int] = {NotificationKind.NEEDS_HUMAN: 4}
_NTFY_DEFAULT_PRIORITY = 3

# ntfy rejects or attaches a message body over 4096 bytes; a step error can be far longer.
_NTFY_MAX_MESSAGE_BYTES = 4096

# ntfy short-code tags (plain text, rendered by ntfy clients).
_NTFY_TAGS: dict[NotificationKind, list[str]] = {
    NotificationKind.NEEDS_HUMAN: ["raised_hand"],
    NotificationKind.FAILED: ["warning"],
    NotificationKind.FINISHED: ["white_check_mark"],
    NotificationKind.INFO: ["information_source"],
}


def _coerce_kind(kind: str) -> NotificationKind:
    """Normalize a plain string to a NotificationKind; an unknown value becomes INFO, never an error."""
    try:
        return NotificationKind(str(kind).strip().lower())
    except ValueError:
        log.warning("notify.unknown_kind", kind=str(kind))
        return NotificationKind.INFO


def _truncate_utf8(text: str, max_bytes: int) -> str:
    """Cut *text* to at most *max_bytes* UTF-8 bytes without splitting a character."""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


# Dashboard pages (sova/dashboard/app.py): `/work/{run_id}` is the run detail page; the older `/runs/{id}`
# only redirects to it. tests/test_ntfy_notifications.py pins both against the app's routes.
RUN_PAGE_PATH = "/work/{run_id}"
AGENTS_PAGE_PATH = "/agents"


def build_item_url(link_base_url: str, run_id: int | None) -> str:
    """Link to the run page when the run is known, else to the Agents page; "" without a base URL."""
    base = link_base_url.strip().rstrip("/")
    if not base:
        return ""
    path = RUN_PAGE_PATH.format(run_id=run_id) if run_id is not None else AGENTS_PAGE_PATH
    return f"{base}{path}"


def _ntfy_wants(config: NotificationConfig, kind: NotificationKind) -> bool:
    """Whether ntfy is configured and *kind* is one of ``ntfy_events`` (unknown entries are ignored)."""
    if not (config.ntfy_url.strip() and config.ntfy_topic.strip()):
        return False
    unknown = [
        event for event in config.ntfy_events if event not in _KIND_VALUES and event not in _warned_unknown_events
    ]
    if unknown:
        _warned_unknown_events.update(unknown)
        log.warning("notify.ntfy_unknown_events", events=unknown)
    return kind.value in config.ntfy_events


async def _safe_notify(log_event: str, coro: Coroutine[Any, Any, None], title: str) -> None:
    """Generic notification wrapper that logs but never raises."""
    try:
        await coro
    except Exception:  # noqa: BLE001 (notification delivery must never break the caller)
        log.warning(log_event, title=title, exc_info=True)


def _fire_and_forget(coro: Coroutine[Any, Any, None]) -> None:
    """Schedule a coroutine as a background task, preventing GC."""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


def notify(
    config: NotificationConfig,
    title: str,
    message: str,
    *,
    kind: str = NotificationKind.INFO,
    subtitle: str = "",
    group: str = "",
    issue: str | int | None = None,
    pr_number: int | None = None,
    run_id: int | None = None,
    project_dir: Path | None = None,
) -> None:
    """Send notifications as fire-and-forget background tasks.

    This is the main entry point. It dispatches to desktop, Slack, email, webhook and ntfy
    based on the config via asyncio.create_task(). The caller does not
    await the notification delivery. Errors are logged but never raised.

    Every call site names its ``kind`` (a guard test enforces it); ``issue``, ``pr_number`` and
    ``run_id`` identify the item for the webhook payload and the ntfy click link. Only ntfy
    filters by kind (``notification.ntfy_events``): the other channels receive every event.
    ``project_dir`` scopes the ntfy token's keyring lookup (defaults to the active project).
    """
    notification_kind = _coerce_kind(kind)
    url = build_item_url(config.link_base_url, run_id)

    if config.desktop:
        coro = send_desktop_notification(title, message, subtitle=subtitle, group=group)
        _fire_and_forget(_safe_notify("notify.desktop_failed", coro, title))

    if config.slack_webhook_url:
        slack_text = f"{subtitle}: {message}" if subtitle else message
        coro = send_slack_notification(config.slack_webhook_url, title, slack_text)
        _fire_and_forget(_safe_notify("notify.slack_failed", coro, title))

    if config.email_enabled and config.email_to and config.email_from and config.email_smtp_host:
        email_text = f"{subtitle}: {message}" if subtitle else message
        coro = send_email_notification(config, title, email_text)
        _fire_and_forget(_safe_notify("notify.email_failed", coro, title))

    if config.webhook_url:
        webhook_text = f"{subtitle}: {message}" if subtitle else message
        item = {
            "kind": notification_kind.value,
            "issue": str(issue) if issue not in (None, "") else None,
            "pr_number": pr_number,
            "run_id": run_id,
            "url": url or None,
        }
        coro = send_webhook_notification(config.webhook_url, config.webhook_headers, title, webhook_text, item=item)
        _fire_and_forget(_safe_notify("notify.webhook_failed", coro, title))

    if _ntfy_wants(config, notification_kind):
        ntfy_text = f"{subtitle}: {message}" if subtitle else message
        coro = send_ntfy_notification(
            config, title, ntfy_text, kind=notification_kind, click_url=url, project_dir=project_dir
        )
        _fire_and_forget(_safe_notify("notify.ntfy_failed", coro, title))


async def send_desktop_notification(
    title: str,
    message: str,
    *,
    subtitle: str = "",
    group: str = "",
) -> None:
    """Send a desktop notification using platform-native tools."""
    if sys.platform == "darwin":
        if shutil.which("terminal-notifier"):
            await _notify_terminal_notifier(title, message, subtitle=subtitle, group=group)
        else:
            await _notify_jxa(title, message, subtitle=subtitle)
        log.info("notify.desktop", platform="macos", title=title)
    elif sys.platform == "linux":
        body = f"{subtitle}\n{message}" if subtitle else message
        await run("notify-send", title, body)
        log.info("notify.desktop", platform="linux", title=title)
    else:
        log.debug("notify.desktop.unsupported", platform=sys.platform)


async def _notify_terminal_notifier(
    title: str,
    message: str,
    *,
    subtitle: str = "",
    group: str = "",
) -> None:
    args = [
        "terminal-notifier",
        "-title",
        title,
        "-message",
        message,
        "-sound",
        "default",
    ]
    if subtitle:
        args += ["-subtitle", subtitle]
    if _ICON_PATH.exists():
        args += ["-appIcon", str(_ICON_PATH)]
    if group:
        args += ["-group", group]
    await run(*args)


async def _notify_jxa(title: str, message: str, *, subtitle: str = "") -> None:
    full_message = f"{subtitle}: {message}" if subtitle else message
    script = (
        "var app = Application.currentApplication();"
        "app.includeStandardAdditions = true;"
        f"app.displayNotification({json.dumps(full_message)},"
        f' {{withTitle: {json.dumps(title)}, soundName: "Glass"}});'
    )
    await run("osascript", "-l", "JavaScript", "-e", script)


async def send_slack_notification(webhook_url: str, title: str, message: str) -> None:
    """Send a Slack notification via incoming webhook."""
    if not webhook_url:
        return

    payload = json.dumps({"text": f"*{title}*\n{message}"})

    await run(
        "curl",
        "-s",
        "-X",
        "POST",
        "-H",
        "Content-Type: application/json",
        "-d",
        payload,
        webhook_url,
        timeout=30,
    )
    log.info("notify.slack", title=title)


async def send_email_notification(config: NotificationConfig, title: str, message: str) -> None:
    """Send an email notification via SMTP."""
    if not config.email_enabled or not config.email_to or not config.email_from or not config.email_smtp_host:
        return

    def _send_email() -> None:
        msg = EmailMessage()
        msg["Subject"] = title
        msg["From"] = config.email_from
        msg["To"] = config.email_to
        msg.set_content(message)

        with smtplib.SMTP(config.email_smtp_host, config.email_smtp_port, timeout=30) as smtp:
            if config.email_smtp_starttls:
                smtp.starttls()
            if config.email_smtp_user and config.email_smtp_password:
                smtp.login(config.email_smtp_user, config.email_smtp_password)
            smtp.send_message(msg)

    await asyncio.to_thread(_send_email)
    log.info("notify.email", title=title, to=config.email_to)


async def send_webhook_notification(
    url: str, headers: str, title: str, message: str, *, item: dict[str, Any] | None = None
) -> None:
    """Send a webhook notification via HTTP POST.

    *item* adds fields (kind, issue, pr_number, run_id, url) next to ``title`` and ``message``, so
    consumers that only read those two keep working.
    """
    if not url:
        return

    parsed_headers: dict[str, str] = {}
    if headers:
        try:
            raw_headers = json.loads(headers)
            parsed_headers = {k: os.path.expandvars(v) for k, v in raw_headers.items()}
        except (json.JSONDecodeError, TypeError, AttributeError):
            log.warning("notify.webhook_headers_invalid", headers=headers)

    payload = {"title": title, "message": message, **(item or {})}

    async with httpx.AsyncClient() as client:
        response = await client.post(url, json=payload, headers=parsed_headers, timeout=_HTTP_TIMEOUT_SECONDS)
        response.raise_for_status()

    log.info("notify.webhook", title=title, url=url)


async def send_ntfy_notification(
    config: NotificationConfig,
    title: str,
    message: str,
    *,
    kind: NotificationKind = NotificationKind.INFO,
    click_url: str = "",
    project_dir: Path | None = None,
) -> None:
    """Publish one JSON message to a self-hosted ntfy server.

    Skipped silently when the URL or topic is empty. The bearer token is resolved here (keyring first),
    sent only in the Authorization header, and never logged; a missing token sends no header.
    A network error or non-2xx response is logged and swallowed.
    """
    base_url = config.ntfy_url.strip()
    topic = config.ntfy_topic.strip()
    if not base_url or not topic:
        return

    payload: dict[str, Any] = {
        "topic": topic,
        "title": title,
        "message": _truncate_utf8(message, _NTFY_MAX_MESSAGE_BYTES),
        "priority": _NTFY_PRIORITY.get(kind, _NTFY_DEFAULT_PRIORITY),
        "tags": _NTFY_TAGS[kind],
    }
    if click_url:
        payload["click"] = click_url

    token = await asyncio.to_thread(resolve_ntfy_token, config.ntfy_token, resolve_project_dir(project_dir))
    headers = {"Authorization": f"Bearer {token}"} if token else {}

    try:
        async with httpx.AsyncClient() as client:
            # JSON publishing posts to the server root; normalize so a trailing slash is not doubled or missing.
            response = await client.post(
                f"{base_url.rstrip('/')}/", json=payload, headers=headers, timeout=_HTTP_TIMEOUT_SECONDS
            )
            response.raise_for_status()
    except httpx.HTTPError as exc:
        status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
        log.warning("notify.ntfy_failed", topic=topic, status=status, exc_info=True)
        return

    log.info("notify.ntfy", topic=topic, kind=kind.value, title=title)
