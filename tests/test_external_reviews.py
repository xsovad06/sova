"""Tests for external review tool integration (config, adapter, pipeline steps)."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

from sova.adapters.base import Task, TaskState
from sova.adapters.external_reviews import (
    ExternalCheckStatus,
    ExternalFinding,
    _fetch_coderabbit_threads,
    _ThreadsResult,
    fetch_coderabbit_findings,
    fetch_sonarcloud_issues,
    format_findings_for_prompt,
    get_check_statuses,
)
from sova.config.models import ExternalReviewsConfig, ProjectConfig, SonarCloudConfig
from sova.core.context import ExecutionContext
from sova.utils.shell import ShellResult


def _shell_result(stdout: str = "", stderr: str = "", returncode: int = 0) -> ShellResult:
    return ShellResult(returncode=returncode, stdout=stdout, stderr=stderr)


def _mock_adapter() -> AsyncMock:
    adapter = AsyncMock()
    adapter.get_state.return_value = TaskState.RESEARCHED
    adapter.get_task.return_value = Task(id="1", title="Test issue")
    return adapter


def _make_ctx(**kwargs: Any) -> ExecutionContext:
    defaults = {
        "project_dir": Path("/nonexistent/sova-test-project"),
        "config": ProjectConfig(github_repo="owner/repo"),
        "adapter": _mock_adapter(),
        "issue_number": "42",
        "role": "developer",
    }
    defaults.update(kwargs)
    return ExecutionContext(**defaults)


def _ext_config(**kwargs: Any) -> ExternalReviewsConfig:
    defaults = {
        "enabled": True,
        "tools": ["sonarcloud", "coderabbit"],
        "poll_interval": 30,
        "timeout": 15,
        "sonarcloud": SonarCloudConfig(project_key="org_repo"),
    }
    defaults.update(kwargs)
    return ExternalReviewsConfig(**defaults)


class TestExternalReviewsConfig:
    def test_defaults(self) -> None:
        cfg = ExternalReviewsConfig()
        assert cfg.enabled is False
        assert cfg.tools == []
        assert cfg.poll_interval == 30
        assert cfg.timeout == 15

    def test_enabled_with_tools(self) -> None:
        cfg = _ext_config()
        assert cfg.enabled is True
        assert cfg.tools == ["sonarcloud", "coderabbit"]
        assert cfg.sonarcloud.project_key == "org_repo"

    def test_nested_in_project_config(self) -> None:
        cfg = ProjectConfig(external_reviews=_ext_config())
        assert cfg.external_reviews.enabled is True
        assert cfg.external_reviews.tools == ["sonarcloud", "coderabbit"]


class TestConfigLoading:
    def test_load_from_toml_with_external_reviews(self, tmp_path: Path) -> None:
        from sova.config.loader import load_config

        toml = '[project]\ngithub_repo = "user/repo"\n\n'
        toml += '[external_reviews]\nenabled = true\ntools = ["sonarcloud", "coderabbit"]\n'
        toml += "poll_interval = 20\ntimeout = 10\n\n"
        toml += '[external_reviews.sonarcloud]\nproject_key = "user_repo"\n'
        (tmp_path / "sova.toml").write_text(toml)
        cfg = load_config(tmp_path)

        assert cfg.external_reviews.enabled is True
        assert cfg.external_reviews.tools == ["sonarcloud", "coderabbit"]
        assert cfg.external_reviews.poll_interval == 20
        assert cfg.external_reviews.sonarcloud.project_key == "user_repo"

    def test_load_from_toml_without_external_reviews(self, tmp_path: Path) -> None:
        from sova.config.loader import load_config

        (tmp_path / "sova.toml").write_text('[project]\ngithub_repo = "user/repo"\n')
        cfg = load_config(tmp_path)
        assert cfg.external_reviews.enabled is False
        assert cfg.external_reviews.tools == []


class TestGetCheckStatuses:
    async def test_returns_statuses_for_tools(self) -> None:
        rollup = {
            "statusCheckRollup": [
                {"name": "SonarCloud Code Analysis", "status": "COMPLETED", "conclusion": "SUCCESS"},
                {"context": "CodeRabbit", "state": "SUCCESS"},
            ]
        }
        with (
            patch("sova.adapters.external_reviews.run", new_callable=AsyncMock) as mock_run,
            patch("sova.utils.gh.run", new_callable=AsyncMock, return_value=_shell_result()),
        ):
            mock_run.return_value = _shell_result(stdout=json.dumps(rollup))
            statuses = await get_check_statuses(1, repo="o/r", tools=["sonarcloud", "coderabbit"])
        assert len(statuses) == 2
        assert statuses[0].completed is True
        assert statuses[1].passed is True

    async def test_missing_check_returns_incomplete(self) -> None:
        with (
            patch("sova.adapters.external_reviews.run", new_callable=AsyncMock) as mock_run,
            patch("sova.utils.gh.run", new_callable=AsyncMock, return_value=_shell_result()),
        ):
            mock_run.return_value = _shell_result(stdout='{"statusCheckRollup": []}')
            statuses = await get_check_statuses(1, repo="o/r", tools=["sonarcloud"])
        assert len(statuses) == 1
        assert statuses[0].completed is False

    async def test_failed_gh_returns_empty(self) -> None:
        with (
            patch("sova.adapters.external_reviews.run", new_callable=AsyncMock) as mock_run,
            patch("sova.utils.gh.run", new_callable=AsyncMock, return_value=_shell_result()),
        ):
            mock_run.return_value = _shell_result(returncode=1, stderr="error")
            statuses = await get_check_statuses(1, repo="o/r", tools=["sonarcloud"])
        assert statuses == []


class TestFetchSonarCloudIssues:
    async def test_parses_issues(self) -> None:
        response = {
            "issues": [
                {
                    "key": "AZ123",
                    "component": "org_repo:sova/core/dag.py",
                    "line": 42,
                    "severity": "MAJOR",
                    "message": "Remove this unused import.",
                },
                {
                    "key": "AZ456",
                    "component": "org_repo:sova/dashboard/app.py",
                    "severity": "MINOR",
                    "message": "Add a docstring.",
                },
            ]
        }
        with patch("sova.adapters.external_reviews.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _shell_result(stdout=json.dumps(response))
            findings = await fetch_sonarcloud_issues("org_repo", 94)
        assert len(findings) == 2
        assert findings[0].file_path == "sova/core/dag.py"
        assert findings[0].line == 42
        assert findings[1].line is None

    async def test_curl_failure(self) -> None:
        with patch("sova.adapters.external_reviews.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _shell_result(returncode=1, stderr="timeout")
            assert await fetch_sonarcloud_issues("org_repo", 94) == []


_GQL_RESPONSE = {
    "data": {
        "repository": {
            "pullRequest": {
                "reviewThreads": {
                    "nodes": [
                        {
                            "id": "PRRT_abc",
                            "isResolved": False,
                            "path": "sova/core/dag.py",
                            "line": 10,
                            "comments": {"nodes": [{"body": "Bug found.", "author": {"login": "coderabbitai"}}]},
                        },
                        {
                            "id": "PRRT_resolved",
                            "isResolved": True,
                            "path": "sova/cli/app.py",
                            "line": 5,
                            "comments": {"nodes": [{"body": "Fixed.", "author": {"login": "coderabbitai"}}]},
                        },
                        {
                            "id": "PRRT_human",
                            "isResolved": False,
                            "path": "sova/config/models.py",
                            "line": 20,
                            "comments": {"nodes": [{"body": "Human comment.", "author": {"login": "xsovad06"}}]},
                        },
                    ]
                }
            }
        }
    }
}


class TestFetchCodeRabbitFindings:
    async def test_filters_unresolved_coderabbit_threads(self) -> None:
        with (
            patch("sova.adapters.external_reviews.run", new_callable=AsyncMock) as mock_run,
            patch("sova.utils.gh.run", new_callable=AsyncMock, return_value=_shell_result()),
        ):
            mock_run.return_value = _shell_result(stdout=json.dumps(_GQL_RESPONSE))
            findings = await fetch_coderabbit_findings("owner/repo", 94)
        assert len(findings) == 1
        assert findings[0].file_path == "sova/core/dag.py"
        assert findings[0].tool_id == "PRRT_abc"

    async def test_returns_thread_ids(self) -> None:
        with (
            patch("sova.adapters.external_reviews.run", new_callable=AsyncMock) as mock_run,
            patch("sova.utils.gh.run", new_callable=AsyncMock, return_value=_shell_result()),
        ):
            mock_run.return_value = _shell_result(stdout=json.dumps(_GQL_RESPONSE))
            result = await _fetch_coderabbit_threads("owner/repo", 94)
        assert result.thread_ids == ["PRRT_abc"]


class TestFormatFindings:
    def test_formats_grouped_by_source(self) -> None:
        findings = [
            ExternalFinding("sonarcloud", "sova/app.py", 10, "MAJOR", "Unused import"),
            ExternalFinding("coderabbit", "sova/cli.py", 20, "MAJOR", "Missing type hint"),
        ]
        result = format_findings_for_prompt(findings)
        assert "SONARCLOUD" in result
        assert "CODERABBIT" in result
        assert "sova/app.py:10" in result

    def test_empty_findings_returns_empty(self) -> None:
        assert format_findings_for_prompt([]) == ""


class TestWaitForExternalReviewsStep:
    async def test_can_skip_when_disabled(self) -> None:
        from sova.core.steps.wait_for_external_reviews import WaitForExternalReviewsStep

        cfg = ProjectConfig(external_reviews=ExternalReviewsConfig(enabled=False))
        ctx = _make_ctx(config=cfg)
        assert await WaitForExternalReviewsStep().can_skip(ctx) is True

    async def test_can_skip_when_no_tools(self) -> None:
        from sova.core.steps.wait_for_external_reviews import WaitForExternalReviewsStep

        cfg = ProjectConfig(external_reviews=ExternalReviewsConfig(enabled=True, tools=[]))
        ctx = _make_ctx(config=cfg)
        assert await WaitForExternalReviewsStep().can_skip(ctx) is True

    async def test_cannot_skip_when_enabled(self) -> None:
        from sova.core.steps.wait_for_external_reviews import WaitForExternalReviewsStep

        cfg = ProjectConfig(external_reviews=_ext_config())
        ctx = _make_ctx(config=cfg)
        assert await WaitForExternalReviewsStep().can_skip(ctx) is False

    async def test_execute_returns_success_when_all_complete(self) -> None:
        from sova.core.steps.wait_for_external_reviews import WaitForExternalReviewsStep

        cfg = ProjectConfig(external_reviews=_ext_config())
        ctx = _make_ctx(config=cfg, pr_number=94)
        completed = [
            ExternalCheckStatus("SonarCloud Code Analysis", completed=True, passed=True),
            ExternalCheckStatus("CodeRabbit", completed=True, passed=True),
        ]
        with patch(
            "sova.core.steps.wait_for_external_reviews.get_check_statuses",
            new_callable=AsyncMock,
            return_value=completed,
        ):
            result = await WaitForExternalReviewsStep().execute(ctx)
        assert result.success
        assert "completed" in result.summary

    async def test_execute_times_out_gracefully(self) -> None:
        from sova.core.steps.wait_for_external_reviews import WaitForExternalReviewsStep

        ext = _ext_config(timeout=1, poll_interval=1)
        cfg = ProjectConfig(external_reviews=ext)
        ctx = _make_ctx(config=cfg, pr_number=94)
        pending = [ExternalCheckStatus("SonarCloud Code Analysis", completed=False, passed=False)]
        with (
            patch(
                "sova.core.steps.wait_for_external_reviews.get_check_statuses",
                new_callable=AsyncMock,
                return_value=pending,
            ),
            patch("sova.core.steps.wait_for_external_reviews.asyncio.sleep", new_callable=AsyncMock),
        ):
            result = await WaitForExternalReviewsStep().execute(ctx)
        assert result.success
        assert "timed out" in result.summary

    async def test_execute_fails_without_pr(self) -> None:
        from sova.core.steps.wait_for_external_reviews import WaitForExternalReviewsStep

        cfg = ProjectConfig(external_reviews=_ext_config())
        ctx = _make_ctx(config=cfg, pr_number=None)
        result = await WaitForExternalReviewsStep().execute(ctx)
        assert not result.success


class TestAddressExternalFindingsStep:
    async def test_can_skip_when_disabled(self) -> None:
        from sova.core.steps.address_external_findings import AddressExternalFindingsStep

        cfg = ProjectConfig(external_reviews=ExternalReviewsConfig(enabled=False))
        ctx = _make_ctx(config=cfg)
        assert await AddressExternalFindingsStep().can_skip(ctx) is True

    async def test_execute_no_findings(self) -> None:
        from sova.core.steps.address_external_findings import AddressExternalFindingsStep

        cfg = ProjectConfig(external_reviews=_ext_config())
        ctx = _make_ctx(config=cfg, pr_number=94)
        with (
            patch(
                "sova.adapters.external_reviews.fetch_sonarcloud_issues",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch(
                "sova.adapters.external_reviews._fetch_coderabbit_threads",
                new_callable=AsyncMock,
                return_value=_ThreadsResult(),
            ),
        ):
            result = await AddressExternalFindingsStep().execute(ctx)
        assert result.success
        assert "No external findings" in result.summary

    async def test_execute_with_findings(self) -> None:
        from sova.core.steps.address_external_findings import AddressExternalFindingsStep

        cfg = ProjectConfig(external_reviews=_ext_config())
        ctx = _make_ctx(config=cfg, pr_number=94, branch_name="feat/test")
        sonar = [ExternalFinding("sonarcloud", "sova/app.py", 10, "MAJOR", "Unused import", "AZ123")]
        cr = _ThreadsResult(
            findings=[ExternalFinding("coderabbit", "sova/cli.py", 20, "MAJOR", "Bug", "PRRT_abc")],
            thread_ids=["PRRT_abc"],
        )
        mock_llm = AsyncMock()
        mock_llm.cost_usd = Decimal("0.05")
        with (
            patch(
                "sova.adapters.external_reviews.fetch_sonarcloud_issues",
                new_callable=AsyncMock,
                return_value=sonar,
            ),
            patch(
                "sova.adapters.external_reviews._fetch_coderabbit_threads",
                new_callable=AsyncMock,
                return_value=cr,
            ),
            patch(
                "sova.llm.client.invoke",
                new_callable=AsyncMock,
                return_value=mock_llm,
            ),
            patch(
                "sova.core.steps.address_external_findings.run",
                new_callable=AsyncMock,
                return_value=_shell_result(),
            ),
            patch("sova.git.operations.commit", new_callable=AsyncMock),
            patch("sova.git.operations.push", new_callable=AsyncMock),
            patch(
                "sova.adapters.external_reviews.resolve_coderabbit_threads",
                new_callable=AsyncMock,
            ) as mock_resolve,
        ):
            result = await AddressExternalFindingsStep().execute(ctx)
        assert result.success
        assert "2 external finding" in result.summary
        mock_resolve.assert_awaited_once()

    async def test_execute_fails_without_pr(self) -> None:
        from sova.core.steps.address_external_findings import AddressExternalFindingsStep

        cfg = ProjectConfig(external_reviews=_ext_config())
        ctx = _make_ctx(config=cfg, pr_number=None)
        result = await AddressExternalFindingsStep().execute(ctx)
        assert not result.success


class TestResolveCodeRabbitThreads:
    async def test_calls_graphql_for_each_thread(self) -> None:
        from sova.adapters.external_reviews import resolve_coderabbit_threads

        with (
            patch("sova.adapters.external_reviews.run", new_callable=AsyncMock) as mock_run,
            patch("sova.utils.gh.run", new_callable=AsyncMock, return_value=_shell_result()),
        ):
            mock_run.return_value = _shell_result(stdout='{"data":{}}')
            await resolve_coderabbit_threads(["PRRT_a", "PRRT_b"])
        assert mock_run.call_count == 2
