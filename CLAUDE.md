@AGENTS.md

# CLAUDE.md

This file provides Claude Code-specific commands and behaviors. Architecture, conventions, and cross-cutting rules are in AGENTS.md and `.claude/rules/*.md` -- this file contains only run commands, knowledge system config, and Claude-specific preferences.

## Running the Project

```bash
# CLI entry point (bash)
./pak help                          # Show all commands
./pak install /path/to/project      # Install PAK into a project
./pak setup /path/to/project        # Run setup wizard

# Dashboard (Python/FastAPI)
cd dashboard && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./pak dashboard                     # Start web UI at http://localhost:8111

# Linting (bash scripts)
shellcheck pak agent/*.sh agent/adapters/*.sh invariants/*.sh

# Testing invariant scripts
for f in invariants/*.sh; do bash "$f" --help 2>/dev/null; done
```

## Starting a Session

```bash
# At session start, always switch to correct GitHub account
gh auth switch --user xsovad06
```

Ticket workflow (branch naming, PR linking, etc.) is in AGENTS.md under "Development Workflow".

## Knowledge System

### Tier 1: Project Rules (always loaded, no truncation)
- `CLAUDE.md` -- This file (Claude Code-specific)
- `AGENTS.md` -- Cross-cutting conventions for all AI tools
- `.claude/rules/*.md` -- Modular knowledge files:
  - `architecture.md` -- Project structure, key paths, design decisions
  - `bash-patterns.md` -- Shell scripting conventions and gotchas

### Tier 2: Session Memory (auto-managed by Claude Code)
- `~/.claude/projects/.../memory/MEMORY.md` -- User preferences, project state (200-line limit)

### Adding new knowledge
- **Confirmed patterns**: Add to `.claude/rules/*.md` (Tier 1)
- **Session findings**: Update auto-memory (Tier 2)

## Claude Code Behavioral Preferences

- Do NOT include "Generated with Claude Code" in PR descriptions
- Always run `shellcheck` on changed bash scripts before creating commits
- NEVER use emojis in any output
