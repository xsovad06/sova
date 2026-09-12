---
description: Run the project's linter and test suite iteratively until both pass
argument-hint: "[scope]"
example: "/sova:test src/payments/"
---

## Name
sova:test

## Synopsis
```
/sova:test [scope]
```

## Description

The `sova:test` command runs the project's linter and test suite, iterating on failures until both pass cleanly. It does not implement new features; it is meant to close out a change that is already written by driving the existing checks to green.

## Implementation

1. **Identify the scope**
   - Determine what to test from the argument, or from `git diff --name-only` against the base branch if no scope is given, to focus on the modules actually changed.

2. **Run the linter**
   - Run the project's configured lint command (check the project's `Makefile`, `package.json` scripts, or CI configuration for the exact command, since these vary by project and language).
   - If it fails, analyze the errors, fix the underlying code (not just the lint output), and re-run.

3. **Run the tests**
   - Run the project's configured test command.
   - If tests fail, analyze the failure, fix the code or the test as appropriate, and re-run.

4. **Scout check**
   - While iterating on failures, note any pre-existing flaky tests, stale imports, or dead code encountered in the touched files, and fix the small, low-risk ones inline.

5. **Iterate**
   - Repeat steps 2 through 4 until both the linter and the test suite pass completely, or until the failure requires a decision the user should make (e.g. an intentionally skipped test).

6. **Report**
   - State the final lint and test status clearly. Do not report success while a check is still failing.

## Return Value
- **Claude agent text**: final linter and test status, and a summary of any fixes applied along the way.

## Examples

1. **Test the whole project**:
   ```
   /sova:test
   ```

2. **Test a specific scope**:
   ```
   /sova:test src/payments/
   ```

## Arguments
- `$1` (optional): a path or module to scope the linter and test run to. Defaults to the files changed on the current branch.

## See Also
- `/sova:develop`: implement changes before testing them
- `/sova:review`: self-review the change after tests pass, before opening a pull request
