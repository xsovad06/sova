"""Merge operations: merge queue detection, config-aware merge, queue polling."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from sova.adapters.base import TaskState
from sova.config.models import IntegrationConfig
from sova.utils.gh import NOT_DONE_LABEL, IssueLinks, parse_issue_links, parse_not_done_items, resolve_gh_env
from sova.utils.logging import get_logger
from sova.utils.markdown import extract_section, mask_fenced_blocks
from sova.utils.shell import run

if TYPE_CHECKING:
    from sova.adapters.base import TaskAdapter

log = get_logger(component="git.merge")


@dataclass
class MergeQueueStatus:
    in_queue: bool
    state: str
    position: int | None
    estimated_time: str

    @property
    def is_merged(self) -> bool:
        return self.state == "MERGED"

    @property
    def is_failed(self) -> bool:
        return self.state in ("UNMERGEABLE", "LOCKED")


@dataclass
class MergeResult:
    success: bool
    merged: bool
    enqueued: bool
    message: str
    needs_poll: bool


_MERGE_QUEUE_DETECTION_QUERY = """
query($owner: String!, $name: String!, $branch: String!) {
  repository(owner: $owner, name: $name) {
    mergeQueue(branch: $branch) {
      configuration {
        mergeMethod
      }
    }
  }
}
"""

_MERGE_QUEUE_STATUS_QUERY = """
query($owner: String!, $name: String!, $pr: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $pr) {
      mergeQueueEntry {
        state
        position
        estimatedTimeToMerge
      }
      merged
    }
  }
}
"""


async def detect_merge_queue(
    *,
    repo: str,
    base_branch: str = "main",
    github_user: str = "",
) -> bool | None:
    owner, name = repo.split("/", 1)
    query = _MERGE_QUEUE_DETECTION_QUERY
    env = await resolve_gh_env(github_user)

    result = await run(
        "gh",
        "api",
        "graphql",
        "-f",
        f"owner={owner}",
        "-f",
        f"name={name}",
        "-f",
        f"branch={base_branch}",
        "-f",
        f"query={query}",
        env=env,
    )

    if not result.success:
        log.warning("git.merge.detect_queue_failed", stderr=result.stderr[:200])
        return None

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        log.warning("git.merge.detect_queue_parse_failed", exc_info=True)
        return None

    repo_data = (data.get("data") or {}).get("repository") or {}
    merge_queue = repo_data.get("mergeQueue")
    if merge_queue is None:
        return False

    return merge_queue.get("configuration") is not None


async def should_use_merge_queue(
    cfg: IntegrationConfig,
    *,
    repo: str,
    base_branch: str = "main",
    github_user: str = "",
) -> bool:
    if cfg.merge_queue_enabled == "true":
        return True
    if cfg.merge_queue_enabled == "false":
        return False

    detected = await detect_merge_queue(repo=repo, base_branch=base_branch, github_user=github_user)
    if detected is None:
        log.warning("git.merge.queue_detection_failed_fallback", msg="Falling back to non-queue merge path")
        return False
    return detected


def _build_merge_args(
    pr_number: int,
    *,
    repo: str,
    cfg: IntegrationConfig,
    use_queue: bool,
) -> list[str]:
    args = ["gh", "pr", "merge", str(pr_number), "--repo", repo]

    if not use_queue:
        if cfg.merge_method == "squash":
            args.append("--squash")
        elif cfg.merge_method == "rebase":
            args.append("--rebase")
        elif cfg.merge_method == "merge":
            args.append("--merge")

    if cfg.delete_branch and not use_queue:
        args.append("--delete-branch")

    return args


async def _get_pr_base_branch(
    pr_number: int,
    *,
    repo: str,
    github_user: str = "",
) -> str:
    env = await resolve_gh_env(github_user)
    result = await run(
        "gh",
        "pr",
        "view",
        str(pr_number),
        "--repo",
        repo,
        "--json",
        "baseRefName",
        "--jq",
        ".baseRefName",
        env=env,
    )
    if result.success and result.stdout.strip():
        return result.stdout.strip()
    log.warning("git.merge.base_branch_lookup_failed", pr=pr_number, msg="Falling back to 'main'")
    return "main"


async def merge_pr(
    pr_number: int,
    *,
    repo: str,
    cfg: IntegrationConfig,
    github_user: str = "",
    base_branch: str = "",
) -> MergeResult:
    if not base_branch:
        base_branch = await _get_pr_base_branch(pr_number, repo=repo, github_user=github_user)

    use_queue = await should_use_merge_queue(
        cfg,
        repo=repo,
        base_branch=base_branch,
        github_user=github_user,
    )

    log.info(
        "git.merge.start",
        pr=pr_number,
        method=cfg.merge_method,
        use_queue=use_queue,
    )

    args = _build_merge_args(pr_number, repo=repo, cfg=cfg, use_queue=use_queue)
    env = await resolve_gh_env(github_user)
    result = await run(*args, env=env)

    if not result.success:
        return MergeResult(
            success=False,
            merged=False,
            enqueued=False,
            message=result.stderr.strip() or result.stdout.strip(),
            needs_poll=False,
        )

    output = result.stdout.strip()

    if use_queue or "already queued" in output.lower() or "added to merge queue" in output.lower():
        return MergeResult(
            success=True,
            merged=False,
            enqueued=True,
            message=output or "PR enqueued in merge queue",
            needs_poll=True,
        )

    return MergeResult(
        success=True,
        merged=True,
        enqueued=False,
        message=output or "PR merged successfully",
        needs_poll=False,
    )


async def get_merge_queue_status(
    pr_number: int,
    *,
    repo: str,
    github_user: str = "",
) -> MergeQueueStatus:
    owner, name = repo.split("/", 1)
    env = await resolve_gh_env(github_user)

    result = await run(
        "gh",
        "api",
        "graphql",
        "-f",
        f"owner={owner}",
        "-f",
        f"name={name}",
        "-F",
        f"pr={pr_number}",
        "-f",
        f"query={_MERGE_QUEUE_STATUS_QUERY}",
        env=env,
    )

    if not result.success:
        log.warning("git.merge.queue_status_failed", pr=pr_number, stderr=result.stderr[:200])
        return MergeQueueStatus(in_queue=True, state="UNKNOWN", position=None, estimated_time="")

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        log.warning("git.merge.queue_status_parse_failed", pr=pr_number, exc_info=True)
        return MergeQueueStatus(in_queue=True, state="UNKNOWN", position=None, estimated_time="")

    pr_data = ((data.get("data") or {}).get("repository") or {}).get("pullRequest") or {}

    if pr_data.get("merged"):
        return MergeQueueStatus(in_queue=False, state="MERGED", position=None, estimated_time="")

    entry = pr_data.get("mergeQueueEntry")
    if entry is None:
        return MergeQueueStatus(in_queue=False, state="NOT_QUEUED", position=None, estimated_time="")

    return MergeQueueStatus(
        in_queue=True,
        state=entry.get("state", "QUEUED"),
        position=entry.get("position"),
        estimated_time=entry.get("estimatedTimeToMerge") or "",
    )


async def poll_merge_queue(
    pr_number: int,
    *,
    repo: str,
    cfg: IntegrationConfig,
    github_user: str = "",
) -> MergeQueueStatus:
    loop = asyncio.get_event_loop()
    start_time = loop.time()
    interval = cfg.merge_queue_poll_interval
    timeout = cfg.merge_queue_timeout
    consecutive_unknown = 0
    max_consecutive_unknown = 5

    while True:
        elapsed = loop.time() - start_time
        if elapsed >= timeout:
            break

        status = await get_merge_queue_status(pr_number, repo=repo, github_user=github_user)

        if status.is_merged:
            log.info("git.merge.queue_merged", pr=pr_number)
            return status

        if status.is_failed:
            log.warning("git.merge.queue_ejected", pr=pr_number, state=status.state)
            return status

        if not status.in_queue:
            log.info("git.merge.queue_left", pr=pr_number, state=status.state)
            return status

        if status.state == "UNKNOWN":
            consecutive_unknown += 1
            if consecutive_unknown >= max_consecutive_unknown:
                log.warning(
                    "git.merge.queue_api_unavailable",
                    pr=pr_number,
                    failures=consecutive_unknown,
                    msg="Too many consecutive API failures, stopping poll",
                )
                return MergeQueueStatus(
                    in_queue=True,
                    state="TIMEOUT",
                    position=None,
                    estimated_time="",
                )
        else:
            consecutive_unknown = 0

        position_info = f" (position {status.position})" if status.position is not None else ""
        log.info("git.merge.queue_waiting", pr=pr_number, state=status.state, position=position_info)

        remaining = timeout - (loop.time() - start_time)
        if remaining <= 0:
            break
        await asyncio.sleep(min(interval, remaining))

    log.warning("git.merge.queue_timeout", pr=pr_number, timeout=timeout)
    return MergeQueueStatus(
        in_queue=True,
        state="TIMEOUT",
        position=None,
        estimated_time="",
    )


async def delete_remote_branch(
    branch: str,
    *,
    repo: str,
    github_user: str = "",
) -> bool:
    env = await resolve_gh_env(github_user)
    result = await run(
        "gh",
        "api",
        "-X",
        "DELETE",
        f"repos/{repo}/git/refs/heads/{branch}",
        env=env,
    )
    if not result.success:
        log.warning("git.merge.delete_branch_failed", branch=branch, stderr=result.stderr[:200])
        return False
    log.info("git.merge.branch_deleted", branch=branch)
    return True


async def handle_post_merge_state(
    issue_number: str | int | None,
    *,
    post_merge_state: str,
    repo: str,
    github_user: str = "",
    adapter: TaskAdapter | None = None,
    project_dir: Path | None = None,
) -> bool:
    """Move one issue to *post_merge_state*. Returns False when nothing was moved.

    An adapter's own failure propagates as its exception; the gh fallback logs it and returns False.
    A closed issue (state ``done``) also leaves ``supervisor.task_queue`` when *project_dir* is given,
    so the queue loses it whether or not the supervisor runs.
    """
    if not issue_number:
        return False
    moved = await _move_to_post_merge_state(
        issue_number, post_merge_state=post_merge_state, repo=repo, github_user=github_user, adapter=adapter
    )
    if moved and post_merge_state == TaskState.DONE.value and project_dir is not None:
        await _drop_from_task_queue(issue_number, project_dir)
    return moved


async def _drop_from_task_queue(issue_number: str | int, project_dir: Path) -> None:
    """Remove a just-closed issue from the saved queue; a failure is logged, never raised.

    The close already happened, so a persistence error must not turn it into a failed merge step.
    """
    from sova.config.db_loader import remove_from_task_queue

    try:
        number = int(issue_number)
    except (TypeError, ValueError):
        return  # a Jira key is not a queue entry
    try:
        await remove_from_task_queue(project_dir, [number])
    except Exception:  # noqa: BLE001 (the issue is already closed; a queue write failure must not undo that result)
        log.warning("git.merge.queue_prune_failed", issue=issue_number, exc_info=True)


async def _move_to_post_merge_state(
    issue_number: str | int,
    *,
    post_merge_state: str,
    repo: str,
    github_user: str,
    adapter: TaskAdapter | None,
) -> bool:
    if adapter is not None:
        _VALID_POST_MERGE = {TaskState.DONE, TaskState.ON_QA}
        try:
            target_state = TaskState(post_merge_state)
        except ValueError:
            log.warning(
                "git.merge.unknown_post_merge_state",
                state=post_merge_state,
                issue=issue_number,
                msg="Invalid post_merge_state value, skipping adapter transition",
            )
            return False
        if target_state not in _VALID_POST_MERGE:
            log.warning(
                "git.merge.unknown_post_merge_state",
                state=post_merge_state,
                issue=issue_number,
                msg="Unsupported post_merge_state value, skipping adapter transition",
            )
            return False
        await adapter.transition_state(str(issue_number), target_state)
        return True

    env = await resolve_gh_env(github_user)

    if post_merge_state == "done":
        result = await run(
            "gh",
            "issue",
            "close",
            str(issue_number),
            "--repo",
            repo,
            env=env,
        )
        if not result.success:
            log.warning("git.merge.close_issue_failed", issue=issue_number, stderr=result.stderr[:200])
        return result.success

    if post_merge_state == "on_qa":
        result = await run(
            "gh",
            "issue",
            "edit",
            str(issue_number),
            "--repo",
            repo,
            "--add-label",
            "agent:on-qa",
            env=env,
        )
        if not result.success:
            log.warning("git.merge.on_qa_label_failed", issue=issue_number, stderr=result.stderr[:200])
        return result.success

    log.warning(
        "git.merge.unknown_post_merge_state",
        state=post_merge_state,
        issue=issue_number,
        msg="Unknown post_merge_state, skipping state transition",
    )
    return False


# Marks the comment a merged "Part of" PR leaves on its issue, so a re-run of /integrate-pr or
# the merge queue monitor finds it and does not post a second one.
_PART_OF_MARKER = "<!-- sova-part-of: pr={pr} -->"
# An unindented, unchecked task-list item, the same top-level rule
# sova/core/steps/_pr_body.py:extract_acceptance_criteria() applies to a criterion.
_UNCHECKED_ITEM_RE = re.compile(r"^[-*+][ \t]+\[ \][ \t]*(\S.*)$")
# The labels that say a PR is still being worked on or reviewed. A merged "Part of" PR is neither, so they come
# off its issue; every other label (agent:ready, priority:, area:) stays.
_PART_OF_STRIP_LABELS = frozenset({"agent:in-review", "agent:in-progress", "sova:revise", "sova:block"})


# Open PRs read when checking whether another PR still works on an issue; a repo with more is read in part.
_OPEN_PR_LIMIT = 200


class _PartOfResult(Enum):
    """How one "Part of" issue came out: labels removed, none to remove, kept for another open PR, or failed."""

    STRIPPED = "stripped"
    NO_LABELS = "no_labels"
    KEPT = "kept"
    FAILED = "failed"


@dataclass
class MergedIssues:
    """What :func:`handle_merged_pr_issues` did to each issue a merged PR body links.

    ``commented``, ``unlabeled`` and ``kept`` are all "Part of" issues with their criteria comment in place;
    they differ in what happened to the workflow labels (removed, none present, left for another open PR).
    """

    transitioned: list[str] = field(default_factory=list)
    commented: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    unlabeled: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)

    def describe(self, post_merge_state: str) -> list[str]:
        return (
            [f"issue #{n} moved to {post_merge_state}" for n in self.transitioned]
            + [
                f"issue #{n} left open (Part of), criteria comment in place, workflow labels cleared"
                for n in self.commented
            ]
            + [
                f"issue #{n} left open (Part of), criteria comment in place, no workflow labels to remove"
                for n in self.unlabeled
            ]
            + [
                f"issue #{n} left open (Part of), criteria comment in place, "
                "workflow labels kept (another open PR still links it)"
                for n in self.kept
            ]
            + [f"issue #{n} not updated (see log)" for n in self.failed]
        )


def _render_part_of_comment(pr_number: int, issue_body: str, not_done: tuple[str, ...] | None = None) -> str:
    """Render the "Part of" comment, listing *not_done* (the PR's own list) or else the issue's unchecked boxes."""
    lines = [
        _PART_OF_MARKER.format(pr=pr_number),
        f"PR #{pr_number} was merged as part of this issue, so the issue stays open. "
        "The PR description says what it left undone.",
        "",
    ]
    if not_done:
        lines.append(NOT_DONE_LABEL)
        lines.extend(f"- {item}" for item in not_done)
        return "\n".join(lines)
    log.info("git.merge.part_of_falls_back_to_issue_boxes", pr=pr_number, reason="no Not done list in PR body")
    section = mask_fenced_blocks(extract_section(issue_body, "Acceptance Criteria", ignore_case=True))
    unchecked = [m.group(1).strip() for line in section.splitlines() if (m := _UNCHECKED_ITEM_RE.match(line))]
    if unchecked:
        lines.append("Acceptance criteria still unchecked in this issue:")
        lines.extend(f"- {item}" for item in unchecked)
    else:
        lines.append("This issue has no unchecked acceptance criteria: confirm the rest is done before closing it.")
    return "\n".join(lines)


class _OpenPrLinks:
    """The issues the open PRs link, read from GitHub at most once per :func:`handle_merged_pr_issues` call.

    A merged body can name several "Part of" issues, and each one asks the same question, so the open-PR list is
    fetched the first time it is needed and the parsed links are reused. A failed or unparseable read is kept too,
    so a broken listing is not retried once per issue.
    """

    def __init__(self, *, repo: str, env: dict[str, str] | None) -> None:
        self._repo = repo
        self._env = env
        self._loaded = False
        self._prs: list[tuple[int | None, IssueLinks]] | None = None
        self._truncated = False

    async def _load(self, pr_number: int) -> None:
        self._loaded = True
        listed = await run(
            "gh", "pr", "list", "--repo", self._repo, "--state", "open", "--limit", str(_OPEN_PR_LIMIT),
            "--json", "number,body", env=self._env,
        )  # fmt: skip
        if not listed.success:
            log.warning("git.merge.open_prs_unreadable", pr=pr_number, stderr=listed.stderr[:200])
            return
        try:
            open_prs = json.loads(listed.stdout)
            self._prs = [(other.get("number"), parse_issue_links(other.get("body") or "")) for other in open_prs]
        except (json.JSONDecodeError, AttributeError, TypeError):
            log.warning("git.merge.open_prs_unparseable", pr=pr_number)
            return
        self._truncated = len(open_prs) >= _OPEN_PR_LIMIT
        if self._truncated:
            log.warning("git.merge.open_prs_truncated", pr=pr_number, limit=_OPEN_PR_LIMIT)

    async def another_links(self, issue: str, pr_number: int) -> bool | None:
        """True when an open PR other than *pr_number* closes or is part of *issue*; None when that cannot be told.

        A listing cut off at ``_OPEN_PR_LIMIT`` that shows no such PR is "cannot be told", not "none": the PRs it
        left out are the older ones, which are the likeliest to still link a long-running issue.
        """
        if not self._loaded:
            await self._load(pr_number)
        if self._prs is None:
            return None
        for number, links in self._prs:
            if number != pr_number and (issue in links.closes or issue in links.part_of):
                return True
        if self._truncated:
            log.warning("git.merge.open_prs_truncated_no_match", issue=issue, pr=pr_number, limit=_OPEN_PR_LIMIT)
            return None
        return False


async def _strip_workflow_labels(
    issue: str, present: set[str], pr_number: int, *, repo: str, env: dict[str, str] | None, open_prs: _OpenPrLinks
) -> _PartOfResult:
    """Remove the workflow labels of *_PART_OF_STRIP_LABELS* that *issue* carries.

    The labels stay when another open PR still closes or is part of the issue: they describe that PR's review
    too, and nothing re-applies them until a further PR opens.
    """
    to_remove = sorted(present & _PART_OF_STRIP_LABELS)
    if not to_remove:
        return _PartOfResult.NO_LABELS
    other_open = await open_prs.another_links(issue, pr_number)
    if other_open is None:
        return _PartOfResult.FAILED
    if other_open:
        log.info("git.merge.part_of_label_strip_skipped", issue=issue, pr=pr_number, reason="another_open_pr")
        return _PartOfResult.KEPT
    args = [flag for label in to_remove for flag in ("--remove-label", label)]
    edited = await run("gh", "issue", "edit", issue, "--repo", repo, *args, env=env)
    if not edited.success:
        log.warning("git.merge.part_of_label_strip_failed", issue=issue, pr=pr_number, stderr=edited.stderr[:200])
        return _PartOfResult.FAILED
    return _PartOfResult.STRIPPED


async def _update_part_of_issue(
    issue: str,
    pr_number: int,
    *,
    repo: str,
    env: dict[str, str] | None,
    open_prs: _OpenPrLinks,
    not_done: tuple[str, ...] | None = None,
) -> _PartOfResult:
    """Leave the "Part of" comment on *issue* unless it carries one, and strip its workflow labels.

    *not_done* is the list parsed from the PR body; without one the comment lists the issue's unchecked boxes.

    FAILED when either step failed, so a comment that posted next to a failed label edit still reads as a failure.
    """
    view = await run("gh", "issue", "view", issue, "--repo", repo, "--json", "body,comments,labels", env=env)
    if not view.success:
        log.warning("git.merge.part_of_issue_unreadable", issue=issue, pr=pr_number, stderr=view.stderr[:200])
        return _PartOfResult.FAILED
    marker = _PART_OF_MARKER.format(pr=pr_number)
    try:
        data = json.loads(view.stdout)
        issue_body = data.get("body") or ""
        already_posted = any(marker in (comment.get("body") or "") for comment in data.get("comments") or [])
        labels = {str(label.get("name")) for label in data.get("labels") or []}
    except (json.JSONDecodeError, AttributeError):
        log.warning("git.merge.part_of_issue_unparseable", issue=issue, pr=pr_number)
        return _PartOfResult.FAILED
    if not already_posted:
        comment = _render_part_of_comment(pr_number, issue_body, not_done)
        posted = await run("gh", "issue", "comment", issue, "--repo", repo, "--body", comment, env=env)
        if not posted.success:
            log.warning("git.merge.part_of_comment_failed", issue=issue, pr=pr_number, stderr=posted.stderr[:200])
            return _PartOfResult.FAILED
    return await _strip_workflow_labels(issue, labels, pr_number, repo=repo, env=env, open_prs=open_prs)


async def read_pr_body(pr_number: int, *, repo: str, env: dict[str, str] | None) -> str | None:
    """Read PR *pr_number*'s body. None when it cannot be read, so "could not tell" is not an empty body."""
    result = await run("gh", "pr", "view", str(pr_number), "--repo", repo, "--json", "body", env=env)
    if not result.success:
        log.warning("git.merge.pr_body_unreadable", pr=pr_number, stderr=result.stderr[:200])
        return None
    try:
        return json.loads(result.stdout).get("body") or ""
    except (json.JSONDecodeError, AttributeError):
        log.warning("git.merge.pr_body_unparseable", pr=pr_number)
        return None


async def read_issue_links(pr_number: int, *, repo: str, env: dict[str, str] | None) -> IssueLinks | None:
    """Read PR *pr_number*'s body and parse its issue links. None when the body cannot be read."""
    body = await read_pr_body(pr_number, repo=repo, env=env)
    return None if body is None else parse_issue_links(body)


async def handle_merged_pr_issues(
    pr_number: int,
    *,
    post_merge_state: str,
    repo: str,
    github_user: str = "",
    adapter: TaskAdapter | None = None,
    project_dir: Path | None = None,
) -> MergedIssues | None:
    """Apply *post_merge_state* to the issues a merged GitHub PR closes, and to no other.

    The merged PR's body decides, through :func:`parse_issue_links`: an issue a closing keyword
    names moves to *post_merge_state*; a ``Part of #N`` issue stays open with one comment naming
    the PR and listing what the PR body says it left undone (the issue's unchecked acceptance criteria when
    the body lists none), and loses the workflow labels of ``_PART_OF_STRIP_LABELS`` it carries unless
    another open PR still links it; a bare ``#N`` is left alone. Which issue the caller associates with the
    PR plays no part, since a PR only "part of" that issue must not close it (#1340). Returns None when the
    PR body cannot be read, so
    "could not tell" never reads as "nothing linked".
    """
    env = await resolve_gh_env(github_user)
    body = await read_pr_body(pr_number, repo=repo, env=env)
    if body is None:
        return None
    links = parse_issue_links(body)
    not_done = parse_not_done_items(body)
    outcome = MergedIssues()
    for issue in links.closes:
        try:
            moved = await handle_post_merge_state(
                issue,
                post_merge_state=post_merge_state,
                repo=repo,
                github_user=github_user,
                adapter=adapter,
                project_dir=project_dir,
            )
        except Exception:  # noqa: BLE001 (one issue's failure must not abort the other issues)
            log.warning("git.merge.post_merge_state_failed", issue=issue, pr=pr_number, exc_info=True)
            moved = False
        (outcome.transitioned if moved else outcome.failed).append(issue)
    open_prs = _OpenPrLinks(repo=repo, env=env)
    for issue in links.part_of:
        try:
            result = await _update_part_of_issue(
                issue, pr_number, repo=repo, env=env, open_prs=open_prs, not_done=not_done
            )
        except Exception:  # noqa: BLE001 (one issue's failure must not abort the other issues)
            log.warning("git.merge.part_of_update_raised", issue=issue, pr=pr_number, exc_info=True)
            result = _PartOfResult.FAILED
        bucket = {
            _PartOfResult.STRIPPED: outcome.commented,
            _PartOfResult.NO_LABELS: outcome.unlabeled,
            _PartOfResult.KEPT: outcome.kept,
            _PartOfResult.FAILED: outcome.failed,
        }[result]
        bucket.append(issue)
    log.info(
        "git.merge.merged_pr_issues_handled",
        pr=pr_number,
        transitioned=outcome.transitioned,
        commented=outcome.commented,
        unlabeled=outcome.unlabeled,
        kept=outcome.kept,
        failed=outcome.failed,
    )
    return outcome


# States an opened PR moves forward to IN_REVIEW. HUMAN_ONLY and NEEDS_SPEC are a person's call and ON_QA and
# DONE are past review; transition_state() replaces every state label, so moving those would erase that call.
_IN_REVIEW_FROM = frozenset({TaskState.BACKLOG, TaskState.TRIAGED, TaskState.RESEARCHED, TaskState.IN_PROGRESS})


@dataclass
class OpenedPrIssues:
    """What :func:`mark_pr_issues_in_review` did to each issue an open PR body links."""

    moved: list[str] = field(default_factory=list)
    already: list[str] = field(default_factory=list)
    left: list[tuple[str, TaskState]] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)

    def describe(self) -> list[str]:
        return (
            [f"issue #{n} moved to {TaskState.IN_REVIEW.value}" for n in self.moved]
            + [f"issue #{n} already {TaskState.IN_REVIEW.value}" for n in self.already]
            + [f"issue #{n} left at {state.value} (not moved)" for n, state in self.left]
            + [f"issue #{n} not updated (see log)" for n in self.failed]
        )


async def mark_pr_issues_in_review(
    pr_number: int, *, repo: str, adapter: TaskAdapter, github_user: str = ""
) -> OpenedPrIssues | None:
    """Move each issue an open PR closes or is part of to IN_REVIEW, as the pipeline's create_pr step does.

    An interactive ``/pr`` has no create_pr step, and a ``Part of`` merge strips the issue's state label, so
    without this the issue's next PR leaves it reading as BACKLOG and a developer run on it fails its state
    gate. Only an issue in ``_IN_REVIEW_FROM`` moves. Returns None when the PR body cannot be read.
    """
    env = await resolve_gh_env(github_user)
    links = await read_issue_links(pr_number, repo=repo, env=env)
    if links is None:
        return None
    outcome = OpenedPrIssues()
    for issue in (*links.closes, *links.part_of):
        try:
            state = await adapter.get_state(issue)
            if state is TaskState.IN_REVIEW:
                outcome.already.append(issue)
            elif state in _IN_REVIEW_FROM:
                await adapter.transition_state(issue, TaskState.IN_REVIEW)
                outcome.moved.append(issue)
            else:
                outcome.left.append((issue, state))
        except Exception:  # noqa: BLE001 (one issue's failure must not abort the other issues)
            log.warning("git.merge.mark_in_review_failed", issue=issue, pr=pr_number, exc_info=True)
            outcome.failed.append(issue)
    log.info(
        "git.merge.opened_pr_issues_handled",
        pr=pr_number,
        moved=outcome.moved,
        already=outcome.already,
        left=[issue for issue, _ in outcome.left],
        failed=outcome.failed,
    )
    return outcome
