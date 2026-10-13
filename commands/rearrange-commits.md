---
name: rearrange-commits
description: Reorganize current branch commits into small, logical, well-documented steps.
user-invocable: true
category: core
inputs: []
outputs:
  - commits_reorganized
---

# Rearrange Commits

Reorganize all commits on the current branch into clean, logical, atomic commits.

## Instructions

### Step 1: Analyze Current State

Resolve the base branch at runtime rather than assuming `main`. Every shell block runs in a fresh shell, so each block below that uses `BASE` re-assigns it first. Fall back to the repo default only when `gh pr view` confirms there is no PR for this branch yet; abort on any other failure (auth, network, API) instead of guessing:

```bash
BASE=$(gh pr view --json baseRefName --jq .baseRefName 2>&1) || { case "$BASE" in *"no pull requests found"*) BASE=$(gh repo view --json defaultBranchRef --jq .defaultBranchRef.name) ;; *) echo "Unable to resolve the base branch: $BASE" >&2; exit 1 ;; esac; }
[ -n "$BASE" ] || { echo "Unable to resolve the base branch" >&2; exit 1; }
echo "Base branch: $BASE"
git fetch origin || { echo "Fetch failed: STOP, origin/$BASE may be stale" >&2; exit 1; }
git branch --show-current
git log "origin/$BASE"..HEAD --oneline
git diff "origin/$BASE"...HEAD --stat
git status
```

### Step 2: Preserve All Changes

Never use `git stash` here: the stash stack is shared by every worktree and session of this repository, so a pop can restore someone else's changes. Commit uncommitted work instead, and keep a backup branch until the rearranged history is verified.

1. Commit any uncommitted changes as a WIP commit (review `git status` first, so nothing that must not be committed, such as secrets or scratch files, is included):
   ```bash
   if [ -n "$(git status --porcelain)" ]; then
     git add -A && git commit -m "chore: WIP snapshot before rearrange" \
       || { echo "WIP commit failed (hook, signing or staging error): STOP, nothing was reset" >&2; exit 1; }
   fi
   ```
2. Create a uniquely named backup branch and note the printed name, since later blocks re-assign `BACKUP` to it. The tree must be fully committed first, or the Step 4 recovery would drop the uncommitted work:
   ```bash
   [ -z "$(git status --porcelain)" ] || { echo "Uncommitted changes remain: STOP before creating the backup" >&2; exit 1; }
   BACKUP="backup/rearrange-$(date +%s)-$$-$RANDOM"
   git branch "$BACKUP" || { echo "Failed to create backup branch $BACKUP: STOP before resetting" >&2; exit 1; }
   echo "Backup branch: $BACKUP"
   ```
3. Soft reset to the point where this branch forked from the base, and note the printed SHA. Use the fork point (`git merge-base`), not `origin/$BASE` itself: if the base branch moved since this branch was last rebased, committing the branch's tree on top of the newer base would silently revert everything the base gained in between.
   ```bash
   BASE=$(gh pr view --json baseRefName --jq .baseRefName 2>&1) || { case "$BASE" in *"no pull requests found"*) BASE=$(gh repo view --json defaultBranchRef --jq .defaultBranchRef.name) ;; *) echo "Unable to resolve the base branch: $BASE" >&2; exit 1 ;; esac; }
   [ -n "$BASE" ] || { echo "Unable to resolve the base branch" >&2; exit 1; }
   FORK_POINT=$(git merge-base HEAD "origin/$BASE")
   echo "Fork point: $FORK_POINT"
   git reset --soft "$FORK_POINT"
   git reset HEAD .
   ```
   All changes, including the WIP snapshot, are now unstaged in the working tree.

### Step 3: Create Small, Logical Commits

Group changes by logical unit and commit in order:

1. Schema/model/migration changes first
2. Core logic/services second
3. API/views/controllers third
4. Tests fourth
5. Configuration/misc last

### Commit Message Format

```
type(scope): short description

Detailed explanation of WHAT, WHY, and HOW.
```

Types: feat, fix, refactor, test, docs, chore, perf

Check the project's commit format invariant or AGENTS.md for valid scopes.
Do NOT invent new scopes: pre-push hooks may reject them.

### Step 4: Verify

```bash
BACKUP=<backup branch printed in Step 2>
FORK_POINT=<fork point printed in Step 2>
git status --porcelain
git diff "$BACKUP"
git log "$FORK_POINT"..HEAD --oneline
git diff "$FORK_POINT"...HEAD --stat
```

`git status --porcelain` and `git diff "$BACKUP"` MUST both print nothing: the rearranged branch must have exactly the tree it had before. If either prints anything, restore the original history and start again from Step 2.3:

```bash
BACKUP=<backup branch printed in Step 2>
git reset --hard "$BACKUP"
```

Ensure:
- All changes are committed (nothing left uncommitted, no WIP commit left in the history)
- Each commit is atomic and self-contained
- Commits are in logical order
- The total diff matches what it was before reorganization

Only once all of that holds, delete the backup:

```bash
BACKUP=<backup branch printed in Step 2>
git branch -D "$BACKUP"
```

If the branch must also be brought up to date with the base, rebase it afterwards. This is a fresh shell, so resolve `BASE` again rather than relying on Step 1's value:
```bash
BASE=$(gh pr view --json baseRefName --jq .baseRefName 2>&1) || { case "$BASE" in *"no pull requests found"*) BASE=$(gh repo view --json defaultBranchRef --jq .defaultBranchRef.name) ;; *) echo "Unable to resolve the base branch: $BASE" >&2; exit 1 ;; esac; }
[ -n "$BASE" ] || { echo "Unable to resolve the base branch" >&2; exit 1; }
git fetch origin && git rebase "origin/$BASE"
```

## Cross-References

- **Called by**: `/develop-full` (Phase 3) and `/pr` (commit organization step)
- **Before PR**: Run `/review` after rearranging, then `/pr`

## Rules

- Each commit = ONE logical change
- Earlier commits should not depend on later ones
- Keep commits small: easier to review and revert
- Never create standalone doc-count commits (e.g., "docs: update counts"): fold doc/count updates into the commit that introduced the change (the one that added the service, step, test, etc.)
- NEVER use emojis in any output
