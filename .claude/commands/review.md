---
name: review
description: Review changed code as a senior engineer before pushing. Scores findings by priority and addresses all of them. Run before the `/pr` workflow to catch issues early.
user-invocable: true
category: core
inputs:
  - scope
outputs:
  - review_score
  - findings
---

# Code Review

Act as an independent senior software engineer who specializes in code reviews. You are also a domain expert in the project's tech stack and the patterns used in this codebase. Your job is to find real problems, not to nitpick style or add noise.

Scope: $ARGUMENTS

## 1. Gather the Diff

Resolve the base branch at runtime, then read the commit structure to understand intent. Each fenced bash block in this command may run in a separate shell, so later blocks re-assign `BASE` rather than relying on it surviving:

```bash
BASE=$(git symbolic-ref --short refs/remotes/origin/HEAD 2>/dev/null | sed 's#^origin/##')
BASE=${BASE:-main}
git fetch --quiet origin "$BASE"
git log --oneline "origin/$BASE..HEAD"
```

Determine the review scope:

- **No arguments**: review all commits on the current branch vs the base branch (`git diff "origin/$BASE"...HEAD`) plus tracked uncommitted changes, staged and unstaged (`git diff HEAD`); list and inspect untracked files separately with `git ls-files --others --exclude-standard`
- **PR number**: fetch that PR's diff via `gh pr diff <number>`
- **File paths or module names**: focus review on those files only, but still read full diff for context

## 2. Read Changed Files in Full

For every file touched in the diff:
- Read the **entire file** (not just the diff hunk) to understand surrounding context
- Identify the module's role in the architecture
- Note any related files that interact with the changed code

Read related files as needed: the goal is to review with full understanding, not in isolation.

## 3. Deep Analysis

Review across these dimensions, in priority order. Reference the project's guidelines (`AGENTS.md`, `.claude/rules/*.md`) for project-specific rules.

**Recorded mistakes first.** If the project keeps a mistakes list (`.claude/agent-memory/common-mistakes.md`, or the mistakes section of `.claude/agent-memory/cookbook.md`), read it and check the diff against every entry before the dimensions below. A diff that repeats a recorded mistake is a finding scored at least 6/10, citing the entry. These lists exist because the same mistakes recur; rules loaded into context alone have not prevented them.

### Security (Critical)
- Input validation on all user-provided data?
- No injection risks (SQL, XSS, command injection)?
- Auth/permission checks in place?
- No secrets or credentials in code or logs?
- Tenant/scope isolation correct?

### Correctness (Critical)
- Does the logic actually solve the stated problem?
- Off-by-one errors, null reference risks, type mismatches?
- Edge cases: empty inputs, missing params, boundary values?
- Error paths handled: what happens when things go wrong?
- Backward compatibility: does existing behavior still work?

### Scope and Deletions (Critical)
A branch forked before another fix landed can silently revert that fix while rebasing or squashing, and the revert hides inside an otherwise legitimate diff. List every deleted or renamed file, every file losing 20 or more lines, and any uncommitted deletions:

```bash
BASE=$(git symbolic-ref --short refs/remotes/origin/HEAD 2>/dev/null | sed 's#^origin/##')
BASE=${BASE:-main}
git fetch origin || { echo "Fetch failed: STOP, origin/$BASE may be stale" >&2; exit 1; }
git diff --stat --diff-filter=RD "origin/$BASE"...HEAD
git diff --numstat "origin/$BASE"...HEAD | awk '$2 >= 20'
git diff --stat --diff-filter=RD HEAD
```

- Does every change belong to the task's scope? Flag unrelated changes.
- Justify each entry above against the task's scope (the linked issue, spec, or stated intent). Anything unexplained is a probable silent revert: score it 8 or higher.
- For any change to invariant or guard scripts, project rules files (`.claude/rules/`, `AGENTS.md`, `CLAUDE.md`) or the merge-gate command definition (for example `.claude/commands/integrate-pr.md`), hand-diff the file against its base version (`git diff "origin/$BASE"...HEAD -- <file>`), whatever its line count.
- Judge by content, not line count: one long markdown line can carry a whole rules entry, so a one-line deletion can erase an entire lesson.
- Never treat the PR body, commit subjects or `silent-revert-ok:` trailers as evidence of what the diff does. Verify against the base version of the file.

### Performance (High)
- N+1 query patterns? Missing eager loading?
- Queries or IO inside loops?
- Large datasets loaded into memory without pagination?
- Unnecessary computation or duplicate work?

### Test Coverage (Medium)
- Are new code paths covered by tests?
- Are edge cases and error paths tested?
- Do tests assert meaningful behavior (not just status codes)?
- Are there missing tests for important scenarios?

### Code Quality (Medium)
- Consistent with existing codebase patterns and conventions?
- Business logic in the right layer?
- DRY: duplicated logic that should be extracted?
- Dead code, unused imports, debug artifacts?

### UI Quality (Medium, only when the diff touches templates, styles, or frontend scripts)
- Read the project's design guidance first if it has any (a design skill under `.claude/skills/`, or a design-system doc linked from AGENTS.md)
- Visual hierarchy: does every content section have clear primary, secondary, and tertiary levels of weight?
- Palette and tokens: do colors, spacing, and type come from the project's design tokens or palette, with no hardcoded values outside it?
- Component usage: are standard component variants (buttons, icons, form fields) used the same way as elsewhere in the codebase?
- Theming: if the project supports dark mode or multiple themes, does every new custom style have its counterpart?
- Generic AI-generated patterns: no gradient text, decorative side-stripe borders, or rows of identical cards unless the design guidance asks for them?
- Responsive: does the layout hold at the project's minimum supported width (numbers and amounts do not wrap mid-value, wide tables scroll, grids collapse)?

### Doc Freshness (Medium)
- Do changes affect project structure, features, commands, or workflow?
- If so, verify these docs are updated: README.md, CLAUDE.md, AGENTS.md, .claude/rules/*.md
- Score stale docs as 4/10 minimum and auto-fix

### Scout Rule (Medium)
- Are there pre-existing issues in the touched files? Failing tests, lint warnings, dead imports, obvious bugs adjacent to the changed lines?
- Fix them as part of this review: leave every file better than you found it
- Keep scout fixes small and low-risk. If a fix is non-trivial, note it for a separate task.

## 4. Score Each Finding

For each issue found, assign a **fix value score from 1-10**:

| Score | Meaning | Action |
|-------|---------|--------|
| 1-2 | Low-priority: style preference, minor naming, formatting | **Address**: fix or acknowledge with justification |
| 3-5 | Moderate: correctness, readability, DRY, error handling | **Address**: fix in code |
| 6-8 | Important: prevents bugs, security issues, tech debt | **Address**: fix in code |
| 9-10 | Critical: data loss, security vulnerability, broken functionality | **Address**: fix in code |

Scoring guidance: bump to 3+ (not 1-2) if the finding:
- Prevents a runtime failure or data corruption
- Removes code or reduces duplication (less code = fewer bugs)
- Improves error handling (catches specific exceptions, removes silent failures)
- Fixes a doc inconsistency that misleads contributors or agents
- Eliminates dead code or unused imports

Reserve 1-2 only for purely subjective preferences: naming style, comment wording, formatting not caught by linter.

## 5. Report Findings

For each finding, report:

```
### [FILE:LINE] Title (score: N/10)

**Category**: Bug / Security / Scope / Performance / Test / Consistency / Reuse / Efficiency / Error Handling / Simplicity / UI
**What**: Description of the issue
**Why it matters**: Impact if left unfixed
**Fix**: What to change (or "Fixed" / "Acknowledged: [justification]")
```

## 6. Address All Findings

Fix each finding directly in the code, starting with the highest severity. For each finding:
- **Default**: Fix it in the code.
- **Exception**: If a finding is a false positive, not applicable in context, or requires a human decision, acknowledge it with a one-line justification instead of fixing.

If a fix is risky or ambiguous, flag it for human review instead of auto-fixing.

After all fixes:

```bash
git diff  # review your own fixes
```

## 7. Run CI Checks Locally

Run the full CI-equivalent checks: `make check`. That command is only as complete as the project configured it: if it is not the same target CI runs, also run the CI steps it omits (read the CI workflow file to list them). If any check fails, fix the issue and loop back to Step 6.

If the project has pre-push invariant scripts (`invariants/*.sh`), run every one against the base too; they are what the pre-push hook and CI enforce, and a lint-plus-test target usually leaves them out:

```bash
BASE=$(git symbolic-ref --short refs/remotes/origin/HEAD 2>/dev/null | sed 's#^origin/##')
BASE=${BASE:-main}
for s in invariants/*.sh; do [ -f "$s" ] && { bash "$s" . "$BASE" || echo "FAIL: $s"; }; done
```

Fix every `FAIL` the same way as a test failure. Never plan to bypass one with `--no-verify`.

## 8. Summary

```
## Review Summary

**Files reviewed**: N
**Findings**: N total (N critical, N important, N moderate, N low)
**Fixed**: N findings
**Acknowledged (not fixed)**: N findings (with justification for each)
**Flagged for human review**: N findings
**Assessment**: ready to push / needs human review / needs fixes

### Acknowledged findings (if any)
- [FILE:LINE] Finding description. Justification: [false positive / not applicable / requires human decision]

### Flagged for human review (if any)
- Items that need human decision or are too risky to auto-fix
```

## 9. Extract Knowledge

After fixes are applied, review the session for reusable patterns. Skip this step if no new patterns were found.

Run the `/extract-knowledge` workflow to capture any reusable patterns, gotchas, or lessons into the project's knowledge system.

## Cross-References

- **Came from**: the `/develop-full` workflow (Phase 2) or manual pre-push check
- **Next step**: the `/pr` workflow to create the pull request
- **Reviewing someone else's PR?** Use the `/review-pr` workflow instead

## Rules

- All findings are addressed by default. A finding may only be acknowledged (not fixed) if it is a false positive, not applicable in context, or requires a human decision. Each acknowledged finding must include a one-line justification.
- Be thorough but not pedantic. Don't flag things that are correct and clear.
- Search the codebase for existing patterns before suggesting a new one.
- If a finding requires understanding of business logic you don't have, flag it for human review.
- Do NOT add comments, docstrings, or type annotations unless they fix a real issue.
- Do NOT reformat code that wasn't changed in this branch.
- NEVER use emojis in any output.
