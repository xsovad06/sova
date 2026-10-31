"""Step 9: Create PR. Generate a structured description and open a pull request."""

from __future__ import annotations

import asyncio
import math
import re
from typing import TYPE_CHECKING

from sqlalchemy.exc import SQLAlchemyError

from sova.adapters.base import TaskState
from sova.config.models import DEFAULT_COMMIT_SCOPES, DEFAULT_COMMIT_TYPES, TaskSourceConfig
from sova.core.context import ExecutionContext
from sova.core.steps._pr_body import (
    _ISSUE_BODY_EXCERPT_LIMIT,
    _UNAVAILABLE,
    _jira_ticket_link,
    build_prompt,
    compose_body,
    extract_acceptance_criteria,
    process_llm_result,
    split_title_tail,
    truncate_diff,
)
from sova.core.steps._pr_body import (
    title_from_branch as _title_from_branch,
)
from sova.core.steps.base import BaseStep, GateCheckResult, StepResult
from sova.core.steps.commit_convention import (
    MAX_LINE_LENGTH,
    format_type_scope,
    infer_scope_from_paths,
    infer_type_from_paths,
    is_valid_commit_line,
    lowercase_first_letter,
    match_type_prefix,
    truncate_commit_line,
)
from sova.git import operations as git_ops
from sova.llm.client import invoke
from sova.utils.formatting import truncate
from sova.utils.logging import get_logger
from sova.utils.shell import run

if TYPE_CHECKING:
    from collections.abc import Collection

    from sqlalchemy.ext.asyncio import AsyncSession

    from sova.adapters.base import TaskAdapter
    from sova.config.models import LdapConfig

log = get_logger(component="step.create_pr")

_MAX_SUGGESTED_REVIEWERS = 2

_DEFAULT_SCOPE = "core"

# Longest the step waits for the throttle loop to create its PR, when no step deadline bounds it.
_QUEUE_WAIT_CAP_SECONDS = 3600
# Kept back from the step deadline so the queue wait ends with time to cancel its entry and report
# the failure, instead of the engine's hard timeout killing the step with the entry still pending.
_QUEUE_WAIT_MARGIN_SECONDS = 60
# Bounds each cleanup DB call (cancel, quota read) so a stuck database cannot hold the step open.
_QUEUE_CLEANUP_TIMEOUT_SECONDS = 10


def _queue_wait_seconds(ctx: ExecutionContext) -> float:
    """How long the step may wait on the queue: its remaining time less a margin, capped.

    Follows ``DevelopStep``'s convention: a deadline that is the run-wide runaway guard is left to the
    engine's own timeout, which routes the failure through the resumable PAUSED path.
    """
    remaining = ctx.step_time_remaining
    if remaining is None or ctx.step_deadline_is_runaway:
        return _QUEUE_WAIT_CAP_SECONDS
    return max(0.0, min(_QUEUE_WAIT_CAP_SECONDS, remaining - _QUEUE_WAIT_MARGIN_SECONDS))


def _infer_scope(changed_files: list[str] | None, scopes: Collection[str] | None = None) -> str:
    """Infer a conventional-commit scope from the PR's changed paths.

    Thin wrapper around :func:`~sova.core.steps.commit_convention.infer_scope_from_paths`
    (shared with ``CommitStep``) defaulting *scopes* to ``DEFAULT_COMMIT_SCOPES``
    (SOVA's own convention) when omitted, kept here so existing call sites and
    tests can keep importing ``_infer_scope`` from this module.
    """
    if scopes is None:
        scopes = DEFAULT_COMMIT_SCOPES
    return infer_scope_from_paths(changed_files, scopes, preferred=_DEFAULT_SCOPE)


def _build_pr_title(
    task_title: str,
    issue_number: str | None,
    task_source: TaskSourceConfig | None = None,
    changed_files: list[str] | None = None,
    scopes: Collection[str] | None = None,
    types: Collection[str] | None = None,
) -> str:
    """Build a PR title from the task title, avoiding double conventional prefixes.

    For JIRA projects, prepends ``[PROJECT_KEY-NUMBER]`` so JIRA's GitHub
    integration auto-links the PR to the ticket.

    For GitHub projects, the scope is inferred from *changed_files* against
    *scopes* (see :func:`_infer_scope`; defaults to SOVA's own convention
    when *scopes* is omitted) so the title is a valid ``type(scope): ...``
    that ``invariants/commit-format.sh`` accepts. The issue number is
    deliberately not in the title: it is linked via ``Closes #N`` in the
    body, and a ``feat(#N): ...`` title parses to an invalid scope that
    trips the ``/integrate-pr`` silent-revert gate. When *task_title* carries
    no existing type prefix, the type is likewise inferred from
    *changed_files* (a docs-only or tests-only change gets "docs"/"test"
    when the project configures them) before falling back to the project's
    default type.

    The result is lowercased after the colon and trimmed to 72 characters,
    the same rules :func:`is_valid_commit_line` applies to the LLM-proposed
    title this builder is the fallback for, so a verbatim issue title well
    over the limit cannot reach the PR just because the LLM call failed.
    """
    resolved_types = types or DEFAULT_COMMIT_TYPES
    match = match_type_prefix(task_title, resolved_types)
    if match:
        commit_type = match.group("type")
        description = task_title[match.end() :]
    else:
        commit_type = infer_type_from_paths(changed_files, resolved_types)
        description = task_title
    description = lowercase_first_letter(description)

    if task_source and task_source.is_jira and issue_number:
        jira_key = f"[{task_source.jira_issue_key(issue_number)}]"
        return truncate_commit_line(f"{jira_key} {commit_type}: {description}")

    scope = _infer_scope(changed_files, scopes)
    return truncate_commit_line(f"{format_type_scope(commit_type, scope)}: {description}")


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
        ts = ctx.config.task_source
        is_freeform = ctx.config.commit.pr_title_format == "freeform" and not (ts and ts.is_jira)
        if is_freeform:
            # A JIRA title always uses the bracketed "[KEY-N]" format
            # regardless of pr_title_format (is_freeform is False for one),
            # so this is reached only for a GitHub project that opted out of
            # the conventional-commit title: no type(scope) prefix, no LLM
            # candidate requested or validated against commit.types/.scopes.
            fallback_title = truncate(task_title.strip() or "update", max_length=MAX_LINE_LENGTH)
        else:
            fallback_title = _build_pr_title(
                task_title,
                ctx.issue_number if ctx.has_issue else None,
                task_source=ts,
                changed_files=changed_files,
                scopes=ctx.config.commit.scopes,
                types=ctx.config.commit.types,
            )
        body, title = await self._generate_pr_body(ctx, task_title, fallback_title, request_title=not is_freeform)

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
        """Enqueue PR creation and wait, within the step deadline, for the background processor to create it."""
        from sova.db.models import PRQueueStatus
        from sova.db.session import get_session
        from sova.supervisor.pr_throttle import QueueEntryMissingError, enqueue, poll_until_created, throttle_loop_alive

        if ctx.task_run_id is None:
            log.warning("step.create_pr.no_task_run_id_for_throttle")
            return await self._create_pr_immediate(ctx, title, body)

        # The loop lives in the daemon, a different process: only its heartbeat file says whether anyone
        # will drain the queue. Without one, an enqueued entry would sit pending until the step times out.
        if not throttle_loop_alive(ctx.project_dir):
            log.warning(
                "step.create_pr.no_throttle_loop",
                project_dir=str(ctx.project_dir),
                action="creating the PR directly, bypassing the CodeRabbit quota",
            )
            return await self._create_pr_immediate(ctx, title, body)

        # Sized before the enqueue: a step with less than the margin left cannot wait for the loop at all, so
        # it must not write an entry only to cancel it at once and report a timeout that never had a chance.
        wait_seconds = _queue_wait_seconds(ctx)
        if wait_seconds <= 0:
            return StepResult(
                success=False,
                summary="No time left to queue the PR",
                error=(
                    f"The step deadline left no time to queue the PR (less than {_QUEUE_WAIT_MARGIN_SECONDS}s "
                    "remained), so it was not queued"
                ),
            )

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

        result = None
        try:
            result = await poll_until_created(_session_factory, entry_id, timeout_seconds=wait_seconds)
        except QueueEntryMissingError:
            # Not a timeout: the row was deleted under the wait, so nothing is left to cancel and whether the
            # loop created a PR first cannot be read back.
            log.warning("step.create_pr.queue_entry_missing", entry_id=entry_id)
            return StepResult(
                success=False,
                summary="PR queue entry disappeared",
                error=(
                    f"Queue entry {entry_id} disappeared before it was processed, "
                    "so it is unknown whether a PR was created"
                ),
            )
        finally:
            # Every way out of the wait (done, timed out, step deadline, cancellation) leaves no pending entry.
            final = await self._cancel_queue_entry(ctx, entry_id)

        if result is None:
            # Timed out, so the cancel above was the last write: `final` is the entry's closing state.
            # The loop may have created the PR in the moment before the cancel landed.
            if final is not None and final["status"] == PRQueueStatus.CREATED and final["pr_number"]:
                result = final
            else:
                return await self._queue_timeout_result(ctx, entry_id, wait_seconds, final)

        if result["status"] == PRQueueStatus.CREATED and result["pr_number"]:
            ctx.pr_number = result["pr_number"]
            ctx.pr_url = result.get("pr_url", "")
            return StepResult(success=True, summary=f"Created PR #{result['pr_number']} (throttled)")

        error_message = result.get("error_message")
        error = error_message if error_message is not None else f"Unknown error (queue entry {entry_id})"
        return StepResult(success=False, summary=f"PR creation failed in queue: {error}", error=error)

    @staticmethod
    async def _cancel_queue_entry(ctx: ExecutionContext, entry_id: int) -> dict | None:
        """Cancel this run's pending queue entries and return the entry's final status, or None.

        Never raises an ordinary exception: it runs on the way out of the wait, including while a cancellation
        is propagating, and a cleanup failure must not replace the real outcome (a ``CancelledError`` still
        passes through, so cancellation is never swallowed). The work is shielded so that cancellation
        cannot abandon the transaction half-written, and bounded so a stuck database cannot hold the step
        past its deadline. If this fails, the run-finalization path cancels the entry as a backstop.

        An entry already ``creating`` is not touched: its ``gh pr create`` cannot be recalled, and the
        loop's own ``created`` write stays the authoritative one.
        """
        from sova.db.session import get_session
        from sova.supervisor.pr_throttle import dequeue, get_queue_entry_status

        async def _cancel_and_read() -> dict | None:
            async with await get_session(project_dir=ctx.project_dir) as session:
                async with session.begin():
                    await dequeue(
                        session,
                        task_run_id=ctx.task_run_id,
                        reason="Cancelled: the create_pr step ended before the entry was processed",
                    )
                return await get_queue_entry_status(session, entry_id)

        try:
            return await asyncio.wait_for(asyncio.shield(_cancel_and_read()), timeout=_QUEUE_CLEANUP_TIMEOUT_SECONDS)
        except Exception:  # noqa: BLE001 (cleanup must not replace the step outcome, whatever the driver raises)
            log.warning("step.create_pr.queue_cancel_failed", entry_id=entry_id, exc_info=True)
            return None

    async def _queue_timeout_result(
        self, ctx: ExecutionContext, entry_id: int, wait_seconds: float, final: dict | None
    ) -> StepResult:
        """Build the explicit failure for a queue wait that ran out, naming the entry and the next quota slot."""
        from sova.db.models import PRQueueStatus

        if final is not None and final["status"] == PRQueueStatus.CREATING:
            log.warning("step.create_pr.queue_entry_in_flight", entry_id=entry_id)
            detail = "its PR creation was already in flight and could not be cancelled, so the PR may still appear"
        else:
            detail = await self._next_slot_hint(ctx)
        error = f"Timed out waiting for queue entry {entry_id} to be processed after {int(wait_seconds)}s: {detail}"
        return StepResult(success=False, summary="PR creation timed out in queue", error=error)

    @staticmethod
    async def _next_slot_hint(ctx: ExecutionContext) -> str:
        """Describe when the CodeRabbit quota next has a free slot, for the queue timeout error."""
        from sova.db.session import get_session
        from sova.supervisor.coderabbit_quota import get_quota_status

        try:
            async with (
                asyncio.timeout(_QUEUE_CLEANUP_TIMEOUT_SECONDS),
                await get_session(project_dir=ctx.project_dir) as session,
            ):
                quota = await get_quota_status(
                    session, ctx.config.coderabbit_quota, project_slug=ctx.config.github_repo
                )
        except Exception:  # noqa: BLE001 (a hint must not replace the queue timeout error, whatever the lookup raises)
            log.warning("step.create_pr.quota_status_failed", exc_info=True)
            return "the next free CodeRabbit quota slot could not be determined"
        if quota.can_create_pr:
            return "a CodeRabbit quota slot was free, so the throttle loop is not draining the queue"
        if quota.next_available_minutes is None:
            return "no CodeRabbit quota slot is free"
        return f"the next free CodeRabbit quota slot opens in about {math.ceil(quota.next_available_minutes)} minutes"

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

    async def _generate_pr_body(
        self, ctx: ExecutionContext, task_title: str, fallback_title: str, *, request_title: bool = True
    ) -> tuple[str, str]:
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
            request_title=request_title,
        )

        model = ctx.resolved_model or ctx.config.agent.model
        try:
            result = await invoke(prompt, model=model, task_type="pr_body", cwd=ctx.working_dir, timeout=120)
        except Exception:  # noqa: BLE001 (guard_prompt raises PromptInjectionError; fall back to the structured body)
            log.warning("step.create_pr.body_generation_failed", fallback="structured", exc_info=True)
            return self._build_fallback_body(ctx, task_title, commit_log, diff_stat), fallback_title

        ctx.add_usage(result)
        body_text, title_candidate = split_title_tail(result.text)
        title = fallback_title
        if (
            request_title
            and title_candidate
            and is_valid_commit_line(title_candidate, types=ctx.config.commit.types, scopes=ctx.config.commit.scopes)
        ):
            title = title_candidate
        sections_markdown, verdicts = process_llm_result(body_text, criteria, ctx.config.commit.owner_only_patterns)
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
            return sections_markdown, title

        ts = ctx.config.task_source
        jira_link = _jira_ticket_link(ts, ctx.issue_number) if ts.is_jira else ""
        body = compose_body(sections_markdown, ctx.issue_number, verdicts, is_jira=ts.is_jira, jira_link=jira_link)
        return body, title

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
