"""MCP service: token generation/validation and self-inspection tool handlers."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import secrets
import stat
from collections import deque
from collections.abc import Awaitable
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from sqlalchemy import func, select

from sova.adapters import close_adapter, create_adapter
from sova.config.loader import load_config
from sova.config.models import ProjectConfig
from sova.dashboard.services.agent_progress import get_step_progress
from sova.db.models import FailureRecord, OutputLine, StepExecution, TaskRun
from sova.db.session import get_session
from sova.git.pr import get_ci_checks as _git_get_ci_checks
from sova.git.pr import get_pr_status as _git_get_pr_status
from sova.llm.egress import scan_and_redact
from sova.utils.formatting import decimal_to_json
from sova.utils.logging import get_logger

log = get_logger(component="dashboard.mcp")


class RunNotFoundError(ValueError):
    """A tool was asked about a run the database does not hold.

    ``user_message`` is the text the JSON-RPC error carries; the endpoint never sends ``str()`` of an exception.
    """

    def __init__(self, run_id: int) -> None:
        self.user_message = f"Run {run_id} not found"
        super().__init__(self.user_message)


class ExplainRequestError(ValueError):
    """A ``sova_explain_issue`` request that names no single issue; ``user_message`` is safe to show the caller."""

    def __init__(self, user_message: str) -> None:
        self.user_message = user_message
        super().__init__(user_message)


def generate_mcp_token(run_id: int, secret: str, expiry_hours: int = 24) -> str:
    """Generate a run-scoped HMAC token for MCP authentication.

    Args:
        run_id: TaskRun ID to encode in the token
        secret: HMAC secret key
        expiry_hours: Token expiry in hours

    Returns:
        HMAC-signed token: {hex-encoded-payload}.{hex-signature}
    """
    exp = datetime.now(timezone.utc) + timedelta(hours=expiry_hours)
    payload = json.dumps({"run_id": run_id, "exp": exp.isoformat()})
    payload_bytes = payload.encode()
    sig = hmac.new(secret.encode(), payload_bytes, hashlib.sha256).hexdigest()
    return f"{payload_bytes.hex()}.{sig}"


def validate_mcp_token(token: str, secret: str) -> int:
    """Validate an MCP token and extract the run_id claim.

    Args:
        token: HMAC-signed token
        secret: HMAC secret key

    Returns:
        run_id from the token

    Raises:
        ValueError: Invalid token format, signature, or expired
    """
    try:
        payload_hex, sig = token.split(".")
        payload_bytes = bytes.fromhex(payload_hex)
        expected_sig = hmac.new(secret.encode(), payload_bytes, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected_sig):
            raise ValueError("Invalid MCP token: signature mismatch")

        payload = json.loads(payload_bytes.decode())
        exp = datetime.fromisoformat(payload["exp"])
        if datetime.now(timezone.utc) > exp:
            raise ValueError("MCP token expired")

        return int(payload["run_id"])
    except (KeyError, ValueError) as e:
        raise ValueError(f"Invalid MCP token: {e}") from e


def get_or_generate_secret(project_dir: Path | None) -> str:
    """Load or generate MCP token secret from config.

    If token_secret is empty in config, generates a random secret
    and persists it to .claude/mcp_secret (gitignored) so it survives
    dashboard restarts. This prevents agent tokens from becoming invalid
    mid-run when the dashboard restarts.
    """
    cfg = load_config(project_dir)
    if cfg.mcp.token_secret:
        return cfg.mcp.token_secret

    # Check for persisted ephemeral secret
    secret_file = (project_dir or Path.cwd()) / ".claude" / "mcp_secret"
    if secret_file.exists():
        try:
            return secret_file.read_text(encoding="utf-8").strip()
        except (OSError, ValueError):
            log.debug("mcp.secret_read_failed", exc_info=True)

    # Generate and persist new secret with restrictive permissions
    secret = secrets.token_hex(32)
    try:
        secret_file.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(secret_file), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
        try:
            os.write(fd, secret.encode())
        finally:
            os.close(fd)
    except (OSError, ValueError):
        log.debug("mcp.secret_write_failed", exc_info=True)
    return secret


async def get_run_status(run_id: int, project_dir: Path | None = None) -> dict:
    """MCP tool: get_run_status (current step, status, elapsed time, pipeline variant).

    Args:
        run_id: TaskRun ID

    Returns:
        {
            status: str,
            current_step: str | None,
            started_at: str,
            elapsed_seconds: float,
            pipeline_variant: str
        }
    """
    async with await get_session(project_dir) as session:
        result = await session.execute(select(TaskRun).where(TaskRun.id == run_id))
        run = result.scalar_one_or_none()
        if not run:
            raise RunNotFoundError(run_id)

        elapsed = 0.0
        if run.started_at:
            started = run.started_at if run.started_at.tzinfo else run.started_at.replace(tzinfo=timezone.utc)
            if run.ended_at:
                ended = run.ended_at if run.ended_at.tzinfo else run.ended_at.replace(tzinfo=timezone.utc)
            else:
                ended = datetime.now(timezone.utc)
            elapsed = (ended - started).total_seconds()

        progress = await get_step_progress(
            run.current_step, role=run.role, pr_number=run.pr_number, project_dir=project_dir
        )
        variant = progress.get("pipeline_variant", "develop")

        return {
            "status": run.status,
            "current_step": run.current_step,
            "started_at": run.started_at.isoformat() if run.started_at else None,
            "elapsed_seconds": elapsed,
            "pipeline_variant": variant,
            "role": run.role,
            "issue_number": run.issue_number,
        }


async def get_budget(run_id: int, project_dir: Path | None = None) -> dict:
    """MCP tool: get_budget (spent, limit, and remaining budget for run and issue).

    Args:
        run_id: TaskRun ID

    Returns:
        {
            spent_usd: Decimal,
            run_limit_usd: Decimal,
            remaining_usd: Decimal,
            issue_total_usd: Decimal,
            issue_limit_usd: Decimal
        }
    """
    async with await get_session(project_dir) as session:
        result = await session.execute(select(TaskRun).where(TaskRun.id == run_id))
        run = result.scalar_one_or_none()
        if not run:
            raise RunNotFoundError(run_id)

        cfg = load_config(project_dir)
        run_limit = Decimal(str(cfg.agent.max_budget))
        issue_limit = Decimal(str(cfg.agent.max_issue_budget))

        spent = Decimal(str(run.total_cost_usd or 0))
        remaining = max(Decimal(0), run_limit - spent)

        issue_total = Decimal(0)
        if run.issue_number:
            issue_runs = await session.execute(
                select(TaskRun.total_cost_usd).where(TaskRun.issue_number == run.issue_number)
            )
            issue_total = sum(Decimal(str(cost or 0)) for (cost,) in issue_runs.fetchall())

    return {
        "spent_usd": decimal_to_json(spent),
        "run_limit_usd": decimal_to_json(run_limit),
        "remaining_usd": decimal_to_json(remaining),
        "issue_total_usd": decimal_to_json(issue_total),
        "issue_limit_usd": decimal_to_json(issue_limit),
    }


async def get_gate_results(run_id: int, project_dir: Path | None = None) -> list[dict]:
    """MCP tool: get_gate_results (step execution history with gate check results).

    Args:
        run_id: TaskRun ID

    Returns:
        [
            {
                step_name: str,
                status: str,
                duration_ms: int | None,
                gate_check_result: dict | None
            },
            ...
        ]
    """
    async with await get_session(project_dir) as session:
        result = await session.execute(
            select(StepExecution).where(StepExecution.task_run_id == run_id).order_by(StepExecution.started_at)
        )
        steps = result.scalars().all()

        return [
            {
                "step_name": step.step_name,
                "status": step.status,
                "duration_ms": step.duration_ms,
                "gate_check_result": json.loads(step.gate_check_result) if step.gate_check_result else None,
            }
            for step in steps
        ]


async def get_pr_status(run_id: int, project_dir: Path | None = None) -> dict:
    """MCP tool: get_pr_status (PR state, CI checks, review decision).

    Args:
        run_id: TaskRun ID

    Returns:
        {
            pr_number: int | None,
            pr_state: str | None,
            ci_checks: [
                {name: str, status: str, conclusion: str},
                ...
            ],
            review_decision: str | None
        }
    """
    async with await get_session(project_dir) as session:
        result = await session.execute(select(TaskRun).where(TaskRun.id == run_id))
        run = result.scalar_one_or_none()
        if not run:
            raise RunNotFoundError(run_id)

        pr_number = run.pr_number
        if not pr_number:
            return {
                "pr_number": None,
                "pr_state": None,
                "ci_checks": [],
                "review_decision": None,
            }

    cfg = load_config(project_dir)
    pr_status_obj = await _git_get_pr_status(pr_number, repo=cfg.github_repo, github_user=cfg.github_user)
    ci_checks_list = await _git_get_ci_checks(pr_number, repo=cfg.github_repo, github_user=cfg.github_user)

    return {
        "pr_number": pr_number,
        "pr_state": pr_status_obj.state,
        "ci_checks": [
            {"name": c.name, "status": c.status.value, "conclusion": c.conclusion.value if c.conclusion else None}
            for c in (ci_checks_list or [])
        ],
        "review_decision": pr_status_obj.review_decision or None,
    }


async def get_issue_context(run_id: int, project_dir: Path | None = None) -> dict:
    """MCP tool: get_issue_context (issue body, labels, comments).

    Args:
        run_id: TaskRun ID

    Returns:
        {
            issue_number: str,
            title: str,
            body: str,
            labels: [str, ...],
            comments: [
                {body: str, author: str},
                ...
            ]
        }
    """
    async with await get_session(project_dir) as session:
        result = await session.execute(select(TaskRun).where(TaskRun.id == run_id))
        run = result.scalar_one_or_none()
        if not run:
            raise RunNotFoundError(run_id)

        issue_number = run.issue_number
        if not issue_number:
            return {
                "issue_number": None,
                "title": None,
                "body": None,
                "labels": [],
                "comments": [],
            }

    cfg = load_config(project_dir)
    adapter = create_adapter(cfg)
    task = await adapter.get_task(issue_number)
    comments_data = await adapter.get_comments(issue_number)

    return {
        "issue_number": issue_number,
        "title": task.title,
        "body": task.body,
        "labels": task.labels,
        "comments": [{"body": c.body, "author": c.author} for c in comments_data],
    }


async def list_run_history(run_id: int, project_dir: Path | None = None) -> list[dict]:
    """MCP tool: list_run_history (all runs for the same issue, ordered newest first).

    Args:
        run_id: TaskRun ID (to resolve the issue number)

    Returns:
        [
            {
                run_id: int,
                role: str,
                status: str,
                started_at: str,
                ended_at: str | None,
                cost_usd: Decimal
            },
            ...
        ]
    """
    async with await get_session(project_dir) as session:
        result = await session.execute(select(TaskRun).where(TaskRun.id == run_id))
        run = result.scalar_one_or_none()
        if not run:
            raise RunNotFoundError(run_id)

        if not run.issue_number:
            return []

        issue_runs = await session.execute(
            select(TaskRun).where(TaskRun.issue_number == run.issue_number).order_by(TaskRun.started_at.desc())
        )
        runs = issue_runs.scalars().all()

        return [
            {
                "run_id": r.id,
                "role": r.role,
                "status": r.status,
                "started_at": r.started_at.isoformat() if r.started_at else None,
                "ended_at": r.ended_at.isoformat() if r.ended_at else None,
                "cost_usd": decimal_to_json(Decimal(str(r.total_cost_usd or 0))),
            }
            for r in runs
        ]


# sova_explain_issue (issue #1544): one issue-scoped view built from the reads above. Every section is a multi-state
# result: "ok" with its fields, "unavailable" with the reason it could not be read, or "not_applicable" when the
# issue has nothing it could describe (no runs, no PR). One failing read never fails the whole call.
SECTION_OK = "ok"
SECTION_UNAVAILABLE = "unavailable"
SECTION_NOT_APPLICABLE = "not_applicable"
# The sections holding tracker text (titles, bodies, comments): untrusted data, never instructions.
EXPLAIN_UNTRUSTED_SECTIONS = ("issue", "runs", "failed_step", "last_output_lines", "pr", "handoff", "worktree")
_EXPLAIN_OUTPUT_LINES = 30
_EXPLAIN_MAX_TEXT = 4000
_EXPLAIN_MAX_COMMENTS = 5
_EXPLAIN_MAX_RUNS = 20
_GIT_TIMEOUT_S = 10.0


def _ok(**fields: object) -> dict:
    return {"status": SECTION_OK, **fields}


def _unavailable(reason: str) -> dict:
    return {"status": SECTION_UNAVAILABLE, "reason": reason}


def _not_applicable(reason: str) -> dict:
    return {"status": SECTION_NOT_APPLICABLE, "reason": reason}


def _clip(text: object, limit: int = _EXPLAIN_MAX_TEXT) -> str:
    """*text* with any secret redacted, cut to *limit*: every free-text field leaving for the agent goes through it."""
    value = scan_and_redact(str(text or "")).redacted_text
    return value if len(value) <= limit else value[:limit] + "... (truncated)"


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


async def _issue_section(issue_number: str, cfg: ProjectConfig) -> dict:
    try:
        adapter = create_adapter(cfg)
        try:
            task = await adapter.get_task(issue_number)
            comments = await adapter.get_comments(issue_number)
        finally:
            await close_adapter(adapter)
    except Exception as e:  # noqa: BLE001 (any tracker failure marks only this section unavailable)
        log.warning("mcp.explain.issue_unavailable", issue=issue_number, error_type=type(e).__name__, exc_info=True)
        return _unavailable(f"The tracker could not be read ({type(e).__name__})")
    agent_labels = [label for label in task.labels if label.startswith("agent:")]
    return _ok(
        title=_clip(task.title, 500),
        state=str(task.state),
        state_label=agent_labels[0] if agent_labels else str(task.state),
        labels=[_clip(label, 100) for label in task.labels],
        url=task.url,
        body=_clip(task.body),
        # get_comments() returns the bodies, newest first.
        comments=[_clip(c, 1000) for c in list(comments)[:_EXPLAIN_MAX_COMMENTS]],
    )


def _run_row(run: TaskRun) -> dict:
    from sova.dashboard.services.run_outcome import run_outcome
    from sova.dashboard.services.work_state import run_action

    return {
        "run_id": run.id,
        "role": run.role,
        "status": run.status,
        "current_step": run.current_step,
        "result": _clip(
            run_outcome(run_action(run.role), run.status, run.error_message, run.handoff_json, run.pr_number), 500
        ),
        "started_at": _iso(run.started_at),
        "ended_at": _iso(run.ended_at),
        "cost_usd": decimal_to_json(Decimal(str(run.total_cost_usd or 0))),
    }


async def _newest_run(issue: str, condition: object, project_dir: Path | None) -> TaskRun | None:
    """The issue's newest run matching *condition*, over its whole history (not only the listed runs)."""
    async with await get_session(project_dir) as session:
        return (
            await session.execute(
                select(TaskRun)
                .where(TaskRun.issue_number == issue, condition)
                .order_by(TaskRun.started_at.desc(), TaskRun.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()


async def _failed_step_section(issue: str, has_runs: bool, project_dir: Path | None) -> dict:
    if not has_runs:
        return _not_applicable("The issue has no runs")
    failed = await _newest_run(issue, TaskRun.status == "failed", project_dir)
    if failed is None:
        return _not_applicable("No run of this issue failed")
    async with await get_session(project_dir) as session:
        step = (
            await session.execute(
                select(StepExecution)
                .where(StepExecution.task_run_id == failed.id, StepExecution.status == "failed")
                .order_by(StepExecution.started_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        record = (
            await session.execute(
                select(FailureRecord)
                .where(FailureRecord.task_run_id == failed.id)
                .order_by(FailureRecord.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
    step_name = (step.step_name if step else None) or (record.step_name if record else None) or failed.current_step
    error = (step.error_message if step else None) or (record.message if record else None) or failed.error_message
    return _ok(run_id=failed.id, step=step_name, error=_clip(error, 2000))


async def _db_output_tail(run_id: int, project_dir: Path | None) -> tuple[list[str], int]:
    """The run's last stored output lines and its total line count, reading only that tail. A failure raises."""
    async with await get_session(project_dir) as session:
        total = (await session.execute(select(func.count()).where(OutputLine.task_run_id == run_id))).scalar() or 0
        if not total:
            return [], 0
        rows = await session.execute(
            select(OutputLine.text)
            .where(OutputLine.task_run_id == run_id)
            .order_by(OutputLine.line_number.desc())
            .limit(_EXPLAIN_OUTPUT_LINES)
        )
        return [text for (text,) in reversed(rows.all())], total


def _file_output_tail(path: Path) -> tuple[list[str], int]:
    """A legacy log file's last lines and its line count, keeping only the tail in memory. A failure raises."""
    tail: deque[str] = deque(maxlen=_EXPLAIN_OUTPUT_LINES)
    total = 0
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            tail.append(line.rstrip("\n"))
            total += 1
    return list(tail), total


async def _output_section(runs: list[TaskRun], project_dir: Path | None) -> dict:
    if not runs:
        return _not_applicable("The issue has no runs")
    newest = runs[0]
    # A read failure raises, so _guarded reports the section unavailable rather than as an empty log.
    lines, total = await _db_output_tail(newest.id, project_dir)
    if total == 0 and newest.output_file_path:
        log_path = Path(newest.output_file_path)
        if not await asyncio.to_thread(log_path.exists):
            # A recorded log that is gone (retention cleanup) is an unreadable field, not an empty one.
            return _unavailable(f"The output log of run {newest.id} no longer exists")
        lines, total = await asyncio.to_thread(_file_output_tail, log_path)
    return _ok(run_id=newest.id, total_lines=total, lines=[_clip(line, 500) for line in lines])


async def _pr_section(issue: str, cfg: ProjectConfig, project_dir: Path | None) -> dict:
    run = await _newest_run(issue, TaskRun.pr_number.isnot(None), project_dir)
    pr_number = run.pr_number if run is not None else None
    if not pr_number:
        return _not_applicable("No run of this issue opened a PR")
    try:
        status = await _git_get_pr_status(pr_number, repo=cfg.github_repo, github_user=cfg.github_user)
    except Exception as e:  # noqa: BLE001 (a GitHub failure marks only this section unavailable)
        log.warning("mcp.explain.pr_unavailable", pr=pr_number, error_type=type(e).__name__, exc_info=True)
        return _unavailable(f"GitHub could not be read for PR #{pr_number} ({type(e).__name__})")
    return _ok(
        number=pr_number,
        title=status.title,
        state=status.state,
        verdict=status.review_decision or None,
        mergeable=status.mergeable,
        url=status.url,
    )


def _handoff_section(issue_number: str, project_dir: Path | None) -> dict:
    from sova.dashboard.services.handoff_service import get_handoff

    try:
        handoff = get_handoff(project_dir, issue_number)
    except Exception as e:  # noqa: BLE001 (an unreadable handoff file marks only this section unavailable)
        log.warning("mcp.explain.handoff_unavailable", issue=issue_number, error_type=type(e).__name__, exc_info=True)
        return _unavailable(f"The handoff file could not be read ({type(e).__name__})")
    if handoff is None:
        return _not_applicable("The issue has no active handoff")
    actions = handoff.get("next_actions") if isinstance(handoff.get("next_actions"), list) else []
    return _ok(
        summary=_clip(handoff.get("summary"), 2000),
        next_actions=[{"id": a.get("id"), "label": _clip(a.get("label"), 200)} for a in actions if isinstance(a, dict)],
    )


async def _git(*args: str, cwd: Path) -> tuple[int, str]:
    from sova.utils.shell import run

    result = await run("git", *args, cwd=cwd, timeout=_GIT_TIMEOUT_S)
    if result.timed_out:
        raise TimeoutError(f"git {args[0]} timed out")
    return result.returncode, result.stdout


async def _worktree_section(issue: str, project_dir: Path | None) -> dict:
    run = await _newest_run(issue, TaskRun.worktree_path.isnot(None) & (TaskRun.worktree_path != ""), project_dir)
    if run is None:
        return _not_applicable("No run of this issue recorded a worktree")
    path = Path(run.worktree_path)
    branch = run.branch_name or ""
    root = project_dir or Path.cwd()
    try:
        exists = await asyncio.to_thread(path.is_dir)
        dirty: bool | None = None
        if exists:
            # No optional locks: an inspection must not refresh the index under an agent's own git command.
            code, out = await _git("--no-optional-locks", "status", "--porcelain", cwd=path)
            if code != 0:
                return _unavailable(f"git status failed in the worktree (exit {code})")
            dirty = bool(out.strip())
        branch_exists: bool | None = None
        if branch:
            code, _out = await _git("rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", cwd=root)
            if code not in (0, 1):
                # --quiet --verify exits 1 for a missing ref; any other code is a repository or inspection error.
                return _unavailable(f"git rev-parse failed for the branch (exit {code})")
            branch_exists = code == 0
    except OSError as e:  # TimeoutError included (a subclass of OSError)
        log.warning("mcp.explain.worktree_unavailable", path=str(path), error_type=type(e).__name__)
        return _unavailable(f"The worktree could not be inspected ({type(e).__name__})")
    return _ok(path=str(path), exists=exists, dirty=dirty, branch=branch or None, branch_exists=branch_exists)


async def _guarded(section: str, call: Awaitable[dict]) -> dict:
    """Await one section's read, turning an unexpected failure into an ``unavailable`` section."""
    try:
        return await call
    except Exception as e:  # noqa: BLE001 (a section that fails is reported unavailable; the others still answer)
        log.warning("mcp.explain.section_failed", section=section, error_type=type(e).__name__, exc_info=True)
        return _unavailable(f"{section} could not be read ({type(e).__name__})")


async def explain_issue(
    issue_number: str | None = None, run_id: int | None = None, project_dir: Path | None = None
) -> dict:
    """MCP tool sova_explain_issue: what SOVA knows about one issue, given its number or one of its run ids.

    Sections: ``issue`` (title, state label, body, recent comments), ``runs`` (each run with its result line),
    ``failed_step`` (the newest failed run's step and error), ``last_output_lines`` (of the newest run), ``pr``
    (state and verdict), ``handoff`` (the active one) and ``worktree`` (path, exists, dirty, branch_exists). An
    issue with no runs has an empty ``runs`` list (a real result) and its run-derived sections not applicable.

    Raises:
        RunNotFoundError: *run_id* names no run.
        ExplainRequestError: neither or both of *issue_number* and *run_id* were given, or the run has no issue.
    """
    if (issue_number is None) == (run_id is None):
        raise ExplainRequestError("Give exactly one of issue or run_id")
    if run_id is not None:
        async with await get_session(project_dir) as session:
            run = (await session.execute(select(TaskRun).where(TaskRun.id == run_id))).scalar_one_or_none()
        if run is None:
            raise RunNotFoundError(run_id)
        if not run.issue_number:
            raise ExplainRequestError(f"Run {run_id} is not tied to an issue")
        issue_number = run.issue_number
    issue = str(issue_number)

    cfg = await asyncio.to_thread(load_config, project_dir)
    runs: list[TaskRun] = []
    try:
        async with await get_session(project_dir) as session:
            result = await session.execute(
                select(TaskRun)
                .where(TaskRun.issue_number == issue)
                .order_by(TaskRun.started_at.desc(), TaskRun.id.desc())
                .limit(_EXPLAIN_MAX_RUNS)
            )
            runs = list(result.scalars().all())
        runs_section = _ok(runs=[_run_row(r) for r in runs])
    except Exception as e:  # noqa: BLE001 (an unreadable run history marks the run sections unavailable)
        log.warning("mcp.explain.runs_unavailable", issue=issue, error_type=type(e).__name__, exc_info=True)
        reason = f"The run history could not be read ({type(e).__name__})"
        return {
            "issue_number": issue,
            "issue": await _guarded("issue", _issue_section(issue, cfg)),
            "runs": _unavailable(reason),
            "failed_step": _unavailable(reason),
            "last_output_lines": _unavailable(reason),
            "pr": _unavailable(reason),
            "handoff": await asyncio.to_thread(_handoff_section, issue, project_dir),
            "worktree": _unavailable(reason),
            "untrusted_sections": list(EXPLAIN_UNTRUSTED_SECTIONS),
        }

    issue_s, failed_s, output_s, pr_s, worktree_s = await asyncio.gather(
        _guarded("issue", _issue_section(issue, cfg)),
        _guarded("failed_step", _failed_step_section(issue, bool(runs), project_dir)),
        _guarded("last_output_lines", _output_section(runs, project_dir)),
        _guarded("pr", _pr_section(issue, cfg, project_dir)),
        _guarded("worktree", _worktree_section(issue, project_dir)),
    )
    return {
        "issue_number": issue,
        "issue": issue_s,
        "runs": runs_section,
        "failed_step": failed_s,
        "last_output_lines": output_s,
        "pr": pr_s,
        "handoff": await asyncio.to_thread(_handoff_section, issue, project_dir),
        "worktree": worktree_s,
        "untrusted_sections": list(EXPLAIN_UNTRUSTED_SECTIONS),
    }
