"""Rebase with LLM-assisted conflict resolution."""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from sova.llm.client import invoke_command
from sova.llm.models import LLMResult
from sova.utils.logging import get_logger
from sova.utils.shell import run

log = get_logger(component="git.rebase")


@dataclass
class RebaseResult:
    """Outcome of a rebase-with-conflict-resolution attempt."""

    success: bool
    conflicts_resolved: int = 0
    error: str = ""


async def _get_conflicted_files(cwd: Path | None = None) -> list[str]:
    """Return list of files with merge conflicts (unmerged paths)."""
    result = await run("git", "diff", "--name-only", "--diff-filter=U", cwd=cwd)
    if not result.success or not result.stdout.strip():
        return []
    return [f.strip() for f in result.stdout.strip().splitlines() if f.strip()]


async def _resolve_conflicts_with_llm(
    conflicted_files: list[str],
    *,
    cwd: Path,
    model: str | None = None,
    max_budget_usd: Decimal | None = None,
) -> LLMResult:
    """Invoke the LLM to resolve merge conflicts in the given files."""
    file_list = "\n".join(f"- `{f}`" for f in conflicted_files)
    prompt = (
        "The following files have git merge conflicts (<<<<<<< / ======= / >>>>>>> markers). "
        "Resolve each conflict by choosing the correct code or merging both sides as appropriate. "
        "Keep the code correct and all tests passing. Do NOT leave any conflict markers.\n\n"
        f"Conflicted files:\n{file_list}\n\n"
        "After resolving, stage each file with `git add`."
    )
    return await invoke_command(prompt, model=model, cwd=cwd, max_budget_usd=max_budget_usd)


async def rebase_with_conflict_resolution(
    base: str,
    *,
    cwd: Path,
    model: str | None = None,
    max_budget_usd: Decimal | None = None,
    max_attempts: int = 3,
    max_commits: int = 5,
) -> tuple[RebaseResult, Decimal]:
    """Rebase onto *base*, using the LLM to resolve conflicts if needed.

    Uses a two-level loop: the outer loop iterates over conflicting commits
    (capped by *max_commits*), while the inner loop retries LLM resolution
    for each commit (capped by *max_attempts*). This prevents a multi-commit
    rebase from exhausting all retry attempts on the first commit.

    Returns a (RebaseResult, cost_usd) tuple.  On unrecoverable failure the
    rebase is aborted so the worktree is never left in a broken state.
    """
    cost = Decimal("0")

    fetch = await run("git", "fetch", "origin", base, cwd=cwd)
    if not fetch.success:
        return RebaseResult(success=False, error=f"Fetch failed: {fetch.stderr[:200]}"), cost

    result = await run("git", "rebase", f"origin/{base}", cwd=cwd)
    if result.success:
        return RebaseResult(success=True), cost

    conflicts_resolved = 0
    hit_commit_cap = False
    for commit_idx in range(max_commits):
        conflicted = await _get_conflicted_files(cwd=cwd)
        if not conflicted:
            env = {**os.environ, "GIT_EDITOR": "true"}
            cont = await run("git", "rebase", "--continue", cwd=cwd, env=env)
            if cont.success:
                return RebaseResult(success=True, conflicts_resolved=conflicts_resolved), cost
            log.warning(
                "git.rebase.continue_failed",
                commit=commit_idx + 1,
                stdout=(cont.stdout or "")[:200],
                stderr=(cont.stderr or "")[:200],
            )
            break

        remaining: list[str] = []
        for attempt in range(1, max_attempts + 1):
            log.info("git.rebase.resolving_conflicts", files=conflicted, commit=commit_idx + 1, attempt=attempt)
            try:
                llm_result = await _resolve_conflicts_with_llm(
                    conflicted,
                    cwd=cwd,
                    model=model,
                    max_budget_usd=max_budget_usd,
                )
                cost += llm_result.cost_usd
            except RuntimeError as exc:
                log.warning("git.rebase.llm_failed", commit=commit_idx + 1, attempt=attempt, error=str(exc))
                await run("git", "rebase", "--abort", cwd=cwd)
                return RebaseResult(success=False, conflicts_resolved=conflicts_resolved, error=str(exc)), cost

            remaining = await _get_conflicted_files(cwd=cwd)
            if not remaining:
                break
            conflicted = remaining

        if remaining:
            log.warning("git.rebase.unresolved", remaining=remaining, commit=commit_idx + 1)
            await run("git", "rebase", "--abort", cwd=cwd)
            return RebaseResult(
                success=False,
                conflicts_resolved=conflicts_resolved,
                error=f"Unresolved conflicts after {max_attempts} attempts: {', '.join(remaining)}",
            ), cost

        conflicts_resolved += len(conflicted)
        env = {**os.environ, "GIT_EDITOR": "true"}
        cont = await run("git", "rebase", "--continue", cwd=cwd, env=env)
        if cont.success:
            return RebaseResult(success=True, conflicts_resolved=conflicts_resolved), cost
    else:
        hit_commit_cap = True

    await run("git", "rebase", "--abort", cwd=cwd)
    if hit_commit_cap:
        error = f"Exceeded max commits cap ({max_commits}, processed {commit_idx + 1} commits) during rebase"
    else:
        error = f"Rebase could not be completed: {(cont.stderr or '')[:200]}".rstrip(": ")
    return RebaseResult(success=False, conflicts_resolved=conflicts_resolved, error=error), cost
