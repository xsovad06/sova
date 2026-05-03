# SOVA -- Vision Document

> **Status**: Vision / Pre-implementation
> **Author**: Damian Sova
> **Last updated**: 2026-04-16

## What This Becomes

The original agent and Project-instructions repositories merged into a single **SOVA** (Software Orchestration Via Agents) -- a standalone application that any software project can install to gain autonomous AI-assisted development capabilities out of the box.

It is not just a script collection. It is a **product** with a UI, setup wizard, and runtime dashboard.

## Core Idea

A unified repository that ships:

1. **Autonomous Agent** (the orchestrator) -- picks tasks, develops via TDD, self-reviews, creates PRs, monitors CI, addresses review feedback, learns from mistakes
2. **Standardized Skills/Commands** -- a general-purpose command library (develop, test, review, pr, etc.) that works on any project without customization
3. **Project Integration Kit** -- generates project-specific instructions, guardrails, and configuration through a guided setup process
4. **Dashboard App** -- a web UI for monitoring, configuration, and the setup wizard

## Architecture

```
project-automation-kit/
  sova/                          # Python package
    cli/                         # Typer CLI (sova run, sova triage, etc.)
    core/                        # Workflow engine, steps, state machine
    roles/                       # Agent roles (triage, researcher, developer, reviewer)
    adapters/                    # Task source plugins (github, jira, linear, manual)
    dashboard/                   # FastAPI web UI
    scheduler/                   # Watch loop, parallel executor, server daemon
    ...
  commands/                      # 22 standardized commands (markdown)
  invariants/                    # Pluggable pre-push constraint checks
  personas/                      # Framework-specific guidance (Django, FastAPI, Odoo)
  knowledge/
    KNOWLEDGE.md                 # Standardized 4-tier knowledge management system
  templates/
    CLAUDE.md                    # Starter config for Claude Code
    AGENTS.md                    # Cross-AI-tool guidance
  docs/
    VISION.md                    # This file
```

## The Two Modes

### Mode 1: Zero-Config (General Instructions)

Install the kit into any project and the agent works immediately using default, general-purpose instructions:

```bash
sova install /path/to/project
sova run 42                      # Work on issue #42
```

The default commands are written generically enough (referencing CLAUDE.md/AGENTS.md for project specifics) that the agent can develop, test, and review code on any Python, JS, Go, or similar project.

### Mode 2: Full Integration (Project-Specific)

Run the setup wizard (CLI or Dashboard UI) to generate tailored configuration:

```bash
sova setup /path/to/project      # Interactive setup wizard
# -- or --
sova dashboard                   # Open web UI, go to Setup tab
```

The setup wizard:
1. Scans the project (tech stack, testing framework, CI, git conventions)
2. Runs `/agent-readiness` to assess and generate domain guidelines
3. Asks the user preference questions (see below)
4. Generates project-specific config, invariants, and knowledge files
5. Optionally commits the configuration to git

## Setup Wizard -- User Preferences

The wizard (CLI or UI) walks the user through these decisions:

### Repository & Workflow
- **Task source**: GitHub Issues, JIRA, Odoo Tasks, Linear, or manual?
- **Base branch**: main, dev, master, or custom?
- **Branch naming**: feat/fix/refactor prefix? Include issue number?
- **Commit format**: conventional commits? Scope required? Max length?

### Agent Behavior
- **Push permission**: Always ask before pushing? Or auto-push after checks pass?
- **PR creation**: Auto-create? Or just push and notify?
- **Review rounds**: How many self-review/address cycles? (default: 2)
- **Budget**: Max USD per task? (default: $10)
- **Model**: opus, sonnet, or auto-select based on task complexity?

### Code Quality
- **AI co-author in commits**: Include "Co-Authored-By: Claude" or strip it?
- **Invariants**: Which pre-push checks? (type hints, no emojis, custom?)
- **Test requirement**: Must all tests pass before push? Coverage threshold?
- **Lint requirement**: Auto-fix or just report?

### Knowledge Management
- **Knowledge tier setup**: Initialize the standardized 4-tier system (see KNOWLEDGE.md in Project-instructions)
- **Rules files**: Which `.claude/rules/*.md` to seed? (architecture, patterns, testing, ui, models)
- **Agent memory**: Initialize `.claude/agent-memory/` with template files?
- **Shared knowledge**: Enable cross-project knowledge at `~/.claude/shared-knowledge/`?

### PR Standards
- **PR template**: What sections? (Summary, Test Plan, Screenshots, Breaking Changes?)
- **PR title format**: Conventional? Include issue number?
- **Auto-link issues**: "Closes #N" in body?

### Notifications
- **Desktop notifications**: On completion, failure, review needed?
- **Slack integration**: Post to channel on PR creation?

## Dashboard App

Extends the existing FastAPI dashboard with:

### Setup Tab (New)
- Project onboarding wizard (step-by-step form)
- Project list (all installed projects with status)
- Re-run setup / update configuration

### Monitoring Tab (Existing, Enhanced)
- Active agents (per project)
- Task queue and history
- Cost tracking (per project, per task, per phase)
- Knowledge stats (learnings, patterns, memory size)

### Settings Tab (New)
- Global preferences (model, budget defaults)
- Per-project overrides
- Invariant management (enable/disable/add custom)
- Persona management

## Task Source Abstraction

The agent currently hardcodes GitHub Issues. This must become pluggable:

```toml
# In sova.toml:
[task_source]
type = "github"          # github | jira | odoo | linear | manual
```

Adapter interface:
- `list_tasks()` -- List available tasks (filtered by milestone/sprint/priority)
- `get_task(id)` -- Get task details (title, description, labels, assignee)
- `set_status(id, status)` -- Update task status (in-progress, done)
- `link_pr(id, pr_url)` -- Associate PR with task

GitHub adapter exists. JIRA, Odoo (via MCP), and Linear adapters to be built.

## Command Reconciliation

**Status: DONE** (completed 2026-04-16)

The Project-instructions command library and agent commands have been unified:
- **22 standardized commands** live in `commands/` (general-purpose, work for both interactive Claude Code and the autonomous agent)
- Agent-specific wrappers (develop-full) compose the general commands, not duplicate them
- Commands reference CLAUDE.md/AGENTS.md for project conventions -- portable across projects
- Each project tracks all commands in git (not gitignored) so the agent always has them in worktrees and fresh clones
- JIRA references replaced with GitHub Issues (`gh` CLI) throughout
- Commands ported from the original agent to the template: debug, new-feature, status
- Old `jira.md` replaced by generic `issue.md`
- `request-review.md` removed (unnecessary)
- Project-specific commands (e.g., architecture-overview, import-patterns, design, review-pr with Koda personality) stay in the target project alongside the generic ones

## Persona System

Auto-detect project type and load relevant guidance:

| Detection Signal | Persona | Guidance | Status |
|-----------------|---------|----------|--------|
| `manage.py` + Django in requirements | `django.md` | Models, views, services, migrations | Ready |
| `fastapi` in requirements | `fastapi.md` | Routers, Pydantic, async patterns | Ready |
| `__manifest__.py` pattern | `odoo.md` | ORM, XML views, TransactionCase | Ready |
| `go.mod` | `go-service.md` | Interfaces, error handling, testing | Planned |
| `package.json` + React | `react.md` | Components, hooks, testing | Planned |
| `Cargo.toml` | `rust.md` | Ownership, traits, error handling | Planned |

Users can also create custom personas or override detection.

## Installation

### Install
```bash
pip install -e .

# Now available system-wide:
sova install /path/to/project
sova run 42
sova dashboard
sova status
```

## Migration Path

### Phase 1: Merge Repos & Standardize Knowledge [IN PROGRESS]
- [DONE] Merge Project-instructions commands into unified library (consolidated to 19)
- [DONE] Reconcile overlapping commands (develop-full wraps develop, etc.)
- [DONE] Convert all JIRA references to GitHub Issues
- [DONE] Standardize AGENTS.md + CLAUDE.md cooperation model
- [DONE] Define 4-tier knowledge management system (KNOWLEDGE.md)
- [DONE] First integration: Income Processor project (AGENTS.md, slimmed CLAUDE.md, all commands tracked)
- [DONE] Rename internal references from gwym-agent to sova
- [DONE] Copy KNOWLEDGE.md into the kit (`knowledge/KNOWLEDGE.md`)
- [DONE] Copy PORTING.md into the kit (`docs/PORTING.md`)
- [DONE] Merge Project-instructions repo: all commands, templates, knowledge docs
- [DONE] Merge AGENTS.md template (PAK template structure + PI's Domain Guidelines, Knowledge System, Agentic Workflow Commands)
- [DONE] Merge CLAUDE.md template (PAK template structure + PI's Behavioral Preferences, detailed Knowledge tiers)

### Phase 2: Task Source Abstraction
- Extract GitHub adapter from orchestrator
- Build JIRA adapter (commands use `gh` by default, JIRA needs adapter)
- Build Odoo adapter (MCP server already exists)

### Phase 3: Setup Wizard
- CLI wizard (interactive prompts)
- Generates conf, invariants, CLAUDE.md/AGENTS.md from templates + user answers

### Phase 4: Dashboard UI
- [DONE] Extend existing FastAPI dashboard
- [DONE] Add Setup tab with project onboarding + directory browser
- [DONE] Add Settings tab for runtime config
- [DONE] Multi-project support: `/p/{slug}/` routing, project registry, contextvars-based per-request config

### Phase 5: Deploy to All Projects
- Income Processor -- [DONE] agent + unified commands + AGENTS.md + 4-tier knowledge
- ave-monorepo -- already has agent, update commands + AGENTS.md integration
- odoo-dev -- fresh install with Odoo persona + Odoo task adapter

## Multi-Project Workflow

SOVA manages agents across multiple projects from a single dashboard instance.

### Developer Experience

```
sova dashboard                   # One instance, one port (8111)
# Open browser tabs:
#   localhost:8111/               → project list (command center)
#   localhost:8111/p/income-processor/  → agent for Income Processor
#   localhost:8111/p/ave-monorepo/      → agent for Ave Monorepo
#   localhost:8111/setup          → onboard new projects
```

Each browser tab is a fully independent workspace — its own agent status, costs, logs, control panel. Tabs don't interfere with each other because config resolution is per-request (Python contextvars), not global state.

### Architecture

- **Project registry**: `~/.config/sova/projects.json` -- maps slugs to project paths
- **URL routing**: `/p/{slug}/` prefixes all project-scoped pages and APIs
- **Per-request config**: middleware reads slug from URL, sets `contextvars.ContextVar` with the project's `.claude/` data directory; all service calls resolve paths dynamically
- **Registration**: automatic on `sova install` or dashboard Setup; manual via `POST /api/projects/register`

### What's Next

The current implementation handles the routing and config isolation. Key areas to evolve:

1. **Home page as command center** — show cross-project summary: running agents, pending checkpoints, total daily cost, recent activity across all projects. Not just a link list.

2. **Process isolation** — each project needs independent process state (PID, output buffer, notifications). Currently `process_service.py` has a single global `_process`. Needs a dict keyed by project slug.

3. **Cross-project cost dashboard** — aggregate view: "How much have I spent today across all projects?" with breakdown by project.

4. **Quick actions from home** — start/stop agents, see checkpoint alerts, jump to control page — all from the project list without navigating first.

5. **Auto-discovery** -- optionally scan common directories (`~/projects/`, `~/Documents/`) for SOVA-installed projects and suggest registration.

### Phase 6: Intelligence and Autonomy
- Intelligent model routing -- dynamically select Opus/Sonnet/Haiku based on task complexity and phase
- Agent self-assessment -- periodic analysis of approval rates, time-to-merge, cost patterns; auto-tune config
- Team knowledge sharing -- sync generalizable learnings (review feedback, codebase patterns) across SOVA installations
- VM deployment / always-on mode -- systemd service, notification abstraction (email/webhook), hybrid laptop+VM operation
- Task complexity dispatcher -- classify tickets by complexity, route simple tasks to lightweight agent configs

See `docs/IDEAS-FROM-MORNING-AGENT.md` for detailed descriptions and current status of each idea.

## Design Principles

1. **Works out of the box** -- General commands must be good enough without customization
2. **Progressive enhancement** -- Setup wizard adds project-specific quality, but is optional
3. **Single source of truth** -- One repo, one install, updates propagate to all projects
4. **Config over code** -- Project differences live in config files, not in forked scripts
5. **Learn and improve** -- Agent memory, review ingestion, and knowledge extraction create a flywheel
6. **User in control** -- Every destructive or visible action respects user preferences (push, PR, commit format)
