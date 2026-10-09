#!/usr/bin/env bash
# Invariant: new code logs through sova.utils.logging, never a stdlib logger.
#
# SOVA logs with structlog: `log = get_logger(component="...")`, then calls such
# as `log.warning("step.push.failed", branch=name, exc_info=True)`. A logger from
# `logging.getLogger(...)` looks the same at the call site, often under the same
# `log` name, but raises TypeError on those keyword arguments, and only when that
# line runs (typically an error path nobody exercised). Eleven modules still
# create stdlib loggers; only lines a branch adds are checked, so those do not
# fail it and no new one can appear.
#
# Escape hatch: end the line with `# stdlib-logging: <reason>`, for example to
# tune a third-party library's logger.
set -euo pipefail

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  echo "Usage: $0 <worktree-dir> [base-branch]"
  echo "Checks that lines added under sova/ create loggers with sova.utils.logging.get_logger,"
  echo "not logging.getLogger. Exempt a line with '# stdlib-logging: <reason>'."
  exit 0
fi

WORKTREE_DIR="${1:-.}"
BASE_BRANCH="${2:-main}"

# The match must come before any `#`, so a comment that mentions the call is not one.
STDLIB_LOGGER='^\+[^#]*(logging\.getLogger[[:space:]]*\(|from[[:space:]]+logging[[:space:]]+import[^#]*getLogger([^[:alnum:]_]|$))'
EXEMPTION='#[[:space:]]*stdlib-logging:[[:space:]]*[^[:space:]]'

changed_files=$(git -C "$WORKTREE_DIR" diff --name-only "origin/$BASE_BRANCH" -- 'sova/*.py' 2> /dev/null || true)
[[ -z "$changed_files" ]] && exit 0

violations=""
while IFS= read -r f; do
  [[ -f "$WORKTREE_DIR/$f" ]] || continue
  # The helper itself has to talk to the stdlib to configure handlers.
  [[ "$f" == "sova/utils/logging.py" ]] && continue
  found=$(git -C "$WORKTREE_DIR" diff "origin/$BASE_BRANCH" -- "$f" \
    | grep -v '^+++' \
    | grep -E "$STDLIB_LOGGER" \
    | grep -vE "$EXEMPTION" || true)
  if [[ -n "$found" ]]; then
    while IFS= read -r line; do
      violations+="  $f: ${line#+}"$'\n'
    done <<< "$found"
  fi
done <<< "$changed_files"

if [[ -n "$violations" ]]; then
  echo "FAIL: new stdlib loggers under sova/ (use get_logger from sova.utils.logging):"
  echo "$violations"
  echo "  from sova.utils.logging import get_logger"
  echo "  log = get_logger(component=\"module.name\")"
  echo "If a stdlib logger is required, end the line with '# stdlib-logging: <reason>'."
  exit 1
fi
exit 0
