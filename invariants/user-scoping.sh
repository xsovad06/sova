#!/usr/bin/env bash
# Invariant: QuerySets in views/services should be filtered by user
# Flags new .objects.all() or .objects.filter() calls without user in views/services
set -euo pipefail

WORKTREE_DIR="$1"
BASE_BRANCH="${2:-main}"

# Only check views.py and services.py files
changed_files=$(git -C "$WORKTREE_DIR" diff --name-only "origin/$BASE_BRANCH" -- '*.py' 2>/dev/null \
  | grep -E '(views|services)\.py$' || true)
[[ -z "$changed_files" ]] && exit 0

violations=""
while IFS= read -r f; do
  [[ -f "$WORKTREE_DIR/$f" ]] || continue
  # Look for .objects.all() or .objects.filter() without user in added lines
  result=$(git -C "$WORKTREE_DIR" diff "origin/$BASE_BRANCH" -- "$f" \
    | grep '^+' | grep -v '^+++' \
    | grep -E '\.objects\.(all|filter|exclude|get)\(' \
    | grep -v 'user' \
    | grep -v '# noqa: user-scope' \
    | grep -v 'test' \
    | grep -v 'migration' || true)
  if [[ -n "$result" ]]; then
    violations+="  $f: $result"$'\n'
  fi
done <<< "$changed_files"

if [[ -n "$violations" ]]; then
  echo "WARN: QuerySet calls without user filter in views/services (add '# noqa: user-scope' to suppress):"
  echo "$violations"
  # Exit 0 (warning only) — not all queries need user scoping (e.g., Currency, shared models)
  exit 0
fi
exit 0
