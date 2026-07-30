"""Tests for AgentRunProvider: SOVA agent activity awareness provider."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite
import pytest

from sova.awareness.base import ItemCategory
from sova.config.models import AwarenessConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE task_runs (
    id INTEGER PRIMARY KEY,
    issue_number TEXT,
    role TEXT NOT NULL DEFAULT 'developer',
    status TEXT NOT NULL DEFAULT 'running',
    pr_number INTEGER,
    handoff_json TEXT,
    error_message TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT
)
"""


async def _make_db(path: Path, rows: list[dict]) -> None:
    """Create a minimal task_runs table and insert rows."""
    async with aiosqlite.connect(str(path)) as db:
        await db.execute(_SCHEMA)
        for row in rows:
            await db.execute(
                """
                INSERT INTO task_runs
                    (id, issue_number, role, status, pr_number, handoff_json, error_message, started_at, ended_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.get("id", 1),
                    row.get("issue_number"),
                    row.get("role", "developer"),
                    row.get("status", "running"),
                    row.get("pr_number"),
                    row.get("handoff_json"),
                    row.get("error_message"),
                    row.get("started_at", _ts(0)),
                    row.get("ended_at"),
                ),
            )
        await db.commit()


def _ts(hours_ago: float) -> str:
    """Return a timestamp N hours before now in SQLite storage format (space separator, no TZ)."""
    dt = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return dt.strftime("%Y-%m-%d %H:%M:%S.%f")


def _handoff(needs_human: bool = False, next_action: str = "") -> str:
    """Return a minimal serialized AgentHandoff JSON string."""
    data: dict = {"needs_human": needs_human}
    if next_action:
        data["next_action"] = next_action
    return json.dumps(data)


# ---------------------------------------------------------------------------
# is_configured
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_is_configured_false_when_no_projects(monkeypatch: pytest.MonkeyPatch) -> None:
    from sova.awareness.providers.agent_runs import AgentRunProvider

    monkeypatch.setattr("sova.awareness.providers.agent_runs.list_projects", lambda: {})
    provider = AgentRunProvider(AwarenessConfig())
    assert await provider.is_configured() is False


@pytest.mark.asyncio
async def test_is_configured_true_when_projects_registered(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from sova.awareness.providers.agent_runs import AgentRunProvider

    monkeypatch.setattr("sova.awareness.providers.agent_runs.list_projects", lambda: {"sova": str(tmp_path)})
    provider = AgentRunProvider(AwarenessConfig())
    assert await provider.is_configured() is True


# ---------------------------------------------------------------------------
# fetch_items -- empty / no projects
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_items_empty_when_no_projects(monkeypatch: pytest.MonkeyPatch) -> None:
    from sova.awareness.providers.agent_runs import AgentRunProvider

    monkeypatch.setattr("sova.awareness.providers.agent_runs.list_projects", lambda: {})
    provider = AgentRunProvider(AwarenessConfig())
    items = await provider.fetch_items()
    assert items == []


@pytest.mark.asyncio
async def test_fetch_items_skips_missing_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from sova.awareness.providers.agent_runs import AgentRunProvider

    no_db_path = tmp_path / "no-db-project"
    no_db_path.mkdir()
    monkeypatch.setattr(
        "sova.awareness.providers.agent_runs.list_projects",
        lambda: {"ghost": str(no_db_path)},
    )
    provider = AgentRunProvider(AwarenessConfig())
    items = await provider.fetch_items()
    assert items == []


@pytest.mark.asyncio
async def test_fetch_items_skips_db_without_task_runs_table(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from sova.awareness.providers.agent_runs import AgentRunProvider

    db_path = tmp_path / ".claude" / "sova.db"
    db_path.parent.mkdir(parents=True)
    async with aiosqlite.connect(str(db_path)) as db:
        await db.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)")
        await db.commit()

    monkeypatch.setattr(
        "sova.awareness.providers.agent_runs.list_projects",
        lambda: {"old": str(tmp_path)},
    )
    provider = AgentRunProvider(AwarenessConfig())
    items = await provider.fetch_items()
    assert items == []


# ---------------------------------------------------------------------------
# Classification: NEEDS_ATTENTION
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failed_run_is_needs_attention(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / ".claude" / "sova.db"
    db_path.parent.mkdir(parents=True)
    await _make_db(
        db_path,
        [{"id": 1, "issue_number": "42", "role": "developer", "status": "failed", "started_at": _ts(1)}],
    )

    from sova.awareness.providers.agent_runs import AgentRunProvider

    monkeypatch.setattr(
        "sova.awareness.providers.agent_runs.list_projects",
        lambda: {"proj": str(tmp_path)},
    )
    provider = AgentRunProvider(AwarenessConfig())
    items = await provider.fetch_items()

    assert len(items) == 1
    assert items[0].category == ItemCategory.NEEDS_ATTENTION
    assert items[0].urgency == 2


@pytest.mark.asyncio
async def test_interrupted_run_is_needs_attention(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / ".claude" / "sova.db"
    db_path.parent.mkdir(parents=True)
    await _make_db(
        db_path,
        [{"id": 1, "issue_number": "7", "role": "researcher", "status": "interrupted", "started_at": _ts(2)}],
    )

    from sova.awareness.providers.agent_runs import AgentRunProvider

    monkeypatch.setattr(
        "sova.awareness.providers.agent_runs.list_projects",
        lambda: {"proj": str(tmp_path)},
    )
    provider = AgentRunProvider(AwarenessConfig())
    items = await provider.fetch_items()

    assert len(items) == 1
    assert items[0].category == ItemCategory.NEEDS_ATTENTION
    assert items[0].urgency == 2


@pytest.mark.asyncio
async def test_done_run_with_needs_human_is_needs_attention(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / ".claude" / "sova.db"
    db_path.parent.mkdir(parents=True)
    await _make_db(
        db_path,
        [
            {
                "id": 1,
                "issue_number": "99",
                "role": "developer",
                "status": "done",
                "handoff_json": _handoff(needs_human=True, next_action="integrate_pr"),
                "started_at": _ts(3),
            }
        ],
    )

    from sova.awareness.providers.agent_runs import AgentRunProvider

    monkeypatch.setattr(
        "sova.awareness.providers.agent_runs.list_projects",
        lambda: {"proj": str(tmp_path)},
    )
    provider = AgentRunProvider(AwarenessConfig())
    items = await provider.fetch_items()

    assert len(items) == 1
    assert items[0].category == ItemCategory.NEEDS_ATTENTION
    assert items[0].urgency == 1


@pytest.mark.asyncio
async def test_since_filter_uses_sqlite_storage_format(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """since filter must work with SQLAlchemy's SQLite storage format (space separator, no TZ)."""
    db_path = tmp_path / ".claude" / "sova.db"
    db_path.parent.mkdir(parents=True)
    # Insert rows using SQLAlchemy's actual storage format (space separator, no +00:00).
    now = datetime.now(timezone.utc)
    recent = (now - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S.%f")
    old = (now - timedelta(hours=48)).strftime("%Y-%m-%d %H:%M:%S.%f")
    async with aiosqlite.connect(str(db_path)) as db:
        await db.execute(_SCHEMA)
        await db.execute(
            "INSERT INTO task_runs (id, role, status, started_at) VALUES (1, 'developer', 'done', ?)",
            (recent,),
        )
        await db.execute(
            "INSERT INTO task_runs (id, role, status, started_at) VALUES (2, 'developer', 'done', ?)",
            (old,),
        )
        await db.commit()

    from sova.awareness.providers.agent_runs import AgentRunProvider

    monkeypatch.setattr(
        "sova.awareness.providers.agent_runs.list_projects",
        lambda: {"proj": str(tmp_path)},
    )
    provider = AgentRunProvider(AwarenessConfig())
    since = now - timedelta(hours=24)
    items = await provider.fetch_items(since=since)

    run_ids = {int(i.id.split(":")[-1]) for i in items}
    assert 1 in run_ids, "recent run within window should be included"
    assert 2 not in run_ids, "old run outside window should be excluded"


@pytest.mark.asyncio
async def test_awaiting_approval_run_is_needs_attention(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / ".claude" / "sova.db"
    db_path.parent.mkdir(parents=True)
    await _make_db(
        db_path,
        [{"id": 1, "issue_number": "50", "role": "developer", "status": "awaiting_approval", "started_at": _ts(1)}],
    )

    from sova.awareness.providers.agent_runs import AgentRunProvider

    monkeypatch.setattr(
        "sova.awareness.providers.agent_runs.list_projects",
        lambda: {"proj": str(tmp_path)},
    )
    provider = AgentRunProvider(AwarenessConfig())
    items = await provider.fetch_items()

    assert len(items) == 1
    assert items[0].category == ItemCategory.NEEDS_ATTENTION
    assert items[0].urgency == 1


@pytest.mark.asyncio
async def test_paused_run_is_needs_attention(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / ".claude" / "sova.db"
    db_path.parent.mkdir(parents=True)
    await _make_db(
        db_path,
        [{"id": 1, "issue_number": "51", "role": "developer", "status": "paused", "started_at": _ts(1)}],
    )

    from sova.awareness.providers.agent_runs import AgentRunProvider

    monkeypatch.setattr(
        "sova.awareness.providers.agent_runs.list_projects",
        lambda: {"proj": str(tmp_path)},
    )
    provider = AgentRunProvider(AwarenessConfig())
    items = await provider.fetch_items()

    assert len(items) == 1
    assert items[0].category == ItemCategory.NEEDS_ATTENTION
    assert items[0].urgency == 1


@pytest.mark.asyncio
async def test_awaiting_approval_outcome_is_descriptive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / ".claude" / "sova.db"
    db_path.parent.mkdir(parents=True)
    await _make_db(
        db_path,
        [{"id": 1, "issue_number": "52", "role": "developer", "status": "awaiting_approval", "started_at": _ts(1)}],
    )

    from sova.awareness.providers.agent_runs import AgentRunProvider

    monkeypatch.setattr(
        "sova.awareness.providers.agent_runs.list_projects",
        lambda: {"proj": str(tmp_path)},
    )
    provider = AgentRunProvider(AwarenessConfig())
    items = await provider.fetch_items()

    assert len(items) == 1
    assert "Completed" not in items[0].body
    assert "awaiting" in items[0].body.lower()


# ---------------------------------------------------------------------------
# Classification: INFORMATIONAL
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_running_run_is_informational(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / ".claude" / "sova.db"
    db_path.parent.mkdir(parents=True)
    await _make_db(
        db_path,
        [{"id": 1, "issue_number": "5", "role": "developer", "status": "running", "started_at": _ts(0.5)}],
    )

    from sova.awareness.providers.agent_runs import AgentRunProvider

    monkeypatch.setattr(
        "sova.awareness.providers.agent_runs.list_projects",
        lambda: {"proj": str(tmp_path)},
    )
    provider = AgentRunProvider(AwarenessConfig())
    items = await provider.fetch_items()

    assert len(items) == 1
    assert items[0].category == ItemCategory.INFORMATIONAL
    assert items[0].urgency == 0


@pytest.mark.asyncio
async def test_done_run_without_needs_human_is_informational(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / ".claude" / "sova.db"
    db_path.parent.mkdir(parents=True)
    await _make_db(
        db_path,
        [
            {
                "id": 1,
                "issue_number": "12",
                "role": "developer",
                "status": "done",
                "handoff_json": _handoff(needs_human=False),
                "pr_number": 55,
                "started_at": _ts(4),
            }
        ],
    )

    from sova.awareness.providers.agent_runs import AgentRunProvider

    monkeypatch.setattr(
        "sova.awareness.providers.agent_runs.list_projects",
        lambda: {"proj": str(tmp_path)},
    )
    provider = AgentRunProvider(AwarenessConfig())
    items = await provider.fetch_items()

    assert len(items) == 1
    assert items[0].category == ItemCategory.INFORMATIONAL


# ---------------------------------------------------------------------------
# Outcome summary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_outcome_pr_number_priority(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / ".claude" / "sova.db"
    db_path.parent.mkdir(parents=True)
    await _make_db(
        db_path,
        [
            {
                "id": 1,
                "issue_number": "10",
                "role": "developer",
                "status": "done",
                "pr_number": 77,
                "handoff_json": _handoff(next_action="integrate_pr"),
                "started_at": _ts(1),
            }
        ],
    )

    from sova.awareness.providers.agent_runs import AgentRunProvider

    monkeypatch.setattr(
        "sova.awareness.providers.agent_runs.list_projects",
        lambda: {"proj": str(tmp_path)},
    )
    provider = AgentRunProvider(AwarenessConfig())
    items = await provider.fetch_items()

    assert "PR #77" in items[0].body


@pytest.mark.asyncio
async def test_outcome_next_action_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / ".claude" / "sova.db"
    db_path.parent.mkdir(parents=True)
    await _make_db(
        db_path,
        [
            {
                "id": 1,
                "issue_number": "20",
                "role": "reviewer",
                "status": "done",
                "handoff_json": _handoff(next_action="address_review"),
                "started_at": _ts(1),
            }
        ],
    )

    from sova.awareness.providers.agent_runs import AgentRunProvider

    monkeypatch.setattr(
        "sova.awareness.providers.agent_runs.list_projects",
        lambda: {"proj": str(tmp_path)},
    )
    provider = AgentRunProvider(AwarenessConfig())
    items = await provider.fetch_items()

    assert "address_review" in items[0].body


@pytest.mark.asyncio
async def test_running_run_outcome_is_in_progress(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / ".claude" / "sova.db"
    db_path.parent.mkdir(parents=True)
    await _make_db(
        db_path,
        [{"id": 1, "issue_number": "5", "role": "developer", "status": "running", "started_at": _ts(0.5)}],
    )

    from sova.awareness.providers.agent_runs import AgentRunProvider

    monkeypatch.setattr(
        "sova.awareness.providers.agent_runs.list_projects",
        lambda: {"proj": str(tmp_path)},
    )
    provider = AgentRunProvider(AwarenessConfig())
    items = await provider.fetch_items()

    assert "In progress" in items[0].body


@pytest.mark.asyncio
async def test_outcome_generic_completed_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / ".claude" / "sova.db"
    db_path.parent.mkdir(parents=True)
    await _make_db(
        db_path,
        [{"id": 1, "issue_number": "30", "role": "researcher", "status": "done", "started_at": _ts(1)}],
    )

    from sova.awareness.providers.agent_runs import AgentRunProvider

    monkeypatch.setattr(
        "sova.awareness.providers.agent_runs.list_projects",
        lambda: {"proj": str(tmp_path)},
    )
    provider = AgentRunProvider(AwarenessConfig())
    items = await provider.fetch_items()

    assert "Completed" in items[0].body


@pytest.mark.asyncio
async def test_failed_run_includes_error_message(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / ".claude" / "sova.db"
    db_path.parent.mkdir(parents=True)
    await _make_db(
        db_path,
        [
            {
                "id": 1,
                "issue_number": "8",
                "role": "developer",
                "status": "failed",
                "error_message": "Budget exceeded after 3 attempts",
                "started_at": _ts(2),
            }
        ],
    )

    from sova.awareness.providers.agent_runs import AgentRunProvider

    monkeypatch.setattr(
        "sova.awareness.providers.agent_runs.list_projects",
        lambda: {"proj": str(tmp_path)},
    )
    provider = AgentRunProvider(AwarenessConfig())
    items = await provider.fetch_items()

    assert "Budget exceeded" in items[0].body


# ---------------------------------------------------------------------------
# Malformed / null handoff_json
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_null_handoff_json_no_crash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / ".claude" / "sova.db"
    db_path.parent.mkdir(parents=True)
    await _make_db(
        db_path,
        [{"id": 1, "issue_number": "5", "role": "developer", "status": "done", "started_at": _ts(1)}],
    )

    from sova.awareness.providers.agent_runs import AgentRunProvider

    monkeypatch.setattr(
        "sova.awareness.providers.agent_runs.list_projects",
        lambda: {"proj": str(tmp_path)},
    )
    provider = AgentRunProvider(AwarenessConfig())
    items = await provider.fetch_items()

    assert len(items) == 1
    assert items[0].category == ItemCategory.INFORMATIONAL


@pytest.mark.asyncio
async def test_malformed_handoff_json_no_crash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / ".claude" / "sova.db"
    db_path.parent.mkdir(parents=True)
    await _make_db(
        db_path,
        [
            {
                "id": 1,
                "issue_number": "6",
                "role": "developer",
                "status": "done",
                "handoff_json": "NOT_VALID_JSON{{{",
                "started_at": _ts(1),
            }
        ],
    )

    from sova.awareness.providers.agent_runs import AgentRunProvider

    monkeypatch.setattr(
        "sova.awareness.providers.agent_runs.list_projects",
        lambda: {"proj": str(tmp_path)},
    )
    provider = AgentRunProvider(AwarenessConfig())
    items = await provider.fetch_items()

    assert len(items) == 1


# ---------------------------------------------------------------------------
# since filter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_since_filter_excludes_old_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / ".claude" / "sova.db"
    db_path.parent.mkdir(parents=True)
    await _make_db(
        db_path,
        [
            {"id": 1, "issue_number": "1", "role": "developer", "status": "done", "started_at": _ts(48)},
            {"id": 2, "issue_number": "2", "role": "developer", "status": "done", "started_at": _ts(1)},
        ],
    )

    from sova.awareness.providers.agent_runs import AgentRunProvider

    monkeypatch.setattr(
        "sova.awareness.providers.agent_runs.list_projects",
        lambda: {"proj": str(tmp_path)},
    )
    provider = AgentRunProvider(AwarenessConfig())
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    items = await provider.fetch_items(since=since)

    run_ids = {int(i.id.split(":")[-1]) for i in items}
    assert 2 in run_ids
    assert 1 not in run_ids


# ---------------------------------------------------------------------------
# Cross-project
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_items_aggregated_across_projects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    proj_a = tmp_path / "proj-a"
    proj_b = tmp_path / "proj-b"
    for p in (proj_a, proj_b):
        db_dir = p / ".claude"
        db_dir.mkdir(parents=True)

    await _make_db(
        proj_a / ".claude" / "sova.db",
        [{"id": 1, "issue_number": "10", "role": "developer", "status": "done", "started_at": _ts(1)}],
    )
    await _make_db(
        proj_b / ".claude" / "sova.db",
        [{"id": 2, "issue_number": "20", "role": "researcher", "status": "failed", "started_at": _ts(2)}],
    )

    from sova.awareness.providers.agent_runs import AgentRunProvider

    monkeypatch.setattr(
        "sova.awareness.providers.agent_runs.list_projects",
        lambda: {"alpha": str(proj_a), "beta": str(proj_b)},
    )
    provider = AgentRunProvider(AwarenessConfig())
    items = await provider.fetch_items()

    assert len(items) == 2
    providers_set = {i.metadata.get("project") for i in items}
    assert "alpha" in providers_set
    assert "beta" in providers_set


# ---------------------------------------------------------------------------
# Item structure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_item_id_format(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / ".claude" / "sova.db"
    db_path.parent.mkdir(parents=True)
    await _make_db(
        db_path,
        [{"id": 42, "issue_number": "7", "role": "developer", "status": "running", "started_at": _ts(0.5)}],
    )

    from sova.awareness.providers.agent_runs import AgentRunProvider

    monkeypatch.setattr(
        "sova.awareness.providers.agent_runs.list_projects",
        lambda: {"myproj": str(tmp_path)},
    )
    provider = AgentRunProvider(AwarenessConfig())
    items = await provider.fetch_items()

    assert items[0].id == "agent_run:myproj:42"


@pytest.mark.asyncio
async def test_item_provider_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / ".claude" / "sova.db"
    db_path.parent.mkdir(parents=True)
    await _make_db(
        db_path,
        [{"id": 1, "issue_number": "1", "role": "developer", "status": "running", "started_at": _ts(0.5)}],
    )

    from sova.awareness.providers.agent_runs import AgentRunProvider

    monkeypatch.setattr(
        "sova.awareness.providers.agent_runs.list_projects",
        lambda: {"p": str(tmp_path)},
    )
    provider = AgentRunProvider(AwarenessConfig())
    items = await provider.fetch_items()

    assert items[0].provider == "agent_runs"


@pytest.mark.asyncio
async def test_null_issue_number_no_crash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / ".claude" / "sova.db"
    db_path.parent.mkdir(parents=True)
    await _make_db(
        db_path,
        [{"id": 1, "issue_number": None, "role": "developer", "status": "running", "started_at": _ts(0.5)}],
    )

    from sova.awareness.providers.agent_runs import AgentRunProvider

    monkeypatch.setattr(
        "sova.awareness.providers.agent_runs.list_projects",
        lambda: {"p": str(tmp_path)},
    )
    provider = AgentRunProvider(AwarenessConfig())
    items = await provider.fetch_items()

    assert len(items) == 1
    assert "(no issue)" in items[0].title


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_provider_auto_registered() -> None:
    from sova.awareness import _PROVIDER_REGISTRY

    assert "agent_runs" in _PROVIDER_REGISTRY
