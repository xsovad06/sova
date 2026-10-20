"""Tests for batch triage via the Batch API."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sova.adapters.base import Task, TaskState
from sova.core.context import ExecutionContext
from sova.llm.models import BatchRequest, BatchResult, LLMResult
from sova.roles.base import TaskAssessment
from sova.roles.triage import TriageRole


def _make_task(issue_id: str, body: str = "Test body with acceptance criteria", title: str = "Test") -> Task:
    return Task(
        id=issue_id,
        title=title,
        body=body,
        state=TaskState.BACKLOG,
        labels=["type:feature"],
        url=f"https://github.com/test/repo/issues/{issue_id}",
    )


def _make_config() -> MagicMock:
    config = MagicMock()
    config.agent.max_budget = Decimal("10.00")
    config.roles = MagicMock()
    config.llm.batch_eligible_tasks = ["triage", "triage_enrich"]
    config.llm.batch_gcs_bucket = ""
    config.llm.batch_gcs_prefix = "sova-batch"
    config.llm.batch_poll_interval = 60
    config.llm.batch_timeout = 86400
    config.llm.routing = {}
    return config


def _make_ctx(config: MagicMock | None = None) -> ExecutionContext:
    cfg = config or _make_config()
    return ExecutionContext(project_dir=Path("/tmp/test"), config=cfg, adapter=MagicMock())


def _llm_result(text: str) -> LLMResult:
    return LLMResult(
        text=text,
        model="claude-sonnet-4-6",
        cost_usd=Decimal("0.01"),
        input_tokens=100,
        output_tokens=50,
    )


def _assessment_json(**overrides: object) -> str:
    base = {
        "suitability": "ready",
        "confidence": 0.9,
        "reasoning": "Good issue",
        "missing_context": [],
        "estimated_complexity": "moderate",
        "suggested_role": "researcher",
    }
    base.update(overrides)
    return json.dumps(base)


class TestAssessTasksBatch:
    @pytest.mark.asyncio
    async def test_batch_assessment_success(self) -> None:
        role = TriageRole()
        ctx = _make_ctx()
        tasks = [_make_task("1"), _make_task("2")]

        batch_results = [
            BatchResult(
                request=BatchRequest(custom_id="1", prompt="p1"),
                result=_llm_result(_assessment_json(suitability="ready")),
            ),
            BatchResult(
                request=BatchRequest(custom_id="2", prompt="p2"),
                result=_llm_result(_assessment_json(suitability="needs_spec")),
            ),
        ]

        with (
            patch("sova.llm.client.invoke_batch", new_callable=AsyncMock, return_value=batch_results),
            patch("sova.llm.client.resolve_model", return_value=("claude-sonnet-4-6", "role:triage")),
            patch("sova.llm.cost.record_cost", new_callable=AsyncMock),
        ):
            results = await role.assess_tasks_batch(tasks, ctx)

        assert len(results) == 2
        assert results[0][1].suitability == "ready"
        assert results[1][1].suitability == "needs_spec"

    @pytest.mark.asyncio
    async def test_batch_passes_triage_task_type_and_project_dir(self) -> None:
        """The batch call opts into task_type routing, scoped to the project's own config."""
        role = TriageRole()
        ctx = _make_ctx()
        tasks = [_make_task("1")]

        mock_invoke = AsyncMock(return_value=[])
        with (
            patch("sova.llm.client.invoke_batch", mock_invoke),
            patch("sova.llm.client.resolve_model", return_value=None),
            patch("sova.llm.cost.record_cost", new_callable=AsyncMock),
        ):
            await role.assess_tasks_batch(tasks, ctx)

        kwargs = mock_invoke.await_args.kwargs
        assert kwargs["task_type"] == "triage"
        # Routing and compression are read from this project's config, not the
        # server process CWD (which in multi-project mode is a different project).
        assert kwargs["cwd"] == ctx.project_dir

    @pytest.mark.asyncio
    async def test_heuristic_fallback_for_empty_body(self) -> None:
        role = TriageRole()
        ctx = _make_ctx()
        tasks = [_make_task("1", body=""), _make_task("2", body="Has content")]

        batch_results = [
            BatchResult(
                request=BatchRequest(custom_id="2", prompt="p2"),
                result=_llm_result(_assessment_json(suitability="ready")),
            ),
        ]

        with (
            patch("sova.llm.client.invoke_batch", new_callable=AsyncMock, return_value=batch_results),
            patch("sova.llm.client.resolve_model", return_value=None),
            patch("sova.llm.cost.record_cost", new_callable=AsyncMock),
        ):
            results = await role.assess_tasks_batch(tasks, ctx)

        assert len(results) == 2
        assert results[0][1].suitability == "needs_spec"
        assert results[1][1].suitability == "ready"

    @pytest.mark.asyncio
    async def test_partial_failure_falls_back_to_heuristic(self) -> None:
        role = TriageRole()
        ctx = _make_ctx()
        tasks = [_make_task("1"), _make_task("2"), _make_task("3")]

        batch_results = [
            BatchResult(
                request=BatchRequest(custom_id="1", prompt="p1"),
                result=_llm_result(_assessment_json(suitability="ready")),
            ),
            BatchResult(
                request=BatchRequest(custom_id="2", prompt="p2"),
                error="Batch item errored",
            ),
            BatchResult(
                request=BatchRequest(custom_id="3", prompt="p3"),
                result=_llm_result(_assessment_json(suitability="needs_research")),
            ),
        ]

        with (
            patch("sova.llm.client.invoke_batch", new_callable=AsyncMock, return_value=batch_results),
            patch("sova.llm.client.resolve_model", return_value=None),
            patch("sova.llm.cost.record_cost", new_callable=AsyncMock),
        ):
            results = await role.assess_tasks_batch(tasks, ctx)

        assert len(results) == 3
        assert results[0][1].suitability == "ready"
        assert results[1][1].confidence == 0.85
        assert results[2][1].suitability == "needs_research"

    @pytest.mark.asyncio
    async def test_batch_failure_falls_back_to_sequential(self) -> None:
        role = TriageRole()
        ctx = _make_ctx()
        tasks = [_make_task("1")]

        mock_assess = AsyncMock(
            return_value=TaskAssessment(
                suitability="ready",
                confidence=0.8,
                reasoning="Heuristic",
                estimated_complexity="moderate",
                suggested_role="researcher",
            )
        )

        with (
            patch("sova.llm.client.invoke_batch", new_callable=AsyncMock, side_effect=RuntimeError("API down")),
            patch("sova.llm.client.resolve_model", return_value=None),
            patch.object(role, "assess_task_with_llm", mock_assess),
        ):
            results = await role.assess_tasks_batch(tasks, ctx)

        assert len(results) == 1
        mock_assess.assert_called_once()

    async def test_batch_failure_preserves_short_circuited_interactive_verdict(self) -> None:
        """A Batch-API failure must not hand a sticky interactive/human_only task back to the LLM.

        Before the fix, invoke_batch() raising fell back to
        _sequential_fallback(tasks, ctx) with the FULL task list, discarding
        the heuristic short-circuit and letting the LLM re-decide (and
        potentially erase) an already-resolved agent:interactive-recommended
        verdict.
        """
        role = TriageRole()
        ctx = _make_ctx()
        normal_task = _make_task("1")
        interactive_task = Task(
            id="2",
            title="Investigate flaky suite",
            body="Needs investigation in staging.",
            state=TaskState.BACKLOG,
            labels=["agent:interactive-recommended"],
            url="https://github.com/test/repo/issues/2",
        )
        tasks = [normal_task, interactive_task]

        mock_assess = AsyncMock(
            return_value=TaskAssessment(
                suitability="ready",
                confidence=0.8,
                reasoning="LLM verdict",
                estimated_complexity="moderate",
                suggested_role="researcher",
            )
        )

        with (
            patch("sova.llm.client.invoke_batch", new_callable=AsyncMock, side_effect=RuntimeError("API down")),
            patch("sova.llm.client.resolve_model", return_value=None),
            patch.object(role, "assess_task_with_llm", mock_assess),
        ):
            results = await role.assess_tasks_batch(tasks, ctx)

        assert len(results) == 2
        result_by_id = {t.id: a for t, a in results}
        assert result_by_id["2"].suitability == "interactive"
        # Only the normal task should ever have reached the LLM fallback.
        mock_assess.assert_called_once_with(normal_task, ctx)

    @pytest.mark.asyncio
    async def test_cost_recorded_for_successful_results(self) -> None:
        role = TriageRole()
        ctx = _make_ctx()
        tasks = [_make_task("42")]

        batch_results = [
            BatchResult(
                request=BatchRequest(custom_id="42", prompt="p"),
                result=_llm_result(_assessment_json()),
            ),
        ]

        mock_record = AsyncMock()

        with (
            patch("sova.llm.client.invoke_batch", new_callable=AsyncMock, return_value=batch_results),
            patch("sova.llm.client.resolve_model", return_value=("claude-sonnet-4-6", "role:triage")),
            patch("sova.llm.cost.record_cost", mock_record),
        ):
            await role.assess_tasks_batch(tasks, ctx)

        mock_record.assert_called_once()
        call_kwargs = mock_record.call_args
        assert call_kwargs[1]["phase"] == "batch_triage"
        assert call_kwargs[1]["issue"] == "42"

    @pytest.mark.asyncio
    async def test_skip_patterns_filter_before_batch(self) -> None:
        role = TriageRole()
        ctx = _make_ctx()
        ctx.config.triage.skip_title_prefixes = ["[QE]"]

        tasks = [
            _make_task("1", title="[QE] Manual test"),
            _make_task("2", title="Normal task"),
        ]

        batch_results = [
            BatchResult(
                request=BatchRequest(custom_id="2", prompt="p"),
                result=_llm_result(_assessment_json(suitability="ready")),
            ),
        ]

        with (
            patch("sova.llm.client.invoke_batch", new_callable=AsyncMock, return_value=batch_results),
            patch("sova.llm.client.resolve_model", return_value=None),
            patch("sova.llm.cost.record_cost", new_callable=AsyncMock),
        ):
            results = await role.assess_tasks_batch(tasks, ctx)

        assert len(results) == 2
        assert results[0][1].suitability == "human_only"
        assert results[1][1].suitability == "ready"

    @pytest.mark.asyncio
    async def test_interactive_patterns_filter_before_batch(self) -> None:
        role = TriageRole()
        ctx = _make_ctx()
        ctx.config.triage.skip_title_prefixes = []
        ctx.config.triage.interactive_title_prefixes = ["[Spike]"]

        tasks = [
            _make_task("1", title="[Spike] Explore caching"),
            _make_task("2", title="Normal task"),
        ]

        batch_results = [
            BatchResult(
                request=BatchRequest(custom_id="2", prompt="p"),
                result=_llm_result(_assessment_json(suitability="ready")),
            ),
        ]

        with (
            patch("sova.llm.client.invoke_batch", new_callable=AsyncMock, return_value=batch_results) as mock_batch,
            patch("sova.llm.client.resolve_model", return_value=None),
            patch("sova.llm.cost.record_cost", new_callable=AsyncMock),
        ):
            results = await role.assess_tasks_batch(tasks, ctx)

        assert len(results) == 2
        assert results[0][1].suitability == "interactive"
        assert results[1][1].suitability == "ready"
        # The interactive ticket must not be sent to the model at all, or the
        # LLM verdict would override the operator's config pattern.
        submitted = [r.custom_id for r in mock_batch.call_args[0][0]]
        assert submitted == ["2"]

    @pytest.mark.asyncio
    async def test_missing_result_gets_heuristic_fallback(self) -> None:
        role = TriageRole()
        ctx = _make_ctx()
        tasks = [_make_task("1"), _make_task("2")]

        batch_results = [
            BatchResult(
                request=BatchRequest(custom_id="1", prompt="p"),
                result=_llm_result(_assessment_json(suitability="ready")),
            ),
        ]

        with (
            patch("sova.llm.client.invoke_batch", new_callable=AsyncMock, return_value=batch_results),
            patch("sova.llm.client.resolve_model", return_value=None),
            patch("sova.llm.cost.record_cost", new_callable=AsyncMock),
        ):
            results = await role.assess_tasks_batch(tasks, ctx)

        assert len(results) == 2
        assert results[0][1].suitability == "ready"
        assert results[1][1].confidence == 0.85


class TestSequentialFallback:
    @pytest.mark.asyncio
    async def test_sequential_calls_assess_task_with_llm(self) -> None:
        role = TriageRole()
        ctx = _make_ctx()
        tasks = [_make_task("1"), _make_task("2")]

        assessment = TaskAssessment(
            suitability="ready",
            confidence=0.8,
            reasoning="Test",
            estimated_complexity="moderate",
            suggested_role="researcher",
        )
        mock_assess = AsyncMock(return_value=assessment)

        with patch.object(role, "assess_task_with_llm", mock_assess):
            results = await role._sequential_fallback(tasks, ctx)

        assert len(results) == 2
        assert mock_assess.call_count == 2
