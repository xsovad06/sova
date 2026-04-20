---
name: issue
description: Fetch and analyze a GitHub Issue.
user-invocable: true
---

# GitHub Issue

Fetch and analyze a GitHub Issue.

## Instructions

1. Get the issue number from `$ARGUMENTS`. If empty, ask the user.

2. Fetch the issue:
   ```bash
   gh issue view <ISSUE_NUMBER> --json number,title,state,assignees,labels,milestone,body,comments
   ```

3. Present:
   - Title, status, assignees, labels, milestone
   - Description (summarized if long)
   - Comments (key discussion points)
   - Related issues (mentioned in body or comments)
   - Suggested approach for implementation

## Cross-References

- **Ready to implement?** Run `/develop-full <ISSUE_NUMBER>`
- **Want to explain approaches?** Run `/develop-explain <description>`
- **Planning your sprint?** Run `/sprint-plan`

## Rules

- NEVER use emojis in any output
