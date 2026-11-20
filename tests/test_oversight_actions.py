"""Tests for sova.oversight.actions: issue creation from oversight findings."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from structlog.testing import capture_logs

from sova.adapters.base import Task, TaskState
from sova.config.models import OversightConfig
from sova.db.models import OversightFinding
from sova.oversight.actions import (
    _FOOTER,
    _find_existing_issue_by_fingerprint,
    _is_issue_open,
    _issue_body,
    _issue_labels,
    _persist_issue_numbers,
    _select_adapter,
    propose_issues,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_finding(
    *,
    title: str = "Test finding",
    scope: str = "global",
    severity: str = "warning",
    confidence: float = 0.8,
    description: str = "Something is wrong",
    recommendation: str = "Fix it",
    project_slug: str = "",
    github_issue_number: int | None = None,
    dismissed: bool = False,
) -> OversightFinding:
    return OversightFinding(
        run_id="run-1",
        title=title,
        scope=scope,
        severity=severity,
        confidence=confidence,
        description=description,
        recommendation=recommendation,
        project_slug=project_slug,
        github_issue_number=github_issue_number,
        dismissed=dismissed,
    )


def _mock_adapter(*, repo: str = "owner/repo") -> AsyncMock:
    adapter = AsyncMock()
    adapter.repo = repo
    adapter.create_issue = AsyncMock(return_value=Task(id="42", title="Created", state=TaskState.BACKLOG))
    adapter.get_task = AsyncMock(return_value=Task(id="42", title="Existing", state=TaskState.BACKLOG))
    return adapter


# ---------------------------------------------------------------------------
# Unit tests: label and body helpers
# ---------------------------------------------------------------------------


class TestIssueLabels:
    def test_critical_gets_bug_and_high_priority(self) -> None:
        finding = _make_finding(severity="critical")
        labels = _issue_labels(finding)
        assert "type: bug" in labels
        assert "agent:triaged" in labels
        assert "priority: high" in labels

    def test_warning_gets_task_and_medium_priority(self) -> None:
        labels = _issue_labels(_make_finding(severity="warning"))
        assert "type: task" in labels
        assert "priority: medium" in labels

    def test_info_gets_feature_and_low_priority(self) -> None:
        labels = _issue_labels(_make_finding(severity="info"))
        assert "type: feature" in labels
        assert "priority: low" in labels

    def test_unknown_severity_defaults_to_feature_and_medium(self) -> None:
        labels = _issue_labels(_make_finding(severity="unknown"))
        assert "type: feature" in labels
        assert "priority: medium" in labels


class TestIssueBody:
    def test_body_contains_description_and_recommendation(self) -> None:
        finding = _make_finding(description="Desc here", recommendation="Do this")
        body = _issue_body(finding)
        assert "Desc here" in body
        assert "## Recommendation" in body
        assert "Do this" in body
        assert _FOOTER in body

    def test_body_without_recommendation(self) -> None:
        finding = _make_finding(description="Only desc", recommendation="")
        body = _issue_body(finding)
        assert "Only desc" in body
        assert "## Recommendation" not in body
        assert _FOOTER in body

    def test_body_without_description(self) -> None:
        finding = _make_finding(description="", recommendation="Only rec")
        body = _issue_body(finding)
        assert "Only rec" in body
        assert _FOOTER in body


# ---------------------------------------------------------------------------
# Adapter selection
# ---------------------------------------------------------------------------


class TestSelectAdapter:
    def test_global_returns_sova_adapter(self) -> None:
        sova = _mock_adapter()
        project = _mock_adapter()
        finding = _make_finding(scope="global")
        result = _select_adapter(finding, sova, {"proj": project})
        assert result is sova

    def test_local_returns_project_adapter(self) -> None:
        sova = _mock_adapter()
        project = _mock_adapter()
        finding = _make_finding(scope="project", project_slug="proj")
        result = _select_adapter(finding, sova, {"proj": project})
        assert result is project

    def test_local_missing_project_returns_none(self) -> None:
        sova = _mock_adapter()
        finding = _make_finding(scope="project", project_slug="missing")
        result = _select_adapter(finding, sova, {})
        assert result is None

    def test_local_empty_slug_returns_none(self) -> None:
        sova = _mock_adapter()
        finding = _make_finding(scope="project", project_slug="")
        result = _select_adapter(finding, sova, {})
        assert result is None


# ---------------------------------------------------------------------------
# propose_issues integration
# ---------------------------------------------------------------------------


class TestProposeIssues:
    @pytest.mark.asyncio
    async def test_disabled_returns_empty(self) -> None:
        cfg = OversightConfig(auto_create_issues=False)
        finding = _make_finding(confidence=0.9)
        result = await propose_issues([finding], cfg, _mock_adapter(), {})
        assert result == []

    @pytest.mark.asyncio
    async def test_creates_issue_for_high_confidence_finding(self) -> None:
        cfg = OversightConfig(auto_create_issues=True)
        sova = _mock_adapter()
        finding = _make_finding(confidence=0.9, scope="global")

        with patch("sova.oversight.actions._persist_issue_numbers", new_callable=AsyncMock):
            result = await propose_issues([finding], cfg, sova, {})

        assert len(result) == 1
        assert result[0].github_issue_number == 42
        sova.create_issue.assert_awaited_once()
        call_kwargs = sova.create_issue.call_args
        assert "agent:triaged" in call_kwargs.kwargs.get("labels", call_kwargs[1].get("labels", []))

    @pytest.mark.asyncio
    async def test_skips_low_confidence(self) -> None:
        cfg = OversightConfig(auto_create_issues=True)
        sova = _mock_adapter()
        finding = _make_finding(confidence=0.3)

        result = await propose_issues([finding], cfg, sova, {})

        assert result == []
        sova.create_issue.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_dismissed(self) -> None:
        cfg = OversightConfig(auto_create_issues=True)
        sova = _mock_adapter()
        finding = _make_finding(confidence=0.9, dismissed=True)

        result = await propose_issues([finding], cfg, sova, {})

        assert result == []
        sova.create_issue.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_existing_open_issue(self) -> None:
        cfg = OversightConfig(auto_create_issues=True)
        sova = _mock_adapter()
        sova.get_task = AsyncMock(return_value=Task(id="10", title="Open", state=TaskState.BACKLOG))
        finding = _make_finding(confidence=0.9, github_issue_number=10)

        result = await propose_issues([finding], cfg, sova, {})

        assert result == []
        sova.create_issue.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_recreates_for_closed_issue(self) -> None:
        cfg = OversightConfig(auto_create_issues=True)
        sova = _mock_adapter()
        sova.get_task = AsyncMock(return_value=Task(id="10", title="Closed", state=TaskState.DONE))
        finding = _make_finding(confidence=0.9, github_issue_number=10)

        with patch("sova.oversight.actions._persist_issue_numbers", new_callable=AsyncMock):
            result = await propose_issues([finding], cfg, sova, {})

        assert len(result) == 1
        sova.create_issue.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_routes_local_finding_to_project_adapter(self) -> None:
        cfg = OversightConfig(auto_create_issues=True)
        sova = _mock_adapter()
        proj = _mock_adapter()
        finding = _make_finding(confidence=0.9, scope="project", project_slug="myproj")

        with patch("sova.oversight.actions._persist_issue_numbers", new_callable=AsyncMock):
            result = await propose_issues([finding], cfg, sova, {"myproj": proj})

        assert len(result) == 1
        proj.create_issue.assert_awaited_once()
        sova.create_issue.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_local_without_adapter(self) -> None:
        cfg = OversightConfig(auto_create_issues=True)
        sova = _mock_adapter()
        finding = _make_finding(confidence=0.9, scope="project", project_slug="missing")

        result = await propose_issues([finding], cfg, sova, {})

        assert result == []

    @pytest.mark.asyncio
    async def test_create_failure_is_non_fatal(self) -> None:
        cfg = OversightConfig(auto_create_issues=True)
        sova = _mock_adapter()
        sova.create_issue = AsyncMock(side_effect=RuntimeError("API error"))
        finding = _make_finding(confidence=0.9)

        result = await propose_issues([finding], cfg, sova, {})

        assert result == []

    @pytest.mark.asyncio
    async def test_multiple_findings_mixed(self) -> None:
        cfg = OversightConfig(auto_create_issues=True)
        sova = _mock_adapter()

        high = _make_finding(title="High", confidence=0.9)
        low = _make_finding(title="Low", confidence=0.3)
        dismissed = _make_finding(title="Dismissed", confidence=0.9, dismissed=True)

        with patch("sova.oversight.actions._persist_issue_numbers", new_callable=AsyncMock):
            result = await propose_issues([high, low, dismissed], cfg, sova, {})

        assert len(result) == 1
        assert result[0].title == "High"

    @pytest.mark.asyncio
    async def test_exact_threshold_included(self) -> None:
        cfg = OversightConfig(auto_create_issues=True)
        sova = _mock_adapter()
        finding = _make_finding(confidence=0.7)

        with patch("sova.oversight.actions._persist_issue_numbers", new_callable=AsyncMock):
            result = await propose_issues([finding], cfg, sova, {}, confidence_threshold=0.7)

        assert len(result) == 1


# ---------------------------------------------------------------------------
# _is_issue_open edge cases
# ---------------------------------------------------------------------------


class TestIsIssueOpen:
    @pytest.mark.asyncio
    async def test_exception_returns_true(self) -> None:
        """Tracker errors are treated as 'still open' to suppress duplicate creation."""
        adapter = _mock_adapter()
        adapter.get_task = AsyncMock(side_effect=RuntimeError("API failure"))
        result = await _is_issue_open(adapter, 99)
        assert result is True


# ---------------------------------------------------------------------------
# _persist_issue_numbers
# ---------------------------------------------------------------------------


class TestPersistIssueNumbers:
    @pytest.mark.asyncio
    async def test_persist_writes_to_db(self) -> None:
        finding = _make_finding(confidence=0.9)
        finding.github_issue_number = 42

        # Use a distinct object from merge so the assignment is exercised
        persisted = _make_finding(confidence=0.9)
        persisted.github_issue_number = None

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_begin = AsyncMock()
        mock_begin.__aenter__ = AsyncMock(return_value=mock_begin)
        mock_begin.__aexit__ = AsyncMock(return_value=False)
        mock_session.begin = lambda: mock_begin
        mock_session.merge = AsyncMock(return_value=persisted)

        async def _fake_get_session(*_a, **_kw):
            return mock_session

        with patch("sova.oversight.actions.get_session", side_effect=_fake_get_session):
            await _persist_issue_numbers([finding])

        mock_session.merge.assert_awaited_once_with(finding)
        assert persisted.github_issue_number == 42

    @pytest.mark.asyncio
    async def test_persist_db_failure_is_non_fatal(self) -> None:
        finding = _make_finding(confidence=0.9)
        finding.github_issue_number = 42

        with (
            patch("sova.oversight.actions.get_session", side_effect=RuntimeError("DB gone")),
            capture_logs() as logs,
        ):
            await _persist_issue_numbers([finding])

        errors = [e for e in logs if e["log_level"] == "error"]
        assert [e["event"] for e in errors] == ["oversight.actions.persist_failed"]
        assert errors[0]["count"] == 1


# ---------------------------------------------------------------------------
# Database scope: every helper writes to the project the caller names
# ---------------------------------------------------------------------------


class TestProjectDirScope:
    @pytest.mark.asyncio
    async def test_propose_issues_scopes_dedup_and_persist_to_project_dir(self, tmp_path: Path) -> None:
        cfg = OversightConfig(auto_create_issues=True)
        sova = _mock_adapter()
        finding = _make_finding(confidence=0.9)

        with (
            patch(
                "sova.oversight.actions._find_existing_issue_by_fingerprint",
                new_callable=AsyncMock,
                return_value=None,
            ) as find,
            patch("sova.oversight.actions._persist_issue_numbers", new_callable=AsyncMock) as persist,
        ):
            await propose_issues([finding], cfg, sova, {}, project_dir=tmp_path)

        assert find.await_args.args[1] == tmp_path
        assert persist.await_args.args[1] == tmp_path

    @pytest.mark.asyncio
    async def test_fingerprint_lookup_opens_the_named_project_session(self, tmp_path: Path) -> None:
        calls: list[dict] = []

        async def _get_session(**kwargs: object) -> None:
            calls.append(kwargs)
            raise RuntimeError("stop here")

        with patch("sova.oversight.actions.get_session", side_effect=_get_session):
            assert await _find_existing_issue_by_fingerprint("abc", tmp_path) is None

        assert calls == [{"project_dir": tmp_path}]

    @pytest.mark.asyncio
    async def test_persist_opens_the_named_project_session(self, tmp_path: Path) -> None:
        calls: list[dict] = []

        async def _get_session(**kwargs: object) -> None:
            calls.append(kwargs)
            raise RuntimeError("stop here")

        finding = _make_finding(confidence=0.9)
        with patch("sova.oversight.actions.get_session", side_effect=_get_session):
            await _persist_issue_numbers([finding], tmp_path)

        assert calls == [{"project_dir": tmp_path}]
