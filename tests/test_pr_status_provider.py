"""Tests for PRStatusProvider: cross-project PR awareness provider."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from sova.awareness.base import ItemCategory
from sova.awareness.providers.pr_status import (
    PRStatusProvider,
    _build_merged_item,
    _classify_check_run,
    _classify_pr,
    _classify_status_context,
    _determine_pr_classification,
    _get_author,
    _parse_gh_timestamp,
    _resolve_targets,
    _summarize_ci,
)
from sova.config.models import AwarenessConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _hours_ago_iso(hours: float) -> str:
    dt = datetime.now(timezone.utc) - timedelta(hours=hours)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _days_ago_iso(days: int) -> str:
    dt = datetime.now(timezone.utc) - timedelta(days=days)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _make_pr(
    number: int = 1,
    title: str = "Test PR",
    author: str = "testuser",
    review_decision: str = "",
    ci_checks: list[dict] | None = None,
    updated_at: str | None = None,
    is_draft: bool = False,
    url: str = "",
    labels: list[dict] | None = None,
    review_requests: list[dict] | None = None,
) -> dict:
    return {
        "number": number,
        "title": title,
        "url": url or f"https://github.com/org/repo/pull/{number}",
        "author": {"login": author},
        "reviewDecision": review_decision,
        "isDraft": is_draft,
        "statusCheckRollup": ci_checks or [],
        "updatedAt": updated_at or _now_iso(),
        "labels": labels or [],
        "reviewRequests": review_requests or [],
    }


@dataclass
class FakeShellResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def success(self) -> bool:
        return self.returncode == 0

    @property
    def is_rate_limited(self) -> bool:
        return False


def _ok(data: list | dict) -> FakeShellResult:
    return FakeShellResult(returncode=0, stdout=json.dumps(data), stderr="")


def _fail(msg: str = "error") -> FakeShellResult:
    return FakeShellResult(returncode=1, stdout="", stderr=msg)


# ---------------------------------------------------------------------------
# is_configured
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_is_configured_true() -> None:
    cfg = AwarenessConfig(pr_github_user="testuser")
    provider = PRStatusProvider(cfg)
    assert await provider.is_configured() is True


@pytest.mark.asyncio
async def test_is_configured_false_when_empty() -> None:
    cfg = AwarenessConfig(pr_github_user="")
    provider = PRStatusProvider(cfg)
    assert await provider.is_configured() is False


# ---------------------------------------------------------------------------
# fetch_items: empty cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_items_empty_when_no_user() -> None:
    cfg = AwarenessConfig(pr_github_user="")
    provider = PRStatusProvider(cfg)
    items = await provider.fetch_items()
    assert items == []


@pytest.mark.asyncio
async def test_fetch_items_empty_when_no_projects(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sova.awareness.providers.pr_status.list_projects", lambda: {})
    cfg = AwarenessConfig(pr_github_user="testuser")
    provider = PRStatusProvider(cfg)
    items = await provider.fetch_items()
    assert items == []


@pytest.mark.asyncio
async def test_fetch_items_skips_non_github_projects(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    (project_dir / "sova.toml").write_text("[project]\n")

    monkeypatch.setattr(
        "sova.awareness.providers.pr_status.list_projects",
        lambda: {"proj": str(project_dir)},
    )
    cfg = AwarenessConfig(pr_github_user="testuser")
    provider = PRStatusProvider(cfg)
    items = await provider.fetch_items()
    assert items == []


# ---------------------------------------------------------------------------
# _classify_pr
# ---------------------------------------------------------------------------


class TestClassifyPr:
    """Tests for the per-PR classification logic."""

    def _stale_cutoff(self) -> datetime:
        return datetime.now(timezone.utc) - timedelta(days=7)

    def test_review_requested_needs_attention_high(self) -> None:
        pr = _make_pr(number=42, author="other-user")
        item = _classify_pr("proj", "org/repo", pr, "testuser", {42}, self._stale_cutoff())
        assert item is not None
        assert item.category == ItemCategory.NEEDS_ATTENTION
        assert item.urgency == 2
        assert item.action_hint == "Review PR"
        assert "Review requested" in item.body

    def test_own_pr_failing_ci(self) -> None:
        pr = _make_pr(
            number=10,
            author="testuser",
            ci_checks=[{"conclusion": "FAILURE", "status": "COMPLETED"}],
        )
        item = _classify_pr("proj", "org/repo", pr, "testuser", set(), self._stale_cutoff())
        assert item is not None
        assert item.category == ItemCategory.NEEDS_ATTENTION
        assert item.urgency == 1
        assert item.action_hint == "Fix CI"

    def test_own_pr_changes_requested(self) -> None:
        pr = _make_pr(
            number=11,
            author="testuser",
            review_decision="CHANGES_REQUESTED",
            ci_checks=[{"conclusion": "SUCCESS", "status": "COMPLETED"}],
        )
        item = _classify_pr("proj", "org/repo", pr, "testuser", set(), self._stale_cutoff())
        assert item is not None
        assert item.category == ItemCategory.NEEDS_ATTENTION
        assert item.urgency == 1
        assert item.action_hint == "Address review"

    def test_own_pr_approved_ci_passing(self) -> None:
        pr = _make_pr(
            number=12,
            author="testuser",
            review_decision="APPROVED",
            ci_checks=[{"conclusion": "SUCCESS", "status": "COMPLETED"}],
        )
        item = _classify_pr("proj", "org/repo", pr, "testuser", set(), self._stale_cutoff())
        assert item is not None
        assert item.category == ItemCategory.NEEDS_ATTENTION
        assert item.urgency == 1
        assert item.action_hint == "Merge PR"

    def test_own_pr_ci_passing_informational(self) -> None:
        pr = _make_pr(
            number=13,
            author="testuser",
            ci_checks=[{"conclusion": "SUCCESS", "status": "COMPLETED"}],
        )
        item = _classify_pr("proj", "org/repo", pr, "testuser", set(), self._stale_cutoff())
        assert item is not None
        assert item.category == ItemCategory.INFORMATIONAL
        assert item.urgency == 0

    def test_own_pr_open_informational(self) -> None:
        pr = _make_pr(number=14, author="testuser")
        item = _classify_pr("proj", "org/repo", pr, "testuser", set(), self._stale_cutoff())
        assert item is not None
        assert item.category == ItemCategory.INFORMATIONAL
        assert "open" in item.body
        assert "CI:" in item.body

    def test_other_pr_not_review_requested_returns_none(self) -> None:
        pr = _make_pr(number=20, author="someone-else")
        item = _classify_pr("proj", "org/repo", pr, "testuser", set(), self._stale_cutoff())
        assert item is None

    def test_draft_pr_returns_none(self) -> None:
        pr = _make_pr(number=30, author="testuser", is_draft=True)
        item = _classify_pr("proj", "org/repo", pr, "testuser", set(), self._stale_cutoff())
        assert item is None

    def test_stale_flag_set(self) -> None:
        pr = _make_pr(number=40, author="testuser", updated_at=_days_ago_iso(10))
        item = _classify_pr("proj", "org/repo", pr, "testuser", set(), self._stale_cutoff())
        assert item is not None
        assert item.metadata["stale"] is True

    def test_not_stale(self) -> None:
        pr = _make_pr(number=41, author="testuser", updated_at=_hours_ago_iso(1))
        item = _classify_pr("proj", "org/repo", pr, "testuser", set(), self._stale_cutoff())
        assert item is not None
        assert item.metadata["stale"] is False

    def test_case_insensitive_author_match(self) -> None:
        pr = _make_pr(number=50, author="TestUser")
        item = _classify_pr("proj", "org/repo", pr, "testuser", set(), self._stale_cutoff())
        assert item is not None
        assert item.category == ItemCategory.INFORMATIONAL

    def test_metadata_populated(self) -> None:
        pr = _make_pr(number=60, author="testuser")
        item = _classify_pr("proj", "org/repo", pr, "testuser", set(), self._stale_cutoff())
        assert item is not None
        assert item.metadata["repo"] == "org/repo"
        assert item.metadata["pr_number"] == 60
        assert item.metadata["project"] == "proj"
        assert item.metadata["author"] == "testuser"

    def test_review_requested_takes_priority_over_own_ci_fail(self) -> None:
        """When a PR is both review-requested AND user's own with failing CI,
        review-requested wins (higher urgency)."""
        pr = _make_pr(
            number=70,
            author="testuser",
            ci_checks=[{"conclusion": "FAILURE", "status": "COMPLETED"}],
        )
        item = _classify_pr("proj", "org/repo", pr, "testuser", {70}, self._stale_cutoff())
        assert item is not None
        assert item.urgency == 2
        assert item.action_hint == "Review PR"

    def test_ci_failure_takes_priority_over_changes_requested(self) -> None:
        """When CI is failing AND changes are requested, CI failure wins (checked first)."""
        pr = _make_pr(
            number=71,
            author="testuser",
            review_decision="CHANGES_REQUESTED",
            ci_checks=[{"conclusion": "FAILURE", "status": "COMPLETED"}],
        )
        item = _classify_pr("proj", "org/repo", pr, "testuser", set(), self._stale_cutoff())
        assert item is not None
        assert item.action_hint == "Fix CI"

    def test_missing_number_returns_none(self) -> None:
        pr = {"title": "No number", "author": {"login": "testuser"}}
        item = _classify_pr("proj", "org/repo", pr, "testuser", set(), self._stale_cutoff())
        assert item is None

    def test_zero_number_returns_none(self) -> None:
        pr = _make_pr(number=0, author="testuser")
        item = _classify_pr("proj", "org/repo", pr, "testuser", set(), self._stale_cutoff())
        assert item is None

    def test_negative_number_returns_none(self) -> None:
        pr = _make_pr(number=-1, author="testuser")
        item = _classify_pr("proj", "org/repo", pr, "testuser", set(), self._stale_cutoff())
        assert item is None


# ---------------------------------------------------------------------------
# _build_merged_item
# ---------------------------------------------------------------------------


class TestBuildMergedItem:
    def test_merged_item_created(self) -> None:
        pr = {
            "number": 100,
            "title": "Merged PR",
            "url": "https://github.com/org/repo/pull/100",
            "author": {"login": "testuser"},
            "mergedAt": _hours_ago_iso(2),
        }
        since = datetime.now(timezone.utc) - timedelta(hours=24)
        item = _build_merged_item("proj", "org/repo", pr, since, "testuser")
        assert item is not None
        assert item.category == ItemCategory.INFORMATIONAL
        assert item.metadata["merged"] is True
        assert item.id == "pr:proj:100:merged"

    def test_merged_item_filtered_by_since(self) -> None:
        pr = {
            "number": 101,
            "title": "Old merge",
            "url": "https://github.com/org/repo/pull/101",
            "author": {"login": "testuser"},
            "mergedAt": _days_ago_iso(3),
        }
        since = datetime.now(timezone.utc) - timedelta(hours=24)
        item = _build_merged_item("proj", "org/repo", pr, since, "testuser")
        assert item is None

    def test_merged_item_filtered_by_author(self) -> None:
        pr = {
            "number": 103,
            "title": "Other's merge",
            "url": "https://github.com/org/repo/pull/103",
            "author": {"login": "other-user"},
            "mergedAt": _hours_ago_iso(1),
        }
        since = datetime.now(timezone.utc) - timedelta(hours=24)
        item = _build_merged_item("proj", "org/repo", pr, since, "testuser")
        assert item is None

    def test_merged_item_no_mergedAt(self) -> None:
        pr = {
            "number": 102,
            "title": "No timestamp",
            "url": "https://github.com/org/repo/pull/102",
            "author": {"login": "testuser"},
        }
        since = datetime.now(timezone.utc) - timedelta(hours=24)
        item = _build_merged_item("proj", "org/repo", pr, since, "testuser")
        assert item is None

    def test_merged_item_missing_number(self) -> None:
        pr = {"title": "No number", "author": {"login": "x"}}
        since = datetime.now(timezone.utc) - timedelta(hours=24)
        item = _build_merged_item("proj", "org/repo", pr, since, "x")
        assert item is None


# ---------------------------------------------------------------------------
# _summarize_ci
# ---------------------------------------------------------------------------


class TestSummarizeCi:
    def test_empty_rollup(self) -> None:
        assert _summarize_ci([]) == "none"

    def test_all_success(self) -> None:
        checks = [
            {"conclusion": "SUCCESS", "status": "COMPLETED"},
            {"conclusion": "SUCCESS", "status": "COMPLETED"},
        ]
        assert _summarize_ci(checks) == "passed"

    def test_any_failure(self) -> None:
        checks = [
            {"conclusion": "SUCCESS", "status": "COMPLETED"},
            {"conclusion": "FAILURE", "status": "COMPLETED"},
        ]
        assert _summarize_ci(checks) == "failed"

    def test_pending(self) -> None:
        checks = [
            {"conclusion": "SUCCESS", "status": "COMPLETED"},
            {"conclusion": "", "status": "IN_PROGRESS"},
        ]
        assert _summarize_ci(checks) == "pending"

    def test_skipped_treated_as_passed(self) -> None:
        checks = [{"conclusion": "SKIPPED", "status": "COMPLETED"}]
        assert _summarize_ci(checks) == "passed"

    def test_status_context_success(self) -> None:
        checks = [{"__typename": "StatusContext", "state": "SUCCESS"}]
        assert _summarize_ci(checks) == "passed"

    def test_status_context_failure(self) -> None:
        checks = [{"__typename": "StatusContext", "state": "FAILURE"}]
        assert _summarize_ci(checks) == "failed"

    def test_timed_out_is_failure(self) -> None:
        checks = [{"conclusion": "TIMED_OUT", "status": "COMPLETED"}]
        assert _summarize_ci(checks) == "failed"

    def test_completed_no_conclusion_is_passed(self) -> None:
        checks = [{"conclusion": "", "status": "COMPLETED"}]
        assert _summarize_ci(checks) == "passed"


# ---------------------------------------------------------------------------
# _parse_gh_timestamp
# ---------------------------------------------------------------------------


class TestParseGhTimestamp:
    def test_z_suffix(self) -> None:
        dt = _parse_gh_timestamp("2026-08-20T10:30:00Z")
        assert dt is not None
        assert dt.tzinfo is not None
        assert dt.year == 2026

    def test_offset(self) -> None:
        dt = _parse_gh_timestamp("2026-08-20T10:30:00+00:00")
        assert dt is not None

    def test_none_value(self) -> None:
        assert _parse_gh_timestamp(None) is None

    def test_empty_string(self) -> None:
        assert _parse_gh_timestamp("") is None

    def test_invalid_string(self) -> None:
        assert _parse_gh_timestamp("not-a-date") is None


# ---------------------------------------------------------------------------
# Integration: fetch_items with mocked shell
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_items_integration(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """End-to-end test with mocked shell and config."""
    project_dir = tmp_path / "myproject"
    project_dir.mkdir()
    (project_dir / "sova.toml").write_text('[project]\ngithub_repo = "org/repo"\ngithub_user = "botuser"\n')

    monkeypatch.setattr(
        "sova.awareness.providers.pr_status.list_projects",
        lambda: {"myproj": str(project_dir)},
    )

    open_prs = [
        _make_pr(number=1, author="testuser", ci_checks=[{"conclusion": "SUCCESS", "status": "COMPLETED"}]),
        _make_pr(number=2, author="other", ci_checks=[{"conclusion": "FAILURE", "status": "COMPLETED"}]),
    ]
    review_requested = [{"number": 2}]
    merged_prs = [
        {
            "number": 3,
            "title": "Merged fix",
            "url": "https://github.com/org/repo/pull/3",
            "author": {"login": "testuser"},
            "mergedAt": _hours_ago_iso(1),
        }
    ]

    async def mock_run(*args, env=None, **kwargs):
        cmd = " ".join(str(a) for a in args)
        if "pr list" in cmd and "--state open" in cmd:
            return _ok(open_prs)
        if "search prs" in cmd:
            return _ok(review_requested)
        if "pr list" in cmd and "--state merged" in cmd:
            return _ok(merged_prs)
        return _fail("unexpected command")

    monkeypatch.setattr("sova.awareness.providers.pr_status.run", mock_run)
    monkeypatch.setattr("sova.awareness.providers.pr_status.resolve_gh_env", AsyncMock(return_value={}))

    cfg = AwarenessConfig(pr_github_user="testuser")
    provider = PRStatusProvider(cfg)
    items = await provider.fetch_items()

    assert len(items) == 3

    by_id = {item.id: item for item in items}

    own_pr = by_id["pr:myproj:1"]
    assert own_pr.category == ItemCategory.INFORMATIONAL
    assert own_pr.metadata["ci_status"] == "passed"

    review_pr = by_id["pr:myproj:2"]
    assert review_pr.category == ItemCategory.NEEDS_ATTENTION
    assert review_pr.urgency == 2
    assert review_pr.action_hint == "Review PR"

    merged = by_id["pr:myproj:3:merged"]
    assert merged.category == ItemCategory.INFORMATIONAL
    assert merged.metadata["merged"] is True


@pytest.mark.asyncio
async def test_fetch_items_tolerates_gh_failure(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Provider returns [] when gh CLI calls fail."""
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    (project_dir / "sova.toml").write_text('[project]\ngithub_repo = "org/repo"\ngithub_user = "bot"\n')

    monkeypatch.setattr(
        "sova.awareness.providers.pr_status.list_projects",
        lambda: {"proj": str(project_dir)},
    )

    async def mock_run(*args, env=None, **kwargs):
        return _fail("network error")

    monkeypatch.setattr("sova.awareness.providers.pr_status.run", mock_run)
    monkeypatch.setattr("sova.awareness.providers.pr_status.resolve_gh_env", AsyncMock(return_value={}))

    cfg = AwarenessConfig(pr_github_user="testuser")
    provider = PRStatusProvider(cfg)
    items = await provider.fetch_items()
    assert items == []


@pytest.mark.asyncio
async def test_fetch_items_multiple_projects(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Items from multiple projects are aggregated."""
    for name in ("alpha", "beta"):
        d = tmp_path / name
        d.mkdir()
        (d / "sova.toml").write_text(f'[project]\ngithub_repo = "org/{name}"\ngithub_user = "bot"\n')

    monkeypatch.setattr(
        "sova.awareness.providers.pr_status.list_projects",
        lambda: {"alpha": str(tmp_path / "alpha"), "beta": str(tmp_path / "beta")},
    )

    async def mock_run(*args, env=None, **kwargs):
        cmd = " ".join(str(a) for a in args)
        if "search prs" in cmd:
            return _ok([])
        if "--state merged" in cmd:
            return _ok([])
        if "--state open" in cmd:
            return _ok([_make_pr(number=1, author="testuser")])
        return _fail()

    monkeypatch.setattr("sova.awareness.providers.pr_status.run", mock_run)
    monkeypatch.setattr("sova.awareness.providers.pr_status.resolve_gh_env", AsyncMock(return_value={}))

    cfg = AwarenessConfig(pr_github_user="testuser")
    provider = PRStatusProvider(cfg)
    items = await provider.fetch_items()

    assert len(items) == 2
    projects = {item.metadata["project"] for item in items}
    assert projects == {"alpha", "beta"}


@pytest.mark.asyncio
async def test_fetch_items_config_load_failure(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Projects with broken config are skipped gracefully."""
    project_dir = tmp_path / "broken"
    project_dir.mkdir()
    (project_dir / "sova.toml").write_text("invalid toml {{{{")

    monkeypatch.setattr(
        "sova.awareness.providers.pr_status.list_projects",
        lambda: {"broken": str(project_dir)},
    )

    cfg = AwarenessConfig(pr_github_user="testuser")
    provider = PRStatusProvider(cfg)
    items = await provider.fetch_items()
    assert items == []


# ---------------------------------------------------------------------------
# _classify_status_context
# ---------------------------------------------------------------------------


class TestClassifyStatusContext:
    def test_success(self) -> None:
        assert _classify_status_context({"state": "SUCCESS"}) == "passed"

    def test_failure(self) -> None:
        assert _classify_status_context({"state": "FAILURE"}) == "failed"

    def test_error(self) -> None:
        assert _classify_status_context({"state": "ERROR"}) == "failed"

    def test_pending(self) -> None:
        assert _classify_status_context({"state": "PENDING"}) == "pending"

    def test_unknown_state(self) -> None:
        assert _classify_status_context({"state": "UNKNOWN"}) == "pending"

    def test_none_state(self) -> None:
        assert _classify_status_context({}) == "pending"

    def test_case_insensitive(self) -> None:
        assert _classify_status_context({"state": "success"}) == "passed"


# ---------------------------------------------------------------------------
# _classify_check_run
# ---------------------------------------------------------------------------


class TestClassifyCheckRun:
    def test_failure_conclusion(self) -> None:
        assert _classify_check_run({"conclusion": "FAILURE", "status": "COMPLETED"}) == "failed"

    def test_error_conclusion(self) -> None:
        assert _classify_check_run({"conclusion": "ERROR", "status": "COMPLETED"}) == "failed"

    def test_timed_out(self) -> None:
        assert _classify_check_run({"conclusion": "TIMED_OUT", "status": "COMPLETED"}) == "failed"

    def test_startup_failure(self) -> None:
        assert _classify_check_run({"conclusion": "STARTUP_FAILURE", "status": "COMPLETED"}) == "failed"

    def test_action_required(self) -> None:
        assert _classify_check_run({"conclusion": "ACTION_REQUIRED", "status": "COMPLETED"}) == "failed"

    def test_stale_conclusion(self) -> None:
        assert _classify_check_run({"conclusion": "STALE", "status": "COMPLETED"}) == "failed"

    def test_success_conclusion(self) -> None:
        assert _classify_check_run({"conclusion": "SUCCESS", "status": "COMPLETED"}) == "passed"

    def test_skipped(self) -> None:
        assert _classify_check_run({"conclusion": "SKIPPED", "status": "COMPLETED"}) == "skipped"

    def test_neutral(self) -> None:
        assert _classify_check_run({"conclusion": "NEUTRAL", "status": "COMPLETED"}) == "skipped"

    def test_cancelled(self) -> None:
        assert _classify_check_run({"conclusion": "CANCELLED", "status": "COMPLETED"}) == "skipped"

    def test_completed_no_conclusion(self) -> None:
        assert _classify_check_run({"conclusion": "", "status": "COMPLETED"}) == "passed"

    def test_in_progress(self) -> None:
        assert _classify_check_run({"conclusion": "", "status": "IN_PROGRESS"}) == "pending"

    def test_queued(self) -> None:
        assert _classify_check_run({"conclusion": "", "status": "QUEUED"}) == "pending"

    def test_none_values(self) -> None:
        assert _classify_check_run({}) == "pending"


# ---------------------------------------------------------------------------
# _determine_pr_classification
# ---------------------------------------------------------------------------


class TestDeterminePrClassification:
    def test_review_requested(self) -> None:
        result = _determine_pr_classification(False, True, "passed", "", 1, "other")
        assert result is not None
        cat, urgency, hint, body = result
        assert cat == ItemCategory.NEEDS_ATTENTION
        assert urgency == 2
        assert hint == "Review PR"

    def test_own_ci_failed(self) -> None:
        result = _determine_pr_classification(True, False, "failed", "", 1, "me")
        assert result is not None
        assert result[0] == ItemCategory.NEEDS_ATTENTION
        assert result[2] == "Fix CI"

    def test_own_changes_requested(self) -> None:
        result = _determine_pr_classification(True, False, "passed", "CHANGES_REQUESTED", 1, "me")
        assert result is not None
        assert result[2] == "Address review"

    def test_own_approved_and_passing(self) -> None:
        result = _determine_pr_classification(True, False, "passed", "APPROVED", 1, "me")
        assert result is not None
        assert result[2] == "Merge PR"

    def test_own_ci_passing_awaiting_review(self) -> None:
        result = _determine_pr_classification(True, False, "passed", "", 1, "me")
        assert result is not None
        assert result[0] == ItemCategory.INFORMATIONAL
        assert "awaiting review" in result[3]

    def test_own_open(self) -> None:
        result = _determine_pr_classification(True, False, "none", "", 1, "me")
        assert result is not None
        assert result[0] == ItemCategory.INFORMATIONAL
        assert "open" in result[3]
        assert "CI: none" in result[3]

    def test_not_own_not_requested(self) -> None:
        result = _determine_pr_classification(False, False, "passed", "", 1, "other")
        assert result is None

    def test_review_requested_overrides_own(self) -> None:
        result = _determine_pr_classification(True, True, "failed", "", 1, "me")
        assert result is not None
        assert result[2] == "Review PR"


# ---------------------------------------------------------------------------
# _get_author
# ---------------------------------------------------------------------------


class TestGetAuthor:
    def test_dict_author(self) -> None:
        assert _get_author({"author": {"login": "alice"}}) == "alice"

    def test_missing_login(self) -> None:
        assert _get_author({"author": {}}) == ""

    def test_string_author(self) -> None:
        assert _get_author({"author": "not-a-dict"}) == ""

    def test_no_author_key(self) -> None:
        assert _get_author({}) == ""

    def test_none_author(self) -> None:
        assert _get_author({"author": None}) == ""


# ---------------------------------------------------------------------------
# _resolve_targets
# ---------------------------------------------------------------------------


class TestResolveTargets:
    def test_valid_project(self, tmp_path) -> None:
        d = tmp_path / "proj"
        d.mkdir()
        (d / "sova.toml").write_text('[project]\ngithub_repo = "org/repo"\ngithub_user = "bot"\n')
        targets = _resolve_targets({"proj": str(d)})
        assert len(targets) == 1
        assert targets[0] == ("proj", "org/repo", "bot")

    def test_nonexistent_dir(self, tmp_path) -> None:
        targets = _resolve_targets({"proj": str(tmp_path / "missing")})
        assert targets == []

    def test_no_github_repo(self, tmp_path) -> None:
        d = tmp_path / "proj"
        d.mkdir()
        (d / "sova.toml").write_text("[project]\n")
        targets = _resolve_targets({"proj": str(d)})
        assert targets == []

    def test_broken_config(self, tmp_path) -> None:
        d = tmp_path / "proj"
        d.mkdir()
        (d / "sova.toml").write_text("{{invalid")
        targets = _resolve_targets({"proj": str(d)})
        assert targets == []


# ---------------------------------------------------------------------------
# _summarize_ci: additional edge cases
# ---------------------------------------------------------------------------


class TestSummarizeCiEdgeCases:
    def test_mixed_status_context_and_check_run(self) -> None:
        rollup = [
            {"__typename": "StatusContext", "state": "SUCCESS"},
            {"conclusion": "SUCCESS", "status": "COMPLETED"},
        ]
        assert _summarize_ci(rollup) == "passed"

    def test_status_context_unknown_returns_pending(self) -> None:
        rollup = [{"__typename": "StatusContext", "state": "WEIRD"}]
        assert _summarize_ci(rollup) == "pending"

    def test_neutral_conclusion(self) -> None:
        rollup = [{"conclusion": "NEUTRAL", "status": "COMPLETED"}]
        assert _summarize_ci(rollup) == "passed"

    def test_cancelled_conclusion(self) -> None:
        rollup = [{"conclusion": "CANCELLED", "status": "COMPLETED"}]
        assert _summarize_ci(rollup) == "passed"

    def test_status_context_error(self) -> None:
        rollup = [{"__typename": "StatusContext", "state": "ERROR"}]
        assert _summarize_ci(rollup) == "failed"

    def test_status_context_pending(self) -> None:
        rollup = [{"__typename": "StatusContext", "state": "PENDING"}]
        assert _summarize_ci(rollup) == "pending"

    def test_failure_takes_priority_over_pending(self) -> None:
        rollup = [
            {"conclusion": "FAILURE", "status": "COMPLETED"},
            {"conclusion": "", "status": "IN_PROGRESS"},
        ]
        assert _summarize_ci(rollup) == "failed"

    def test_pending_takes_priority_over_passed(self) -> None:
        rollup = [
            {"conclusion": "SUCCESS", "status": "COMPLETED"},
            {"conclusion": "", "status": "IN_PROGRESS"},
        ]
        assert _summarize_ci(rollup) == "pending"

    def test_startup_failure(self) -> None:
        rollup = [{"conclusion": "STARTUP_FAILURE", "status": "COMPLETED"}]
        assert _summarize_ci(rollup) == "failed"

    def test_action_required(self) -> None:
        rollup = [{"conclusion": "ACTION_REQUIRED", "status": "COMPLETED"}]
        assert _summarize_ci(rollup) == "failed"

    def test_stale_conclusion(self) -> None:
        rollup = [{"conclusion": "STALE", "status": "COMPLETED"}]
        assert _summarize_ci(rollup) == "failed"

    def test_status_context_unknown_treated_as_pending(self) -> None:
        rollup = [{"__typename": "StatusContext", "state": "QUEUED"}]
        assert _summarize_ci(rollup) == "pending"

    def test_only_unknown_status_contexts_is_pending(self) -> None:
        rollup = [
            {"__typename": "StatusContext", "state": "UNKNOWN"},
            {"__typename": "StatusContext", "state": "WEIRD"},
        ]
        assert _summarize_ci(rollup) == "pending"


# ---------------------------------------------------------------------------
# _safe_fetch: timeout and error handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_safe_fetch_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Timeout returns empty list."""
    from sova.awareness.providers.pr_status import _safe_fetch

    async def slow_fetch(*args, **kwargs):
        await asyncio.sleep(100)
        return []

    monkeypatch.setattr("sova.awareness.providers.pr_status._fetch_project_prs", slow_fetch)
    monkeypatch.setattr("sova.awareness.providers.pr_status._FETCH_TIMEOUT", 0.01)

    sem = asyncio.Semaphore(1)
    result = await _safe_fetch("slug", "org/repo", "bot", "user", None, sem)
    assert result == []


@pytest.mark.asyncio
async def test_safe_fetch_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unexpected exception returns empty list."""
    from sova.awareness.providers.pr_status import _safe_fetch

    async def broken_fetch(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("sova.awareness.providers.pr_status._fetch_project_prs", broken_fetch)

    sem = asyncio.Semaphore(1)
    result = await _safe_fetch("slug", "org/repo", "bot", "user", None, sem)
    assert result == []


# ---------------------------------------------------------------------------
# gh CLI error paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_open_prs_parse_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Invalid JSON from gh pr list returns empty list."""
    from sova.awareness.providers.pr_status import _fetch_open_prs

    async def mock_run(*args, env=None, **kwargs):
        return FakeShellResult(returncode=0, stdout="not json", stderr="")

    monkeypatch.setattr("sova.awareness.providers.pr_status.run", mock_run)
    result = await _fetch_open_prs("org/repo", {})
    assert result == []


@pytest.mark.asyncio
async def test_fetch_review_requested_parse_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Invalid JSON from search returns empty set."""
    from sova.awareness.providers.pr_status import _fetch_review_requested

    async def mock_run(*args, env=None, **kwargs):
        return FakeShellResult(returncode=0, stdout="not json", stderr="")

    monkeypatch.setattr("sova.awareness.providers.pr_status.run", mock_run)
    result = await _fetch_review_requested("org/repo", "user", {})
    assert result == set()


@pytest.mark.asyncio
async def test_fetch_review_requested_key_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing 'number' key in search results returns empty set."""
    from sova.awareness.providers.pr_status import _fetch_review_requested

    async def mock_run(*args, env=None, **kwargs):
        return FakeShellResult(returncode=0, stdout='[{"id": 1}]', stderr="")

    monkeypatch.setattr("sova.awareness.providers.pr_status.run", mock_run)
    result = await _fetch_review_requested("org/repo", "user", {})
    assert result == set()


@pytest.mark.asyncio
async def test_fetch_review_requested_command_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Failed search command returns empty set."""
    from sova.awareness.providers.pr_status import _fetch_review_requested

    async def mock_run(*args, env=None, **kwargs):
        return _fail("error")

    monkeypatch.setattr("sova.awareness.providers.pr_status.run", mock_run)
    result = await _fetch_review_requested("org/repo", "user", {})
    assert result == set()


@pytest.mark.asyncio
async def test_fetch_merged_prs_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Failed merged PR fetch returns empty list."""
    from sova.awareness.providers.pr_status import _fetch_merged_prs

    async def mock_run(*args, env=None, **kwargs):
        return _fail("error")

    monkeypatch.setattr("sova.awareness.providers.pr_status.run", mock_run)
    result = await _fetch_merged_prs("org/repo", {})
    assert result == []


@pytest.mark.asyncio
async def test_fetch_merged_prs_parse_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Invalid JSON from merged PR list returns empty list."""
    from sova.awareness.providers.pr_status import _fetch_merged_prs

    async def mock_run(*args, env=None, **kwargs):
        return FakeShellResult(returncode=0, stdout="not json", stderr="")

    monkeypatch.setattr("sova.awareness.providers.pr_status.run", mock_run)
    result = await _fetch_merged_prs("org/repo", {})
    assert result == []


@pytest.mark.asyncio
async def test_fetch_open_prs_command_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Failed open PR list returns empty list."""
    from sova.awareness.providers.pr_status import _fetch_open_prs

    async def mock_run(*args, env=None, **kwargs):
        return _fail("error")

    monkeypatch.setattr("sova.awareness.providers.pr_status.run", mock_run)
    result = await _fetch_open_prs("org/repo", {})
    assert result == []
