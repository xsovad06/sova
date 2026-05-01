"""Notification system for human-in-the-loop workflows.

Sends desktop and/or Slack notifications when agents need human input.
Triggered when a handoff has needs_human=True.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Coroutine
from typing import Any

from sova.config.models import NotificationConfig
from sova.utils.logging import get_logger
from sova.utils.shell import run

log = get_logger(component="ipc.notifications")

# Hold references to background tasks so they aren't garbage-collected
_background_tasks: set[asyncio.Task[None]] = set()


async def _safe_desktop(title: str, message: str) -> None:
    """Desktop notification wrapper that logs but never raises."""
    try:
        await send_desktop_notification(title, message)
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


def notify(config: NotificationConfig, title: str, message: str) -> None:
    """Send notifications as fire-and-forget background tasks.

    This is the main entry point. It dispatches to desktop and/or Slack
    based on the config via asyncio.create_task(). The caller does not
    await the notification delivery. Errors are logged but never raised.
    """
    if config.desktop:
        _fire_and_forget(_safe_desktop(title, message))

    if config.slack_webhook_url:
        _fire_and_forget(_safe_slack(config.slack_webhook_url, title, message))


async def send_desktop_notification(title: str, message: str) -> None:
    """Send a desktop notification using platform-native tools."""
    if sys.platform == "darwin":
        safe_title = title.replace("\\", "\\\\").replace('"', '\\"')
        safe_message = message.replace("\\", "\\\\").replace('"', '\\"')
        script = f'display notification "{safe_message}" with title "{safe_title}" sound name "Glass"'
        await run("osascript", "-e", script)
        log.info("notify.desktop", platform="macos", title=title)
    elif sys.platform == "linux":
        await run("notify-send", title, message)
        log.info("notify.desktop", platform="linux", title=title)
    else:
        log.debug("notify.desktop.unsupported", platform=sys.platform)


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
