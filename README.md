# Project Automation Kit (PAK)

A standalone application that any software project can install to gain autonomous AI-assisted development capabilities out of the box. Takes issues from your tracker, develops solutions using TDD, self-reviews, creates PRs, monitors CI, addresses review feedback, and learns from mistakes -- all autonomously.

## Features

- **Autonomous Agent** -- picks tasks, develops via TDD, self-reviews, creates PRs, monitors CI
- **Pluggable Task Sources** -- GitHub Issues, JIRA, Linear, or manual input
- **19 Standardized Commands** -- develop, test, review, PR, debug, and more -- works on any project
- **Setup Wizard** -- CLI or web UI to configure the agent for your project
- **Dashboard** -- web UI for monitoring, configuration, and project onboarding
- **Persona System** -- auto-detects your tech stack (Django, FastAPI, Odoo) and loads relevant guidance
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
git clone <repo> ~/project-automation-kit
cd ~/project-automation-kit

# Option A: Symlink (preferred)
ln -sf "$(pwd)/pak" ~/.local/bin/pak

# Option B: Add to PATH (in ~/.zshrc or ~/.bashrc)
export PATH="$HOME/project-automation-kit:$PATH"
```

After either option, `pak` is available globally -- run it from inside any project directory.

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

### Dashboard (Recommended)

The dashboard is the primary interface for controlling agents, monitoring tasks, and onboarding projects:

```bash
make serve                       # Start dashboard at http://localhost:8111
```

### CLI

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

# Issue preparation
pak triage                    # Triage all open issues for autonomous suitability
pak triage <issue>            # Triage a single issue
pak harden                    # Harden all open issues (enrich body + re-triage)
pak harden <issue>            # Harden a single issue
pak harden --dry-run          # Preview hardening without posting to GitHub
pak harden <issue> --dry-run  # Preview hardening for a single issue

# Knowledge & quality
pak memory search <query>     # Search agent memory
pak knowledge list             # List knowledge files
pak invariants check           # Run invariant checks
pak readiness                  # Assess repo AI-readiness

# Maintenance
pak cleanup                   # Remove stale worktrees
pak help                      # Show all commands
```

### Development

```bash
make check                    # Run linter + tests (CI-equivalent)
make test                     # Run all tests (bash + python)
make lint                     # ShellCheck + Ruff
make format                   # Auto-format Python code
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
| `__manifest__.py` | `odoo` | ORM, XML views, testing |

More personas (React, Go, Rust, etc.) planned. Override with `PERSONA_MAP` config or explicit mapping.

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
- `branch-naming` -- Enforce branch naming conventions
- `commit-format` -- Enforce conventional commit format
- `money-decimal` -- No float() for monetary values
- `no-ai-coauthor` -- No AI co-author references in commits
- `no-emojis` -- No emoji characters in code/docs
- `type-hints` -- New functions must have return type annotations
- `user-scoping` -- QuerySet user filtering (warning only)

Add custom invariants by placing `.sh` scripts in the invariants directory.

## Issue Triage & Hardening

Before the agent works on issues, two preparation commands improve success rates.

### Triage (`pak triage`)

Heuristic scoring (no LLM call) that classifies issues into three autonomy levels:

| Verdict | Score | Meaning |
|---------|-------|---------|
| `autonomous` | >= 4 | Agent can handle without human intervention |
| `guided` | 1-3 | Agent can develop but expects checkpoints |
| `human` | < 1 | Needs human-led development |

Scoring signals:

| Signal | Score | Examples |
|--------|-------|---------|
| Bug/fix label | +3 | `type: bug`, `fix`, `patch` |
| Refactor/chore label | +2 | `refactor`, `chore`, `tech-debt` |
| Feature label | +1 | `type: feature`, `enhancement` |
| Acceptance criteria (checkboxes) | +3 | `- [ ] criterion` in body |
| File/module hints | +2 | `apps/`, `src/`, `lib/` in body |
| Model/schema details | +1 | Field names, types, endpoints |
| Test plan included | +1 | `test plan`, `pytest`, `factory` |
| Out-of-scope constraints | +1 | `do not modify`, `out of scope` |
| Detailed description (200+ chars) | +1 | Substantive body text |
| Vague description (<50 chars) | -2 | Minimal body |
| Creative/design task | -4 | Logo, brand identity, illustration |
| Complex UI/visualization | -3 | D3, canvas, animation, mockup |
| Research/spike label | -3 | `research`, `spike`, `investigate` |
| Architectural/migration work | -2 | Schema change, API redesign |
| External dependency | -1 | Third-party API, OAuth, webhook |
| Critical priority | -1 | Human oversight recommended |

Triage applies `agent:autonomous`, `agent:guided`, or `agent:human` labels automatically.

### Hardening (`pak harden`)

LLM-powered issue enrichment that transforms vague issues into agent-ready tickets. The hardening prompt asks Claude to produce a complete updated issue body with:

- **Technical Approach** -- architecture, data flow, key design decisions
- **Models & Schemas** -- concrete field names, types, endpoint signatures
- **Acceptance Criteria** -- specific, testable checkboxes with security/scoping checks
- **Scope** -- affected files/components, complexity estimate, dependencies
- **Conflict Check** -- flags overlapping issues to avoid duplicate work
- **Implementation Hints** -- key files, patterns, gotchas

The hardened content **replaces the issue body** (not just a comment) so agents read it directly. After updating the body, hardening automatically re-runs triage with the enriched content.

Use `--dry-run` to preview the analysis without posting:

```bash
pak harden 42 --dry-run     # Preview, then review before applying
pak harden 42               # Apply to GitHub
```

## Project Structure

```
project-automation-kit/
  pak                            # CLI entry point
  Makefile                       # Development workflow (make serve/test/lint/check)
  agent/
    orchestrator.sh              # Main autonomous agent
    install.sh                   # Per-project installer
    setup-wizard.sh              # Interactive setup wizard
    detect-persona.sh            # Auto-detection for personas
    pak-agent.conf.default       # Config template
    adapters/                    # Task source adapters
      interface.sh               # Adapter interface + loader
      github.sh                  # GitHub Issues adapter
      jira.sh                    # JIRA adapter (skeleton)
      linear.sh                  # Linear adapter (skeleton)
      manual.sh                  # Manual task input
  commands/                       # 19 standardized commands
    develop.md                   # Development workflow
    develop-full.md              # End-to-end (develop + test + review + PR)
    test.md                      # Testing workflow
    review.md                    # Code review
    pr.md                        # PR creation
    address-pr.md                # PR review comment handling
    ingest-review.md             # Post-PR learning extraction
    debug.md                     # Debugging workflow
    ...                          # + 11 more standardized commands
  invariants/
    branch-naming.sh
    commit-format.sh
    money-decimal.sh
    no-ai-coauthor.sh
    no-emojis.sh
    type-hints.sh
    user-scoping.sh
  personas/
    django.md                    # Django persona
    fastapi.md                   # FastAPI persona
    odoo.md                      # Odoo persona
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
    tests/                       # pytest smoke tests
    pyproject.toml               # pytest + ruff config
    requirements.txt
  docs/
    VISION.md                    # Product vision and roadmap
    PORTING.md                   # Integration guide for new projects
    handoff-protocol.md          # Inter-agent handoff protocol spec
    IDEAS-FROM-MORNING-AGENT.md  # Future ideas from predecessor agent
  assets/
    agent-icon.png               # Notification icon
```
