"""Tests for resuming outage-failed runs once connectivity returns.

The fixtures reproduce the incident that motivated the feature (Gwym, 2026-10-01):
runs 1610-1613 failed during a 20-minute outage, then the supervisor respawned
both issues four minutes later when the connection came back.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from sova.dashboard.services import agent_recovery
from sova.dashboard.services.agent_recovery import attempt_network_self_heal
from sova.db.models import StepExecution, TaskRun
from sova.db.session import close_db, get_session, init_db

NOW = datetime.now(timezone.utc)

DNS_FAILURE = (
    "Claude CLI failed (exit 1): terminal_reason=api_error; is_error=true; "
    "API Error: Can't reach the API server — check your internet or DNS (ENOTFOUND)"
)
PUSH_FAILURE = (
    "Command failed: git push -u origin feat/issue-659\nExit code: 128\n"
    "stderr: ssh: connect to host github.com port 22: Operation timed out"
)


@pytest.fixture(autouse=True)
async def setup_db():
    os.environ["SOVA_DATABASE_URL"] = "sqlite+aiosqlite://"
    await init_db(run_migrations=False)
    yield
    await close_db()
    os.environ.pop("SOVA_DATABASE_URL", None)


@pytest.fixture
def healthy_network():
    """Connection up and healthy well past the grace period."""
    with patch("sova.supervisor.network_health.get_connectivity_tracker") as mock:
        mock.return_value.is_down.return_value = False
        mock.return_value.healthy_for_seconds.return_value = 600.0
        yield mock.return_value


@pytest.fixture
def guard_config():
    from sova.config.models import NetworkGuardConfig

    cfg = type("Cfg", (), {"network_guard": NetworkGuardConfig()})()
    with patch("sova.config.loader.load_config", return_value=cfg):
        yield cfg


@pytest.fixture
def spawner():
    with patch.object(
        agent_recovery, "attempt_network_self_heal", wraps=attempt_network_self_heal
    ):  # keep the real function
        with patch(
            "sova.dashboard.services.agent_lifecycle.start_agent",
            new_callable=AsyncMock,
            return_value={"run_id": 9999},
        ) as mock_start:
            yield mock_start


async def _add(*objs) -> None:
    async with await get_session() as session:
        for obj in objs:
            session.add(obj)
        await session.commit()


def _run(
    *,
    issue: str,
    status: str,
    role: str = "developer",
    error: str | None = DNS_FAILURE,
    minutes_ago: int = 5,
    cost: str = "0",
    termination_reason: str | None = None,
    pr_number: int | None = None,
) -> TaskRun:
    ended = NOW - timedelta(minutes=minutes_ago)
    return TaskRun(
        issue_number=issue,
        role=role,
        status=status,
        started_at=ended - timedelta(minutes=10),
        ended_at=ended,
        error_message=error,
        total_cost_usd=Decimal(cost),
        termination_reason=termination_reason,
        pr_number=pr_number,
    )


class TestEligibility:
    async def test_resumes_an_outage_failed_run(self, healthy_network, guard_config, spawner) -> None:
        await _add(_run(issue="659", status="failed", error=PUSH_FAILURE, cost="5.65"))

        resumed = await attempt_network_self_heal(Path("/tmp/project"))

        assert len(resumed) == 1
        assert resumed[0]["issue"] == "659"
        # Resume, not respawn: this is what preserves the 14 minutes and $5.65
        # the original run had already spent before its final push failed.
        kwargs = spawner.await_args.kwargs
        assert kwargs["resume_run_id"] == resumed[0]["run_id"]
        assert kwargs["role"] == "developer"

    async def test_ignores_a_failure_that_is_not_network_related(self, healthy_network, guard_config, spawner) -> None:
        # Overlapping the outage window is a filter, never evidence. This is the
        # line between this feature and the general auto-retry that was removed.
        await _add(_run(issue="700", status="failed", error="AssertionError: test_foo failed"))

        assert await attempt_network_self_heal(Path("/tmp/project")) == []
        spawner.assert_not_awaited()

    async def test_ignores_a_run_with_no_error_message(self, healthy_network, guard_config, spawner) -> None:
        await _add(_run(issue="701", status="failed", error=None))
        assert await attempt_network_self_heal(Path("/tmp/project")) == []

    @pytest.mark.parametrize("status", ["done", "stopped", "running", "paused", "awaiting_approval"])
    async def test_ignores_non_failure_statuses(self, healthy_network, guard_config, spawner, status: str) -> None:
        await _add(_run(issue="702", status=status))
        assert await attempt_network_self_heal(Path("/tmp/project")) == []

    async def test_never_resumes_a_deliberate_stop(self, healthy_network, guard_config, spawner) -> None:
        # A stop button or watchdog kill carries a termination_reason. Even if
        # its message mentions the network, undoing a deliberate stop
        # automatically would be wrong.
        await _add(
            _run(
                issue="703",
                status="interrupted",
                error=DNS_FAILURE,
                termination_reason="manual_stop",
            )
        )
        assert await attempt_network_self_heal(Path("/tmp/project")) == []

    async def test_ignores_command_roles(self, healthy_network, guard_config, spawner) -> None:
        # No checkpoint to resume, and the supervisor re-proposes the action.
        await _add(_run(issue="704", status="failed", role="command:address-pr"))
        assert await attempt_network_self_heal(Path("/tmp/project")) == []

    async def test_ignores_reviewer_role(self, healthy_network, guard_config, spawner) -> None:
        # Reviewer is a non-pipeline role with no --resume checkpoint of its
        # own; auto-resuming a failed review risks posting a stale verdict
        # against a PR head that has since moved on.
        await _add(_run(issue="706", status="failed", role="reviewer"))
        assert await attempt_network_self_heal(Path("/tmp/project")) == []

    async def test_ignores_failures_older_than_the_window(self, healthy_network, guard_config, spawner) -> None:
        await _add(_run(issue="705", status="failed", minutes_ago=120))
        assert await attempt_network_self_heal(Path("/tmp/project")) == []


class TestExactlyOnce:
    async def test_skips_an_issue_the_system_already_redid(self, healthy_network, guard_config, spawner) -> None:
        """The motivating incident's own resolution: the supervisor respawned
        both issues once the network returned, so resuming the outage-era runs
        would duplicate work and could open a second PR."""
        await _add(_run(issue="656", status="failed"))
        await _add(_run(issue="656", status="running", error=None, minutes_ago=1))

        assert await attempt_network_self_heal(Path("/tmp/project")) == []
        spawner.assert_not_awaited()

    async def test_skips_an_older_outage_run_behind_a_newer_unrelated_failure(
        self, healthy_network, guard_config, spawner
    ) -> None:
        """A newer failure for the same issue blocks an older outage candidate
        even when that newer run's own cause has nothing to do with the
        network. Resuming the older run would redo work a more recent, albeit
        differently-failed, attempt has already superseded. Rows are added
        oldest first so id order matches the `minutes_ago` timestamps, as it
        always does in production."""
        await _add(
            _run(issue="707", status="failed", error=DNS_FAILURE, minutes_ago=5),
            _run(issue="707", status="failed", error="AssertionError: test_foo failed", minutes_ago=2),
        )

        assert await attempt_network_self_heal(Path("/tmp/project")) == []
        spawner.assert_not_awaited()

    async def test_skips_an_outage_run_behind_a_newer_run_in_an_unmatched_status(
        self, healthy_network, guard_config, spawner
    ) -> None:
        """A newer run the outer query's own filters exclude (here, a status
        outside _SELF_HEAL_STATUSES) must still block an older candidate: it
        never enters the seen_issues loop at all, so only the status-blind
        newer-run check in `_has_newer_live_run` can catch it.

        `_has_newer_live_run` compares by `TaskRun.id`, which in production
        always increases with creation time, so the rows are added here in
        that same order (older first) to keep id order consistent with the
        `minutes_ago` timestamps rather than contradicting them.
        """
        await _add(
            _run(issue="708", status="failed", error=DNS_FAILURE, minutes_ago=5),
            _run(issue="708", status="rejected", error=None, minutes_ago=2),
        )

        assert await attempt_network_self_heal(Path("/tmp/project")) == []
        spawner.assert_not_awaited()

    async def test_skips_a_run_that_was_already_resumed(self, healthy_network, guard_config, spawner) -> None:
        async with await get_session() as session:
            failed = _run(issue="660", status="failed")
            session.add(failed)
            await session.flush()
            # resumed_from_id is written by WorkflowEngine for every --resume
            # spawn, so it marks "already handled" with no extra column.
            session.add(
                TaskRun(
                    issue_number="660",
                    role="developer",
                    status="failed",
                    started_at=NOW,
                    resumed_from_id=failed.id,
                    error_message=DNS_FAILURE,
                )
            )
            await session.commit()

        resumed = await attempt_network_self_heal(Path("/tmp/project"))
        # Nothing is resumed: the original is excluded because something already
        # resumed it, and the resume itself is excluded because chains are
        # capped at one automatic follow-up.
        assert [r["issue"] for r in resumed] == []

    async def test_chains_are_capped_at_one_automatic_follow_up(self, healthy_network, guard_config, spawner) -> None:
        """A resume that also dies from an outage is handed back to a human.

        Without this the flap guard and hourly cap would still bound the waste,
        but a flapping connection could keep spending the retry budget on the
        same work indefinitely.
        """
        await _add(
            TaskRun(
                issue_number="670",
                role="developer",
                status="failed",
                started_at=NOW - timedelta(minutes=8),
                ended_at=NOW - timedelta(minutes=3),
                error_message=PUSH_FAILURE,
                resumed_from_id=4242,
            )
        )

        assert await attempt_network_self_heal(Path("/tmp/project")) == []
        spawner.assert_not_awaited()

    async def test_one_resume_per_tick(self, healthy_network, guard_config, spawner) -> None:
        await _add(
            _run(issue="801", status="failed"),
            _run(issue="802", status="failed"),
            _run(issue="803", status="failed"),
        )

        resumed = await attempt_network_self_heal(Path("/tmp/project"))

        assert len(resumed) == 1
        assert spawner.await_count == 1

    async def test_most_recovered_work_goes_first(self, healthy_network, guard_config, spawner) -> None:
        async with await get_session() as session:
            cheap = _run(issue="901", status="failed", cost="0")
            valuable = _run(issue="902", status="failed", cost="5.65")
            session.add_all([cheap, valuable])
            await session.flush()
            for _ in range(4):
                session.add(StepExecution(task_run_id=valuable.id, step_name="develop", status="done"))
            await session.commit()

        resumed = await attempt_network_self_heal(Path("/tmp/project"))

        assert resumed[0]["issue"] == "902"
        assert resumed[0]["steps_done"] == 4


class TestGuards:
    async def test_holds_off_while_the_connection_is_still_down(self, guard_config, spawner) -> None:
        await _add(_run(issue="661", status="failed"))

        with patch("sova.supervisor.network_health.get_connectivity_tracker") as mock:
            mock.return_value.is_down.return_value = True
            assert await attempt_network_self_heal(Path("/tmp/project")) == []
        spawner.assert_not_awaited()

    async def test_holds_off_until_the_grace_period_passes(self, guard_config, spawner) -> None:
        # A connection that has only just answered may be about to drop again.
        await _add(_run(issue="662", status="failed"))

        with patch("sova.supervisor.network_health.get_connectivity_tracker") as mock:
            mock.return_value.is_down.return_value = False
            mock.return_value.healthy_for_seconds.return_value = 5.0
            assert await attempt_network_self_heal(Path("/tmp/project")) == []
        spawner.assert_not_awaited()

    async def test_respects_the_hourly_cap(self, healthy_network, guard_config, spawner) -> None:
        async with await get_session() as session:
            session.add(_run(issue="663", status="failed"))
            for i in range(3):
                session.add(
                    TaskRun(
                        issue_number=f"90{i}",
                        role="developer",
                        status="done",
                        started_at=NOW - timedelta(minutes=10),
                        resumed_from_id=1000 + i,
                    )
                )
            await session.commit()

        assert await attempt_network_self_heal(Path("/tmp/project")) == []

    async def test_disabled_by_config(self, healthy_network, spawner) -> None:
        from sova.config.models import NetworkGuardConfig

        await _add(_run(issue="664", status="failed"))
        cfg = type("Cfg", (), {"network_guard": NetworkGuardConfig(auto_resume=False)})()
        with patch("sova.config.loader.load_config", return_value=cfg):
            assert await attempt_network_self_heal(Path("/tmp/project")) == []

    async def test_a_rejected_spawn_is_not_reported_as_resumed(self, healthy_network, guard_config) -> None:
        # force=False on purpose, so the slot, memory and conflict gates still
        # apply; a rejection must not be recorded as a successful resume.
        await _add(_run(issue="665", status="failed"))
        with patch(
            "sova.dashboard.services.agent_lifecycle.start_agent",
            new_callable=AsyncMock,
            return_value={"error": "Maximum concurrent agents reached (3)"},
        ):
            assert await attempt_network_self_heal(Path("/tmp/project")) == []

    async def test_db_failure_fails_open(self, healthy_network, guard_config) -> None:
        with patch(
            "sova.dashboard.services.agent_recovery._find_self_heal_candidates",
            new_callable=AsyncMock,
            side_effect=RuntimeError("db down"),
        ):
            assert await attempt_network_self_heal(Path("/tmp/project")) == []


class TestMultiProjectSlugResolution:
    """`_periodic_recovery_loop` is not a request context, so the per-request
    `get_project_slug()` contextvar `start_agent()` falls back to is never set
    for a self-heal resume. In multi-project mode, every registered project is
    swept in turn (`_collect_sweep_dirs(..., is_multi=True)`); without
    resolving the slug explicitly from `project_dir`, every resume would land
    on the unpopulated `_DEFAULT_SLUG` pool regardless of which project the
    candidate actually came from.
    """

    async def test_resume_targets_the_candidates_own_project_slug(self, healthy_network, guard_config, spawner) -> None:
        project_a = Path("/tmp/project-a")
        project_b = Path("/tmp/project-b")
        await _add(_run(issue="910", status="failed", cost="1"))
        await _add(_run(issue="920", status="failed", cost="1"))

        def fake_slug(path: Path) -> str | None:
            return {project_a: "slug-a", project_b: "slug-b"}.get(Path(path))

        with patch("sova.config.registry.find_slug_for_path", side_effect=fake_slug):
            await attempt_network_self_heal(project_a)
            assert spawner.await_args.kwargs["slug"] == "slug-a"

            await attempt_network_self_heal(project_b)
            assert spawner.await_args.kwargs["slug"] == "slug-b"

    async def test_unregistered_project_dir_falls_back_to_default_resolution(
        self, healthy_network, guard_config, spawner
    ) -> None:
        # Single-project mode: the directory isn't in the registry at all, so
        # slug resolution must not error, and must fall through to
        # start_agent()'s own default (get_project_slug() / _DEFAULT_SLUG).
        await _add(_run(issue="930", status="failed"))

        await attempt_network_self_heal(Path("/tmp/unregistered-project"))

        assert spawner.await_args.kwargs["slug"] is None
