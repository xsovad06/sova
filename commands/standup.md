---
name: standup
description: Daily standup summary -- git state, open PRs, GitHub Issues, and suggested focus.
user-invocable: true
category: management
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
   gh pr list --author @me --state open
   ```
   For each: number, title, check status, review state.
   Flag PRs with failing checks.

3. **Assigned issues**:
   ```bash
   gh issue list --assignee @me --state open --json number,title,labels,milestone
   ```
   Group by status labels (e.g., in-progress, backlog, blocked).
   If the project uses GitHub Projects:
   ```bash
   gh project item-list --owner <OWNER> --format json 2>/dev/null || true
   ```

4. **Present summary**:
   - **Working on**: current branch + uncommitted changes
   - **Open PRs**: list with CI/review status
   - **Issue queue**: grouped by status/priority
   - **Suggested focus**: what to work on based on priority and blockers

## Cross-References

- **Want to pick up a new task?** Run `/find-task`
- **Ready to work on something?** Run `/develop-full <ISSUE_NUMBER>`

## Rules

- NEVER use emojis in any output
