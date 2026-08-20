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
# shellcheck source=resolve_root.sh
source "$SCRIPT_DIR/resolve_root.sh"

# Branch name regex pattern (GitHub issues only; cf. sova/git/worktree.py _ISSUE_BRANCH_RE for JIRA support)
readonly ISSUE_BRANCH_PATTERN='^(feat|fix|refactor|chore|test|docs)/issue-([0-9]+)'

# Skip logging for SOVA-spawned agent sessions (data comes from DB instead)
if [ "${SOVA_AGENT_RUN:-}" = "1" ]; then
    exit 0
fi

event="${1:?Usage: log.sh <event> [issue_number] [notes]}"
issue="${2:-}"
notes="${3:-}"

# Validate event name
valid_events=(
    'session_start' 'session_end' 'issue_loaded'
    'spec_start' 'spec_complete'
    'develop_start' 'develop_complete'
    'review_start' 'review_complete'
    'pr_start' 'pr_complete' 'pr_created'
    'address_pr_start' 'address_pr_complete'
    'integrate_pr_start' 'integrate_pr_complete'
    'approve_merge_start' 'approve_merge_complete'
    'ci_check_start' 'ci_passed' 'ci_failed'
    'human_idle_start' 'human_idle_end' 'error'
    'test'  # For testing only
)
if [[ ! " ${valid_events[*]} " =~ " ${event} " ]]; then
    echo "Invalid event: $event" >&2
    echo "Valid events: ${valid_events[*]}" >&2
    exit 1
fi

# Auto-detect issue number from branch name if not provided
if [ -z "$issue" ]; then
    branch_name=$(git branch --show-current 2>/dev/null || echo "")
    if [[ "$branch_name" =~ $ISSUE_BRANCH_PATTERN ]]; then
        issue="${BASH_REMATCH[2]}"
    fi
fi

# Resolve log directory to primary checkout (survives worktree cleanup).
# LOG_DIR env var overrides for test isolation.
LOG_DIR="$(resolve_log_dir)"
mkdir -p "$LOG_DIR"

# Sticky issue file: persists the issue number across branch switches within a
# session. Written on first successful detection, read back as fallback when
# branch detection fails (e.g., after integrate-pr switches to main).
# Cleared on session_start to prevent stale carryover between sessions.
_STICKY_FILE="${LOG_DIR}/.current_issue"

if [ "$event" = "session_start" ] && [ -f "$_STICKY_FILE" ]; then
    rm -f "$_STICKY_FILE"
fi

if [ -n "$issue" ]; then
    echo "$issue" > "$_STICKY_FILE"
elif [ -f "$_STICKY_FILE" ]; then
    issue=$(cat "$_STICKY_FILE")
fi

# Warn if no issue could be determined, but continue logging with null
if [ -z "$issue" ]; then
    echo "Warning: No issue number provided or detected from branch" >&2
fi
if [ -n "$issue" ]; then
    log_file="${LOG_DIR}/issue-${issue}.jsonl"
else
    log_file="${LOG_DIR}/issue-null.jsonl"
fi
ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

# Build JSON safely using jq to prevent injection
if [ -n "$issue" ]; then
    issue_json="$issue"
else
    issue_json="null"
fi

if [ -n "$notes" ]; then
    json_output=$(jq -n --arg ts "$ts" --arg event "$event" --arg notes "$notes" \
        "{ts: \$ts, event: \$event, issue: $issue_json, notes: \$notes}")
else
    json_output=$(jq -n --arg ts "$ts" --arg event "$event" \
        "{ts: \$ts, event: \$event, issue: $issue_json}")
fi

# Write to log file and verify success
if ! printf '%s\n' "$json_output" >> "$log_file"; then
    echo "Failed to write log to $log_file" >&2
    exit 1
fi

if [ -n "$issue" ]; then
    echo "Logged: ${event} for issue #${issue} at ${ts}"
else
    echo "Logged: ${event} (no issue) at ${ts}"
fi
