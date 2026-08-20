# SOVA -- Software Orchestration Via Agents

<p align="center">
  <img src="assets/branding/sova-logo.jpg" alt="SOVA Logo" width="200">
</p>

[![CI](https://github.com/xsovad06/sova/actions/workflows/ci.yml/badge.svg)](https://github.com/xsovad06/sova/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.12+-blue.svg)](https://python.org)

## What is SOVA?

SOVA is a standalone application that turns GitHub Issues into merged pull requests -- autonomously. Install it into any software project and it will triage issues, research the codebase, develop solutions using TDD, self-review, create PRs, monitor CI, and address review feedback. You stay in control through a web dashboard and a human-in-the-loop handoff system, while SOVA handles the repetitive engineering work.

## Why SOVA?

- **End-to-end automation** -- from issue triage to merged PR, SOVA handles the full development cycle. You approve, it executes.
- **Quality by design** -- gate checks between every pipeline step catch problems early. Agents refuse to work on issues that haven't been properly triaged and researched.
- **Learns from mistakes** -- a 4-tier knowledge system captures review feedback, common patterns, and gotchas. Each run is better than the last.
- **You stay in control** -- agents are ephemeral (spawn, work, handoff, exit). The dashboard shows what they did, what they need, and lets you decide when to merge. Nothing ships without your approval.
- **Works on any project** -- install once, configure with a TOML file, and SOVA adapts to your stack via persona auto-detection (Django, FastAPI, Odoo, and more).

## Key Features

- **Role-Based Agents** -- specialized triage, researcher, developer, and reviewer roles with automatic dispatch and autonomous Developer-Reviewer chaining
- **Gate-Checked Pipeline** -- every step validates its output before the next begins; Developer pipeline (15 steps), Address-Review pipeline (9 steps)
- **Web Dashboard** -- 19-page UI for monitoring runs, costs, agent control, lifecycle tracking, and configuration
- **24/7 Server Mode** -- scheduler with priority-based watch loop and parallel execution
- **Handoff System** -- per-issue handoff files eliminate race conditions in parallel agent runs; dashboard renders action buttons for human decisions
- **27 Standardized Commands** -- develop, test, review, PR, ship, debug, and more -- works on any project
- **4-Tier Knowledge System** -- layered memory with cross-project learning ([details](knowledge/KNOWLEDGE.md))
- **Persona Auto-Detection** -- detects your tech stack and loads relevant guidance (see [`personas/`](personas/))
- **Pluggable Task Sources** -- GitHub Issues and Jira Cloud supported, Linear planned

## Architecture Overview

```mermaid
graph LR
    A[Issue Tracker] --> B[Triage]
    B --> C[Research]
    C --> D[Develop]
    D --> E[Self-Review]
    E --> F[Create PR]
    F --> G[Monitor CI]
    G --> H[Code Review]
    H --> I[Merge]

    style A fill:#313244,stroke:#cdd6f4,color:#cdd6f4
    style B fill:#313244,stroke:#89b4fa,color:#89b4fa
    style C fill:#313244,stroke:#89b4fa,color:#89b4fa
    style D fill:#313244,stroke:#a6e3a1,color:#a6e3a1
    style E fill:#313244,stroke:#a6e3a1,color:#a6e3a1
    style F fill:#313244,stroke:#f9e2af,color:#f9e2af
    style G fill:#313244,stroke:#f9e2af,color:#f9e2af
    style H fill:#313244,stroke:#cba6f7,color:#cba6f7
    style I fill:#313244,stroke:#a6e3a1,color:#a6e3a1
```

Each transition is enforced by gate checks. The Developer refuses issues that haven't been triaged and researched first (use `--force` to bypass for quick fixes).

## Requirements

- Python 3.12+
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) (`claude`)
- [GitHub CLI](https://cli.github.com/) (`gh`) -- authenticated
- `git` with configured identity: `git config user.name` and `git config user.email`
- `terminal-notifier` (macOS, optional) -- `brew install terminal-notifier` for rich desktop notifications with custom icon and sound

## Installation

```bash
git clone https://github.com/xsovad06/sova.git
cd sova
pip install --user -e .
```

Ensure your Python user bin directory is on `PATH`:

```bash
# macOS (replace 3.12 with your Python version)
export PATH="$HOME/Library/Python/3.12/bin:$PATH"

# Linux
export PATH="$HOME/.local/bin:$PATH"
```

For development (tests, linting):

```bash
pip install --user -e ".[dev]"
```

## Quick Start

```bash
# Install SOVA into your project (creates sova.toml, deploys commands)
sova install /path/to/project

# Optional: run the interactive setup wizard for custom configuration
sova setup /path/to/project

# Triage an issue to assess agent suitability
sova triage 42

# Work on an issue (runs the full pipeline: develop, test, review, PR)
sova run 42

# Or start the server for fully autonomous operation
sova server start
```

## Dashboard

The web dashboard is SOVA's primary interface for monitoring and controlling agents.

```bash
sova dashboard --project /path/to/project    # http://localhost:8111
```

| Page | Purpose |
|------|---------|
| **Home** | Project list (command center) for multi-project installations |
| **Dashboard** | Overview with agent status strip, pipeline progress, recent activity |
| **Agents** | Multi-agent control panel: start/stop, view handoff actions, task browser |
| **Run Detail** | Per-run step pipeline, logs, and cost breakdown |
| **Lifecycle** | Issue lifecycle rail (development through post-merge) |
| **Costs** | Per-task and per-model cost tracking and aggregation |
| **Queue** | Batch triage/harden operations with progress tracking |
| **Logs** | Real-time agent output streaming |
| **Memory** | Browse and search the knowledge base (memories, patterns, extractions) |
| **Roles** | View and edit custom workflow roles (DAG-based) |
| **Settings** | Runtime configuration management |
| **Setup** | Project onboarding wizard with directory browser |
| **Style Guide** | Design system reference with live component examples |

## Configuration

SOVA uses a `sova.toml` file in each project root. Minimal example (required fields only):

```toml
github_repo = "owner/repo"
github_user = "owner"

[task_source]
type = "github"
```

Key optional sections:

```toml
[agent]
model = "opus"              # LLM model for agent steps
max_budget = 10.0           # Max USD per run ($10 default)

[ci]
max_fix_attempts = 2        # Auto-fix CI failures (0 to disable)
max_wait = 900              # Seconds to wait for CI checks

[pipeline]
auto_handoff = true         # Developer auto-hands off to Reviewer
auto_address_review = true  # Reviewer auto-triggers address-review

[telemetry]
hub_url = "https://your-hub.example.com"  # Push run summaries to a central hub (SOVA_TELEMETRY_HUB_URL)
hub_token = ""                             # Bearer token (SOVA_TELEMETRY_HUB_TOKEN)
machine_id = ""                            # Auto-derived from hostname+username when empty (SOVA_TELEMETRY_MACHINE_ID)
```

For the full configuration reference, see [`sova/config/models.py`](sova/config/models.py). Environment variables override TOML values using the `SOVA_` prefix (e.g., `SOVA_BASE_BRANCH=develop`).

## Notifications

SOVA supports multiple notification backends for agent handoffs and system events:

```toml
[notification]
# Desktop notifications (macOS via terminal-notifier or JXA, Linux via notify-send)
desktop = false

# Slack webhook
slack_webhook_url = ""

# Email via SMTP
email_enabled = true
email_to = "dev@example.com"
email_from = "sova@example.com"
email_smtp_host = "smtp.example.com"
email_smtp_port = 587
email_smtp_starttls = true
# Credentials via env: SOVA_NOTIFICATION_EMAIL_SMTP_USER, SOVA_NOTIFICATION_EMAIL_SMTP_PASSWORD

# Generic webhook (HTTP POST)
webhook_url = "https://hooks.example.com/sova"
webhook_headers = '{"Authorization": "Bearer token123"}'  # JSON object with custom headers
```

All backends are fire-and-forget. Errors are logged but never block agent execution.

## Server Management

SOVA can run as an always-on service (dashboard + scheduler daemon):

```bash
# Start the server
sova server start --host 127.0.0.1 --port 8111

# Restart the server
sova server restart

# Stop the server
sova server stop

# Check server status
sova server status

# View activity summary
sova server digest --hours 24

# Install as system service (systemd on Linux, launchd on macOS)
sova server install-service --type systemd --project /path/to/project
```

After installing as a service:

```bash
# systemd
systemctl --user enable sova-server
systemctl --user start sova-server
systemctl --user status sova-server

# launchd
launchctl load ~/Library/LaunchAgents/com.sova.server.plist
launchctl start com.sova.server
launchctl list | grep sova
```

**Health monitoring**: The server exposes health and digest endpoints for external monitoring:
- `GET /api/scheduler/health` -- uptime, scan count, error count, agent slots, DB status
- `GET /api/scheduler/digest?hours=24` -- tasks completed/failed, cost summary

**Log rotation**: When `log_file` is configured, logs rotate automatically (10 MB max, 5 backups by default). Configure via:

```toml
[server]
log_max_bytes = 10485760    # 10 MB
log_backup_count = 5
```

## CLI Reference

| Category | Commands |
|----------|----------|
| **Core** | `sova run <issue>`, `sova triage <issue>`, `sova harden <issue>`, `sova watch`, `sova parallel` |
| **Server** | `sova server start\|stop\|restart\|status\|digest\|install-service` |
| **Setup** | `sova install <path>`, `sova setup <path>`, `sova uninstall <path>`, `sova init-db`, `sova doctor` |
| **PR Ops** | `sova address-pr <pr>`, `sova maintain-pr <pr>`, `sova review-pr <pr>`, `sova learn-from-pr <pr>` |
| **Monitor** | `sova status`, `sova costs`, `sova config`, `sova dashboard` |
| **Knowledge** | `sova memory search\|prune\|dump\|export\|import` |
| **Commands** | `sova commands list\|diff\|update\|sync` |
| **MCP** | `sova mcp serve` |
| **Maintenance** | `sova cleanup` |

Run `sova --help` for the full list.

## How It Works

SOVA uses a **role-based architecture** with four specialized agent types:

1. **Triage** -- assesses issues for agent suitability, applies labels (`agent:ready`, `agent:needs-spec`, `agent:human-only`)
2. **Researcher** -- investigates the codebase and writes an implementation spec
3. **Developer** -- implements the solution using TDD, creates a PR, monitors CI
4. **Reviewer** -- reviews the PR, posts findings, triggers a fix cycle if needed

Agents are **ephemeral**: each one spawns, does its job, writes a handoff file, and exits. The orchestrator (scheduler or dashboard) reads the handoff and spawns the next agent in the chain. Developer and Reviewer chain autonomously -- when the Developer finishes, it hands off to the Reviewer, which hands back to the Developer if findings exist. This loop continues until the code is clean, then SOVA notifies you and waits for your merge decision.

## Task Sources

| Source | Status |
|--------|--------|
| GitHub Issues | Supported |
| Jira Cloud | Supported ([configuration guide](docs/jira-configuration-guide.md)) |
| Linear | Planned |

## Contributing

Contributions are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for setup, code style, and PR guidelines.

```bash
make check    # Lint + test (CI-equivalent)
make lint     # ShellCheck + Ruff
make test     # All tests (bash + python)
make format   # Auto-format Python
```

## Getting Help

- Run `sova doctor` to check your environment setup
- Run `sova --help` or `sova <command> --help` for CLI usage
- File an [issue](https://github.com/xsovad06/sova/issues) for bugs or questions
- See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup

## License

Licensed under the [Apache License, Version 2.0](LICENSE).
