---
name: find-task
description: Browse GitHub Issues backlog and help select the next task.
user-invocable: true
---

# Find Task

Browse the project's GitHub Issues and help select the next task to work on.

## Instructions

1. **Fetch issues**:
   ```bash
   gh issue list --state open --json number,title,labels,milestone,assignees --limit 30
   ```

2. **Categorize** by effort and priority:
   - **Quick wins**: small scope, clear requirements
   - **Medium effort**: well-defined but multi-file
   - **Bigger work**: architectural, multi-component

3. **Present** a summary to the user with recommendations

4. **On selection**: help set up the feature branch (run `/new-feature`)

## Rules

- NEVER use emojis in any output
