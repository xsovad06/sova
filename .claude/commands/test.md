---
name: test
description: Run tests and linter for the current module until all issues are fixed.
user-invocable: true
---

# Test Runner

Run tests and linter, fixing issues iteratively until everything passes.

## Instructions

### Step 1: Identify the Module

Determine which module to test from `$ARGUMENTS` or the current directory.
If unclear, check `git diff --name-only` to see which modules have changes.

### Step 2: Run Linter

Run the project's lint command (see CLAUDE.md for the exact command).

For bash scripts:
```bash
shellcheck pak agent/*.sh agent/adapters/*.sh invariants/*.sh
```

If linter fails:
- Analyze the errors
- Fix the code issues
- Re-run until it passes

### Step 3: Run Tests

Run the project's test command (see CLAUDE.md for the exact command).

For dashboard:
```bash
cd dashboard && .venv/bin/python -m pytest 2>/dev/null || echo "No test suite yet"
```

For invariants:
```bash
for f in invariants/*.sh; do bash -n "$f"; done
```

If tests fail:
- Analyze the test failures
- Fix the code or tests as needed
- Re-run until they pass

### Step 4: Scout Check

While fixing test or lint failures, scan each touched file for pre-existing
issues: flaky test patterns (reading real filesystem instead of tmp_path, missing
monkeypatch isolation), stale imports, dead code. Fix them alongside the
failures. Keep scout fixes small and low-risk.

### Step 5: Iterate

Repeat Steps 2-4 until both linter and tests pass completely.

## Rules

- Always fix linter issues before running tests
- After fixing issues, re-run to verify the fix
- Continue iterating until both pass
- Report final status when everything is green
- NEVER use emojis in any output
