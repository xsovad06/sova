#!/usr/bin/env bash
# Invariant: Monetary values must use Decimal, never float
# Checks changed Python files for float usage near money-related variable names
set -euo pipefail

WORKTREE_DIR="$1"
BASE_BRANCH="${2:-main}"

changed_files=$(git -C "$WORKTREE_DIR" diff --name-only "origin/$BASE_BRANCH" -- '*.py' 2>/dev/null || true)
[[ -z "$changed_files" ]] && exit 0

violations=""
while IFS= read -r f; do
  [[ -f "$WORKTREE_DIR/$f" ]] || continue
  # Look for float() calls near monetary variable names in changed lines
  result=$(git -C "$WORKTREE_DIR" diff "origin/$BASE_BRANCH" -- "$f" \
    | grep '^+' | grep -v '^+++' \
    | grep -iE 'float\s*\(' \
    | grep -iE 'amount|price|cost|total|balance|value|salary|income|expense|net_worth|rate' || true)
  if [[ -n "$result" ]]; then
    violations+="  $f: $result"$'\n'
  fi
done <<< "$changed_files"

if [[ -n "$violations" ]]; then
  echo "FAIL: float() used with monetary variables (use Decimal instead):"
  echo "$violations"
  exit 1
fi
exit 0
