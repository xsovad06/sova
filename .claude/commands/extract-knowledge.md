---
name: extract-knowledge
description: Extract reusable knowledge from this session into the project's knowledge system.
user-invocable: true
category: learning
inputs:
  - session_context
outputs:
  - memories_extracted
---

# Extract Knowledge

Review this session's work and extract reusable knowledge into the project's knowledge system.

## Knowledge Architecture

This project uses a four-tier knowledge system:

### Tier 0: `~/.claude/shared-knowledge/` (cross-project patterns)
Reusable patterns that apply across multiple repositories. Loaded by agent startup scripts.

### Tier 1: `.claude/rules/*.md` (canonical, always loaded via CLAUDE.md)
Stable patterns loaded into every session via CLAUDE.md. Primary destination for confirmed conventions.

### Tier 2: `.claude/agent-memory/` (agent learnings, loaded by morning agent)
Lessons learned from development and review cycles. Destination for agent-specific patterns that improve autonomous development quality.

- **`MEMORY.md`** -- Index file
- **`cookbook.md`** -- Topical knowledge by domain, common mistakes with occurrence counts

### Tier 3: User auto-memory (`~/.claude/projects/.../memory/`)
User preferences, workflow conventions, and project state.

## Steps

### 1. Identify What Changed

Run `git diff` and `git diff --cached` to see current changes. Also review the conversation for patterns, gotchas, decisions, and fixes that came up during the session.

### 2. Categorize Findings

For each finding, determine the right destination:

| Finding type | Destination |
|---|---|
| Domain-specific patterns (security, performance, etc.) | `.claude/rules/<domain>.md` |
| ORM/framework gotchas, review lessons, recurring mistakes | `.claude/agent-memory/cookbook.md` (under matching domain section) |
| Agent workflow or project pattern changes | `.claude/agent-memory/MEMORY.md` |
| User preferences, workflow, collaboration style | User auto-memory |

### 3. Check for Duplicates

Before writing anything:
1. Read the target file
2. Check for existing entries about the same topic
3. Update existing entries rather than adding duplicates
4. Remove entries that are now outdated or wrong

### 4. Write Knowledge

**For `.claude/rules/*.md` (Tier 1):**
- Follow the existing structure and formatting of the target file. Write concise entries with bold labels, then the lesson. These are always loaded -- keep entries actionable and specific to this project.

**For `.claude/agent-memory/` (Tier 2):**
- One line per pattern -- bold label, then the lesson
- Include the "why" -- not just what to do, but why it matters
- Include file paths when relevant

**For user auto-memory (Tier 3):**
- Follow the two-step process: write topic file, then update `MEMORY.md` index
- Only for genuinely new user preferences or workflow changes

### 5. Verify Sizes

Agent memory files should stay concise:
- `MEMORY.md` -- under 20 lines (index only)
- `cookbook.md` -- under 200 lines (prune oldest `[confirmed: 0]` entries if needed)

If a pattern has matured from agent-memory into a stable convention, promote it to the appropriate `.claude/rules/*.md` file and remove the agent-memory entry.

### 6. Summary

List what was extracted and where it was saved. Flag any patterns promoted from Tier 2 to Tier 1.

## What to Look For

- **Bugs fixed** -- root cause, how to avoid next time
- **Performance issues** -- the pattern used to fix them
- **Permission/auth issues** -- framework quirks, permission patterns
- **Migration gotchas** -- schema vs data, deployment considerations
- **ORM/framework patterns** -- query tricks, validation edge cases
- **Test patterns** -- new fixtures, assertion techniques, edge case coverage
- **Review feedback** -- what reviewers flagged and why
- **Recurring mistakes** -- same issue appearing in multiple sessions

## Cross-References

- **Called by**: `/review` (Step 9) and `/after-merge` (post-merge learning)
- **Broader assessment**: Run `/agent-readiness` to evaluate the full knowledge system

## Rules

- Only save **confirmed, stable patterns** -- not one-off debugging notes
- Update existing entries when the pattern evolved
- Keep entries actionable -- someone reading them should know exactly what to do
- No session-specific context (task details, in-progress work)
- **Promote mature patterns** from agent-memory to `.claude/rules/*.md`
- NEVER use emojis in any output
