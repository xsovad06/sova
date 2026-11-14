"""Tests for SOVA database operations."""

from __future__ import annotations

import os
import types
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import inspect, select

from sova.db.models import (
    CostRecord,
    FailureRecord,
    IssueLifecycle,
    LifecyclePhaseRecord,
    StepExecution,
    TaskAssessmentRecord,
    TaskRun,
)
from sova.db.session import close_db, get_session, init_db


def _import_migration(migration_number: str) -> types.ModuleType:
    """Import a migration module by revision number.

    Args:
        migration_number: Revision number (e.g., "011", "027")

    Returns:
        The imported migration module
    """
    import importlib.util
    from pathlib import Path

    versions_dir = Path(__file__).resolve().parent.parent / "sova" / "db" / "migrations" / "versions"
    migration_files = list(versions_dir.glob(f"{migration_number}_*.py"))
    if not migration_files:
        raise FileNotFoundError(f"No migration file found for revision {migration_number}")

    migration_path = migration_files[0]
    spec = importlib.util.spec_from_file_location(f"migration_{migration_number}", migration_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Uses the shared per-worker in-memory DB, emptied before each test
# (see ``tests/conftest.py:reset_db_rows``). Several tests here call
# ``init_db()``/``close_db()``/``init_db_for_project()`` themselves as the thing
# under test; ``close_db()`` disposes the shared engine too, and the next test's
# ``reset_db_rows`` sweep recreates its schema.
pytestmark = pytest.mark.usefixtures("reset_db_rows")


async def test_get_session_initializes_the_engine_on_first_use(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With no engine yet, ``get_session()`` runs ``init_db()`` for the resolved URL instead of failing."""
    import sova.db.session as session_module

    monkeypatch.setenv("SOVA_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'first.db'}")
    monkeypatch.setattr(session_module, "_engine", None)
    monkeypatch.setattr(session_module, "_session_factory", None)

    async with await get_session() as session:
        session.add(TaskRun(issue_number="1", role="developer", status="pending"))
        await session.commit()

    assert session_module._engine is not None
    assert (tmp_path / "first.db").exists()


async def test_create_task_run() -> None:
    """Create and retrieve a task run."""
    async with await get_session() as session:
        run = TaskRun(
            issue_number="42",
            role="developer",
            status="pending",
            project_slug="test-project",
        )
        session.add(run)
        await session.commit()
        await session.refresh(run)

        assert run.id is not None
        assert run.issue_number == "42"
        assert run.role == "developer"
        assert run.status == "pending"
        assert run.total_cost_usd == Decimal("0")
        assert run.started_at is not None


async def test_create_step_execution() -> None:
    """Create a step execution linked to a task run."""
    async with await get_session() as session:
        run = TaskRun(issue_number="10", role="developer", status="in_progress")
        session.add(run)
        await session.commit()
        await session.refresh(run)

        step = StepExecution(
            task_run_id=run.id,
            step_name="develop",
            status="success",
            cost_usd=Decimal("1.50"),
            duration_ms=45000,
            output_summary="Implemented feature X",
            retry_count=2,
        )
        session.add(step)
        await session.commit()
        await session.refresh(step)

        assert step.task_run_id == run.id
        assert step.cost_usd == Decimal("1.50")
        assert step.retry_count == 2


async def test_step_execution_retry_count_defaults_to_zero() -> None:
    """retry_count defaults to 0 when not specified."""
    async with await get_session() as session:
        run = TaskRun(issue_number="11", role="developer", status="in_progress")
        session.add(run)
        await session.commit()
        await session.refresh(run)

        step = StepExecution(
            task_run_id=run.id,
            step_name="sync",
            status="success",
            cost_usd=Decimal("0"),
            duration_ms=100,
        )
        session.add(step)
        await session.commit()
        await session.refresh(step)

        assert step.retry_count == 0


async def test_create_failure_record(tmp_path: Path) -> None:
    """Create a failure record with context."""
    async with await get_session() as session:
        run = TaskRun(issue_number="99", role="developer", status="failed")
        session.add(run)
        await session.commit()
        await session.refresh(run)

        failure = FailureRecord(
            task_run_id=run.id,
            step_name="develop",
            failure_type="gate_check",
            message="Development produced no code changes",
            context={"git_diff": "", "worktree": str(tmp_path / "wt")},
        )
        session.add(failure)
        await session.commit()
        await session.refresh(failure)

        assert failure.failure_type == "gate_check"
        assert failure.context["git_diff"] == ""
        assert failure.resolved is False


async def test_create_cost_record() -> None:
    """Create a cost record for an LLM invocation."""
    async with await get_session() as session:
        cost = CostRecord(
            phase="step4-develop",
            issue="42",
            model="claude-opus-4",
            input_tokens=5000,
            output_tokens=2000,
            cost_usd=Decimal("0.35"),
            duration_ms=12000,
        )
        session.add(cost)
        await session.commit()
        await session.refresh(cost)

        assert cost.model == "claude-opus-4"
        assert cost.cost_usd == Decimal("0.35")
        assert cost.model_selection_reason is None
        assert cost.cache_read_tokens is None
        assert cost.cache_write_tokens is None


async def test_cost_record_cache_token_breakdown() -> None:
    """CostRecord stores granular cache token breakdown."""
    async with await get_session() as session:
        cost = CostRecord(
            phase="develop",
            issue="70",
            model="claude-opus-4-6",
            input_tokens=8000,
            output_tokens=3000,
            cache_tokens=600,
            cache_read_tokens=100,
            cache_write_tokens=500,
            cost_usd=Decimal("1.50"),
            duration_ms=15000,
        )
        session.add(cost)
        await session.commit()
        await session.refresh(cost)

        assert cost.cache_tokens == 600
        assert cost.cache_read_tokens == 100
        assert cost.cache_write_tokens == 500


async def test_cost_record_with_model_selection_reason() -> None:
    """CostRecord stores model_selection_reason when provided."""
    async with await get_session() as session:
        cost = CostRecord(
            phase="triage",
            issue="99",
            model="haiku",
            cost_usd=Decimal("0.01"),
            model_selection_reason="role:triage->haiku",
        )
        session.add(cost)
        await session.commit()
        await session.refresh(cost)

        assert cost.model_selection_reason == "role:triage->haiku"


async def test_migration_011_column_exists_helper() -> None:
    """Migration 011 _column_exists correctly detects model_selection_reason column."""
    mod = _import_migration("011")

    from unittest.mock import MagicMock, patch

    from alembic import op

    mock_conn = MagicMock()
    mock_inspector = MagicMock()
    mock_inspector.get_columns.return_value = [
        {"name": "id"},
        {"name": "model"},
        {"name": "model_selection_reason"},
    ]

    with patch.object(op, "get_bind", return_value=mock_conn), patch("sqlalchemy.inspect", return_value=mock_inspector):
        assert mod._column_exists("cost_records", "model_selection_reason") is True
        assert mod._column_exists("cost_records", "nonexistent") is False


async def test_migration_011_upgrade_skip_when_exists() -> None:
    """Migration 011 upgrade is idempotent: skips if column already exists."""
    from unittest.mock import patch

    mod = _import_migration("011")

    with (
        patch.object(mod, "_column_exists", return_value=True) as mock_exists,
        patch.object(mod, "op") as mock_op,
    ):
        mod.upgrade()
        mock_exists.assert_called_once_with("cost_records", "model_selection_reason")
        mock_op.add_column.assert_not_called()


async def test_migration_011_upgrade_adds_column() -> None:
    """Migration 011 upgrade adds column when it doesn't exist."""
    from unittest.mock import patch

    mod = _import_migration("011")

    with patch.object(mod, "_column_exists", return_value=False), patch.object(mod, "op") as mock_op:
        mod.upgrade()
        mock_op.add_column.assert_called_once()


async def test_migration_011_downgrade_drops_column() -> None:
    """Migration 011 downgrade drops column when it exists."""
    from unittest.mock import patch

    mod = _import_migration("011")

    with patch.object(mod, "_column_exists", return_value=True), patch.object(mod, "op") as mock_op:
        mod.downgrade()
        mock_op.drop_column.assert_called_once_with("cost_records", "model_selection_reason")


async def test_migration_011_downgrade_skip_when_missing() -> None:
    """Migration 011 downgrade is idempotent: skips if column doesn't exist."""
    from unittest.mock import patch

    mod = _import_migration("011")

    with patch.object(mod, "_column_exists", return_value=False), patch.object(mod, "op") as mock_op:
        mod.downgrade()
        mock_op.drop_column.assert_not_called()


async def test_migration_028_upgrade_adds_columns() -> None:
    """Migration 028 upgrade adds cache_read_tokens and cache_write_tokens."""
    from unittest.mock import MagicMock, patch

    mod = _import_migration("028")

    mock_inspector = MagicMock()
    mock_inspector.get_columns.return_value = [{"name": "id"}, {"name": "model"}]

    with patch.object(mod.sa, "inspect", return_value=mock_inspector), patch.object(mod, "op") as mock_op:
        mod.upgrade()
        assert mock_op.add_column.call_count == 2


async def test_migration_028_upgrade_skip_when_exists() -> None:
    """Migration 028 upgrade is idempotent."""
    from unittest.mock import MagicMock, patch

    mod = _import_migration("028")

    mock_inspector = MagicMock()
    mock_inspector.get_columns.return_value = [
        {"name": "id"},
        {"name": "cache_read_tokens"},
        {"name": "cache_write_tokens"},
    ]

    with patch.object(mod.sa, "inspect", return_value=mock_inspector), patch.object(mod, "op") as mock_op:
        mod.upgrade()
        mock_op.add_column.assert_not_called()


async def test_migration_028_downgrade_drops_columns() -> None:
    """Migration 028 downgrade drops both cache token columns."""
    from unittest.mock import MagicMock, patch

    mod = _import_migration("028")

    mock_inspector = MagicMock()
    mock_inspector.get_columns.return_value = [
        {"name": "id"},
        {"name": "cache_read_tokens"},
        {"name": "cache_write_tokens"},
    ]

    with patch.object(mod.sa, "inspect", return_value=mock_inspector), patch.object(mod, "op") as mock_op:
        mod.downgrade()
        mock_op.drop_column.assert_any_call("cost_records", "cache_write_tokens")
        mock_op.drop_column.assert_any_call("cost_records", "cache_read_tokens")
        assert mock_op.drop_column.call_count == 2


async def test_migration_028_downgrade_skip_when_missing() -> None:
    """Migration 028 downgrade is idempotent: skips if columns already absent."""
    from unittest.mock import MagicMock, patch

    mod = _import_migration("028")

    mock_inspector = MagicMock()
    mock_inspector.get_columns.return_value = [{"name": "id"}, {"name": "model"}]

    with patch.object(mod.sa, "inspect", return_value=mock_inspector), patch.object(mod, "op") as mock_op:
        mod.downgrade()
        mock_op.drop_column.assert_not_called()


async def test_migration_028_upgrade_partial_one_column_exists() -> None:
    """Migration 028 upgrade adds only the missing column when one already exists."""
    from unittest.mock import MagicMock, call, patch

    mod = _import_migration("028")

    mock_inspector = MagicMock()
    mock_inspector.get_columns.return_value = [
        {"name": "id"},
        {"name": "cache_read_tokens"},
    ]

    with patch.object(mod.sa, "inspect", return_value=mock_inspector), patch.object(mod, "op") as mock_op:
        mod.upgrade()
        assert mock_op.add_column.call_count == 1
        added_col = mock_op.add_column.call_args
        assert added_col == call("cost_records", mock_op.add_column.call_args[0][1])
        assert "cache_write_tokens" in str(added_col)


async def test_migration_028_downgrade_partial_one_column_exists() -> None:
    """Migration 028 downgrade drops only the column that exists."""
    from unittest.mock import MagicMock, patch

    mod = _import_migration("028")

    mock_inspector = MagicMock()
    mock_inspector.get_columns.return_value = [
        {"name": "id"},
        {"name": "cache_write_tokens"},
    ]

    with patch.object(mod.sa, "inspect", return_value=mock_inspector), patch.object(mod, "op") as mock_op:
        mod.downgrade()
        mock_op.drop_column.assert_called_once_with("cost_records", "cache_write_tokens")


async def test_migration_028_column_exists_helper() -> None:
    """Migration 028 _column_exists detects columns correctly."""
    from unittest.mock import MagicMock, patch

    mod = _import_migration("028")

    mock_inspector = MagicMock()
    mock_inspector.get_columns.return_value = [{"name": "id"}, {"name": "cache_read_tokens"}]

    with patch.object(mod, "op") as mock_op, patch.object(mod.sa, "inspect", return_value=mock_inspector):
        mock_op.get_bind.return_value = MagicMock()
        assert mod._column_exists("cost_records", "cache_read_tokens") is True
        assert mod._column_exists("cost_records", "cache_write_tokens") is False


async def test_migration_034_upgrade_adds_columns() -> None:
    """Migration 034 upgrade adds pre_compression_input_tokens and tokens_saved."""
    from unittest.mock import MagicMock, patch

    mod = _import_migration("034")

    mock_inspector = MagicMock()
    mock_inspector.get_columns.return_value = [{"name": "id"}, {"name": "model"}]

    with patch.object(mod.sa, "inspect", return_value=mock_inspector), patch.object(mod, "op") as mock_op:
        mod.upgrade()
        assert mock_op.add_column.call_count == 2


async def test_migration_034_upgrade_skip_when_exists() -> None:
    """Migration 034 upgrade is idempotent."""
    from unittest.mock import MagicMock, patch

    mod = _import_migration("034")

    mock_inspector = MagicMock()
    mock_inspector.get_columns.return_value = [
        {"name": "id"},
        {"name": "pre_compression_input_tokens"},
        {"name": "tokens_saved"},
    ]

    with patch.object(mod.sa, "inspect", return_value=mock_inspector), patch.object(mod, "op") as mock_op:
        mod.upgrade()
        mock_op.add_column.assert_not_called()


async def test_migration_034_downgrade_drops_columns() -> None:
    """Migration 034 downgrade drops both compression savings columns."""
    from unittest.mock import MagicMock, patch

    mod = _import_migration("034")

    mock_inspector = MagicMock()
    mock_inspector.get_columns.return_value = [
        {"name": "id"},
        {"name": "pre_compression_input_tokens"},
        {"name": "tokens_saved"},
    ]

    with patch.object(mod.sa, "inspect", return_value=mock_inspector), patch.object(mod, "op") as mock_op:
        mod.downgrade()
        mock_op.drop_column.assert_any_call("cost_records", "tokens_saved")
        mock_op.drop_column.assert_any_call("cost_records", "pre_compression_input_tokens")
        assert mock_op.drop_column.call_count == 2


async def test_migration_034_revision_chain() -> None:
    """Migration 034 follows 033."""
    mod = _import_migration("034")

    assert mod.revision == "034"
    assert mod.down_revision == "033"


async def test_migration_041_upgrade_adds_both_columns() -> None:
    """Migration 041 adds cost_records.num_turns and task_runs.phase_usage."""
    from unittest.mock import MagicMock, patch

    mod = _import_migration("041")

    mock_inspector = MagicMock()
    mock_inspector.get_columns.return_value = [{"name": "id"}]

    with patch.object(mod.sa, "inspect", return_value=mock_inspector), patch.object(mod, "op") as mock_op:
        mod.upgrade()
        tables = [c.args[0] for c in mock_op.batch_alter_table.call_args_list]
        batch_op = mock_op.batch_alter_table.return_value.__enter__.return_value
        added = [c.args[0].name for c in batch_op.add_column.call_args_list]
        assert tables == ["cost_records", "task_runs"]
        assert added == ["num_turns", "phase_usage"]


async def test_migration_041_upgrade_skip_when_exists() -> None:
    """Migration 041 upgrade is idempotent."""
    from unittest.mock import MagicMock, patch

    mod = _import_migration("041")

    mock_inspector = MagicMock()
    mock_inspector.get_columns.return_value = [{"name": "id"}, {"name": "num_turns"}, {"name": "phase_usage"}]

    with patch.object(mod.sa, "inspect", return_value=mock_inspector), patch.object(mod, "op") as mock_op:
        mod.upgrade()
        mock_op.batch_alter_table.assert_not_called()


async def test_migration_041_downgrade_drops_both_columns() -> None:
    from unittest.mock import MagicMock, patch

    mod = _import_migration("041")

    mock_inspector = MagicMock()
    mock_inspector.get_columns.return_value = [{"name": "id"}, {"name": "num_turns"}, {"name": "phase_usage"}]

    with patch.object(mod.sa, "inspect", return_value=mock_inspector), patch.object(mod, "op") as mock_op:
        mod.downgrade()
        # batch mode, so a SQLite build without native DROP COLUMN still downgrades (database-guidelines.md)
        tables = [c.args[0] for c in mock_op.batch_alter_table.call_args_list]
        batch_op = mock_op.batch_alter_table.return_value.__enter__.return_value
        dropped = [c.args[0] for c in batch_op.drop_column.call_args_list]
        assert tables == ["task_runs", "cost_records"]
        assert dropped == ["phase_usage", "num_turns"]
        mock_op.drop_column.assert_not_called()


async def test_migration_041_skips_a_missing_table() -> None:
    """A partial schema (no cost_records yet) is left to create_all instead of crashing the upgrade."""
    from unittest.mock import MagicMock, patch

    mod = _import_migration("041")

    mock_inspector = MagicMock()
    mock_inspector.has_table.side_effect = lambda name: name == "task_runs"
    mock_inspector.get_columns.return_value = [{"name": "id"}]

    with patch.object(mod.sa, "inspect", return_value=mock_inspector), patch.object(mod, "op") as mock_op:
        mod.upgrade()
        mod.downgrade()
        # Only the existing table is altered, once, by the upgrade; the downgrade finds nothing to drop.
        assert [c.args[0] for c in mock_op.batch_alter_table.call_args_list] == ["task_runs"]


async def test_migration_041_revision_chain() -> None:
    mod = _import_migration("041")

    assert mod.revision == "041"
    assert mod.down_revision == "040"


async def test_migration_041_round_trips_on_a_real_sqlite_file(tmp_path: Path) -> None:
    """Upgrade to head adds both nullable columns; downgrade to 040 removes them; re-upgrade restores them."""
    import asyncio
    import sqlite3
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    db_path = tmp_path / "turns.db"
    alembic_cfg = Config(str(Path(__file__).parent.parent / "sova" / "db" / "alembic.ini"))
    alembic_cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db_path}")

    def columns() -> tuple[set[str], set[str]]:
        raw = sqlite3.connect(str(db_path))
        try:
            cost = {r[1] for r in raw.execute("PRAGMA table_info(cost_records)")}
            runs = {r[1] for r in raw.execute("PRAGMA table_info(task_runs)")}
        finally:
            raw.close()
        return cost, runs

    await asyncio.to_thread(command.upgrade, alembic_cfg, "head")
    cost, runs = columns()
    assert "num_turns" in cost and "phase_usage" in runs

    await asyncio.to_thread(command.downgrade, alembic_cfg, "040")
    cost, runs = columns()
    assert "num_turns" not in cost and "phase_usage" not in runs

    await asyncio.to_thread(command.upgrade, alembic_cfg, "head")
    cost, runs = columns()
    assert "num_turns" in cost and "phase_usage" in runs


async def test_migration_042_revision_chain() -> None:
    mod = _import_migration("042")

    assert mod.revision == "042"
    assert mod.down_revision == "041"


async def test_migration_042_round_trips_on_a_real_sqlite_file(tmp_path: Path) -> None:
    """Upgrade makes the CPU columns nullable and keeps rows; downgrade restores NOT NULL with 0.0 for NULLs."""
    import asyncio
    import sqlite3
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    db_path = tmp_path / "nullable_cpu.db"
    alembic_cfg = Config(str(Path(__file__).parent.parent / "sova" / "db" / "alembic.ini"))
    alembic_cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db_path}")

    def not_null() -> dict[tuple[str, str], int]:
        raw = sqlite3.connect(str(db_path))
        try:
            return {
                (table, row[1]): row[3]
                for table in ("resource_samples", "resource_summaries")
                for row in raw.execute(f"PRAGMA table_info({table})")
                if row[1] in {"cpu_percent", "peak_cpu_percent", "avg_cpu_percent"}
            }
        finally:
            raw.close()

    expected = {
        ("resource_samples", "cpu_percent"),
        ("resource_summaries", "peak_cpu_percent"),
        ("resource_summaries", "avg_cpu_percent"),
    }
    await asyncio.to_thread(command.upgrade, alembic_cfg, "head")
    assert not_null().keys() == expected
    assert set(not_null().values()) == {0}

    raw = sqlite3.connect(str(db_path))
    try:
        raw.execute(
            "INSERT INTO task_runs (id, issue_number, role, status, project_slug) VALUES (1, '1', 'd', 'x', 'p')"
        )
        raw.execute(
            "INSERT INTO resource_samples (task_run_id, sampled_at, cpu_percent, memory_rss_bytes, memory_vms_bytes, "
            "num_children, num_threads) VALUES (1, '2026-11-11 00:00:00', NULL, 10, 20, 0, 1)"
        )
        raw.commit()
    finally:
        raw.close()

    await asyncio.to_thread(command.downgrade, alembic_cfg, "041")
    assert set(not_null().values()) == {1}
    raw = sqlite3.connect(str(db_path))
    try:
        assert raw.execute("SELECT cpu_percent, memory_rss_bytes FROM resource_samples").fetchall() == [(0, 10)]
    finally:
        raw.close()

    await asyncio.to_thread(command.upgrade, alembic_cfg, "head")
    assert set(not_null().values()) == {0}


async def test_migration_043_creates_and_drops_pr_retros(tmp_path: Path) -> None:
    import asyncio
    import sqlite3

    from alembic import command
    from alembic.config import Config

    mod = _import_migration("043")
    assert (mod.revision, mod.down_revision) == ("043", "042")

    db_path = tmp_path / "retro.db"
    alembic_cfg = Config(str(Path(__file__).parent.parent / "sova" / "db" / "alembic.ini"))
    alembic_cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db_path}")

    def columns() -> set[str]:
        raw = sqlite3.connect(str(db_path))
        try:
            return {row[1] for row in raw.execute("PRAGMA table_info(pr_retros)")}
        finally:
            raw.close()

    await asyncio.to_thread(command.upgrade, alembic_cfg, "head")
    assert {"pr_number", "merged_at", "findings", "kinds_digest", "classify_cost_usd"} <= columns()

    await asyncio.to_thread(command.downgrade, alembic_cfg, "042")
    assert columns() == set()


def _alembic_config_for(db_path: Path, **attributes: object):
    from alembic.config import Config

    cfg = Config(str(Path(__file__).parent.parent / "sova" / "db" / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db_path}")
    cfg.attributes.update(attributes)
    return cfg


def _table_names(db_path: Path) -> set[str]:
    import sqlite3

    raw = sqlite3.connect(str(db_path))
    try:
        return {row[0] for row in raw.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    finally:
        raw.close()


def _seed_memories(db_path: Path, *, edges: bool) -> None:
    import sqlite3

    raw = sqlite3.connect(str(db_path))
    try:
        raw.execute(
            "INSERT INTO memories (id, category, title, content, embedding, created_at, updated_at) "
            "VALUES (1, 'learning', 'first', 'body', '[0.5, 0.25]', '2026-11-01 10:00:00', '2026-11-01 10:00:00')"
        )
        raw.execute(
            "INSERT INTO memories (id, category, title, content, created_at, updated_at) "
            "VALUES (2, 'learning', 'second', 'body', '2026-11-02 10:00:00', '2026-11-02 10:00:00')"
        )
        if edges:
            raw.execute(
                "INSERT INTO memory_edges (source_id, target_id, relation, weight) VALUES (1, 2, 'relates_to', 0.75)"
            )
        raw.commit()
    finally:
        raw.close()


async def test_migration_044_revision_chain() -> None:
    mod = _import_migration("044")

    assert (mod.revision, mod.down_revision) == ("044", "043")


async def test_migration_044_exports_both_tables_then_drops_them(tmp_path: Path) -> None:
    import asyncio
    import json

    from alembic import command

    db_path = tmp_path / ".claude" / "sova.db"
    db_path.parent.mkdir()
    cfg = _alembic_config_for(db_path)
    await asyncio.to_thread(command.upgrade, cfg, "043")
    _seed_memories(db_path, edges=True)

    await asyncio.to_thread(command.upgrade, cfg, "head")

    assert not {"memories", "memory_edges"} & _table_names(db_path)
    (export,) = db_path.parent.glob("memories-export-*.json")
    data = json.loads(export.read_text(encoding="utf-8"))
    assert [m["title"] for m in data["memories"]] == ["first", "second"]
    assert data["memories"][0]["embedding"] == [0.5, 0.25]
    assert data["memories"][0]["created_at"] == "2026-11-01 10:00:00"
    assert [(e["source_id"], e["target_id"], e["weight"]) for e in data["memory_edges"]] == [(1, 2, 0.75)]


async def test_migration_044_exports_a_value_the_orm_types_cannot_parse(tmp_path: Path) -> None:
    """Rows are read as stored, so a malformed datetime string neither aborts the migration nor changes."""
    import asyncio
    import json
    import sqlite3

    from alembic import command

    db_path = tmp_path / ".claude" / "sova.db"
    db_path.parent.mkdir()
    cfg = _alembic_config_for(db_path)
    await asyncio.to_thread(command.upgrade, cfg, "043")
    _seed_memories(db_path, edges=False)
    raw = sqlite3.connect(str(db_path))
    try:
        raw.execute("UPDATE memories SET created_at = 'not-a-date', embedding = 'not json' WHERE id = 2")
        raw.commit()
    finally:
        raw.close()

    await asyncio.to_thread(command.upgrade, cfg, "head")

    assert not {"memories", "memory_edges"} & _table_names(db_path)
    (export,) = db_path.parent.glob("memories-export-*.json")
    second = json.loads(export.read_text(encoding="utf-8"))["memories"][1]
    assert (second["created_at"], second["embedding"]) == ("not-a-date", "not json")


async def test_migration_044_deletes_the_orphan_sweep_marker(tmp_path: Path) -> None:
    import asyncio
    import sqlite3

    from alembic import command

    db_path = tmp_path / ".claude" / "sova.db"
    db_path.parent.mkdir()
    cfg = _alembic_config_for(db_path)
    await asyncio.to_thread(command.upgrade, cfg, "043")
    raw = sqlite3.connect(str(db_path))
    try:
        raw.execute("INSERT INTO project_settings (key, value) VALUES ('db.orphan_edges_swept', 'true')")
        raw.execute("INSERT INTO project_settings (key, value) VALUES ('github_repo', '\"o/r\"')")
        raw.commit()
    finally:
        raw.close()

    await asyncio.to_thread(command.upgrade, cfg, "head")

    raw = sqlite3.connect(str(db_path))
    try:
        assert [r[0] for r in raw.execute("SELECT key FROM project_settings")] == ["github_repo"]
    finally:
        raw.close()


async def test_migration_044_rows_in_one_table_still_write_both_keys(tmp_path: Path) -> None:
    import asyncio
    import json

    from alembic import command

    db_path = tmp_path / ".claude" / "sova.db"
    db_path.parent.mkdir()
    cfg = _alembic_config_for(db_path)
    await asyncio.to_thread(command.upgrade, cfg, "043")
    _seed_memories(db_path, edges=False)

    await asyncio.to_thread(command.upgrade, cfg, "head")

    (export,) = db_path.parent.glob("memories-export-*.json")
    data = json.loads(export.read_text(encoding="utf-8"))
    assert len(data["memories"]) == 2
    assert data["memory_edges"] == []


async def test_migration_044_writes_into_the_configured_directory(tmp_path: Path) -> None:
    """``_run_migrations()`` hands the project's ``.claude/`` over; it is created when missing."""
    import asyncio

    from alembic import command

    db_path = tmp_path / "elsewhere" / "sova.db"
    db_path.parent.mkdir()
    export_dir = tmp_path / "project" / ".claude"
    cfg = _alembic_config_for(db_path, memories_export_dir=export_dir)
    await asyncio.to_thread(command.upgrade, cfg, "043")
    _seed_memories(db_path, edges=False)

    await asyncio.to_thread(command.upgrade, cfg, "head")

    assert len(list(export_dir.glob("memories-export-*.json"))) == 1
    assert not list(db_path.parent.glob("memories-export-*.json"))


@pytest.mark.parametrize("tables_present", [True, False], ids=["empty tables", "tables absent"])
async def test_migration_044_writes_no_file_without_rows(tmp_path: Path, tables_present: bool) -> None:
    import asyncio
    import sqlite3

    from alembic import command

    db_path = tmp_path / ".claude" / "sova.db"
    db_path.parent.mkdir()
    cfg = _alembic_config_for(db_path)
    await asyncio.to_thread(command.upgrade, cfg, "043")
    if not tables_present:
        raw = sqlite3.connect(str(db_path))
        try:
            raw.execute("DROP TABLE memory_edges")
            raw.execute("DROP TABLE memories")
            raw.commit()
        finally:
            raw.close()

    await asyncio.to_thread(command.upgrade, cfg, "head")

    assert not {"memories", "memory_edges"} & _table_names(db_path)
    assert not list(db_path.parent.glob("memories-export-*.json"))


async def test_migration_044_aborts_with_tables_intact_when_the_export_fails(tmp_path: Path) -> None:
    import asyncio

    from alembic import command

    db_path = tmp_path / ".claude" / "sova.db"
    db_path.parent.mkdir()
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("in the way", encoding="utf-8")
    cfg = _alembic_config_for(db_path, memories_export_dir=blocker / ".claude")
    await asyncio.to_thread(command.upgrade, cfg, "043")
    _seed_memories(db_path, edges=True)

    with pytest.raises(OSError):
        await asyncio.to_thread(command.upgrade, cfg, "head")

    assert {"memories", "memory_edges"} <= _table_names(db_path)


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
async def test_migration_044_export_is_owner_only(tmp_path: Path) -> None:
    mod = _import_migration("044")
    old_umask = os.umask(0o022)
    try:
        written = mod._write_export(tmp_path, {"memories": [], "memory_edges": []})
    finally:
        os.umask(old_umask)

    assert written.stat().st_mode & 0o777 == 0o600


async def test_migration_044_never_overwrites_an_existing_export(tmp_path: Path) -> None:
    from datetime import timezone
    from unittest.mock import patch

    mod = _import_migration("044")
    fixed = datetime(2026, 11, 13, 12, 0, 0, tzinfo=timezone.utc)
    existing = tmp_path / "memories-export-20261113T120000000000Z.json"
    existing.write_text("keep me", encoding="utf-8")

    with patch.object(mod, "datetime") as fake_datetime:
        fake_datetime.now.return_value = fixed
        written = mod._write_export(tmp_path, {"memories": [], "memory_edges": []})

    assert written.name == "memories-export-20261113T120000000000Z-1.json"
    assert existing.read_text(encoding="utf-8") == "keep me"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (datetime(2026, 11, 1, 10, 0, 0), "2026-11-01T10:00:00"),
        (Decimal("0.7500"), "0.7500"),
        (b"\x00\xff", "AP8="),
        (memoryview(b"\x00\xff"), "AP8="),
        (object, "<class 'object'>"),
    ],
    ids=["datetime", "decimal", "bytes", "memoryview", "other"],
)
async def test_migration_044_serializes_every_value_type(value: object, expected: str) -> None:
    assert _import_migration("044")._json_default(value) == expected


def _memory_schema(db_path: Path) -> dict[str, object]:
    """Columns, indexes and foreign keys of the two memory tables, for comparing two schema states."""
    import sqlite3

    raw = sqlite3.connect(str(db_path))
    try:
        schema: dict[str, object] = {}
        for table in ("memories", "memory_edges"):
            schema[f"{table}.columns"] = sorted(
                (r[1], r[2], r[3], r[5]) for r in raw.execute(f"PRAGMA table_info({table})")
            )
            schema[f"{table}.indexes"] = sorted(r[1] for r in raw.execute(f"PRAGMA index_list({table})"))
            schema[f"{table}.fks"] = sorted(
                (r[2], r[3], r[4], r[6]) for r in raw.execute(f"PRAGMA foreign_key_list({table})")
            )
        return schema
    finally:
        raw.close()


async def test_migration_044_downgrade_recreates_both_tables_empty(tmp_path: Path) -> None:
    import asyncio
    import sqlite3

    from alembic import command

    reference = tmp_path / "ref" / "sova.db"
    reference.parent.mkdir()
    await asyncio.to_thread(command.upgrade, _alembic_config_for(reference), "043")
    db_path = tmp_path / ".claude" / "sova.db"
    db_path.parent.mkdir()
    cfg = _alembic_config_for(db_path)
    await asyncio.to_thread(command.upgrade, cfg, "head")
    assert not {"memories", "memory_edges"} & _table_names(db_path)

    await asyncio.to_thread(command.downgrade, cfg, "043")

    raw = sqlite3.connect(str(db_path))
    try:
        assert raw.execute("SELECT COUNT(*) FROM memories").fetchone() == (0,)
        assert raw.execute("SELECT COUNT(*) FROM memory_edges").fetchone() == (0,)
    finally:
        raw.close()
    expected = _memory_schema(reference)
    actual = _memory_schema(db_path)
    assert actual["memories.fks"] == expected["memories.fks"] == []
    assert actual["memory_edges.fks"] == expected["memory_edges.fks"]
    # The auto-named unique-constraint index differs only by name; compare the named indexes.
    named = lambda idx: sorted(i for i in idx if i.startswith("ix_"))  # noqa: E731
    assert named(actual["memories.indexes"]) == named(expected["memories.indexes"])
    assert named(actual["memory_edges.indexes"]) == named(expected["memory_edges.indexes"])


async def test_init_db_exports_old_memories_into_the_project_claude_dir(tmp_path: Path, monkeypatch) -> None:
    """The server's own startup migration, not only the alembic CLI, writes the export under the project."""
    import asyncio

    from alembic import command

    monkeypatch.delenv("SOVA_DATABASE_URL", raising=False)
    db_path = tmp_path / ".claude" / "sova.db"
    db_path.parent.mkdir()
    await asyncio.to_thread(command.upgrade, _alembic_config_for(db_path), "043")
    _seed_memories(db_path, edges=True)

    await init_db(tmp_path)
    try:
        assert len(list((tmp_path / ".claude").glob("memories-export-*.json"))) == 1
        assert not {"memories", "memory_edges"} & _table_names(db_path)
    finally:
        await close_db()


async def test_init_db_without_project_dir_exports_next_to_the_global_db(tmp_path: Path, monkeypatch) -> None:
    """The bare ``init_db()`` uses the global database, so its export never lands in an ambient project."""
    import asyncio

    from alembic import command

    monkeypatch.delenv("SOVA_DATABASE_URL", raising=False)
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    db_path = home / ".config" / "sova" / "sova.db"
    db_path.parent.mkdir(parents=True)
    project = tmp_path / "project"
    project.mkdir()
    await asyncio.to_thread(command.upgrade, _alembic_config_for(db_path), "043")
    _seed_memories(db_path, edges=True)
    monkeypatch.setattr("sova.config.context.get_project_dir", lambda: project)

    await init_db()
    try:
        assert len(list(db_path.parent.glob("memories-export-*.json"))) == 1
        assert not (project / ".claude").exists()
    finally:
        await close_db()


async def test_init_db_ambient_project_owning_the_database_receives_the_export(tmp_path: Path, monkeypatch) -> None:
    from sova.db.session import _ambient_project_owning, _get_database_url

    monkeypatch.delenv("SOVA_DATABASE_URL", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    monkeypatch.setattr("sova.config.context.get_project_dir", lambda: tmp_path)

    assert _ambient_project_owning(_get_database_url(tmp_path)) == tmp_path
    assert _ambient_project_owning(_get_database_url(None)) is None


async def test_init_db_with_no_project_context_writes_nothing_under_the_cwd(tmp_path: Path, monkeypatch) -> None:
    import asyncio

    from alembic import command

    monkeypatch.delenv("SOVA_DATABASE_URL", raising=False)
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    db_path = home / ".config" / "sova" / "sova.db"
    db_path.parent.mkdir(parents=True)
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    await asyncio.to_thread(command.upgrade, _alembic_config_for(db_path), "043")
    _seed_memories(db_path, edges=False)

    await init_db()
    try:
        assert not (cwd / ".claude").exists()
        assert len(list(db_path.parent.glob("memories-export-*.json"))) == 1
    finally:
        await close_db()


@pytest.mark.parametrize(
    ("dialect", "database", "configured", "expected_reason"),
    [
        ("sqlite", "/data/proj/.claude/sova.db", "/cfg/.claude", "project"),
        ("postgresql", "sova", "/cfg/.claude", "project"),
        ("sqlite", "/data/proj/.claude/sova.db", None, "database_file"),
        ("postgresql", "sova", None, "fallback_state_dir"),
        ("sqlite", ":memory:", None, "fallback_state_dir"),
    ],
)
async def test_migration_044_export_dir_precedence(
    dialect: str, database: str, configured, expected_reason: str
) -> None:
    """A configured directory wins over the database file's directory, which wins over the state dir."""
    from unittest.mock import MagicMock, patch

    mod = _import_migration("044")
    bind = MagicMock()
    bind.dialect.name = dialect
    bind.engine.url.database = database
    attributes = {} if configured is None else {"memories_export_dir": configured}
    with patch.object(mod.op, "get_context") as get_context:
        get_context.return_value.config.attributes = attributes
        path, reason = mod._export_dir(bind)

    assert reason == expected_reason
    expected_path = {
        "project": Path(configured) if configured else None,
        "database_file": Path(database).parent,
        "fallback_state_dir": Path.home() / ".config" / "sova",
    }[expected_reason]
    assert path == expected_path


@pytest.mark.skipif(not os.environ.get("SOVA_TEST_POSTGRES_URL"), reason="SOVA_TEST_POSTGRES_URL not set")
async def test_migration_044_drops_the_tables_on_postgresql(tmp_path: Path) -> None:
    """Needs an empty PostgreSQL database (refused otherwise: the cleanup downgrades to base)."""
    import asyncio
    import json

    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, inspect, text

    url = os.environ["SOVA_TEST_POSTGRES_URL"]
    sync_url = url.replace("+asyncpg", "+psycopg")
    engine = create_engine(sync_url)
    assert not inspect(engine).get_table_names(), "SOVA_TEST_POSTGRES_URL must point at an empty database"
    cfg = Config(str(Path(__file__).parent.parent / "sova" / "db" / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    cfg.attributes["memories_export_dir"] = tmp_path
    try:
        await asyncio.to_thread(command.upgrade, cfg, "043")
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO memories (category, title, content) VALUES ('learning', 't', 'c')"))
            conn.execute(text("INSERT INTO memories (category, title, content) VALUES ('learning', 'u', 'd')"))
            conn.execute(
                text(
                    "INSERT INTO memory_edges (source_id, target_id, relation, weight) "
                    "SELECT a.id, b.id, 'relates_to', 0.75 FROM memories a, memories b "
                    "WHERE a.title = 't' AND b.title = 'u'"
                )
            )

        await asyncio.to_thread(command.upgrade, cfg, "head")

        tables = set(inspect(engine).get_table_names())
        assert not {"memories", "memory_edges"} & tables
        (export,) = tmp_path.glob("memories-export-*.json")
        data = json.loads(export.read_text(encoding="utf-8"))
        assert [m["title"] for m in data["memories"]] == ["t", "u"]
        assert data["memories"][0]["created_at"]
        assert [float(e["weight"]) for e in data["memory_edges"]] == [0.75]
    finally:
        await asyncio.to_thread(command.downgrade, cfg, "base")
        engine.dispose()


async def test_register_sqlite_pragmas_sets_synchronous_normal(tmp_path) -> None:
    """Every new connection should get PRAGMA synchronous=NORMAL, not the FULL default.

    FULL fsyncs on every commit; under concurrent agent writers the commit
    window grows long enough to exhaust the 30s busy timeout. synchronous is
    per-connection (unlike journal_mode, which persists in the file), so this
    must run on the SQLAlchemy "connect" event rather than once at startup.
    """
    from sqlalchemy.ext.asyncio import create_async_engine

    from sova.db.session import _register_sqlite_pragmas

    db_path = tmp_path / "pragma_test.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    _register_sqlite_pragmas(engine)
    try:
        async with engine.connect() as conn:
            result = await conn.exec_driver_sql("PRAGMA synchronous")
            row = result.fetchone()
            assert row[0] == 1, f"expected synchronous=NORMAL (1), got {row[0]}"
    finally:
        await engine.dispose()


async def test_register_sqlite_pragmas_swallows_exception(tmp_path) -> None:
    """A failure setting PRAGMAs on connect must be logged and swallowed, not raised.

    Invokes the registered "connect" listener directly with a fake DBAPI
    connection whose cursor() raises: going through a real engine.connect()
    is not viable here, since the dialect's own "connect" listener (registered
    before ours) already calls cursor() first for get_isolation_level(), so a
    generically flaky cursor breaks dialect initialization instead of
    exercising our pragma listener's except branch.
    """
    from sqlalchemy.ext.asyncio import create_async_engine

    from sova.db.session import _register_sqlite_pragmas

    db_path = tmp_path / "pragma_fail_test.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    _register_sqlite_pragmas(engine)

    pragma_listener = list(engine.sync_engine.pool.dispatch.connect)[-1]

    class _RaisingConnection:
        def cursor(self):
            raise RuntimeError("boom")

    pragma_listener(_RaisingConnection(), None)  # must not raise

    await engine.dispose()


async def test_create_task_assessment() -> None:
    """Create a task assessment record."""
    async with await get_session() as session:
        assessment = TaskAssessmentRecord(
            issue_number="55",
            project_slug="test",
            suitability="ready",
            confidence=0.85,
            reasoning="Well-defined task with clear acceptance criteria",
            missing_context=[],
            estimated_complexity="simple",
            suggested_role="developer",
        )
        session.add(assessment)
        await session.commit()
        await session.refresh(assessment)

        assert assessment.suitability == "ready"
        assert float(assessment.confidence) == pytest.approx(0.85)


async def test_task_assessment_default_project_slug() -> None:
    """TaskAssessmentRecord without explicit project_slug defaults to empty string."""
    async with await get_session() as session:
        assessment = TaskAssessmentRecord(
            issue_number="99",
            suitability="needs_spec",
            confidence=0.60,
            reasoning="Missing acceptance criteria",
        )
        session.add(assessment)
        await session.commit()
        await session.refresh(assessment)

        assert assessment.project_slug == ""
        assert assessment.issue_number == "99"


async def test_query_task_runs_by_status() -> None:
    """Query task runs filtered by status."""
    async with await get_session() as session:
        session.add(TaskRun(issue_number="1", status="done", role="developer"))
        session.add(TaskRun(issue_number="2", status="in_progress", role="developer"))
        session.add(TaskRun(issue_number="3", status="done", role="reviewer"))
        await session.commit()

        result = await session.execute(select(TaskRun).where(TaskRun.status == "done"))
        done_runs = result.scalars().all()
        assert len(done_runs) == 2


async def test_assessments_project_slug_index_exists() -> None:
    """task_assessments table has an index on project_slug for multi-project filtering."""
    async with await get_session() as session:
        conn = await session.connection()
        indexes = await conn.run_sync(lambda sync_conn: inspect(sync_conn).get_indexes("task_assessments"))
    index_names = {idx["name"] for idx in indexes}
    assert "ix_assessments_project_slug" in index_names


async def test_filter_assessments_by_project_slug() -> None:
    """Queries filtering on project_slug benefit from the new index."""
    async with await get_session() as session:
        session.add(
            TaskAssessmentRecord(
                issue_number="1",
                project_slug="alpha",
                suitability="ready",
                confidence=0.9,
                reasoning="Good",
            )
        )
        session.add(
            TaskAssessmentRecord(
                issue_number="2",
                project_slug="beta",
                suitability="ready",
                confidence=0.8,
                reasoning="OK",
            )
        )
        session.add(
            TaskAssessmentRecord(
                issue_number="3",
                project_slug="alpha",
                suitability="needs_spec",
                confidence=0.6,
                reasoning="Thin",
            )
        )
        await session.commit()

        result = await session.execute(select(TaskAssessmentRecord).where(TaskAssessmentRecord.project_slug == "alpha"))
        alpha = result.scalars().all()
        assert len(alpha) == 2


# ---------------------------------------------------------------------------
# Migration fallback self-healing
# ---------------------------------------------------------------------------


class TestMigrationFallback:
    """The create_all+stamp fallback is scoped to a genuinely fresh database.

    A database that already has tables (pre-Alembic, or stamped behind head)
    must re-raise on a migration failure instead of silently stamping over
    unknown prior state: see issue #1205.
    """

    async def test_bogus_version_reraises_instead_of_self_healing(self, tmp_path) -> None:
        """A stamped revision outside the chain must propagate, not self-heal.

        Self-healing here would silently stamp a database with unverified prior
        state at head, masking whatever DDL never actually ran.
        """
        from alembic.util.exc import CommandError
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine

        from sova.db.session import _run_migrations

        db_path = tmp_path / "test.db"
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{db_path}",
            connect_args={"check_same_thread": False},
        )

        # First run: create tables and stamp at head
        await _run_migrations(engine)

        # Corrupt: set a bogus version_num that does not exist in the revision chain.
        async with engine.begin() as conn:
            await conn.execute(text("UPDATE alembic_version SET version_num = 'bogus_xyz'"))

        with pytest.raises(CommandError, match="Can't locate revision"):
            await _run_migrations(engine)

        # alembic_version must remain untouched, not silently repaired at head.
        async with engine.connect() as conn:
            row = await conn.run_sync(lambda c: c.execute(text("SELECT version_num FROM alembic_version")).fetchone())
        assert row[0] == "bogus_xyz"

        await engine.dispose()

    async def test_upgrade_failure_on_existing_schema_reraises(self, tmp_path) -> None:
        """A migration failure on a DB with real tables must propagate, not self-heal."""
        from unittest.mock import patch

        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine

        from sova.db.session import _run_migrations

        db_path = tmp_path / "real-schema-fail.db"
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{db_path}",
            connect_args={"check_same_thread": False},
        )

        await _run_migrations(engine)
        async with engine.begin() as conn:
            await conn.execute(text("UPDATE alembic_version SET version_num = '001'"))

        with (
            patch("alembic.command.upgrade", side_effect=RuntimeError("boom")),
            pytest.raises(RuntimeError, match="boom"),
        ):
            await _run_migrations(engine)

        async with engine.connect() as conn:
            row = await conn.run_sync(lambda c: c.execute(text("SELECT version_num FROM alembic_version")).fetchone())
        assert row[0] == "001"

        await engine.dispose()

    async def test_stamp_failure_on_pre_alembic_db_reraises(self, tmp_path) -> None:
        """A pre-Alembic DB with real tables (stamp path) must also re-raise on failure."""
        from unittest.mock import patch

        from sqlalchemy.ext.asyncio import create_async_engine

        from sova.db.models import Base
        from sova.db.session import _run_migrations

        db_path = tmp_path / "pre-alembic-fail.db"
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{db_path}",
            connect_args={"check_same_thread": False},
        )
        # Simulate a pre-Alembic database: real tables exist, no alembic_version.
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        with (
            patch("alembic.command.stamp", side_effect=RuntimeError("boom")),
            pytest.raises(RuntimeError, match="boom"),
        ):
            await _run_migrations(engine)

        async with engine.connect() as conn:
            table_names = await conn.run_sync(lambda c: inspect(c).get_table_names())
        assert "alembic_version" not in table_names

        await engine.dispose()

    async def test_fresh_db_upgrade_failure_still_falls_back(self, tmp_path) -> None:
        """A genuinely fresh DB (no tables at all) must still self-heal on failure."""
        from unittest.mock import patch

        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine

        from sova.db.session import _run_migrations

        db_path = tmp_path / "fresh-fail.db"
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{db_path}",
            connect_args={"check_same_thread": False},
        )
        # Force the file to exist and be non-empty so the fallback's create_all has
        # something to run against (the backup-before-DDL path is exercised too).
        async with engine.begin() as conn:
            await conn.execute(text("CREATE TABLE _touch (id INTEGER PRIMARY KEY)"))
            await conn.execute(text("DROP TABLE _touch"))

        with patch("alembic.command.upgrade", side_effect=RuntimeError("boom")):
            result = await _run_migrations(engine)

        assert result is True
        async with engine.connect() as conn:
            row = await conn.run_sync(lambda c: c.execute(text("SELECT version_num FROM alembic_version")).fetchone())
        assert row is not None

        await engine.dispose()

    async def test_empty_alembic_version_self_heals(self, tmp_path) -> None:
        """An empty alembic_version table (case 4) should be dropped and re-stamped."""
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine

        from sova.db.session import _run_migrations

        db_path = tmp_path / "test.db"
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{db_path}",
            connect_args={"check_same_thread": False},
        )

        await _run_migrations(engine)

        # Corrupt: empty the version table
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM alembic_version"))

        await _run_migrations(engine)

        async with engine.connect() as conn:
            row = await conn.run_sync(lambda c: c.execute(text("SELECT version_num FROM alembic_version")).fetchone())
        assert row is not None
        assert row[0] != ""

        await engine.dispose()

    async def test_blank_version_num_self_heals(self, tmp_path) -> None:
        """A single-table DB with version_num='' (case 5) must self-heal, not raise.

        A row that exists but holds an empty/whitespace string is just as corrupt
        as no row at all: left in place, Alembic's own MigrationContext chokes on
        it with a bare AssertionError. It must be dropped and treated like a fresh
        database, same as the no-row case covered by
        test_empty_alembic_version_self_heals above.
        """
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine

        from sova.db.session import _run_migrations

        db_path = tmp_path / "blank-version.db"
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{db_path}",
            connect_args={"check_same_thread": False},
        )

        # A database whose only table is alembic_version, holding a blank version_num.
        async with engine.begin() as conn:
            await conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
            await conn.execute(text("INSERT INTO alembic_version (version_num) VALUES ('   ')"))

        result = await _run_migrations(engine)

        assert result is True
        async with engine.connect() as conn:
            table_names = await conn.run_sync(lambda c: inspect(c).get_table_names())
            row = await conn.run_sync(lambda c: c.execute(text("SELECT version_num FROM alembic_version")).fetchone())
        assert "task_runs" in table_names
        assert row is not None
        assert row[0].strip() != ""

        await engine.dispose()


# ---------------------------------------------------------------------------
# Issue-less TaskRun (nullable issue_number + run_label)
# ---------------------------------------------------------------------------


async def test_create_issueless_task_run() -> None:
    """Create a TaskRun with no issue_number (project-scope role)."""
    async with await get_session() as session:
        run = TaskRun(
            issue_number=None,
            run_label="planner-1718640000",
            role="planner",
            status="pending",
        )
        session.add(run)
        await session.commit()
        await session.refresh(run)

        assert run.id is not None
        assert not run.issue_number  # None or empty string
        assert run.run_label == "planner-1718640000"
        assert run.role == "planner"


async def test_issueless_task_run_empty_string() -> None:
    """Create a TaskRun with empty string issue_number."""
    async with await get_session() as session:
        run = TaskRun(
            issue_number="",
            run_label="sprint-planner",
            role="planner",
            status="running",
        )
        session.add(run)
        await session.commit()
        await session.refresh(run)

        assert run.issue_number == ""
        assert run.run_label == "sprint-planner"


async def test_normalize_issue_number_handles_none() -> None:
    """The validator should accept None for issue-less runs."""
    run = TaskRun(issue_number=None, role="planner", status="pending")
    assert not run.issue_number  # None or empty string


async def test_normalize_issue_number_strips_hash() -> None:
    """The validator should strip '#' prefix."""
    run = TaskRun(issue_number="#42", role="developer", status="pending")
    assert run.issue_number == "42"


async def test_query_issueless_runs() -> None:
    """Query runs with NULL issue_number."""
    async with await get_session() as session:
        session.add(TaskRun(issue_number=None, run_label="plan-a", role="planner", status="done"))
        session.add(TaskRun(issue_number="42", role="developer", status="done"))
        session.add(TaskRun(issue_number=None, run_label="plan-b", role="planner", status="running"))
        session.add(TaskRun(issue_number="", run_label="plan-c", role="planner", status="done"))
        await session.commit()

        # Query runs without a real issue number (None or empty)
        result = await session.execute(
            select(TaskRun).where(TaskRun.issue_number.is_(None) | (TaskRun.issue_number == ""))
        )
        issueless = result.scalars().all()
        assert len(issueless) == 3
        labels = {r.run_label for r in issueless}
        assert labels == {"plan-a", "plan-b", "plan-c"}


# ---------------------------------------------------------------------------
# FK index coverage (issue #235)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "table_name,index_name",
    [
        ("task_runs", "ix_task_runs_lifecycle_id"),
        ("task_runs", "ix_task_runs_workflow_definition_id"),
        ("lifecycle_phases", "ix_lifecycle_phases_task_run_id"),
    ],
)
async def test_fk_index_exists(table_name: str, index_name: str) -> None:
    """FK columns have indexes."""
    async with await get_session() as session:
        conn = await session.connection()
        indexes = await conn.run_sync(lambda sync_conn: inspect(sync_conn).get_indexes(table_name))
    index_names = {idx["name"] for idx in indexes}
    assert index_name in index_names


async def test_lifecycle_phases_composite_index_exists() -> None:
    """lifecycle_phases has composite index on (lifecycle_id, phase)."""
    async with await get_session() as session:
        conn = await session.connection()
        indexes = await conn.run_sync(lambda sync_conn: inspect(sync_conn).get_indexes("lifecycle_phases"))
    composite = [idx for idx in indexes if idx["name"] == "ix_lifecycle_phases_lifecycle_phase"]
    assert len(composite) == 1
    assert composite[0]["column_names"] == ["lifecycle_id", "phase"]


async def test_old_lifecycle_phases_lifecycle_index_replaced() -> None:
    """The old single-column ix_lifecycle_phases_lifecycle index should not exist."""
    async with await get_session() as session:
        conn = await session.connection()
        indexes = await conn.run_sync(lambda sync_conn: inspect(sync_conn).get_indexes("lifecycle_phases"))
    index_names = {idx["name"] for idx in indexes}
    assert "ix_lifecycle_phases_lifecycle" not in index_names


async def test_lifecycle_phases_query_benefits_from_composite_index() -> None:
    """Query filtering on (lifecycle_id, phase) works correctly."""
    async with await get_session() as session:
        lc = IssueLifecycle(issue_number="50", project_slug="test")
        session.add(lc)
        await session.commit()
        await session.refresh(lc)

        session.add(LifecyclePhaseRecord(lifecycle_id=lc.id, phase="development", status="active"))
        session.add(LifecyclePhaseRecord(lifecycle_id=lc.id, phase="review", status="pending"))
        await session.commit()

        result = await session.execute(
            select(LifecyclePhaseRecord).where(
                LifecyclePhaseRecord.lifecycle_id == lc.id,
                LifecyclePhaseRecord.phase == "development",
            )
        )
        records = result.scalars().all()
        assert len(records) == 1
        assert records[0].status == "active"


# ---------------------------------------------------------------------------
# Migration 012 tests
# ---------------------------------------------------------------------------


async def test_migration_012_get_index_names_helper() -> None:
    """Migration 012 _get_index_names correctly collects index names."""
    from unittest.mock import MagicMock, patch

    from alembic import op

    mod = _import_migration("012")

    mock_conn = MagicMock()
    mock_inspector = MagicMock()
    mock_inspector.get_indexes.return_value = [
        {"name": "ix_task_runs_lifecycle_id", "column_names": ["lifecycle_id"]},
        {"name": "ix_task_runs_issue", "column_names": ["issue_number"]},
    ]

    with patch.object(op, "get_bind", return_value=mock_conn), patch("sqlalchemy.inspect", return_value=mock_inspector):
        result = mod._get_index_names("task_runs")
        assert result == {"ix_task_runs_lifecycle_id", "ix_task_runs_issue"}


async def test_migration_012_upgrade_idempotent() -> None:
    """Migration 012 upgrade skips existing indexes."""
    from unittest.mock import patch

    mod = _import_migration("012")

    all_indexes = {
        "ix_task_runs_lifecycle_id",
        "ix_task_runs_workflow_definition_id",
        "ix_lifecycle_phases_task_run_id",
        "ix_lifecycle_phases_lifecycle_phase",
        "ix_lifecycle_phases_lifecycle",
    }

    with (
        patch.object(mod, "_get_index_names", return_value=all_indexes),
        patch.object(mod, "op") as mock_op,
    ):
        mod.upgrade()
        mock_op.create_index.assert_not_called()
        mock_op.drop_index.assert_called_once()


async def test_migration_012_upgrade_creates_indexes() -> None:
    """Migration 012 upgrade creates all indexes when none exist."""
    from unittest.mock import patch

    mod = _import_migration("012")

    with (
        patch.object(mod, "_get_index_names", return_value=set()),
        patch.object(mod, "op") as mock_op,
    ):
        mod.upgrade()
        assert mock_op.create_index.call_count == 4
        mock_op.drop_index.assert_not_called()


async def test_migration_012_downgrade_restores_old_index() -> None:
    """Migration 012 downgrade recreates the old single-column index."""
    from unittest.mock import patch

    mod = _import_migration("012")

    post_upgrade_indexes = {
        "ix_lifecycle_phases_lifecycle_phase",
        "ix_lifecycle_phases_task_run_id",
        "ix_task_runs_workflow_definition_id",
        "ix_task_runs_lifecycle_id",
    }

    with (
        patch.object(mod, "_get_index_names", return_value=post_upgrade_indexes),
        patch.object(mod, "op") as mock_op,
    ):
        mod.downgrade()
        mock_op.create_index.assert_called_once_with(
            "ix_lifecycle_phases_lifecycle", "lifecycle_phases", ["lifecycle_id"]
        )
        assert mock_op.drop_index.call_count == 4


# ---------------------------------------------------------------------------
# init_db_for_project lock (issue #235)
# ---------------------------------------------------------------------------


async def test_init_db_for_project_lock_prevents_concurrent_init(tmp_path) -> None:
    """Concurrent init_db_for_project calls should not duplicate work."""
    import asyncio
    from unittest.mock import patch

    from sova.db.session import _engines, init_db_for_project

    test_dir = tmp_path / "test-lock-project"
    test_url = f"sqlite+aiosqlite:///{test_dir}/.claude/sova.db"

    _engines.pop(test_url, None)

    call_count = 0

    async def slow_migrations(engine, project_dir):
        nonlocal call_count
        call_count += 1
        await asyncio.sleep(0.05)

    with (
        patch("sova.db.session._get_database_url", return_value=test_url),
        patch("sova.db.session._run_migrations", side_effect=slow_migrations),
        patch("sova.db.session._backup_db"),
        patch("sova.db.session._get_db_path_from_url", return_value=None),
    ):
        await asyncio.gather(
            init_db_for_project(test_dir),
            init_db_for_project(test_dir),
            init_db_for_project(test_dir),
        )

    assert call_count == 1
    _engines.pop(test_url, None)


async def test_init_db_for_project_disposes_sqlite_engine(tmp_path) -> None:
    """init_db_for_project disposes the engine after migration for SQLite DBs."""
    from unittest.mock import AsyncMock, patch

    from sova.db.session import _engines, init_db_for_project

    test_dir = tmp_path / "dispose-test"
    test_url = f"sqlite+aiosqlite:///{test_dir}/.claude/sova.db"
    _engines.pop(test_url, None)

    mock_engine = AsyncMock()
    mock_engine.dispose = AsyncMock()

    with (
        patch("sova.db.session._get_database_url", return_value=test_url),
        patch("sova.db.session._run_migrations", new_callable=AsyncMock, return_value=True),
        patch("sova.db.session._enable_sqlite_wal", new_callable=AsyncMock),
        patch("sova.db.session._backup_db"),
        patch("sova.db.session._get_db_path_from_url", return_value=tmp_path / "sova.db"),
        patch("sova.db.session.create_async_engine", return_value=mock_engine),
        patch("sova.db.session.async_sessionmaker"),
    ):
        await init_db_for_project(test_dir)
        mock_engine.dispose.assert_awaited_once()

    _engines.pop(test_url, None)  # dispose test cleanup


async def test_init_db_for_project_skips_dispose_for_non_sqlite(tmp_path) -> None:
    """init_db_for_project skips dispose for non-SQLite (e.g., PostgreSQL) DBs."""
    from unittest.mock import AsyncMock, patch

    from sova.db.session import _engines, init_db_for_project

    test_dir = tmp_path / "pg-test"
    test_url = "postgresql+asyncpg://localhost/test"
    _engines.pop(test_url, None)

    mock_engine = AsyncMock()

    with (
        patch("sova.db.session._get_database_url", return_value=test_url),
        patch("sova.db.session._run_migrations", new_callable=AsyncMock),
        patch("sova.db.session._backup_db"),
        patch("sova.db.session._get_db_path_from_url", return_value=None),
        patch("sova.db.session.create_async_engine", return_value=mock_engine),
        patch("sova.db.session.async_sessionmaker"),
    ):
        await init_db_for_project(test_dir)
        mock_engine.dispose.assert_not_awaited()

    _engines.pop(test_url, None)


# ---------------------------------------------------------------------------
# WAL mode + busy_timeout setup (_enable_sqlite_wal)
# ---------------------------------------------------------------------------


async def test_enable_sqlite_wal_sets_wal_mode(tmp_path) -> None:
    """_enable_sqlite_wal must switch the DB to WAL journal mode."""
    import sqlite3

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from sova.db.session import _enable_sqlite_wal

    db_path = tmp_path / "wal-test.db"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )

    async with engine.begin() as conn:
        await conn.execute(text("CREATE TABLE IF NOT EXISTS _probe (id INTEGER PRIMARY KEY)"))

    await _enable_sqlite_wal(engine)
    await engine.dispose()

    raw = sqlite3.connect(str(db_path))
    mode = raw.execute("PRAGMA journal_mode").fetchone()[0]
    raw.close()

    assert mode == "wal"


async def test_enable_sqlite_wal_is_idempotent(tmp_path) -> None:
    """Calling _enable_sqlite_wal twice must not raise."""
    from sqlalchemy.ext.asyncio import create_async_engine

    from sova.db.session import _enable_sqlite_wal

    db_path = tmp_path / "wal-idem.db"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )

    await _enable_sqlite_wal(engine)
    await _enable_sqlite_wal(engine)
    await engine.dispose()


# ---------------------------------------------------------------------------
# _run_migrations fast path (skip upgrade when already at head)
# ---------------------------------------------------------------------------


class TestRunMigrationsAtHead:
    """_run_migrations must skip the upgrade when DB is already at head."""

    async def test_fast_path_returns_false_when_at_head(self, tmp_path) -> None:
        """Second call on an up-to-date DB must return False (no DDL run)."""
        from sqlalchemy.ext.asyncio import create_async_engine

        from sova.db.session import _run_migrations

        db_path = tmp_path / "at-head.db"
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{db_path}",
            connect_args={"check_same_thread": False},
        )

        ddl1 = await _run_migrations(engine)
        assert ddl1 is True

        ddl2 = await _run_migrations(engine)
        assert ddl2 is False

        await engine.dispose()

    async def test_fast_path_does_not_call_alembic_upgrade(self, tmp_path) -> None:
        """When at head, alembic.command.upgrade must not be invoked."""
        from unittest.mock import patch

        from sqlalchemy.ext.asyncio import create_async_engine

        from sova.db.session import _run_migrations

        db_path = tmp_path / "skip-upgrade.db"
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{db_path}",
            connect_args={"check_same_thread": False},
        )

        await _run_migrations(engine)

        with patch("alembic.command.upgrade") as mock_upgrade:
            result = await _run_migrations(engine)

        assert result is False
        mock_upgrade.assert_not_called()
        await engine.dispose()

    async def test_upgrade_runs_when_behind_head(self, tmp_path) -> None:
        """When DB is behind head, upgrade must run and return True.

        Uses a real partial upgrade (stopping at an early revision), not a fresh
        head schema with the version stamp rewound: the real chain is not
        idempotent against already-applied DDL (e.g. migration 002's
        unguarded ``ADD COLUMN``), so faking "behind head" that way would fail
        for a reason unrelated to what this test checks.
        """
        from pathlib import Path

        from alembic import command
        from alembic.config import Config
        from sqlalchemy.ext.asyncio import create_async_engine

        from sova.db import session as db_session
        from sova.db.session import _run_migrations

        db_path = tmp_path / "behind-head.db"
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{db_path}",
            connect_args={"check_same_thread": False},
        )

        alembic_cfg = Config(str(Path(db_session.__file__).parent / "alembic.ini"))

        def _upgrade_partial(sync_conn):
            alembic_cfg.attributes["connection"] = sync_conn
            command.upgrade(alembic_cfg, "020")

        async with engine.begin() as conn:
            await conn.run_sync(_upgrade_partial)

        result = await _run_migrations(engine)
        assert result is True
        await engine.dispose()


class TestRunMigrationsHelpers:
    """Branches of the helpers _run_migrations() delegates to."""

    @staticmethod
    def _engine(db_path):
        from sqlalchemy.ext.asyncio import create_async_engine

        return create_async_engine(f"sqlite+aiosqlite:///{db_path}", connect_args={"check_same_thread": False})

    async def test_run_migrations_passes_project_claude_dir_to_alembic(self, tmp_path) -> None:
        """``project_dir`` reaches the Alembic config as the memories export directory."""
        from unittest.mock import patch

        from sova.db.session import _run_migrations

        seen: list[object] = []

        def _record(cfg, _rev):
            seen.append(cfg.attributes.get("memories_export_dir"))

        engine = self._engine(tmp_path / "export-dir.db")
        with patch("alembic.command.upgrade", side_effect=_record):
            assert await _run_migrations(engine, tmp_path) is True
        await engine.dispose()

        assert seen == [tmp_path / ".claude"]

    async def test_run_migrations_without_project_dir_sets_no_export_dir(self, tmp_path) -> None:
        """Without ``project_dir`` the attribute is absent, so migration 044 uses its own default."""
        from unittest.mock import patch

        from sova.db.session import _run_migrations

        seen: list[bool] = []
        engine = self._engine(tmp_path / "no-export-dir.db")
        with patch(
            "alembic.command.upgrade",
            side_effect=lambda cfg, _rev: seen.append("memories_export_dir" in cfg.attributes),
        ):
            await _run_migrations(engine)
        await engine.dispose()

        assert seen == [False]

    async def test_run_migrations_at_head_reports_schema_drift(self, tmp_path) -> None:
        """At head, a missing ORM column is logged as schema drift and no DDL runs."""
        from unittest.mock import patch

        from sova.db.session import _run_migrations

        engine = self._engine(tmp_path / "drift.db")
        await _run_migrations(engine)
        with (
            patch("sova.db.session._find_missing_columns", return_value=["task_runs.ghost"]),
            patch("sova.db.session._log") as log,
        ):
            assert await _run_migrations(engine) is False
        await engine.dispose()

        log.error.assert_called_once()
        assert log.error.call_args.args[0] == "db.schema_drift"
        assert log.error.call_args.kwargs["missing_columns"] == "task_runs.ghost"

    async def test_run_migrations_at_head_schema_verify_failure_fails_open(self, tmp_path) -> None:
        """A drift check that cannot inspect the DB warns and still takes the fast path."""
        from unittest.mock import patch

        from sova.db.session import _run_migrations

        engine = self._engine(tmp_path / "verify-fail.db")
        await _run_migrations(engine)
        with (
            patch("sova.db.session._find_missing_columns", side_effect=RuntimeError("inspect boom")),
            patch("sova.db.session._log") as log,
        ):
            assert await _run_migrations(engine) is False
        await engine.dispose()

        log.warning.assert_called_once()
        assert log.warning.call_args.args[0] == "db.schema_verify_failed"
        log.error.assert_not_called()

    async def test_run_migrations_fresh_db_stamp_failure_is_advisory(self, tmp_path) -> None:
        """On an empty DB, a failed upgrade falls back to create_all; a failed stamp only warns."""
        from unittest.mock import patch

        from sova.db.session import _run_migrations

        engine = self._engine(tmp_path / "stamp-fail.db")
        with (
            patch("alembic.command.upgrade", side_effect=RuntimeError("upgrade boom")),
            patch("alembic.command.stamp", side_effect=RuntimeError("stamp boom")),
            patch("sova.db.session._log") as log,
        ):
            assert await _run_migrations(engine) is True

        async with engine.connect() as conn:
            tables = await conn.run_sync(lambda c: inspect(c).get_table_names())
        await engine.dispose()

        assert "task_runs" in tables
        assert "alembic_version" not in tables
        events = [call.args[0] for call in log.warning.call_args_list]
        assert events == ["db.alembic_migration_failed_fallback_create_all", "db.alembic_stamp_failed_tables_untracked"]


# ---------------------------------------------------------------------------
# _get_db_path_from_url: file vs. non-file SQLite URLs (issue #1205)
# ---------------------------------------------------------------------------


class TestGetDbPathFromUrl:
    def test_file_backed_url_returns_path(self) -> None:
        from pathlib import Path

        from sova.db.session import _get_db_path_from_url

        assert _get_db_path_from_url("sqlite+aiosqlite:////tmp/sova.db") == Path("/tmp/sova.db")

    def test_bare_in_memory_url_returns_none(self) -> None:
        from sova.db.session import _get_db_path_from_url

        assert _get_db_path_from_url("sqlite+aiosqlite://") is None

    def test_explicit_memory_url_returns_none(self) -> None:
        """``:memory:`` must be treated like the bare in-memory form.

        Disposal of an in-memory engine destroys the database, not just its
        connections, so every file-vs-memory gate in session.py (the
        migration lock, the previous-engine dispose guard, the post-migration
        dispose-and-reopen step) depends on this returning None here too.
        """
        from sova.db.session import _get_db_path_from_url

        assert _get_db_path_from_url("sqlite+aiosqlite:///:memory:") is None

    def test_shared_cache_memory_uri_returns_none(self) -> None:
        from sova.db.session import _get_db_path_from_url

        assert _get_db_path_from_url("sqlite+aiosqlite:///file:test?mode=memory&cache=shared&uri=true") is None

    def test_non_sqlite_url_returns_none(self) -> None:
        from sova.db.session import _get_db_path_from_url

        assert _get_db_path_from_url("postgresql+asyncpg://user:pass@localhost/sova") is None


# ---------------------------------------------------------------------------
# _backup_db: WAL-consistent, rotates two generations (issue #1205)
# ---------------------------------------------------------------------------


class TestBackupDb:
    async def test_backup_writes_a_restorable_copy(self, tmp_path) -> None:
        """The backup must contain the data committed before it was taken."""
        import sqlite3

        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine

        from sova.db.session import _backup_db

        db_path = tmp_path / "wal-backup.db"
        url = f"sqlite+aiosqlite:///{db_path}"
        engine = create_async_engine(url, connect_args={"check_same_thread": False})

        async with engine.begin() as conn:
            await conn.execute(text("PRAGMA journal_mode=WAL"))
            await conn.execute(text("CREATE TABLE probe (value TEXT)"))
            await conn.execute(text("INSERT INTO probe VALUES ('kept')"))

        backup_path = await _backup_db(url)
        await engine.dispose()

        assert backup_path is not None
        assert backup_path.exists()

        raw = sqlite3.connect(str(backup_path))
        try:
            rows = raw.execute("SELECT value FROM probe").fetchall()
        finally:
            raw.close()
        assert rows == [("kept",)]

    async def test_backup_rotates_to_one_prior_generation(self, tmp_path) -> None:
        """A second backup must move the first to .db.bak.1, not discard it."""
        import sqlite3

        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine

        from sova.db.session import _backup_db

        db_path = tmp_path / "rotate.db"
        url = f"sqlite+aiosqlite:///{db_path}"
        engine = create_async_engine(url, connect_args={"check_same_thread": False})

        async with engine.begin() as conn:
            await conn.execute(text("CREATE TABLE probe (value TEXT)"))
            await conn.execute(text("INSERT INTO probe VALUES ('first')"))

        first_backup = await _backup_db(url)

        async with engine.begin() as conn:
            await conn.execute(text("INSERT INTO probe VALUES ('second')"))

        second_backup = await _backup_db(url)
        await engine.dispose()

        previous = db_path.with_suffix(".db.bak.1")
        assert previous.exists()
        assert first_backup == second_backup  # same .db.bak path, rotated contents

        for path, expected in ((previous, [("first",)]), (second_backup, [("first",), ("second",)])):
            raw = sqlite3.connect(str(path))
            try:
                assert raw.execute("SELECT value FROM probe ORDER BY rowid").fetchall() == expected
            finally:
                raw.close()

    async def test_backup_returns_none_for_empty_file(self, tmp_path) -> None:
        """A 0-byte database must not produce a pointless empty backup."""
        from sova.db.session import _backup_db

        db_path = tmp_path / "empty.db"
        db_path.touch()
        url = f"sqlite+aiosqlite:///{db_path}"

        assert await _backup_db(url) is None

    async def test_run_migrations_backs_up_before_ddl(self, tmp_path) -> None:
        """A database with real prior schema must be backed up before upgrade runs."""
        from unittest.mock import AsyncMock, patch

        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine

        from sova.db.session import _run_migrations

        db_path = tmp_path / "backup-before-ddl.db"
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{db_path}",
            connect_args={"check_same_thread": False},
        )

        await _run_migrations(engine)
        async with engine.begin() as conn:
            await conn.execute(text("UPDATE alembic_version SET version_num = '001'"))

        # The real migration chain is not idempotent against a DB already fully
        # upgraded (only the version stamp was rewound), so upgrade itself is
        # stubbed out here: this test asserts backup ordering, not upgrade success.
        with (
            patch("sova.db.session._backup_db", new_callable=AsyncMock) as mock_backup,
            patch("alembic.command.upgrade"),
        ):
            result = await _run_migrations(engine)

        mock_backup.assert_awaited_once()
        assert result is True
        await engine.dispose()

    async def test_run_migrations_skips_backup_on_fast_path(self, tmp_path) -> None:
        """A DB already at head must not pay for a backup it does not need."""
        from unittest.mock import AsyncMock, patch

        from sqlalchemy.ext.asyncio import create_async_engine

        from sova.db.session import _run_migrations

        db_path = tmp_path / "fast-path-no-backup.db"
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{db_path}",
            connect_args={"check_same_thread": False},
        )

        await _run_migrations(engine)

        with patch("sova.db.session._backup_db", new_callable=AsyncMock) as mock_backup:
            result = await _run_migrations(engine)

        assert result is False
        mock_backup.assert_not_awaited()
        await engine.dispose()


# ---------------------------------------------------------------------------
# PRAGMA foreign_keys and orphan cleanup (issue #1205)
# ---------------------------------------------------------------------------


class TestSqliteForeignKeys:
    async def test_foreign_keys_pragma_is_on(self, tmp_path) -> None:
        """Every new SQLite connection must enforce foreign keys."""
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine

        from sova.db.session import _enable_sqlite_wal

        db_path = tmp_path / "fk-pragma.db"
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{db_path}",
            connect_args={"check_same_thread": False},
        )

        await _enable_sqlite_wal(engine)

        async with engine.connect() as conn:
            result = await conn.execute(text("PRAGMA foreign_keys"))
            value = result.scalar()
        await engine.dispose()

        assert value == 1

    async def test_batch_migration_succeeds_on_populated_fk_tables(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A real batch-recreate migration must not fail under foreign_keys=ON.

        Regression test for issue #1205: Alembic's SQLite batch mode (required for
        any ALTER/DROP COLUMN) does CREATE tmp / INSERT / DROP TABLE <orig> / RENAME,
        and PRAGMA foreign_keys=ON turns the DROP of a table other populated tables
        reference into an IntegrityError. Builds a DB at revision 019 (one migration
        before 020's `batch_alter_table("task_runs").drop_column(...)`) with a
        task_runs row and a child output_lines row referencing it, then drives the
        upgrade to head through the real init_db_for_project() path, the same one
        the dashboard/CLI/scheduler use.
        """
        import sqlite3
        from pathlib import Path

        from alembic import command
        from alembic.config import Config
        from sqlalchemy import text

        from sova.db.session import _engines, _get_database_url, init_db_for_project

        monkeypatch.delenv("SOVA_DATABASE_URL", raising=False)
        project_dir = tmp_path / "fk-batch-project"
        (project_dir / ".claude").mkdir(parents=True)
        url = _get_database_url(project_dir)
        db_path = project_dir / ".claude" / "sova.db"
        _engines.pop(url, None)

        alembic_cfg = Config(str(Path(__file__).parent.parent / "sova" / "db" / "alembic.ini"))
        alembic_cfg.set_main_option("sqlalchemy.url", url)
        # Alembic's own env.py calls asyncio.run() in this (no-connection-passed)
        # mode, which cannot nest inside this already-running test event loop.
        import asyncio

        await asyncio.to_thread(command.upgrade, alembic_cfg, "019")

        raw = sqlite3.connect(str(db_path))
        raw.execute("INSERT INTO task_runs (id, issue_number) VALUES (1, '1')")
        raw.execute("INSERT INTO output_lines (task_run_id, line_number, text) VALUES (1, 1, 'kept')")
        raw.commit()
        raw.close()

        try:
            await init_db_for_project(project_dir)

            engine, _ = _engines[url]
            async with engine.connect() as conn:
                fk = (await conn.execute(text("PRAGMA foreign_keys"))).scalar()
                rows = (await conn.execute(text("SELECT text FROM output_lines WHERE task_run_id = 1"))).fetchall()
        finally:
            engine_entry = _engines.pop(url, None)
            if engine_entry is not None:
                await engine_entry[0].dispose()

        assert fk == 1
        assert [r[0] for r in rows] == ["kept"]

    async def test_deleting_workflow_definition_nulls_task_run_reference(self, tmp_path) -> None:
        """With foreign_keys enabled, deleting a workflow_definitions row must not block on task_runs.

        Regression test for issue #1205: the FK had no ondelete behavior, so
        PRAGMA foreign_keys=ON turned role_service.delete_definition()'s bare
        session.delete() into an IntegrityError for any role that was ever run.
        ondelete="SET NULL" (migration 040) preserves the TaskRun audit row
        instead of blocking the delete.
        """
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine

        from sova.db.session import _enable_sqlite_wal, _run_migrations

        db_path = tmp_path / "workflow-fk.db"
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{db_path}",
            connect_args={"check_same_thread": False},
        )
        await _run_migrations(engine)
        await engine.dispose()
        await _enable_sqlite_wal(engine)

        async with engine.begin() as conn:
            await conn.execute(text("INSERT INTO workflow_definitions (id, name, graph_json) VALUES (1, 'wf', '{}')"))
            await conn.execute(text("INSERT INTO task_runs (id, workflow_definition_id) VALUES (1, 1)"))

        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM workflow_definitions WHERE id = 1"))

        async with engine.connect() as conn:
            row = (await conn.execute(text("SELECT workflow_definition_id FROM task_runs WHERE id = 1"))).fetchone()
        await engine.dispose()

        assert row[0] is None

    async def test_migration_040_handles_unnamed_fk_from_create_all(self, tmp_path) -> None:
        """Migration 040 must not assume the FK it drops carries a specific name.

        Regression test for issue #1205 review: `Base.metadata` has no
        naming_convention, so a `task_runs` table built the way the pre-#1205
        self-healing fallback built it (raw DDL, no ORM `ondelete`) has an
        *unnamed* FK on `workflow_definition_id`. The original migration 040
        assumed the name `fk_task_runs_workflow_definition` and crashed with
        "No such constraint" on exactly this shape. Builds that shape by hand
        (not via `Base.metadata.create_all`, which already carries the fixed
        model's `ondelete="SET NULL"`), stamps at 039, then upgrades to head.
        """
        import sqlite3
        from pathlib import Path

        from alembic import command
        from alembic.config import Config

        db_path = tmp_path / "unnamed-fk.db"
        url = f"sqlite+aiosqlite:///{db_path}"

        raw = sqlite3.connect(str(db_path))
        try:
            raw.execute(
                "CREATE TABLE workflow_definitions ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, name VARCHAR(100) NOT NULL UNIQUE, graph_json JSON NOT NULL)"
            )
            raw.execute(
                "CREATE TABLE task_runs ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, workflow_definition_id INTEGER, "
                "FOREIGN KEY(workflow_definition_id) REFERENCES workflow_definitions (id))"
            )
            raw.commit()
            fks_before = raw.execute("PRAGMA foreign_key_list(task_runs)").fetchall()
        finally:
            raw.close()

        assert fks_before[0][3] == "workflow_definition_id"
        assert fks_before[0][6] == "NO ACTION"  # no ondelete yet

        alembic_cfg = Config(str(Path(__file__).parent.parent / "sova" / "db" / "alembic.ini"))
        alembic_cfg.set_main_option("sqlalchemy.url", url)
        import asyncio

        await asyncio.to_thread(command.stamp, alembic_cfg, "039")
        await asyncio.to_thread(command.upgrade, alembic_cfg, "head")  # must not raise

        raw = sqlite3.connect(str(db_path))
        try:
            fks_after = raw.execute("PRAGMA foreign_key_list(task_runs)").fetchall()
        finally:
            raw.close()

        assert fks_after[0][3] == "workflow_definition_id"
        assert fks_after[0][6] == "SET NULL"

    async def test_foreign_keys_pragma_is_enabled_after_wal_setup(self, tmp_path) -> None:
        """With foreign_keys enabled, deleting a parent row cascades to its children."""
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine

        from sova.db.session import _enable_sqlite_wal, _run_migrations

        db_path = tmp_path / "cascade.db"
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{db_path}",
            connect_args={"check_same_thread": False},
        )
        await _run_migrations(engine)
        await engine.dispose()
        await _enable_sqlite_wal(engine)

        async with engine.begin() as conn:
            await conn.execute(text("CREATE TABLE parent_t (id INTEGER PRIMARY KEY)"))
            await conn.execute(
                text(
                    "CREATE TABLE child_t (id INTEGER PRIMARY KEY, "
                    "parent_id INTEGER REFERENCES parent_t(id) ON DELETE CASCADE)"
                )
            )
            await conn.execute(text("INSERT INTO parent_t (id) VALUES (1)"))
            await conn.execute(text("INSERT INTO child_t (id, parent_id) VALUES (1, 1)"))

        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM parent_t WHERE id = 1"))

        async with engine.connect() as conn:
            count = (await conn.execute(text("SELECT COUNT(*) FROM child_t"))).scalar()
        await engine.dispose()

        assert count == 0


# ---------------------------------------------------------------------------
# Cross-process migration lock (issue #1205)
# ---------------------------------------------------------------------------


class TestMigrationLock:
    async def test_lock_serializes_a_concurrent_holder(self, tmp_path) -> None:
        """A second acquirer must block until the first releases the lock."""
        import threading
        import time

        from sova.db.session import _acquire_migration_lock_sync, _release_migration_lock_sync

        lock_path = tmp_path / "test.migrate.lock"
        events: list[str] = []

        first_fd = _acquire_migration_lock_sync(lock_path)
        events.append("first-acquired")

        def second_holder() -> None:
            fd = _acquire_migration_lock_sync(lock_path)
            events.append("second-acquired")
            _release_migration_lock_sync(fd)

        thread = threading.Thread(target=second_holder)
        thread.start()
        time.sleep(0.1)
        # The second thread must still be blocked: only one event recorded so far.
        assert events == ["first-acquired"]

        _release_migration_lock_sync(first_fd)
        thread.join(timeout=5)

        assert events == ["first-acquired", "second-acquired"]

    async def test_migration_lock_is_a_noop_for_non_file_databases(self) -> None:
        """In-memory and non-SQLite URLs must not attempt any file locking."""
        from sova.db.session import _migration_lock

        async with _migration_lock("sqlite+aiosqlite://"):
            pass  # must not raise, no lock file touched

    async def test_init_db_for_project_uses_the_migration_lock(self, tmp_path) -> None:
        """init_db_for_project must take the cross-process lock around migrations."""
        from unittest.mock import AsyncMock, patch

        from sova.db.session import _engines, init_db_for_project

        test_dir = tmp_path / "lock-wired-project"
        test_url = f"sqlite+aiosqlite:///{test_dir}/.claude/sova.db"
        _engines.pop(test_url, None)

        calls: list[str] = []

        class _FakeLock:
            async def __aenter__(self):
                calls.append("enter")

            async def __aexit__(self, *exc):
                calls.append("exit")

        with (
            patch("sova.db.session._get_database_url", return_value=test_url),
            patch("sova.db.session._run_migrations", new_callable=AsyncMock, return_value=False),
            patch("sova.db.session._enable_sqlite_wal", new_callable=AsyncMock),
            patch("sova.db.session._get_db_path_from_url", return_value=None),
            patch("sova.db.session._migration_lock", return_value=_FakeLock()),
        ):
            await init_db_for_project(test_dir)

        assert calls == ["enter", "exit"]
        _engines.pop(test_url, None)


# ---------------------------------------------------------------------------
# _get_alembic_head caching
# ---------------------------------------------------------------------------


async def test_get_alembic_head_returns_current_head() -> None:
    """_get_alembic_head must return the highest-numbered migration.

    Derived from the version filenames rather than hardcoded, so adding a
    migration does not fail this test. It still catches a broken chain, where
    the resolved head is not the newest revision on disk.
    """
    import pathlib

    from alembic.config import Config

    from sova.db import session as session_mod
    from sova.db.session import _get_alembic_head

    session_mod._ALEMBIC_HEAD_CACHE = None

    migrations_dir = pathlib.Path(session_mod.__file__).parent / "migrations" / "versions"
    expected = max(path.name.split("_")[0] for path in migrations_dir.glob("[0-9]*_*.py"))

    alembic_cfg = Config(str(pathlib.Path(session_mod.__file__).parent / "alembic.ini"))
    head = _get_alembic_head(alembic_cfg)
    assert head == expected


async def test_get_alembic_head_caches_result() -> None:
    """ScriptDirectory.from_config must be called only once across repeated calls."""
    import pathlib
    from unittest.mock import patch

    from alembic.config import Config
    from alembic.script import ScriptDirectory

    from sova.db import session as session_mod
    from sova.db.session import _get_alembic_head

    session_mod._ALEMBIC_HEAD_CACHE = None

    alembic_cfg = Config(str(pathlib.Path(session_mod.__file__).parent / "alembic.ini"))
    original = ScriptDirectory.from_config
    call_count = 0

    def counting_from_config(cfg):
        nonlocal call_count
        call_count += 1
        return original(cfg)

    with patch.object(ScriptDirectory, "from_config", side_effect=counting_from_config):
        head1 = _get_alembic_head(alembic_cfg)
        head2 = _get_alembic_head(alembic_cfg)
        head3 = _get_alembic_head(alembic_cfg)

    assert call_count == 1
    assert head1 == head2 == head3


# ---------------------------------------------------------------------------
# _write_loop uses run_in_executor (MetricsSnapshotWriter)
# ---------------------------------------------------------------------------


async def test_write_loop_dispatches_disk_io_to_executor(tmp_path) -> None:
    """_write_loop must call _flush_to_disk via run_in_executor (not the full snapshot).

    Metrics collection (_collect_metrics) runs on the event loop thread because it reads
    shared mutable state (pa.agents). Only the disk I/O (_flush_to_disk) is offloaded.
    """
    import asyncio
    from unittest.mock import MagicMock, patch

    from sova.monitoring.cross_project import MetricsSnapshotWriter

    writer = MetricsSnapshotWriter(
        project_dir=tmp_path,
        project_name="test",
        dashboard_port=9999,
        metrics_dir=tmp_path / "metrics",
    )
    # Return a fake available metrics dict so _flush_to_disk is reached
    writer._collect_metrics = MagicMock(return_value={"available": True, "system": {}, "agents": [], "agent_slots": {}})
    flush_mock = MagicMock()
    writer._flush_to_disk = flush_mock

    loop = asyncio.get_event_loop()
    executor_funcs: list[object] = []
    flush_dispatched = asyncio.Event()

    async def spy_run_in_executor(executor, func, *args):
        executor_funcs.append(func)
        func(*args)
        if func is flush_mock:
            flush_dispatched.set()

    with (
        patch("sova.monitoring.cross_project._WRITE_INTERVAL", 0),
        patch.object(loop, "run_in_executor", side_effect=spy_run_in_executor),
    ):
        task = asyncio.create_task(writer._write_loop())
        try:
            # Wait for the dispatch the assertions below check for, rather than
            # assuming a fixed delay was enough for the loop to reach it (#1209).
            await asyncio.wait_for(flush_dispatched.wait(), timeout=5)
        finally:
            # In a `finally` so a timed-out wait still stops the writer loop:
            # left running, it would keep calling the patched executor after
            # the patch has been torn down.
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    # _flush_to_disk (disk I/O only) must be dispatched to the executor
    assert any(f is flush_mock for f in executor_funcs)
    # _collect_metrics must NOT be in executor calls (it runs on the event loop thread)
    assert not any(getattr(f, "__name__", "") == "_collect_metrics" for f in executor_funcs)


# ---------------------------------------------------------------------------
# _get_alembic_head exception path
# ---------------------------------------------------------------------------


async def test_get_alembic_head_returns_none_on_exception() -> None:
    """_get_alembic_head returns None when ScriptDirectory.from_config raises."""
    from unittest.mock import patch

    from alembic.config import Config
    from alembic.script import ScriptDirectory

    from sova.db import session as session_mod
    from sova.db.session import _get_alembic_head

    session_mod._ALEMBIC_HEAD_CACHE = None

    cfg = Config()
    with patch.object(ScriptDirectory, "from_config", side_effect=RuntimeError("no scripts")):
        result = _get_alembic_head(cfg)

    assert result is None
    # Cache should remain None so a later success is still attempted.
    assert session_mod._ALEMBIC_HEAD_CACHE is None


# ---------------------------------------------------------------------------
# _enable_sqlite_wal exception path
# ---------------------------------------------------------------------------


async def test_enable_sqlite_wal_swallows_exception(tmp_path) -> None:
    """_enable_sqlite_wal logs and swallows exceptions instead of propagating."""
    from unittest.mock import patch

    from sqlalchemy.ext.asyncio import create_async_engine

    from sova.db.session import _enable_sqlite_wal

    db_path = tmp_path / "wal-exc.db"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )

    with patch("sqlalchemy.ext.asyncio.AsyncConnection.execute", side_effect=Exception("pragma fail")):
        # Must not raise even when the PRAGMA fails.
        await _enable_sqlite_wal(engine)

    await engine.dispose()


# ---------------------------------------------------------------------------
# init_db with run_migrations=True for a file-based SQLite DB
# ---------------------------------------------------------------------------


async def test_init_db_enables_wal_for_file_sqlite(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """init_db calls _enable_sqlite_wal when using a file-based SQLite DB."""
    from unittest.mock import AsyncMock, patch

    # Clear the in-memory URL set by autouse fixture so project_dir is used.
    monkeypatch.delenv("SOVA_DATABASE_URL", raising=False)
    try:
        wal_mock = AsyncMock()
        with (
            patch("sova.db.session._enable_sqlite_wal", wal_mock),
            patch("sova.db.session._run_migrations", new_callable=AsyncMock, return_value=True),
            patch("sova.db.session._backup_db"),
        ):
            await init_db(project_dir=tmp_path)
            wal_mock.assert_awaited_once()
    finally:
        await close_db()


@pytest.mark.parametrize("ddl_executed", [True, False])
async def test_init_db_disposes_regardless_of_ddl(
    tmp_path, monkeypatch: pytest.MonkeyPatch, ddl_executed: bool
) -> None:
    """init_db always disposes a file-SQLite engine after migrations, DDL or not.

    Pragmas (foreign_keys in particular, see _enable_sqlite_wal's docstring) must
    not be registered until migrations finish, so any connection migrations opened
    beforehand has to be evicted from the pool before _enable_sqlite_wal() runs.
    That dispose is unconditional: it no longer depends on whether DDL actually
    executed (the "already at head" fast path still opens connections of its own
    during the version/drift checks).
    """
    from unittest.mock import AsyncMock, MagicMock, patch

    monkeypatch.delenv("SOVA_DATABASE_URL", raising=False)
    try:
        mock_engine = MagicMock()
        mock_engine.dispose = AsyncMock()

        with (
            patch("sova.db.session._enable_sqlite_wal", new_callable=AsyncMock),
            patch("sova.db.session._run_migrations", new_callable=AsyncMock, return_value=ddl_executed),
            patch("sova.db.session._backup_db"),
            patch("sova.db.session.create_async_engine", return_value=mock_engine),
            patch("sova.db.session.async_sessionmaker"),
        ):
            await init_db(project_dir=tmp_path)
            mock_engine.dispose.assert_awaited_once()
    finally:
        await close_db()


async def test_init_db_skips_dispose_when_previous_engine_still_registered(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """init_db() must not dispose a previous engine another project's url still holds.

    project-a and project-b resolve to different urls, so the first engine stays
    reachable via ``_engines[url_a]`` (and therefore via
    ``get_session(project_dir=proj-a)``/``get_session_factory()``) even after a
    second ``init_db()`` call replaces the module-level ``_engine`` pointer.
    Disposing it anyway would pull the pool out from under any other code still
    holding that factory.

    Each engine also disposes itself exactly once as part of the pragma-timing
    fix (see ``_enable_sqlite_wal``'s docstring), independent of this "previous
    engine" logic, so ``first_engine.dispose`` is awaited once after the first
    call regardless. The behavior under test is that the *second* call does not
    push that count to two.
    """
    from unittest.mock import AsyncMock, MagicMock, patch

    from sova.db.session import _engines

    monkeypatch.delenv("SOVA_DATABASE_URL", raising=False)
    try:
        first_engine = MagicMock()
        first_engine.dispose = AsyncMock()
        second_engine = MagicMock()
        second_engine.dispose = AsyncMock()

        with (
            patch("sova.db.session._enable_sqlite_wal", new_callable=AsyncMock),
            patch("sova.db.session._run_migrations", new_callable=AsyncMock, return_value=False),
            patch("sova.db.session._backup_db"),
            patch("sova.db.session.create_async_engine", side_effect=[first_engine, second_engine]),
            patch("sova.db.session.async_sessionmaker"),
        ):
            await init_db(project_dir=tmp_path / "proj-a")
            url_a = next(url for url, (engine, _) in _engines.items() if engine is first_engine)
            first_engine.dispose.assert_awaited_once()  # its own pragma-timing self-dispose

            await init_db(project_dir=tmp_path / "proj-b")
            first_engine.dispose.assert_awaited_once()  # unchanged: still reachable via _engines[url_a]
            second_engine.dispose.assert_awaited_once()  # its own pragma-timing self-dispose
            assert _engines[url_a][0] is first_engine
    finally:
        await close_db()


async def test_init_db_disposes_previous_engine_when_unreachable(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A second init_db() call for the *same* project must dispose the first engine.

    Same project_dir means the same url, so the second call overwrites
    ``_engines[url]`` in place: the first engine is no longer reachable through
    any path, and must be disposed rather than leaking its pool.

    ``first_engine.dispose`` is awaited once after the first call already (its
    own pragma-timing self-dispose, see the sibling test above), so the second
    call must push its count to two: one more for being genuinely orphaned.
    """
    from unittest.mock import AsyncMock, MagicMock, patch

    monkeypatch.delenv("SOVA_DATABASE_URL", raising=False)
    try:
        first_engine = MagicMock()
        first_engine.dispose = AsyncMock()
        second_engine = MagicMock()
        second_engine.dispose = AsyncMock()

        with (
            patch("sova.db.session._enable_sqlite_wal", new_callable=AsyncMock),
            patch("sova.db.session._run_migrations", new_callable=AsyncMock, return_value=False),
            patch("sova.db.session._backup_db"),
            patch("sova.db.session.create_async_engine", side_effect=[first_engine, second_engine]),
            patch("sova.db.session.async_sessionmaker"),
        ):
            await init_db(project_dir=tmp_path / "proj-a")
            first_engine.dispose.assert_awaited_once()

            await init_db(project_dir=tmp_path / "proj-a")
            assert first_engine.dispose.await_count == 2
            second_engine.dispose.assert_awaited_once()
    finally:
        await close_db()


async def test_init_db_skips_dispose_for_in_memory_previous_engine(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A previous in-memory engine must never be disposed: its pool *is* the database.

    Regression test for issue #1205 review: unlike a file-backed SQLite engine
    (whose pool is just a reclaimable connection cache), an in-memory SQLite
    engine's pool holds the only copy of its data (see tests/conftest.py's
    close_db() warning). The old same-url dispose check didn't distinguish the
    two, so a second init_db() call resolving to the same in-memory url (e.g.
    SOVA_DATABASE_URL="sqlite+aiosqlite://", the shared worker url reset_db_rows
    pins before every test) silently destroyed data a concurrent caller might
    still be using.
    """
    from unittest.mock import AsyncMock, MagicMock, patch

    monkeypatch.setenv("SOVA_DATABASE_URL", "sqlite+aiosqlite://")
    try:
        first_engine = MagicMock()
        first_engine.dispose = AsyncMock()
        second_engine = MagicMock()
        second_engine.dispose = AsyncMock()

        with (
            patch("sova.db.session._enable_sqlite_wal", new_callable=AsyncMock),
            patch("sova.db.session._run_migrations", new_callable=AsyncMock, return_value=False),
            patch("sova.db.session._backup_db"),
            patch("sova.db.session.create_async_engine", side_effect=[first_engine, second_engine]),
            patch("sova.db.session.async_sessionmaker"),
        ):
            await init_db()
            first_engine.dispose.assert_not_awaited()

            await init_db()
            first_engine.dispose.assert_not_awaited()
            second_engine.dispose.assert_not_awaited()
    finally:
        await close_db()


async def test_init_db_serializes_concurrent_calls_for_the_same_url(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two concurrent init_db() calls for the same url must not interleave.

    Regression test for issue #1205 review: unlike init_db_for_project(),
    init_db() took no per-url asyncio lock, so two concurrent callers (e.g.
    two requests both hitting get_session() right after startup, both finding
    _session_factory still None) could each create an engine, each overwrite
    _engines[url], and the second one's reachability check would then find
    the first one's engine orphaned and dispose it while the first coroutine
    was still inside _run_migrations() on it. Wrapping the body in
    _get_init_lock(url), the same per-url guard init_db_for_project() already
    uses, forces the second call to wait for the first to finish entirely
    (migrations included) before it starts its own prologue: a fake
    _run_migrations that sleeps makes any interleaving observable as
    "enter, enter, exit, exit" instead of "enter, exit, enter, exit".
    """
    import asyncio
    from unittest.mock import AsyncMock, MagicMock, patch

    monkeypatch.delenv("SOVA_DATABASE_URL", raising=False)
    events: list[str] = []

    async def _fake_run_migrations(engine, project_dir):
        events.append("enter")
        await asyncio.sleep(0.05)
        events.append("exit")
        return False

    try:
        engine_a = MagicMock()
        engine_a.dispose = AsyncMock()
        engine_b = MagicMock()
        engine_b.dispose = AsyncMock()

        with (
            patch("sova.db.session._enable_sqlite_wal", new_callable=AsyncMock),
            patch("sova.db.session._run_migrations", side_effect=_fake_run_migrations),
            patch("sova.db.session._backup_db"),
            patch("sova.db.session.create_async_engine", side_effect=[engine_a, engine_b]),
            patch("sova.db.session.async_sessionmaker"),
        ):
            await asyncio.gather(
                init_db(project_dir=tmp_path / "proj-race"),
                init_db(project_dir=tmp_path / "proj-race"),
            )

        assert events == ["enter", "exit", "enter", "exit"]
    finally:
        await close_db()


# ---------------------------------------------------------------------------
# DB file mode: secrets at rest (#1204)
# ---------------------------------------------------------------------------


async def test_init_db_sets_restrictive_file_mode(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A freshly created SQLite file is chmod 0600: it holds secrets
    (provider API keys, Jira/webhook tokens) in project_settings, so
    group/world access must be denied regardless of the process umask."""
    import stat
    from unittest.mock import AsyncMock, patch

    monkeypatch.delenv("SOVA_DATABASE_URL", raising=False)
    try:
        with patch("sova.db.session._run_migrations", new_callable=AsyncMock, return_value=False):
            await init_db(project_dir=tmp_path)

        db_path = tmp_path / ".claude" / "sova.db"
        assert db_path.exists()
        assert stat.S_IMODE(db_path.stat().st_mode) == 0o600
    finally:
        await close_db()


def test_ensure_db_file_secured_before_open_creates_missing_file_restrictive(tmp_path) -> None:
    """A missing SQLite file is pre-created at 0600 before any engine opens
    it, so SQLite's own file creation (which honors the process umask) never
    gets a chance to leave it briefly permissive."""
    import stat

    from sova.db.session import _ensure_db_file_secured_before_open

    db_path = tmp_path / "nested" / "sova.db"

    _ensure_db_file_secured_before_open(db_path)

    assert db_path.exists()
    assert stat.S_IMODE(db_path.stat().st_mode) == 0o600


def test_ensure_db_file_secured_before_open_secures_existing_file(tmp_path) -> None:
    """An existing, permissive file is chmod'd immediately rather than left
    exposed until the post-migration _secure_db_file() call."""
    import os
    import stat

    from sova.db.session import _ensure_db_file_secured_before_open

    db_path = tmp_path / "sova.db"
    db_path.write_bytes(b"data")
    os.chmod(db_path, 0o644)

    _ensure_db_file_secured_before_open(db_path)

    assert stat.S_IMODE(db_path.stat().st_mode) == 0o600


async def test_init_db_secures_new_file_before_engine_opens(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The database file must already be restrictive by the time the engine
    connects, not only after migrations finish: the earlier ordering left a
    new file at the umask-derived mode for the full migration window."""
    import stat
    from unittest.mock import AsyncMock, patch

    monkeypatch.delenv("SOVA_DATABASE_URL", raising=False)
    observed_mode = {}

    def _spy_create_engine(url, **kwargs):
        from sqlalchemy.ext.asyncio import create_async_engine as real_create_async_engine

        db_path = tmp_path / ".claude" / "sova.db"
        if db_path.exists():
            observed_mode["mode"] = stat.S_IMODE(db_path.stat().st_mode)
        return real_create_async_engine(url, **kwargs)

    try:
        with (
            patch("sova.db.session._run_migrations", new_callable=AsyncMock, return_value=False),
            patch("sova.db.session.create_async_engine", side_effect=_spy_create_engine),
        ):
            await init_db(project_dir=tmp_path)

        assert observed_mode.get("mode") == 0o600
    finally:
        await close_db()


def _write_probe_db(db_path) -> None:
    """Create a minimal, valid SQLite database at db_path (SQLite's backup API
    requires a real database, not arbitrary bytes)."""
    import sqlite3

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("CREATE TABLE probe (value TEXT)")
        conn.execute("INSERT INTO probe VALUES ('kept')")
        conn.commit()
    finally:
        conn.close()


async def test_backup_db_sets_restrictive_file_mode(tmp_path) -> None:
    """_backup_db's copy gets 0600 mode: a backup of a secrets-bearing file is
    itself secrets-bearing."""
    import os
    import stat

    from sova.db.session import _backup_db

    db_path = tmp_path / "sova.db"
    _write_probe_db(db_path)
    os.chmod(db_path, 0o644)

    backup = await _backup_db(f"sqlite+aiosqlite:///{db_path}")

    assert backup is not None
    assert backup.exists()
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600


async def test_backup_db_destination_restrictive_before_copy_runs(tmp_path) -> None:
    """The backup destination is created at 0600 before any content is written
    into it, not chmod'd only after: writing under a permissive, umask-derived
    mode would otherwise expose the secrets being written for the duration of
    the backup."""
    import os
    import sqlite3
    import stat
    from unittest.mock import patch

    from sova.db.session import _backup_db

    db_path = tmp_path / "sova.db"
    _write_probe_db(db_path)
    os.chmod(db_path, 0o644)

    backup_path = tmp_path / "sova.db.bak"
    observed_mode = {}
    real_connect = sqlite3.connect

    def _spy_connect(target, *args, **kwargs):
        if str(target) == str(backup_path) and backup_path.exists():
            observed_mode["mode"] = stat.S_IMODE(os.stat(backup_path).st_mode)
        return real_connect(target, *args, **kwargs)

    with patch("sova.db.session.sqlite3.connect", side_effect=_spy_connect):
        backup = await _backup_db(f"sqlite+aiosqlite:///{db_path}")

    assert backup is not None
    assert observed_mode.get("mode") == 0o600


async def test_backup_db_chmods_preexisting_permissive_backup_before_copy(tmp_path) -> None:
    """A stale, permissive backup from an older version is rotated aside and the
    new backup is created fresh at 0600, so its secrets are never written
    through the stale file's permissive mode."""
    import os
    import sqlite3
    import stat
    from unittest.mock import patch

    from sova.db.session import _backup_db

    db_path = tmp_path / "sova.db"
    _write_probe_db(db_path)
    os.chmod(db_path, 0o644)

    backup_path = tmp_path / "sova.db.bak"
    backup_path.write_bytes(b"stale")
    os.chmod(backup_path, 0o644)

    observed_mode = {}
    real_connect = sqlite3.connect

    def _spy_connect(target, *args, **kwargs):
        if str(target) == str(backup_path) and backup_path.exists():
            observed_mode["mode"] = stat.S_IMODE(os.stat(backup_path).st_mode)
        return real_connect(target, *args, **kwargs)

    with patch("sova.db.session.sqlite3.connect", side_effect=_spy_connect):
        backup = await _backup_db(f"sqlite+aiosqlite:///{db_path}")

    assert backup is not None
    assert observed_mode.get("mode") == 0o600
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600


def test_secure_db_file_secures_wal_and_shm_sidecars(tmp_path) -> None:
    """An active WAL/SHM sidecar is secured alongside the main file.

    SQLite creates the `-wal`/`-shm` sidecars with the main file's permissions
    at the time it first opened it, so a main file secured after the fact
    (e.g. an upgrade from an older, more permissive SOVA version) would
    otherwise leave a stale, world-readable sidecar holding committed rows.
    """
    import os
    import stat

    from sova.db.session import _secure_db_file

    db_path = tmp_path / "sova.db"
    wal_path = tmp_path / "sova.db-wal"
    shm_path = tmp_path / "sova.db-shm"
    for p in (db_path, wal_path, shm_path):
        p.write_bytes(b"data")
        os.chmod(p, 0o644)

    _secure_db_file(db_path)

    for p in (db_path, wal_path, shm_path):
        assert stat.S_IMODE(p.stat().st_mode) == 0o600


def test_secure_db_file_propagates_chmod_failure(tmp_path) -> None:
    """A chmod failure must abort the caller, not leave a permissive file in
    place while the caller proceeds as though it were secured."""
    from unittest.mock import patch

    from sova.db.session import _secure_db_file

    db_path = tmp_path / "sova.db"
    db_path.write_bytes(b"data")

    with patch("sova.db.session.os.chmod", side_effect=OSError("denied")), pytest.raises(OSError):
        _secure_db_file(db_path)


# ---------------------------------------------------------------------------
# _resolve_issue_worktree: empty / all-hyphens wt_id guard
# ---------------------------------------------------------------------------


async def test_resolve_issue_worktree_skips_when_wt_id_all_hyphens(tmp_path) -> None:
    """Branch names of only path separators must not trigger create_worktree."""
    from unittest.mock import AsyncMock, patch

    from sova.dashboard.services.agent_context import _resolve_issue_worktree

    with (
        patch(
            "sova.dashboard.services.agent_context.find_worktree_by_branch",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "sova.git.worktree.create_worktree",
            new_callable=AsyncMock,
        ) as mock_create,
    ):
        result = await _resolve_issue_worktree("", tmp_path, branch_name="///", pr_number=None)

    assert result == tmp_path
    mock_create.assert_not_awaited()


# ---------------------------------------------------------------------------
# Migration 035 (schema drift repair) and the ORM/migration drift guard
# ---------------------------------------------------------------------------

_MEMORY_LIFECYCLE_COLUMNS = (
    "embedding",
    "retrieval_count",
    "last_retrieved_at",
    "archived",
    "health_score",
)


async def _migrated_engine(db_path):
    """Build a file-backed SQLite DB by replaying the real migration chain.

    Not create_all: only a migration-built schema can reveal a model column that
    no migration creates, which is the drift these tests exist to catch.
    """
    from sqlalchemy.ext.asyncio import create_async_engine

    from sova.db.session import _run_migrations

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    await _run_migrations(engine)
    return engine


async def test_migration_035_revision_chain() -> None:
    """Migration 035 follows 034."""
    mod = _import_migration("035")

    assert mod.revision == "035"
    assert mod.down_revision == "034"


async def test_migration_035_upgrade_adds_memory_columns() -> None:
    """Migration 035 upgrade adds all five drifted memory columns."""
    from unittest.mock import MagicMock, patch

    mod = _import_migration("035")

    mock_inspector = MagicMock()
    mock_inspector.get_columns.return_value = [{"name": "id"}, {"name": "title"}]
    mock_inspector.get_indexes.return_value = []

    with patch.object(mod.sa, "inspect", return_value=mock_inspector), patch.object(mod, "op") as mock_op:
        mod.upgrade()

    added = [call.args[1].name for call in mock_op.add_column.call_args_list]
    assert sorted(added) == sorted(_MEMORY_LIFECYCLE_COLUMNS)


async def test_migration_035_upgrade_skip_when_exists() -> None:
    """Migration 035 upgrade is idempotent for both columns and indexes."""
    from unittest.mock import MagicMock, patch

    mod = _import_migration("035")

    mock_inspector = MagicMock()
    mock_inspector.get_columns.return_value = [{"name": c} for c in _MEMORY_LIFECYCLE_COLUMNS]
    mock_inspector.get_indexes.return_value = [{"name": name} for name, _t, _c in mod._MISSING_INDEXES]

    with patch.object(mod.sa, "inspect", return_value=mock_inspector), patch.object(mod, "op") as mock_op:
        mod.upgrade()

    mock_op.add_column.assert_not_called()
    mock_op.create_index.assert_not_called()


async def test_migration_035_does_not_duplicate_task_assessment_indexes() -> None:
    """001_initial_schema already indexes those columns under different names.

    Creating the model's names too would leave a second redundant index on each
    column, so 035 must leave task_assessments alone.
    """
    mod = _import_migration("035")

    indexed_tables = {table for _name, table, _cols in mod._MISSING_INDEXES}
    assert "task_assessments" not in indexed_tables


async def test_migration_035_adds_lifecycle_columns_keeping_rows(tmp_path) -> None:
    """A DB at 034 has no memory lifecycle columns; upgrading to 035 adds them, rows intact.

    A drifted database (columns dropped under a stamp at 034) is not simulated: head no longer has the table.
    """
    import asyncio
    import sqlite3

    from alembic import command

    db_path = tmp_path / "at-034.db"
    alembic_cfg = _alembic_config_for(db_path)

    def columns() -> set[str]:
        raw = sqlite3.connect(str(db_path))
        try:
            return {row[1] for row in raw.execute("PRAGMA table_info(memories)")}
        finally:
            raw.close()

    await asyncio.to_thread(command.upgrade, alembic_cfg, "034")
    assert not set(_MEMORY_LIFECYCLE_COLUMNS) & columns()
    raw = sqlite3.connect(str(db_path))
    try:
        raw.execute("INSERT INTO memories (category, title, content) VALUES ('learning', 'kept', 'body')")
        raw.commit()
    finally:
        raw.close()

    await asyncio.to_thread(command.upgrade, alembic_cfg, "035")

    assert set(_MEMORY_LIFECYCLE_COLUMNS) <= columns()
    raw = sqlite3.connect(str(db_path))
    try:
        assert raw.execute("SELECT title FROM memories").fetchall() == [("kept",)]
    finally:
        raw.close()


class TestSchemaDriftGuard:
    """The migration chain must produce exactly the schema the ORM declares.

    Columns added to a model without a migration are invisible to Alembic: the DB
    stays stamped at head while every query naming that column fails. That is how
    the whole memory subsystem broke. Tests build their schema with create_all, so
    only a migration-replayed database can catch it.
    """

    async def test_no_table_or_column_drift(self, tmp_path) -> None:
        """Every ORM table and column must exist in the migration-built schema.

        Reuses the same helper the server runs at startup, which is itself covered
        by TestFindMissingColumns.
        """
        from sova.db.session import _find_missing_columns

        engine = await _migrated_engine(tmp_path / "drift-guard.db")
        async with engine.connect() as conn:
            drift = await conn.run_sync(_find_missing_columns)
        await engine.dispose()

        assert not drift, (
            f"ORM declares {len(drift)} table/column(s) no migration creates: {drift}. "
            "Add a migration for them, or every existing database will fail on queries touching that table."
        )

    async def test_no_index_column_drift(self, tmp_path) -> None:
        """Every ORM index must have a physical counterpart over the same columns.

        Matched by column set rather than name: 001_initial_schema created some
        indexes under names the models later diverged from, which is harmless.
        """
        from sova.db.models import Base

        engine = await _migrated_engine(tmp_path / "index-drift.db")

        def _collect(sync_conn):
            inspector = inspect(sync_conn)
            return {
                name: {tuple(i["column_names"]) for i in inspector.get_indexes(name)}
                for name in inspector.get_table_names()
            }

        async with engine.connect() as conn:
            live_indexes = await conn.run_sync(_collect)
        await engine.dispose()

        drift: list[str] = []
        for name, table in Base.metadata.tables.items():
            covered = live_indexes.get(name, set())
            for index in table.indexes:
                columns = tuple(c.name for c in index.columns)
                if columns not in covered:
                    drift.append(f"{name}{list(columns)}")

        assert not drift, f"ORM declares {len(drift)} index(es) no migration creates: {drift}"


class TestFindMissingColumns:
    """_run_migrations must report drift it cannot repair instead of staying silent."""

    async def test_returns_empty_for_a_healthy_schema(self, tmp_path) -> None:
        from sova.db.session import _find_missing_columns

        engine = await _migrated_engine(tmp_path / "healthy.db")
        async with engine.connect() as conn:
            missing = await conn.run_sync(_find_missing_columns)
        await engine.dispose()

        assert missing == []

    async def test_reports_a_dropped_column(self, tmp_path) -> None:
        from sqlalchemy import text

        from sova.db.session import _find_missing_columns

        engine = await _migrated_engine(tmp_path / "dropped.db")
        async with engine.begin() as conn:
            await conn.execute(text("ALTER TABLE task_assessments DROP COLUMN sub_tasks"))

        async with engine.connect() as conn:
            missing = await conn.run_sync(_find_missing_columns)
        await engine.dispose()

        assert missing == ["task_assessments.sub_tasks"]

    async def test_at_head_with_drift_logs_an_error(self, tmp_path) -> None:
        """The fast path must name the missing column, not skip silently."""
        from unittest.mock import MagicMock, patch

        from sqlalchemy import text

        from sova.db.session import _run_migrations

        engine = await _migrated_engine(tmp_path / "at-head-drift.db")
        async with engine.begin() as conn:
            await conn.execute(text("ALTER TABLE task_assessments DROP COLUMN sub_tasks"))

        mock_logger = MagicMock()
        with patch("sova.db.session._log", mock_logger):
            result = await _run_migrations(engine)
        await engine.dispose()

        assert result is False  # still no DDL: Alembic cannot repair this
        mock_logger.error.assert_called_once()
        # Rendered as a structured kwarg rather than interpolated into the
        # message, so reordering the log kwargs cannot turn this into a false pass.
        assert mock_logger.error.call_args.kwargs["missing_columns"] == "task_assessments.sub_tasks"

    async def test_verification_failure_does_not_block_startup(self, tmp_path) -> None:
        """The check is diagnostic: a schema it cannot inspect must still start."""
        from unittest.mock import MagicMock, patch

        from sova.db.session import _run_migrations

        engine = await _migrated_engine(tmp_path / "uninspectable.db")

        mock_logger = MagicMock()
        with (
            patch("sova.db.session._find_missing_columns", side_effect=RuntimeError("inspection blew up")),
            patch("sova.db.session._log", mock_logger),
        ):
            result = await _run_migrations(engine)
        await engine.dispose()

        assert result is False
        mock_logger.warning.assert_called_once()
        mock_logger.error.assert_not_called()

    async def test_at_head_without_drift_logs_nothing(self, tmp_path) -> None:
        """A healthy at-head database must not log a drift error on every restart."""
        from unittest.mock import MagicMock, patch

        from sova.db.session import _run_migrations

        engine = await _migrated_engine(tmp_path / "at-head-clean.db")

        mock_logger = MagicMock()
        with patch("sova.db.session._log", mock_logger):
            result = await _run_migrations(engine)
        await engine.dispose()

        assert result is False
        mock_logger.error.assert_not_called()


class TestInspectSchema:
    """inspect_schema() is the read-only view shared by `sova db status` and `sova doctor`.

    Each test clears SOVA_DATABASE_URL (the autouse fixture pins it to an in-memory
    database) so the project directory under test decides which database is read.
    """

    @staticmethod
    async def _migrated_project(project_dir):
        """Build ``<project>/.claude/sova.db`` by replaying the real migration chain."""
        db_path = project_dir / ".claude" / "sova.db"
        db_path.parent.mkdir(parents=True)
        engine = await _migrated_engine(db_path)
        await engine.dispose()
        return db_path

    async def test_no_file_for_a_fresh_project_and_creates_nothing(self, tmp_path, monkeypatch) -> None:
        from sova.db.session import inspect_schema

        monkeypatch.delenv("SOVA_DATABASE_URL", raising=False)
        result = await inspect_schema(tmp_path)

        assert result.state == "no_file"
        assert not (tmp_path / ".claude").exists()

    async def test_no_tables_for_an_empty_database_file(self, tmp_path, monkeypatch) -> None:
        from sova.db.session import inspect_schema

        monkeypatch.delenv("SOVA_DATABASE_URL", raising=False)
        (tmp_path / ".claude").mkdir()
        (tmp_path / ".claude" / "sova.db").touch()

        result = await inspect_schema(tmp_path)

        assert result.state == "no_tables"

    async def test_ready_and_at_head_after_the_migration_chain(self, tmp_path, monkeypatch) -> None:
        from sova.db.session import inspect_schema

        monkeypatch.delenv("SOVA_DATABASE_URL", raising=False)
        await self._migrated_project(tmp_path)

        result = await inspect_schema(tmp_path)

        assert result.state == "ready"
        assert result.legacy is False
        assert result.head is not None
        assert result.current == result.head
        assert result.at_head is True
        assert result.missing == ()

    async def test_behind_head_reports_the_stamped_revision(self, tmp_path, monkeypatch) -> None:
        import sqlite3
        from contextlib import closing

        from sova.db.session import inspect_schema

        monkeypatch.delenv("SOVA_DATABASE_URL", raising=False)
        db_path = await self._migrated_project(tmp_path)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute("UPDATE alembic_version SET version_num = '0001_stale'")

        result = await inspect_schema(tmp_path)

        assert result.state == "ready"
        assert result.current == "0001_stale"
        assert result.head not in (None, "0001_stale")
        assert result.at_head is False

    async def test_legacy_database_without_alembic_version(self, tmp_path, monkeypatch) -> None:
        """Application tables with no alembic_version are unversioned, not empty."""
        from sqlalchemy.ext.asyncio import create_async_engine

        from sova.db.models import Base
        from sova.db.session import inspect_schema

        monkeypatch.delenv("SOVA_DATABASE_URL", raising=False)
        db_path = tmp_path / ".claude" / "sova.db"
        db_path.parent.mkdir()
        engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", connect_args={"check_same_thread": False})
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await engine.dispose()

        result = await inspect_schema(tmp_path)

        assert result.state == "ready"
        assert result.legacy is True
        assert result.current is None
        assert result.at_head is False

    async def test_missing_columns_are_reported_even_at_head(self, tmp_path, monkeypatch) -> None:
        from unittest.mock import patch

        from sova.db.session import inspect_schema

        monkeypatch.delenv("SOVA_DATABASE_URL", raising=False)
        await self._migrated_project(tmp_path)

        with patch("sova.db.session._find_missing_columns", return_value=["task_runs.bogus_column"]):
            result = await inspect_schema(tmp_path)

        assert result.at_head is True
        assert result.missing == ("task_runs.bogus_column",)
