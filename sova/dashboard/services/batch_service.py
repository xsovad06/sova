"""Batch operations: triage/harden/run multiple issues from the dashboard."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from sova.dashboard.services.feed_service import FeedEventSeverity, emit_safe
from sova.dashboard.services.queue_service import VALID_STATES_FOR_ACTION
from sova.utils.logging import get_logger

if TYPE_CHECKING:
    from sova.adapters.base import TaskAdapter
    from sova.config.models import ProjectConfig
    from sova.roles.base import TaskAssessment
    from sova.roles.triage import TriageRole

log = get_logger(component="dashboard.batch")

DEFAULT_CONCURRENCY = {"triage": 3, "harden": 2}


@dataclass
class BatchItemResult:
    issue_id: str
    status: str = "pending"  # pending | running | done | failed | skipped
    detail: str = ""


@dataclass
class BatchJob:
    batch_id: str
    action: str
    status: str = "running"  # running | done | cancelled
    results: list[BatchItemResult] = field(default_factory=list)
    cancelled: bool = False
    max_concurrency: int = 1
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: datetime | None = None
    project_dir: Path | None = None
    _task: asyncio.Task | None = field(default=None, repr=False)

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def completed_count(self) -> int:
        return sum(1 for r in self.results if r.status in ("done", "failed", "skipped"))

    def to_dict(self) -> dict:
        return {
            "batch_id": self.batch_id,
            "action": self.action,
            "status": self.status,
            "total": self.total,
            "completed": self.completed_count,
            "failed": sum(1 for r in self.results if r.status == "failed"),
            "max_concurrency": self.max_concurrency,
            "results": [{"issue_id": r.issue_id, "status": r.status, "detail": r.detail} for r in self.results],
        }


_active_batches: dict[str, BatchJob] = {}

_MAX_COMPLETED_BATCHES = 50


async def cancel_all_batches() -> None:
    """Cancel all running batch tasks. Called during lifespan shutdown."""
    tasks = []
    for job in _active_batches.values():
        if job._task is not None and not job._task.done():
            job._task.cancel()
            tasks.append(job._task)
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def _prune_completed() -> None:
    """Remove oldest completed batches when over the limit."""
    completed = [(bid, job) for bid, job in _active_batches.items() if job.status != "running"]
    if len(completed) > _MAX_COMPLETED_BATCHES:
        completed.sort(key=lambda x: x[1].completed_at or x[1].created_at)
        for bid, _ in completed[: len(completed) - _MAX_COMPLETED_BATCHES]:
            del _active_batches[bid]


def get_batch_status(batch_id: str) -> dict | None:
    job = _active_batches.get(batch_id)
    if job is None:
        return None
    return job.to_dict()


def get_active_batch(project_dir: Path | None = None) -> dict | None:
    """Return the first running batch, optionally filtered by project."""
    for job in _active_batches.values():
        if job.status != "running":
            continue
        if project_dir is not None and job.project_dir != project_dir:
            continue
        return job.to_dict()
    return None


def cancel_batch(batch_id: str) -> bool:
    job = _active_batches.get(batch_id)
    if job is None or job.status != "running":
        return False
    job.cancelled = True
    return True


def start_batch(
    action: str,
    issue_ids: list[str],
    project_dir: Path,
    options: dict | None = None,
) -> str:
    """Start a batch operation and return the batch_id."""
    opts = options or {}
    concurrency = opts.get("max_concurrency", DEFAULT_CONCURRENCY.get(action, 1))

    batch_id = uuid.uuid4().hex[:12]
    job = BatchJob(
        batch_id=batch_id,
        action=action,
        max_concurrency=concurrency,
        results=[BatchItemResult(issue_id=iid) for iid in issue_ids],
        project_dir=project_dir,
    )
    _active_batches[batch_id] = job

    if action == "triage":
        job._task = asyncio.create_task(_run_batch_triage(job, project_dir))
    elif action == "harden":
        skip_triage = opts.get("skip_triage", False)
        job._task = asyncio.create_task(_run_batch_harden(job, project_dir, skip_triage=skip_triage))
    else:
        job.status = "done"
        for r in job.results:
            r.status = "failed"
            r.detail = f"Unknown action: {action}"

    log.info("batch.started", batch_id=batch_id, action=action, count=len(issue_ids), concurrency=concurrency)

    emit_safe(
        f"Batch {action} started ({len(issue_ids)} issues)",
        category="batch",
        metadata={"batch_id": batch_id, "action": action, "count": len(issue_ids)},
    )

    return batch_id


async def start_batch_run(
    issue_ids: list[str],
    _project_dir: Path,
) -> dict:
    """Start an agent for the first issue. Returns status dict."""
    from sova.dashboard.services.control_service import start_agent

    if not issue_ids:
        return {"error": "No issues provided"}

    first = issue_ids[0]
    result = await start_agent(issue=first)

    return {
        "started": first,
        "remaining": issue_ids[1:],
        "agent_result": result,
    }


async def _run_batch_triage(job: BatchJob, project_dir: Path) -> None:
    try:
        from sova.adapters import create_adapter
        from sova.adapters.base import TaskState
        from sova.config.loader import load_config
        from sova.db.session import init_db
        from sova.roles.triage import TriageRole

        await init_db(project_dir)
        config = load_config(project_dir)
        adapter = create_adapter(config)
        role = TriageRole()

        batch_assessments = await _try_batch_llm_triage(job, config, adapter, role, project_dir)

        sem = asyncio.Semaphore(job.max_concurrency)

        async def _process_item(item: BatchItemResult) -> None:
            async with sem:
                if job.cancelled:
                    item.status = "skipped"
                    item.detail = "Cancelled"
                    return

                item.status = "running"
                try:
                    task = await adapter.get_task(item.issue_id)

                    if task.state not in VALID_STATES_FOR_ACTION["triage"]:
                        item.status = "skipped"
                        item.detail = f"State {task.state.value} not eligible for triage"
                        return

                    triage_cfg = config.triage

                    assessment = batch_assessments.get(item.issue_id)
                    if assessment is None:
                        assessment = role.heuristic_assess(task, triage_cfg)

                    if triage_cfg.mode == "dry_run":
                        item.status = "done"
                        item.detail = f"Suitability: {assessment.suitability} (dry run)"
                        log.info("batch.triage.dry_run", issue=item.issue_id, suitability=assessment.suitability)
                        return

                    if triage_cfg.auto_label:
                        label_name = role.resolve_label(assessment.suitability, triage_cfg)
                        if label_name:
                            await adapter.add_label(task.id, label_name)

                    assessment_section = role._build_assessment_comment(task, assessment)
                    if triage_cfg.mode == "comment":
                        await adapter.post_comment(task.id, assessment_section)
                    elif triage_cfg.write_body:
                        updated_body = (task.body or "").rstrip() + "\n\n" + assessment_section
                        await adapter.edit_body(task.id, updated_body)

                    if triage_cfg.write_transition and task.state in role.allowed_input_states:
                        await adapter.transition_state(task.id, TaskState.TRIAGED)

                    item.status = "done"
                    item.detail = f"Suitability: {assessment.suitability}"
                    log.info("batch.triage.done", issue=item.issue_id, suitability=assessment.suitability)

                except Exception as exc:
                    item.status = "failed"
                    item.detail = str(exc)
                    log.warning("batch.triage.failed", issue=item.issue_id, error=str(exc))

        await asyncio.gather(*[_process_item(item) for item in job.results])

    except Exception as exc:
        log.error("batch.triage.fatal", error=str(exc))
        for item in job.results:
            if item.status == "pending":
                item.status = "failed"
                item.detail = f"Batch setup failed: {exc}"
    finally:
        job.status = "cancelled" if job.cancelled else "done"
        job.completed_at = datetime.now(timezone.utc)
        _prune_completed()
        log.info("batch.completed", batch_id=job.batch_id, status=job.status)

        failed = sum(1 for r in job.results if r.status == "failed")
        sev = FeedEventSeverity.warning if failed else FeedEventSeverity.success
        total = len(job.results)
        outcome = "cancelled" if job.cancelled else "completed"
        emit_safe(
            f"Batch triage {outcome}: {total - failed}/{total} succeeded",
            severity=sev,
            category="batch",
            metadata={"batch_id": job.batch_id, "failed": failed, "total": total},
        )


async def _run_batch_harden(
    job: BatchJob,
    project_dir: Path,
    *,
    skip_triage: bool,
) -> None:
    try:
        from dataclasses import replace

        from sova.adapters import create_adapter
        from sova.adapters.base import TaskFilters
        from sova.cli.commands.harden import (
            _build_harden_prompt,
            _detect_issue_type,
            _format_issues_summary,
            _load_issue_template,
            _load_project_docs,
        )
        from sova.config.loader import load_config
        from sova.db.session import init_db
        from sova.llm.client import invoke
        from sova.roles.triage import TriageRole
        from sova.utils.markdown import strip_code_fences as _strip_code_fences
        from sova.utils.markdown import strip_preamble

        await init_db(project_dir)
        config = load_config(project_dir)
        adapter = create_adapter(config)

        all_open = await adapter.list_tasks(TaskFilters(state="open"))
        project_docs = _load_project_docs(project_dir)
        all_issues_summary = _format_issues_summary(all_open)

        sem = asyncio.Semaphore(job.max_concurrency)

        async def _process_item(item: BatchItemResult) -> None:
            async with sem:
                if job.cancelled:
                    item.status = "skipped"
                    item.detail = "Cancelled"
                    return

                item.status = "running"
                try:
                    task = await adapter.get_task(item.issue_id)

                    if task.state not in VALID_STATES_FOR_ACTION["harden"]:
                        item.status = "skipped"
                        item.detail = f"State {task.state.value} not eligible for harden"
                        return

                    issue_type = _detect_issue_type(task.labels)
                    template_content = _load_issue_template(project_dir, issue_type)
                    prompt = _build_harden_prompt(task, project_docs, all_issues_summary, template_content, issue_type)

                    result = await invoke(prompt, task_type="harden", cwd=project_dir, timeout=300)
                    enriched_body = strip_preamble(_strip_code_fences(result.text))

                    if not enriched_body.strip():
                        item.status = "failed"
                        item.detail = "LLM returned empty result"
                        return

                    await adapter.edit_body(task.id, enriched_body)

                    triage_detail = ""
                    if not skip_triage:
                        try:
                            enriched_task = replace(task, body=enriched_body)
                            role = TriageRole()
                            triage_cfg = config.triage
                            assessment = role.heuristic_assess(enriched_task, triage_cfg)
                            if triage_cfg.auto_label:
                                label = role.resolve_label(assessment.suitability, triage_cfg)
                                if label:
                                    await adapter.add_label(task.id, label)
                            triage_detail = f", re-triaged: {assessment.suitability}"
                        except Exception:
                            triage_detail = ", re-triage failed"

                    item.status = "done"
                    item.detail = f"Hardened{triage_detail}"
                    log.info("batch.harden.done", issue=item.issue_id)

                except Exception as exc:
                    item.status = "failed"
                    item.detail = str(exc)
                    log.warning("batch.harden.failed", issue=item.issue_id, error=str(exc))

        await asyncio.gather(*[_process_item(item) for item in job.results])

    except Exception as exc:
        log.error("batch.harden.fatal", error=str(exc))
        for item in job.results:
            if item.status == "pending":
                item.status = "failed"
                item.detail = f"Batch setup failed: {exc}"
    finally:
        job.status = "cancelled" if job.cancelled else "done"
        job.completed_at = datetime.now(timezone.utc)
        _prune_completed()
        log.info("batch.completed", batch_id=job.batch_id, status=job.status)

        failed = sum(1 for r in job.results if r.status == "failed")
        sev = FeedEventSeverity.warning if failed else FeedEventSeverity.success
        total = len(job.results)
        emit_safe(
            f"Batch harden completed: {total - failed}/{total} succeeded",
            severity=sev,
            category="batch",
            metadata={"batch_id": job.batch_id, "failed": failed, "total": total},
        )


async def _try_batch_llm_triage(
    job: BatchJob,
    config: ProjectConfig,
    adapter: TaskAdapter,
    role: TriageRole,
    project_dir: Path,
) -> dict[str, TaskAssessment]:
    """Attempt LLM-based batch triage via the Batch API.

    Returns a dict mapping issue_id -> TaskAssessment for issues that were
    successfully assessed via the batch path. Returns empty dict if the batch
    path is unavailable or fails.
    """

    if "triage" not in config.llm.batch_eligible_tasks:
        return {}

    from sova.llm.providers.anthropic_batch import create_batch_provider

    if create_batch_provider(gcs_bucket=config.llm.batch_gcs_bucket, gcs_prefix=config.llm.batch_gcs_prefix) is None:
        return {}

    try:
        from sova.core.context import ExecutionContext

        ctx = ExecutionContext(project_dir=project_dir, config=config, adapter=adapter)

        eligible_tasks = []
        task_issue_ids: dict[str, str] = {}
        for item in job.results:
            if job.cancelled:
                break
            try:
                task = await adapter.get_task(item.issue_id)
                if task.state not in VALID_STATES_FOR_ACTION["triage"]:
                    continue
                eligible_tasks.append(task)
                task_issue_ids[str(task.id)] = item.issue_id
            except Exception:
                log.warning("batch.triage.fetch_failed", issue=item.issue_id, exc_info=True)

        if not eligible_tasks:
            return {}

        log.info("batch.triage.llm_batch", count=len(eligible_tasks))
        results = await role.assess_tasks_batch(eligible_tasks, ctx)

        assessments: dict[str, TaskAssessment] = {}
        for task, assessment in results:
            issue_id = task_issue_ids.get(str(task.id), str(task.id))
            assessments[issue_id] = assessment

        log.info("batch.triage.llm_batch_done", assessed=len(assessments))
        return assessments

    except Exception as exc:
        log.warning("batch.triage.llm_batch_failed", error=str(exc), exc_info=True)
        return {}
