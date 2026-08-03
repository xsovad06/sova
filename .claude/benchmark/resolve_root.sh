#!/usr/bin/env bash
# Resolve the primary (main) worktree root for benchmark log storage.
# Mirrors sova/git/worktree.py:get_primary_worktree_root().
#
# In the main checkout: git-common-dir returns ".git" (relative) -> use toplevel.
# In a linked worktree: returns "/path/to/main/.git" (absolute) -> use parent.
# Fallback: caller's SCRIPT_DIR/../..

resolve_project_root() {
    local common_dir
    common_dir="$(git rev-parse --git-common-dir 2>/dev/null)" || {
        (cd "$(dirname "${BASH_SOURCE[1]}")/../.." && pwd)
        return
    }

    case "$common_dir" in
        /*)
            # Absolute path = linked worktree. Parent of .git is the main checkout.
            (cd "$common_dir/.." && pwd)
            ;;
        *)
            # Relative path (.git) = primary checkout.
            git rev-parse --show-toplevel 2>/dev/null || (cd "$(dirname "${BASH_SOURCE[1]}")/../.." && pwd)
            ;;
    esac
}

resolve_log_dir() {
    local root
    root="$(resolve_project_root)"
    echo "${LOG_DIR:-$root/.claude/benchmark}"
}
