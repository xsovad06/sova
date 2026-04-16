# Agent Dashboard

Web dashboard for monitoring the Morning Agent. Read-only observer that parses the agent's existing data files (costs, logs, task state, memory) and presents them in a browser UI.

## Installation

The dashboard is installed automatically by the morning agent's `install.sh`:

```bash
cd /path/to/your-repo
/path/to/morning-agent/install.sh              # installs agent + dashboard
/path/to/morning-agent/install.sh --no-dashboard  # agent only
```

After install, run:
```bash
.claude/scripts/agent-dashboard.sh          # http://localhost:8111
.claude/scripts/agent-dashboard.sh 9090     # custom port
```

### Development Mode

For working on the dashboard itself:

```bash
cd morning-agent/dashboard
make dev                                              # http://localhost:8111 with auto-reload
python run.py --data-dir /path/to/repo/.claude        # Point at specific repo
```

## Views

| View | Path | Data Source |
|------|------|-------------|
| Overview | `/` | Aggregated summary cards |
| Costs | `/costs` | `agent-memory/costs.jsonl` |
| Logs | `/logs` | `agent-memory/agent.log` |
| Tasks | `/tasks` | `worktrees/*/task-state.json` + `agent-memory/task-history.md` |
| Memory | `/memory` | `agent-memory/memory.db` + markdown files |
| Queue | `/queue` | Simulated priority scan from task state |

### Overview

Summary cards showing active tasks, daily/weekly costs, completion rate, and recent log entries. Auto-refreshes every 10 seconds.

### Costs

Daily cost bar chart (14 days), per-ticket and per-phase breakdowns, model split (sonnet vs opus), and full session table with token counts and duration.

### Logs

Filterable log viewer with level (INFO/WARN/ERROR) and component filters, text search, and auto-refresh every 5 seconds. Newest entries first.

### Tasks

Active task cards showing ticket ID, current step, branch, PR number, and time in current state. Task history table parsed from `task-history.md`.

### Memory

Full-text search of `memory.db` (SQLite FTS5), tag cloud filter, and tabbed viewer for markdown memory files (MEMORY.md, learnings.md, review-feedback.md, common-mistakes.md) rendered as HTML.

### Priority Queue

Simulated priority scan showing what the agent would pick next in watch mode:
- P0: interrupted or paused tasks
- P1: PRs needing attention
- P2: JIRA backlog items

## Prerequisites

- Python 3.10+
- No npm, no build step
- The morning agent must be installed in the target project (data files must exist)

## Configuration

The launcher script auto-detects the project's `.claude/` directory. For manual override:

```bash
export AGENT_DATA_DIR=/path/to/repo/.claude
```

Or pass `--data-dir` to `run.py`.

## Tech Stack

- FastAPI + Uvicorn (Python backend)
- Jinja2 templates (server-side rendering)
- Tailwind CSS via CDN (no build step)
- Vanilla JavaScript (client-side fetch + render)
- Dark theme by default (toggle available)

## Future: Agent Control (Phase B)

A planned Phase B will add the ability to start, stop, and interact with the agent from the dashboard via a file-based control protocol. See the plan in `.claude/plans/` for details.
