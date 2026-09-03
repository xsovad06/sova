# SOVA -- Vision Document

> **Status**: Post-implementation, pre-release
> **Author**: Damian Sova
> **Last updated**: 2026-07-22

## What This Becomes

The original agent and Project-instructions repositories merged into a single **SOVA** (Software Orchestration Via Agents) -- a standalone application that any software project can install to gain autonomous AI-assisted development capabilities out of the box.

It is not just a script collection. It is a **product** with a CLI, web dashboard, scheduler daemon, and setup wizard.

## Core Idea

A unified repository that ships:

1. **Autonomous Agent** (the orchestrator) -- picks tasks, develops via TDD, self-reviews, creates PRs, monitors CI, addresses review feedback, learns from mistakes. Role-based pipeline: Triage -> Researcher -> Developer -> Reviewer, with autonomous Developer-Reviewer chaining.
2. **Standardized Skills/Commands** -- 27 general-purpose commands (develop, test, review, pr, ship, etc.) that work on any project without customization.
3. **Project Integration Kit** -- generates project-specific instructions, guardrails, and configuration through a guided setup process.
4. **Dashboard App** -- FastAPI web UI with 19 pages for monitoring, agent control, lifecycle tracking, and configuration.
5. **Scheduler Daemon** -- watch loop with priority-based task scanning, parallel agent execution, and combined server mode.

## Architecture

```
sova/
  sova/                            # Python package (15 modules)
    cli/                           # Typer CLI (22 subcommands)
    core/                          # Workflow engine, 26 step implementations, state machine
    roles/                         # Agent roles (triage, researcher, developer, reviewer)
    adapters/                      # Task source plugins (GitHub implemented; JIRA, Linear planned)
    dashboard/                     # FastAPI web UI (16 routers, 22 services, 23 templates)
    scheduler/                     # Watch loop, parallel executor, server daemon
    llm/                           # Claude CLI async wrapper, cost tracking
    mcp/                           # MCP server for tool integration
    git/                           # Branch, PR, rebase (with LLM conflict resolution), worktree
    ipc/                           # Agent process control, handoff protocol, notifications
    knowledge/                     # Memory CRUD, tier loading, persona detection, extraction
    commands/                      # Command distribution (catalog, templates, manifest)
    config/                        # Pydantic Settings v2 + TOML config + project registry
    db/                            # SQLAlchemy 2.0 async ORM (10 models), Alembic migrations
    utils/                         # Logging, shell, formatting
  commands/                        # 27 standardized commands (markdown with frontmatter)
  invariants/                      # Pluggable pre-push constraint checks (bash)
  personas/                        # Framework-specific guidance (Django, FastAPI, Odoo)
  knowledge/
    KNOWLEDGE.md                   # 4-tier knowledge management system
  templates/
    CLAUDE.md                      # Starter config for Claude Code
    AGENTS.md                      # Cross-AI-tool guidance
  docs/
    VISION.md                      # This file
    design-system.md               # Dashboard design system reference
    handoff-protocol.md            # Agent handoff protocol
    naming-journey.md              # How SOVA got its name
  tests/                           # pytest suite (1,982 test functions)
  deploy/                          # systemd + launchd service files
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
- **Knowledge tier setup**: Initialize the standardized 4-tier system (see KNOWLEDGE.md)
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

## Agent Pipeline

### Developer Pipeline (15 steps)
Sync -> Assess -> CreateWorktree -> Develop -> Simplify -> SelfReview -> Commit -> Validate -> Push -> CreatePR -> WaitForExternalReviews -> AddressExternalFindings -> MonitorCI -> ExtractMemory -> HandoffToReviewer

### Address-Review Pipeline (9 steps)
Rebase -> AddressReview -> Commit -> Validate -> Push -> MonitorCI -> ResolveExternalReviews -> ExtractMemory -> HandoffToUser

### Role Chaining
Developer -> Reviewer -> Developer runs autonomously via `HandoffAction.auto_execute`. The Developer writes a handoff to the Reviewer (auto-spawn), the Reviewer writes back to the Developer if findings exist (auto-spawn) or to the user if clean (manual "Integrate PR" button). Issues stay `IN_REVIEW` until the human merges.

## Dashboard App

FastAPI web UI with Catppuccin dark theme, Tailwind CSS, and SVG icon system.

### Pages (14 active)
- **Home**: project list (command center) for multi-project installations
- **Dashboard**: overview with agent strip, pipeline progress, recent activity
- **Agents**: multi-agent control panel (start/stop, status, handoff actions)
- **Work**: issue-centric view linking issues to their TaskRuns and lifecycle
- **Run Detail**: per-run step pipeline, logs, cost breakdown
- **Lifecycle**: issue lifecycle rail (development -> post_pr -> review -> address_review -> integrate -> post_merge)
- **Costs**: per-task and per-model cost tracking
- **Queue**: batch operations (triage/harden multiple issues), progress bar
- **Logs**: real-time agent output streaming
- **Memory**: knowledge base browser (memories, patterns, extractions)
- **Settings**: runtime configuration management
- **Setup**: project onboarding wizard with directory browser
- **Roles**: role management and custom role configuration
- **Role Editor**: visual DAG editor for custom workflow roles
- **Style Guide**: design system reference with live component examples

### API
15 routers under `/api`: overview, runs, costs, control, handoff, lifecycle, memory, logs, tasks, queue, settings, setup, agents, work, roles.

21 backend services covering run management, cost aggregation, agent lifecycle, handoff processing, batch operations, and more.

## Task Source Abstraction

The agent uses a pluggable adapter pattern for task sources:

```toml
# In sova.toml:
[task_source]
type = "github"          # github | jira | linear | manual (planned)
```

### TaskAdapter ABC (14 async methods)
- `list_tasks()` -- list available tasks (filtered by state/labels)
- `get_task(id)` -- get task details (title, description, labels, assignee)
- `get_state(id)` -- get current task state from labels
- `transition_state(id, state)` -- update task state (labels + board move)
- `assign(id, user)` -- assign task to user
- `add_label(id, label)` / `remove_label(id, label)` -- label management
- `post_comment(id, body)` -- post issue comment
- `post_pr_comment(pr, body)` -- post PR comment
- `post_pr_review(pr, body, event, comments)` -- post formal PR review
- `edit_body(id, body)` -- edit issue body
- `link_pr(id, pr_url)` -- associate PR with task
- `get_pr_reviews(pr)` -- get reviews for a pull request

**Implemented**: GitHub (full, including Projects V2 board integration), JIRA Cloud (httpx-based, async, with lifecycle enrichment).
**Planned**: Linear, Odoo (via MCP).

## Persona System

Auto-detect project type and load relevant guidance:

| Detection Signal | Persona | Guidance | Status |
|-----------------|---------|----------|--------|
| `manage.py` + Django in requirements | `django.md` | Models, views, services, migrations | Done |
| `fastapi` in requirements | `fastapi.md` | Routers, Pydantic, async patterns | Done |
| `__manifest__.py` pattern | `odoo.md` | ORM, XML views, TransactionCase | Done |
| `go.mod` | `go-service.md` | Interfaces, error handling, testing | Planned |
| `package.json` + React | `react.md` | Components, hooks, testing | Planned |
| `Cargo.toml` | `rust.md` | Ownership, traits, error handling | Planned |

Users can also create custom personas or override detection.

## Command System

**Status: DONE** (27 standardized commands)

The command library is unified and portable across projects:
- 27 standardized commands in `commands/` (general-purpose, work for both interactive Claude Code and the autonomous agent)
- Agent-specific wrappers (develop-full) compose the general commands, not duplicate them
- Commands reference CLAUDE.md/AGENTS.md for project conventions -- portable across projects
- Each project tracks all commands in git (not gitignored) so the agent always has them in worktrees and fresh clones
- Command distribution system (`sova commands install/update/diff`) handles reconciliation with SHA-256 manifest tracking

## Installation

```bash
pip install -e .

# Now available system-wide:
sova install /path/to/project
sova run 42
sova dashboard
sova server start
sova status
```

## Multi-Project Workflow

SOVA manages agents across multiple projects from a single dashboard instance.

### Developer Experience

```
sova dashboard                   # One instance, one port (8111)
# Open browser tabs:
#   localhost:8111/               -> project list (command center)
#   localhost:8111/p/my-backend/        -> agent for My Backend
#   localhost:8111/p/my-frontend/       -> agent for My Frontend
#   localhost:8111/setup          -> onboard new projects
```

Each browser tab is a fully independent workspace -- its own agent status, costs, logs, control panel. Tabs don't interfere with each other because config resolution is per-request (Python contextvars), not global state.

### Architecture

- **Project registry**: `~/.config/sova/projects.json` -- maps slugs to project paths
- **URL routing**: `/p/{slug}/` prefixes all project-scoped pages and APIs
- **Per-request config**: middleware reads slug from URL, sets `contextvars.ContextVar` with the project's `.claude/` data directory; all service calls resolve paths dynamically
- **Registration**: automatic on `sova install` or dashboard Setup; manual via `POST /api/projects/register`

### Evolving Areas

1. **Home page as command center** -- cross-project summary: running agents, pending checkpoints, total daily cost, recent activity across all projects. Currently a project list; expanding to a full overview.
2. **Process isolation** -- each project has independent process state via `_ProjectAgents` dict in `agent_lifecycle.py`. Further isolation (separate process groups, resource limits) is possible.
3. **Cross-project cost dashboard** -- aggregate view: "How much have I spent today across all projects?" with breakdown by project.
4. **Quick actions from home** -- start/stop agents, see checkpoint alerts, jump to control page from the project list.

## Completed Milestones

| Phase | What | Key Deliverables |
|-------|------|-----------------|
| Phase 0 | Foundation | pyproject.toml, config, DB, utils, CLI skeleton, 21 tests |
| Phase 1 | Adapters + LLM + Git | GitHub adapter, Claude CLI wrapper, git operations, 106 tests |
| Phase 2 | Core Workflow + Roles | WorkflowEngine, state machine, 4 roles, IPC, knowledge, 291 tests |
| Phase 3 | CLI + Triage | 18 CLI subcommands, triage workflow, 318 tests |
| Phase 4 | Dashboard | FastAPI app factory, services, routers, templates, 370 tests |
| Phase 5 | Scheduler + Server | WatchLoop, ParallelExecutor, SOVAServer, systemd/launchd, 403 tests |
| Phase 6 | Cutover | Removed ~5,500 lines bash, migration commands, 503 tests |
| Post-phase | Hardening | Handoff actions, multi-project, batch ops, design system, lifecycle, 1,982 tests |

## Release Roadmap (P3: v0.1 Public Release)

Target: October 2026.

| # | Task | Status |
|---|------|--------|
| #19 | Choose license | Done (Apache 2.0) |
| #23 | CI pipeline | Done |
| #16 | Polish README | Done |
| #6 | Measure velocity (SOVA vs manual) | Open |
| #18 | Deploy to 2+ external projects | Done |
| #20 | Tag v0.1.0 release | Open |
| #22 | Make repository public | Done |
| #21 | Write launch blog post | Open |

## Model Selection & Provider Abstraction (Q4 2026)

Fix model selection crash (unavailable version warning), consolidate fallback logic, and enable multi-provider support (OpenAI, Ollama, Vertex AI). 15 PRs in 5 phases. Minimum viable fix (PR1-PR4): typed errors + client-owned fallback with shared deadline + task-type routing with pinning. See [docs/model-selection-architecture.md](docs/model-selection-architecture.md) and [docs/model-selection-risk-assessment.md](docs/model-selection-risk-assessment.md) for the complete analysis and migration path.

## Future Roadmap (P4+)

| # | Feature | Description |
|---|---------|-------------|
| #33 | VM deployment + always-on mode | systemd service hardening, notification abstraction, hybrid laptop+VM |
| ~~#32~~ | ~~Team knowledge sharing~~ | ~~sync generalizable learnings across SOVA installations~~ (done) |
| ~~#51~~ | ~~Visual role builder~~ | ~~dashboard page for composing custom agent roles as workflow DAGs~~ (done) |
| #24 | Evaluate monetization | Q1 2027 decision: open-core vs hosted vs consulting |
| ~~--~~ | ~~JIRA adapter~~ | ~~JIRA Cloud task source with lifecycle enrichment~~ (done: #220) |
| -- | Additional adapters | Linear, Odoo task source adapters |
| -- | Additional personas | Go, React, Rust framework guidance |
| ~~--~~ | ~~Intelligent model routing~~ | ~~dynamically select Opus/Sonnet/Haiku based on task complexity~~ (done: #155-157) |
| ~~#277~~ | ~~Pipeline determinism~~ | ~~replace LLM calls with deterministic code in PR body, memory extraction, triage, review ingestion~~ (done) |
| ~~#254-259~~ | ~~Resource monitoring~~ | ~~per-agent CPU/memory tracking, live dashboard widget, cross-project aggregation, capacity advisor~~ (done: #254-259) |
| ~~#355~~ | ~~Resource exhaustion guard (Phase 0)~~ | ~~Fix available memory metric: show allocatable memory, not compressed~~ (done: #355) |
| ~~#356-357~~ | ~~Resource exhaustion guard~~ | ~~Pre-spawn gate + dashboard warning banner (#356, Phase 1), supervisor memory pressure gate (#357, Phase 2)~~ (done: #356, #357) |
| ~~#366~~ | ~~Dashboard output polling~~ | ~~Replace 1s HTTP short-poll with SSE + adaptive backoff~~ (done: #366) |
| #367 | Dashboard agent widget UX | Fix: widget persists in DOM after TTL expires (no `loadAgents()` scheduled post-TTL); cost always shows $0.00 (live cost not synced to DB after each successful step) |
| #368 | Dashboard markdown rendering | Render markdown in agent output views: load `marked.js` in `run_detail.html`, replace `textContent` with `innerHTML = marked.parse(...)`, apply `.prose-invert` CSS |
| ~~#410~~ | ~~SyncStep + RebaseStep hardening~~ | ~~Stash before checkout, handle divergent branches, catch worktree lock errors~~ (done: #410) |
| ~~#411~~ | ~~MonitorCI reliability~~ | ~~Increase CI timeout, lint instructions, commit guards~~ (done: #411) |
| ~~#412~~ | ~~Address-PR validation + bypass~~ | ~~Worktree-aware git check, SHA comparison~~ (done: #412) |
| ~~#413~~ | ~~Issue quality gate~~ | ~~Triage-time quality scoring, enrichment, template enforcement~~ (done: #413) |

## Design Principles

1. **Works out of the box** -- general commands must be good enough without customization
2. **Progressive enhancement** -- setup wizard adds project-specific quality, but is optional
3. **Single source of truth** -- one repo, one install, updates propagate to all projects
4. **Config over code** -- project differences live in config files, not in forked scripts
5. **Learn and improve** -- agent memory, review ingestion, and knowledge extraction create a flywheel
6. **User in control** -- every destructive or visible action respects user preferences (push, PR, commit format)
7. **Ephemeral agents** -- spawn, work, write handoff, die. No persistent sessions. Dashboard provides the interactive bridge.
