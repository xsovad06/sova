"""Step 9: Create PR. Generate a structured description and open a pull request."""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING

from sqlalchemy.exc import SQLAlchemyError

from sova.adapters.base import TaskState
from sova.config.models import TaskSourceConfig
from sova.core.context import ExecutionContext
from sova.core.steps._pr_body import (
    _ISSUE_BODY_EXCERPT_LIMIT,
    _UNAVAILABLE,
    _jira_ticket_link,
    build_prompt,
    compose_body,
    extract_acceptance_criteria,
    process_llm_result,
    truncate_diff,
)
from sova.core.steps._pr_body import (
    title_from_branch as _title_from_branch,
)
from sova.core.steps.base import BaseStep, GateCheckResult, StepResult
from sova.git import operations as git_ops
from sova.llm.client import invoke
from sova.utils.formatting import truncate
from sova.utils.logging import get_logger
from sova.utils.shell import run

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from sova.adapters.base import TaskAdapter
    from sova.config.models import LdapConfig

log = get_logger(component="step.create_pr")

_MAX_SUGGESTED_REVIEWERS = 2

_CONVENTIONAL_RE = re.compile(
    r"^(feat|fix|refactor|test|docs|chore|ci)"
    r"(?:\([^)]*\))?"
    r":\s*",
)

# Valid conventional-commit scopes, kept in sync with invariants/commit-format.sh
# (VALID_SCOPES) and AGENTS.md. A top-level sova/<dir> maps to its scope name.
_VALID_SCOPES = frozenset(
    {
        "dashboard",
        "commands",
        "personas",
        "invariants",
        "knowledge",
        "cli",
        "docs",
        "scheduler",
        "ipc",
        "adapters",
        "roles",
        "core",
        "config",
        "supervisor",
        "mcp",
        "monitoring",
        "db",
        "awareness",
        "oversight",
        "deps",
    }
)
_DEFAULT_SCOPE = "core"


def _infer_scope(changed_files: list[str] | None) -> str:
    """Infer a conventional-commit scope from the PR's changed paths.

    Maps each top-level ``sova/<dir>/`` path to ``<dir>`` when it names a valid
    scope and returns the most frequently touched one. Falls back to
    ``_DEFAULT_SCOPE`` ("core") for mixed, non-``sova``, or empty changes, so the
    title always carries a scope that ``invariants/commit-format.sh`` accepts.
    A single path outside ``sova/<valid-scope>/`` makes the whole change
    "mixed" and falls back too, even when other paths are validly scoped:
    a PR touching both ``sova/dashboard/app.py`` and ``README.md`` is not a
    pure dashboard change. Ties resolve to the first scope seen, which keeps
    the result deterministic.
    """
    if not changed_files:
        return _DEFAULT_SCOPE

    counts: dict[str, int] = {}
    order: list[str] = []
    for path in changed_files:
        parts = path.strip().split("/")
        if len(parts) < 3 or parts[0] != "sova" or parts[1] not in _VALID_SCOPES:
            return _DEFAULT_SCOPE
        scope = parts[1]
        if scope not in counts:
            order.append(scope)
        counts[scope] = counts.get(scope, 0) + 1

    if not counts:
        return _DEFAULT_SCOPE
    # Most-touched scope wins; ties break to the first one seen (order index).
    return max(order, key=lambda s: (counts[s], -order.index(s)))


def _build_pr_title(
    task_title: str,
    issue_number: str | None,
    task_source: TaskSourceConfig | None = None,
    changed_files: list[str] | None = None,
) -> str:
    """Build a PR title from the task title, avoiding double conventional prefixes.

    For JIRA projects, prepends ``[PROJECT_KEY-NUMBER]`` so JIRA's GitHub
    integration auto-links the PR to the ticket.

    For GitHub projects, the scope is inferred from *changed_files* (see
    :func:`_infer_scope`) so the title is a valid ``type(scope): ...`` that
    ``invariants/commit-format.sh`` accepts. The issue number is deliberately
    not in the title: it is linked via ``Closes #N`` in the body, and a
    ``feat(#N): ...`` title parses to an invalid scope that trips the
    ``/integrate-pr`` silent-revert gate.
    """
    match = _CONVENTIONAL_RE.match(task_title)
    if match:
        commit_type = match.group(1)
        description = task_title[match.end() :]
    else:
        commit_type = "feat"
        description = task_title

    if task_source and task_source.is_jira and issue_number:
        jira_key = f"[{task_source.jira_issue_key(issue_number)}]"
        return f"{jira_key} {commit_type}: {description}"

    scope = _infer_scope(changed_files)
    return f"{commit_type}({scope}): {description}"


async def suggest_ldap_reviewers(
    ldap_config: LdapConfig,
    adapter: TaskAdapter,
    *,
    github_user: str,
    issue_number: str | None,
    pr_number: int,
) -> None:
    """Suggest teammates of the PR author as reviewers via LDAP org data.

    Shared between ``CreatePRStep`` (direct PR creation) and the CodeRabbit
    throttle queue processor (``sova.supervisor.pr_throttle``), so queued PRs
    get the same reviewer suggestion as directly-created ones.

    ``github_user`` is not necessarily the author's LDAP uid: the two
    identifiers are frequently different (e.g. GitHub username vs. Kerberos
    id). ``ldap_config.uid_mapping`` maps one to the other explicitly; when a
    user has no mapping entry, ``github_user`` is used as the LDAP uid
    directly (a best-effort assumption, not a guarantee), and a lookup miss
    is logged distinguishably from a genuine directory miss so the identity
    mismatch is diagnosable.

    Best-effort: any missing precondition (LDAP unavailable, no VPN, author
    not found, no manager/teammates) is a silent no-op so PR creation is
    never blocked on a directory lookup.
    """
    from sova.adapters.ldap_client import create_ldap_client

    client = create_ldap_client(ldap_config)
    if client is None:
        return

    if not github_user:
        return

    identity_mapped = github_user in ldap_config.uid_mapping
    author_uid = ldap_config.uid_mapping.get(github_user, github_user)

    if not await client.check_connectivity():
        log.warning("ldap_reviewers.vpn_unavailable", pr=pr_number)
        return

    try:
        manager_chain = await client.find_manager_chain(author_uid, max_depth=1)
        if not manager_chain:
            if not identity_mapped:
                log.info(
                    "ldap_reviewers.identity_unmapped",
                    pr=pr_number,
                    github_user=github_user,
                    reason="no ldap.uid_mapping entry; assumed github_user equals LDAP uid",
                )
            return
        teammates = await client.get_org_chart(manager_chain[0].uid, depth=1)
    except Exception:  # noqa: BLE001 (LDAP lookup is best-effort; PR creation must never block on it)
        log.warning("ldap_reviewers.lookup_failed", pr=pr_number, exc_info=True)
        return

    candidates = [p for p in teammates if p.uid and p.uid != author_uid][:_MAX_SUGGESTED_REVIEWERS]
    for person in candidates:
        try:
            await adapter.add_reviewer(issue_number or "", pr_number, person.uid)
        except Exception:  # noqa: BLE001 (adapter-specific reviewer API failures vary; non-fatal)
            log.warning("ldap_reviewers.add_reviewer_failed", pr=pr_number, user=person.uid, exc_info=True)


class CreatePRStep(BaseStep):
    name = "create_pr"

    async def execute(self, ctx: ExecutionContext) -> StepResult:
        log.info("step.create_pr", label=ctx.display_label, branch=ctx.branch_name)

        # Adopted PR skips throttle (edge case 5)
        adopted = await self._try_adopt_existing_pr(ctx)
        if adopted:
            return adopted

        task_title = await self._resolve_task_title(ctx)
        changed_files = await self._resolve_changed_files(ctx)
        title = _build_pr_title(
            task_title,
            ctx.issue_number if ctx.has_issue else None,
            task_source=ctx.config.task_source,
            changed_files=changed_files,
        )
        body = await self._generate_pr_body(ctx, task_title)

        if ctx.config.coderabbit_quota.enabled:
            return await self._create_pr_throttled(ctx, title, body)

        return await self._create_pr_immediate(ctx, title, body)

    @staticmethod
    async def _resolve_task_title(ctx: ExecutionContext) -> str:
        """Resolve a meaningful task title, falling back to the adapter when ctx.task is missing."""
        if ctx.task:
            return ctx.task.title
        if ctx.has_issue:
            try:
                task = await ctx.adapter.get_task(ctx.issue_number)
                ctx.task = task
                return task.title
            except Exception:  # noqa: BLE001 (adapter raises AdapterError/ValueError too; stay fail-open)
                log.warning("step.create_pr.title_fallback_failed", issue=ctx.issue_number, exc_info=True)
        return _title_from_branch(ctx.branch_name)

    @staticmethod
    async def _resolve_changed_files(ctx: ExecutionContext) -> list[str]:
        """Return the paths changed by this branch since it diverged from its base, for scope inference.

        Uses the merge-base (three-dot) diff rather than a direct comparison
        against the base branch's current tip, so a base branch that has
        advanced since this branch diverged does not pull base-only changes
        into the PR's own changed-path list. Diffs against the remote-tracking
        ref (``origin/{base}``), not the local one, matching ``_generate_pr_body``
        below and ``ConfidenceScoreStep``: a stale local base (this worktree's
        branch was created before a later push to the base branch) would
        otherwise mix the base's own commits into the scope inference input.

        Only the GitHub title path infers a scope; JIRA titles use a bracketed
        ticket key instead, so this skips the git call there. Fails open to an
        empty list (``_build_pr_title`` then falls back to the default scope)
        rather than letting a git failure abort PR creation.
        """
        if ctx.config.task_source.is_jira:
            return []
        try:
            result = await run("git", "diff", f"origin/{ctx.base_branch}...HEAD", "--name-only", cwd=ctx.working_dir)
        except OSError:
            log.warning("step.create_pr.changed_files_failed", branch=ctx.branch_name, exc_info=True)
            return []
        if not result.success:
            log.warning("step.create_pr.changed_files_failed", branch=ctx.branch_name)
            return []
        return [line for line in result.stdout.splitlines() if line.strip()]

    async def _create_pr_immediate(self, ctx: ExecutionContext, title: str, body: str) -> StepResult:
        """Create PR directly (no throttle)."""
        try:
            pr_info = await git_ops.create_pr(
                title=title,
                body=body,
                base=ctx.base_branch,
                head=ctx.branch_name,
                repo=ctx.repo,
            )
            ctx.pr_number = pr_info.number
            ctx.pr_url = pr_info.url
            await self._post_create_side_effects(ctx, pr_info.number)
            return StepResult(success=True, summary=f"Created PR #{pr_info.number}")
        except RuntimeError as exc:
            adopted = await self._try_adopt_from_error(ctx, str(exc))
            if adopted:
                return adopted
            return StepResult(success=False, summary="Failed to create PR", error=str(exc))

    async def _create_pr_throttled(self, ctx: ExecutionContext, title: str, body: str) -> StepResult:
        """Enqueue PR creation and poll until the background processor creates it."""
        from sova.db.session import get_session
        from sova.supervisor.pr_throttle import enqueue, poll_until_created

        if ctx.task_run_id is None:
            log.warning("step.create_pr.no_task_run_id_for_throttle")
            return await self._create_pr_immediate(ctx, title, body)

        try:
            async with await get_session(project_dir=ctx.project_dir) as session:
                async with session.begin():
                    entry_id = await enqueue(
                        session,
                        task_run_id=ctx.task_run_id,
                        issue_number=ctx.issue_number if ctx.has_issue else None,
                        title=title,
                        body=body,
                        base_branch=ctx.base_branch,
                        head_branch=ctx.branch_name,
                        repo=ctx.repo,
                        github_user=ctx.config.github_user,
                        project_slug=ctx.config.github_repo,
                    )
        except (OSError, RuntimeError, SQLAlchemyError) as exc:
            log.warning("step.create_pr.enqueue_failed", error=str(exc), exc_info=True)
            return await self._create_pr_immediate(ctx, title, body)

        log.info("step.create_pr.queued", entry_id=entry_id, label=ctx.display_label)

        # Session factory for polling
        async def _session_factory() -> AsyncSession:
            return await get_session(project_dir=ctx.project_dir)

        result = await poll_until_created(_session_factory, entry_id)
        if result is None:
            return StepResult(
                success=False,
                summary="PR creation timed out in queue",
                error=f"Timed out waiting for queue entry {entry_id} to be processed",
            )

        from sova.db.models import PRQueueStatus

        if result["status"] == PRQueueStatus.CREATED and result["pr_number"]:
            ctx.pr_number = result["pr_number"]
            ctx.pr_url = result.get("pr_url", "")
            return StepResult(success=True, summary=f"Created PR #{result['pr_number']} (throttled)")

        error_message = result.get("error_message")
        error = error_message if error_message is not None else f"Unknown error (queue entry {entry_id})"
        return StepResult(success=False, summary=f"PR creation failed in queue: {error}", error=error)

    async def _try_adopt_existing_pr(self, ctx: ExecutionContext) -> StepResult | None:
        if not ctx.has_issue:
            return None
        existing = await git_ops.find_pr_for_issue(
            ctx.issue_number,
            repo=ctx.repo,
            github_user=ctx.config.github_user,
        )
        if not existing:
            return None
        log.info("step.create_pr.existing_found", pr=existing.number)
        ctx.pr_number = existing.number
        ctx.pr_url = existing.url
        try:
            await ctx.adapter.transition_state(ctx.issue_number, TaskState.IN_REVIEW)
        except Exception:  # noqa: BLE001 (adapter raises AdapterError/ValueError too; stay fail-open)
            log.warning("step.create_pr.tracker_update_failed", exc_info=True)
        return StepResult(success=True, summary=f"Adopted existing PR #{existing.number}")

    async def _try_adopt_from_error(self, ctx: ExecutionContext, error_msg: str) -> StepResult | None:
        """Parse 'already exists' error from gh CLI and adopt the existing PR."""
        if "already exists" not in error_msg:
            return None
        match = re.search(r"/pull/(\d+)", error_msg)
        if not match:
            return None
        pr_number = int(match.group(1))
        log.info("step.create_pr.adopted_from_error", pr=pr_number)
        ctx.pr_number = pr_number
        url_match = re.search(r"(https://github\.com/\S+/pull/\d+)", error_msg)
        ctx.pr_url = url_match.group(1) if url_match else ""
        await self._post_create_side_effects(ctx, pr_number)
        return StepResult(success=True, summary=f"Adopted existing PR #{pr_number}")

    async def _post_create_side_effects(self, ctx: ExecutionContext, pr_number: int) -> None:
        if ctx.config.github_user:
            try:
                await git_ops.assign_pr(
                    pr_number,
                    assignee=ctx.config.github_user,
                    repo=ctx.repo,
                    github_user=ctx.config.github_user,
                )
            except (RuntimeError, OSError):
                log.warning("step.create_pr.assign_failed", exc_info=True)
        if ctx.has_issue:
            try:
                await ctx.adapter.transition_state(ctx.issue_number, TaskState.IN_REVIEW)
            except Exception:  # noqa: BLE001 (adapter raises AdapterError/ValueError too; stay fail-open)
                log.warning("step.create_pr.tracker_update_failed", exc_info=True)
        await self._trigger_coderabbit_review(ctx, pr_number)
        if ctx.config.ldap.enabled:
            await self._suggest_reviewers(ctx, pr_number)

    async def _suggest_reviewers(self, ctx: ExecutionContext, pr_number: int) -> None:
        """Thin wrapper around the shared ``suggest_ldap_reviewers()`` helper."""
        await suggest_ldap_reviewers(
            ctx.config.ldap,
            ctx.adapter,
            github_user=ctx.config.github_user,
            issue_number=ctx.issue_number if ctx.has_issue else None,
            pr_number=pr_number,
        )

    @staticmethod
    async def _trigger_coderabbit_review(ctx: ExecutionContext, pr_number: int) -> None:
        if not ctx.config.external_reviews.coderabbit.trigger_review:
            return
        log.info("step.create_pr.trigger_coderabbit", pr=pr_number)
        try:
            await ctx.adapter.post_pr_comment(pr_number, "@coderabbitai review")
        except Exception:  # noqa: BLE001 (adapter raises AdapterError/ValueError too; stay fail-open)
            log.warning("step.create_pr.trigger_coderabbit_failed", pr=pr_number, exc_info=True)

    async def _generate_pr_body(self, ctx: ExecutionContext, task_title: str) -> str:
        # Two deliberately different ranges: the three-dot form is the merge-base
        # diff (what this branch changed), but for "git log" it is the symmetric
        # difference, which would also list the base branch's own new commits.
        # Both use the remote-tracking ref, not the local one, matching
        # RefreshPrBodyStep and ConfidenceScoreStep: a stale local base (this
        # worktree created before a later push to the base branch) would
        # otherwise pollute both the body and the acceptance-criteria diff
        # with the base's own commits since this branch's actual fork point.
        diff_range = f"origin/{ctx.base_branch}...HEAD"
        log_range = f"origin/{ctx.base_branch}..HEAD"
        log_result, diff_stat_result, diff_content_result = await asyncio.gather(
            run("git", "log", log_range, "--format=%h %s%n%b", "--no-merges", cwd=ctx.working_dir),
            run("git", "diff", diff_range, "--stat", cwd=ctx.working_dir),
            run("git", "diff", diff_range, cwd=ctx.working_dir),
        )

        issue_body = ctx.task.body if ctx.task else ""
        commit_log = log_result.stdout.strip() if log_result.success else _UNAVAILABLE
        diff_stat = diff_stat_result.stdout.strip() if diff_stat_result.success else _UNAVAILABLE
        full_diff = diff_content_result.stdout.strip() if diff_content_result.success else _UNAVAILABLE
        diff_content = truncate_diff(full_diff, diff_content_result.success)

        prompt, criteria = build_prompt(
            ctx,
            task_title=task_title,
            issue_body=issue_body,
            commit_log=commit_log,
            diff_stat=diff_stat,
            diff_content=diff_content,
        )

        model = ctx.resolved_model or ctx.config.agent.model
        try:
            result = await invoke(prompt, model=model, task_type="pr_body", cwd=ctx.working_dir, timeout=120)
        except Exception:  # noqa: BLE001 (guard_prompt raises PromptInjectionError; fall back to the structured body)
            log.warning("step.create_pr.body_generation_failed", fallback="structured", exc_info=True)
            return self._build_fallback_body(ctx, task_title, commit_log, diff_stat)

        ctx.add_usage(result)
        sections_markdown, verdicts = process_llm_result(result.text, criteria, ctx.config.commit.owner_only_patterns)
        diff_ok = log_result.success and diff_stat_result.success and diff_content_result.success
        if verdicts and not diff_ok:
            # The model still answered "met" for every criterion checkable
            # only against the diff it never saw (git evidence unavailable):
            # crediting that guess would violate the fail-toward-"Part of"
            # rule the rest of this PR follows. A genuine "unmet"/"owner"
            # verdict can still come from the issue text alone, so only
            # "met" is demoted.
            verdicts = [(text, "unknown" if verdict == "met" else verdict) for text, verdict in verdicts]

        if not ctx.has_issue:
            return sections_markdown

        ts = ctx.config.task_source
        jira_link = _jira_ticket_link(ts, ctx.issue_number) if ts.is_jira else ""
        return compose_body(sections_markdown, ctx.issue_number, verdicts, is_jira=ts.is_jira, jira_link=jira_link)

    @staticmethod
    def _build_fallback_body(ctx: ExecutionContext, task_title: str, commit_log: str, diff_stat: str) -> str:
        """Build a structured PR body without an LLM, for when body generation fails.

        The closing decision is computed deterministically from the issue's
        acceptance criteria (via :func:`compose_body`), the same fail-toward-
        "Part of #N" rule every other generation path follows: a criterion
        that cannot be verified here (no LLM ran) must never be credited as
        met, so any criteria present yield ``Part of #N`` rather than an
        unconditional ``Closes #N``.
        """
        ts = ctx.config.task_source
        lines = [
            "## Summary",
            "",
            f"Automated changes for: {task_title}",
            "",
        ]

        issue_body = (ctx.task.body if ctx.task else "") or ""
        stripped_body = issue_body.strip()
        if stripped_body:
            excerpt = truncate(stripped_body, max_length=_ISSUE_BODY_EXCERPT_LIMIT)
            lines.extend(["## Context", "", excerpt, ""])

        lines.extend(
            [
                "## Commits",
                "",
                "```",
                commit_log or _UNAVAILABLE,
                "```",
                "",
                "## Files changed",
                "",
                "```",
                diff_stat or _UNAVAILABLE,
                "```",
            ]
        )
        body = "\n".join(lines)
        if not ctx.has_issue:
            return body

        if ts.is_jira:
            jira_link = _jira_ticket_link(ts, ctx.issue_number)
            return compose_body(body, ctx.issue_number, None, is_jira=True, jira_link=jira_link)

        criteria = extract_acceptance_criteria(issue_body)
        # "unknown", not "unmet": no LLM ran in this path, so no criterion
        # was actually checked. "unmet" would claim the criteria were
        # verified and found incomplete, which render_closing_block()
        # reserves for a real model-reported verdict.
        verdicts = [(c, "unknown") for c in criteria] if criteria else None
        return compose_body(body, ctx.issue_number, verdicts, is_jira=False)

    async def validate_output(self, ctx: ExecutionContext) -> GateCheckResult:
        """Gate: PR number must have been extracted."""
        if ctx.pr_number is not None:
            return GateCheckResult(passed=True)
        return GateCheckResult(passed=False, reason="No PR number after create_pr step")

    async def can_skip(self, ctx: ExecutionContext) -> bool:
        # Skip if PR already exists (resumed run)
        return ctx.pr_number is not None
