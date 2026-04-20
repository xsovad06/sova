---
name: sprint-plan
description: Review and prioritize your assigned GitHub Issues for the sprint.
user-invocable: true
---

# Sprint Planning

Review your assigned GitHub Issues, prioritize them, and manage your sprint queue.

## Instructions

1. **Fetch assigned issues**:
   ```bash
   gh issue list --assignee @me --state open --limit 50 --json number,title,labels,milestone,updatedAt,body
   ```
   If the project uses GitHub Projects, also fetch board state:
   ```bash
   gh project item-list --owner <OWNER> --format json 2>/dev/null || true
   ```

2. **Categorize by status**:
   - **Active work**: issues with in-progress labels or linked open PRs
   - **Queued**: assigned but not started (backlog, ready)
   - **Needs refinement**: issues with vague descriptions or missing acceptance criteria
   - **Blocked**: issues with blocker labels or dependency on other issues

3. **For each issue**, provide:
   - Issue number, title, current labels
   - Estimated effort: small / medium / large (based on title and description)
   - Dependencies: does it block or depend on other issues?
   - Suggested order based on priority labels and dependencies

4. **Suggest a sprint plan**:
   - What to finish first (active work)
   - What to pick up next (from queued)
   - What needs refinement before starting

5. **If the user wants to re-prioritize**:
   - Add/remove labels: `gh issue edit <NUMBER> --add-label "priority:high"`
   - Unassign to defer: `gh issue edit <NUMBER> --remove-assignee @me`

6. **If `$ARGUMENTS` contains an issue number**, fetch its details with `gh issue view <NUMBER>` and provide a deeper analysis.

## Cross-References

- **Want to start a task?** Run `/develop-full <ISSUE_NUMBER>`
- **Need more context on an issue?** Run `/issue <ISSUE_NUMBER>`
- **Quick daily check?** Run `/standup` instead

## Rules

- NEVER use emojis in any output
