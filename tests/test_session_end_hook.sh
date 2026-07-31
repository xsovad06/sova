#!/usr/bin/env bash
# Tests for the session_end benchmark hook
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
HOOK="$PROJECT_ROOT/.claude/benchmark/session_end_hook.sh"

# Constants matching the hook
readonly DATA_SOURCE_FALLBACK="fallback"
readonly DATA_SOURCE_TRANSCRIPT="transcript"
readonly DATA_SOURCE_PARTIAL="partial"
readonly MODEL_UNKNOWN="unknown"

GREEN='\033[0;32m'
RED='\033[0;31m'
NC='\033[0m'

TEST_DIR=$(mktemp -d)
export LOG_DIR="$TEST_DIR"

pass=0
fail=0

cleanup() {
    rm -rf "$TEST_DIR"
}
trap cleanup EXIT

assert_pass() {
    local label="$1"
    echo -e "${GREEN}pass${NC} $label"
    pass=$((pass + 1))
}

assert_fail() {
    local label="$1"
    echo -e "${RED}FAIL${NC} $label"
    fail=$((fail + 1))
}

# Helper: get session_end line from log and parse with jq
get_session_end() {
    local log_file="$1"
    grep '"session_end"' "$log_file" 2>/dev/null | tail -1 || true
}

# ── Setup: Create temporary git fixture with issue branch ────
# Create a minimal git repo as a test fixture to avoid depending on the
# current checkout state (which may be detached HEAD in CI or non-issue branch).
GIT_FIXTURE="$TEST_DIR/git-fixture"
mkdir -p "$GIT_FIXTURE"
cd "$GIT_FIXTURE"
git init -q
git config user.email "test@example.com"
git config user.name "Test User"
echo "test" > README.md
git add README.md
git commit -q -m "Initial commit"
git checkout -q -b feat/issue-42

# ── Test 1: No-op when no log file exists ─────────────────────
# Even on an issue branch, if no benchmark log exists, hook is a no-op.
cd "$TEST_DIR"
if echo '{"session_id":"s1","transcript_path":"","cwd":"'"$GIT_FIXTURE"'"}' \
    | bash "$HOOK" 2>/dev/null; then
    assert_pass "No-op exits successfully when no log exists"
else
    assert_fail "No-op exits successfully when no log exists (exit code: $?)"
fi

if [ ! -f "$TEST_DIR/issue-42.jsonl" ]; then
    assert_pass "No-op when no log created"
else
    assert_fail "No-op when no log created"
fi

# ── Test 2: No-op on non-issue branch ─────────────────────────
cd "$GIT_FIXTURE"
git checkout -q -b feature/something-else
cd "$TEST_DIR"

if echo '{"session_id":"s2","transcript_path":"","cwd":"'"$GIT_FIXTURE"'"}' \
    | bash "$HOOK" 2>/dev/null; then
    assert_pass "No-op exits successfully on non-issue branch"
else
    assert_fail "No-op exits successfully on non-issue branch (exit code: $?)"
fi

if [ ! -f "$TEST_DIR/issue-null.jsonl" ]; then
    assert_pass "No-op when non-issue branch creates no log"
else
    assert_fail "No-op when non-issue branch creates no log"
fi

# Switch back to issue branch for remaining tests
cd "$GIT_FIXTURE"
git checkout -q feat/issue-42
cd "$TEST_DIR"

issue=42
log_file="$TEST_DIR/issue-${issue}.jsonl"

# ── Test 3: No-op when benchmark log doesn't exist ────────────
if echo '{"session_id":"s3","transcript_path":"","cwd":"'"$GIT_FIXTURE"'"}' \
    | bash "$HOOK" 2>/dev/null; then
    assert_pass "No-op exits successfully when benchmark log missing"
else
    assert_fail "No-op exits successfully when benchmark log missing (exit code: $?)"
fi

if [ ! -f "$log_file" ]; then
    assert_pass "No-op when benchmark log missing"
else
    assert_fail "No-op when benchmark log missing"
fi

# ── Test 4: Appends session_end with fallback data ────────────
echo '{"ts":"2026-07-31T10:00:00Z","event":"develop_start","issue":'"$issue"'}' \
    > "$log_file"

echo '{"session_id":"s4","transcript_path":"","cwd":"'"$GIT_FIXTURE"'"}' \
    | bash "$HOOK" 2>/dev/null

session_line="$(get_session_end "$log_file")"
if [ -n "$session_line" ]; then
    assert_pass "Appends session_end event"
else
    assert_fail "Appends session_end event"
fi

if echo "$session_line" | jq -e '.data_source == "fallback"' >/dev/null 2>&1; then
    assert_pass "data_source is fallback when no transcript"
else
    assert_fail "data_source is fallback when no transcript"
fi

if echo "$session_line" | jq -e '.model == "unknown"' >/dev/null 2>&1; then
    assert_pass "model is unknown when no transcript"
else
    assert_fail "model is unknown when no transcript"
fi

# ── Test 5: Parses transcript for tokens and model ────────────
echo '{"ts":"2026-07-31T10:00:00Z","event":"develop_start","issue":'"$issue"'}' \
    > "$log_file"

TRANSCRIPT="$TEST_DIR/transcript.jsonl"
cat > "$TRANSCRIPT" <<'JSONL'
{"type":"human","timestamp":"2026-07-31T10:00:00.000Z","message":"hello"}
{"type":"assistant","timestamp":"2026-07-31T10:05:00.000Z","model":"claude-sonnet-4-20250514","usage":{"input_tokens":1000,"output_tokens":500,"cache_read_input_tokens":200,"cache_creation_input_tokens":100}}
{"type":"assistant","timestamp":"2026-07-31T10:10:00.000Z","model":"claude-sonnet-4-20250514","usage":{"input_tokens":2000,"output_tokens":800,"cache_read_input_tokens":300,"cache_creation_input_tokens":0}}
JSONL

echo '{"session_id":"s5","transcript_path":"'"$TRANSCRIPT"'","cwd":"'"$GIT_FIXTURE"'"}' \
    | bash "$HOOK" 2>/dev/null

session_line="$(get_session_end "$log_file")"

if echo "$session_line" | jq -e '.model == "claude-sonnet-4-20250514"' >/dev/null 2>&1; then
    assert_pass "Model extracted from transcript"
else
    assert_fail "Model extracted from transcript"
fi

# Token totals: input 1000+2000=3000, output 500+800=1300, total=4300
if echo "$session_line" | jq -e '.total_tokens == 4300' >/dev/null 2>&1; then
    assert_pass "Total tokens computed correctly"
else
    assert_fail "Total tokens computed correctly"
    echo "  Got: $(echo "$session_line" | jq '{total_tokens, tokens}')" >&2
fi

if echo "$session_line" | jq -e '.data_source == "transcript"' >/dev/null 2>&1; then
    assert_pass "data_source is transcript"
else
    assert_fail "data_source is transcript"
fi

if echo "$session_line" | jq -e '.cost_usd != null and .cost_usd > 0' >/dev/null 2>&1; then
    assert_pass "cost_usd computed (non-null)"
else
    assert_fail "cost_usd computed (non-null)"
fi

# cache_read: 200+300=500, cache_write: 100+0=100
if echo "$session_line" | jq -e '
    .tokens.input == 3000 and .tokens.output == 1300 and
    .tokens.cache_read == 500 and .tokens.cache_write == 100
' >/dev/null 2>&1; then
    assert_pass "Individual token fields correct"
else
    assert_fail "Individual token fields correct"
    echo "  tokens: $(echo "$session_line" | jq '.tokens')" >&2
fi

# ── Test 6: Cost computation accuracy (Opus) ──────────────────
echo '{"ts":"2026-07-31T10:00:00Z","event":"develop_start","issue":'"$issue"'}' \
    > "$log_file"

TRANSCRIPT2="$TEST_DIR/transcript2.jsonl"
cat > "$TRANSCRIPT2" <<'JSONL'
{"type":"assistant","timestamp":"2026-07-31T10:00:00.000Z","model":"claude-opus-4-20250514","usage":{"input_tokens":1000000,"output_tokens":100000,"cache_read_input_tokens":0,"cache_creation_input_tokens":0}}
JSONL

echo '{"session_id":"s6","transcript_path":"'"$TRANSCRIPT2"'","cwd":"'"$GIT_FIXTURE"'"}' \
    | bash "$HOOK" 2>/dev/null

session_line="$(get_session_end "$log_file")"

# Opus: 1M input * $15/M + 100K output * $75/M = $15 + $7.5 = $22.50
expected_cost="22.500000"
actual_cost=$(echo "$session_line" | jq -r '.cost_usd')
if [ "$actual_cost" = "$expected_cost" ]; then
    assert_pass "Opus cost computation accurate ($actual_cost)"
else
    assert_fail "Opus cost computation (expected $expected_cost, got $actual_cost)"
fi

# ── Test 7: Duration computation ──────────────────────────────
echo '{"ts":"2026-07-31T10:00:00Z","event":"develop_start","issue":'"$issue"'}' \
    > "$log_file"

TRANSCRIPT3="$TEST_DIR/transcript3.jsonl"
cat > "$TRANSCRIPT3" <<'JSONL'
{"type":"human","timestamp":"2026-07-31T10:00:00.000Z","message":"start"}
{"type":"assistant","timestamp":"2026-07-31T10:30:00.000Z","model":"claude-sonnet-4-20250514","usage":{"input_tokens":100,"output_tokens":50,"cache_read_input_tokens":0,"cache_creation_input_tokens":0}}
JSONL

echo '{"session_id":"s7","transcript_path":"'"$TRANSCRIPT3"'","cwd":"'"$GIT_FIXTURE"'"}' \
    | bash "$HOOK" 2>/dev/null

session_line="$(get_session_end "$log_file")"

# 30 minutes = 1800 seconds
actual_duration=$(echo "$session_line" | jq -r '.duration_seconds')
if [ "$actual_duration" = "1800" ]; then
    assert_pass "Duration computed correctly (1800s)"
else
    assert_fail "Duration computation (expected 1800, got $actual_duration)"
fi

# ── Test 8: Session ID captured ───────────────────────────────
if echo "$session_line" | jq -e '.session_id == "s7"' >/dev/null 2>&1; then
    assert_pass "Session ID captured from payload"
else
    assert_fail "Session ID captured from payload"
fi

# ── Test 9: Unknown model yields null cost ────────────────────
echo '{"ts":"2026-07-31T10:00:00Z","event":"develop_start","issue":'"$issue"'}' \
    > "$log_file"

TRANSCRIPT4="$TEST_DIR/transcript4.jsonl"
cat > "$TRANSCRIPT4" <<'JSONL'
{"type":"assistant","timestamp":"2026-07-31T10:00:00.000Z","model":"some-unknown-model","usage":{"input_tokens":100,"output_tokens":50,"cache_read_input_tokens":0,"cache_creation_input_tokens":0}}
JSONL

echo '{"session_id":"s8","transcript_path":"'"$TRANSCRIPT4"'","cwd":"'"$GIT_FIXTURE"'"}' \
    | bash "$HOOK" 2>/dev/null

session_line="$(get_session_end "$log_file")"
if echo "$session_line" | jq -e '.cost_usd == null' >/dev/null 2>&1; then
    assert_pass "Unknown model yields null cost"
else
    assert_fail "Unknown model yields null cost"
fi

# ── Test 10: Partial transcript (events but no usage) ─────────
echo '{"ts":"2026-07-31T10:00:00Z","event":"develop_start","issue":'"$issue"'}' \
    > "$log_file"

TRANSCRIPT5="$TEST_DIR/transcript5.jsonl"
cat > "$TRANSCRIPT5" <<'JSONL'
{"type":"human","timestamp":"2026-07-31T10:00:00.000Z","message":"hello"}
{"type":"assistant","timestamp":"2026-07-31T10:05:00.000Z","content":"response"}
JSONL

echo '{"session_id":"s9","transcript_path":"'"$TRANSCRIPT5"'","cwd":"'"$GIT_FIXTURE"'"}' \
    | bash "$HOOK" 2>/dev/null

session_line="$(get_session_end "$log_file")"
if echo "$session_line" | jq -e '.data_source == "partial"' >/dev/null 2>&1; then
    assert_pass "Partial transcript yields partial data_source"
else
    assert_fail "Partial transcript yields partial data_source"
fi

if echo "$session_line" | jq -e '.model == "unknown"' >/dev/null 2>&1; then
    assert_pass "Partial transcript yields unknown model"
else
    assert_fail "Partial transcript yields unknown model"
fi

# ── Test 11: Sonnet cost includes cache costs ─────────────────
echo '{"ts":"2026-07-31T10:00:00Z","event":"develop_start","issue":'"$issue"'}' \
    > "$log_file"

TRANSCRIPT6="$TEST_DIR/transcript6.jsonl"
# Sonnet: input 10K @ $3/M, output 5K @ $15/M, cache_read 2K @ $0.30/M, cache_write 1K @ $3.75/M
# = (10000*3 + 5000*15 + 2000*0.30 + 1000*3.75) / 1000000 = (30000 + 75000 + 600 + 3750) / 1000000 = 0.10935
cat > "$TRANSCRIPT6" <<'JSONL'
{"type":"assistant","timestamp":"2026-07-31T10:00:00.000Z","model":"claude-sonnet-4-20250514","usage":{"input_tokens":10000,"output_tokens":5000,"cache_read_input_tokens":2000,"cache_creation_input_tokens":1000}}
JSONL

echo '{"session_id":"s10","transcript_path":"'"$TRANSCRIPT6"'","cwd":"'"$GIT_FIXTURE"'"}' \
    | bash "$HOOK" 2>/dev/null

session_line="$(get_session_end "$log_file")"
expected_cost="0.109350"
actual_cost=$(echo "$session_line" | jq -r '.cost_usd')
if [ "$actual_cost" = "$expected_cost" ]; then
    assert_pass "Sonnet cost includes cache costs ($actual_cost)"
else
    assert_fail "Sonnet cost includes cache costs (expected $expected_cost, got $actual_cost)"
fi

# ── Test 12: Malformed transcript with non-numeric token values ─
echo '{"ts":"2026-07-31T10:00:00Z","event":"develop_start","issue":'"$issue"'}' \
    > "$log_file"

TRANSCRIPT7="$TEST_DIR/transcript7.jsonl"
cat > "$TRANSCRIPT7" <<'JSONL'
{"type":"assistant","timestamp":"2026-07-31T10:00:00.000Z","model":"claude-sonnet-4-20250514","usage":{"input_tokens":"not-a-number","output_tokens":500,"cache_read_input_tokens":"invalid","cache_creation_input_tokens":0}}
JSONL

echo '{"session_id":"s11","transcript_path":"'"$TRANSCRIPT7"'","cwd":"'"$GIT_FIXTURE"'"}' \
    | bash "$HOOK" 2>/dev/null

session_line="$(get_session_end "$log_file")"
if echo "$session_line" | jq -e '.total_tokens == 500' >/dev/null 2>&1; then
    assert_pass "Malformed tokens gracefully handled (total=500)"
else
    assert_fail "Malformed tokens gracefully handled"
    echo "  Got total_tokens: $(echo "$session_line" | jq '.total_tokens')" >&2
fi

if echo "$session_line" | jq -e '.tokens.input == 0 and .tokens.output == 500' >/dev/null 2>&1; then
    assert_pass "Non-numeric input_tokens defaults to 0"
else
    assert_fail "Non-numeric input_tokens defaults to 0"
fi

# ── Test 13: Transcript with missing/null usage fields ────────
echo '{"ts":"2026-07-31T10:00:00Z","event":"develop_start","issue":'"$issue"'}' \
    > "$log_file"

TRANSCRIPT8="$TEST_DIR/transcript8.jsonl"
cat > "$TRANSCRIPT8" <<'JSONL'
{"type":"assistant","timestamp":"2026-07-31T10:00:00.000Z","model":"claude-sonnet-4-20250514","usage":null}
{"type":"assistant","timestamp":"2026-07-31T10:05:00.000Z","model":"claude-sonnet-4-20250514"}
JSONL

echo '{"session_id":"s12","transcript_path":"'"$TRANSCRIPT8"'","cwd":"'"$GIT_FIXTURE"'"}' \
    | bash "$HOOK" 2>/dev/null

session_line="$(get_session_end "$log_file")"
if echo "$session_line" | jq -e '.total_tokens == 0' >/dev/null 2>&1; then
    assert_pass "Missing usage fields default to 0"
else
    assert_fail "Missing usage fields default to 0"
    echo "  Got total_tokens: $(echo "$session_line" | jq '.total_tokens')" >&2
fi

if echo "$session_line" | jq -e '.data_source == "partial"' >/dev/null 2>&1; then
    assert_pass "Null usage yields partial data_source"
else
    assert_fail "Null usage yields partial data_source"
fi

# ── Test 14: Corrupted JSON that jq cannot parse ──────────────
echo '{"ts":"2026-07-31T10:00:00Z","event":"develop_start","issue":'"$issue"'}' \
    > "$log_file"

TRANSCRIPT9="$TEST_DIR/transcript9.jsonl"
cat > "$TRANSCRIPT9" <<'JSONL'
{"type":"assistant","timestamp":"2026-07-31T10:00:00.000Z","model":"claude-sonnet-4
JSONL

echo '{"session_id":"s13","transcript_path":"'"$TRANSCRIPT9"'","cwd":"'"$GIT_FIXTURE"'"}' \
    | bash "$HOOK" 2>/dev/null

session_line="$(get_session_end "$log_file")"
if echo "$session_line" | jq -e '.data_source == "fallback"' >/dev/null 2>&1; then
    assert_pass "Corrupted JSON falls back to fallback data_source"
else
    assert_fail "Corrupted JSON falls back to fallback data_source"
fi

if echo "$session_line" | jq -e '.total_tokens == 0' >/dev/null 2>&1; then
    assert_pass "Corrupted JSON produces zero tokens"
else
    assert_fail "Corrupted JSON produces zero tokens"
fi

# ── Test 15: Mixed valid/invalid fields extracts what's valid ─
echo '{"ts":"2026-07-31T10:00:00Z","event":"develop_start","issue":'"$issue"'}' \
    > "$log_file"

TRANSCRIPT10="$TEST_DIR/transcript10.jsonl"
cat > "$TRANSCRIPT10" <<'JSONL'
{"type":"assistant","timestamp":"2026-07-31T10:00:00.000Z","model":"claude-sonnet-4-20250514","usage":{"input_tokens":1000,"output_tokens":"invalid","cache_read_input_tokens":null,"cache_creation_input_tokens":50}}
JSONL

echo '{"session_id":"s14","transcript_path":"'"$TRANSCRIPT10"'","cwd":"'"$GIT_FIXTURE"'"}' \
    | bash "$HOOK" 2>/dev/null

session_line="$(get_session_end "$log_file")"
if echo "$session_line" | jq -e '.total_tokens == 1000' >/dev/null 2>&1; then
    assert_pass "Mixed valid/invalid fields: valid fields extracted"
else
    assert_fail "Mixed valid/invalid fields: valid fields extracted"
    echo "  Got total_tokens: $(echo "$session_line" | jq '.total_tokens')" >&2
fi

if echo "$session_line" | jq -e '.tokens.input == 1000 and .tokens.output == 0 and .tokens.cache_write == 50' >/dev/null 2>&1; then
    assert_pass "Mixed fields: input=1000, output=0, cache_write=50"
else
    assert_fail "Mixed fields: input=1000, output=0, cache_write=50"
    echo "  Got tokens: $(echo "$session_line" | jq '.tokens')" >&2
fi

echo ""
echo "Passed: $pass, Failed: $fail"
[[ $fail -eq 0 ]]
