# AI Agent Guidance for SOVA

This file provides cross-cutting guidance for any AI agent (Claude Code, Cursor, CodeRabbit, etc.) working in this repository.

SOVA (Software Orchestration Via Agents) is a standalone application that any software project can install to gain autonomous AI-assisted development capabilities. It takes issues from your tracker, develops solutions using TDD, self-reviews, creates PRs, monitors CI, addresses review feedback, and learns from mistakes. The system is written in Python (CLI, roles, scheduler, dashboard), with a legacy bash orchestrator still present during migration.

## Context Index

| File | Purpose |
|------|---------|
| `CLAUDE.md` | Claude Code-specific commands, run instructions, knowledge system |
| `AGENTS.md` | Cross-cutting conventions for all AI tools (this file) |
| `README.md` | Project overview, installation, usage |
| `docs/VISION.md` | Product vision and roadmap |
| `docs/REWRITE-PLAN.md` | SOVA rewrite plan with phase details |
| `.claude/rules/architecture.md` | Project structure, key paths, design decisions |
| `.claude/rules/bash-patterns.md` | Shell scripting conventions and gotchas |
| `.claude/rules/workflow.md` | Development workflow and task finding |

## Project Structure

```
project-automation-kit/
  sova/                            # Python package (SOVA)
    cli/                           # Typer CLI (sova run, sova triage, etc.)
      app.py                       # Main app, subcommand registration
      commands/                    # Command modules (run, triage, project, pr, admin, memory)
    core/                          # Workflow engine, steps, state machine, context
    roles/                         # Agent roles (triage, researcher, developer, reviewer, dispatcher)
    adapters/                      # Task source plugins (github, jira, linear, manual)
    llm/                           # Claude CLI wrapper, cost tracking
    git/                           # Git operations, worktree management
    ipc/                           # Inter-process: handoff protocol, process control, notifications
    knowledge/                     # Memory CRUD, tier loading, personas, review patterns
    scheduler/                     # Watch loop, parallel executor, server daemon
    dashboard/                     # FastAPI web UI
      app.py                       # App factory
      routers/                     # API routes (overview, runs, costs, control, handoff, memory)
      services/                    # Business logic (run, cost, memory, control, handoff)
      templates/                   # Jinja2 HTML (Catppuccin dark + Tailwind)
      static/                      # JS + CSS + favicon + logo
    commands/                      # Command distribution (catalog, templates, manifest, distribution)
    config/                        # Pydantic Settings + TOML config
    db/                            # SQLAlchemy ORM models + async session
    utils/                         # Logging, shell, formatting
  agent/                           # Legacy bash orchestrator (kept during migration)
  commands/                        # 20 standardized commands (markdown with category frontmatter)
  .githooks/                       # Git hooks (tracked, mirroring CI checks)
  invariants/                      # Pre-push constraint check scripts (bash)
  personas/                        # Tech-stack-specific guidance (markdown)
  knowledge/
    KNOWLEDGE.md                   # 4-tier knowledge management system
  templates/                       # Project scaffolding templates
  deploy/                          # systemd + launchd service files
  tests/                           # pytest suite (403+ tests)
  docs/
    VISION.md                      # Product vision and roadmap
    REWRITE-PLAN.md                # SOVA rewrite plan with phase details
  assets/
    agent-icon.png                 # Notification icon
```

## Project Tracker

This project uses **GitHub Issues** with a project board.

- **GitHub account**: always `xsovad06` (email: `sovicka99@gmail.com`)
- **Issue templates**: `.github/ISSUE_TEMPLATE/` (bug.md, feature.md, task.md)
- **Labels**: `type:` (feature/task/infra/bug), `priority:` (critical/high/medium/low), `area:` (agent/dashboard/commands/personas/invariants/knowledge/docs), `agent:` (triaged/researched/ready/in-progress/in-review/needs-spec/needs-research/human-only)

### Ticket workflow
1. Pick an open issue
2. Create feature branch: `feat/<name>`, `fix/<name>`, or `refactor/<name>`
3. Develop using TDD approach (`/develop`, `/test`)
4. PR links to issue via `Closes #<number>`
5. After merge: run `/after-merge`

## Key Conventions

### Code Patterns -- Bash (agent, invariants, CLI)
- **set -euo pipefail** at the top of every script
- **Quoting**: always double-quote variables (`"$var"`, `"${arr[@]}"`)
- **Functions**: use `snake_case`, declare `local` variables
- **Logging**: use helper functions (e.g., `log_info`, `log_error`) -- not raw `echo`
- **Config**: shell-sourceable key=value files (`. "$CONFIG_FILE"`)
- **Exit codes**: 0 = success, 1 = error, 2 = usage error
- **ShellCheck**: all bash scripts must pass `shellcheck` with no warnings
- **No bashisms in shebangs**: use `#!/usr/bin/env bash`

### Code Patterns -- Python (CLI, agent, dashboard)
- **Type hints**: required on all function signatures
- **f-strings**: preferred for string formatting
- **No emojis**: not in code or documentation
- **Line length**: 120 max
- **Formatter**: Ruff (lint + format) when available

### Code Patterns -- Markdown (commands, personas, knowledge)
- **Frontmatter**: commands use YAML frontmatter (`name`, `description`, `user-invocable`)
- **Headings**: use ATX style (`#`, `##`, `###`)
- **No emojis**: not in documentation
- **Code blocks**: always specify language (```bash, ```python, etc.)

### Testing
- **All checks**: `make check` (lint + test, CI-equivalent)
- **Bash scripts**: `make lint-bash` (shellcheck) + `make test-bash` (invariant `--help`)
- **Python**: `make test-py` (pytest suite in `tests/`, 403+ tests covering all `sova/` modules)
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
Types: `feat`, `fix`, `refactor`, `test`, `docs`, `chore`.
Scopes: `agent`, `dashboard`, `commands`, `personas`, `invariants`, `knowledge`, `cli`, `docs`, `scheduler`, `ipc`, `adapters`, `roles`, `core`.

Examples:
- `feat(agent): add Linear task source adapter`
- `fix(dashboard): correct cost tracking calculation`
- `refactor(cli): simplify pak argument parsing`
- `docs(readme): update installation instructions`

### Pull Requests
- Always assign PRs to `xsovad06`
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
- Change project configuration files (`sova.toml`, `pak-agent.conf`, `.claude/settings.json`)
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

## Development Workflow
- **SSH**: repo-level `core.sshCommand` is configured for the personal key
- **Before any push**: ask the user for explicit approval
- **After any merge**: run `/after-merge` (pull main, delete branches, update memory)
- **Before creating PR**: run `/review` (lint, test, review changes)
- **Commit policy**: no fix-on-fix, no separate doc commits for changes in the same PR. Use `/rearrange-commits` before pushing.

## Agentic Workflow Commands

This repo has Claude Code commands in `.claude/commands/`:

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
- `/ship-pr` -- rebase approved PR, push, write handoff for dashboard
- `/approve-merge` -- merge PR (squash), delete branch, post-merge cleanup
- `/agent-resume` -- smart router: assess PR state and decide next action

### Project Management
- `/new-feature` -- set up a new feature branch
- `/standup` -- daily context summary
- `/find-task` -- browse GitHub Issues backlog

### Post-Work
- `/after-merge` -- post-merge cleanup
