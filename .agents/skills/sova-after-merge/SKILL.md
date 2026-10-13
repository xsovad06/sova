---
name: sova-after-merge
description: "Post-merge cleanup: sync the base branch, delete the branch, clean worktrees, update issue status, capture learnings."
---

# After Merge Cleanup

The `sova-integrate-pr` skill is the preferred path (it merges, audits and cleans up in one run); this command is kept for PRs merged outside it.

Run this after a PR has been merged to clean up local state and capture learnings.

## Instructions

Each shell block below runs as its own shell invocation, so each one assigns every variable it reads.

1. **Get the PR** from `the arguments provided when this skill is invoked` (PR number or branch name). If empty, check for recently merged PRs:
   ```bash
   gh pr list --author @me --state merged --limit 5
   ```

2. **Switch to the base branch and pull** (from the primary checkout, not a worktree):
   ```bash
   BASE=$(gh pr view <PR_NUMBER> --json baseRefName --jq .baseRefName)
   git checkout "$BASE"
   git pull origin "$BASE"
   ```

3. **Clean up worktrees** (if any exist for this ticket). Do this before deleting the branch: git refuses to delete a branch that a worktree still has checked out.
   ```bash
   git worktree list
   ```
   Remove worktrees for the merged ticket. Do not pass `--force`: if a worktree has uncommitted changes, leave it in place and report it rather than discarding the work.
   ```bash
   git worktree remove .claude/worktrees/<ID>
   ```
   Prune dangling worktree references (directories removed but entries remain):
   ```bash
   git worktree prune
   ```

4. **Delete the merged branch locally**:
   ```bash
   git branch -d <branch-name>
   ```
   If `-d` refuses ("not fully merged", expected after a rebase merge), confirm `BASE=$(gh pr view <PR_NUMBER> --json baseRefName --jq .baseRefName); git diff "origin/$BASE" <branch-name>` prints nothing, then `git branch -D <branch-name>`. If the diff is non-empty only because the base branch moved on after the merge, compare against the PR's merge commit instead (`gh pr view <PR_NUMBER> --json mergeCommit --jq .mergeCommit.oid`); if that is not empty either, keep the branch and report it.

5. **Close linked issue and clean up labels**:
   ```bash
   gh issue close <ISSUE_NUMBER> 2>/dev/null || true
   ```
   Remove stale workflow labels that are no longer meaningful on a closed issue:
   ```bash
   gh issue edit <ISSUE_NUMBER> --remove-label "agent:in-review" --remove-label "agent:in-progress" --remove-label "sova:revise" --remove-label "sova:block" 2>/dev/null || true
   ```

6. **Capture learnings** from the PR review (run the `sova-ingest-review` skill):
   - Fetch PR review data
   - Extract lessons: patterns to follow, mistakes to avoid, style preferences, test coverage gaps
   - Update `.claude/agent-memory/cookbook.md` (under matching domain section)

7. **Report** what was cleaned up, any worktree or branch kept and why, and what was learned.

## Cross-References

- **Preferred path**: the `sova-integrate-pr` skill merges, runs the post-merge audit, and does this cleanup in one run
- **Learning from the review**: calls the `sova-ingest-review` skill internally
- **Extract broader knowledge**: run the `sova-extract-knowledge` skill if significant patterns emerged
- **Ready for next task?** Run the `sova-find-task` skill or the `sova-standup` skill

## Rules

- Never swallow a failed branch or worktree deletion with `|| true`: report what was kept and why
- NEVER use emojis in any output
