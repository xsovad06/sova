#!/usr/bin/env bash
# Invariant: Commit messages must follow conventional commits format
# Format: type(scope): description
# Types: feat, fix, refactor, test, docs, chore
# Scopes: agent, dashboard, commands, personas, invariants, knowledge, cli, docs
set -euo pipefail

WORKTREE_DIR="$1"
BASE_BRANCH="${2:-main}"

if [[ "${1:-}" == "--help" ]]; then
  echo "Usage: $0 <worktree-dir> [base-branch]"
  echo "Checks that commit messages follow conventional commits format."
  exit 0
fi

VALID_TYPES="feat|fix|refactor|test|docs|chore"
VALID_SCOPES="agent|dashboard|commands|personas|invariants|knowledge|cli|docs|readme"

commits=$(git -C "$WORKTREE_DIR" log --format="%H %s" "origin/$BASE_BRANCH..HEAD" 2>/dev/null || true)
[[ -z "$commits" ]] && exit 0

violations=""
while IFS= read -r line; do
  [[ -z "$line" ]] && continue
  hash="${line%% *}"
  subject="${line#* }"
  short_hash="${hash:0:8}"

  if ! echo "$subject" | grep -qE "^($VALID_TYPES)\(($VALID_SCOPES)\): .+"; then
    violations+="  $short_hash $subject"$'\n'
  fi
done <<< "$commits"

if [[ -n "$violations" ]]; then
  echo "FAIL: Commit messages not in conventional format 'type(scope): description':"
  echo "$violations"
  echo "Valid types: ${VALID_TYPES//|/, }"
  echo "Valid scopes: ${VALID_SCOPES//|/, }"
  exit 1
fi
exit 0
