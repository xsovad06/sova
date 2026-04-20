---
name: debug
description: Systematic debugging workflow -- reproduce, locate, diagnose, fix, verify, prevent.
user-invocable: true
category: meta
---

# Systematic Debugging

Debug an issue in the project.

**Problem description**: $ARGUMENTS

## Workflow

### 1. Reproduce
- Understand the problem from the description
- Check application logs for errors
- Try to reproduce the issue (via test, curl, or browser)
- If the issue is unclear, ask the user for more details

### 2. Locate
- Identify the relevant code path (URL -> view/controller -> service -> model)
- Read the relevant source files to understand the flow
- Check recent changes with `git log --oneline -10` and `git diff` -- was something recently changed?
- Search for related error messages in the codebase

### 3. Diagnose
- Trace the execution flow from entry point to the error
- Identify the root cause -- not just the symptom
- Check common issues:
  - Missing migrations or schema changes
  - Import errors / circular imports
  - Configuration mismatches between environments
  - Authentication/permission issues
  - Database constraint violations
  - Race conditions or state management bugs

### 4. Fix
- Apply the minimal fix that addresses the root cause
- Do not introduce workarounds -- fix the actual problem
- If the fix touches multiple files, explain the connection

### 5. Verify
- Write a test that reproduces the original issue (should fail before fix, pass after)
- Run the project's test suite (see CLAUDE.md for commands)
- Check that no regressions were introduced
- Verify application logs are clean

### 6. Prevent
- If this bug class could recur, suggest a test that would catch it
- If it was caused by a missing convention, suggest updating AGENTS.md or project guidelines

## Cross-References

- **Need to run tests?** Run `/test` to iterate on failures
- **Want a review of the fix?** Run `/review` before pushing

## Rules

- NEVER use emojis in any output
- Focus on root cause, not symptoms
