"""Rebase with LLM-assisted conflict resolution."""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

from sova.llm.client import invoke_command
from sova.llm.models import LLMResult
from sova.utils.logging import get_logger
from sova.utils.shell import run

if TYPE_CHECKING:
    from sova.llm.provider import LLMProvider

log = get_logger(component="git.rebase")

_CONFLICT_MARKER_RE = re.compile(r"^(?:[<>|]{7}(?: .*)?|={7})$", re.MULTILINE)

_DEFAULT_RESOLUTION_PROMPT = (
    "The following file has git merge conflicts. The file uses diff3 conflict style "
    "with three sections: ours (<<<<<<< HEAD), base (||||||| merged common ancestors), "
    "and theirs (>>>>>>> branch).\n\n"
    "Resolve all conflicts by producing the COMPLETE file content with no conflict markers. "
    "Return ONLY the file content, no explanations, no markdown fences.\n\n"
    "File: {filename}\n\n"
    "```\n{content}\n```"
)


@dataclass
class RebaseResult:
    """Outcome of a rebase-with-conflict-resolution attempt."""

    success: bool
    conflicts_resolved: int = 0
    error: str = ""


def _normalize_resolution(text: str) -> str:
    """Normalize text for consensus comparison."""
    text = text.replace("\r\n", "\n")
    lines = text.split("\n")
    lines = [line.rstrip() for line in lines]
    return "\n".join(lines).rstrip("\n")


def _has_conflict_markers(text: str) -> bool:
    return bool(_CONFLICT_MARKER_RE.search(text))


def _is_valid_resolution(text: str, *, original_non_marker_lines: int) -> bool:
    if _has_conflict_markers(text):
        return False
    if original_non_marker_lines > 0:
        result_lines = len(text.strip().splitlines()) if text.strip() else 0
        if result_lines < original_non_marker_lines * 0.5:
            return False
    return True


def _count_non_marker_lines(content: str) -> int:
    return sum(1 for line in content.splitlines() if not _CONFLICT_MARKER_RE.match(line))


def _select_consensus(resolutions: list[str], *, threshold: float) -> str | None:
    if len(resolutions) < 2:
        return None
    normalized = [_normalize_resolution(r) for r in resolutions]
    groups: dict[str, list[int]] = {}
    for i, norm in enumerate(normalized):
        if norm not in groups:
            groups[norm] = []
        groups[norm].append(i)

    total = len(normalized)
    for _norm_text, indices in sorted(groups.items(), key=lambda x: -len(x[1])):
        count = len(indices)
        if count >= total * threshold:
            return resolutions[indices[0]]
    return None


def _build_file_resolution_prompt(
    filename: str,
    content: str,
    *,
    template: str | None,
) -> str:
    if template:
        return template.format(filename=filename, content=content)
    return _DEFAULT_RESOLUTION_PROMPT.format(filename=filename, content=content)


def _find_prompt_template(model: str, templates: dict[str, str]) -> str | None:
    for prefix in sorted(templates, key=len, reverse=True):
        if model.startswith(prefix) or f"/{prefix}" in model:
            return templates[prefix]
    return None


async def _configure_diff3(cwd: Path) -> None:
    result = await run("git", "config", "--local", "merge.conflictstyle", "diff3", cwd=cwd)
    if not result.success:
        log.warning("git.rebase.diff3_config_failed", stderr=(result.stderr or "")[:200])


async def _resolve_file_with_consensus(
    filename: str,
    file_content: str,
    *,
    models: list[str],
    providers: dict[str, LLMProvider],
    consensus_threshold: float,
    prompt_templates: dict[str, str],
    max_budget_usd: Decimal | None,
) -> tuple[str | None, Decimal]:
    """Fan out a single file to multiple models and attempt consensus.

    Returns (resolved_text, total_cost). resolved_text is None if no consensus.
    """
    cost = Decimal("0")
    if len(models) < 2:
        return None, cost

    original_non_marker = _count_non_marker_lines(file_content)
    # Advisory only: LiteLLM providers track cost but cannot enforce a cap.
    per_model_budget = max_budget_usd / len(models) if max_budget_usd else None

    async def _invoke_model(model_id: str) -> tuple[str | None, Decimal]:
        provider = providers[model_id]
        try:
            template = _find_prompt_template(model_id, prompt_templates)
            prompt = _build_file_resolution_prompt(filename, file_content, template=template)
            result = await provider.invoke(
                prompt,
                model=model_id,
                max_budget_usd=per_model_budget,
            )
            return result.text, result.cost_usd
        except Exception:
            log.warning("git.rebase.consensus_model_failed", model=model_id, file=filename, exc_info=True)
            return None, Decimal("0")

    tasks = [_invoke_model(m) for m in models]
    results = await asyncio.gather(*tasks)

    valid_resolutions: list[str] = []
    for text, model_cost in results:
        cost += model_cost
        if text is None:
            continue
        if not _is_valid_resolution(text, original_non_marker_lines=original_non_marker):
            log.info("git.rebase.invalid_resolution_filtered", file=filename)
            continue
        valid_resolutions.append(text)

    if not valid_resolutions:
        return None, cost

    winner = _select_consensus(valid_resolutions, threshold=consensus_threshold)
    return winner, cost


async def _get_conflicted_files(cwd: Path | None = None) -> list[str]:
    """Return list of files with merge conflicts (unmerged paths)."""
    result = await run("git", "diff", "--name-only", "--diff-filter=U", cwd=cwd)
    if not result.success or not result.stdout.strip():
        return []
    return [f.strip() for f in result.stdout.strip().splitlines() if f.strip()]


def _load_consensus_config(
    cwd: Path,
) -> tuple[list[str], float, dict[str, str], float | None]:
    """Load conflict resolution config. Returns (models, threshold, templates, timeout)."""
    try:
        from sova.config.loader import load_config

        cfg = load_config(cwd)
        cr = cfg.conflict_resolution
        timeout = float(cfg.llm.cli_timeout) if cfg.llm.cli_timeout else None
        return list(cr.models), cr.consensus_threshold, dict(cr.prompt_templates), timeout
    except Exception:
        log.debug("git.rebase.config_load_failed", exc_info=True)
        from sova.config.models import ConflictResolutionConfig

        return [], ConflictResolutionConfig().consensus_threshold, {}, None


def _create_providers(
    models: list[str],
    timeout: float | None = None,
) -> dict[str, LLMProvider] | None:
    """Create LiteLLM providers for consensus models. Returns None if unavailable."""
    try:
        from sova.llm.litellm_provider import _HAS_LITELLM, LiteLLMProvider

        if not _HAS_LITELLM:
            log.warning(
                "git.rebase.litellm_unavailable",
                msg="Multi-model conflict resolution requires litellm. Falling back to single-model.",
            )
            return None
        providers: dict[str, LLMProvider] = {}
        for model_id in models:
            providers[model_id] = LiteLLMProvider(model=model_id, timeout=timeout)
        return providers
    except Exception:
        log.warning("git.rebase.litellm_provider_init_failed", exc_info=True)
        return None


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


async def _try_consensus_resolution(
    conflicted_files: list[str],
    *,
    cwd: Path,
    models: list[str],
    providers: dict[str, LLMProvider],
    consensus_threshold: float,
    prompt_templates: dict[str, str],
    max_budget_usd: Decimal | None,
) -> tuple[bool, Decimal]:
    """Attempt multi-model consensus resolution for all conflicted files.

    Returns (all_resolved, total_cost). When False is returned, earlier files
    may already be written and staged; the caller falls back to single-model.
    """
    total_cost = Decimal("0")
    per_file_budget = max_budget_usd / len(conflicted_files) if max_budget_usd and conflicted_files else None

    for filepath in conflicted_files:
        abs_path = cwd / filepath
        try:
            file_content = abs_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            log.warning("git.rebase.file_read_failed", file=filepath, exc_info=True)
            return False, total_cost

        resolved, cost = await _resolve_file_with_consensus(
            filepath,
            file_content,
            models=models,
            providers=providers,
            consensus_threshold=consensus_threshold,
            prompt_templates=prompt_templates,
            max_budget_usd=per_file_budget,
        )
        total_cost += cost

        if resolved is None:
            log.info("git.rebase.no_consensus", file=filepath)
            return False, total_cost

        abs_path.write_text(resolved)
        stage = await run("git", "add", filepath, cwd=cwd)
        if not stage.success:
            log.warning("git.rebase.stage_failed", file=filepath, stderr=(stage.stderr or "")[:200])
            return False, total_cost

    return True, total_cost


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

    When multiple models are configured via ``[conflict_resolution]`` in
    sova.toml, files are resolved using multi-model consensus before falling
    back to the single-model interactive path.

    Stashes any uncommitted changes before rebasing and restores them after,
    so the worktree does not need to be clean before calling this function.

    Returns a (RebaseResult, cost_usd) tuple.  On unrecoverable failure the
    rebase is aborted so the worktree is never left in a broken state.
    """
    cost = Decimal("0")

    cr_models, cr_threshold, cr_templates, cr_timeout = _load_consensus_config(cwd)
    use_consensus = len(cr_models) >= 2
    providers: dict[str, LLMProvider] | None = None

    if use_consensus:
        await _configure_diff3(cwd)
        providers = _create_providers(cr_models, timeout=cr_timeout)
        if providers is None:
            use_consensus = False

    fetch = await run("git", "fetch", "origin", base, cwd=cwd)
    if not fetch.success:
        return RebaseResult(success=False, error=f"Fetch failed: {fetch.stderr[:200]}"), cost

    stash_result = await run("git", "stash", "--include-untracked", cwd=cwd)
    stashed = stash_result.success and "No local changes to save" not in stash_result.stdout
    if not stash_result.success:
        log.warning("git.rebase.stash_failed", stderr=(stash_result.stderr or "")[:200])

    async def _pop_stash() -> bool:
        """Restore stashed changes. Returns False if the pop failed."""
        if not stashed:
            return True
        pop = await run("git", "stash", "pop", cwd=cwd)
        if not pop.success:
            log.warning("git.rebase.stash_pop_failed", stderr=(pop.stderr or "")[:200])
            return False
        return True

    result = await run("git", "rebase", f"origin/{base}", cwd=cwd)
    if result.success:
        if not await _pop_stash():
            return RebaseResult(success=False, error="Stash restore failed after clean rebase"), cost
        return RebaseResult(success=True), cost

    initial_error = (result.stderr or result.stdout or "")[:300]
    conflicted = await _get_conflicted_files(cwd=cwd)
    if not conflicted:
        await run("git", "rebase", "--abort", cwd=cwd)
        await _pop_stash()
        return RebaseResult(success=False, error=f"Rebase failed: {initial_error}"), cost

    conflicts_resolved = 0
    hit_commit_cap = False
    for commit_idx in range(max_commits):
        if not conflicted:
            env = {**os.environ, "GIT_EDITOR": "true"}
            cont = await run("git", "rebase", "--continue", cwd=cwd, env=env)
            if cont.success:
                if not await _pop_stash():
                    return RebaseResult(success=False, error="Stash restore failed after rebase"), cost
                return RebaseResult(success=True, conflicts_resolved=conflicts_resolved), cost
            log.warning(
                "git.rebase.continue_failed",
                commit=commit_idx + 1,
                stdout=(cont.stdout or "")[:200],
                stderr=(cont.stderr or "")[:200],
            )
            break

        consensus_resolved = False
        if use_consensus and providers is not None:
            log.info(
                "git.rebase.trying_consensus",
                files=conflicted,
                commit=commit_idx + 1,
                models=cr_models,
            )
            consensus_resolved, consensus_cost = await _try_consensus_resolution(
                conflicted,
                cwd=cwd,
                models=cr_models,
                providers=providers,
                consensus_threshold=cr_threshold,
                prompt_templates=cr_templates,
                max_budget_usd=max_budget_usd,
            )
            cost += consensus_cost

            if consensus_resolved:
                remaining = await _get_conflicted_files(cwd=cwd)
                if not remaining:
                    conflicts_resolved += len(conflicted)
                    env = {**os.environ, "GIT_EDITOR": "true"}
                    cont = await run("git", "rebase", "--continue", cwd=cwd, env=env)
                    if cont.success:
                        if not await _pop_stash():
                            return RebaseResult(success=False, error="Stash restore failed after rebase"), cost
                        return RebaseResult(success=True, conflicts_resolved=conflicts_resolved), cost
                    conflicted = await _get_conflicted_files(cwd=cwd)
                    continue
                consensus_resolved = False

        if not consensus_resolved:
            if use_consensus and providers is not None:
                still_conflicted = await _get_conflicted_files(cwd=cwd)
                if still_conflicted:
                    conflicted = still_conflicted
            remaining_list: list[str] = []
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
                    await _pop_stash()
                    return RebaseResult(success=False, conflicts_resolved=conflicts_resolved, error=str(exc)), cost

                remaining_list = await _get_conflicted_files(cwd=cwd)
                if not remaining_list:
                    break
                conflicted = remaining_list

            if remaining_list:
                log.warning("git.rebase.unresolved", remaining=remaining_list, commit=commit_idx + 1)
                await run("git", "rebase", "--abort", cwd=cwd)
                await _pop_stash()
                return RebaseResult(
                    success=False,
                    conflicts_resolved=conflicts_resolved,
                    error=f"Unresolved conflicts after {max_attempts} attempts: {', '.join(remaining_list)}",
                ), cost

        conflicts_resolved += len(conflicted)
        env = {**os.environ, "GIT_EDITOR": "true"}
        cont = await run("git", "rebase", "--continue", cwd=cwd, env=env)
        if cont.success:
            if not await _pop_stash():
                return RebaseResult(success=False, error="Stash restore failed after rebase"), cost
            return RebaseResult(success=True, conflicts_resolved=conflicts_resolved), cost
        conflicted = await _get_conflicted_files(cwd=cwd)
    else:
        hit_commit_cap = True

    await run("git", "rebase", "--abort", cwd=cwd)
    await _pop_stash()
    if hit_commit_cap:
        error = f"Exceeded max commits cap ({max_commits}, processed {commit_idx + 1} commits) during rebase"
    else:
        error = f"Rebase could not be completed: {(cont.stderr or '')[:200]}".rstrip(": ")
    return RebaseResult(success=False, conflicts_resolved=conflicts_resolved, error=error), cost
