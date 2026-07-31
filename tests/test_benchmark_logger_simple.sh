#!/usr/bin/env bash
# Simple test for benchmark logger
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
LOGGER="$PROJECT_ROOT/.claude/benchmark/log.sh"

GREEN='\033[0;32m'
RED='\033[0;31m'
NC='\033[0m'

# Branch name regex pattern (must match log.sh)
readonly ISSUE_BRANCH_PATTERN='^(feat|fix|refactor|chore|test|docs)/issue-([0-9]+)'

# Use temporary directory for test isolation
TEST_LOG_DIR=$(mktemp -d)
export LOG_DIR="$TEST_LOG_DIR"

pass=0
fail=0

# Cleanup function
cleanup() {
    rm -rf "$TEST_LOG_DIR"
}
trap cleanup EXIT

# Helper: Run logger with custom log dir
run_logger() {
    (cd "$PROJECT_ROOT" && bash "$LOGGER" "$@")
}

# Test 1: Explicit issue number
run_logger "test" "42" "notes" >/dev/null 2>&1
if grep -q '"issue": 42' "$TEST_LOG_DIR/issue-42.jsonl" 2>/dev/null; then
    echo -e "${GREEN}✓${NC} Explicit issue number"
    pass=$((pass + 1))
else
    echo -e "${RED}✗${NC} Explicit issue number"
    fail=$((fail + 1))
fi

# Test 2: Auto-detect from current branch (deterministic - create temp branch)
current_branch=$(git -C "$PROJECT_ROOT" branch --show-current)
test_branch="test/issue-888"

if git -C "$PROJECT_ROOT" checkout -b "$test_branch" >/dev/null 2>&1; then
    run_logger "test" >/dev/null 2>&1
    if grep -q '"issue": 888' "$TEST_LOG_DIR/issue-888.jsonl" 2>/dev/null; then
        echo -e "${GREEN}✓${NC} Auto-detect from temp branch"
        pass=$((pass + 1))
    else
        echo -e "${RED}✗${NC} Auto-detect from temp branch"
        fail=$((fail + 1))
    fi

    # Restore original branch and delete test branch
    git -C "$PROJECT_ROOT" checkout "$current_branch" >/dev/null 2>&1
    git -C "$PROJECT_ROOT" branch -D "$test_branch" >/dev/null 2>&1
else
    echo -e "${RED}✗${NC} Failed to create temp branch"
    fail=$((fail + 1))
fi

# Test 3: Notes field
run_logger "test" "789" "pr_number=123" >/dev/null 2>&1
if grep -q '"notes": "pr_number=123"' "$TEST_LOG_DIR/issue-789.jsonl" 2>/dev/null; then
    echo -e "${GREEN}✓${NC} Notes field included"
    pass=$((pass + 1))
else
    echo -e "${RED}✗${NC} Notes field included"
    fail=$((fail + 1))
fi

# Test 4: No notes field (verify file exists before negative assertion)
run_logger "test" "999" >/dev/null 2>&1
if [[ -s "$TEST_LOG_DIR/issue-999.jsonl" ]]; then
    if ! grep -q '"notes":' "$TEST_LOG_DIR/issue-999.jsonl" 2>/dev/null; then
        echo -e "${GREEN}✓${NC} Notes field omitted"
        pass=$((pass + 1))
    else
        echo -e "${RED}✗${NC} Notes field omitted (found notes when none expected)"
        fail=$((fail + 1))
    fi
else
    echo -e "${RED}✗${NC} Notes field omitted (log file not created)"
    fail=$((fail + 1))
fi

# Test 5: No issue warning (deterministic - use temp detached HEAD)
saved_branch=$(git -C "$PROJECT_ROOT" branch --show-current)
saved_commit=$(git -C "$PROJECT_ROOT" rev-parse HEAD)

# Guard: skip test if on detached HEAD or if we can't save state
if [ -z "$saved_branch" ] || [ -z "$saved_commit" ]; then
    echo -e "${RED}✗${NC} Skipped Test 5 (detached HEAD or unable to save state)"
    fail=$((fail + 1))
else
    # Set up cleanup trap for git state
    cleanup_git() {
        if ! git -C "$PROJECT_ROOT" checkout "$saved_branch" >/dev/null 2>&1; then
            # Fallback: restore to commit if branch checkout fails
            git -C "$PROJECT_ROOT" checkout "$saved_commit" >/dev/null 2>&1
        fi
    }
    trap cleanup_git EXIT

    if git -C "$PROJECT_ROOT" checkout --detach >/dev/null 2>&1; then
        output=$(run_logger "test" 2>&1)

        # Restore original state before checking results
        cleanup_git

        # Verify: warning issued, file created with null issue
        if [[ "$output" == *"Warning: No issue number provided or detected from branch"* ]] && \
           [ -f "$TEST_LOG_DIR/issue-null.jsonl" ] && \
           grep -q '"issue": null' "$TEST_LOG_DIR/issue-null.jsonl"; then
            echo -e "${GREEN}✓${NC} No issue warning on detached HEAD"
            pass=$((pass + 1))
        else
            echo -e "${RED}✗${NC} No issue warning on detached HEAD"
            fail=$((fail + 1))
        fi
    else
        echo -e "${RED}✗${NC} Failed to create detached HEAD"
        fail=$((fail + 1))
    fi

    # Remove the git cleanup trap (main cleanup trap remains)
    trap cleanup EXIT
fi

echo ""
echo "Passed: $pass, Failed: $fail"
[[ $fail -eq 0 ]]
