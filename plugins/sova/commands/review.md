---
description: Review changed code as a senior engineer before pushing, scoring and addressing findings by priority
argument-hint: "[scope]"
example: "/sova:review"
---

## Name
sova:review

## Synopsis
```
/sova:review [scope]
```

## Description

The `sova:review` command reviews the current changes as an independent senior engineer would, looking for real problems rather than style nitpicks. It scores findings by priority, fixes what it can fix directly, and reports the rest so the user can decide before pushing.

Run this before `/sova:pr` to catch issues while they are still cheap to fix, rather than after a reviewer (human or bot) finds them on the open pull request.

## Implementation

1. **Gather the diff**
   - Determine the scope from the argument, or default to the working tree's uncommitted and unpushed changes (`git diff` against the merge base of the current branch).

2. **Review for correctness first**
   - Logic errors, off-by-one mistakes, missing null/edge-case handling, race conditions, and incorrect assumptions about external state.
   - Security issues: injection, unsafe deserialization, secrets in code, missing authorization checks.
   - Data integrity: migrations, schema changes, and anything that could corrupt or lose persisted state.

3. **Review for quality**
   - Whether the change matches existing patterns and conventions in the codebase.
   - Test coverage: are the new code paths actually exercised by tests, including edge cases?
   - Whether documentation that describes the changed behavior is still accurate.

4. **Score and prioritize findings**
   - Rank findings by severity/impact, not by how easy they are to describe.
   - Distinguish must-fix issues from optional suggestions.

5. **Address findings**
   - Fix must-fix issues directly, matching the codebase's existing style.
   - For anything not fixed automatically (design trade-offs, out-of-scope issues), report it clearly with file and line references so the user can decide.

6. **Report**
   - Summarize the findings addressed, the findings deferred (with reasoning), and the final review score.

## Return Value
- **Claude agent text**: a review report with a priority-ordered list of findings, which ones were fixed, and a final score.
- **Working tree**: any findings fixed directly, applied to the existing changes.

## Examples

1. **Review the current working-tree diff**:
   ```
   /sova:review
   ```

2. **Review a specific scope**:
   ```
   /sova:review src/api/
   ```

## Arguments
- `$1` (optional): the scope to review (a path, branch, or diff range). Defaults to the current working tree's changes.

## See Also
- `/sova:develop` -- implement the change being reviewed
- `/sova:pr` -- create a pull request once the review is clean
