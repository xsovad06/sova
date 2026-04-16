"""Simulate the agent's priority scan logic (read-only)."""

import json

from app import config


def get_priority_queue() -> list[dict]:
    """Scan task states and build a priority queue like _priority_scan() does."""
    queue = []
    worktree_dir = config.WORKTREE_DIR

    if not worktree_dir.exists():
        return queue

    for ticket_dir in worktree_dir.iterdir():
        if not ticket_dir.is_dir():
            continue
        state_file = ticket_dir / "task-state.json"
        if not state_file.exists():
            continue
        try:
            data = json.loads(state_file.read_text())
        except (json.JSONDecodeError, OSError):
            continue

        status = data.get("status", "")
        ticket = data.get("jira_key", ticket_dir.name)
        last_step = data.get("last_step", "")
        next_step = data.get("next_step", "")

        # P0: interrupted tasks (in_progress with a next step)
        if status == "in_progress" and next_step:
            queue.append(
                {
                    "priority": 0,
                    "priority_label": "P0 - Resume",
                    "ticket": ticket,
                    "reason": f"In progress at {last_step}, next: {next_step}",
                    "action": f"Resume from {next_step}",
                }
            )

        # P0: paused tasks
        elif status == "paused":
            reason = data.get("paused_reason", "unknown")
            queue.append(
                {
                    "priority": 0,
                    "priority_label": "P0 - Paused",
                    "ticket": ticket,
                    "reason": f"Paused at {last_step}: {reason}",
                    "action": f"Resume from {next_step or last_step}",
                }
            )

        # P1: tasks with PRs that may need attention
        elif status == "in_progress" and data.get("pr_number"):
            queue.append(
                {
                    "priority": 1,
                    "priority_label": "P1 - PR Active",
                    "ticket": ticket,
                    "reason": f"PR {data['pr_number']} — at {last_step}",
                    "action": "Check PR status and review comments",
                }
            )

    queue.sort(key=lambda x: (x["priority"], x["ticket"]))
    return queue
