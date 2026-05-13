---
name: develop
description: Develop a feature or fix based on the provided description, then run tests to verify.
user-invocable: true
category: core
---

Develop the requested feature or fix, then verify with tests.

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
5. **Testability** -- Code MUST be testable; non-negotiable
6. **Proper logging** -- Add meaningful logs for debugging and monitoring
7. **Scout rule** -- Leave every file you touch better than you found it

## Workflow

### Step 1: Understand the Context

1. Read the project's CLAUDE.md and AGENTS.md for conventions
2. Read agent memory files if they exist:
   - `.claude/agent-memory/MEMORY.md`
   - `.claude/agent-memory/cookbook.md`
3. Identify which module(s) this work touches and read relevant source code
4. Understand the existing patterns before writing any code

### Step 2: Write Tests First (TDD)

- Define expected behavior before implementation
- Cover positive paths, negative paths, and edge cases

### Step 3: Implement the Solution

Follow existing codebase conventions:
- Use the project's established architecture layers (see AGENTS.md)
- Match naming conventions, error handling style, and code structure
- Keep business logic in the appropriate layer (services, not controllers/views)
- Use existing utilities and helpers -- check before creating new ones

### Step 4: Scout Check

For every file you touched, scan for pre-existing issues and fix them:
- Failing or flaky tests in the same test file
- Lint warnings or type errors in the same module
- Obvious bugs adjacent to your changes (off-by-one, missing None checks)
- Stale imports, dead code, or leftover debug prints in the same file

Keep scout fixes small and low-risk. Do not refactor entire files -- just clean
up what you see while you are already there. If a scout fix is non-trivial,
note it for a separate task instead of doing it inline.

### Step 5: Verify

Run the project's linter and test suite (see CLAUDE.md for commands):
```bash
# Run linter
<project lint command from CLAUDE.md>

# Run tests
<project test command from CLAUDE.md>
```

If tests fail, fix and re-run (up to 3 attempts).

### Step 5: Self-Check

Before declaring done:
- [ ] All tests pass
- [ ] Linter is clean
- [ ] No debug code or print statements left
- [ ] No unnecessary changes outside the task scope
- [ ] New code follows existing patterns exactly

## Cross-References

- **Testing issues?** Run `/test` to iterate on failures
- **Ready to review?** Run `/review` for a self-review before pushing
- **Full workflow?** Use `/develop-full` instead for end-to-end (develop + test + review + PR)

## Rules

- Follow patterns from the project's existing code and CLAUDE.md/AGENTS.md
- NEVER use emojis in any output
- Do NOT add docstrings, comments, or type annotations to code you didn't change
- Do NOT refactor or "improve" code outside the scope of the task
