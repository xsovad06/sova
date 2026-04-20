---
name: review
description: Review changed code as a senior engineer before pushing. Scores findings by fix value and auto-fixes high-value issues (>=3/10). Run before /pr to catch issues early.
user-invocable: true
category: core
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

## 4. Score Each Finding

For each issue found, assign a **fix value score from 1-10**:

| Score | Meaning | Action |
|-------|---------|--------|
| 1-2 | Cosmetic or stylistic nitpick, no real impact | Report only, do not fix |
| 3-5 | Meaningful improvement: correctness, readability, or maintainability | **Auto-fix** |
| 6-8 | Important: prevents bugs, security issues, or significant tech debt | **Auto-fix** |
| 9-10 | Critical: data loss, security vulnerability, or broken functionality | **Auto-fix** |

## 5. Report Findings

For each finding, report:

```
### [FILE:LINE] Title (score: N/10)

**Category**: Bug / Security / Performance / Test / Consistency / Reuse / Efficiency / Error Handling / Simplicity
**What**: Description of the issue
**Why it matters**: Impact if left unfixed
**Fix**: What to change (or "Auto-fixed" if score >= 3)
```

## 6. Auto-Fix All Findings Scored >= 3

Fix each qualifying finding directly in the code. After all fixes:

```bash
git diff  # review your own fixes
```

If a fix is risky or ambiguous, flag it for human review instead of auto-fixing.

## 7. Run CI Checks Locally

Run the same checks the pipeline will run (see CLAUDE.md for commands).
Only run checks for modules with changed files. If any check fails, fix the issue and loop back to Step 6.

## 8. Summary

```
## Review Summary

**Files reviewed**: N
**Findings**: N total (N critical, N important, N moderate, N low)
**Auto-fixed**: N findings (scores >= 3)
**Skipped**: N findings (scores 1-2, cosmetic only)
**Assessment**: ready to push / needs human review / needs fixes

### Remaining items (if any)
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

- Be thorough but not pedantic. Don't flag things that are correct and clear.
- Search the codebase for existing patterns before suggesting a new one.
- If a finding requires understanding of business logic you don't have, flag it for human review.
- Do NOT add comments, docstrings, or type annotations unless they fix a real issue (score >= 3).
- Do NOT reformat code that wasn't changed in this branch.
- NEVER use emojis in any output.
