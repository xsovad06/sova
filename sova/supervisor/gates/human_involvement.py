"""Human involvement gate: blocks epic, human-only and interactive-recommended issues."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import async_sessionmaker

from sova.adapters.base import Task
from sova.knowledge.assessments import get_latest_assessment
from sova.supervisor.gates import BlockReason
from sova.supervisor.labels import is_epic, is_human_only, is_interactive_recommended, is_ready
from sova.supervisor.suitability import SuitabilityMode, assess_suitability
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


# The modes this gate ever blocks on. `blocked` is deliberately absent:
# dependency blocking is `check_dependency_gate`'s job and is config-gated by
# `supervisor.respect_dependencies`, so honouring it here would bypass that
# switch. `autonomous`/`unclassified` never block. Kept explicit (rather than
# inferred from `_BLOCKS`' keys) so a mode that becomes blocking in the future
# cannot silently fall through `_BLOCKS.get()`'s `None` default into "allow".
_BLOCKING_MODES: frozenset[SuitabilityMode] = frozenset(
    {SuitabilityMode.EPIC, SuitabilityMode.HUMAN, SuitabilityMode.INTERACTIVE}
)

# (suitability mode, came from the local record) -> (log event, detail template).
_BLOCKS: dict[tuple[SuitabilityMode, bool], tuple[str, str]] = {
    (SuitabilityMode.EPIC, False): (
        "human_involvement_gate.blocked_epic",
        "Issue #{issue} is labeled type: epic (tracking container, not agent work)",
    ),
    (SuitabilityMode.HUMAN, False): (
        "human_involvement_gate.blocked_human_only",
        "Issue #{issue} is labeled agent:human-only",
    ),
    (SuitabilityMode.HUMAN, True): (
        "human_involvement_gate.blocked_human_only_local",
        "Issue #{issue} has a local human-only suitability assessment (clear with `sova assess clear {issue}`)",
    ),
    (SuitabilityMode.INTERACTIVE, False): (
        "human_involvement_gate.blocked_interactive_recommended",
        "Issue #{issue} is labeled agent:interactive-recommended (manual start only)",
    ),
    (SuitabilityMode.INTERACTIVE, True): (
        "human_involvement_gate.blocked_interactive_recommended_local",
        "Issue #{issue} has a local interactive-recommended suitability assessment "
        "(manual start only; clear with `sova assess clear {issue}`)",
    ),
}


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
    """Block issues classified `epic`, `human` or `interactive` by `assess_suitability`.

    human-only is a hard block (defense-in-depth alongside state-based HUMAN_ONLY).
    interactive-recommended blocks autonomous scheduling; manual dashboard starts
    bypass the progression engine entirely. An epic is a tracking container that
    no role should ever be spawned on.

    Classification runs with `consider_dependencies=False`, never the caller's
    real dependency graph: `blocked` outranks `interactive` in the classifier's
    precedence, so feeding a real graph in would silently downgrade an
    interactive-recommended issue that also has an open dependency to `blocked`
    and stop this gate blocking it at all. Dependency blocking is
    `check_dependency_gate`'s job, and is config-gated separately.

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
    task = Task(id=str(issue), title="", labels=labels)

    local_assessment: str | None = None
    # Guarded by the same labels `assess_suitability()` would use to decide
    # the verdict without ever consulting the local record: when one of them
    # is already present, the DB lookup's result can never change the
    # outcome, so skip the session open and query entirely.
    if (
        session_factory is not None
        and candidate in _PRE_DEVELOPMENT_CANDIDATES
        and not is_ready(labels)
        and not _is_past_pre_development(labels)
        and not is_human_only(labels)
        and not is_interactive_recommended(labels)
        and not is_epic(labels)
    ):
        try:
            async with session_factory() as session:
                record = await get_latest_assessment(session, issue_number=str(issue), project_slug=project_slug)
            local_assessment = record.suitability if record is not None else None
        # An unreadable local record must fail open on this layer only: a
        # label-based block must still apply regardless of local store health.
        except Exception:  # noqa: BLE001 (unreadable local store must fail open, not block)
            log.warning("human_involvement_gate.local_lookup_failed", issue=issue, exc_info=True)

    suitability = assess_suitability(task, consider_dependencies=False, local_assessment=local_assessment)
    from_local = any(reason.get("source") == "local_assessment" for reason in suitability.reasons)

    block = _BLOCKS.get((suitability.mode, from_local))
    if block is None:
        if suitability.mode not in _BLOCKING_MODES:
            return None
        log.warning(
            "human_involvement_gate.unmapped_blocking_mode",
            issue=issue,
            mode=suitability.mode.value,
            from_local=from_local,
            reasons=suitability.reasons,
        )
        return BlockReason(
            gate="human_involvement",
            detail=f"Issue #{issue} classified as {suitability.mode.value} with no mapped block message "
            f"(reasons: {suitability.reasons})",
        )
    event, detail = block
    log.info(event, issue=issue)
    return BlockReason(gate="human_involvement", detail=detail.format(issue=issue))
