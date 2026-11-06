---
name: after-merge
description: "Post-merge cleanup: sync the base branch, delete the branch, clean worktrees, update issue status, capture learnings."
user-invocable: true
category: pr
inputs:
  - pr_number
  - branch_name
outputs:
  - cleanup_done
---

# After Merge Cleanup

The `/integrate-pr` workflow is the preferred path (it merges, audits and cleans up in one run); this command is kept for PRs merged outside it.

Run this after a PR has been merged to clean up local state and capture learnings.

## Instructions

Each shell block below runs as its own shell invocation, so each one assigns every variable it reads.

1. **Get the PR** from `$ARGUMENTS` (PR number or branch name). If empty, check for recently merged PRs:
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

5. **Update the linked issues and clean up labels**. On a GitHub project, the merged PR's body decides which issues the merge finishes:
   ```bash
   sova linked-issues <PR_NUMBER> --apply
   ```
   Each issue a closing keyword names (`Closes #N`, `Fixes #N`, `Resolves #N` and their other forms) moves to `integration.post_merge_state` ("done" closes it). Each `Part of #N` issue stays open, gets one comment listing its unchecked acceptance criteria, and loses the workflow labels `agent:in-review`, `agent:in-progress`, `sova:revise` and `sova:block` it carries, unless another open PR still links the issue (those labels then stay, and the output says so), so a half-finished issue no longer reads as awaiting review. A bare `#N` is left alone. Never close an issue by hand instead: a `Part of` issue must stay open. A non-zero exit means an issue was not updated (a failed comment or label removal included), so name each such issue from the output in the final report and carry on. On a Jira project, move the ticket the PR title names through its workflow instead.

   Then, for each issue the command moved (a closing reference, not a `Part of` or bare one), remove the workflow labels that no longer apply after the merge. Only the labels the issue carries are passed, since `gh` errors on a label the repository never created (for example `sova:block`); name any failure in the final report instead of discarding it:
   ```bash
   if ! LABELS=$(gh issue view <ISSUE_NUMBER> --json labels --jq '.labels[].name'); then
     echo "FAILED: could not read the labels of #<ISSUE_NUMBER>" >&2
   else
     ARGS=()
     for label in agent:in-review agent:in-progress sova:revise sova:block; do
       if printf '%s\n' "$LABELS" | grep -qxF "$label"; then ARGS+=(--remove-label "$label"); fi
     done
     if [ "${#ARGS[@]}" -gt 0 ] && ! gh issue edit <ISSUE_NUMBER> "${ARGS[@]}"; then
       echo "FAILED: could not remove the workflow labels of #<ISSUE_NUMBER>" >&2
     fi
   fi
   ```

6. **Capture learnings** from the PR review (run the `/ingest-review` workflow):
   - Fetch PR review data
   - Extract lessons: patterns to follow, mistakes to avoid, style preferences, test coverage gaps
   - Update the PRIMARY checkout's cookbook (`cookbook/<domain>.md` when split, otherwise `cookbook.md` under the matching section) before removing any worktree: a worktree's copy of `.claude/agent-memory/` may be gitignored and is deleted with it

7. **Report** what was cleaned up, any worktree or branch kept and why, and what was learned.

## Cross-References

- **Preferred path**: the `/integrate-pr` workflow merges, runs the post-merge audit, and does this cleanup in one run
- **Learning from the review**: calls the `/ingest-review` workflow internally
- **Extract broader knowledge**: run the `/extract-knowledge` workflow if significant patterns emerged
- **Ready for next task?** Run the `/find-task` workflow or the `/standup` workflow

## Rules

- Never swallow a failed branch or worktree deletion with `|| true`: report what was kept and why
- NEVER use emojis in any output
