"""Shared doubles for the conversation tests (issue #1544): a fake agent process and a fake runtime.

Each line the fake process prints is JSON (``{"text": ...}``, ``{"tool": ...}`` or ``{"result": {...}}``), and a
turn can be held open until the test releases it.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

from sova.dashboard.services import conversation_service as svc
from sova.ipc.runtime import AgentRuntime
from sova.llm.models import LLMResult, StreamEvent


class FakeProcess:
    def __init__(self, lines: list[str], code: int = 0, hold: asyncio.Event | None = None) -> None:
        self._lines = lines
        self._code = code
        self._hold = hold
        self.stopped = False
        self._done = asyncio.Event()

    async def stdout_lines(self) -> AsyncIterator[str]:
        for line in self._lines:
            yield line
        if self._hold is not None:
            waiter = asyncio.ensure_future(self._hold.wait())
            stopper = asyncio.ensure_future(self._done.wait())
            try:
                await asyncio.wait({waiter, stopper}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                # Also when this generator is cancelled mid-wait (a timeout): no waiter is left pending.
                waiter.cancel()
                stopper.cancel()
                await asyncio.gather(waiter, stopper, return_exceptions=True)

    async def stderr_lines(self) -> AsyncIterator[str]:
        if self._code:
            yield "fatal: runtime exploded"

    async def wait(self) -> int:
        return -15 if self.stopped else self._code

    async def stop(self, timeout: float = 10.0, *, cause: str = "", requester: str = "") -> None:
        self.stopped = True
        self._done.set()


class FakeRuntime(AgentRuntime):
    def __init__(self, *, continues: bool, script: Callable[[int], FakeProcess] | None = None) -> None:
        super().__init__()
        self._continues = continues
        self.calls: list[dict[str, Any]] = []
        self._script = script
        # What each session id this runtime handed out has been told, so a resumed turn knows only its own history.
        self._sessions: dict[str, str] = {}

    @property
    def name(self) -> str:
        return "fake"

    @property
    def continues_sessions(self) -> bool:
        return self._continues

    @property
    def enforces_budget_cap(self) -> bool:
        return True

    async def spawn(self, prompt: str, cwd: str | Path, **_kw: Any) -> Any:  # pragma: no cover (never a pipeline)
        raise AssertionError("a conversation turn never uses spawn()")

    async def spawn_conversation_turn(self, prompt: str, cwd: str | Path, **kw: Any) -> Any:
        self.calls.append({"prompt": prompt, "cwd": cwd, **kw})
        n = len(self.calls)
        if self._script is not None:
            return self._script(n)
        # The answer echoes what the prompt and the resumed session carried, so a wrong or stale session id shows.
        told = self._sessions.get(kw.get("resume_session") or "", "") + prompt
        self._sessions[f"sess-{n}"] = told
        knows = "remembers blue" if "blue" in told else "knows nothing"
        return FakeProcess(
            [
                json.dumps({"tool": "[mcp__sova__sova_status]"}),
                json.dumps({"text": f"answer {n}: {knows}"}),
                json.dumps({"result": {"cost": "0.25", "session": f"sess-{n}"}}),
            ]
        )

    def parse_output(self, line: str) -> StreamEvent | None:
        data = json.loads(line)
        if "result" in data:
            r = data["result"]
            result = LLMResult(
                text=r.get("text", ""),
                model="m",
                cost_usd=Decimal(r["cost"]),
                session_id=r.get("session", ""),
                stop_reason=r.get("stop", ""),
            )
            return StreamEvent(type="result", text=result.text, result=result)
        if "tool" in data:
            return StreamEvent(type="content", text=data["tool"], lines=[data["tool"]], kinds=["tool"])
        return StreamEvent(type="content", text=data["text"])

    async def check_available(self) -> tuple[bool, str]:
        return True, "ok"


async def drop_live_turns() -> None:
    """Cancel and await any worker a test left running, then forget every turn and closing mark."""
    tasks = [turn.task for turn in svc._turns.values() if turn.task is not None and not turn.task.done()]
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    svc._turns.clear()
    svc._closing.clear()
