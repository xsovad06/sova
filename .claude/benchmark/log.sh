#!/usr/bin/env bash
set -euo pipefail

# Benchmark event logger for issue #6 (SOVA vs interactive velocity comparison)
# Usage: bash .claude/benchmark/log.sh <event> <issue_number> [notes]
# Events: session_start, session_end, issue_loaded, spec_start, spec_complete,
#         develop_start, develop_complete, review_start, review_complete,
#         pr_created, ci_check_start, ci_passed, ci_failed,
#         human_idle_start, human_idle_end, error

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

event="${1:?Usage: log.sh <event> <issue_number> [notes]}"
issue="${2:?Usage: log.sh <event> <issue_number> [notes]}"
notes="${3:-}"

log_file="${SCRIPT_DIR}/issue-${issue}.jsonl"

ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

if [ -n "$notes" ]; then
    printf '{"ts": "%s", "event": "%s", "issue": %s, "notes": "%s"}\n' \
        "$ts" "$event" "$issue" "$notes" >> "$log_file"
else
    printf '{"ts": "%s", "event": "%s", "issue": %s}\n' \
        "$ts" "$event" "$issue" >> "$log_file"
fi

echo "Logged: ${event} for issue #${issue} at ${ts}"
