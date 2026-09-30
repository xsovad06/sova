"""Tests for reliability_service (issue #979)."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sova.dashboard.services import reliability_service
from sova.db.models import CostRecord, StepExecution, TaskRun
from sova.db.session import close_db, get_session, init_db

NOW = datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
async def setup_db():
    os.environ["SOVA_DATABASE_URL"] = "sqlite+aiosqlite://"
    await init_db(run_migrations=False)
    yield
    await close_db()
    os.environ.pop("SOVA_DATABASE_URL", None)


async def _add(*objs) -> None:
    async with await get_session() as session:
        for obj in objs:
            session.add(obj)
        await session.commit()


def _run(role: str, status: str, *, days_ago: int = 1, cost: str = "0", error: str | None = None) -> TaskRun:
    return TaskRun(
        role=role,
        status=status,
        started_at=NOW - timedelta(days=days_ago),
        total_cost_usd=Decimal(cost),
        error_message=error,
    )


class TestClassifyFailureCause:
    def test_none_message_is_unclassified(self) -> None:
        assert reliability_service.classify_failure_cause(None) == "unclassified"

    def test_empty_message_is_unclassified(self) -> None:
        assert reliability_service.classify_failure_cause("") == "unclassified"

    def test_step_hard_timeout(self) -> None:
        assert reliability_service.classify_failure_cause("step_hard_timeout") == "step_timeout"

    def test_fix_llm_timeout(self) -> None:
        msg = "fix_llm_timeout on cycle 2: LLM call exceeded budget"
        assert reliability_service.classify_failure_cause(msg) == "fix_llm_timeout"

    def test_fix_llm_failed(self) -> None:
        msg = "fix_llm_failed on cycle 1: bad response"
        assert reliability_service.classify_failure_cause(msg) == "fix_llm_failed"

    def test_process_exit_shape(self) -> None:
        msg = "Process exited with code 1 (step=develop); last output: boom"
        assert reliability_service.classify_failure_cause(msg) == "process_exit"

    def test_process_exit_negative_code_from_signal_termination(self) -> None:
        # asyncio reports a signal-terminated process as a negative exit code
        # (e.g. -9 for SIGKILL/OOM, -15 for SIGTERM), per the termination
        # provenance work documented in .claude/rules/architecture.md.
        msg = "Process exited with code -9 (step=develop); last output: oom-killed"
        assert reliability_service.classify_failure_cause(msg) == "process_exit"

    def test_gate_check_reason(self) -> None:
        msg = "No commits ahead of base branch after commit step"
        assert reliability_service.classify_failure_cause(msg) == "gate_check"

    def test_llm_billing_delegates_to_classify_error(self) -> None:
        # billing/rate-limit/model-unavailable are ambiguous labels too (see
        # _AMBIGUOUS_LLM_LABELS): an LLM context marker is required, same as
        # llm_provider_unavailable/llm_timeout below.
        msg = "anthropic: budget_exhausted, cannot enforce a budget cap natively"
        assert reliability_service.classify_failure_cause(msg) == "llm_billing"

    def test_llm_rate_limit(self) -> None:
        msg = "anthropic API error: provider returned 429"
        assert reliability_service.classify_failure_cause(msg) == "llm_rate_limit"

    def test_unmatched_message_is_other(self) -> None:
        assert reliability_service.classify_failure_cause("something totally unexpected happened") == "other"

    def test_multi_match_prefers_terminal_first_like_classify_error(self) -> None:
        # Mirrors classify_error()'s own terminal-first scan: billing wins over rate limit.
        msg = "claude cli: budget_exhausted while provider returned 429"
        assert reliability_service.classify_failure_cause(msg) == "llm_billing"

    @pytest.mark.parametrize(
        "msg",
        [
            # GitHub API rate limiting (sova/supervisor/github_quota.py) carries
            # no LLM-specific vocabulary at all, but would satisfy classify_error()'s
            # rate-limit pattern ("429") on its own.
            "GitHub API rate limit exceeded, retry after 60s (status 429)",
            # CI minutes budget (sova/supervisor/ci_budget.py).
            "CI budget exceeded, skipping checks (budget_exhausted)",
            # CodeRabbit review quota (sova/supervisor/coderabbit_quota.py).
            "CodeRabbit review quota exhausted, rate_limit hit for this repo",
        ],
    )
    def test_non_llm_quota_and_billing_messages_are_not_reported_as_llm_outages(self, msg: str) -> None:
        assert reliability_service.classify_failure_cause(msg) == "other"

    @pytest.mark.parametrize(
        "msg",
        [
            # classify_error()'s "no such file or directory" pattern, which errors.py
            # scopes to LLM invocation details only.
            "git worktree add failed: /p/.claude/worktrees/42: No such file or directory",
            # Its "command not found" pattern, from any shell step.
            "shellcheck: command not found",
            # Its generic "timed out" pattern, from CI polling rather than an LLM call.
            "CI monitoring timed out after 1800s waiting for checks",
        ],
    )
    def test_shell_and_git_failures_are_not_reported_as_llm_outages(self, msg: str) -> None:
        assert reliability_service.classify_failure_cause(msg) == "other"

    @pytest.mark.parametrize(
        ("msg", "expected"),
        [
            ("[Errno 2] No such file or directory: 'claude'", "llm_provider_unavailable"),
            ("claude cli connection refused", "llm_provider_unavailable"),
            ("LLM invocation timed out after 600s", "llm_timeout"),
            ("anthropic request timed out", "llm_timeout"),
            ("anthropic model is not available on your vertex deployment", "llm_model_unavailable"),
            ("claude code rate_limit exceeded, backing off", "llm_rate_limit"),
        ],
    )
    def test_llm_scoped_messages_still_classify(self, msg: str, expected: str) -> None:
        assert reliability_service.classify_failure_cause(msg) == expected

    def test_bare_model_unavailable_without_llm_marker_is_other(self) -> None:
        # "is not available" alone (classify_error()'s ModelUnavailableError pattern)
        # could just as easily describe an unrelated feature-gating message.
        msg = "This action is not available while the repository is archived"
        assert reliability_service.classify_failure_cause(msg) == "other"

    def test_bare_claude_path_segment_does_not_corroborate(self) -> None:
        # Every SOVA path contains ".claude/", so it must not act as an LLM signal.
        msg = "cannot read /home/u/.claude/agent-control/handoff.json: No such file or directory"
        assert reliability_service.classify_failure_cause(msg) == "other"

    def test_bare_provider_word_does_not_corroborate_llm_outage(self) -> None:
        # A generic adapter/network error naming its own "provider" (e.g. a Jira
        # TaskAdapter) must not be misclassified as an LLM outage just because
        # the word "provider" appears somewhere in the message.
        msg = "TaskAdapter for provider 'jira' failed: no such file or directory"
        assert reliability_service.classify_failure_cause(msg) == "other"


class TestStatusPartition:
    def test_failure_and_incomplete_partition_terminal_statuses(self) -> None:
        """get_success_by_role() promises done + failures + incomplete == total_runs.

        That only holds while _FAILURE_STATUSES and _INCOMPLETE_STATUSES together
        cover every terminal status except "done". A new TaskRun status added to
        TASK_RUN_TERMINAL without a bucket here would land in total_runs and in
        neither column, silently breaking the reconciliation.
        """
        from sova.core.state import TASK_RUN_TERMINAL

        buckets = reliability_service._FAILURE_STATUSES | reliability_service._INCOMPLETE_STATUSES
        assert buckets == TASK_RUN_TERMINAL - {"done"}
        assert not (reliability_service._FAILURE_STATUSES & reliability_service._INCOMPLETE_STATUSES)


class TestGetSuccessByRole:
    async def test_empty_when_no_terminal_runs(self) -> None:
        async with await get_session() as session:
            rows = await reliability_service.get_success_by_role(session)
        assert rows == []

    async def test_basic_rate(self) -> None:
        await _add(*[_run("developer", "done") for _ in range(6)], *[_run("developer", "failed") for _ in range(4)])
        async with await get_session() as session:
            rows = await reliability_service.get_success_by_role(session)
        assert len(rows) == 1
        row = rows[0]
        assert row["role"] == "developer"
        assert row["total_runs"] == 10
        assert row["done"] == 6
        assert row["failures"] == 4
        assert row["success_rate"] == 0.6
        assert row["insufficient_data"] is False

    async def test_insufficient_data_below_min_runs(self) -> None:
        await _add(_run("researcher", "done"), _run("researcher", "failed"))
        async with await get_session() as session:
            rows = await reliability_service.get_success_by_role(session)
        assert rows[0]["insufficient_data"] is True
        assert rows[0]["total_runs"] == 2
        assert rows[0]["success_rate"] == 0.5

    async def test_excludes_still_running_runs(self) -> None:
        await _add(*[_run("developer", "done") for _ in range(10)], _run("developer", "running"))
        async with await get_session() as session:
            rows = await reliability_service.get_success_by_role(session)
        assert rows[0]["total_runs"] == 10

    async def test_paused_and_awaiting_approval_are_incomplete_not_failure(self) -> None:
        await _add(
            *[_run("developer", "done") for _ in range(8)],
            _run("developer", "paused"),
            _run("developer", "awaiting_approval"),
        )
        async with await get_session() as session:
            rows = await reliability_service.get_success_by_role(session)
        row = rows[0]
        assert row["total_runs"] == 10
        assert row["done"] == 8
        assert row["failures"] == 0
        assert row["incomplete"] == 2
        assert row["success_rate"] == 0.8

    async def test_unknown_role_string_still_appears(self) -> None:
        await _add(*[_run("some-removed-custom-role", "done") for _ in range(10)])
        async with await get_session() as session:
            rows = await reliability_service.get_success_by_role(session)
        assert rows[0]["role"] == "some-removed-custom-role"

    async def test_window_excludes_runs_outside_days(self) -> None:
        await _add(_run("developer", "done", days_ago=1), _run("developer", "done", days_ago=45))
        async with await get_session() as session:
            rows = await reliability_service.get_success_by_role(session, days=30)
        assert rows[0]["total_runs"] == 1

    async def test_naive_started_at_is_still_counted_within_window(self) -> None:
        # Mirrors get_monthly_projection()'s guard against timezone-naive
        # started_at values from older rows: a naive datetime representing a
        # recent UTC instant must still land inside the window filter, not be
        # silently excluded (or crash) because it carries no tzinfo.
        naive_recent = TaskRun(
            role="developer",
            status="done",
            started_at=(NOW - timedelta(days=1)).replace(tzinfo=None),
            total_cost_usd=Decimal("0"),
        )
        await _add(naive_recent)
        async with await get_session() as session:
            rows = await reliability_service.get_success_by_role(session, days=30)
        assert rows[0]["total_runs"] == 1
        assert rows[0]["done"] == 1

    async def test_null_role_normalizes_to_unknown_instead_of_crashing_sort(self) -> None:
        # TaskRun.role is nullable=False, so a NULL role cannot be written through
        # the ORM or even a raw INSERT against this schema; it can only arise from
        # historical data written before a migration added the constraint (the
        # kind of schema drift documented in .claude/rules/architecture.md). The
        # DB round-trip can't construct that state, so the query result is faked
        # directly: a None role must not reach sorted(), which raises TypeError
        # comparing None to str.
        class FakeResult:
            def all(self):
                return [SimpleNamespace(role=None, status="done", count=1)]

        session = AsyncMock()
        session.execute.return_value = FakeResult()
        rows = await reliability_service.get_success_by_role(session)
        assert rows[0]["role"] == "unknown"
        assert rows[0]["total_runs"] == 1


class TestGetFailureTaxonomy:
    async def test_empty_when_no_failures(self) -> None:
        await _add(*[_run("developer", "done") for _ in range(5)])
        async with await get_session() as session:
            rows = await reliability_service.get_failure_taxonomy(session)
        assert rows == []

    async def test_groups_by_cause_and_sums_to_failure_total(self) -> None:
        await _add(
            _run("developer", "failed", error="step_hard_timeout"),
            _run("developer", "failed", error="step_hard_timeout"),
            _run("developer", "rejected", error=None),
            _run("developer", "interrupted", error="Process exited with code 1"),
        )
        async with await get_session() as session:
            rows = await reliability_service.get_failure_taxonomy(session)
        total = sum(row["count"] for row in rows)
        assert total == 4
        by_cause = {row["cause"]: row["count"] for row in rows}
        assert by_cause["step_timeout"] == 2
        assert by_cause["unclassified"] == 1
        assert by_cause["process_exit"] == 1

    async def test_sorted_highest_count_first(self) -> None:
        await _add(
            *[_run("developer", "failed", error="step_hard_timeout") for _ in range(3)],
            _run("developer", "failed", error="odd one-off failure"),
        )
        async with await get_session() as session:
            rows = await reliability_service.get_failure_taxonomy(session)
        assert rows[0]["cause"] == "step_timeout"
        assert rows[0]["count"] == 3

    async def test_top_failing_step_from_step_executions(self) -> None:
        run = _run("developer", "failed", error="No commits ahead of base branch after commit step")
        await _add(run)
        async with await get_session() as session:
            step = StepExecution(task_run_id=run.id, step_name="commit", status="failed")
            session.add(step)
            await session.commit()

        async with await get_session() as session:
            rows = await reliability_service.get_failure_taxonomy(session)
        assert rows[0]["top_failing_step"] == "commit"

    async def test_top_failing_step_is_the_first_failed_step_of_a_run(self) -> None:
        # A run with several failed steps must report deterministically, not
        # whichever row the DB happened to return last.
        run = _run("developer", "failed", error="step_hard_timeout")
        await _add(run)
        async with await get_session() as session:
            session.add(StepExecution(task_run_id=run.id, step_name="develop", status="failed"))
            session.add(StepExecution(task_run_id=run.id, step_name="validate", status="failed"))
            await session.commit()

        async with await get_session() as session:
            rows = await reliability_service.get_failure_taxonomy(session)
        assert rows[0]["top_failing_step"] == "develop"

    async def test_step_lookup_is_scoped_to_the_window(self) -> None:
        # The subquery must carry the same window predicate as the run fetch, so a
        # failed step on an out-of-window run never leaks into the taxonomy.
        old_run = _run("developer", "failed", error="step_hard_timeout", days_ago=45)
        recent = _run("developer", "failed", error="step_hard_timeout", days_ago=1)
        await _add(old_run, recent)
        async with await get_session() as session:
            session.add(StepExecution(task_run_id=old_run.id, step_name="push", status="failed"))
            await session.commit()

        async with await get_session() as session:
            rows = await reliability_service.get_failure_taxonomy(session, days=30)
        assert rows[0]["count"] == 1
        assert rows[0]["top_failing_step"] is None

    async def test_top_failing_step_none_when_no_step_execution(self) -> None:
        await _add(_run("developer", "interrupted", error=None))
        async with await get_session() as session:
            rows = await reliability_service.get_failure_taxonomy(session)
        assert rows[0]["top_failing_step"] is None


class TestGetSpendByOutcome:
    async def test_empty_when_no_terminal_runs(self) -> None:
        async with await get_session() as session:
            data = await reliability_service.get_spend_by_outcome(session)
        assert data["completing"]["run_count"] == 0
        assert data["completing"]["cost_usd"] == Decimal("0")
        assert data["non_completing"]["run_count"] == 0

    async def test_splits_completing_and_non_completing(self) -> None:
        await _add(
            _run("developer", "done", cost="1.50"),
            _run("developer", "done", cost="2.00"),
            _run("developer", "failed", cost="0.75"),
            _run("developer", "paused", cost="0.25"),
        )
        async with await get_session() as session:
            data = await reliability_service.get_spend_by_outcome(session)
        assert data["completing"]["run_count"] == 2
        assert data["completing"]["cost_usd"] == Decimal("3.5")
        assert data["non_completing"]["run_count"] == 2
        assert data["non_completing"]["cost_usd"] == Decimal("1")

    async def test_excludes_running_runs(self) -> None:
        await _add(_run("developer", "done", cost="1.00"), _run("developer", "running", cost="5.00"))
        async with await get_session() as session:
            data = await reliability_service.get_spend_by_outcome(session)
        assert data["completing"]["cost_usd"] == Decimal("1")
        assert data["completing"]["run_count"] == 1

    async def test_tokens_joined_from_cost_record(self) -> None:
        run = _run("developer", "done", cost="1.00")
        await _add(run)
        async with await get_session() as session:
            session.add(
                CostRecord(
                    task_run_id=run.id,
                    phase="develop",
                    model="claude-sonnet-5",
                    input_tokens=100,
                    output_tokens=50,
                )
            )
            await session.commit()

        async with await get_session() as session:
            data = await reliability_service.get_spend_by_outcome(session)
        assert data["completing"]["tokens_in"] == 100
        assert data["completing"]["tokens_out"] == 50

    async def test_orphan_cost_records_are_ignored(self) -> None:
        run = _run("developer", "done", cost="1.00")
        await _add(run)
        async with await get_session() as session:
            session.add(CostRecord(task_run_id=None, phase="develop", model="claude-sonnet-5", input_tokens=999))
            await session.commit()

        async with await get_session() as session:
            data = await reliability_service.get_spend_by_outcome(session)
        assert data["completing"]["tokens_in"] == 0

    async def test_window_excludes_runs_outside_days(self) -> None:
        run = _run("developer", "done", cost="1.00", days_ago=1)
        old_run = _run("developer", "done", cost="9.00", days_ago=45)
        await _add(run, old_run)
        async with await get_session() as session:
            session.add(
                CostRecord(
                    task_run_id=run.id, phase="develop", model="claude-sonnet-5", input_tokens=10, output_tokens=5
                )
            )
            session.add(
                CostRecord(
                    task_run_id=old_run.id,
                    phase="develop",
                    model="claude-sonnet-5",
                    input_tokens=999,
                    output_tokens=999,
                )
            )
            await session.commit()

        async with await get_session() as session:
            data = await reliability_service.get_spend_by_outcome(session, days=30)
        assert data["completing"]["run_count"] == 1
        assert data["completing"]["cost_usd"] == Decimal("1")
        assert data["completing"]["tokens_in"] == 10
        assert data["completing"]["tokens_out"] == 5

    async def test_non_completing_statuses_matches_terminal_minus_done(self) -> None:
        """reliability.html derives its "Non-Completing" card label from this list
        instead of hardcoding the status set, so it cannot drift silently (the same
        class of bug TestStatusPartition guards against for the failure/incomplete
        buckets).
        """
        from sova.core.state import TASK_RUN_TERMINAL

        async with await get_session() as session:
            data = await reliability_service.get_spend_by_outcome(session)
        assert set(data["non_completing_statuses"]) == TASK_RUN_TERMINAL - {"done"}
        assert data["non_completing_statuses"] == sorted(data["non_completing_statuses"])
