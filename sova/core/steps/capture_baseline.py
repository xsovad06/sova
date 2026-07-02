"""Step 3b: Capture test baseline snapshot before development begins.

Runs the project's test suite in the fresh worktree and stores per-test
results as a JSON artifact. Downstream steps (ValidateStep, MonitorCIStep)
diff current results against this baseline to distinguish true regressions
from pre-existing failures.

Non-fatal: a failed capture does not block the pipeline.
"""

from __future__ import annotations

from sova.core.context import ExecutionContext
from sova.core.steps.base import BaseStep, GateCheckResult, StepResult
from sova.core.test_baseline import baseline_path, run_test_suite, save_baseline
from sova.utils.logging import get_logger

log = get_logger(component="step.capture_baseline")


class CaptureBaselineStep(BaseStep):
    name = "capture_baseline"

    async def execute(self, ctx: ExecutionContext) -> StepResult:
        if not ctx.config.testing.baseline_enabled:
            log.info("step.capture_baseline.disabled")
            return StepResult(success=True, summary="Test baseline capture disabled via config")

        if ctx.worktree_dir is None:
            log.warning("step.capture_baseline.no_worktree")
            return StepResult(success=True, summary="No worktree, skipping baseline capture")

        test_cmd = ctx.config.test_cmd
        if not test_cmd or not test_cmd.strip():
            log.info("step.capture_baseline.no_test_cmd")
            return StepResult(success=True, summary="No test command configured, skipping baseline")

        try:
            snapshot = await run_test_suite(
                test_cmd=test_cmd,
                cwd=ctx.worktree_dir,
                cmd_timeout=ctx.config.testing.baseline_timeout,
            )
            path = save_baseline(snapshot, ctx.worktree_dir)
            ctx.test_baseline_path = path

            test_count = len(snapshot.tests)
            mode_label = "per-test" if snapshot.mode == "per_test" else "exit-code only"
            summary = f"Baseline captured ({mode_label}, {test_count} tests, exit code {snapshot.exit_code})"
            log.info("step.capture_baseline.done", mode=snapshot.mode, tests=test_count)
            return StepResult(success=True, summary=summary)

        except Exception as exc:
            log.warning("step.capture_baseline.failed", error=str(exc), exc_info=True)
            return StepResult(
                success=True,
                summary=f"Baseline capture failed (non-fatal): {exc}",
            )

    async def validate_output(self, ctx: ExecutionContext) -> GateCheckResult:
        # Non-fatal: always pass. The baseline is optional.
        return GateCheckResult(passed=True)

    async def can_skip(self, ctx: ExecutionContext) -> bool:
        if self.name in ctx.completed_steps:
            return True
        # Skip if baseline already exists (resume after crash)
        if ctx.worktree_dir and baseline_path(ctx.worktree_dir).exists():
            ctx.test_baseline_path = baseline_path(ctx.worktree_dir)
            log.info("step.capture_baseline.already_exists", path=str(ctx.test_baseline_path))
            return True
        return False
