#!/usr/bin/env bash
set -euo pipefail

# Claude Code SessionStart hook: logs session_start event to the benchmark log.
# Only fires for branches matching the issue pattern (feat/issue-NNN, etc.).
# Graceful no-op when not on a benchmark branch.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=resolve_root.sh
source "$SCRIPT_DIR/resolve_root.sh"

readonly ISSUE_BRANCH_PATTERN='^(feat|fix|refactor|chore|test|docs)/issue-([0-9]+)'

main() {
    local project_dir
    project_dir="$(resolve_project_root)"

    local branch_name issue
    branch_name="$(git -C "$project_dir" branch --show-current 2>/dev/null || echo "")"
    issue=""
    if [[ "$branch_name" =~ $ISSUE_BRANCH_PATTERN ]]; then
        issue="${BASH_REMATCH[2]}"
    fi

    if [ -z "$issue" ]; then
        exit 0
    fi

    local log_dir
    log_dir="$(resolve_log_dir)"
    mkdir -p "$log_dir"
    local log_file="$log_dir/issue-${issue}.jsonl"

    local ts
    ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

    jq -cn --arg ts "$ts" --argjson issue "$issue" \
      '{ts: $ts, event: "session_start", issue: $issue}' >> "$log_file"
}

main
