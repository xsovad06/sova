"""Tests for sova.oversight.analysis: LLM analysis of oversight snapshots."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from sova.db.models import Base, OversightFinding, OversightRun, OversightRunStatus
from sova.llm.models import LLMResult
from sova.oversight.analysis import (
    _build_prompt,
    _clamp,
    _finding_from_dict,
    _parse_findings,
    _persist_findings,
    _serialize_snapshot,
    _truncate_projects,
    analyze_snapshot,
    compute_fingerprint,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def db_session():
    """Provide an in-memory SQLite DB with schema."""
    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def _mock_get_session(**kwargs):
        return factory()

    with patch("sova.db.session.get_session", side_effect=_mock_get_session):
        yield factory

    await engine.dispose()


def _make_provider(response_text: str) -> AsyncMock:
    """Create a mock LLM provider that returns the given text."""
    provider = AsyncMock()
    result = LLMResult(text=response_text, model="test")
    provider.invoke = AsyncMock(return_value=result)
    return provider


# ---------------------------------------------------------------------------
# _serialize_snapshot tests
# ---------------------------------------------------------------------------


class TestSerializeSnapshot:
    def test_dict_passthrough(self) -> None:
        data = {"projects": [{"slug": "test"}]}
        result = _serialize_snapshot(data)
        assert '"slug": "test"' in result

    def test_none_returns_empty_obj(self) -> None:
        assert _serialize_snapshot(None) == "{}"

    def test_object_with_to_dict(self) -> None:
        class FakeSnapshot:
            def to_dict(self):
                return {"key": "value"}

        result = _serialize_snapshot(FakeSnapshot())
        assert '"key": "value"' in result

    def test_truncation_removes_issues(self) -> None:
        data = {
            "projects": [
                {
                    "slug": "big-project",
                    "open_issues": [{"number": i, "title": f"Issue {i}"} for i in range(100)],
                    "open_prs": [{"number": i, "title": f"PR {i}"} for i in range(100)],
                }
            ]
        }
        result = _serialize_snapshot(data, max_chars=500)
        assert len(result) <= 500

    def test_fallback_string(self) -> None:
        result = _serialize_snapshot(42)
        assert '"raw": "42"' in result


# ---------------------------------------------------------------------------
# _build_prompt tests
# ---------------------------------------------------------------------------


class TestBuildPrompt:
    def test_includes_persona(self) -> None:
        prompt = _build_prompt("My custom persona", '{"projects": []}')
        assert "My custom persona" in prompt
        assert "## Snapshot" in prompt

    def test_empty_persona_uses_default(self) -> None:
        prompt = _build_prompt("", '{"projects": []}')
        assert "operations analyst" in prompt

    def test_whitespace_persona_uses_default(self) -> None:
        prompt = _build_prompt("   ", '{"projects": []}')
        assert "operations analyst" in prompt


# ---------------------------------------------------------------------------
# _parse_findings tests
# ---------------------------------------------------------------------------


class TestParseFindings:
    def test_valid_array(self) -> None:
        text = json.dumps([{"title": "Test", "scope": "global"}])
        result = _parse_findings(text)
        assert len(result) == 1
        assert result[0]["title"] == "Test"

    def test_empty_array(self) -> None:
        assert _parse_findings("[]") == []

    def test_markdown_code_fence(self) -> None:
        text = '```json\n[{"title": "Fenced", "scope": "global"}]\n```'
        result = _parse_findings(text)
        assert len(result) == 1
        assert result[0]["title"] == "Fenced"

    def test_invalid_json_raises(self) -> None:
        with pytest.raises(ValueError, match="No JSON found"):
            _parse_findings("not json at all")

    def test_non_array_raises(self) -> None:
        with pytest.raises(TypeError, match="Expected JSON array"):
            _parse_findings('{"title": "not an array"}')


# ---------------------------------------------------------------------------
# _clamp tests
# ---------------------------------------------------------------------------


class TestClamp:
    def test_within_range(self) -> None:
        assert _clamp(0.5, 0.0, 1.0) == 0.5

    def test_below_min(self) -> None:
        assert _clamp(-0.5, 0.0, 1.0) == 0.0

    def test_above_max(self) -> None:
        assert _clamp(1.5, 0.0, 1.0) == 1.0


# ---------------------------------------------------------------------------
# _finding_from_dict tests
# ---------------------------------------------------------------------------


class TestFindingFromDict:
    def test_valid_finding(self) -> None:
        d = {
            "title": "High failure rate",
            "scope": "project",
            "severity": "warning",
            "description": "Too many failures",
            "recommendation": "Investigate CI",
            "confidence": 0.8,
            "project_slug": "my-project",
        }
        finding = _finding_from_dict(d, "run-123")
        assert finding is not None
        assert finding.title == "High failure rate"
        assert finding.scope == "project"
        assert finding.severity == "warning"
        assert finding.confidence == 0.8
        assert finding.run_id == "run-123"
        assert finding.dismissed is False
        assert finding.github_issue_number is None

    def test_missing_title_returns_none(self) -> None:
        assert _finding_from_dict({"scope": "global"}, "run-123") is None

    def test_missing_scope_returns_none(self) -> None:
        assert _finding_from_dict({"title": "Test"}, "run-123") is None

    def test_defaults(self) -> None:
        finding = _finding_from_dict({"title": "Test", "scope": "global"}, "run-123")
        assert finding is not None
        assert finding.severity == "info"
        assert finding.description == ""
        assert finding.confidence == 0.5

    def test_confidence_clamped(self) -> None:
        finding = _finding_from_dict({"title": "T", "scope": "global", "confidence": 5.0}, "run-123")
        assert finding is not None
        assert finding.confidence == 1.0

    def test_invalid_confidence_uses_default(self) -> None:
        finding = _finding_from_dict({"title": "T", "scope": "global", "confidence": "not a number"}, "run-123")
        assert finding is not None
        assert finding.confidence == 0.5

    def test_long_title_truncated(self) -> None:
        finding = _finding_from_dict({"title": "A" * 500, "scope": "global"}, "run-123")
        assert finding is not None
        assert len(finding.title) == 300


# ---------------------------------------------------------------------------
# analyze_snapshot integration tests
# ---------------------------------------------------------------------------


class TestAnalyzeSnapshot:
    @pytest.mark.asyncio
    async def test_happy_path(self, db_session) -> None:
        findings_json = json.dumps(
            [
                {"title": "CI flaky", "scope": "project", "severity": "warning", "project_slug": "sova"},
                {"title": "Slot pressure", "scope": "global", "severity": "info"},
            ]
        )
        provider = _make_provider(findings_json)

        # Seed an OversightRun so FK is valid
        async with db_session() as session:
            async with session.begin():
                session.add(OversightRun(id="run-1", status="running", cycle_number=1))

        result, error = await analyze_snapshot(
            {"projects": []},
            "run-1",
            "Be concise",
            provider,
            model="test-model",
        )

        assert error is None
        assert len(result) == 2
        assert result[0].title == "CI flaky"
        assert result[1].title == "Slot pressure"

        # Verify persisted
        async with db_session() as session:
            stmt = select(OversightFinding).where(OversightFinding.run_id == "run-1")
            rows = (await session.execute(stmt)).scalars().all()
            assert len(rows) == 2

    @pytest.mark.asyncio
    async def test_empty_findings(self, db_session) -> None:
        provider = _make_provider("[]")

        async with db_session() as session:
            async with session.begin():
                session.add(OversightRun(id="run-2", status="running", cycle_number=1))

        result, error = await analyze_snapshot({"projects": []}, "run-2", "", provider)
        assert error is None
        assert result == []

    @pytest.mark.asyncio
    async def test_invalid_json_returns_error(self, db_session) -> None:
        provider = _make_provider("this is not json")

        async with db_session() as session:
            async with session.begin():
                session.add(OversightRun(id="run-3", status="running", cycle_number=1))

        result, error = await analyze_snapshot({"projects": []}, "run-3", "", provider)
        assert result == []
        assert error is not None
        assert "partial" in error

    @pytest.mark.asyncio
    async def test_deduplication(self, db_session) -> None:
        # Pre-seed a finding with the same title
        async with db_session() as session:
            async with session.begin():
                session.add(OversightRun(id="old-run", status="done", cycle_number=1))
                session.add(
                    OversightFinding(
                        run_id="old-run",
                        title="Existing finding",
                        scope="global",
                        fingerprint=compute_fingerprint("Existing finding", "global", ""),
                    )
                )

        async with db_session() as session:
            async with session.begin():
                session.add(OversightRun(id="run-4", status="running", cycle_number=2))

        findings_json = json.dumps(
            [
                {"title": "Existing finding", "scope": "global"},
                {"title": "New finding", "scope": "project", "project_slug": "test"},
            ]
        )
        provider = _make_provider(findings_json)

        result, error = await analyze_snapshot({"projects": []}, "run-4", "", provider)

        # Only the new finding should be persisted
        assert error is None
        assert len(result) == 1
        assert result[0].title == "New finding"

        # Verify exactly one new row was persisted (the duplicate was skipped)
        async with db_session() as session:
            stmt = select(OversightFinding).where(OversightFinding.run_id == "run-4")
            rows = (await session.execute(stmt)).scalars().all()
            assert len(rows) == 1
            assert rows[0].title == "New finding"

            # Verify total DB state: 2 findings across both runs
            all_stmt = select(OversightFinding)
            all_findings = (await session.execute(all_stmt)).scalars().all()
            assert len(all_findings) == 2
            titles = {f.title for f in all_findings}
            assert titles == {"Existing finding", "New finding"}

    @pytest.mark.asyncio
    async def test_llm_exception_returns_error(self, db_session) -> None:
        provider = AsyncMock()
        provider.invoke = AsyncMock(side_effect=RuntimeError("LLM down"))

        async with db_session() as session:
            async with session.begin():
                session.add(OversightRun(id="run-5", status="running", cycle_number=1))

        result, error = await analyze_snapshot({"projects": []}, "run-5", "", provider)
        assert result == []
        assert error is not None
        assert "partial" in error

    @pytest.mark.asyncio
    async def test_skips_invalid_entries(self, db_session) -> None:
        findings_json = json.dumps(
            [
                {"title": "Valid", "scope": "global"},
                {"no_title": True},
                "not a dict",
                {"title": "Also valid", "scope": "project", "project_slug": "x"},
            ]
        )
        provider = _make_provider(findings_json)

        async with db_session() as session:
            async with session.begin():
                session.add(OversightRun(id="run-6", status="running", cycle_number=1))

        result, error = await analyze_snapshot({"projects": []}, "run-6", "", provider)
        assert error is None
        assert len(result) == 2
        assert result[0].title == "Valid"
        assert result[1].title == "Also valid"

    @pytest.mark.asyncio
    async def test_non_array_json_returns_error(self, db_session) -> None:
        provider = _make_provider('{"not": "an array"}')

        async with db_session() as session:
            async with session.begin():
                session.add(OversightRun(id="run-7", status="running", cycle_number=1))

        result, error = await analyze_snapshot({"projects": []}, "run-7", "", provider)
        assert result == []
        assert error is not None
        assert "partial" in error


# ---------------------------------------------------------------------------
# OversightAgent._analyze wiring test
# ---------------------------------------------------------------------------


class TestAgentAnalyzeWiring:
    @pytest.mark.asyncio
    async def test_analyze_called_on_snapshot(self) -> None:
        """Verify that _analyze is called when observation succeeds."""
        import asyncio

        from sova.config.models import OversightConfig
        from sova.oversight.agent import OversightAgent

        cfg = OversightConfig(enabled=True, wake_interval_minutes=1)
        agent = OversightAgent(config=cfg)

        analyze_called = False
        recorded: list[dict] = []

        async def _mock_observe():
            return {"projects": []}

        async def _mock_analyze(snapshot, run_id):
            nonlocal analyze_called
            analyze_called = True
            return [], None

        async def _mock_record(run_id, cycle, status, duration_ms, *, started_at=None, error=None, snapshot=None):
            recorded.append({"status": status})

        async def _fake_sleep(seconds):
            raise asyncio.CancelledError

        with (
            patch.object(agent, "_observe", side_effect=_mock_observe),
            patch.object(agent, "_analyze", side_effect=_mock_analyze),
            patch.object(agent, "_record_run", side_effect=_mock_record),
            patch.object(agent, "_reload_config", return_value=cfg),
            patch.object(agent, "_interruptible_sleep", side_effect=_fake_sleep),
        ):
            task = agent.start()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert analyze_called is True
        assert recorded[0]["status"] == OversightRunStatus.DONE

    @pytest.mark.asyncio
    async def test_analyze_not_called_on_failed_observation(self) -> None:
        """Verify that _analyze is NOT called when observation returns None."""
        import asyncio

        from sova.config.models import OversightConfig
        from sova.oversight.agent import OversightAgent

        cfg = OversightConfig(enabled=True, wake_interval_minutes=1)
        agent = OversightAgent(config=cfg)

        analyze_called = False

        async def _mock_observe():
            return None

        async def _mock_analyze(snapshot, run_id):
            nonlocal analyze_called
            analyze_called = True
            return [], None

        async def _mock_record(*args, **kwargs):
            pass

        async def _fake_sleep(seconds):
            raise asyncio.CancelledError

        with (
            patch.object(agent, "_observe", side_effect=_mock_observe),
            patch.object(agent, "_analyze", side_effect=_mock_analyze),
            patch.object(agent, "_record_run", side_effect=_mock_record),
            patch.object(agent, "_reload_config", return_value=cfg),
            patch.object(agent, "_interruptible_sleep", side_effect=_fake_sleep),
        ):
            task = agent.start()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert analyze_called is False

    @pytest.mark.asyncio
    async def test_analyze_failure_does_not_crash_cycle(self) -> None:
        """If _analyze raises, the cycle catches it and records ERROR with the error message.

        Note: This test mocks _analyze to raise, which bypasses the normal exception
        handling in _analyze(). In production, _analyze() catches exceptions and returns
        an error string, which the outer loop uses to set status=ERROR."""
        import asyncio

        from sova.config.models import OversightConfig
        from sova.oversight.agent import OversightAgent

        cfg = OversightConfig(enabled=True, wake_interval_minutes=1)
        agent = OversightAgent(config=cfg)

        recorded: list[dict] = []

        async def _mock_observe():
            return {"projects": []}

        async def _mock_record(run_id, cycle, status, duration_ms, *, started_at=None, error=None, snapshot=None):
            recorded.append({"status": status, "error": error})

        async def _fake_sleep(seconds):
            raise asyncio.CancelledError

        with (
            patch.object(agent, "_observe", side_effect=_mock_observe),
            patch.object(agent, "_analyze", side_effect=RuntimeError("LLM exploded")),
            patch.object(agent, "_record_run", side_effect=_mock_record),
            patch.object(agent, "_reload_config", return_value=cfg),
            patch.object(agent, "_interruptible_sleep", side_effect=_fake_sleep),
        ):
            task = agent.start()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert len(recorded) == 1
        assert recorded[0]["status"] == OversightRunStatus.ERROR


# ---------------------------------------------------------------------------
# OversightFinding model test
# ---------------------------------------------------------------------------


class TestOversightFindingModel:
    def test_model_fields(self) -> None:
        finding = OversightFinding(
            run_id="abc-123",
            title="Test finding",
            scope="global",
            severity="warning",
            confidence=0.9,
            dismissed=False,
        )
        assert finding.run_id == "abc-123"
        assert finding.title == "Test finding"
        assert finding.scope == "global"
        assert finding.severity == "warning"
        assert finding.confidence == 0.9
        assert finding.dismissed is False
        assert finding.github_issue_number is None


# ---------------------------------------------------------------------------
# Migration test
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _truncate_projects tests
# ---------------------------------------------------------------------------


class TestTruncateProjects:
    def test_no_projects_key(self) -> None:
        data: dict = {"other": "value"}
        _truncate_projects(data)
        assert data == {"other": "value"}

    def test_projects_not_list(self) -> None:
        data: dict = {"projects": "not a list"}
        _truncate_projects(data)
        assert data["projects"] == "not a list"

    def test_non_dict_project_skipped(self) -> None:
        data: dict = {"projects": ["not a dict", {"slug": "a", "open_issues": [1, 2]}]}
        _truncate_projects(data)
        assert data["projects"][0] == "not a dict"
        assert data["projects"][1]["open_issues"] == []


# ---------------------------------------------------------------------------
# _serialize_snapshot hard truncation
# ---------------------------------------------------------------------------


class TestSerializeSnapshotHardTruncate:
    def test_hard_truncate_when_still_too_large(self) -> None:
        """When truncation of projects is not enough, hard-truncate the text."""
        data = {"large_field": "x" * 200}
        result = _serialize_snapshot(data, max_chars=50)
        assert len(result) == 50


# ---------------------------------------------------------------------------
# _finding_from_dict edge cases
# ---------------------------------------------------------------------------


class TestFindingFromDictEdgeCases:
    def test_invalid_severity_falls_back(self) -> None:
        finding = _finding_from_dict({"title": "T", "scope": "global", "severity": "bogus"}, "r")
        assert finding is not None
        assert finding.severity == "info"

    def test_invalid_scope_falls_back(self) -> None:
        finding = _finding_from_dict({"title": "T", "scope": "unknown"}, "r")
        assert finding is not None
        assert finding.scope == "project"

    def test_project_slug_truncated(self) -> None:
        finding = _finding_from_dict({"title": "T", "scope": "global", "project_slug": "a" * 200}, "r")
        assert finding is not None
        assert len(finding.project_slug) == 100


# ---------------------------------------------------------------------------
# _persist_findings error path
# ---------------------------------------------------------------------------


class TestPersistFindings:
    @pytest.mark.asyncio
    async def test_persist_failure_returns_zero(self) -> None:
        finding = OversightFinding(
            run_id="run-x",
            title="Will fail",
            scope="global",
        )

        async def _failing_session(**kwargs):
            raise RuntimeError("DB unavailable")

        with patch("sova.db.session.get_session", side_effect=_failing_session):
            result = await _persist_findings([finding], "run-x")
        assert result == 0


# ---------------------------------------------------------------------------
# analyze_snapshot edge cases
# ---------------------------------------------------------------------------


class TestAnalyzeSnapshotEdgeCases:
    @pytest.mark.asyncio
    async def test_dedup_query_failure_still_works(self, db_session) -> None:
        """When dedup query fails, analysis proceeds without dedup."""
        findings_json = json.dumps([{"title": "Finding", "scope": "global"}])
        provider = _make_provider(findings_json)

        async with db_session() as session:
            async with session.begin():
                session.add(OversightRun(id="run-dq", status="running", cycle_number=1))

        # Patch _load_recent_fingerprints to simulate DB failure during dedup
        with patch(
            "sova.oversight.analysis._load_recent_fingerprints",
            side_effect=RuntimeError("DB down"),
        ):
            result, error = await analyze_snapshot({"projects": []}, "run-dq", "", provider)

        assert error is None
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_persistence_failure_returns_partial(self, db_session) -> None:
        """When findings exist but persistence fails, return partial error."""
        findings_json = json.dumps([{"title": "Persist fail", "scope": "global"}])
        provider = _make_provider(findings_json)

        async with db_session() as session:
            async with session.begin():
                session.add(OversightRun(id="run-pf", status="running", cycle_number=1))

        with patch("sova.oversight.analysis._persist_findings", return_value=0):
            result, error = await analyze_snapshot({"projects": []}, "run-pf", "", provider)

        assert result == []
        assert error is not None
        assert "persist failed" in error

    @pytest.mark.asyncio
    async def test_config_params_passed_through(self, db_session) -> None:
        """Verify dedup_window_days and analysis_timeout are forwarded."""
        findings_json = json.dumps([{"title": "Config test", "scope": "global"}])
        provider = _make_provider(findings_json)

        async with db_session() as session:
            async with session.begin():
                session.add(OversightRun(id="run-cfg", status="running", cycle_number=1))

        result, error = await analyze_snapshot(
            {"projects": []},
            "run-cfg",
            "",
            provider,
            dedup_window_days=7,
            analysis_timeout=30,
        )
        assert error is None
        # Verify timeout was passed to provider
        provider.invoke.assert_called_once()
        call_kwargs = provider.invoke.call_args
        assert call_kwargs.kwargs.get("timeout") == 30


# ---------------------------------------------------------------------------
# OversightAgent unit tests
# ---------------------------------------------------------------------------


class TestOversightAgentUnit:
    def test_determine_outcome_done(self) -> None:
        from sova.oversight.agent import OversightAgent

        status, err = OversightAgent._determine_outcome({"data": 1}, None)
        assert status == OversightRunStatus.DONE
        assert err is None

    def test_determine_outcome_observation_failed(self) -> None:
        from sova.oversight.agent import OversightAgent

        status, err = OversightAgent._determine_outcome(None, None)
        assert status == OversightRunStatus.ERROR
        assert err == "observation_failed"

    def test_determine_outcome_analysis_error(self) -> None:
        from sova.oversight.agent import OversightAgent

        status, err = OversightAgent._determine_outcome({"data": 1}, "some error")
        assert status == OversightRunStatus.ERROR
        assert err == "some error"

    def test_get_system_prompt_empty(self) -> None:
        from sova.config.models import OversightConfig
        from sova.oversight.agent import OversightAgent

        agent = OversightAgent(config=OversightConfig())
        assert agent.get_system_prompt() == ""

    def test_get_system_prompt_with_persona(self) -> None:
        from sova.config.models import OversightConfig
        from sova.oversight.agent import OversightAgent

        agent = OversightAgent(config=OversightConfig())
        agent._persona = "Be strict about failures"
        prompt = agent.get_system_prompt()
        assert "Be strict about failures" in prompt
        assert "Operations Persona" in prompt

    @pytest.mark.asyncio
    async def test_stop_no_task(self) -> None:
        from sova.config.models import OversightConfig
        from sova.oversight.agent import OversightAgent

        agent = OversightAgent(config=OversightConfig())
        await agent.stop()  # Should not raise

    @pytest.mark.asyncio
    async def test_stop_with_running_task(self) -> None:
        import asyncio

        from sova.config.models import OversightConfig
        from sova.oversight.agent import OversightAgent

        agent = OversightAgent(config=OversightConfig())

        async def _forever():
            await asyncio.sleep(3600)

        agent._task = asyncio.create_task(_forever())
        await agent.stop()
        assert agent._task is None

    @pytest.mark.asyncio
    async def test_record_error_safe_swallows_exception(self) -> None:
        from sova.config.models import OversightConfig
        from sova.oversight.agent import OversightAgent

        agent = OversightAgent(config=OversightConfig())

        async def _failing_record(*args, **kwargs):
            raise RuntimeError("DB exploded")

        with patch.object(agent, "_record_run", side_effect=_failing_record):
            from datetime import datetime, timezone

            await agent._record_error_safe("r1", 1, 100, datetime.now(timezone.utc), "test error")  # Should not raise

    @pytest.mark.asyncio
    async def test_cancelled_error_records_and_reraises(self) -> None:
        """CancelledError path records error and re-raises."""
        import asyncio

        from sova.config.models import OversightConfig
        from sova.oversight.agent import OversightAgent

        cfg = OversightConfig(enabled=True, wake_interval_minutes=1)
        agent = OversightAgent(config=cfg)
        recorded: list[dict] = []

        async def _mock_observe():
            raise asyncio.CancelledError

        async def _mock_record_error_safe(run_id, cycle, duration_ms, started_at, error):
            recorded.append({"error": error})

        with (
            patch.object(agent, "_observe", side_effect=_mock_observe),
            patch.object(agent, "_record_error_safe", side_effect=_mock_record_error_safe),
            patch.object(agent, "_reload_config", return_value=cfg),
            patch("sova.oversight.agent.load_persona", return_value=""),
        ):
            task = agent.start()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert len(recorded) == 1
        assert recorded[0]["error"] == "cancelled"


# ---------------------------------------------------------------------------
# Migration test
# ---------------------------------------------------------------------------


class TestOversightFindingsMigration:
    def test_migration_metadata(self) -> None:
        import importlib

        mig = importlib.import_module("sova.db.migrations.versions.027_add_oversight_findings_table")
        assert mig.revision == "027"
        assert mig.down_revision == "026"


# ---------------------------------------------------------------------------
# Fingerprint migration test
# ---------------------------------------------------------------------------


class TestFingerprintMigration:
    """Migration 031 adds fingerprint column."""

    async def test_migration_revision(self) -> None:
        from tests.test_db import _import_migration

        mod = _import_migration("031")
        assert mod.revision == "031"
        assert mod.down_revision == "030"


class TestComputeFingerprint:
    """Tests for compute_fingerprint()."""

    def test_basic(self) -> None:
        fp = compute_fingerprint("CPU at 85%", "global", "")
        assert isinstance(fp, str)
        assert len(fp) == 16

    def test_normalizes_numbers(self) -> None:
        fp1 = compute_fingerprint("CPU at 85%", "global", "")
        fp2 = compute_fingerprint("CPU at 92%", "global", "")
        assert fp1 == fp2

    def test_normalizes_issue_refs(self) -> None:
        fp1 = compute_fingerprint("Issue #42 failing", "project", "sova")
        fp2 = compute_fingerprint("Issue #99 failing", "project", "sova")
        assert fp1 == fp2

    def test_case_insensitive(self) -> None:
        fp1 = compute_fingerprint("High CPU Usage", "global", "")
        fp2 = compute_fingerprint("high cpu usage", "global", "")
        assert fp1 == fp2

    def test_different_scopes_differ(self) -> None:
        fp1 = compute_fingerprint("Same title", "global", "")
        fp2 = compute_fingerprint("Same title", "project", "")
        assert fp1 != fp2

    def test_different_projects_differ(self) -> None:
        fp1 = compute_fingerprint("Same title", "project", "sova")
        fp2 = compute_fingerprint("Same title", "project", "other")
        assert fp1 != fp2

    def test_whitespace_normalized(self) -> None:
        fp1 = compute_fingerprint("  high   cpu  ", "global", "")
        fp2 = compute_fingerprint("high cpu", "global", "")
        assert fp1 == fp2

    def test_different_titles_differ(self) -> None:
        fp1 = compute_fingerprint("CPU issue", "global", "")
        fp2 = compute_fingerprint("Memory issue", "global", "")
        assert fp1 != fp2

    def test_normalizes_start_of_title_numbers(self) -> None:
        fp1 = compute_fingerprint("100 failures detected", "global", "")
        fp2 = compute_fingerprint("200 failures detected", "global", "")
        assert fp1 == fp2


class TestConfigReload:
    """Tests for OversightAgent config hot-reload."""

    def test_reload_config_returns_config(self) -> None:
        from sova.config.models import OversightConfig
        from sova.oversight.agent import OversightAgent

        cfg = OversightConfig(enabled=True)
        agent = OversightAgent(config=cfg)
        result = agent.reload_config()
        assert isinstance(result, OversightConfig)

    def test_reload_config_updates_on_success(self, tmp_path) -> None:
        from sova.config.models import OversightConfig
        from sova.oversight.agent import OversightAgent

        cfg = OversightConfig(enabled=False, wake_interval_minutes=30)
        agent = OversightAgent(config=cfg, project_dir=str(tmp_path))

        new_cfg = OversightConfig(enabled=True, wake_interval_minutes=15)
        mock_project_cfg = type("C", (), {"oversight": new_cfg})()

        with patch("sova.config.loader.load_config", return_value=mock_project_cfg):
            result = agent._reload_config()

        assert result.enabled is True
        assert result.wake_interval_minutes == 15

    def test_reload_config_falls_back_on_error(self) -> None:
        from sova.config.models import OversightConfig
        from sova.oversight.agent import OversightAgent

        cfg = OversightConfig(enabled=True, wake_interval_minutes=60)
        agent = OversightAgent(config=cfg)

        with patch("sova.config.loader.load_config", side_effect=RuntimeError("boom")):
            result = agent._reload_config()

        assert result is cfg
        assert result.wake_interval_minutes == 60

    @pytest.mark.asyncio
    async def test_reload_starts_agent_when_enabled_from_disabled(self) -> None:
        from sova.config.models import OversightConfig
        from sova.dashboard.routers.settings import _reload_oversight_config

        cfg = OversightConfig(enabled=False)
        agent = MagicMock()
        agent._config = cfg
        agent.running = False
        agent.reload_config.side_effect = lambda: setattr(agent, "_config", OversightConfig(enabled=True))

        with patch(
            "sova.dashboard.routers.oversight.get_oversight_agent",
            return_value=agent,
        ):
            await _reload_oversight_config()

        agent.reload_config.assert_called_once()
        agent.start.assert_called_once()

    @pytest.mark.asyncio
    async def test_reload_does_not_restart_already_running_agent(self) -> None:
        from sova.config.models import OversightConfig
        from sova.dashboard.routers.settings import _reload_oversight_config

        cfg = OversightConfig(enabled=True)
        agent = MagicMock()
        agent._config = cfg
        agent.running = True

        with patch(
            "sova.dashboard.routers.oversight.get_oversight_agent",
            return_value=agent,
        ):
            await _reload_oversight_config()

        agent.reload_config.assert_called_once()
        agent.start.assert_not_called()


class TestFingerprintCrossCheck:
    """Tests for _find_existing_issue_by_fingerprint in actions."""

    @pytest.mark.asyncio
    async def test_returns_none_when_no_match(self) -> None:
        from sova.oversight.actions import _find_existing_issue_by_fingerprint

        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.first.return_value = None
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("sova.db.session.get_session", return_value=mock_session):
            result = await _find_existing_issue_by_fingerprint("abc123")
            assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_for_none_fingerprint(self) -> None:
        from sova.oversight.actions import _find_existing_issue_by_fingerprint

        result = await _find_existing_issue_by_fingerprint(None)
        assert result is None


class TestFindingFromDictFingerprint:
    """Tests that _finding_from_dict sets the fingerprint field."""

    def test_finding_has_fingerprint(self) -> None:
        from sova.oversight.analysis import _finding_from_dict

        finding = _finding_from_dict(
            {"title": "Test finding", "scope": "global", "project_slug": ""},
            "run-1",
        )
        assert finding is not None
        assert finding.fingerprint is not None
        assert len(finding.fingerprint) == 16

    def test_finding_fingerprint_matches_compute(self) -> None:
        from sova.oversight.analysis import _finding_from_dict

        finding = _finding_from_dict(
            {"title": "Test finding", "scope": "global", "project_slug": "sova"},
            "run-1",
        )
        expected = compute_fingerprint("Test finding", "global", "sova")
        assert finding.fingerprint == expected
