#!/usr/bin/env bash
# Simple test for benchmark logger

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
LOGGER="$PROJECT_ROOT/.claude/benchmark/log.sh"
LOG_DIR="$PROJECT_ROOT/.claude/benchmark"

GREEN='\033[0;32m'
RED='\033[0;31m'
NC='\033[0m'

pass=0
fail=0

# Test 1: Explicit issue number
bash "$LOGGER" "test" "42" "notes" >/dev/null 2>&1
if grep -q '"issue": 42' "$LOG_DIR/issue-42.jsonl" 2>/dev/null; then
    echo -e "${GREEN}✓${NC} Explicit issue number"
    ((pass++))
else
    echo -e "${RED}✗${NC} Explicit issue number"
    ((fail++))
fi
rm -f "$LOG_DIR/issue-42.jsonl"

# Test 2: Auto-detect from current branch (feat/issue-568)
current_branch=$(git branch --show-current)
if [[ "$current_branch" =~ ^(feat|fix)/issue-([0-9]+) ]]; then
    expected_issue="${BASH_REMATCH[2]}"
    bash "$LOGGER" "test" "" "auto" >/dev/null 2>&1
    if grep -q "\"issue\": $expected_issue" "$LOG_DIR/issue-$expected_issue.jsonl" 2>/dev/null; then
        echo -e "${GREEN}✓${NC} Auto-detect from current branch"
        ((pass++))
    else
        echo -e "${RED}✗${NC} Auto-detect from current branch"
        ((fail++))
    fi
    rm -f "$LOG_DIR/issue-$expected_issue.jsonl"
else
    echo -e "${GREEN}~${NC} Auto-detect (skipped - not on feat/issue-N branch)"
fi

# Test 3: Notes field
bash "$LOGGER" "test" "789" "pr_number=123" >/dev/null 2>&1
if grep -q '"notes": "pr_number=123"' "$LOG_DIR/issue-789.jsonl" 2>/dev/null; then
    echo -e "${GREEN}✓${NC} Notes field included"
    ((pass++))
else
    echo -e "${RED}✗${NC} Notes field included"
    ((fail++))
fi
rm -f "$LOG_DIR/issue-789.jsonl"

# Test 4: No notes field
bash "$LOGGER" "test" "999" >/dev/null 2>&1
if ! grep -q '"notes":' "$LOG_DIR/issue-999.jsonl" 2>/dev/null; then
    echo -e "${GREEN}✓${NC} Notes field omitted"
    ((pass++))
else
    echo -e "${RED}✗${NC} Notes field omitted"
    ((fail++))
fi
rm -f "$LOG_DIR/issue-999.jsonl"

echo ""
echo "Passed: $pass, Failed: $fail"
[[ $fail -eq 0 ]]
