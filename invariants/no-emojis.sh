#!/usr/bin/env bash
# Invariant: No emojis in code or documentation
set -euo pipefail

WORKTREE_DIR="$1"
BASE_BRANCH="${2:-main}"

changed_files=$(git -C "$WORKTREE_DIR" diff --name-only "origin/$BASE_BRANCH" -- '*.py' '*.md' '*.html' '*.txt' 2>/dev/null || true)
[[ -z "$changed_files" ]] && exit 0

# Check only added lines for emoji characters (common Unicode emoji ranges)
violations=""
while IFS= read -r f; do
  [[ -f "$WORKTREE_DIR/$f" ]] || continue
  result=$(git -C "$WORKTREE_DIR" diff "origin/$BASE_BRANCH" -- "$f" \
    | grep '^+' | grep -v '^+++' \
    | grep -P '[\x{1F300}-\x{1F9FF}\x{2600}-\x{26FF}\x{2700}-\x{27BF}\x{FE00}-\x{FE0F}\x{1F000}-\x{1F02F}]' 2>/dev/null || true)
  if [[ -n "$result" ]]; then
    violations+="  $f: $result"$'\n'
  fi
done <<< "$changed_files"

if [[ -n "$violations" ]]; then
  echo "FAIL: Emoji characters found in changed files:"
  echo "$violations"
  exit 1
fi
exit 0
