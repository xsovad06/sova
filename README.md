# SOVA -- Software Orchestration Via Agents

A standalone application that any software project can install to gain autonomous AI-assisted development capabilities out of the box. Takes issues from your tracker, triages them, develops solutions using TDD, self-reviews, creates PRs, monitors CI, addresses review feedback, and learns from mistakes -- all autonomously.

## Features

- **Role-Based Agents** -- specialized triage, researcher, developer, and reviewer roles with automatic dispatch
- **Mandatory Pipeline** -- Triage -> Research -> Develop with gate checks between every step
- **Pluggable Task Sources** -- GitHub Issues, JIRA, Linear, or manual input
- **20 Standardized Commands** -- develop, test, review, PR, debug, and more -- works on any project
- **Dashboard** -- web UI for monitoring runs, costs, agent control, and memory
- **Scheduler** -- 24/7 server mode with priority-based watch loop and parallel execution
- **Handoff System** -- agents write state for the next agent; dashboard renders action buttons
- **Knowledge System** -- 4-tier layered knowledge with cross-project learning
- **Persona System** -- auto-detects your tech stack (Django, FastAPI, Odoo) and loads relevant guidance

## Requirements

- Python 3.12+
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) (`claude`)
- [GitHub CLI](https://cli.github.com/) (`gh`) -- authenticated
- `git`

## Installation

```bash
git clone <repo> ~/project-automation-kit
cd ~/project-automation-kit
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

After installation, the `sova` command is available in your virtualenv.

## Quick Start

```bash
# 1. Install SOVA into your project
sova install /path/to/project

# 2. Run the setup wizard (optional, for customized config)
sova setup /path/to/project

# 3. Triage an issue
sova triage 42

# 4. Work on an issue
sova run 42

# 5. Or start the server for autonomous operation
sova server start
```

## Usage

### Dashboard

The dashboard is the primary interface for controlling agents, monitoring tasks, and reviewing costs:

```bash
sova dashboard --project /path/to/project    # Start at http://localhost:8111
# or
make serve                                    # Shortcut (uses Makefile)
```

Pages:
- **Overview** -- run summary, active tasks, cost totals
- **Runs** -- task run history with status, cost, and drill-down
- **Costs** -- per-issue, per-model, and daily cost tracking
- **Control** -- start/stop agents, live output streaming, handoff action buttons
- **Memory** -- knowledge browser with search

### CLI

```bash
# Core workflow
sova run [issue]                 # Work on a specific issue
sova triage [issue]              # Triage issues for agent suitability

# Server mode (dashboard + scheduler)
sova server start                # Start daemon (dashboard + watch loop)
sova server stop                 # Stop daemon
sova server status               # Check daemon status

# Project setup
sova install /path/to/project    # Install SOVA into a project
sova setup /path/to/project      # Interactive setup wizard

# PR operations
sova address-pr <pr>             # Address PR review comments
sova maintain-pr <pr>            # Rebase + sync PR description
sova review-pr <pr>              # Run automated reviewer
sova learn-from-pr <pr>          # Ingest PR review feedback

# Monitoring
sova status                      # Show agent status
sova costs                       # Show cost tracking
sova dashboard [--project PATH]  # Launch web dashboard

# Knowledge & commands
sova memory search <query>       # Search agent memory
sova memory prune                # Remove stale memories
sova commands list               # List installed commands
sova commands diff               # Show differences vs canonical
sova commands update             # Sync commands from source

# Maintenance
sova cleanup                     # Remove stale worktrees
```

### Development

```bash
make check                       # Run linter + tests (CI-equivalent)
make test                        # Run all tests (bash + python, 403+ tests)
make lint                        # ShellCheck + Ruff
make format                      # Auto-format Python code
```

## Agent Roles

SOVA uses specialized agent roles instead of a single monolithic agent:

| Role | Purpose | Tracker State |
|------|---------|---------------|
| **Triage** | Assesses issues for agent suitability | Backlog -> Triaged |
| **Researcher** | Investigates codebase, writes spec | Triaged -> Researched |
| **Developer** | TDD development, PR creation | Researched -> In Progress -> In Review -> Done |
| **Reviewer** | Reviews PRs, posts findings | Posts review on PR |

### The Mandatory Pipeline

```
Backlog -> [Triage] -> Triaged -> [Researcher] -> Researched -> [Developer] -> Done
```

Each gate is enforced: the Developer **refuses** issues not in "Researched" state. Use `--force` to bypass for quick fixes.

### Triage Labels

| Label | Meaning |
|-------|---------|
| `agent:ready` | Ready for autonomous development |
| `agent:needs-spec` | Issue needs clearer specification |
| `agent:needs-research` | Needs codebase investigation first |
| `agent:human-only` | Requires human-led development |

## Workflow

The Developer role follows a 12-step workflow with gate checks:

| Step | Name | Gate Check |
|------|------|-----------|
| 1 | Sync main | Base branch up to date |
| 2 | Task selection | Task details loaded |
| 3 | Create worktree | Worktree exists and clean |
| 4 | Development | `git diff` shows changed files |
| 5 | Simplify | Changes still exist after simplification |
| 5b | Self-review | Findings addressed |
| 6 | Push | Tests + invariants pass |
| 7 | Create PR | PR number extracted |
| 8 | Monitor CI | Checks passed |
| 8b | Automated review | Review posted |
| 8c | Address review | Findings resolved |
| 9 | Complete | Learnings recorded |

## Configuration

SOVA uses `sova.toml` per project:

```toml
github_repo = "user/repo"
github_user = ""
base_branch = "main"
test_cmd = "make test"
lint_cmd = "make lint"

[task_source]
type = "github"

[agent]
model = "opus"
max_budget = "10.00"

[triage]
auto_label = true
min_confidence = 0.7

[roles]
default = "developer"
```

Legacy `pak-agent.conf` (shell-sourceable) is also supported as a fallback.

## Task Sources

| Source | Config | Status |
|--------|--------|--------|
| GitHub Issues | `type = "github"` | Ready |
| JIRA | `type = "jira"` | Skeleton |
| Linear | `type = "linear"` | Skeleton |
| Manual | `type = "manual"` | Ready |

## Personas

Auto-detected from project files:

| Detection Signal | Persona | Guidance |
|-----------------|---------|----------|
| `manage.py` + Django in requirements | `django` | Models, views, services, migrations |
| `fastapi` in requirements | `fastapi` | Routers, Pydantic, async patterns |
| `__manifest__.py` | `odoo` | ORM, XML views, testing |

## Knowledge System

Standardized 4-tier knowledge architecture (see [knowledge/KNOWLEDGE.md](knowledge/KNOWLEDGE.md)):

| Tier | Location | Scope | Managed By |
|------|----------|-------|------------|
| 0 | `~/.claude/shared-knowledge/` | Cross-project | User (promote/demote) |
| 1 | `AGENTS.md`, `CLAUDE.md`, `.claude/rules/`, `.claude/commands/` | Project (git-tracked) | Setup wizard + user |
| 2 | `.claude/agent-memory/` | Agent (gitignored) | Agent (auto-learning) |
| 3 | `~/.claude/projects/.../memory/` | Session (auto-managed) | Claude Code |

## Project Structure

```
project-automation-kit/
  sova/                            # Python package
    cli/                           # Typer CLI
    core/                          # Workflow engine, steps, state machine
    roles/                         # Agent roles (triage, researcher, developer, reviewer)
    adapters/                      # Task source plugins
    llm/                           # Claude CLI wrapper, cost tracking
    git/                           # Git operations, worktree management
    ipc/                           # Handoff protocol, process control, notifications
    knowledge/                     # Memory, tiers, personas
    scheduler/                     # Watch loop, parallel executor, server
    dashboard/                     # FastAPI web UI
    commands/                      # Command distribution
    config/                        # Pydantic Settings + TOML
    db/                            # SQLAlchemy ORM + async session
  agent/                           # Legacy bash orchestrator
  commands/                        # 20 standardized commands (markdown)
  invariants/                      # Pre-push constraint scripts (bash)
  personas/                        # Tech-stack guidance (markdown)
  tests/                           # pytest suite (403+ tests)
  deploy/                          # systemd + launchd service files
  docs/                            # Vision, rewrite plan
```
