---
name: ingest-review
description: Ingest PR review feedback into agent memory for continuous learning. Provide PR number.
user-invocable: true
category: learning
---

# Ingest PR Review Feedback

Process review comments from a merged PR and update agent memory.

## Instructions

1. Get the PR number from `$ARGUMENTS`. If empty, ask the user.

2. Fetch PR data:
   ```bash
   gh pr view <PR_NUMBER> --json comments,reviews,body,title
   gh api repos/<OWNER>/<REPO>/pulls/<PR_NUMBER>/comments
   ```

3. Analyze review comments and extract lessons:
   - **Patterns to always follow** -- things reviewers praised or requested
   - **Mistakes to avoid** -- bugs caught, missing edge cases, style violations
   - **Style preferences** -- formatting, naming, structural preferences
   - **Test coverage gaps** -- missing assertions, untested scenarios

4. Read existing memory file:
   - `.claude/agent-memory/cookbook.md`

5. Update `.claude/agent-memory/cookbook.md`:
   - Append new findings under the matching domain section (no duplicates)
   - If a mistake has appeared before, add it to the "Common Mistakes" section with `[Nx]` count
   - If a finding is high-impact, add it to `MEMORY.md`

6. Report what was learned and which files were updated.

## Cross-References

- **Run automatically after merge**: `/after-merge` includes this step
- **Extract broader knowledge**: Run `/extract-knowledge` for session-wide lessons

## Rules

- Only record actionable, specific lessons -- not generic advice
- NEVER use emojis in any output
