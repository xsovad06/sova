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

4. Read existing memory files:
   - `.claude/agent-memory/review-feedback.md`
   - `.claude/agent-memory/common-mistakes.md`
   - `.claude/agent-memory/MEMORY.md`

5. Update memory files:
   - Append new findings to `review-feedback.md` under the appropriate section
   - If a mistake has appeared before, add it to `common-mistakes.md`
   - If a finding is high-impact, add it to `MEMORY.md`
   - Do NOT duplicate existing entries

6. Log the PR in `.claude/agent-memory/task-history.md`:
   - Ticket, date, summary, outcome

7. Report what was learned and which files were updated.

## Cross-References

- **Run automatically after merge**: `/after-merge` includes this step
- **Extract broader knowledge**: Run `/extract-knowledge` for session-wide lessons

## Rules

- Only record actionable, specific lessons -- not generic advice
- NEVER use emojis in any output
