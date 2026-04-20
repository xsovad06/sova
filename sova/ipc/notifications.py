"""Notification system for human-in-the-loop workflows.

Sends desktop and/or Slack notifications when agents need human input.
Triggered when a handoff has needs_human=True.
"""

from __future__ import annotations

import json
import sys

from sova.config.models import NotificationConfig
from sova.utils.logging import get_logger
from sova.utils.shell import run

log = get_logger(component="ipc.notifications")


async def notify(config: NotificationConfig, title: str, message: str) -> None:
    """Send notifications based on configuration.

    This is the main entry point. It dispatches to desktop and/or Slack
    based on the config. Errors are logged but never raised -- notifications
    are non-fatal side effects.
    """
    if config.desktop:
        try:
            await send_desktop_notification(title, message)
        except Exception:
            log.warning("notify.desktop_failed", title=title, exc_info=True)

    if config.slack_webhook_url:
        try:
            await send_slack_notification(config.slack_webhook_url, title, message)
        except Exception:
            log.warning("notify.slack_failed", title=title, exc_info=True)


async def send_desktop_notification(title: str, message: str) -> None:
    """Send a desktop notification using platform-native tools."""
    if sys.platform == "darwin":
        safe_title = title.replace("\\", "\\\\").replace('"', '\\"')
        safe_message = message.replace("\\", "\\\\").replace('"', '\\"')
        script = f'display notification "{safe_message}" with title "{safe_title}"'
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
