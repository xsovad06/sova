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

## Config System
- Template: `agent/gwym-agent.conf.default`
- Per-project: `.claude/scripts/gwym-agent.conf` (shell-sourceable)
- Key fields: AGENT_MODEL, MAX_BUDGET, TASK_SOURCE, TASK_SOURCE_CONFIG, COMMIT_FORMAT, PR_TITLE_FORMAT, BRANCH_NAMING

## Key Design Decisions
- **Bash for agent**: zero runtime deps beyond git/gh/jq/claude, runs everywhere
- **Shell-sourceable config**: simple `. "$conf"` to load, no parser needed
- **Worktree isolation**: each task gets its own git worktree (parallel-safe)
- **Adapter pattern for task sources**: swap GitHub/JIRA/Linear without touching orchestrator
- **Markdown commands**: Claude Code loads them as slash commands, agent injects them as prompts
- **Persona auto-detection**: detects project tech stack and loads relevant guidance
