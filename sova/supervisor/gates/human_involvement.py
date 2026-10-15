"""Human involvement gate: blocks human-only and interactive-recommended issues."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import async_sessionmaker

from sova.knowledge.assessments import get_latest_assessment
from sova.supervisor.dependency_graph import is_human_only, is_interactive_recommended, is_ready
from sova.supervisor.gates import BlockReason
from sova.utils.logging import get_logger

log = get_logger(component="supervisor.gates.human_involvement")

# The only candidates where a local suitability record may block: everything
# before a PR exists. Once a candidate reaches address-review, integrate, or
# any repair/reset action, a local record (which nothing ever clears) must
# never be able to block finishing work a human already started.
_PRE_DEVELOPMENT_CANDIDATES = frozenset({"spawn_triage", "spawn_researcher", "spawn_developer"})

# Any of these pipeline-state labels mean the issue has moved past the point
# where a local suitability record applies, mirroring how a tracker label is
# stripped by `set_state()` on every real transition. Checked alongside
# `agent:ready` so the local-record fallback self-clears instead of needing
# `sova assess clear` to be remembered.
_PAST_PRE_DEVELOPMENT_LABELS = frozenset(
    {"agent:triaged", "agent:researched", "agent:in-progress", "agent:in-review", "agent:on-qa"}
)


def _is_past_pre_development(labels: list[str]) -> bool:
    normalized = {label.lower().strip() for label in labels}
    return bool(normalized & _PAST_PRE_DEVELOPMENT_LABELS)


async def check_human_involvement_gate(
    issue: int,
    labels: list[str],
    *,
    session_factory: async_sessionmaker | None = None,
    project_slug: str = "",
    candidate: str = "",
) -> BlockReason | None:
    """Block issues labeled agent:human-only or agent:interactive-recommended.

    human-only is a hard block (defense-in-depth alongside state-based HUMAN_ONLY).
    interactive-recommended blocks autonomous scheduling; manual dashboard starts
    bypass the progression engine entirely.

    When neither suitability label is present, falls back to the local
    `task_assessments` record (`sova assess set`), used for unassigned tickets
    that never receive a tracker label. A label always wins over the local
    record when both exist, in both directions: a `ready` label clears any
    conflicting local `human_only`/`interactive_recommended` record, and vice
    versa a local `ready` record never overrides a blocking label.

    Unlike a tracker label, the local record never self-clears, so the
    fallback is scoped to pre-development candidates
    (`spawn_triage`/`spawn_researcher`/`spawn_developer`, passed as
    `candidate`): a record can gate *starting* work but can never block an
    already-started PR from being reviewed, addressed, or integrated. Any
    later pipeline-state label (`agent:triaged`, `agent:researched`,
    `agent:in-progress`, `agent:in-review`, `agent:on-qa`) short-circuits the
    fallback the same way `agent:ready` does, as additional defense in depth.
    """
    if is_human_only(labels):
        log.info("human_involvement_gate.blocked_human_only", issue=issue)
        return BlockReason(
            gate="human_involvement",
            detail=f"Issue #{issue} is labeled agent:human-only",
        )
    if is_interactive_recommended(labels):
        log.info("human_involvement_gate.blocked_interactive_recommended", issue=issue)
        return BlockReason(
            gate="human_involvement",
            detail=f"Issue #{issue} is labeled agent:interactive-recommended (manual start only)",
        )

    if (
        session_factory is None
        or candidate not in _PRE_DEVELOPMENT_CANDIDATES
        or is_ready(labels)
        or _is_past_pre_development(labels)
    ):
        return None

    try:
        async with session_factory() as session:
            record = await get_latest_assessment(session, issue_number=str(issue), project_slug=project_slug)
        suitability = record.suitability if record is not None else None
    # An unreadable local record must fail open, not block.
    except Exception:  # noqa: BLE001 (unreadable local store must fail open, not block)
        log.warning("human_involvement_gate.local_lookup_failed", issue=issue, exc_info=True)
        return None

    if suitability == "human_only":
        log.info("human_involvement_gate.blocked_human_only_local", issue=issue)
        return BlockReason(
            gate="human_involvement",
            detail=(
                f"Issue #{issue} has a local human-only suitability assessment (clear with `sova assess clear {issue}`)"
            ),
        )
    if suitability == "interactive_recommended":
        log.info("human_involvement_gate.blocked_interactive_recommended_local", issue=issue)
        return BlockReason(
            gate="human_involvement",
            detail=(
                f"Issue #{issue} has a local interactive-recommended suitability assessment "
                f"(manual start only; clear with `sova assess clear {issue}`)"
            ),
        )
    return None
