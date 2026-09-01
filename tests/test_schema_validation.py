"""Tests for JSON Schema validation with retry."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest

from sova.core.schema_validation import (
    ValidationError,
    _build_retry_prompt,
    _format_validation_error,
    _try_parse_and_validate,
    validate_step_output,
)

# Sample schemas
REVIEW_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "type": "object",
    "required": ["findings", "summary"],
    "additionalProperties": False,
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["file", "severity", "category", "description"],
                "properties": {
                    "file": {"type": "string"},
                    "line": {"type": ["integer", "null"]},
                    "severity": {"type": "integer", "minimum": 1, "maximum": 10},
                    "category": {"type": "string"},
                    "description": {"type": "string"},
                    "suggestion": {"type": "string"},
                },
                "additionalProperties": False,
            },
        },
        "summary": {"type": "string"},
    },
}

TRIAGE_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "type": "object",
    "required": ["suitability", "confidence", "reasoning", "estimated_complexity", "suggested_role"],
    "additionalProperties": False,
    "properties": {
        "suitability": {"type": "string", "enum": ["ready", "needs_spec", "needs_research", "human_only"]},
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "reasoning": {"type": "string"},
        "missing_context": {"type": "array", "items": {"type": "string"}},
        "estimated_complexity": {
            "type": "string",
            "enum": ["trivial", "simple", "moderate", "complex", "epic"],
        },
        "suggested_role": {"type": "string"},
        "sub_tasks": {"type": "array", "items": {"type": "string"}},
    },
}


@pytest.mark.asyncio
async def test_valid_output_passes():
    """Valid JSON passes validation on first attempt."""
    valid_json = """
    {
        "findings": [
            {
                "file": "test.py",
                "severity": 7,
                "category": "bug",
                "description": "Null check missing",
                "suggestion": "Add if x is None"
            }
        ],
        "summary": "Found one critical bug"
    }
    """

    llm_invoke = AsyncMock()

    result, cost = await validate_step_output(
        raw_text=valid_json, schema=REVIEW_SCHEMA, llm_invoke=llm_invoke, original_prompt="Review this code"
    )

    assert result["findings"][0]["file"] == "test.py"
    assert result["summary"] == "Found one critical bug"
    assert cost == Decimal("0")
    llm_invoke.assert_not_called()


@pytest.mark.asyncio
async def test_missing_required_field_triggers_retry():
    """Missing required field triggers retry with error context."""
    invalid_json = '{"findings": []}'  # missing "summary"
    fixed_json = '{"findings": [], "summary": "No issues found"}'

    llm_invoke = AsyncMock()
    llm_invoke.return_value.text = fixed_json
    llm_invoke.return_value.cost_usd = Decimal("0.01")

    result, cost = await validate_step_output(
        raw_text=invalid_json,
        schema=REVIEW_SCHEMA,
        llm_invoke=llm_invoke,
        original_prompt="Review this code",
    )

    assert result["summary"] == "No issues found"
    assert cost == Decimal("0.01")
    llm_invoke.assert_called_once()
    call_prompt = llm_invoke.call_args[0][0]
    assert "validation error" in call_prompt.lower()
    assert "summary" in call_prompt.lower()


@pytest.mark.asyncio
async def test_type_error_triggers_retry():
    """Wrong type triggers retry."""
    invalid_json = '{"findings": [], "summary": 123}'  # summary should be string
    fixed_json = '{"findings": [], "summary": "Clean code"}'

    llm_invoke = AsyncMock()
    llm_invoke.return_value.text = fixed_json
    llm_invoke.return_value.cost_usd = Decimal("0.005")

    result, cost = await validate_step_output(
        raw_text=invalid_json,
        schema=REVIEW_SCHEMA,
        llm_invoke=llm_invoke,
        original_prompt="Review this code",
    )

    assert result["summary"] == "Clean code"
    assert cost == Decimal("0.005")


@pytest.mark.asyncio
async def test_additional_properties_rejected():
    """Extra properties are rejected when additionalProperties: false."""
    invalid_json = '{"findings": [], "summary": "Clean", "extra_field": "should fail"}'
    fixed_json = '{"findings": [], "summary": "Clean"}'

    llm_invoke = AsyncMock()
    llm_invoke.return_value.text = fixed_json
    llm_invoke.return_value.cost_usd = Decimal("0.002")

    result, cost = await validate_step_output(
        raw_text=invalid_json,
        schema=REVIEW_SCHEMA,
        llm_invoke=llm_invoke,
        original_prompt="Review this code",
    )

    assert "extra_field" not in result
    assert cost == Decimal("0.002")
    llm_invoke.assert_called_once()


@pytest.mark.asyncio
async def test_exhausted_retries_raises():
    """After max_retries attempts, raises ValidationError with retry cost."""
    invalid_json = '{"findings": []}'  # always missing "summary"

    llm_invoke = AsyncMock()
    llm_invoke.return_value.text = invalid_json
    llm_invoke.return_value.cost_usd = Decimal("0.01")

    with pytest.raises(ValidationError) as exc_info:
        await validate_step_output(
            raw_text=invalid_json,
            schema=REVIEW_SCHEMA,
            llm_invoke=llm_invoke,
            original_prompt="Review this code",
            max_retries=2,
        )

    assert "exhausted" in str(exc_info.value).lower()
    assert llm_invoke.call_count == 2
    assert exc_info.value.retry_cost == Decimal("0.02")


@pytest.mark.asyncio
async def test_no_json_found_retries():
    """When no JSON is found on first attempt, retries like any other error."""
    prose_only = "This is just plain text with no JSON."
    fixed_json = '{"findings": [], "summary": "Fixed on retry"}'

    llm_invoke = AsyncMock()
    llm_invoke.return_value.text = fixed_json
    llm_invoke.return_value.cost_usd = Decimal("0.01")

    result, cost = await validate_step_output(
        raw_text=prose_only,
        schema=REVIEW_SCHEMA,
        llm_invoke=llm_invoke,
        original_prompt="Review this code",
        max_retries=1,
    )

    assert result["summary"] == "Fixed on retry"
    assert cost == Decimal("0.01")
    llm_invoke.assert_called_once()


@pytest.mark.asyncio
async def test_no_json_found_no_retries_raises():
    """When no JSON is found and max_retries=0, raises ValidationError."""
    prose_only = "This is just plain text with no JSON."

    llm_invoke = AsyncMock()

    with pytest.raises(ValidationError) as exc_info:
        await validate_step_output(
            raw_text=prose_only,
            schema=REVIEW_SCHEMA,
            llm_invoke=llm_invoke,
            original_prompt="Review this code",
            max_retries=0,
        )

    assert "no json found" in str(exc_info.value).lower()
    llm_invoke.assert_not_called()


@pytest.mark.asyncio
async def test_cost_accumulation_across_retries():
    """Total cost accumulates across all retry attempts."""
    invalid_json = '{"findings": []}'
    still_invalid = '{"findings": [], "summary": 123}'  # wrong type
    valid_json = '{"findings": [], "summary": "Fixed"}'

    llm_invoke = AsyncMock()
    llm_invoke.side_effect = [
        AsyncMock(text=still_invalid, cost_usd=Decimal("0.01")),
        AsyncMock(text=valid_json, cost_usd=Decimal("0.015")),
    ]

    result, cost = await validate_step_output(
        raw_text=invalid_json,
        schema=REVIEW_SCHEMA,
        llm_invoke=llm_invoke,
        original_prompt="Review this code",
        max_retries=2,
    )

    assert result["summary"] == "Fixed"
    assert cost == Decimal("0.025")  # 0.01 + 0.015
    assert llm_invoke.call_count == 2


@pytest.mark.asyncio
async def test_triage_schema_validation():
    """Validate triage assessment output."""
    valid_json = """
    {
        "suitability": "ready",
        "confidence": 0.85,
        "reasoning": "Well-defined issue with clear scope",
        "estimated_complexity": "moderate",
        "suggested_role": "developer"
    }
    """

    llm_invoke = AsyncMock()

    result, cost = await validate_step_output(
        raw_text=valid_json,
        schema=TRIAGE_SCHEMA,
        llm_invoke=llm_invoke,
        original_prompt="Assess this issue",
    )

    assert result["suitability"] == "ready"
    assert result["confidence"] == 0.85
    assert cost == Decimal("0")


@pytest.mark.asyncio
async def test_invalid_enum_value_triggers_retry():
    """Invalid enum value triggers retry."""
    invalid_json = (
        '{"suitability": "invalid_value", "confidence": 0.9, "reasoning": "Test", '
        '"estimated_complexity": "moderate", "suggested_role": "developer"}'
    )
    fixed_json = (
        '{"suitability": "ready", "confidence": 0.9, "reasoning": "Test", '
        '"estimated_complexity": "moderate", "suggested_role": "developer"}'
    )

    llm_invoke = AsyncMock()
    llm_invoke.return_value.text = fixed_json
    llm_invoke.return_value.cost_usd = Decimal("0.008")

    result, cost = await validate_step_output(
        raw_text=invalid_json,
        schema=TRIAGE_SCHEMA,
        llm_invoke=llm_invoke,
        original_prompt="Assess this issue",
    )

    assert result["suitability"] == "ready"
    assert cost == Decimal("0.008")
    llm_invoke.assert_called_once()


@pytest.mark.asyncio
async def test_markdown_fence_stripped_before_validation():
    """Markdown fences are stripped before validation."""
    fenced_json = """```json
    {
        "findings": [],
        "summary": "Clean"
    }
    ```"""

    llm_invoke = AsyncMock()

    result, cost = await validate_step_output(
        raw_text=fenced_json,
        schema=REVIEW_SCHEMA,
        llm_invoke=llm_invoke,
        original_prompt="Review this code",
    )

    assert result["summary"] == "Clean"
    llm_invoke.assert_not_called()


# --- _try_parse_and_validate helper ---


SIMPLE_SCHEMA = {
    "type": "object",
    "required": ["name"],
    "properties": {"name": {"type": "string"}},
    "additionalProperties": False,
}


def test_try_parse_and_validate_no_json():
    """Returns (None, error, False) when text has no JSON."""
    data, error, had_json = _try_parse_and_validate("just plain text", SIMPLE_SCHEMA)
    assert data is None
    assert "no json" in error.lower()
    assert had_json is False


def test_try_parse_and_validate_json_decode_error():
    """Returns (None, error, True) when JSON is malformed."""
    data, error, had_json = _try_parse_and_validate('{"name": broken}', SIMPLE_SCHEMA)
    assert data is None
    assert "parse error" in error.lower()
    assert had_json is True


def test_try_parse_and_validate_schema_error():
    """Returns (None, error, True) when JSON doesn't match schema."""
    data, error, had_json = _try_parse_and_validate('{"name": 42}', SIMPLE_SCHEMA)
    assert data is None
    assert error is not None
    assert had_json is True


def test_try_parse_and_validate_success():
    """Returns (data, None, True) on valid JSON."""
    data, error, had_json = _try_parse_and_validate('{"name": "alice"}', SIMPLE_SCHEMA)
    assert data == {"name": "alice"}
    assert error is None
    assert had_json is True


# --- _format_validation_error ---


def test_format_validation_error_with_path():
    """Formats error with dotted path for nested violations."""
    import jsonschema as js

    nested_schema = {
        "type": "object",
        "properties": {"items": {"type": "array", "items": {"type": "string"}}},
    }
    try:
        js.validate(instance={"items": [1]}, schema=nested_schema)
    except js.ValidationError as e:
        msg = _format_validation_error(e)
        assert "items.0" in msg


def test_format_validation_error_at_root():
    """Formats error with 'root' for top-level violations."""
    import jsonschema as js

    try:
        js.validate(instance="not an object", schema={"type": "object"})
    except js.ValidationError as e:
        msg = _format_validation_error(e)
        assert "'root'" in msg


# --- _build_retry_prompt ---


def test_build_retry_prompt_truncates_long_output():
    """Truncates failed output longer than 500 chars."""
    long_output = "x" * 600
    prompt = _build_retry_prompt("original", "some error", long_output)
    assert "..." in prompt
    assert len(long_output) > 500


def test_build_retry_prompt_preserves_short_output():
    """Preserves full output when under 500 chars."""
    short_output = "short"
    prompt = _build_retry_prompt("original", "some error", short_output)
    assert "..." not in prompt
    assert "short" in prompt
    assert "some error" in prompt
    assert "original" in prompt


# --- validate_step_output: no-JSON on retry (line 86) ---


@pytest.mark.asyncio
async def test_no_json_on_retry_continues_to_next_attempt():
    """When retry response has no JSON, logs warning and tries again."""
    invalid_json = '{"findings": []}'  # missing "summary" (schema error on first attempt)
    no_json_response = "I apologize, let me try again."
    valid_json = '{"findings": [], "summary": "Fixed"}'

    llm_invoke = AsyncMock()
    llm_invoke.side_effect = [
        AsyncMock(text=no_json_response, cost_usd=Decimal("0.01")),
        AsyncMock(text=valid_json, cost_usd=Decimal("0.01")),
    ]

    result, cost = await validate_step_output(
        raw_text=invalid_json,
        schema=REVIEW_SCHEMA,
        llm_invoke=llm_invoke,
        original_prompt="Review this code",
        max_retries=2,
    )

    assert result["summary"] == "Fixed"
    assert cost == Decimal("0.02")
    assert llm_invoke.call_count == 2


@pytest.mark.asyncio
async def test_json_parse_error_triggers_retry():
    """Malformed JSON triggers retry via _try_parse_and_validate."""
    malformed = '{"findings": [, "summary": "bad"}'
    fixed = '{"findings": [], "summary": "Fixed"}'

    llm_invoke = AsyncMock()
    llm_invoke.return_value.text = fixed
    llm_invoke.return_value.cost_usd = Decimal("0.01")

    result, cost = await validate_step_output(
        raw_text=malformed,
        schema=REVIEW_SCHEMA,
        llm_invoke=llm_invoke,
        original_prompt="Review this code",
    )

    assert result["summary"] == "Fixed"
    assert cost == Decimal("0.01")


@pytest.mark.asyncio
async def test_retry_callback_exception_preserves_cost():
    """When the retry callback raises, ValidationError carries accumulated cost."""
    invalid_json = '{"findings": []}'  # missing "summary"

    call_count = 0

    async def failing_invoke(prompt: str):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            result = AsyncMock()
            result.text = invalid_json  # still invalid
            result.cost_usd = Decimal("0.01")
            return result
        raise RuntimeError("Budget exhausted")

    with pytest.raises(ValidationError) as exc_info:
        await validate_step_output(
            raw_text=invalid_json,
            schema=REVIEW_SCHEMA,
            llm_invoke=failing_invoke,
            original_prompt="Review this code",
            max_retries=2,
        )

    assert exc_info.value.retry_cost == Decimal("0.01")
    assert "Budget exhausted" in str(exc_info.value)


@pytest.mark.asyncio
async def test_max_retries_zero_valid_json():
    """With max_retries=0, valid JSON still succeeds."""
    valid_json = '{"findings": [], "summary": "Clean"}'
    llm_invoke = AsyncMock()

    result, cost = await validate_step_output(
        raw_text=valid_json,
        schema=REVIEW_SCHEMA,
        llm_invoke=llm_invoke,
        original_prompt="Review",
        max_retries=0,
    )

    assert result["summary"] == "Clean"
    assert cost == Decimal("0")
    llm_invoke.assert_not_called()


# --- Integration: reviewer fallback on ValidationError ---


@pytest.mark.asyncio
async def test_reviewer_validation_fallback_uses_parse_findings():
    """When validate_step_output raises, reviewer falls back to _parse_findings."""
    from sova.roles._review_comments import ReviewResult, _parse_findings
    from sova.roles.reviewer import ReviewerRole

    reviewer = ReviewerRole()

    llm_text = (
        '{"findings": [{"file": "a.py", "severity": 5, '
        '"category": "bug", "description": "test desc"}], "summary": "test summary"}'
    )

    mock_invoke_result = AsyncMock()
    mock_invoke_result.text = llm_text
    mock_invoke_result.cost_usd = Decimal("0.01")

    mock_ctx = AsyncMock()
    mock_ctx.working_dir = "/tmp"
    mock_ctx.config.agent.max_budget = Decimal("1.00")
    mock_ctx.cost_usd = Decimal("0")
    mock_ctx.add_cost = lambda x: None

    mock_task = AsyncMock()
    mock_task.id = "1"
    mock_task.title = "Test"
    mock_task.body = "body"

    costs_added = []

    def track_cost(c):
        costs_added.append(c)

    mock_ctx.add_cost = track_cost

    with (
        patch("sova.roles.reviewer.invoke", new_callable=AsyncMock, return_value=mock_invoke_result),
        patch(
            "sova.roles.reviewer.validate_step_output",
            side_effect=ValidationError("exhausted retries", retry_cost=Decimal("0.02")),
        ),
    ):
        result = await reviewer._run_single_review(
            ctx=mock_ctx,
            task=mock_task,
            diff="diff --git a.py\n+hello",
            files=["a.py"],
            spec_sections=None,
        )

    assert isinstance(result, ReviewResult)
    expected_findings, expected_summary = _parse_findings(llm_text)
    assert len(result.findings) == len(expected_findings)
    assert Decimal("0.02") in costs_added


# --- Integration: triage fallback on ValidationError ---


@pytest.mark.asyncio
async def test_triage_validation_fallback_uses_defaults():
    """When validate_step_output raises, triage uses strict fallback (rejects incomplete)."""
    from sova.roles.triage import TriageRole

    role = TriageRole()

    llm_text = '{"confidence": 0.9, "reasoning": "looks good"}'

    mock_invoke_result = AsyncMock()
    mock_invoke_result.text = llm_text
    mock_invoke_result.cost_usd = Decimal("0.01")

    mock_ctx = AsyncMock()
    mock_ctx.project_dir = "/tmp"
    mock_ctx.config.agent.max_budget = Decimal("1.00")
    mock_ctx.config.roles = {}
    mock_ctx.config.llm = AsyncMock()
    mock_ctx.cost_usd = Decimal("0")
    mock_ctx.task_run_id = None

    mock_task = AsyncMock()
    mock_task.id = "42"
    mock_task.title = "Test task"
    mock_task.body = "A real description"
    mock_task.labels = []

    costs_added = []

    def track_cost(c):
        costs_added.append(c)

    mock_ctx.add_cost = track_cost

    with (
        patch("sova.llm.client.invoke", new_callable=AsyncMock, return_value=mock_invoke_result),
        patch("sova.llm.client.resolve_model", return_value=("sonnet", "default")),
        patch("sova.llm.cost.record_cost", new_callable=AsyncMock),
        patch(
            "sova.roles.triage.validate_step_output",
            side_effect=ValidationError("exhausted retries", retry_cost=Decimal("0.03")),
        ),
    ):
        result = await role.assess_task_with_llm(mock_task, mock_ctx)

    assert result is not None
    assert Decimal("0.03") in costs_added
    # Strict fallback rejects incomplete output (missing suitability), so falls back to heuristic
    assert result.suitability in ("ready", "needs_research", "needs_spec", "human_only")
