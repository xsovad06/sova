---
name: find-task
description: Browse GitHub Issues backlog, prioritize sprint work, and suggest next tasks.
user-invocable: true
---

# Find Next Task

Browse the GitHub Issues backlog, review sprint priorities, and suggest what to work on next.

**Scope**: $ARGUMENTS

## Instructions

### 1. Fetch Open Issues

```bash
# Assigned issues first
gh issue list --assignee @me --state open --limit 50 --json number,title,labels,milestone,updatedAt,body

# If no assigned issues, broaden to all open
gh issue list --state open --limit 30 --json number,title,labels,milestone,updatedAt
```

If `$ARGUMENTS` contains filters (label, milestone), incorporate them:
```bash
gh issue list --state open --label "<label>" --milestone "<milestone>" --limit 30
```

### 2. Categorize by Status

- **Active work**: issues with in-progress labels or linked open PRs
- **Queued**: assigned but not started (backlog, ready)
- **Quick wins**: small, well-scoped tasks (look for size/effort labels)
- **Medium effort**: refactors, feature work with clear scope
- **Needs refinement**: issues with vague descriptions or missing acceptance criteria
- **Blocked**: issues with blocker labels or dependency on other issues

### 3. Analyze Each Issue

For each issue, provide:
- Issue number, title, current labels
- Estimated effort: small / medium / large (based on title and description)
- Dependencies: does it block or depend on other issues?
- Suggested priority based on labels, dependencies, and effort

### 4. Suggest a Plan

- What to finish first (active work)
- What to pick up next (from queued, prioritized by labels and dependencies)
- Quick wins that can be done between larger tasks
- What needs refinement before starting

### 5. Present and Wait

Present the summary and wait for the user to choose.

### 6. When the User Selects an Issue

- Assign it (if not already): `gh issue edit <NUMBER> --add-assignee @me`
- Suggest creating a feature branch: `git checkout -b feat/<short-name> main`

### 7. Re-prioritize (if requested)

- Add/remove labels: `gh issue edit <NUMBER> --add-label "priority:high"`
- Unassign to defer: `gh issue edit <NUMBER> --remove-assignee @me`

## Cross-References

- **After selecting a task**: Run `/develop-full <ISSUE_NUMBER>` to start working
- **Want a deeper look at an issue?** Run `/issue <ISSUE_NUMBER>`
- **Daily overview**: Run `/standup` for full context

## Rules

- NEVER use emojis in any output
