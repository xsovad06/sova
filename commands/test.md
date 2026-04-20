---
name: test
description: Run tests and linter for the current project until all issues are fixed.
user-invocable: true
category: core
---

# Test Runner

Run tests and linter, fixing issues iteratively until everything passes.

## Instructions

### Step 1: Identify the Scope

Determine what to test from `$ARGUMENTS` or the current directory.
If unclear, check `git diff --name-only` to see which modules have changes.

### Step 2: Run Linter

Run the project's lint command (see CLAUDE.md for the exact command).

If linter fails:
- Analyze the errors
- Fix the code issues
- Re-run until it passes

### Step 3: Run Tests

Run the project's test command (see CLAUDE.md for the exact command).

If tests fail:
- Analyze the test failures
- Fix the code or tests as needed
- Re-run until they pass

### Step 4: Iterate

Repeat Steps 2-3 until both linter and tests pass completely.

## Cross-References

- **After tests pass**: Run `/review` to self-review before pushing
- **Full workflow**: Use `/develop-full` for the complete develop-test-review-pr cycle

## Rules

- Always fix linter issues before running tests
- After fixing issues, re-run to verify the fix
- Continue iterating until both pass
- Report final status when everything is green
- NEVER use emojis in any output
