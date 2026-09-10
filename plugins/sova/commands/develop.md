---
description: Implement a feature or fix using test-driven development, then verify with the project's test suite
argument-hint: "<task-description-or-issue-reference>"
example: "/sova:develop Add rate limiting to the login endpoint"
---

## Name
sova:develop

## Synopsis
```
/sova:develop <task-description-or-issue-reference>
```

## Description

The `sova:develop` command implements a requested feature or fix using a test-driven development approach, then verifies the result by running the project's own test suite. It is intended for a single, well-scoped unit of work: a bug fix, a small feature, or a targeted refactor.

The command favors the smallest, simplest change that satisfies the request over generic or clever solutions, and matches the conventions already present in the codebase (naming, error handling, architecture layers) rather than imposing new patterns.

## Implementation

1. **Understand the context**
   - Read the task description or referenced issue.
   - Read the project's own contributor documentation (e.g. `CLAUDE.md`, `AGENTS.md`, `CONTRIBUTING.md`, or an equivalent) for conventions.
   - Identify which module(s) the work touches and read the relevant existing source before writing any code.

2. **Write tests first**
   - Define the expected behavior as tests before writing the implementation.
   - Cover the positive path, the negative path, and edge cases.

3. **Implement the solution**
   - Follow the project's established architecture and layering.
   - Match existing naming conventions, error handling style, and code structure.
   - Reuse existing utilities and helpers instead of duplicating logic.
   - Keep the change scoped to the task; do not refactor unrelated code.

4. **Scout check**
   - For every file touched, look for adjacent pre-existing issues (obvious bugs, stale imports, dead code, missing null checks) and fix the small, low-risk ones inline. Note larger issues for a separate task rather than expanding scope.

5. **Verify**
   - Run the project's linter and test suite (check the project's `Makefile`, `package.json` scripts, or CI configuration for the exact commands, since these vary by project and language).
   - If a check fails, diagnose and fix, then re-run. Retry up to three times before reporting the failure back to the user with full context.

6. **Self-check before finishing**
   - All tests pass.
   - The linter is clean.
   - No debug code or stray print/log statements were left behind.
   - No changes were made outside the task's scope.

## Return Value
- **Claude agent text**: a summary of the files changed, the tests added or updated, and the final test/lint status.
- **Working tree**: the implementation and its tests, uncommitted, ready for review.

## Examples

1. **Feature request in plain language**:
   ```
   /sova:develop Add a --dry-run flag to the cleanup script
   ```

2. **Issue reference**:
   ```
   /sova:develop Fix #482: pagination returns duplicate rows on page 2
   ```

## Arguments
- `$1` (required): a description of the task, or a reference to an issue/ticket to develop.

## See Also
- `/sova:spec` -- produce a design document before development starts, for larger or ambiguous tasks
- `/sova:test` -- run the linter and test suite iteratively without making new changes
- `/sova:review` -- self-review the change before pushing
