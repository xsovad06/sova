#!/usr/bin/env bash
# Invariant: Branch names must follow the naming convention
# Format: feat/<name>, fix/<name>, refactor/<name>, docs/<name>, chore/<name>, test/<name>
set -euo pipefail

WORKTREE_DIR="$1"

if [[ "${1:-}" == "--help" ]]; then
  echo "Usage: $0 <worktree-dir>"
  echo "Checks that the current branch follows the naming convention."
  exit 0
fi

# In CI (detached HEAD), use GITHUB_HEAD_REF (PR) or GITHUB_REF_NAME (push)
if [[ -n "${GITHUB_HEAD_REF:-}" ]]; then
  branch="$GITHUB_HEAD_REF"
elif [[ -n "${GITHUB_REF_NAME:-}" ]]; then
  branch="$GITHUB_REF_NAME"
else
  branch=$(git -C "$WORKTREE_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null || true)
fi
[[ -z "$branch" ]] && exit 0

# main/master are always allowed
if [[ "$branch" == "main" || "$branch" == "master" ]]; then
  exit 0
fi

if ! echo "$branch" | grep -qE '^(feat|fix|refactor|docs|chore|test)/[a-z0-9][a-z0-9-]+$'; then
  echo "FAIL: Branch name '$branch' does not follow convention."
  echo "Expected: feat/<name>, fix/<name>, refactor/<name>, docs/<name>, chore/<name>, or test/<name>"
  echo "Name part must be lowercase alphanumeric with hyphens (e.g., feat/add-linear-adapter)."
  exit 1
fi
exit 0
