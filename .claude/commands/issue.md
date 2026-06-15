---
name: issue
description: Fetch and analyze a task from the project tracker.
user-invocable: true
category: management
inputs:
  - issue_number
outputs:
  - issue_analysis
---

# Issue

Fetch and analyze a task from the project tracker.

## Instructions

1. Get the issue number or ticket key from `$ARGUMENTS`. If empty, ask the user.

2. Determine the task source by reading `sova.toml` (if it exists) and checking `[task_source] type`.

3. Fetch the task:

   **GitHub** (default, or no sova.toml):
   ```bash
   gh issue view $ARGUMENTS --json number,title,state,assignees,labels,milestone,body,comments
   ```

   **JIRA** (`task_source.type = "jira"`):
   ```bash
   jira issue view $ARGUMENTS --plain
   ```

4. Present:
   - Title, status, assignees, labels/components, milestone/sprint
   - Description (summarized if long)
   - Comments or activity (key discussion points)
   - Related/linked issues (mentioned in body, comments, or JIRA links)
   - Suggested approach for implementation

## Cross-References

- **Ready to implement?** Run `/develop-full <ISSUE_NUMBER>`
- **Want a spec first?** Run `/spec <ISSUE_NUMBER>`
- **Planning your sprint?** Run `/find-task`

## Rules

- NEVER use emojis in any output
