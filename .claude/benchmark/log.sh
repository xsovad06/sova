#!/usr/bin/env bash
set -euo pipefail

# Benchmark event logger for issue #6 (SOVA vs interactive velocity comparison)
# Usage: bash .claude/benchmark/log.sh <event> [issue_number] [notes]
# Events: session_start, session_end, issue_loaded, spec_start, spec_complete,
#         develop_start, develop_complete, review_start, review_complete,
#         pr_start, pr_complete, pr_created,
#         address_pr_start, address_pr_complete,
#         integrate_pr_start, integrate_pr_complete,
#         approve_merge_start, approve_merge_complete,
#         ci_check_start, ci_passed, ci_failed,
#         human_idle_start, human_idle_end, error

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Branch name regex pattern (GitHub issues only; cf. sova/git/worktree.py _ISSUE_BRANCH_RE for JIRA support)
readonly ISSUE_BRANCH_PATTERN='^(feat|fix|refactor|chore|test|docs)/issue-([0-9]+)'

event="${1:?Usage: log.sh <event> [issue_number] [notes]}"
issue="${2:-}"
notes="${3:-}"

# Auto-detect issue number from branch name if not provided
if [ -z "$issue" ]; then
    branch_name=$(git branch --show-current 2>/dev/null || echo "")
    if [[ "$branch_name" =~ $ISSUE_BRANCH_PATTERN ]]; then
        issue="${BASH_REMATCH[2]}"
    fi
fi

# Skip logging if no issue could be determined
if [ -z "$issue" ]; then
    echo "Warning: No issue number provided or detected from branch" >&2
    exit 0
fi

log_file="${SCRIPT_DIR}/issue-${issue}.jsonl"
ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

# Build JSON (simple printf - notes field is optional)
if [ -n "$notes" ]; then
    printf '{"ts": "%s", "event": "%s", "issue": %s, "notes": "%s"}\n' \
        "$ts" "$event" "$issue" "$notes" >> "$log_file"
else
    printf '{"ts": "%s", "event": "%s", "issue": %s}\n' \
        "$ts" "$event" "$issue" >> "$log_file"
fi

echo "Logged: ${event} for issue #${issue} at ${ts}"
