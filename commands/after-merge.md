---
name: after-merge
description: Post-merge cleanup -- sync main, delete branch, clean worktrees, update issue status, capture learnings.
user-invocable: true
category: pr
---

# After Merge Cleanup

Run this after a PR has been merged to clean up local state and capture learnings.

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

4. **Clean up worktrees** (if any exist for this ticket):
   ```bash
   git worktree list
   ```
   Remove worktrees for the merged ticket:
   ```bash
   git worktree remove .claude/worktrees/<ID> --force 2>/dev/null || true
   ```

5. **Close linked issue and update project board** (if not auto-closed by PR):
   ```bash
   gh issue close <ISSUE_NUMBER> 2>/dev/null || true
   ```
   If the project uses a GitHub Projects board, move the issue to "Done".

6. **Capture learnings** from the PR review (run `/ingest-review` workflow):
   - Fetch PR review data
   - Extract lessons: patterns to follow, mistakes to avoid, style preferences, test coverage gaps
   - Update `.claude/agent-memory/cookbook.md` (under matching domain section)

7. **Report** what was cleaned up and what was learned.

## Cross-References

- **Learning from the review**: Calls `/ingest-review` internally
- **Extract broader knowledge**: Run `/extract-knowledge` if significant patterns emerged
- **Ready for next task?** Run `/find-task` or `/standup`

## Rules

- NEVER use emojis in any output
