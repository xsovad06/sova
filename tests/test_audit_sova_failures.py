"""Tests for scripts/audit_sova_failures.py diagnostic script."""

import json
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

import aiosqlite
import pytest

# Import the script module
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import audit_sova_failures


@pytest.fixture
async def mock_db():
    """Create an in-memory SQLite database for testing."""
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row

    # Create schema
    await db.execute("""
        CREATE TABLE task_runs (
            id INTEGER PRIMARY KEY,
            status TEXT,
            started_at TEXT,
            error_message TEXT,
            total_cost_usd REAL
        )
    """)
    await db.execute("""
        CREATE TABLE step_executions (
            id INTEGER PRIMARY KEY,
            step_name TEXT,
            status TEXT
        )
    """)
    await db.execute("""
        CREATE TABLE failure_records (
            id INTEGER PRIMARY KEY,
            message TEXT
        )
    """)
    await db.commit()

    yield db
    await db.close()


@pytest.mark.asyncio
async def test_get_sova_db_path_success():
    """Test get_sova_db_path resolves path from git worktree."""
    mock_result = MagicMock()
    mock_result.stdout = ".git\n"

    with patch("subprocess.run", return_value=mock_result) as mock_run:
        path = await audit_sova_failures.get_sova_db_path()

        mock_run.assert_called_once()
        assert path == Path.cwd() / ".claude" / "sova.db"


@pytest.mark.asyncio
async def test_get_sova_db_path_worktree():
    """Test get_sova_db_path resolves absolute git-common-dir (worktree case)."""
    mock_result = MagicMock()
    mock_result.stdout = "/abs/path/to/project/.git\n"

    with patch("subprocess.run", return_value=mock_result):
        path = await audit_sova_failures.get_sova_db_path()

        assert path == Path("/abs/path/to/project") / ".claude" / "sova.db"


@pytest.mark.asyncio
async def test_get_sova_db_path_git_failure():
    """Test get_sova_db_path falls back to cwd on git failure."""
    with patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, "git")):
        path = await audit_sova_failures.get_sova_db_path()

        assert path == Path.cwd() / ".claude" / "sova.db"


@pytest.mark.asyncio
async def test_query_overall_stats(mock_db):
    """Test query_overall_stats aggregates TaskRun statistics."""
    # Insert test data
    await mock_db.execute(
        "INSERT INTO task_runs (status, total_cost_usd) VALUES (?, ?)",
        ("done", 1.5),
    )
    await mock_db.execute(
        "INSERT INTO task_runs (status, total_cost_usd) VALUES (?, ?)",
        ("failed", 0.75),
    )
    await mock_db.execute(
        "INSERT INTO task_runs (status, total_cost_usd) VALUES (?, ?)",
        ("interrupted", 0.25),
    )
    await mock_db.execute(
        "INSERT INTO task_runs (status, total_cost_usd) VALUES (?, ?)",
        ("running", 0.0),
    )
    await mock_db.commit()

    result = await audit_sova_failures.query_overall_stats(mock_db)

    assert result["total"] == 4
    assert result["done"] == 1
    assert result["failed"] == 1
    assert result["interrupted"] == 1
    assert result["in_progress"] == 1
    assert result["failure_rate"] == 25.0
    assert result["success_rate"] == 25.0
    assert result["total_cost"] == Decimal("2.5")


@pytest.mark.asyncio
async def test_query_overall_stats_empty_db(mock_db):
    """Test query_overall_stats handles empty database."""
    result = await audit_sova_failures.query_overall_stats(mock_db)

    assert result["total"] == 0
    assert result["failure_rate"] == 0
    assert result["success_rate"] == 0
    assert result["total_cost"] == Decimal("0")


@pytest.mark.asyncio
async def test_query_failures_by_step(mock_db):
    """Test query_failures_by_step groups failures by step name."""
    # Insert test data
    await mock_db.executemany(
        "INSERT INTO step_executions (step_name, status) VALUES (?, ?)",
        [
            ("develop", "failed"),
            ("develop", "done"),
            ("test", "failed"),
            ("test", "failed"),
            ("test", "done"),
        ],
    )
    await mock_db.commit()

    result = await audit_sova_failures.query_failures_by_step(mock_db)

    assert len(result) == 2
    # Sorted by failures DESC
    assert result[0]["step"] == "test"
    assert result[0]["failures"] == 2
    assert result[0]["total"] == 3
    assert result[0]["rate"] == pytest.approx(66.7, abs=0.1)

    assert result[1]["step"] == "develop"
    assert result[1]["failures"] == 1
    assert result[1]["total"] == 2
    assert result[1]["rate"] == 50.0


@pytest.mark.asyncio
async def test_query_failures_by_step_no_failures(mock_db):
    """Test query_failures_by_step filters out steps with zero failures."""
    await mock_db.execute(
        "INSERT INTO step_executions (step_name, status) VALUES (?, ?)",
        ("develop", "done"),
    )
    await mock_db.commit()

    result = await audit_sova_failures.query_failures_by_step(mock_db)

    assert len(result) == 0


@pytest.mark.asyncio
async def test_query_error_clusters(mock_db):
    """Test query_error_clusters aggregates error messages."""
    await mock_db.executemany(
        "INSERT INTO failure_records (message) VALUES (?)",
        [
            ("Timeout exceeded",),
            ("Timeout exceeded",),
            ("Budget exceeded",),
            ("",),  # Empty message filtered
            (None,),  # NULL message filtered
        ],
    )
    await mock_db.commit()

    result = await audit_sova_failures.query_error_clusters(mock_db)

    assert len(result) == 2
    assert result[0]["message"] == "Timeout exceeded"
    assert result[0]["count"] == 2
    assert result[1]["message"] == "Budget exceeded"
    assert result[1]["count"] == 1


@pytest.mark.asyncio
async def test_query_timeout_failures(mock_db):
    """Test query_timeout_failures analyzes timeout failures and before/after rates."""
    # Before fix (< 2026-08-19)
    await mock_db.executemany(
        "INSERT INTO task_runs (status, started_at, error_message) VALUES (?, ?, ?)",
        [
            ("failed", "2026-08-18 10:00:00", "timeout exceeded"),
            ("failed", "2026-08-18 11:00:00", "some other error"),
            ("done", "2026-08-18 12:00:00", None),
        ],
    )
    # After fix (>= 2026-08-19)
    await mock_db.executemany(
        "INSERT INTO task_runs (status, started_at, error_message) VALUES (?, ?, ?)",
        [
            ("failed", "2026-08-19 10:00:00", "Timed out waiting"),
            ("done", "2026-08-19 11:00:00", None),
            ("done", "2026-08-20 10:00:00", None),
        ],
    )
    await mock_db.commit()

    result = await audit_sova_failures.query_timeout_failures(mock_db)

    assert result["timeout_failures"] == 2
    assert result["before_fix"]["total"] == 3
    assert result["before_fix"]["failed"] == 2
    assert result["before_fix"]["rate"] == pytest.approx(66.7, abs=0.1)
    assert result["after_fix"]["total"] == 3
    assert result["after_fix"]["failed"] == 1
    assert result["after_fix"]["rate"] == pytest.approx(33.3, abs=0.1)


@pytest.mark.asyncio
async def test_query_timeout_failures_no_data(mock_db):
    """Test query_timeout_failures handles empty results."""
    result = await audit_sova_failures.query_timeout_failures(mock_db)

    assert result["timeout_failures"] == 0
    assert result["before_fix"]["rate"] == 0
    assert result["after_fix"]["rate"] == 0


@pytest.mark.asyncio
async def test_query_budget_failures(mock_db):
    """Test query_budget_failures counts budget-exceeded errors."""
    await mock_db.executemany(
        "INSERT INTO task_runs (status, error_message) VALUES (?, ?)",
        [
            ("failed", "budget exceeded"),
            ("failed", "Budget limit reached"),
            ("failed", "timeout"),
        ],
    )
    await mock_db.commit()

    result = await audit_sova_failures.query_budget_failures(mock_db)

    assert result == 2


@pytest.mark.asyncio
async def test_query_gate_failures(mock_db):
    """Test query_gate_failures counts gate check errors."""
    await mock_db.executemany(
        "INSERT INTO task_runs (status, error_message) VALUES (?, ?)",
        [
            ("failed", "no code changes detected"),
            ("failed", "no substantive changes made"),
            ("failed", "gate check failed"),
            ("failed", "timeout"),
        ],
    )
    await mock_db.commit()

    result = await audit_sova_failures.query_gate_failures(mock_db)

    assert result == 3


@pytest.mark.asyncio
async def test_main_db_not_found():
    """Test main returns 1 when database file does not exist."""
    with patch("audit_sova_failures.get_sova_db_path") as mock_path:
        mock_path.return_value = Path("/nonexistent/sova.db")

        with patch("sys.argv", ["audit_sova_failures.py"]):
            exit_code = await audit_sova_failures.main()

    assert exit_code == 1


@pytest.mark.asyncio
async def test_main_json_output(tmp_path):
    """Test main produces valid JSON output in --json mode."""
    db_path = tmp_path / "sova.db"

    async with aiosqlite.connect(db_path) as file_db:
        await file_db.execute("""
            CREATE TABLE task_runs (
                id INTEGER PRIMARY KEY,
                status TEXT,
                started_at TEXT,
                error_message TEXT,
                total_cost_usd REAL
            )
        """)
        await file_db.execute("""
            CREATE TABLE step_executions (
                id INTEGER PRIMARY KEY,
                step_name TEXT,
                status TEXT
            )
        """)
        await file_db.execute("""
            CREATE TABLE failure_records (
                id INTEGER PRIMARY KEY,
                message TEXT
            )
        """)
        await file_db.execute(
            "INSERT INTO task_runs (status, total_cost_usd, started_at) VALUES (?, ?, ?)",
            ("done", 1.5, "2026-08-20 10:00:00"),
        )
        await file_db.commit()

    with patch("audit_sova_failures.get_sova_db_path", return_value=db_path):
        with patch("sys.argv", ["audit_sova_failures.py", "--json"]):
            with patch("builtins.print") as mock_print:
                exit_code = await audit_sova_failures.main()

    assert exit_code == 0
    output = mock_print.call_args[0][0]
    data = json.loads(output)
    assert "overall" in data
    assert "step_failures" in data
    assert "error_clusters" in data
    assert "timeout_analysis" in data
    assert data["overall"]["total"] == 1
    assert data["overall"]["total_cost"] == "1.5"


@pytest.mark.asyncio
async def test_main_human_readable_output(tmp_path):
    """Test main produces human-readable report by default."""
    db_path = tmp_path / "sova.db"

    async with aiosqlite.connect(db_path) as file_db:
        await file_db.execute("""
            CREATE TABLE task_runs (
                id INTEGER PRIMARY KEY,
                status TEXT,
                started_at TEXT,
                error_message TEXT,
                total_cost_usd REAL
            )
        """)
        await file_db.execute("""
            CREATE TABLE step_executions (
                id INTEGER PRIMARY KEY,
                step_name TEXT,
                status TEXT
            )
        """)
        await file_db.execute("""
            CREATE TABLE failure_records (
                id INTEGER PRIMARY KEY,
                message TEXT
            )
        """)
        await file_db.execute(
            "INSERT INTO task_runs (status, total_cost_usd, started_at) VALUES (?, ?, ?)",
            ("done", 2.0, "2026-08-20 10:00:00"),
        )
        await file_db.commit()

    with patch("audit_sova_failures.get_sova_db_path", return_value=db_path):
        with patch("sys.argv", ["audit_sova_failures.py"]):
            with patch("builtins.print") as mock_print:
                exit_code = await audit_sova_failures.main()

    assert exit_code == 0
    # Verify report header was printed
    calls = [str(call) for call in mock_print.call_args_list]
    assert any("SOVA PROJECT FAILURE AUDIT" in call for call in calls)


@pytest.mark.asyncio
async def test_main_detailed_output(tmp_path):
    """Test main shows detailed error clusters with --detailed flag."""
    db_path = tmp_path / "sova.db"

    async with aiosqlite.connect(db_path) as file_db:
        await file_db.execute("""
            CREATE TABLE task_runs (
                id INTEGER PRIMARY KEY,
                status TEXT,
                started_at TEXT,
                error_message TEXT,
                total_cost_usd REAL
            )
        """)
        await file_db.execute("""
            CREATE TABLE step_executions (
                id INTEGER PRIMARY KEY,
                step_name TEXT,
                status TEXT
            )
        """)
        await file_db.execute("""
            CREATE TABLE failure_records (
                id INTEGER PRIMARY KEY,
                message TEXT
            )
        """)
        await file_db.executemany(
            "INSERT INTO failure_records (message) VALUES (?)",
            [("Timeout exceeded",), ("Budget exceeded",)],
        )
        await file_db.execute(
            "INSERT INTO task_runs (status, total_cost_usd) VALUES (?, ?)",
            ("done", 1.0),
        )
        await file_db.commit()

    with patch("audit_sova_failures.get_sova_db_path", return_value=db_path):
        with patch("sys.argv", ["audit_sova_failures.py", "--detailed"]):
            with patch("builtins.print") as mock_print:
                exit_code = await audit_sova_failures.main()

    assert exit_code == 0
    calls = [str(call) for call in mock_print.call_args_list]
    assert any("ERROR MESSAGE CLUSTERS" in call for call in calls)


@pytest.mark.asyncio
async def test_main_exception_handling(tmp_path):
    """Test main handles exceptions and returns 1."""
    db_path = tmp_path / "sova.db"

    # Create a malformed DB (no tables)
    async with aiosqlite.connect(db_path):
        pass

    with patch("audit_sova_failures.get_sova_db_path", return_value=db_path):
        with patch("sys.argv", ["audit_sova_failures.py"]):
            with patch("sys.stderr"):
                exit_code = await audit_sova_failures.main()

    assert exit_code == 1


@pytest.mark.asyncio
async def test_main_exception_json_mode(tmp_path):
    """Test main outputs JSON error in --json mode on exception."""
    db_path = tmp_path / "sova.db"

    # Create a malformed DB
    async with aiosqlite.connect(db_path):
        pass

    with patch("audit_sova_failures.get_sova_db_path", return_value=db_path):
        with patch("sys.argv", ["audit_sova_failures.py", "--json"]):
            with patch("builtins.print") as mock_print:
                exit_code = await audit_sova_failures.main()

    assert exit_code == 1
    # Verify JSON error was printed
    output = mock_print.call_args[0][0]
    data = json.loads(output)
    assert "error" in data
