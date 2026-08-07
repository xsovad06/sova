"""Supervisor planner: LLM-based resource-aware reasoning before action dispatch.

Assembles a resource snapshot and work state, loads the supervisor persona,
calls the LLM (Sonnet via direct Anthropic API), and returns a structured
PlanResult. The deterministic engine then filters its decisions against the
approved plan.

When ``ANTHROPIC_API_KEY`` is absent or any error occurs, ``plan()`` returns
``None`` and the engine runs in current deterministic mode (no failure, no
warning spam).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

from sova.utils.logging import get_logger

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from sova.adapters.base import TaskAdapter
    from sova.config.models import ProjectConfig

log = get_logger(component="supervisor.planner")

_ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
_MODEL = "claude-sonnet-4-20250514"
_MAX_TOKENS = 1024
_TIMEOUT_SECONDS = 30.0
_warned_no_key = False

_VALID_ACTIONS = frozenset(
    {
        "spawn_researcher",
        "spawn_developer",
        "spawn_integrate",
        "spawn_address_review",
        "spawn_rebase",
    }
)

_SENSITIVE_RE = re.compile(
    r"authorization\s*[:=]\s*\S+\s+\S+"
    r"|(?:api[_-]?key|token|secret|password|credential|bearer|authorization)\s*[:=]\s*\S+"
    r"|bearer\s+\S+",
    re.IGNORECASE,
)
_MAX_ERROR_LEN = 120


def _sanitize_error(msg: str | None) -> str:
    """Truncate and redact error messages before including in LLM prompt."""
    if not msg:
        return "unknown"
    clean = _SENSITIVE_RE.sub("[REDACTED]", msg)
    if len(clean) > _MAX_ERROR_LEN:
        clean = clean[:_MAX_ERROR_LEN] + "..."
    return clean


_SYSTEM_PROMPT = """\
You are a supervisor planning agent for a software development fleet.
Your job is to decide which actions to take this cycle, given the current
resource constraints and work state.

{persona}

Respond with a JSON object (no markdown fences, no commentary):
{{
  "reasoning": "Your chain-of-thought explanation of the plan",
  "actions": [
    {{"action": "<action_type>", "issue": <number>, "priority": <number>, "reason": "Why this action now"}}
  ],
  "deferred": [
    {{"action": "<action_type>", "issue": <number>, "reason": "Why this is deferred"}}
  ]
}}

Valid action types: spawn_researcher, spawn_developer, spawn_integrate,
spawn_address_review, spawn_rebase.

Rules:
- Only include actions that make sense given available resources
- Prioritize actions that consume no scarce resources (merging approved PRs)
- Consider CodeRabbit review limits when deciding how many developers to spawn
- If CI budget is low, prefer merging over starting new work
- Deferred list should explain WHY each item is held back
- Empty actions list is valid (means "do nothing this cycle")"""


@dataclass(frozen=True, slots=True)
class PlannedAction:
    action: str
    issue: int
    priority: int
    reason: str


@dataclass(frozen=True, slots=True)
class DeferredAction:
    action: str
    issue: int
    reason: str


@dataclass(frozen=True, slots=True)
class PlanResult:
    reasoning: str
    actions: tuple[PlannedAction, ...] = field(default_factory=tuple)
    deferred: tuple[DeferredAction, ...] = field(default_factory=tuple)


class SupervisorPlanner:
    """Assembles context, calls LLM, returns structured plan."""

    def __init__(
        self,
        config: ProjectConfig,
        project_dir: Path,
        session_factory: async_sessionmaker,
    ) -> None:
        self._config = config
        self._project_dir = project_dir
        self._session_factory = session_factory

    async def plan(self, adapter: TaskAdapter) -> PlanResult | None:
        """Assemble context, call LLM, return structured plan.

        Returns None when ANTHROPIC_API_KEY is absent, the LLM call times out,
        the response is unparseable, or any other error occurs.
        """
        global _warned_no_key  # noqa: PLW0603
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            if not _warned_no_key:
                log.info("planner.no_api_key", detail="ANTHROPIC_API_KEY not set; LLM planning disabled")
                _warned_no_key = True
            return None

        try:
            user_prompt = await self._assemble_context(adapter)
            persona = self._load_persona()
            safe_persona = persona.replace("{", "{{").replace("}", "}}")
            system_prompt = _SYSTEM_PROMPT.format(persona=safe_persona)

            log.debug("planner.llm_call_start", model=_MODEL, prompt_length=len(user_prompt))
            start = time.monotonic()

            raw = await self._call_llm(api_key, system_prompt, user_prompt)
            if raw is None:
                return None

            duration_ms = int((time.monotonic() - start) * 1000)
            result = self._parse_response(raw)
            if result is None:
                return None

            log.info(
                "planner.llm_call_complete",
                duration_ms=duration_ms,
                actions_count=len(result.actions),
                deferred_count=len(result.deferred),
            )
            return result
        except Exception:
            log.warning("planner.plan_error", exc_info=True)
            return None

    async def _assemble_context(self, adapter: TaskAdapter) -> str:
        """Build the structured context prompt from all available data sources."""
        sections: list[str] = []

        sections.append(await self._get_resource_snapshot())
        sections.append(await self._get_open_prs())
        sections.append(await self._get_issue_counts(adapter))
        sections.append(self._get_priority_queue())
        sections.append(await self._get_recent_failures())

        section_count = len([s for s in sections if s])
        log.debug("planner.context_assembled", section_count=section_count, persona_loaded=True)
        return "\n\n".join(s for s in sections if s)

    def _load_persona(self) -> str:
        from sova.supervisor.persona import load_persona

        return load_persona(self._config.supervisor.persona_path)

    async def _get_resource_snapshot(self) -> str:
        lines = ["## Resource Snapshot"]

        # GitHub API quota
        try:
            from sova.supervisor.github_quota import get_github_quota_tracker

            tracker = get_github_quota_tracker(self._config.github_user)
            status = tracker.get_status()
            lines.append(
                f"- GitHub API: limited={status.is_limited}, "
                f"hits_in_window={status.hits_in_window}, "
                f"cooldown_remaining={status.cooldown_remaining_seconds:.0f}s"
            )
        except Exception:
            lines.append("- GitHub API: data unavailable")

        # CodeRabbit quota
        try:
            from sova.supervisor.coderabbit_quota import get_quota_status

            async with self._session_factory() as session:
                quota = await get_quota_status(session, self._config.coderabbit_quota)
            lines.append(
                f"- CodeRabbit: {quota.reviews_in_window}/{quota.reviews_per_hour} reviews this hour, "
                f"can_create_pr={quota.can_create_pr}"
                + (f", next_available_in={quota.next_available_minutes:.0f}m" if quota.next_available_minutes else "")
            )
        except Exception:
            lines.append("- CodeRabbit: data unavailable")

        # CI budget
        try:
            from sova.supervisor.ci_budget import get_ci_budget_tracker

            ci_tracker = get_ci_budget_tracker(self._config.github_user)
            budget = await ci_tracker.get_budget(self._config.github_repo, self._config.github_user)
            lines.append(
                f"- CI Budget: {budget.used}/{budget.total} minutes ({budget.pct_used:.0f}% used), "
                f"remaining={budget.remaining}"
            )
        except Exception:
            lines.append("- CI Budget: data unavailable")

        # Agent slots
        lines.append(f"- Agent Slots: max={self._config.max_parallel_agents}")

        return "\n".join(lines)

    async def _get_open_prs(self) -> str:
        try:
            proc = await asyncio.create_subprocess_exec(
                "gh",
                "pr",
                "list",
                "--repo",
                self._config.github_repo,
                "--json",
                "number,title,reviewDecision,statusCheckRollup,mergeable,headRefName",
                "--limit",
                "20",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15.0)
            except asyncio.TimeoutError:
                if proc.returncode is None:
                    proc.kill()
                    await proc.communicate()
                return "## Open PRs\nPR data unavailable (timeout)"
            if proc.returncode != 0:
                return "## Open PRs\nPR data unavailable"

            prs = json.loads(stdout.decode())
            if not prs:
                return "## Open PRs\nNo open PRs"

            lines = [
                "## Open PRs",
                "| # | Title | Review | CI | Mergeable |",
                "|---|-------|--------|----|-----------|",
            ]
            for pr in prs:
                ci_status = "unknown"
                checks = pr.get("statusCheckRollup") or []
                if checks:
                    states = [c.get("conclusion") or c.get("status", "") for c in checks]
                    if all(s == "SUCCESS" for s in states):
                        ci_status = "passing"
                    elif any(s == "FAILURE" for s in states):
                        ci_status = "failing"
                    else:
                        ci_status = "pending"
                lines.append(
                    f"| #{pr['number']} | {pr.get('title', '')[:50]} | "
                    f"{pr.get('reviewDecision', 'NONE')} | {ci_status} | "
                    f"{pr.get('mergeable', 'UNKNOWN')} |"
                )
            return "\n".join(lines)
        except Exception:
            return "## Open PRs\nPR data unavailable"

    async def _get_issue_counts(self, adapter: TaskAdapter) -> str:
        try:
            tasks = await adapter.list_tasks()
            from collections import Counter

            counts: Counter[str] = Counter()
            for task in tasks:
                counts[task.state.value] += 1
            lines = ["## Issue Counts by State"]
            for state_val, count in sorted(counts.items()):
                lines.append(f"- {state_val}: {count}")
            return "\n".join(lines) if len(lines) > 1 else "## Issue Counts by State\nNo issues found"
        except Exception:
            return "## Issue Counts by State\nData unavailable"

    def _get_priority_queue(self) -> str:
        queue = self._config.supervisor.task_queue
        if not queue:
            return "## Priority Queue\nNo explicit task queue configured"
        items = ", ".join(f"#{q}" for q in queue)
        return f"## Priority Queue\n{items}"

    async def _get_recent_failures(self) -> str:
        try:
            from datetime import datetime, timedelta, timezone

            from sqlalchemy import select

            from sova.db.models import TaskRun

            cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
            async with self._session_factory() as session:
                stmt = (
                    select(TaskRun)
                    .where(TaskRun.status == "failed")
                    .where(TaskRun.started_at >= cutoff)
                    .order_by(TaskRun.started_at.desc())
                    .limit(10)
                )
                result = await session.execute(stmt)
                runs = result.scalars().all()

            if not runs:
                return "## Recent Failures (24h)\nNo failures in the last 24 hours"

            lines = ["## Recent Failures (24h)"]
            for run in runs:
                error_summary = _sanitize_error(run.error_message)
                lines.append(f"- Issue #{run.issue_number}, role={run.role}, error={error_summary}")
            return "\n".join(lines)
        except Exception:
            return "## Recent Failures (24h)\nData unavailable"

    async def _call_llm(self, api_key: str, system_prompt: str, user_prompt: str) -> dict | None:
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    _ANTHROPIC_API_URL,
                    headers={
                        "x-api-key": api_key,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json={
                        "model": _MODEL,
                        "max_tokens": _MAX_TOKENS,
                        "system": system_prompt,
                        "messages": [{"role": "user", "content": user_prompt}],
                    },
                    timeout=_TIMEOUT_SECONDS,
                )
                response.raise_for_status()
                data = response.json()
                raw_text = data["content"][0]["text"]
                return json.loads(raw_text)
        except httpx.TimeoutException:
            log.warning("planner.llm_call_timeout", timeout_seconds=_TIMEOUT_SECONDS)
            return None
        except httpx.HTTPStatusError as exc:
            log.warning("planner.llm_call_error", error=str(exc), status_code=exc.response.status_code)
            return None
        except json.JSONDecodeError as exc:
            log.warning("planner.parse_error", raw_response=str(exc)[:200])
            return None
        except Exception:
            log.warning("planner.llm_call_error", exc_info=True)
            return None

    def _parse_response(self, raw: dict) -> PlanResult | None:
        reasoning = raw.get("reasoning")
        if not reasoning:
            log.warning("planner.parse_error", raw_response="missing 'reasoning' field")
            return None

        actions: list[PlannedAction] = []
        for i, item in enumerate(raw.get("actions") or []):
            action_str = item.get("action", "")
            if action_str not in _VALID_ACTIONS:
                log.warning("planner.invalid_action", action=action_str, issue=item.get("issue"))
                continue
            issue = item.get("issue")
            if not isinstance(issue, int) or issue <= 0:
                log.warning("planner.invalid_action", action=action_str, issue=issue)
                continue
            priority = item.get("priority", i + 1)
            if not isinstance(priority, int) or priority <= 0:
                priority = i + 1
            actions.append(
                PlannedAction(
                    action=action_str,
                    issue=issue,
                    priority=priority,
                    reason=str(item.get("reason", "")),
                )
            )

        deferred: list[DeferredAction] = []
        for item in raw.get("deferred") or []:
            action_str = item.get("action", "")
            issue = item.get("issue")
            if not isinstance(issue, int) or issue <= 0:
                continue
            deferred.append(
                DeferredAction(
                    action=action_str,
                    issue=issue,
                    reason=str(item.get("reason", "")),
                )
            )

        return PlanResult(
            reasoning=reasoning,
            actions=tuple(actions),
            deferred=tuple(deferred),
        )
