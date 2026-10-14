---
name: sova-extract-knowledge
description: "Extract reusable knowledge from this session into the project's knowledge system."
---

# Extract Knowledge

Review this session's work and extract reusable knowledge into the project's knowledge system.

## Knowledge Architecture

This project uses a four-tier knowledge system:

### Tier 0: `~/.claude/shared-knowledge/` (cross-project patterns)
Reusable patterns that apply across multiple repositories. Loaded by agent startup scripts.

### Tier 1: `.claude/rules/*.md` (project rules, always loaded)
Stable patterns loaded into every session via CLAUDE.md. Primary destination for confirmed conventions that apply to all contributors (human and agent).

### Tier 2: `.claude/agent-memory/` (agent learnings)
Lessons learned from development and review cycles. Destination for agent-specific patterns not yet promoted to Tier 1.

- **`MEMORY.md`**: index file (if the project has one)
- **`cookbook/<domain>.md`** (index in `cookbook/README.md`), or a single **`cookbook.md`** in smaller projects: topical knowledge by domain, one bullet per lesson with `(PR #N; confirmed: N)`
- **`common-mistakes.md`**: recurring mistakes with `(occurrences: N)`, checked against every diff by the `sova-review` skill

Always write Tier 2 files in the PRIMARY checkout (`git rev-parse --git-common-dir` resolves it from a worktree): `.claude/agent-memory/` may be gitignored, and a worktree's copy is deleted with the worktree.

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
| ORM/framework gotchas, review lessons | the matching `.claude/agent-memory/cookbook/<domain>.md` (or the section of `cookbook.md`) |
| Recurring mistakes | `.claude/agent-memory/common-mistakes.md` |
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
- Follow the existing structure and formatting of the target file
- Write concise entries with bold labels, then the lesson
- These are always loaded, so keep entries actionable and specific to this project

**For `.claude/agent-memory/` (Tier 2):**
- One line per pattern: bold label, then the lesson
- Include the "why": not just what to do, but why it matters
- Include file paths when relevant

**For user auto-memory (Tier 3):**
- Follow the two-step process: write topic file, then update `MEMORY.md` index
- Only for genuinely new user preferences or workflow changes

### 5. Verify Sizes

Agent memory files should stay concise:
- `MEMORY.md`: under 20 lines (index only)
- Each cookbook file (or a single `cookbook.md`): under 200 lines and about 40 KB, since agents read whole files. Entries stay one bullet of at most ~500 characters; the incident narrative belongs in the PR, not the cookbook. When a domain file outgrows that, prune stale entries (verify against the code first), drop entries that are now enforced by a test or invariant, and promote confirmed ones; split a single `cookbook.md` into `cookbook/<domain>.md` files once it outgrows the limit.

If a pattern has matured from agent-memory into a stable convention, promote it to the appropriate `.claude/rules/*.md` file and remove the agent-memory entry.

### 6. Summary

List what was extracted and where it was saved. Flag any patterns promoted from Tier 2 to Tier 1.

## What to Look For

- **Bugs fixed**: root cause, how to avoid next time
- **Performance issues**: the pattern used to fix them
- **Permission/auth issues**: framework quirks, permission patterns
- **Migration gotchas**: schema vs data, deployment considerations
- **ORM/framework patterns**: query tricks, validation edge cases
- **Test patterns**: new fixtures, assertion techniques, edge case coverage
- **Review feedback**: what reviewers flagged and why
- **Recurring mistakes**: same issue appearing in multiple sessions

## Cross-References

- **Called by**: the `sova-review` skill (Step 9). The `sova-integrate-pr` skill then verifies, before merging, that review learnings were captured (its Documentation and Knowledge phase)
- **Broader assessment**: Run the `sova-agent-readiness` skill to evaluate the full knowledge system

## Rules

- Only save **confirmed, stable patterns**, not one-off debugging notes
- Update existing entries when the pattern evolved
- Keep entries actionable: someone reading them should know exactly what to do
- No session-specific context (task details, in-progress work)
- **Promote mature patterns** from agent-memory to `.claude/rules/*.md`
- NEVER use emojis in any output
