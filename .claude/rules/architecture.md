# Architecture

## Component Overview

PAK has three main components:

### 1. CLI (`pak`)
- Single bash script at repo root
- Dispatches subcommands to agent scripts
- Handles argument parsing, project directory resolution
- Should remain thin -- logic lives in the scripts it calls

### 2. Agent (`agent/`)
- `orchestrator.sh` -- the main autonomous agent (~3500 lines bash)
  - 11-step workflow: sync -> task selection -> worktree -> develop -> simplify -> review -> push -> PR -> CI -> review -> done
  - Modular: each step is a function
  - Resumable: tracks state per-issue
- `install.sh` -- per-project installer (copies scripts, personas, config template)
- `setup-wizard.sh` -- interactive CLI setup (scans project, asks preferences, writes config)
- `detect-persona.sh` -- auto-detects tech stack from project files
- `adapters/` -- pluggable task source adapters (github, jira, linear, manual)
  - `interface.sh` defines the adapter API (list_tasks, get_task, update_task, etc.)
  - Each adapter sources the interface and implements the functions

### 3. Dashboard (`dashboard/`)
- Python/FastAPI web UI
- Jinja2 templates + Tailwind CSS (via CDN)
- Reads agent state from `.claude/` directory of target project
- Tabs: overview, control, setup, settings, costs, logs, tasks, memory, queue
- Tests: `dashboard/tests/` (pytest + httpx async client), run via `make test-py`
- Config: `dashboard/pyproject.toml` (pytest + ruff settings)
- **Handoff system**: agents write `.claude/agent-control/handoff.json` to pass state between agents
  - `handoff_service.py` -- read/write/archive handoff files (mtime-cached)
  - Dashboard renders handoff action buttons on the control page
  - Enables chaining: `ship-pr` -> `agent-resume` -> `approve-merge`
- **Claude command execution**: dashboard can run Claude Code commands via `start_claude_command()`

## Config System
- Template: `agent/pak-agent.conf.default`
- Per-project: `.claude/scripts/pak-agent.conf` (shell-sourceable)
- Key fields: AGENT_MODEL, MAX_BUDGET, TASK_SOURCE, TASK_SOURCE_CONFIG, COMMIT_FORMAT, PR_TITLE_FORMAT, BRANCH_NAMING

## Naming Convention

The project's full name is **Project Automation Kit**. The abbreviation **PAK** is used internally
and as the CLI command (`pak`). This is a deliberate namespacing strategy:

- **Marketing / docs / README**: use the full name "Project Automation Kit" to avoid ambiguity
- **CLI command**: `pak` (short, ergonomic)
- **Config files / internal references**: `pak-` prefix (e.g., `pak-agent.conf`, `pak-agent.sh`)
- **Never use "PAK" as a standalone brand** -- always pair with context (e.g., "PAK CLI", "the PAK agent")

This decision was made after a naming conflict analysis (April 2026). Known conflicts:
- **Stakpak "paks"** (`paks` CLI) -- an AI agent skills package manager in the same ecosystem. Different domain (DevOps + skill packaging vs. autonomous development), but the CLI names are one character apart.
- **pak (R)** -- R package installer (`pak.r-lib.org`). Different ecosystem entirely.
- **IBM Cloud Pak** -- enterprise container tooling. Namespaced as `ibm-pak` / `cloudctl`.

The full name "Project Automation Kit" has no known conflicts and is descriptive enough to stand on its own in search results and documentation.

## Development Workflow
- `Makefile` at repo root provides all development targets
- `make serve` -- start dashboard
- `make check` -- lint + test (CI-equivalent)
- `make test` -- bash (shellcheck + invariant --help) + python (pytest)
- `make lint` -- shellcheck + ruff
- `make format` -- ruff auto-format

## Key Design Decisions
- **Bash for agent**: zero runtime deps beyond git/gh/jq/claude, runs everywhere
- **Shell-sourceable config**: simple `. "$conf"` to load, no parser needed
- **Worktree isolation**: each task gets its own git worktree (parallel-safe)
- **Adapter pattern for task sources**: swap GitHub/JIRA/Linear without touching orchestrator
- **Markdown commands**: Claude Code loads them as slash commands, agent injects them as prompts
- **Persona auto-detection**: detects project tech stack and loads relevant guidance
- **Handoff protocol**: JSON-based inter-agent state passing enables autonomous multi-agent workflows
- **Short-lived agent model**: agents run, write handoff, exit; dashboard provides the interactive bridge
