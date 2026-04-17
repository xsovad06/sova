---
name: standup
description: Daily standup summary -- git state, open PRs, GitHub Issues, and suggested focus.
user-invocable: true
---

# Daily Standup

Generate a quick daily context dump.

## Instructions

1. **Git state**:
   ```bash
   git branch --show-current
   git status --short
   git log --oneline -5
   ```
   If on a feature branch: `git rev-list --count main..HEAD`

2. **Open PRs**:
   ```bash
   gh pr list --author @me --state open --json number,title,url,reviewDecision,statusCheckRollup
   ```
   For each: number, title, check status, review state.
   Flag PRs with failing checks.

3. **Assigned issues**:
   ```bash
   gh issue list --assignee @me --state open --json number,title,labels,milestone
   ```
   Group by status: In Progress, Code Review, Backlog.

4. **Present summary**:
   - **Working on**: current branch + uncommitted changes
   - **Open PRs**: list with CI/review status
   - **Issue queue**: grouped by status
   - **Suggested focus**: what to work on based on priority and blockers

## Rules

- Keep the report concise -- one screen max
- NEVER use emojis in any output
