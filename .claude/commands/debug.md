---
name: debug
description: Systematic debugging workflow -- reproduce, locate, diagnose, fix, verify, prevent.
user-invocable: true
---

# Systematic Debugging

Debug an issue in the project.

**Problem description**: $ARGUMENTS

## Workflow

### 1. Reproduce
- Understand the problem from the description
- Try to reproduce the issue (run the script, check output)
- If the issue is unclear, ask the user for more details

### 2. Locate
- Identify the relevant code path
- Read the relevant source files to understand the flow
- Check recent changes with `git log --oneline -10` and `git diff` -- was something recently changed?
- Search for related error messages in the codebase

### 3. Diagnose
- Trace the execution flow from entry point to the error
- Identify the root cause -- not just the symptom
- Check common issues:
  - Unquoted variables or word splitting
  - Missing file/directory checks
  - Wrong exit codes propagating through `set -e`
  - Config file not found or malformed
  - Missing dependencies (git, gh, jq, claude)
  - macOS vs Linux compatibility (GNU vs BSD flags)

### 4. Fix
- Apply the minimal fix that addresses the root cause
- Do not introduce workarounds -- fix the actual problem
- If the fix touches multiple files, explain the connection
- **Scout rule**: while in each file, fix any pre-existing issues you notice (lint warnings, dead imports, obvious bugs adjacent to your fix). Keep scout fixes small and low-risk.

### 5. Verify
- Run ShellCheck on changed bash scripts
- Test the fixed script manually
- Check that no regressions were introduced

### 6. Prevent
- If this bug class could recur, suggest a convention update
- If it was caused by a missing pattern, suggest updating `.claude/rules/bash-patterns.md`

## Cross-References

- **Need to run tests?** Run `/test` to iterate on failures
- **Want a review of the fix?** Run `/review` before pushing

## Rules

- NEVER use emojis in any output
- Focus on root cause, not symptoms
