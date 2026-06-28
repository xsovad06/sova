"""Tests for planner pipeline steps (scan, generate, validate)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sova.adapters.base import Task, TaskState
from sova.config.models import ProjectConfig
from sova.core.context import ExecutionContext
from sova.core.planning import PlannedTask, PlanResult, ProjectScanResult
from sova.core.steps.generate_tasks import GenerateTasksStep, _extract_json, _parse_tasks
from sova.core.steps.scan_project import ScanProjectStep, _scan_project_root
from sova.core.steps.validate_tasks import ValidateTasksStep, _word_overlap


def _mock_adapter() -> AsyncMock:
    adapter = AsyncMock()
    adapter.list_tasks.return_value = [
        Task(
            id="1",
            title="Fix login bug",
            labels=["type: bug", "priority: high"],
            state=TaskState.BACKLOG,
        ),
        Task(
            id="2",
            title="Add dashboard metrics",
            labels=["type: feature"],
            state=TaskState.TRIAGED,
            milestone="v1.0",
        ),
        Task(
            id="3",
            title="Refactor CLI parser",
            labels=["type: task", "area: cli"],
            state=TaskState.BACKLOG,
        ),
    ]
    return adapter


def _make_ctx(**kwargs) -> ExecutionContext:
    defaults = {
        "project_dir": Path("/tmp/test-project"),
        "config": ProjectConfig(),
        "adapter": _mock_adapter(),
        "issue_number": "",
        "role": "planner",
    }
    defaults.update(kwargs)
    return ExecutionContext(**defaults)


def _make_valid_task(title: str = "feat(core): add validation layer", **kwargs) -> PlannedTask:
    defaults = {
        "title": title,
        "body": (
            "Implement a validation layer for incoming data.\n\n"
            "## Acceptance Criteria\n\n- [ ] Validate all inputs\n- [ ] Return proper errors"
        ),
        "labels": ["type: feature", "priority: medium"],
        "priority": "medium",
        "complexity": "medium",
        "rationale": "Prevents invalid data from reaching core logic.",
    }
    defaults.update(kwargs)
    return PlannedTask(**defaults)


# ---------------------------------------------------------------------------
# ScanProjectStep
# ---------------------------------------------------------------------------


class TestScanProjectStep:
    @pytest.mark.asyncio
    async def test_scan_populates_context(self, tmp_path: Path) -> None:
        """ScanProjectStep gathers issues, commits, structure, and tech stack."""
        # Create marker files
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'test'\n")
        (tmp_path / "src").mkdir()
        (tmp_path / "README.md").write_text("# Test")

        ctx = _make_ctx(project_dir=tmp_path)
        step = ScanProjectStep()

        with patch("sova.core.steps.scan_project.run") as mock_run:
            mock_run.return_value = MagicMock(success=True, stdout="abc1234 feat: first\ndef5678 fix: second\n")
            result = await step.execute(ctx)

        assert result.success
        assert ctx.plan_result is not None
        assert ctx.plan_result.scan is not None
        assert len(ctx.plan_result.scan.open_issues) == 3
        assert len(ctx.plan_result.scan.recent_commits) == 2
        assert "python" in ctx.plan_result.scan.tech_stack
        assert "pyproject.toml" in ctx.plan_result.scan.project_structure
        assert ctx.plan_result.scan.label_summary["type: bug"] == 1
        assert "v1.0" in ctx.plan_result.scan.milestone_summary

    @pytest.mark.asyncio
    async def test_scan_handles_adapter_failure(self, tmp_path: Path) -> None:
        """ScanProjectStep succeeds even when adapter.list_tasks() raises."""
        adapter = AsyncMock()
        adapter.list_tasks.side_effect = RuntimeError("API down")
        ctx = _make_ctx(project_dir=tmp_path, adapter=adapter)
        step = ScanProjectStep()

        with patch("sova.core.steps.scan_project.run") as mock_run:
            mock_run.return_value = MagicMock(success=True, stdout="")
            result = await step.execute(ctx)

        assert result.success
        assert ctx.plan_result.scan.open_issues == []

    @pytest.mark.asyncio
    async def test_scan_gate_check(self, tmp_path: Path) -> None:
        """validate_output passes when scan is populated, fails otherwise."""
        ctx = _make_ctx(project_dir=tmp_path)
        step = ScanProjectStep()

        # Before execution -- no plan_result
        gate = await step.validate_output(ctx)
        assert not gate.passed

        # After execution
        with patch("sova.core.steps.scan_project.run") as mock_run:
            mock_run.return_value = MagicMock(success=True, stdout="")
            await step.execute(ctx)

        gate = await step.validate_output(ctx)
        assert gate.passed

    @pytest.mark.asyncio
    async def test_scan_raw_summary_contains_issues(self, tmp_path: Path) -> None:
        """raw_summary should mention open issues."""
        ctx = _make_ctx(project_dir=tmp_path)
        step = ScanProjectStep()

        with patch("sova.core.steps.scan_project.run") as mock_run:
            mock_run.return_value = MagicMock(success=True, stdout="")
            await step.execute(ctx)

        assert "Fix login bug" in ctx.plan_result.scan.raw_summary
        assert "## Open Issues" in ctx.plan_result.scan.raw_summary


class TestScanProjectRoot:
    def test_python_detected(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text("")
        tech_stack, _structure = _scan_project_root(tmp_path)
        assert "python" in tech_stack

    def test_node_detected(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text("{}")
        tech_stack, _structure = _scan_project_root(tmp_path)
        assert "node" in tech_stack

    def test_empty_dir(self, tmp_path: Path) -> None:
        tech_stack, structure = _scan_project_root(tmp_path)
        assert tech_stack == []
        assert structure == []

    def test_structure_excludes_hidden(self, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        (tmp_path / "src").mkdir()
        (tmp_path / "README.md").write_text("")
        _tech_stack, structure = _scan_project_root(tmp_path)
        assert "src/" in structure
        assert "README.md" in structure
        assert ".git/" not in structure


# ---------------------------------------------------------------------------
# GenerateTasksStep
# ---------------------------------------------------------------------------


class TestGenerateTasksStep:
    @pytest.mark.asyncio
    async def test_generate_parses_llm_response(self) -> None:
        """GenerateTasksStep invokes LLM and parses JSON into PlannedTask list."""
        import json

        tasks_json = json.dumps(
            [
                {
                    "title": "feat(cli): add export command",
                    "body": "Export data to CSV.\n\n## Acceptance Criteria\n\n- [ ] CSV output works",
                    "labels": ["type: feature"],
                    "priority": "medium",
                    "complexity": "small",
                    "rationale": "Users need data export.",
                },
                {
                    "title": "fix(dashboard): correct timezone display",
                    "body": "Timestamps show UTC.\n\n## Acceptance Criteria\n\n- [ ] Local timezone used",
                    "labels": ["type: bug"],
                    "priority": "high",
                    "complexity": "small",
                    "rationale": "Confusing for users.",
                },
            ]
        )

        llm_result = MagicMock(text=tasks_json, cost_usd=Decimal("0.01"), total_tokens=500)
        ctx = _make_ctx()
        ctx.plan_result = PlanResult(scan=ProjectScanResult(raw_summary="test summary"))

        step = GenerateTasksStep()
        with patch("sova.llm.client.invoke", new_callable=AsyncMock, return_value=llm_result):
            result = await step.execute(ctx)

        assert result.success
        assert len(ctx.plan_result.proposed_tasks) == 2
        assert ctx.plan_result.proposed_tasks[0].title == "feat(cli): add export command"
        assert result.cost_usd == Decimal("0.01")

    @pytest.mark.asyncio
    async def test_generate_handles_fenced_json(self) -> None:
        """GenerateTasksStep handles markdown-fenced JSON responses."""
        fenced = (
            '```json\n[{"title": "feat(x): test",'
            ' "body": "Body here.\\n\\n## Acceptance Criteria\\n\\n- [ ] Done",'
            ' "labels": [], "priority": "low", "complexity": "small",'
            ' "rationale": "Test."}]\n```'
        )
        llm_result = MagicMock(text=fenced, cost_usd=Decimal("0.005"), total_tokens=200)
        ctx = _make_ctx()
        ctx.plan_result = PlanResult(scan=ProjectScanResult(raw_summary="test"))

        step = GenerateTasksStep()
        with patch("sova.llm.client.invoke", new_callable=AsyncMock, return_value=llm_result):
            result = await step.execute(ctx)

        assert result.success
        assert len(ctx.plan_result.proposed_tasks) == 1

    @pytest.mark.asyncio
    async def test_generate_fails_on_invalid_json(self) -> None:
        """GenerateTasksStep returns failure when LLM returns non-JSON."""
        llm_result = MagicMock(text="I cannot generate tasks.", cost_usd=Decimal("0.01"), total_tokens=50)
        ctx = _make_ctx()
        ctx.plan_result = PlanResult(scan=ProjectScanResult(raw_summary="test"))

        step = GenerateTasksStep()
        with patch("sova.llm.client.invoke", new_callable=AsyncMock, return_value=llm_result):
            result = await step.execute(ctx)

        assert not result.success
        assert "parse" in result.summary.lower()

    @pytest.mark.asyncio
    async def test_generate_fails_without_scan(self) -> None:
        """GenerateTasksStep fails when no scan result is available."""
        ctx = _make_ctx()
        step = GenerateTasksStep()
        result = await step.execute(ctx)
        assert not result.success
        assert "scan" in result.error.lower()

    @pytest.mark.asyncio
    async def test_generate_gate_check(self) -> None:
        """validate_output passes when proposed_tasks is non-empty."""
        ctx = _make_ctx()
        step = GenerateTasksStep()

        # No plan_result
        gate = await step.validate_output(ctx)
        assert not gate.passed

        # With tasks
        ctx.plan_result = PlanResult(proposed_tasks=[_make_valid_task()])
        gate = await step.validate_output(ctx)
        assert gate.passed

    @pytest.mark.asyncio
    async def test_generate_respects_budget(self) -> None:
        """GenerateTasksStep refuses to run when budget is exceeded."""
        ctx = _make_ctx()
        ctx.cost_usd = Decimal("999999")
        ctx.plan_result = PlanResult(scan=ProjectScanResult(raw_summary="test"))

        step = GenerateTasksStep()
        result = await step.execute(ctx)
        assert not result.success
        assert "budget" in result.summary.lower()


class TestExtractJson:
    def test_plain_json(self) -> None:
        assert _extract_json('[{"a": 1}]') == '[{"a": 1}]'

    def test_fenced_json(self) -> None:
        text = '```json\n[{"a": 1}]\n```'
        assert _extract_json(text) == '[{"a": 1}]'

    def test_fenced_no_lang(self) -> None:
        text = '```\n[{"a": 1}]\n```'
        assert _extract_json(text) == '[{"a": 1}]'


class TestParseTasks:
    def test_valid_array(self) -> None:
        import json

        data = json.dumps(
            [
                {
                    "title": "test",
                    "body": "body",
                    "labels": [],
                    "priority": "high",
                    "complexity": "small",
                    "rationale": "r",
                }
            ]
        )
        result = _parse_tasks(data)
        assert result is not None
        assert len(result) == 1
        assert result[0].title == "test"

    def test_not_a_list(self) -> None:
        assert _parse_tasks('{"key": "value"}') is None

    def test_invalid_json(self) -> None:
        assert _parse_tasks("not json at all") is None

    def test_empty_array(self) -> None:
        result = _parse_tasks("[]")
        assert result is not None
        assert len(result) == 0


# ---------------------------------------------------------------------------
# ValidateTasksStep
# ---------------------------------------------------------------------------


class TestValidateTasksStep:
    @pytest.mark.asyncio
    async def test_validate_accepts_good_tasks(self) -> None:
        """ValidateTasksStep accepts tasks that pass all checks."""
        titles = [
            "feat(core): add validation layer",
            "fix(dashboard): correct timezone display",
            "feat(cli): implement export command",
            "refactor(db): normalize schema tables",
            "feat(adapters): add linear integration",
        ]
        tasks = [_make_valid_task(t) for t in titles]
        ctx = _make_ctx()
        ctx.plan_result = PlanResult(
            scan=ProjectScanResult(open_issues=[]),
            proposed_tasks=tasks,
        )

        step = ValidateTasksStep()
        result = await step.execute(ctx)

        assert result.success
        assert len(ctx.plan_result.valid_tasks) == 5
        assert len(ctx.plan_result.rejected_reasons) == 0

    @pytest.mark.asyncio
    async def test_validate_rejects_vague_titles(self) -> None:
        """Tasks with vague titles are rejected."""
        tasks = [
            _make_valid_task("Improve things overall"),
            _make_valid_task("Update stuff"),
            _make_valid_task("feat(core): specific task one"),
            _make_valid_task("feat(cli): specific task two"),
            _make_valid_task("feat(db): specific task three"),
        ]
        ctx = _make_ctx()
        ctx.plan_result = PlanResult(
            scan=ProjectScanResult(open_issues=[]),
            proposed_tasks=tasks,
        )

        step = ValidateTasksStep()
        result = await step.execute(ctx)

        assert result.success
        assert len(ctx.plan_result.valid_tasks) == 3
        assert len(ctx.plan_result.rejected_reasons) == 2

    @pytest.mark.asyncio
    async def test_validate_rejects_missing_acceptance_criteria(self) -> None:
        """Tasks without acceptance criteria are rejected."""
        task = _make_valid_task()
        task.body = (
            "This task needs to be done. It involves changing several files "
            "and updating the configuration across multiple modules."
        )
        ctx = _make_ctx()
        ctx.plan_result = PlanResult(
            scan=ProjectScanResult(open_issues=[]),
            proposed_tasks=[
                task,
                _make_valid_task("feat(a): a1"),
                _make_valid_task("feat(b): b1"),
                _make_valid_task("feat(c): c1"),
            ],
        )

        step = ValidateTasksStep()
        result = await step.execute(ctx)

        assert result.success
        assert len(ctx.plan_result.rejected_reasons) == 1
        assert "acceptance criteria" in ctx.plan_result.rejected_reasons[0].lower()

    @pytest.mark.asyncio
    async def test_validate_detects_duplicates_against_open_issues(self) -> None:
        """Tasks that overlap with existing issues are rejected."""
        open_issues = [{"number": "10", "title": "Fix login bug in authentication module"}]
        tasks = [
            _make_valid_task("fix(auth): fix login bug in authentication module"),
            _make_valid_task("feat(dashboard): add new chart widget"),
            _make_valid_task("feat(cli): add export command"),
            _make_valid_task("feat(db): add migration tool"),
        ]
        ctx = _make_ctx()
        ctx.plan_result = PlanResult(
            scan=ProjectScanResult(open_issues=open_issues),
            proposed_tasks=tasks,
        )

        step = ValidateTasksStep()
        result = await step.execute(ctx)

        assert result.success
        assert len(ctx.plan_result.valid_tasks) == 3
        assert any("duplicate" in r.lower() for r in ctx.plan_result.rejected_reasons)

    @pytest.mark.asyncio
    async def test_validate_self_deduplicates(self) -> None:
        """Duplicate proposed tasks are filtered out."""
        tasks = [
            _make_valid_task("feat(core): add validation layer for inputs"),
            _make_valid_task("feat(core): add validation layer for all inputs"),  # near-duplicate
            _make_valid_task("feat(cli): add export command"),
            _make_valid_task("feat(db): add migration tool"),
        ]
        ctx = _make_ctx()
        ctx.plan_result = PlanResult(
            scan=ProjectScanResult(open_issues=[]),
            proposed_tasks=tasks,
        )

        step = ValidateTasksStep()
        result = await step.execute(ctx)

        assert result.success
        assert len(ctx.plan_result.valid_tasks) == 3
        assert any("self-duplicate" in r.lower() for r in ctx.plan_result.rejected_reasons)

    @pytest.mark.asyncio
    async def test_validate_fails_below_threshold(self) -> None:
        """ValidateTasksStep fails when fewer than 3 tasks pass."""
        tasks = [
            _make_valid_task("Improve everything"),
            _make_valid_task("Update all the things"),
        ]
        ctx = _make_ctx()
        ctx.plan_result = PlanResult(
            scan=ProjectScanResult(open_issues=[]),
            proposed_tasks=tasks,
        )

        step = ValidateTasksStep()
        result = await step.execute(ctx)

        assert not result.success
        assert "insufficient" in result.error.lower()

    @pytest.mark.asyncio
    async def test_validate_gate_check(self) -> None:
        """validate_output reflects valid_tasks count."""
        ctx = _make_ctx()
        step = ValidateTasksStep()

        # No plan_result
        gate = await step.validate_output(ctx)
        assert not gate.passed

        # Below threshold
        ctx.plan_result = PlanResult(valid_tasks=[_make_valid_task(), _make_valid_task("feat(b): b")])
        gate = await step.validate_output(ctx)
        assert not gate.passed

        # At threshold
        ctx.plan_result.valid_tasks.append(_make_valid_task("feat(c): c"))
        gate = await step.validate_output(ctx)
        assert gate.passed

    @pytest.mark.asyncio
    async def test_validate_rejects_short_body(self) -> None:
        """Tasks with body < 50 chars are rejected."""
        task = _make_valid_task()
        task.body = "Too short."
        ctx = _make_ctx()
        ctx.plan_result = PlanResult(
            scan=ProjectScanResult(open_issues=[]),
            proposed_tasks=[
                task,
                _make_valid_task("feat(a): a1"),
                _make_valid_task("feat(b): b1"),
                _make_valid_task("feat(c): c1"),
            ],
        )

        step = ValidateTasksStep()
        result = await step.execute(ctx)

        assert result.success
        assert any("too short" in r.lower() for r in ctx.plan_result.rejected_reasons)


class TestWordOverlap:
    def test_identical_strings(self) -> None:
        assert _word_overlap("fix login bug", "fix login bug") == 1.0

    def test_no_overlap(self) -> None:
        assert _word_overlap("alpha beta gamma", "delta epsilon zeta") == 0.0

    def test_partial_overlap(self) -> None:
        overlap = _word_overlap("fix login bug module", "fix login issue other")
        assert 0.3 < overlap < 0.7

    def test_empty_strings(self) -> None:
        assert _word_overlap("", "") == 0.0


# ---------------------------------------------------------------------------
# Pipeline composition
# ---------------------------------------------------------------------------


class TestPlannerPipeline:
    def test_get_planner_steps(self) -> None:
        from sova.core.steps import get_planner_steps

        steps = get_planner_steps()
        assert len(steps) == 4
        names = [s.name for s in steps]
        assert names == ["scan_project", "generate_tasks", "validate_tasks", "extract_memory"]

    def test_get_planner_step_names(self) -> None:
        from sova.core.steps import get_planner_step_names

        names = get_planner_step_names()
        assert names == ["scan_project", "generate_tasks", "validate_tasks", "extract_memory"]
