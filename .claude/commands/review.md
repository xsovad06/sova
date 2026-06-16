---
name: review
description: Review changed code as a senior engineer before pushing. Scores findings by priority and addresses all of them. Run before /pr to catch issues early.
user-invocable: true
category: core
inputs:
  - scope
outputs:
  - review_score
  - findings
---

# Code Review

Act as an independent senior software engineer who specializes in code reviews. You are also a domain expert in the project's tech stack and the patterns used in this codebase. Your job is to find real problems -- not to nitpick style or add noise.

Scope: $ARGUMENTS

## 1. Gather the Diff

Determine the review scope:

- **No arguments**: review all commits on the current branch vs the main branch (`git diff main...HEAD`) plus any uncommitted changes (`git diff`)
- **PR number**: fetch that PR's diff via `gh pr diff <number>`
- **File paths or module names**: focus review on those files only, but still read full diff for context

Run `git log --oneline main..HEAD` to understand the commit structure and intent.

## 2. Read Changed Files in Full

For every file touched in the diff:
- Read the **entire file** (not just the diff hunk) to understand surrounding context
- Identify the module's role in the architecture
- Note any related files that interact with the changed code

Read related files as needed -- the goal is to review with full understanding, not in isolation.

## 3. Deep Analysis

Review across these dimensions, in priority order. Reference the project's guidelines (`AGENTS.md`, `docs/*-guidelines.md`) for project-specific rules.

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
- Error paths handled -- what happens when things go wrong?
- Backward compatibility -- does existing behavior still work?

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
- DRY -- duplicated logic that should be extracted?
- Dead code, unused imports, debug artifacts?

### Doc Freshness (Medium)
- Do changes affect project structure, features, commands, or workflow?
- If so, verify these docs are updated: README.md, CLAUDE.md, AGENTS.md, .claude/rules/*.md
- Score stale docs as 4/10 minimum and auto-fix

### Scout Rule (Medium)
- Are there pre-existing issues in the touched files? Failing tests, lint warnings, dead imports, obvious bugs adjacent to the changed lines?
- Fix them as part of this review -- leave every file better than you found it
- Keep scout fixes small and low-risk. If a fix is non-trivial, note it for a separate task.

## 4. Score Each Finding

For each issue found, assign a **fix value score from 1-10**:

| Score | Meaning | Action |
|-------|---------|--------|
| 1-2 | Low-priority: style preference, minor naming, formatting | **Address**: fix or acknowledge with justification |
| 3-5 | Moderate: correctness, readability, DRY, error handling | **Address**: fix in code |
| 6-8 | Important: prevents bugs, security issues, tech debt | **Address**: fix in code |
| 9-10 | Critical: data loss, security vulnerability, broken functionality | **Address**: fix in code |

Scoring guidance -- bump to 3+ (not 1-2) if the finding:
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

**Category**: Bug / Security / Performance / Test / Consistency / Reuse / Efficiency / Error Handling / Simplicity
**What**: Description of the issue
**Why it matters**: Impact if left unfixed
**Fix**: What to change (or "Fixed" / "Acknowledged -- [justification]")
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

Run the same checks the pipeline will run (see CLAUDE.md for commands).
Only run checks for modules with changed files. If any check fails, fix the issue and loop back to Step 6.

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
- [FILE:LINE] Finding description -- Justification: [false positive / not applicable / requires human decision]

### Flagged for human review (if any)
- Items that need human decision or are too risky to auto-fix
```

## 9. Extract Knowledge

After fixes are applied, review the session for reusable patterns. Skip this step if no new patterns were found.

Run `/extract-knowledge` to capture any reusable patterns, gotchas, or lessons into the project's knowledge system.

## Cross-References

- **Came from**: `/develop-full` (Phase 2) or manual pre-push check
- **Next step**: `/pr` to create the pull request
- **Reviewing someone else's PR?** Use `/review-pr` instead

## Rules

- All findings are addressed by default. A finding may only be acknowledged (not fixed) if it is a false positive, not applicable in context, or requires a human decision. Each acknowledged finding must include a one-line justification.
- Be thorough but not pedantic. Don't flag things that are correct and clear.
- Search the codebase for existing patterns before suggesting a new one.
- If a finding requires understanding of business logic you don't have, flag it for human review.
- Do NOT add comments, docstrings, or type annotations unless they fix a real issue.
- Do NOT reformat code that wasn't changed in this branch.
- NEVER use emojis in any output.
