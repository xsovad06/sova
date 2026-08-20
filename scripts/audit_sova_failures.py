#!/usr/bin/env python3
"""Audit SOVA project failure patterns to diagnose 44.2% failure rate.

This script performs a comprehensive analysis of:
1. Overall failure rate broken down by status
2. Failure rate before/after timeout fix (#687, #699)
3. Top failure patterns by step name
4. Top error message clusters
5. Timeout-related failure analysis
6. Budget-related failure analysis
7. Gate check failure analysis
8. Configuration recommendations

Usage:
    python scripts/audit_sova_failures.py
    python scripts/audit_sova_failures.py --detailed
    python scripts/audit_sova_failures.py --cutoff-date 2026-08-19
"""

import argparse
import asyncio
import json
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

import aiosqlite


async def get_sova_db_path() -> Path:
    """Resolve SOVA project database path."""
    import subprocess

    # Use git to resolve the primary worktree root (handles both main repo and worktrees)
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            capture_output=True,
            text=True,
            check=True,
        )
        git_common = Path(result.stdout.strip())
        # If relative (e.g., ".git"), we're in primary; if absolute, we're in a worktree
        if git_common.is_absolute():
            project_root = git_common.parent
        else:
            project_root = Path.cwd()
        return project_root / ".claude" / "sova.db"
    except subprocess.CalledProcessError:
        # Fallback to cwd if not in a git repo
        return Path.cwd() / ".claude" / "sova.db"


async def query_overall_stats(db: aiosqlite.Connection) -> dict[str, Any]:
    """Query overall TaskRun statistics."""
    sql = """
        SELECT
            COUNT(*) as total,
            COALESCE(SUM(CASE WHEN status = 'done' THEN 1 ELSE 0 END), 0) as done,
            COALESCE(SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END), 0) as failed,
            COALESCE(SUM(CASE WHEN status = 'interrupted' THEN 1 ELSE 0 END), 0) as interrupted,
            COALESCE(SUM(CASE WHEN status = 'rejected' THEN 1 ELSE 0 END), 0) as rejected,
            COALESCE(SUM(CASE WHEN status IN ('paused', 'running', 'pending') THEN 1 ELSE 0 END), 0) as in_progress,
            COALESCE(SUM(total_cost_usd), 0) as total_cost
        FROM task_runs
    """
    async with db.execute(sql) as cur:
        row = await cur.fetchone()

    total = row[0]
    done = row[1]
    failed = row[2]
    interrupted = row[3]
    rejected = row[4]
    in_progress = row[5]
    total_cost = Decimal(str(row[6]))

    failure_rate = (failed / total * 100) if total > 0 else 0
    success_rate = (done / total * 100) if total > 0 else 0

    return {
        "total": total,
        "done": done,
        "failed": failed,
        "interrupted": interrupted,
        "rejected": rejected,
        "in_progress": in_progress,
        "failure_rate": round(failure_rate, 1),
        "success_rate": round(success_rate, 1),
        "total_cost": total_cost,
    }


async def query_failures_by_step(db: aiosqlite.Connection) -> list[dict[str, Any]]:
    """Query failure counts by step name."""
    sql = """
        SELECT
            step_name,
            COUNT(*) as total,
            SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failures,
            SUM(CASE WHEN status = 'interrupted' THEN 1 ELSE 0 END) as interrupted
        FROM step_executions
        GROUP BY step_name
        ORDER BY failures DESC, total DESC
    """
    async with db.execute(sql) as cur:
        rows = await cur.fetchall()

    return [
        {
            "step": row[0],
            "total": row[1],
            "failures": row[2],
            "interrupted": row[3],
            "rate": round(row[2] / row[1] * 100, 1) if row[1] > 0 else 0,
        }
        for row in rows
        if row[2] > 0  # Only include steps with failures
    ]


async def query_error_clusters(db: aiosqlite.Connection) -> list[dict[str, Any]]:
    """Query and cluster error messages from FailureRecord."""
    sql = """
        SELECT message, COUNT(*) as cnt
        FROM failure_records
        WHERE message IS NOT NULL AND message != ''
        GROUP BY message
        ORDER BY cnt DESC
        LIMIT 50
    """
    async with db.execute(sql) as cur:
        rows = await cur.fetchall()

    return [{"message": row[0], "count": row[1]} for row in rows]


async def query_timeout_failures(db: aiosqlite.Connection, cutoff_date: str = "2026-08-19") -> dict[str, Any]:
    """Analyze timeout-related failures."""
    # Check failures with timeout errors
    timeout_sql = """
        SELECT COUNT(*)
        FROM task_runs
        WHERE status = 'failed'
          AND (error_message LIKE '%timeout%' OR error_message LIKE '%Timed out%')
    """
    async with db.execute(timeout_sql) as cur:
        row = await cur.fetchone()
        timeout_count = row[0]
    before_sql = f"""
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failed
        FROM task_runs
        WHERE date(started_at) < '{cutoff_date}'
    """
    async with db.execute(before_sql) as cur:
        row = await cur.fetchone()
        before_total = row[0] or 0
        before_failed = row[1] or 0

    after_sql = f"""
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failed
        FROM task_runs
        WHERE date(started_at) >= '{cutoff_date}'
    """
    async with db.execute(after_sql) as cur:
        row = await cur.fetchone()
        after_total = row[0] or 0
        after_failed = row[1] or 0

    before_rate = (before_failed / before_total * 100) if before_total > 0 else 0
    after_rate = (after_failed / after_total * 100) if after_total > 0 else 0

    return {
        "timeout_failures": timeout_count,
        "before_fix": {
            "total": before_total,
            "failed": before_failed,
            "rate": round(before_rate, 1),
        },
        "after_fix": {
            "total": after_total,
            "failed": after_failed,
            "rate": round(after_rate, 1),
        },
    }


async def query_budget_failures(db: aiosqlite.Connection) -> int:
    """Count budget-exceeded failures."""
    sql = """
        SELECT COUNT(*)
        FROM task_runs
        WHERE status = 'failed'
          AND error_message LIKE '%budget%'
    """
    async with db.execute(sql) as cur:
        row = await cur.fetchone()
        return row[0]


async def query_gate_failures(db: aiosqlite.Connection) -> int:
    """Count gate check failures (no substantive changes)."""
    sql = """
        SELECT COUNT(*)
        FROM task_runs
        WHERE status = 'failed'
          AND (error_message LIKE '%no code changes%'
            OR error_message LIKE '%no substantive changes%'
            OR error_message LIKE '%gate check%')
    """
    async with db.execute(sql) as cur:
        row = await cur.fetchone()
        return row[0]


async def main() -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Audit SOVA failure patterns")
    parser.add_argument("--detailed", action="store_true", help="Show detailed error messages")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument(
        "--cutoff-date",
        default="2026-08-19",
        help="Date for before/after comparison (YYYY-MM-DD, default: 2026-08-19)",
    )
    args = parser.parse_args()

    try:
        db_path = await get_sova_db_path()
        if not db_path.exists():
            print(f"Error: SOVA database not found at {db_path}", file=sys.stderr)
            return 1

        # Open read-only connection
        uri = db_path.as_uri() + "?mode=ro"
        async with aiosqlite.connect(uri, uri=True) as db:
            db.row_factory = aiosqlite.Row

            # Gather all data
            overall = await query_overall_stats(db)
            step_failures = await query_failures_by_step(db)
            error_clusters = await query_error_clusters(db)
            timeout_data = await query_timeout_failures(db, args.cutoff_date)
            budget_failures = await query_budget_failures(db)
            gate_failures = await query_gate_failures(db)

        if args.json:
            # JSON output for programmatic use
            result = {
                "overall": overall,
                "step_failures": step_failures,
                "error_clusters": error_clusters,
                "timeout_analysis": timeout_data,
                "budget_failures": budget_failures,
                "gate_failures": gate_failures,
            }
            print(json.dumps(result, indent=2, default=str))
        else:
            # Human-readable report
            print("=" * 80)
            print("SOVA PROJECT FAILURE AUDIT")
            print("=" * 80)
            print()

            print("OVERALL STATISTICS")
            print("-" * 80)
            print(f"Total Runs:       {overall['total']}")
            print(f"  Done:           {overall['done']} ({overall['success_rate']}%)")
            print(f"  Failed:         {overall['failed']} ({overall['failure_rate']}%)")
            print(f"  Interrupted:    {overall['interrupted']}")
            print(f"  Rejected:       {overall['rejected']}")
            print(f"  In Progress:    {overall['in_progress']}")
            print(f"Total Cost:       ${overall['total_cost']:.2f}")
            print()

            print("TIMEOUT ANALYSIS")
            print("-" * 80)
            print(f"Timeout Failures: {timeout_data['timeout_failures']}")
            print()
            print(f"Before Timeout Fix (#687, #699) - before {args.cutoff_date}:")
            print(
                f"  Total: {timeout_data['before_fix']['total']}, "
                f"Failed: {timeout_data['before_fix']['failed']} "
                f"({timeout_data['before_fix']['rate']}%)"
            )
            print()
            print(f"After Timeout Fix - {args.cutoff_date} onwards:")
            print(
                f"  Total: {timeout_data['after_fix']['total']}, "
                f"Failed: {timeout_data['after_fix']['failed']} "
                f"({timeout_data['after_fix']['rate']}%)"
            )

            if timeout_data["before_fix"]["total"] > 0 and timeout_data["after_fix"]["total"] > 0:
                improvement = timeout_data["before_fix"]["rate"] - timeout_data["after_fix"]["rate"]
                print(f"  Improvement: {improvement:.1f} percentage points")
            print()

            print("FAILURE CATEGORIZATION")
            print("-" * 80)
            print(f"Budget Exceeded:     {budget_failures}")
            print(f"Gate Check Failures: {gate_failures}")
            print(f"Timeout Failures:    {timeout_data['timeout_failures']}")
            print()

            print("TOP 15 FAILURE-PRONE STEPS")
            print("-" * 80)
            for i, step in enumerate(step_failures[:15], 1):
                print(f"{i:2}. {step['step']:20s} {step['failures']:3}/{step['total']:3} ({step['rate']:5.1f}%)")
            print()

            if args.detailed:
                print("TOP 20 ERROR MESSAGE CLUSTERS")
                print("-" * 80)
                for i, cluster in enumerate(error_clusters[:20], 1):
                    msg = cluster["message"]
                    if len(msg) > 120:
                        msg = msg[:117] + "..."
                    print(f"{i:2}. [{cluster['count']:2}x] {msg}")
                print()

        return 0

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        if args.json:
            print(json.dumps({"error": str(e)}), file=sys.stdout)
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
