---
name: standup
description: Daily context summary -- git state, open PRs, assigned issues, suggested focus.
user-invocable: true
---

# Daily Standup

Provide a quick summary of the current project state and suggest what to focus on.

## Instructions

1. **Git state** (run in parallel):
   - `git branch --show-current`
   - `git status --short`
   - `git log --oneline -5`

2. **Open PRs**:
   ```bash
   gh pr list --author @me --state open --json number,title,url,reviewDecision,statusCheckRollup
   ```

3. **Assigned issues**:
   ```bash
   gh issue list --assignee @me --state open --json number,title,labels,milestone
   ```

4. **Report**:
   - Current branch and uncommitted changes
   - Open PRs with CI/review status
   - Assigned issues grouped by priority
   - Suggested focus (most impactful next action)

## Rules

- Keep the report concise -- one screen max
- NEVER use emojis in any output
