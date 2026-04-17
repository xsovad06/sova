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
2. Read agent memory files if they exist:
   - `.claude/agent-memory/MEMORY.md`
   - `.claude/agent-memory/learnings.md`
   - `.claude/agent-memory/review-feedback.md`
   - `.claude/agent-memory/common-mistakes.md`
3. Identify which module(s) this work touches and read relevant source code
4. Understand the existing patterns before writing any code

### Step 2: Plan the Implementation

- Identify which files need changes
- Consider impact on other components (does changing orchestrator.sh affect install.sh?)
- For bash: will ShellCheck pass? For Python: will Ruff pass?

### Step 3: Implement the Solution (TDD)

1. **Write tests first** -- define expected behavior before implementation
2. **Implement the solution** -- follow existing codebase conventions:
   - **Bash**: `set -euo pipefail`, quote variables, `local` in functions, logging helpers
   - **Python**: type hints, f-strings, match existing patterns in dashboard/
   - **Markdown**: YAML frontmatter for commands, ATX headings, no emojis
   - Use existing utilities and helpers -- check before creating new ones
3. **Run the project's linter** (see CLAUDE.md for commands)
4. **Run the project's tests** (see CLAUDE.md for commands)
5. If tests fail, fix and re-run (up to 3 attempts)

### Step 4: Self-Review

1. Review your own diff: `git diff`
2. Check for:
   - Bugs, edge cases, off-by-one errors
   - Missing test coverage
   - Security concerns
   - Code style consistency with the rest of the module
   - Unnecessary changes or leftover debug code
3. Auto-fix any findings scored >= 3/10.
4. Re-run tests after fixes.

### Step 5: Self-Check

Before declaring done:
- [ ] ShellCheck passes on changed bash scripts
- [ ] No debug code or print statements left
- [ ] No unnecessary changes outside the task scope
- [ ] New code follows existing patterns exactly
- [ ] Changed scripts are still executable (`chmod +x`)

## Rules

- Follow patterns from the project's existing code and CLAUDE.md/AGENTS.md
- NEVER use emojis in any output
- Do NOT add docstrings, comments, or type annotations to code you didn't change
- Do NOT refactor or "improve" code outside the scope of the task
