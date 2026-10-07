---
name: sova-issue
description: "Fetch and analyze a task from the project tracker."
---

# Issue

Fetch and analyze a task from the project tracker.

## Instructions

1. Get the issue number or ticket key from `the arguments provided when this skill is invoked`. If empty, ask the user.

2. Determine the task source by running `sova config` and reading the `task_source` row (configuration lives in `.claude/sova.db`, not in a file).

3. Fetch the task:

   **GitHub** (the default):
   ```bash
   gh issue view <arguments> --json number,title,state,assignees,labels,milestone,body,comments
   ```

   **JIRA** (`task_source.type = "jira"`):
   ```bash
   jira issue view <arguments> --plain
   ```

4. Present:
   - Title, status, assignees, labels/components, milestone/sprint
   - Description (summarized if long)
   - Comments or activity (key discussion points)
   - Related/linked issues (mentioned in body, comments, or JIRA links)
   - Suggested approach for implementation

## Cross-References

- **Ready to implement?** Run the `sova-develop-full` skill <ISSUE_NUMBER>
- **Want a spec first?** Run the `sova-spec` skill <ISSUE_NUMBER>
- **Planning your sprint?** Run the `sova-find-task` skill

## Rules

- NEVER use emojis in any output
