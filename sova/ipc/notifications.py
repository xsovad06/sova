"""Notification system for human-in-the-loop workflows.

Sends desktop and/or Slack notifications when agents need human input.
Triggered when a handoff has needs_human=True.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

from sova.config.models import NotificationConfig
from sova.utils.logging import get_logger
from sova.utils.shell import run

log = get_logger(component="ipc.notifications")

_background_tasks: set[asyncio.Task[None]] = set()

_ICON_PATH = Path(__file__).parent.parent.parent / "assets" / "agent-icon.png"


async def _safe_desktop(
    title: str,
    message: str,
    *,
    subtitle: str = "",
    group: str = "",
) -> None:
    """Desktop notification wrapper that logs but never raises."""
    try:
        await send_desktop_notification(title, message, subtitle=subtitle, group=group)
    except Exception:
        log.warning("notify.desktop_failed", title=title, exc_info=True)


async def _safe_slack(webhook_url: str, title: str, message: str) -> None:
    """Slack notification wrapper that logs but never raises."""
    try:
        await send_slack_notification(webhook_url, title, message)
    except Exception:
        log.warning("notify.slack_failed", title=title, exc_info=True)


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
        _fire_and_forget(_safe_desktop(title, message, subtitle=subtitle, group=group))

    if config.slack_webhook_url:
        slack_text = f"{subtitle}: {message}" if subtitle else message
        _fire_and_forget(_safe_slack(config.slack_webhook_url, title, slack_text))


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
    full_message = f"{subtitle} -- {message}" if subtitle else message
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
