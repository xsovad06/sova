---
name: review
description: Review changed code as a senior engineer before pushing. Scores findings by fix value and auto-fixes high-value issues (>=3/10).
user-invocable: true
---

# Code Review

Review the current branch changes as a senior engineer. Catch issues before pushing, score them by value, and auto-fix high-value findings.

## Instructions

### Step 1: Get the Changes

```bash
git diff main..HEAD
git log --oneline main..HEAD
git diff main..HEAD --stat
```

If there are no commits ahead of main, fall back to uncommitted changes:
```bash
git diff HEAD
git diff --cached
```

### Step 2: Review Each Changed File

Analyze every changed file for:

- **Bugs**: logic errors, off-by-one, null/nil handling, race conditions, error swallowing
- **Security**: injection vulnerabilities, auth bypass, secrets in code, unsafe deserialization
- **Performance**: N+1 patterns, unbounded operations, missing indexes, hot-path bloat
- **Test coverage**: are new code paths tested? Edge cases? Are tests meaningful or just smoke?
- **Consistency**: does new code follow existing patterns in the module? Naming, structure, error handling style?
- **Missing changes**: if a schema changed, are migrations created? If an API changed, is the spec updated?
- **Doc freshness**: do the changes affect project structure, features, commands, or workflow? If so, verify these docs are updated:
  - `README.md` -- project tree, feature list, usage examples
  - `CLAUDE.md` -- run commands, knowledge tiers
  - `AGENTS.md` -- conventions, testing instructions
  - `.claude/rules/architecture.md` -- component overview, design decisions
  - `docs/VISION.md` -- roadmap phases (if applicable)
  Score stale docs as 4/10 minimum and auto-fix.
- **Code reuse**: does new code duplicate existing utilities or helpers in the codebase?
- **Efficiency**: redundant computations, repeated file reads, duplicate API calls, missed concurrency
- **Error handling**: are errors propagated correctly? Are failure modes tested? Silent failures?
- **Simplicity**: is the solution more complex than necessary? Over-abstracted? Could be done in fewer lines?

### Step 3: Score Each Finding

For each issue found, assign a **fix value score from 1-10**:

| Score | Meaning | Action |
|-------|---------|--------|
| 1-2 | Purely subjective preference: naming style, comment wording, formatting not caught by linter | Auto-fix if it removes code, improves error handling, or reduces duplication; skip otherwise |
| 3-5 | Meaningful improvement: correctness, readability, DRY, error handling, code removal | **Auto-fix** |
| 6-8 | Important: prevents bugs, security issues, or significant tech debt | **Auto-fix** |
| 9-10 | Critical: data loss, security vulnerability, or broken functionality | **Auto-fix** |

**Scoring criteria** -- higher scores for findings that:
- Prevent a runtime failure or data corruption
- Close a security hole
- Remove code that will confuse the next reader
- Eliminate a performance cliff (not a micro-optimization)
- Fix incorrect behavior vs. just suboptimal behavior

**Lower scores** for findings that:
- Are purely stylistic (formatting, naming preferences)
- Add comments or documentation
- Refactor working code for marginal improvement
- Are subjective ("I would have done it differently")

### Step 4: Report Findings

For each finding, report:

```
### [FILE:LINE] Title (score: N/10)

**Category**: Bug / Security / Performance / Test / Consistency / Reuse / Efficiency / Error Handling / Simplicity
**What**: Description of the issue
**Why it matters**: Impact if left unfixed
**Fix**: What to change (or "Auto-fixed" if score >= 3)
```

### Step 5: Auto-Fix All Findings Scored >= 3

Fix each qualifying finding directly in the code. After all fixes:

```bash
# Verify nothing is broken
git diff  # review your own fixes
```

If a fix is risky or ambiguous, flag it for human review instead of auto-fixing.

### Step 6: Commit Review Fixes

If you made any auto-fixes, stage and commit them:
```bash
git add -A
git commit -m "fix: address review findings"
```

Skip this step if no fixes were made.

### Step 7: Run CI Checks Locally

Run the same checks the pipeline will run, scoped to modules that have changes (see CLAUDE.md for commands).
Only run checks for modules with changed files. If any check fails, fix the issue and loop back to Step 5 before proceeding.

### Step 8: Summary

```
## Review Summary

**Files reviewed**: N
**Findings**: N total (N critical, N important, N moderate, N low)
**Auto-fixed**: N findings (scores >= 3)
**Skipped**: N findings (scores 1-2, subjective only)
**Assessment**: ready to push / needs human review / needs fixes

### Remaining items (if any)
- Items that need human decision or are too risky to auto-fix
```

## Rules

- Be thorough but not pedantic. Don't flag things that are correct and clear.
- Search the codebase for existing patterns before suggesting a new one.
- If a finding requires understanding of business logic you don't have, flag it for human review rather than guessing.
- Do NOT add comments, docstrings, or type annotations unless they fix a real issue (score >= 3).
- Do NOT reformat code that wasn't changed in this branch.
- NEVER use emojis in any output.
