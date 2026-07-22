"""Merge operations: merge queue detection, config-aware merge, queue polling."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

from sova.config.models import IntegrationConfig
from sova.utils.gh import resolve_gh_env
from sova.utils.logging import get_logger
from sova.utils.shell import run

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
query($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) {
    mergeQueue(branch: "%s") {
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
    query = _MERGE_QUEUE_DETECTION_QUERY % base_branch
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
) -> None:
    if not issue_number:
        return

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
        return

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
        return

    log.warning(
        "git.merge.unknown_post_merge_state",
        state=post_merge_state,
        issue=issue_number,
        msg=f"Unknown post_merge_state {post_merge_state!r} for GitHub adapter, skipping state transition",
    )
