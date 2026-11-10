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


def _check(conclusion: str, status: str = "COMPLETED") -> dict[str, str]:
    return {"conclusion": conclusion, "status": status}


def _status_context(state: str) -> dict[str, str]:
    return {"__typename": "StatusContext", "state": state}


# ---------------------------------------------------------------------------
# is_configured
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("github_user", "expected"),
    [pytest.param("testuser", True, id="user-set"), pytest.param("", False, id="false-when-empty")],
)
async def test_is_configured(github_user: str, expected: bool) -> None:
    cfg = AwarenessConfig(pr_github_user=github_user)
    provider = PRStatusProvider(cfg)
    assert await provider.is_configured() is expected


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

    @pytest.mark.parametrize(
        ("pr_kwargs", "action_hint"),
        [
            pytest.param({"number": 10, "ci_checks": [_check("FAILURE")]}, "Fix CI", id="own-pr-failing-ci"),
            pytest.param(
                {"number": 11, "review_decision": "CHANGES_REQUESTED", "ci_checks": [_check("SUCCESS")]},
                "Address review",
                id="own-pr-changes-requested",
            ),
            pytest.param(
                {"number": 12, "review_decision": "APPROVED", "ci_checks": [_check("SUCCESS")]},
                "Merge PR",
                id="own-pr-approved-ci-passing",
            ),
        ],
    )
    def test_own_pr_needs_attention(self, pr_kwargs: dict[str, object], action_hint: str) -> None:
        pr = _make_pr(author="testuser", **pr_kwargs)  # type: ignore[arg-type]
        item = _classify_pr("proj", "org/repo", pr, "testuser", set(), self._stale_cutoff())
        assert item is not None
        assert item.category == ItemCategory.NEEDS_ATTENTION
        assert item.urgency == 1
        assert item.action_hint == action_hint

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

    @pytest.mark.parametrize(
        "pr",
        [
            pytest.param(_make_pr(number=20, author="someone-else"), id="other-pr-not-review-requested"),
            pytest.param(_make_pr(number=30, author="testuser", is_draft=True), id="draft-pr"),
            pytest.param({"title": "No number", "author": {"login": "testuser"}}, id="missing-number"),
            pytest.param(_make_pr(number=0, author="testuser"), id="zero-number"),
            pytest.param(_make_pr(number=-1, author="testuser"), id="negative-number"),
        ],
    )
    def test_returns_none(self, pr: dict) -> None:
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
    @pytest.mark.parametrize(
        ("checks", "expected"),
        [
            pytest.param([], "none", id="empty-rollup"),
            pytest.param([_check("SUCCESS"), _check("SUCCESS")], "passed", id="all-success"),
            pytest.param([_check("SUCCESS"), _check("FAILURE")], "failed", id="any-failure"),
            pytest.param([_check("SUCCESS"), _check("", "IN_PROGRESS")], "pending", id="pending"),
            pytest.param([_check("SKIPPED")], "passed", id="skipped-treated-as-passed"),
            pytest.param([_status_context("SUCCESS")], "passed", id="status-context-success"),
            pytest.param([_status_context("FAILURE")], "failed", id="status-context-failure"),
            pytest.param([_check("TIMED_OUT")], "failed", id="timed-out-is-failure"),
            pytest.param([_check("")], "passed", id="completed-no-conclusion-is-passed"),
            pytest.param(
                [_status_context("SUCCESS"), _check("SUCCESS")], "passed", id="mixed-status-context-and-check-run"
            ),
            pytest.param([_status_context("WEIRD")], "pending", id="status-context-unknown-returns-pending"),
            pytest.param([_check("NEUTRAL")], "passed", id="neutral-conclusion"),
            pytest.param([_check("CANCELLED")], "passed", id="cancelled-conclusion"),
            pytest.param([_status_context("ERROR")], "failed", id="status-context-error"),
            pytest.param([_status_context("PENDING")], "pending", id="status-context-pending"),
            pytest.param(
                [_check("FAILURE"), _check("", "IN_PROGRESS")], "failed", id="failure-takes-priority-over-pending"
            ),
            pytest.param(
                [_check("SUCCESS"), _check("", "IN_PROGRESS")], "pending", id="pending-takes-priority-over-passed"
            ),
            pytest.param([_check("STARTUP_FAILURE")], "failed", id="startup-failure"),
            pytest.param([_check("ACTION_REQUIRED")], "failed", id="action-required"),
            pytest.param([_check("STALE")], "failed", id="stale-conclusion"),
            pytest.param([_status_context("QUEUED")], "pending", id="status-context-unknown-treated-as-pending"),
            pytest.param(
                [_status_context("UNKNOWN"), _status_context("WEIRD")],
                "pending",
                id="only-unknown-status-contexts-is-pending",
            ),
        ],
    )
    def test_summarize_ci(self, checks: list[dict[str, str]], expected: str) -> None:
        assert _summarize_ci(checks) == expected


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
    @pytest.mark.parametrize(
        ("context", "expected"),
        [
            pytest.param({"state": "SUCCESS"}, "passed", id="success"),
            pytest.param({"state": "FAILURE"}, "failed", id="failure"),
            pytest.param({"state": "ERROR"}, "failed", id="error"),
            pytest.param({"state": "PENDING"}, "pending", id="pending"),
            pytest.param({"state": "UNKNOWN"}, "pending", id="unknown-state"),
            pytest.param({}, "pending", id="none-state"),
            pytest.param({"state": "success"}, "passed", id="case-insensitive"),
        ],
    )
    def test_classify_status_context(self, context: dict[str, str], expected: str) -> None:
        assert _classify_status_context(context) == expected


# ---------------------------------------------------------------------------
# _classify_check_run
# ---------------------------------------------------------------------------


class TestClassifyCheckRun:
    @pytest.mark.parametrize(
        ("check", "expected"),
        [
            pytest.param(_check("FAILURE"), "failed", id="failure-conclusion"),
            pytest.param(_check("ERROR"), "failed", id="error-conclusion"),
            pytest.param(_check("TIMED_OUT"), "failed", id="timed-out"),
            pytest.param(_check("STARTUP_FAILURE"), "failed", id="startup-failure"),
            pytest.param(_check("ACTION_REQUIRED"), "failed", id="action-required"),
            pytest.param(_check("STALE"), "failed", id="stale-conclusion"),
            pytest.param(_check("SUCCESS"), "passed", id="success-conclusion"),
            pytest.param(_check("SKIPPED"), "skipped", id="skipped"),
            pytest.param(_check("NEUTRAL"), "skipped", id="neutral"),
            pytest.param(_check("CANCELLED"), "skipped", id="cancelled"),
            pytest.param(_check(""), "passed", id="completed-no-conclusion"),
            pytest.param(_check("", "IN_PROGRESS"), "pending", id="in-progress"),
            pytest.param(_check("", "QUEUED"), "pending", id="queued"),
            pytest.param({}, "pending", id="none-values"),
        ],
    )
    def test_classify_check_run(self, check: dict[str, str], expected: str) -> None:
        assert _classify_check_run(check) == expected


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

    @pytest.mark.parametrize(
        ("is_own", "review_requested", "ci_status", "review_decision", "author", "action_hint"),
        [
            pytest.param(
                True, False, "passed", "CHANGES_REQUESTED", "me", "Address review", id="own-changes-requested"
            ),
            pytest.param(True, False, "passed", "APPROVED", "me", "Merge PR", id="own-approved-and-passing"),
            pytest.param(True, True, "failed", "", "me", "Review PR", id="review-requested-overrides-own"),
        ],
    )
    def test_action_hint(
        self,
        is_own: bool,
        review_requested: bool,
        ci_status: str,
        review_decision: str,
        author: str,
        action_hint: str,
    ) -> None:
        result = _determine_pr_classification(is_own, review_requested, ci_status, review_decision, 1, author)
        assert result is not None
        assert result[2] == action_hint

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


# ---------------------------------------------------------------------------
# _get_author
# ---------------------------------------------------------------------------


class TestGetAuthor:
    @pytest.mark.parametrize(
        ("pr", "expected"),
        [
            pytest.param({"author": {"login": "alice"}}, "alice", id="dict-author"),
            pytest.param({"author": {}}, "", id="missing-login"),
            pytest.param({"author": "not-a-dict"}, "", id="string-author"),
            pytest.param({}, "", id="no-author-key"),
            pytest.param({"author": None}, "", id="none-author"),
        ],
    )
    def test_get_author(self, pr: dict, expected: str) -> None:
        assert _get_author(pr) == expected


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

    @pytest.mark.parametrize(
        "toml_text",
        [pytest.param("[project]\n", id="no-github-repo"), pytest.param("{{invalid", id="broken-config")],
    )
    def test_unusable_config_yields_no_target(self, tmp_path, toml_text: str) -> None:
        d = tmp_path / "proj"
        d.mkdir()
        (d / "sova.toml").write_text(toml_text)
        targets = _resolve_targets({"proj": str(d)})
        assert targets == []


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
