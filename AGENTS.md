# AI Agent Guidance for SOVA

This file provides cross-cutting guidance for any AI agent (Claude Code, Cursor, CodeRabbit, etc.) working in this repository.

SOVA (Software Orchestration Via Agents) is a standalone application that any software project can install to gain autonomous AI-assisted development capabilities. It takes issues from your tracker, develops solutions using TDD, self-reviews, creates PRs, monitors CI, addresses review feedback, and learns from mistakes. The system is written in Python (CLI, roles, scheduler, dashboard).

## Context Index

| File | Purpose |
|------|---------|
| `CLAUDE.md` | Claude Code-specific commands, run instructions, knowledge system |
| `AGENTS.md` | Cross-cutting conventions for all AI tools (this file) |
| `README.md` | Project overview, installation, usage |
| `docs/VISION.md` | Product vision and roadmap |
| `.claude/rules/architecture.md` | Project structure, key paths, design decisions |
| `.claude/rules/bash-patterns.md` | Shell scripting conventions and gotchas |
| `.claude/rules/workflow.md` | Development workflow and task finding |
| `docs/security-guidelines.md` | Credential handling, input sanitization, subprocess safety |
| `docs/performance-guidelines.md` | Timeouts, caching, concurrency, async patterns |
| `docs/error-handling-guidelines.md` | Exceptions, logging, retries, fallbacks, circuit breakers |
| `docs/api-contracts-guidelines.md` | Dashboard API, adapter ABC, LLM output formats |
| `docs/database-guidelines.md` | ORM models, session management, migrations |
| `docs/testing-guidelines.md` | pytest patterns, fixtures, mocking, test isolation |
| `docs/integration-guidelines.md` | GitHub/Jira/SonarCloud integration, handoff protocol |
| `docs/jira-configuration-guide.md` | Jira Cloud setup, JQL filters, status mapping, troubleshooting |

## Project Structure

```
sova/
  sova/                            # Python package (SOVA)
    cli/                           # Typer CLI (sova run, sova triage, etc.)
      app.py                       # Main app, subcommand registration
      commands/                    # Command modules (run, triage, harden, project, pr, admin, memory, migrate, commands, server, mcp, supervisor)
    core/                          # Workflow engine, steps, state machine, context, output writer, DAG executor
    roles/                         # Agent roles (triage, researcher, developer, reviewer, custom, planner, dispatcher)
    adapters/                      # Task source plugins (github; jira, linear, manual planned)
    llm/                           # Claude CLI wrapper, cost tracking
    git/                           # Git operations (branch, pr, rebase), worktree management
    ipc/                           # Inter-process: handoff protocol, process control, notifications
    knowledge/                     # Memory CRUD, tier loading, personas, review patterns
    scheduler/                     # Watch loop, parallel executor, server daemon
    dashboard/                     # FastAPI web UI
      app.py                       # App factory
      routers/                     # 25 API routers (overview, runs, costs, control, feed, handoff, lifecycle, memory, logs, tasks, queue, quota, settings, setup, agents, work, roles, spec, prs, dependencies, resources, supervisor, fleet_manager, fleet_insights, telemetry)
      services/                    # 36 services (run, cost, memory, control, feed, handoff, lifecycle, queue, batch, work, work_item, task, log, settings, setup, agent_lifecycle, agent_output, agent_recovery, agent_handoff, agent_pool, agent_db, agent_status, agent_context, agent_progress, agent_validation, output, role, spec, pr, resource, llm_suggestion, output_stream, fleet, fleet_manager, supervisor, telemetry_push)
      templates/                   # Jinja2 HTML (Catppuccin dark + Tailwind)
      static/                      # JS + CSS + favicon + logo
    mcp/                           # MCP server (provider-agnostic agent tools via Model Context Protocol)
    awareness/                     # Awareness subsystem (AwarenessProvider ABC, AwarenessItem, ItemCategory, provider registry, BriefingService aggregation engine, rendering models)
    oversight/                     # Oversight agent: background daemon, operations persona (user-maintained LLM guidance)
    supervisor/                    # Supervisor-level services (TaskProgressionEngine: dependency-aware deterministic state machine; CodeRabbit quota tracking; task dependency graph)
    commands/                      # Command + guideline distribution (catalog, templates, manifest, distribution)
    config/                        # Pydantic Settings + TOML config + project registry + request context
    monitoring/                     # Resource monitoring (psutil-based CPU, memory, I/O tracking)
    db/                            # SQLAlchemy ORM models + async session
    utils/                         # Logging, shell, formatting
  commands/                        # 30 standardized commands (markdown with category frontmatter)
  .githooks/                       # Git hooks (tracked, mirroring CI checks)
  invariants/                      # Pre-push constraint check scripts (bash)
  guidelines/                      # Distributable guideline templates (installed to .claude/rules/)
  skills/                          # Distributable skill templates (installed to .claude/skills/)
  personas/                        # Tech-stack-specific guidance (markdown)
  knowledge/
    KNOWLEDGE.md                   # 4-tier knowledge management system
  templates/                       # Project scaffolding templates
  deploy/                          # systemd + launchd service files
  tests/                           # pytest suite (3200+ tests, 5198 at last count)
  docs/
    VISION.md                      # Product vision and roadmap
    ARCHITECTURE.md                # Architecture overview (points to .claude/rules/)
    *-guidelines.md                # Domain-specific guidelines (7 files)
    design-system.md               # Dashboard design system reference
    handoff-protocol.md            # Agent handoff protocol
    naming-journey.md              # How SOVA got its name
  assets/
    agent-icon.png                 # Notification icon
```

## Project Tracker

This project uses **GitHub Issues** with a project board.

- **GitHub account**: use the `github_user` configured in `sova.toml` (verify with `gh auth status`)
- **Issue templates**: `.github/ISSUE_TEMPLATE/` (bug.md, feature.md, task.md)
- **Labels**: `type:` (feature/task/infra/bug), `priority:` (critical/high/medium/low), `area:` (agent/dashboard/commands/personas/invariants/knowledge/docs), `agent:` (triaged/researched/ready/in-progress/in-review/needs-spec/needs-research/human-only)

### Ticket workflow
1. Pick an open issue
2. Create feature branch: `feat/<name>`, `fix/<name>`, or `refactor/<name>`
3. Develop using TDD approach (`/develop`, `/test`)
4. PR links to issue via `Closes #<number>`
5. After merge: run `/after-merge`

## Key Conventions

### Code Patterns: Bash (invariants)
- **set -euo pipefail** at the top of every script
- **Quoting**: always double-quote variables (`"$var"`, `"${arr[@]}"`)
- **Functions**: use `snake_case`, declare `local` variables
- **Exit codes**: 0 = success, 1 = error, 2 = usage error
- **ShellCheck**: all bash scripts must pass `shellcheck` with no warnings
- **No bashisms in shebangs**: use `#!/usr/bin/env bash`

### Code Patterns: Python (CLI, agent, dashboard)
- **Type hints**: required on all function signatures
- **f-strings**: preferred for string formatting
- **No emojis**: not in code or documentation
- **No double dashes**: never use `--` as a separator in prose, comments, descriptions, PR bodies, review comments, or any text output. Use a colon, period, comma, or parentheses instead. Example: `Fixed: updated type hint` not `Fixed -- updated type hint`. This applies everywhere: code comments, commit messages, PR descriptions, review findings, issue bodies, command descriptions, and documentation.
- **Line length**: 120 max
- **Formatter**: Ruff (lint + format) when available

### Code Patterns: Markdown (commands, personas, knowledge)
- **Frontmatter**: commands use YAML frontmatter (`name`, `description`, `user-invocable`)
- **Headings**: use ATX style (`#`, `##`, `###`)
- **No emojis**: not in documentation
- **Code blocks**: always specify language (```bash, ```python, etc.)

### Testing
- **All checks**: `make check` (lint + test, CI-equivalent)
- **Bash scripts**: `make lint-bash` (shellcheck on invariants) + `make test-bash` (invariant `--help`)
- **Python**: `make test-py` (pytest suite in `tests/` covering all `sova/` modules)
- **Invariants**: each invariant script should handle `--help` gracefully
- **Commands**: validate markdown structure (frontmatter, sections)

### Documentation
- **Every code change must leave docs accurate** -- verify before committing
- `README.md` -- update when project structure, features, usage, or commands change
- `CLAUDE.md` -- update when run commands, knowledge tiers, or workflow change
- `AGENTS.md` -- update when conventions, testing, or tooling change
- `.claude/rules/architecture.md` -- update when components, config, or design decisions change
- `docs/VISION.md` -- update when roadmap phases change
- Keep docs concise: document what exists, not plans
- The `/review` command checks doc freshness automatically -- stale docs score 4/10+

### Commit Messages
Conventional commits format: `type(scope): short description`.
Types: `feat`, `fix`, `refactor`, `test`, `docs`, `chore`, `ci`.
Scopes: `dashboard`, `commands`, `personas`, `invariants`, `knowledge`, `cli`, `docs`, `scheduler`, `ipc`, `adapters`, `roles`, `core`, `config`, `supervisor`, `mcp`, `monitoring`, `db`, `awareness`.
The `ci` type is scopeless (repo-wide infrastructure); all other types require a scope.

Examples:
- `feat(adapters): add Linear task source adapter`
- `fix(dashboard): correct cost tracking calculation`
- `refactor(cli): simplify argument parsing`
- `ci: add invariants job to CI pipeline`
- `docs(readme): update installation instructions`

### Pull Requests
- Always assign PRs to the user configured as `github_user` in `sova.toml`
- Link to issue via `Closes #<number>` in PR body
- Never include "Generated with Claude Code" or similar AI branding in PR descriptions
- Include a Test Plan section

### Agent Autonomy Boundaries

**Always do** (no approval needed):
- Run `shellcheck` on changed bash scripts before committing
- Run tests and linters before creating PRs
- Fix lint/shellcheck warnings in code you wrote
- Read any file in the repo to understand context
- Create feature branches from main

**Ask first** (requires explicit user approval):
- Push to remote / create PRs
- Modify database schemas or migration files
- Add, remove, or upgrade dependencies
- Delete files or branches
- Modify CI/CD pipeline configuration
- Change project configuration files (`sova.toml`, `.claude/settings.json`)
- Run commands that affect external services (GitHub API writes, notifications)

**Never do**:
- Add AI co-author lines or AI branding in commits/PRs
- Force-push to main/master
- Commit secrets (`.env`, credentials, API keys)
- Commit generated files (`.DS_Store`, `__pycache__/`, `*.pyc`, `dashboard/.venv/`)
- Use fix-on-fix commits -- squash fixes into the commit they fix
- Create separate doc commits for changes in the same PR
- Use emojis in code, documentation, or commit messages
- Skip pre-commit hooks (`--no-verify`)

### Role Chaining and Circuit Breaker

Agents chain autonomously: Developer -> Reviewer -> Developer (address review). The address-review circuit breaker prevents infinite bot re-review loops (e.g., CodeRabbit repeatedly requesting changes). It counts completed address-review runs by querying `TaskRun` records with an `address_review` `StepExecution`, filtered by issue and PR number. When the count reaches `pipeline.max_address_review_cycles` (default 2, 0=unlimited), auto-execution is blocked and a manual-only handoff is written so the dashboard shows "Address Review (manual)" and "Integrate PR" buttons. See `.claude/rules/architecture.md` for full details.

## Development Workflow
- **SSH**: repo-level `core.sshCommand` is configured for the personal key
- **Before any push**: ask the user for explicit approval
- **After any merge**: run `/after-merge` (pull main, delete branches, update memory)
- **Before creating PR**: run `/review` (lint, test, review changes)
- **Commit policy**: no fix-on-fix, no separate doc commits for changes in the same PR. Use `/rearrange-commits` before pushing.

## Agentic Workflow Commands

This repo has Claude Code commands in `.claude/commands/`:

### Planning
- `/spec` -- produce a structured spec document for an issue; approving it guides `/develop`

### Core Development
- `/develop` -- implement a feature or fix (TDD approach)
- `/develop-full` -- full workflow: TDD, lint, test, self-review, commit organization
- `/test` -- run linter and tests iteratively
- `/review` -- pre-push code review with scoring and auto-fix (>=3/10)
- `/review-full` -- full pre-push pipeline: simplify -> review -> rearrange-commits
- `/debug` -- systematic debugging workflow

### Pull Requests
- `/pr` -- create PR with standard template (supports AI feedback + incremental workflows)
- `/rearrange-commits` -- reorganize branch commits into clean steps

### Shipping Pipeline
- `/integrate-pr` -- full pipeline: rebase, CI, merge, cleanup, learn (one click)
- `/approve-merge` -- merge PR (squash), delete branch, post-merge cleanup
- `/agent-resume` -- smart router: assess PR state and decide next action

### Post-Work
- `/after-merge` -- post-merge cleanup
- `/ingest-review` -- extract review feedback into agent memory
- `/extract-knowledge` -- promote learnings across knowledge tiers

### Project Management
- `/new-feature` -- set up a new feature branch
- `/standup` -- daily context summary
- `/find-task` -- browse GitHub Issues backlog
