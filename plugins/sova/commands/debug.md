---
description: Systematic debugging workflow: reproduce, locate, diagnose, fix, verify, prevent
argument-hint: "<issue-description>"
example: "/sova:debug Users are getting logged out after 5 minutes instead of 30"
---

## Name
sova:debug

## Synopsis
```
/sova:debug <issue-description>
```

## Description

The `sova:debug` command works through a bug systematically instead of guessing at fixes: reproduce the problem, locate the responsible code, diagnose the root cause, apply a fix, verify it, and add a regression test to prevent recurrence. It is meant for tasks where the cause of a failure is not yet known, as opposed to `/sova:develop`, which is for implementing already-understood changes.

## Implementation

1. **Reproduce**
   - Turn the bug description into a concrete, minimal reproduction: a failing test, a specific input, or a sequence of steps.
   - If it cannot be reproduced, say so explicitly and gather more information rather than guessing at a fix.

2. **Locate**
   - Use the reproduction to narrow down which module, function, or layer is responsible.
   - Read the relevant code paths rather than assuming based on the symptom alone.

3. **Diagnose**
   - Identify the actual root cause, not just the first place the symptom becomes visible. A stack trace often points at the effect, not the cause.
   - State the root cause explicitly before writing a fix.

4. **Fix**
   - Apply the smallest change that addresses the root cause.
   - Avoid defensive patches that mask the symptom without addressing why it happened.

5. **Verify**
   - Confirm the original reproduction no longer fails.
   - Run the project's full test suite to check for regressions elsewhere.

6. **Prevent**
   - Add a regression test that would have caught this bug, so it cannot silently reappear.
   - If the root cause suggests a class of similar bugs elsewhere in the codebase, note it for follow-up rather than expanding scope to fix all of them inline.

## Return Value
- **Claude agent text**: the reproduction steps, the diagnosed root cause, the fix applied, and the regression test added.
- **Working tree**: the fix and its regression test, uncommitted, ready for review.

## Examples

1. **Behavioral bug report**:
   ```
   /sova:debug The export button downloads an empty CSV when filters are applied
   ```

2. **Issue reference**:
   ```
   /sova:debug #341
   ```

## Arguments
- `$1` (required): a description of the bug, or a reference to an issue describing it.

## See Also
- `/sova:develop`: implement a change once the bug is understood, if a larger fix is needed
- `/sova:test`: run the test suite to check for regressions after a fix
