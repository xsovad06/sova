#!/usr/bin/env bash
set -euo pipefail

# GWYM Agent — Autonomous development workflow for GWYM (Grow With Your Money)
# Usage:
#   ./gwym-agent.sh                        # Full workflow (task selection -> PR -> monitor)
#   ./gwym-agent.sh 42                     # Work on specific GitHub issue #42
#   ./gwym-agent.sh --address-pr 42        # Address PR review comments, fix, push
#   ./gwym-agent.sh --maintain-pr 42       # Rebase PR on main + sync description with changes
#   ./gwym-agent.sh --learn-from-pr 42     # Ingest PR review feedback into memory
#   ./gwym-agent.sh --review-pr 42         # Run Koda (automated reviewer) on a PR
#   ./gwym-agent.sh --investigate 42       # Run investigation mode on an issue
#   ./gwym-agent.sh --investigate 42 --doc # Investigation + Google Doc creation
#   ./gwym-agent.sh --watch                # Continuous mode (loop with priority scanner)
#   ./gwym-agent.sh --parallel 42 45       # Run agent on multiple issues concurrently
#   ./gwym-agent.sh --parallel             # Auto-select issues for parallel execution
#   ./gwym-agent.sh --memory search <q>    # Search agent memory (full-text)
#   ./gwym-agent.sh --memory prune         # Remove stale memories (>90 days, closed issues)
#   ./gwym-agent.sh --readiness            # Assess + improve repo's AI-development readiness
#   ./gwym-agent.sh --status               # Show status dashboard
#   ./gwym-agent.sh --cleanup              # Remove stale worktrees (interactive)
#   ./gwym-agent.sh --costs                # Show cost tracking summary

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}" 2>/dev/null || echo "${BASH_SOURCE[0]}")")" && pwd)"
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
CONF_FILE="$REPO_ROOT/.claude/scripts/gwym-agent.conf"
WORKTREE_BASE="$REPO_ROOT/.claude/worktrees"
MEMORY_DIR="$REPO_ROOT/.claude/agent-memory"
mkdir -p "$MEMORY_DIR"

# ─── Load config ───────────────────────────────────────────────────────────────

if [[ ! -f "$CONF_FILE" ]]; then
  cp "$SCRIPT_DIR/gwym-agent.conf.default" "$CONF_FILE" 2>/dev/null || true
fi

# Defaults (overridden by conf file)
GITHUB_REPO="xsovad06/Income_processor"
GITHUB_USER="xsovad06"
TEST_CMD="make test"
LINT_CMD="make lint"
FORMAT_CMD="make format"
BASE_BRANCH="main"
ISSUE_MILESTONE=""
ISSUE_LABELS=""
AGENT_MODEL="opus"
MAX_BUDGET="10.00"
CI_POLL_INTERVAL=60
CI_MAX_WAIT=600
FLAKY_CHECKS=""
WORKTREE_COPY_FILES=".env,.env.local"
NO_AI_COAUTHOR="true"
COMMIT_AUTHOR=""
DEV_SERVER_PORT=8002
WORKTREE_TTL_DONE_DAYS=3
WORKTREE_TTL_PAUSED_DAYS=7
SKIP_MANUAL_TEST="true"
REVIEW_ENABLED="true"
REVIEW_MAX_ROUNDS=2
SHARED_KNOWLEDGE_DIR="$HOME/.claude/shared-knowledge"
INVARIANTS_DIR=""
MAX_PARALLEL_AGENTS=2
SCANNER_GITHUB_CHECK="true"
WATCH_INTERVAL_ACTIVE=300
WATCH_INTERVAL_IDLE=1800
WATCH_AUTO_SELECT_ISSUES="true"
WATCH_VETO_SECONDS=30
PERSONA_MAP=""

# shellcheck source=/dev/null
[[ -f "$CONF_FILE" ]] && source "$CONF_FILE"

# ─── Task context globals (set -u safe) ──────────────────────────────────────

BRANCH_NAME=""
PR_NUMBER=""
SESSION_ID=""
WORKTREE_DIR=""
TICKET_DETAILS=""

# ─── Dashboard Control Mode ──────────────────────────────────────────────────

DASHBOARD_MODE=false
CONTROL_DIR="$REPO_ROOT/.claude/agent-control"
DASHBOARD_RESPONSE=""
DASHBOARD_RESPONSE_VALUE=""

dashboard_init() {
  mkdir -p "$CONTROL_DIR"
  echo $$ > "$CONTROL_DIR/agent.pid"
  dashboard_write_status "running" ""
  rm -f "$CONTROL_DIR/request.json" "$CONTROL_DIR/response.json"
  trap 'dashboard_cleanup' EXIT
}

dashboard_cleanup() {
  rm -f "$CONTROL_DIR/agent.pid" "$CONTROL_DIR/request.json" "$CONTROL_DIR/response.json" "$CONTROL_DIR/notifications.jsonl"
  dashboard_write_status "idle" ""
}

dashboard_write_status() {
  local status="$1"
  local step="${2:-}"
  jq -n \
    --arg status "$status" \
    --arg mode "${SPECIFIC_ISSUE:-workflow}" \
    --arg issue "${ISSUE:-}" \
    --arg step "$step" \
    --arg updated_at "$(date -u +"%Y-%m-%dT%H:%M:%SZ")" \
    '{status:$status,mode:$mode,issue:$issue,step:$step,updated_at:$updated_at}' \
    > "$CONTROL_DIR/status.json"
}

dashboard_request() {
  local req_type="$1"
  local message="$2"
  local options_json="$3"
  local context_json="${4:-{\}}"
  local accepts_text="${5:-false}"

  local req_id
  req_id="req-$(date +%s)-$$"

  # Write request
  cat > "$CONTROL_DIR/request.json" << REQEOF
{"id":"$req_id","timestamp":"$(date -u +"%Y-%m-%dT%H:%M:%SZ")","type":"$req_type","message":$(printf '%s' "$message" | jq -Rs .),"options":$options_json,"context":$context_json,"accepts_text":$accepts_text}
REQEOF

  dashboard_write_status "waiting" "$req_type"
  log_msg INFO dashboard "Waiting for dashboard response: $req_type"

  # Poll for response
  while true; do
    if [[ -f "$CONTROL_DIR/response.json" ]]; then
      local resp_id
      resp_id=$(jq -r '.id // ""' "$CONTROL_DIR/response.json" 2>/dev/null || echo "")
      if [[ "$resp_id" == "$req_id" ]]; then
        DASHBOARD_RESPONSE=$(jq -r '.action // ""' "$CONTROL_DIR/response.json" 2>/dev/null || echo "")
        DASHBOARD_RESPONSE_VALUE=$(jq -r '.value // ""' "$CONTROL_DIR/response.json" 2>/dev/null || echo "")
        rm -f "$CONTROL_DIR/request.json" "$CONTROL_DIR/response.json"
        dashboard_write_status "running" ""
        log_msg INFO dashboard "Received response: action=$DASHBOARD_RESPONSE"
        return 0
      fi
    fi
    sleep 2
  done
}

# ─── Persona Loading ─────────────────────────────────────────────────────────

PERSONAS_DIR="$SCRIPT_DIR/personas"
ACTIVE_PERSONA=""
PERSONA_GUIDANCE=""
PERSONA_MCP_TOOLS=""

_set_persona() {
  local name="$1"
  local persona_file="$PERSONAS_DIR/${name}.md"
  [[ -f "$persona_file" ]] || return 1
  ACTIVE_PERSONA="$name"
  PERSONA_GUIDANCE=$(cat "$persona_file")

  local mcp_file="$PERSONAS_DIR/${name}.mcp.json"
  if [[ -f "$mcp_file" ]]; then
    PERSONA_MCP_TOOLS=$(jq -r '.tools[]' "$mcp_file" 2>/dev/null | paste -sd',' -)
  else
    PERSONA_MCP_TOOLS=""
  fi
  return 0
}

load_persona() {
  local target_dir="${1:-$REPO_ROOT}"
  ACTIVE_PERSONA=""
  PERSONA_GUIDANCE=""
  PERSONA_MCP_TOOLS=""

  # Strategy 1: Check config file for explicit persona mapping
  if [[ -n "${PERSONA_MAP:-}" ]]; then
    local repo_name
    repo_name=$(basename "$target_dir")
    IFS=',' read -ra mappings <<< "$PERSONA_MAP"
    for mapping in "${mappings[@]}"; do
      local map_repo map_persona
      IFS=':' read -r map_repo map_persona <<< "$mapping"
      [[ "$repo_name" == "$map_repo" ]] && _set_persona "$map_persona" && return 0
    done
  fi

  # Strategy 2: Auto-detect from CLAUDE.md
  if [[ -f "$target_dir/CLAUDE.md" ]]; then
    local claude_md
    claude_md=$(cat "$target_dir/CLAUDE.md")
    if echo "$claude_md" | grep -qi "django\|rbac\|permission"; then
      _set_persona "rbac" && return 0
    elif echo "$claude_md" | grep -qi "go\.mod\|golang\|grpc"; then
      _set_persona "go-service" && return 0
    elif echo "$claude_md" | grep -qi "react\|typescript\|patternfly"; then
      _set_persona "frontend" && return 0
    fi
  fi

  # Strategy 3: Detect from file presence
  if [[ -f "$target_dir/go.mod" ]]; then
    _set_persona "go-service" && return 0
  elif [[ -f "$target_dir/package.json" ]]; then
    _set_persona "frontend" && return 0
  elif [[ -f "$target_dir/manage.py" || -f "$target_dir/setup.py" ]]; then
    _set_persona "rbac" && return 0
  fi
}

# ─── Command File Loading ────────────────────────────────────────────────────

_load_command() {
  local name="$1"
  # Prefer target repo's commands (worktree), fall back to main repo, then agent repo
  local cmd_file="${WORKTREE_DIR:+$WORKTREE_DIR/.claude/commands/${name}.md}"
  if [[ -z "$cmd_file" || ! -f "$cmd_file" ]]; then
    cmd_file="$REPO_ROOT/.claude/commands/${name}.md"
  fi
  if [[ ! -f "$cmd_file" ]]; then
    cmd_file="$SCRIPT_DIR/commands/${name}.md"
  fi
  if [[ -f "$cmd_file" ]]; then
    # Strip YAML frontmatter block (lines between first --- pair)
    awk '/^---$/ && !done { count++; if (count == 2) done = 1; next } done' "$cmd_file"
  fi
}

# ─── Structured Logging ───────────────────────────────────────────────────────

LOG_FILE="${MEMORY_DIR}/agent.log"

log_msg() {
  local level="$1"
  local phase="$2"
  shift 2
  local msg="$*"
  local ts
  ts=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
  local line="[$ts] [$level] [$phase] $msg"
  echo "$line" >&2
  echo "$line" >> "$LOG_FILE"
}

# ─── Task State ───────────────────────────────────────────────────────────────

task_state_file() {
  echo "$WORKTREE_BASE/$1/task-state.json"
}

task_state_write() {
  local issue="$1"
  local status="$2"
  local last_step="$3"
  local next_step="${4:-}"
  local state_file
  state_file=$(task_state_file "$issue")
  mkdir -p "$(dirname "$state_file")"

  local now
  now=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

  # Preserve persistent fields from existing file
  local created_at="$now" pr_number_val="" branch_name_val="" paused_reason_val=""
  if [[ -f "$state_file" ]]; then
    read -r created_at pr_number_val branch_name_val paused_reason_val < <(
      jq -r '[(.created_at // ""), (.pr_number // ""), (.branch_name // ""), (.paused_reason // "")] | @tsv' "$state_file" 2>/dev/null
    ) || { created_at="$now"; pr_number_val=""; branch_name_val=""; paused_reason_val=""; }
    [[ -z "$created_at" ]] && created_at="$now"
  fi

  # Clear paused_reason when resuming
  if [[ "$status" != "paused" ]]; then
    paused_reason_val=""
  fi

  local tmp_file
  tmp_file=$(mktemp)
  jq -n \
    --arg issue_number "$issue" \
    --arg status "$status" \
    --arg last_step "$last_step" \
    --arg next_step "$next_step" \
    --arg pr_number "$pr_number_val" \
    --arg branch_name "$branch_name_val" \
    --arg paused_reason "$paused_reason_val" \
    --arg created_at "$created_at" \
    --arg updated_at "$now" \
    '{issue_number: $issue_number, status: $status, last_step: $last_step, next_step: $next_step, pr_number: $pr_number, branch_name: $branch_name, paused_reason: $paused_reason, created_at: $created_at, updated_at: $updated_at}' \
    > "$tmp_file" && mv "$tmp_file" "$state_file"

  log_msg INFO state "Updated task state: #$issue status=$status step=$last_step"
}

task_state_set_field() {
  local issue="$1"
  local field="$2"
  local value="$3"
  local state_file
  state_file=$(task_state_file "$issue")
  if [[ -f "$state_file" ]]; then
    local tmp_file
    tmp_file=$(mktemp)
    # shellcheck disable=SC2064
    trap "rm -f '$tmp_file'" RETURN
    jq --arg f "$field" --arg v "$value" --arg now "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
      '.[$f] = $v | .updated_at = $now' "$state_file" > "$tmp_file" && mv "$tmp_file" "$state_file"
  fi
}

task_state_load_all() {
  log_msg INFO state "Loading task landscape..."
  local found=false
  if [[ -d "$WORKTREE_BASE" ]]; then
    for sf in "$WORKTREE_BASE"/*/task-state.json; do
      [[ -f "$sf" ]] || continue
      found=true
      local tk st ls
      read -r tk st ls < <(jq -r '[.issue_number, .status, .last_step] | @tsv' "$sf" 2>/dev/null) || continue
      log_msg INFO state "  #$tk: status=$st last_step=$ls"
    done
  fi
  if [[ "$found" == "false" ]]; then
    log_msg INFO state "  No active tasks found."
  fi
}

pause_task() {
  local issue="$1"
  local step="$2"
  local reason="$3"
  log_msg WARN pause "Pausing #$issue at $step: $reason"
  task_state_write "$issue" "paused" "$step" "$step"
  task_state_set_field "$issue" "paused_reason" "$reason"
  notify "Issue #$issue paused: $reason"
}

# Returns the current branch in a worktree, or empty string if detached/missing.
worktree_branch() {
  local dir="$1"
  local branch
  branch=$(git -C "$dir" rev-parse --abbrev-ref HEAD 2>/dev/null || true)
  [[ "$branch" == "HEAD" ]] && branch=""
  echo "$branch"
}

restore_task_context() {
  local issue="$1"
  local sf
  sf=$(task_state_file "$issue")
  [[ -f "$sf" ]] || return 1
  WORKTREE_DIR="$WORKTREE_BASE/$issue"
  # Reset before reading to prevent stale values on jq failure
  BRANCH_NAME="" PR_NUMBER="" SESSION_ID=""
  read -r BRANCH_NAME PR_NUMBER SESSION_ID < <(
    jq -r '[(.branch_name // ""), (.pr_number // ""), (.session_id // "")] | @tsv' "$sf" 2>/dev/null
  ) || true
  # Task state from older runs or crashes may lack branch_name -- resolve from git
  if [[ -z "$BRANCH_NAME" && -d "$WORKTREE_DIR" ]]; then
    BRANCH_NAME=$(worktree_branch "$WORKTREE_DIR")
  fi
  [[ -z "$BRANCH_NAME" ]] && BRANCH_NAME="agent/feat/$issue"
  TICKET_DETAILS=""
  TICKET_DETAILS=$(gh issue view "$issue" --json title,body,labels,milestone 2>/dev/null) || true
}

# ─── Priority Scanner ────────────────────────────────────────────────────────

_check_unresolved_threads() {
  local pr_number="$1"
  local repo_owner repo_name
  repo_owner=$(echo "$GITHUB_REPO" | cut -d/ -f1)
  repo_name=$(echo "$GITHUB_REPO" | cut -d/ -f2)

  gh api graphql -f query="
    query {
      repository(owner:\"$repo_owner\", name:\"$repo_name\") {
        pullRequest(number:$pr_number) {
          reviewThreads(first:100) {
            nodes { isResolved comments(first:1) { nodes { body author { login } } } }
          }
        }
      }
    }" 2>/dev/null | jq '[.data.repository.pullRequest.reviewThreads.nodes[] | select(.isResolved == false)] | length' 2>/dev/null || echo "0"
}

priority_scan() {
  local results_file
  results_file=$(mktemp)
  # shellcheck disable=SC2064
  trap "rm -f '$results_file'" RETURN

  # Cache tracked PR numbers from task-state files
  local tracked_prs=""
  if [[ -d "$WORKTREE_BASE" ]]; then
    for sf in "$WORKTREE_BASE"/*/task-state.json; do
      [[ -f "$sf" ]] || continue
      local tk st ls reason pr_val
      read -r tk st ls reason pr_val < <(
        jq -r '[.issue_number, .status, .last_step, (.paused_reason // ""), (.pr_number // "")] | @tsv' "$sf" 2>/dev/null
      ) || continue

      # Source 1: Local task-state files (P0 for in_progress/paused)
      if [[ "$st" != "done" ]]; then
        case "$st" in
          in_progress) echo "P0|RESUME|$tk|Interrupted at $ls" >> "$results_file" ;;
          paused)      echo "P0|PAUSED|$tk|${reason:-Unknown reason}" >> "$results_file" ;;
        esac
      fi

      # Build tracked PR set for Source 2
      [[ -n "$pr_val" ]] && tracked_prs="${tracked_prs}|${pr_val}|"
    done
  fi

  # Source 2: GitHub PRs needing attention (P1)
  if [[ "${SCANNER_GITHUB_CHECK:-true}" == "true" ]]; then
    local open_prs
    open_prs=$(gh pr list --author "@me" --json number,title,headRefName,reviewDecision 2>/dev/null || echo "[]")

    local pr_json pr_num pr_title
    while IFS= read -r pr_json; do
      [[ -z "$pr_json" ]] && continue
      pr_num=$(echo "$pr_json" | jq -r '.number')
      pr_title=$(echo "$pr_json" | jq -r '.title')

      # Skip PRs already tracked
      [[ "$tracked_prs" == *"|${pr_num}|"* ]] && continue

      # Check review status (from cached data, no API call)
      local review_decision
      review_decision=$(echo "$pr_json" | jq -r '.reviewDecision // ""')
      if [[ "$review_decision" == "CHANGES_REQUESTED" ]]; then
        echo "P1|REVIEW|$pr_num|PR #$pr_num ($pr_title) has changes requested" >> "$results_file"
        continue
      fi

      # Check CI status (requires per-PR API call)
      local failing
      failing=$(gh pr checks "$pr_num" --json name,state 2>/dev/null \
        | jq '[.[] | select(.state == "FAILURE")] | length' 2>/dev/null || echo "0")
      if [[ "$failing" -gt 0 ]]; then
        echo "P1|CI_FAIL|$pr_num|PR #$pr_num ($pr_title) has $failing failing checks" >> "$results_file"
        continue
      fi

      # Check for unresolved review threads (GraphQL -- REST API lacks isResolved)
      local unresolved_threads
      unresolved_threads=$(_check_unresolved_threads "$pr_num" 2>/dev/null || echo "0")
      if [[ "$unresolved_threads" -gt 0 ]] 2>/dev/null; then
        echo "P1|COMMENTS|$pr_num|PR #$pr_num ($pr_title) has $unresolved_threads unresolved threads" >> "$results_file"
      fi
    done < <(echo "$open_prs" | jq -c '.[]' 2>/dev/null)
  fi

  # Source 3: GitHub Issues (backlog)
  local issue_args=("--state" "open")
  [[ -n "$ISSUE_MILESTONE" ]] && issue_args+=("--milestone" "$ISSUE_MILESTONE")
  local issue_count
  issue_count=$(gh issue list "${issue_args[@]}" --json number 2>/dev/null | jq 'length' 2>/dev/null || echo "0")
  if [[ "$issue_count" -gt 0 ]]; then
    echo "P2|ISSUES|new|$issue_count issues available in backlog" >> "$results_file"
  fi

  # Output sorted results
  sort "$results_file" 2>/dev/null
}

# ─── Post-Merge Auto-Learn ───────────────────────────────────────────────────

detect_merged_prs() {
  [[ -d "$WORKTREE_BASE" ]] || return 0

  # Batch: get all merged PRs by current user in one API call
  local merged_pr_nums
  merged_pr_nums=$(gh pr list --author "@me" --state merged --json number -q '.[].number' 2>/dev/null || echo "")
  [[ -z "$merged_pr_nums" ]] && return 0

  for sf in "$WORKTREE_BASE"/*/task-state.json; do
    [[ -f "$sf" ]] || continue
    local tk st pr_val branch_val
    read -r tk st pr_val branch_val < <(
      jq -r '[.issue_number, .status, (.pr_number // ""), (.branch_name // "")] | @tsv' "$sf" 2>/dev/null
    ) || continue
    [[ -z "$pr_val" || "$st" == "done" ]] && continue

    # Check against cached merged PR list
    if echo "$merged_pr_nums" | grep -qx "$pr_val"; then
      echo "$tk|$pr_val|$branch_val"
    fi
  done
}

auto_learn_merged() {
  local merged
  merged=$(detect_merged_prs)
  [[ -z "$merged" ]] && return 0

  while IFS='|' read -r issue pr_num branch; do
    [[ -z "$issue" ]] && continue
    [[ -z "$branch" ]] && branch="agent/feat/$issue"
    log_msg INFO auto-learn "PR #$pr_num for #$issue was merged. Running post-merge workflow..."
    notify "PR #$pr_num merged — learning and cleaning up"

    # 1. Learn from PR
    log_msg INFO auto-learn "Ingesting review feedback from PR #$pr_num..."
    _learn_from_pr_core "$pr_num" >/dev/null 2>&1 || {
      log_msg WARN auto-learn "Claude learn session failed for PR #$pr_num"
    }

    # 2. Mark task as done
    task_state_write "$issue" "done" "step9_complete" ""

    # 3. Auto-cleanup worktree
    local wt_dir="$WORKTREE_BASE/$issue"
    if [[ -d "$wt_dir" ]]; then
      cleanup_worktree "$wt_dir" "$branch"
      log_msg INFO auto-learn "Cleaned up worktree for #$issue"
    fi

    # 4. Remove in-progress label
    gh issue edit "$issue" --remove-label "in-progress" 2>/dev/null || true

    log_msg INFO auto-learn "Post-merge workflow complete for #$issue (PR #$pr_num)"
  done <<< "$merged"
}

# Core learn-from-pr logic (shared by interactive and auto-learn paths)
_learn_from_pr_core() {
  local pr_number="$1"
  local pr_data
  pr_data=$(gh pr view "$pr_number" --json comments,reviews,body,title 2>/dev/null) || return 1
  local pr_comments
  pr_comments=$(gh api "repos/$GITHUB_REPO/pulls/$pr_number/comments" 2>/dev/null) || true

  local ingest_guidelines
  ingest_guidelines=$(_load_command "ingest-review")

  local result
  result=$(claude_run_tracked "learn-pr" "PR-$pr_number" "You are the GWYM Agent. You just received review feedback on a PR.

PR data:
$pr_data

Review comments:
$pr_comments

Review ingestion guidelines (from project commands):
${ingest_guidelines:-Extract lessons from the review: patterns to follow, mistakes to avoid, style preferences, test gaps. Update memory files.}

Memory directory: $MEMORY_DIR

Additional instructions:
- Be concise. Only record actionable, specific lessons — not generic advice.
- Also output a MEMORY_ENTRIES section at the end with structured entries to save:
  MEMORY_ENTRY|category|title|tags
  (one per line, category is learning or review_feedback)")
  echo "$result"

  # Track review patterns for future proactive fixes
  track_review_patterns "$pr_number"

  # Parse and save structured memory entries to SQLite
  if command -v sqlite3 &>/dev/null; then
    echo "$result" | grep '^MEMORY_ENTRY|' | while IFS='|' read -r _ category title tags; do
      [[ -z "$title" ]] && continue
      memory_add "$category" "$title" "From PR #$pr_number review" "$tags" "$GITHUB_REPO" ""
    done
  fi
}

# ─── Worktree Auto-Cleanup ──────────────────────────────────────────────────

worktree_auto_cleanup() {
  [[ -d "$WORKTREE_BASE" ]] || return 0
  local now_epoch
  now_epoch=$(date +%s)

  for sf in "$WORKTREE_BASE"/*/task-state.json; do
    [[ -f "$sf" ]] || continue
    local tk st updated_at
    read -r tk st updated_at < <(
      jq -r '[.issue_number, .status, (.updated_at // "")] | @tsv' "$sf" 2>/dev/null
    ) || continue

    [[ -z "$updated_at" ]] && continue

    # Calculate age in days
    local updated_epoch age_days
    updated_epoch=$(date -j -f "%Y-%m-%dT%H:%M:%SZ" "$updated_at" +%s 2>/dev/null || date -d "$updated_at" +%s 2>/dev/null || echo "")
    [[ -z "$updated_epoch" ]] && continue
    age_days=$(( (now_epoch - updated_epoch) / 86400 ))

    local wt_dir="$WORKTREE_BASE/$tk"
    local branch
    branch=$(jq -r '.branch_name // ""' "$sf" 2>/dev/null || echo "agent/feat/$tk")

    case "$st" in
      done)
        if [[ "$age_days" -ge "$WORKTREE_TTL_DONE_DAYS" ]]; then
          log_msg INFO cleanup "Auto-removing done worktree #$tk (${age_days}d old)"
          cleanup_worktree "$wt_dir" "$branch"
        fi
        ;;
      paused)
        if [[ "$age_days" -ge "$WORKTREE_TTL_PAUSED_DAYS" ]]; then
          notify "Paused worktree #$tk is ${age_days}d old — consider removing"
          log_msg WARN cleanup "Paused worktree #$tk is ${age_days}d old (TTL: ${WORKTREE_TTL_PAUSED_DAYS}d)"
        fi
        ;;
      # Never auto-delete in_progress worktrees
    esac
  done
}

# ─── Smarter Memory (SQLite) ─────────────────────────────────────────────────

MEMORY_DB="${MEMORY_DIR}/memory.db"
_MEMORY_DB_INIT=false
_REVIEW_DB_INIT=false

sql_escape() {
  printf '%s' "$1" | sed "s/'/''/g"
}

memory_init_db() {
  [[ "$_MEMORY_DB_INIT" == "true" ]] && return 0
  _MEMORY_DB_INIT=true
  sqlite3 "$MEMORY_DB" <<'SQL'
CREATE TABLE IF NOT EXISTS memories (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  category TEXT NOT NULL CHECK(category IN ('learning', 'review_feedback', 'codebase_pattern')),
  title TEXT NOT NULL,
  content TEXT NOT NULL,
  tags TEXT DEFAULT '',
  repo TEXT DEFAULT '',
  issue_number TEXT DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
  superseded_by INTEGER REFERENCES memories(id)
);
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(title, content, tags, content=memories, content_rowid=id);
CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
  INSERT INTO memories_fts(rowid, title, content, tags) VALUES (new.id, new.title, new.content, new.tags);
END;
CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
  INSERT INTO memories_fts(memories_fts, rowid, title, content, tags) VALUES ('delete', old.id, old.title, old.content, old.tags);
END;
CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
  INSERT INTO memories_fts(memories_fts, rowid, title, content, tags) VALUES ('delete', old.id, old.title, old.content, old.tags);
  INSERT INTO memories_fts(rowid, title, content, tags) VALUES (new.id, new.title, new.content, new.tags);
END;
SQL
}

memory_add() {
  local category="$1" title="$2" content="$3"
  local tags="${4:-}" repo="${5:-}" issue_number="${6:-}"
  memory_init_db
  sqlite3 "$MEMORY_DB" <<SQL
INSERT INTO memories (category, title, content, tags, repo, issue_number)
VALUES ('$(sql_escape "$category")', '$(sql_escape "$title")', '$(sql_escape "$content")', '$(sql_escape "$tags")', '$(sql_escape "$repo")', '$(sql_escape "$issue_number")');
SQL
  log_msg INFO memory "Added memory: [$category] $title"
}

memory_search() {
  local query="$1"
  local limit="${2:-10}"
  memory_init_db
  # Escape FTS5 special characters and wrap as phrase to prevent query injection
  local fts_query
  fts_query=$(printf '%s' "$query" | sed 's/[\"*()^:-]/ /g' | sed "s/'/''/g")
  sqlite3 -header -column "$MEMORY_DB" <<SQL
SELECT m.id, m.category, m.title, substr(m.content, 1, 120) AS content_preview, m.tags, m.issue_number, m.created_at
FROM memories m
JOIN memories_fts f ON m.id = f.rowid
WHERE memories_fts MATCH '"${fts_query}"'
  AND m.superseded_by IS NULL
ORDER BY rank
LIMIT $limit;
SQL
}

memory_search_by_tags() {
  local tags="$1"
  local limit="${2:-10}"
  memory_init_db
  # tags is comma-separated; match any
  local where_clause=""
  IFS=',' read -ra tag_array <<< "$tags"
  for tag in "${tag_array[@]}"; do
    tag=$(echo "$tag" | xargs)  # trim
    [[ -n "$where_clause" ]] && where_clause="$where_clause OR "
    local tag_esc
    tag_esc=$(sql_escape "$tag")
    where_clause="${where_clause}m.tags LIKE '%${tag_esc}%'"
  done
  sqlite3 -header -column "$MEMORY_DB" <<SQL
SELECT id, category, title, substr(content, 1, 120) AS content_preview, tags, issue_number, created_at
FROM memories m
WHERE ($where_clause) AND superseded_by IS NULL
ORDER BY created_at DESC
LIMIT $limit;
SQL
}

memory_get() {
  local id="$1"
  [[ "$id" =~ ^[0-9]+$ ]] || { echo "Invalid memory ID: $id"; return 1; }
  memory_init_db
  sqlite3 -header -column "$MEMORY_DB" "SELECT * FROM memories WHERE id = $id;"
}

memory_prune_stale() {
  memory_init_db
  local cutoff_date
  cutoff_date=$(date -u -v-90d +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null || date -u -d "90 days ago" +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null)
  [[ -z "$cutoff_date" ]] && return 1

  log_msg INFO memory "Pruning memories older than 90 days (before $cutoff_date)..."

  local stale_rows
  stale_rows=$(sqlite3 "$MEMORY_DB" "SELECT id, issue_number FROM memories WHERE created_at < '$cutoff_date' AND superseded_by IS NULL;" 2>/dev/null)
  [[ -z "$stale_rows" ]] && { log_msg INFO memory "No stale memories found"; return 0; }

  local stale_count=0
  while IFS='|' read -r mid missue; do
    [[ -z "$mid" ]] && continue
    # Check if GitHub issue is closed (batch would be better but GitHub CLI doesn't support it well)
    if [[ -n "$missue" ]]; then
      local issue_state
      issue_state=$(gh issue view "$missue" --json state -q '.state' 2>/dev/null || echo "")
      if [[ "$issue_state" == "CLOSED" ]]; then
        sqlite3 "$MEMORY_DB" "DELETE FROM memories WHERE id = $mid;"
        stale_count=$((stale_count + 1))
        continue
      fi
    fi
    # Check if referenced files still exist
    local files_ref
    files_ref=$(sqlite3 "$MEMORY_DB" "SELECT content FROM memories WHERE id = $mid;" | grep -oE '[a-zA-Z0-9_/.-]+\.(py|html|md|js|css|sh)' | head -5)
    local all_gone=true
    if [[ -n "$files_ref" ]]; then
      while IFS= read -r fpath; do
        [[ -f "$REPO_ROOT/$fpath" ]] && { all_gone=false; break; }
      done <<< "$files_ref"
      if $all_gone; then
        sqlite3 "$MEMORY_DB" "DELETE FROM memories WHERE id = $mid;"
        stale_count=$((stale_count + 1))
      fi
    fi
  done <<< "$stale_rows"

  log_msg INFO memory "Pruned $stale_count stale memories"
}

# ─── Layered Knowledge Scoping ────────────────────────────────────────────────

_read_knowledge_tier() {
  local dir="$1"
  local label="$2"
  [[ -d "$dir" ]] || return 0
  local found=false
  for f in "$dir"/*.md; do
    [[ -f "$f" ]] || continue
    found=true
    local basename
    basename=$(basename "$f")
    echo "--- [$label] $basename ---"
    cat "$f"
    echo ""
  done
  if $found; then
    log_msg INFO knowledge "Loaded $label knowledge from $dir"
  fi
}

load_knowledge() {
  local issue="${1:-}"
  local knowledge=""

  # Tier 1: Shared (cross-project)
  if [[ -d "$SHARED_KNOWLEDGE_DIR" ]]; then
    knowledge+=$(_read_knowledge_tier "$SHARED_KNOWLEDGE_DIR" "shared")
  fi

  # Tier 2: Project-level
  knowledge+=$(_read_knowledge_tier "$MEMORY_DIR" "project")

  # Tier 3: Session/task-specific
  if [[ -n "$issue" && -d "$MEMORY_DIR/sessions/$issue" ]]; then
    knowledge+=$(_read_knowledge_tier "$MEMORY_DIR/sessions/$issue" "session")
  fi

  echo "$knowledge"
}

save_session_context() {
  local issue="$1"
  local content="$2"
  local filename="${3:-context.md}"
  local session_dir="$MEMORY_DIR/sessions/$issue"
  mkdir -p "$session_dir"
  echo "$content" > "$session_dir/$filename"
  log_msg INFO knowledge "Saved session context: $session_dir/$filename"
}

knowledge_list() {
  echo "=== Knowledge Inventory ==="
  echo ""

  echo "Tier 1 — Shared (cross-project): $SHARED_KNOWLEDGE_DIR"
  if [[ -d "$SHARED_KNOWLEDGE_DIR" ]]; then
    local count=0
    for f in "$SHARED_KNOWLEDGE_DIR"/*.md; do
      [[ -f "$f" ]] || continue
      count=$((count + 1))
      echo "  $(basename "$f")  ($(wc -l < "$f" | tr -d ' ') lines)"
    done
    [[ $count -eq 0 ]] && echo "  (empty)"
  else
    echo "  (not created yet)"
  fi

  echo ""
  echo "Tier 2 — Project: $MEMORY_DIR"
  local count=0
  for f in "$MEMORY_DIR"/*.md; do
    [[ -f "$f" ]] || continue
    count=$((count + 1))
    echo "  $(basename "$f")  ($(wc -l < "$f" | tr -d ' ') lines)"
  done
  [[ $count -eq 0 ]] && echo "  (empty)"

  echo ""
  echo "Tier 3 — Sessions: $MEMORY_DIR/sessions/"
  if [[ -d "$MEMORY_DIR/sessions" ]]; then
    local session_count=0
    for d in "$MEMORY_DIR/sessions"/*/; do
      [[ -d "$d" ]] || continue
      session_count=$((session_count + 1))
      local issue_num
      issue_num=$(basename "$d")
      local file_count
      file_count=$(find "$d" -name '*.md' 2>/dev/null | wc -l | tr -d ' ')
      echo "  #$issue_num  ($file_count files)"
    done
    [[ $session_count -eq 0 ]] && echo "  (empty)"
  else
    echo "  (empty)"
  fi

  echo ""
  echo "SQLite memories: $(sqlite3 "$MEMORY_DB" "SELECT COUNT(*) FROM memories WHERE superseded_by IS NULL;" 2>/dev/null || echo "0") entries"
}

knowledge_promote() {
  local file="$1"
  if [[ ! -f "$MEMORY_DIR/$file" ]]; then
    echo "File not found in project knowledge: $MEMORY_DIR/$file"
    return 1
  fi
  mkdir -p "$SHARED_KNOWLEDGE_DIR"
  cp "$MEMORY_DIR/$file" "$SHARED_KNOWLEDGE_DIR/$file"
  echo "Promoted $file to shared knowledge: $SHARED_KNOWLEDGE_DIR/$file"
  log_msg INFO knowledge "Promoted $file from project to shared tier"
}

knowledge_demote() {
  local file="$1"
  if [[ ! -f "$SHARED_KNOWLEDGE_DIR/$file" ]]; then
    echo "File not found in shared knowledge: $SHARED_KNOWLEDGE_DIR/$file"
    return 1
  fi
  rm "$SHARED_KNOWLEDGE_DIR/$file"
  echo "Removed $file from shared knowledge."
  log_msg INFO knowledge "Demoted $file from shared tier"
}

# ─── Invariants / Constraints ─────────────────────────────────────────────────

_resolve_invariants_dir() {
  if [[ -n "$INVARIANTS_DIR" ]]; then
    echo "$INVARIANTS_DIR"
  else
    echo "$SCRIPT_DIR/invariants"
  fi
}

run_invariants() {
  local worktree_dir="$1"
  local base_branch="${2:-$BASE_BRANCH}"
  local inv_dir
  inv_dir=$(_resolve_invariants_dir)

  if [[ ! -d "$inv_dir" ]]; then
    log_msg WARN invariants "No invariants directory found at $inv_dir"
    return 0
  fi

  local total=0 passed=0 failed=0 warnings=0
  local failure_details=""

  for script in "$inv_dir"/*.sh; do
    [[ -f "$script" ]] || continue
    total=$((total + 1))
    local name
    name=$(basename "$script" .sh)

    local output=""
    local exit_code=0
    output=$(bash "$script" "$worktree_dir" "$base_branch" 2>&1) || exit_code=$?

    if [[ $exit_code -eq 0 ]]; then
      if [[ "$output" == WARN:* ]]; then
        warnings=$((warnings + 1))
        echo "  [WARN] $name: ${output#WARN: }"
      else
        passed=$((passed + 1))
        echo "  [PASS] $name"
      fi
    else
      failed=$((failed + 1))
      echo "  [FAIL] $name"
      failure_details+="$output"$'\n'
    fi
  done

  echo ""
  echo "Invariants: $passed passed, $warnings warnings, $failed failed (of $total)"

  if [[ $failed -gt 0 ]]; then
    echo ""
    echo "Failure details:"
    echo "$failure_details"
    return 1
  fi
  return 0
}

invariants_list() {
  local inv_dir
  inv_dir=$(_resolve_invariants_dir)
  echo "=== Invariants ==="
  echo "Directory: $inv_dir"
  echo ""
  if [[ ! -d "$inv_dir" ]]; then
    echo "  (directory does not exist)"
    return
  fi
  for script in "$inv_dir"/*.sh; do
    [[ -f "$script" ]] || { echo "  (no invariants defined)"; return; }
    local name desc
    name=$(basename "$script" .sh)
    desc=$(head -2 "$script" | grep '^#' | tail -1 | sed 's/^# *//')
    echo "  $name — $desc"
  done
}

# ─── Review Intelligence ────────────────────────────────────────────────────

REVIEW_PATTERNS_DB="${MEMORY_DIR}/review-patterns.db"

review_patterns_init() {
  [[ "$_REVIEW_DB_INIT" == "true" ]] && return 0
  _REVIEW_DB_INIT=true
  sqlite3 "$REVIEW_PATTERNS_DB" <<'SQL'
CREATE TABLE IF NOT EXISTS review_patterns (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  reviewer TEXT NOT NULL,
  pattern TEXT NOT NULL,
  category TEXT DEFAULT 'general',
  frequency INTEGER DEFAULT 1,
  last_seen TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
  example TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS addressed_comments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  pr_number INTEGER NOT NULL,
  comment_id TEXT NOT NULL UNIQUE,
  addressed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
SQL
}

track_review_patterns() {
  local pr_number="$1"
  command -v sqlite3 &>/dev/null || return 0
  review_patterns_init

  local comments
  comments=$(gh api "repos/$GITHUB_REPO/pulls/$pr_number/comments" 2>/dev/null || echo "[]")
  [[ "$comments" == "[]" ]] && return 0

  local classify_result
  classify_result=$(claude_run_tracked "review-classify" "PR-$pr_number" "Classify the following PR review comments into patterns.

PR #$pr_number comments:
$comments

For each comment, output one line in this format:
PATTERN|reviewer_login|category|short_pattern_description

Categories: error_handling, testing, style, security, performance, naming, documentation, other

Only output PATTERN lines, nothing else. Skip bot comments (dependabot).
Group similar comments into one pattern." 2>/dev/null) || return 0

  echo "$classify_result" | grep '^PATTERN|' | while IFS='|' read -r _ reviewer category pattern; do
    [[ -z "$reviewer" || -z "$pattern" ]] && continue
    local existing
    local esc_reviewer esc_pattern esc_category
    esc_reviewer=$(sql_escape "$reviewer")
    esc_pattern=$(sql_escape "$pattern")
    esc_category=$(sql_escape "$category")
    existing=$(sqlite3 "$REVIEW_PATTERNS_DB" \
      "SELECT id FROM review_patterns WHERE reviewer='$esc_reviewer' AND pattern='$esc_pattern' LIMIT 1;" 2>/dev/null)
    if [[ -n "$existing" ]]; then
      sqlite3 "$REVIEW_PATTERNS_DB" \
        "UPDATE review_patterns SET frequency = frequency + 1, last_seen = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = $existing;"
    else
      sqlite3 "$REVIEW_PATTERNS_DB" \
        "INSERT INTO review_patterns (reviewer, pattern, category) VALUES ('$esc_reviewer', '$esc_pattern', '$esc_category');"
    fi
  done
  log_msg INFO review-intel "Tracked review patterns from PR #$pr_number"
}

get_review_patterns() {
  local limit="${1:-10}"
  command -v sqlite3 &>/dev/null || return 0
  [[ -f "$REVIEW_PATTERNS_DB" ]] || return 0
  sqlite3 -header -column "$REVIEW_PATTERNS_DB" \
    "SELECT reviewer, pattern, category, frequency FROM review_patterns ORDER BY frequency DESC LIMIT $limit;" 2>/dev/null
}

mark_comment_addressed() {
  local pr_number="$1" comment_id="$2"
  command -v sqlite3 &>/dev/null || return 0
  review_patterns_init
  sqlite3 "$REVIEW_PATTERNS_DB" \
    "INSERT OR IGNORE INTO addressed_comments (pr_number, comment_id) VALUES ($pr_number, '$(sql_escape "$comment_id")');"
}

get_unaddressed_comments() {
  local pr_number="$1"
  command -v sqlite3 &>/dev/null || { gh api "repos/$GITHUB_REPO/pulls/$pr_number/comments" 2>/dev/null; return; }
  [[ -f "$REVIEW_PATTERNS_DB" ]] || { gh api "repos/$GITHUB_REPO/pulls/$pr_number/comments" 2>/dev/null; return; }

  local all_comments
  all_comments=$(gh api "repos/$GITHUB_REPO/pulls/$pr_number/comments" 2>/dev/null || echo "[]")

  local addressed_ids
  addressed_ids=$(sqlite3 "$REVIEW_PATTERNS_DB" \
    "SELECT comment_id FROM addressed_comments WHERE pr_number = $pr_number;" 2>/dev/null || echo "")

  if [[ -z "$addressed_ids" ]]; then
    echo "$all_comments"
  else
    local jq_filter
    jq_filter=$(echo "$addressed_ids" | jq -R -s 'split("\n") | map(select(. != ""))')
    echo "$all_comments" | jq --argjson addressed "$jq_filter" '[.[] | select((.id | tostring) as $id | ($addressed | index($id)) | not)]'
  fi
}

# ─── Watch Mode: Auto-Select GitHub Issue ─────────────────────────────────────

watch_auto_select_issue() {
  log_msg INFO watch-issues "Querying GitHub Issues backlog..."
  local issue_args=("--state" "open" "--json" "number,title,labels,milestone,assignees")
  [[ -n "$ISSUE_MILESTONE" ]] && issue_args+=("--milestone" "$ISSUE_MILESTONE")

  local task_list
  task_list=$(gh issue list "${issue_args[@]}" 2>&1) || {
    log_msg ERROR watch-issues "Error querying GitHub Issues: $task_list"
    return 1
  }

  if [[ "$task_list" == "[]" || -z "$task_list" ]]; then
    log_msg INFO watch-issues "No open issues match the filter."
    return 0
  fi

  local task_history=""
  [[ -f "$MEMORY_DIR/task-history.md" ]] && task_history=$(tail -100 "$MEMORY_DIR/task-history.md")

  log_msg INFO watch-issues "Using Claude to auto-select the best issue..."
  local selection
  selection=$(claude_run_tracked "watch-select" "auto-select" "You are the GWYM Agent in autonomous watch mode. Pick the single best GitHub issue to work on next.

GitHub issues:
$task_list

Previously completed tasks (avoid re-doing these):
${task_history:-None yet.}

Selection criteria (in priority order):
1. Assigned to the current user and not started yet
2. Unassigned issues suitable for autonomous development
3. Prefer small/medium effort over large
4. Skip issues that require UI work, manual infra changes, or are spikes/investigations
5. Skip issues already being worked on by someone else
6. Prefer tasks the agent can realistically complete autonomously (code changes, tests, API work)

IMPORTANT: Output ONLY a single line in this exact format:
SELECTED: ISSUE_NUMBER | reason for selection

If no suitable issue exists, output:
SELECTED: NONE | reason")

  log_msg INFO watch-issues "Selection result: $selection"

  local selected_issue selected_reason
  selected_issue=$(echo "$selection" | grep -oE 'SELECTED: [0-9]+' | head -1 | sed 's/SELECTED: //')
  selected_reason=$(echo "$selection" | grep 'SELECTED:' | head -1 | sed 's/.*| *//')

  if [[ -z "$selected_issue" || "$selected_issue" == "NONE" ]]; then
    log_msg INFO watch-issues "No suitable issue found: $selected_reason"
    return 0
  fi

  log_msg INFO watch-issues "Auto-selected: #$selected_issue — $selected_reason"

  # Veto window
  local veto_seconds="${WATCH_VETO_SECONDS:-30}"
  local vetoed=false

  if $DASHBOARD_MODE; then
    dashboard_request "veto" \
      "Auto-starting #$selected_issue in ${veto_seconds}s. Proceed or veto?" \
      '["proceed","veto"]' \
      "{\"issue\":\"$selected_issue\",\"reason\":$(printf '%s' "$selected_reason" | jq -Rs .),\"timeout\":$veto_seconds}" \
      false
    [[ "$DASHBOARD_RESPONSE" == "veto" ]] && vetoed=true
  else
    # Terminal mode: trap SIGINT so Ctrl+C skips this task without killing watch mode
    trap 'vetoed=true' INT
    notify "Auto-starting: #$selected_issue — ${selected_reason}. Cancel within ${veto_seconds}s (Ctrl+C)" "Blow"

    echo ""
    echo "  Auto-selected: #$selected_issue"
    echo "  Reason: $selected_reason"
    echo "  Starting in ${veto_seconds}s... (Ctrl+C to skip this task)"
    echo ""

    local elapsed=0
    while ! $vetoed && [[ "$elapsed" -lt "$veto_seconds" ]]; do
      sleep 1
      elapsed=$((elapsed + 1))
    done
    trap - INT
  fi

  if $vetoed; then
    log_msg INFO watch-issues "Issue #$selected_issue vetoed by user."
    notify "Skipped: #$selected_issue"
    return 0
  fi

  log_msg INFO watch-issues "Starting full workflow for #$selected_issue"
  task_state_write "$selected_issue" "in_progress" "step2_auto_selected" "step3"

  # Mark issue as in-progress
  gh issue edit "$selected_issue" --add-label "in-progress" 2>/dev/null || true

  run_from_step "step3" "$selected_issue"
}

# ─── Watch Mode ──────────────────────────────────────────────────────────────

WATCH_LOCK_FILE="/tmp/gwym-agent.lock"

watch_mode() {
  # Atomic file lock via flock
  exec 9>"$WATCH_LOCK_FILE"
  if ! flock -n 9; then
    log_msg ERROR watch "Another gwym-agent instance is running"
    exit 1
  fi
  trap 'rm -f "$WATCH_LOCK_FILE"' EXIT

  # Graceful shutdown
  local watch_running=true
  trap 'watch_running=false; log_msg INFO watch "Shutting down..."; notify "GWYM Agent stopped"' INT TERM

  log_msg INFO watch "Watch mode started (active: ${WATCH_INTERVAL_ACTIVE}s, idle: ${WATCH_INTERVAL_IDLE}s)"
  notify "GWYM Agent watch mode started"

  local cycle=0
  while $watch_running; do
    cycle=$((cycle + 1))
    log_msg INFO watch "── Cycle $cycle ──────────────────────────────────"

    # 1. Auto-learn from merged PRs
    auto_learn_merged

    # 2. Auto-cleanup stale worktrees
    worktree_auto_cleanup

    # 3. Run priority scanner
    local scan_output
    scan_output=$(priority_scan)

    local sleep_interval="$WATCH_INTERVAL_IDLE"

    if [[ -n "$scan_output" ]]; then
      log_msg INFO watch "Scanner found work:"
      echo "$scan_output" | while IFS='|' read -r priority type key reason; do
        log_msg INFO watch "  [$priority] $type $key: $reason"
      done

      # Pick the top priority item
      local top_priority top_type top_key _top_reason
      IFS='|' read -r top_priority top_type top_key _top_reason <<< "$(echo "$scan_output" | head -1)"

      log_msg INFO watch "Auto-selecting: [$top_priority] $top_type $top_key"
      notify "Working on: $top_type $top_key"

      case "$top_type" in
        RESUME|PAUSED)
          # Run in subshell — step functions may call exit
          (
            restore_task_context "$top_key"
            local resume_step
            resume_step=$(jq -r '.next_step // ""' "$(task_state_file "$top_key")" 2>/dev/null || echo "")
            [[ -z "$resume_step" ]] && resume_step="step3"
            run_from_step "$resume_step" "$top_key"
          ) || true
          sleep_interval="$WATCH_INTERVAL_ACTIVE"
          ;;
        CI_FAIL|REVIEW|COMMENTS)
          # Run in subshell
          ( handle_address_pr "$top_key" ) || true
          sleep_interval="$WATCH_INTERVAL_ACTIVE"
          ;;
        ISSUES)
          if [[ "${WATCH_AUTO_SELECT_ISSUES:-true}" != "true" ]]; then
            log_msg INFO watch "Issue auto-select disabled. Skipping backlog items."
          else
            log_msg INFO watch "Auto-selecting issue from backlog..."
            ( watch_auto_select_issue ) || true
            sleep_interval="$WATCH_INTERVAL_ACTIVE"
          fi
          ;;
      esac
    else
      log_msg INFO watch "No work found. Sleeping ${WATCH_INTERVAL_IDLE}s..."
      # Run memory pruning during idle cycles
      if command -v sqlite3 &>/dev/null && [[ -f "$MEMORY_DB" ]] && (( cycle % 48 == 0 )); then
        memory_prune_stale
      fi
    fi

    # Sleep with interruptibility
    if $watch_running; then
      log_msg INFO watch "Next cycle in ${sleep_interval}s"
      local elapsed=0
      while $watch_running && [[ "$elapsed" -lt "$sleep_interval" ]]; do
        sleep 5
        elapsed=$((elapsed + 5))
      done
    fi
  done

  log_msg INFO watch "Watch mode stopped after $cycle cycles"
}

# ─── Cost Tracking ────────────────────────────────────────────────────────────

COST_FILE="${MEMORY_DIR}/costs.jsonl"

_record_cost() {
  local json_output="$1"
  local phase="$2"
  local issue="$3"

  local cost_record
  cost_record=$(echo "$json_output" | jq -c \
    --arg ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    --arg issue "$issue" \
    --arg ph "$phase" \
    '{timestamp: $ts, issue: $issue, phase: $ph, model: (.modelUsage | keys[0] // "unknown"), input_tokens: (.modelUsage[.modelUsage | keys[0]].inputTokens // 0), output_tokens: (.modelUsage[.modelUsage | keys[0]].outputTokens // 0), cache_tokens: (.modelUsage[.modelUsage | keys[0]].cacheCreationInputTokens // 0), cost_usd: (.total_cost_usd // 0), duration_ms: (.duration_ms // 0)}' \
    2>/dev/null) || true

  if [[ -n "$cost_record" && "$cost_record" != "null" ]]; then
    echo "$cost_record" >> "$COST_FILE"
    local cost_usd
    cost_usd=$(echo "$cost_record" | jq -r '.cost_usd' 2>/dev/null)
    log_msg INFO costs "Recorded: phase=$phase cost=\$${cost_usd}"
  else
    log_msg WARN costs "Failed to record cost for phase=$phase"
  fi
}

# Run claude, record costs, return raw JSON
_claude_invoke() {
  local phase="$1"
  local issue="$2"
  local prompt="$3"
  shift 3

  # Merge persona MCP tools into allowed tools list
  local allowed_tools="Read,Edit,Write,Bash,Glob,Grep,TodoWrite"
  if [[ -n "$PERSONA_MCP_TOOLS" ]]; then
    allowed_tools="${allowed_tools},${PERSONA_MCP_TOOLS}"
  fi

  local json_output
  json_output=$(claude -p "$prompt" \
    --model "$AGENT_MODEL" \
    --max-budget-usd "$MAX_BUDGET" \
    --allowedTools "$allowed_tools" \
    --output-format json \
    "$@" 2>/dev/null)

  _record_cost "$json_output" "$phase" "$issue" >&2
  echo "$json_output"
}

claude_run_tracked() {
  local json_output
  json_output=$(_claude_invoke "$@")
  echo "$json_output" | jq -r '.result // .' 2>/dev/null || echo "$json_output"
}

# Returns raw JSON (for session_id extraction)
claude_run_tracked_json() {
  _claude_invoke "$@"
}

# ─── CI Failure Classification ────────────────────────────────────────────────

classify_ci_failure() {
  local pr_number="$1"
  local worktree_dir="$2"
  local issue="$3"

  local failed_checks
  failed_checks=$(gh pr checks "$pr_number" 2>/dev/null | grep -i "fail" || true)

  if [[ -z "$failed_checks" ]]; then
    echo "NO_FAILURES"
    return 0
  fi

  log_msg INFO ci-classify "Classifying CI failures for PR #$pr_number..."

  # Check for known flaky patterns
  if [[ -n "$FLAKY_CHECKS" ]]; then
    local flaky_matches="" non_flaky=""
    while IFS= read -r line; do
      local check_name
      check_name=$(echo "$line" | awk '{print $1}')
      if echo "$check_name" | grep -qE "$FLAKY_CHECKS"; then
        flaky_matches="${flaky_matches}${line}\n"
      else
        non_flaky="${non_flaky}${line}\n"
      fi
    done <<< "$failed_checks"

    if [[ -z "$non_flaky" && -n "$flaky_matches" ]]; then
      log_msg INFO ci-classify "All failures are known flaky checks."
      echo "FLAKY_RETEST"
      return 0
    fi
  fi

  local changed_files=""
  if [[ -d "$worktree_dir" ]]; then
    changed_files=$(git -C "$worktree_dir" diff --name-only "origin/$BASE_BRANCH" 2>/dev/null || true)
  fi

  local classification
  classification=$(claude_run_tracked "ci-classify" "$issue" "You are classifying CI failures on PR #$pr_number.

Failed checks:
$failed_checks

Files changed in this PR:
$changed_files

Classify EACH failure into exactly one category:
1. FLAKY — intermittent/timing issue, not related to code changes
2. OUR_CODE — test failure in code that was changed in this PR
3. UNRELATED — real failure in code NOT changed in this PR
4. LINT_FORMAT — linting or formatting issue that can be auto-fixed

Output a JSON array, nothing else:
[{\"check\": \"name\", \"category\": \"FLAKY|OUR_CODE|UNRELATED|LINT_FORMAT\", \"reason\": \"brief explanation\"}]")

  log_msg INFO ci-classify "CI classification: $classification"

  local has_our_code=0 has_unrelated=0 has_lint=0
  read -r has_our_code has_unrelated has_lint < <(
    echo "$classification" | jq -r '[
      [.[] | select(.category == "OUR_CODE")] | length,
      [.[] | select(.category == "UNRELATED")] | length,
      [.[] | select(.category == "LINT_FORMAT")] | length
    ] | @tsv' 2>/dev/null
  ) || true

  # Handle lint/format issues: auto-fix
  if [[ "$has_lint" -gt 0 ]]; then
    log_msg INFO ci-classify "Lint/format failures detected. Auto-fixing..."
    claude_run_tracked "ci-lint" "$issue" "Fix lint/formatting issues in the worktree.
Working directory: $worktree_dir

1. Run: cd $worktree_dir && $FORMAT_CMD
2. Run: cd $worktree_dir && $LINT_CMD
3. Run: cd $worktree_dir && $TEST_CMD
4. Amend the commit: cd $worktree_dir && git add -A && git commit --amend --no-edit
5. Push: cd $worktree_dir && git push --force-with-lease"
    echo "LINT_FIXED"
    return 0
  fi

  # Handle real test failures in our code
  if [[ "$has_our_code" -gt 0 ]]; then
    log_msg WARN ci-classify "Real test failures in changed code. Investigating..."
    claude_run_tracked "ci-fix" "$issue" "CI tests are failing in code you changed for PR #$pr_number.

Classification:
$classification

Working directory: $worktree_dir

1. Identify the failing tests
2. Reproduce locally: cd $worktree_dir && $TEST_CMD
3. Fix the root cause
4. Run full test suite: cd $worktree_dir && $TEST_CMD
5. Amend the commit: cd $worktree_dir && git add -A && git commit --amend --no-edit
6. Push: cd $worktree_dir && git push --force-with-lease"
    echo "OUR_CODE_FIXED"
    return 0
  fi

  # Handle unrelated failures
  if [[ "$has_unrelated" -gt 0 ]]; then
    log_msg WARN ci-classify "Unrelated test failures detected."
    notify "PR #$pr_number has unrelated CI failures"
    local unrelated_details
    unrelated_details=$(echo "$classification" | jq -r '.[] | select(.category == "UNRELATED") | "- \(.check): \(.reason)"' 2>/dev/null || true)
    gh pr comment "$pr_number" --body "CI has failures unrelated to this PR's changes:
$unrelated_details

These may need attention from the team." 2>/dev/null || true
    echo "UNRELATED_FLAGGED"
    return 0
  fi

  # Fallback: all classified as flaky
  log_msg INFO ci-classify "All failures classified as flaky."
  echo "FLAKY_RETEST"
}

# ─── Status Dashboard (CLI) ─────────────────────────────────────────────────

render_dashboard() {
  echo ""
  echo "================================================================"
  echo "  GWYM Agent Dashboard"
  echo "================================================================"
  echo ""
  echo "Config: model=$AGENT_MODEL | budget=\$$MAX_BUDGET/task | repo=$GITHUB_REPO"
  echo ""

  # ─── Tasks ───
  local active="" paused="" done_recent=""
  local now_epoch
  now_epoch=$(date +%s)
  local week_ago=$((now_epoch - 7 * 86400))

  if [[ -d "$WORKTREE_BASE" ]]; then
    for sf in "$WORKTREE_BASE"/*/task-state.json; do
      [[ -f "$sf" ]] || continue
      local tk st ls reason pr_val branch_val updated_at
      read -r tk st ls reason pr_val branch_val updated_at < <(
        jq -r '[.issue_number, .status, .last_step, (.paused_reason // ""), (.pr_number // ""), (.branch_name // ""), (.updated_at // "")] | @tsv' "$sf" 2>/dev/null
      ) || continue

      local title=""
      title=$(gh issue view "$tk" --json title -q '.title' 2>/dev/null || echo "")
      [[ ${#title} -gt 45 ]] && title="${title:0:42}..."

      local pr_info="-"
      if [[ -n "$pr_val" ]]; then
        local pr_state checks_output checks_pass checks_total
        pr_state=$(gh pr view "$pr_val" --json state -q '.state' 2>/dev/null || echo "?")
        checks_output=$(gh pr checks "$pr_val" 2>/dev/null || true)
        checks_pass=$(echo "$checks_output" | grep -c "pass" || echo "?")
        checks_total=$(echo "$checks_output" | wc -l | tr -d ' ' || echo "?")
        pr_info="PR #$pr_val ($pr_state, $checks_pass/$checks_total)"
      fi

      local issue_cost="?"
      if [[ -f "$COST_FILE" ]]; then
        issue_cost=$(jq -s --arg iss "$tk" \
          '[.[] | select(.issue == $iss)] | map(.cost_usd) | add | . * 100 | round / 100' \
          "$COST_FILE" 2>/dev/null || echo "?")
        [[ "$issue_cost" == "null" ]] && issue_cost="0.00"
      fi

      local line="  #${tk}  ${title:-unknown}  ${ls}  \$${issue_cost}  ${pr_info}"

      case "$st" in
        in_progress) active+="$line"$'\n' ;;
        paused)      paused+="  #${tk}  ${title:-unknown}  ${ls}  \$${issue_cost}  Reason: ${reason:-unknown}"$'\n' ;;
        done)
          local updated_epoch=0
          if [[ -n "$updated_at" ]]; then
            updated_epoch=$(date -j -f "%Y-%m-%dT%H:%M:%SZ" "$updated_at" +%s 2>/dev/null || date -d "$updated_at" +%s 2>/dev/null || echo "0")
          fi
          if [[ $updated_epoch -gt $week_ago ]]; then
            done_recent+="$line"$'\n'
          fi
          ;;
      esac
    done
  fi

  if [[ -n "$active" ]]; then
    echo "Active Tasks:"
    echo "$active"
  else
    echo "Active Tasks: (none)"
    echo ""
  fi

  if [[ -n "$paused" ]]; then
    echo "Paused Tasks:"
    echo "$paused"
  fi

  if [[ -n "$done_recent" ]]; then
    echo "Recent (last 7 days):"
    echo "$done_recent"
  fi

  # ─── Costs ───
  echo "Cost Summary:"
  if [[ -f "$COST_FILE" ]]; then
    local today_date
    today_date=$(date +%Y-%m-%d)
    local week_start
    week_start=$(date -v-7d +%Y-%m-%d 2>/dev/null || date -d "7 days ago" +%Y-%m-%d 2>/dev/null || echo "")

    local today_cost week_cost total_cost
    today_cost=$(jq -s --arg d "$today_date" \
      '[.[] | select(.timestamp | startswith($d))] | map(.cost_usd) | add // 0 | . * 100 | round / 100' \
      "$COST_FILE" 2>/dev/null || echo "0")
    if [[ -n "$week_start" ]]; then
      week_cost=$(jq -s --arg d "$week_start" \
        '[.[] | select(.timestamp >= $d)] | map(.cost_usd) | add // 0 | . * 100 | round / 100' \
        "$COST_FILE" 2>/dev/null || echo "0")
    else
      week_cost="?"
    fi
    total_cost=$(jq -s 'map(.cost_usd) | add // 0 | . * 100 | round / 100' \
      "$COST_FILE" 2>/dev/null || echo "0")

    echo "  Today: \$$today_cost | This week: \$$week_cost | Total: \$$total_cost"
  else
    echo "  (no cost data)"
  fi
  echo "  Budget per task: \$$MAX_BUDGET"
  echo ""

  # ─── Knowledge ───
  echo "Knowledge:"
  local shared_count=0 project_count=0 memory_count=0
  if [[ -d "$SHARED_KNOWLEDGE_DIR" ]]; then
    shared_count=$(find "$SHARED_KNOWLEDGE_DIR" -name '*.md' 2>/dev/null | wc -l | tr -d ' ')
  fi
  for f in "$MEMORY_DIR"/*.md; do
    [[ -f "$f" ]] && project_count=$((project_count + 1))
  done
  if [[ -f "$MEMORY_DB" ]]; then
    memory_count=$(sqlite3 "$MEMORY_DB" "SELECT COUNT(*) FROM memories WHERE superseded_by IS NULL;" 2>/dev/null || echo "0")
  fi
  echo "  Shared: $shared_count files | Project: $project_count files | SQLite: $memory_count entries"

  # ─── Invariants ───
  local inv_dir
  inv_dir=$(_resolve_invariants_dir)
  local inv_count=0
  if [[ -d "$inv_dir" ]]; then
    inv_count=$(find "$inv_dir" -name '*.sh' 2>/dev/null | wc -l | tr -d ' ')
  fi
  echo "  Invariants: $inv_count defined"
  echo ""
}

# ─── Helpers ───────────────────────────────────────────────────────────────────

AGENT_ICON="$REPO_ROOT/.claude/assets/agent-icon.png"

notify() {
  local msg="$1"
  local sound="${2:-Ping}"
  # In dashboard mode, write to notifications.jsonl for the dashboard to pick up
  if $DASHBOARD_MODE; then
    log_msg INFO notify "$msg"
    local notif_id
    notif_id="notif-$(date +%s)-$$"
    printf '{"id":"%s","ts":"%s","message":%s,"sound":"%s"}\n' \
      "$notif_id" "$(date -u +"%Y-%m-%dT%H:%M:%SZ")" \
      "$(printf '%s' "$msg" | jq -Rs .)" "$sound" \
      >> "$CONTROL_DIR/notifications.jsonl"
    return 0
  fi
  if command -v terminal-notifier &>/dev/null; then
    local extra_args=()
    [[ -f "$AGENT_ICON" ]] && extra_args+=(-contentImage "$AGENT_ICON")
    terminal-notifier -title "GWYM Agent" -message "$msg" -sound "$sound" \
      "${extra_args[@]}" \
      -activate com.apple.Terminal -group "gwym-agent" 2>/dev/null || true
  else
    local msg_safe
    msg_safe=$(printf '%s' "$msg" | sed 's/[\"\\]/\\&/g')
    osascript -e "display notification \"$msg_safe\" with title \"GWYM Agent\" sound name \"$sound\"" 2>/dev/null || true
  fi
}

banner() {
  local msg="$1"
  local phase="${2:-general}"
  echo ""
  echo "════════════════════════════════════════════════════════════"
  echo "  $msg"
  echo "════════════════════════════════════════════════════════════"
  echo ""
  log_msg INFO "$phase" "$msg"
}

show_ready_for_review() {
  local pr_number="$1"
  local extra_info="${2:-}"

  local pr_data
  pr_data=$(gh pr view "$pr_number" --json title,url,statusCheckRollup 2>/dev/null || echo "{}")
  local pr_url pr_title ci_passing
  pr_url=$(echo "$pr_data" | jq -r '.url // ""' 2>/dev/null)
  pr_title=$(echo "$pr_data" | jq -r '.title // ""' 2>/dev/null)
  ci_passing=$(echo "$pr_data" | jq '[.statusCheckRollup[]? | select(.status == "COMPLETED" and .conclusion == "SUCCESS")] | length' 2>/dev/null || echo "?")
  [[ -z "$pr_url" ]] && pr_url="https://github.com/$GITHUB_REPO/pull/$pr_number"

  notify "PR #$pr_number ready for review!" "Glass"

  echo "PR #$pr_number is ready for human review."
  echo ""
  echo "  URL: $pr_url"
  [[ -n "$extra_info" ]] && echo "  $extra_info"
  echo "  CI: $ci_passing checks passing"
  echo ""
}

ask() {
  local prompt="$1"
  local result
  if $DASHBOARD_MODE; then
    dashboard_request "generic" "$prompt" '["ok"]' '{}' true
    result="$DASHBOARD_RESPONSE_VALUE"
    [[ -z "$result" ]] && result="$DASHBOARD_RESPONSE"
  else
    read -rp "$prompt" result
  fi
  echo "$result"
}

cleanup_worktree() {
  local wt_dir="$1"
  local branch_name="$2"
  if [[ -d "$wt_dir" ]]; then
    log_msg INFO cleanup "Cleaning up worktree: $wt_dir"
    git -C "$REPO_ROOT" worktree remove "$wt_dir" --force 2>/dev/null || true
    git -C "$REPO_ROOT" branch -D "$branch_name" 2>/dev/null || true
  fi
}

VSCODE_CMD=""
find_vscode() {
  if command -v code &>/dev/null; then
    VSCODE_CMD="code"
  elif [[ -x "/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code" ]]; then
    VSCODE_CMD="/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code"
  fi
}
find_vscode

open_vscode_worktree() {
  local worktree_dir="$1"
  if [[ -n "$VSCODE_CMD" ]]; then
    "$VSCODE_CMD" "$worktree_dir" --new-window 2>/dev/null || true
  fi
}

open_vscode_diff() {
  if [[ -n "$VSCODE_CMD" ]]; then
    sleep 2
    "$VSCODE_CMD" --command "workbench.view.scm" 2>/dev/null || true
  fi
}

# ─── Issue Slug Helper ───────────────────────────────────────────────────────

issue_slug() {
  local issue="$1"
  local title
  title=$(gh issue view "$issue" --json title -q '.title' 2>/dev/null || echo "")
  if [[ -n "$title" ]]; then
    echo "$title" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9 ]//g' | awk '{for(i=1;i<=4&&i<=NF;i++) printf "%s-",$i}' | sed 's/-$//'
  else
    echo "issue"
  fi
}

# ─── Step Functions ──────────────────────────────────────────────────────────

run_step1() {
  local issue="$1"
  banner "Step 1: Syncing $BASE_BRANCH" step1

  git -C "$REPO_ROOT" fetch origin "$BASE_BRANCH"
  git -C "$REPO_ROOT" checkout "$BASE_BRANCH" 2>/dev/null || true
  git -C "$REPO_ROOT" pull origin "$BASE_BRANCH"
  log_msg INFO step1 "$BASE_BRANCH is up to date."
}

run_step2() {
  local issue="$1"
  log_msg INFO step2 "Working on issue #$issue"
}

run_step3() {
  local issue="$1"
  banner "Step 3: Creating isolated worktree" step3

  local slug
  slug=$(issue_slug "$issue")
  BRANCH_NAME="${BRANCH_NAME:-agent/feat/${issue}-${slug}}"
  WORKTREE_DIR="${WORKTREE_DIR:-$WORKTREE_BASE/$issue}"

  # Resume-aware: if worktree exists, reuse it
  if [[ -d "$WORKTREE_DIR" ]]; then
    log_msg INFO step3 "Worktree exists, reusing: $WORKTREE_DIR"
    local current_branch
    current_branch=$(worktree_branch "$WORKTREE_DIR")
    [[ -n "$current_branch" ]] && BRANCH_NAME="$current_branch"
    log_msg INFO step3 "Branch: $BRANCH_NAME"
  else
    # Clean up stale branch if exists
    git -C "$REPO_ROOT" branch -D "$BRANCH_NAME" 2>/dev/null || true

    mkdir -p "$WORKTREE_BASE"
    git -C "$REPO_ROOT" worktree add "$WORKTREE_DIR" -b "$BRANCH_NAME" "origin/$BASE_BRANCH" || {
      pause_task "$issue" "step3_worktree" "Failed to create worktree"
      return 1
    }
    log_msg INFO step3 "Worktree created at: $WORKTREE_DIR | Branch: $BRANCH_NAME"

    # Copy gitignored files needed for development
    IFS=',' read -ra copy_files <<< "$WORKTREE_COPY_FILES"
    for f in "${copy_files[@]}"; do
      [[ -f "$REPO_ROOT/$f" ]] && cp "$REPO_ROOT/$f" "$WORKTREE_DIR/$f"
    done

    # Symlink .venv so the worktree shares the Python virtualenv
    if [[ -d "$REPO_ROOT/.venv" && ! -e "$WORKTREE_DIR/.venv" ]]; then
      ln -s "$REPO_ROOT/.venv" "$WORKTREE_DIR/.venv"
      log_msg INFO step3 "Symlinked .venv into worktree"
    fi
  fi

  # Ensure Docker DB is running
  docker compose -f "$REPO_ROOT/docker-compose.yml" up -d db 2>/dev/null || true

  task_state_write "$issue" "in_progress" "step3_worktree" "step4"
  task_state_set_field "$issue" "branch_name" "$BRANCH_NAME"

  # Mark issue as in-progress
  gh issue edit "$issue" --add-label "in-progress" 2>/dev/null || true

  # Load persona for the target project
  load_persona "$WORKTREE_DIR"
  if [[ -n "$ACTIVE_PERSONA" ]]; then
    log_msg INFO step3 "Loaded persona: $ACTIVE_PERSONA"
  fi

  # Open VSCode in the worktree
  if [[ -n "$VSCODE_CMD" ]]; then
    log_msg INFO step3 "Opening VSCode in worktree..."
    open_vscode_worktree "$WORKTREE_DIR"
  fi
}

run_step4() {
  local issue="$1"
  banner "Step 4: Developing (autonomous)" step4

  # Resume-aware: skip dev if work already exists
  local ahead
  ahead=$(git -C "$WORKTREE_DIR" rev-list --count "origin/$BASE_BRANCH..HEAD" 2>/dev/null || echo "0")
  if [[ "$ahead" -gt 0 ]]; then
    log_msg INFO step4 "Found $ahead commits ahead of $BASE_BRANCH. Skipping dev."
    task_state_write "$issue" "in_progress" "step4_dev" "step5"
    return 0
  fi
  if [[ -n $(git -C "$WORKTREE_DIR" status --porcelain 2>/dev/null) ]]; then
    log_msg INFO step4 "Uncommitted changes found. Skipping dev, will self-review."
    task_state_write "$issue" "in_progress" "step4_dev" "step5"
    return 0
  fi

  log_msg INFO step4 "Agent is working on #$issue in the worktree..."
  echo "This may take several minutes. You will be notified when review is needed."
  echo ""

  # Fetch issue details if not already loaded
  if [[ -z "$TICKET_DETAILS" ]]; then
    TICKET_DETAILS=$(gh issue view "$issue" --json title,body,labels,milestone 2>/dev/null) || true
  fi

  # Load layered knowledge (shared + project + session)
  local knowledge_context=""
  knowledge_context=$(load_knowledge "$issue" 2>/dev/null || true)

  # Search SQLite memory for relevant context
  local memory_context=""
  if [[ -f "$MEMORY_DB" ]]; then
    local search_terms
    search_terms=$(echo "$TICKET_DETAILS" | jq -r '.title + " " + .body' 2>/dev/null | tr '[:upper:]' '[:lower:]' | grep -oE '\b[a-z]{4,}\b' | sort -u | head -10 | tr '\n' ' ')
    if [[ -n "$search_terms" ]]; then
      memory_context=$(memory_search "$search_terms" 5 2>/dev/null || true)
    fi
  fi

  # Load development guidelines from command file
  local dev_guidelines
  dev_guidelines=$(_load_command "develop-full")

  local issue_title issue_body
  issue_title=$(echo "$TICKET_DETAILS" | jq -r '.title // ""' 2>/dev/null)
  issue_body=$(echo "$TICKET_DETAILS" | jq -r '.body // ""' 2>/dev/null)

  local dev_prompt="You are the GWYM Agent working autonomously on GitHub issue #$issue.

Issue title: $issue_title
Issue body:
$issue_body

Working directory: $WORKTREE_DIR

Before you start, read your memory files for past learnings:
- $MEMORY_DIR/MEMORY.md
- $MEMORY_DIR/learnings.md (if exists)
- $MEMORY_DIR/review-feedback.md (if exists)
- $MEMORY_DIR/common-mistakes.md (if exists)

Knowledge base (layered — shared conventions, project patterns, task context):
$knowledge_context

Relevant memories from SQLite (if any):
$memory_context

Project-specific guidance (persona: ${ACTIVE_PERSONA:-none}):
${PERSONA_GUIDANCE:-No persona loaded. Follow patterns from CLAUDE.md.}

Development guidelines (from project commands):
${dev_guidelines:-Follow this workflow:
1. Understand the issue requirements thoroughly
2. Read relevant source code in the worktree
3. Write tests first (TDD approach)
4. Implement the solution
5. Run the linter: cd $WORKTREE_DIR && $LINT_CMD
6. Run tests: cd $WORKTREE_DIR && $TEST_CMD
7. If tests fail, fix and re-run (up to 3 attempts)
8. Organize changes into logical, atomic commits with conventional commit format}

Override commands for this project:
- Lint: cd $WORKTREE_DIR && $LINT_CMD
- Format: cd $WORKTREE_DIR && $FORMAT_CMD
- Test: cd $WORKTREE_DIR && $TEST_CMD

After completing development, write a file at $WORKTREE_DIR/.agent-summary.md with:
- **Big picture**: A 3-5 sentence paragraph explaining the problem, why it matters, and why the chosen approach
- **What changed**: Brief description of all changes made
- **How it worked before**: The previous behavior
- **How it works now**: The new behavior
- **Manual test instructions**: Specific steps to verify the changes
- **Files changed**: List of modified/added files with one-line descriptions

Important:
- All work must happen inside $WORKTREE_DIR
- Follow patterns from CLAUDE.md
- Match existing codebase conventions
- Commit format: type(scope): description
- Use Decimal for monetary values, never float
- All querysets must be filtered by request.user
- Type hints required on all functions
- NEVER add Co-Authored-By or any AI reference in commits
- NEVER use emojis in any output"

  SESSION_ID=$(claude_run_tracked_json "step4" "$issue" "$dev_prompt" | jq -r '.session_id // empty') || true

  if [[ -z "$SESSION_ID" ]]; then
    log_msg WARN step4 "Could not capture session ID. Continuing without session resumption."
  else
    task_state_set_field "$issue" "session_id" "$SESSION_ID"
  fi

  # Check if dev actually produced changes
  local changes_after
  changes_after=$(git -C "$WORKTREE_DIR" rev-list --count "origin/$BASE_BRANCH..HEAD" 2>/dev/null || echo "0")
  if [[ "$changes_after" -eq 0 && -z $(git -C "$WORKTREE_DIR" status --porcelain 2>/dev/null) ]]; then
    pause_task "$issue" "step4_dev" "Development session produced no changes"
    return 1
  fi

  task_state_write "$issue" "in_progress" "step4_dev" "step5"
}

run_step5() {
  local issue="$1"
  banner "Step 5: Simplify (reuse, quality, efficiency)" step5

  log_msg INFO step5 "Running /simplify on changes..."

  local simplify_diff
  simplify_diff=$(git -C "$WORKTREE_DIR" diff "origin/$BASE_BRANCH" 2>/dev/null)

  local reuse_prompt="Review the following diff for code reuse opportunities in the codebase at $WORKTREE_DIR.

Diff:
$simplify_diff

1. Search for existing utilities and helpers that could replace newly written code
2. Flag any new function that duplicates existing functionality — suggest the existing function instead
3. Flag inline logic that could use an existing utility (hand-rolled string manipulation, manual path handling, etc.)
4. If you find issues, fix them directly in the worktree
5. Run tests after fixes: cd $WORKTREE_DIR && $TEST_CMD
6. Output a short summary of what you found and fixed (or 'No issues found')"

  local quality_prompt="Review the following diff for code quality issues in the codebase at $WORKTREE_DIR.

Diff:
$simplify_diff

Check for:
1. Redundant state that duplicates existing state or could be derived
2. Copy-paste with slight variation that should be unified
3. Leaky abstractions or broken abstraction boundaries
4. Unnecessary comments that explain WHAT instead of WHY
5. If you find issues, fix them directly in the worktree
6. Output a short summary of what you found and fixed (or 'No issues found')"

  local efficiency_prompt="Review the following diff for efficiency issues in the codebase at $WORKTREE_DIR.

Diff:
$simplify_diff

Check for:
1. N+1 query patterns, redundant computations, repeated file reads
2. Missing select_related/prefetch_related on Django querysets
3. Template .count or .all in loops (should use annotate)
4. Unnecessary work on hot paths
5. If you find issues, fix them directly in the worktree
6. Output a short summary of what you found and fixed (or 'No issues found')"

  # Run all three analyses in parallel
  local reuse_file quality_file efficiency_file
  reuse_file=$(mktemp)
  quality_file=$(mktemp)
  efficiency_file=$(mktemp)
  # shellcheck disable=SC2064
  trap "rm -f '$reuse_file' '$quality_file' '$efficiency_file'" RETURN

  claude_run_tracked "step5-reuse" "$issue" "$reuse_prompt" > "$reuse_file" 2>&1 &
  local reuse_pid=$!

  claude_run_tracked "step5-quality" "$issue" "$quality_prompt" > "$quality_file" 2>&1 &
  local quality_pid=$!

  claude_run_tracked "step5-efficiency" "$issue" "$efficiency_prompt" > "$efficiency_file" 2>&1 &
  local efficiency_pid=$!

  log_msg INFO step5 "Waiting for 3 parallel analyses to complete..."
  wait $reuse_pid $quality_pid $efficiency_pid || true

  echo ""
  echo "─── Reuse Review ───────────────────────────────────────────"
  cat "$reuse_file"
  echo ""
  echo "─── Quality Review ─────────────────────────────────────────"
  cat "$quality_file"
  echo ""
  echo "─── Efficiency Review ────────────────────────────────────────"
  cat "$efficiency_file"
  echo "────────────────────────────────────────────────────────────"
  echo ""

  # Re-run tests after simplification fixes
  log_msg INFO step5 "Re-running tests after simplification..."
  (cd "$WORKTREE_DIR" && eval "$TEST_CMD") || {
    log_msg WARN step5 "Tests failed after simplification."
  }

  # Amend changes into existing commits
  if [[ -n $(git -C "$WORKTREE_DIR" status --porcelain 2>/dev/null) ]]; then
    log_msg INFO step5 "Amending simplification fixes into commits..."
    git -C "$WORKTREE_DIR" add -A
    git -C "$WORKTREE_DIR" commit --amend --no-edit
  fi

  task_state_write "$issue" "in_progress" "step5_simplified" "step5b"
}

run_step5b() {
  local issue="$1"
  banner "Step 5b: Self-review (/review)" step5b

  log_msg INFO step5b "Agent is reviewing its own changes..."

  # Inject known reviewer patterns
  local known_patterns=""
  known_patterns=$(get_review_patterns 15 2>/dev/null || true)

  # Load review guidelines from command file
  local review_guidelines
  review_guidelines=$(_load_command "review")

  local review_prompt="You are reviewing your own work on issue #$issue.

Working directory: $WORKTREE_DIR

Known reviewer preferences (address these proactively):
${known_patterns:-No review patterns tracked yet.}

Review guidelines (from project commands):
${review_guidelines:-Review the changes critically for bugs, edge cases, missing test coverage, code style, security, and consistency with CLAUDE.md.}

After reviewing:
1. Run: cd $WORKTREE_DIR && git diff origin/$BASE_BRANCH..HEAD
2. Check each issue against the review guidelines and known reviewer preferences above
3. If you find issues with Value >= 3/10, fix them and amend the commit
4. Run tests again after fixes: cd $WORKTREE_DIR && $TEST_CMD
5. Update $WORKTREE_DIR/.agent-summary.md if your review changed anything
6. List what you found and fixed"

  if [[ -n "$SESSION_ID" ]]; then
    claude_run_tracked "step5b" "$issue" "$review_prompt" --resume "$SESSION_ID"
  else
    claude_run_tracked "step5b" "$issue" "$review_prompt"
  fi

  task_state_write "$issue" "in_progress" "step5b_reviewed" "step6"
}

run_step6() {
  local issue="$1"
  banner "Step 6: Autonomous push" step6

  # Verify tests and lint pass
  log_msg INFO step6 "Running final quality checks..."

  local lint_ok=true test_ok=true
  (cd "$WORKTREE_DIR" && eval "$LINT_CMD") || lint_ok=false
  (cd "$WORKTREE_DIR" && eval "$TEST_CMD") || test_ok=false

  if [[ "$lint_ok" == "false" || "$test_ok" == "false" ]]; then
    log_msg WARN step6 "Quality checks failed. Pausing for human review."
    notify "Issue #$issue — quality checks failed, needs human review"

    echo "Lint passed: $lint_ok"
    echo "Tests passed: $test_ok"
    echo ""
    echo "Worktree: $WORKTREE_DIR"

    local review_choice
    if $DASHBOARD_MODE; then
      dashboard_request "quality_fail" \
        "Quality checks failed (lint: $lint_ok, tests: $test_ok). Fix or abort?" \
        '["fix","abort"]' \
        "{\"issue\":\"$issue\",\"worktree\":\"$WORKTREE_DIR\"}" \
        false
      review_choice="$DASHBOARD_RESPONSE"
    else
      review_choice=$(ask "Action? (fix / abort): ")
    fi
    case "$review_choice" in
      fix|f)
        log_msg INFO step6 "Sending back to development..."
        task_state_write "$issue" "in_progress" "step6_fix" "step4"
        run_from_step "step4" "$issue"
        return $?
        ;;
      *)
        pause_task "$issue" "step6_failed" "Quality checks failed"
        return 1
        ;;
    esac
  fi

  # Run invariants (machine-checkable constraints)
  log_msg INFO step6 "Running invariants..."
  if ! run_invariants "$WORKTREE_DIR" "$BASE_BRANCH"; then
    log_msg WARN step6 "Invariant violations detected. Sending back to fix..."
    local inv_output
    inv_output=$(run_invariants "$WORKTREE_DIR" "$BASE_BRANCH" 2>&1 || true)

    # Try auto-fix via Claude
    claude_run_tracked "step6-invariants" "$issue" "Invariant checks failed before push. Fix the violations.

Working directory: $WORKTREE_DIR

Invariant failures:
$inv_output

Fix the issues, then run:
- $LINT_CMD
- $TEST_CMD
- Amend the commit: cd $WORKTREE_DIR && git add -A && git commit --amend --no-edit" 2>/dev/null || true

    # Re-check invariants after fix attempt
    if ! run_invariants "$WORKTREE_DIR" "$BASE_BRANCH"; then
      pause_task "$issue" "step6_invariants" "Invariant violations could not be auto-fixed"
      return 1
    fi
    log_msg INFO step6 "Invariant violations fixed."
  fi

  # Guard against stale branch name -- may have been renamed or task state corrupted
  local actual_branch
  actual_branch=$(worktree_branch "$WORKTREE_DIR")
  if [[ -n "$actual_branch" && "$actual_branch" != "$BRANCH_NAME" ]]; then
    log_msg WARN step6 "Branch mismatch: task state says '$BRANCH_NAME', git says '$actual_branch'. Using git."
    BRANCH_NAME="$actual_branch"
    task_state_set_field "$issue" "branch_name" "$BRANCH_NAME"
  fi

  # All checks passed — push autonomously
  log_msg INFO step6 "All checks passed. Pushing branch..."
  if ! git -C "$WORKTREE_DIR" push -u origin "$BRANCH_NAME" 2>&1; then
    # Non-fast-forward: remote branch has diverged
    local remote_exists
    remote_exists=$(git -C "$WORKTREE_DIR" ls-remote --heads origin "$BRANCH_NAME" 2>/dev/null | head -1)
    if [[ -n "$remote_exists" ]]; then
      log_msg WARN step6 "Push rejected (non-fast-forward). Attempting rebase onto remote."
      if git -C "$WORKTREE_DIR" pull --rebase origin "$BRANCH_NAME" 2>&1; then
        log_msg INFO step6 "Rebase succeeded. Pushing again..."
        git -C "$WORKTREE_DIR" push -u origin "$BRANCH_NAME" || {
          pause_task "$issue" "step6_push" "Failed to push branch $BRANCH_NAME after rebase"
          return 1
        }
      else
        git -C "$WORKTREE_DIR" rebase --abort 2>/dev/null || true
        local existing_pr_author
        existing_pr_author=$(gh pr list --head "$BRANCH_NAME" --json author -q '.[0].author.login' 2>/dev/null || echo "")
        local my_login
        my_login=$(gh api user -q '.login' 2>/dev/null || echo "")
        if [[ -z "$existing_pr_author" || "$existing_pr_author" == "$my_login" ]]; then
          log_msg WARN step6 "Rebase failed. Force-pushing (no foreign PR on this branch)."
          git -C "$WORKTREE_DIR" push --force-with-lease -u origin "$BRANCH_NAME" || {
            pause_task "$issue" "step6_push" "Failed to force-push branch $BRANCH_NAME"
            return 1
          }
        else
          pause_task "$issue" "step6_push" "Branch $BRANCH_NAME has an open PR by $existing_pr_author — cannot force-push"
          return 1
        fi
      fi
    else
      pause_task "$issue" "step6_push" "Failed to push branch $BRANCH_NAME"
      return 1
    fi
  fi

  log_msg INFO step6 "Branch pushed successfully."
  task_state_write "$issue" "in_progress" "step6_pushed" "step7"
}

run_step7() {
  local issue="$1"
  banner "Step 7: Creating PR" step7

  # Resume-aware: check if PR already exists
  if [[ -n "$PR_NUMBER" ]]; then
    local pr_exists
    pr_exists=$(gh pr view "$PR_NUMBER" --json number -q '.number' 2>/dev/null || echo "")
    if [[ -n "$pr_exists" ]]; then
      log_msg INFO step7 "PR #$PR_NUMBER already exists. Skipping creation."
      task_state_write "$issue" "in_progress" "step7_pr" "step8"
      return 0
    fi
  fi

  # Guard against stale branch name
  local actual_branch
  actual_branch=$(worktree_branch "$WORKTREE_DIR")
  if [[ -n "$actual_branch" && "$actual_branch" != "$BRANCH_NAME" ]]; then
    log_msg WARN step7 "Branch mismatch: task state says '$BRANCH_NAME', git says '$actual_branch'. Using git."
    BRANCH_NAME="$actual_branch"
    task_state_set_field "$issue" "branch_name" "$BRANCH_NAME"
  fi

  log_msg INFO step7 "Creating PR with Claude..."

  # Load PR creation guidelines from command file
  local pr_guidelines
  pr_guidelines=$(_load_command "pr")

  local issue_title
  issue_title=$(echo "$TICKET_DETAILS" | jq -r '.title // ""' 2>/dev/null)

  local pr_prompt="You are creating a PR for GitHub issue #$issue.

Working directory: $WORKTREE_DIR
Branch: $BRANCH_NAME
Base: $BASE_BRANCH
GitHub repo: $GITHUB_REPO
Issue: #$issue ($issue_title)

PR creation guidelines (from project commands):
${pr_guidelines:-Create PR with: gh pr create --base $BASE_BRANCH --head $BRANCH_NAME}

Context for the PR body:
1. Run: cd $WORKTREE_DIR && git log --oneline origin/$BASE_BRANCH..HEAD
2. Run: cd $WORKTREE_DIR && git diff origin/$BASE_BRANCH --stat
3. Read $WORKTREE_DIR/.agent-summary.md for context

Additional instructions:
- The branch is already pushed. Skip rebase and push steps.
- PR title format: type(scope): description (e.g., feat(imports): add CSV validation)
- PR body MUST include 'Closes #$issue' to auto-close the issue
- PR body format: Description / Changes / Local Testing / Checklist — concise, no walls of text
- After PR is created, assign it: gh pr edit <number> --add-assignee @me
- At the end of your output, print the PR number on its own line prefixed with PR_NUMBER=
- NEVER add Co-Authored-By or any AI reference
- NEVER use emojis in any output"

  local pr_output
  pr_output=$(claude_run_tracked "step7" "$issue" "$pr_prompt")
  echo "$pr_output"

  # Extract PR number from output
  PR_NUMBER=$(echo "$pr_output" | grep -oE 'PR_NUMBER=[0-9]+' | head -1 | sed 's/PR_NUMBER=//')
  if [[ -z "$PR_NUMBER" ]]; then
    PR_NUMBER=$(echo "$pr_output" | grep -oE 'pull/[0-9]+' | head -1 | sed 's|pull/||')
  fi

  task_state_write "$issue" "in_progress" "step7_pr" "step8"
  if [[ -n "$PR_NUMBER" ]]; then
    task_state_set_field "$issue" "pr_number" "$PR_NUMBER"
  fi
}

run_step8() {
  local issue="$1"

  if [[ -z "$PR_NUMBER" ]]; then
    banner "Done (no PR number captured)" step9
    notify "Agent finished — check PR manually" "Glass"
    echo "Could not capture PR number for monitoring."
    return 0
  fi

  banner "Step 8: Monitoring PR #$PR_NUMBER CI" step8

  log_msg INFO step8 "Waiting for CI checks..."
  log_msg INFO step8 "Polling every ${CI_POLL_INTERVAL}s (max ${CI_MAX_WAIT}s)"

  local elapsed=0
  local ci_status=0

  while [[ $elapsed -lt $CI_MAX_WAIT ]]; do
    sleep "$CI_POLL_INTERVAL"
    elapsed=$((elapsed + CI_POLL_INTERVAL))

    local checks_output
    checks_output=$(gh pr checks "$PR_NUMBER" 2>/dev/null || true)
    ci_status=$(echo "$checks_output" | grep -ci "fail" || true)
    local checks_done
    checks_done=$(echo "$checks_output" | grep -vc "pending\|running" || true)
    local checks_total
    checks_total=$(echo "$checks_output" | wc -l | tr -d ' ')

    log_msg INFO step8 "[${elapsed}s] Checks: $checks_done/$checks_total complete, $ci_status failing"

    # If all checks are done and none failing, we're done
    if [[ "$ci_status" == "0" && "$checks_done" -gt 0 ]]; then
      local still_pending
      still_pending=$(echo "$checks_output" | grep -c "pending\|running" || true)
      if [[ "$still_pending" == "0" ]]; then
        log_msg INFO step8 "All CI checks passed!"
        break
      fi
    fi

    # If CI is failing, classify and act
    if [[ "$ci_status" -gt 0 && "$elapsed" -gt 120 ]]; then
      local ci_action
      ci_action=$(classify_ci_failure "$PR_NUMBER" "$WORKTREE_DIR" "$issue")
      log_msg INFO step8 "CI action taken: $ci_action"
      if [[ "$ci_action" == "UNRELATED_FLAGGED" ]]; then
        log_msg WARN step8 "Unrelated failures flagged. Stopping monitor."
        break
      fi
      # Give CI time to re-run after fix
      elapsed=$((elapsed - CI_POLL_INTERVAL * 2))
    fi
  done

  task_state_write "$issue" "in_progress" "step8_monitored" "step8b"
}

run_step8b() {
  local issue="$1"

  if [[ "$REVIEW_ENABLED" != "true" ]]; then
    log_msg INFO step8b "Automated review disabled (REVIEW_ENABLED=$REVIEW_ENABLED). Skipping."
    task_state_write "$issue" "in_progress" "step8b_skipped" "step9"
    return 0
  fi

  if [[ -z "$PR_NUMBER" ]]; then
    log_msg WARN step8b "No PR number — skipping review."
    task_state_write "$issue" "in_progress" "step8b_skipped" "step9"
    return 0
  fi

  banner "Step 8b: Koda Review (PR #$PR_NUMBER)" step8b

  local review_guidelines
  review_guidelines=$(_load_command "review-pr")

  log_msg INFO step8b "Launching Koda (automated reviewer) on PR #$PR_NUMBER..."
  local review_output
  review_output=$(claude_run_tracked "step8b-review" "$issue" "You are Koda, the automated PR reviewer.

Run the /review-pr command for PR #$PR_NUMBER.

PR review guidelines (from project commands):
${review_guidelines:-Review the PR thoroughly and post findings on GitHub.}

Additional instructions:
- GitHub repo: $GITHUB_REPO
- Always switch to the correct GitHub account first: gh auth switch --user $GITHUB_USER
- Post the review to GitHub immediately (do NOT ask for confirmation)
- At the END of your output, print one of these verdict lines:
  VERDICT=APPROVE
  VERDICT=COMMENT
  VERDICT=REQUEST_CHANGES
- NEVER use emojis in any output")

  echo "$review_output"

  local verdict
  verdict=$(echo "$review_output" | grep -oE 'VERDICT=(APPROVE|COMMENT|REQUEST_CHANGES)' | tail -1 | sed 's/VERDICT=//')
  log_msg INFO step8b "Koda verdict: ${verdict:-unknown}"

  if [[ "$verdict" == "REQUEST_CHANGES" ]]; then
    task_state_write "$issue" "in_progress" "step8b_changes_requested" "step8c"
  else
    log_msg INFO step8b "No changes requested. Proceeding to done."
    task_state_write "$issue" "in_progress" "step8b_approved" "step9"
  fi
}

run_step8c() {
  local issue="$1"
  banner "Step 8c: Addressing Koda's Findings (PR #$PR_NUMBER)" step8c

  if [[ -z "$PR_NUMBER" ]]; then
    log_msg WARN step8c "No PR number — skipping."
    task_state_write "$issue" "in_progress" "step8c_skipped" "step9"
    return 0
  fi

  local review_round=1
  local max_rounds="${REVIEW_MAX_ROUNDS:-2}"

  while [[ $review_round -le $max_rounds ]]; do
    log_msg INFO step8c "Addressing review (round $review_round/$max_rounds)..."

    local pr_reviews
    pr_reviews=$(gh api "repos/$GITHUB_REPO/pulls/$PR_NUMBER/reviews" --jq '.[] | "\(.user.login) (\(.submitted_at)): \(.state)\n\(.body)\n"' 2>/dev/null || true)

    local pr_review_comments
    pr_review_comments=$(gh api "repos/$GITHUB_REPO/pulls/$PR_NUMBER/comments" 2>/dev/null || true)

    local address_pr_guidelines
    address_pr_guidelines=$(_load_command "address-pr")

    local worktree_dir="${WORKTREE_DIR:-$WORKTREE_BASE/$issue}"

    local agent_output
    agent_output=$(claude_run_tracked "step8c-fix-r$review_round" "$issue" "You are the GWYM Agent. Koda (the automated reviewer) has requested changes on your PR.

PR #$PR_NUMBER
Working directory: $worktree_dir
GitHub repo: $GITHUB_REPO

Review comments (inline):
$pr_review_comments

Review summaries:
$pr_reviews

PR review handling guidelines (from project commands):
${address_pr_guidelines:-Score each comment 1-10 and fix those scoring 3+, decline those below 3.}

Additional instructions:
- Fix all findings with Value >= 3/10 — these are required
- Findings with Value 1-2/10 — acknowledge but skip
- Lint command: cd $worktree_dir && $LINT_CMD
- Test command: cd $worktree_dir && $TEST_CMD
- NEVER create 'fix' commits — always amend/squash into existing commits
- Push with: cd $worktree_dir && git push --force-with-lease
- After fixing and pushing, reply to each addressed comment on GitHub
- At the END, print: ALL_RESOLVED or NEEDS_HUMAN_INPUT (with bullet list)
- NEVER add Co-Authored-By or any AI reference
- NEVER use emojis in any output")

    echo "$agent_output"

    if echo "$agent_output" | grep -q "NEEDS_HUMAN_INPUT"; then
      log_msg WARN step8c "Agent needs human input on some findings."
      notify "PR #$PR_NUMBER — agent needs help with Koda's review" "Submarine"
      break
    fi

    # Wait for CI after push
    log_msg INFO step8c "Waiting for CI after fix push..."
    local ci_elapsed=0
    while [[ $ci_elapsed -lt $CI_MAX_WAIT ]]; do
      sleep "$CI_POLL_INTERVAL"
      ci_elapsed=$((ci_elapsed + CI_POLL_INTERVAL))
      local checks_output
      checks_output=$(gh pr checks "$PR_NUMBER" 2>/dev/null || true)
      local failing
      failing=$(echo "$checks_output" | grep -c "fail" || echo "0")
      local pending
      pending=$(echo "$checks_output" | grep -c "pending\|running" || echo "0")
      if [[ "$failing" == "0" && "$pending" == "0" ]]; then
        log_msg INFO step8c "CI passed after fixes."
        break
      fi
      if [[ "$failing" -gt 0 && "$ci_elapsed" -gt 120 ]]; then
        log_msg WARN step8c "CI failing after fix push — agent will need to handle."
        break
      fi
    done

    # Re-run Koda for another round if we have rounds left
    review_round=$((review_round + 1))
    if [[ $review_round -le $max_rounds ]]; then
      log_msg INFO step8c "Re-running Koda review (round $review_round)..."
      local re_review_output
      re_review_output=$(claude_run_tracked "step8c-rereview-r$review_round" "$issue" "You are Koda, the automated PR reviewer. This is a re-review after the agent addressed your previous findings.

Run /review-pr for PR #$PR_NUMBER. Focus on whether previous findings were properly addressed and check for any new issues introduced by the fixes.

GitHub repo: $GITHUB_REPO
Always switch to the correct GitHub account first: gh auth switch --user $GITHUB_USER
Post the review to GitHub immediately.
At the END, print: VERDICT=APPROVE, VERDICT=COMMENT, or VERDICT=REQUEST_CHANGES
NEVER use emojis in any output")

      echo "$re_review_output"

      local re_verdict
      re_verdict=$(echo "$re_review_output" | grep -oE 'VERDICT=(APPROVE|COMMENT|REQUEST_CHANGES)' | tail -1 | sed 's/VERDICT=//')
      log_msg INFO step8c "Koda re-review verdict (round $review_round): ${re_verdict:-unknown}"

      if [[ "$re_verdict" != "REQUEST_CHANGES" ]]; then
        log_msg INFO step8c "Koda satisfied. Moving on."
        break
      fi
    fi
  done

  task_state_write "$issue" "in_progress" "step8c_done" "step9"
}

run_step9() {
  local issue="$1"
  banner "Step 9: Done" step9

  if [[ -n "$PR_NUMBER" ]]; then
    show_ready_for_review "$PR_NUMBER"

    echo "Next steps:"
    echo "  1. After approval + merge: $0 --learn-from-pr $PR_NUMBER"
    echo "  2. Clean up: $0 --cleanup"
    echo ""
  else
    notify "Agent finished — check PR manually" "Glass"
    echo "No PR number available. Check GitHub manually."
  fi

  # Record learnings (tiered knowledge)
  log_msg INFO step9 "Recording learnings..."
  mkdir -p "$MEMORY_DIR/sessions/$issue"

  local ingest_guidelines
  ingest_guidelines=$(_load_command "ingest-review")

  local learn_prompt
  learn_prompt="You just completed GitHub issue #$issue. Record what you learned.

Review ingestion guidelines (from project commands):
${ingest_guidelines:-Record learnings from this development session.}

Memory directory: $MEMORY_DIR
Session directory: $MEMORY_DIR/sessions/$issue
Shared knowledge directory: $SHARED_KNOWLEDGE_DIR
Issue: #$issue
Date: $(date +%Y-%m-%d)

Instructions:
- Since this is post-development (not post-review), focus on patterns discovered, mistakes self-corrected, and codebase surprises
- Categorize each learning by tier:
  * SHARED: Universal patterns (Python conventions, git workflow, testing strategies) — write to $SHARED_KNOWLEDGE_DIR/ (create dir if needed)
  * PROJECT: Project-specific patterns — update $MEMORY_DIR/learnings.md
  * SESSION: Task-specific context — write to $MEMORY_DIR/sessions/$issue/decisions.md
- Update $MEMORY_DIR/task-history.md (append: | $(date +%Y-%m-%d) | #$issue | summary | PR created |)
- Do NOT duplicate existing entries
- Keep entries concise — one line per finding"

  claude_run_tracked "step9-learn" "$issue" "$learn_prompt" 2>/dev/null || true

  # Remove in-progress label
  gh issue edit "$issue" --remove-label "in-progress" 2>/dev/null || true

  log_msg INFO step9 "Agent going to sleep."
  task_state_write "$issue" "done" "step9_complete" ""
}

# ─── Investigation Mode ─────────────────────────────────────────────────────

needs_investigation() {
  local ticket_details="$1"
  local was_set=false
  shopt -q nocasematch && was_set=true
  shopt -s nocasematch
  if [[ "$ticket_details" =~ (^|[^a-zA-Z])(spike|investigation|research|design\ doc|architecture\ decision|adr)([^a-zA-Z]|$) ]]; then
    $was_set || shopt -u nocasematch
    return 0
  fi
  $was_set || shopt -u nocasematch
  return 1
}

run_investigation() {
  local issue="$1"
  local use_doc="${2:-false}"
  banner "Investigation Mode" investigate

  log_msg INFO investigate "Running investigation for #$issue..."

  # Fetch issue details if not loaded
  if [[ -z "$TICKET_DETAILS" ]]; then
    TICKET_DETAILS=$(gh issue view "$issue" --json title,body,labels,milestone 2>/dev/null) || true
  fi

  local issue_title issue_body
  issue_title=$(echo "$TICKET_DETAILS" | jq -r '.title // ""' 2>/dev/null)
  issue_body=$(echo "$TICKET_DETAILS" | jq -r '.body // ""' 2>/dev/null)

  local investigate_prompt="You are the GWYM Agent in INVESTIGATION MODE for GitHub issue #$issue.

Issue title: $issue_title
Issue body:
$issue_body

This issue requires research before implementation. Do NOT write code or create commits.

Working directory: $REPO_ROOT (read-only investigation)

Instructions:
1. Read the codebase to understand the problem space:
   - Identify relevant files, modules, and patterns
   - Understand the current architecture in the affected area
   - Check for similar past implementations or related code
2. Search memory for related prior work:
   - Read $MEMORY_DIR/MEMORY.md, $MEMORY_DIR/learnings.md, $MEMORY_DIR/review-feedback.md
3. Identify possible approaches (at least 2-3):
   - For each approach, describe: what changes, trade-offs, effort estimate, risks
   - Recommend one approach with clear reasoning
4. Identify open questions or ambiguities that need human input
5. List files that would need to change for the recommended approach

Output a structured analysis in this format:

## Investigation: #$issue

### Problem Statement
(1-2 paragraphs explaining the problem)

### Current State
(How things work today in the relevant area)

### Approaches Considered

#### Approach A: (name)
- Changes: ...
- Pros: ...
- Cons: ...
- Effort: small/medium/large
- Risk: low/medium/high

#### Approach B: (name)
(same structure)

### Recommendation
(Which approach and why)

### Open Questions
(Numbered list of things that need human decision)

### Files to Change
(List of files that would be modified)

NEVER use emojis. Keep everything professional and plain-text."

  local investigation_output
  investigation_output=$(claude_run_tracked "investigate" "$issue" "$investigate_prompt")

  echo ""
  echo "─── Investigation Results ──────────────────────────────────"
  echo "$investigation_output"
  echo "────────────────────────────────────────────────────────────"
  echo ""

  # Post to GitHub issue as comment
  log_msg INFO investigate "Posting investigation to GitHub issue..."
  gh issue comment "$issue" --body "$investigation_output" 2>/dev/null || {
    log_msg WARN investigate "Failed to post to GitHub issue. Investigation saved to stdout only."
  }

  # If use_doc is true, create a Google Doc
  if [[ "$use_doc" == "true" ]]; then
    log_msg INFO investigate "Creating Google Doc for investigation..."
    local doc_prompt="You are creating a formal investigation document for issue #$issue.

Use the Google Docs MCP tools to:
1. Create a new document titled: 'Investigation: #$issue - $issue_title'
2. Add the following content with proper headings and formatting:

$investigation_output

3. If there are architecture diagrams that would help, describe them
4. Print the document URL at the end (prefixed with DOC_URL=)

Use these MCP tools:
- mcp__google-docs__create_document to create the doc
- mcp__google-docs__add_heading for section headers
- mcp__google-docs__add_paragraph for content

Keep the document professional. No emojis."

    local doc_output
    doc_output=$(claude_run_tracked "investigate-doc" "$issue" "$doc_prompt" \
      --allowedTools "Read,Glob,Grep,Bash,mcp__google-docs__create_document,mcp__google-docs__add_heading,mcp__google-docs__add_paragraph,mcp__google-docs__add_bullet_list,mcp__google-docs__add_numbered_list,mcp__google-docs__add_horizontal_rule")
    echo "$doc_output"

    local doc_url
    doc_url=$(echo "$doc_output" | grep -oE 'DOC_URL=https://[^ ]+' | head -1 | sed 's/DOC_URL=//')
    if [[ -n "$doc_url" ]]; then
      gh issue comment "$issue" --body "Investigation document: $doc_url" 2>/dev/null || true
      log_msg INFO investigate "Google Doc created: $doc_url"
    fi
  fi

  # Pause and wait for human decision
  task_state_write "$issue" "paused" "investigation_posted" "step3"
  task_state_set_field "$issue" "paused_reason" "Investigation posted — awaiting human decision on approach"
  notify "Investigation for #$issue posted — review and pick an approach"

  echo ""
  echo "Investigation posted to GitHub issue."
  echo "Review the analysis, then re-run to continue with the chosen approach."
  echo ""
  echo "To resume: $0 $issue"
}

# ─── Step Dispatcher ─────────────────────────────────────────────────────────

run_from_step() {
  local start_step="$1"
  local issue="$2"
  local steps=(step1 step2 step3 step4 step5 step5b step6 step7 step8 step8b step8c step9)
  local start_idx=0

  for i in "${!steps[@]}"; do
    if [[ "$start_step" == ${steps[$i]}* ]]; then
      start_idx=$i
      break
    fi
  done

  log_msg INFO resume "Starting from ${steps[$start_idx]} for #$issue"

  for ((i=start_idx; i<${#steps[@]}; i++)); do
    $DASHBOARD_MODE && dashboard_write_status "running" "${steps[$i]}"
    "run_${steps[$i]}" "$issue" || {
      log_msg WARN resume "Workflow stopped at ${steps[$i]} for #$issue (exit code: $?)"
      $DASHBOARD_MODE && dashboard_write_status "running" "stopped_${steps[$i]}"
      return $?
    }
  done
}

# ─── Subcommands ──────────────────────────────────────────────────────────────

handle_cleanup() {
  banner "Cleaning up stale worktrees" cleanup
  if [[ -d "$WORKTREE_BASE" ]]; then
    for wt in "$WORKTREE_BASE"/*/; do
      [[ -d "$wt" ]] || continue
      local name
      name=$(basename "$wt")
      echo "  Found: $name"
      local answer
      if $DASHBOARD_MODE; then
        dashboard_request "cleanup" \
          "Remove worktree $name?" \
          '["yes","no"]' \
          "{\"worktree\":\"$name\"}" \
          false
        answer="$DASHBOARD_RESPONSE"
      else
        answer=$(ask "  Remove? (yes/no): ")
      fi
      if [[ "$answer" == "yes" ]]; then
        local branch
        branch=$(jq -r '.branch_name // ""' "$wt/task-state.json" 2>/dev/null || echo "agent/feat/$name")
        cleanup_worktree "$wt" "$branch"
        echo "  Removed."
      fi
    done
  else
    echo "No worktrees found."
  fi
  exit 0
}

handle_learn_from_pr() {
  local pr_number="$1"
  banner "Learning from PR #$pr_number" learn-pr

  log_msg INFO learn-pr "Processing feedback with Claude..."
  _learn_from_pr_core "$pr_number" || {
    log_msg ERROR learn-pr "Failed to learn from PR #$pr_number"
    exit 1
  }

  echo ""
  echo "Done. Check $MEMORY_DIR/ for updated learnings."
  exit 0
}

handle_address_pr() {
  local pr_number="$1"
  banner "Addressing PR #$pr_number review comments" address-pr

  local pr_branch
  pr_branch=$(gh pr view "$pr_number" --json headRefName -q '.headRefName' 2>&1) || {
    log_msg ERROR address-pr "Error fetching PR: $pr_branch"
    exit 1
  }

  # Extract issue number from branch name (pattern: agent/feat/N-slug)
  local issue
  issue=$(echo "$pr_branch" | grep -oE '[0-9]+' | head -1 || echo "")

  local worktree_dir=""
  if [[ -n "$issue" && -d "$WORKTREE_BASE/$issue" ]]; then
    worktree_dir="$WORKTREE_BASE/$issue"
  else
    local wt_name="pr-$pr_number"
    worktree_dir="$WORKTREE_BASE/$wt_name"
    if [[ ! -d "$worktree_dir" ]]; then
      log_msg INFO address-pr "Creating worktree from PR branch: $pr_branch"
      mkdir -p "$WORKTREE_BASE"
      git -C "$REPO_ROOT" fetch origin "$pr_branch"
      git -C "$REPO_ROOT" worktree add "$worktree_dir" "origin/$pr_branch" 2>/dev/null || \
        git -C "$REPO_ROOT" worktree add "$worktree_dir" -b "address/$wt_name" "origin/$pr_branch"
    fi
  fi

  echo "Worktree: $worktree_dir"
  echo "Branch: $pr_branch"
  echo ""

  log_msg INFO address-pr "Fetching review comments..."
  local pr_data
  pr_data=$(gh pr view "$pr_number" --json comments,reviews,body,title,reviewRequests 2>&1)

  # Use conversation threading to get only unaddressed comments
  local pr_review_comments
  pr_review_comments=$(get_unaddressed_comments "$pr_number" 2>/dev/null || \
    gh api "repos/$GITHUB_REPO/pulls/$pr_number/comments" 2>&1)

  local pr_reviews
  pr_reviews=$(gh api "repos/$GITHUB_REPO/pulls/$pr_number/reviews" 2>&1)

  local total_comments
  total_comments=$(echo "$pr_review_comments" | jq 'length' 2>/dev/null || echo "?")
  echo "Found $total_comments review comment(s)."
  echo ""

  local repo_owner repo_name
  repo_owner=$(echo "$GITHUB_REPO" | cut -d/ -f1)
  repo_name=$(echo "$GITHUB_REPO" | cut -d/ -f2)

  log_msg INFO address-pr "Agent is addressing review feedback..."

  local address_pr_guidelines
  address_pr_guidelines=$(_load_command "address-pr")

  local agent_output
  agent_output=$(claude_run_tracked "address-pr" "PR-$pr_number" "You are the GWYM Agent. A PR has been reviewed and you need to address the feedback.

PR #$pr_number
Working directory: $worktree_dir
GitHub repo: $GITHUB_REPO

PR overview:
$pr_data

Review comments (inline):
$pr_review_comments

Review summaries:
$pr_reviews

PR review handling guidelines (from project commands):
${address_pr_guidelines:-Score each comment 1-10 and fix those scoring 3+, decline those below 3.}

Additional instructions:
- Lint command: cd $worktree_dir && $LINT_CMD
- Test command: cd $worktree_dir && $TEST_CMD
- Memory dir: $MEMORY_DIR
- Repo owner/name for GraphQL: $repo_owner / $repo_name
- For inline comment replies: gh api repos/$GITHUB_REPO/pulls/$pr_number/comments/{id}/replies -f body='...'
- After fixing a comment, ALWAYS resolve the thread: gh api graphql -f query='mutation { resolveReviewThread(input: {threadId: \"THREAD_ID\"}) { thread { isResolved } } }'
- To find thread IDs: gh api graphql -f query='{ repository(owner: \"$repo_owner\", name: \"$repo_name\") { pullRequest(number: $pr_number) { reviewThreads(first: 50) { nodes { id isResolved comments(first: 1) { nodes { databaseId body } } } } } }'
- NEVER create 'fix' commits — always amend/squash into existing commits
- Push with: cd $worktree_dir && git push --force-with-lease
- At the END, print: ALL_RESOLVED or NEEDS_HUMAN_INPUT (with bullet list)
- NEVER add Co-Authored-By or any AI reference
- NEVER use emojis in any output")

  echo "$agent_output"
  echo ""

  # Mark comments as addressed
  local comment_ids
  comment_ids=$(echo "$pr_review_comments" | jq -r '.[].id' 2>/dev/null || true)
  if [[ -n "$comment_ids" ]]; then
    while IFS= read -r cid; do
      [[ -n "$cid" ]] && mark_comment_addressed "$pr_number" "$cid"
    done <<< "$comment_ids"
  fi

  # Open VSCode for review of the fixes
  if [[ -n "$VSCODE_CMD" ]]; then
    echo "Opening VSCode to review fixes..."
    open_vscode_worktree "$worktree_dir"
  fi

  # Show what was pushed
  echo ""
  echo "─── Changes pushed ─────────────────────────────────────────"
  git -C "$worktree_dir" log --oneline -3
  echo "────────────────────────────────────────────────────────────"
  echo ""

  # Check if agent needs human help
  if echo "$agent_output" | grep -q "NEEDS_HUMAN_INPUT"; then
    notify "PR #$pr_number — agent needs your help on some comments"

    echo "The agent couldn't resolve some comments on its own."
    echo "Items needing your input are listed above."
    echo ""

    while true; do
      local human_input
      if $DASHBOARD_MODE; then
        dashboard_request "pr_guidance" \
          "Unresolved PR comments remain. Provide guidance, or mark as done." \
          '["done"]' \
          "{\"pr_number\":\"$pr_number\"}" \
          true
        if [[ "$DASHBOARD_RESPONSE" == "done" && -z "$DASHBOARD_RESPONSE_VALUE" ]]; then
          human_input="done"
        elif [[ -n "$DASHBOARD_RESPONSE_VALUE" ]]; then
          human_input="fix:$DASHBOARD_RESPONSE_VALUE"
        else
          human_input="done"
        fi
      else
        human_input=$(ask "Provide guidance (or 'done' when handled on GitHub, or 'fix: <instructions>'): ")
      fi
      case "$human_input" in
        done)
          echo "OK, continuing."
          break
          ;;
        fix:*)
          local instructions="${human_input#fix:}"
          log_msg INFO address-pr "Sending human instructions to the agent..."
          claude_run_tracked "address-pr-fix" "PR-$pr_number" "The developer provided additional guidance for PR #$pr_number:

$instructions

Working directory: $worktree_dir

1. Apply the requested changes
2. Run linter: $LINT_CMD
3. Run tests: $TEST_CMD
4. Amend the commit and push
5. Reply to the relevant GitHub comments explaining what was done
6. Resolve the threads you just addressed"
          echo ""
          echo "Fix applied."
          ;;
        *)
          echo "Use 'done' or 'fix: <instructions>'"
          ;;
      esac
    done
  fi

  # Verify all threads are resolved
  echo ""
  echo "Checking PR thread status..."
  local unresolved
  unresolved=$(gh api graphql -f query="
    query {
      repository(owner:\"$repo_owner\", name:\"$repo_name\") {
        pullRequest(number:$pr_number) {
          reviewThreads(first:100) {
            nodes { isResolved comments(first:1) { nodes { body author { login } } } }
          }
        }
      }
    }" 2>/dev/null | jq '[.data.repository.pullRequest.reviewThreads.nodes[] | select(.isResolved == false)] | length' 2>/dev/null || echo "?")

  if [[ "$unresolved" == "0" ]]; then
    echo "All review threads resolved!"
    echo ""
    show_ready_for_review "$pr_number" "All comments addressed and resolved."
  else
    echo "$unresolved thread(s) still unresolved."
    echo "Handle them on GitHub or run: $0 --address-pr $pr_number"
  fi

  echo "After approval + merge: $0 --learn-from-pr $pr_number"
  exit 0
}

handle_maintain_pr() {
  local pr_number="$1"
  banner "Maintaining PR #$pr_number" maintain-pr

  # Fetch PR metadata
  local pr_json
  pr_json=$(gh pr view "$pr_number" --json headRefName,baseRefName,commits,comments,reviews,body,title,mergeStateStatus 2>&1) || {
    log_msg ERROR maintain-pr "Error fetching PR: $pr_json"
    exit 1
  }

  local pr_branch
  pr_branch=$(echo "$pr_json" | jq -r '.headRefName')
  local merge_status
  merge_status=$(echo "$pr_json" | jq -r '.mergeStateStatus')

  log_msg INFO maintain-pr "Branch: $pr_branch | Merge status: $merge_status"

  # ── 1. Rebase onto fresh main ──────────────────────────────────────────────
  log_msg INFO maintain-pr "Syncing $BASE_BRANCH and rebasing PR branch..."

  local issue
  issue=$(echo "$pr_branch" | grep -oE '[0-9]+' | head -1 || echo "")

  local work_dir=""
  if [[ -n "$issue" && -d "$WORKTREE_BASE/$issue" ]]; then
    work_dir="$WORKTREE_BASE/$issue"
  elif [[ -d "$WORKTREE_BASE/pr-$pr_number" ]]; then
    work_dir="$WORKTREE_BASE/pr-$pr_number"
  fi

  if [[ -n "$work_dir" ]]; then
    log_msg INFO maintain-pr "Using existing worktree: $work_dir"
    git -C "$work_dir" fetch origin "$BASE_BRANCH"
    if ! git -C "$work_dir" rebase "origin/$BASE_BRANCH"; then
      log_msg ERROR maintain-pr "Rebase has conflicts. Aborting rebase — resolve manually."
      git -C "$work_dir" rebase --abort
      exit 1
    fi
    git -C "$work_dir" push --force-with-lease
    log_msg INFO maintain-pr "Branch rebased and pushed."
  else
    log_msg INFO maintain-pr "No worktree found. Rebasing from main repo..."
    git -C "$REPO_ROOT" fetch origin "$BASE_BRANCH" "$pr_branch"
    git -C "$REPO_ROOT" checkout "$pr_branch" 2>/dev/null || git -C "$REPO_ROOT" checkout -b "$pr_branch" "origin/$pr_branch"
    if ! git -C "$REPO_ROOT" rebase "origin/$BASE_BRANCH"; then
      log_msg ERROR maintain-pr "Rebase has conflicts. Aborting rebase — resolve manually."
      git -C "$REPO_ROOT" rebase --abort
      git -C "$REPO_ROOT" checkout "$BASE_BRANCH"
      exit 1
    fi
    git -C "$REPO_ROOT" push --force-with-lease
    git -C "$REPO_ROOT" checkout "$BASE_BRANCH"
    log_msg INFO maintain-pr "Branch rebased and pushed."
  fi

  # ── 2. Update PR description to match current changes ───────────────────────
  log_msg INFO maintain-pr "Updating PR description to match current changes..."

  local pr_review_comments
  pr_review_comments=$(gh api "repos/$GITHUB_REPO/pulls/$pr_number/comments" 2>&1)

  local pr_issue_comments
  pr_issue_comments=$(gh pr view "$pr_number" --json comments -q '.comments[].body' 2>&1)

  local current_diff
  current_diff=$(gh pr diff "$pr_number" --name-only 2>&1)

  local commit_messages
  commit_messages=$(gh pr view "$pr_number" --json commits -q '.commits[].messageHeadline' 2>&1)

  claude_run_tracked "maintain-pr" "PR-$pr_number" "You are the GWYM Agent. Update the PR description to accurately reflect the CURRENT state of PR #$pr_number.

Current PR title: $(echo "$pr_json" | jq -r '.title')

Current PR description:
$(echo "$pr_json" | jq -r '.body')

Commit messages on the PR:
$commit_messages

Files changed:
$current_diff

PR conversation (comments and review discussions):
$pr_issue_comments

Review comments (inline):
$pr_review_comments

Instructions:
1. Read the current PR description and compare it to the actual changes (files, commits)
2. Read the PR conversation to understand what was discussed, what was changed, and what was removed
3. Generate an updated PR description that:
   - Accurately lists ONLY the changes that are currently in the PR (not removed ones)
   - Reflects any decisions made in the conversation (e.g., 'migration removed per reviewer feedback')
   - Keeps the same format/structure as the current description
   - Updates the test plan to match current reality
   - Preserves the issue link (Closes #N)
4. Update the PR description using:
   gh pr edit $pr_number --body '<new body>'
   Use a HEREDOC for the body to preserve formatting.
5. Do NOT change the PR title unless it's factually wrong.
6. Print a short summary of what changed in the description."

  echo ""
  log_msg INFO maintain-pr "PR #$pr_number maintained: rebased on $BASE_BRANCH + description updated."
  exit 0
}

handle_review_pr() {
  local pr_number="$1"
  banner "Koda reviewing PR #$pr_number" review-pr

  local review_guidelines
  review_guidelines=$(_load_command "review-pr")

  log_msg INFO review-pr "Launching Koda on PR #$pr_number..."
  local review_output
  review_output=$(claude_run_tracked "review-pr" "PR-$pr_number" "You are Koda, the automated PR reviewer.

Run the /review-pr command for PR #$pr_number.

PR review guidelines (from project commands):
${review_guidelines:-Review the PR thoroughly and post findings on GitHub.}

Additional instructions:
- GitHub repo: $GITHUB_REPO
- Always switch to the correct GitHub account first: gh auth switch --user $GITHUB_USER
- Post the review to GitHub immediately (do NOT ask for confirmation)
- NEVER use emojis in any output")

  echo "$review_output"
  echo ""
  echo "Koda review posted for PR #$pr_number."
  exit 0
}

# ─── Parallel Orchestration ───────────────────────────────────────────────────

run_single_issue_bg() {
  local issue="$1"
  local log_file="$MEMORY_DIR/agent-$issue.log"

  log_msg INFO parallel "Starting background agent for #$issue (log: $log_file)"

  # Reset per-issue state
  SPECIFIC_ISSUE="$issue"
  TICKET_DETAILS=""
  WORKTREE_DIR=""
  BRANCH_NAME=""
  PR_NUMBER=""
  SESSION_ID=""

  TICKET_DETAILS=$(gh issue view "$issue" --json title,body,labels,milestone 2>/dev/null) || true

  # Check for resume
  if [[ -f "$(task_state_file "$issue")" ]]; then
    restore_task_context "$issue"
    local resume_step
    resume_step=$(jq -r '.next_step // .last_step' "$(task_state_file "$issue")" 2>/dev/null)
    if [[ -n "$resume_step" && "$resume_step" != "null" ]]; then
      task_state_write "$issue" "in_progress" "$resume_step" ""
      run_from_step "$resume_step" "$issue" >> "$log_file" 2>&1
      return $?
    fi
  fi

  run_from_step "step3" "$issue" >> "$log_file" 2>&1
}

handle_parallel() {
  local issues=("$@")

  # Sync main once before any work
  run_step1 "parallel"

  # If no issues specified, auto-select from priority scan
  if [[ ${#issues[@]} -eq 0 ]]; then
    banner "Parallel mode: scanning for issues" parallel

    local scan_results
    scan_results=$(priority_scan)

    if [[ -z "$scan_results" ]]; then
      echo "No tasks available for parallel execution."
      exit 0
    fi

    echo "Priority queue:"
    echo "$scan_results" | nl -ba
    echo ""

    local issue_nums=()
    local count=0
    while IFS='|' read -r _ type key _; do
      [[ $count -ge $MAX_PARALLEL_AGENTS ]] && break
      case "$type" in
        RESUME|PAUSED)
          issue_nums+=("$key")
          count=$((count + 1))
          ;;
        ISSUES)
          local backlog_issues
          local issue_args=("--state" "open" "--json" "number")
          [[ -n "$ISSUE_MILESTONE" ]] && issue_args+=("--milestone" "$ISSUE_MILESTONE")
          backlog_issues=$(gh issue list "${issue_args[@]}" 2>/dev/null \
            | jq -r '.[].number' | head -$((MAX_PARALLEL_AGENTS - count)))
          while IFS= read -r bi; do
            [[ -z "$bi" ]] && continue
            issue_nums+=("$bi")
            count=$((count + 1))
            [[ $count -ge $MAX_PARALLEL_AGENTS ]] && break
          done <<< "$backlog_issues"
          ;;
      esac
    done <<< "$scan_results"

    if [[ ${#issue_nums[@]} -eq 0 ]]; then
      echo "No issues selected for parallel execution."
      exit 0
    fi

    issues=("${issue_nums[@]}")
  fi

  # Cap at MAX_PARALLEL_AGENTS
  if [[ ${#issues[@]} -gt $MAX_PARALLEL_AGENTS ]]; then
    log_msg WARN parallel "Capping at $MAX_PARALLEL_AGENTS agents (requested ${#issues[@]})"
    issues=("${issues[@]:0:$MAX_PARALLEL_AGENTS}")
  fi

  banner "Parallel mode: launching ${#issues[@]} agents" parallel

  local pids=()
  for issue in "${issues[@]}"; do
    local title
    title=$(gh issue view "$issue" --json title -q '.title' 2>/dev/null || echo "unknown")
    echo "  Launching #$issue: $title"
    run_single_issue_bg "$issue" &
    pids+=("$!")
  done

  echo ""
  echo "Agents running in background (${#pids[@]} total)."
  echo "Monitor with: $0 --status"
  echo ""
  echo "Logs:"
  for issue in "${issues[@]}"; do
    echo "  #$issue: $MEMORY_DIR/agent-$issue.log"
  done
  echo ""

  local failed=0
  for pid in "${pids[@]}"; do
    wait "$pid" || failed=$((failed + 1))
  done

  echo ""
  if [[ $failed -eq 0 ]]; then
    echo "All ${#issues[@]} agents completed successfully."
    notify "Parallel run complete: ${#issues[@]} agents finished" "Glass"
  else
    echo "$failed of ${#issues[@]} agents encountered issues."
    notify "Parallel run: $failed agents need attention" "Basso"
  fi

  render_dashboard
}

# ─── Parse arguments ──────────────────────────────────────────────────────────

SPECIFIC_ISSUE=""

# Extract --dashboard flag (can combine with any mode)
ARGS=()
for arg in "$@"; do
  if [[ "$arg" == "--dashboard" ]]; then
    DASHBOARD_MODE=true
  else
    ARGS+=("$arg")
  fi
done
set -- "${ARGS[@]+"${ARGS[@]}"}"

if $DASHBOARD_MODE; then
  dashboard_init
  log_msg INFO dashboard "Dashboard mode enabled (PID $$)"
fi

if [[ "${1:-}" == "--watch" ]]; then
  watch_mode
  exit 0
elif [[ "${1:-}" == "--parallel" ]]; then
  shift
  handle_parallel "$@"
  exit 0
elif [[ "${1:-}" == "--cleanup" ]]; then
  handle_cleanup
elif [[ "${1:-}" == "--learn-from-pr" ]]; then
  [[ -z "${2:-}" ]] && { echo "Usage: $0 --learn-from-pr <PR_NUMBER>"; exit 1; }
  handle_learn_from_pr "$2"
elif [[ "${1:-}" == "--address-pr" ]]; then
  [[ -z "${2:-}" ]] && { echo "Usage: $0 --address-pr <PR_NUMBER>"; exit 1; }
  handle_address_pr "$2"
elif [[ "${1:-}" == "--maintain-pr" ]]; then
  [[ -z "${2:-}" ]] && { echo "Usage: $0 --maintain-pr <PR_NUMBER>"; exit 1; }
  handle_maintain_pr "$2"
elif [[ "${1:-}" == "--review-pr" ]]; then
  [[ -z "${2:-}" ]] && { echo "Usage: $0 --review-pr <PR_NUMBER>"; exit 1; }
  handle_review_pr "$2"
elif [[ "${1:-}" == "--investigate" ]]; then
  [[ -z "${2:-}" ]] && { echo "Usage: $0 --investigate <ISSUE_NUMBER> [--doc]"; exit 1; }
  ISSUE="$2"
  TICKET_DETAILS=$(gh issue view "$ISSUE" --json title,body,labels,milestone 2>/dev/null) || true
  use_doc="false"
  shift 2
  for arg in "$@"; do
    [[ "$arg" == "--doc" ]] && use_doc="true"
  done
  run_investigation "$ISSUE" "$use_doc"
  exit 0
elif [[ "${1:-}" == "--memory" ]]; then
  case "${2:-}" in
    search)
      [[ -z "${3:-}" ]] && { echo "Usage: $0 --memory search <query>"; exit 1; }
      memory_search "$3"
      ;;
    tags)
      [[ -z "${3:-}" ]] && { echo "Usage: $0 --memory tags <tag1,tag2>"; exit 1; }
      memory_search_by_tags "$3"
      ;;
    get)
      [[ -z "${3:-}" ]] && { echo "Usage: $0 --memory get <id>"; exit 1; }
      memory_get "$3"
      ;;
    prune)
      memory_prune_stale
      ;;
    *)
      echo "Usage: $0 --memory <search|tags|get|prune> [args]"
      ;;
  esac
  exit 0
elif [[ "${1:-}" == "--readiness" ]]; then
  banner "Agent Readiness Assessment" readiness
  readiness_prompt=$(_load_command "agent-readiness")
  if [[ -z "$readiness_prompt" ]]; then
    log_msg ERROR readiness "agent-readiness.md not found in .claude/commands/"
    echo "Copy agent-readiness.md to $REPO_ROOT/.claude/commands/ first."
    exit 1
  fi
  log_msg INFO readiness "Running readiness assessment on $REPO_ROOT..."
  claude_run_tracked "readiness" "repo-assessment" "$readiness_prompt" \
    --allowedTools "Read,Edit,Write,Bash,Glob,Grep,TodoWrite,Agent,AskUserQuestion,WebSearch,WebFetch"
  exit 0
elif [[ "${1:-}" == "--status" ]]; then
  render_dashboard
  exit 0
elif [[ "${1:-}" == "--invariants" ]]; then
  case "${2:-}" in
    list)
      invariants_list
      ;;
    check)
      check_dir="${3:-$REPO_ROOT}"
      echo "Running invariants against: $check_dir"
      echo ""
      run_invariants "$check_dir" "$BASE_BRANCH"
      ;;
    *)
      echo "Usage: $0 --invariants <list|check> [directory]"
      echo ""
      echo "  list            Show all defined invariants"
      echo "  check [dir]     Run invariants against a directory (default: repo root)"
      ;;
  esac
  exit 0
elif [[ "${1:-}" == "--knowledge" ]]; then
  case "${2:-}" in
    list)
      knowledge_list
      ;;
    promote)
      [[ -z "${3:-}" ]] && { echo "Usage: $0 --knowledge promote <filename.md>"; exit 1; }
      knowledge_promote "$3"
      ;;
    demote)
      [[ -z "${3:-}" ]] && { echo "Usage: $0 --knowledge demote <filename.md>"; exit 1; }
      knowledge_demote "$3"
      ;;
    *)
      echo "Usage: $0 --knowledge <list|promote|demote> [args]"
      echo ""
      echo "  list               Show all knowledge files across all tiers"
      echo "  promote <file.md>  Copy a project file to shared (cross-project) tier"
      echo "  demote <file.md>   Remove a file from shared tier"
      ;;
  esac
  exit 0
elif [[ "${1:-}" == "--costs" ]]; then
  if [[ ! -f "$COST_FILE" ]]; then
    echo "No cost data found at $COST_FILE"
    exit 0
  fi
  echo "=== Cost Summary ==="
  jq -s '{
    by_issue: (group_by(.issue) | map({issue: .[0].issue, total_usd: (map(.cost_usd) | add | . * 100 | round / 100), sessions: length})),
    by_phase: (group_by(.phase) | map({phase: .[0].phase, total_usd: (map(.cost_usd) | add | . * 100 | round / 100), sessions: length})),
    total: {cost_usd: (map(.cost_usd) | add | . * 100 | round / 100), sessions: length}
  }' "$COST_FILE"
  exit 0
elif [[ -n "${1:-}" ]]; then
  SPECIFIC_ISSUE="$1"
  if [[ ! "$SPECIFIC_ISSUE" =~ ^[0-9]+$ ]]; then
    echo "Invalid issue number: $SPECIFIC_ISSUE (expected a number, e.g., 42)"
    exit 1
  fi
fi

# ─── Pre-flight checks ────────────────────────────────────────────────────────

banner "GWYM Agent — Pre-flight Checks" preflight

for cmd in claude gh git jq; do
  if ! command -v "$cmd" &>/dev/null; then
    log_msg ERROR preflight "Required tool '$cmd' missing. Please install it."
    exit 1
  fi
done

if [[ -z "$VSCODE_CMD" ]]; then
  log_msg WARN preflight "VSCode CLI not found. Review will fall back to terminal only."
  echo "  Install: Cmd+Shift+P → 'Shell Command: Install code command in PATH'"
fi

# Ensure correct GitHub account
local_gh_user=$(gh api user -q '.login' 2>/dev/null || echo "")
if [[ -n "$GITHUB_USER" && "$local_gh_user" != "$GITHUB_USER" ]]; then
  log_msg WARN preflight "GitHub CLI logged in as '$local_gh_user', expected '$GITHUB_USER'. Switching..."
  gh auth switch --user "$GITHUB_USER" 2>/dev/null || true
fi

log_msg INFO preflight "All tools available. Model: $AGENT_MODEL | Budget: \$$MAX_BUDGET/task | Repo: $GITHUB_REPO"
echo ""

# Show existing task landscape
task_state_load_all

# ─── Step 1: Sync main ──────────────────────────────────────────────────────

run_step1 "${SPECIFIC_ISSUE:-new}"

# ─── Step 2: Task selection ────────────────────────────────────────────────

if [[ -z "$SPECIFIC_ISSUE" ]]; then
  banner "Step 2: Scanning priorities" step2

  log_msg INFO step2 "Scanning task landscape..."
  SCAN_RESULTS=$(priority_scan)

  if [[ -n "$SCAN_RESULTS" ]]; then
    echo "Priority queue:"
    echo "$SCAN_RESULTS" | nl -ba
    echo ""

    TOP_ITEM=$(echo "$SCAN_RESULTS" | head -1)
    TOP_PRIORITY=$(echo "$TOP_ITEM" | cut -d'|' -f1)
    TOP_TYPE=$(echo "$TOP_ITEM" | cut -d'|' -f2)
    TOP_KEY=$(echo "$TOP_ITEM" | cut -d'|' -f3)
    TOP_REASON=$(echo "$TOP_ITEM" | cut -d'|' -f4)

    log_msg INFO step2 "Recommended: [$TOP_PRIORITY] $TOP_TYPE #$TOP_KEY — $TOP_REASON"

    if $DASHBOARD_MODE; then
      items_json=$(echo "$SCAN_RESULTS" | jq -Rs 'split("\n") | map(select(length > 0))')
      dashboard_request "select_task" \
        "Priority scan found items. Recommended: [$TOP_PRIORITY] #$TOP_KEY — $TOP_REASON" \
        '["confirm","backlog","skip"]' \
        "{\"recommended\":\"$TOP_KEY\",\"type\":\"$TOP_TYPE\",\"priority\":\"$TOP_PRIORITY\",\"reason\":$(printf '%s' "$TOP_REASON" | jq -Rs .),\"items\":$items_json}" \
        true
      CHOICE="$DASHBOARD_RESPONSE_VALUE"
      [[ -z "$CHOICE" && "$DASHBOARD_RESPONSE" == "confirm" ]] && CHOICE=""
      [[ "$DASHBOARD_RESPONSE" == "skip" ]] && CHOICE="skip"
      [[ "$DASHBOARD_RESPONSE" == "backlog" ]] && CHOICE="backlog"
    else
      CHOICE=$(ask "Proceed with #$TOP_KEY? (Enter=yes, number=pick, issue#=direct, skip=exit): ")
    fi

    if [[ "$CHOICE" == "skip" ]]; then
      echo "Skipping. Goodbye!"
      exit 0
    elif [[ "$CHOICE" == "backlog" ]]; then
      SPECIFIC_ISSUE=""
      # Fall through to GitHub Issues selection below
    fi

    SELECTED_ITEM=""
    if [[ -z "$CHOICE" || "$CHOICE" == "y" || "$CHOICE" == "yes" ]]; then
      SELECTED_ITEM="$TOP_ITEM"
    elif [[ "$CHOICE" =~ ^[0-9]+$ ]]; then
      # Could be a line number or an issue number
      line_item=$(echo "$SCAN_RESULTS" | sed -n "${CHOICE}p")
      if [[ -n "$line_item" ]]; then
        SELECTED_ITEM="$line_item"
      else
        SPECIFIC_ISSUE="$CHOICE"
      fi
    fi

    if [[ -n "$SELECTED_ITEM" && -z "$SPECIFIC_ISSUE" ]]; then
      SEL_TYPE=$(echo "$SELECTED_ITEM" | cut -d'|' -f2)
      SEL_KEY=$(echo "$SELECTED_ITEM" | cut -d'|' -f3)

      case "$SEL_TYPE" in
        RESUME|PAUSED)
          SPECIFIC_ISSUE="$SEL_KEY"
          restore_task_context "$SPECIFIC_ISSUE"
          resume_step=$(jq -r '.next_step // .last_step' "$(task_state_file "$SPECIFIC_ISSUE")" 2>/dev/null)
          [[ -z "$resume_step" ]] && resume_step="step3"
          log_msg INFO step2 "Resuming #$SPECIFIC_ISSUE from $resume_step"
          task_state_write "$SPECIFIC_ISSUE" "in_progress" "$resume_step" ""
          run_from_step "$resume_step" "$SPECIFIC_ISSUE"
          exit 0
          ;;
        CI_FAIL|REVIEW|COMMENTS)
          handle_address_pr "$SEL_KEY"
          ;;
        ISSUES)
          SPECIFIC_ISSUE=""
          ;;
      esac
    fi
  fi

  # GitHub Issues-based task selection
  if [[ -z "$SPECIFIC_ISSUE" ]]; then
    log_msg INFO step2 "Querying GitHub Issues backlog..."
    issue_args=("--state" "open" "--json" "number,title,labels,milestone,assignees")
    [[ -n "$ISSUE_MILESTONE" ]] && issue_args+=("--milestone" "$ISSUE_MILESTONE")

    issue_list=""
    issue_list=$(gh issue list "${issue_args[@]}" 2>&1) || {
      log_msg ERROR step2 "Error querying GitHub Issues: $issue_list"
      exit 1
    }

    if [[ "$issue_list" == "[]" || -z "$issue_list" ]]; then
      echo "No open issues found."
      exit 0
    fi

    echo "Open issues:"
    echo "$issue_list" | jq -r '.[] | "  #\(.number) \(.title) [\(.labels | map(.name) | join(", "))]"'
    echo ""

    SPECIFIC_ISSUE=$(ask "Enter issue number to work on: ")
    if [[ -z "$SPECIFIC_ISSUE" || ! "$SPECIFIC_ISSUE" =~ ^[0-9]+$ ]]; then
      echo "Invalid issue number. Goodbye!"
      exit 0
    fi
  fi
fi

# ─── Run full workflow for selected issue ─────────────────────────────────

TICKET_DETAILS=$(gh issue view "$SPECIFIC_ISSUE" --json title,body,labels,milestone 2>/dev/null) || true

# Check if this is a resume — detect prior progress to avoid re-running completed steps
resume_step=""
sf=$(task_state_file "$SPECIFIC_ISSUE")
if [[ -f "$sf" ]]; then
  resume_step=$(jq -r '.next_step // ""' "$sf" 2>/dev/null || echo "")
  if [[ -n "$resume_step" && "$resume_step" != "step3" ]]; then
    restore_task_context "$SPECIFIC_ISSUE"
    log_msg INFO main "Existing progress found. Resuming #$SPECIFIC_ISSUE from $resume_step"
    task_state_write "$SPECIFIC_ISSUE" "in_progress" "$resume_step" ""
    run_from_step "$resume_step" "$SPECIFIC_ISSUE"
    exit 0
  fi
fi

# Check if this is an investigation/spike issue
if needs_investigation "$TICKET_DETAILS"; then
  echo ""
  echo "This issue appears to be a spike/investigation."
  echo "Running investigation mode (research only, no code changes)."
  echo ""
  if $DASHBOARD_MODE; then
    dashboard_request "confirm" \
      "This issue appears to be a spike/investigation. Proceed with investigation?" \
      '["yes","no","doc"]' \
      "{\"issue\":\"$SPECIFIC_ISSUE\"}" \
      false
    inv_choice="$DASHBOARD_RESPONSE"
  else
    read -r -p "Proceed with investigation? [Y/n/doc] " inv_choice
  fi
  case "${inv_choice,,}" in
    n|no) echo "Skipping investigation, proceeding to development."; run_from_step "step3" "$SPECIFIC_ISSUE" ;;
    doc)  run_investigation "$SPECIFIC_ISSUE" "true" ;;
    *)    run_investigation "$SPECIFIC_ISSUE" "false" ;;
  esac
else
  run_from_step "step3" "$SPECIFIC_ISSUE"
fi
