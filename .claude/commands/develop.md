---
name: develop
description: Develop a feature or fix based on the provided description, then verify.
user-invocable: true
---

Develop the requested feature or fix, then verify it works.

## Instructions

You are a development agent. Your job is to implement the requested changes and verify they work.
You are an expert in the project's tech stack (see CLAUDE.md and AGENTS.md for conventions).

**Task to develop**: $ARGUMENTS

---

## Core Principles

1. **Simplicity first** -- Make the smallest, simplest change possible
2. **Readability over cleverness** -- Code should be instantly understandable
3. **Match existing patterns** -- Use the project's conventions, not generic patterns
4. **Consistency** -- Match existing codebase style exactly
5. **Testability** -- Code should be verifiable

## Workflow

### Step 1: Understand the Context

1. Read the project's CLAUDE.md and AGENTS.md for conventions
2. Identify which module(s) this work touches and read relevant source code
3. Understand the existing patterns before writing any code

### Step 2: Plan the Implementation

- Identify which files need changes
- Consider impact on other components (does changing orchestrator.sh affect install.sh?)
- For bash: will ShellCheck pass? For Python: will Ruff pass?

### Step 3: Implement the Solution

Follow existing codebase conventions:
- **Bash**: `set -euo pipefail`, quote variables, `local` in functions, logging helpers
- **Python**: type hints, f-strings, match existing patterns in dashboard/
- **Markdown**: YAML frontmatter for commands, ATX headings, no emojis
- Use existing utilities and helpers -- check before creating new ones

### Step 4: Verify

```bash
# Lint bash scripts
shellcheck pak agent/*.sh agent/adapters/*.sh invariants/*.sh

# Test invariants (if changed)
for f in invariants/*.sh; do bash "$f" --help 2>/dev/null; done

# Dashboard (if changed)
cd dashboard && .venv/bin/python -m pytest 2>/dev/null || echo "No tests yet"
```

If checks fail, fix and re-run (up to 3 attempts).

### Step 5: Self-Check

Before declaring done:
- [ ] ShellCheck passes on changed bash scripts
- [ ] No debug code or print statements left
- [ ] No unnecessary changes outside the task scope
- [ ] New code follows existing patterns exactly
- [ ] Changed scripts are still executable (`chmod +x`)

## Cross-References

- **Ready to review?** Run `/review` for a self-review before pushing
- **Need to debug?** Run `/debug` for systematic debugging

## Rules

- Follow patterns from the project's existing code and CLAUDE.md/AGENTS.md
- NEVER use emojis in any output
- Do NOT add docstrings, comments, or type annotations to code you didn't change
- Do NOT refactor or "improve" code outside the scope of the task
