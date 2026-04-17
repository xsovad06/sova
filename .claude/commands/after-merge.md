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

4. **Close linked issue and update project board** (if not auto-closed by PR):
   ```bash
   # Close the issue
   gh issue close <ISSUE_NUMBER> 2>/dev/null || true

   # Move to "Done" on the project board
   ITEM_ID=$(gh api graphql -f query='query { user(login:"xsovad06") { projectV2(number:2) { items(first:50) { nodes { id content { ... on Issue { number } } } } } } }' --jq '.data.user.projectV2.items.nodes[] | select(.content.number == <ISSUE_NUMBER>) | .id')
   if [ -n "$ITEM_ID" ]; then
     gh api graphql -f query='mutation { updateProjectV2ItemFieldValue(input: { projectId: "PVT_kwHOArVFrc4BU8uF", itemId: "'"$ITEM_ID"'", fieldId: "PVTSSF_lAHOArVFrc4BU8uFzhMdaz8", value: { singleSelectOptionId: "98236657" } }) { projectV2Item { id } } }'
   fi
   ```

5. **Report** what was cleaned up.

## Cross-References

- **Ready for next task?** Run `/find-task` or `/standup`

## Rules

- NEVER use emojis in any output
