"""Launch-modal suggestions and model checks: complexity tier, model suggestion, and model validation.

The complexity and suitability halves of the launch modal degrade to partial data on purpose (a warning or a
suggestion must never fail the modal), so their broad handlers stay. The model validators raise
``ModelOverrideError`` carrying the HTTP status and detail the route reports.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from sova.dashboard.services.agent_validation import classify_for_launch
from sova.dashboard.services.work_item_service import load_classification_graph
from sova.db.session import get_session
from sova.supervisor.suitability import AGENT_ELIGIBLE_MODES, Suitability
from sova.utils.logging import get_logger

log = get_logger(component="dashboard.llm_suggestion")

# The launch modal's warning waits this long for the tracker, the dependency graph and the classification
# together, so a slow tracker delays the modal by at most this and only loses the warning.
_SUITABILITY_DISPLAY_TIMEOUT_S = 5.0


class ModelOverrideError(Exception):
    """A model or internal LLM model override the run cannot honor; ``status_code`` is the HTTP status to report."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


async def _fetch_task_or_none(cfg: Any, issue_number: str) -> Any:
    """The tracker issue for the launch modal, or None (logged) when it cannot be read."""
    from sova.adapters import create_adapter

    try:
        return await create_adapter(cfg).get_task(issue_number)
    except Exception:  # noqa: BLE001 (complexity assessment falls back to the moderate tier)
        log.debug("complexity.task_fetch_failed", issue=issue_number, exc_info=True)
        return None


async def _display_suitability(
    issue_number: str, role: str | None, project_dir: Path, cfg: Any, task_future: asyncio.Future
) -> Suitability | None:
    """The issue's classification for the launch modal, dependencies included, or None when unknown.

    Runs concurrently with the complexity resolution, sharing its task fetch (*task_future*, shielded so this
    wait running out does not cancel the fetch the complexity step still needs). The dependency graph comes
    from the shared work-items build; with none available the classification falls back to labels only, rather
    than failing closed to a `blocked` that says nothing about the issue.
    """
    try:
        async with asyncio.timeout(_SUITABILITY_DISPLAY_TIMEOUT_S):
            task = await asyncio.shield(task_future)
            if task is None:
                return None
            graph = await load_classification_graph(project_dir)
            return await classify_for_launch(
                issue_number,
                role,
                project_dir,
                task=task,
                cfg=cfg,
                graph=graph,
                timeout_s=_SUITABILITY_DISPLAY_TIMEOUT_S,
            )
    except TimeoutError:
        log.warning("complexity.suitability_timed_out", issue=issue_number, timeout_s=_SUITABILITY_DISPLAY_TIMEOUT_S)
        return None
    except Exception:  # noqa: BLE001 (a warning must never fail the launch modal)
        log.warning("complexity.suitability_failed", issue=issue_number, exc_info=True)
        return None


async def get_issue_complexity(issue_number: str, role: str | None = None) -> dict:
    """Return complexity tier and suggested model for an issue.

    Checks TaskAssessmentRecord first; falls back to on-the-fly assessment
    via ``assess_complexity()`` using issue metadata from the adapter. ``suitability`` is the issue's
    classification (``mode`` and ``reasons``, open dependencies included) when it is anything but autonomous or
    unclassified, so the launch modal can warn before the user starts a run; null when the issue is fit or
    could not be classified within ``_SUITABILITY_DISPLAY_TIMEOUT_S``. It is resolved concurrently with the
    complexity, so a slow tracker costs the modal at most that timeout.
    The optional *role* names the run being launched: a triage or reviewer run is never refused, so for those
    (or a nickname of them, or no role on an issue the dispatcher would route to one) ``suitability`` is null.
    """
    from sova.config.context import resolve_project_dir_or_none
    from sova.config.loader import load_config
    from sova.dashboard.services.agent_pool import get_default_project_dir
    from sova.db.models import TaskAssessmentRecord
    from sova.llm.complexity import ComplexityTier, assess_complexity
    from sova.llm.routing import route_model

    # The single-project default counts: a dashboard started for a project elsewhere must read that project.
    project_dir = resolve_project_dir_or_none(default_project_dir=get_default_project_dir())
    cfg = load_config(project_dir)

    # One tracker read serves the complexity fallback and the suitability warning, started up front so the
    # DB lookup below overlaps it.
    task_future = asyncio.ensure_future(_fetch_task_or_none(cfg, issue_number))
    suitability_future = asyncio.ensure_future(_display_suitability(issue_number, role, project_dir, cfg, task_future))
    try:
        # 1. Check DB for existing assessment
        complexity_str: str | None = None
        try:
            async with await get_session(project_dir) as session:
                row = (
                    await session.execute(
                        select(TaskAssessmentRecord.estimated_complexity)
                        .where(TaskAssessmentRecord.issue_number == issue_number)
                        .order_by(TaskAssessmentRecord.assessed_at.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
        except (SQLAlchemyError, OSError):
            # The stored assessment is only a shortcut: without it the on-the-fly assessment below still answers.
            log.warning("complexity.assessment_lookup_failed", issue=issue_number, exc_info=True)
        else:
            if row:
                complexity_str = row

        # 2. Compute on-the-fly if no DB record
        if complexity_str is None:
            complexity_str = ComplexityTier.MODERATE.value
            task = await task_future
            if task is not None:
                try:
                    complexity_str = assess_complexity(
                        task.title,
                        task.description or "",
                        labels=list(task.labels or []),
                    ).value
                except Exception:  # noqa: BLE001 (complexity assessment falls back to the moderate tier)
                    log.debug("complexity.assess_fallback_failed", issue=issue_number, exc_info=True)

        suitability = await suitability_future
    finally:
        for pending in (suitability_future, task_future):
            if not pending.done():
                pending.cancel()

    try:
        tier = ComplexityTier(complexity_str)
    except ValueError:
        log.debug("complexity.invalid_db_value", value=complexity_str, issue=issue_number)
        tier = ComplexityTier.MODERATE
    model_alias, reason = route_model(tier, llm_config=cfg.llm)

    # The runtime catalog lists concrete IDs ("claude-opus-5"), never a bare
    # tier alias ("opus"), so the modal can only preselect the suggestion once
    # it is resolved the way the pipeline resolves it. A resolution failure
    # leaves the alias, which simply matches nothing and falls back to "no
    # override" in the modal.
    try:
        from sova.llm.client import resolve_alias

        suggested_model_id = resolve_alias(model_alias, cfg.llm)
    except Exception:  # noqa: BLE001 (a suggestion must never fail the launch modal)
        log.debug("complexity.resolve_alias_failed", alias=model_alias, exc_info=True)
        suggested_model_id = model_alias

    warn = suitability is not None and suitability.mode not in AGENT_ELIGIBLE_MODES

    # No per-tier option list: the launch modal's Model select is populated
    # from runtime-layer discovery (GET /api/models/available?layer=runtime,
    # #1039) and reads only the suggestion below, as the default it
    # preselects when that model is in the active runtime's catalog.
    return {
        "complexity": tier.value,
        "suggested_model": model_alias,
        "suggested_model_id": suggested_model_id,
        "reason": reason,
        "confirm_model": cfg.dashboard.confirm_model,
        "suitability": {"mode": suitability.mode.value, "reasons": suitability.reasons} if warn else None,
    }


async def validate_llm_model_override(role: str | None, model_id: str) -> None:
    """Reject an override the run could not honor, before anything is spawned.

    Checked against a fresh read of the same ``layer="llm"`` discovery the
    launch modal populated from, so an ID that stopped being valid between
    page load and submit (e.g. the provider config changed) is caught here
    rather than at the first model call.

    A ``source: "curated"`` payload is treated as no answer at all, not as a
    catalog to validate against: it is ``models_service``'s Anthropic-only
    static fallback, returned while the provider is unauthenticated,
    misconfigured, or timed out. The launch modal already refuses to offer
    any model in that state, so accepting one here would let only a stale
    page or a scripted call through, with an ID the configured provider
    cannot serve.

    Raises ``ModelOverrideError`` (400 for a bad role or unknown ID, 503 when discovery cannot verify it).
    """
    from sova.config.context import resolve_project_dir
    from sova.dashboard.services import models_service
    from sova.dashboard.services.agent_lifecycle import llm_model_override_role_error
    from sova.dashboard.services.agent_pool import get_default_project_dir

    role_error = llm_model_override_role_error(role)
    if role_error:
        raise ModelOverrideError(400, role_error)

    try:
        available = await models_service.get_available_models(
            resolve_project_dir(default_project_dir=get_default_project_dir()), layer="llm"
        )
    except Exception as exc:  # noqa: BLE001 (any discovery failure means the override cannot be verified)
        log.warning("agents.llm_model_override_discovery_failed", model=model_id, exc_info=True)
        raise ModelOverrideError(
            503, "Could not verify the internal LLM model override: model discovery is unavailable"
        ) from exc

    if available.get("source") == "curated":
        log.warning("agents.llm_model_override_discovery_curated", model=model_id)
        raise ModelOverrideError(
            503,
            "Could not verify the internal LLM model override: the current provider's model "
            "discovery is unavailable (serving a curated fallback list)",
        )

    known_ids = {entry.get("id") for entry in available.get("models", [])}
    if model_id not in known_ids:
        raise ModelOverrideError(
            400,
            f"Unknown internal LLM model {model_id!r}: not offered by the current provider's model discovery",
        )


async def validate_runtime_model(model_id: str) -> None:
    """Reject a runtime model the run could not honor, before anything is spawned.

    The ``layer="runtime"`` counterpart of ``validate_llm_model_override()``,
    against a fresh read of the discovery the launch modal populated from. A
    runtime that ignores a caller-supplied model (``honours_model_override``
    false, e.g. Codex) is a 400, not a silent no-op; an ``"unavailable"``
    catalog cannot vouch for any ID, so it is a 503 like a failed fetch.

    Raises ``ModelOverrideError`` with the status the route reports.
    """
    from sova.config.context import resolve_project_dir
    from sova.dashboard.services import models_service
    from sova.dashboard.services.agent_pool import get_default_project_dir

    try:
        available = await models_service.get_available_models(
            resolve_project_dir(default_project_dir=get_default_project_dir()), layer="runtime"
        )
    except Exception as exc:  # noqa: BLE001 (any discovery failure means the model cannot be verified)
        log.warning("agents.runtime_model_discovery_failed", model=model_id, exc_info=True)
        raise ModelOverrideError(503, "Could not verify the runtime model: model discovery is unavailable") from exc

    if available.get("source") == "unavailable":
        log.warning("agents.runtime_model_discovery_unavailable", model=model_id)
        raise ModelOverrideError(
            503, "Could not verify the runtime model: the agent runtime is unavailable or unauthenticated"
        )
    if available.get("honours_model_override") is False:
        raise ModelOverrideError(
            400, f"The {available.get('provider_identity')} runtime does not support choosing a model"
        )
    known_ids = {entry.get("id") for entry in available.get("models", [])}
    if model_id not in known_ids:
        raise ModelOverrideError(400, f"Unknown runtime model {model_id!r}: not in the active runtime's catalog")
