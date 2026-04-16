# Knowledge Management System

> Standardized 4-tier knowledge architecture for AI-assisted development.
> Used by both interactive Claude Code sessions and the autonomous agent.

## Overview

Knowledge is organized into four tiers, from most shared to most ephemeral:

```
Tier 0: Shared (cross-project)     ~/.claude/shared-knowledge/
Tier 1: Project (tracked in git)   AGENTS.md, CLAUDE.md, .claude/rules/, .claude/commands/
Tier 2: Agent Memory (gitignored)  .claude/agent-memory/
Tier 3: Session (auto-managed)     ~/.claude/projects/.../memory/
```

## Tier 0 -- Shared Knowledge (Cross-Project)

**Location**: `~/.claude/shared-knowledge/*.md`

Universal conventions and patterns that apply across all projects. Team-level knowledge that has been validated in multiple projects.

**Examples**:
- Git workflow conventions
- Code review standards
- Testing philosophy
- Language-agnostic patterns

**Management**:
- Promote from Tier 1 after validation in 2+ projects: `pak knowledge promote <file>`
- Demote if no longer applicable: `pak knowledge demote <file>`

## Tier 1 -- Project Knowledge (Tracked in Git)

Always loaded, shared between interactive and agent sessions.

### AGENTS.md (Root)
Cross-cutting conventions for all AI tools:
- Project overview and architecture
- Task tracker configuration
- Code patterns and conventions
- Commit and PR rules
- Testing requirements

### CLAUDE.md (Root)
Claude Code-specific configuration:
- Imports `@AGENTS.md` for shared conventions
- Run commands (test, lint, format)
- Knowledge system configuration
- Agent-specific settings

### .claude/rules/*.md
Modular knowledge files loaded every session (no truncation):
- `architecture.md` -- app structure, key paths, architectural decisions
- `patterns.md` -- framework/language gotchas and lessons learned
- `testing.md` -- fixtures, factories, testing conventions
- `ui-patterns.md` -- frontend patterns (if applicable)
- `models.md` -- domain model patterns
- Additional files as the project grows

### .claude/commands/*.md
Workflow commands loaded on demand:
- 26 standardized commands from the Project Automation Kit
- Project-specific commands (architecture-overview, domain deep-dives)

## Tier 2 -- Agent Memory (Gitignored)

Agent-specific learnings, loaded at task start. Not shared with interactive sessions.

**Location**: `.claude/agent-memory/`

### Core Files
- `MEMORY.md` -- quick reference, project patterns (< 80 lines, loaded every session)
- `learnings.md` -- self-review findings (< 150 lines)
- `review-feedback.md` -- PR review lessons (< 150 lines)
- `common-mistakes.md` -- recurring errors (< 100 lines)
- `task-history.md` -- completed tasks log

### Per-Task Context
`.claude/agent-memory/sessions/<issue>/` -- context and decisions for a specific task:
- `task_state.json` -- workflow step tracking (for resume)
- `context.md` -- task-specific notes and decisions

### Structured Storage
- `memory.db` -- SQLite FTS5 for full-text search across learnings
- `review-patterns.db` -- reviewer preference tracking
- `costs.jsonl` -- cost tracking per session

## Tier 3 -- Session Memory (Auto-Managed)

Managed automatically by Claude Code. User preferences and project state.

**Location**: `~/.claude/projects/.../memory/MEMORY.md`

- 200-line limit (auto-truncated)
- Persists across conversations within the same project
- Not shared across projects

## Promotion Flow

Knowledge flows upward as it proves valuable:

```
Tier 2 (agent learns)
  → confirmed in 2+ tasks →
Tier 1 (project knowledge, tracked in git)
  → confirmed in 2+ projects →
Tier 0 (shared knowledge)
```

### When to Promote

- **Tier 2 → Tier 1**: Pattern has been validated in at least 2 tasks and is broadly applicable to the project
- **Tier 1 → Tier 0**: Convention has been validated in at least 2 projects and is language/framework-agnostic

### How to Promote

```bash
# Promote agent memory to project rules
pak knowledge promote learnings.md    # copies to .claude/rules/

# Promote project knowledge to shared
pak knowledge promote-shared patterns.md  # copies to ~/.claude/shared-knowledge/

# Demote (remove from shared)
pak knowledge demote patterns.md
```

## Size Guidelines

Keep files focused and concise to avoid context pollution:

| File | Max Lines | Purpose |
|------|-----------|---------|
| AGENTS.md | 200 | Project-wide conventions |
| CLAUDE.md | 100 | Tool-specific config |
| .claude/rules/*.md | 150 each | Domain knowledge |
| MEMORY.md (Tier 2) | 80 | Quick agent reference |
| learnings.md | 150 | Self-review findings |
| review-feedback.md | 150 | PR review lessons |
| common-mistakes.md | 100 | Recurring errors |

## Setup

The setup wizard (`pak setup`) initializes the knowledge system:
1. Creates `.claude/rules/` with seed files based on detected tech stack
2. Creates `.claude/agent-memory/` with template files
3. Configures `.gitignore` (agent-memory ignored, rules tracked)
4. Optionally enables shared knowledge directory
