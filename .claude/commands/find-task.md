---
name: find-task
description: Browse the SOVA project board, show priority-ordered tasks, and suggest what to work on next.
user-invocable: true
---

# Find Next Task

Browse the SOVA Roadmap project board (priority-ordered) and suggest the next task.

**Scope**: $ARGUMENTS

## Instructions

### 1. Fetch Tasks from Project Board (Priority Order)

```bash
gh api graphql -f query='query { user(login:"xsovad06") { projectV2(number:2) { items(first:30) { nodes { content { ... on Issue { number title state labels(first:10) { nodes { name } } } } order: fieldValueByName(name:"Priority Order") { ... on ProjectV2ItemFieldNumberValue { number } } phase: fieldValueByName(name:"Phase") { ... on ProjectV2ItemFieldSingleSelectValue { name } } } } } } }' --jq '.data.user.projectV2.items.nodes | sort_by(.order.number) | .[] | select(.content.state == "OPEN")'
```

If `$ARGUMENTS` contains a phase filter (e.g., "phase 1"), narrow to that phase.

### 2. Present in Priority Order

Show a table:

| Order | Issue | Phase | Labels | Status |
|-------|-------|-------|--------|--------|

Group by phase. Mark the **top unblocked issue** as the recommended next task.

### 3. Check Dependencies

For the top 3-5 candidates, read their issue bodies:
```bash
gh issue view <NUMBER> --json body --jq '.body'
```

Check the "Dependencies" section. If a dependency issue is still open, mark it as blocked and skip to the next.

### 4. Recommend

Present the recommended task with:
- Why it's next (priority order, no blockers, phase sequencing)
- Brief summary of what it involves (from issue body)
- Link to the relevant section in `docs/REWRITE-PLAN.md`
- Estimated scope (from issue acceptance criteria count)

### 5. When the User Selects

- Read `docs/REWRITE-PLAN.md` for architectural context
- Read the full issue body for implementation details
- Suggest: "Run `/develop-full <ISSUE_NUMBER>` to start working"

## Cross-References

- **Architecture & plan**: `docs/REWRITE-PLAN.md`
- **After selecting a task**: Run `/develop-full <ISSUE_NUMBER>`
- **Daily overview**: Run `/standup`

## Rules

- NEVER use emojis in any output
- Always show tasks in Priority Order (from project board), not arbitrary sorting
- The lowest-numbered open, unblocked task is the default recommendation
