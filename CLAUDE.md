@AGENTS.md

# CLAUDE.md

This file provides Claude Code-specific commands and behaviors. Architecture, conventions, and cross-cutting rules are in AGENTS.md and `.claude/rules/*.md` -- this file contains only run commands, knowledge system config, and Claude-specific preferences.

## Running the Project

```bash
# SOVA CLI (globally installed via: pip install --user --break-system-packages -e .)
sova --help                         # Show all commands
sova install /path/to/project       # Install SOVA into a project
sova setup /path/to/project         # Run setup wizard
sova uninstall /path/to/project     # Remove SOVA from a project
sova run 42                         # Work on issue #42
sova triage 42                      # Triage a single issue
sova briefing                       # Show awareness briefing
sova dashboard --project /path      # Start web UI at http://localhost:8111

# Server management (dashboard + scheduler daemon)
sova server start                   # Start the server
sova server restart                 # Restart the server
sova server stop                    # Stop the server
sova server status                  # Check if server is running
sova server digest --hours 24       # Show activity summary
sova server install-service --type systemd  # Install as system service

# Dashboard (shortcut)
make serve                          # Start web UI at http://localhost:8111

# Development
make check                          # Run all linters + tests (CI-equivalent)
make test                           # Run bash + python tests
make lint                           # ShellCheck + Ruff
make format                         # Auto-format Python code

# Git hooks (after fresh clone)
git config core.hooksPath .githooks
```

## Starting a Session

```bash
# Verify you are authenticated as the correct GitHub user (must match github_user in sova.toml)
gh auth status
```

Ticket workflow (branch naming, PR linking, etc.) is in AGENTS.md under "Development Workflow".

## Knowledge System (4-Tier)

### Tier 1: Project Rules (always loaded, no truncation)
- `CLAUDE.md` -- This file (Claude Code-specific)
- `AGENTS.md` -- Cross-cutting conventions for all AI tools
- `.claude/rules/*.md` -- Modular knowledge files:
  - `architecture.md` -- Project structure, key paths, design decisions
  - `bash-patterns.md` -- Shell scripting conventions and gotchas
- `.claude/commands/*.md` -- Workflow commands (on-demand, loaded via `/command`)

### Tier 2: Agent Memory (persists across tasks, gitignored)
- `.claude/agent-memory/MEMORY.md` -- Index file
- `.claude/agent-memory/cookbook.md` -- Topical knowledge by domain, common mistakes with occurrence counts

### Tier 3: Session Memory (auto-managed by Claude Code)
- `~/.claude/projects/.../memory/MEMORY.md` -- User preferences, project state (200-line limit)

### Adding new knowledge
- **Confirmed patterns**: Add to `.claude/rules/*.md` (Tier 1)
- **New findings (not yet confirmed)**: Add to `.claude/agent-memory/` (Tier 2)
- **Session state / preferences**: Auto-memory (Tier 3)
- **Promote**: Tier 2 → Tier 1 when confirmed in 2+ tasks

## Claude Code Behavioral Preferences

- Do NOT include "Generated with Claude Code" in PR descriptions
- Always run `shellcheck` on changed bash scripts before creating commits
- NEVER use emojis in any output
