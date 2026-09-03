"""Cost tracking for LLM invocations."""

from __future__ import annotations

from sova.db.models import CostRecord
from sova.db.session import get_session
from sova.llm.models import LLMResult
from sova.utils.logging import get_logger

log = get_logger(component="llm.cost")


async def record_cost(
    result: LLMResult,
    phase: str,
    issue: str = "",
    task_run_id: int | None = None,
    model_selection_reason: str | None = None,
) -> CostRecord:
    """Persist an LLM invocation cost to the database.

    Args:
        result: The LLMResult from a Claude invocation.
        phase: Workflow phase (e.g., "develop", "review", "triage").
        issue: Issue number/identifier.
        task_run_id: Optional FK to the TaskRun this belongs to.
        model_selection_reason: Why this model was selected (e.g., "role:triage->haiku").

    Returns:
        The created CostRecord.
    """
    record = CostRecord(
        task_run_id=task_run_id,
        phase=phase,
        issue=issue.lstrip("#") if issue else issue,
        model=result.model,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        cache_tokens=result.cache_read_tokens + result.cache_creation_tokens,
        cache_read_tokens=result.cache_read_tokens,
        cache_write_tokens=result.cache_creation_tokens,
        cost_usd=result.cost_usd,
        duration_ms=result.duration_ms,
        model_selection_reason=model_selection_reason,
        pre_compression_input_tokens=result.pre_compression_input_tokens,
        tokens_saved=result.tokens_saved,
    )

    async with await get_session() as session:
        session.add(record)
        await session.commit()
        await session.refresh(record)

    log.info(
        "llm.cost_recorded",
        model=result.model,
        cost_usd=str(result.cost_usd),
        tokens=result.total_tokens,
        phase=phase,
        issue=issue,
    )

    return record
