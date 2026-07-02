"""Tests for memory extraction infrastructure."""

from __future__ import annotations

import json
import os
from decimal import Decimal
from pathlib import Path

import pytest

from sova.db.session import close_db, init_db


@pytest.fixture(autouse=True)
async def setup_db():
    """Initialize a fresh in-memory DB for each test."""
    os.environ["SOVA_DATABASE_URL"] = "sqlite+aiosqlite://"
    await init_db(run_migrations=False)
    yield
    await close_db()
    os.environ.pop("SOVA_DATABASE_URL", None)


# ---------------------------------------------------------------------------
# _parse_extraction_response
# ---------------------------------------------------------------------------


def test_parse_valid_json() -> None:
    from sova.knowledge.extraction import _parse_extraction_response

    text = json.dumps(
        [
            {
                "category": "learning",
                "title": "Always validate inputs",
                "content": "Input validation prevents runtime errors",
                "tags": ["validation", "python"],
            }
        ]
    )
    result = _parse_extraction_response(text)
    assert len(result) == 1
    assert result[0].category == "learning"
    assert result[0].title == "Always validate inputs"
    assert result[0].tags == ["validation", "python"]


def test_parse_empty_array() -> None:
    from sova.knowledge.extraction import _parse_extraction_response

    result = _parse_extraction_response("[]")
    assert result == []


def test_parse_strips_markdown_fences() -> None:
    from sova.knowledge.extraction import _parse_extraction_response

    text = '```json\n[{"category": "learning", "title": "Test", "content": "Content", "tags": []}]\n```'
    result = _parse_extraction_response(text)
    assert len(result) == 1
    assert result[0].title == "Test"


def test_parse_invalid_json_returns_empty() -> None:
    from sova.knowledge.extraction import _parse_extraction_response

    result = _parse_extraction_response("this is not json at all")
    assert result == []


def test_parse_extracts_array_from_surrounding_text() -> None:
    from sova.knowledge.extraction import _parse_extraction_response

    text = 'Here are the learnings:\n[{"category": "learning", "title": "T", "content": "C", "tags": []}]\nDone.'
    result = _parse_extraction_response(text)
    assert len(result) == 1


def test_parse_limits_to_five() -> None:
    from sova.knowledge.extraction import _parse_extraction_response

    items = [{"category": "learning", "title": f"Item {i}", "content": f"Content {i}", "tags": []} for i in range(10)]
    result = _parse_extraction_response(json.dumps(items))
    assert len(result) == 5


def test_parse_skips_empty_title_or_content() -> None:
    from sova.knowledge.extraction import _parse_extraction_response

    text = json.dumps(
        [
            {"category": "learning", "title": "", "content": "Has content", "tags": []},
            {"category": "learning", "title": "Has title", "content": "", "tags": []},
            {"category": "learning", "title": "Valid", "content": "Valid content", "tags": []},
        ]
    )
    result = _parse_extraction_response(text)
    assert len(result) == 1
    assert result[0].title == "Valid"


def test_parse_non_array_json_returns_empty() -> None:
    from sova.knowledge.extraction import _parse_extraction_response

    text = json.dumps({"category": "learning", "title": "T", "content": "C"})
    result = _parse_extraction_response(text)
    assert result == []


def test_parse_normalizes_invalid_category() -> None:
    from sova.knowledge.extraction import _parse_extraction_response

    text = json.dumps([{"category": "invalid_cat", "title": "T", "content": "C", "tags": []}])
    result = _parse_extraction_response(text)
    assert result[0].category == "learning"


def test_parse_truncates_long_title() -> None:
    from sova.knowledge.extraction import _parse_extraction_response

    text = json.dumps([{"category": "learning", "title": "x" * 300, "content": "C", "tags": []}])
    result = _parse_extraction_response(text)
    assert len(result[0].title) == 200


# ---------------------------------------------------------------------------
# _build_extraction_prompt
# ---------------------------------------------------------------------------


def test_prompt_includes_role_and_task() -> None:
    from sova.knowledge.extraction import _build_extraction_prompt

    prompt = _build_extraction_prompt(
        role="developer",
        task_title="Add user auth",
        files_changed=["src/auth.py"],
        step_summaries=["develop: completed"],
    )
    assert "developer" in prompt
    assert "Add user auth" in prompt
    assert "src/auth.py" in prompt
    assert "develop: completed" in prompt


def test_prompt_includes_review_findings() -> None:
    from sova.knowledge.extraction import _build_extraction_prompt

    findings = [
        {"file": "api.py", "line": 42, "severity": 7, "category": "bug", "description": "Missing null check"},
    ]
    prompt = _build_extraction_prompt(
        role="reviewer",
        task_title="Fix API",
        files_changed=[],
        step_summaries=["review: 1 findings"],
        review_findings=findings,
    )
    assert "Review Findings" in prompt
    assert "Missing null check" in prompt
    assert "api.py:42" in prompt


def test_prompt_handles_empty_inputs() -> None:
    from sova.knowledge.extraction import _build_extraction_prompt

    prompt = _build_extraction_prompt(
        role="developer",
        task_title="Task",
        files_changed=[],
        step_summaries=[],
    )
    assert "(none)" in prompt


# ---------------------------------------------------------------------------
# _titles_match
# ---------------------------------------------------------------------------


def test_titles_exact_match() -> None:
    from sova.knowledge.similarity import titles_match as _titles_match

    assert _titles_match("Always validate inputs", "Always validate inputs") is True


def test_titles_case_insensitive() -> None:
    from sova.knowledge.similarity import titles_match as _titles_match

    assert _titles_match("Always Validate Inputs", "always validate inputs") is True


def test_titles_substring_match() -> None:
    from sova.knowledge.similarity import titles_match as _titles_match

    assert _titles_match("Always validate user inputs", "Always validate user inputs before processing") is True


def test_titles_no_match() -> None:
    from sova.knowledge.similarity import titles_match as _titles_match

    assert _titles_match("Validate inputs", "Handle errors") is False


def test_titles_short_substring_no_match() -> None:
    from sova.knowledge.similarity import titles_match as _titles_match

    assert _titles_match("Short", "Short in a longer title") is False


# ---------------------------------------------------------------------------
# _parse_confirmation_counter
# ---------------------------------------------------------------------------


def test_parse_counter_present() -> None:
    from sova.knowledge.similarity import parse_confirmation_counter as _parse_confirmation_counter

    assert _parse_confirmation_counter("Some content\n\n[confirmed: 2]") == 2


def test_parse_counter_absent() -> None:
    from sova.knowledge.similarity import parse_confirmation_counter as _parse_confirmation_counter

    assert _parse_confirmation_counter("No counter here") == 0


# ---------------------------------------------------------------------------
# _deduplicate_and_store
# ---------------------------------------------------------------------------


async def test_dedup_stores_new_memory() -> None:
    from sova.knowledge.extraction import ExtractedMemory, _deduplicate_and_store
    from sova.knowledge.memory import search

    mem = ExtractedMemory(category="learning", title="New pattern for testing", content="Description", tags=["test"])
    result = await _deduplicate_and_store(mem, repo="user/repo", issue_number="1")

    assert result == "stored"
    stored = await search(category="learning")
    assert len(stored) == 1
    assert "[confirmed: 0]" in stored[0].content


async def test_dedup_confirms_existing() -> None:
    from sova.knowledge.extraction import ExtractedMemory, _deduplicate_and_store
    from sova.knowledge.memory import search, store

    await store(
        category="learning",
        title="Existing pattern for validation",
        content="Original content\n\n[confirmed: 0]",
        tags=["test"],
    )

    mem = ExtractedMemory(
        category="learning", title="Existing pattern for validation", content="Updated", tags=["test"]
    )
    result = await _deduplicate_and_store(mem, repo="user/repo", issue_number="1")

    assert result == "confirmed"
    stored = await search(category="learning")
    assert len(stored) == 1
    assert "[confirmed: 1]" in stored[0].content


async def test_dedup_promotes_at_threshold() -> None:
    from sova.knowledge.extraction import ExtractedMemory, _deduplicate_and_store
    from sova.knowledge.memory import get, store

    created = await store(
        category="learning",
        title="Mature pattern ready to promote",
        content="Content\n\n[confirmed: 2]",
        tags=["test"],
    )

    mem = ExtractedMemory(category="learning", title="Mature pattern ready to promote", content="Same", tags=["test"])
    result = await _deduplicate_and_store(mem, repo="user/repo", issue_number="1")

    assert result == "confirmed"
    updated = await get(created.id)
    assert updated is not None
    assert updated.tier == "shared"
    assert "[confirmed: 3]" in updated.content


# ---------------------------------------------------------------------------
# extract_memories (full integration, LLM mocked)
# ---------------------------------------------------------------------------


async def test_extract_memories_is_noop() -> None:
    """extract_memories is a no-op that returns an empty ExtractionResult."""
    from sova.knowledge.extraction import extract_memories

    result = await extract_memories(
        role="developer",
        issue_number="42",
        repo="user/repo",
        task_title="Fix DB session leak",
        files_changed=["sova/db/session.py"],
        step_summaries=["develop: completed"],
        cwd="/tmp",
    )

    assert result.memories_stored == 0
    assert result.memories_confirmed == 0
    assert result.cost_usd == Decimal("0")
    assert result.error is None


# ---------------------------------------------------------------------------
# ExtractMemoryStep
# ---------------------------------------------------------------------------


async def test_step_execute_returns_noop() -> None:
    from sova.core.steps.extract_memory import ExtractMemoryStep

    step = ExtractMemoryStep()
    ctx = _make_ctx()
    result = await step.execute(ctx)

    assert result.success is True
    assert "No novel learnings" in result.summary


async def test_step_validate_always_passes() -> None:
    from sova.core.steps.extract_memory import ExtractMemoryStep

    step = ExtractMemoryStep()
    ctx = _make_ctx()
    gate = await step.validate_output(ctx)
    assert gate.passed is True


async def test_step_can_skip_when_completed() -> None:
    from sova.core.steps.extract_memory import ExtractMemoryStep

    step = ExtractMemoryStep()
    ctx = _make_ctx(completed_steps=frozenset({"extract_memory"}))
    assert await step.can_skip(ctx) is True


async def test_step_can_skip_when_not_completed() -> None:
    from sova.core.steps.extract_memory import ExtractMemoryStep

    step = ExtractMemoryStep()
    ctx = _make_ctx()
    assert await step.can_skip(ctx) is False


# ---------------------------------------------------------------------------
# Pipeline registration
# ---------------------------------------------------------------------------


def test_developer_pipeline_includes_extract_memory() -> None:
    from sova.core.steps import get_developer_steps

    names = [s.name for s in get_developer_steps()]
    assert "extract_memory" in names
    assert names.index("extract_memory") == names.index("handoff_to_reviewer") - 1


def test_address_review_pipeline_includes_extract_memory() -> None:
    from sova.core.steps import get_address_review_steps

    names = [s.name for s in get_address_review_steps()]
    assert "extract_memory" in names
    assert names.index("extract_memory") == names.index("handoff_to_user") - 1


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ctx(*, completed_steps: frozenset[str] | None = None) -> object:
    """Create a minimal mock ExecutionContext for step tests."""
    from unittest.mock import MagicMock

    from sova.adapters.base import Task

    ctx = MagicMock()
    ctx.role = "developer"
    ctx.issue_number = "42"
    ctx.repo = "user/repo"
    ctx.task = Task(id="42", title="Test task", body="", state="in_progress", labels=[], url="")
    ctx.files_changed = ["src/main.py"]
    ctx.working_dir = Path("/tmp")
    ctx.completed_steps = completed_steps or frozenset()
    return ctx
