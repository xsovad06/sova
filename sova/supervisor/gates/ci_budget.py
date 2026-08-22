"""CI budget gate: blocks developer spawns when GitHub Actions minutes are low."""

from __future__ import annotations

from sova.supervisor.gates import BlockReason
from sova.utils.logging import get_logger

log = get_logger(component="supervisor.gates.ci_budget")


async def check_ci_budget_gate(
    *,
    is_developer: bool,
    github_user: str,
    github_repo: str,
    ci_block_minutes: int,
) -> BlockReason | None:
    """Check GitHub Actions CI minutes headroom. Blocks SPAWN_DEVELOPER when low. Fail-open."""
    if not is_developer:
        return None

    try:
        from sova.supervisor.ci_budget import _UNLIMITED_SENTINEL, get_ci_budget_tracker

        if not github_repo:
            return None
        if ci_block_minutes <= 0:
            return None

        tracker = get_ci_budget_tracker(github_user)
        budget = await tracker.get_budget(github_repo, github_user)

        if budget.remaining >= _UNLIMITED_SENTINEL:
            return None

        if budget.total == 0 and budget.remaining == 0:
            return None

        if budget.remaining < ci_block_minutes:
            return BlockReason(
                gate="ci_budget",
                detail=(f"CI minutes low: {budget.remaining} remaining (threshold: {ci_block_minutes})"),
            )
    except Exception:
        log.debug("ci_budget_gate.check_failed", exc_info=True)

    return None
