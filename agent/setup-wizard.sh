#!/usr/bin/env bash
set -euo pipefail

# Project Automation Kit — Interactive Setup Wizard
# Scans the project, asks user preferences, and generates configuration.
#
# Usage:
#   pak setup [path]
#   ./agent/setup-wizard.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PAK_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PROJECT_DIR="$(pwd)"
CONF_DIR="$PROJECT_DIR/.claude/scripts"
CONF_FILE="$CONF_DIR/gwym-agent.conf"

# Colors
BOLD='\033[1m'
DIM='\033[2m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
NC='\033[0m'

header() {
  echo ""
  echo -e "${BOLD}${BLUE}━━━ $1 ━━━${NC}"
  echo ""
}

info() {
  echo -e "${DIM}$1${NC}"
}

prompt() {
  local var_name="$1"
  local prompt_text="$2"
  local default="$3"
  local value

  if [[ -n "$default" ]]; then
    echo -en "${CYAN}$prompt_text${NC} ${DIM}[$default]${NC}: "
  else
    echo -en "${CYAN}$prompt_text${NC}: "
  fi
  read -r value
  value="${value:-$default}"
  eval "$var_name=\"$value\""
}

prompt_choice() {
  local var_name="$1"
  local prompt_text="$2"
  local options="$3"
  local default="$4"

  echo -e "${CYAN}$prompt_text${NC}"
  echo -e "${DIM}  Options: $options${NC}"
  echo -en "${DIM}  [$default]${NC}: "
  read -r value
  value="${value:-$default}"
  eval "$var_name=\"$value\""
}

prompt_yesno() {
  local var_name="$1"
  local prompt_text="$2"
  local default="$3"

  echo -en "${CYAN}$prompt_text${NC} ${DIM}[$default]${NC}: "
  read -r value
  value="${value:-$default}"
  case "$value" in
    [Yy]|[Yy]es|YES) eval "$var_name=true" ;;
    *) eval "$var_name=false" ;;
  esac
}

# ─── Project Scanning ────────────────────────────────────────────────────────

detect_tech_stack() {
  local detected=()

  # Python
  if [[ -f "requirements.txt" || -f "pyproject.toml" || -f "setup.py" || -f "Pipfile" ]]; then
    detected+=("python")
    if [[ -f "manage.py" ]] && grep -q "django" requirements.txt pyproject.toml setup.py 2>/dev/null; then
      detected+=("django")
    fi
    if grep -q "fastapi" requirements.txt pyproject.toml setup.py 2>/dev/null; then
      detected+=("fastapi")
    fi
  fi

  # JavaScript/TypeScript
  if [[ -f "package.json" ]]; then
    detected+=("javascript")
    if grep -q '"react"' package.json 2>/dev/null; then
      detected+=("react")
    fi
    if grep -q '"next"' package.json 2>/dev/null; then
      detected+=("nextjs")
    fi
    if grep -q '"vue"' package.json 2>/dev/null; then
      detected+=("vue")
    fi
    if [[ -f "tsconfig.json" ]]; then
      detected+=("typescript")
    fi
  fi

  # Go
  if [[ -f "go.mod" ]]; then
    detected+=("go")
  fi

  # Rust
  if [[ -f "Cargo.toml" ]]; then
    detected+=("rust")
  fi

  # Odoo
  if find . -maxdepth 3 -name "__manifest__.py" 2>/dev/null | head -1 | grep -q .; then
    detected+=("odoo")
  fi

  # CI
  if [[ -d ".github/workflows" ]]; then
    detected+=("github-actions")
  fi
  if [[ -f ".gitlab-ci.yml" ]]; then
    detected+=("gitlab-ci")
  fi

  echo "${detected[*]}"
}

detect_persona() {
  local stack="$1"
  if [[ "$stack" == *"django"* ]]; then echo "django"
  elif [[ "$stack" == *"fastapi"* ]]; then echo "fastapi"
  elif [[ "$stack" == *"odoo"* ]]; then echo "odoo"
  elif [[ "$stack" == *"react"* ]]; then echo "react"
  elif [[ "$stack" == *"go"* ]]; then echo "go-service"
  elif [[ "$stack" == *"rust"* ]]; then echo "rust"
  else echo ""
  fi
}

detect_test_cmd() {
  if [[ -f "Makefile" ]] && grep -q "^test:" Makefile; then echo "make test"
  elif [[ -f "package.json" ]] && grep -q '"test"' package.json; then echo "npm test"
  elif [[ -f "pyproject.toml" ]] && grep -q "pytest" pyproject.toml; then echo "pytest"
  elif [[ -f "Cargo.toml" ]]; then echo "cargo test"
  elif [[ -f "go.mod" ]]; then echo "go test ./..."
  else echo ""
  fi
}

detect_lint_cmd() {
  if [[ -f "Makefile" ]] && grep -q "^lint:" Makefile; then echo "make lint"
  elif [[ -f "package.json" ]] && grep -q '"lint"' package.json; then echo "npm run lint"
  elif [[ -f "pyproject.toml" ]] && grep -q "ruff" pyproject.toml; then echo "ruff check ."
  elif [[ -f "Cargo.toml" ]]; then echo "cargo clippy"
  elif [[ -f "go.mod" ]]; then echo "golangci-lint run"
  else echo ""
  fi
}

detect_format_cmd() {
  if [[ -f "Makefile" ]] && grep -q "^format:" Makefile; then echo "make format"
  elif [[ -f "package.json" ]] && grep -q '"format"' package.json; then echo "npm run format"
  elif [[ -f "pyproject.toml" ]] && grep -q "ruff" pyproject.toml; then echo "ruff format ."
  elif [[ -f "Cargo.toml" ]]; then echo "cargo fmt"
  elif [[ -f "go.mod" ]]; then echo "gofmt -w ."
  else echo ""
  fi
}

detect_github_repo() {
  local remote_url
  remote_url=$(git remote get-url origin 2>/dev/null || echo "")
  if [[ "$remote_url" =~ github\.com[^:/]*[:/]([^/]+/[^/.]+) ]]; then
    echo "${BASH_REMATCH[1]}"
  fi
}

detect_base_branch() {
  # Check for common base branches
  for branch in main master develop dev; do
    if git rev-parse --verify "$branch" &>/dev/null; then
      echo "$branch"
      return
    fi
  done
  echo "main"
}

# ─── Main Wizard ─────────────────────────────────────────────────────────────

echo ""
echo -e "${BOLD}${GREEN}Project Automation Kit — Setup Wizard${NC}"
echo -e "${DIM}Scanning project and configuring autonomous agent...${NC}"

header "Project Scan"

TECH_STACK=$(detect_tech_stack)
DETECTED_PERSONA=$(detect_persona "$TECH_STACK")
DETECTED_TEST=$(detect_test_cmd)
DETECTED_LINT=$(detect_lint_cmd)
DETECTED_FORMAT=$(detect_format_cmd)
DETECTED_REPO=$(detect_github_repo)
DETECTED_BRANCH=$(detect_base_branch)

echo -e "  Tech stack: ${GREEN}${TECH_STACK:-none detected}${NC}"
echo -e "  Persona:    ${GREEN}${DETECTED_PERSONA:-none}${NC}"
echo -e "  Test cmd:   ${GREEN}${DETECTED_TEST:-not found}${NC}"
echo -e "  Lint cmd:   ${GREEN}${DETECTED_LINT:-not found}${NC}"
echo -e "  GitHub:     ${GREEN}${DETECTED_REPO:-not found}${NC}"
echo -e "  Base:       ${GREEN}${DETECTED_BRANCH}${NC}"

# ─── Repository & Workflow ───────────────────────────────────────────────────

header "Repository & Workflow"

prompt_choice TASK_SOURCE "Task source" "github, jira, linear, manual" "github"
prompt GITHUB_REPO "GitHub repo (owner/name)" "$DETECTED_REPO"
prompt BASE_BRANCH "Base branch" "$DETECTED_BRANCH"
prompt_choice BRANCH_NAMING "Branch naming format" "conventional (feat/42-slug), freeform" "conventional"
prompt_choice COMMIT_FORMAT "Commit format" "conventional (type(scope): desc), freeform" "conventional"

# ─── Agent Behavior ──────────────────────────────────────────────────────────

header "Agent Behavior"

prompt_choice AGENT_MODEL "Default model" "opus, sonnet, auto" "opus"
prompt MAX_BUDGET "Max budget per task (USD)" "10.00"
prompt REVIEW_MAX_ROUNDS "Self-review rounds" "2"
prompt_yesno AUTO_PUSH "Auto-push after checks pass?" "no"

# ─── Code Quality ────────────────────────────────────────────────────────────

header "Code Quality"

prompt TEST_CMD "Test command" "$DETECTED_TEST"
prompt LINT_CMD "Lint command" "$DETECTED_LINT"
prompt FORMAT_CMD "Format command" "$DETECTED_FORMAT"
prompt_yesno NO_AI_COAUTHOR "Strip AI co-author from commits?" "no"

# ─── PR Settings ─────────────────────────────────────────────────────────────

header "PR Settings"

prompt_choice PR_TITLE_FORMAT "PR title format" "conventional, freeform" "conventional"
prompt_yesno PR_AUTO_LINK "Auto-link issues (Closes #N) in PR body?" "yes"

# ─── Knowledge (4-Tier System) ───────────────────────────────────────────────

header "Knowledge Management (4-Tier System)"

info "Tier 0: Shared cross-project knowledge (~/.claude/shared-knowledge/)"
info "Tier 1: Project rules (.claude/rules/) — tracked in git"
info "Tier 2: Agent memory (.claude/agent-memory/) — gitignored"
info "Tier 3: Session memory (auto-managed by Claude Code)"
echo ""

prompt_yesno INIT_RULES "Initialize .claude/rules/ with seed files for detected stack?" "yes"
prompt_yesno TRACK_RULES_IN_GIT "Track project rules (Tier 1) in git?" "yes"
prompt_yesno TRACK_MEMORY_IN_GIT "Track agent memory (Tier 2) in git?" "no"
prompt_yesno ENABLE_SHARED_KNOWLEDGE "Enable shared knowledge (Tier 0) at ~/.claude/shared-knowledge/?" "yes"

# ─── Generate Configuration ──────────────────────────────────────────────────

header "Generating Configuration"

mkdir -p "$CONF_DIR"

cat > "$CONF_FILE" << EOF
# Project Automation Kit — Configuration
# Generated by setup wizard on $(date +%Y-%m-%d)

# ─── Task Source ─────────────────────────────────────────────────────────────
TASK_SOURCE="$TASK_SOURCE"
TASK_SOURCE_CONFIG=""

# ─── Project Settings ─────────────────────────────────────────────────────────
GITHUB_REPO="$GITHUB_REPO"
GITHUB_USER=""
TEST_CMD="$TEST_CMD"
LINT_CMD="$LINT_CMD"
FORMAT_CMD="$FORMAT_CMD"
BASE_BRANCH="$BASE_BRANCH"

# ─── Agent Settings ───────────────────────────────────────────────────────────
AGENT_MODEL="$AGENT_MODEL"
MAX_BUDGET="$MAX_BUDGET"
REVIEW_ENABLED="true"
REVIEW_MAX_ROUNDS=$REVIEW_MAX_ROUNDS

# ─── Commit & PR Settings ────────────────────────────────────────────────────
NO_AI_COAUTHOR="${NO_AI_COAUTHOR}"
COMMIT_FORMAT="$COMMIT_FORMAT"
PR_TITLE_FORMAT="$PR_TITLE_FORMAT"
PR_AUTO_LINK_ISSUES="${PR_AUTO_LINK}"
BRANCH_NAMING="$BRANCH_NAMING"

# ─── Persona ─────────────────────────────────────────────────────────────────
PERSONA_MAP=""  # Auto-detection is default

# ─── Remaining defaults (from gwym-agent.conf.default) ───────────────────────
ISSUE_MILESTONE=""
ISSUE_LABELS=""
SKIP_MANUAL_TEST="true"
AUTO_APPROVE_FIXES="false"
CI_POLL_INTERVAL=60
CI_MAX_WAIT=600
FLAKY_CHECKS=""
SCANNER_GITHUB_CHECK="true"
WATCH_INTERVAL_ACTIVE=300
WATCH_INTERVAL_IDLE=1800
WATCH_AUTO_SELECT_ISSUES="true"
WATCH_VETO_SECONDS=30
WORKTREE_COPY_FILES=".env,.env.local"
WORKTREE_TTL_DONE_DAYS=3
WORKTREE_TTL_PAUSED_DAYS=7
SHARED_KNOWLEDGE_DIR="\$HOME/.claude/shared-knowledge"
INVARIANTS_DIR=""
MAX_PARALLEL_AGENTS=2
COMMIT_AUTHOR=""
SLACK_CHANNEL=""
EOF

echo -e "  ${GREEN}Created:${NC} $CONF_FILE"

# Run the installer to set up the rest
echo ""
echo -e "${DIM}Running installer...${NC}"
"$SCRIPT_DIR/install.sh" --no-dashboard 2>&1 | sed 's/^/  /'

# Initialize knowledge tiers based on preferences

# Tier 1: Project rules
if [[ "$INIT_RULES" == "true" ]]; then
  mkdir -p .claude/rules
  if [[ ! -f .claude/rules/architecture.md ]]; then
    echo "# Architecture" > .claude/rules/architecture.md
    echo "# Patterns and Gotchas" > .claude/rules/patterns.md
    echo "# Testing Conventions" > .claude/rules/testing.md
    echo -e "  ${GREEN}Created:${NC} .claude/rules/ seed files"
  fi
fi

# Tier 0: Shared knowledge directory
if [[ "$ENABLE_SHARED_KNOWLEDGE" == "true" ]]; then
  mkdir -p "$HOME/.claude/shared-knowledge"
  echo -e "  ${GREEN}Enabled:${NC} ~/.claude/shared-knowledge/"
fi

# Update .gitignore based on preferences
if [[ "$TRACK_MEMORY_IN_GIT" == "false" ]]; then
  if ! grep -qF ".claude/agent-memory/" .gitignore 2>/dev/null; then
    echo ".claude/agent-memory/" >> .gitignore
  fi
fi

if [[ "$TRACK_RULES_IN_GIT" == "false" ]]; then
  if ! grep -qF ".claude/rules/" .gitignore 2>/dev/null; then
    echo ".claude/rules/" >> .gitignore
  fi
fi

echo ""
echo -e "${BOLD}${GREEN}Setup complete!${NC}"
echo ""
echo "Next steps:"
echo "  1. Review the generated config: $CONF_FILE"
echo "  2. Run: pak run <issue-number>"
echo "  3. Or start watch mode: pak watch"
echo ""
