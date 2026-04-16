---
name: ingest-review
description: Extract and save learnings from a PR review into agent memory.
user-invocable: false
---

# Ingest PR Review Feedback

Extract actionable lessons from a completed PR review and save them to agent memory.

## Process

### 1. Analyze the Review

Read all review comments, conversations, and the final resolution for each.

### 2. Extract Patterns

For each comment that led to a code change, identify:
- **What was wrong**: The specific mistake or gap
- **Why it matters**: Impact on the codebase
- **What to do instead**: The correct pattern going forward
- **Category**: error_handling, testing, style, security, performance, naming, documentation, other

### 3. Update Memory Files

Append to `.claude/agent-memory/review-feedback.md`:
```
### PR #N — YYYY-MM-DD
- [category] Pattern description (from reviewer)
```

If the same pattern appears in `.claude/agent-memory/common-mistakes.md`, increment its count.
If a pattern appears for the 2nd time across any memory file, add it to `common-mistakes.md`.

### 4. Output Structured Entries

At the end, output entries for SQLite storage:
```
MEMORY_ENTRY|category|title|tags
```

## Rules

- Only record actionable, specific lessons — not generic advice
- Skip cosmetic or subjective feedback
- Keep entries concise — one line per finding
- Do not duplicate existing memory entries
