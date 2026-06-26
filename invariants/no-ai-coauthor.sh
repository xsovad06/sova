#!/usr/bin/env bash
# Invariant: No AI/Claude co-author references in commits
set -euo pipefail

WORKTREE_DIR="$1"
BASE_BRANCH="${2:-main}"

commits=$(git -C "$WORKTREE_DIR" log --format="%H %s%n%b" "origin/$BASE_BRANCH..HEAD" 2>/dev/null || true)
[[ -z "$commits" ]] && exit 0

violations=$(echo "$commits" | grep -iE 'co-authored-by.*(claude|anthropic|copilot|openai|gemini|\bai\b|noreply@anthropic)|generated.*by.*claude' || true)

if [[ -n "$violations" ]]; then
  echo "FAIL: AI co-author references found in commits:"
  echo "$violations"
  exit 1
fi
exit 0
