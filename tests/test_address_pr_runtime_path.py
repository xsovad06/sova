"""Codex Address PR runs through the host-owned review pipeline."""

import subprocess
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestCodexAddressPrDispatch:
    async def test_pr_command_uses_project_runtime_in_multi_project_server(self, tmp_path: Path) -> None:
        from sova.dashboard.services import agent_lifecycle
        from sova.dashboard.services.agent_pool import ProjectAgents

        pa = ProjectAgents()
        pa.project_dir = tmp_path
        server_runtime = MagicMock()
        server_runtime.name = "claude-code"
        project_config = MagicMock()
        project_config.agent.runtime = "codex"
        with (
            patch.object(agent_lifecycle, "_get_project_agents", return_value=pa),
            patch.object(agent_lifecycle, "get_runtime", return_value=server_runtime),
            patch("sova.config.loader.load_config", return_value=project_config),
            patch.object(
                agent_lifecycle, "_resolve_command_context", new_callable=AsyncMock, return_value=(1496, "1418")
            ),
            patch.object(
                agent_lifecycle, "start_agent", new_callable=AsyncMock, return_value={"status": "started"}
            ) as start,
        ):
            result = await agent_lifecycle.start_command("address-pr", {"pr": 1496})

        assert result == {"status": "started"}
        start.assert_awaited_once_with("1418", role="developer", pr_number=1496, slug=None)

    async def test_pr_command_requires_pr_number(self, tmp_path: Path) -> None:
        from sova.dashboard.services import agent_lifecycle
        from sova.dashboard.services.agent_pool import ProjectAgents

        pa = ProjectAgents()
        pa.project_dir = tmp_path
        project_config = MagicMock()
        project_config.agent.runtime = "codex"
        with (
            patch.object(agent_lifecycle, "_get_project_agents", return_value=pa),
            patch("sova.config.loader.load_config", return_value=project_config),
            patch.object(
                agent_lifecycle, "_resolve_command_context", new_callable=AsyncMock, return_value=(None, "1418")
            ),
            patch.object(agent_lifecycle, "start_agent", new_callable=AsyncMock) as start,
        ):
            result = await agent_lifecycle.start_command("address-pr", {"issue": "1418"})

        assert "PR number" in result["error"]
        start.assert_not_awaited()


class TestAddressPrOutcome:
    @pytest.mark.parametrize(
        ("reviews", "expected"),
        [
            ([{"body": "<!-- sova-addressed -->", "submittedAt": "2026-11-18T18:01:00Z"}], True),
            ([{"body": "<!-- sova-addressed -->", "submittedAt": "2026-11-18T17:59:00Z"}], False),
            ([{"body": "review without marker", "submittedAt": "2026-11-18T18:01:00Z"}], False),
            ([], False),
        ],
    )
    async def test_summary_must_be_posted_during_run(self, tmp_path: Path, reviews: list[dict], expected: bool) -> None:
        import json

        from sova.dashboard.services.agent_db import _check_address_summary
        from sova.dashboard.services.agent_pool import AgentState

        agent = MagicMock(spec=AgentState)
        agent.pr_number = 1496
        agent.project_dir = tmp_path
        agent.started_at_utc = datetime(2026, 11, 18, 18, 0, tzinfo=timezone.utc)
        with patch(
            "sova.dashboard.services.agent_db._fetch_pr_fields",
            new_callable=AsyncMock,
            return_value=(json.dumps(reviews), False),
        ):
            assert await _check_address_summary(agent) == (expected, False)

    async def test_pushed_run_without_summary_fails(self, tmp_path: Path) -> None:
        from sova.dashboard.services.agent_db import _validate_address_pr
        from sova.dashboard.services.agent_pool import AgentState

        agent = MagicMock(spec=AgentState)
        agent.pr_number = 1496
        agent.project_dir = tmp_path
        agent.pre_run_sha = "before"
        with (
            patch(
                "sova.dashboard.services.agent_db._fetch_output_lines",
                new_callable=AsyncMock,
                return_value=["pushed fixes"],
            ),
            patch(
                "sova.dashboard.services.agent_db._check_pr_pushed_via_sha",
                new_callable=AsyncMock,
                return_value=(True, False),
            ),
            patch(
                "sova.dashboard.services.agent_db._check_address_summary",
                new_callable=AsyncMock,
                return_value=(False, False),
            ),
        ):
            result = await _validate_address_pr(1, agent)

        assert result.failure_reason == "address-pr exited before posting the address summary for PR #1496"

    async def test_unreachable_reviews_leave_outcome_unverified(self, tmp_path: Path) -> None:
        from sova.dashboard.services.agent_db import _validate_address_pr
        from sova.dashboard.services.agent_pool import AgentState

        agent = MagicMock(spec=AgentState)
        agent.pr_number = 1496
        agent.project_dir = tmp_path
        agent.pre_run_sha = "before"
        with (
            patch(
                "sova.dashboard.services.agent_db._fetch_output_lines",
                new_callable=AsyncMock,
                return_value=["pushed fixes"],
            ),
            patch(
                "sova.dashboard.services.agent_db._check_pr_pushed_via_sha",
                new_callable=AsyncMock,
                return_value=(True, False),
            ),
            patch(
                "sova.dashboard.services.agent_db._check_address_summary",
                new_callable=AsyncMock,
                return_value=(None, True),
            ),
        ):
            result = await _validate_address_pr(1, agent)

        assert result.network_blocked is True
        assert "unverified" in result.failure_reason

    async def test_validator_exception_cannot_mark_run_done(self, tmp_path: Path) -> None:
        from sova.dashboard.services.agent_db import _validate_command_outcome
        from sova.dashboard.services.agent_pool import AgentState

        agent = MagicMock(spec=AgentState)
        agent.role = "command:/address-pr"
        with patch.dict(
            "sova.dashboard.services.agent_db._COMMAND_VALIDATORS",
            {"address-pr": AsyncMock(side_effect=RuntimeError("lookup failed"))},
        ):
            result = await _validate_command_outcome(1, agent)

        assert "unverified" in result.failure_reason

    @pytest.mark.parametrize("head_result", [(False, False), (None, False)])
    async def test_unchanged_or_unverified_head_is_not_a_completed_run(
        self, tmp_path: Path, head_result: tuple[bool | None, bool]
    ) -> None:
        from sova.dashboard.services.agent_db import _validate_address_pr
        from sova.dashboard.services.agent_pool import AgentState

        agent = MagicMock(spec=AgentState)
        agent.pr_number = 1496
        agent.pre_run_sha = "before"
        agent.project_dir = tmp_path
        with (
            patch(
                "sova.dashboard.services.agent_db._fetch_output_lines",
                new_callable=AsyncMock,
                return_value=["git push --force-with-lease", "pushed to remote"],
            ),
            patch(
                "sova.dashboard.services.agent_db._check_pr_pushed_via_sha",
                new_callable=AsyncMock,
                return_value=head_result,
            ),
            patch(
                "sova.dashboard.services.agent_db._check_pr_branch_pushed",
                new_callable=AsyncMock,
                return_value=(True, False),
            ),
        ):
            result = await _validate_address_pr(1, agent)

        assert result.failure_reason is not None


class TestHostCommitFold:
    async def test_codex_fixes_are_amended_by_sova(self, tmp_path: Path) -> None:
        from sova.config.models import ProjectConfig
        from sova.core.steps.rearrange_commits import RearrangeCommitsStep

        def git(*args: str) -> str:
            return subprocess.run(
                ["git", *args], cwd=tmp_path, check=True, capture_output=True, text=True
            ).stdout.strip()

        git("init", "-b", "main")
        git("config", "user.email", "test@example.com")
        git("config", "user.name", "Test")
        git("config", "commit.gpgsign", "false")
        (tmp_path / "code.py").write_text("value = 1\n", encoding="utf-8")
        git("add", "code.py")
        git("commit", "-m", "feat(core): add code")
        git("switch", "-c", "feat/test")
        (tmp_path / "code.py").write_text("value = 2\n", encoding="utf-8")
        git("add", "code.py")
        git("commit", "-m", "fix(core): correct code")
        original = git("rev-parse", "HEAD")
        (tmp_path / "code.py").write_text("value = 3\n", encoding="utf-8")
        artifact = tmp_path / ".agents" / "skills" / "generated"
        artifact.mkdir(parents=True)
        (artifact / "SKILL.md").write_text("generated\n", encoding="utf-8")

        ctx = MagicMock()
        ctx.working_dir = tmp_path
        ctx.base_branch = "main"
        ctx.branch_name = "feat/test"
        ctx.config = ProjectConfig()
        ctx.config.agent.runtime = "codex"
        result = await RearrangeCommitsStep().execute(ctx)

        assert result.success, result.error
        assert git("rev-parse", "HEAD") != original
        assert git("rev-list", "--count", "main..HEAD") == "1"
        assert git("show", "HEAD:code.py") == "value = 3"
        assert not git("ls-files", ".agents")
