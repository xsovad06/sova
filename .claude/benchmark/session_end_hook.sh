#!/usr/bin/env bash
set -euo pipefail

# Claude Code Stop hook: captures session metrics and appends to benchmark log.
# Receives JSON payload via stdin with session_id, transcript_path, cwd, etc.
# Graceful no-op when not on a benchmark branch or no log exists.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Branch name regex pattern (must match log.sh: GitHub issues only)
readonly ISSUE_BRANCH_PATTERN='^(feat|fix|refactor|chore|test|docs)/issue-([0-9]+)'

# Data source constants
readonly DATA_SOURCE_FALLBACK="fallback"
readonly DATA_SOURCE_TRANSCRIPT="transcript"
readonly DATA_SOURCE_PARTIAL="partial"
readonly MODEL_UNKNOWN="unknown"

# Model pricing (USD per token, Anthropic public pricing as of 2025)
# Format: model_prefix:input_rate:output_rate:cache_read_rate:cache_write_rate
readonly -a MODEL_PRICING=(
    "claude-opus-4:15.00:75.00:1.50:18.75"
    "claude-sonnet-4:3.00:15.00:0.30:3.75"
    "claude-haiku-4:0.80:4.00:0.08:1.00"
)

main() {
    local hook_payload
    hook_payload="$(cat)"

    # Parse hook payload (individual jq calls for reliable empty-field handling)
    # Note: @tsv with IFS read fails on leading empty fields (bash skips leading delimiters)
    local transcript_path session_id cwd
    transcript_path="$(printf '%s' "$hook_payload" | jq -r '.transcript_path // ""')" || true
    session_id="$(printf '%s' "$hook_payload" | jq -r '.session_id // ""')" || true
    cwd="$(printf '%s' "$hook_payload" | jq -r '.cwd // ""')" || true

    # Use cwd from payload, fallback to SCRIPT_DIR-based detection
    local project_dir
    if [ -n "$cwd" ]; then
        project_dir="$cwd"
    else
        project_dir="$(cd "$SCRIPT_DIR/../.." && pwd)"
    fi

    # Detect issue number from branch name
    local branch_name issue
    branch_name="$(git -C "$project_dir" branch --show-current 2>/dev/null || echo "")"
    issue=""
    if [[ "$branch_name" =~ $ISSUE_BRANCH_PATTERN ]]; then
        issue="${BASH_REMATCH[2]}"
    fi

    # No issue detected: not a benchmark session, silent no-op
    if [ -z "$issue" ]; then
        exit 0
    fi

    # Check if benchmark log exists for this issue (only append, never create)
    local log_dir="${LOG_DIR:-$SCRIPT_DIR}"
    local log_file="${log_dir}/issue-${issue}.jsonl"
    if [ ! -f "$log_file" ]; then
        exit 0
    fi

    # Parse transcript for metrics (single jq invocation for efficiency)
    # NOTE: transcript_path from hook payload is trusted (Claude Code provides it).
    # Hook runs in user's environment with user permissions, not a security boundary.
    local model="" input_tokens=0 output_tokens=0 cache_read=0 cache_write=0
    local first_ts="" last_ts="" data_source="$DATA_SOURCE_FALLBACK"

    if [ -n "$transcript_path" ] && [ -f "$transcript_path" ]; then
        # Single pass extraction: model, aggregated tokens, and timestamps
        local transcript_data
        transcript_data="$(jq -s '{
            model: (map(select(.model != null) | .model) | first),
            usage: ([.[] | select(.usage != null) | .usage] | {
                input: (map(.input_tokens // 0) | add // 0),
                output: (map(.output_tokens // 0) | add // 0),
                cache_read: (map(.cache_read_input_tokens // 0) | add // 0),
                cache_write: (map(.cache_creation_input_tokens // 0) | add // 0)
            }),
            first_ts: (map(select(.timestamp != null) | .timestamp) | first),
            last_ts: (map(select(.timestamp != null) | .timestamp) | last)
        }' "$transcript_path" 2>/dev/null)" || true

        if [ -n "$transcript_data" ] && [ "$transcript_data" != "null" ]; then
            model="$(printf '%s' "$transcript_data" | jq -r '.model // ""')" || true
            # Ensure numeric output even if transcript contains non-numeric values
            input_tokens="$(printf '%s' "$transcript_data" | jq -r '(.usage.input // 0) | if type == "number" then . else 0 end')" || input_tokens=0
            output_tokens="$(printf '%s' "$transcript_data" | jq -r '(.usage.output // 0) | if type == "number" then . else 0 end')" || output_tokens=0
            cache_read="$(printf '%s' "$transcript_data" | jq -r '(.usage.cache_read // 0) | if type == "number" then . else 0 end')" || cache_read=0
            cache_write="$(printf '%s' "$transcript_data" | jq -r '(.usage.cache_write // 0) | if type == "number" then . else 0 end')" || cache_write=0
            first_ts="$(printf '%s' "$transcript_data" | jq -r '.first_ts // ""')" || true
            last_ts="$(printf '%s' "$transcript_data" | jq -r '.last_ts // ""')" || true

            # Safe numeric comparison with regex guard
            # Require both model and token data for "transcript" status
            if [ -n "$model" ] && [[ "$input_tokens" =~ ^[0-9]+$ ]] && [ "$input_tokens" -gt 0 ]; then
                data_source="$DATA_SOURCE_TRANSCRIPT"
            elif [ -n "$model" ] || [ -n "$first_ts" ]; then
                # Model without tokens, or timestamps without model = partial data
                data_source="$DATA_SOURCE_PARTIAL"
            fi
        fi
    fi

    # Compute duration in seconds
    # Date parsing: tries macOS format (-jf) then Linux format (-d)
    # Falls back to null duration on unsupported platforms or parse errors
    local duration_seconds="null"
    if [ -n "$first_ts" ] && [ -n "$last_ts" ]; then
        local first_epoch last_epoch
        first_epoch="$(date -jf "%Y-%m-%dT%H:%M:%S" "${first_ts%%.*}" "+%s" 2>/dev/null || date -d "$first_ts" "+%s" 2>/dev/null)" || true
        last_epoch="$(date -jf "%Y-%m-%dT%H:%M:%S" "${last_ts%%.*}" "+%s" 2>/dev/null || date -d "$last_ts" "+%s" 2>/dev/null)" || true
        if [ -n "$first_epoch" ] && [ -n "$last_epoch" ]; then
            duration_seconds=$((last_epoch - first_epoch))
        fi
    fi

    # Compute cost based on model pricing
    local cost_usd="null"
    if [ -n "$model" ] && [[ "$input_tokens" =~ ^[0-9]+$ ]] && [ "$input_tokens" -gt 0 ]; then
        cost_usd="$(compute_cost "$model" "$input_tokens" "$output_tokens" "$cache_read" "$cache_write")"
    fi

    # Fallback model from hook payload or default
    if [ -z "$model" ]; then
        model="$MODEL_UNKNOWN"
    fi

    # Build the session_end JSON entry
    local ts
    ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

    # Safe arithmetic: validate numeric values before expansion
    local total_tokens=0
    if [[ "$input_tokens" =~ ^[0-9]+$ ]] && [[ "$output_tokens" =~ ^[0-9]+$ ]]; then
        total_tokens=$((input_tokens + output_tokens))
    fi

    local json_output
    json_output="$(jq -cn \
        --arg ts "$ts" \
        --arg event "session_end" \
        --argjson issue "$issue" \
        --arg model "$model" \
        --arg session_id "${session_id:-unknown}" \
        --argjson duration_seconds "$duration_seconds" \
        --argjson total_tokens "$total_tokens" \
        --argjson cost_usd "$cost_usd" \
        --arg data_source "$data_source" \
        --argjson input_tokens "$input_tokens" \
        --argjson output_tokens "$output_tokens" \
        --argjson cache_read "$cache_read" \
        --argjson cache_write "$cache_write" \
        '{
            ts: $ts,
            event: $event,
            issue: $issue,
            model: $model,
            session_id: $session_id,
            duration_seconds: $duration_seconds,
            total_tokens: $total_tokens,
            cost_usd: $cost_usd,
            data_source: $data_source,
            tokens: {
                input: $input_tokens,
                output: $output_tokens,
                cache_read: $cache_read,
                cache_write: $cache_write
            }
        }'
    )"

    printf '%s\n' "$json_output" >> "$log_file"
}

compute_cost() {
    local model="$1"
    local input="$2"
    local output="$3"
    local c_read="$4"
    local c_write="$5"

    local input_rate="" output_rate="" cache_read_rate="" cache_write_rate=""

    for pricing in "${MODEL_PRICING[@]}"; do
        local prefix
        prefix="${pricing%%:*}"
        if [[ "$model" == "$prefix"* ]]; then
            IFS=':' read -r _ input_rate output_rate cache_read_rate cache_write_rate <<< "$pricing"
            break
        fi
    done

    if [ -z "$input_rate" ]; then
        echo "null"
        return
    fi

    # Rates are per million tokens; compute cost
    # Use awk for floating point arithmetic
    awk -v input="$input" -v output="$output" \
        -v c_read="$c_read" -v c_write="$c_write" \
        -v ir="$input_rate" -v or="$output_rate" \
        -v crr="$cache_read_rate" -v cwr="$cache_write_rate" \
        'BEGIN {
            cost = (input * ir + output * or + c_read * crr + c_write * cwr) / 1000000
            printf "%.6f", cost
        }'
}

main
