@AGENTS.md

# CLAUDE.md

This file provides Claude Code-specific commands and behaviors. Architecture, conventions, and cross-cutting rules are in AGENTS.md and `.claude/rules/*.md`. This file contains only run commands, knowledge system config, and Claude-specific preferences.

## Running the Project

```bash
# SOVA CLI (globally installed via: pip install --user --break-system-packages -e .)
sova --help                         # Show all commands
sova install /path/to/project       # Install SOVA into a project
sova setup /path/to/project         # Run setup wizard
sova uninstall /path/to/project     # Remove SOVA from a project
sova run 42                         # Work on issue #42
sova run 42 --override-suitability  # Work on an epic or human-only issue anyway
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
make check                          # Run all linters + tests (same pytest invocation as CI via make test-ci; see docs/testing-guidelines.md)
make test                           # Run bash + python tests
make lint                           # ShellCheck + Ruff + dashboard JS syntax check (node --check) + whole-tree invariant scans
make format                         # Auto-format Python code
make marketplace                    # Regenerate plugins/sova/ from sova/commands/data/commands/ (run after editing a published command)
make commands-render                # Regenerate .claude/commands/ from sova/commands/data/commands/ (run after editing a managed command)
make skills-render                  # Regenerate .agents/skills/ from sova/commands/data/commands/ and sova/commands/data/skills/ (run after editing a managed command or skill)

# Git hooks (after fresh clone)
git config core.hooksPath .githooks
```

## Starting a Session

```bash
# Verify you are authenticated as the correct GitHub user
gh auth status
```

Ticket workflow (branch naming, PR linking, etc.) is in AGENTS.md under "Development Workflow".

## Configuration

SOVA uses **database-backed configuration** (`.claude/sova.db`) instead of `sova.toml`. The migration is automatic:

- **First run**: `sova install /path/to/project` creates the database
- **Read**: `sova config` prints a summary table; `sova config get <key>` prints one resolved value (lists and dicts as JSON, a secret key as `(secret)`, an unknown key as exit code 1), which is how a command or script reads a setting it must not hardcode (log warnings go to stderr, so stdout carries only the value)
- **Edit**: `sova config set <key> <value>`, or the dashboard settings page. Both go through the same
  validation, so neither can persist a value that makes `load_config()` raise. List-valued keys accept
  a JSON array or comma-separated text: `sova config set awareness.providers "gmail, gcal"`
- **Last resort**: `sqlite3 .claude/sova.db` (all values stored as JSON; see troubleshooting below).
  Prefer `sova config set`, which writes valid JSON for you
- **Environment overrides**: `SOVA_GITHUB_REPO=owner/repo` (prefix with `SOVA_`, replace dots with underscores: `SOVA_AGENT_MODEL`)

### Troubleshooting Config Load Errors

For "Skipping setting 'X': invalid JSON value": every stored value must be valid JSON, and `sova config set <key> <value>` still works when the config no longer loads (full entry: docs/troubleshooting-config.md, "Config Load Errors").

When Python upgrades (e.g., 3.13 → 3.14), reinstall SOVA:
```bash
python3 -m pip install --user --break-system-packages -e .
```

For an unknown `llm.provider` value (e.g. after a typo or downgrade), see
"Setting an Unknown LLM Provider" in `docs/troubleshooting-config.md`.

## Alternative Agent Runtime (Codex, Optional)

SOVA defaults to spawning Claude Code (`agent.runtime = "claude-code"`) to execute pipeline steps. Setting `agent.runtime = "codex"` spawns OpenAI's Codex CLI instead. See `docs/codex-runtime-setup.md` for authentication (browser login with keyring storage, device-auth and stdin API-key fallbacks, trusted-automation-only `CODEX_API_KEY`), sandbox configuration, and rollback.

## Token Compression (Optional)

RTK compresses bash output and Headroom compresses broader context; both are optional. The RTK hook command must be `"rtk hook claude"`, never bare `rtk`, which blocks tool calls (full entry: docs/token-compression.md, "RTK Token Compression (Optional)").

## Knowledge System (4-Tier)

### Tier 1: Project Rules (always loaded, no truncation)
- `CLAUDE.md`: This file (Claude Code-specific)
- `AGENTS.md`: Cross-cutting conventions for all AI tools
- `.claude/rules/*.md`: Modular knowledge files:
  - `architecture.md`: Project structure, key paths, design decisions
  - `bash-patterns.md`: Shell scripting conventions and gotchas
  - `workflow.md`: Development workflow, command maintenance, issue state
- `.claude/commands/*.md`: Workflow commands (on-demand, loaded via `/command`)

### Tier 2: Agent Memory (persists across tasks, gitignored, primary checkout only)
- `.claude/agent-memory/cookbook/README.md`: domain index; `cookbook/<domain>.md`: one bullet per lesson, read only the domains a change touches
- `.claude/agent-memory/common-mistakes.md`: recurring mistakes with occurrence counts, checked by `/review` against every diff
- Always write these in the primary checkout, never a worktree's copy (see `.claude/rules/workflow.md` "Agent Memory Location")

### Tier 3: Session Memory (auto-managed by Claude Code)
- `~/.claude/projects/.../memory/MEMORY.md`: User preferences, project state (200-line limit)

### Adding new knowledge
- **Confirmed patterns**: Add to `.claude/rules/*.md` (Tier 1)
- **New findings (not yet confirmed)**: Add to `.claude/agent-memory/` (Tier 2)
- **Session state / preferences**: Auto-memory (Tier 3)
- **Promote**: Tier 2 → Tier 1 when confirmed in 2+ tasks
- **On-demand reference**: narrative and incident history go to `docs/*.md`, leaving a short rule line with a full-entry pointer in Tier 1; `tests/test_instruction_surface_budget.py` caps each Tier 1 file

## Claude Code Behavioral Preferences

- Do NOT include "Generated with Claude Code" in PR descriptions
- Always run `shellcheck` on changed bash scripts before creating commits
- NEVER use emojis in any output
