"""Process management — spawn, stop, and monitor the agent."""

import json
import os
import subprocess
import threading
from collections import deque
from pathlib import Path

from app import config

# Module-level process state
_process: subprocess.Popen | None = None
_output_lines: deque[str] = deque(maxlen=5000)

# mtime-based cache for notifications
_notif_cache: tuple[float, list[dict]] = (0.0, [])


def _control_dir() -> Path:
    return config.DATA_DIR / "agent-control"


def _agent_script() -> Path:
    return config.SCRIPTS_DIR / "gwym-agent.sh"


def _find_agent_script() -> Path:
    """Find the gwym-agent.sh script."""
    script = _agent_script()
    if script.exists():
        return script
    dashboard_dir = Path(__file__).parent.parent.parent
    candidate = dashboard_dir.parent / "gwym-agent.sh"
    if candidate.exists():
        return candidate
    return script


def start_agent(mode: str = "workflow", ticket: str | None = None, extra_args: list[str] | None = None) -> dict:
    """Start the agent as a subprocess with --dashboard flag."""
    global _process

    if _process and _process.poll() is None:
        return {"error": "Agent is already running", "pid": _process.pid}

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

    return {"status": "started", "pid": _process.pid, "mode": mode, "ticket": ticket}


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
    return list(_output_lines)[since:]


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
