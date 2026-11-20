"""Fakes for the process boundary: an agent runtime, its process, a ``gh`` stub on PATH and a real git project.

Behavioural tests fake only what crosses the process boundary (the coding agent, ``gh``) and run everything
between that and the test against real code, a real temporary git repository and a real SQLite database. The
fixtures that hand these out are ``fake_runtime``, ``fake_gh`` and ``git_project`` in ``tests/conftest.py``.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import textwrap
import threading
import time
from collections.abc import AsyncIterator, Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

from sova.ipc.runtime import AgentRuntime
from sova.llm.models import LLMResult, StreamEvent
from sova.llm.provider import LLMProvider


class FakeAgentProcess:
    """An agent process that stays alive until ``finish()`` or ``stop()``, so no finalizer runs mid-test."""

    def __init__(self, pid: int = 9999, exit_code: int = 0) -> None:
        self.pid = pid
        self.returncode: int | None = None
        self._exit_code = exit_code
        self._done = asyncio.Event()
        self.stop_calls: list[dict[str, str]] = []

    async def stdout_lines(self) -> AsyncIterator[str]:
        await self._done.wait()
        for line in ():
            yield line

    async def stderr_lines(self) -> AsyncIterator[str]:
        await self._done.wait()
        for line in ():
            yield line

    async def wait(self) -> int:
        await self._done.wait()
        return self._exit_code

    def finish(self) -> None:
        self.returncode = self._exit_code
        self._done.set()

    async def stop(self, timeout: float = 10.0, *, cause: str = "", requester: str = "") -> None:
        self.stop_calls.append({"cause": cause, "requester": requester})
        self.finish()


class FakeAgentRuntime(AgentRuntime):
    """The one ``AgentRuntime`` double: records each ``spawn()`` and returns a ``FakeAgentProcess``."""

    def __init__(self, *, pid: int = 9999) -> None:
        super().__init__()
        self.pid = pid
        self.spawn_calls: list[dict[str, Any]] = []
        self.processes: list[FakeAgentProcess] = []

    @property
    def name(self) -> str:
        return "fake"

    async def spawn(self, prompt: str, cwd: str | Path, **kwargs: Any) -> FakeAgentProcess:  # type: ignore[override]
        self.spawn_calls.append({"prompt": prompt, "cwd": Path(cwd), **kwargs})
        process = FakeAgentProcess(pid=self.pid)
        self.processes.append(process)
        return process

    def parse_output(self, line: str) -> StreamEvent | None:
        return StreamEvent(type="content", text=line) if line.strip() else None

    async def check_available(self) -> tuple[bool, str]:
        return True, "fake runtime"

    @property
    def last_spawn(self) -> dict[str, Any]:
        return self.spawn_calls[-1]


class FakeLLMProvider(LLMProvider):
    """The text-only provider boundary: records each prompt and answers with a fixed result."""

    def __init__(self, *, text: str = "ok", cost_usd: str = "0.0123") -> None:
        self.text = text
        self.cost_usd = Decimal(cost_usd)
        self.calls: list[dict[str, Any]] = []

    async def invoke(self, prompt: str, **kwargs: Any) -> LLMResult:  # type: ignore[override]
        self.calls.append({"prompt": prompt, **kwargs})
        return LLMResult(text=self.text, model=kwargs.get("model") or "fake-model", cost_usd=self.cost_usd)

    async def invoke_streaming(self, prompt: str, **kwargs: Any) -> AsyncIterator[StreamEvent]:  # type: ignore[override]
        for event in ():
            yield event

    async def check_available(self) -> tuple[bool, str]:
        return True, "fake provider"


_GH_STUB = textwrap.dedent(
    """\
    #!{python}
    import json, sys, time
    from pathlib import Path

    state = Path({state_dir!r})
    args = sys.argv[1:]
    with (state / "calls.jsonl").open("a", encoding="utf-8") as log:
        log.write(json.dumps(args) + "\\n")
    joined = " ".join(args)
    for rule in json.loads((state / "rules.json").read_text(encoding="utf-8")):
        if all(part in joined for part in rule["match"]):
            time.sleep(rule["delay"])
            sys.stdout.write(rule["stdout"])
            sys.stderr.write(rule["stderr"])
            sys.exit(rule["returncode"])
    sys.stderr.write("fake gh: no response scripted for: " + joined + "\\n")
    sys.exit(1)
    """
)


class FakeGh:
    """A ``gh`` executable on PATH that answers from scripted rules and records every call.

    A rule matches when every ``match`` substring is in the space-joined argument list; the first
    rule added wins. A call no rule matches exits 1 with a message naming it, so a command that
    reaches for a call the test did not script fails visibly.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self._rules: list[dict[str, Any]] = []
        directory.mkdir(parents=True, exist_ok=True)
        script = directory / "gh"
        script.write_text(_GH_STUB.format(python=sys.executable, state_dir=str(directory)), encoding="utf-8")
        script.chmod(0o755)
        (directory / "calls.jsonl").write_text("", encoding="utf-8")
        self._flush()

    def _flush(self) -> None:
        (self.directory / "rules.json").write_text(json.dumps(self._rules), encoding="utf-8")

    def respond(self, *match: str, stdout: str = "", stderr: str = "", returncode: int = 0, delay: float = 0.0) -> None:
        self._rules.append(
            {"match": list(match), "stdout": stdout, "stderr": stderr, "returncode": returncode, "delay": delay}
        )
        self._flush()

    def respond_json(self, *match: str, payload: Any) -> None:
        self.respond(*match, stdout=json.dumps(payload))

    def call_count(self, *parts: str) -> int:
        return sum(1 for call in self.calls if all(part in " ".join(call) for part in parts))

    @property
    def calls(self) -> list[list[str]]:
        text = (self.directory / "calls.jsonl").read_text(encoding="utf-8")
        return [json.loads(line) for line in text.splitlines() if line]

    def called_with(self, *parts: str) -> bool:
        return any(all(part in " ".join(call) for part in parts) for call in self.calls)


def add_origin(root: Path, bare: Path) -> Path:
    """Give *root* a real bare ``origin`` with ``main`` pushed, and return the bare repository's path."""
    git(root.parent, "init", "-q", "--bare", "-b", "main", str(bare))
    git(root, "remote", "add", "origin", str(bare))
    git(root, "push", "-q", "-u", "origin", "main")
    return bare


def send_signal_when(sig: signal.Signals, condition: Callable[[], bool], *, timeout: float = 20.0) -> threading.Event:
    """Deliver *sig* to this process once *condition()* holds, from a daemon thread; set the event to cancel.

    For a test that drives a command which only ends on a signal (`sova watch` on SIGINT, `sova run` on
    SIGTERM). The caller's *condition* must be true only after the command has installed its own handler,
    because a signal that arrives earlier takes the default action and ends the test run (`sova watch` has no
    handler of its own: it relies on asyncio turning SIGINT into a cancellation, so it needs the inherited
    SIGINT handler to be Python's default). Gives up quietly
    after *timeout* seconds, so a command that never reaches the condition fails on its own assertions.
    """
    cancelled = threading.Event()

    def poll() -> None:
        deadline = time.monotonic() + timeout
        while not cancelled.is_set() and time.monotonic() < deadline:
            if condition():
                os.kill(os.getpid(), sig)
                return
            time.sleep(0.02)

    threading.Thread(target=poll, daemon=True).start()
    return cancelled


def git(cwd: Path, *args: str) -> str:
    """Run a real git command in *cwd*, failing the test on a non-zero exit."""
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
        env={
            "PATH": os.environ["PATH"],
            "HOME": str(cwd),
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
        },
    )
    return result.stdout.strip()


def init_git_project(root: Path) -> Path:
    """Create a real git repository with one commit on ``main`` and return its root."""
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", "-q", "-b", "main")
    # Repo-local identity: commands under test run git in-process (a rebase needs a committer) and CI runners
    # have no global user.name/user.email, unlike a developer machine.
    git(root, "config", "user.name", "Test")
    git(root, "config", "user.email", "test@example.com")
    (root / "README.md").write_text("# project\n", encoding="utf-8")
    git(root, "add", "README.md")
    git(root, "commit", "-q", "-m", "chore(docs): initial commit")
    return root


def add_issue_worktree(root: Path, issue: str, branch: str) -> Path:
    """Create the real worktree ``.claude/worktrees/<issue>`` on a new *branch* and return its path."""
    worktree = root / ".claude" / "worktrees" / issue
    git(root, "worktree", "add", "-q", "-b", branch, str(worktree))
    return worktree
