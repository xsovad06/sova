# Project Automation Kit (PAK)

A standalone application that any software project can install to gain autonomous AI-assisted development capabilities out of the box. Takes issues from your tracker, develops solutions using TDD, self-reviews, creates PRs, monitors CI, addresses review feedback, and learns from mistakes -- all autonomously.

## Features

- **Autonomous Agent** -- picks tasks, develops via TDD, self-reviews, creates PRs, monitors CI
- **Pluggable Task Sources** -- GitHub Issues, JIRA, Linear, or manual input
- **26 Standardized Commands** -- develop, test, review, PR, debug, and more -- works on any project
- **Setup Wizard** -- CLI or web UI to configure the agent for your project
- **Dashboard** -- web UI for monitoring, configuration, and project onboarding
- **Persona System** -- auto-detects your tech stack (Django, FastAPI, React, Go, Rust, Odoo) and loads relevant guidance
- **Knowledge System** -- standardized 4-tier layered knowledge with cross-project learning
- **Invariant Checks** -- pluggable pre-push constraint checks

## Requirements

- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) (`claude`)
- [GitHub CLI](https://cli.github.com/) (`gh`) -- authenticated
- `git`, `jq`
- Optional: `terminal-notifier` (macOS notifications), `sqlite3` (structured memory)

## Installation

### Global Install (Recommended)

```bash
git clone <repo> ~/.claude/project-automation-kit
cd ~/.claude/project-automation-kit

# Add pak to your PATH (add to ~/.zshrc or ~/.bashrc):
export PATH="$HOME/.claude/project-automation-kit:$PATH"
```

### Per-Project Install

```bash
cd /path/to/your-project
pak install .                    # Full install (agent + dashboard)
pak install . --no-dashboard     # Agent only
pak install . --update           # Quick sync (script + personas only)
```

## Quick Start

```bash
# 1. Install into your project
pak install /path/to/project

# 2. Run the setup wizard (optional, for customized config)
pak setup /path/to/project

# 3. Work on an issue
pak run 42

# 4. Or let the agent work autonomously
pak watch
```

## Usage

```bash
# Core workflow
pak run [issue]               # Work on a specific issue (or interactive selection)
pak watch                     # Continuous autonomous mode
pak parallel [issues...]      # Run multiple issues concurrently

# PR operations
pak address-pr <pr>           # Address PR review comments
pak maintain-pr <pr>          # Rebase + sync PR description
pak review-pr <pr>            # Run automated reviewer
pak learn-from-pr <pr>        # Ingest PR review feedback

# Monitoring
pak status                    # Show agent status dashboard
pak costs                     # Show cost tracking
pak dashboard [port]          # Launch web dashboard (default: 8111)

# Investigation
pak investigate <issue>       # Research mode (no code changes)

# Knowledge & quality
pak memory search <query>     # Search agent memory
pak knowledge list             # List knowledge files
pak invariants check           # Run invariant checks
pak readiness                  # Assess repo AI-readiness

# Maintenance
pak cleanup                   # Remove stale worktrees
pak help                      # Show all commands
```

## Workflow

The agent follows an 11-step workflow:

| Step | Name | Description |
|------|------|-------------|
| 1 | Sync main | Fetch and pull latest base branch |
| 2 | Task selection | Pick task from configured source or use provided ID |
| 3 | Create worktree | Isolated git worktree for the task |
| 4 | Development | Autonomous TDD via Claude (tests first, implement, lint, test) |
| 5 | Simplify | Parallel agents check reuse, quality, efficiency |
| 5b | Self-review | Full code review with auto-fix for significant findings |
| 6 | Push | Run invariants + tests + lint; push if all pass |
| 7 | Create PR | PR with conventional title, linked to task |
| 8 | Monitor CI | Poll CI, classify and auto-fix failures |
| 8b | Automated review | Reviewer posts structured findings on the PR |
| 8c | Address review | Agent fixes significant findings, re-review loop |
| 9 | Done | Notification, record learnings, update task state |

## Two Modes

### Mode 1: Zero-Config

Install and the agent works immediately with sensible defaults:

```bash
pak install /path/to/project
pak run 42
```

### Mode 2: Full Integration

Run the setup wizard for tailored configuration:

```bash
pak setup /path/to/project     # CLI wizard
# -- or --
pak dashboard                   # Web UI, go to Setup tab
```

The wizard scans your project, asks preferences (task source, branch naming, commit format, model, budget, invariants), and generates project-specific config.

## Task Sources

| Source | Config | Status |
|--------|--------|--------|
| GitHub Issues | `TASK_SOURCE="github"` | Ready |
| JIRA | `TASK_SOURCE="jira"` | Skeleton |
| Linear | `TASK_SOURCE="linear"` | Skeleton |
| Manual | `TASK_SOURCE="manual"` | Ready |

## Personas

Auto-detected from project files:

| Detection Signal | Persona | Guidance |
|-----------------|---------|----------|
| `manage.py` + Django in requirements | `django` | Models, views, services, migrations |
| `fastapi` in requirements | `fastapi` | Routers, Pydantic, async patterns |
| `package.json` + React | `react` | Components, hooks, testing |
| `go.mod` | `go-service` | Interfaces, error handling, testing |
| `Cargo.toml` | `rust` | Ownership, traits, error handling |
| `__manifest__.py` | `odoo` | ORM, XML views, testing |

Override with `PERSONA_MAP` config or explicit mapping.

## Dashboard

Web-based dashboard built with FastAPI:

```bash
pak dashboard          # http://localhost:8111
```

Tabs:
- **Overview** -- agent status, active tasks
- **Control** -- task assignment, checkpoint responses
- **Setup** -- project onboarding wizard (web form)
- **Settings** -- runtime config, invariant management, persona list
- **Costs** -- per-task and per-project cost tracking
- **Logs** -- real-time agent output streaming
- **Tasks** -- task history and status
- **Memory** -- knowledge browser
- **Queue** -- task queue management

## Knowledge System

Standardized 4-tier knowledge architecture (see [knowledge/KNOWLEDGE.md](knowledge/KNOWLEDGE.md)):

| Tier | Location | Scope | Managed By |
|------|----------|-------|------------|
| 0 | `~/.claude/shared-knowledge/` | Cross-project | User (promote/demote) |
| 1 | `AGENTS.md`, `CLAUDE.md`, `.claude/rules/`, `.claude/commands/` | Project (git-tracked) | Setup wizard + user |
| 2 | `.claude/agent-memory/` | Agent (gitignored) | Agent (auto-learning) |
| 3 | `~/.claude/projects/.../memory/` | Session (auto-managed) | Claude Code |

Promotion flow: Tier 2 (confirmed in 2+ tasks) → Tier 1 (confirmed in 2+ projects) → Tier 0.

```bash
pak knowledge promote learnings.md    # Tier 2 → Tier 1
pak knowledge demote patterns.md      # Remove from shared
```

## Invariants

Pre-push constraint checks (bundled):
- `money-decimal` -- No float() for monetary values
- `no-ai-coauthor` -- No AI co-author references in commits
- `no-emojis` -- No emoji characters in code/docs
- `type-hints` -- New functions must have return type annotations
- `user-scoping` -- QuerySet user filtering (warning only)

Add custom invariants by placing `.sh` scripts in the invariants directory.

## Project Structure

```
project-automation-kit/
  pak                            # CLI entry point
  agent/
    orchestrator.sh              # Main autonomous agent
    install.sh                   # Per-project installer
    setup-wizard.sh              # Interactive setup wizard
    detect-persona.sh            # Auto-detection for personas
    gwym-agent.conf.default      # Config template
    adapters/                    # Task source adapters
      interface.sh               # Adapter interface + loader
      github.sh                  # GitHub Issues adapter
      jira.sh                    # JIRA adapter (skeleton)
      linear.sh                  # Linear adapter (skeleton)
      manual.sh                  # Manual task input
  commands/                       # 26 standardized commands
    develop.md                   # Development workflow
    develop-full.md              # End-to-end (develop + test + review + PR)
    test.md                      # Testing workflow
    review.md                    # Code review
    pr.md                        # PR creation
    address-pr.md                # PR review comment handling
    ingest-review.md             # Post-PR learning extraction
    debug.md                     # Debugging workflow
    ...                          # + 18 more standardized commands
  invariants/
    money-decimal.sh
    no-ai-coauthor.sh
    no-emojis.sh
    type-hints.sh
    user-scoping.sh
  personas/
    django.md                    # Django persona
    fastapi.md                   # FastAPI persona
    react.md                     # React persona
    rust.md                      # Rust persona
    odoo.md                      # Odoo persona
    go-service.md                # Go service persona
    frontend.md                  # Generic frontend persona
    rbac.md                      # RBAC/backend persona
  knowledge/
    KNOWLEDGE.md                 # 4-tier knowledge management system
  templates/
    CLAUDE.md                    # Starter Claude Code config
    AGENTS.md                    # Cross-AI-tool guidance
    agent-memory/                # Memory file templates
      MEMORY.md
      learnings.md
      review-feedback.md
      common-mistakes.md
      task-history.md
  dashboard/
    app/                         # FastAPI web UI
      main.py
      config.py
      routers/                   # API routes
      services/                  # Business logic
      templates/                 # Jinja2 HTML templates
      static/                    # JS + CSS
    requirements.txt
  docs/
    VISION.md                    # Product vision and roadmap
  assets/
    agent-icon.png               # Notification icon
```
