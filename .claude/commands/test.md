---
name: test
description: Run linter and tests for the current project until all issues are fixed.
user-invocable: true
---

# Test Runner

Run linter and tests, fixing issues iteratively until everything passes.

## Instructions

### Step 1: Identify the Scope

Determine what to test from `$ARGUMENTS` or the current directory.
If unclear, check `git diff --name-only` to see which files have changes.

### Step 2: Run ShellCheck (bash scripts)

```bash
shellcheck pak agent/*.sh agent/adapters/*.sh invariants/*.sh
```

If ShellCheck fails:
- Analyze the warnings/errors
- Fix the code issues
- Re-run until it passes

### Step 3: Run Dashboard Tests (if applicable)

```bash
cd dashboard && .venv/bin/python -m pytest 2>/dev/null || echo "No test suite yet"
```

If tests fail:
- Analyze the test failures
- Fix the code or tests as needed
- Re-run until they pass

### Step 4: Validate Invariant Scripts

```bash
for f in invariants/*.sh; do bash -n "$f"; done
```

### Step 5: Iterate

Repeat Steps 2-4 until everything passes.

## Cross-References

- **After tests pass**: Run `/review` to self-review before pushing
- **Need to debug?**: Run `/debug` for systematic debugging

## Rules

- Always fix linter issues before running tests
- After fixing issues, re-run to verify the fix
- Continue iterating until everything passes
- Report final status when everything is green
- NEVER use emojis in any output
