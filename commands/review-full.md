---
name: review-full
description: Full pre-push pipeline -- simplify changed code, review as senior engineer, organize commits. Run before /pr.
user-invocable: true
category: core
---

# Full Review Pipeline

Run a complete pre-push quality pipeline: simplify code, review for issues, then organize commits into clean history.

Scope: $ARGUMENTS

## Phase 1: Simplify

Follow the `/simplify` workflow -- review all changed code for opportunities to reduce complexity:

1. Get the diff scope:
   ```bash
   git diff main..HEAD --stat
   git diff --cached --stat
   git diff --stat
   ```

2. For each changed file, read the **entire file** and check:
   - **Code reuse**: does new code duplicate existing utilities or helpers? Search the codebase.
   - **Over-engineering**: unnecessary abstractions, premature generalization, excessive configurability
   - **Dead code**: unused imports, unreachable branches, commented-out code
   - **Redundancy**: repeated logic that could be a shared helper (only if 3+ occurrences)
   - **Complexity**: can any function be simplified without losing clarity?
   - **Efficiency**: redundant computations, repeated file reads, duplicate API calls

3. Apply simplifications directly. After each change, verify tests still pass (see CLAUDE.md for commands).

4. If no simplifications are needed, state that and move on.

## Phase 2: Review

Follow the `/review` command workflow:

1. Review all changed files for bugs, security, performance, test coverage, consistency, doc freshness, and scout rule (fix pre-existing issues in touched files).
2. Score each finding (1-10 priority). Address all findings (fix or acknowledge with justification).
3. Run CI checks locally. Fix any failures before proceeding.
4. Report findings in the standard review format.

See `/review` for the full review checklist and scoring criteria.

## Phase 3: Organize Commits

Follow the `/rearrange-commits` command workflow:

1. Soft reset all commits back to origin/main (stash uncommitted changes first if needed).
2. Create small, logical, atomic commits in dependency order.
3. Verify: all changes committed, each commit atomic, total diff matches original.

See `/rearrange-commits` for the full commit organization process.

## Phase 4: Summary

```
## Review-Full Summary

**Simplifications**: N changes applied
**Review findings**: N total (N fixed, N acknowledged)
**Commits**: reorganized into N clean commits
**Assessment**: ready to push / needs human review
```

## Workflow Chain

This command orchestrates:
1. Simplify -- reduce complexity and duplication
2. `/review` -- senior-engineer code review with auto-fix
3. `/rearrange-commits` -- organize into logical commits

After this command completes, run `/pr` to create the pull request.

## Cross-References

- **Came from**: `/develop-full` (as an alternative to its built-in Phase 2-3) or manual pre-push check
- **Next step**: `/pr` to create the pull request

## Rules

- All findings are addressed by default -- fix or acknowledge with justification (false positive, not applicable, requires human decision).
- Be thorough but not pedantic. Don't flag things that are correct and clear.
- Do NOT reformat code that wasn't changed in this branch.
- If a simplification or fix is risky, flag it for human review instead of applying.
- Each phase builds on the previous -- Phase 2 reviews the state after Phase 1's changes.
- NEVER use emojis in any output.
