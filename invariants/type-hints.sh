#!/usr/bin/env bash
# Invariant: New Python functions must have type annotations
# Checks for def statements without -> return type annotation
set -euo pipefail

WORKTREE_DIR="$1"
BASE_BRANCH="${2:-main}"

changed_files=$(git -C "$WORKTREE_DIR" diff --name-only "origin/$BASE_BRANCH" -- '*.py' 2>/dev/null || true)
[[ -z "$changed_files" ]] && exit 0

violations=""
while IFS= read -r f; do
  [[ -f "$WORKTREE_DIR/$f" ]] || continue
  # Skip test files and migrations
  [[ "$f" == */tests/* || "$f" == */migrations/* ]] && continue
  # Look for new function defs without return type annotation
  result=$(git -C "$WORKTREE_DIR" diff "origin/$BASE_BRANCH" -- "$f" \
    | grep '^+' | grep -v '^+++' \
    | grep -E '^\+\s*def\s+' \
    | grep -v '\->' || true)
  if [[ -n "$result" ]]; then
    violations+="  $f: $result"$'\n'
  fi
done <<< "$changed_files"

if [[ -n "$violations" ]]; then
  echo "FAIL: New functions missing return type annotation (->):"
  echo "$violations"
  exit 1
fi
exit 0
