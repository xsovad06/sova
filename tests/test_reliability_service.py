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

    def test_network_outage_wins_over_process_exit_shape(self) -> None:
        # Verbatim from a real outage (Gwym run 1612). _build_exit_failure_message()
        # wraps the real cause in the "Process exited with code N" shape, so
        # without the leading network rule _PROCESS_EXIT_RE claims this first and
        # an outage is reported as a generic nonzero exit.
        msg = (
            "Process exited with code 1; last output: "
            "[stderr] RuntimeError: Failed to fetch issue #659: error connecting to api.github.com | "
            "[stderr] check your internet connection or https://githubstatus.com"
        )
        assert reliability_service.classify_failure_cause(msg) == "network_unreachable"

    def test_network_outage_wins_over_fix_llm_prefix(self) -> None:
        # The fix_llm_* buckets exist to measure how well SOVA's own fix loop
        # converges (issue #977); an outage-caused timeout left in that bucket
        # is a false data point for exactly that measurement.
        msg = "fix_llm_timeout on cycle 1: Can't reach the API server (ENOTFOUND)"
        assert reliability_service.classify_failure_cause(msg) == "network_unreachable"

    def test_claude_cli_dns_failure(self) -> None:
        msg = (
            "Claude CLI failed (exit 1): terminal_reason=api_error; is_error=true; "
            "API Error: Can't reach the API server — check your internet or DNS (ENOTFOUND)"
        )
        assert reliability_service.classify_failure_cause(msg) == "network_unreachable"

    def test_git_push_ssh_transport_failure(self) -> None:
        msg = (
            "Command failed: git push -u origin feat/issue-659\nExit code: 128\n"
            "stderr: ssh: connect to host github.com port 22: Operation timed out\n"
            "fatal: Could not read from remote repository."
        )
        assert reliability_service.classify_failure_cause(msg) == "network_unreachable"

    def test_local_timeout_is_not_reclassified_as_network(self) -> None:
        # The new leading rule must not swallow unrelated buckets: a bare local
        # timeout has no remote endpoint to corroborate it.
        assert reliability_service.classify_failure_cause("step_hard_timeout") == "step_timeout"

    def test_llm_billing_delegates_to_classify_error(self) -> None:
        # billing/rate-limit/model-unavailable are ambiguous labels too (see
        # _AMBIGUOUS_LLM_LABELS): an LLM context marker is required, same as
        # llm_provider_unavailable/llm_timeout below.
        msg = "anthropic: budget_exhausted, cannot enforce a budget cap natively"
        assert reliability_service.classify_failure_cause(msg) == "llm_billing"

    def test_llm_rate_limit(self) -> None:
        msg = "anthropic API error: provider returned 429"
        assert reliability_service.classify_failure_cause(msg) == "llm_rate_limit"

    def test_llm_configuration_needs_no_context_marker(self) -> None:
        # Unlike billing/rate-limit/model-unavailable, "llm_configuration" is
        # not ambiguous: the token is SOVA-generated and namespaced
        # (CodexRuntime.spawn()), so it classifies without an LLM context
        # marker, distinct from a billing incident in the reliability view.
        msg = "sova_configuration_error: Codex CLI cannot enforce a budget cap (requested $5.00)"
        assert reliability_service.classify_failure_cause(msg) == "llm_configuration"

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


def _month_start(months_back: int = 0) -> datetime:
    index = NOW.year * 12 + NOW.month - 1 - months_back
    return datetime(index // 12, index % 12 + 1, 10, 12, tzinfo=timezone.utc)


def _done(
    role: str | None, *, started_at: datetime | None = None, minutes: int | None = 10, cost: str = "1"
) -> TaskRun:
    started = started_at or NOW - timedelta(days=1)
    return TaskRun(
        role=role,
        status="done",
        started_at=started,
        ended_at=started + timedelta(minutes=minutes) if minutes is not None else None,
        total_cost_usd=Decimal(cost),
    )


def _cost(run: TaskRun, phase: str = "develop", **kwargs) -> CostRecord:
    values = {"input_tokens": 0, "output_tokens": 0, "model": "claude-sonnet-5-5"} | kwargs
    # Every writer stores cache_tokens as the granular sum (sova/llm/cost.py, agent_db.py).
    values.setdefault("cache_tokens", (values.get("cache_read_tokens") or 0) + (values.get("cache_write_tokens") or 0))
    return CostRecord(task_run_id=run.id, phase=phase, **values)


def _step(run: TaskRun, name: str) -> StepExecution:
    return StepExecution(task_run_id=run.id, step_name=name, status="done")


class TestDisplayRole:
    @pytest.mark.parametrize(
        ("role", "steps", "expected"),
        [
            (None, (), "unknown"),
            ("", (), "unknown"),
            ("command:", (), "unknown"),
            ("command:   ", (), "unknown"),
            ("command:/", (), "unknown"),
            ("command:/integrate-pr 123", (), "command:integrate-pr"),
            ("command:/integrate-pr", (), "command:integrate-pr"),
            ("command:integrate-pr", (), "command:integrate-pr"),
            ("developer", ("develop",), "developer:develop"),
            ("developer", ("address_review",), "developer:address-review"),
            ("developer", ("develop", "address_review"), "developer:develop"),
            ("developer", (), "developer"),
            ("reviewer", ("develop",), "reviewer"),
        ],
    )
    def test_display_role(self, role: str | None, steps: tuple[str, ...], expected: str) -> None:
        assert reliability_service._display_role(role, steps) == expected


class TestStepNamesExist:
    def test_split_step_names_are_registered(self) -> None:
        from sova.core.steps import STEP_REGISTRY

        assert reliability_service._DEVELOP_STEP in STEP_REGISTRY
        assert reliability_service._ADDRESS_REVIEW_STEP in STEP_REGISTRY


class TestMonthWindowStart:
    def test_one_month_is_the_current_month(self) -> None:
        start = reliability_service._month_window_start(1)
        assert (start.year, start.month, start.day) == (NOW.year, NOW.month, 1)
        assert start.tzinfo is not None

    def test_crosses_a_year_boundary(self) -> None:
        start = reliability_service._month_window_start(14)
        expected = _month_start(13)
        assert (start.year, start.month) == (expected.year, expected.month)


class TestGetUsageByRole:
    async def test_empty(self) -> None:
        async with await get_session() as session:
            assert await reliability_service.get_usage_by_role(session) == []

    async def test_averages_per_month_and_role(self) -> None:
        a = _done("reviewer", started_at=_month_start(), minutes=10, cost="1")
        b = _done("reviewer", started_at=_month_start(), minutes=20, cost="2")
        older = _done("reviewer", started_at=_month_start(1), minutes=5, cost="4")
        await _add(a, b, older)
        await _add(
            _cost(a, num_turns=10, input_tokens=100, cache_read_tokens=50, cache_write_tokens=50, output_tokens=20),
            _cost(b, num_turns=20, input_tokens=300, cache_read_tokens=100, cache_write_tokens=None, output_tokens=40),
            _cost(older, num_turns=4, input_tokens=10, output_tokens=2),
        )
        async with await get_session() as session:
            rows = await reliability_service.get_usage_by_role(session, months=3)

        assert [r["month"] for r in rows] == [f"{_month_start(1):%Y-%m}", f"{_month_start():%Y-%m}"]
        previous, current = rows
        assert previous["done_runs"] == 1
        assert previous["avg_cost_usd"] == Decimal("4")
        assert current["role"] == "reviewer"
        assert current["done_runs"] == 2
        assert current["avg_cost_usd"] == Decimal("1.5")
        assert current["avg_duration_seconds"] == 900.0
        assert current["avg_turns"] == 15.0
        assert current["avg_context_tokens"] == 300.0
        assert current["avg_output_tokens"] == 30.0

    async def test_only_done_runs_count(self) -> None:
        failed = _done("reviewer")
        failed.status = "failed"
        await _add(_done("reviewer"), failed)
        async with await get_session() as session:
            rows = await reliability_service.get_usage_by_role(session)
        assert rows[0]["done_runs"] == 1

    async def test_runs_before_the_window_are_excluded(self) -> None:
        await _add(_done("reviewer", started_at=_month_start(5)), _done("triage", started_at=_month_start(1)))
        async with await get_session() as session:
            rows = await reliability_service.get_usage_by_role(session, months=2)
        assert [r["role"] for r in rows] == ["triage"]

    async def test_legacy_command_roles_fold_and_empty_role_is_unknown(self) -> None:
        await _add(
            _done("command:/integrate-pr 123"),
            _done("command:/integrate-pr"),
            _done("command:"),
            _done(""),
        )
        async with await get_session() as session:
            rows = await reliability_service.get_usage_by_role(session)
        counts = {r["role"]: r["done_runs"] for r in rows}
        assert counts == {"command:integrate-pr": 2, "unknown": 2}

    async def test_developer_split_by_step_with_develop_taking_precedence(self) -> None:
        initial = _done("developer")
        addressing = _done("developer")
        both = _done("developer")
        bare = _done("developer")
        await _add(initial, addressing, both, bare)
        await _add(
            _step(initial, "develop"),
            _step(addressing, "address_review"),
            _step(both, "address_review"),
            _step(both, "develop"),
            _step(both, "develop"),
        )
        async with await get_session() as session:
            rows = await reliability_service.get_usage_by_role(session)
        counts = {r["role"]: r["done_runs"] for r in rows}
        assert counts == {"developer": 1, "developer:address-review": 1, "developer:develop": 2}

    async def test_runs_without_cost_records_are_not_averaged_as_zero(self) -> None:
        with_usage = _done("reviewer", cost="2")
        without = _done("reviewer", cost="0")
        await _add(with_usage, without)
        await _add(_cost(with_usage, num_turns=8, input_tokens=100, output_tokens=10))
        async with await get_session() as session:
            (row,) = await reliability_service.get_usage_by_role(session)
        assert row["done_runs"] == 2
        assert row["avg_cost_usd"] == Decimal("1")
        assert row["avg_turns"] == 8.0
        assert row["avg_context_tokens"] == 100.0
        assert row["avg_output_tokens"] == 10.0

    async def test_turns_average_only_over_runs_that_reported_them(self) -> None:
        reported = _done("reviewer")
        legacy = _done("reviewer")
        await _add(reported, legacy)
        await _add(_cost(reported, num_turns=6), _cost(legacy, num_turns=None, input_tokens=40))
        async with await get_session() as session:
            (row,) = await reliability_service.get_usage_by_role(session)
        assert row["avg_turns"] == 6.0

    async def test_turns_none_when_no_run_reported(self) -> None:
        run = _done("reviewer")
        await _add(run)
        await _add(_cost(run, num_turns=None, input_tokens=40))
        async with await get_session() as session:
            (row,) = await reliability_service.get_usage_by_role(session)
        assert row["avg_turns"] is None
        assert row["avg_context_tokens"] == 40.0

    async def test_tokens_none_when_no_run_has_cost_records(self) -> None:
        await _add(_done("reviewer"))
        async with await get_session() as session:
            (row,) = await reliability_service.get_usage_by_role(session)
        assert row["avg_context_tokens"] is None
        assert row["avg_output_tokens"] is None

    async def test_null_ended_at_falls_back_to_cost_record_duration(self) -> None:
        run = _done("reviewer", minutes=None)
        await _add(run)
        await _add(_cost(run, duration_ms=30_000), _cost(run, phase="commit", duration_ms=15_000))
        async with await get_session() as session:
            (row,) = await reliability_service.get_usage_by_role(session)
        assert row["avg_duration_seconds"] == 45.0

    async def test_unknown_duration_is_not_averaged_as_zero(self) -> None:
        unknown = _done("reviewer", minutes=None)
        no_duration = _done("reviewer", minutes=None)
        known = _done("reviewer", minutes=10)
        await _add(unknown, no_duration, known)
        await _add(_cost(no_duration, duration_ms=0))
        async with await get_session() as session:
            (row,) = await reliability_service.get_usage_by_role(session)
        assert row["done_runs"] == 3
        assert row["avg_duration_seconds"] == 600.0

    async def test_duration_none_when_no_run_has_one(self) -> None:
        await _add(_done("reviewer", minutes=None))
        async with await get_session() as session:
            (row,) = await reliability_service.get_usage_by_role(session)
        assert row["avg_duration_seconds"] is None

    async def test_legacy_cache_tokens_without_granular_columns_count_as_context(self) -> None:
        run = _done("reviewer")
        await _add(run)
        await _add(_cost(run, input_tokens=100, cache_tokens=900, cache_read_tokens=None, cache_write_tokens=None))
        async with await get_session() as session:
            (row,) = await reliability_service.get_usage_by_role(session)
            (step,) = await reliability_service.get_usage_by_step(session)
        assert row["avg_context_tokens"] == 1000.0
        assert step["avg_context_tokens"] == 1000.0

    async def test_negative_duration_is_clamped(self) -> None:
        run = _done("reviewer", minutes=-5)
        await _add(run)
        async with await get_session() as session:
            (row,) = await reliability_service.get_usage_by_role(session)
        assert row["avg_duration_seconds"] == 0.0

    async def test_naive_started_at_is_bucketed_as_utc(self) -> None:
        naive = _month_start().replace(tzinfo=None)
        await _add(_done("reviewer", started_at=naive))
        async with await get_session() as session:
            (row,) = await reliability_service.get_usage_by_role(session)
        assert row["month"] == f"{_month_start():%Y-%m}"
        assert row["avg_duration_seconds"] == 600.0


class TestGetUsageByStep:
    async def test_empty(self) -> None:
        async with await get_session() as session:
            assert await reliability_service.get_usage_by_step(session) == []

    async def test_step_rows(self) -> None:
        a = _done("developer")
        b = _done("developer")
        await _add(a, b)
        await _add(
            _cost(
                a,
                "develop",
                num_turns=30,
                input_tokens=100,
                cache_read_tokens=400,
                cache_write_tokens=100,
                output_tokens=50,
                cost_usd=Decimal("1"),
                duration_ms=60_000,
            ),
            _cost(
                b, "develop", num_turns=10, input_tokens=200, output_tokens=150, cost_usd=Decimal("3"), duration_ms=0
            ),
            _cost(a, "commit", num_turns=2),
            _cost(a, "agent", num_turns=99),
        )
        async with await get_session() as session:
            rows = await reliability_service.get_usage_by_step(session)

        assert [r["name"] for r in rows] == ["commit", "develop"]
        develop = rows[1]
        assert develop["kind"] == "step"
        assert develop["command"] is None
        assert develop["runs"] == 2
        assert develop["avg_turns"] == 20.0
        assert develop["avg_model_calls"] is None
        assert develop["avg_context_tokens"] == 400.0
        assert develop["avg_output_tokens"] == 100.0
        assert develop["avg_cost_usd"] == Decimal("2")
        assert develop["avg_duration_seconds"] == 30.0

    async def test_a_steps_records_are_summed_per_run_before_averaging(self) -> None:
        run = _done("developer")
        await _add(run)
        await _add(_cost(run, "develop", num_turns=4), _cost(run, "develop", num_turns=6))
        async with await get_session() as session:
            (row,) = await reliability_service.get_usage_by_step(session)
        assert row["runs"] == 1
        assert row["avg_turns"] == 10.0

    async def test_only_done_runs_within_window(self) -> None:
        failed = _done("developer")
        failed.status = "failed"
        old = _done("developer", started_at=NOW - timedelta(days=60))
        await _add(failed, old)
        await _add(_cost(failed), _cost(old))
        async with await get_session() as session:
            assert await reliability_service.get_usage_by_step(session, days=30) == []

    async def test_command_phase_rows(self) -> None:
        one = _done("command:/integrate-pr 7")
        two = _done("command:/integrate-pr")
        other = _done("command:/address-pr 9")
        await _add(one, two, other)
        one.phase_usage = [
            {"phase": "ci-wait", "model_calls": 4, "context_tokens": 4000, "duration_ms": 10_000},
            {"phase": "merge", "model_calls": 2, "context_tokens": 2000, "duration_ms": 2_000},
        ]
        two.phase_usage = [{"phase": "ci-wait", "model_calls": 8, "context_tokens": 8000, "duration_ms": 30_000}]
        other.phase_usage = [{"phase": "fix", "model_calls": 5, "context_tokens": 500, "duration_ms": 1_000}]
        async with await get_session() as session:
            session.add_all([one, two, other])
            await session.commit()
        await _add(_cost(one, "agent", num_turns=50, output_tokens=999, cost_usd=Decimal("5")))

        async with await get_session() as session:
            rows = await reliability_service.get_usage_by_step(session)

        assert [(r["kind"], r["command"], r["name"]) for r in rows] == [
            ("command_phase", "command:address-pr", "fix"),
            ("command_phase", "command:integrate-pr", "ci-wait"),
            ("command_phase", "command:integrate-pr", "merge"),
        ]
        ci_wait = rows[1]
        assert ci_wait["runs"] == 2
        assert ci_wait["avg_model_calls"] == 6.0
        assert ci_wait["avg_context_tokens"] == 6000.0
        assert ci_wait["avg_duration_seconds"] == 20.0
        assert ci_wait["avg_turns"] is None
        assert ci_wait["avg_output_tokens"] is None
        assert ci_wait["avg_cost_usd"] is None

    async def test_malformed_phase_usage_is_skipped(self) -> None:
        bad_shape = _done("command:/integrate-pr")
        mixed = _done("command:/integrate-pr")
        await _add(bad_shape, mixed)
        bad_shape.phase_usage = {"phase": "merge"}
        mixed.phase_usage = [
            "merge",
            {"phase": "merge"},
            {"phase": "", "model_calls": 1, "context_tokens": 1, "duration_ms": 1},
            {"phase": "merge", "model_calls": True, "context_tokens": 1, "duration_ms": 1},
            {"phase": "merge", "model_calls": -1, "context_tokens": 1, "duration_ms": 1},
            {"phase": "report", "model_calls": 3, "context_tokens": 30, "duration_ms": 3000},
        ]
        async with await get_session() as session:
            session.add_all([bad_shape, mixed])
            await session.commit()
        async with await get_session() as session:
            rows = await reliability_service.get_usage_by_step(session)
        assert [(r["name"], r["runs"]) for r in rows] == [("report", 1)]

    async def test_role_filter_uses_the_display_role(self) -> None:
        initial = _done("developer")
        addressing = _done("developer")
        command = _done("command:/integrate-pr 1")
        await _add(initial, addressing, command)
        await _add(_step(initial, "develop"), _step(addressing, "address_review"))
        await _add(_cost(initial, "develop"), _cost(addressing, "address_review"), _cost(addressing, "commit"))
        command.phase_usage = [{"phase": "merge", "model_calls": 1, "context_tokens": 10, "duration_ms": 1000}]
        async with await get_session() as session:
            session.add(command)
            await session.commit()

        async with await get_session() as session:
            addressed = await reliability_service.get_usage_by_step(session, role="developer:address-review")
            initial_rows = await reliability_service.get_usage_by_step(session, role="developer:develop")
            commands = await reliability_service.get_usage_by_step(session, role="command:integrate-pr")
            nobody = await reliability_service.get_usage_by_step(session, role="nobody")
            everyone = await reliability_service.get_usage_by_step(session)

        assert [r["name"] for r in addressed] == ["address_review", "commit"]
        assert [r["name"] for r in initial_rows] == ["develop"]
        assert [(r["kind"], r["name"]) for r in commands] == [("command_phase", "merge")]
        assert nobody == []
        assert len(everyone) == 4
