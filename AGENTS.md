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
| `docs/architecture-deep-dive.md` | On-demand: full component, module and decision entries (with the decision index); architecture.md keeps a rule line or index entry each |
| `.claude/rules/bash-patterns.md` | Shell scripting conventions and gotchas |
| `.claude/rules/workflow.md` | Development workflow and task finding |
| `docs/model-selection-architecture.md` | LLM model routing, provider abstraction, model discovery/enumeration, design details |
| `docs/model-selection-risk-assessment.md` | Model selection risks, migration path, rollback strategy |
| `docs/model-selection-migration-guide.md` | Config shape for openai/ollama/vertex provider types |
| `docs/security-guidelines.md` | Credential handling, input sanitization, subprocess safety |
| `docs/performance-guidelines.md` | Timeouts, caching, concurrency, async patterns |
| `docs/error-handling-guidelines.md` | Exceptions, logging, retries, fallbacks, circuit breakers |
| `docs/api-contracts-guidelines.md` | Dashboard API, adapter ABC, LLM output formats |
| `docs/database-guidelines.md` | ORM models, session management, migrations |
| `docs/testing-guidelines.md` | pytest patterns, fixtures, mocking, test isolation |
| `docs/deferred-consolidation.md` | Work an audit-driven consolidation pass deferred (import-layering contract, Codex runtime split, `sova/workflow/`), and the behaviour changes of the slices that landed |
| `docs/integration-guidelines.md` | GitHub/Jira/SonarCloud integration, handoff protocol |
| `docs/jira-configuration-guide.md` | Jira Cloud setup, JQL filters, status mapping, troubleshooting |
| `docs/ai-provider-setup-guide.md` | LLM provider setup (`llm.provider`): credentials, model ids, Connections page |
| `docs/commit-convention-setup.md` | Per-project commit types/scopes (`commit.types`, `commit.scopes`) used for generated commit subjects and PR titles |
| `docs/codex-runtime-setup.md` | Codex agent runtime setup (`agent.runtime = "codex"`): auth, sandbox, rollback |
| `docs/openai-only-runbook.md` | Run one pipeline OpenAI-only by env overrides: model fields, isolation, what runs where, rollback, pilot log |
| `docs/invariants.md` | Per-invariant detail for the pre-push and CI checks in `invariants/` |
| `docs/workflow-history.md` | Full text and incident history behind `.claude/rules/workflow.md`'s rules |
| `docs/token-compression.md` | Optional RTK and Headroom token compression setup |
| `docs/commands/health-audit.md` | Maintainer rationale for the health-audit command; not distributed, so the command stays under its size cap (`tests/test_command_size_budget.py`) |

## Project Structure

Top level only (full tree, full entry: docs/ARCHITECTURE.md, "Project Structure"; components: `.claude/rules/architecture.md`).

```
sova/                  # Python package: cli, core, roles, adapters, llm, git, ipc, agents, knowledge,
                       # scheduler, dashboard, a2a, mcp, awareness, oversight, supervisor, commands,
                       # config, monitoring, db, utils
sova/commands/data/    # canonical commands, skills, guidelines, personas: edit here, then re-render
.claude/commands/      # rendered copy Claude Code loads (make commands-render)
.agents/skills/        # rendered copy Codex loads (make skills-render)
plugins/sova/          # generated Claude Code plugin (make marketplace)
invariants/            # pre-push and CI constraint checks (bash)
tests/                 # pytest suite (13000+ tests)
.githooks/  .github/scripts/  knowledge/  templates/  deploy/  docs/  assets/
```

## Project Tracker

This project uses **GitHub Issues** with a project board.

- **GitHub account**: use the configured `github_user` (`sova config get github_user`; verify with `gh auth status`)
- **Issue templates**: `.github/ISSUE_TEMPLATE/` (bug.md, feature.md, task.md)
- **Labels**: `type:` (feature/task/infra/bug/epic), `priority:` (blocker/critical/high/medium/low/undefined), `area:` (agent/dashboard/commands/personas/invariants/knowledge/docs), `agent:` (triaged/researched/ready/in-progress/in-review/needs-spec/needs-research/human-only/interactive-recommended/on-hold)

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
- **No double dashes**: never use `--` as a separator in prose, comments, descriptions, PR bodies, review comments, or any text output. Use a colon, period, comma, or parentheses instead. Example: `Fixed: updated type hint` not `Fixed -- updated type hint`. This applies everywhere: code comments, commit messages, PR descriptions, review findings, issue bodies, command descriptions, and documentation. Enforced on the whole tree, not only on changed lines: `tests/test_no_double_dash.py` runs `invariants/no-double-dash.sh --all` over every tracked file, and the same script checks added lines in pre-push and CI.
- **Line length**: 120 max
- **Formatter**: Ruff (lint + format) when available
- **Never clamp an explicit config value against a broader default**: `min(develop.step_timeout, agent.step_timeout)` silently discards the operator's specific override. Let the more specific value win; if a hard ceiling is genuinely required, log when it fires.
- **Failure must not look like success**: a helper that swallows an error returns a sentinel or a multi-state result, never a value in its success range (`0`, `[]`, `False`). See `docs/error-handling-guidelines.md` "Failure Must Not Look Like Success".
- **Pass context explicitly into deferred work**: anything queued, scheduled, or run in a background task receives `project_dir` and `github_user` as arguments rather than reading an ambient contextvar or the current working directory, which belong to whoever happens to run it later.
- **Falsy-default trap**: never use `x or default` when `x` can be a legitimate falsy non-`None` value (`0`, `""`, `[]`, `False`). It silently discards the real value. Use `x if x is not None else default` or an explicit membership/identity check instead.
- **Reading text files: always specify `encoding="utf-8"` and catch `(OSError, UnicodeDecodeError)`**: bare `path.read_text()` uses the platform default encoding, and `UnicodeDecodeError` is not a subclass of `OSError`, so an `except OSError` guard alone still crashes on a non-UTF-8 file. Use the shared `read_text_or_none()` (`sova/utils/files.py`) instead of another hand-rolled copy (full entry: docs/error-handling-guidelines.md, "Reading Text Files").

### Change Discipline
- **Fan-out audit**: when adding a status value, schema field, signal, or helper, or changing a function's return contract, grep every producer, consumer, sibling closure, and mirrored function and update them in the same PR. A translation function's fallback bucket is where a new value is silently absorbed into the wrong category.
- **LLM-written prose is a claim, not a fact**: before acting on an LLM-written description of code (a PR body, a bot or self-review finding, a doc or cookbook paragraph), grep the cited symbols and check the cited behavior. A finding that contradicts a convention a test enforces is the thing to distrust.

### Code Patterns: Markdown (commands, personas, knowledge)
- **Frontmatter**: commands use YAML frontmatter (`name`, `description`, `user-invocable`)
- **Headings**: use ATX style (`#`, `##`, `###`)
- **No emojis**: not in documentation
- **Code blocks**: always specify language (```bash, ```python, etc.)

### Dependencies
- **After any `pyproject.toml` dependency edit, run `uv lock` and commit `uv.lock`**: CI installs the exact versions in that universal lock (`uv export --locked`, which fails on a stale lock); local installs still use pip.

### Testing
- **All checks**: `make check` (lint + `make test-bash` + `make test-ci`, the same pytest invocation CI runs)
- **Bash scripts**: `make lint-bash` + `make test-bash`; every script under `invariants/` and `.github/scripts/` must handle `--help` gracefully and stay executable (CI enforces both)
- **Dashboard JS**: `make lint-js` (`node --check` per script and ES module); `node` is a dev requirement
- **Python (local iteration)**: `make test-py` (deselects `runtime`/`stress`/`chaos`/`evaluation`; `PYTEST_WORKERS`, default 2)
- **CI test invocation**: `make test-ci`, which does not deselect those markers, so a test carrying one gates itself with its own `skipif`; SOVA caps its `-n auto` per agent (`testing.check_workers`)
- **Commands**: validate markdown structure (frontmatter, sections)
- Real-money targets, CI matrix, coverage artifact: full entry: docs/testing-guidelines.md, "CI Matrix and Opt-in Targets"

### Documentation
- **Every code change must leave docs accurate**: verify before committing
- `README.md`: update when project structure, features, usage, or commands change
- `CLAUDE.md`: update when run commands, knowledge tiers, or workflow change
- `AGENTS.md`: update when conventions, testing, or tooling change
- `.claude/rules/architecture.md`: update when components, config, or design decisions change
- `docs/VISION.md`: update when roadmap phases change
- Keep docs concise: document what exists, not plans
- The `/review` command checks doc freshness automatically: stale docs score 4/10+

### Commit Messages
Conventional commits format: `type(scope): short description`.
Types: `feat`, `fix`, `refactor`, `test`, `docs`, `chore`, `ci`.
Scopes: `dashboard`, `commands`, `personas`, `invariants`, `knowledge`, `cli`, `docs`, `scheduler`, `ipc`, `adapters`, `roles`, `core`, `config`, `supervisor`, `mcp`, `monitoring`, `db`, `awareness`, `oversight`, `deps`.
The `ci` type is scopeless (repo-wide infrastructure); all other types require a scope.

Examples:
- `feat(adapters): add Linear task source adapter`
- `fix(dashboard): correct cost tracking calculation`
- `refactor(cli): simplify argument parsing`
- `ci: add invariants job to CI pipeline`
- `docs(readme): update installation instructions`

### Silent Reverts
- A rebase, squash, or conflict resolution done from a stale tree can drop code another PR already merged, with no conflict and a green CI (the reverted PR's tests disappear with its code). `invariants/silent-revert-guard.sh` runs in pre-push and CI and fails a commit that removes most of what a recent `origin/main` commit added.
- After any rebase or amend against a moved `main`, run `git diff origin/main...HEAD --stat` and confirm it lists only this PR's files. An unrelated file, or a deletion count far above the PR's scope, is a revert.
- When the guard fires, restore the code. Add `silent-revert-ok: <reason>` to the commit body only for a deliberate removal, and give the reason: a bare acknowledgment does not exempt the commit.

### Pull Requests
- Always assign PRs to the configured `github_user` (`sova config get github_user`)
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
- Use fix-on-fix commits: squash fixes into the commit they fix
- Create separate doc commits for changes in the same PR
- Use emojis in code, documentation, or commit messages
- Skip pre-commit hooks (`--no-verify`)

### Role Chaining and the Address-Review Budget

Agents chain autonomously (Developer -> Reviewer -> Developer -> Reviewer); `pipeline.max_address_review_cycles` (default 3, 0=unlimited) caps address cycles per PR, then the PR resolves to `PR_REVIEW_EXHAUSTED` with Integrate (full entry: docs/architecture-deep-dive.md, "Role Chaining and the Address-Review Budget").

## Development Workflow
- **SSH**: repo-level `core.sshCommand` is configured for the personal key
- **Before any push**: ask the user for explicit approval
- **After any merge**: run `/after-merge` (pull main, delete branches, update memory)
- **Before creating PR**: run `/review` (lint, test, review changes)
- **Commit policy**: no fix-on-fix, no separate doc commits for changes in the same PR. Use `/rearrange-commits` before pushing.

## Agentic Workflow Commands

Rendered from `sova/commands/data/commands/` into `.claude/commands/`; the harness lists them (grouped list, full entry: docs/ARCHITECTURE.md, "Agentic Workflow Commands").
