"""MCP service -- token generation/validation and self-inspection tool handlers."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import stat
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select

from sova.adapters import create_adapter
from sova.config.loader import load_config
from sova.dashboard.services.agent_progress import get_step_progress
from sova.db.models import StepExecution, TaskRun
from sova.db.session import get_session
from sova.git.pr import get_ci_checks as _git_get_ci_checks
from sova.git.pr import get_pr_status as _git_get_pr_status
from sova.utils.formatting import decimal_to_json
from sova.utils.logging import get_logger

log = get_logger(component="dashboard.mcp")


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
            return secret_file.read_text().strip()
        except Exception:
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
    except Exception:
        log.debug("mcp.secret_write_failed", exc_info=True)
    return secret


async def get_run_status(run_id: int, project_dir: Path | None = None) -> dict:
    """MCP tool: get_run_status -- current step, status, elapsed time, pipeline variant.

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
            raise ValueError(f"Run {run_id} not found")

        elapsed = 0.0
        if run.started_at:
            started = run.started_at if run.started_at.tzinfo else run.started_at.replace(tzinfo=timezone.utc)
            if run.ended_at:
                ended = run.ended_at if run.ended_at.tzinfo else run.ended_at.replace(tzinfo=timezone.utc)
            else:
                ended = datetime.now(timezone.utc)
            elapsed = (ended - started).total_seconds()

        progress = get_step_progress(run.current_step, role=run.role, pr_number=run.pr_number)
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
    """MCP tool: get_budget -- spent, limit, and remaining budget for run and issue.

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
            raise ValueError(f"Run {run_id} not found")

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
    """MCP tool: get_gate_results -- step execution history with gate check results.

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
    """MCP tool: get_pr_status -- PR state, CI checks, review decision.

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
            raise ValueError(f"Run {run_id} not found")

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
    """MCP tool: get_issue_context -- issue body, labels, comments.

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
            raise ValueError(f"Run {run_id} not found")

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
    """MCP tool: list_run_history -- all runs for the same issue, ordered newest first.

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
            raise ValueError(f"Run {run_id} not found")

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
