---
name: after-merge
description: Post-merge cleanup -- sync main, delete branch, update issue status.
user-invocable: true
---

# After Merge Cleanup

Run this after a PR has been merged to clean up local state.

## Instructions

1. **Get the PR** from `$ARGUMENTS` (PR number or branch name). If empty, check for recently merged PRs:
   ```bash
   gh pr list --author @me --state merged --limit 5
   ```

2. **Switch to main and pull**:
   ```bash
   git checkout main
   git pull origin main
   ```

3. **Delete the merged branch locally**:
   ```bash
   git branch -d <branch-name>
   ```
   If the branch name isn't obvious: `git branch --merged main`

4. **Close linked issue** (if not auto-closed by PR):
   ```bash
   gh issue close <ISSUE_NUMBER> 2>/dev/null || true
   ```

5. **Report** what was cleaned up.

## Cross-References

- **Ready for next task?** Run `/find-task` or `/standup`

## Rules

- NEVER use emojis in any output
