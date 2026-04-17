"""Process management — spawn, stop, and monitor the agent and Claude Code commands."""

import itertools
import json
import os
import shutil
import subprocess
import threading
from collections import deque
from pathlib import Path

from app import config

COMMANDS_DIR = Path(__file__).parent.parent.parent.parent / "commands"

# Module-level process state
_process: subprocess.Popen | None = None
_output_lines: deque[str] = deque(maxlen=5000)

# mtime-based cache for notifications
_notif_cache: tuple[float, list[dict]] = (0.0, [])


def _control_dir() -> Path:
    return config.DATA_DIR / "agent-control"


def _agent_script() -> Path:
    return config.SCRIPTS_DIR / "pak-agent.sh"


def _find_agent_script() -> Path:
    """Find the pak-agent.sh script."""
    script = _agent_script()
    if script.exists():
        return script
    dashboard_dir = Path(__file__).parent.parent.parent
    candidate = dashboard_dir.parent / "pak-agent.sh"
    if candidate.exists():
        return candidate
    return script


def _check_running() -> dict | None:
    """Return an error dict if a process is already running, else None."""
    if _process and _process.poll() is None:
        return {"error": "Agent is already running", "pid": _process.pid}
    return None


def _spawn_and_stream(cmd: list[str]) -> subprocess.Popen:
    """Clear output, spawn a subprocess, and start a reader thread."""
    global _process

    _output_lines.clear()
    repo_root = config.DATA_DIR.parent
    _process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=str(repo_root),
        env={**os.environ, "TERM": "dumb"},
        text=True,
        bufsize=1,
    )

    def _reader():
        for line in _process.stdout:
            _output_lines.append(line.rstrip("\n"))

    threading.Thread(target=_reader, daemon=True).start()
    return _process


def start_agent(mode: str = "workflow", ticket: str | None = None, extra_args: list[str] | None = None) -> dict:
    """Start the agent as a subprocess with --dashboard flag."""
    if err := _check_running():
        return err

    script = _find_agent_script()
    if not script.exists():
        return {"error": f"Agent script not found at {script}"}

    cmd = [str(script), "--dashboard"]

    if mode == "watch":
        cmd.append("--watch")
    elif mode == "cleanup":
        cmd.append("--cleanup")
    elif mode == "readiness":
        cmd.append("--readiness")
    elif mode == "costs":
        cmd.append("--costs")
    elif mode == "handle-pr" and ticket:
        cmd.extend(["--handle-pr", ticket])
    elif mode == "address-pr" and ticket:
        cmd.extend(["--address-pr", ticket])
    elif mode == "maintain-pr" and ticket:
        cmd.extend(["--maintain-pr", ticket])
    elif mode == "learn-from-pr" and ticket:
        cmd.extend(["--learn-from-pr", ticket])
    elif mode == "investigate" and ticket:
        cmd.extend(["--investigate", ticket])
    elif mode == "ticket" and ticket:
        cmd.append(ticket)

    if extra_args:
        cmd.extend(extra_args)

    proc = _spawn_and_stream(cmd)
    return {"status": "started", "pid": proc.pid, "mode": mode, "ticket": ticket}


def stop_agent() -> dict:
    """Stop the running agent gracefully."""
    global _process

    if not _process or _process.poll() is not None:
        _process = None
        return {"status": "not_running"}

    pid = _process.pid
    _process.terminate()
    try:
        _process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        _process.kill()
        _process.wait(timeout=5)

    _process = None
    return {"status": "stopped", "pid": pid}


def get_status() -> dict:
    """Get current agent status from process state + control files."""
    result = {
        "running": False,
        "pid": None,
        "status": "idle",
        "mode": "",
        "ticket": "",
        "step": "",
    }

    if _process and _process.poll() is None:
        result["running"] = True
        result["pid"] = _process.pid

    status_file = _control_dir() / "status.json"
    if status_file.exists():
        try:
            data = json.loads(status_file.read_text())
            result["status"] = data.get("status", "unknown")
            result["mode"] = data.get("mode", "")
            result["ticket"] = data.get("ticket", "")
            result["step"] = data.get("step", "")
        except (json.JSONDecodeError, OSError):
            pass

    if not result["running"] and result["status"] == "running":
        result["status"] = "idle"

    return result


def get_pending_request() -> dict | None:
    """Read pending request.json if it exists."""
    req_file = _control_dir() / "request.json"
    if not req_file.exists():
        return None
    try:
        return json.loads(req_file.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def send_response(request_id: str, action: str, value: str = "") -> dict:
    """Write response.json for the agent to pick up."""
    ctrl = _control_dir()
    ctrl.mkdir(parents=True, exist_ok=True)
    resp = {"id": request_id, "action": action, "value": value}
    (ctrl / "response.json").write_text(json.dumps(resp))
    return {"status": "sent", "response": resp}


def get_output(since: int = 0) -> list[str]:
    """Get agent stdout lines since a given index."""
    return list(itertools.islice(_output_lines, since, None))


def _read_notifications() -> list[dict]:
    """Read notifications with mtime-based caching."""
    global _notif_cache
    notif_file = _control_dir() / "notifications.jsonl"
    if not notif_file.exists():
        _notif_cache = (0.0, [])
        return []
    try:
        mtime = notif_file.stat().st_mtime
        if mtime == _notif_cache[0]:
            return _notif_cache[1]
        result = []
        for line in notif_file.read_text().strip().splitlines():
            if line.strip():
                try:
                    result.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        _notif_cache = (mtime, result)
        return result
    except OSError:
        return []


def get_notifications(since: int = 0) -> list[dict]:
    """Get notifications from offset `since`."""
    return _read_notifications()[since:]


def get_notification_count() -> int:
    """Get total number of notifications."""
    return len(_read_notifications())


def _find_claude_cli() -> str | None:
    """Find the Claude Code CLI binary."""
    claude = shutil.which("claude")
    if claude:
        return claude
    for candidate in [
        Path.home() / ".local" / "bin" / "claude",
        Path("/usr/local/bin/claude"),
    ]:
        if candidate.exists():
            return str(candidate)
    return None


def _find_command_file(command_name: str) -> Path | None:
    """Find a command .md file by name.

    Search order:
    1. Project-level commands (.claude/commands/ in the target repo)
    2. General PAK commands (commands/)
    """
    project_cmd = config.DATA_DIR / "commands" / f"{command_name}.md"
    if project_cmd.exists():
        return project_cmd

    general_cmd = COMMANDS_DIR / f"{command_name}.md"
    if general_cmd.exists():
        return general_cmd

    return None


def start_claude_command(command_name: str, args: dict | None = None) -> dict:
    """Start a Claude Code command as a subprocess.

    Finds the command .md file (project-level first, then general),
    reads its content, and runs it via the Claude CLI with --print mode.
    """
    if err := _check_running():
        return err

    claude = _find_claude_cli()
    if not claude:
        return {"error": "Claude Code CLI not found"}

    cmd_file = _find_command_file(command_name)
    if not cmd_file:
        return {"error": f"Command file not found: {command_name}.md"}

    # Build the argument string from the args dict
    arg_parts = []
    if args:
        for key, value in args.items():
            arg_parts.append(f"{key}={value}")
    arg_str = " ".join(arg_parts) if arg_parts else ""

    # Read the command file and substitute $ARGUMENTS
    prompt = cmd_file.read_text()
    prompt = prompt.replace("$ARGUMENTS", arg_str)

    cmd = [
        claude,
        "--print",
        "--dangerously-skip-permissions",
        "--model",
        "sonnet",
        "-p",
        prompt,
    ]

    proc = _spawn_and_stream(cmd)
    source = "project" if (config.DATA_DIR / "commands" / f"{command_name}.md").exists() else "general"
    return {
        "status": "started",
        "pid": proc.pid,
        "command": command_name,
        "source": source,
        "args": args,
    }


def run_shell_command(command: str, args: dict | None = None) -> dict:
    """Run a simple shell command (for handoff shell actions)."""
    if err := _check_running():
        return err

    cmd_parts = [command]
    if args:
        if "message" in args:
            cmd_parts.append(str(args["message"]))
        elif "pr" in args:
            cmd_parts.append(str(args["pr"]))

    proc = _spawn_and_stream(cmd_parts)
    return {"status": "started", "pid": proc.pid, "command": " ".join(cmd_parts)}
