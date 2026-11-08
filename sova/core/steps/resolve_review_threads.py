"""Step: Resolve review threads (reply to and resolve the addressed threads, post the address summary after push).

Runs directly after ``push`` and before CI, and reads no CI state and calls no LLM. A pushed fix is
what answers a finding, not a green build: ``resolve_next_action()`` ranks ``ci_failed`` above every
thread and verdict rule, so resolving early cannot make a red PR look ready, and putting the step
ahead of ``monitor_ci`` means a CI timeout can no longer strand the threads or the cycle record.
"""

from __future__ import annotations

from sova.core.context import ExecutionContext
from sova.core.steps.base import BaseStep, GateCheckResult, StepResult
from sova.utils.gh import resolve_gh_env
from sova.utils.logging import get_logger
from sova.utils.review_markers import SOVA_ADDRESSED_MARKER_RE, format_address_summary
from sova.utils.shell import run

log = get_logger(component="step.resolve_review_threads")


async def _local_head(ctx: ExecutionContext) -> str | None:
    """The worktree's HEAD, or None when it cannot be read.

    A pruned or never-created ``working_dir`` makes the spawn itself raise (FileNotFoundError on the
    cwd), which would abort the step entirely. The commit anchor is cosmetic for the summary, so the
    caller degrades to an unanchored one instead of losing the cycle.
    """
    try:
        head = await run("git", "rev-parse", "HEAD", cwd=ctx.working_dir)
    except OSError:
        log.warning("step.resolve_review_threads.head_lookup_failed", cwd=str(ctx.working_dir), exc_info=True)
        return None
    return (head.stdout.strip() or None) if head.success else None


async def _remote_tip(ctx: ExecutionContext) -> str | None:
    """The tip of the PR branch on origin, or None when it cannot be determined."""
    if not ctx.branch_name:
        return None
    try:
        # The same identity push() authenticates as; a bare ls-remote on a private HTTPS remote can fail or
        # use another account's credentials, which would read as "not moved" and strand every thread.
        github_user = ctx.config.github_user
        env = await resolve_gh_env(github_user) if github_user else None
        result = await run("git", "ls-remote", "origin", f"refs/heads/{ctx.branch_name}", cwd=ctx.working_dir, env=env)
    except OSError:
        log.warning("step.resolve_review_threads.remote_lookup_failed", exc_info=True)
        return None
    if not result.success:
        log.warning("step.resolve_review_threads.remote_lookup_failed", stderr=result.stderr[:200])
        return None
    fields = result.stdout.split()
    return fields[0] if fields else None


async def _content_changed(ctx: ExecutionContext, base: str) -> bool:
    """True when HEAD's tree differs from the cycle base's tree. A failed lookup counts as unchanged.

    ``rearrange_commits`` runs between ``address_review`` and ``push`` and can reword, squash or re-sign
    commits, which gives HEAD a new SHA over the identical tree. Only a different tree shows a fix landed.
    """
    try:
        result = await run("git", "diff", "--quiet", base, "HEAD", cwd=ctx.working_dir)
    except OSError:
        log.warning("step.resolve_review_threads.diff_failed", exc_info=True)
        return False
    # git diff --quiet: 0 = identical, 1 = differs, anything else = the comparison itself failed.
    return result.returncode == 1


async def _head_moved(ctx: ExecutionContext, head_sha: str | None) -> bool:
    """True when this cycle's fixes are on the remote branch: HEAD's content left the cycle's base and origin has it.

    A successful ``push`` is not proof, because pushing an unchanged branch also succeeds. Only a HEAD that
    differs from the one ``address_review`` started from, whose tree differs too (a history rewrite alone
    changes the SHA but no code), and that the remote branch now points at, shows the cycle's fixes were
    published. A rebase before ``address_review`` is already part of the base.
    """
    base = ctx.address_cycle_base_head
    if not head_sha or not base or head_sha == base:
        return False
    if not await _content_changed(ctx, base):
        return False
    return await _remote_tip(ctx) == head_sha


def _already_replied(thread: dict, reply_body: str) -> bool:
    """True when the thread's last comment is this cycle's reply, left by an attempt that did not resolve it."""
    nodes = (thread.get("lastComment") or {}).get("nodes") or []
    last = next(iter(reversed(nodes)), None)
    return last is not None and (last.get("body") or "").strip() == reply_body


async def _reply_and_resolve(ctx: ExecutionContext, head_sha: str) -> tuple[int, int]:
    """Reply to and resolve each addressed thread still open. Returns (replied, resolved).

    A thread a human already resolved is left alone and not counted. A failed reply or resolve is logged
    and the loop moves on, so one bad thread cannot keep the summary from being posted. A thread whose
    reply failed is left open rather than closed with no explanation. A thread whose last comment already is
    this cycle's reply (an earlier attempt replied but did not resolve) is resolved without a second reply.
    """
    from sova.adapters.external_reviews import fetch_review_threads, reply_to_review_thread, resolve_review_thread

    threads = await fetch_review_threads(ctx.repo, ctx.pr_number, github_user=ctx.config.github_user)
    if threads is None:
        log.warning("step.resolve_review_threads.thread_fetch_failed", pr=ctx.pr_number)
        return 0, 0
    open_threads = {t["id"]: t for t in threads if t.get("id") and not t.get("isResolved")}

    reply_body = f"Addressed in `{head_sha[:7]}`."
    replied = resolved = 0
    for thread_id in ctx.addressed_review_thread_ids:
        thread = open_threads.get(thread_id)
        if thread is None:
            log.info("step.resolve_review_threads.thread_not_open", thread_id=thread_id)
            continue
        if _already_replied(thread, reply_body):
            log.info("step.resolve_review_threads.reply_already_posted", thread_id=thread_id)
        elif await reply_to_review_thread(thread_id, reply_body, github_user=ctx.config.github_user):
            replied += 1
        else:
            continue
        if await resolve_review_thread(thread_id, github_user=ctx.config.github_user):
            resolved += 1
    return replied, resolved


async def _post_address_summary(ctx: ExecutionContext, head_sha: str | None, *, head_moved: bool) -> bool:
    """Post the ``## Address Review`` summary as a COMMENT-state review on the PR.

    The autonomous counterpart of ``/address-pr`` step 15. The dashboard reads the marker from the PR's
    review list to tell that the standing SOVA verdict has been addressed, so without this post a
    pipeline-addressed PR keeps showing "SOVA Changes Requested".

    Posted for every cycle, including one whose ``addressed_review_findings`` is empty (the "drained the
    pending documentation queue" outcome) and one whose head did not move (rendered as "No changes were
    pushed", listing no finding as addressed, with no reply and no thread resolved). The marker is what
    ``parse_review_history()`` counts into ``address_cycles``, which bounds the Reviewer -> Developer chain
    via ``pipeline.max_address_review_cycles``: skipping it for a cycle that changed nothing would hide that
    cycle from the budget and let the chain repeat it forever. ``format_address_summary()`` renders the empty
    case with its own sentence instead of an empty table.

    An earlier attempt of this same cycle (a resumed run) that already posted it is recognised by the cycle's
    ``address_summary_posted`` flag, and by a marker already on the pushed head when the head moved. Without
    a move the head is not new and a marker on it may belong to a previous cycle, so only the flag applies.

    Never raises: a failed post is logged and returns False, which does leave the cycle uncounted (an
    accepted gap: no local record of it survives).
    """
    if ctx.address_summary_posted:
        log.info("step.resolve_review_threads.summary_already_posted", pr=ctx.pr_number)
        return True
    round_no = 1
    try:
        reviews = await ctx.adapter.get_pr_reviews(ctx.pr_number)
        markers = [m for m in (SOVA_ADDRESSED_MARKER_RE.search(r.body or "") for r in reviews) if m]
        round_no += len(markers)
        if head_moved and head_sha and any((m.group(1) or "").lower() == head_sha.lower() for m in markers):
            log.info("step.resolve_review_threads.summary_already_posted", pr=ctx.pr_number, head=head_sha[:7])
            ctx.address_summary_posted = True
            return True
    except Exception:  # noqa: BLE001 (round numbering is cosmetic; adapter raises AdapterError/ValueError too)
        log.debug("step.resolve_review_threads.round_lookup_failed", pr=ctx.pr_number, exc_info=True)

    # A cycle whose fixes did not reach the remote branch still posts the marker (the budget counts it) but
    # lists nothing as addressed.
    findings = ctx.addressed_review_findings if head_moved else []
    body = format_address_summary(findings, head_sha=head_sha, round_no=round_no, pushed=head_moved)
    try:
        await ctx.adapter.post_pr_review(ctx.pr_number, body=body, event="COMMENT", comments=[])
    except Exception:  # noqa: BLE001 (adapter raises AdapterError/ValueError too; stay fail-open)
        log.warning("step.resolve_review_threads.summary_post_failed", pr=ctx.pr_number, exc_info=True)
        return False
    ctx.address_summary_posted = True
    log.info("step.resolve_review_threads.summary_posted", pr=ctx.pr_number, findings=len(findings), round=round_no)
    return True


async def finish_address_cycle(ctx: ExecutionContext) -> StepResult:
    """Reply to and resolve the cycle's addressed threads and post its summary marker.

    Shared by ``ResolveReviewThreadsStep`` (right after ``push``) and ``ResolveExternalReviewsStep``, which
    runs it as a fallback for a persisted ``[pipelines] address_review`` override that predates this step.
    """
    if ctx.pipeline_variant != "address_review":
        # A non-default configured pipeline can include this step outside the address-review pipeline;
        # posting sova-addressed there would let review history count a cycle that never ran the
        # address-review flow and could mark an older verdict as addressed.
        return StepResult(success=True, summary="Not an address-review run")
    if ctx.pr_number is None:
        return StepResult(success=True, summary="No PR to resolve review threads for")
    if not ctx.address_cycle_recorded:
        # A resume that skipped address_review with no persisted record (a run that predates the record):
        # an empty summary here would claim "no findings were pending" over fixes made before the resume.
        log.info("step.resolve_review_threads.no_cycle_recorded", pr=ctx.pr_number)
        return StepResult(success=True, summary="No address cycle recorded; nothing to resolve")

    log.info("step.resolve_review_threads", pr=ctx.pr_number, threads=len(ctx.addressed_review_thread_ids))

    head_sha = await _local_head(ctx)
    head_moved = await _head_moved(ctx, head_sha)

    replied = resolved = 0
    if not head_moved:
        log.info("step.resolve_review_threads.head_unmoved", pr=ctx.pr_number, head=(head_sha or "")[:7])
    elif ctx.addressed_review_thread_ids and head_sha:
        try:
            replied, resolved = await _reply_and_resolve(ctx, head_sha)
        except Exception:  # noqa: BLE001 (thread resolution is best effort; the summary must still post)
            log.warning("step.resolve_review_threads.thread_resolution_failed", pr=ctx.pr_number, exc_info=True)
            replied = resolved = 0
        log.info("step.resolve_review_threads.threads_resolved", replied=replied, resolved=resolved)

    summary_posted = await _post_address_summary(ctx, head_sha, head_moved=head_moved)

    parts = []
    if resolved:
        parts.append(f"{resolved} threads resolved")
    if summary_posted:
        parts.append("address summary posted")
    return StepResult(success=True, summary=", ".join(parts) if parts else "No review threads to resolve")


class ResolveReviewThreadsStep(BaseStep):
    name = "resolve_review_threads"
    max_retries = 0

    async def execute(self, ctx: ExecutionContext) -> StepResult:
        return await finish_address_cycle(ctx)

    async def validate_output(self, ctx: ExecutionContext) -> GateCheckResult:
        return GateCheckResult(passed=True)

    async def can_skip(self, ctx: ExecutionContext) -> bool:
        if self.name in ctx.completed_steps:
            return True
        return not ctx.pr_number
