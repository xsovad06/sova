"""Agent control API — start, stop, status, respond, handoff actions, and log streaming."""

import subprocess
from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel

from app.services import handoff_service, process_service

router = APIRouter()


class StartRequest(BaseModel):
    mode: str = "workflow"
    ticket: str | None = None
    args: list[str] | None = None


class RespondRequest(BaseModel):
    id: str
    action: str
    value: str = ""


class ExecuteActionRequest(BaseModel):
    action_id: str


class RunCommandRequest(BaseModel):
    command: str
    args: dict | None = None


@router.post("/agent/start")
async def start_agent(req: StartRequest):
    return process_service.start_agent(req.mode, req.ticket, req.args)


@router.post("/agent/stop")
async def stop_agent():
    return process_service.stop_agent()


@router.get("/agent/status")
async def agent_status():
    return process_service.get_status()


@router.get("/agent/request")
async def pending_request():
    req = process_service.get_pending_request()
    if req is None:
        return {"pending": False}
    return {"pending": True, "request": req}


@router.post("/agent/respond")
async def respond(req: RespondRequest):
    return process_service.send_response(req.id, req.action, req.value)


@router.get("/agent/output")
async def agent_output(since: int = 0):
    lines = process_service.get_output(since)
    return {"lines": lines, "total": since + len(lines)}


# --- Handoff endpoints ---


@router.get("/agent/handoff")
async def get_handoff():
    """Get the current handoff state."""
    handoff = handoff_service.get_handoff()
    if handoff is None:
        return {"has_handoff": False}
    return {"has_handoff": True, "handoff": handoff}


@router.post("/agent/handoff/execute")
async def execute_handoff_action(req: ExecuteActionRequest):
    """Execute a specific action from the current handoff.

    Finds the action by ID in the handoff's next_actions, resolves its
    execution parameters, archives the handoff, and starts the appropriate
    agent or command.
    """
    handoff = handoff_service.get_handoff()
    if not handoff:
        return {"error": "No active handoff"}

    # Find the action
    actions = handoff.get("next_actions", [])
    action = next((a for a in actions if a.get("id") == req.action_id), None)
    if not action:
        return {"error": f"Action '{req.action_id}' not found in handoff"}

    # Build execution params
    exec_params = handoff_service.build_action_command(action)

    # Clear the current handoff (archives then deletes) before starting new work
    handoff_service.clear_handoff()

    # Execute based on type
    if exec_params["type"] == "agent":
        result = process_service.start_agent(
            exec_params.get("mode", "workflow"),
            exec_params.get("ticket"),
        )
    elif exec_params["type"] == "claude-command":
        result = process_service.start_claude_command(
            exec_params["command"],
            exec_params.get("args"),
        )
    elif exec_params["type"] == "shell":
        result = process_service.run_shell_command(
            exec_params["command"],
            exec_params.get("args"),
        )
    else:
        return {"error": f"Unknown execution type: {exec_params['type']}"}

    result["action"] = action.get("label", req.action_id)
    return result


@router.post("/agent/handoff/clear")
async def clear_handoff():
    """Archive and clear the current handoff."""
    cleared = handoff_service.clear_handoff()
    return {"cleared": cleared}


@router.get("/agent/handoff/archive")
async def handoff_archive(limit: int = 20):
    """Get recent archived handoffs."""
    return {"archives": handoff_service.get_archive(limit)}


@router.post("/agent/command")
async def run_command(req: RunCommandRequest):
    """Run a Claude Code command file directly (not via handoff)."""
    return process_service.start_claude_command(req.command, req.args)


@router.get("/agent/notifications")
async def agent_notifications(since: int = 0):
    notifs = process_service.get_notifications(since)
    total = since + len(notifs)
    return {"notifications": notifs, "total": total}


@router.get("/agent/worktree-summary")
async def worktree_summary(path: str):
    """Read the .agent-summary.md and git diff from a worktree.

    Returns structured data for the checkpoint approval UI:
    - summary_md: raw markdown of .agent-summary.md
    - sections: parsed sections (big_picture, what_changed, etc.)
    - diff: full git diff output
    """
    worktree = Path(path)
    result: dict = {"found": False, "summary_md": "", "sections": {}, "diff": "", "files": []}

    if not worktree.is_dir():
        return result

    # Read .agent-summary.md
    summary_file = worktree / ".agent-summary.md"
    if summary_file.exists():
        try:
            md = summary_file.read_text()
            result["summary_md"] = md
            result["found"] = True

            # Parse sections from markdown
            current_section = ""
            current_content: list[str] = []
            for line in md.splitlines():
                if line.startswith("## "):
                    if current_section:
                        key = current_section.lower().replace(" ", "_")
                        result["sections"][key] = "\n".join(current_content).strip()
                    current_section = line[3:].strip()
                    current_content = []
                else:
                    current_content.append(line)
            if current_section:
                key = current_section.lower().replace(" ", "_")
                result["sections"][key] = "\n".join(current_content).strip()
        except OSError:
            pass

    # Get the diff (last commit vs parent)
    try:
        diff = subprocess.run(
            ["git", "diff", "HEAD~1", "--stat"],
            cwd=str(worktree),
            capture_output=True,
            text=True,
            timeout=10,
        )
        if diff.returncode == 0:
            result["diff_stat"] = diff.stdout.strip()

        diff_files = subprocess.run(
            ["git", "diff", "HEAD~1", "--name-status"],
            cwd=str(worktree),
            capture_output=True,
            text=True,
            timeout=10,
        )
        if diff_files.returncode == 0:
            for line in diff_files.stdout.strip().splitlines():
                parts = line.split("\t", 1)
                if len(parts) == 2:
                    status_letter = parts[0].strip()
                    status_map = {"A": "added", "M": "modified", "D": "deleted", "R": "renamed"}
                    result["files"].append(
                        {
                            "path": parts[1],
                            "status": status_map.get(status_letter[0], status_letter),
                        }
                    )
    except (subprocess.TimeoutExpired, OSError):
        pass

    return result


# WebSocket endpoint is registered in main.py (outside /api prefix)
