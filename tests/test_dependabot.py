"""Tests for Dependabot auto-merge: detection, CI polling, merge/close, sweep."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from sova.config.models import DependabotConfig, NotificationConfig
from sova.supervisor.dependabot import (
    DependabotMonitor,
    DependabotPR,
    MergeResult,
    _detect_group,
    _detect_major_bump,
    _parse_dependabot_pr,
    _should_skip,
    _wait_for_ci,
    classify_dependabot_prs,
    is_dependabot_pr,
    sweep_dependabot_prs,
)


@pytest.fixture(autouse=True)
def _clear_rate_limit_trackers():
    from sova.supervisor import github_quota

    github_quota._trackers.clear()
    yield
    github_quota._trackers.clear()


# ---------------------------------------------------------------------------
# Detection helpers
# ---------------------------------------------------------------------------


class TestIsDependabotPR:
    def test_dependabot_bot_login(self) -> None:
        pr = {"author": {"login": "dependabot[bot]"}}
        assert is_dependabot_pr(pr) is True

    def test_app_dependabot_login(self) -> None:
        pr = {"author": {"login": "app/dependabot"}}
        assert is_dependabot_pr(pr) is True

    def test_human_author(self) -> None:
        pr = {"author": {"login": "xsovad06"}}
        assert is_dependabot_pr(pr) is False

    def test_missing_author(self) -> None:
        pr = {"author": None}
        assert is_dependabot_pr(pr) is False

    def test_empty_pr(self) -> None:
        pr: dict = {}
        assert is_dependabot_pr(pr) is False

    def test_case_insensitive(self) -> None:
        pr = {"author": {"login": "Dependabot[bot]"}}
        assert is_dependabot_pr(pr) is True


class TestDetectMajorBump:
    def test_major_bump_whole_numbers(self) -> None:
        assert _detect_major_bump("bump actions/checkout from 4 to 7") is True

    def test_minor_bump(self) -> None:
        assert _detect_major_bump("bump actions/checkout from 4.1.0 to 4.2.0") is False

    def test_patch_bump(self) -> None:
        assert _detect_major_bump("bump pytest from 8.3.1 to 8.3.2") is False

    def test_major_in_group_title(self) -> None:
        assert _detect_major_bump("bump the testing group: django from 5.2 to 6.0") is True

    def test_no_version_pattern(self) -> None:
        assert _detect_major_bump("update requirements") is False

    def test_update_style(self) -> None:
        assert _detect_major_bump("update whitenoise requirement from 6.7 to 7.0") is True

    def test_same_major_different_minor(self) -> None:
        assert _detect_major_bump("bump from 2.17.0 to 2.18.1") is False

    def test_comparator_range_major_bump(self) -> None:
        assert _detect_major_bump("update whitenoise requirement from <7.0,>=6.7 to >=7.0,<8.0") is True

    def test_comparator_range_same_major(self) -> None:
        assert _detect_major_bump("update django requirement from >=5.2,<6.0 to >=5.3,<6.0") is False

    def test_tilde_comparator_major_bump(self) -> None:
        assert _detect_major_bump("update django requirement from ~=5.2 to ~=6.0") is True

    def test_major_bump_in_body(self) -> None:
        title = "bump the django-ecosystem group with 3 updates"
        body = "Updates `django` from 5.2 to 6.0\nUpdates `celery` from 5.3 to 5.4"
        assert _detect_major_bump(title, body) is True

    def test_no_major_bump_in_body(self) -> None:
        title = "bump the testing group with 2 updates"
        body = "Updates `pytest` from 8.3.1 to 8.3.2\nUpdates `ruff` from 0.15.0 to 0.16.0"
        assert _detect_major_bump(title, body) is False


class TestDetectGroup:
    def test_production_dependencies(self) -> None:
        assert _detect_group("bump the production-dependencies group with 6 updates") == "production-dependencies"

    def test_django_ecosystem(self) -> None:
        assert _detect_group("bump the django-ecosystem group with 3 updates") == "django-ecosystem"

    def test_github_actions(self) -> None:
        assert _detect_group("bump the github-actions group with 2 updates") == "github-actions"

    def test_testing(self) -> None:
        assert _detect_group("bump the testing group with 6 updates") == "testing"

    def test_ungrouped(self) -> None:
        assert _detect_group("bump actions/checkout from 4 to 5") == ""

    def test_single_package(self) -> None:
        assert _detect_group("update whitenoise requirement from <7.0,>=6.7 to >=6.12.0,<7.0") == ""


class TestParseDependabotPR:
    def test_full_parse(self) -> None:
        pr = {
            "number": 400,
            "title": "chore(infra): bump the production-dependencies group with 6 updates",
            "url": "https://github.com/owner/repo/pull/400",
            "labels": [{"name": "type: infra"}, {"name": "dependabot:approved"}],
            "author": {"login": "dependabot[bot]"},
        }
        result = _parse_dependabot_pr(pr)
        assert result.number == 400
        assert result.group == "production-dependencies"
        assert result.has_major_bump is False
        assert "type: infra" in result.labels
        assert "dependabot:approved" in result.labels

    def test_major_bump_detected(self) -> None:
        pr = {
            "number": 401,
            "title": "chore(infra): bump django from 5.2 to 6.0",
            "url": "",
            "labels": [],
        }
        result = _parse_dependabot_pr(pr)
        assert result.has_major_bump is True
        assert result.group == ""

    def test_labels_as_strings(self) -> None:
        pr = {
            "number": 402,
            "title": "test",
            "url": "",
            "labels": ["type: infra"],
        }
        result = _parse_dependabot_pr(pr)
        assert result.labels == ["type: infra"]

    def test_major_bump_from_body(self) -> None:
        pr = {
            "number": 403,
            "title": "bump the django-ecosystem group with 3 updates",
            "body": "Updates `django` from 5.2 to 6.0",
            "url": "",
            "labels": [],
        }
        result = _parse_dependabot_pr(pr)
        assert result.has_major_bump is True
        assert result.group == "django-ecosystem"


class TestShouldSkip:
    def _cfg(self, **overrides: object) -> DependabotConfig:
        defaults: dict = {
            "enabled": True,
            "auto_merge_groups": ["github-actions", "testing", "production-dependencies"],
            "require_approval_groups": ["django-ecosystem"],
            "approval_label": "dependabot:approved",
        }
        defaults.update(overrides)
        return DependabotConfig(**defaults)

    def test_major_bump_skipped(self) -> None:
        pr = DependabotPR(
            number=1, title="bump from 5.0 to 6.0", url="", labels=[], group="testing", has_major_bump=True
        )
        assert _should_skip(pr, self._cfg()) == "major version bump detected"

    def test_approval_required_without_label(self) -> None:
        pr = DependabotPR(
            number=2,
            title="bump the django-ecosystem group",
            url="",
            labels=[],
            group="django-ecosystem",
            has_major_bump=False,
        )
        reason = _should_skip(pr, self._cfg())
        assert "requires" in reason
        assert "dependabot:approved" in reason

    def test_approval_required_with_label(self) -> None:
        pr = DependabotPR(
            number=3,
            title="bump the django-ecosystem group",
            url="",
            labels=["dependabot:approved"],
            group="django-ecosystem",
            has_major_bump=False,
        )
        assert _should_skip(pr, self._cfg()) == ""

    def test_auto_merge_group_passes(self) -> None:
        pr = DependabotPR(
            number=4,
            title="bump the testing group",
            url="",
            labels=[],
            group="testing",
            has_major_bump=False,
        )
        assert _should_skip(pr, self._cfg()) == ""

    def test_ungrouped_pr_passes(self) -> None:
        pr = DependabotPR(
            number=5,
            title="bump pytest",
            url="",
            labels=[],
            group="",
            has_major_bump=False,
        )
        assert _should_skip(pr, self._cfg()) == ""

    def test_group_not_in_auto_merge_groups(self) -> None:
        pr = DependabotPR(
            number=6,
            title="bump the unknown-group group",
            url="",
            labels=[],
            group="unknown-group",
            has_major_bump=False,
        )
        reason = _should_skip(pr, self._cfg())
        assert "not in auto_merge_groups" in reason

    def test_empty_auto_merge_groups_allows_all(self) -> None:
        pr = DependabotPR(
            number=7,
            title="bump the anything group",
            url="",
            labels=[],
            group="anything",
            has_major_bump=False,
        )
        assert _should_skip(pr, self._cfg(auto_merge_groups=[])) == ""


# ---------------------------------------------------------------------------
# classify_dependabot_prs
# ---------------------------------------------------------------------------


class TestClassifyDependabotPrs:
    def test_filters_non_dependabot(self) -> None:
        prs = [
            {"number": 1, "title": "human PR", "author": {"login": "xsovad06"}, "url": "", "labels": []},
            {"number": 2, "title": "bump pytest", "author": {"login": "dependabot[bot]"}, "url": "", "labels": []},
        ]
        result = classify_dependabot_prs(prs, DependabotConfig(enabled=True))
        assert len(result) == 1
        assert result[0][0].number == 2

    def test_returns_skip_reason(self) -> None:
        prs = [
            {
                "number": 1,
                "title": "bump django from 5.0 to 6.0",
                "author": {"login": "dependabot[bot]"},
                "url": "",
                "labels": [],
            },
        ]
        result = classify_dependabot_prs(prs, DependabotConfig(enabled=True))
        assert result[0][1] == "major version bump detected"


# ---------------------------------------------------------------------------
# CI polling
# ---------------------------------------------------------------------------


class TestWaitForCI:
    @pytest.mark.asyncio
    async def test_ci_passes(self) -> None:
        from sova.git.pr import CheckConclusion, CheckStatus, CICheck

        checks = [
            CICheck(name="lint", status=CheckStatus.COMPLETED, conclusion=CheckConclusion.SUCCESS, details_url=""),
            CICheck(name="test", status=CheckStatus.COMPLETED, conclusion=CheckConclusion.SUCCESS, details_url=""),
        ]
        with patch("sova.git.pr.get_ci_checks", new_callable=AsyncMock, return_value=checks):
            result = await _wait_for_ci(1, repo="o/r", github_user="u", poll_interval=1, timeout=10)
        assert result == "passed"

    @pytest.mark.asyncio
    async def test_ci_fails(self) -> None:
        from sova.git.pr import CheckConclusion, CheckStatus, CICheck

        checks = [
            CICheck(name="lint", status=CheckStatus.COMPLETED, conclusion=CheckConclusion.SUCCESS, details_url=""),
            CICheck(name="test", status=CheckStatus.COMPLETED, conclusion=CheckConclusion.FAILURE, details_url=""),
        ]
        with patch("sova.git.pr.get_ci_checks", new_callable=AsyncMock, return_value=checks):
            result = await _wait_for_ci(1, repo="o/r", github_user="u", poll_interval=1, timeout=10)
        assert result == "failed"

    @pytest.mark.asyncio
    async def test_no_checks_after_grace_period(self) -> None:
        with patch("sova.git.pr.get_ci_checks", new_callable=AsyncMock, return_value=[]):
            result = await _wait_for_ci(
                1, repo="o/r", github_user="u", poll_interval=1, timeout=10, no_checks_grace_period=0
            )
        assert result == "passed"

    @pytest.mark.asyncio
    async def test_no_checks_within_grace_period_keeps_polling(self) -> None:
        from sova.git.pr import CheckConclusion, CheckStatus, CICheck

        checks_sequence = [
            [],
            [CICheck(name="lint", status=CheckStatus.COMPLETED, conclusion=CheckConclusion.SUCCESS, details_url="")],
        ]
        with patch("sova.git.pr.get_ci_checks", new_callable=AsyncMock, side_effect=checks_sequence):
            result = await _wait_for_ci(
                1, repo="o/r", github_user="u", poll_interval=0, timeout=10, no_checks_grace_period=60
            )
        assert result == "passed"

    @pytest.mark.asyncio
    async def test_fetch_error(self) -> None:
        with patch("sova.git.pr.get_ci_checks", new_callable=AsyncMock, return_value=None):
            result = await _wait_for_ci(1, repo="o/r", github_user="u", poll_interval=1, timeout=10)
        assert result == "error"

    @pytest.mark.asyncio
    async def test_skipped_checks_pass(self) -> None:
        from sova.git.pr import CheckConclusion, CheckStatus, CICheck

        checks = [
            CICheck(name="lint", status=CheckStatus.COMPLETED, conclusion=CheckConclusion.SUCCESS, details_url=""),
            CICheck(name="skip", status=CheckStatus.COMPLETED, conclusion=CheckConclusion.SKIPPED, details_url=""),
        ]
        with patch("sova.git.pr.get_ci_checks", new_callable=AsyncMock, return_value=checks):
            result = await _wait_for_ci(1, repo="o/r", github_user="u", poll_interval=1, timeout=10)
        assert result == "passed"

    @pytest.mark.asyncio
    async def test_timeout(self) -> None:
        from sova.git.pr import CheckStatus, CICheck

        checks = [CICheck(name="slow", status=CheckStatus.IN_PROGRESS, conclusion=None, details_url="")]
        with patch("sova.git.pr.get_ci_checks", new_callable=AsyncMock, return_value=checks) as mock_checks:
            result = await _wait_for_ci(1, repo="o/r", github_user="u", poll_interval=0, timeout=0.05)
        assert result == "timeout"
        assert mock_checks.await_count >= 1

    @pytest.mark.asyncio
    async def test_cancelled_conclusion_retries(self) -> None:
        from sova.git.pr import CheckConclusion, CheckStatus, CICheck

        cancelled = [
            CICheck(name="build", status=CheckStatus.COMPLETED, conclusion=CheckConclusion.CANCELLED, details_url="")
        ]
        with patch("sova.git.pr.get_ci_checks", new_callable=AsyncMock, return_value=cancelled):
            result = await _wait_for_ci(1, repo="o/r", github_user="u", poll_interval=0, timeout=0.05)
        assert result == "timeout"


# ---------------------------------------------------------------------------
# Merge / close operations
# ---------------------------------------------------------------------------


class TestMergePR:
    @pytest.mark.asyncio
    async def test_merge_confirmed(self) -> None:
        from sova.supervisor.dependabot import _merge_pr
        from sova.utils.shell import ShellResult

        mock_result = ShellResult(returncode=0, stdout="", stderr="")
        with (
            patch("sova.utils.gh.resolve_gh_env", new_callable=AsyncMock, return_value=None),
            patch("sova.utils.shell.run", new_callable=AsyncMock, return_value=mock_result),
            patch("sova.supervisor.github_quota.track_rate_limit"),
            patch("sova.supervisor.dependabot._check_pr_state", new_callable=AsyncMock, return_value="MERGED"),
        ):
            assert await _merge_pr(42, repo="o/r", github_user="u") == "merged"

    @pytest.mark.asyncio
    async def test_merge_pending(self) -> None:
        from sova.supervisor.dependabot import _merge_pr
        from sova.utils.shell import ShellResult

        mock_result = ShellResult(returncode=0, stdout="", stderr="")
        with (
            patch("sova.utils.gh.resolve_gh_env", new_callable=AsyncMock, return_value=None),
            patch("sova.utils.shell.run", new_callable=AsyncMock, return_value=mock_result),
            patch("sova.supervisor.github_quota.track_rate_limit"),
            patch("sova.supervisor.dependabot._check_pr_state", new_callable=AsyncMock, return_value="OPEN"),
        ):
            assert await _merge_pr(42, repo="o/r", github_user="u") == "pending"

    @pytest.mark.asyncio
    async def test_merge_failure(self) -> None:
        from sova.supervisor.dependabot import _merge_pr
        from sova.utils.shell import ShellResult

        mock_result = ShellResult(returncode=1, stdout="", stderr="merge failed")
        with (
            patch("sova.utils.gh.resolve_gh_env", new_callable=AsyncMock, return_value=None),
            patch("sova.utils.shell.run", new_callable=AsyncMock, return_value=mock_result),
            patch("sova.supervisor.github_quota.track_rate_limit"),
        ):
            assert await _merge_pr(42, repo="o/r", github_user="u") == "error"


class TestClosePR:
    @pytest.mark.asyncio
    async def test_close_success(self) -> None:
        from sova.supervisor.dependabot import _close_pr_with_comment
        from sova.utils.shell import ShellResult

        mock_result = ShellResult(returncode=0, stdout="", stderr="")
        with (
            patch("sova.utils.gh.resolve_gh_env", new_callable=AsyncMock, return_value=None),
            patch("sova.utils.shell.run", new_callable=AsyncMock, return_value=mock_result),
            patch("sova.supervisor.github_quota.track_rate_limit"),
        ):
            assert await _close_pr_with_comment(42, repo="o/r", github_user="u", reason="CI failed") is True


# ---------------------------------------------------------------------------
# Sweep
# ---------------------------------------------------------------------------


class TestSweep:
    def _make_gh_pr(
        self,
        number: int,
        title: str = "bump pytest from 8.3.1 to 8.3.2",
        author: str = "dependabot[bot]",
        labels: list[str] | None = None,
    ) -> dict:
        return {
            "number": number,
            "title": title,
            "url": f"https://github.com/o/r/pull/{number}",
            "author": {"login": author},
            "labels": [{"name": lbl} for lbl in (labels or [])],
        }

    @pytest.mark.asyncio
    async def test_sweep_no_prs(self) -> None:
        with patch("sova.git.pr.list_open_prs", new_callable=AsyncMock, return_value=[]):
            results = await sweep_dependabot_prs(
                project_dir=Path("/tmp"),
                repo="o/r",
                github_user="u",
                config=DependabotConfig(enabled=True),
            )
        assert results == []

    @pytest.mark.asyncio
    async def test_sweep_filters_non_dependabot(self) -> None:
        prs = [
            self._make_gh_pr(1, author="xsovad06"),
            self._make_gh_pr(2, author="dependabot[bot]"),
        ]

        async def _mock_process(pr, **kwargs) -> MergeResult:
            return MergeResult(pr.number, pr.title, "merged")

        with (
            patch("sova.git.pr.list_open_prs", new_callable=AsyncMock, return_value=prs),
            patch("sova.supervisor.dependabot._process_pr", side_effect=_mock_process),
        ):
            results = await sweep_dependabot_prs(
                project_dir=Path("/tmp"),
                repo="o/r",
                github_user="u",
                config=DependabotConfig(enabled=True),
            )
        assert len(results) == 1
        assert results[0].pr_number == 2

    @pytest.mark.asyncio
    async def test_sweep_skips_major_bumps(self) -> None:
        prs = [self._make_gh_pr(1, title="bump django from 5.2 to 6.0")]

        with (
            patch("sova.git.pr.list_open_prs", new_callable=AsyncMock, return_value=prs),
            patch("sova.supervisor.dependabot._wait_for_ci", new_callable=AsyncMock),
            patch("sova.supervisor.dependabot._merge_pr", new_callable=AsyncMock),
        ):
            results = await sweep_dependabot_prs(
                project_dir=Path("/tmp"),
                repo="o/r",
                github_user="u",
                config=DependabotConfig(enabled=True),
            )
        assert results[0].action == "skipped"
        assert "major version bump" in results[0].reason

    @pytest.mark.asyncio
    async def test_sweep_rate_limited(self) -> None:
        from sova.supervisor.github_quota import get_github_quota_tracker

        tracker = get_github_quota_tracker("u")
        tracker._in_cooldown = True

        results = await sweep_dependabot_prs(
            project_dir=Path("/tmp"),
            repo="o/r",
            github_user="u",
            config=DependabotConfig(enabled=True),
        )
        assert results == []

    @pytest.mark.asyncio
    async def test_sweep_disabled_returns_empty(self) -> None:
        results = await sweep_dependabot_prs(
            project_dir=Path("/tmp"),
            repo="o/r",
            github_user="u",
            config=DependabotConfig(enabled=False),
        )
        assert results == []


# ---------------------------------------------------------------------------
# Monitor dataclass
# ---------------------------------------------------------------------------


class TestDependabotMonitor:
    def test_constructor(self) -> None:
        monitor = DependabotMonitor(
            project_dir=Path("/tmp"),
            config=DependabotConfig(enabled=True),
            notification_config=NotificationConfig(),
            repo="o/r",
            github_user="u",
        )
        assert monitor.repo == "o/r"
        assert monitor.last_results == []


# ---------------------------------------------------------------------------
# Config model
# ---------------------------------------------------------------------------


class TestDependabotConfig:
    def test_defaults(self) -> None:
        cfg = DependabotConfig()
        assert cfg.enabled is False
        assert cfg.poll_interval_seconds == 3600
        assert cfg.ci_poll_interval_seconds == 60
        assert cfg.ci_poll_timeout_seconds == 1800
        assert cfg.approval_label == "dependabot:approved"
        assert "django-ecosystem" in cfg.require_approval_groups
        assert "github-actions" in cfg.auto_merge_groups

    def test_custom_values(self) -> None:
        cfg = DependabotConfig(
            enabled=True,
            poll_interval_seconds=600,
            auto_merge_groups=["all"],
            require_approval_groups=[],
        )
        assert cfg.enabled is True
        assert cfg.poll_interval_seconds == 600
        assert cfg.auto_merge_groups == ["all"]
        assert cfg.require_approval_groups == []


# ---------------------------------------------------------------------------
# HTTP endpoint tests
# ---------------------------------------------------------------------------


class TestDependabotEndpoints:
    @pytest.fixture
    def _app(self):
        from fastapi import FastAPI

        from sova.dashboard.routers.dependabot import router

        app = FastAPI()
        app.include_router(router, prefix="/api")
        return app

    @pytest.fixture
    async def client(self, _app):
        import httpx

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_app),
            base_url="http://testserver",
        ) as ac:
            yield ac

    @pytest.fixture(autouse=True)
    def _reset_sweep_state(self):
        import sova.dashboard.routers.dependabot as dep_router

        dep_router._sweep_states.clear()
        yield
        dep_router._sweep_states.clear()

    @pytest.mark.asyncio
    async def test_status_endpoint(self, client) -> None:
        with patch("sova.dashboard.routers.dependabot.load_config") as mock_cfg:
            mock_cfg.return_value.dependabot = DependabotConfig(enabled=True)
            resp = await client.get("/api/dependabot/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["enabled"] is True
        assert "poll_interval_seconds" in data

    @pytest.mark.asyncio
    async def test_prs_endpoint_no_repo(self, client) -> None:
        with patch("sova.dashboard.routers.dependabot.load_config") as mock_cfg:
            mock_cfg.return_value.github_repo = ""
            resp = await client.get("/api/dependabot/prs")
        assert resp.status_code == 503

    @pytest.mark.asyncio
    async def test_prs_endpoint_success(self, client) -> None:
        with (
            patch("sova.dashboard.routers.dependabot.load_config") as mock_cfg,
            patch("sova.git.pr.list_open_prs", new_callable=AsyncMock, return_value=[]),
        ):
            mock_cfg.return_value.github_repo = "o/r"
            mock_cfg.return_value.github_user = "u"
            mock_cfg.return_value.dependabot = DependabotConfig(enabled=True)
            resp = await client.get("/api/dependabot/prs")
        assert resp.status_code == 200
        assert resp.json()["total"] == 0

    @pytest.mark.asyncio
    async def test_sweep_endpoint_returns_202(self, client) -> None:
        with (
            patch("sova.dashboard.routers.dependabot.load_config") as mock_cfg,
            patch(
                "sova.supervisor.dependabot.sweep_dependabot_prs",
                new_callable=AsyncMock,
                return_value=[],
            ),
        ):
            mock_cfg.return_value.github_repo = "o/r"
            mock_cfg.return_value.github_user = "u"
            mock_cfg.return_value.dependabot = DependabotConfig(enabled=True)
            mock_cfg.return_value.notification = NotificationConfig()
            resp = await client.post("/api/dependabot/sweep")
        assert resp.status_code == 202
        assert resp.json()["status"] == "started"

    @pytest.mark.asyncio
    async def test_sweep_results_endpoint(self, client) -> None:
        resp = await client.get("/api/dependabot/sweep/results")
        assert resp.status_code == 200
        assert "status" in resp.json()

    @pytest.mark.asyncio
    async def test_sweep_results_shows_error(self, client) -> None:
        import sova.dashboard.routers.dependabot as dep_router

        project_key = str(Path.cwd())
        state = dep_router._get_sweep_state(project_key)
        state.failed = True
        state.last_results = None
        with patch("sova.dashboard.routers.dependabot.get_project_dir", return_value=Path.cwd()):
            resp = await client.get("/api/dependabot/sweep/results")
        assert resp.status_code == 200
        assert resp.json()["status"] == "error"

    @pytest.mark.asyncio
    async def test_sweep_results_with_data(self, client) -> None:
        import sova.dashboard.routers.dependabot as dep_router

        project_key = str(Path.cwd())
        state = dep_router._get_sweep_state(project_key)
        state.failed = False
        state.last_results = [
            {"pr_number": 100, "title": "bump pytest", "action": "merged", "reason": ""},
            {"pr_number": 101, "title": "bump ruff", "action": "skipped", "reason": "major"},
        ]
        with patch("sova.dashboard.routers.dependabot.get_project_dir", return_value=Path.cwd()):
            resp = await client.get("/api/dependabot/sweep/results")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "complete"
        assert data["summary"]["merged"] == 1
        assert data["summary"]["skipped"] == 1
        assert data["summary"]["total"] == 2

    @pytest.mark.asyncio
    async def test_sweep_returns_already_running(self, client) -> None:
        import sova.dashboard.routers.dependabot as dep_router

        project_dir = Path.cwd()
        project_key = str(project_dir)
        state = dep_router._get_sweep_state(project_key)
        await state.lock.acquire()
        try:
            with (
                patch("sova.dashboard.routers.dependabot.load_config") as mock_cfg,
                patch("sova.dashboard.routers.dependabot.get_project_dir", return_value=project_dir),
            ):
                mock_cfg.return_value.github_repo = "o/r"
                resp = await client.post("/api/dependabot/sweep")
            assert resp.status_code == 202
            assert resp.json()["status"] == "already_running"
        finally:
            state.lock.release()

    @pytest.mark.asyncio
    async def test_sweep_stale_lock_recovers(self, client) -> None:
        import sova.dashboard.routers.dependabot as dep_router

        project_dir = Path.cwd()
        project_key = str(project_dir)
        state = dep_router._get_sweep_state(project_key)
        await state.lock.acquire()
        state.acquired_at = 1.0  # far in the past (monotonic time near zero)

        try:
            with (
                patch("sova.dashboard.routers.dependabot.load_config") as mock_cfg,
                patch("sova.dashboard.routers.dependabot.get_project_dir", return_value=project_dir),
                patch(
                    "sova.supervisor.dependabot.sweep_dependabot_prs",
                    new_callable=AsyncMock,
                    return_value=[],
                ),
            ):
                mock_cfg.return_value.github_repo = "o/r"
                mock_cfg.return_value.github_user = "u"
                mock_cfg.return_value.dependabot = DependabotConfig(enabled=True, ci_poll_timeout_seconds=1)
                mock_cfg.return_value.notification = NotificationConfig()
                resp = await client.post("/api/dependabot/sweep")
            assert resp.status_code == 202
            assert resp.json()["status"] == "started"
        finally:
            if state.lock.locked():
                state.lock.release()

    @pytest.mark.asyncio
    async def test_prs_endpoint_fetch_error(self, client) -> None:
        with (
            patch("sova.dashboard.routers.dependabot.load_config") as mock_cfg,
            patch("sova.git.pr.list_open_prs", new_callable=AsyncMock, side_effect=RuntimeError("API down")),
        ):
            mock_cfg.return_value.github_repo = "o/r"
            mock_cfg.return_value.github_user = "u"
            resp = await client.get("/api/dependabot/prs")
        assert resp.status_code == 500

    @pytest.mark.asyncio
    async def test_sweep_no_repo(self, client) -> None:
        with patch("sova.dashboard.routers.dependabot.load_config") as mock_cfg:
            mock_cfg.return_value.github_repo = ""
            resp = await client.post("/api/dependabot/sweep")
        assert resp.status_code == 503


# ---------------------------------------------------------------------------
# _process_pr integration
# ---------------------------------------------------------------------------


class TestProcessPR:
    def _make_pr(self, **kwargs) -> DependabotPR:
        defaults = {
            "number": 100,
            "title": "bump pytest from 8.3.1 to 8.3.2",
            "url": "https://github.com/o/r/pull/100",
            "labels": [],
            "group": "testing",
            "has_major_bump": False,
        }
        defaults.update(kwargs)
        return DependabotPR(**defaults)

    @pytest.mark.asyncio
    async def test_process_pr_ci_passed_merged(self) -> None:
        from sova.supervisor.dependabot import _process_pr

        pr = self._make_pr()
        with (
            patch("sova.supervisor.dependabot._wait_for_ci", new_callable=AsyncMock, return_value="passed"),
            patch("sova.supervisor.dependabot._merge_pr", new_callable=AsyncMock, return_value="merged"),
            patch("sova.supervisor.dependabot._record_dependabot_cost", new_callable=AsyncMock),
            patch("sova.supervisor.dependabot._emit_feed_event"),
            patch("sova.supervisor.dependabot._notify_action"),
        ):
            result = await _process_pr(
                pr,
                repo="o/r",
                github_user="u",
                config=DependabotConfig(enabled=True, auto_merge_groups=["testing"]),
                project_dir=Path("/tmp/test"),  # noqa: S108
            )
        assert result.action == "merged"

    @pytest.mark.asyncio
    async def test_process_pr_ci_passed_pending(self) -> None:
        from sova.supervisor.dependabot import _process_pr

        pr = self._make_pr()
        with (
            patch("sova.supervisor.dependabot._wait_for_ci", new_callable=AsyncMock, return_value="passed"),
            patch("sova.supervisor.dependabot._merge_pr", new_callable=AsyncMock, return_value="pending"),
        ):
            result = await _process_pr(
                pr,
                repo="o/r",
                github_user="u",
                config=DependabotConfig(enabled=True, auto_merge_groups=["testing"]),
                project_dir=Path("/tmp/test"),  # noqa: S108
            )
        assert result.action == "waiting"
        assert "auto-merge" in result.reason

    @pytest.mark.asyncio
    async def test_process_pr_ci_passed_merge_error(self) -> None:
        from sova.supervisor.dependabot import _process_pr

        pr = self._make_pr()
        with (
            patch("sova.supervisor.dependabot._wait_for_ci", new_callable=AsyncMock, return_value="passed"),
            patch("sova.supervisor.dependabot._merge_pr", new_callable=AsyncMock, return_value="error"),
        ):
            result = await _process_pr(
                pr,
                repo="o/r",
                github_user="u",
                config=DependabotConfig(enabled=True, auto_merge_groups=["testing"]),
                project_dir=Path("/tmp/test"),  # noqa: S108
            )
        assert result.action == "error"
        assert "merge command failed" in result.reason

    @pytest.mark.asyncio
    async def test_process_pr_ci_failed_closed(self) -> None:
        from sova.supervisor.dependabot import _process_pr

        pr = self._make_pr()
        with (
            patch("sova.supervisor.dependabot._wait_for_ci", new_callable=AsyncMock, return_value="failed"),
            patch("sova.supervisor.dependabot._close_pr_with_comment", new_callable=AsyncMock, return_value=True),
            patch("sova.supervisor.dependabot._record_dependabot_cost", new_callable=AsyncMock),
            patch("sova.supervisor.dependabot._emit_feed_event"),
            patch("sova.supervisor.dependabot._notify_action"),
        ):
            result = await _process_pr(
                pr,
                repo="o/r",
                github_user="u",
                config=DependabotConfig(enabled=True, auto_merge_groups=["testing"]),
                project_dir=Path("/tmp/test"),  # noqa: S108
            )
        assert result.action == "closed"
        assert "CI failed" in result.reason

    @pytest.mark.asyncio
    async def test_process_pr_ci_failed_close_error(self) -> None:
        from sova.supervisor.dependabot import _process_pr

        pr = self._make_pr()
        with (
            patch("sova.supervisor.dependabot._wait_for_ci", new_callable=AsyncMock, return_value="failed"),
            patch("sova.supervisor.dependabot._close_pr_with_comment", new_callable=AsyncMock, return_value=False),
        ):
            result = await _process_pr(
                pr,
                repo="o/r",
                github_user="u",
                config=DependabotConfig(enabled=True, auto_merge_groups=["testing"]),
                project_dir=Path("/tmp/test"),  # noqa: S108
            )
        assert result.action == "error"
        assert "close command failed" in result.reason

    @pytest.mark.asyncio
    async def test_process_pr_ci_timeout(self) -> None:
        from sova.supervisor.dependabot import _process_pr

        pr = self._make_pr()
        with patch("sova.supervisor.dependabot._wait_for_ci", new_callable=AsyncMock, return_value="timeout"):
            result = await _process_pr(
                pr,
                repo="o/r",
                github_user="u",
                config=DependabotConfig(enabled=True, auto_merge_groups=["testing"]),
                project_dir=Path("/tmp/test"),  # noqa: S108
            )
        assert result.action == "waiting"
        assert "timed out" in result.reason

    @pytest.mark.asyncio
    async def test_process_pr_ci_error(self) -> None:
        from sova.supervisor.dependabot import _process_pr

        pr = self._make_pr()
        with patch("sova.supervisor.dependabot._wait_for_ci", new_callable=AsyncMock, return_value="error"):
            result = await _process_pr(
                pr,
                repo="o/r",
                github_user="u",
                config=DependabotConfig(enabled=True, auto_merge_groups=["testing"]),
                project_dir=Path("/tmp/test"),  # noqa: S108
            )
        assert result.action == "error"
        assert "error" in result.reason

    @pytest.mark.asyncio
    async def test_process_pr_skipped(self) -> None:
        from sova.supervisor.dependabot import _process_pr

        pr = self._make_pr(has_major_bump=True)
        result = await _process_pr(
            pr,
            repo="o/r",
            github_user="u",
            config=DependabotConfig(enabled=True),
            project_dir=Path("/tmp/test"),
        )
        assert result.action == "skipped"
        assert "major version bump" in result.reason


# ---------------------------------------------------------------------------
# Feed + notification helpers
# ---------------------------------------------------------------------------


class TestFeedAndNotify:
    def test_emit_feed_event_merged(self) -> None:
        from sova.supervisor.dependabot import _emit_feed_event

        with patch("sova.dashboard.services.feed_service.emit_safe") as mock_emit:
            _emit_feed_event(42, "bump pytest", "merged", repo="o/r")
            mock_emit.assert_called_once()
            args, kwargs = mock_emit.call_args
            assert "auto-merged" in args[0]
            assert kwargs["category"] == "dependabot"

    def test_emit_feed_event_exception_swallowed(self) -> None:
        from sova.supervisor.dependabot import _emit_feed_event

        with patch("sova.dashboard.services.feed_service.emit_safe", side_effect=RuntimeError("boom")):
            _emit_feed_event(42, "bump pytest", "merged", repo="o/r")

    def test_notify_action_with_config(self) -> None:
        from sova.supervisor.dependabot import _notify_action

        with patch("sova.ipc.notifications.notify") as mock_notify:
            _notify_action(42, "bump pytest", "merged", config=NotificationConfig())
            mock_notify.assert_called_once()

    def test_notify_action_none_config(self) -> None:
        from sova.supervisor.dependabot import _notify_action

        _notify_action(42, "bump pytest", "merged", config=None)

    def test_notify_action_exception_swallowed(self) -> None:
        from sova.supervisor.dependabot import _notify_action

        with patch("sova.ipc.notifications.notify", side_effect=RuntimeError("boom")):
            _notify_action(42, "bump pytest", "merged", config=NotificationConfig())


# ---------------------------------------------------------------------------
# _check_pr_state
# ---------------------------------------------------------------------------


class TestCheckPRState:
    @pytest.mark.asyncio
    async def test_returns_merged(self) -> None:
        from sova.supervisor.dependabot import _check_pr_state
        from sova.utils.shell import ShellResult

        result = ShellResult(returncode=0, stdout="MERGED\n", stderr="")
        with (
            patch("sova.utils.gh.resolve_gh_env", new_callable=AsyncMock, return_value=None),
            patch("sova.utils.shell.run", new_callable=AsyncMock, return_value=result),
        ):
            assert await _check_pr_state(42, repo="o/r", github_user="u") == "MERGED"

    @pytest.mark.asyncio
    async def test_returns_unknown_on_failure(self) -> None:
        from sova.supervisor.dependabot import _check_pr_state
        from sova.utils.shell import ShellResult

        result = ShellResult(returncode=1, stdout="", stderr="err")
        with (
            patch("sova.utils.gh.resolve_gh_env", new_callable=AsyncMock, return_value=None),
            patch("sova.utils.shell.run", new_callable=AsyncMock, return_value=result),
        ):
            assert await _check_pr_state(42, repo="o/r", github_user="u") == "unknown"


# ---------------------------------------------------------------------------
# _record_dependabot_cost
# ---------------------------------------------------------------------------


class TestRecordDependabotCost:
    @pytest.mark.asyncio
    async def test_records_cost(self) -> None:
        from sova.supervisor.dependabot import _record_dependabot_cost

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        with patch("sova.db.session.get_session", new_callable=AsyncMock, return_value=mock_session):
            await _record_dependabot_cost(42, "merged", project_dir=Path("/tmp/test"))
        mock_session.add.assert_called_once()
        mock_session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_handles_db_error(self) -> None:
        from sova.supervisor.dependabot import _record_dependabot_cost

        with patch("sova.db.session.get_session", new_callable=AsyncMock, side_effect=RuntimeError("db fail")):
            await _record_dependabot_cost(42, "merged", project_dir=Path("/tmp/test"))


# ---------------------------------------------------------------------------
# create_monitors_for_projects
# ---------------------------------------------------------------------------


class TestCreateMonitorsForProjects:
    def test_creates_monitor_for_enabled_project(self) -> None:
        from sova.supervisor.dependabot import create_monitors_for_projects

        mock_cfg = type(
            "C",
            (),
            {
                "dependabot": DependabotConfig(enabled=True),
                "github_repo": "o/r",
                "github_user": "u",
                "notification": NotificationConfig(),
            },
        )()
        with (
            patch("sova.config.registry.list_projects", return_value={"proj": "/tmp/test"}),
            patch("pathlib.Path.is_dir", return_value=True),
            patch("sova.config.loader.load_config", return_value=mock_cfg),
        ):
            monitors = create_monitors_for_projects()
        assert len(monitors) == 1
        assert monitors[0].repo == "o/r"

    def test_skips_disabled_project(self) -> None:
        from sova.supervisor.dependabot import create_monitors_for_projects

        mock_cfg = type(
            "C",
            (),
            {
                "dependabot": DependabotConfig(enabled=False),
                "github_repo": "o/r",
                "github_user": "u",
                "notification": NotificationConfig(),
            },
        )()
        with (
            patch("sova.config.registry.list_projects", return_value={"proj": "/tmp/test"}),
            patch("pathlib.Path.is_dir", return_value=True),
            patch("sova.config.loader.load_config", return_value=mock_cfg),
        ):
            monitors = create_monitors_for_projects()
        assert len(monitors) == 0

    def test_skips_missing_directory(self) -> None:
        from sova.supervisor.dependabot import create_monitors_for_projects

        with (
            patch("sova.config.registry.list_projects", return_value={"proj": "/tmp/nonexistent"}),
            patch("pathlib.Path.is_dir", return_value=False),
        ):
            monitors = create_monitors_for_projects()
        assert len(monitors) == 0

    def test_handles_config_error(self) -> None:
        from sova.supervisor.dependabot import create_monitors_for_projects

        with (
            patch("sova.config.registry.list_projects", return_value={"proj": "/tmp/test"}),
            patch("pathlib.Path.is_dir", return_value=True),
            patch("sova.config.loader.load_config", side_effect=RuntimeError("bad config")),
        ):
            monitors = create_monitors_for_projects()
        assert len(monitors) == 0


# ---------------------------------------------------------------------------
# DependabotMonitor.run_loop
# ---------------------------------------------------------------------------


class TestDependabotMonitorLoop:
    @pytest.mark.asyncio
    async def test_run_loop_single_cycle(self) -> None:
        monitor = DependabotMonitor(
            project_dir=Path("/tmp/test"),
            config=DependabotConfig(enabled=True, poll_interval_seconds=1),
            notification_config=NotificationConfig(),
            repo="o/r",
            github_user="u",
        )
        results = [MergeResult(1, "bump pytest", "merged")]
        call_count = 0

        async def _mock_sweep(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                raise asyncio.CancelledError
            return results

        with (
            patch("sova.supervisor.dependabot.sweep_dependabot_prs", side_effect=_mock_sweep),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            with pytest.raises(asyncio.CancelledError):
                await monitor.run_loop()
        assert monitor.last_results == results

    @pytest.mark.asyncio
    async def test_run_loop_handles_exception(self) -> None:
        monitor = DependabotMonitor(
            project_dir=Path("/tmp/test"),
            config=DependabotConfig(enabled=True, poll_interval_seconds=1),
            notification_config=NotificationConfig(),
            repo="o/r",
            github_user="u",
        )
        call_count = 0

        async def _mock_sweep(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("transient")
            raise asyncio.CancelledError

        with (
            patch("sova.supervisor.dependabot.sweep_dependabot_prs", side_effect=_mock_sweep),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            with pytest.raises(asyncio.CancelledError):
                await monitor.run_loop()
        assert monitor.last_results == []


# ---------------------------------------------------------------------------
# Sweep with concurrent processing
# ---------------------------------------------------------------------------


class TestSweepConcurrent:
    def _make_gh_pr(self, number: int, title: str = "bump pytest") -> dict:
        return {
            "number": number,
            "title": title,
            "url": f"https://github.com/o/r/pull/{number}",
            "author": {"login": "dependabot[bot]"},
            "labels": [],
        }

    @pytest.mark.asyncio
    async def test_sweep_processes_concurrently(self) -> None:
        prs = [self._make_gh_pr(i) for i in range(5)]

        async def _mock_process(pr, **kwargs) -> MergeResult:
            return MergeResult(pr.number, pr.title, "merged")

        with (
            patch("sova.git.pr.list_open_prs", new_callable=AsyncMock, return_value=prs),
            patch("sova.supervisor.dependabot._process_pr", side_effect=_mock_process),
        ):
            results = await sweep_dependabot_prs(
                project_dir=Path("/tmp/test"),
                repo="o/r",
                github_user="u",
                config=DependabotConfig(enabled=True),
            )
        assert len(results) == 5
        assert all(r.action == "merged" for r in results)

    @pytest.mark.asyncio
    async def test_sweep_handles_process_exception(self) -> None:
        prs = [self._make_gh_pr(1)]

        async def _mock_process(pr, **kwargs) -> MergeResult:
            raise RuntimeError("boom")

        with (
            patch("sova.git.pr.list_open_prs", new_callable=AsyncMock, return_value=prs),
            patch("sova.supervisor.dependabot._process_pr", side_effect=_mock_process),
        ):
            results = await sweep_dependabot_prs(
                project_dir=Path("/tmp/test"),
                repo="o/r",
                github_user="u",
                config=DependabotConfig(enabled=True),
            )
        assert len(results) == 1
        assert results[0].action == "error"
        assert "unhandled exception" in results[0].reason


# ---------------------------------------------------------------------------
# CLI maintain command
# ---------------------------------------------------------------------------


class TestMaintainCLI:
    @pytest.fixture
    def _capture_console(self):
        """Capture Rich console output by replacing the module-level console with a StringIO-backed one."""
        from io import StringIO

        from rich.console import Console

        from sova.cli.commands import maintain

        buf = StringIO()
        original = maintain.console
        maintain.console = Console(file=buf, no_color=True)
        yield buf
        maintain.console = original

    @pytest.mark.asyncio
    async def test_maintain_dry_run_no_prs(self, _capture_console) -> None:
        from sova.cli.commands.maintain import _maintain

        mock_cfg = type(
            "C",
            (),
            {
                "github_repo": "o/r",
                "github_user": "u",
                "dependabot": DependabotConfig(enabled=True),
            },
        )()
        with (
            patch("sova.config.loader.load_config", return_value=mock_cfg),
            patch("sova.git.pr.list_open_prs", new_callable=AsyncMock, return_value=[]),
        ):
            await _maintain(project_dir=Path("/tmp/test"), dry_run=True)  # noqa: S108

        output = _capture_console.getvalue()
        assert "Inspecting" in output
        assert "No open Dependabot PRs" in output

    @pytest.mark.asyncio
    async def test_maintain_dry_run_with_prs(self, _capture_console) -> None:
        from sova.cli.commands.maintain import _maintain

        prs = [
            {
                "number": 10,
                "title": "bump pytest from 8.0 to 8.1",
                "author": {"login": "dependabot[bot]"},
                "url": "",
                "labels": [],
            }
        ]
        mock_cfg = type(
            "C",
            (),
            {
                "github_repo": "o/r",
                "github_user": "u",
                "dependabot": DependabotConfig(enabled=True),
            },
        )()
        with (
            patch("sova.config.loader.load_config", return_value=mock_cfg),
            patch("sova.git.pr.list_open_prs", new_callable=AsyncMock, return_value=prs),
        ):
            await _maintain(project_dir=Path("/tmp/test"), dry_run=True)  # noqa: S108

        output = _capture_console.getvalue()
        assert "dry run" in output.lower()
        assert "#10" in output
        assert "bump pytest" in output

    @pytest.mark.asyncio
    async def test_maintain_dry_run_fetch_error(self, _capture_console) -> None:
        import typer

        from sova.cli.commands.maintain import _maintain

        mock_cfg = type(
            "C",
            (),
            {
                "github_repo": "o/r",
                "github_user": "u",
                "dependabot": DependabotConfig(enabled=True),
            },
        )()
        with (
            patch("sova.config.loader.load_config", return_value=mock_cfg),
            patch("sova.git.pr.list_open_prs", new_callable=AsyncMock, side_effect=RuntimeError("fail")),
            pytest.raises(typer.Exit),
        ):
            await _maintain(project_dir=Path("/tmp/test"), dry_run=True)  # noqa: S108

        output = _capture_console.getvalue()
        assert "Failed to fetch" in output

    @pytest.mark.asyncio
    async def test_maintain_no_repo(self, _capture_console) -> None:
        import typer

        from sova.cli.commands.maintain import _maintain

        mock_cfg = type("C", (), {"github_repo": "", "dependabot": DependabotConfig()})()
        with (
            patch("sova.config.loader.load_config", return_value=mock_cfg),
            pytest.raises(typer.Exit),
        ):
            await _maintain(project_dir=Path("/tmp/test"), dry_run=False)  # noqa: S108

        output = _capture_console.getvalue()
        assert "No github_repo" in output

    @pytest.mark.asyncio
    async def test_maintain_sweep_no_results(self, _capture_console) -> None:
        from sova.cli.commands.maintain import _maintain

        mock_cfg = type(
            "C",
            (),
            {
                "github_repo": "o/r",
                "github_user": "u",
                "dependabot": DependabotConfig(enabled=True),
                "notification": NotificationConfig(),
            },
        )()
        with (
            patch("sova.config.loader.load_config", return_value=mock_cfg),
            patch("sova.supervisor.dependabot.sweep_dependabot_prs", new_callable=AsyncMock, return_value=[]),
        ):
            await _maintain(project_dir=Path("/tmp/test"), dry_run=False)  # noqa: S108

        output = _capture_console.getvalue()
        assert "Sweeping" in output
        assert "No open Dependabot PRs" in output

    @pytest.mark.asyncio
    async def test_maintain_sweep_exits_nonzero_on_errors(self, _capture_console) -> None:
        import typer

        from sova.cli.commands.maintain import _maintain

        results = [
            MergeResult(10, "bump pytest", "merged"),
            MergeResult(11, "bump broken", "error", "merge failed"),
        ]
        mock_cfg = type(
            "C",
            (),
            {
                "github_repo": "o/r",
                "github_user": "u",
                "dependabot": DependabotConfig(enabled=True),
                "notification": NotificationConfig(),
            },
        )()
        with (
            patch("sova.config.loader.load_config", return_value=mock_cfg),
            patch("sova.supervisor.dependabot.sweep_dependabot_prs", new_callable=AsyncMock, return_value=results),
            pytest.raises(typer.Exit) as exc_info,
        ):
            await _maintain(project_dir=Path("/tmp/test"), dry_run=False)  # noqa: S108

        assert exc_info.value.exit_code == 1

    @pytest.mark.asyncio
    async def test_maintain_sweep_with_results(self, _capture_console) -> None:
        import typer

        from sova.cli.commands.maintain import _maintain

        results = [
            MergeResult(10, "bump pytest", "merged"),
            MergeResult(11, "bump ruff", "skipped", "major version bump"),
            MergeResult(12, "bump django", "closed", "CI failed"),
            MergeResult(13, "bump slow", "waiting", "CI timed out"),
            MergeResult(14, "bump broken", "error", "merge failed"),
        ]
        mock_cfg = type(
            "C",
            (),
            {
                "github_repo": "o/r",
                "github_user": "u",
                "dependabot": DependabotConfig(enabled=True),
                "notification": NotificationConfig(),
            },
        )()
        with (
            patch("sova.config.loader.load_config", return_value=mock_cfg),
            patch("sova.supervisor.dependabot.sweep_dependabot_prs", new_callable=AsyncMock, return_value=results),
            pytest.raises(typer.Exit),
        ):
            await _maintain(project_dir=Path("/tmp/test"), dry_run=False)  # noqa: S108

        output = _capture_console.getvalue()
        assert "merged" in output.lower()
        assert "skipped" in output.lower()
        assert "closed" in output.lower()
        assert "waiting" in output.lower()
        assert "error" in output.lower()
        assert "Summary" in output
        assert "1 merged" in output
