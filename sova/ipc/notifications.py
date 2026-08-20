"""Notification system for human-in-the-loop workflows.

Sends desktop and/or Slack notifications when agents need human input.
Triggered when a handoff has needs_human=True.
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
from pathlib import Path
from typing import Any

import httpx

from sova.config.models import NotificationConfig
from sova.utils.logging import get_logger
from sova.utils.shell import run

log = get_logger(component="ipc.notifications")

_background_tasks: set[asyncio.Task[None]] = set()

_ICON_PATH = Path(__file__).parent.parent.parent / "assets" / "agent-icon.png"


async def _safe_notify(log_event: str, coro: Coroutine[Any, Any, None], title: str) -> None:
    """Generic notification wrapper that logs but never raises."""
    try:
        await coro
    except Exception:
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
    subtitle: str = "",
    group: str = "",
) -> None:
    """Send notifications as fire-and-forget background tasks.

    This is the main entry point. It dispatches to desktop and/or Slack
    based on the config via asyncio.create_task(). The caller does not
    await the notification delivery. Errors are logged but never raised.
    """
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
        coro = send_webhook_notification(config.webhook_url, config.webhook_headers, title, webhook_text)
        _fire_and_forget(_safe_notify("notify.webhook_failed", coro, title))


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


async def send_webhook_notification(url: str, headers: str, title: str, message: str) -> None:
    """Send a webhook notification via HTTP POST."""
    if not url:
        return

    parsed_headers: dict[str, str] = {}
    if headers:
        try:
            raw_headers = json.loads(headers)
            parsed_headers = {k: os.path.expandvars(v) for k, v in raw_headers.items()}
        except (json.JSONDecodeError, TypeError, AttributeError):
            log.warning("notify.webhook_headers_invalid", headers=headers)

    payload = {"title": title, "message": message}

    async with httpx.AsyncClient() as client:
        response = await client.post(url, json=payload, headers=parsed_headers, timeout=30)
        response.raise_for_status()

    log.info("notify.webhook", title=title, url=url)
