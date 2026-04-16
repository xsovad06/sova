---
name: find-task
description: Browse GitHub Issues backlog and suggest suitable tasks to pick up.
user-invocable: true
---

# Find Next Task

Browse the GitHub Issues backlog and suggest suitable tasks.

## Instructions

1. Fetch open issues assigned to you (or all open issues if none assigned):
   ```bash
   gh issue list --assignee @me --state open --limit 30 --json number,title,labels,milestone,updatedAt
   ```
   If no assigned issues, broaden:
   ```bash
   gh issue list --state open --limit 30 --json number,title,labels,milestone,updatedAt
   ```
   If `$ARGUMENTS` contains additional filters (label, milestone), incorporate them:
   ```bash
   gh issue list --state open --label "<label>" --milestone "<milestone>" --limit 30
   ```

2. Analyze and categorize:
   - **Already on your plate**: issues assigned to you with in-progress labels or linked PRs
   - **Quick wins**: small, well-scoped tasks (look for size/effort labels)
   - **Medium effort**: refactors, feature work with clear scope
   - **Bigger but interesting**: structural improvements, multi-part work
   - For each: issue number, title, labels, one-line rationale

3. Present the summary and wait for the user to choose.

4. If the user selects an issue:
   - Assign it (if not already): `gh issue edit <NUMBER> --add-assignee @me`
   - Suggest creating a feature branch: `git checkout -b feat/<short-name> main`

## Cross-References

- **After selecting a task**: Run `/develop-full <ISSUE_NUMBER>` to start working
- **Want a deeper look at an issue?** Run `/issue <ISSUE_NUMBER>`
- **Daily overview**: Run `/standup` for full context

## Rules

- NEVER use emojis in any output
