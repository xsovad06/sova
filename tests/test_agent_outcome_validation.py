"""agent outcome validation: command and pipeline outcomes, PR pushed and diff guards.

Split from tests/test_dashboard.py; see docs/testing-guidelines.md, "Test File Layout".
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sova.db.models import TaskRun
from sova.db.session import get_session

# Uses the shared per-worker in-memory DB, emptied before each test
# (see ``tests/conftest.py:reset_db_rows``). A test here that leaves a
# background task touching the DB when its event loop closes (the real
# ``start_agent()`` calls in ``TestDuplicateAgentPrevention``) can invalidate the
# shared connection; the next test's sweep recreates the schema.
pytestmark = pytest.mark.usefixtures("reset_db_rows")


# ---------------------------------------------------------------------------
# Verdict parsing from output lines
# ---------------------------------------------------------------------------


class TestCommandOutcomeValidation:
    """_validate_command_outcome checks whether command runs produced expected outcomes."""

    @pytest.mark.parametrize(
        "role",
        [
            pytest.param("developer", id="non-command-role"),
            pytest.param("command:some-other-command", id="unknown-command"),
        ],
    )
    async def test_role_skipped(self, role: str) -> None:
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_db import _validate_command_outcome
        from sova.dashboard.services.agent_pool import AgentState

        agent = MagicMock(spec=AgentState)
        agent.role = role
        result = await _validate_command_outcome(1, agent)
        assert result.failure_reason is None

    @pytest.mark.parametrize("command", ["rearrange-commits", "pr"])
    async def test_squash_command_dispatches_to_diff_guard(self, command: str) -> None:
        """rearrange-commits and pr both rewrite history by soft-resetting and recommitting.

        Neither has an address-pr-style push tier to confirm first (rearrange-commits
        never pushes, and pr may be creating the PR), so _COMMAND_VALIDATORS must route
        each straight to _check_pr_diff_guard rather than leaving it unguarded like an
        unknown command (issue #1251).
        """
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services.agent_db import OutcomeValidation, _validate_command_outcome
        from sova.dashboard.services.agent_pool import AgentState

        agent = MagicMock(spec=AgentState)
        agent.role = f"command:{command}"

        with patch(
            "sova.dashboard.services.agent_db._check_pr_diff_guard",
            new_callable=AsyncMock,
            return_value=OutcomeValidation(f"{command} possible silent revert: shared.py"),
        ) as mock_guard:
            result = await _validate_command_outcome(1, agent)

        mock_guard.assert_awaited_once_with(agent)
        assert result.failure_reason is not None
        assert "silent revert" in result.failure_reason

    async def test_address_pr_fails_without_push_evidence(self) -> None:
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_db import _validate_command_outcome
        from sova.dashboard.services.agent_pool import AgentState
        from sova.db.models import OutputLine

        session = await get_session()
        async with session.begin():
            run = TaskRun(issue_number="310", role="command:address-pr", status="done", pr_number=100)
            session.add(run)
            await session.flush()
            run_id = run.id

            session.add(OutputLine(task_run_id=run_id, line_number=1, text="All findings already addressed."))
            session.add(OutputLine(task_run_id=run_id, line_number=2, text="Tests pass. Summary complete."))

        agent = MagicMock(spec=AgentState)
        agent.role = "command:address-pr"
        agent.pr_number = 100
        agent.pre_run_sha = None
        agent.project_dir = None
        result = await _validate_command_outcome(run_id, agent)
        assert result.failure_reason is not None
        assert "baseline was unavailable" in result.failure_reason

    async def test_address_pr_output_claim_is_not_push_evidence(self) -> None:
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_db import _validate_command_outcome
        from sova.dashboard.services.agent_pool import AgentState
        from sova.db.models import OutputLine

        session = await get_session()
        async with session.begin():
            run = TaskRun(issue_number="311", role="command:address-pr", status="done", pr_number=101)
            session.add(run)
            await session.flush()
            run_id = run.id

            session.add(OutputLine(task_run_id=run_id, line_number=1, text="Fixed all findings."))
            session.add(OutputLine(task_run_id=run_id, line_number=2, text="Committed: fix(core): address review"))
            session.add(OutputLine(task_run_id=run_id, line_number=3, text="git push --force-with-lease"))

        agent = MagicMock(spec=AgentState)
        agent.role = "command:address-pr"
        agent.pr_number = 101
        agent.pre_run_sha = None
        agent.project_dir = None
        result = await _validate_command_outcome(run_id, agent)
        assert result.failure_reason is not None
        assert "baseline was unavailable" in result.failure_reason

    async def test_address_pr_passes_when_pr_head_changed(self) -> None:
        """A changed remote PR head confirms that this run pushed."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services.agent_db import _VALID, _validate_command_outcome
        from sova.dashboard.services.agent_pool import AgentState

        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(issue_number="312", role="command:address-pr", status="done", pr_number=102)
                session.add(run)
                await session.flush()
                run_id = run.id

        agent = MagicMock(spec=AgentState)
        agent.role = "command:address-pr"
        agent.pr_number = 102
        agent.pre_run_sha = "aaa111"
        agent.project_dir = None
        with (
            patch(
                "sova.dashboard.services.agent_db._fetch_output_lines",
                new_callable=AsyncMock,
                return_value=["some output"],
            ),
            patch(
                "sova.dashboard.services.agent_db._check_pr_pushed_via_sha",
                new_callable=AsyncMock,
                return_value=(True, False),
            ),
            patch(
                "sova.dashboard.services.agent_db._check_address_summary",
                new_callable=AsyncMock,
                return_value=(True, False),
            ),
            patch("sova.dashboard.services.agent_db._check_pr_diff_guard", new_callable=AsyncMock, return_value=_VALID),
        ):
            result = await _validate_command_outcome(run_id, agent)
        assert result.failure_reason is None

    async def test_address_pr_fails_when_pr_head_unchanged(self) -> None:
        """An unchanged remote PR head proves this run made no push."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services.agent_db import _validate_command_outcome
        from sova.dashboard.services.agent_pool import AgentState

        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(issue_number="313", role="command:address-pr", status="done", pr_number=103)
                session.add(run)
                await session.flush()
                run_id = run.id

        agent = MagicMock(spec=AgentState)
        agent.role = "command:address-pr"
        agent.pr_number = 103
        agent.pre_run_sha = "aaa111"
        agent.project_dir = None
        with (
            patch(
                "sova.dashboard.services.agent_db._fetch_output_lines",
                new_callable=AsyncMock,
                return_value=["some output"],
            ),
            patch(
                "sova.dashboard.services.agent_db._check_pr_pushed_via_sha",
                new_callable=AsyncMock,
                return_value=(False, False),
            ),
        ):
            result = await _validate_command_outcome(run_id, agent)
        assert result.failure_reason is not None
        assert "without pushing" in result.failure_reason

    async def test_review_pr_fails_without_post_evidence(self) -> None:
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_db import _validate_command_outcome
        from sova.dashboard.services.agent_pool import AgentState
        from sova.db.models import OutputLine

        session = await get_session()
        async with session.begin():
            run = TaskRun(issue_number="312", role="command:review-pr", status="done", pr_number=102)
            session.add(run)
            await session.flush()
            run_id = run.id

            session.add(OutputLine(task_run_id=run_id, line_number=1, text="Reviewed the code."))
            session.add(OutputLine(task_run_id=run_id, line_number=2, text="Verdict: Approve: looks good"))

        agent = MagicMock(spec=AgentState)
        agent.role = "command:review-pr"
        agent.pr_number = 102
        agent.project_dir = None
        result = await _validate_command_outcome(run_id, agent)
        assert result.failure_reason is not None
        assert "without posting a review" in result.failure_reason

    async def test_review_pr_passes_with_post_evidence(self) -> None:
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_db import _validate_command_outcome
        from sova.dashboard.services.agent_pool import AgentState
        from sova.db.models import OutputLine

        session = await get_session()
        async with session.begin():
            run = TaskRun(issue_number="313", role="command:review-pr", status="done", pr_number=103)
            session.add(run)
            await session.flush()
            run_id = run.id

            session.add(OutputLine(task_run_id=run_id, line_number=1, text="Analysis complete."))
            session.add(
                OutputLine(
                    task_run_id=run_id,
                    line_number=2,
                    text="Review posted at: https://github.com/org/repo/pull/103#pullrequestreview-123",
                )
            )

        agent = MagicMock(spec=AgentState)
        agent.role = "command:review-pr"
        agent.pr_number = 103
        agent.project_dir = None
        result = await _validate_command_outcome(run_id, agent)
        assert result.failure_reason is None

    async def test_no_output_lines_fails_validation(self) -> None:
        """When no output lines exist, fail-closed: agent never ran meaningfully."""
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_db import _validate_command_outcome
        from sova.dashboard.services.agent_pool import AgentState

        session = await get_session()
        async with session.begin():
            run = TaskRun(issue_number="314", role="command:address-pr", status="done", pr_number=104)
            session.add(run)
            await session.flush()
            run_id = run.id

        agent = MagicMock(spec=AgentState)
        agent.role = "command:address-pr"
        agent.pr_number = 104
        agent.pre_run_sha = None
        agent.project_dir = None
        result = await _validate_command_outcome(run_id, agent)
        assert result.failure_reason is not None
        assert "no output" in result.failure_reason


class TestReviewPrVerdictPersistence:
    """_validate_review_pr extracts verdict marker and persists handoff_json."""

    @pytest.mark.parametrize(
        ("lines", "expected"),
        [
            pytest.param(
                ["some output", "<!-- sova-review: approve -->", "more output"], ("approve", None), id="approve"
            ),
            pytest.param(["<!-- sova-review: revise -->"], ("revise", None), id="revise"),
            pytest.param(["<!-- sova-review: block -->"], ("block", None), id="block"),
            pytest.param(["<!-- sova-review: APPROVE -->"], ("approve", None), id="case-insensitive"),
            pytest.param(
                ["<!-- sova-review: approve -->", "<!-- sova-review: revise -->"], ("revise", None), id="last-wins"
            ),
            pytest.param(
                ["<!-- sova-review: approve --> <!-- sova-review: revise -->"],
                ("revise", None),
                id="same-line-last-wins",
            ),
            pytest.param(["<!--  sova-review:  approve  -->"], ("approve", None), id="extra-whitespace"),
            pytest.param(["<!-- sova-review: revise sha=abc1234 -->"], ("revise", "abc1234"), id="captures-sha"),
        ],
    )
    async def test_extract_marker(self, lines: list[str], expected: tuple[str, str | None]) -> None:
        from sova.dashboard.services.agent_db import _extract_review_verdict_marker

        assert _extract_review_verdict_marker(lines) == expected

    async def test_extract_marker_none_when_absent(self) -> None:
        from sova.dashboard.services.agent_db import _extract_review_verdict_marker

        lines = ["no marker here", "just regular output"]
        assert _extract_review_verdict_marker(lines) is None

    async def test_persist_writes_handoff_json_approve(self) -> None:
        from sova.dashboard.services.agent_db import _persist_review_verdict

        session = await get_session()
        async with session.begin():
            run = TaskRun(issue_number="359", role="command:review-pr", status="done", pr_number=200)
            session.add(run)
            await session.flush()
            run_id = run.id

        await _persist_review_verdict(run_id, "approve", None)

        async with await get_session() as session:
            run = await session.get(TaskRun, run_id)
            assert run.handoff_json is not None
            assert run.handoff_json["next_action"] == "approve"
            assert run.handoff_json["pending_findings"] == []

    async def test_persist_writes_handoff_json_revise(self) -> None:
        from sova.dashboard.services.agent_db import _persist_review_verdict

        session = await get_session()
        async with session.begin():
            run = TaskRun(issue_number="359", role="command:review-pr", status="done", pr_number=201)
            session.add(run)
            await session.flush()
            run_id = run.id

        await _persist_review_verdict(run_id, "revise", None)

        async with await get_session() as session:
            run = await session.get(TaskRun, run_id)
            assert run.handoff_json is not None
            assert run.handoff_json["next_action"] == "address_review"

    async def test_persist_writes_handoff_json_block(self) -> None:
        from sova.dashboard.services.agent_db import _persist_review_verdict

        session = await get_session()
        async with session.begin():
            run = TaskRun(issue_number="359", role="command:review-pr", status="done", pr_number=202)
            session.add(run)
            await session.flush()
            run_id = run.id

        await _persist_review_verdict(run_id, "block", None)

        async with await get_session() as session:
            run = await session.get(TaskRun, run_id)
            assert run.handoff_json["next_action"] == "address_review"

    async def test_persist_writes_review_head_sha_metadata(self) -> None:
        from sova.dashboard.services.agent_db import _persist_review_verdict

        session = await get_session()
        async with session.begin():
            run = TaskRun(issue_number="359", role="command:review-pr", status="done", pr_number=209)
            session.add(run)
            await session.flush()
            run_id = run.id

        await _persist_review_verdict(run_id, "revise", None, sha="abc1234")

        async with await get_session() as session:
            run = await session.get(TaskRun, run_id)
            assert run.handoff_json["metadata"]["review_head_sha"] == "abc1234"

    async def test_persist_skips_when_handoff_already_set(self) -> None:
        from sova.dashboard.services.agent_db import _persist_review_verdict

        existing = {"next_action": "approve", "pending_findings": []}
        session = await get_session()
        async with session.begin():
            run = TaskRun(
                issue_number="359",
                role="command:review-pr",
                status="done",
                pr_number=203,
                handoff_json=existing,
            )
            session.add(run)
            await session.flush()
            run_id = run.id

        await _persist_review_verdict(run_id, "revise", None)

        async with await get_session() as session:
            run = await session.get(TaskRun, run_id)
            assert run.handoff_json["next_action"] == "approve"

    async def test_validate_review_pr_persists_verdict_from_marker(self) -> None:
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_db import _validate_review_pr
        from sova.dashboard.services.agent_pool import AgentState
        from sova.db.models import OutputLine

        session = await get_session()
        async with session.begin():
            run = TaskRun(issue_number="359", role="command:review-pr", status="done", pr_number=204)
            session.add(run)
            await session.flush()
            run_id = run.id

            session.add(OutputLine(task_run_id=run_id, line_number=1, text="<!-- sova-review: revise sha=deadbeef -->"))
            session.add(
                OutputLine(
                    task_run_id=run_id,
                    line_number=2,
                    text="Review posted at: https://github.com/org/repo/pull/204#pullrequestreview-456",
                )
            )

        agent = MagicMock(spec=AgentState)
        agent.role = "command:review-pr"
        agent.pr_number = 204
        agent.project_dir = None

        result = await _validate_review_pr(run_id, agent)
        assert result.failure_reason is None

        async with await get_session() as session:
            run = await session.get(TaskRun, run_id)
            assert run.handoff_json is not None
            assert run.handoff_json["next_action"] == "address_review"
            assert run.handoff_json["metadata"]["review_head_sha"] == "deadbeef"

    async def test_validate_review_pr_no_marker_defaults_to_revise(self) -> None:
        """Without a sova-review marker, there's nothing left to parse: default to revise."""
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_db import _validate_review_pr
        from sova.dashboard.services.agent_pool import AgentState
        from sova.db.models import OutputLine

        session = await get_session()
        async with session.begin():
            run = TaskRun(issue_number="359", role="command:review-pr", status="done", pr_number=205)
            session.add(run)
            await session.flush()
            run_id = run.id

            session.add(OutputLine(task_run_id=run_id, line_number=1, text="Verdict: Approve"))
            session.add(
                OutputLine(
                    task_run_id=run_id,
                    line_number=2,
                    text="Review posted successfully via pullRequestReview API",
                )
            )

        agent = MagicMock(spec=AgentState)
        agent.role = "command:review-pr"
        agent.pr_number = 205
        agent.project_dir = None

        result = await _validate_review_pr(run_id, agent)
        assert result.failure_reason is None

        async with await get_session() as session:
            run = await session.get(TaskRun, run_id)
            assert run.handoff_json is not None
            assert run.handoff_json["next_action"] == "address_review"

    async def test_validate_review_pr_no_verdict_defaults_to_revise(self) -> None:
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_db import _validate_review_pr
        from sova.dashboard.services.agent_pool import AgentState
        from sova.db.models import OutputLine

        session = await get_session()
        async with session.begin():
            run = TaskRun(issue_number="359", role="command:review-pr", status="done", pr_number=206)
            session.add(run)
            await session.flush()
            run_id = run.id

            session.add(
                OutputLine(
                    task_run_id=run_id,
                    line_number=1,
                    text="Review posted at: https://github.com/org/repo/pull/206#pullrequestreview-789",
                )
            )

        agent = MagicMock(spec=AgentState)
        agent.role = "command:review-pr"
        agent.pr_number = 206
        agent.project_dir = None

        result = await _validate_review_pr(run_id, agent)
        assert result.failure_reason is None

        async with await get_session() as session:
            run = await session.get(TaskRun, run_id)
            assert run.handoff_json is not None
            assert run.handoff_json["next_action"] == "address_review"

    async def test_validate_review_pr_fails_when_no_pr_number(self) -> None:
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_db import _validate_review_pr
        from sova.dashboard.services.agent_pool import AgentState

        agent = MagicMock(spec=AgentState)
        agent.pr_number = None
        agent.project_dir = None

        result = await _validate_review_pr(1, agent)
        assert result.failure_reason == "review-pr run has no associated PR number"

    async def test_validate_review_pr_fails_when_no_output(self) -> None:
        from unittest.mock import MagicMock, patch

        from sova.dashboard.services.agent_db import _validate_review_pr
        from sova.dashboard.services.agent_pool import AgentState

        agent = MagicMock(spec=AgentState)
        agent.pr_number = 300
        agent.project_dir = None

        with patch("sova.dashboard.services.agent_db._fetch_output_lines", return_value=None):
            result = await _validate_review_pr(1, agent)  # run_id is arbitrary; mock bypasses DB
        assert result.failure_reason == "review-pr has no recorded output"

    async def test_validate_review_pr_fails_when_no_post_evidence(self) -> None:
        from unittest.mock import MagicMock, patch

        from sova.dashboard.services.agent_db import _validate_review_pr
        from sova.dashboard.services.agent_pool import AgentState

        agent = MagicMock(spec=AgentState)
        agent.pr_number = 42
        agent.project_dir = None

        with patch(
            "sova.dashboard.services.agent_db._fetch_output_lines",
            return_value=["some unrelated output", "no evidence here"],
        ):
            result = await _validate_review_pr(1, agent)
        assert result.failure_reason == "review-pr completed without posting a review on GitHub"


class TestValidateMergeCommand:
    """_validate_merge_command catches integrate-pr runs that exit

    cleanly without actually merging the PR. For example, a headless agent
    that backgrounds the CI-poll loop and then ends its turn believing
    something will resume it later.
    """

    async def test_skips_when_no_pr_number(self) -> None:
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_db import _validate_merge_command
        from sova.dashboard.services.agent_pool import AgentState

        agent = MagicMock(spec=AgentState)
        agent.role = "command:integrate-pr"
        agent.pr_number = None

        assert (await _validate_merge_command(1, agent)).failure_reason is None

    @pytest.mark.parametrize(
        "state",
        [
            pytest.param("MERGED", id="pr-merged"),
            # A CLOSED (not merged) PR is the command's own documented early-stop, not a failure.
            pytest.param("CLOSED", id="pr-closed"),
        ],
    )
    async def test_passes_when_pr_finished(self, state: str) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services.agent_db import _validate_merge_command
        from sova.dashboard.services.agent_pool import AgentState

        agent = MagicMock(spec=AgentState)
        agent.role = "command:integrate-pr"
        agent.pr_number = 955
        agent.project_dir = None

        mock_cfg = MagicMock(github_repo="owner/repo", github_user="user")
        mock_status = MagicMock(state=state)
        with (
            patch("sova.dashboard.services.agent_db.load_config", return_value=mock_cfg),
            patch("sova.git.pr.get_pr_status", new_callable=AsyncMock, return_value=mock_status),
        ):
            assert (await _validate_merge_command(1, agent)).failure_reason is None

    async def test_fails_when_pr_still_open(self) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services.agent_db import _validate_merge_command
        from sova.dashboard.services.agent_pool import AgentState

        agent = MagicMock(spec=AgentState)
        agent.role = "command:integrate-pr"
        agent.pr_number = 955
        agent.project_dir = None

        mock_cfg = MagicMock(github_repo="owner/repo", github_user="user")
        mock_status = MagicMock(state="OPEN")
        with (
            patch("sova.dashboard.services.agent_db.load_config", return_value=mock_cfg),
            patch("sova.git.pr.get_pr_status", new_callable=AsyncMock, return_value=mock_status),
        ):
            result = await _validate_merge_command(1, agent)
        assert result.failure_reason == "integrate-pr exited without merging PR #955 (state=OPEN)"

    async def test_passes_when_open_but_actively_queued(self, tmp_path: Path) -> None:
        """An enqueued PR is tracked by MergeQueueMonitor independently; not a failure."""
        from datetime import datetime, timezone
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services.agent_db import _validate_merge_command
        from sova.dashboard.services.agent_pool import AgentState
        from sova.db.models import MergeQueueEntry

        session = await get_session()
        async with session.begin():
            session.add(
                MergeQueueEntry(
                    pr_number=956,
                    repo="owner/repo",
                    issue_number="915",
                    project_dir=str(tmp_path),
                    enqueued_at=datetime.now(timezone.utc),
                    github_user="user",
                    branch_name="feat/issue-915",
                    status="queued",
                )
            )

        agent = MagicMock(spec=AgentState)
        agent.role = "command:integrate-pr"
        agent.pr_number = 956
        agent.project_dir = None

        mock_cfg = MagicMock(github_repo="owner/repo", github_user="user")
        mock_status = MagicMock(state="OPEN")
        with (
            patch("sova.dashboard.services.agent_db.load_config", return_value=mock_cfg),
            patch("sova.git.pr.get_pr_status", new_callable=AsyncMock, return_value=mock_status),
        ):
            assert (await _validate_merge_command(1, agent)).failure_reason is None

    async def test_open_with_resolved_queue_entry_still_fails(self, tmp_path: Path) -> None:
        """A resolved (merged/ejected) queue entry no longer excuses an open PR."""
        from datetime import datetime, timezone
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services.agent_db import _validate_merge_command
        from sova.dashboard.services.agent_pool import AgentState
        from sova.db.models import MergeQueueEntry

        session = await get_session()
        async with session.begin():
            session.add(
                MergeQueueEntry(
                    pr_number=957,
                    repo="owner/repo",
                    issue_number="916",
                    project_dir=str(tmp_path),
                    enqueued_at=datetime.now(timezone.utc),
                    github_user="user",
                    branch_name="feat/issue-916",
                    status="ejected",
                    resolved_at=datetime.now(timezone.utc),
                )
            )

        agent = MagicMock(spec=AgentState)
        agent.role = "command:integrate-pr"
        agent.pr_number = 957
        agent.project_dir = None

        mock_cfg = MagicMock(github_repo="owner/repo", github_user="user")
        mock_status = MagicMock(state="OPEN")
        with (
            patch("sova.dashboard.services.agent_db.load_config", return_value=mock_cfg),
            patch("sova.git.pr.get_pr_status", new_callable=AsyncMock, return_value=mock_status),
        ):
            result = await _validate_merge_command(1, agent)
        assert result.failure_reason == "integrate-pr exited without merging PR #957 (state=OPEN)"

    async def test_fails_open_on_gh_api_error(self) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services.agent_db import _validate_merge_command
        from sova.dashboard.services.agent_pool import AgentState

        agent = MagicMock(spec=AgentState)
        agent.role = "command:integrate-pr"
        agent.pr_number = 955
        agent.project_dir = None

        mock_cfg = MagicMock(github_repo="owner/repo", github_user="user")
        with (
            patch("sova.dashboard.services.agent_db.load_config", return_value=mock_cfg),
            patch("sova.git.pr.get_pr_status", new_callable=AsyncMock, side_effect=RuntimeError("not found")),
        ):
            assert (await _validate_merge_command(1, agent)).failure_reason is None

    async def test_skips_when_no_github_repo_configured(self) -> None:
        from unittest.mock import MagicMock, patch

        from sova.dashboard.services.agent_db import _validate_merge_command
        from sova.dashboard.services.agent_pool import AgentState

        agent = MagicMock(spec=AgentState)
        agent.role = "command:integrate-pr"
        agent.pr_number = 955
        agent.project_dir = None

        mock_cfg = MagicMock(github_repo="")
        with patch("sova.dashboard.services.agent_db.load_config", return_value=mock_cfg):
            assert (await _validate_merge_command(1, agent)).failure_reason is None


class TestPipelineOutcomeValidation:
    """_validate_pipeline_outcome checks whether pipeline roles executed their workflows."""

    @pytest.mark.parametrize(
        "role",
        [
            pytest.param("command:address-pr", id="command-role"),
            pytest.param("triage", id="non-pipeline-role"),
            pytest.param(None, id="none-role"),
        ],
    )
    async def test_role_skipped(self, role: str | None) -> None:
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_db import _validate_pipeline_outcome
        from sova.dashboard.services.agent_pool import AgentState

        agent = MagicMock(spec=AgentState)
        agent.role = role
        result = await _validate_pipeline_outcome(1, agent)
        assert result is None

    async def test_developer_pipeline_bypassed(self) -> None:
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_db import _validate_pipeline_outcome
        from sova.dashboard.services.agent_pool import AgentState

        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(
                    issue_number="400",
                    role="developer",
                    status="done",
                    current_step="agent",
                    pr_number=None,
                )
                session.add(run)
                await session.flush()
                run_id = run.id

        agent = MagicMock(spec=AgentState)
        agent.role = "developer"
        agent.project_dir = None
        result = await _validate_pipeline_outcome(run_id, agent)
        assert result is not None
        assert "Pipeline bypassed" in result
        assert "no PR was created" in result

    async def test_researcher_pipeline_bypassed(self) -> None:
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_db import _validate_pipeline_outcome
        from sova.dashboard.services.agent_pool import AgentState

        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(
                    issue_number="401",
                    role="researcher",
                    status="done",
                    current_step="agent",
                )
                session.add(run)
                await session.flush()
                run_id = run.id

        agent = MagicMock(spec=AgentState)
        agent.role = "researcher"
        agent.project_dir = None
        result = await _validate_pipeline_outcome(run_id, agent)
        assert result is not None
        assert "Pipeline bypassed" in result
        assert "no PR was created" not in result

    async def test_planner_pipeline_bypassed(self) -> None:
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_db import _validate_pipeline_outcome
        from sova.dashboard.services.agent_pool import AgentState

        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(
                    issue_number="402",
                    role="planner",
                    status="done",
                    current_step="agent",
                )
                session.add(run)
                await session.flush()
                run_id = run.id

        agent = MagicMock(spec=AgentState)
        agent.role = "planner"
        agent.project_dir = None
        result = await _validate_pipeline_outcome(run_id, agent)
        assert result is not None
        assert "Pipeline bypassed" in result

    @pytest.mark.parametrize(
        ("current_step", "pr_number", "steps"),
        [
            pytest.param(
                "create_pr", 42, [("develop", "done"), ("create_pr", "done")], id="developer-pipeline-ran-successfully"
            ),
            # Only early steps (before commit) ran: legitimate early stop, not a failure.
            pytest.param("assess", None, [("sync", "done"), ("assess", "done")], id="no-commit-step-no-pr-ok"),
            # Commit step with non-done status should not trigger the check.
            pytest.param("commit", None, [("develop", "done"), ("commit", "failed")], id="commit-failed-no-downgrade"),
        ],
    )
    async def test_developer_outcome_valid(
        self, current_step: str, pr_number: int | None, steps: list[tuple[str, str]]
    ) -> None:
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_db import _validate_pipeline_outcome
        from sova.dashboard.services.agent_pool import AgentState
        from sova.db.models import StepExecution

        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(
                    issue_number="403",
                    role="developer",
                    status="done",
                    current_step=current_step,
                    pr_number=pr_number,
                )
                session.add(run)
                await session.flush()
                run_id = run.id
                for step_name, step_status in steps:
                    session.add(StepExecution(task_run_id=run_id, step_name=step_name, status=step_status))

        agent = MagicMock(spec=AgentState)
        agent.role = "developer"
        agent.project_dir = None
        result = await _validate_pipeline_outcome(run_id, agent)
        assert result is None

    async def test_developer_pipeline_ran_no_pr_after_create_pr(self) -> None:
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_db import _validate_pipeline_outcome
        from sova.dashboard.services.agent_pool import AgentState
        from sova.db.models import StepExecution

        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(
                    issue_number="404",
                    role="developer",
                    status="done",
                    current_step="push",
                    pr_number=None,
                )
                session.add(run)
                await session.flush()
                run_id = run.id
                session.add(StepExecution(task_run_id=run_id, step_name="develop", status="done"))
                session.add(StepExecution(task_run_id=run_id, step_name="create_pr", status="done"))

        agent = MagicMock(spec=AgentState)
        agent.role = "developer"
        agent.project_dir = None
        result = await _validate_pipeline_outcome(run_id, agent)
        assert result is not None
        assert "Pipeline incomplete" in result

    async def test_developer_early_step_no_pr_ok(self) -> None:
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_db import _validate_pipeline_outcome
        from sova.dashboard.services.agent_pool import AgentState
        from sova.db.models import StepExecution

        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(
                    issue_number="405",
                    role="developer",
                    status="done",
                    current_step="develop",
                    pr_number=None,
                )
                session.add(run)
                await session.flush()
                run_id = run.id
                session.add(StepExecution(task_run_id=run_id, step_name="develop", status="done"))

        agent = MagicMock(spec=AgentState)
        agent.role = "developer"
        agent.project_dir = None
        result = await _validate_pipeline_outcome(run_id, agent)
        assert result is None

    async def test_sentinel_cleared_zero_steps(self) -> None:
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_db import _validate_pipeline_outcome
        from sova.dashboard.services.agent_pool import AgentState

        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(
                    issue_number="406",
                    role="developer",
                    status="done",
                    current_step=None,
                )
                session.add(run)
                await session.flush()
                run_id = run.id

        agent = MagicMock(spec=AgentState)
        agent.role = "developer"
        agent.project_dir = None
        result = await _validate_pipeline_outcome(run_id, agent)
        assert result is None

    async def test_sentinel_active_with_steps(self) -> None:
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_db import _validate_pipeline_outcome
        from sova.dashboard.services.agent_pool import AgentState
        from sova.db.models import StepExecution

        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(
                    issue_number="407",
                    role="developer",
                    status="done",
                    current_step="agent",
                )
                session.add(run)
                await session.flush()
                run_id = run.id
                session.add(StepExecution(task_run_id=run_id, step_name="develop", status="done"))

        agent = MagicMock(spec=AgentState)
        agent.role = "developer"
        agent.project_dir = None
        result = await _validate_pipeline_outcome(run_id, agent)
        assert result is None

    async def test_logs_prompt_on_bypass(self) -> None:
        from unittest.mock import MagicMock, patch

        from sova.dashboard.services.agent_db import _validate_pipeline_outcome
        from sova.dashboard.services.agent_pool import AgentState

        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(
                    issue_number="412",
                    role="developer",
                    status="done",
                    current_step="agent",
                    pr_number=None,
                )
                session.add(run)
                await session.flush()
                run_id = run.id

        agent = MagicMock(spec=AgentState)
        agent.role = "developer"
        agent.project_dir = None
        agent.prompt = "Run sova run 99 --run-id 500"

        with patch("sova.dashboard.services.agent_db.log") as mock_log:
            result = await _validate_pipeline_outcome(run_id, agent)

        assert result is not None
        assert "Pipeline bypassed" in result
        mock_log.warning.assert_called_once()
        call_args = mock_log.warning.call_args
        assert call_args[0][0] == "validate_pipeline.bypass_diagnostic"
        assert "Run sova run 99" in call_args[1]["prompt_sent"]

    async def test_developer_commit_done_no_push_no_pr(self) -> None:
        """Commit step ran but push/create_pr did not: pipeline stopped mid-flight."""
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_db import _validate_pipeline_outcome
        from sova.dashboard.services.agent_pool import AgentState
        from sova.db.models import StepExecution

        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(
                    issue_number="450",
                    role="developer",
                    status="done",
                    current_step="commit",
                    pr_number=None,
                )
                session.add(run)
                await session.flush()
                run_id = run.id
                session.add(StepExecution(task_run_id=run_id, step_name="develop", status="done"))
                session.add(StepExecution(task_run_id=run_id, step_name="commit", status="done"))

        agent = MagicMock(spec=AgentState)
        agent.role = "developer"
        agent.project_dir = None
        result = await _validate_pipeline_outcome(run_id, agent)
        assert result is not None
        assert "committed code but never pushed" in result

    async def test_developer_commit_done_with_pr_ok(self) -> None:
        """Commit step ran and pr_number is set: not a mid-flight stop."""
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_db import _validate_pipeline_outcome
        from sova.dashboard.services.agent_pool import AgentState
        from sova.db.models import StepExecution

        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(
                    issue_number="451",
                    role="developer",
                    status="done",
                    current_step="create_pr",
                    pr_number=99,
                )
                session.add(run)
                await session.flush()
                run_id = run.id
                session.add(StepExecution(task_run_id=run_id, step_name="develop", status="done"))
                session.add(StepExecution(task_run_id=run_id, step_name="commit", status="done"))
                session.add(StepExecution(task_run_id=run_id, step_name="push", status="done"))
                session.add(StepExecution(task_run_id=run_id, step_name="create_pr", status="done"))

        agent = MagicMock(spec=AgentState)
        agent.role = "developer"
        agent.project_dir = None
        result = await _validate_pipeline_outcome(run_id, agent)
        assert result is None

    async def test_researcher_commit_check_not_applied(self) -> None:
        """Researcher role should not trigger the commit-without-push check."""
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_db import _validate_pipeline_outcome
        from sova.dashboard.services.agent_pool import AgentState
        from sova.db.models import StepExecution

        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(
                    issue_number="453",
                    role="researcher",
                    status="done",
                    current_step="research",
                    pr_number=None,
                )
                session.add(run)
                await session.flush()
                run_id = run.id
                session.add(StepExecution(task_run_id=run_id, step_name="research", status="done"))

        agent = MagicMock(spec=AgentState)
        agent.role = "researcher"
        agent.project_dir = None
        result = await _validate_pipeline_outcome(run_id, agent)
        assert result is None

    async def test_bypass_message_includes_diagnostics(self, tmp_path: Path) -> None:
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_db import _validate_pipeline_outcome
        from sova.dashboard.services.agent_pool import AgentState

        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(
                    issue_number="832",
                    role="researcher",
                    status="done",
                    current_step="agent",
                    worktree_path=str(tmp_path / "worktree" / "42"),
                )
                session.add(run)
                await session.flush()
                run_id = run.id

        agent = MagicMock(spec=AgentState)
        agent.role = "researcher"
        agent.project_dir = tmp_path / "project"
        agent.prompt = None
        result = await _validate_pipeline_outcome(run_id, agent)
        assert result is not None
        assert "Pipeline bypassed" in result
        assert f"worktree={tmp_path / 'worktree' / '42'}" in result
        assert f"project_dir={tmp_path / 'project'}" in result
        assert "started_at=" in result

    async def test_bypass_message_omits_empty_diagnostics(self) -> None:
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_db import _validate_pipeline_outcome
        from sova.dashboard.services.agent_pool import AgentState

        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(
                    issue_number="833",
                    role="researcher",
                    status="done",
                    current_step="agent",
                )
                session.add(run)
                await session.flush()
                run_id = run.id

        agent = MagicMock(spec=AgentState)
        agent.role = "researcher"
        agent.project_dir = None
        agent.prompt = None
        result = await _validate_pipeline_outcome(run_id, agent)
        assert result is not None
        assert "Pipeline bypassed" in result
        assert "started_at=" in result
        assert "worktree=" not in result


class TestReadFileHandoff:
    def test_returns_none_when_no_file(self, tmp_path: Path) -> None:
        from sova.dashboard.services.agent_db import _read_file_handoff

        assert _read_file_handoff(tmp_path) is None

    def test_reads_valid_handoff(self, tmp_path: Path) -> None:
        import json

        from sova.dashboard.services.agent_db import _read_file_handoff

        control_dir = tmp_path / ".claude" / "agent-control"
        control_dir.mkdir(parents=True)
        handoff = {
            "id": "test",
            "source": "reviewer",
            "status": "awaiting_action",
            "issue": "42",
            "pr_number": 99,
            "summary": "test",
            "details": {"next_action": "address_review", "findings": [{"severity": 5}]},
            "next_actions": [],
        }
        (control_dir / "handoff.json").write_text(json.dumps(handoff))

        result = _read_file_handoff(tmp_path)
        assert result is not None
        assert result["issue"] == "42"
        assert result["pr_number"] == 99
        assert result["details"]["next_action"] == "address_review"
        assert result["source"] == "reviewer"

    def test_returns_none_on_corrupt_json(self, tmp_path: Path) -> None:
        from sova.dashboard.services.agent_db import _read_file_handoff

        control_dir = tmp_path / ".claude" / "agent-control"
        control_dir.mkdir(parents=True)
        (control_dir / "handoff.json").write_text("not json")

        assert _read_file_handoff(tmp_path) is None


# ---------------------------------------------------------------------------
# Pipeline variant detection: get_step_progress
# ---------------------------------------------------------------------------


class TestAgentContextHelpers:
    """Tests for agent_context helper functions."""

    @pytest.mark.parametrize(
        ("content", "expected"),
        [
            pytest.param("---\nname: test\n---\nBody content", "Body content", id="removes-yaml"),
            pytest.param("Just regular content", "Just regular content", id="no-frontmatter-unchanged"),
        ],
    )
    def test_strip_frontmatter(self, content: str, expected: str) -> None:
        from sova.dashboard.services.agent_context import _strip_frontmatter

        assert _strip_frontmatter(content) == expected

    def test_resolve_command_prompt_local_command(self, tmp_path: Path) -> None:
        """_resolve_command_prompt returns slash command for local project commands."""
        from sova.dashboard.services.agent_context import _resolve_command_prompt

        cmd_dir = tmp_path / ".claude" / "commands"
        cmd_dir.mkdir(parents=True)
        (cmd_dir / "test-cmd.md").write_text("# Test")

        result = _resolve_command_prompt("test-cmd", {"issue": "42"}, tmp_path)
        assert result == "/test-cmd issue=42"

    def test_resolve_command_prompt_local_command_no_args(self, tmp_path: Path) -> None:
        """_resolve_command_prompt returns slash command without args."""
        from sova.dashboard.services.agent_context import _resolve_command_prompt

        cmd_dir = tmp_path / ".claude" / "commands"
        cmd_dir.mkdir(parents=True)
        (cmd_dir / "test-cmd.md").write_text("# Test")

        result = _resolve_command_prompt("test-cmd", None, tmp_path)
        assert result == "/test-cmd"

    def test_resolve_command_prompt_fallback(self, tmp_path: Path) -> None:
        """_resolve_command_prompt returns slash command when no file found."""
        from sova.dashboard.services.agent_context import _resolve_command_prompt

        result = _resolve_command_prompt("nonexistent", {"x": "1"}, tmp_path)
        assert result == "/nonexistent x=1"

    def test_known_command_names_includes_project_local_command(self, tmp_path: Path) -> None:
        """A command in the project's own .claude/commands/ is in the known set."""
        from sova.dashboard.services.agent_context import known_command_names

        cmd_dir = tmp_path / ".claude" / "commands"
        cmd_dir.mkdir(parents=True)
        (cmd_dir / "test-cmd.md").write_text("---\nname: test-cmd\n---\n# Test")

        assert "test-cmd" in known_command_names(tmp_path)

    def test_known_command_names_includes_sova_fallback_command(self, tmp_path: Path) -> None:
        """A project with no local override still resolves SOVA's own catalog,
        matching _resolve_command_prompt's own fallback lookup order."""
        from sova.dashboard.services.agent_context import known_command_names

        # A real SOVA-distributed command, never vendored into this empty project dir.
        assert "integrate-pr" in known_command_names(tmp_path)

    def test_known_command_names_excludes_unknown_name(self, tmp_path: Path) -> None:
        """A name with no backing file anywhere is not in the known set."""
        from sova.dashboard.services.agent_context import known_command_names

        assert "nonexistent" not in known_command_names(tmp_path)
        assert "../../etc/passwd" not in known_command_names(tmp_path)

    def test_known_command_names_includes_file_with_no_frontmatter_name(self, tmp_path: Path) -> None:
        """A resolvable command file with no frontmatter `name:` field is still known.

        `_resolve_command_prompt` matches by filename alone (`target_cmd.is_file()`),
        not by parsing frontmatter, so a validator built from `sova.commands.catalog.discover()`
        (which skips files with no `name:` field) would reject a command it would
        actually resolve. `.claude/commands/agent-resume.md` and `request-review.md`
        are real examples of this in SOVA's own command set.
        """
        from sova.dashboard.services.agent_context import known_command_names

        cmd_dir = tmp_path / ".claude" / "commands"
        cmd_dir.mkdir(parents=True)
        (cmd_dir / "no-name-field.md").write_text("---\ndescription: no name field\n---\n# Body")

        assert "no-name-field" in known_command_names(tmp_path)

    def test_known_command_names_excludes_directory_matching_glob(self, tmp_path: Path) -> None:
        """A directory named like a command file (`rogue.md/`) is not a known command.

        `glob("*.md")` matches directories as well as files; `_resolve_command_prompt`
        only ever resolves a real file (`target_cmd.is_file()`), so a directory match
        here would let `known_command_names` falsely validate a name that endpoint can
        never actually serve, and that resolver's own fallback would then stringify
        it as a bare `/rogue` sent to the agent with no backing file.
        """
        from sova.dashboard.services.agent_context import known_command_names

        cmd_dir = tmp_path / ".claude" / "commands"
        cmd_dir.mkdir(parents=True)
        (cmd_dir / "rogue.md").mkdir()

        assert "rogue" not in known_command_names(tmp_path)

    async def test_resolve_command_context_with_issue(self, tmp_path: Path) -> None:
        """_resolve_command_context extracts issue from args."""
        from sova.dashboard.services.agent_context import _resolve_command_context

        pr_number, issue = await _resolve_command_context({"issue": "42"}, "develop", tmp_path)
        assert pr_number is None
        assert issue == "42"

    async def test_resolve_command_context_with_pr(self, tmp_path: Path) -> None:
        """_resolve_command_context extracts pr_number from args."""
        from sova.dashboard.services.agent_context import _resolve_command_context

        pr_number, issue = await _resolve_command_context({"issue": "42", "pr": "100"}, "develop", tmp_path)
        assert pr_number == 100
        assert issue == "42"

    async def test_resolve_command_context_invalid_pr(self, tmp_path: Path) -> None:
        """_resolve_command_context handles non-numeric PR gracefully."""
        from sova.dashboard.services.agent_context import _resolve_command_context

        pr_number, issue = await _resolve_command_context({"pr": "abc"}, "develop", tmp_path)
        assert pr_number is None
        assert issue == ""

    async def test_resolve_command_context_no_issue_no_pr(self, tmp_path: Path) -> None:
        """Without issue or PR, issue is empty: never the command name."""
        from sova.dashboard.services.agent_context import _resolve_command_context

        pr_number, issue = await _resolve_command_context({}, "address-pr", tmp_path)
        assert pr_number is None
        assert issue == ""

    async def test_resolve_command_context_pr_resolves_issue(self, tmp_path: Path) -> None:
        """_resolve_command_context resolves issue from PR when issue is missing."""
        from unittest.mock import AsyncMock, patch

        from sova.dashboard.services.agent_context import _resolve_command_context

        with patch(
            "sova.dashboard.services.agent_context._resolve_issue_from_pr",
            new_callable=AsyncMock,
            return_value="55",
        ):
            pr_number, issue = await _resolve_command_context({"pr": "100"}, "develop", tmp_path)
        assert pr_number == 100
        assert issue == "55"

    async def test_resolve_project_gh_env_success(self, tmp_path: Path) -> None:
        """_resolve_project_gh_env returns env dict on success."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services.agent_context import _resolve_project_gh_env

        mock_cfg = MagicMock()
        mock_cfg.github_user = "testuser"
        with (
            patch("sova.dashboard.services.agent_context.load_config", return_value=mock_cfg),
            patch(
                "sova.utils.gh.resolve_gh_env",
                new_callable=AsyncMock,
                return_value={"GH_TOKEN": "tok"},
            ),
        ):
            result = await _resolve_project_gh_env(tmp_path)
        assert result == {"GH_TOKEN": "tok"}

    async def test_resolve_project_gh_env_failure(self, tmp_path: Path) -> None:
        """_resolve_project_gh_env returns None on failure."""
        from unittest.mock import patch

        from sova.dashboard.services.agent_context import _resolve_project_gh_env

        with patch(
            "sova.dashboard.services.agent_context.load_config",
            side_effect=Exception("no config"),
        ):
            result = await _resolve_project_gh_env(tmp_path)
        assert result is None

    async def test_resolve_issue_from_pr_non_numeric(self, tmp_path: Path) -> None:
        """_resolve_issue_from_pr handles non-numeric pr_number gracefully."""
        from sova.dashboard.services.agent_context import _resolve_issue_from_pr

        result = await _resolve_issue_from_pr("abc", tmp_path)
        assert result == ""

    def test_merge_mcp_env_no_mcp_returns_gh_env(self, tmp_path: Path) -> None:
        """merge_mcp_env returns gh_env unchanged when MCP is disabled."""
        from unittest.mock import patch

        from sova.dashboard.services.agent_context import merge_mcp_env

        gh_env = {"GH_TOKEN": "tok", "PATH": "/usr/bin"}
        with patch(
            "sova.dashboard.services.agent_context._resolve_mcp_env",
            return_value={},
        ):
            result = merge_mcp_env(gh_env, 1, tmp_path)
        assert result == {"GH_TOKEN": "tok", "PATH": "/usr/bin"}

    def test_merge_mcp_env_no_mcp_none_gh_env_stays_none(self, tmp_path: Path) -> None:
        """merge_mcp_env passes through None when MCP is disabled (subprocess inherits parent env)."""
        from unittest.mock import patch

        from sova.dashboard.services.agent_context import merge_mcp_env

        with patch(
            "sova.dashboard.services.agent_context._resolve_mcp_env",
            return_value={},
        ):
            result = merge_mcp_env(None, 1, tmp_path)
        assert result is None

    def test_merge_mcp_env_merges_into_gh_env(self, tmp_path: Path) -> None:
        """merge_mcp_env adds MCP vars on top of the resolved gh_env."""
        from unittest.mock import patch

        from sova.dashboard.services.agent_context import merge_mcp_env

        gh_env = {"GH_TOKEN": "tok", "PATH": "/usr/bin"}
        with patch(
            "sova.dashboard.services.agent_context._resolve_mcp_env",
            return_value={"SOVA_MCP_TOKEN": "mtok", "SOVA_MCP_URL": "http://x/mcp"},
        ):
            result = merge_mcp_env(gh_env, 1, tmp_path)
        assert result["GH_TOKEN"] == "tok"
        assert result["PATH"] == "/usr/bin"
        assert result["SOVA_MCP_TOKEN"] == "mtok"
        assert result["SOVA_MCP_URL"] == "http://x/mcp"

    def test_merge_mcp_env_none_gh_env_falls_back_to_os_environ(self, tmp_path: Path) -> None:
        """Regression: when gh_env is None but MCP is enabled, the result must inherit os.environ.

        Previously the function returned only the two MCP keys, dropping PATH, which
        made the agent subprocess spawn fail with FileNotFoundError ('sova' not on PATH).
        """
        from unittest.mock import patch

        from sova.dashboard.services.agent_context import merge_mcp_env

        with (
            patch(
                "sova.dashboard.services.agent_context._resolve_mcp_env",
                return_value={"SOVA_MCP_TOKEN": "mtok", "SOVA_MCP_URL": "http://x/mcp"},
            ),
            patch.dict(
                "sova.dashboard.services.agent_context.os.environ",
                {"PATH": "/custom/bin", "HOME": "/home/x"},
                clear=True,
            ),
        ):
            result = merge_mcp_env(None, 1, tmp_path)
        assert result is not None
        assert result["PATH"] == "/custom/bin"
        assert result["HOME"] == "/home/x"
        assert result["SOVA_MCP_TOKEN"] == "mtok"
        assert result["SOVA_MCP_URL"] == "http://x/mcp"

    async def test_resolve_issue_from_pr_success(self, tmp_path: Path) -> None:
        """_resolve_issue_from_pr extracts issue from PR body."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services.agent_context import _resolve_issue_from_pr

        mock_result = MagicMock()
        mock_result.success = True
        mock_result.stdout = json.dumps({"body": "Some text\nCloses #42\nMore text", "headRefName": ""})
        with (
            patch(
                "sova.dashboard.services.agent_context.run_shell",
                new_callable=AsyncMock,
                return_value=mock_result,
            ),
            patch(
                "sova.dashboard.services.agent_context._is_issue",
                new_callable=AsyncMock,
                return_value=True,
            ),
        ):
            result = await _resolve_issue_from_pr(100, tmp_path)
        assert result == "42"

    async def test_resolve_issue_from_pr_no_match(self, tmp_path: Path) -> None:
        """_resolve_issue_from_pr returns empty when no issue link found."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services.agent_context import _resolve_issue_from_pr

        mock_result = MagicMock()
        mock_result.success = True
        mock_result.stdout = json.dumps({"body": "No issue link here", "headRefName": ""})
        with patch(
            "sova.dashboard.services.agent_context.run_shell",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            result = await _resolve_issue_from_pr(100, tmp_path)
        assert result == ""


class TestIsIssue:
    """Unit tests for the _is_issue helper."""

    @pytest.mark.parametrize(
        ("issue_number", "success", "stdout", "expected"),
        [
            # The Issues API returns no pull_request field (empty output): a real issue.
            pytest.param("42", True, "", True, id="true-for-real-issue"),
            # The Issues API returns a pull_request field: it is a PR.
            pytest.param("339", True, '{"url": "https://..."}', False, id="false-for-pr"),
            # The gh command fails (success=False).
            pytest.param("99", False, "", False, id="false-on-failed-command"),
        ],
    )
    async def test_classifies_number(
        self, tmp_path: Path, monkeypatch, issue_number: str, success: bool, stdout: str, expected: bool
    ) -> None:
        from unittest.mock import AsyncMock

        from sova.dashboard.services.agent_context import _is_issue

        mock_result = AsyncMock()
        mock_result.return_value.success = success
        mock_result.return_value.stdout = stdout
        monkeypatch.setattr("sova.dashboard.services.agent_context.run_shell", mock_result)

        assert await _is_issue(issue_number, tmp_path) is expected

    async def test_returns_true_on_exception(self, tmp_path: Path, monkeypatch) -> None:
        """Returns True (safe default) when run_shell raises."""
        from unittest.mock import AsyncMock

        from sova.dashboard.services.agent_context import _is_issue

        monkeypatch.setattr(
            "sova.dashboard.services.agent_context.run_shell",
            AsyncMock(side_effect=OSError("gh not found")),
        )

        assert await _is_issue("99", tmp_path) is True


class TestCheckPrBranchPushed:
    """Unit tests for the _check_pr_branch_pushed helper."""

    async def test_returns_none_when_no_pr_number(self, tmp_path: Path) -> None:
        """Returns None immediately when agent has no pr_number."""
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_db import _check_pr_branch_pushed
        from sova.dashboard.services.agent_pool import AgentState

        agent = MagicMock(spec=AgentState)
        agent.pr_number = 0
        assert await _check_pr_branch_pushed(agent) == (None, False)

    async def test_returns_none_when_branch_lookup_fails(self, tmp_path: Path, monkeypatch) -> None:
        """Returns None when the gh CLI fails to return a branch name."""
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_db import _check_pr_branch_pushed
        from sova.dashboard.services.agent_pool import AgentState

        agent = MagicMock(spec=AgentState)
        agent.pr_number = 100
        agent.project_dir = tmp_path

        async def mock_run(*args, **kwargs):
            result = MagicMock()
            result.is_network_unreachable = False
            result.success = False
            result.stdout = ""
            return result

        monkeypatch.setattr("sova.utils.shell.run", mock_run)
        assert await _check_pr_branch_pushed(agent) == (None, False)

    async def test_returns_none_when_fetch_fails(self, tmp_path: Path, monkeypatch) -> None:
        """Returns None when git fetch fails, to prevent stale-ref false result."""
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_db import _check_pr_branch_pushed
        from sova.dashboard.services.agent_pool import AgentState

        agent = MagicMock(spec=AgentState)
        agent.pr_number = 100
        agent.project_dir = tmp_path
        call_count = 0

        async def mock_run(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            result = MagicMock()
            result.is_network_unreachable = False
            if call_count == 1:
                result.success = True
                result.stdout = "feature/my-branch\n"
            else:
                result.success = False
                result.stdout = ""
            return result

        monkeypatch.setattr("sova.utils.shell.run", mock_run)
        assert await _check_pr_branch_pushed(agent) == (None, False)

    async def test_returns_true_when_no_unpushed_commits(self, tmp_path: Path, monkeypatch) -> None:
        """Returns True when rev-list count is 0 (branch is pushed)."""
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_db import _check_pr_branch_pushed
        from sova.dashboard.services.agent_pool import AgentState

        agent = MagicMock(spec=AgentState)
        agent.pr_number = 100
        agent.project_dir = tmp_path
        call_count = 0

        async def mock_run(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            result = MagicMock()
            result.is_network_unreachable = False
            result.success = True
            if call_count == 1:
                result.stdout = "feature/my-branch\n"
            elif call_count == 2:
                result.stdout = ""
            else:
                result.stdout = "0\n"
            return result

        monkeypatch.setattr("sova.utils.shell.run", mock_run)
        assert await _check_pr_branch_pushed(agent) == (True, False)

    async def test_returns_false_when_unpushed_commits(self, tmp_path: Path, monkeypatch) -> None:
        """Returns False when rev-list count > 0 (commits not pushed)."""
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_db import _check_pr_branch_pushed
        from sova.dashboard.services.agent_pool import AgentState

        agent = MagicMock(spec=AgentState)
        agent.pr_number = 100
        agent.project_dir = tmp_path
        call_count = 0

        async def mock_run(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            result = MagicMock()
            result.is_network_unreachable = False
            result.success = True
            if call_count == 1:
                result.stdout = "feature/my-branch\n"
            elif call_count == 2:
                result.stdout = ""
            else:
                result.stdout = "3\n"
            return result

        monkeypatch.setattr("sova.utils.shell.run", mock_run)
        assert await _check_pr_branch_pushed(agent) == (False, False)


class TestCapturePrHeadSha:
    """Unit tests for _capture_pr_head_sha."""

    async def test_returns_sha_on_success(self, tmp_path: Path, monkeypatch) -> None:
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_db import _capture_pr_head_sha

        async def mock_run(*args, **kwargs):
            result = MagicMock()
            result.success = True
            result.stdout = "abc123def456\n"
            return result

        monkeypatch.setattr("sova.utils.shell.run", mock_run)
        sha = await _capture_pr_head_sha(42, tmp_path)
        assert sha == "abc123def456"

    @pytest.mark.parametrize(
        ("success", "stdout"),
        [
            pytest.param(False, "", id="failure"),
            pytest.param(True, "   \n", id="empty-stdout"),
        ],
    )
    async def test_returns_none_on_unusable_output(
        self, tmp_path: Path, monkeypatch, success: bool, stdout: str
    ) -> None:
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_db import _capture_pr_head_sha

        async def mock_run(*args, **kwargs):
            result = MagicMock()
            result.success = success
            result.stdout = stdout
            return result

        monkeypatch.setattr("sova.utils.shell.run", mock_run)
        sha = await _capture_pr_head_sha(42, tmp_path)
        assert sha is None

    async def test_returns_none_on_exception(self, tmp_path: Path, monkeypatch) -> None:
        from sova.dashboard.services.agent_db import _capture_pr_head_sha

        async def mock_run(*args, **kwargs):
            raise TimeoutError("timed out")

        monkeypatch.setattr("sova.utils.shell.run", mock_run)
        sha = await _capture_pr_head_sha(42, tmp_path)
        assert sha is None


class TestCaptureWorktreeHeadSha:
    """Unit tests for _capture_worktree_head_sha."""

    async def test_returns_sha_on_success(self, tmp_path: Path, monkeypatch) -> None:
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_db import _capture_worktree_head_sha

        async def mock_run(*args, **kwargs):
            result = MagicMock()
            result.success = True
            result.stdout = "abc123def456\n"
            return result

        monkeypatch.setattr("sova.utils.shell.run", mock_run)
        sha = await _capture_worktree_head_sha(tmp_path)
        assert sha == "abc123def456"

    @pytest.mark.parametrize(
        ("success", "stdout"),
        [
            pytest.param(False, "", id="failure"),
            pytest.param(True, "   \n", id="empty-stdout"),
        ],
    )
    async def test_returns_none_on_unusable_output(
        self, tmp_path: Path, monkeypatch, success: bool, stdout: str
    ) -> None:
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_db import _capture_worktree_head_sha

        async def mock_run(*args, **kwargs):
            result = MagicMock()
            result.success = success
            result.stdout = stdout
            return result

        monkeypatch.setattr("sova.utils.shell.run", mock_run)
        sha = await _capture_worktree_head_sha(tmp_path)
        assert sha is None

    async def test_returns_none_on_exception(self, tmp_path: Path, monkeypatch) -> None:
        from sova.dashboard.services.agent_db import _capture_worktree_head_sha

        async def mock_run(*args, **kwargs):
            raise TimeoutError("timed out")

        monkeypatch.setattr("sova.utils.shell.run", mock_run)
        sha = await _capture_worktree_head_sha(tmp_path)
        assert sha is None


class TestCheckPrPushedViaSha:
    """Unit tests for _check_pr_pushed_via_sha."""

    @pytest.mark.parametrize(
        ("pre_run_sha", "pr_number"),
        [
            pytest.param(None, 42, id="no-pre-run-sha"),
            pytest.param("abc123", None, id="no-pr-number"),
        ],
    )
    async def test_returns_none_without_inputs(self, pre_run_sha: str | None, pr_number: int | None) -> None:
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_db import _check_pr_pushed_via_sha
        from sova.dashboard.services.agent_pool import AgentState

        agent = MagicMock(spec=AgentState)
        agent.pre_run_sha = pre_run_sha
        agent.pr_number = pr_number
        assert await _check_pr_pushed_via_sha(agent) == (None, False)

    @pytest.mark.parametrize(
        ("pre_run_sha", "success", "stdout", "expected"),
        [
            pytest.param("aaa111", True, "bbb222\tOPEN\n", (True, False), id="true-when-sha-changed"),
            pytest.param("aaa111", True, "aaa111\tMERGED\n", (True, False), id="true-when-pr-merged"),
            pytest.param("aaa111", True, "aaa111\tOPEN\n", (False, False), id="false-when-sha-unchanged"),
            pytest.param("aaa111", False, "", (None, False), id="none-on-api-failure"),
            pytest.param("abc123", True, "\tOPEN\n", (None, False), id="none-when-sha-empty"),
            pytest.param("abc123", True, "deadbeef\t\n", (None, False), id="none-when-state-empty"),
        ],
    )
    async def test_head_sha_check(
        self,
        tmp_path: Path,
        monkeypatch,
        pre_run_sha: str,
        success: bool,
        stdout: str,
        expected: tuple[bool | None, bool],
    ) -> None:
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_db import _check_pr_pushed_via_sha
        from sova.dashboard.services.agent_pool import AgentState

        agent = MagicMock(spec=AgentState)
        agent.pre_run_sha = pre_run_sha
        agent.pr_number = 42
        agent.project_dir = tmp_path

        async def mock_run(*args, **kwargs):
            result = MagicMock()
            result.is_network_unreachable = False
            result.success = success
            result.stdout = stdout
            return result

        monkeypatch.setattr("sova.utils.shell.run", mock_run)
        assert await _check_pr_pushed_via_sha(agent) == expected

    async def test_returns_none_on_exception(self, tmp_path: Path, monkeypatch) -> None:
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_db import _check_pr_pushed_via_sha
        from sova.dashboard.services.agent_pool import AgentState

        agent = MagicMock(spec=AgentState)
        agent.pre_run_sha = "aaa111"
        agent.pr_number = 42
        agent.project_dir = tmp_path

        async def mock_run(*args, **kwargs):
            raise TimeoutError("timed out")

        monkeypatch.setattr("sova.utils.shell.run", mock_run)
        assert await _check_pr_pushed_via_sha(agent) == (None, False)


class TestValidateAddressPrWithSha:
    """Tests that _validate_address_pr requires a changed remote PR head."""

    async def test_pr_number_none_returns_error(self, tmp_path: Path) -> None:
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_db import _validate_address_pr
        from sova.dashboard.services.agent_pool import AgentState

        agent = MagicMock(spec=AgentState)
        agent.pr_number = None
        agent.project_dir = tmp_path

        result = await _validate_address_pr(1, agent)
        assert result.failure_reason is not None
        assert "no associated PR number" in result.failure_reason

    async def test_no_output_returns_error(self, tmp_path: Path) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services.agent_db import _validate_address_pr
        from sova.dashboard.services.agent_pool import AgentState

        agent = MagicMock(spec=AgentState)
        agent.pr_number = 42
        agent.project_dir = tmp_path

        with patch(
            "sova.dashboard.services.agent_db._fetch_output_lines",
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = await _validate_address_pr(1, agent)
        assert result.failure_reason is not None
        assert "no output" in result.failure_reason

    async def test_sha_check_short_circuits_on_push_detected(self, tmp_path: Path) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services.agent_db import _VALID, _validate_address_pr
        from sova.dashboard.services.agent_pool import AgentState

        agent = MagicMock(spec=AgentState)
        agent.pr_number = 42
        agent.pre_run_sha = "aaa111"
        agent.project_dir = tmp_path

        with (
            patch(
                "sova.dashboard.services.agent_db._fetch_output_lines",
                new_callable=AsyncMock,
                return_value=["some output"],
            ),
            patch(
                "sova.dashboard.services.agent_db._check_pr_pushed_via_sha",
                new_callable=AsyncMock,
                return_value=(True, False),
            ) as sha_mock,
            patch(
                "sova.dashboard.services.agent_db._check_address_summary",
                new_callable=AsyncMock,
                return_value=(True, False),
            ),
            patch(
                "sova.dashboard.services.agent_db._check_pr_branch_pushed",
                new_callable=AsyncMock,
            ) as branch_mock,
            patch(
                "sova.dashboard.services.agent_db._check_pr_diff_guard",
                new_callable=AsyncMock,
                return_value=_VALID,
            ),
        ):
            result = await _validate_address_pr(1, agent)
        assert result.failure_reason is None
        sha_mock.assert_awaited_once_with(agent)
        branch_mock.assert_not_awaited()

    async def test_inconclusive_sha_cannot_pass_from_synchronized_branch(self, tmp_path: Path) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services.agent_db import _VALID, _validate_address_pr
        from sova.dashboard.services.agent_pool import AgentState

        agent = MagicMock(spec=AgentState)
        agent.pr_number = 42
        agent.pre_run_sha = "aaa111"
        agent.project_dir = tmp_path

        with (
            patch(
                "sova.dashboard.services.agent_db._fetch_output_lines",
                new_callable=AsyncMock,
                return_value=["some output"],
            ),
            patch(
                "sova.dashboard.services.agent_db._check_pr_pushed_via_sha",
                new_callable=AsyncMock,
                return_value=(None, False),
            ),
            patch(
                "sova.dashboard.services.agent_db._check_pr_branch_pushed",
                new_callable=AsyncMock,
                return_value=(True, False),
            ),
            patch(
                "sova.dashboard.services.agent_db._check_pr_diff_guard",
                new_callable=AsyncMock,
                return_value=_VALID,
            ),
        ):
            result = await _validate_address_pr(1, agent)
        assert result.failure_reason is not None
        assert "unverified" in result.failure_reason

    async def test_all_tiers_inconclusive_returns_error(self, tmp_path: Path) -> None:
        """An unknown PR head cannot prove that the command pushed fixes."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services.agent_db import _validate_address_pr
        from sova.dashboard.services.agent_pool import AgentState

        agent = MagicMock(spec=AgentState)
        agent.pr_number = 42
        agent.pre_run_sha = "aaa111"
        agent.project_dir = tmp_path

        with (
            patch(
                "sova.dashboard.services.agent_db._fetch_output_lines",
                new_callable=AsyncMock,
                return_value=["addressed review comments", "fixed the bug"],
            ),
            patch(
                "sova.dashboard.services.agent_db._check_pr_pushed_via_sha",
                new_callable=AsyncMock,
                return_value=(None, False),
            ),
            patch(
                "sova.dashboard.services.agent_db._check_pr_branch_pushed",
                new_callable=AsyncMock,
                return_value=(None, False),
            ),
        ):
            result = await _validate_address_pr(1, agent)
        assert result.failure_reason is not None
        assert "unverified" in result.failure_reason

    async def test_text_scan_single_keyword_not_enough(self, tmp_path: Path) -> None:
        """One keyword match is not sufficient (reduces false positives)."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services.agent_db import _validate_address_pr
        from sova.dashboard.services.agent_pool import AgentState

        agent = MagicMock(spec=AgentState)
        agent.pr_number = 42
        agent.pre_run_sha = "aaa111"
        agent.project_dir = tmp_path

        with (
            patch(
                "sova.dashboard.services.agent_db._fetch_output_lines",
                new_callable=AsyncMock,
                return_value=["ran git push origin branch"],
            ),
            patch(
                "sova.dashboard.services.agent_db._check_pr_pushed_via_sha",
                new_callable=AsyncMock,
                return_value=(None, False),
            ),
            patch(
                "sova.dashboard.services.agent_db._check_pr_branch_pushed",
                new_callable=AsyncMock,
                return_value=(None, False),
            ),
        ):
            result = await _validate_address_pr(1, agent)
        assert result.failure_reason is not None
        assert "unverified" in result.failure_reason

    async def test_text_scan_two_keywords_does_not_prove_push(self, tmp_path: Path) -> None:
        """A model's text cannot prove that a remote ref moved."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services.agent_db import _VALID, _validate_address_pr
        from sova.dashboard.services.agent_pool import AgentState

        agent = MagicMock(spec=AgentState)
        agent.pr_number = 42
        agent.pre_run_sha = "aaa111"
        agent.project_dir = tmp_path

        with (
            patch(
                "sova.dashboard.services.agent_db._fetch_output_lines",
                new_callable=AsyncMock,
                return_value=["git push --force-with-lease origin branch", "pushed to remote"],
            ),
            patch(
                "sova.dashboard.services.agent_db._check_pr_pushed_via_sha",
                new_callable=AsyncMock,
                return_value=(None, False),
            ),
            patch(
                "sova.dashboard.services.agent_db._check_pr_branch_pushed",
                new_callable=AsyncMock,
                return_value=(None, False),
            ),
            patch(
                "sova.dashboard.services.agent_db._check_pr_diff_guard",
                new_callable=AsyncMock,
                return_value=_VALID,
            ),
        ):
            result = await _validate_address_pr(1, agent)
        assert result.failure_reason is not None

    async def test_branch_check_false_returns_error(self, tmp_path: Path) -> None:
        """Branch check returning False (unpushed commits exist) fails immediately."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services.agent_db import _validate_address_pr
        from sova.dashboard.services.agent_pool import AgentState

        agent = MagicMock(spec=AgentState)
        agent.pr_number = 42
        agent.pre_run_sha = "aaa111"
        agent.project_dir = tmp_path

        with (
            patch(
                "sova.dashboard.services.agent_db._fetch_output_lines",
                new_callable=AsyncMock,
                return_value=["some output"],
            ),
            patch(
                "sova.dashboard.services.agent_db._check_pr_pushed_via_sha",
                new_callable=AsyncMock,
                return_value=(False, False),
            ),
            patch(
                "sova.dashboard.services.agent_db._check_pr_branch_pushed",
                new_callable=AsyncMock,
                return_value=(False, False),
            ),
        ):
            result = await _validate_address_pr(1, agent)
        assert result.failure_reason is not None
        assert "without pushing" in result.failure_reason

    async def test_network_blocked_verification_is_not_reported_as_no_push(self, tmp_path: Path) -> None:
        """An outage during verification must not be reported as the agent doing nothing.

        This is the Gwym failure: every tier of this validator needs GitHub, so
        during an outage all of them go inconclusive and the old fallback said
        "address-pr completed without pushing changes" about a run whose actual
        outcome was simply unknown.
        """
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services.agent_db import _validate_address_pr
        from sova.dashboard.services.agent_pool import AgentState

        agent = MagicMock(spec=AgentState)
        agent.pr_number = 687
        agent.pre_run_sha = "aaa111"
        agent.project_dir = tmp_path

        with (
            patch(
                "sova.dashboard.services.agent_db._fetch_output_lines",
                new_callable=AsyncMock,
                return_value=["addressed review comments"],
            ),
            patch(
                "sova.dashboard.services.agent_db._check_pr_pushed_via_sha",
                new_callable=AsyncMock,
                return_value=(None, True),
            ),
            patch(
                "sova.dashboard.services.agent_db._check_pr_branch_pushed",
                new_callable=AsyncMock,
                return_value=(None, True),
            ),
        ):
            result = await _validate_address_pr(1, agent)

        assert result.network_blocked is True
        assert "unverified" in result.failure_reason
        assert "could not check" in result.failure_reason
        assert "without pushing" not in result.failure_reason

    async def test_inconclusive_without_network_failure_still_fails_closed(self, tmp_path: Path) -> None:
        """Fail-closed is preserved: only a network-blocked check gets the benefit of the doubt."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services.agent_db import _validate_address_pr
        from sova.dashboard.services.agent_pool import AgentState

        agent = MagicMock(spec=AgentState)
        agent.pr_number = 42
        agent.pre_run_sha = "aaa111"
        agent.project_dir = tmp_path

        with (
            patch(
                "sova.dashboard.services.agent_db._fetch_output_lines",
                new_callable=AsyncMock,
                return_value=["did some thinking"],
            ),
            patch(
                "sova.dashboard.services.agent_db._check_pr_pushed_via_sha",
                new_callable=AsyncMock,
                return_value=(None, False),
            ),
            patch(
                "sova.dashboard.services.agent_db._check_pr_branch_pushed",
                new_callable=AsyncMock,
                return_value=(None, False),
            ),
        ):
            result = await _validate_address_pr(1, agent)

        assert result.network_blocked is False
        assert "unverified" in result.failure_reason


class TestCheckPrDiffGuard:
    """Tests that _check_pr_diff_guard wires sova.core.pr_diff_guard into address-pr validation."""

    async def test_no_baseline_returns_valid(self, tmp_path: Path) -> None:
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_db import _check_pr_diff_guard
        from sova.dashboard.services.agent_pool import AgentState

        agent = MagicMock(spec=AgentState)
        agent.pre_run_sha = None
        agent.pre_run_head_sha = None
        agent.project_dir = tmp_path

        result = await _check_pr_diff_guard(agent)
        assert result.failure_reason is None

    async def test_uses_worktree_path_and_pr_branch_ref_not_project_dir_or_head(self, tmp_path: Path) -> None:
        """A PR-based command run never executes in project_dir (agent_lifecycle.py aborts first

        if worktree isolation fails), so the guard must inspect agent.worktree_path on the PR's
        own branch ref, never project_dir's HEAD (which, for this repo, stays parked on main).
        """
        from unittest.mock import AsyncMock, MagicMock, call, patch

        from sova.core.pr_diff_guard import GuardResult
        from sova.dashboard.services.agent_db import _check_pr_diff_guard
        from sova.dashboard.services.agent_pool import AgentState

        agent = MagicMock(spec=AgentState)
        agent.pr_number = 42
        agent.pre_run_sha = "aaa111"
        agent.project_dir = tmp_path
        agent.worktree_path = tmp_path / "worktree-42"
        agent.issue = None
        agent.role = "command:address-pr"

        with (
            patch("sova.dashboard.services.agent_db.load_config") as mock_cfg,
            patch(
                "sova.dashboard.services.agent_db._fetch_pr_fields",
                new_callable=AsyncMock,
                return_value=("feat/issue-42", False),
            ) as mock_fetch,
            patch(
                "sova.core.pr_diff_guard.check_silent_revert",
                new_callable=AsyncMock,
                return_value=GuardResult(ok=True, pre_merge_base="pre_base_sha", post_merge_base="base_sha"),
            ) as mock_revert,
            patch(
                "sova.core.pr_diff_guard.check_pin_downgrades",
                new_callable=AsyncMock,
                return_value=([], False),
            ) as mock_pins,
        ):
            mock_cfg.return_value.base_branch = "main"
            result = await _check_pr_diff_guard(agent)

        assert result.failure_reason is None
        mock_fetch.assert_awaited_once_with(
            42, agent.worktree_path, "headRefName,baseRefName", "[.headRefName, .baseRefName] | @tsv"
        )
        mock_revert.assert_awaited_once_with(agent.worktree_path, "main", "aaa111", head_ref="feat/issue-42")
        assert mock_pins.await_args_list == [
            call(agent.worktree_path, "pre_base_sha", "aaa111"),
            call(agent.worktree_path, "base_sha", "feat/issue-42"),
        ]

    async def test_uses_pr_base_ref_name_not_config_default(self, tmp_path: Path) -> None:
        """The guard must diff against the PR's own base branch, not the project-wide

        default: for a stacked PR (based on another branch) or a release branch, using
        cfg.base_branch instead would attribute everything the real base contributed to
        "the base changed it since the fork", manufacturing or masking candidates.
        """
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.core.pr_diff_guard import GuardResult
        from sova.dashboard.services.agent_db import _check_pr_diff_guard
        from sova.dashboard.services.agent_pool import AgentState

        agent = MagicMock(spec=AgentState)
        agent.pr_number = 42
        agent.pre_run_sha = "aaa111"
        agent.pre_run_head_sha = None
        agent.project_dir = tmp_path
        agent.worktree_path = tmp_path / "worktree-42"
        agent.issue = None
        agent.role = "command:address-pr"

        with (
            patch("sova.dashboard.services.agent_db.load_config") as mock_cfg,
            patch(
                "sova.dashboard.services.agent_db._fetch_pr_fields",
                new_callable=AsyncMock,
                return_value=("feat/issue-42\trelease/1.0", False),
            ),
            patch(
                "sova.core.pr_diff_guard.check_silent_revert",
                new_callable=AsyncMock,
                return_value=GuardResult(ok=True, pre_merge_base="pre_base_sha", post_merge_base="base_sha"),
            ) as mock_revert,
            patch(
                "sova.core.pr_diff_guard.check_pin_downgrades",
                new_callable=AsyncMock,
                return_value=([], False),
            ),
        ):
            mock_cfg.return_value.base_branch = "main"
            result = await _check_pr_diff_guard(agent)

        assert result.failure_reason is None
        mock_revert.assert_awaited_once_with(agent.worktree_path, "release/1.0", "aaa111", head_ref="feat/issue-42")

    async def test_skips_pin_check_when_revert_merge_bases_unresolvable(self, tmp_path: Path) -> None:
        """When check_silent_revert() itself skipped (ok=True, no fork points resolved, e.g.

        the baseline SHA is not present locally), running the pin check against that same
        unresolvable baseline would just rediscover "cannot verify" as a false
        network_blocked outcome, cancelling the skip. The pin check must not run at all.
        """
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.core.pr_diff_guard import GuardResult
        from sova.dashboard.services.agent_db import _check_pr_diff_guard
        from sova.dashboard.services.agent_pool import AgentState

        agent = MagicMock(spec=AgentState)
        agent.pr_number = 42
        agent.pre_run_sha = "aaa111"
        agent.pre_run_head_sha = None
        agent.project_dir = tmp_path
        agent.worktree_path = None
        agent.issue = None
        agent.role = "command:address-pr"

        with (
            patch("sova.dashboard.services.agent_db.load_config") as mock_cfg,
            patch(
                "sova.dashboard.services.agent_db._fetch_pr_fields",
                new_callable=AsyncMock,
                return_value=("feat/issue-42", False),
            ),
            patch(
                "sova.core.pr_diff_guard.check_silent_revert",
                new_callable=AsyncMock,
                return_value=GuardResult(ok=True),
            ),
            patch(
                "sova.core.pr_diff_guard.check_pin_downgrades",
                new_callable=AsyncMock,
            ) as mock_pins,
        ):
            mock_cfg.return_value.base_branch = "main"
            result = await _check_pr_diff_guard(agent)

        assert result.failure_reason is None
        mock_pins.assert_not_awaited()

    async def test_falls_back_to_pre_run_head_sha_when_no_pr_baseline(self, tmp_path: Path) -> None:
        """A non-PR command run (e.g. rearrange-commits invoked without a PR number) never

        gets pre_run_sha, since that is only ever captured from a PR's headRefOid; the
        guard must still reach a baseline via pre_run_head_sha, the worktree's local HEAD
        captured at spawn time, or the rearrange-commits validator can never fire.
        """
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.core.pr_diff_guard import GuardResult
        from sova.dashboard.services.agent_db import _check_pr_diff_guard
        from sova.dashboard.services.agent_pool import AgentState

        agent = MagicMock(spec=AgentState)
        agent.pr_number = None
        agent.pre_run_sha = None
        agent.pre_run_head_sha = "local111"
        agent.project_dir = tmp_path
        agent.worktree_path = tmp_path / "worktree"
        agent.issue = None
        agent.role = "command:rearrange-commits"

        with (
            patch("sova.dashboard.services.agent_db.load_config") as mock_cfg,
            patch(
                "sova.core.pr_diff_guard.check_silent_revert",
                new_callable=AsyncMock,
                return_value=GuardResult(ok=True),
            ) as mock_revert,
            patch(
                "sova.core.pr_diff_guard.check_pin_downgrades",
                new_callable=AsyncMock,
                return_value=([], False),
            ),
        ):
            mock_cfg.return_value.base_branch = "main"
            result = await _check_pr_diff_guard(agent)

        assert result.failure_reason is None
        mock_revert.assert_awaited_once_with(agent.worktree_path, "main", "local111", head_ref="HEAD")

    async def test_falls_back_to_project_dir_when_no_worktree_path(self, tmp_path: Path) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.core.pr_diff_guard import GuardResult
        from sova.dashboard.services.agent_db import _check_pr_diff_guard
        from sova.dashboard.services.agent_pool import AgentState

        agent = MagicMock(spec=AgentState)
        agent.pr_number = None
        agent.pre_run_sha = "aaa111"
        agent.project_dir = tmp_path
        agent.worktree_path = None
        agent.issue = None
        agent.role = "command:address-pr"

        with (
            patch("sova.dashboard.services.agent_db.load_config") as mock_cfg,
            patch(
                "sova.core.pr_diff_guard.check_silent_revert",
                new_callable=AsyncMock,
                return_value=GuardResult(ok=True),
            ) as mock_revert,
            patch(
                "sova.core.pr_diff_guard.check_pin_downgrades",
                new_callable=AsyncMock,
                return_value=([], False),
            ),
        ):
            mock_cfg.return_value.base_branch = "main"
            result = await _check_pr_diff_guard(agent)

        assert result.failure_reason is None
        mock_revert.assert_awaited_once_with(tmp_path, "main", "aaa111", head_ref="HEAD")

    async def test_silent_revert_detected_fails(self, tmp_path: Path) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.core.pr_diff_guard import GuardResult
        from sova.dashboard.services.agent_db import _check_pr_diff_guard
        from sova.dashboard.services.agent_pool import AgentState

        agent = MagicMock(spec=AgentState)
        agent.pr_number = 42
        agent.pre_run_sha = "aaa111"
        agent.project_dir = tmp_path
        agent.worktree_path = None
        agent.issue = None
        agent.role = "command:address-pr"

        with (
            patch("sova.dashboard.services.agent_db.load_config") as mock_cfg,
            patch(
                "sova.dashboard.services.agent_db._fetch_pr_fields",
                new_callable=AsyncMock,
                return_value=("feat/issue-42", False),
            ),
            patch(
                "sova.core.pr_diff_guard.check_silent_revert",
                new_callable=AsyncMock,
                return_value=GuardResult(
                    ok=False,
                    reason="possible silent revert: shared.py",
                    files=frozenset({"shared.py"}),
                ),
            ),
        ):
            mock_cfg.return_value.base_branch = "main"
            result = await _check_pr_diff_guard(agent)

        assert result.failure_reason is not None
        assert "possible silent revert" in result.failure_reason
        assert result.network_blocked is False

    async def test_inconclusive_revert_check_is_network_blocked_not_failed(self, tmp_path: Path) -> None:
        """An unresolvable merge-base/diff is 'could not verify', never 'verified bad' (see

        architecture.md "A network outage is a first-class condition"): it must not consume
        the address-review budget or report a false revert, so it is surfaced via
        network_blocked rather than a bare failure_reason.
        """
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.core.pr_diff_guard import GuardResult
        from sova.dashboard.services.agent_db import _check_pr_diff_guard
        from sova.dashboard.services.agent_pool import AgentState

        agent = MagicMock(spec=AgentState)
        agent.pr_number = 42
        agent.pre_run_sha = "aaa111"
        agent.project_dir = tmp_path
        agent.worktree_path = None
        agent.issue = None
        agent.role = "command:address-pr"

        with (
            patch("sova.dashboard.services.agent_db.load_config") as mock_cfg,
            patch(
                "sova.dashboard.services.agent_db._fetch_pr_fields",
                new_callable=AsyncMock,
                return_value=("feat/issue-42", False),
            ),
            patch(
                "sova.core.pr_diff_guard.check_silent_revert",
                new_callable=AsyncMock,
                return_value=GuardResult(
                    ok=False,
                    reason="could not fetch origin/main to verify the diff",
                    inconclusive=True,
                ),
            ),
        ):
            mock_cfg.return_value.base_branch = "main"
            result = await _check_pr_diff_guard(agent)

        assert result.failure_reason is not None
        assert result.network_blocked is True

    async def test_branch_name_fetch_network_blocked_is_network_blocked_not_failed(self, tmp_path: Path) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services.agent_db import _check_pr_diff_guard
        from sova.dashboard.services.agent_pool import AgentState

        agent = MagicMock(spec=AgentState)
        agent.pr_number = 42
        agent.pre_run_sha = "aaa111"
        agent.project_dir = tmp_path
        agent.worktree_path = None
        agent.issue = None
        agent.role = "command:address-pr"

        with (
            patch("sova.dashboard.services.agent_db.load_config") as mock_cfg,
            patch(
                "sova.dashboard.services.agent_db._fetch_pr_fields",
                new_callable=AsyncMock,
                return_value=(None, True),
            ),
        ):
            mock_cfg.return_value.base_branch = "main"
            result = await _check_pr_diff_guard(agent)

        assert result.failure_reason is not None
        assert result.network_blocked is True

    async def test_clean_diff_returns_valid(self, tmp_path: Path) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.core.pr_diff_guard import GuardResult
        from sova.dashboard.services.agent_db import _check_pr_diff_guard
        from sova.dashboard.services.agent_pool import AgentState

        agent = MagicMock(spec=AgentState)
        agent.pr_number = 42
        agent.pre_run_sha = "aaa111"
        agent.project_dir = tmp_path
        agent.worktree_path = None
        agent.issue = None
        agent.role = "command:address-pr"

        with (
            patch("sova.dashboard.services.agent_db.load_config") as mock_cfg,
            patch(
                "sova.dashboard.services.agent_db._fetch_pr_fields",
                new_callable=AsyncMock,
                return_value=("feat/issue-42", False),
            ),
            patch(
                "sova.core.pr_diff_guard.check_silent_revert",
                new_callable=AsyncMock,
                return_value=GuardResult(ok=True),
            ),
            patch(
                "sova.core.pr_diff_guard.check_pin_downgrades",
                new_callable=AsyncMock,
                return_value=([], False),
            ),
        ):
            mock_cfg.return_value.base_branch = "main"
            result = await _check_pr_diff_guard(agent)

        assert result.failure_reason is None

    async def test_guard_exception_does_not_fail_run(self, tmp_path: Path) -> None:
        """A guard crash must not block an otherwise-valid push (additive, not a new failure mode)."""
        from unittest.mock import MagicMock, patch

        from sova.dashboard.services.agent_db import _check_pr_diff_guard
        from sova.dashboard.services.agent_pool import AgentState

        agent = MagicMock(spec=AgentState)
        agent.pr_number = 42
        agent.pre_run_sha = "aaa111"
        agent.project_dir = tmp_path
        agent.worktree_path = None
        agent.issue = None
        agent.role = "command:address-pr"

        with patch("sova.dashboard.services.agent_db.load_config", side_effect=RuntimeError("boom")):
            result = await _check_pr_diff_guard(agent)

        assert result.failure_reason is None

    async def test_pin_downgrade_fails_when_not_exempted(self, tmp_path: Path) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.core.pr_diff_guard import GuardResult, PinDowngrade
        from sova.dashboard.services.agent_db import _check_pr_diff_guard
        from sova.dashboard.services.agent_pool import AgentState

        agent = MagicMock(spec=AgentState)
        agent.pr_number = 42
        agent.pre_run_sha = "aaa111"
        agent.project_dir = tmp_path
        agent.worktree_path = None
        agent.issue = None
        agent.role = "command:address-pr"
        agent.prompt = "Fix the flaky test"

        with (
            patch("sova.dashboard.services.agent_db.load_config") as mock_cfg,
            patch(
                "sova.dashboard.services.agent_db._fetch_pr_fields",
                new_callable=AsyncMock,
                return_value=("feat/issue-42", False),
            ),
            patch(
                "sova.core.pr_diff_guard.check_silent_revert",
                new_callable=AsyncMock,
                return_value=GuardResult(ok=True, pre_merge_base="pre_base_sha", post_merge_base="base_sha"),
            ),
            patch(
                "sova.core.pr_diff_guard.check_pin_downgrades",
                new_callable=AsyncMock,
                side_effect=[
                    ([], False),
                    (
                        [
                            PinDowngrade(
                                file="requirements.txt",
                                package="requests",
                                from_version="2.31.0",
                                to_version="2.20.0",
                            )
                        ],
                        False,
                    ),
                ],
            ),
        ):
            mock_cfg.return_value.base_branch = "main"
            result = await _check_pr_diff_guard(agent)

        assert result.failure_reason is not None
        assert "downgrade" in result.failure_reason
        assert "requests" in result.failure_reason

    async def test_pin_downgrade_exempted_when_file_named_in_prompt(self, tmp_path: Path) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.core.pr_diff_guard import GuardResult, PinDowngrade
        from sova.dashboard.services.agent_db import _check_pr_diff_guard
        from sova.dashboard.services.agent_pool import AgentState

        agent = MagicMock(spec=AgentState)
        agent.pr_number = 42
        agent.pre_run_sha = "aaa111"
        agent.project_dir = tmp_path
        agent.worktree_path = None
        agent.issue = None
        agent.role = "command:address-pr"
        agent.prompt = "Pin requests back down in requirements.txt to fix the regression"

        with (
            patch("sova.dashboard.services.agent_db.load_config") as mock_cfg,
            patch(
                "sova.dashboard.services.agent_db._fetch_pr_fields",
                new_callable=AsyncMock,
                return_value=("feat/issue-42", False),
            ),
            patch(
                "sova.core.pr_diff_guard.check_silent_revert",
                new_callable=AsyncMock,
                return_value=GuardResult(ok=True, pre_merge_base="pre_base_sha", post_merge_base="base_sha"),
            ),
            patch(
                "sova.core.pr_diff_guard.check_pin_downgrades",
                new_callable=AsyncMock,
                side_effect=[
                    ([], False),
                    (
                        [
                            PinDowngrade(
                                file="requirements.txt",
                                package="requests",
                                from_version="2.31.0",
                                to_version="2.20.0",
                            )
                        ],
                        False,
                    ),
                ],
            ),
        ):
            mock_cfg.return_value.base_branch = "main"
            result = await _check_pr_diff_guard(agent)

        assert result.failure_reason is None

    async def test_pin_downgrade_exempted_when_file_named_in_issue_body(self, tmp_path: Path) -> None:
        """The realistic prompt is just "/address-pr pr=42" once the command is installed under

        .claude/commands/ (agent_context.py::_resolve_command_prompt), which can never name a
        file; the exemption must still work when the issue body (not the prompt) names it.
        """
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.core.pr_diff_guard import GuardResult, PinDowngrade
        from sova.dashboard.services.agent_db import _check_pr_diff_guard
        from sova.dashboard.services.agent_pool import AgentState

        agent = MagicMock(spec=AgentState)
        agent.pr_number = 42
        agent.pre_run_sha = "aaa111"
        agent.project_dir = tmp_path
        agent.worktree_path = None
        agent.issue = "99"
        agent.role = "command:address-pr"
        agent.prompt = "/address-pr pr=42"

        mock_task = MagicMock()
        mock_task.body = "Please pin requests back down in requirements.txt to fix the regression"

        with (
            patch("sova.dashboard.services.agent_db.load_config") as mock_cfg,
            patch(
                "sova.dashboard.services.agent_db._fetch_pr_fields",
                new_callable=AsyncMock,
                return_value=("feat/issue-42", False),
            ),
            patch(
                "sova.core.pr_diff_guard.check_silent_revert",
                new_callable=AsyncMock,
                return_value=GuardResult(ok=True, pre_merge_base="pre_base_sha", post_merge_base="base_sha"),
            ),
            patch(
                "sova.core.pr_diff_guard.check_pin_downgrades",
                new_callable=AsyncMock,
                side_effect=[
                    ([], False),
                    (
                        [
                            PinDowngrade(
                                file="requirements.txt",
                                package="requests",
                                from_version="2.31.0",
                                to_version="2.20.0",
                            )
                        ],
                        False,
                    ),
                ],
            ),
            patch("sova.core.spec_utils.read_spec", return_value=None),
            patch("sova.adapters.create_adapter") as mock_create_adapter,
        ):
            mock_cfg.return_value.base_branch = "main"
            mock_create_adapter.return_value.get_task = AsyncMock(return_value=mock_task)
            result = await _check_pr_diff_guard(agent)

        assert result.failure_reason is None


class TestPipelineBypassDiagnosticLogging:
    """Tests that _validate_pipeline_outcome logs prompt on bypass."""

    async def test_logs_prompt_on_bypass(self) -> None:
        from unittest.mock import MagicMock, patch

        from sova.dashboard.services.agent_db import _validate_pipeline_outcome
        from sova.dashboard.services.agent_pool import AgentState

        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(
                    issue_number="99",
                    role="developer",
                    status="done",
                    current_step="agent",
                )
                session.add(run)
                await session.flush()
                run_id = run.id

        agent = MagicMock(spec=AgentState)
        agent.role = "developer"
        agent.project_dir = None
        agent.prompt = "Run sova run 99"
        with patch("sova.dashboard.services.agent_db.log") as mock_log:
            result = await _validate_pipeline_outcome(run_id, agent)
        assert result is not None
        assert "Pipeline bypassed" in result
        mock_log.warning.assert_any_call(
            "validate_pipeline.bypass_diagnostic",
            run_id=run_id,
            role="developer",
            prompt_sent="Run sova run 99",
        )


class TestBuildBypassMessage:
    """Tests for _build_bypass_message helper."""

    def test_developer_no_pr(self) -> None:
        from unittest.mock import patch

        from sova.dashboard.services.agent_db import _build_bypass_message

        with patch("sova.dashboard.services.agent_db.log"):
            msg = _build_bypass_message("developer", None, "some prompt", 1)
        assert "Pipeline bypassed" in msg
        assert "no PR was created" in msg

    def test_developer_with_pr(self) -> None:
        from unittest.mock import patch

        from sova.dashboard.services.agent_db import _build_bypass_message

        with patch("sova.dashboard.services.agent_db.log"):
            msg = _build_bypass_message("developer", 42, "some prompt", 1)
        assert "Pipeline bypassed" in msg
        assert "no PR was created" not in msg

    def test_researcher_role(self) -> None:
        from unittest.mock import patch

        from sova.dashboard.services.agent_db import _build_bypass_message

        with patch("sova.dashboard.services.agent_db.log"):
            msg = _build_bypass_message("researcher", None, None, 1)
        assert "researcher" in msg
        assert "no PR was created" not in msg

    def test_no_prompt_skips_log(self) -> None:
        from unittest.mock import patch

        from sova.dashboard.services.agent_db import _build_bypass_message

        with patch("sova.dashboard.services.agent_db.log") as mock_log:
            _build_bypass_message("developer", None, None, 1)
        mock_log.warning.assert_not_called()

    def test_prompt_truncated_to_500(self) -> None:
        from unittest.mock import patch

        from sova.dashboard.services.agent_db import _build_bypass_message

        long_prompt = "x" * 1000
        with patch("sova.dashboard.services.agent_db.log") as mock_log:
            _build_bypass_message("developer", None, long_prompt, 5)
        mock_log.warning.assert_called_once()
        call_kwargs = mock_log.warning.call_args
        assert len(call_kwargs[1]["prompt_sent"]) == 500

    def test_includes_diagnostic_fields(self, tmp_path: Path) -> None:
        from sova.dashboard.services.agent_db import _build_bypass_message, _collect_bypass_diagnostics

        diag = _collect_bypass_diagnostics(
            "2026-08-13T10:00:00", str(tmp_path / "worktrees" / "99"), str(tmp_path / "project")
        )
        msg = _build_bypass_message("researcher", None, None, 42, diagnostics=diag)
        assert "started_at=2026-08-13T10:00:00" in msg
        assert f"worktree={tmp_path / 'worktrees' / '99'}" in msg
        assert f"project_dir={tmp_path / 'project'}" in msg

    def test_omits_diagnostics_when_none(self) -> None:
        from sova.dashboard.services.agent_db import _build_bypass_message

        msg = _build_bypass_message("researcher", None, None, 42)
        assert "Pipeline bypassed" in msg
        assert "[" not in msg


@pytest.mark.asyncio
class TestCheckIncompletePr:
    """Tests for _check_incomplete_pr helper."""

    async def test_returns_none_when_no_pr_steps(self) -> None:
        from sova.dashboard.services.agent_db import _check_incomplete_pr

        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(issue_number="1", role="developer", status="done")
                session.add(run)
                await session.flush()
                run_id = run.id

        async with await get_session() as session:
            async with session.begin():
                result = await _check_incomplete_pr(run_id, session)
        assert result is None

    async def test_returns_message_when_create_pr_done(self) -> None:
        from sova.core.state import STEP_DONE_STATUSES
        from sova.dashboard.services.agent_db import _check_incomplete_pr
        from sova.db.models import StepExecution

        done_status = next(iter(STEP_DONE_STATUSES))

        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(issue_number="2", role="developer", status="done")
                session.add(run)
                await session.flush()
                run_id = run.id
                step = StepExecution(task_run_id=run_id, step_name="create_pr", status=done_status)
                session.add(step)

        async with await get_session() as session:
            async with session.begin():
                result = await _check_incomplete_pr(run_id, session)
        assert result is not None
        assert "pr_number is still None" in result
