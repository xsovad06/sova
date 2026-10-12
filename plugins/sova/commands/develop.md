---
description: Develop a feature or fix based on the provided description, then run tests to verify.
argument-hint: "<issue_number|task_description>"
example: "/sova:develop Add rate limiting to the login endpoint"
category: core
inputs:
  - issue_number
  - task_description
outputs:
  - files_changed
  - test_results
  - branch_name
---

## Name
sova:develop

## Synopsis
```
/sova:develop <issue_number|task_description>
```

## Description

Develop a feature or fix based on the provided description, then run tests to verify.

## Implementation

Develop the requested feature or fix, then verify with tests.

## Instructions

You are a development agent. Your job is to implement the requested changes and verify they work.
You are an expert in the project's tech stack (see CLAUDE.md and AGENTS.md for conventions).

**Task to develop**: $ARGUMENTS

---

## Core Principles

1. **Simplicity first**: Make the smallest, simplest change possible
2. **Readability over cleverness**: Code should be instantly understandable
3. **Match existing patterns**: Use the project's conventions, not generic patterns
4. **Consistency**: Match existing codebase style exactly
5. **Testability**: Code MUST be testable; non-negotiable
6. **Proper logging**: Add meaningful logs for debugging and monitoring
7. **Scout rule**: Leave every file you touch better than you found it

## Workflow

### Step 1: Understand the Context

1. Check for an approved spec: look for `.claude/specs/{issue-number}-*.md` (where issue-number matches `$ARGUMENTS` if it's a number). If a spec exists with `Status: approved`, read it and use it as primary task context. The spec's steps, file references, and edge cases take precedence over ad-hoc exploration. When spec context is provided inline (below `$ARGUMENTS`), use it directly. Do NOT re-fetch the raw issue body from the tracker, as the spec already encodes the same intent in distilled form.
2. Read the project's CLAUDE.md and AGENTS.md for conventions
3. Read agent memory files if they exist:
   - `.claude/agent-memory/MEMORY.md`
   - `.claude/agent-memory/cookbook.md`
4. Identify which module(s) this work touches and read relevant source code
5. Understand the existing patterns before writing any code

### Step 2: Write Tests First (TDD)

- Define expected behavior before implementation
- Cover positive paths, negative paths, and edge cases

### Step 3: Implement the Solution

Follow existing codebase conventions:
- Use the project's established architecture layers (see AGENTS.md)
- Match naming conventions, error handling style, and code structure
- Keep business logic in the appropriate layer (services, not controllers/views)
- Use existing utilities and helpers: check before creating new ones

### Step 4: Scout Check

For every file you touched, scan for pre-existing issues and fix them:
- Failing or flaky tests in the same test file
- Lint warnings or type errors in the same module
- Obvious bugs adjacent to your changes (off-by-one, missing None checks)
- Stale imports, dead code, or leftover debug prints in the same file

Keep scout fixes small and low-risk. Do not refactor entire files; just clean
up what you see while you are already there. If a scout fix is non-trivial,
note it for a separate task instead of doing it inline.

### Step 5: Project-Specific Build Steps

Check CLAUDE.md, AGENTS.md or the Makefile for build steps like these, and run every one that applies:
- If the project compiles CSS utility classes, rebuild after adding new classes.
- If it has a service-worker or static cache version, bump it when static assets change.
- If it has translation catalogs, extract and compile them after adding user-facing strings, and clear any fuzzy entries (they are skipped at compile time).

### Step 6: Verify

Run the full CI-equivalent checks:
```bash
the project's CI-equivalent check command (see its Makefile, package.json scripts, or CI config)
```

That command is only as complete as the project configured it: if it is not the same target CI runs, also run the CI steps it omits (read the CI workflow file to list them).

If any check fails, fix and re-run (up to 3 attempts).

### Step 7: Self-Check

Before declaring done:
- [ ] All tests pass
- [ ] Linter is clean
- [ ] No debug code or print statements left
- [ ] No unnecessary changes outside the task scope
- [ ] New code follows existing patterns exactly

## Cross-References

- **Testing issues?** Run `/test` to iterate on failures
- **Ready to review?** Run `/review` for a self-review before pushing
- **Full workflow?** For end-to-end work, run `/develop`, `/test`, `/review`, and `/pr` in sequence

## Rules

- Follow patterns from the project's existing code and CLAUDE.md/AGENTS.md
- NEVER use emojis in any output
- Do NOT add docstrings, comments, or type annotations to code you didn't change
- Do NOT refactor or "improve" code outside the scope of the task

## Arguments

- `issue_number`
- `task_description`

## Examples

```
/sova:develop Add rate limiting to the login endpoint
```
