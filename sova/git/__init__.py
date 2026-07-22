"""Git operations and worktree management for SOVA."""

from sova.git.branch import (
    commit,
    create_branch,
    get_current_branch,
    push,
    rebase,
    sync_branch,
)
from sova.git.merge import (
    MergeQueueStatus,
    MergeResult,
    delete_remote_branch,
    detect_merge_queue,
    handle_post_merge_state,
    merge_pr,
    poll_merge_queue,
    should_use_merge_queue,
)
from sova.git.pr import (
    CheckConclusion,
    CheckStatus,
    CICheck,
    PRInfo,
    PRStatus,
    assign_pr,
    create_pr,
    find_pr_for_issue,
    get_ci_checks,
    get_pr_diff,
    get_pr_files,
    get_pr_status,
)
from sova.git.rebase import (
    rebase_with_conflict_resolution,
)
from sova.git.worktree import (
    WorktreeInfo,
    cleanup_stale_worktrees,
    cleanup_worktree,
    create_worktree,
    get_primary_worktree_root,
    list_worktrees,
)

__all__ = [
    # Branch
    "commit",
    "create_branch",
    "get_current_branch",
    "push",
    "rebase",
    "sync_branch",
    # PR
    "CICheck",
    "CheckConclusion",
    "CheckStatus",
    "PRInfo",
    "PRStatus",
    "assign_pr",
    "create_pr",
    "find_pr_for_issue",
    "get_ci_checks",
    "get_pr_diff",
    "get_pr_files",
    "get_pr_status",
    "MergeQueueStatus",
    "MergeResult",
    "delete_remote_branch",
    "detect_merge_queue",
    "handle_post_merge_state",
    "merge_pr",
    "poll_merge_queue",
    "should_use_merge_queue",
    # Rebase
    "rebase_with_conflict_resolution",
    # Worktree
    "WorktreeInfo",
    "cleanup_stale_worktrees",
    "cleanup_worktree",
    "create_worktree",
    "get_primary_worktree_root",
    "list_worktrees",
]
