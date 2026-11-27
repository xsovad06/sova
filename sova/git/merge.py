"""Merge operations: merge queue detection, config-aware merge, queue polling."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote

from sova.adapters.base import TaskState
from sova.config.models import IntegrationConfig
from sova.utils.gh import IssueLinks, parse_issue_links, resolve_gh_env
from sova.utils.logging import get_logger
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
    subject: str = "",
) -> list[str]:
    """Build the ``gh pr merge`` argv. *subject* (a validated squash subject) rides only on a direct squash."""
    args = ["gh", "pr", "merge", str(pr_number), "--repo", repo]

    if not use_queue:
        if cfg.merge_method == "squash":
            args.append("--squash")
            if subject:
                args.extend(["--subject", subject])
        elif cfg.merge_method == "rebase":
            args.append("--rebase")
        elif cfg.merge_method == "merge":
            args.append("--merge")

    if cfg.delete_branch and not use_queue:
        args.append("--delete-branch")

    return args


async def squash_is_sole_merge_method(*, repo: str, github_user: str = "") -> bool | None:
    """True when the repository allows squash merging and nothing else, False otherwise.

    None when the allowed methods cannot be read, so "could not tell" never reads as "several methods".
    Under ``merge_method = "auto"`` the repository default decides, and only this case is certainly squash.
    """
    env = await resolve_gh_env(github_user)
    result = await run(
        "gh",
        "repo",
        "view",
        repo,
        "--json",
        "rebaseMergeAllowed,squashMergeAllowed,mergeCommitAllowed",
        env=env,
    )
    if not result.success:
        log.warning("git.merge.allowed_methods_failed", repo=repo, stderr=result.stderr[:200])
        return None
    try:
        data = json.loads(result.stdout)
        return bool(data["squashMergeAllowed"]) and not data["rebaseMergeAllowed"] and not data["mergeCommitAllowed"]
    except (json.JSONDecodeError, KeyError, TypeError):
        log.warning("git.merge.allowed_methods_unparseable", repo=repo)
        return None


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
    subject: str = "",
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

    args = _build_merge_args(pr_number, repo=repo, cfg=cfg, use_queue=use_queue, subject=subject)
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


# Workflow labels that no longer apply once a merged PR has finished an issue. Suitability and hold labels
# (agent:ready, agent:on-hold, sova:approved) are a person's call and stay; agent:on-qa is the on_qa state itself.
_MERGED_STRIP_LABELS = ("agent:in-review", "agent:in-progress", "sova:revise", "sova:block")
# The post-merge states that finish an issue; any other leaves it open and in play, so its labels stay.
_STRIP_AFTER_STATES = frozenset({TaskState.DONE.value, TaskState.ON_QA.value})


@dataclass
class MergedIssues:
    """What :func:`handle_merged_pr_issues` did to each issue a merged PR body closes.

    ``labels_left`` maps a moved issue whose workflow labels were not all removed to the labels still on it,
    or to None when its labels could not be read. Either is a failure (:attr:`has_failures`).
    """

    transitioned: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    part_of: list[str] = field(default_factory=list)
    labels_left: dict[str, tuple[str, ...] | None] = field(default_factory=dict)

    @property
    def has_failures(self) -> bool:
        return bool(self.failed or self.labels_left)

    def describe(self, post_merge_state: str) -> list[str]:
        # A "Part of" issue is a deliberate skip, not a failure: it is listed so the operator can tell
        # "left open on purpose" from "not linked".
        return (
            [f"issue #{n} moved to {post_merge_state}" for n in self.transitioned]
            + [f"issue #{n} not updated (see log)" for n in self.failed]
            + [_describe_labels_left(n, left) for n, left in self.labels_left.items()]
            + [f"issue #{n} left open (Part of): unchanged" for n in self.part_of]
        )


def _describe_labels_left(issue: str, left: tuple[str, ...] | None) -> str:
    if left is None:
        return f"issue #{issue} labels unreadable: workflow labels may remain, remove them by hand"
    return f"issue #{issue} still labeled {', '.join(left)}: remove by hand"


async def _strip_workflow_labels(issue: str, *, repo: str, env: dict[str, str] | None) -> tuple[str, ...] | None:
    """Remove the :data:`_MERGED_STRIP_LABELS` the issue carries. Returns the ones left, or None if unreadable.

    Only carried labels are removed, so a repository that never created one (``sova:block``) is no error, and a
    label already gone by the time of the delete (404) counts as removed.
    """
    view = await run("gh", "issue", "view", issue, "--repo", repo, "--json", "labels", env=env)
    if not view.success:
        log.warning("git.merge.labels_unreadable", issue=issue, stderr=view.stderr[:200])
        return None
    try:
        carried = {label["name"] for label in json.loads(view.stdout).get("labels", [])}
    except (json.JSONDecodeError, AttributeError, KeyError, TypeError):
        log.warning("git.merge.labels_unparseable", issue=issue)
        return None
    left: list[str] = []
    for label in _MERGED_STRIP_LABELS:
        if label not in carried:
            continue
        result = await run(
            "gh", "api", "--method", "DELETE", f"repos/{repo}/issues/{issue}/labels/{quote(label, safe='')}", env=env
        )
        if not result.success and "http 404" not in result.stderr.lower():
            log.warning("git.merge.label_remove_failed", issue=issue, label=label, stderr=result.stderr[:200])
            left.append(label)
    return tuple(left)


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
    names moves to *post_merge_state*. A ``Part of #N`` issue (a hand-written or older PR body) and a
    bare ``#N`` are left untouched, with no comment and no label change. Which issue the caller
    associates with the PR plays no part (#1340). Returns None when the PR body cannot be read, so
    "could not tell" never reads as "nothing linked".

    A closing issue moved to ``done`` or ``on_qa`` also loses the :data:`_MERGED_STRIP_LABELS` it carries,
    whether the adapter or the ``gh`` fallback moved it. A removal that fails is listed in ``labels_left``;
    the close itself stands (#1425).
    """
    env = await resolve_gh_env(github_user)
    links = await read_issue_links(pr_number, repo=repo, env=env)
    if links is None:
        return None
    outcome = MergedIssues(part_of=list(links.part_of))
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
        if moved and post_merge_state in _STRIP_AFTER_STATES:
            try:
                left = await _strip_workflow_labels(issue, repo=repo, env=env)
            except Exception:  # noqa: BLE001 (the issue is already moved; a label failure must not abort the rest)
                log.warning("git.merge.label_strip_failed", issue=issue, pr=pr_number, exc_info=True)
                left = None
            if left is None or left:
                outcome.labels_left[issue] = left
    log.info(
        "git.merge.merged_pr_issues_handled",
        pr=pr_number,
        transitioned=outcome.transitioned,
        part_of_ignored=outcome.part_of,
        failed=outcome.failed,
        labels_left=outcome.labels_left,
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

    An interactive ``/pr`` has no create_pr step, so without this the issue keeps reading as BACKLOG and a
    developer run on it fails its state gate. Only an issue in ``_IN_REVIEW_FROM`` moves. Returns None when
    the PR body cannot be read.
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
