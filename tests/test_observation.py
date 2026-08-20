"""Tests for sova.oversight.observation -- cross-project health snapshot."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from sova.oversight.observation import (
    AgentSlotSummary,
    IssueSummary,
    OversightSnapshot,
    ProjectSnapshot,
    PRSummary,
    RunSummary,
    _collect_all_projects,  # noqa: PLC2701
    _collect_db_data,  # noqa: PLC2701
    _collect_fleet_slots,  # noqa: PLC2701
    _collect_github_data,  # noqa: PLC2701
    _collect_project,  # noqa: PLC2701
    _fetch_open_issues,  # noqa: PLC2701
    _fetch_open_prs,  # noqa: PLC2701
    build_snapshot,
)


@pytest.fixture(autouse=True)
def _clear_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SOVA_DATABASE_URL", raising=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _create_project_db(db_path: Path) -> None:
    """Create a minimal task_runs table (matching the columns observation queries)."""
    import aiosqlite as aiosqlite_mod

    db_path.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite_mod.connect(str(db_path)) as db:
        await db.execute(
            "CREATE TABLE IF NOT EXISTS task_runs ("
            "  id TEXT PRIMARY KEY,"
            "  issue_number TEXT,"
            "  run_label TEXT NOT NULL DEFAULT '',"
            "  role TEXT NOT NULL DEFAULT 'developer',"
            "  status TEXT NOT NULL DEFAULT 'pending',"
            "  branch_name TEXT NOT NULL DEFAULT '',"
            "  worktree_path TEXT NOT NULL DEFAULT '',"
            "  project_slug TEXT NOT NULL DEFAULT '',"
            "  total_cost_usd REAL DEFAULT 0,"
            "  started_at TEXT"
            ")"
        )
        await db.commit()


async def _insert_task_run(db_path: Path, run_id: str, issue: str, status: str) -> None:
    """Insert a task_run row using raw SQL to avoid Numeric/aiosqlite type mismatch."""
    import aiosqlite as aiosqlite_mod

    async with aiosqlite_mod.connect(str(db_path)) as db:
        await db.execute(
            "INSERT INTO task_runs (id, issue_number, status) VALUES (?, ?, ?)",
            (run_id, issue, status),
        )
        await db.commit()


def _make_registry(tmp_path: Path, projects: dict[str, Path]) -> None:
    import json as json_mod

    registry_dir = tmp_path / ".config" / "sova"
    registry_dir.mkdir(parents=True, exist_ok=True)
    data = {slug: str(path) for slug, path in projects.items()}
    (registry_dir / "projects.json").write_text(json_mod.dumps(data))


# ---------------------------------------------------------------------------
# Dataclass tests
# ---------------------------------------------------------------------------


class TestDataclasses:
    def test_oversight_snapshot_defaults(self) -> None:
        snap = OversightSnapshot()
        assert snap.projects == []
        assert snap.agent_slots.total_max_slots == 0
        assert snap.partial is False
        assert snap.collected_at > 0

    def test_oversight_snapshot_to_dict(self) -> None:
        snap = OversightSnapshot(
            projects=[ProjectSnapshot(slug="test", path="/tmp/test")],
            agent_slots=AgentSlotSummary(total_max_slots=5, active_agents=2),
        )
        d = snap.to_dict()
        assert d["agent_slots"]["total_max_slots"] == 5
        assert len(d["projects"]) == 1
        assert d["projects"][0]["slug"] == "test"

    def test_project_snapshot_defaults(self) -> None:
        ps = ProjectSnapshot(slug="s", path="/p")
        assert ps.timed_out is False
        assert ps.runs.total == 0
        assert ps.open_prs == []
        assert ps.open_issues == []

    def test_run_summary_frozen(self) -> None:
        rs = RunSummary(total=10, running=1, done=8, failed=1)
        with pytest.raises(AttributeError):
            rs.total = 20  # type: ignore[misc]

    def test_pr_summary(self) -> None:
        pr = PRSummary(number=1, title="Fix bug", state="OPEN", draft=True)
        assert pr.draft is True

    def test_issue_summary(self) -> None:
        issue = IssueSummary(number=42, title="Add feature", labels=["enhancement"])
        assert issue.labels == ["enhancement"]


# ---------------------------------------------------------------------------
# DB collection tests
# ---------------------------------------------------------------------------


class TestCollectDbData:
    @pytest.mark.asyncio
    async def test_generic_exception_sets_timed_out(self, tmp_path: Path) -> None:
        """Cover the generic Exception handler in _collect_db_data (lines 228-230).

        Uses a non-OperationalError exception so the first except clause misses it.
        """
        import aiosqlite as aiosqlite_mod

        db_path = tmp_path / "sova.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        # Create a valid DB so connect succeeds, then break the execute call
        async with aiosqlite_mod.connect(str(db_path)) as db:
            await db.execute("CREATE TABLE task_runs (id TEXT PRIMARY KEY, status TEXT)")
            await db.commit()

        project = ProjectSnapshot(slug="test", path=str(tmp_path))

        # Patch db.execute to raise a non-OperationalError after connect succeeds
        original_connect = aiosqlite_mod.connect

        class _BrokenConnection:
            """Context manager that raises TypeError on execute after PRAGMA."""

            def __init__(self, *args, **kwargs):
                self._real = original_connect(*args, **kwargs)
                self._conn = None
                self._call_count = 0

            async def __aenter__(self):
                self._conn = await self._real.__aenter__()
                original_execute = self._conn.execute

                def patched_execute(sql, *a, **kw):
                    self._call_count += 1
                    # Let PRAGMAs through (first 2 calls), fail on schema check
                    if self._call_count > 2:
                        raise TypeError("simulated non-OperationalError")
                    return original_execute(sql, *a, **kw)

                self._conn.execute = patched_execute
                return self._conn

            async def __aexit__(self, *args):
                return await self._real.__aexit__(*args)

        with patch("sova.oversight.observation.aiosqlite") as mock_mod:
            mock_mod.connect = lambda *a, **kw: _BrokenConnection(*a, **kw)
            mock_mod.Row = aiosqlite_mod.Row
            mock_mod.OperationalError = aiosqlite_mod.OperationalError
            await _collect_db_data(project, db_path)

        assert project.timed_out is True
        assert project.failure_reason == "error"

    @pytest.mark.asyncio
    async def test_collects_run_stats(self, tmp_path: Path) -> None:
        db_path = tmp_path / "project" / ".claude" / "sova.db"
        await _create_project_db(db_path)
        await _insert_task_run(db_path, "1", "1", "done")
        await _insert_task_run(db_path, "2", "2", "failed")
        await _insert_task_run(db_path, "3", "3", "running")

        project = ProjectSnapshot(slug="test", path=str(tmp_path / "project"))
        await _collect_db_data(project, db_path)

        assert project.runs.total == 3
        assert project.runs.done == 1
        assert project.runs.failed == 1
        assert project.runs.running == 1
        assert project.timed_out is False

    @pytest.mark.asyncio
    async def test_missing_db_file(self, tmp_path: Path) -> None:
        db_path = tmp_path / "nonexistent" / "sova.db"
        project = ProjectSnapshot(slug="test", path=str(tmp_path))
        await _collect_db_data(project, db_path)
        assert project.timed_out is True
        assert project.failure_reason == "db_error"

    @pytest.mark.asyncio
    async def test_no_task_runs_table(self, tmp_path: Path) -> None:
        """Pre-migration DB without task_runs table."""
        import aiosqlite

        db_path = tmp_path / "sova.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(str(db_path)) as db:
            await db.execute("CREATE TABLE other (id TEXT)")
            await db.commit()

        project = ProjectSnapshot(slug="test", path=str(tmp_path))
        await _collect_db_data(project, db_path)
        assert project.runs.total == 0  # skipped gracefully

    @pytest.mark.asyncio
    async def test_empty_task_runs_table(self, tmp_path: Path) -> None:
        db_path = tmp_path / "project" / ".claude" / "sova.db"
        await _create_project_db(db_path)

        project = ProjectSnapshot(slug="test", path=str(tmp_path / "project"))
        await _collect_db_data(project, db_path)

        assert project.runs.total == 0
        assert project.runs.done == 0


# ---------------------------------------------------------------------------
# GitHub collection tests
# ---------------------------------------------------------------------------


class TestFetchOpenPrs:
    @pytest.mark.asyncio
    async def test_parses_pr_list(self) -> None:
        mock_result = AsyncMock()
        mock_result.success = True
        mock_result.stdout = json.dumps(
            [
                {"number": 1, "title": "Fix bug", "state": "OPEN", "isDraft": False},
                {"number": 2, "title": "WIP", "state": "OPEN", "isDraft": True},
            ]
        )

        with patch("sova.oversight.observation.run", return_value=mock_result):
            prs = await _fetch_open_prs("owner/repo")

        assert len(prs) == 2
        assert prs[0].number == 1
        assert prs[0].draft is False
        assert prs[1].draft is True

    @pytest.mark.asyncio
    async def test_gh_not_available(self) -> None:
        mock_result = AsyncMock()
        mock_result.success = False
        mock_result.returncode = 127

        with patch("sova.oversight.observation.run", return_value=mock_result):
            prs = await _fetch_open_prs("owner/repo")

        assert prs == []

    @pytest.mark.asyncio
    async def test_empty_stdout(self) -> None:
        mock_result = AsyncMock()
        mock_result.success = True
        mock_result.stdout = ""

        with patch("sova.oversight.observation.run", return_value=mock_result):
            prs = await _fetch_open_prs("owner/repo")

        assert prs == []

    @pytest.mark.asyncio
    async def test_invalid_json(self) -> None:
        mock_result = AsyncMock()
        mock_result.success = True
        mock_result.stdout = "not json"

        with patch("sova.oversight.observation.run", return_value=mock_result):
            prs = await _fetch_open_prs("owner/repo")

        assert prs == []


class TestFetchOpenIssues:
    @pytest.mark.asyncio
    async def test_parses_issue_list(self) -> None:
        mock_result = AsyncMock()
        mock_result.success = True
        mock_result.stdout = json.dumps(
            [
                {"number": 10, "title": "Bug report", "labels": [{"name": "bug"}]},
                {"number": 11, "title": "Feature", "labels": []},
            ]
        )

        with patch("sova.oversight.observation.run", return_value=mock_result):
            issues = await _fetch_open_issues("owner/repo")

        assert len(issues) == 2
        assert issues[0].labels == ["bug"]
        assert issues[1].labels == []

    @pytest.mark.asyncio
    async def test_gh_failure(self) -> None:
        mock_result = AsyncMock()
        mock_result.success = False
        mock_result.returncode = 1

        with patch("sova.oversight.observation.run", return_value=mock_result):
            issues = await _fetch_open_issues("owner/repo")

        assert issues == []


class TestCollectGithubData:
    @pytest.mark.asyncio
    async def test_both_succeed(self) -> None:
        project = ProjectSnapshot(slug="test", path="/tmp/test")

        async def mock_fetch_prs(repo):
            return [PRSummary(number=1, title="PR", state="OPEN")]

        async def mock_fetch_issues(repo):
            return [IssueSummary(number=10, title="Issue")]

        with (
            patch("sova.oversight.observation._fetch_open_prs", side_effect=mock_fetch_prs),
            patch("sova.oversight.observation._fetch_open_issues", side_effect=mock_fetch_issues),
        ):
            await _collect_github_data(project, "owner/repo")

        assert len(project.open_prs) == 1
        assert len(project.open_issues) == 1

    @pytest.mark.asyncio
    async def test_issue_fetch_fails_prs_succeed(self) -> None:
        """Cover the issues exception path in _collect_github_data (line 252)."""
        project = ProjectSnapshot(slug="test", path="/tmp/test")

        async def mock_fetch_prs(repo):
            return [PRSummary(number=1, title="PR", state="OPEN")]

        with (
            patch("sova.oversight.observation._fetch_open_prs", side_effect=mock_fetch_prs),
            patch("sova.oversight.observation._fetch_open_issues", side_effect=RuntimeError("fail")),
        ):
            await _collect_github_data(project, "owner/repo")

        assert len(project.open_prs) == 1
        assert project.open_issues == []

    @pytest.mark.asyncio
    async def test_pr_fetch_fails_issues_succeed(self) -> None:
        project = ProjectSnapshot(slug="test", path="/tmp/test")

        async def mock_fetch_issues(repo):
            return [IssueSummary(number=10, title="Issue")]

        with (
            patch("sova.oversight.observation._fetch_open_prs", side_effect=RuntimeError("fail")),
            patch("sova.oversight.observation._fetch_open_issues", side_effect=mock_fetch_issues),
        ):
            await _collect_github_data(project, "owner/repo")

        assert project.open_prs == []
        assert len(project.open_issues) == 1


# ---------------------------------------------------------------------------
# Fleet slots tests
# ---------------------------------------------------------------------------


class TestCollectProject:
    @pytest.mark.asyncio
    async def test_config_load_failure_returns_early(self, tmp_path: Path) -> None:
        """Cover the load_config exception path in _collect_project (lines 180-182)."""
        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        db_path = project_dir / ".claude" / "sova.db"
        await _create_project_db(db_path)

        project = ProjectSnapshot(slug="proj", path=str(project_dir))

        with patch("sova.oversight.observation.load_config", side_effect=ValueError("bad config")):
            await _collect_project(project, project_dir)

        # DB data still collected, but GitHub skipped
        assert project.runs.total == 0
        assert project.open_prs == []


class TestCollectAllProjects:
    @pytest.mark.asyncio
    async def test_per_project_timeout_sets_timed_out(self) -> None:
        """Cover per-project timeout handler in _guarded (lines 161-162)."""
        snapshot = OversightSnapshot()

        async def _slow_collect(project, path):
            await asyncio.sleep(100)

        with patch("sova.oversight.observation._collect_project", side_effect=_slow_collect):
            await _collect_all_projects({"slow": "/tmp/slow"}, per_project_timeout=0.01, snapshot=snapshot)

        assert len(snapshot.projects) == 1
        assert snapshot.projects[0].timed_out is True
        assert snapshot.projects[0].failure_reason == "timeout"


class TestCollectFleetSlots:
    @pytest.mark.asyncio
    async def test_import_error_returns_default(self) -> None:
        # Block the fleet_service module so the import inside _collect_fleet_slots raises ImportError
        import sys

        real_module = sys.modules.get("sova.dashboard.services.fleet_service")
        sys.modules["sova.dashboard.services.fleet_service"] = None  # type: ignore[assignment]
        try:
            result = await _collect_fleet_slots({})
            assert result == AgentSlotSummary()
        finally:
            if real_module is not None:
                sys.modules["sova.dashboard.services.fleet_service"] = real_module
            else:
                sys.modules.pop("sova.dashboard.services.fleet_service", None)

    @pytest.mark.asyncio
    async def test_happy_path_with_projects(self) -> None:
        """Cover the fleet slots iteration and config load (lines 328-337)."""
        from dataclasses import dataclass

        from sova.dashboard.services.fleet_service import FleetService

        @dataclass
        class FakeInsights:
            projects_scanned: list[str]
            total_runs: int

        mock_insights = FakeInsights(projects_scanned=["proj-a", "proj-b"], total_runs=3)

        mock_cfg = AsyncMock()
        mock_cfg.max_parallel_agents = 4

        with (
            patch.object(FleetService, "get_insights", return_value=mock_insights),
            patch("sova.oversight.observation.load_config", return_value=mock_cfg),
        ):
            result = await _collect_fleet_slots({"proj-a": "/tmp/a", "proj-b": "/tmp/b"})

        assert result.total_max_slots == 8  # 4 + 4
        # active_agents is always 0 from _collect_fleet_slots; build_snapshot derives
        # it from per-project running counts after project collection completes.
        assert result.active_agents == 0

    @pytest.mark.asyncio
    async def test_fleet_slot_config_error_skips_project(self) -> None:
        """Cover the per-project config error handler (lines 335-336)."""
        from dataclasses import dataclass

        from sova.dashboard.services.fleet_service import FleetService

        @dataclass
        class FakeInsights:
            projects_scanned: list[str]
            total_runs: int

        mock_insights = FakeInsights(projects_scanned=["good", "bad"], total_runs=1)

        def fake_load_config(path):
            if "bad" in str(path):
                raise RuntimeError("config error")
            cfg = AsyncMock()
            cfg.max_parallel_agents = 2
            return cfg

        with (
            patch.object(FleetService, "get_insights", return_value=mock_insights),
            patch("sova.oversight.observation.load_config", side_effect=fake_load_config),
        ):
            result = await _collect_fleet_slots({"good": "/tmp/good", "bad": "/tmp/bad"})

        assert result.total_max_slots == 2  # only "good" counted
        assert result.active_agents == 0  # derived from per-project runs in build_snapshot

    @pytest.mark.asyncio
    async def test_exception_returns_default(self) -> None:
        from sova.dashboard.services.fleet_service import FleetService

        with patch.object(FleetService, "get_insights", side_effect=RuntimeError("boom")):
            result = await _collect_fleet_slots({})
        assert result == AgentSlotSummary()


# ---------------------------------------------------------------------------
# build_snapshot integration tests
# ---------------------------------------------------------------------------


class TestBuildSnapshot:
    @pytest.mark.asyncio
    async def test_empty_registry(self) -> None:
        with patch("sova.oversight.observation.list_projects", return_value={}):
            snap = await build_snapshot()

        assert snap.projects == []
        assert snap.partial is False

    @pytest.mark.asyncio
    async def test_single_project_with_db(self, tmp_path: Path) -> None:
        project_dir = tmp_path / "myproject"
        project_dir.mkdir()
        db_path = project_dir / ".claude" / "sova.db"
        await _create_project_db(db_path)
        await _insert_task_run(db_path, "r1", "1", "done")
        await _insert_task_run(db_path, "r2", "2", "done")

        # Write a minimal sova.toml so load_config works
        (project_dir / "sova.toml").write_text("")

        with (
            patch("sova.oversight.observation.list_projects", return_value={"myproject": str(project_dir)}),
            patch("sova.oversight.observation._collect_fleet_slots", return_value=AgentSlotSummary()),
            patch("sova.oversight.observation._collect_github_data", new_callable=AsyncMock),
        ):
            snap = await build_snapshot()

        assert len(snap.projects) == 1
        assert snap.projects[0].slug == "myproject"
        assert snap.projects[0].runs.total == 2
        assert snap.projects[0].runs.done == 2
        assert snap.partial is False

    @pytest.mark.asyncio
    async def test_project_timeout_sets_timed_out(self) -> None:
        async def _slow_collect(project, path):
            import asyncio

            await asyncio.sleep(100)

        with (
            patch("sova.oversight.observation.list_projects", return_value={"slow": "/tmp/slow"}),
            patch("sova.oversight.observation._collect_fleet_slots", return_value=AgentSlotSummary()),
            patch("sova.oversight.observation._collect_project", side_effect=_slow_collect),
        ):
            snap = await build_snapshot(timeout=0.1)

        assert len(snap.projects) == 0 or snap.projects[0].timed_out or snap.partial

    @pytest.mark.asyncio
    async def test_project_error_sets_timed_out(self) -> None:
        with (
            patch("sova.oversight.observation.list_projects", return_value={"bad": "/tmp/bad"}),
            patch("sova.oversight.observation._collect_fleet_slots", return_value=AgentSlotSummary()),
            patch("sova.oversight.observation._collect_project", side_effect=RuntimeError("boom")),
        ):
            snap = await build_snapshot()

        assert len(snap.projects) == 1
        assert snap.projects[0].timed_out is True
        assert snap.projects[0].failure_reason == "error"

    @pytest.mark.asyncio
    async def test_missing_github_repo_skips_github(self, tmp_path: Path) -> None:
        project_dir = tmp_path / "norepo"
        project_dir.mkdir()
        # No sova.toml: load_config returns defaults (github_repo="")
        db_path = project_dir / ".claude" / "sova.db"
        await _create_project_db(db_path)

        with (
            patch("sova.oversight.observation.list_projects", return_value={"norepo": str(project_dir)}),
            patch("sova.oversight.observation._collect_fleet_slots", return_value=AgentSlotSummary()),
        ):
            snap = await build_snapshot()

        assert len(snap.projects) == 1
        assert snap.projects[0].open_prs == []
        assert snap.projects[0].open_issues == []

    @pytest.mark.asyncio
    async def test_to_dict_is_json_serializable(self) -> None:
        snap = OversightSnapshot(
            projects=[
                ProjectSnapshot(
                    slug="test",
                    path="/tmp/test",
                    runs=RunSummary(total=5, running=1, done=3, failed=1),
                    open_prs=[PRSummary(number=1, title="PR", state="OPEN")],
                    open_issues=[IssueSummary(number=10, title="Issue", labels=["bug"])],
                ),
            ],
            agent_slots=AgentSlotSummary(total_max_slots=4, active_agents=2),
        )
        d = snap.to_dict()
        serialized = json.dumps(d)
        assert '"slug": "test"' in serialized


# ---------------------------------------------------------------------------
# OversightAgent integration: snapshot wiring
# ---------------------------------------------------------------------------


class TestAgentObservation:
    @pytest.mark.asyncio
    async def test_observe_returns_dict(self) -> None:
        from sova.config.models import OversightConfig
        from sova.oversight.agent import OversightAgent

        agent = OversightAgent(config=OversightConfig())

        with patch("sova.oversight.observation.list_projects", return_value={}):
            result = await agent._observe()

        assert isinstance(result, dict)
        assert result["projects"] == []

    @pytest.mark.asyncio
    async def test_observe_returns_none_on_error(self) -> None:
        from sova.config.models import OversightConfig
        from sova.oversight.agent import OversightAgent

        agent = OversightAgent(config=OversightConfig())

        with patch("sova.oversight.observation.build_snapshot", side_effect=RuntimeError("fail")):
            result = await agent._observe()

        assert result is None

    @pytest.mark.asyncio
    async def test_record_run_includes_snapshot(self) -> None:
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        from sova.config.models import OversightConfig
        from sova.db.models import Base, OversightRun, OversightRunStatus
        from sova.oversight.agent import OversightAgent

        engine = create_async_engine("sqlite+aiosqlite://", echo=False)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        factory = async_sessionmaker(engine, expire_on_commit=False)

        async def _mock_get_session(**kwargs):
            return factory()

        cfg = OversightConfig()
        agent = OversightAgent(config=cfg)
        snapshot_data = {"collected_at": 1234567890, "projects": [], "agent_slots": {}, "partial": False}

        with patch("sova.db.session.get_session", side_effect=_mock_get_session):
            await agent._record_run("snap-1", 1, OversightRunStatus.DONE, 100, snapshot=snapshot_data)

        async with factory() as session:
            from sqlalchemy import select

            result = await session.execute(select(OversightRun))
            runs = result.scalars().all()

        assert len(runs) == 1
        assert runs[0].snapshot_json is not None
        assert runs[0].snapshot_json["collected_at"] == 1234567890

        await engine.dispose()


# ---------------------------------------------------------------------------
# Migration test
# ---------------------------------------------------------------------------


class TestObservationMigration:
    def test_migration_metadata(self) -> None:
        import importlib

        mig = importlib.import_module("sova.db.migrations.versions.025_add_oversight_snapshot_json")
        assert mig.revision == "025"
        assert mig.down_revision == "024"
