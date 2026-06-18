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
  [[ "$f" == tests/* || "$f" == */tests/* || "$f" == */migrations/* ]] && continue
  # Look for new function defs without return type annotation.
  # Multi-line signatures have -> on a later line, so extract function
  # names from the diff and check the actual file for the full signature.
  new_funcs=$(git -C "$WORKTREE_DIR" diff "origin/$BASE_BRANCH" -- "$f" \
    | grep '^+' | grep -v '^+++' \
    | grep -oE '^\+\s*def\s+\w+' \
    | sed 's/^+[[:space:]]*//' | sed 's/^def //' || true)
  [[ -z "$new_funcs" ]] && continue
  while IFS= read -r func_name; do
    # Extract the full signature from def to the line ending with ):  or -> ...:
    sig=$(sed -n "/def ${func_name}(/,/^[^#]*):$/p" "$WORKTREE_DIR/$f" | head -20)
    if ! echo "$sig" | grep -q '\->'; then
      violations+="  $f: def ${func_name}()"$'\n'
    fi
  done <<< "$new_funcs"
done <<< "$changed_files"

if [[ -n "$violations" ]]; then
  echo "FAIL: New functions missing return type annotation (->):"
  echo "$violations"
  exit 1
fi
exit 0
