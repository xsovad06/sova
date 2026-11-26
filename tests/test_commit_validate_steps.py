"""Commit, validate, rearrange-commits and push steps.

Split from tests/test_core.py; see docs/testing-guidelines.md, "Test File Layout".
"""

from __future__ import annotations

import shlex
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest

from sova.adapters.base import Task
from sova.config.models import ProjectConfig, TaskSourceConfig
from tests.core_helpers import _make_ctx

# Every test in this module runs against the shared per-worker in-memory DB
# (see ``tests/conftest.py:reset_db_rows``).
pytestmark = pytest.mark.usefixtures("reset_db_rows")


class TestCommitStep:
    @pytest.fixture(autouse=True)
    def _stub_subject_proposal(self):
        """CommitStep's LLM-assisted subject proposal must not run for real in
        tests that don't exercise it; falling back to the deterministic
        builder keeps every existing assertion about the deterministic
        message valid. Individual tests can still override this with their
        own nested `patch("sova.core.steps.commit.invoke")`."""
        with patch("sova.core.steps.commit.invoke", new_callable=AsyncMock, side_effect=RuntimeError("stub")):
            yield

    async def test_commits_uncommitted_changes(self, tmp_path: Path) -> None:
        from sova.core.steps.commit import CommitStep

        ctx = _make_ctx(tmp_path, worktree_dir=tmp_path / "worktree")
        ctx.task = Task(id="73", title="LLM provider abstraction")
        step = CommitStep()

        with (
            patch("sova.core.steps.commit.run") as mock_run,
            patch("sova.core.steps.commit.git_ops.commit", new_callable=AsyncMock) as mock_commit,
        ):
            mock_run.side_effect = [
                MagicMock(success=True, stdout=" config/base.py | 5 +++++\n"),  # diff --stat HEAD
                MagicMock(success=True, stdout=""),  # diff --cached --stat
                MagicMock(success=True, stdout="config/base.py\0"),  # diff --name-only -z HEAD
                MagicMock(success=True, stdout=""),  # diff --cached --name-only -z
                MagicMock(success=True, stdout="apps/ai/__init__.py\n"),  # untracked
                MagicMock(success=True, stdout=""),  # log (no commits yet)
            ]
            result = await step.execute(ctx)

        assert result.success
        mock_commit.assert_awaited_once()
        msg = mock_commit.call_args[0][0]
        # The deterministic subject's first letter is lowercased per the
        # project's conventional-commit convention, except for an all-caps
        # opening word: "LLM" is an acronym, not a capitalized sentence.
        assert "feat(core): LLM provider abstraction" in msg
        assert "Refs #42" in msg
        assert mock_commit.call_args.kwargs["no_verify"] is False

    async def test_detect_changes_dedups_a_path_appearing_in_both_staged_and_unstaged_diffs(
        self, tmp_path: Path
    ) -> None:
        """A staged file also shows up in the plain `git diff --stat HEAD`
        (which covers staged and unstaged together against HEAD), so without
        deduplication it would be counted twice in scope inference's
        most-touched-scope tally, biasing the result toward whatever
        happened to be staged."""
        from sova.core.steps.commit import CommitStep

        ctx = _make_ctx(tmp_path, worktree_dir=tmp_path / "worktree")
        step = CommitStep()

        with patch("sova.core.steps.commit.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(success=True, stdout=" apps/billing/a.py | 1 +\n"),
                MagicMock(success=True, stdout=" apps/billing/a.py | 1 +\n"),
                MagicMock(success=True, stdout="apps/billing/a.py\0"),
                MagicMock(success=True, stdout="apps/billing/a.py\0"),
                MagicMock(success=True, stdout=""),
            ]
            survey = await step._detect_changes(ctx)

        assert survey.changed_paths() == ["apps/billing/a.py"]

    async def test_long_issue_title_subject_is_trimmed_to_72_chars(self, tmp_path: Path) -> None:
        """The deterministic fallback (LLM stubbed to fail by the fixture) has
        to produce a valid lowercase subject within 72 characters by itself."""
        from sova.core.steps.commit import CommitStep
        from sova.core.steps.commit_convention import MAX_LINE_LENGTH, is_valid_commit_line

        gwym_title = (
            "Make the pricing claims match the app: drop tax optimization, "
            "gate the health score trend, drop the plan parameter"
        )
        ctx = _make_ctx(tmp_path, worktree_dir=tmp_path / "worktree")
        ctx.task = Task(id="73", title=gwym_title)
        step = CommitStep()

        with (
            patch("sova.core.steps.commit.run") as mock_run,
            patch("sova.core.steps.commit.git_ops.commit", new_callable=AsyncMock) as mock_commit,
        ):
            mock_run.side_effect = [
                MagicMock(success=True, stdout=" docs/a.md | 5 +++++\n"),
                MagicMock(success=True, stdout=""),
                MagicMock(success=True, stdout="docs/a.md\0"),
                MagicMock(success=True, stdout=""),
                MagicMock(success=True, stdout=""),
            ]
            result = await step.execute(ctx)

        assert result.success
        subject = mock_commit.call_args[0][0].splitlines()[0]
        assert len(subject) <= MAX_LINE_LENGTH
        # A docs-only change gets type and scope "docs" from the changed
        # path, not SOVA's default "feat(core)".
        assert subject.startswith("docs(docs): make the pricing claims match the app")
        assert is_valid_commit_line(subject, types=["docs"], scopes=["docs"])

    async def test_subject_proposal_prompt_carries_the_changed_paths(self, tmp_path: Path) -> None:
        """Type and scope follow from the changed paths, so the prompt must
        carry them: a title-only prompt can only echo the issue's wording."""
        from sova.core.steps.commit import CommitStep
        from sova.llm.models import LLMResult

        ctx = _make_ctx(tmp_path, worktree_dir=tmp_path / "worktree")
        ctx.task = Task(id="73", title="Retire stale documentation")
        step = CommitStep()

        with (
            patch("sova.core.steps.commit.run") as mock_run,
            patch("sova.core.steps.commit.git_ops.commit", new_callable=AsyncMock),
            patch("sova.core.steps.commit.invoke", new_callable=AsyncMock) as mock_invoke,
        ):
            mock_run.side_effect = [
                MagicMock(success=True, stdout=" docs/project-health-status.md | 12 ++------\n"),
                MagicMock(success=True, stdout=""),
                MagicMock(success=True, stdout="docs/project-health-status.md\0"),
                MagicMock(success=True, stdout=""),
                MagicMock(success=True, stdout="docs/new-note.md\0"),
            ]
            mock_invoke.return_value = LLMResult(
                text="docs(core): retire the stale health status doc", model="haiku", cost_usd=Decimal("0.001")
            )
            await step.execute(ctx)

        prompt = mock_invoke.call_args[0][0] if mock_invoke.call_args[0] else mock_invoke.call_args[1]["prompt"]
        assert "docs/project-health-status.md" in prompt
        assert "docs/new-note.md (new file)" in prompt

    async def test_uses_a_valid_llm_proposed_subject_verbatim(self, tmp_path: Path) -> None:
        from sova.core.steps.commit import CommitStep
        from sova.llm.models import LLMResult

        ctx = _make_ctx(tmp_path, worktree_dir=tmp_path / "worktree")
        ctx.task = Task(id="73", title="LLM provider abstraction")
        step = CommitStep()

        with (
            patch("sova.core.steps.commit.run") as mock_run,
            patch("sova.core.steps.commit.git_ops.commit", new_callable=AsyncMock) as mock_commit,
            patch("sova.core.steps.commit.invoke", new_callable=AsyncMock) as mock_invoke,
        ):
            mock_run.side_effect = [
                MagicMock(success=True, stdout=" config/base.py | 5 +++++\n"),
                MagicMock(success=True, stdout=""),
                MagicMock(success=True, stdout="config/base.py\0"),
                MagicMock(success=True, stdout=""),
                MagicMock(success=True, stdout="apps/ai/__init__.py\n"),
                MagicMock(success=True, stdout=""),
            ]
            mock_invoke.return_value = LLMResult(
                text="feat(core): add llm provider abstraction", model="haiku", cost_usd=Decimal("0.001")
            )
            result = await step.execute(ctx)

        assert result.success
        msg = mock_commit.call_args[0][0]
        assert msg.startswith("feat(core): add llm provider abstraction")
        assert "Refs #42" in msg

    async def test_falls_back_to_deterministic_subject_when_llm_proposal_invalid(self, tmp_path: Path) -> None:
        from sova.core.steps.commit import CommitStep
        from sova.llm.models import LLMResult

        ctx = _make_ctx(tmp_path, worktree_dir=tmp_path / "worktree")
        ctx.task = Task(id="73", title="validate commit messages")
        step = CommitStep()

        with (
            patch("sova.core.steps.commit.run") as mock_run,
            patch("sova.core.steps.commit.git_ops.commit", new_callable=AsyncMock) as mock_commit,
            patch("sova.core.steps.commit.invoke", new_callable=AsyncMock) as mock_invoke,
        ):
            mock_run.side_effect = [
                MagicMock(success=True, stdout=" README.md | 5 +++++\n"),
                MagicMock(success=True, stdout=""),
                MagicMock(success=True, stdout="README.md\0"),
                MagicMock(success=True, stdout=""),
                MagicMock(success=True, stdout=""),
            ]
            # Unconfigured type "perf" must be rejected: falls back to the
            # deterministic builder instead of being accepted "because it
            # looks like a real convention".
            mock_invoke.return_value = LLMResult(
                text="perf(core): validate commit messages", model="haiku", cost_usd=Decimal("0.001")
            )
            result = await step.execute(ctx)

        assert result.success
        msg = mock_commit.call_args[0][0]
        assert msg.startswith("feat(core): validate commit messages")

    async def test_deterministic_fallback_uses_the_projects_configured_scope(self, tmp_path: Path) -> None:
        """A project whose commit.scopes omits "core" entirely must not get
        "core" from the deterministic fallback: that scope would fail the
        project's own commit-format check, which is the exact drift this
        configuration exists to stop. The scope itself comes from the
        changed path: "app/billing.py" names the configured "billing" scope
        via its filename, not the first entry in the configured list."""
        from sova.config.models import CommitConfig
        from sova.core.steps.commit import CommitStep

        ctx = _make_ctx(
            tmp_path,
            worktree_dir=tmp_path / "worktree",
            config=ProjectConfig(commit=CommitConfig(scopes=["accounts", "billing"])),
        )
        ctx.task = Task(id="73", title="retry failed billing charges")
        step = CommitStep()

        with (
            patch("sova.core.steps.commit.run") as mock_run,
            patch("sova.core.steps.commit.git_ops.commit", new_callable=AsyncMock) as mock_commit,
            patch("sova.core.steps.commit.invoke", new_callable=AsyncMock, side_effect=RuntimeError("stub")),
        ):
            mock_run.side_effect = [
                MagicMock(success=True, stdout=" app/billing.py | 5 +++++\n"),
                MagicMock(success=True, stdout=""),
                MagicMock(success=True, stdout="app/billing.py\0"),
                MagicMock(success=True, stdout=""),
                MagicMock(success=True, stdout=""),
            ]
            result = await step.execute(ctx)

        assert result.success
        msg = mock_commit.call_args[0][0]
        assert msg.startswith("feat(billing): retry failed billing charges")

    async def test_deterministic_fallback_infers_scope_from_a_directory_segment(self, tmp_path: Path) -> None:
        """A project whose directory layout isn't SOVA's own must still get a
        real scope from the changed path, not just a configured default."""
        from sova.config.models import CommitConfig
        from sova.core.steps.commit import CommitStep

        ctx = _make_ctx(
            tmp_path,
            worktree_dir=tmp_path / "worktree",
            config=ProjectConfig(commit=CommitConfig(scopes=["accounts", "billing"])),
        )
        ctx.task = Task(id="73", title="gate the tax calculation")
        step = CommitStep()

        with (
            patch("sova.core.steps.commit.run") as mock_run,
            patch("sova.core.steps.commit.git_ops.commit", new_callable=AsyncMock) as mock_commit,
            patch("sova.core.steps.commit.invoke", new_callable=AsyncMock, side_effect=RuntimeError("stub")),
        ):
            mock_run.side_effect = [
                MagicMock(success=True, stdout=" apps/accounts/models.py | 5 +++++\n"),
                MagicMock(success=True, stdout=""),
                MagicMock(success=True, stdout="apps/accounts/models.py\0"),
                MagicMock(success=True, stdout=""),
                MagicMock(success=True, stdout=""),
            ]
            result = await step.execute(ctx)

        assert result.success
        msg = mock_commit.call_args[0][0]
        assert msg.startswith("feat(accounts): gate the tax calculation")

    async def test_long_path_scope_inference_survives_diffstat_truncation(self, tmp_path: Path) -> None:
        """`git diff --stat` abbreviates a long path's leading segments to
        "...", exactly the segments scope inference matches against; the
        exact `--name-only` path list must drive inference instead, or this
        one long path falls back to the default scope and poisons the
        result."""
        from sova.config.models import CommitConfig
        from sova.core.steps.commit import CommitStep

        long_path = "sova/db/migrations/versions/041_add_task_run_and_cost_record_more_indexes.py"
        ctx = _make_ctx(
            tmp_path,
            worktree_dir=tmp_path / "worktree",
            config=ProjectConfig(commit=CommitConfig(scopes=["core", "db"])),
        )
        ctx.task = Task(id="73", title="add more indexes")
        step = CommitStep()

        with (
            patch("sova.core.steps.commit.run") as mock_run,
            patch("sova.core.steps.commit.git_ops.commit", new_callable=AsyncMock) as mock_commit,
            patch("sova.core.steps.commit.invoke", new_callable=AsyncMock, side_effect=RuntimeError("stub")),
        ):
            mock_run.side_effect = [
                MagicMock(success=True, stdout=" .../migrations/versions/041_...py | 5 +++++\n"),  # abbreviated
                MagicMock(success=True, stdout=""),
                MagicMock(success=True, stdout=f"{long_path}\0"),  # exact path, never abbreviated
                MagicMock(success=True, stdout=""),
                MagicMock(success=True, stdout=""),
            ]
            result = await step.execute(ctx)

        assert result.success
        msg = mock_commit.call_args[0][0]
        assert msg.startswith("feat(db): add more indexes")

    async def test_deterministic_fallback_infers_docs_type_from_a_docs_only_change(self, tmp_path: Path) -> None:
        """A branch that only touches docs/ gets type "docs", not the
        project's default type, when the project configures "docs"."""
        from sova.core.steps.commit import CommitStep

        ctx = _make_ctx(tmp_path, worktree_dir=tmp_path / "worktree")
        ctx.task = Task(id="73", title="explain the retry policy")
        step = CommitStep()

        with (
            patch("sova.core.steps.commit.run") as mock_run,
            patch("sova.core.steps.commit.git_ops.commit", new_callable=AsyncMock) as mock_commit,
            patch("sova.core.steps.commit.invoke", new_callable=AsyncMock, side_effect=RuntimeError("stub")),
        ):
            mock_run.side_effect = [
                MagicMock(success=True, stdout=" docs/retry-policy.md | 5 +++++\n"),
                MagicMock(success=True, stdout=""),
                MagicMock(success=True, stdout="docs/retry-policy.md\0"),
                MagicMock(success=True, stdout=""),
                MagicMock(success=True, stdout=""),
            ]
            result = await step.execute(ctx)

        assert result.success
        msg = mock_commit.call_args[0][0]
        assert msg.startswith("docs(docs): explain the retry policy")

    async def test_commit_step_end_to_end_with_a_non_sova_type_and_scope_vocabulary(self, tmp_path: Path) -> None:
        """A project whose commit.types/scopes carry its own vocabulary (not
        SOVA's feat/fix/.../core/dashboard/...) must still commit
        successfully via the deterministic fallback when the LLM proposal
        fails, not raise and leave the step failed."""
        from sova.config.models import CommitConfig
        from sova.core.steps.commit import CommitStep

        ctx = _make_ctx(
            tmp_path,
            worktree_dir=tmp_path / "worktree",
            config=ProjectConfig(commit=CommitConfig(types=["feature", "bugfix"], scopes=["accounts"])),
        )
        ctx.task = Task(id="73", title="retry failed billing charges")
        step = CommitStep()

        with (
            patch("sova.core.steps.commit.run") as mock_run,
            patch("sova.core.steps.commit.git_ops.commit", new_callable=AsyncMock) as mock_commit,
            patch("sova.core.steps.commit.invoke", new_callable=AsyncMock, side_effect=RuntimeError("stub")),
        ):
            mock_run.side_effect = [
                MagicMock(success=True, stdout=" apps/accounts/models.py | 5 +++++\n"),
                MagicMock(success=True, stdout=""),
                MagicMock(success=True, stdout="apps/accounts/models.py\0"),
                MagicMock(success=True, stdout=""),
                MagicMock(success=True, stdout=""),
            ]
            result = await step.execute(ctx)

        assert result.success
        msg = mock_commit.call_args[0][0]
        assert msg.startswith("feature(accounts): retry failed billing charges")

    async def test_deterministic_fallback_strips_a_configured_non_sova_type_prefix(self, tmp_path: Path) -> None:
        """An issue title already prefixed with a project's own configured
        type must be stripped, not stapled with a second prefix."""
        from sova.config.models import CommitConfig
        from sova.core.steps.commit import CommitStep

        ctx = _make_ctx(
            tmp_path,
            worktree_dir=tmp_path / "worktree",
            config=ProjectConfig(commit=CommitConfig(types=["feature", "bugfix"], scopes=["accounts"])),
        )
        ctx.task = Task(id="73", title="feature: add widget")
        step = CommitStep()

        with (
            patch("sova.core.steps.commit.run") as mock_run,
            patch("sova.core.steps.commit.git_ops.commit", new_callable=AsyncMock) as mock_commit,
            patch("sova.core.steps.commit.invoke", new_callable=AsyncMock, side_effect=RuntimeError("stub")),
        ):
            mock_run.side_effect = [
                MagicMock(success=True, stdout=" apps/accounts/models.py | 5 +++++\n"),
                MagicMock(success=True, stdout=""),
                MagicMock(success=True, stdout="apps/accounts/models.py\0"),
                MagicMock(success=True, stdout=""),
                MagicMock(success=True, stdout=""),
            ]
            result = await step.execute(ctx)

        assert result.success
        msg = mock_commit.call_args[0][0]
        assert msg.startswith("feature(accounts): add widget")
        assert "feature(accounts): feature:" not in msg

    async def test_skips_hooks_when_budget_critically_low(self, tmp_path: Path) -> None:
        """Below the 8% threshold, commit passes no_verify=True to preserve work."""
        from sova.config.models import AgentConfig
        from sova.core.steps.commit import CommitStep

        config = ProjectConfig(agent=AgentConfig(max_budget=Decimal("10.00")))
        ctx = _make_ctx(tmp_path, worktree_dir=tmp_path / "worktree", config=config)
        ctx.cost_usd = Decimal("9.50")  # 5% remaining, below 8% threshold
        ctx.task = Task(id="73", title="LLM provider abstraction")
        step = CommitStep()

        with (
            patch("sova.core.steps.commit.run") as mock_run,
            patch("sova.core.steps.commit.git_ops.commit", new_callable=AsyncMock) as mock_commit,
        ):
            mock_run.side_effect = [
                MagicMock(success=True, stdout=" config/base.py | 5 +++++\n"),  # diff --stat HEAD
                MagicMock(success=True, stdout=""),  # diff --cached --stat
                MagicMock(success=True, stdout="config/base.py\0"),  # diff --name-only -z HEAD
                MagicMock(success=True, stdout=""),  # diff --cached --name-only -z
                MagicMock(success=True, stdout=""),  # untracked
                MagicMock(success=True, stdout=""),  # log (no commits yet)
            ]
            result = await step.execute(ctx)

        assert result.success
        assert mock_commit.call_args.kwargs["no_verify"] is True

    async def test_subject_proposal_skipped_when_budget_critically_low(self, tmp_path: Path) -> None:
        """The cheap LLM subject proposal is purely a quality improvement;
        a run with its budget nearly exhausted should not pay for it when
        the deterministic builder produces an acceptable subject for free."""
        from sova.config.models import AgentConfig
        from sova.core.steps.commit import CommitStep

        config = ProjectConfig(agent=AgentConfig(max_budget=Decimal("10.00")))
        ctx = _make_ctx(tmp_path, worktree_dir=tmp_path / "worktree", config=config)
        ctx.cost_usd = Decimal("9.50")  # 5% remaining, below the 8% threshold
        ctx.task = Task(id="73", title="LLM provider abstraction")
        step = CommitStep()

        with (
            patch("sova.core.steps.commit.run") as mock_run,
            patch("sova.core.steps.commit.git_ops.commit", new_callable=AsyncMock),
            patch("sova.core.steps.commit.invoke", new_callable=AsyncMock) as mock_invoke,
        ):
            mock_run.side_effect = [
                MagicMock(success=True, stdout=" config/base.py | 5 +++++\n"),
                MagicMock(success=True, stdout=""),
                MagicMock(success=True, stdout="config/base.py\0"),
                MagicMock(success=True, stdout=""),
                MagicMock(success=True, stdout=""),
                MagicMock(success=True, stdout=""),
            ]
            result = await step.execute(ctx)

        assert result.success
        mock_invoke.assert_not_awaited()

    async def test_normalizes_double_prefixed_task_title(self, tmp_path: Path) -> None:
        """Generated commit messages should not contain duplicate conventional prefixes."""
        from sova.core.steps.commit import CommitStep

        ctx = _make_ctx(tmp_path, worktree_dir=tmp_path / "worktree")
        ctx.task = Task(id="73", title="feat(core): validate commit messages")
        step = CommitStep()

        with (
            patch("sova.core.steps.commit.run") as mock_run,
            patch("sova.core.steps.commit.git_ops.commit", new_callable=AsyncMock) as mock_commit,
        ):
            mock_run.side_effect = [
                MagicMock(success=True, stdout=" commit.py | 3 +++\n"),
                MagicMock(success=True, stdout=""),
                MagicMock(success=True, stdout="commit.py\0"),
                MagicMock(success=True, stdout=""),
                MagicMock(success=True, stdout=""),
                MagicMock(success=True, stdout=""),
            ]
            result = await step.execute(ctx)

        assert result.success
        msg = mock_commit.call_args[0][0]
        assert msg.startswith("feat(core): validate commit messages")
        assert "feat(core): feat(core):" not in msg
        assert "Refs #42" in msg

    async def test_normalizes_scope_with_digits(self, tmp_path: Path) -> None:
        """Generated commit messages should normalize scopes containing digits."""
        from sova.core.steps.commit import CommitStep

        ctx = _make_ctx(tmp_path, worktree_dir=tmp_path / "worktree")
        ctx.task = Task(id="73", title="feat(api2): validate commit messages")
        step = CommitStep()

        with (
            patch("sova.core.steps.commit.run") as mock_run,
            patch("sova.core.steps.commit.git_ops.commit", new_callable=AsyncMock) as mock_commit,
        ):
            mock_run.side_effect = [
                MagicMock(success=True, stdout=" commit.py | 3 +++\n"),
                MagicMock(success=True, stdout=""),
                MagicMock(success=True, stdout="commit.py\0"),
                MagicMock(success=True, stdout=""),
                MagicMock(success=True, stdout=""),
                MagicMock(success=True, stdout=""),
            ]
            result = await step.execute(ctx)

        assert result.success
        msg = mock_commit.call_args[0][0]
        assert msg.startswith("feat(core): validate commit messages")
        assert "Refs #42" in msg

    async def test_skips_when_already_committed(self, tmp_path: Path) -> None:
        from sova.core.steps.commit import CommitStep

        ctx = _make_ctx(tmp_path, worktree_dir=tmp_path / "worktree")
        step = CommitStep()

        with patch("sova.core.steps.commit.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(success=True, stdout=""),  # diff --stat HEAD (clean)
                MagicMock(success=True, stdout=""),  # diff --cached --stat (clean)
                MagicMock(success=True, stdout=""),  # diff --name-only -z HEAD (clean)
                MagicMock(success=True, stdout=""),  # diff --cached --name-only -z (clean)
                MagicMock(success=True, stdout=""),  # untracked (clean)
                MagicMock(success=True, stdout="abc123 feat: something\n"),  # log (has commits)
            ]
            result = await step.execute(ctx)

        assert result.success
        assert "already exist" in result.summary

    async def test_fails_when_no_changes_and_no_commits(self, tmp_path: Path) -> None:
        from sova.core.steps.commit import CommitStep

        ctx = _make_ctx(tmp_path, worktree_dir=tmp_path / "worktree")
        step = CommitStep()

        with patch("sova.core.steps.commit.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(success=True, stdout=""),  # diff --stat HEAD
                MagicMock(success=True, stdout=""),  # diff --cached --stat
                MagicMock(success=True, stdout=""),  # diff --name-only -z HEAD
                MagicMock(success=True, stdout=""),  # diff --cached --name-only -z
                MagicMock(success=True, stdout=""),  # untracked
                MagicMock(success=True, stdout=""),  # log (no commits)
            ]
            result = await step.execute(ctx)

        assert not result.success

    async def test_message_build_failure_is_reported_not_raised(self, tmp_path: Path) -> None:
        """A bug in commit-message construction must surface as a failed
        StepResult, not crash the step: AGENTS.md's "failure must not look
        like success" cuts both ways, it also must not look like an
        unhandled exception that aborts the whole pipeline."""
        from sova.core.steps.commit import CommitStep

        ctx = _make_ctx(tmp_path, worktree_dir=tmp_path / "worktree")
        step = CommitStep()

        with (
            patch("sova.core.steps.commit.run") as mock_run,
            patch.object(CommitStep, "_build_commit_message", new_callable=AsyncMock, side_effect=ValueError("boom")),
        ):
            mock_run.side_effect = [
                MagicMock(success=True, stdout=" a.py | 1 +\n"),
                MagicMock(success=True, stdout=""),
                MagicMock(success=True, stdout="a.py\0"),
                MagicMock(success=True, stdout=""),
                MagicMock(success=True, stdout=""),
            ]
            result = await step.execute(ctx)

        assert not result.success
        assert "boom" in result.error

    async def test_gate_requires_commits_ahead(self, tmp_path: Path) -> None:
        from sova.core.steps.commit import CommitStep

        ctx = _make_ctx(tmp_path, worktree_dir=tmp_path / "worktree")
        step = CommitStep()

        with patch("sova.core.steps.commit.run") as mock_run:
            mock_run.return_value = MagicMock(success=True, stdout="")
            gate = await step.validate_output(ctx)

        assert not gate.passed

    async def test_gate_passes_with_commits(self, tmp_path: Path) -> None:
        from sova.core.steps.commit import CommitStep

        ctx = _make_ctx(tmp_path, worktree_dir=tmp_path / "worktree")
        step = CommitStep()

        with patch("sova.core.steps.commit.run") as mock_run:
            mock_run.return_value = MagicMock(success=True, stdout="abc123 feat: something\n")
            gate = await step.validate_output(ctx)

        assert gate.passed

    async def test_address_review_uses_fix_prefix(self, tmp_path: Path) -> None:
        """Address-review commits should use 'fix:' not 'feat:'."""
        from sova.core.steps.commit import CommitStep

        ctx = _make_ctx(
            tmp_path,
            worktree_dir=tmp_path / "worktree",
            pr_number=130,
            completed_steps=frozenset({"address_review"}),
        )
        step = CommitStep()

        with (
            patch("sova.core.steps.commit.run") as mock_run,
            patch("sova.core.steps.commit.git_ops.commit", new_callable=AsyncMock) as mock_commit,
        ):
            mock_run.side_effect = [
                MagicMock(success=True, stdout=" fix.py | 3 +++\n"),  # diff --stat HEAD
                MagicMock(success=True, stdout=""),  # diff --cached --stat
                MagicMock(success=True, stdout="fix.py\0"),  # diff --name-only -z HEAD
                MagicMock(success=True, stdout=""),  # diff --cached --name-only -z
                MagicMock(success=True, stdout=""),  # untracked
                MagicMock(success=True, stdout=""),  # log
            ]
            result = await step.execute(ctx)

        assert result.success
        msg = mock_commit.call_args[0][0]
        assert msg.startswith("fix(core):")
        assert "issue 42" in msg
        assert "Closes" not in msg

    async def test_developer_commit_without_task_uses_issue_number(self, tmp_path: Path) -> None:
        """When ctx.task is None (no adapter fetch), falls back to issue number."""
        from sova.core.steps.commit import CommitStep

        ctx = _make_ctx(tmp_path, worktree_dir=tmp_path / "worktree")
        ctx.task = None
        step = CommitStep()

        with (
            patch("sova.core.steps.commit.run") as mock_run,
            patch("sova.core.steps.commit.git_ops.commit", new_callable=AsyncMock) as mock_commit,
        ):
            mock_run.side_effect = [
                MagicMock(success=True, stdout=" fix.py | 3 +++\n"),
                MagicMock(success=True, stdout=""),
                MagicMock(success=True, stdout="fix.py\0"),
                MagicMock(success=True, stdout=""),
                MagicMock(success=True, stdout=""),
                MagicMock(success=True, stdout=""),
            ]
            result = await step.execute(ctx)

        assert result.success
        msg = mock_commit.call_args[0][0]
        assert "feat(core): issue 42" in msg
        assert "Refs #42" in msg


class TestValidateStep:
    @pytest.fixture(autouse=True)
    def _stub_hook_stdin(self):
        """Building the hook stdin issues its own run() calls, which would shift
        the positional side_effect lists below. Covered by test_validate_hook_stdin."""
        with patch(
            "sova.core.steps.validate.build_pre_push_stdin",
            AsyncMock(return_value=f"refs/heads/x {'a' * 40} refs/heads/x {'0' * 40}\n"),
        ):
            yield

    async def test_skips_when_no_hook(self, tmp_path: Path) -> None:
        from sova.core.steps.validate import ValidateStep

        ctx = _make_ctx(tmp_path, worktree_dir=tmp_path / "worktree")
        step = ValidateStep()

        with patch("sova.core.steps.validate.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(success=False, stdout=""),  # git config --get core.hooksPath
                MagicMock(success=True, stdout=".git\n"),  # git rev-parse --git-dir
                MagicMock(success=False, stdout=""),  # test -x (no hook)
            ]
            result = await step.execute(ctx)

        assert result.success
        assert "no pre-push hook" in result.summary.lower()

    async def test_passes_when_hook_succeeds(self, tmp_path: Path) -> None:
        from sova.core.steps.validate import ValidateStep

        ctx = _make_ctx(tmp_path, worktree_dir=tmp_path / "worktree")
        step = ValidateStep()

        with patch("sova.core.steps.validate.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(success=True, stdout=".githooks\n"),  # core.hooksPath
                MagicMock(success=True, stdout=""),  # test -x (hook exists)
                MagicMock(success=True, stdout="All checks passed\n", stderr="", timed_out=False),  # hook run
            ]
            result = await step.execute(ctx)

        assert result.success
        assert "passed" in result.summary.lower()
        assert step._fix_loop_ran is False

    async def test_invokes_llm_on_hook_failure(self, tmp_path: Path) -> None:
        from sova.core.steps.validate import ValidateStep
        from sova.llm.models import LLMResult

        ctx = _make_ctx(tmp_path, worktree_dir=tmp_path / "worktree")
        step = ValidateStep()

        with (
            patch("sova.core.steps.validate.run") as mock_run,
            patch("sova.core.steps.validate.dispatch_prompt", new_callable=AsyncMock) as mock_invoke,
        ):
            mock_run.side_effect = [
                MagicMock(success=True, stdout=".githooks\n"),  # core.hooksPath
                MagicMock(success=True, stdout=""),  # test -x
                MagicMock(success=False, stdout="FAIL: missing type hints\n", stderr="", timed_out=False),  # hook fails
                MagicMock(success=True, stdout="abc123\n"),  # pre-dispatch HEAD
                MagicMock(success=True, stdout="abc123\n"),  # post-dispatch HEAD (unchanged)
                MagicMock(
                    success=True, stdout="All checks passed\n", stderr="", timed_out=False
                ),  # hook passes after fix
                MagicMock(success=True, stdout=""),  # git status --porcelain (worktree clean)
            ]
            mock_invoke.return_value = LLMResult(
                text="Fixed type hints",
                model="sonnet",
                cost_usd=Decimal("0.02"),
            )
            result = await step.execute(ctx)

        assert result.success
        assert "1 fix attempt" in result.summary
        assert step._fix_loop_ran is True
        mock_invoke.assert_awaited_once()
        prompt = mock_invoke.call_args[1].get("prompt", mock_invoke.call_args[0][1])
        assert "missing type hints" in prompt
        assert "leave the changes uncommitted" in prompt
        assert "do not run git commit" in prompt

    async def test_folds_fix_into_branch_before_retrying_hook(self, tmp_path: Path) -> None:
        """The fix is folded into its commit (fixup + autosquash) before the
        hook is retried, not committed on its own as a separate commit."""
        from sova.core.steps.validate import ValidateStep
        from sova.llm.models import LLMResult

        ctx = _make_ctx(tmp_path, worktree_dir=tmp_path / "worktree")
        step = ValidateStep()

        with (
            patch("sova.core.steps.validate.run") as mock_run,
            patch("sova.core.steps.validate.dispatch_prompt", new_callable=AsyncMock) as mock_invoke,
            patch("sova.core.steps.validate.fold_fix_into_branch", new_callable=AsyncMock) as mock_fold,
        ):
            mock_run.side_effect = [
                MagicMock(success=True, stdout=".githooks\n"),  # core.hooksPath
                MagicMock(success=True, stdout=""),  # test -x
                MagicMock(success=False, stdout="FAIL: error\n", stderr="", timed_out=False),  # hook fails
                MagicMock(success=True, stdout="abc123\n"),  # pre-dispatch HEAD
                MagicMock(success=True, stdout="abc123\n"),  # post-dispatch HEAD (unchanged)
                MagicMock(success=True, stdout="All checks passed\n", stderr="", timed_out=False),  # hook passes
            ]
            mock_fold.return_value = True
            mock_invoke.return_value = LLMResult(text="Fixed", model="sonnet", cost_usd=Decimal("0.01"))
            result = await step.execute(ctx)

        assert result.success
        mock_fold.assert_awaited_once_with(ctx.working_dir, ctx.base_branch)

    async def test_unwinds_an_ordinary_commit_the_fix_agent_made_despite_instructions(self, tmp_path: Path) -> None:
        """The fix prompt asks the agent to leave a file-content fix
        uncommitted; an agent that commits it normally anyway must still
        have it folded into the branch commit, not left as a separate,
        non-conventional commit `fold_fix_into_branch()` never looks at."""
        from sova.core.steps.validate import ValidateStep
        from sova.utils.shell import run

        await run("git", "init", "-q", "-b", "main", cwd=tmp_path)
        await run("git", "config", "user.email", "test@example.com", cwd=tmp_path)
        await run("git", "config", "user.name", "Test User", cwd=tmp_path)
        await run("git", "config", "commit.gpgsign", "false", cwd=tmp_path)
        await run("git", "config", "core.hooksPath", "/dev/null", cwd=tmp_path)
        (tmp_path / "a.py").write_text("a\n")
        await run("git", "add", "a.py", cwd=tmp_path)
        await run("git", "commit", "-m", "feat(core): add a", cwd=tmp_path)

        pre_head = await run("git", "rev-parse", "HEAD", cwd=tmp_path)

        (tmp_path / "a.py").write_text("a\nfixed\n")
        await run("git", "add", "a.py", cwd=tmp_path)
        await run("git", "commit", "-m", "fix: address pre-push hook violations", cwd=tmp_path)

        await ValidateStep._unwind_ordinary_commits(tmp_path, pre_head, 1, "main")

        head_after = await run("git", "rev-parse", "HEAD", cwd=tmp_path)
        assert head_after.stdout.strip() == pre_head.stdout.strip()
        staged = await run("git", "diff", "--cached", "--name-only", cwd=tmp_path)
        assert staged.stdout.strip() == "a.py"

    async def test_leaves_a_deliberate_reword_marker_commit_alone(self, tmp_path: Path) -> None:
        """A commit the agent made on purpose via `--fixup=reword:<sha>`
        (an `amend!` marker) must not be unwound: `fold_fix_into_branch()`
        is what handles it, via autosquash."""
        from sova.core.steps.validate import ValidateStep
        from sova.utils.shell import run

        await run("git", "init", "-q", "-b", "main", cwd=tmp_path)
        await run("git", "config", "user.email", "test@example.com", cwd=tmp_path)
        await run("git", "config", "user.name", "Test User", cwd=tmp_path)
        await run("git", "config", "commit.gpgsign", "false", cwd=tmp_path)
        await run("git", "config", "core.hooksPath", "/dev/null", cwd=tmp_path)
        (tmp_path / "a.py").write_text("a\n")
        await run("git", "add", "a.py", cwd=tmp_path)
        await run("git", "commit", "-m", "Add a (bad subject)", cwd=tmp_path)
        target = (await run("git", "rev-parse", "HEAD", cwd=tmp_path)).stdout.strip()

        pre_head = await run("git", "rev-parse", "HEAD", cwd=tmp_path)
        await run("git", "commit", "--fixup", f"reword:{target}", "--allow-empty", cwd=tmp_path)
        marker_head = await run("git", "rev-parse", "HEAD", cwd=tmp_path)

        await ValidateStep._unwind_ordinary_commits(tmp_path, pre_head, 1, "main")

        head_after = await run("git", "rev-parse", "HEAD", cwd=tmp_path)
        assert head_after.stdout.strip() == marker_head.stdout.strip()

    async def test_leaves_an_amended_reword_alone(self, tmp_path: Path) -> None:
        """`git commit --amend` is the natural way an agent rewords a commit's
        message: pre_dispatch_head is no longer an ancestor of the new HEAD
        afterward, and the reword must survive, not be reset away."""
        from sova.core.steps.validate import ValidateStep
        from sova.utils.shell import run

        await run("git", "init", "-q", "-b", "main", cwd=tmp_path)
        await run("git", "config", "user.email", "test@example.com", cwd=tmp_path)
        await run("git", "config", "user.name", "Test User", cwd=tmp_path)
        await run("git", "config", "commit.gpgsign", "false", cwd=tmp_path)
        await run("git", "config", "core.hooksPath", "/dev/null", cwd=tmp_path)
        (tmp_path / "a.py").write_text("a\n")
        await run("git", "add", "a.py", cwd=tmp_path)
        await run("git", "commit", "-m", "Bad subject not conventional", cwd=tmp_path)

        pre_head = await run("git", "rev-parse", "HEAD", cwd=tmp_path)
        await run("git", "commit", "--amend", "-m", "feat(core): add a", cwd=tmp_path)
        amended_head = await run("git", "rev-parse", "HEAD", cwd=tmp_path)
        assert amended_head.stdout.strip() != pre_head.stdout.strip()

        await ValidateStep._unwind_ordinary_commits(tmp_path, pre_head, 1, "main")

        head_after = await run("git", "rev-parse", "HEAD", cwd=tmp_path)
        assert head_after.stdout.strip() == amended_head.stdout.strip(), "the reword must survive, not be reverted"
        subject = await run("git", "log", "-1", "--format=%s", cwd=tmp_path)
        assert subject.stdout.strip() == "feat(core): add a"
        status = await run("git", "status", "--porcelain", cwd=tmp_path)
        assert status.stdout.strip() == ""

    async def test_leaves_an_amend_that_also_changes_content_alone(self, tmp_path: Path) -> None:
        """An amend that both rewords the subject and changes file content
        must survive intact, and the content ends up folded into a branch
        commit rather than left staged or reverted."""
        from sova.core.steps._commit_fold import fold_fix_into_branch
        from sova.core.steps.validate import ValidateStep
        from sova.utils.shell import run

        await run("git", "init", "-q", "-b", "main", cwd=tmp_path)
        await run("git", "config", "user.email", "test@example.com", cwd=tmp_path)
        await run("git", "config", "user.name", "Test User", cwd=tmp_path)
        await run("git", "config", "commit.gpgsign", "false", cwd=tmp_path)
        await run("git", "config", "core.hooksPath", "/dev/null", cwd=tmp_path)
        (tmp_path / "a.py").write_text("a\n")
        await run("git", "add", "a.py", cwd=tmp_path)
        await run("git", "commit", "-m", "Bad subject not conventional", cwd=tmp_path)
        head = (await run("git", "rev-parse", "HEAD", cwd=tmp_path)).stdout.strip()
        await run("git", "update-ref", "refs/remotes/origin/main", head, cwd=tmp_path)
        await run("git", "commit", "--allow-empty", "-m", "feat(core): add b", cwd=tmp_path)

        pre_head = await run("git", "rev-parse", "HEAD", cwd=tmp_path)
        (tmp_path / "a.py").write_text("a\nfixed\n")
        await run("git", "add", "a.py", cwd=tmp_path)
        await run("git", "commit", "--amend", "-m", "feat(core): add b (fixed)", cwd=tmp_path)
        amended_head = await run("git", "rev-parse", "HEAD", cwd=tmp_path)

        await ValidateStep._unwind_ordinary_commits(tmp_path, pre_head, 1, "main")

        head_after = await run("git", "rev-parse", "HEAD", cwd=tmp_path)
        assert head_after.stdout.strip() == amended_head.stdout.strip()

        folded = await fold_fix_into_branch(tmp_path, "main")
        assert folded is False, "nothing left to fold: the amend already carries the content fix"
        status = await run("git", "status", "--porcelain", cwd=tmp_path)
        assert status.stdout.strip() == ""
        content = (tmp_path / "a.py").read_text()
        assert content == "a\nfixed\n"

    async def test_nothing_to_fold_is_not_misreported_as_fix_left_uncommitted(self, tmp_path: Path) -> None:
        """When nothing needed folding (fold returns False with no stray
        marker commit) and the worktree is clean, the pass summary must not
        claim a fix was "left uncommitted": there is none."""
        from sova.core.steps.validate import ValidateStep
        from sova.llm.models import LLMResult

        ctx = _make_ctx(tmp_path, worktree_dir=tmp_path / "worktree")
        step = ValidateStep()

        with (
            patch("sova.core.steps.validate.run") as mock_run,
            patch("sova.core.steps.validate.dispatch_prompt", new_callable=AsyncMock) as mock_invoke,
            patch("sova.core.steps.validate.fold_fix_into_branch", new_callable=AsyncMock, return_value=False),
            patch("sova.core.steps.validate.has_unresolved_marker_commits", new_callable=AsyncMock, return_value=False),
        ):
            mock_run.side_effect = [
                MagicMock(success=True, stdout=".githooks\n"),
                MagicMock(success=True, stdout=""),
                MagicMock(success=False, stdout="FAIL: error\n", stderr="", timed_out=False),
                MagicMock(success=True, stdout="abc123\n"),  # pre-dispatch HEAD
                MagicMock(success=True, stdout="abc123\n"),  # post-dispatch HEAD (unchanged)
                MagicMock(success=True, stdout="All checks passed\n", stderr="", timed_out=False),
                MagicMock(success=True, stdout=""),  # git status --porcelain: clean tree
            ]
            mock_invoke.return_value = LLMResult(text="Fixed", model="sonnet", cost_usd=Decimal("0.01"))
            result = await step.execute(ctx)

        assert result.success
        assert "fix left uncommitted" not in result.summary
        assert "1 fix attempt" in result.summary

    async def test_fold_failure_with_a_dirty_tree_fails_the_attempt(self, tmp_path: Path) -> None:
        """When the fold genuinely fails and the fix is still sitting
        uncommitted, the hook passing against that worktree content must not
        be reported as success: the fix never reached the branch."""
        from sova.core.steps.validate import ValidateStep
        from sova.llm.models import LLMResult

        ctx = _make_ctx(tmp_path, worktree_dir=tmp_path / "worktree")
        step = ValidateStep()

        with (
            patch("sova.core.steps.validate.run") as mock_run,
            patch("sova.core.steps.validate.dispatch_prompt", new_callable=AsyncMock) as mock_invoke,
            patch("sova.core.steps.validate.fold_fix_into_branch", new_callable=AsyncMock, return_value=False),
            patch("sova.core.steps.validate.has_unresolved_marker_commits", new_callable=AsyncMock, return_value=False),
        ):
            mock_run.side_effect = [
                MagicMock(success=True, stdout=".githooks\n"),
                MagicMock(success=True, stdout=""),
                MagicMock(success=False, stdout="FAIL: error\n", stderr="", timed_out=False),
                MagicMock(success=True, stdout="abc123\n"),  # pre-dispatch HEAD
                MagicMock(success=True, stdout="abc123\n"),  # post-dispatch HEAD (unchanged)
                MagicMock(success=True, stdout="All checks passed\n", stderr="", timed_out=False),
                MagicMock(success=True, stdout="?? new_file.py\n"),  # git status --porcelain: still dirty
            ]
            mock_invoke.return_value = LLMResult(text="Fixed", model="sonnet", cost_usd=Decimal("0.01"))
            result = await step.execute(ctx)

        assert not result.success
        assert result.error == "fold_failed"
        assert "could not be folded" in result.summary

    async def test_fails_the_attempt_when_a_marker_commit_is_stranded(self, tmp_path: Path) -> None:
        """A fold that leaves a fixup!/squash!/amend! marker commit on the
        branch must fail the attempt rather than retry the hook on a branch
        that is not pushable."""
        from sova.core.steps.validate import ValidateStep
        from sova.llm.models import LLMResult

        ctx = _make_ctx(tmp_path, worktree_dir=tmp_path / "worktree")
        step = ValidateStep()

        with (
            patch("sova.core.steps.validate.run") as mock_run,
            patch("sova.core.steps.validate.dispatch_prompt", new_callable=AsyncMock) as mock_invoke,
            patch("sova.core.steps.validate.fold_fix_into_branch", new_callable=AsyncMock, return_value=False),
            patch("sova.core.steps.validate.has_unresolved_marker_commits", new_callable=AsyncMock, return_value=True),
        ):
            mock_run.side_effect = [
                MagicMock(success=True, stdout=".githooks\n"),
                MagicMock(success=True, stdout=""),
                MagicMock(success=False, stdout="FAIL: error\n", stderr="", timed_out=False),
                MagicMock(success=True, stdout="abc123\n"),  # pre-dispatch HEAD
                MagicMock(success=True, stdout="abc123\n"),  # post-dispatch HEAD (unchanged)
            ]
            mock_invoke.return_value = LLMResult(text="Fixed", model="sonnet", cost_usd=Decimal("0.01"))
            result = await step.execute(ctx)

        assert not result.success
        assert result.error == "marker_commit_stranded"
        mock_invoke.assert_awaited_once()

    async def test_fails_after_max_attempts(self, tmp_path: Path) -> None:
        from sova.core.steps.validate import ValidateStep
        from sova.llm.models import LLMResult

        ctx = _make_ctx(tmp_path, worktree_dir=tmp_path / "worktree")
        step = ValidateStep()

        with (
            patch("sova.core.steps.validate.run") as mock_run,
            patch("sova.core.steps.validate.dispatch_prompt", new_callable=AsyncMock) as mock_invoke,
        ):
            mock_run.side_effect = [
                MagicMock(success=True, stdout=".githooks\n"),  # core.hooksPath
                MagicMock(success=True, stdout=""),  # test -x
                MagicMock(success=False, stdout="FAIL: error\n", stderr="", timed_out=False),  # hook fails
                MagicMock(success=True, stdout="abc123\n"),  # pre-dispatch HEAD (attempt 1)
                MagicMock(success=True, stdout="abc123\n"),  # post-dispatch HEAD (attempt 1, unchanged)
                MagicMock(success=False, stdout="FAIL: error\n", stderr="", timed_out=False),  # still fails after fix 1
                MagicMock(success=True, stdout="abc123\n"),  # pre-dispatch HEAD (attempt 2)
                MagicMock(success=True, stdout="abc123\n"),  # post-dispatch HEAD (attempt 2, unchanged)
                MagicMock(success=False, stdout="FAIL: error\n", stderr="", timed_out=False),  # still fails after fix 2
            ]
            mock_invoke.return_value = LLMResult(
                text="Attempted fix",
                model="sonnet",
                cost_usd=Decimal("0.01"),
            )
            result = await step.execute(ctx)

        assert not result.success
        assert "still failing" in result.summary.lower()
        assert mock_invoke.await_count == 2

    async def test_fix_loop_escalates_to_dev_model_after_first_failed_attempt(self, tmp_path: Path) -> None:
        """Issue #780: attempt 1 uses llm.routing["validate"]; a failed attempt 1
        escalates every remaining attempt to the step's dev model directly,
        bypassing task-type routing, and records why on the step-scoped reason."""
        from sova.config.models import LLMConfig, ProjectConfig
        from sova.core.steps.validate import ValidateStep
        from sova.llm.models import LLMResult

        ctx = _make_ctx(
            tmp_path,
            worktree_dir=tmp_path / "worktree",
            config=ProjectConfig(llm=LLMConfig(routing={"validate": "haiku"})),
        )
        step = ValidateStep()

        with (
            patch("sova.core.steps.validate.run") as mock_run,
            patch("sova.core.steps.validate.dispatch_prompt", new_callable=AsyncMock) as mock_invoke,
        ):
            mock_run.side_effect = [
                MagicMock(success=True, stdout=".githooks\n"),  # core.hooksPath
                MagicMock(success=True, stdout=""),  # test -x
                MagicMock(success=False, stdout="FAIL: error\n", stderr="", timed_out=False),  # hook fails
                MagicMock(success=True, stdout="abc123\n"),  # pre-dispatch HEAD (attempt 1)
                MagicMock(success=True, stdout="abc123\n"),  # post-dispatch HEAD (attempt 1, unchanged)
                MagicMock(success=False, stdout="FAIL: error\n", stderr="", timed_out=False),  # still fails after fix 1
                MagicMock(success=True, stdout="abc123\n"),  # pre-dispatch HEAD (attempt 2)
                MagicMock(success=True, stdout="abc123\n"),  # post-dispatch HEAD (attempt 2, unchanged)
                MagicMock(success=True, stdout="All checks passed\n", stderr="", timed_out=False),  # fix 2 passes
                MagicMock(success=True, stdout=""),  # git status --porcelain (worktree clean)
            ]
            # routed_task_type_model simulates what the real dispatch_prompt()
            # (mocked out here) would set on an attempt whose configured
            # route survived the compatibility check (issue #780 review):
            # only attempt 1 carries a task_type, so this has no bearing on
            # attempt 2, which never routes regardless of the field.
            mock_invoke.return_value = LLMResult(
                text="Attempted fix", model="sonnet", cost_usd=Decimal("0.01"), routed_task_type_model="haiku"
            )
            result = await step.execute(ctx)

        assert result.success
        assert mock_invoke.await_count == 2
        first_call, second_call = mock_invoke.call_args_list
        assert first_call.kwargs["task_type"] == "validate"
        assert second_call.kwargs["task_type"] is None
        assert first_call.kwargs["model"] == second_call.kwargs["model"]
        assert "escalated to dev model" in ctx.step_model_selection_reason
        assert ctx.model_selection_reason is None

    async def test_fix_loop_buckets_the_routed_attempt_spend_under_the_model_that_ran(self, tmp_path: Path) -> None:
        """Issue #780 review: attempt 1's routed spend lands in routed_spend under the
        model that ran; attempt 2 on the dev model stays on the step's provider totals."""
        from sova.config.models import LLMConfig, ProjectConfig
        from sova.core.steps.validate import ValidateStep
        from sova.llm.models import LLMResult

        ctx = _make_ctx(
            tmp_path,
            worktree_dir=tmp_path / "worktree",
            config=ProjectConfig(llm=LLMConfig(routing={"validate": "claude-haiku-4-5"})),
        )
        ctx.resolved_model = "claude-opus-5"
        step = ValidateStep()

        with (
            patch("sova.core.steps.validate.run") as mock_run,
            patch("sova.core.steps.validate.dispatch_prompt", new_callable=AsyncMock) as mock_invoke,
        ):
            mock_run.side_effect = [
                MagicMock(success=True, stdout=".githooks\n"),
                MagicMock(success=True, stdout=""),
                MagicMock(success=False, stdout="FAIL: error\n", stderr="", timed_out=False),
                MagicMock(success=True, stdout="abc123\n"),  # pre-dispatch HEAD (attempt 1)
                MagicMock(success=True, stdout="abc123\n"),  # post-dispatch HEAD (attempt 1, unchanged)
                MagicMock(success=False, stdout="FAIL: error\n", stderr="", timed_out=False),
                MagicMock(success=True, stdout="abc123\n"),  # pre-dispatch HEAD (attempt 2)
                MagicMock(success=True, stdout="abc123\n"),  # post-dispatch HEAD (attempt 2, unchanged)
                MagicMock(success=True, stdout="All checks passed\n", stderr="", timed_out=False),
                MagicMock(success=True, stdout=""),  # git status --porcelain (worktree clean)
            ]
            mock_invoke.side_effect = [
                LLMResult(
                    text="Attempted fix",
                    model="claude-haiku-4-5",
                    cost_usd=Decimal("0.01"),
                    routed_task_type_model="claude-haiku-4-5",
                ),
                LLMResult(text="Fixed", model="claude-opus-5", cost_usd=Decimal("0.20")),
            ]
            result = await step.execute(ctx)

        assert result.success
        assert set(ctx.routed_spend) == {"claude-haiku-4-5"}
        routed = ctx.routed_spend["claude-haiku-4-5"]
        assert routed.cost_usd == Decimal("0.01")
        assert routed.reason == "task_type:validate fix attempt 1 routed to claude-haiku-4-5"

    async def test_fix_loop_without_a_route_never_splits_spend(self, tmp_path: Path) -> None:
        """With no route configured, a result whose id differs from resolved_model
        (an alias spelled differently) still stays on the provider path."""
        from sova.core.steps.validate import ValidateStep
        from sova.llm.models import LLMResult

        ctx = _make_ctx(tmp_path, worktree_dir=tmp_path / "worktree")
        ctx.resolved_model = "claude-opus-5"
        step = ValidateStep()

        with (
            patch("sova.core.steps.validate.run") as mock_run,
            patch("sova.core.steps.validate.dispatch_prompt", new_callable=AsyncMock) as mock_invoke,
        ):
            mock_run.side_effect = [
                MagicMock(success=True, stdout=".githooks\n"),
                MagicMock(success=True, stdout=""),
                MagicMock(success=False, stdout="FAIL: error\n", stderr="", timed_out=False),
                MagicMock(success=True, stdout="abc123\n"),  # pre-dispatch HEAD
                MagicMock(success=True, stdout="abc123\n"),  # post-dispatch HEAD (unchanged)
                MagicMock(success=True, stdout="All checks passed\n", stderr="", timed_out=False),
                MagicMock(success=True, stdout=""),  # git status --porcelain (worktree clean)
            ]
            mock_invoke.return_value = LLMResult(text="Fixed", model="claude-opus-5-20261001", cost_usd=Decimal("0.2"))
            result = await step.execute(ctx)

        assert result.success
        assert ctx.routed_spend == {}

    async def test_fix_loop_records_no_escalation_while_a_fallback_is_in_flight(self, tmp_path: Path) -> None:
        """ctx.routing_task_type() suppresses the route once a fallback is walking the
        chain, so attempt 1 never used it and attempt 2 is not an escalation off it."""
        from sova.config.models import LLMConfig, ProjectConfig
        from sova.core.steps.validate import ValidateStep
        from sova.llm.models import LLMResult

        ctx = _make_ctx(
            tmp_path,
            worktree_dir=tmp_path / "worktree",
            config=ProjectConfig(llm=LLMConfig(routing={"validate": "haiku"})),
            fallback_model_index=1,
        )
        step = ValidateStep()

        with (
            patch("sova.core.steps.validate.run") as mock_run,
            patch("sova.core.steps.validate.dispatch_prompt", new_callable=AsyncMock) as mock_invoke,
        ):
            mock_run.side_effect = [
                MagicMock(success=True, stdout=".githooks\n"),
                MagicMock(success=True, stdout=""),
                MagicMock(success=False, stdout="FAIL: error\n", stderr="", timed_out=False),
                MagicMock(success=True, stdout="abc123\n"),  # pre-dispatch HEAD (attempt 1)
                MagicMock(success=True, stdout="abc123\n"),  # post-dispatch HEAD (attempt 1, unchanged)
                MagicMock(success=False, stdout="FAIL: error\n", stderr="", timed_out=False),
                MagicMock(success=True, stdout="abc123\n"),  # pre-dispatch HEAD (attempt 2)
                MagicMock(success=True, stdout="abc123\n"),  # post-dispatch HEAD (attempt 2, unchanged)
                MagicMock(success=True, stdout="All checks passed\n", stderr="", timed_out=False),
                MagicMock(success=True, stdout=""),  # git status --porcelain (worktree clean)
            ]
            mock_invoke.return_value = LLMResult(text="Attempted fix", model="sonnet", cost_usd=Decimal("0.01"))
            result = await step.execute(ctx)

        assert result.success
        first_call, second_call = mock_invoke.call_args_list
        assert first_call.kwargs["task_type"] is None
        assert second_call.kwargs["task_type"] is None
        assert ctx.step_model_selection_reason is None

    async def test_fix_loop_records_no_escalation_without_a_configured_route(self, tmp_path: Path) -> None:
        """With the default empty llm.routing both attempts resolve to the same
        model, so recording an escalation would be a false cost-analysis record."""
        from sova.core.steps.validate import ValidateStep
        from sova.llm.models import LLMResult

        ctx = _make_ctx(tmp_path, worktree_dir=tmp_path / "worktree")
        step = ValidateStep()

        with (
            patch("sova.core.steps.validate.run") as mock_run,
            patch("sova.core.steps.validate.dispatch_prompt", new_callable=AsyncMock) as mock_invoke,
        ):
            mock_run.side_effect = [
                MagicMock(success=True, stdout=".githooks\n"),
                MagicMock(success=True, stdout=""),
                MagicMock(success=False, stdout="FAIL: error\n", stderr="", timed_out=False),
                MagicMock(success=True, stdout="abc123\n"),  # pre-dispatch HEAD (attempt 1)
                MagicMock(success=True, stdout="abc123\n"),  # post-dispatch HEAD (attempt 1, unchanged)
                MagicMock(success=False, stdout="FAIL: error\n", stderr="", timed_out=False),
                MagicMock(success=True, stdout="abc123\n"),  # pre-dispatch HEAD (attempt 2)
                MagicMock(success=True, stdout="abc123\n"),  # post-dispatch HEAD (attempt 2, unchanged)
                MagicMock(success=True, stdout="All checks passed\n", stderr="", timed_out=False),
                MagicMock(success=True, stdout=""),  # git status --porcelain (worktree clean)
            ]
            mock_invoke.return_value = LLMResult(text="Attempted fix", model="sonnet", cost_usd=Decimal("0.01"))
            result = await step.execute(ctx)

        assert result.success
        assert mock_invoke.await_count == 2
        assert ctx.step_model_selection_reason is None

    async def test_llm_timeout_produces_distinct_marker(self, tmp_path: Path) -> None:
        """A fix-LLM timeout must be tagged distinctly from a generic RuntimeError
        and from the unrelated step_hard_timeout string (issue #977)."""
        from sova.core.steps.validate import ValidateStep
        from sova.llm.errors import LLMTimeoutError

        ctx = _make_ctx(tmp_path, worktree_dir=tmp_path / "worktree")
        step = ValidateStep()

        with (
            patch("sova.core.steps.validate.run") as mock_run,
            patch(
                "sova.core.steps.validate.dispatch_prompt",
                new_callable=AsyncMock,
                side_effect=LLMTimeoutError("Command timed out after 180s"),
            ),
        ):
            mock_run.side_effect = [
                MagicMock(success=True, stdout=".githooks\n"),  # core.hooksPath
                MagicMock(success=True, stdout=""),  # test -x
                MagicMock(success=False, stdout="FAIL: error\n", stderr="", timed_out=False),  # hook fails
                MagicMock(success=True, stdout="abc123\n"),  # pre-dispatch HEAD
            ]
            result = await step.execute(ctx)

        assert not result.success
        assert result.error.startswith("fix_llm_timeout on cycle 1:")
        assert result.error != "step_hard_timeout"

    async def test_low_budget_fraction_stops_retry_without_failing(self, tmp_path: Path) -> None:
        """Below the 20% threshold, the fix loop stops retrying but reports success."""
        from sova.config.models import AgentConfig
        from sova.core.steps.validate import ValidateStep

        config = ProjectConfig(agent=AgentConfig(max_budget=Decimal("10.00")))
        ctx = _make_ctx(tmp_path, worktree_dir=tmp_path / "worktree", config=config)
        ctx.cost_usd = Decimal("8.50")  # 15% remaining, below 20% threshold
        step = ValidateStep()

        with (
            patch("sova.core.steps.validate.run") as mock_run,
            patch("sova.core.steps.validate.dispatch_prompt", new_callable=AsyncMock) as mock_invoke,
        ):
            mock_run.side_effect = [
                MagicMock(success=True, stdout=".githooks\n"),  # core.hooksPath
                MagicMock(success=True, stdout=""),  # test -x
                MagicMock(success=False, stdout="FAIL: error\n", stderr="", timed_out=False),  # hook fails
            ]
            result = await step.execute(ctx)

        assert result.success
        assert "stopping fix retries" in result.summary
        mock_invoke.assert_not_awaited()

    async def test_gate_requires_commits(self, tmp_path: Path) -> None:
        from sova.core.steps.validate import ValidateStep

        ctx = _make_ctx(tmp_path, worktree_dir=tmp_path / "worktree")
        step = ValidateStep()

        with patch("sova.core.steps.validate.run") as mock_run:
            mock_run.return_value = MagicMock(success=True, stdout="")
            gate = await step.validate_output(ctx)

        assert not gate.passed

    async def test_gate_passes_with_commits(self, tmp_path: Path) -> None:
        from sova.core.steps.validate import ValidateStep

        ctx = _make_ctx(tmp_path, worktree_dir=tmp_path / "worktree")
        step = ValidateStep()

        with patch("sova.core.steps.validate.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(success=True, stdout="abc123 feat: something\n"),  # git log
                MagicMock(success=True, stdout=""),  # git diff --stat HEAD (unstaged)
                MagicMock(success=True, stdout=""),  # git diff --cached --stat (staged)
                MagicMock(success=True, stdout=""),  # git status --porcelain --untracked-files=all
            ]
            gate = await step.validate_output(ctx)

        assert gate.passed

    async def test_gate_ignores_preexisting_untracked_files_when_the_hook_never_failed(self, tmp_path: Path) -> None:
        """An untracked build/test artifact already sitting in the worktree
        (unrelated to this step) must not fail the gate when the hook passed
        without ever needing a fix: the untracked check only guards against
        what the fix loop itself might leave behind."""
        from sova.core.steps.validate import ValidateStep

        ctx = _make_ctx(tmp_path, worktree_dir=tmp_path / "worktree")
        step = ValidateStep()
        assert step._fix_loop_ran is False

        with patch("sova.core.steps.validate.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(success=True, stdout="abc123 feat: something\n"),  # git log
                MagicMock(success=True, stdout=""),  # git diff --stat HEAD (unstaged)
                MagicMock(success=True, stdout=""),  # git diff --cached --stat (staged)
            ]
            gate = await step.validate_output(ctx)

        assert gate.passed
        # The untracked check is skipped entirely: no 4th git call.
        assert mock_run.call_count == 3

    async def test_gate_fails_on_untracked_files_once_the_fix_loop_ran(self, tmp_path: Path) -> None:
        """Once the hook failed and the fix loop ran, a leftover untracked
        file is still a genuine gate failure."""
        from sova.core.steps.validate import ValidateStep

        ctx = _make_ctx(tmp_path, worktree_dir=tmp_path / "worktree")
        step = ValidateStep()
        step._fix_loop_ran = True

        with patch("sova.core.steps.validate.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(success=True, stdout="abc123 feat: something\n"),  # git log
                MagicMock(success=True, stdout=""),  # git diff --stat HEAD (unstaged)
                MagicMock(success=True, stdout=""),  # git diff --cached --stat (staged)
                MagicMock(success=True, stdout="?? leftover.py\n"),  # git status (untracked)
            ]
            gate = await step.validate_output(ctx)

        assert not gate.passed
        assert "Untracked" in gate.reason

    async def test_auto_detects_githooks_directory(self, tmp_path: Path) -> None:
        from sova.core.steps.validate import find_pre_push_hook

        hooks_dir = tmp_path / ".githooks"
        hooks_dir.mkdir()
        hook_file = hooks_dir / "pre-push"
        hook_file.write_text("#!/bin/bash\nexit 0\n")
        hook_file.chmod(0o755)

        with patch("sova.core.steps.validate.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(success=False, stdout=""),  # core.hooksPath not set
                MagicMock(success=True, stdout=f"{tmp_path}\n"),  # git rev-parse --show-toplevel
                MagicMock(success=True),  # git config core.hooksPath .githooks
                MagicMock(success=True),  # test -x .githooks/pre-push
            ]

            result = await find_pre_push_hook(tmp_path)

        assert result == ".githooks/pre-push"
        config_call = mock_run.call_args_list[2]
        assert config_call[0] == ("git", "config", "core.hooksPath", ".githooks")

    def test_truncate_hook_output_under_limit(self) -> None:
        from sova.core.steps.validate import _truncate_hook_output

        short = "x" * 8000
        assert _truncate_hook_output(short) == short

    def test_truncate_hook_output_over_limit(self) -> None:
        from sova.core.steps.validate import _truncate_hook_output

        output = "A" * 4000 + "B" * 5000
        result = _truncate_hook_output(output)
        assert result.startswith("...(truncated)")
        assert result.endswith("B" * 5000)
        assert len(result.split("\n", 1)[1]) == 8000

    def test_truncate_hook_output_empty(self) -> None:
        from sova.core.steps.validate import _truncate_hook_output

        assert _truncate_hook_output("") == ""

    async def test_llm_prompt_uses_truncated_output(self, tmp_path: Path) -> None:
        from sova.core.steps.validate import _HOOK_OUTPUT_LIMIT, ValidateStep
        from sova.llm.models import LLMResult

        ctx = _make_ctx(tmp_path, worktree_dir=tmp_path / "worktree")
        step = ValidateStep()
        large_output = "HEADER\n" * 2000 + "ACTUAL ERROR"

        with (
            patch("sova.core.steps.validate.run") as mock_run,
            patch("sova.core.steps.validate.dispatch_prompt", new_callable=AsyncMock) as mock_invoke,
        ):
            mock_run.side_effect = [
                MagicMock(success=True, stdout=".githooks\n"),
                MagicMock(success=True, stdout=""),
                MagicMock(success=False, stdout=large_output, stderr="", timed_out=False),
                MagicMock(success=True, stdout="abc123\n"),  # pre-dispatch HEAD
                MagicMock(success=True, stdout="abc123\n"),  # post-dispatch HEAD (unchanged)
                MagicMock(success=True, stdout="ok\n", stderr="", timed_out=False),
                MagicMock(success=True, stdout=""),  # git status --porcelain (worktree clean)
            ]
            mock_invoke.return_value = LLMResult(
                text="Fixed",
                model="sonnet",
                cost_usd=Decimal("0.01"),
            )
            await step.execute(ctx)

        prompt = mock_invoke.call_args[0][1]
        assert "ACTUAL ERROR" in prompt
        assert "truncated" in prompt
        code_block = prompt.split("```\n")[1].split("\n```")[0]
        assert len(code_block) <= _HOOK_OUTPUT_LIMIT + 100

    async def test_retry_path_also_truncates(self, tmp_path: Path) -> None:
        from sova.core.steps.validate import _HOOK_OUTPUT_LIMIT, ValidateStep
        from sova.llm.models import LLMResult

        ctx = _make_ctx(tmp_path, worktree_dir=tmp_path / "worktree")
        step = ValidateStep()
        large_retry_output = "Z" * 10000 + "RETRY_ERROR"

        with (
            patch("sova.core.steps.validate.run") as mock_run,
            patch("sova.core.steps.validate.dispatch_prompt", new_callable=AsyncMock) as mock_invoke,
        ):
            mock_run.side_effect = [
                MagicMock(success=True, stdout=".githooks\n"),
                MagicMock(success=True, stdout=""),
                MagicMock(success=False, stdout="short error", stderr="", timed_out=False),
                MagicMock(success=True, stdout="abc123\n"),  # pre-dispatch HEAD (attempt 1)
                MagicMock(success=True, stdout="abc123\n"),  # post-dispatch HEAD (attempt 1, unchanged)
                MagicMock(success=False, stdout=large_retry_output, stderr="", timed_out=False),
                MagicMock(success=True, stdout="abc123\n"),  # pre-dispatch HEAD (attempt 2)
                MagicMock(success=True, stdout="abc123\n"),  # post-dispatch HEAD (attempt 2, unchanged)
                MagicMock(success=False, stdout=large_retry_output, stderr="", timed_out=False),
            ]
            mock_invoke.return_value = LLMResult(
                text="Attempted",
                model="sonnet",
                cost_usd=Decimal("0.01"),
            )
            await step.execute(ctx)

        second_call_prompt = mock_invoke.call_args_list[1][0][1]
        assert "RETRY_ERROR" in second_call_prompt
        assert "truncated" in second_call_prompt
        code_block = second_call_prompt.split("```\n")[1].split("\n```")[0]
        assert len(code_block) <= _HOOK_OUTPUT_LIMIT + 100


class TestPushStep:
    async def test_gate_check_requires_commits_ahead(self, tmp_path: Path) -> None:
        from sova.core.steps.push import PushStep

        ctx = _make_ctx(tmp_path, branch_name="feat/test", worktree_dir=tmp_path / "worktree")
        step = PushStep()

        with patch("sova.core.steps.push.run") as mock_run:
            mock_run.return_value = MagicMock(success=True, stdout="0\n")
            gate = await step.validate_output(ctx)

        assert not gate.passed


class TestRearrangeCommitsStep:
    async def test_execute_invokes_rearrange_command(self, tmp_path: Path) -> None:
        from sova.core.steps.rearrange_commits import RearrangeCommitsStep

        ctx = _make_ctx(tmp_path, worktree_dir=tmp_path / "worktree")
        step = RearrangeCommitsStep()

        with (
            patch("sova.core.steps.rearrange_commits.dispatch_command", new_callable=AsyncMock) as mock_invoke,
            self._known_position(),
        ):
            mock_invoke.return_value = MagicMock(cost_usd=Decimal("0.02"), text="Commits reorganized")
            result = await step.execute(ctx)

        assert result.success
        assert "reorganized" in result.summary
        mock_invoke.assert_awaited_once_with(
            step,
            "/rearrange-commits",
            model=ctx.config.agent.model,
            fallback_model=None,
            task_type="rearrange_commits",
            cwd=ctx.working_dir,
            max_budget_usd=ANY,
            timeout=ANY,
            ctx=ctx,
        )

    async def test_execute_returns_failure_on_error(self, tmp_path: Path) -> None:
        from sova.core.steps.rearrange_commits import RearrangeCommitsStep

        ctx = _make_ctx(tmp_path, worktree_dir=tmp_path / "worktree")
        step = RearrangeCommitsStep()

        with (
            patch("sova.core.steps.rearrange_commits.dispatch_command", new_callable=AsyncMock) as mock_invoke,
            self._known_position(),
        ):
            mock_invoke.side_effect = RuntimeError("command not found")
            result = await step.execute(ctx)

        assert not result.success
        assert "failed" in result.summary

    @staticmethod
    def _feature_repo(repo: Path) -> Callable[..., str]:
        """A repo on `feature`, one commit ahead of `main`, with an uncommitted edit and a new file."""
        import subprocess

        def git(*args: str) -> str:
            return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()

        repo.mkdir()
        git("init", "-q")
        git("config", "user.email", "t@example.com")
        git("config", "user.name", "t")
        git("config", "commit.gpgsign", "false")
        (repo / "src.py").write_text("x = 1\n", encoding="utf-8")
        git("add", "-A")
        git("commit", "-qm", "base")
        git("branch", "-M", "main")
        git("checkout", "-qb", "feature")
        (repo / "src.py").write_text("x = 2\n", encoding="utf-8")
        git("commit", "-qam", "feat(core): real work")
        (repo / "src.py").write_text("x = 3  # review fix\n", encoding="utf-8")
        (repo / "new.py").write_text("y = 1\n", encoding="utf-8")
        return git

    @staticmethod
    def _known_position() -> Any:
        """Stand in for a worktree that does not exist on disk: a known position and no operation running."""
        from contextlib import ExitStack

        from sova.core.steps.rearrange_commits import RearrangeCommitsStep

        stack = ExitStack()
        stack.enter_context(
            patch.object(
                RearrangeCommitsStep, "_position", new_callable=AsyncMock, return_value=("abc123", "refs/heads/f")
            )
        )
        stack.enter_context(
            patch.object(RearrangeCommitsStep, "_operations_in_progress", new_callable=AsyncMock, return_value=[])
        )
        return stack

    @staticmethod
    def _recovery_ref(git: Callable[..., str], kind: str) -> str:
        """The one `refs/sova/rearrange/<tag>/<kind>` ref a restore wrote."""
        refs = git("for-each-ref", "--format=%(refname)", "refs/sova/rearrange/").split()
        matching = [ref for ref in refs if ref.endswith(f"/{kind}")]
        assert len(matching) == 1, refs
        return matching[0]

    @staticmethod
    def _snapshot_and_reset(git: Callable[..., str]) -> None:
        """What /rearrange-commits does before recommitting: WIP snapshot, backup branch, reset to the fork point."""
        git("add", "-A")
        git("commit", "-qm", "chore: WIP snapshot before rearrange")
        git("branch", "backup/rearrange-test")
        git("reset", "--soft", git("merge-base", "HEAD", "main"))
        git("reset", "-q", "HEAD", ".")

    async def test_failed_agent_after_reset_restores_branch_and_keeps_files(self, tmp_path: Path) -> None:
        """An agent that dies between the command's WIP snapshot and its recommits must not
        leave the branch on the base commit: every later address-pr run then sees a branch
        that diverged from its remote and refuses to push (PR #1539, run 1813)."""
        from sova.core.steps.rearrange_commits import RearrangeCommitsStep

        repo = tmp_path / "worktree"
        git = self._feature_repo(repo)
        start_head = git("rev-parse", "HEAD")

        async def snapshot_reset_then_die(*_args: object, **_kwargs: object) -> None:
            self._snapshot_and_reset(git)
            raise RuntimeError("API Error: Can't reach the API server (ENOTFOUND)")

        ctx = _make_ctx(tmp_path / "project", worktree_dir=repo)
        with patch("sova.core.steps.rearrange_commits.dispatch_command", side_effect=snapshot_reset_then_die):
            result = await RearrangeCommitsStep().execute(ctx)

        assert not result.success
        assert "restored" in result.summary
        assert git("rev-parse", "HEAD") == start_head
        assert (repo / "src.py").read_text(encoding="utf-8") == "x = 3  # review fix\n"
        assert (repo / "new.py").read_text(encoding="utf-8") == "y = 1\n"
        assert git("branch", "--list", "backup/rearrange-test")

    async def test_cancelled_step_after_reset_restores_branch_before_reraising(self, tmp_path: Path) -> None:
        """The engine's hard timeout cancels the step; the branch must be restored before its partial-work commit."""
        import asyncio

        from sova.core.steps.rearrange_commits import RearrangeCommitsStep

        repo = tmp_path / "worktree"
        git = self._feature_repo(repo)
        start_head = git("rev-parse", "HEAD")

        async def snapshot_reset_then_cancelled(*_args: object, **_kwargs: object) -> None:
            self._snapshot_and_reset(git)
            raise asyncio.CancelledError

        ctx = _make_ctx(tmp_path / "project", worktree_dir=repo)
        with (
            patch("sova.core.steps.rearrange_commits.dispatch_command", side_effect=snapshot_reset_then_cancelled),
            pytest.raises(asyncio.CancelledError),
        ):
            await RearrangeCommitsStep().execute(ctx)

        assert git("rev-parse", "HEAD") == start_head
        assert (repo / "src.py").read_text(encoding="utf-8") == "x = 3  # review fix\n"

    async def test_agent_exiting_cleanly_after_reset_fails_and_restores(self, tmp_path: Path) -> None:
        from sova.core.steps.rearrange_commits import RearrangeCommitsStep

        repo = tmp_path / "worktree"
        git = self._feature_repo(repo)
        start_head = git("rev-parse", "HEAD")

        async def snapshot_reset_then_stop(*_args: object, **_kwargs: object) -> MagicMock:
            self._snapshot_and_reset(git)
            return MagicMock(cost_usd=Decimal("0.01"))

        ctx = _make_ctx(tmp_path / "project", worktree_dir=repo)
        with patch("sova.core.steps.rearrange_commits.dispatch_command", side_effect=snapshot_reset_then_stop):
            result = await RearrangeCommitsStep().execute(ctx)

        assert not result.success
        assert "incomplete" in result.summary
        assert "restored" in result.summary
        assert "No commits ahead of base" in (result.error or "")
        assert git("rev-parse", "HEAD") == start_head

    async def test_completed_rearrange_keeps_the_new_history(self, tmp_path: Path) -> None:
        from sova.core.steps.rearrange_commits import RearrangeCommitsStep

        repo = tmp_path / "worktree"
        git = self._feature_repo(repo)
        start_head = git("rev-parse", "HEAD")

        async def rearrange(*_args: object, **_kwargs: object) -> MagicMock:
            self._snapshot_and_reset(git)
            git("add", "src.py")
            git("commit", "-qm", "feat(core): real work with the review fix")
            git("add", "new.py")
            git("commit", "-qm", "feat(core): add new module")
            return MagicMock(cost_usd=Decimal("0.01"))

        ctx = _make_ctx(tmp_path / "project", worktree_dir=repo)
        with patch("sova.core.steps.rearrange_commits.dispatch_command", side_effect=rearrange):
            result = await RearrangeCommitsStep().execute(ctx)

        assert result.success, result.error
        assert git("rev-parse", "HEAD") != start_head
        assert git("rev-list", "--count", "main..HEAD") == "2"

    async def test_failed_agent_without_moving_head_leaves_branch_alone(self, tmp_path: Path) -> None:
        from sova.core.steps.rearrange_commits import RearrangeCommitsStep

        repo = tmp_path / "worktree"
        git = self._feature_repo(repo)
        start_head = git("rev-parse", "HEAD")

        ctx = _make_ctx(tmp_path / "project", worktree_dir=repo)
        with patch("sova.core.steps.rearrange_commits.dispatch_command", side_effect=RuntimeError("exit 1")):
            result = await RearrangeCommitsStep().execute(ctx)

        assert not result.success
        assert result.summary == "Commit reorganization failed"
        assert git("rev-parse", "HEAD") == start_head
        assert git("status", "--porcelain") == "M src.py\n?? new.py"

    async def test_failed_restore_names_the_worktree_branch_and_commit_to_reset_to(self, tmp_path: Path) -> None:
        from sova.core.steps.rearrange_commits import RearrangeCommitsStep

        worktree = tmp_path / "worktree"
        ctx = _make_ctx(tmp_path, worktree_dir=worktree)
        with (
            patch("sova.core.steps.rearrange_commits.dispatch_command", side_effect=RuntimeError("exit 1")),
            # (HEAD, ref) before dispatch, then after the agent moved HEAD.
            patch.object(
                RearrangeCommitsStep,
                "_position",
                new_callable=AsyncMock,
                side_effect=[("aaa111", "refs/heads/feature"), ("bbb222", "refs/heads/feature")],
            ),
            patch("sova.core.steps.rearrange_commits.run", new_callable=AsyncMock) as mock_run,
        ):
            mock_run.return_value = MagicMock(success=False, stdout="", stderr="index.lock exists")
            result = await RearrangeCommitsStep().execute(ctx)

        assert not result.success
        # Re-attaches the branch before resetting, so it cannot move whatever HEAD happens to point at.
        git = f"git -C {shlex.quote(str(worktree))}"
        assert (
            f"{git} update-ref refs/heads/feature aaa111 && {git} symbolic-ref HEAD refs/heads/feature "
            f"&& {git} reset -q aaa111"
        ) in result.summary

    async def test_agent_that_dies_on_the_backup_branch_leaves_both_branches_correct(self, tmp_path: Path) -> None:
        """Dying after checking out the backup branch must not reset the backup branch onto the feature commit."""
        from sova.core.steps.rearrange_commits import RearrangeCommitsStep

        repo = tmp_path / "worktree"
        git = self._feature_repo(repo)
        start_head = git("rev-parse", "HEAD")

        async def snapshot_checkout_backup_then_die(*_args: object, **_kwargs: object) -> None:
            self._snapshot_and_reset(git)
            git("checkout", "-qf", "backup/rearrange-test")
            raise RuntimeError("API Error: Can't reach the API server (ENOTFOUND)")

        ctx = _make_ctx(tmp_path / "project", worktree_dir=repo)
        with patch("sova.core.steps.rearrange_commits.dispatch_command", side_effect=snapshot_checkout_backup_then_die):
            result = await RearrangeCommitsStep().execute(ctx)

        assert not result.success
        assert "restored" in result.summary
        assert git("symbolic-ref", "HEAD") == "refs/heads/feature"
        assert git("rev-parse", "feature") == start_head
        assert git("log", "-1", "--format=%s", "backup/rearrange-test") == "chore: WIP snapshot before rearrange"
        assert (repo / "src.py").read_text(encoding="utf-8") == "x = 3  # review fix\n"
        assert (repo / "new.py").read_text(encoding="utf-8") == "y = 1\n"

    async def test_cancelled_agent_on_the_backup_branch_is_restored_before_reraising(self, tmp_path: Path) -> None:
        import asyncio

        from sova.core.steps.rearrange_commits import RearrangeCommitsStep

        repo = tmp_path / "worktree"
        git = self._feature_repo(repo)
        start_head = git("rev-parse", "HEAD")

        async def snapshot_checkout_backup_then_cancelled(*_args: object, **_kwargs: object) -> None:
            self._snapshot_and_reset(git)
            git("checkout", "-qf", "backup/rearrange-test")
            raise asyncio.CancelledError

        ctx = _make_ctx(tmp_path / "project", worktree_dir=repo)
        with (
            patch(
                "sova.core.steps.rearrange_commits.dispatch_command",
                side_effect=snapshot_checkout_backup_then_cancelled,
            ),
            pytest.raises(asyncio.CancelledError),
        ):
            await RearrangeCommitsStep().execute(ctx)

        assert git("symbolic-ref", "HEAD") == "refs/heads/feature"
        assert git("rev-parse", "feature") == start_head
        assert git("log", "-1", "--format=%s", "backup/rearrange-test") == "chore: WIP snapshot before rearrange"

    async def test_agent_that_dies_mid_rebase_has_it_aborted_and_the_branch_restored(self, tmp_path: Path) -> None:
        import os
        import subprocess

        from sova.core.steps.rearrange_commits import RearrangeCommitsStep

        repo = tmp_path / "worktree"
        git = self._feature_repo(repo)
        start_head = git("rev-parse", "HEAD")

        async def snapshot_start_rebase_then_die(*_args: object, **_kwargs: object) -> None:
            git("add", "-A")
            git("commit", "-qm", "chore: WIP snapshot before rearrange")
            git("branch", "backup/rearrange-test")
            # Stop the interactive rebase on its first commit: a detached HEAD with a rebase left running.
            env = {**os.environ, "GIT_SEQUENCE_EDITOR": "sed -i.bak 's/^pick/edit/'"}
            subprocess.run(
                ["git", "rebase", "-i", "main"], cwd=repo, check=True, capture_output=True, text=True, env=env
            )
            # An edit made while the rebase is stopped, which `rebase --abort` resets away.
            (repo / "src.py").write_text("x = 99  # mid-rebase edit\n", encoding="utf-8")
            raise RuntimeError("API Error: Can't reach the API server (ENOTFOUND)")

        ctx = _make_ctx(tmp_path / "project", worktree_dir=repo)
        with patch("sova.core.steps.rearrange_commits.dispatch_command", side_effect=snapshot_start_rebase_then_die):
            result = await RearrangeCommitsStep().execute(ctx)

        assert not result.success
        assert "restored" in result.summary
        assert not (repo / ".git" / "rebase-merge").exists()
        assert git("symbolic-ref", "HEAD") == "refs/heads/feature"
        assert git("rev-parse", "HEAD") == start_head
        assert git("log", "-1", "--format=%s", "backup/rearrange-test") == "chore: WIP snapshot before rearrange"
        assert (repo / "src.py").read_text(encoding="utf-8") == "x = 3  # review fix\n"
        # The mid-rebase edit survives the abort in the edits ref, and the shared stash stack is untouched.
        edits_ref = self._recovery_ref(git, "edits")
        assert git("show", f"{edits_ref}:src.py") == "x = 99  # mid-rebase edit"
        assert git("stash", "list") == ""
        assert edits_ref in result.summary

    async def test_a_cancel_during_the_output_checks_restores_a_reset_branch(self, tmp_path: Path) -> None:
        import asyncio

        from sova.core.steps.rearrange_commits import RearrangeCommitsStep

        repo = tmp_path / "worktree"
        git = self._feature_repo(repo)
        start_head = git("rev-parse", "HEAD")

        async def snapshot_reset_then_stop(*_args: object, **_kwargs: object) -> MagicMock:
            self._snapshot_and_reset(git)
            return MagicMock(cost_usd=Decimal("0.01"))

        ctx = _make_ctx(tmp_path / "project", worktree_dir=repo)
        with (
            patch("sova.core.steps.rearrange_commits.dispatch_command", side_effect=snapshot_reset_then_stop),
            patch.object(RearrangeCommitsStep, "validate_output", side_effect=asyncio.CancelledError),
            pytest.raises(asyncio.CancelledError),
        ):
            await RearrangeCommitsStep().execute(ctx)

        assert git("symbolic-ref", "HEAD") == "refs/heads/feature"
        assert git("rev-parse", "HEAD") == start_head
        assert (repo / "src.py").read_text(encoding="utf-8") == "x = 3  # review fix\n"

    async def test_a_cancel_during_the_output_checks_keeps_a_finished_rearrangement(self, tmp_path: Path) -> None:
        import asyncio

        from sova.core.steps.rearrange_commits import RearrangeCommitsStep

        repo = tmp_path / "worktree"
        git = self._feature_repo(repo)
        start_head = git("rev-parse", "HEAD")

        async def rearrange(*_args: object, **_kwargs: object) -> MagicMock:
            self._snapshot_and_reset(git)
            git("add", "src.py")
            git("commit", "-qm", "feat(core): real work with the review fix")
            git("add", "new.py")
            git("commit", "-qm", "feat(core): add new module")
            return MagicMock(cost_usd=Decimal("0.01"))

        ctx = _make_ctx(tmp_path / "project", worktree_dir=repo)
        with (
            patch("sova.core.steps.rearrange_commits.dispatch_command", side_effect=rearrange),
            patch.object(RearrangeCommitsStep, "validate_output", side_effect=asyncio.CancelledError),
            pytest.raises(asyncio.CancelledError),
        ):
            await RearrangeCommitsStep().execute(ctx)

        assert git("rev-parse", "HEAD") != start_head
        assert git("rev-list", "--count", "main..HEAD") == "2"

    async def test_a_failed_abort_puts_the_matching_abort_first_in_the_recovery_command(self, tmp_path: Path) -> None:
        from sova.core.steps.rearrange_commits import RearrangeCommitsStep

        worktree = tmp_path / "worktree"
        ctx = _make_ctx(tmp_path, worktree_dir=worktree)
        with (
            patch("sova.core.steps.rearrange_commits.dispatch_command", side_effect=RuntimeError("exit 1")),
            patch.object(
                RearrangeCommitsStep, "_position", new_callable=AsyncMock, return_value=("aaa111", "refs/heads/f")
            ),
            patch.object(
                RearrangeCommitsStep,
                "_abort_in_progress",
                new_callable=AsyncMock,
                return_value=(False, ("rebase", "--abort"), False),
            ),
            patch.object(RearrangeCommitsStep, "_operations_in_progress", new_callable=AsyncMock, return_value=[]),
        ):
            result = await RearrangeCommitsStep().execute(ctx)

        git = f"git -C {shlex.quote(str(worktree))}"
        assert f"restore it with: {git} rebase --abort && {git} update-ref" in result.summary

    async def test_a_restore_that_hits_an_unusable_worktree_fails_closed(self, tmp_path: Path) -> None:
        from sova.core.steps.rearrange_commits import RearrangeCommitsStep

        with patch("sova.core.steps.rearrange_commits.run", new_callable=AsyncMock, side_effect=OSError("gone")):
            restore = await RearrangeCommitsStep._restore_head(tmp_path, ("aaa111", "refs/heads/f"), "main")

        assert restore.restored is False
        assert "reset -q aaa111" in restore.recovery

    async def test_cancellation_with_a_failed_restore_blocks_the_engines_partial_work_commit(
        self, tmp_path: Path
    ) -> None:
        import asyncio

        from sova.core.steps.rearrange_commits import RearrangeCommitsStep

        repo = tmp_path / "worktree"
        git = self._feature_repo(repo)

        async def snapshot_reset_then_cancelled(*_args: object, **_kwargs: object) -> None:
            self._snapshot_and_reset(git)
            raise asyncio.CancelledError

        ctx = _make_ctx(tmp_path / "project", worktree_dir=repo)
        with (
            patch("sova.core.steps.rearrange_commits.dispatch_command", side_effect=snapshot_reset_then_cancelled),
            patch.object(
                RearrangeCommitsStep,
                "_restore_head_unguarded",
                new_callable=AsyncMock,
                side_effect=OSError("index.lock"),
            ),
            pytest.raises(asyncio.CancelledError),
        ):
            await RearrangeCommitsStep().execute(ctx)

        assert "restore it with: git -C" in ctx.partial_work_blocked

    async def test_a_second_cancel_does_not_abandon_the_restore(self, tmp_path: Path) -> None:
        import asyncio

        from sova.core.steps.rearrange_commits import RearrangeCommitsStep, _Restore

        started = asyncio.Event()
        release = asyncio.Event()
        finished = asyncio.Event()

        async def blocked_restore(*_args: object) -> _Restore:
            started.set()
            await release.wait()
            finished.set()
            return _Restore(True)

        with patch.object(RearrangeCommitsStep, "_restore_head", side_effect=blocked_restore):
            task = asyncio.ensure_future(RearrangeCommitsStep._restore_uninterrupted(tmp_path, ("a", None), "main"))
            await started.wait()
            # The restore cannot finish until released, so each cancel is delivered while it is still running:
            # one yield lets the waiting helper take the cancel and loop back to wait for the restore.
            for _ in range(2):
                task.cancel()
                await asyncio.sleep(0)
                assert not task.done()
                assert not finished.is_set()
            release.set()
            restore = await task

        assert finished.is_set()
        assert restore.restored is True

    async def test_restore_uninterrupted_reports_a_restore_task_that_raised(self, tmp_path: Path) -> None:
        from sova.core.steps.rearrange_commits import RearrangeCommitsStep

        with patch.object(RearrangeCommitsStep, "_restore_head", new_callable=AsyncMock, side_effect=ValueError("x")):
            restore = await RearrangeCommitsStep._restore_uninterrupted(tmp_path, ("a" * 40, "refs/heads/f"), "main")

        assert restore.restored is False
        assert "reset -q" in restore.recovery

    async def test_save_attempt_tolerates_a_failed_ref_write(self, tmp_path: Path) -> None:
        from sova.core.steps.rearrange_commits import RearrangeCommitsStep

        failed = MagicMock(success=False, stderr="cannot lock ref")
        with patch("sova.core.steps.rearrange_commits.run", new_callable=AsyncMock, return_value=failed) as fake_run:
            saved = await RearrangeCommitsStep._save_attempt(tmp_path, "abc123", "refs/sova/rearrange/x/attempt")

        assert saved is False
        fake_run.assert_awaited_once()

    def test_restore_steps_only_resets_when_the_ref_is_unchanged(self) -> None:
        from sova.core.steps.rearrange_commits import RearrangeCommitsStep

        steps = RearrangeCommitsStep._restore_steps(("abc", "refs/heads/f"), "refs/heads/f")

        assert steps == [("reset", "-q", "abc")]

    def test_restore_steps_reattaches_the_recorded_branch(self) -> None:
        from sova.core.steps.rearrange_commits import RearrangeCommitsStep

        steps = RearrangeCommitsStep._restore_steps(("abc", "refs/heads/f"), "refs/heads/backup")

        assert steps == [
            ("update-ref", "refs/heads/f", "abc"),
            ("symbolic-ref", "HEAD", "refs/heads/f"),
            ("reset", "-q", "abc"),
        ]

    def test_restore_steps_reattaches_a_detached_start(self) -> None:
        from sova.core.steps.rearrange_commits import RearrangeCommitsStep

        steps = RearrangeCommitsStep._restore_steps(("abc", None), "refs/heads/backup")

        assert steps == [("update-ref", "--no-deref", "HEAD", "abc"), ("reset", "-q", "abc")]

    async def test_a_failing_restore_command_is_a_failed_restore(self, tmp_path: Path) -> None:
        from sova.core.steps import rearrange_commits
        from sova.core.steps.rearrange_commits import RearrangeCommitsStep

        repo = tmp_path / "worktree"
        git = self._feature_repo(repo)
        start = (git("rev-parse", "HEAD"), "refs/heads/feature")
        git("reset", "-q", "--soft", "main")

        failed = MagicMock(success=False, stderr="fatal: cannot reset")
        real_run = rearrange_commits.run

        async def run_but_fail_reset(*args: object, **kwargs: object) -> object:
            if args[:2] == ("git", "reset"):
                return failed
            return await real_run(*args, **kwargs)

        with patch("sova.core.steps.rearrange_commits.run", side_effect=run_but_fail_reset):
            restore = await RearrangeCommitsStep._restore_head(repo, start, "main")

        assert restore.restored is False
        assert "reset -q" in restore.recovery

    async def test_an_agent_that_finished_rearranging_before_failing_keeps_its_history(self, tmp_path: Path) -> None:
        """A late non-zero exit (budget cap, API error) must not throw away commits that are already right."""
        from sova.core.steps.rearrange_commits import RearrangeCommitsStep

        repo = tmp_path / "worktree"
        git = self._feature_repo(repo)
        start_head = git("rev-parse", "HEAD")

        async def rearrange_then_die(*_args: object, **_kwargs: object) -> None:
            self._snapshot_and_reset(git)
            git("add", "-A")
            git("commit", "-qm", "feat(core): real work with the review fix")
            raise RuntimeError("budget exceeded")

        ctx = _make_ctx(tmp_path / "project", worktree_dir=repo)
        with patch("sova.core.steps.rearrange_commits.dispatch_command", side_effect=rearrange_then_die):
            result = await RearrangeCommitsStep().execute(ctx)

        assert not result.success
        assert git("rev-parse", "HEAD") != start_head
        assert git("rev-list", "--count", "main..HEAD") == "1"

    async def test_a_restore_saves_the_agents_head_to_a_ref_first(self, tmp_path: Path) -> None:
        from sova.core.steps.rearrange_commits import RearrangeCommitsStep

        repo = tmp_path / "worktree"
        git = self._feature_repo(repo)
        wip_head = ""

        async def snapshot_reset_then_die(*_args: object, **_kwargs: object) -> None:
            nonlocal wip_head
            self._snapshot_and_reset(git)
            wip_head = git("rev-parse", "HEAD")
            raise RuntimeError("exit 1")

        ctx = _make_ctx(tmp_path / "project", worktree_dir=repo)
        with patch("sova.core.steps.rearrange_commits.dispatch_command", side_effect=snapshot_reset_then_die):
            await RearrangeCommitsStep().execute(ctx)

        assert git("rev-parse", self._recovery_ref(git, "attempt")) == wip_head

    async def test_a_revert_the_agent_left_running_is_aborted(self, tmp_path: Path) -> None:
        from sova.core.steps.rearrange_commits import RearrangeCommitsStep

        repo = tmp_path / "worktree"
        git = self._feature_repo(repo)
        git("add", "-A")
        git("commit", "-qm", "chore: clean tree")
        (repo / "src.py").write_text("x = 9\n", encoding="utf-8")
        git("commit", "-qam", "feat(core): conflicting follow-up")
        import subprocess

        reverted = subprocess.run(
            ["git", "revert", "--no-edit", "HEAD~1"], cwd=repo, capture_output=True, text=True, check=False
        )
        assert reverted.returncode != 0
        assert (repo / ".git" / "REVERT_HEAD").exists()

        aborted, failed, edits_unsaved = await RearrangeCommitsStep._abort_in_progress(repo)

        assert (aborted, failed, edits_unsaved) == (True, None, False)
        assert not (repo / ".git" / "REVERT_HEAD").exists()

    async def test_an_unreadable_start_position_stops_the_step_before_dispatch(self, tmp_path: Path) -> None:
        from sova.core.steps.rearrange_commits import RearrangeCommitsStep

        ctx = _make_ctx(tmp_path, worktree_dir=tmp_path / "worktree")
        with (
            patch("sova.core.steps.rearrange_commits.dispatch_command", new_callable=AsyncMock) as dispatch,
            patch.object(RearrangeCommitsStep, "_position", new_callable=AsyncMock, return_value=None),
        ):
            result = await RearrangeCommitsStep().execute(ctx)

        assert not result.success
        assert "nothing was changed" in result.summary
        dispatch.assert_not_called()

    async def test_a_symbolic_ref_failure_is_not_recorded_as_a_detached_head(self, tmp_path: Path) -> None:
        from sova.core.steps.rearrange_commits import RearrangeCommitsStep

        async def fake_run(*args: object, **_kwargs: object) -> MagicMock:
            if args[:2] == ("git", "symbolic-ref"):
                return MagicMock(success=False, returncode=128, stdout="", stderr="timed out")
            return MagicMock(success=True, returncode=0, stdout="abc123\n", stderr="")

        with patch("sova.core.steps.rearrange_commits.run", side_effect=fake_run):
            assert await RearrangeCommitsStep._position(tmp_path) is None

    async def test_a_detached_head_is_a_known_position(self, tmp_path: Path) -> None:
        from sova.core.steps.rearrange_commits import RearrangeCommitsStep

        repo = tmp_path / "worktree"
        git = self._feature_repo(repo)
        git("checkout", "-q", "--detach")

        assert await RearrangeCommitsStep._position(repo) == (git("rev-parse", "HEAD"), None)

    async def test_an_operation_already_running_is_left_alone_and_stops_the_step(self, tmp_path: Path) -> None:
        import subprocess

        from sova.core.steps.rearrange_commits import RearrangeCommitsStep

        repo = tmp_path / "worktree"
        git = self._feature_repo(repo)
        git("add", "-A")
        git("commit", "-qm", "chore: clean tree")
        (repo / "src.py").write_text("x = 9\n", encoding="utf-8")
        git("commit", "-qam", "feat(core): conflicting follow-up")
        subprocess.run(["git", "revert", "--no-edit", "HEAD~1"], cwd=repo, capture_output=True, check=False)
        assert (repo / ".git" / "REVERT_HEAD").exists()

        ctx = _make_ctx(tmp_path / "project", worktree_dir=repo)
        with patch("sova.core.steps.rearrange_commits.dispatch_command", new_callable=AsyncMock) as dispatch:
            result = await RearrangeCommitsStep().execute(ctx)

        assert not result.success
        assert "already in progress" in result.summary
        assert "REVERT_HEAD" in result.summary
        dispatch.assert_not_called()
        assert (repo / ".git" / "REVERT_HEAD").exists()

    async def test_a_conflicted_rebase_has_its_edits_saved_before_the_abort(self, tmp_path: Path) -> None:
        """`git stash create` refuses an index with unresolved conflicts; the edits must still be saved."""
        import os
        import subprocess

        from sova.core.steps.rearrange_commits import RearrangeCommitsStep

        repo = tmp_path / "worktree"
        git = self._feature_repo(repo)
        git("add", "-A")
        git("commit", "-qm", "chore: clean tree")
        git("checkout", "-q", "main")
        (repo / "src.py").write_text("x = 100  # conflicting main change\n", encoding="utf-8")
        git("commit", "-qam", "feat(core): main moves")
        git("checkout", "-q", "feature")
        (repo / "other.py").write_text("z = 1\n", encoding="utf-8")
        git("add", "other.py")
        git("commit", "-qm", "feat(core): add other")

        async def start_conflicting_rebase_then_die(*_args: object, **_kwargs: object) -> None:
            env = {**os.environ, "GIT_SEQUENCE_EDITOR": "true"}
            stopped = subprocess.run(
                ["git", "rebase", "-i", "main"], cwd=repo, capture_output=True, text=True, env=env, check=False
            )
            assert stopped.returncode != 0
            assert git("ls-files", "--unmerged")
            (repo / "src.py").write_text("x = 42  # resolved by hand, never staged\n", encoding="utf-8")
            raise RuntimeError("API Error: Can't reach the API server (ENOTFOUND)")

        start_head = git("rev-parse", "HEAD")

        ctx = _make_ctx(tmp_path / "project", worktree_dir=repo)
        with patch("sova.core.steps.rearrange_commits.dispatch_command", side_effect=start_conflicting_rebase_then_die):
            result = await RearrangeCommitsStep().execute(ctx)

        assert not result.success
        assert "restored" in result.summary
        assert git("rev-parse", "HEAD") == start_head
        assert not (repo / ".git" / "rebase-merge").exists()
        edits_ref = self._recovery_ref(git, "edits")
        assert git("show", f"{edits_ref}:src.py") == "x = 42  # resolved by hand, never staged"

    async def test_an_abort_is_refused_when_the_edits_could_not_be_saved(self, tmp_path: Path) -> None:
        import os
        import subprocess

        from sova.core.steps import rearrange_commits
        from sova.core.steps.rearrange_commits import RearrangeCommitsStep

        repo = tmp_path / "worktree"
        git = self._feature_repo(repo)
        git("add", "-A")
        git("commit", "-qm", "chore: clean tree")

        async def start_rebase_then_die(*_args: object, **_kwargs: object) -> None:
            env = {**os.environ, "GIT_SEQUENCE_EDITOR": "sed -i.bak 's/^pick/edit/'"}
            subprocess.run(["git", "rebase", "-i", "main"], cwd=repo, check=True, capture_output=True, env=env)
            (repo / "src.py").write_text("x = 99  # mid-rebase edit\n", encoding="utf-8")
            raise RuntimeError("exit 1")

        real_run = rearrange_commits.run

        async def run_but_fail_the_edits_ref(*args: object, **kwargs: object) -> object:
            if args[:2] == ("git", "update-ref") and "/edits" in str(args[2]):
                return MagicMock(success=False, stdout="", stderr="cannot lock ref")
            return await real_run(*args, **kwargs)

        ctx = _make_ctx(tmp_path / "project", worktree_dir=repo)
        with (
            patch("sova.core.steps.rearrange_commits.dispatch_command", side_effect=start_rebase_then_die),
            patch("sova.core.steps.rearrange_commits.run", side_effect=run_but_fail_the_edits_ref),
        ):
            result = await RearrangeCommitsStep().execute(ctx)

        assert not result.success
        assert "first copy your tracked-file edits" in result.summary
        assert (repo / ".git" / "rebase-merge").exists()
        assert (repo / "src.py").read_text(encoding="utf-8") == "x = 99  # mid-rebase edit\n"

    def test_the_recovery_command_passes_the_ref_and_path_to_git_literally(self) -> None:
        from sova.core.steps.rearrange_commits import RearrangeCommitsStep

        command = RearrangeCommitsStep._recovery_command(
            Path("/tmp/work tree"), ("abc123", "refs/heads/fix;id;$HOME"), ("rebase", "--abort")
        )

        tokens = shlex.split(command)
        assert "/tmp/work tree" in tokens
        assert tokens.count("refs/heads/fix;id;$HOME") == 2  # update-ref and symbolic-ref
        assert tokens[:6] == ["git", "-C", "/tmp/work tree", "rebase", "--abort", "&&"]

    def test_the_recovery_command_for_a_detached_start_does_not_name_a_branch(self) -> None:
        from sova.core.steps.rearrange_commits import RearrangeCommitsStep

        command = RearrangeCommitsStep._recovery_command(Path("/tmp/a b"), ("abc123", None))

        assert shlex.split(command) == [
            *["git", "-C", "/tmp/a b", "update-ref", "--no-deref", "HEAD", "abc123", "&&"],
            *["git", "-C", "/tmp/a b", "reset", "-q", "abc123"],
        ]

    def test_recovery_refs_differ_per_worktree_and_per_attempt(self) -> None:
        from sova.core.steps.rearrange_commits import RearrangeCommitsStep

        first = RearrangeCommitsStep._recovery_refs(Path("/a/one"), "a" * 40)
        other_worktree = RearrangeCommitsStep._recovery_refs(Path("/a/two"), "a" * 40)
        again = RearrangeCommitsStep._recovery_refs(Path("/a/one"), "a" * 40)

        assert first[0] != other_worktree[0]
        assert first[1] != other_worktree[1]
        assert first != again
        assert first[0].endswith("/attempt") and first[1].endswith("/edits")

    async def test_validate_passes_when_commits_ahead_and_clean(self, tmp_path: Path) -> None:
        from sova.core.steps.rearrange_commits import RearrangeCommitsStep

        ctx = _make_ctx(tmp_path, worktree_dir=tmp_path / "worktree")
        step = RearrangeCommitsStep()

        with patch("sova.core.steps.rearrange_commits.run", new_callable=AsyncMock) as mock_run:
            mock_run.side_effect = [
                MagicMock(success=True, stdout="abc123 feat(core): something\n"),  # log
                MagicMock(success=True, stdout=""),  # diff --stat (clean)
                MagicMock(success=True, stdout=""),  # staged (clean)
                MagicMock(success=True, stdout=""),  # status --porcelain (no untracked)
            ]
            gate = await step.validate_output(ctx)

        assert gate.passed

    async def test_validate_fails_when_untracked_files_remain(self, tmp_path: Path) -> None:
        from sova.core.steps.rearrange_commits import RearrangeCommitsStep

        ctx = _make_ctx(tmp_path, worktree_dir=tmp_path / "worktree")
        step = RearrangeCommitsStep()

        with patch("sova.core.steps.rearrange_commits.run", new_callable=AsyncMock) as mock_run:
            mock_run.side_effect = [
                MagicMock(success=True, stdout="abc123 feat(core): something\n"),  # log
                MagicMock(success=True, stdout=""),  # diff --stat (clean)
                MagicMock(success=True, stdout=""),  # staged (clean)
                MagicMock(success=True, stdout="?? new_file.py\n"),  # status --porcelain (untracked)
            ]
            gate = await step.validate_output(ctx)

        assert not gate.passed
        assert "Untracked" in gate.reason

    async def test_validate_excludes_agent_infrastructure_pathspecs(self, tmp_path: Path) -> None:
        """The diff checks must exclude .claude/ and .sova/ entirely.

        ensure_claude_artifacts() mirrors .claude/{commands,rules,skills} from the
        primary checkout into every worktree on every run, so churn there is
        inherited state rather than the agent's output. Counting it paused every
        address-review run whenever the primary checkout was dirty (issue #1090).
        """
        from sova.core.steps.rearrange_commits import RearrangeCommitsStep

        ctx = _make_ctx(tmp_path, worktree_dir=tmp_path / "worktree")
        step = RearrangeCommitsStep()

        with patch("sova.core.steps.rearrange_commits.run", new_callable=AsyncMock) as mock_run:
            mock_run.side_effect = [
                MagicMock(success=True, stdout="abc123 feat(core): something\n"),  # log
                MagicMock(success=True, stdout=""),  # diff --stat
                MagicMock(success=True, stdout=""),  # staged
                MagicMock(success=True, stdout=""),  # status --porcelain
            ]
            gate = await step.validate_output(ctx)

        assert gate.passed
        unstaged_argv = mock_run.await_args_list[1].args
        staged_argv = mock_run.await_args_list[2].args
        for argv in (unstaged_argv, staged_argv):
            assert ":(exclude).claude/" in argv
            assert ":(exclude).sova/" in argv

    async def test_validate_passes_with_only_inherited_claude_dirt(self, tmp_path: Path) -> None:
        """Against a real repo: modified tracked .claude/ files must not fail the gate."""
        import subprocess

        from sova.core.steps.rearrange_commits import RearrangeCommitsStep

        def git(*args: str) -> None:
            subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True, text=True)

        git("init", "-q")
        git("config", "user.email", "t@example.com")
        git("config", "user.name", "t")
        (tmp_path / "src.py").write_text("x = 1\n")
        (tmp_path / ".claude" / "commands").mkdir(parents=True)
        (tmp_path / ".claude" / "commands" / "develop.md").write_text("original\n")
        git("add", "-A")
        git("commit", "-qm", "base")
        git("branch", "-M", "main")
        git("checkout", "-qb", "feature")
        (tmp_path / "src.py").write_text("x = 2\n")
        git("commit", "-qam", "feat(core): real work")

        # Inherited dirt only: a tracked .claude/ file mirrored from the primary checkout.
        (tmp_path / ".claude" / "commands" / "develop.md").write_text("rendered by a sync\n")

        ctx = _make_ctx(tmp_path / "project", worktree_dir=tmp_path)  # config.base_branch defaults to "main"
        gate = await RearrangeCommitsStep().validate_output(ctx)

        assert gate.passed, gate.reason

    async def test_validate_still_fails_on_genuine_uncommitted_source(self, tmp_path: Path) -> None:
        """The gate must keep catching real work the rearrange left uncommitted."""
        import subprocess

        from sova.core.steps.rearrange_commits import RearrangeCommitsStep

        def git(*args: str) -> None:
            subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True, text=True)

        git("init", "-q")
        git("config", "user.email", "t@example.com")
        git("config", "user.name", "t")
        (tmp_path / "src.py").write_text("x = 1\n")
        git("add", "-A")
        git("commit", "-qm", "base")
        git("branch", "-M", "main")
        git("checkout", "-qb", "feature")
        (tmp_path / "src.py").write_text("x = 2\n")
        git("commit", "-qam", "feat(core): real work")
        (tmp_path / "src.py").write_text("x = 3  # left behind\n")

        ctx = _make_ctx(tmp_path / "project", worktree_dir=tmp_path)  # config.base_branch defaults to "main"
        gate = await RearrangeCommitsStep().validate_output(ctx)

        assert not gate.passed
        assert "Uncommitted" in gate.reason

    async def test_validate_fails_when_no_commits(self, tmp_path: Path) -> None:
        from sova.core.steps.rearrange_commits import RearrangeCommitsStep

        ctx = _make_ctx(tmp_path, worktree_dir=tmp_path / "worktree")
        step = RearrangeCommitsStep()

        with patch("sova.core.steps.rearrange_commits.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = MagicMock(success=True, stdout="")  # no commits
            gate = await step.validate_output(ctx)

        assert not gate.passed
        assert "No commits" in gate.reason

    async def test_validate_fails_when_uncommitted_changes_remain(self, tmp_path: Path) -> None:
        from sova.core.steps.rearrange_commits import RearrangeCommitsStep

        ctx = _make_ctx(tmp_path, worktree_dir=tmp_path / "worktree")
        step = RearrangeCommitsStep()

        with patch("sova.core.steps.rearrange_commits.run", new_callable=AsyncMock) as mock_run:
            mock_run.side_effect = [
                MagicMock(success=True, stdout="abc123 feat(core): something\n"),  # log (commits exist)
                MagicMock(success=True, stdout=" file.py | 3 +++\n"),  # diff --stat (dirty)
                MagicMock(success=True, stdout=""),  # staged
            ]
            gate = await step.validate_output(ctx)

        assert not gate.passed
        assert "Uncommitted" in gate.reason

    async def test_validate_fails_when_git_status_fails(self, tmp_path: Path) -> None:
        from sova.core.steps.rearrange_commits import RearrangeCommitsStep

        ctx = _make_ctx(tmp_path, worktree_dir=tmp_path / "worktree")
        step = RearrangeCommitsStep()

        with patch("sova.core.steps.rearrange_commits.run", new_callable=AsyncMock) as mock_run:
            mock_run.side_effect = [
                MagicMock(success=True, stdout="abc123 feat(core): something\n"),  # log
                MagicMock(success=True, stdout=""),  # diff --stat (clean)
                MagicMock(success=True, stdout=""),  # staged (clean)
                MagicMock(success=False, stdout=""),  # status --porcelain (failed)
            ]
            gate = await step.validate_output(ctx)

        assert not gate.passed
        assert "git status failed" in gate.reason

    async def test_validate_ignores_claude_and_sova_untracked(self, tmp_path: Path) -> None:
        from sova.core.steps.rearrange_commits import RearrangeCommitsStep

        ctx = _make_ctx(tmp_path, worktree_dir=tmp_path / "worktree")
        step = RearrangeCommitsStep()

        with patch("sova.core.steps.rearrange_commits.run", new_callable=AsyncMock) as mock_run:
            mock_run.side_effect = [
                MagicMock(success=True, stdout="abc123 feat(core): something\n"),  # log
                MagicMock(success=True, stdout=""),  # diff --stat (clean)
                MagicMock(success=True, stdout=""),  # staged (clean)
                MagicMock(
                    success=True,
                    stdout="?? .claude/commands/foo.md\n?? .sova/state.json\n",
                ),
            ]
            gate = await step.validate_output(ctx)

        assert gate.passed

    async def test_validate_ignores_mirrored_codex_skills_untracked(self, tmp_path: Path) -> None:
        """A freshly-mirrored `.agents/skills/` tree (Codex's manifest and command-derived
        skills included) must not fail the gate as the agent's own leftover work. Regression
        test for the #1090 failure mode reopening: with `--untracked-files=all` git lists
        each mirrored path individually rather than collapsing them to `?? .agents/`, so the
        whole-directory `.agents/skills/` exclusion prefix can match every line."""
        from sova.core.steps.rearrange_commits import RearrangeCommitsStep

        ctx = _make_ctx(tmp_path, worktree_dir=tmp_path / "worktree")
        step = RearrangeCommitsStep()

        with patch("sova.core.steps.rearrange_commits.run", new_callable=AsyncMock) as mock_run:
            mock_run.side_effect = [
                MagicMock(success=True, stdout="abc123 feat(core): something\n"),  # log
                MagicMock(success=True, stdout=""),  # diff --stat (clean)
                MagicMock(success=True, stdout=""),  # staged (clean)
                MagicMock(
                    success=True,
                    stdout=(
                        "?? .agents/skills/.sova-manifest.json\n"
                        "?? .agents/skills/sova-develop/SKILL.md\n"
                        "?? .agents/skills/testing-patterns/SKILL.md\n"
                    ),
                ),
            ]
            gate = await step.validate_output(ctx)

        assert gate.passed

    async def test_validate_reports_untracked_filenames(self, tmp_path: Path) -> None:
        from sova.core.steps.rearrange_commits import RearrangeCommitsStep

        ctx = _make_ctx(tmp_path, worktree_dir=tmp_path / "worktree")
        step = RearrangeCommitsStep()

        with patch("sova.core.steps.rearrange_commits.run", new_callable=AsyncMock) as mock_run:
            mock_run.side_effect = [
                MagicMock(success=True, stdout="abc123 feat(core): something\n"),  # log
                MagicMock(success=True, stdout=""),  # diff --stat (clean)
                MagicMock(success=True, stdout=""),  # staged (clean)
                MagicMock(success=True, stdout="?? leftover.py\n?? extra.txt\n"),
            ]
            gate = await step.validate_output(ctx)

        assert not gate.passed
        assert "leftover.py" in gate.reason
        assert "extra.txt" in gate.reason

    async def test_can_skip_when_already_completed(self, tmp_path: Path) -> None:
        from sova.core.steps.rearrange_commits import RearrangeCommitsStep

        ctx = _make_ctx(tmp_path, worktree_dir=tmp_path / "worktree", completed_steps=frozenset({"rearrange_commits"}))
        step = RearrangeCommitsStep()

        assert await step.can_skip(ctx)

    async def test_cannot_skip_when_not_completed(self, tmp_path: Path) -> None:
        from sova.core.steps.rearrange_commits import RearrangeCommitsStep

        ctx = _make_ctx(tmp_path, worktree_dir=tmp_path / "worktree")
        step = RearrangeCommitsStep()

        assert not await step.can_skip(ctx)


class TestEnsureCommitted:
    """Tests for MonitorCIStep._ensure_committed lint-then-commit safety net."""

    async def test_ruff_fixes_committed(self, tmp_path: Path) -> None:
        from sova.core.steps.monitor_ci import MonitorCIStep

        ctx = _make_ctx(tmp_path, pr_number=10)
        step = MonitorCIStep()
        run = AsyncMock()
        run.return_value = MagicMock(success=True, stdout="M sova/core/foo.py\n", stderr="")

        result = await step._ensure_committed(ctx, run)

        assert result is True
        calls = [c[0] for c in run.call_args_list]
        assert ("ruff", "check", "--fix", ".") == calls[0][:4]
        assert ("ruff", "format", ".") == calls[1][:3]
        assert ("git", "status", "--porcelain") == calls[2][:3]
        assert ("git", "add", "-u") == calls[3][:3]
        assert calls[4][0] == "git" and calls[4][1] == "commit"

    async def test_no_changes_returns_false(self, tmp_path: Path) -> None:
        from sova.core.steps.monitor_ci import MonitorCIStep

        ctx = _make_ctx(tmp_path, pr_number=10)
        step = MonitorCIStep()
        run = AsyncMock()
        run.return_value = MagicMock(success=True, stdout="", stderr="")

        result = await step._ensure_committed(ctx, run)

        assert result is False

    async def test_ruff_not_installed_continues(self, tmp_path: Path) -> None:
        from sova.core.steps.monitor_ci import MonitorCIStep

        ctx = _make_ctx(tmp_path, pr_number=10)
        step = MonitorCIStep()

        ruff_fail = MagicMock(success=False, stdout="", stderr="command not found")
        status_clean = MagicMock(success=True, stdout="", stderr="")

        run = AsyncMock(side_effect=[ruff_fail, status_clean])
        result = await step._ensure_committed(ctx, run)

        assert result is False
        assert run.call_count == 2

    async def test_ruff_makes_no_changes(self, tmp_path: Path) -> None:
        from sova.core.steps.monitor_ci import MonitorCIStep

        ctx = _make_ctx(tmp_path, pr_number=10)
        step = MonitorCIStep()

        ruff_ok = MagicMock(success=True, stdout="", stderr="")
        format_ok = MagicMock(success=True, stdout="", stderr="")
        status_clean = MagicMock(success=True, stdout="", stderr="")

        run = AsyncMock(side_effect=[ruff_ok, format_ok, status_clean])
        result = await step._ensure_committed(ctx, run)

        assert result is False

    async def test_stage_failure_returns_none(self, tmp_path: Path) -> None:
        from sova.core.steps.monitor_ci import MonitorCIStep

        ctx = _make_ctx(tmp_path, pr_number=10)
        step = MonitorCIStep()

        ruff_ok = MagicMock(success=True, stdout="", stderr="")
        format_ok = MagicMock(success=True, stdout="", stderr="")
        status_dirty = MagicMock(success=True, stdout="M foo.py\n", stderr="")
        add_fail = MagicMock(success=False, stdout="", stderr="error")

        run = AsyncMock(side_effect=[ruff_ok, format_ok, status_dirty, add_fail])
        result = await step._ensure_committed(ctx, run)

        assert result is None

    async def test_commit_failure_returns_none(self, tmp_path: Path) -> None:
        from sova.core.steps.monitor_ci import MonitorCIStep

        ctx = _make_ctx(tmp_path, pr_number=10)
        step = MonitorCIStep()

        ruff_ok = MagicMock(success=True, stdout="", stderr="")
        format_ok = MagicMock(success=True, stdout="", stderr="")
        status_dirty = MagicMock(success=True, stdout="M foo.py\n", stderr="")
        add_ok = MagicMock(success=True, stdout="", stderr="")
        commit_fail = MagicMock(success=False, stdout="", stderr="error")

        run = AsyncMock(side_effect=[ruff_ok, format_ok, status_dirty, add_ok, commit_fail])
        result = await step._ensure_committed(ctx, run)

        assert result is None


class TestValidateFixRunsEnsureCommitted:
    """Verify _ensure_committed is called before the pre-push hook in _validate_fix."""

    async def test_ensure_committed_called_before_hook(self, tmp_path: Path) -> None:
        from sova.core.steps.monitor_ci import MonitorCIStep

        ctx = _make_ctx(tmp_path, pr_number=10, branch_name="feat/test")
        step = MonitorCIStep()

        call_order = []

        async def mock_ensure(c, r):
            call_order.append("ensure_committed")
            return False

        async def mock_find_hook(wd):
            call_order.append("find_hook")
            return None

        run = AsyncMock()
        run.return_value = MagicMock(success=True, stdout="1\n", stderr="")

        with (
            patch.object(step, "_ensure_committed", side_effect=mock_ensure),
            patch("sova.core.steps.validate.find_pre_push_hook", mock_find_hook),
        ):
            await step._validate_fix(ctx, 1, 1, run)

        assert call_order == ["ensure_committed", "find_hook"]

    async def test_auto_commits_when_llm_skips_commit(self, tmp_path: Path) -> None:
        """_ensure_committed creates a commit when LLM left changes uncommitted."""
        from sova.core.steps.monitor_ci import MonitorCIStep

        ctx = _make_ctx(tmp_path, pr_number=10, branch_name="feat/test")
        step = MonitorCIStep()

        ruff_ok = MagicMock(success=True, stdout="", stderr="")
        format_ok = MagicMock(success=True, stdout="", stderr="")
        status_dirty = MagicMock(success=True, stdout="M foo.py\n", stderr="")
        add_ok = MagicMock(success=True, stdout="", stderr="")
        commit_ok = MagicMock(success=True, stdout="", stderr="")

        run = AsyncMock(side_effect=[ruff_ok, format_ok, status_dirty, add_ok, commit_ok])
        committed = await step._ensure_committed(ctx, run)

        assert committed is True
        commit_call = run.call_args_list[4]
        assert commit_call[0][1] == "commit"
        assert "fix(core):" in commit_call[0][3]

    async def test_ensure_committed_failure_skips_attempt(self, tmp_path: Path) -> None:
        """_validate_fix skips attempt when _ensure_committed returns None."""
        from sova.core.steps.monitor_ci import MonitorCIStep

        ctx = _make_ctx(tmp_path, pr_number=10, branch_name="feat/test")
        step = MonitorCIStep()

        async def mock_ensure_fail(c, r):
            return None

        run = AsyncMock()
        run.return_value = MagicMock(success=True, stdout="1\n", stderr="")

        with patch.object(step, "_ensure_committed", side_effect=mock_ensure_fail):
            should_skip, error = await step._validate_fix(ctx, 1, 2, run)

        assert should_skip is True
        assert error is None

    async def test_ensure_committed_failure_last_attempt_errors(self, tmp_path: Path) -> None:
        """_validate_fix returns error on last attempt when _ensure_committed fails."""
        from sova.core.steps.monitor_ci import MonitorCIStep

        ctx = _make_ctx(tmp_path, pr_number=10, branch_name="feat/test")
        step = MonitorCIStep()

        async def mock_ensure_fail(c, r):
            return None

        run = AsyncMock()
        run.return_value = MagicMock(success=True, stdout="1\n", stderr="")

        with patch.object(step, "_ensure_committed", side_effect=mock_ensure_fail):
            should_skip, error = await step._validate_fix(ctx, 2, 2, run)

        assert should_skip is False
        assert error is not None
        assert not error.success
        assert "stage/commit" in error.summary


class TestBuildFixPromptEnhancements:
    """Verify _build_fix_prompt includes lint instructions and invariant rules."""

    def test_includes_ruff_instructions(self, tmp_path: Path) -> None:
        from sova.core.steps.monitor_ci import MonitorCIStep

        ctx = _make_ctx(tmp_path, pr_number=10)
        check = MagicMock()
        check.name = "tests"
        step = MonitorCIStep()

        prompt = step._build_fix_prompt([check], "error log", "abc123 commit", ctx)

        assert "ruff check --fix" in prompt
        assert "ruff format" in prompt

    def test_includes_invariant_rules(self, tmp_path: Path) -> None:
        from sova.core.steps.monitor_ci import MonitorCIStep

        ctx = _make_ctx(tmp_path, pr_number=10)
        check = MagicMock()
        check.name = "tests"
        step = MonitorCIStep()

        prompt = step._build_fix_prompt([check], "error log", "abc123 commit", ctx)

        assert "Decimal" in prompt
        assert "floats" in prompt
        assert "co-author" in prompt
        assert "emojis" in prompt
        assert "Project Invariant Rules" in prompt

    def test_prompt_uses_scoped_commit_format(self, tmp_path: Path) -> None:
        from sova.core.steps.monitor_ci import MonitorCIStep

        ctx = _make_ctx(tmp_path, pr_number=10)
        check = MagicMock()
        check.name = "tests"
        step = MonitorCIStep()

        prompt = step._build_fix_prompt([check], "error log", "abc123 commit", ctx)

        assert "fix(core):" in prompt


# ---------------------------------------------------------------------------
# CommitStep: agent artifact filtering
# ---------------------------------------------------------------------------


class TestCommitStepAgentArtifacts:
    @pytest.fixture(autouse=True)
    def _stub_subject_proposal(self):
        with patch("sova.core.steps.commit.invoke", new_callable=AsyncMock, side_effect=RuntimeError("stub")):
            yield

    async def test_skips_commit_when_only_agent_artifacts_untracked(self, tmp_path: Path) -> None:
        from sova.core.steps.commit import CommitStep

        ctx = _make_ctx(tmp_path, worktree_dir=tmp_path / "worktree")
        step = CommitStep()

        with patch("sova.core.steps.commit.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(success=True, stdout=""),  # diff --stat HEAD (clean)
                MagicMock(success=True, stdout=""),  # diff --cached --stat (clean)
                MagicMock(success=True, stdout=""),  # diff --name-only -z HEAD (clean)
                MagicMock(success=True, stdout=""),  # diff --cached --name-only -z (clean)
                MagicMock(success=True, stdout=".claude/settings.json\n.agent-summary.md\n"),  # only artifacts
                MagicMock(success=True, stdout="abc123 feat: real work\n"),  # has commits
            ]
            result = await step.execute(ctx)

        assert result.success
        assert "already exist" in result.summary

    async def test_commits_meaningful_untracked_files(self, tmp_path: Path) -> None:
        from sova.core.steps.commit import CommitStep

        ctx = _make_ctx(tmp_path, worktree_dir=tmp_path / "worktree")
        ctx.task = Task(id="42", title="Add feature")
        step = CommitStep()

        with (
            patch("sova.core.steps.commit.run") as mock_run,
            patch("sova.core.steps.commit.git_ops.commit", new_callable=AsyncMock) as mock_commit,
        ):
            mock_run.side_effect = [
                MagicMock(success=True, stdout=""),  # diff --stat HEAD
                MagicMock(success=True, stdout=""),  # diff --cached --stat
                MagicMock(success=True, stdout=""),  # diff --name-only -z HEAD
                MagicMock(success=True, stdout=""),  # diff --cached --name-only -z
                MagicMock(success=True, stdout="src/new_file.py\n.claude/tmp.json\n"),  # mixed
                MagicMock(success=True, stdout=""),  # log (no commits)
            ]
            result = await step.execute(ctx)

        assert result.success
        mock_commit.assert_awaited_once()


# ---------------------------------------------------------------------------
# CommitStep: issueless runs
# ---------------------------------------------------------------------------


class TestCommitStepIssueless:
    @pytest.fixture(autouse=True)
    def _stub_subject_proposal(self):
        with patch("sova.core.steps.commit.invoke", new_callable=AsyncMock, side_effect=RuntimeError("stub")):
            yield

    async def test_issueless_commit_uses_run_label(self, tmp_path: Path) -> None:
        """Issueless runs should use run_label in commit message, no Closes line."""
        from sova.core.steps.commit import CommitStep

        ctx = _make_ctx(tmp_path, issue_number="", run_label="sprint-planning", worktree_dir=tmp_path / "worktree")
        ctx.task = None
        step = CommitStep()

        with (
            patch("sova.core.steps.commit.run") as mock_run,
            patch("sova.core.steps.commit.git_ops.commit", new_callable=AsyncMock) as mock_commit,
        ):
            mock_run.side_effect = [
                MagicMock(success=True, stdout=" plan.py | 3 +++\n"),
                MagicMock(success=True, stdout=""),
                MagicMock(success=True, stdout="plan.py\0"),
                MagicMock(success=True, stdout=""),
                MagicMock(success=True, stdout=""),
            ]
            result = await step.execute(ctx)

        assert result.success
        msg = mock_commit.call_args[0][0]
        assert "sprint-planning" in msg
        assert "Closes" not in msg

    async def test_issueless_commit_fallback_title(self, tmp_path: Path) -> None:
        """When no task, no issue, and no run_label, commit uses 'run'."""
        from sova.core.steps.commit import CommitStep

        ctx = _make_ctx(tmp_path, issue_number="", run_label="", worktree_dir=tmp_path / "worktree")
        ctx.task = None
        step = CommitStep()

        with (
            patch("sova.core.steps.commit.run") as mock_run,
            patch("sova.core.steps.commit.git_ops.commit", new_callable=AsyncMock) as mock_commit,
        ):
            mock_run.side_effect = [
                MagicMock(success=True, stdout=" plan.py | 3 +++\n"),
                MagicMock(success=True, stdout=""),
                MagicMock(success=True, stdout="plan.py\0"),
                MagicMock(success=True, stdout=""),
                MagicMock(success=True, stdout=""),
            ]
            result = await step.execute(ctx)

        assert result.success
        msg = mock_commit.call_args[0][0]
        assert "feat(core): run" in msg
        assert "Closes" not in msg

    async def test_issueless_address_review_uses_run_label(self, tmp_path: Path) -> None:
        """Address-review without issue uses run_label for commit message."""
        from sova.core.steps.commit import CommitStep

        ctx = _make_ctx(
            tmp_path,
            issue_number="",
            run_label="sprint-planning",
            worktree_dir=tmp_path / "worktree",
            pr_number=200,
            completed_steps=frozenset({"address_review"}),
        )
        ctx.task = None
        step = CommitStep()

        with (
            patch("sova.core.steps.commit.run") as mock_run,
            patch("sova.core.steps.commit.git_ops.commit", new_callable=AsyncMock) as mock_commit,
        ):
            mock_run.side_effect = [
                MagicMock(success=True, stdout=" fix.py | 3 +++\n"),
                MagicMock(success=True, stdout=""),
                MagicMock(success=True, stdout="fix.py\0"),
                MagicMock(success=True, stdout=""),
                MagicMock(success=True, stdout=""),
            ]
            result = await step.execute(ctx)

        assert result.success
        msg = mock_commit.call_args[0][0]
        assert msg.startswith("fix(core):")
        assert "sprint-planning" in msg

    async def test_commit_failure_returns_error(self, tmp_path: Path) -> None:
        """RuntimeError during git commit should return a failed StepResult."""
        from sova.core.steps.commit import CommitStep

        ctx = _make_ctx(tmp_path, worktree_dir=tmp_path / "worktree")
        step = CommitStep()

        with (
            patch("sova.core.steps.commit.run") as mock_run,
            patch("sova.core.steps.commit.git_ops.commit", new_callable=AsyncMock) as mock_commit,
        ):
            mock_run.side_effect = [
                MagicMock(success=True, stdout=" fix.py | 3 +++\n"),
                MagicMock(success=True, stdout=""),
                MagicMock(success=True, stdout="fix.py\0"),
                MagicMock(success=True, stdout=""),
                MagicMock(success=True, stdout=""),
            ]
            mock_commit.side_effect = RuntimeError("suspicious file staged")
            result = await step.execute(ctx)

        assert not result.success
        assert "Commit failed" in result.summary


# ---------------------------------------------------------------------------
# CommitStep: JIRA-aware linking
# ---------------------------------------------------------------------------


class TestCommitStepJira:
    @pytest.fixture(autouse=True)
    def _stub_subject_proposal(self):
        with patch("sova.core.steps.commit.invoke", new_callable=AsyncMock, side_effect=RuntimeError("stub")):
            yield

    async def test_jira_commit_message_has_ticket_link(self, tmp_path: Path) -> None:
        """JIRA projects should emit JIRA: link instead of Closes #N."""
        from sova.core.steps.commit import CommitStep

        ctx = _make_ctx(
            tmp_path,
            worktree_dir=tmp_path / "worktree",
            issue_number="48767",
            config=ProjectConfig(
                task_source=TaskSourceConfig(
                    type="jira",
                    jira_project_key="PROJ",
                    jira_base_url="https://jira.example.com",
                )
            ),
        )
        ctx.task = Task(id="48767", title="Add security logging")
        step = CommitStep()

        with (
            patch("sova.core.steps.commit.run") as mock_run,
            patch("sova.core.steps.commit.git_ops.commit", new_callable=AsyncMock) as mock_commit,
        ):
            mock_run.side_effect = [
                MagicMock(success=True, stdout=" security.py | 5 +++++\n"),
                MagicMock(success=True, stdout=""),
                MagicMock(success=True, stdout="security.py\0"),
                MagicMock(success=True, stdout=""),
                MagicMock(success=True, stdout=""),
            ]
            result = await step.execute(ctx)

        assert result.success
        msg = mock_commit.call_args[0][0]
        assert "JIRA: https://jira.example.com/browse/PROJ-48767" in msg
        assert "Closes #" not in msg

    async def test_github_commit_message_unchanged(self, tmp_path: Path) -> None:
        """GitHub projects should produce Refs #N, not a GitHub closing keyword."""
        from sova.core.steps.commit import CommitStep

        ctx = _make_ctx(tmp_path, worktree_dir=tmp_path / "worktree")
        ctx.task = Task(id="42", title="Fix login bug")
        ctx.config = ProjectConfig(task_source=TaskSourceConfig(type="github"))
        step = CommitStep()

        with (
            patch("sova.core.steps.commit.run") as mock_run,
            patch("sova.core.steps.commit.git_ops.commit", new_callable=AsyncMock) as mock_commit,
        ):
            mock_run.side_effect = [
                MagicMock(success=True, stdout=" login.py | 3 +++\n"),
                MagicMock(success=True, stdout=""),
                MagicMock(success=True, stdout="login.py\0"),
                MagicMock(success=True, stdout=""),
                MagicMock(success=True, stdout=""),
            ]
            result = await step.execute(ctx)

        assert result.success
        msg = mock_commit.call_args[0][0]
        assert "Refs #42" in msg
        assert "Closes #" not in msg
        assert "JIRA:" not in msg


# ---------------------------------------------------------------------------
# PushStep: execute path
# ---------------------------------------------------------------------------


class TestPushStepExecute:
    async def test_execute_calls_git_push(self, tmp_path: Path) -> None:
        from sova.core.steps.push import PushStep
        from sova.git.branch import DivergenceCheck, DivergenceStatus

        ctx = _make_ctx(tmp_path, branch_name="feat/test", worktree_dir=tmp_path / "worktree")
        step = PushStep()

        with (
            patch("sova.core.steps.push.git_ops.push", new_callable=AsyncMock) as mock_push,
            patch(
                "sova.core.steps.push.git_ops.check_branch_divergence",
                new_callable=AsyncMock,
                return_value=DivergenceCheck(status=DivergenceStatus.ANCESTOR, remote_sha="aaa", head_sha="bbb"),
            ),
        ):
            result = await step.execute(ctx)

        assert result.success
        assert "feat/test" in result.summary
        mock_push.assert_awaited_once_with(
            "feat/test",
            force=False,
            lease_sha=None,
            set_upstream=True,
            cwd=tmp_path / "worktree",
            no_verify=False,
            github_user=ctx.config.github_user,
            config=ctx.config,
        )

    async def test_execute_plain_push_on_first_push(self, tmp_path: Path) -> None:
        """No remote ref (first push of a new branch) must resolve to a plain push."""
        from sova.core.steps.push import PushStep
        from sova.git.branch import DivergenceCheck, DivergenceStatus

        ctx = _make_ctx(tmp_path, branch_name="feat/test", worktree_dir=tmp_path / "worktree")
        step = PushStep()

        with (
            patch("sova.core.steps.push.git_ops.push", new_callable=AsyncMock) as mock_push,
            patch(
                "sova.core.steps.push.git_ops.check_branch_divergence",
                new_callable=AsyncMock,
                return_value=DivergenceCheck(status=DivergenceStatus.NO_REMOTE_REF),
            ),
        ):
            result = await step.execute(ctx)

        assert result.success
        mock_push.assert_awaited_once_with(
            "feat/test",
            force=False,
            lease_sha=None,
            set_upstream=True,
            cwd=tmp_path / "worktree",
            no_verify=False,
            github_user=ctx.config.github_user,
            config=ctx.config,
        )

    async def test_execute_plain_push_when_divergence_check_errors(self, tmp_path: Path) -> None:
        """A failed divergence check (unreachable git, shallow clone) must fall
        through to a plain push rather than guessing at force."""
        from sova.core.steps.push import PushStep
        from sova.git.branch import DivergenceCheck, DivergenceStatus

        ctx = _make_ctx(tmp_path, branch_name="feat/test", worktree_dir=tmp_path / "worktree")
        step = PushStep()

        with (
            patch("sova.core.steps.push.git_ops.push", new_callable=AsyncMock) as mock_push,
            patch(
                "sova.core.steps.push.git_ops.check_branch_divergence",
                new_callable=AsyncMock,
                return_value=DivergenceCheck(status=DivergenceStatus.ERROR, detail="fetch failed"),
            ),
        ):
            result = await step.execute(ctx)

        assert result.success
        mock_push.assert_awaited_once_with(
            "feat/test",
            force=False,
            lease_sha=None,
            set_upstream=True,
            cwd=tmp_path / "worktree",
            no_verify=False,
            github_user=ctx.config.github_user,
            config=ctx.config,
        )

    async def test_execute_force_with_lease_when_diverged(self, tmp_path: Path) -> None:
        from sova.core.steps.push import PushStep
        from sova.dashboard.services.feed_service import FeedEventSeverity
        from sova.git.branch import DivergenceCheck, DivergenceStatus

        ctx = _make_ctx(tmp_path, branch_name="feat/test", worktree_dir=tmp_path / "worktree")
        step = PushStep()

        with (
            patch("sova.core.steps.push.git_ops.push", new_callable=AsyncMock) as mock_push,
            patch(
                "sova.core.steps.push.git_ops.check_branch_divergence",
                new_callable=AsyncMock,
                return_value=DivergenceCheck(status=DivergenceStatus.DIVERGED, remote_sha="deadbeef", head_sha="cafe"),
            ),
            patch(
                "sova.core.steps.push.git_ops.list_dropped_commits",
                new_callable=AsyncMock,
                return_value=["deadbee dropped commit"],
            ),
            patch("sova.dashboard.services.feed_service.emit_safe", autospec=True) as mock_emit,
        ):
            result = await step.execute(ctx)

        assert result.success
        mock_push.assert_awaited_once_with(
            "feat/test",
            force=True,
            lease_sha="deadbeef",
            set_upstream=True,
            cwd=tmp_path / "worktree",
            no_verify=False,
            github_user=ctx.config.github_user,
            config=ctx.config,
        )
        mock_emit.assert_called_once()
        _, emit_kwargs = mock_emit.call_args
        assert emit_kwargs["severity"] == FeedEventSeverity.warning

    async def test_execute_emits_info_when_force_push_drops_nothing(self, tmp_path: Path) -> None:
        from sova.core.steps.push import PushStep
        from sova.dashboard.services.feed_service import FeedEventSeverity
        from sova.git.branch import DivergenceCheck, DivergenceStatus

        ctx = _make_ctx(tmp_path, branch_name="feat/test", worktree_dir=tmp_path / "worktree")
        step = PushStep()

        with (
            patch("sova.core.steps.push.git_ops.push", new_callable=AsyncMock),
            patch(
                "sova.core.steps.push.git_ops.check_branch_divergence",
                new_callable=AsyncMock,
                return_value=DivergenceCheck(status=DivergenceStatus.DIVERGED, remote_sha="deadbeef", head_sha="cafe"),
            ),
            patch(
                "sova.core.steps.push.git_ops.list_dropped_commits",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch("sova.dashboard.services.feed_service.emit_safe", autospec=True) as mock_emit,
        ):
            await step.execute(ctx)

        _, emit_kwargs = mock_emit.call_args
        assert emit_kwargs["severity"] == FeedEventSeverity.info

    async def test_execute_skips_hooks_when_budget_critically_low(self, tmp_path: Path) -> None:
        """When dollar budget is critically low, Push must skip the pre-push
        hook, matching CommitStep's BUDGET_SKIP_HOOKS_THRESHOLD so both
        hook-skip decisions agree. Keyed on budget_remaining_fraction alone
        (not the composite resource_remaining_fraction), since wall-clock or
        LLM-call pressure must never disable validation on its own."""
        from sova.config.models import AgentConfig
        from sova.core.steps.push import PushStep
        from sova.git.branch import DivergenceCheck, DivergenceStatus

        config = ProjectConfig(agent=AgentConfig(max_budget=Decimal("10.00")))
        ctx = _make_ctx(tmp_path, branch_name="feat/test", worktree_dir=tmp_path / "worktree", config=config)
        ctx.cost_usd = Decimal("9.50")  # 5% remaining, below the 8% skip-hooks threshold
        step = PushStep()

        with (
            patch("sova.core.steps.push.git_ops.push", new_callable=AsyncMock) as mock_push,
            patch(
                "sova.core.steps.push.git_ops.check_branch_divergence",
                new_callable=AsyncMock,
                return_value=DivergenceCheck(status=DivergenceStatus.ANCESTOR, remote_sha="aaa", head_sha="bbb"),
            ),
        ):
            result = await step.execute(ctx)

        assert result.success
        mock_push.assert_awaited_once_with(
            "feat/test",
            force=False,
            lease_sha=None,
            set_upstream=True,
            cwd=tmp_path / "worktree",
            no_verify=True,
            github_user=ctx.config.github_user,
            config=ctx.config,
        )

    async def test_execute_rejects_empty_branch_name(self, tmp_path: Path) -> None:
        """An empty branch_name must fail fast without ever calling git_ops.push."""
        from sova.core.steps.push import PushStep

        ctx = _make_ctx(tmp_path, branch_name="", worktree_dir=tmp_path / "worktree")
        step = PushStep()

        with patch("sova.core.steps.push.git_ops.push", new_callable=AsyncMock) as mock_push:
            result = await step.execute(ctx)

        assert not result.success
        assert "branch_name is empty" in result.error
        mock_push.assert_not_awaited()

    async def test_execute_handles_push_failure(self, tmp_path: Path) -> None:
        from sova.core.steps.push import PushStep
        from sova.git.branch import DivergenceCheck, DivergenceStatus

        ctx = _make_ctx(tmp_path, branch_name="feat/test", worktree_dir=tmp_path / "worktree")
        step = PushStep()

        with (
            patch("sova.core.steps.push.git_ops.push", new_callable=AsyncMock) as mock_push,
            patch(
                "sova.core.steps.push.git_ops.check_branch_divergence",
                new_callable=AsyncMock,
                return_value=DivergenceCheck(status=DivergenceStatus.ANCESTOR, remote_sha="aaa", head_sha="bbb"),
            ),
        ):
            mock_push.side_effect = RuntimeError("Permission denied")
            result = await step.execute(ctx)

        assert not result.success
        assert "Permission denied" in result.error

    async def test_execute_retries_once_on_rejection_when_lease_unchanged(self, tmp_path: Path) -> None:
        """A force push rejected due to a transient failure (the fresh
        divergence check on retry observes the SAME remote SHA as the first
        attempt) is re-evaluated once and retried with that unchanged lease,
        not repeated identically."""
        from sova.core.steps.push import PushStep
        from sova.git.branch import DivergenceCheck, DivergenceStatus

        ctx = _make_ctx(tmp_path, branch_name="feat/test", worktree_dir=tmp_path / "worktree")
        step = PushStep()

        divergence_results = [
            DivergenceCheck(status=DivergenceStatus.DIVERGED, remote_sha="same-sha", head_sha="bbb"),
            DivergenceCheck(status=DivergenceStatus.DIVERGED, remote_sha="same-sha", head_sha="bbb"),
        ]

        with (
            patch("sova.core.steps.push.git_ops.push", new_callable=AsyncMock) as mock_push,
            patch(
                "sova.core.steps.push.git_ops.check_branch_divergence",
                new_callable=AsyncMock,
                side_effect=divergence_results,
            ),
            patch(
                "sova.core.steps.push.git_ops.list_dropped_commits",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch("sova.dashboard.services.feed_service.emit_safe", autospec=True),
        ):
            mock_push.side_effect = [RuntimeError("! [rejected] (stale info)"), None]
            result = await step.execute(ctx)

        assert result.success
        assert mock_push.await_count == 2
        second_call_kwargs = mock_push.call_args_list[1].kwargs
        assert second_call_kwargs["force"] is True
        assert second_call_kwargs["lease_sha"] == "same-sha"

    async def test_execute_aborts_retry_when_remote_moved(self, tmp_path: Path) -> None:
        """A force push rejected because a concurrent writer landed a real
        commit must never retry with a freshly re-derived lease: that lease
        would be trivially satisfied by the very commit that caused the
        rejection, silently discarding it. The retry's own divergence check
        observing a different remote SHA than the first attempt must abort
        with a clear error instead of pushing again."""
        from sova.core.steps.push import PushStep
        from sova.git.branch import DivergenceCheck, DivergenceStatus

        ctx = _make_ctx(tmp_path, branch_name="feat/test", worktree_dir=tmp_path / "worktree")
        step = PushStep()

        divergence_results = [
            DivergenceCheck(status=DivergenceStatus.DIVERGED, remote_sha="stale-sha", head_sha="bbb"),
            DivergenceCheck(status=DivergenceStatus.DIVERGED, remote_sha="fresh-sha", head_sha="bbb"),
        ]

        with (
            patch("sova.core.steps.push.git_ops.push", new_callable=AsyncMock) as mock_push,
            patch(
                "sova.core.steps.push.git_ops.check_branch_divergence",
                new_callable=AsyncMock,
                side_effect=divergence_results,
            ),
            patch(
                "sova.core.steps.push.git_ops.list_dropped_commits",
                new_callable=AsyncMock,
            ) as mock_dropped,
            patch("sova.dashboard.services.feed_service.emit_safe", autospec=True) as mock_emit,
        ):
            mock_push.side_effect = [RuntimeError("! [rejected] (stale info)"), None]
            result = await step.execute(ctx)

        assert not result.success
        assert "stale-sha" in result.error
        assert "fresh-sha" in result.error
        mock_push.assert_awaited_once()
        mock_dropped.assert_not_awaited()
        mock_emit.assert_not_called()

    async def test_execute_plain_push_rejection_fails_without_escalating(self, tmp_path: Path) -> None:
        """A plain push (original decision was ANCESTOR, not DIVERGED) that
        gets rejected means a concurrent writer landed a real commit between
        the check and the push. This must surface as a clear error on the
        first attempt, never escalate to a force push."""
        from sova.core.steps.push import PushStep
        from sova.git.branch import DivergenceCheck, DivergenceStatus

        ctx = _make_ctx(tmp_path, branch_name="feat/test", worktree_dir=tmp_path / "worktree")
        step = PushStep()

        with (
            patch("sova.core.steps.push.git_ops.push", new_callable=AsyncMock) as mock_push,
            patch(
                "sova.core.steps.push.git_ops.check_branch_divergence",
                new_callable=AsyncMock,
                return_value=DivergenceCheck(status=DivergenceStatus.ANCESTOR, remote_sha="aaa", head_sha="bbb"),
            ),
        ):
            mock_push.side_effect = RuntimeError("! [rejected] (non-fast-forward)")
            result = await step.execute(ctx)

        assert not result.success
        assert "rejected" in result.error.lower()
        mock_push.assert_awaited_once()
        assert mock_push.await_args.kwargs["force"] is False

    async def test_execute_surfaces_error_when_retry_also_rejected(self, tmp_path: Path) -> None:
        """A concurrent writer refusing both attempts must surface as a clear
        error, never fall back to a bare force push."""
        from sova.core.steps.push import PushStep
        from sova.git.branch import DivergenceCheck, DivergenceStatus

        ctx = _make_ctx(tmp_path, branch_name="feat/test", worktree_dir=tmp_path / "worktree")
        step = PushStep()

        with (
            patch("sova.core.steps.push.git_ops.push", new_callable=AsyncMock) as mock_push,
            patch(
                "sova.core.steps.push.git_ops.check_branch_divergence",
                new_callable=AsyncMock,
                return_value=DivergenceCheck(status=DivergenceStatus.DIVERGED, remote_sha="aaa", head_sha="bbb"),
            ),
            patch(
                "sova.core.steps.push.git_ops.list_dropped_commits",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch("sova.dashboard.services.feed_service.emit_safe", autospec=True),
        ):
            mock_push.side_effect = RuntimeError("! [rejected] (stale info)")
            result = await step.execute(ctx)

        assert not result.success
        assert "rejected" in result.error.lower()
        assert mock_push.await_count == 2

    async def test_validate_output_passes_with_commits(self, tmp_path: Path) -> None:
        from sova.core.steps.push import PushStep

        ctx = _make_ctx(tmp_path, branch_name="feat/test", worktree_dir=tmp_path / "worktree")
        step = PushStep()

        with patch("sova.core.steps.push.run") as mock_run:
            mock_run.return_value = MagicMock(success=True, stdout="3\n")
            gate = await step.validate_output(ctx)

        assert gate.passed

    async def test_validate_output_fails_on_command_failure(self, tmp_path: Path) -> None:
        from sova.core.steps.push import PushStep

        ctx = _make_ctx(tmp_path, branch_name="feat/test", worktree_dir=tmp_path / "worktree")
        step = PushStep()

        with patch("sova.core.steps.push.run") as mock_run:
            mock_run.return_value = MagicMock(success=False, stdout="")
            gate = await step.validate_output(ctx)

        assert not gate.passed
        assert "Failed to count" in gate.reason

    async def test_validate_output_fails_on_bad_output(self, tmp_path: Path) -> None:
        from sova.core.steps.push import PushStep

        ctx = _make_ctx(tmp_path, branch_name="feat/test", worktree_dir=tmp_path / "worktree")
        step = PushStep()

        with patch("sova.core.steps.push.run") as mock_run:
            mock_run.return_value = MagicMock(success=True, stdout="not-a-number\n")
            gate = await step.validate_output(ctx)

        assert not gate.passed
        assert "Unexpected rev-list" in gate.reason
