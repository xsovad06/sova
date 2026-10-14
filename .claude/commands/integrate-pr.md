---
name: integrate-pr
description: Full integration pipeline. Verify mergeability, gate silent reverts, wait for CI, merge, clean up. One click from approved PR to done.
user-invocable: true
category: pr
inputs:
  - pr_number
outputs:
  - merge_result
---

# Integrate PR

Merge a PR that is already ready, then clean up. Verifies mergeability, checks the replayed commits for silent reverts, merges, audits what actually landed, cleans up branches/worktrees/stashes, closes the linked issue, and runs post-merge cleanup. Replaces the need to run `/after-merge` separately. Works for both manual invocation and autonomous agent use.

**This command does not push by default.** A push creates a new head SHA and spends a full CI cycle, which on a ready PR is pure waste and delays the merge by the length of the suite. The PR is expected to arrive here mergeable, with review learnings and documentation already folded in by `/address-pr`. Two things justify a push, both meaning another PR merged into the base branch since this one was last pushed: a real conflict (`mergeable: CONFLICTING`), or, under this repository's strict status-check policy, the branch simply being behind (`mergeStateStatus: BEHIND`) even with no conflicting lines. Phase 2 resolves either case and is explicit about the cost.

PR: $ARGUMENTS

## Instructions

**Shell blocks.** Every fenced block below runs as its own shell invocation: no variable survives from one block to the next, so each block assigns every variable it reads. `<PR_NUMBER>`, `<BASE_BRANCH>`, `<HEAD_BRANCH>` and `<ISSUE_NUMBER>` are filled in from the Phase 1 metadata.

**Primary checkout.** State shared between phases lives in the PRIMARY checkout's `.claude/agent-control/`. The Update subroutine may `cd` into a per-issue worktree, and `.claude/agent-control/` is not mirrored into worktrees, so a bare relative path would read and write a different file depending on where each phase runs. Every block that needs it opens with these two lines (the first is `git rev-parse --git-common-dir`; the `case` maps a linked worktree to its primary checkout), never assuming the current directory is it:

```bash
COMMON_DIR=$(git rev-parse --git-common-dir 2>/dev/null)
case "$COMMON_DIR" in /*) PRIMARY_ROOT="${COMMON_DIR%/.git}" ;; *) PRIMARY_ROOT="$(git rev-parse --show-toplevel)" ;; esac
```

**Silent-revert guard.** Phase 2b and Phase 6 use the project's silent-revert guard if it has one: a script that flags commits silently undoing other work and honours an explicit commit range (for example `invariants/silent-revert-guard.sh <worktree> <base>` with `SILENT_REVERT_RANGE=<a>..<b>`). Both blocks resolve its repo-relative path with the same expression, re-derived in each block because shells do not carry over:

```bash
GUARD_PATH="${SILENT_REVERT_GUARD:-invariants/silent-revert-guard.sh}"
```

If the project's CLAUDE.md or AGENTS.md names a different silent-revert guard script, set SILENT_REVERT_GUARD to its repo-relative path before running these blocks (for example by prefixing the block with `export SILENT_REVERT_GUARD=<path>`). Both phases run the copy from `origin/<BASE_BRANCH>`, never the PR's own copy, so a PR cannot weaken the check that judges it. If no guard honouring an explicit range exists at the resolved path, both phases print a `SKIPPED` line naming the path they checked and continue; say so in the Phase 7 report, since a wrong path skips both gates silently otherwise.

### Phase 1: Identify the PR

Determine the PR number from available context, in priority order:

1. **From arguments** (`$ARGUMENTS`): use directly if a number is provided
2. **From current branch**: query for an open PR on the current branch:
   ```bash
   gh pr view --json number,title,body,state,baseRefName,headRefName,statusCheckRollup,reviewDecision,commits,mergeable
   ```
3. **From recent PRs**: if on the base branch, list recent open PRs authored by the current user:
   ```bash
   gh pr list --author @me --state open --limit 10
   ```
   Ask the user which one to integrate (unless running autonomously, in which case stop and report ambiguity).

If no PR can be identified, stop and report clearly.

Once identified, fetch full PR metadata:

```bash
gh pr view <PR_NUMBER> --json number,title,body,state,baseRefName,headRefName,statusCheckRollup,reviewDecision,commits,mergeable
```

**Stop if**:
- PR state is `CLOSED`: report and stop.
- PR state is `MERGED`: skip straight to Phase 6 (cleanup). Phase 6 still runs
  its post-merge silent-revert audit, which is the only check an out-of-band
  merge (web UI, another agent, a merge queue) ever gets.

Extract the linked issue number from the PR body (patterns: `Closes #N`, `Fixes #N`, `Resolves #N`) or title (`#N`). This is optional: the pipeline works without a linked issue.

Log the review decision status (APPROVED, CHANGES_REQUESTED, etc.) but do NOT require formal approval to proceed. The user invoking this command is the approval.

### Phase 2: Assess Mergeability (push only when the PR cannot merge as it stands)

The default path through this command is: do not touch the branch, do not push,
do not re-run CI. Every push here creates a new head SHA and costs a full CI
cycle, so a push must be justified by GitHub refusing to merge, never by
routine hygiene.

Reset the push-tracking state file first. It is a file rather than a shell
variable because Phases 2 through 4 run as separate shell invocations:

```bash
COMMON_DIR=$(git rev-parse --git-common-dir 2>/dev/null)
case "$COMMON_DIR" in /*) PRIMARY_ROOT="${COMMON_DIR%/.git}" ;; *) PRIMARY_ROOT="$(git rev-parse --show-toplevel)" ;; esac
mkdir -p "$PRIMARY_ROOT/.claude/agent-control"
echo 0 > "$PRIMARY_ROOT/.claude/agent-control/integrate-pushed"
```

Ask GitHub whether the PR can merge:

```bash
gh pr view <PR_NUMBER> --json mergeable,mergeStateStatus,baseRefName,headRefName
```

Act on `mergeable` first, then, when it is clean, on `mergeStateStatus`:

- **`mergeable: CONFLICTING`**: a PR merged into the base branch since this one
  was last pushed and the two diverge on the same lines. Run the update
  subroutine below; expect it to hit real conflicts.

- **`mergeable: UNKNOWN`**: GitHub is still computing mergeability. Re-query up
  to 5 times, 5 seconds apart. If it is still `UNKNOWN`, treat it as
  `MERGEABLE` and let the merge attempt in Phase 5 surface the real answer. Do
  not rebase speculatively.

- **`mergeable: MERGEABLE` and `mergeStateStatus: BEHIND`**: no conflicting
  lines, but the branch has not incorporated everything merged into the base
  branch since it was last pushed. This repository's `main-protection` ruleset
  sets `strict_required_status_checks_policy: true`, so GitHub will refuse to
  merge until the branch is updated, regardless of how green its existing
  checks are. Run the update subroutine below; expect it to complete cleanly
  with no conflicts, since `mergeable` already ruled those out.

  This is a real, unavoidable CI cost, not a routine-hygiene push: skipping it
  would leave the branch permanently unmergeable under this ruleset. Confirm
  the policy before assuming it applies in another project:
  ```bash
  gh api repos/<OWNER>/<REPO>/rulesets --jq '.[] | select(.target == "branch") | .id'
  gh api repos/<OWNER>/<REPO>/rulesets/<ID> \
    --jq '.rules[] | select(.type == "required_status_checks")
          | .parameters.strict_required_status_checks_policy'
  ```
  If that returns `false`, a `BEHIND` state is not a merge blocker there and
  this step may be skipped, matching the `MERGEABLE`/not-`BEHIND` case below.

- **`mergeable: MERGEABLE` and anything else** (typically `CLEAN`): nothing to
  do. Do NOT rebase, do NOT push. Go to Phase 2b.

#### Update subroutine

Handles both a real conflict (`mergeable: CONFLICTING`) and a clean-but-stale
branch (`mergeStateStatus: BEHIND`). The rebase step is identical either way;
only step 3 (conflict resolution) has anything to do when there are no
conflicts to resolve.

1. **Get onto the PR branch safely.** Never run `git checkout <HEAD_BRANCH>` in
   the primary checkout: if a worktree already holds the branch, the checkout
   fails or, worse, leaves uncommitted work that bleeds onto the base branch
   later. Resolve the worktree first:
   ```bash
   git fetch origin || { echo "Fetch failed: STOP, refs may be stale" >&2; exit 1; }
   git worktree prune
   HEAD_BRANCH_REF="branch refs/heads/<HEAD_BRANCH>"
   git worktree list --porcelain | grep -Fx -B2 "$HEAD_BRANCH_REF" \
     | grep "^worktree " | head -1 | sed 's/^worktree //'
   ```
   If that prints a path, `cd` into it and verify `git branch --show-current`
   equals `<HEAD_BRANCH>`. If it prints nothing and the current branch already
   matches `<HEAD_BRANCH>`, you are in the right place. If neither holds, STOP
   and report: there is no safe place to rebase.

2. **Rebase**. Save the pre-rebase commit first, as a ref rather than a shell
   variable so step 6 (a later shell) can still read it: once the rebase
   completes, `git rebase --abort` reports no rebase is in progress and cannot
   undo it, so a validation failure discovered afterward needs this to
   restore the branch.
   ```bash
   git update-ref "refs/integrate-backup/<PR_NUMBER>" HEAD
   git rebase origin/<BASE_BRANCH>
   ```

3. **Resolve only conflicts you fully understand.** Bounded: at most 5
   conflicting commits, at most 3 attempts each.

   Safe to resolve autonomously:
   - both sides added entries to the same list, import block, or registry
   - both sides edited the same file in non-overlapping places
   - the base branch renamed or moved something this PR also touches, where the
     intent of each side is unambiguous
   - a lockfile or generated file that can be regenerated

   STOP and hand off instead of guessing:
   - both sides changed the same function's behaviour or signature
   - the conflict spans a semantic decision (which of two implementations wins)
   - resolving would silently drop a change from either side

4. After each resolution, `git add <file>` and `git rebase --continue`, then
   verify nothing is left behind:
   ```bash
   grep -rn "<<<<<<<\|>>>>>>>" <resolved_files>
   ```
   This must produce no output. `git add` clears a file's unmerged index entry
   whether or not the markers were removed, so the index alone does not prove
   the conflict was resolved.

5. When all commits are replayed, run the project's linter and tests (see
   CLAUDE.md). The rebase pulled in new base-branch code, so a green result
   from before the rebase means nothing.

6. **On any failure**:
   - **Unresolvable conflict or attempt budget exhausted** (rebase still in
     progress):
     ```bash
     git rebase --abort
     ```
   - **Tests failing after the rebase completed** (step 5, no rebase in
     progress: `git rebase --abort` here reports nothing to abort and leaves
     the rebased commits in place):
     ```bash
     git reset --hard "refs/integrate-backup/<PR_NUMBER>"
     ```
   Then delete the backup ref (`git update-ref -d "refs/integrate-backup/<PR_NUMBER>"`),
   stop, and report the conflicting files, pointing the user at
   `/address-pr <PR_NUMBER>`. Never leave the worktree mid-rebase.

7. **Before pushing, fold in any stale documentation for free.** This push is
   already required, so capturing documentation now costs nothing extra: run
   the Phase 3 check (steps 1-2 below) right here, before this push goes out.
   Any later amend would only rewrite the local commit and never reach the PR
   head that Phase 5 merges.

   If the check finds something stale, stage it now (do not commit or push it
   separately). The loop skips paths absent from both the worktree and the
   index, and paths the project gitignores: `git add` exits non-zero on either,
   and some projects keep `.claude/` local-only, so their memory and rules never
   belong in a commit. Tracked entries are staged unconditionally first
   (`git check-ignore` on a directory can report it ignored even though it has
   tracked descendants, which would otherwise skip their deletions too), and
   the ignore check applies only to staging new, untracked files:
   ```bash
   for p in .claude/agent-memory/ AGENTS.md README.md docs/ .claude/rules/; do
     [ -n "$(git ls-files -- "$p")" ] && git add -u -- "$p"
     { [ -e "$p" ] && ! git check-ignore -q "$p"; } && git add -A -- "$p"
   done
   ```

   Then amend anything staged into this commit and push once:
   ```bash
   COMMON_DIR=$(git rev-parse --git-common-dir 2>/dev/null)
   case "$COMMON_DIR" in /*) PRIMARY_ROOT="${COMMON_DIR%/.git}" ;; *) PRIMARY_ROOT="$(git rev-parse --show-toplevel)" ;; esac
   if ! git diff --cached --quiet; then
     git commit --amend --no-edit
   fi
   git push --force-with-lease && echo 1 > "$PRIMARY_ROOT/.claude/agent-control/integrate-pushed" \
     && git update-ref -d "refs/integrate-backup/<PR_NUMBER>"
   ```
   **Stop if the push fails** (branch protection, permissions) and report.

   **Phase 3 is then a no-op**: its state-file check below sees `1` and skips
   entirely, since the check already ran here, before this push.

8. **Return to the primary checkout** before continuing, whether or not step 1
   `cd`'d into a worktree. Every later phase assumes it is running from the
   primary checkout; the Update subroutine is the one place in this command
   that changes that:
   ```bash
   COMMON_DIR=$(git rev-parse --git-common-dir 2>/dev/null)
   case "$COMMON_DIR" in /*) PRIMARY_ROOT="${COMMON_DIR%/.git}" ;; *) PRIMARY_ROOT="$(git rev-parse --show-toplevel)" ;; esac
   cd "$PRIMARY_ROOT"
   ```

### Phase 2b: Silent-Revert Trial Merge Gate (hard-blocking)

A textually clean merge can still silently drop work: a branch forked before a later base-branch commit can carry a stale copy of a file, and landing it overwrites the newer content without a single conflict marker. `mergeable: CLEAN` only means git's merge did not stop. This gate reproduces locally, at zero CI cost, what Phase 5 will land on the current base tip, and runs the silent-revert guard against exactly that. It runs after Phase 2 whether or not the Update subroutine ran.

The reproduction follows the merge method Phase 5 will use (`merge_method` from the config loader; for `auto`, the methods the repository allows) and the shape of the PR:

- **Rebase replay**, only when the method is certainly `rebase` (configured, or `auto` on a repository that allows nothing else) and the PR contains no merge commits. The PR's commits are replayed one by one in a throwaway worktree and the guard judges each replayed commit under its own subject's scope, exactly as they will land.
- **Merged result**, in every other case: squash or merge-commit methods, an `auto` method that could be either, or any PR containing merge commits. A branch that merged its base in to resolve a conflict keeps that resolution in the merge commit, so replaying its linear commits conflicts even though GitHub merges it cleanly and keeps reporting `MERGEABLE`/`CLEAN`; judging that replay would block the PR forever. Instead the gate computes the single merged tree (`git merge-tree --write-tree`, git 2.38 or later), wraps it in one commit on top of the base whose message is the PR title (so a scope-based guard reads the conventional-commit scope, as it would on a squash commit) followed by every `silent-revert-ok:` line from the PR's commit bodies (so declared exemptions still apply), and runs the guard on that one commit. A PR that legitimately deletes across several scopes names them all in its title (`feat(core,imports): ...`) or carries the guard's justification trailer. On a git older than 2.38 the gate falls back to the rebase replay and says so.

**This gate is hard-blocking, with no override flag**: these reverts happen invisibly, so a bypass would defeat it. The only path past a real violation is fixing the branch (drop or amend the offending commit, or, when the cross-scope deletion is intentional, add the guard's justification trailer such as `silent-revert-ok: <reason>`) and letting `/pr` re-push.

Run the whole gate as one block; the subshell's `trap` removes the trial worktree, the trial ref and the guard copy on every exit path:

```bash
(
  PR=<PR_NUMBER>
  GUARD_PATH="${SILENT_REVERT_GUARD:-invariants/silent-revert-guard.sh}"
  COMMON_DIR=$(git rev-parse --git-common-dir 2>/dev/null)
  case "$COMMON_DIR" in /*) PRIMARY_ROOT="${COMMON_DIR%/.git}" ;; *) PRIMARY_ROOT="$(git rev-parse --show-toplevel)" ;; esac
  cd "$PRIMARY_ROOT" || exit 1
  BASE=$(gh pr view "$PR" --json baseRefName --jq .baseRefName)
  [ -n "$BASE" ] || { echo "GATE_ERROR: cannot resolve the base branch"; exit 1; }
  git fetch origin "$BASE" || { echo "GATE_ERROR: fetch of origin/$BASE failed"; exit 1; }
  if ! git show "origin/$BASE:$GUARD_PATH" 2>/dev/null | grep -q SILENT_REVERT_RANGE; then
    echo "GATE_SKIPPED: no silent-revert guard honouring SILENT_REVERT_RANGE at origin/$BASE:$GUARD_PATH"
    exit 0
  fi

  TRIAL_REF="refs/pr-trial/$PR"
  GUARD=$(mktemp)
  TRIAL=$(mktemp -d)
  trap 'git -C "$TRIAL" rebase --abort 2>/dev/null; git worktree remove --force "$TRIAL" 2>/dev/null;
        rm -rf "$TRIAL" "$GUARD"; git update-ref -d "$TRIAL_REF" 2>/dev/null; git worktree prune' EXIT
  git show "origin/$BASE:$GUARD_PATH" > "$GUARD"

  # The PR ref works for same-repo and fork PRs alike; a fork's branch is not in origin.
  git fetch origin "+refs/pull/$PR/head:$TRIAL_REF" || { echo "GATE_ERROR: cannot fetch the PR head"; exit 1; }
  HEAD_OID=$(gh pr view "$PR" --json headRefOid --jq .headRefOid)
  if [ "$(git rev-parse "$TRIAL_REF")" != "$HEAD_OID" ]; then
    echo "GATE_ERROR: fetched PR head is not headRefOid $HEAD_OID yet (a push just landed); retry shortly"
    exit 1
  fi

  # Reproduce what Phase 5 will land. Only a certain rebase merge of a linear PR
  # is replayed commit by commit; everything else is judged as one merged result.
  METHOD=$(python3 -c "from pathlib import Path; from sova.config.loader import load_config; \
print(load_config(Path('.')).integration.merge_method)" 2>/dev/null)
  [ -n "$METHOD" ] || METHOD=auto
  if [ "$METHOD" = auto ]; then
    ALLOWED=$(gh repo view --json rebaseMergeAllowed,squashMergeAllowed,mergeCommitAllowed \
      --jq '"\(.rebaseMergeAllowed) \(.squashMergeAllowed) \(.mergeCommitAllowed)"' 2>/dev/null)
    [ "$ALLOWED" = "true false false" ] && METHOD=rebase
  fi
  REPLAY=merged
  if [ "$METHOD" = rebase ] && [ -z "$(git rev-list --merges "origin/$BASE..$TRIAL_REF")" ]; then
    REPLAY=rebase
  fi
  GIT_VER=$(git version | sed -E 's/^git version ([0-9]+)\.([0-9]+).*/\1 \2/')
  GIT_MAJOR=${GIT_VER% *}; GIT_MINOR=${GIT_VER#* }
  if [ "$REPLAY" = merged ] && { [ "$GIT_MAJOR" -lt 2 ] || { [ "$GIT_MAJOR" -eq 2 ] && [ "$GIT_MINOR" -lt 38 ]; }; }; then
    echo "NOTE: git $GIT_MAJOR.$GIT_MINOR has no merge-tree --write-tree (needs 2.38+); falling back to the rebase replay, which can conflict where the real $METHOD merge would not"
    REPLAY=rebase-fallback
  fi
  echo "Merge method: $METHOD; replay: $REPLAY"

  if [ "$REPLAY" = merged ]; then
    MERGE_OUT=$(git merge-tree --write-tree "origin/$BASE" "$TRIAL_REF")
    RC=$?
    if [ "$RC" -eq 1 ]; then
      echo "GATE_CONFLICT: merging the PR head into origin/$BASE conflicts"
      exit 2
    fi
    [ "$RC" -eq 0 ] || { echo "GATE_ERROR: git merge-tree failed with exit $RC"; exit 1; }
    TITLE=$(gh pr view "$PR" --json title --jq .title)
    [ -n "$TITLE" ] || { echo "GATE_ERROR: cannot read the PR title"; exit 1; }
    EXEMPT=$(git log --format=%B "origin/$BASE..$TRIAL_REF" | grep -i '^[[:space:]]*silent-revert-ok:')
    if [ -n "$EXEMPT" ]; then set -- -m "$TITLE" -m "$EXEMPT"; else set -- -m "$TITLE"; fi
    TRIAL_COMMIT=$(GIT_AUTHOR_NAME=integrate-pr GIT_AUTHOR_EMAIL=integrate-pr@localhost \
      GIT_COMMITTER_NAME=integrate-pr GIT_COMMITTER_EMAIL=integrate-pr@localhost \
      git commit-tree "$(printf '%s\n' "$MERGE_OUT" | head -1)" -p "origin/$BASE" "$@") \
      || { echo "GATE_ERROR: cannot build the trial merge commit"; exit 1; }
    DIR="$PRIMARY_ROOT"
    RANGE="origin/$BASE..$TRIAL_COMMIT"
  else
    git worktree add --detach "$TRIAL" "$TRIAL_REF" >/dev/null || { echo "GATE_ERROR: cannot create the trial worktree"; exit 1; }
    if ! git -C "$TRIAL" rebase "origin/$BASE" >/dev/null 2>&1; then
      echo "GATE_REPLAY_CONFLICT: replaying the PR's commits one by one onto origin/$BASE conflicts"
      exit 2
    fi
    DIR="$TRIAL"
    RANGE="origin/$BASE..HEAD"
  fi

  # Negative control: a guard that passes an unresolvable range is not live.
  if SILENT_REVERT_RANGE="0000000000000000000000000000000000000000..HEAD" \
      bash "$GUARD" "$DIR" "$BASE" >/dev/null 2>&1; then
    echo "GUARD_NOT_LIVE: the guard exited 0 on an unresolvable range"
    exit 3
  fi

  if SILENT_REVERT_RANGE="$RANGE" bash "$GUARD" "$DIR" "$BASE"; then
    echo "GATE_PASS $HEAD_OID"
  else
    echo "GATE_FAIL"
    exit 4
  fi
)
```

Act on the marker it prints:

- **`GATE_PASS <sha>`** or **`GATE_SKIPPED`**: continue to Phase 3. Record which one, and the replay line printed above it, for the Phase 7 report, and keep the gated `<sha>`: Phase 5 merges only that exact head.
- **`GATE_CONFLICT`** (merged result): the PR head and the base tip conflict as a merge, which GitHub did not report in Phase 2, so the base branch almost certainly moved since then. Do not resolve it here. Stop and report; re-running `/integrate-pr <PR_NUMBER>` re-assesses mergeability, now `CONFLICTING`, and routes through the Update subroutine.
- **`GATE_REPLAY_CONFLICT`** (rebase replay): the commits do not replay cleanly onto the base, although the PR may well merge cleanly as a whole: GitHub computes `mergeable`/`CLEAN` for a merge, not a rebase, so do not report the PR as unmergeable and do not suggest simply re-running, which would hit the same replay. When the replay line says `rebase`, the rebase merge Phase 5 would perform fails on these same commits. Stop and report the remedy: rebase `<HEAD_BRANCH>` onto `origin/<BASE_BRANCH>`, resolve the conflicts, and push, then re-run `/integrate-pr <PR_NUMBER>`; the rewritten commits replay cleanly. When the replay line says `rebase-fallback`, the conflict may be an artefact of the old git rather than of the merge Phase 5 would perform: upgrading git to 2.38 or later and re-running judges the merged result instead, and the rebase-and-push remedy also works.
- **`GUARD_NOT_LIVE`**: STOP. The base branch's guard passes anything, so neither this gate nor the Phase 6 audit can be trusted. Report it as a defect in the guard itself.
- **`GATE_FAIL`**: STOP. Do not proceed to Phase 3, 4 or 5. Report the guard's output verbatim (commit, file, line counts, scope) and the fix described above. On the merged-result path the flagged commit is the synthetic trial commit, whose subject is the PR title: if the title's scope is too narrow for a legitimate cross-scope change, widening the title's scope is a fix as valid as the justification trailer.
- **`GATE_ERROR`**: STOP and report the error; nothing was checked.

### Phase 3: Documentation and Knowledge (verify; do not push on its own)

Knowledge capture and documentation freshness belong to `/address-pr` and `/review`, which run while the branch is already being pushed. This phase only verifies that they did their job. It must never create a head SHA of its own: a documentation amend on an otherwise-ready PR buys nothing and costs a full CI cycle.

**Skip this phase entirely if Phase 2 already pushed** (the state file reads `1`): the Update subroutine's step 7 already ran this exact check before that push, and a second run risks a confusing amend after the push has gone out. Read the state file from the primary checkout:
```bash
COMMON_DIR=$(git rev-parse --git-common-dir 2>/dev/null)
case "$COMMON_DIR" in /*) PRIMARY_ROOT="${COMMON_DIR%/.git}" ;; *) PRIMARY_ROOT="$(git rev-parse --show-toplevel)" ;; esac
PUSHED=$(cat "$PRIMARY_ROOT/.claude/agent-control/integrate-pushed" 2>/dev/null || echo 0)
echo "PUSHED=$PUSHED"
```

Only run the rest of this phase if `.claude/agent-memory/` exists in the
project AND `PUSHED` is `0`.

1. **Check for uncaptured review learnings**: fetch review data from the PR
   (`gh pr view`, `gh api repos/.../pulls/<N>/comments`,
   `gh api repos/.../pulls/<N>/reviews`). Enumerate every review thread from
   the whole PR history, resolved and unresolved, and check whether the
   actionable ones are already reflected in `.claude/agent-memory/cookbook.md`.

2. **Check documentation counts**: run the verification commands (test count,
   service count, router count) and compare against `AGENTS.md`, `README.md`,
   and the project's vision or roadmap doc, if AGENTS.md or README.md links one.

3. **Act on the result**:

   - **Nothing stale**: continue to Phase 4. This is the expected outcome when
     `/address-pr` ran properly.

   - **Stale**: Phase 2 did not push, so there is no push to ride for free. Do
     NOT amend and do NOT push. Queue the content for the next branch in the
     PRIMARY checkout, so it survives this PR's worktree cleanup: run
     `mkdir -p "$PRIMARY_ROOT/.claude/agent-control"` (resolving `PRIMARY_ROOT`
     as above), then append the following block to
     `$PRIMARY_ROOT/.claude/agent-control/pending-docs.md` with a file-editing
     tool, not a shell heredoc (agent-generated content may contain a line
     reading exactly `EOF`, which would end a heredoc early and run the rest).
     ```
     ## From PR #<PR_NUMBER> (<DATE>)
     <the cookbook entries, rule promotions and count corrections, verbatim>
     ```
     `/address-pr` drains this queue at the start of its next run, resolving
     the primary checkout the same way, and folds the content into that
     branch's own commits, which are pushed anyway. Report the queued items in
     Phase 7.

   - **Override**: if `$ARGUMENTS` contains `--with-docs`, the user has
     explicitly accepted the extra CI cycle. Amend and push directly, then
     re-run Phase 2b (the head SHA changed):
     ```bash
     COMMON_DIR=$(git rev-parse --git-common-dir 2>/dev/null)
     case "$COMMON_DIR" in /*) PRIMARY_ROOT="${COMMON_DIR%/.git}" ;; *) PRIMARY_ROOT="$(git rev-parse --show-toplevel)" ;; esac
     for p in .claude/agent-memory/ AGENTS.md README.md docs/ .claude/rules/; do
       [ -n "$(git ls-files -- "$p")" ] && git add -u -- "$p"
       { [ -e "$p" ] && ! git check-ignore -q "$p"; } && git add -A -- "$p"
     done
     if ! git diff --cached --quiet; then
       git commit --amend --no-edit
       git push --force-with-lease && echo 1 > "$PRIMARY_ROOT/.claude/agent-control/integrate-pushed"
     fi
     ```

### Phase 4: Wait for CI

**Fast path (skip the wait entirely).** This is the expected path. Nothing was
pushed unless Phase 2 had to update the branch, so the PR head SHA is unchanged
and any CI that already ran is still valid. When the push-tracking state file
still reads `0`, confirm the existing checks are green and proceed straight to
Phase 5 without polling:

```bash
COMMON_DIR=$(git rev-parse --git-common-dir 2>/dev/null)
case "$COMMON_DIR" in /*) PRIMARY_ROOT="${COMMON_DIR%/.git}" ;; *) PRIMARY_ROOT="$(git rev-parse --show-toplevel)" ;; esac
# Default to 0 if the state file is missing (defensive: never assume a push).
PUSHED=$(cat "$PRIMARY_ROOT/.claude/agent-control/integrate-pushed" 2>/dev/null || echo 0)
if [ "$PUSHED" -eq 0 ]; then
  # `gh pr checks` exits non-zero (exit 8) while checks are pending but still
  # writes valid JSON, so never append a fallback document with `||`: keep the
  # real output and treat empty or invalid JSON as non-green.
  CHECKS_JSON=$(gh pr checks <PR_NUMBER> --json name,bucket 2>/dev/null)
  TOTAL=$(echo "$CHECKS_JSON" | jq 'length' 2>/dev/null || echo 0)
  PENDING=$(echo "$CHECKS_JSON" | jq '[.[] | select(.bucket == "pending")] | length' 2>/dev/null || echo 1)
  FAILED=$(echo "$CHECKS_JSON" | jq '[.[] | select(.bucket == "fail" or .bucket == "cancel")] | length' 2>/dev/null || echo 1)
  # Require at least one check: zero checks is not a green fast path, poll instead.
  if [ "${TOTAL:-0}" -gt 0 ] && [ "${PENDING:-1}" -eq 0 ] && [ "${FAILED:-1}" -eq 0 ]; then
    echo "No re-push: existing CI is complete and green. Skipping the poll."
  fi
fi
```

If it printed "Skipping the poll", go to Phase 5. Otherwise (a push happened,
or checks are still pending, failed or absent), fall through to the poll below.
The poll includes external review bots (e.g., CodeRabbit) that appear as
pending StatusContext checks. Requires gh CLI v2.32+ (for the `bucket` field).

**Run the poll as a synchronous foreground command and wait for it to finish.** Do NOT launch it as a background task and then stop to "wait" for a notification: headless mode has no mechanism to resume you when a background task completes, and ending your turn without a tool call terminates the run, leaving the PR unmerged. One call polls at most 16 x 30s (8 minutes), which exceeds the Bash tool's 2 minute default timeout, so pass an explicit `timeout` of at least 540000 ms (never above the tool's 600000 ms cap). If more waiting is needed, repeat the same block as a new, separate call rather than lengthening one loop past the cap.

```bash
# bucket (not state) normalizes raw states into: pass, fail, pending, skipping, cancel.
# Grace period: the first 5 iterations tolerate TOTAL=0 while checks register after a push.
for i in $(seq 1 16); do
  echo "CI poll attempt $i/16"
  CHECKS_JSON=$(gh pr checks <PR_NUMBER> --json name,bucket 2>/dev/null)
  [ -n "$CHECKS_JSON" ] || CHECKS_JSON="[]"
  echo "$CHECKS_JSON" | jq -r '.[] | "\(.bucket)\t\(.name)"'
  STATS=$(echo "$CHECKS_JSON" | jq -r '
    (length | tostring) + "\t" +
    ([.[] | select(.bucket == "pending")] | length | tostring) + "\t" +
    ([.[] | select(.bucket == "fail" or .bucket == "cancel")] | length | tostring)
  ' 2>/dev/null) || { echo "Failed to parse CI check status"; break; }
  IFS=$'\t' read -r TOTAL PENDING FAILED <<< "$STATS"
  if [ "$TOTAL" -eq 0 ]; then
    if [ "$i" -lt 5 ]; then
      echo "No checks registered yet (grace period $i/5)"
      sleep 30
      continue
    fi
    echo "NO_CHECKS: no CI checks configured (grace period expired)"
    break
  fi
  if [ "$PENDING" -eq 0 ]; then
    if [ "$FAILED" -gt 0 ]; then echo "CI FAILED: $FAILED check(s) failed"; else echo "CI PASSED: all $TOTAL checks passed"; fi
    break
  fi
  if [ "$i" -eq 16 ]; then
    echo "CI TIMEOUT: checks still pending after 8 minutes"
    break
  fi
  sleep 30
done
```

Act on the result:
- **CI PASSED**: also verify that no blocking `CHANGES_REQUESTED` review remains (`gh pr view <PR_NUMBER> --json reviewDecision`; `gh pr checks` monitors CI status only, not review decisions).
  - If `reviewDecision` is `CHANGES_REQUESTED`: stop. Never merge while a review (bot or human, including CodeRabbit) is requesting changes; treat it as a real reviewer with its own address-review cycle. Write a handoff pointing at `/address-pr <PR_NUMBER>` and report that integration is blocked on unaddressed review feedback.
  - Otherwise, proceed to Phase 5.
- **NO_CHECKS** (no CI checks configured): proceed to Phase 5. No checks means nothing to wait for.
- **CI FAILED**: analyze the failure output briefly.
  - For infrastructure/flaky issues (network timeouts, resource limits, unrelated tests), post a retry comment and re-run the polling block once more. On second failure, stop and report the diagnosis.
  - For real code issues, stop and report the diagnosis with failing check details.
- **CI TIMEOUT**: re-run the polling block once more as a separate call. If it times out again, stop and report. The user can re-run `/integrate-pr` after CI completes.

### Phase 5: Merge

Resolve the merge settings through the config loader (they are not in the `sova config` table):

```bash
python3 -c "from pathlib import Path; from sova.config.loader import load_config; \
i=load_config(Path('.')).integration; print(i.merge_method, i.delete_branch, i.post_merge_state)"
```

- `merge_method`: "auto" (repo default), "squash", "rebase", or "merge"
- `delete_branch`: true/false (default true)
- `merge_queue_enabled`: "auto" (detect via GraphQL), "true", "false"
- `post_merge_state`: "done" (close issue) or "on_qa" (add label, keep open)

**Merge queue detection**: using the base branch from Phase 1, query the GraphQL API to check if a merge queue is configured.

If merge queue is detected:
- Omit merge strategy flags (queue controls strategy)
- Omit `--delete-branch` (handled after queue processing)
- Run: `gh pr merge <PR_NUMBER>`
- If enqueued, write a merge queue marker file in the primary checkout so the dashboard can track the PR:
  ```bash
  COMMON_DIR=$(git rev-parse --git-common-dir 2>/dev/null)
  case "$COMMON_DIR" in /*) PRIMARY_ROOT="${COMMON_DIR%/.git}" ;; *) PRIMARY_ROOT="$(git rev-parse --show-toplevel)" ;; esac
  mkdir -p "$PRIMARY_ROOT/.claude/agent-control"
  python3 -c "import json; print(json.dumps({'pr_number': <PR_NUMBER>, 'repo': '<OWNER/REPO>', 'issue_number': '<ISSUE_NUMBER>', 'branch_name': '<HEAD_BRANCH>'}))" > "$PRIMARY_ROOT/.claude/agent-control/merge-queue-<PR_NUMBER>.json"
  ```
- Then poll merge queue status via GraphQL every `merge_queue_poll_interval` seconds (default 30), under the same foreground and timeout rules as the Phase 4 poll
- On MERGED: proceed to Phase 6. If `delete_branch = true`, delete remote branch via GitHub API
- On UNMERGEABLE: report ejection and stop
- On TIMEOUT: report the PR is still enqueued, stop

If merge queue is NOT detected:

```bash
gh pr merge <PR_NUMBER> [--squash|--rebase|--merge] [--delete-branch] [--match-head-commit <GATED_SHA>]
```

If `merge_method` is "auto", omit strategy flags to use the GitHub repo default. Otherwise use the configured method. Only include `--delete-branch` when `delete_branch = true`. When Phase 2b printed `GATE_PASS <sha>`, pass `--match-head-commit <sha>` so GitHub refuses the merge if the head moved after the gate ran; on that refusal, re-run `/integrate-pr <PR_NUMBER>` from Phase 2.

**Stop if merge fails**: report the error (usually merge conflicts, branch protection, or required reviews). Recover by cause:
- **"the base branch policy prohibits the merge"**: a repository ruleset blocks CLI merges outright, and every merge method fails the same way even when the PR is approved and green. Do not retry with a different method. Ask the user to merge with the GitHub UI button, then re-run `/integrate-pr <PR_NUMBER>`; Phase 1 sees the PR as `MERGED` and continues with cleanup.
- **Head changed (`--match-head-commit` refusal)**: re-run `/integrate-pr <PR_NUMBER>` from Phase 2, as above. Do not send the user to the UI: the new head has not passed the gate.
- **Conflicts, pending required reviews or checks**: report what blocks the merge; it must be resolved before any merge path, CLI or UI, can succeed.

### Phase 6: Post-Merge Cleanup (incorporates `/after-merge`)

Return to the primary checkout first: the Update subroutine may have `cd`'d into a per-issue worktree, where `git checkout <BASE_BRANCH>` fails (git refuses to check out a branch that is checked out elsewhere).

```bash
COMMON_DIR=$(git rev-parse --git-common-dir 2>/dev/null)
case "$COMMON_DIR" in
  /*) cd "${COMMON_DIR%/.git}" ;;   # linked worktree: return to primary checkout
  *)  ;;                            # already primary
esac

git checkout <BASE_BRANCH>
git pull origin <BASE_BRANCH>
```

**Post-merge silent-revert audit (non-skippable backstop).** Phase 2b gates every merge this command performs itself, but a PR can also arrive here already `MERGED` (Phase 1's skip-ahead path) where Phase 2b never ran. This audit runs the guard against what the merge actually landed, resolved from GitHub's own merge record rather than local pre/post-pull tips (which break when local state was stale before an out-of-band merge), and derived from the landed commit's shape:

- **Merge commit** (two parents): guards skip merge commits, and a conflict resolution lives only in one, so the merge's first-parent diff is wrapped in a single commit (titled with the PR title plus the PR's `silent-revert-ok:` lines, dated as the merge) and judged as one, exactly as Phase 2b judges a merged result.
- **One PR commit**: whether rebased or squashed, exactly one commit landed.
- **Rebase merge**: the landed window's subjects match the PR's commits in order.
- **Squash merge**: the landed subject ends with `(#<PR_NUMBER>)`, it has one parent, and none of the PR's commit subjects appear among the commits just before it.
- **Anything else** (a rebase merge that dropped an empty commit or linearized one of the PR's merge commits lands fewer commits than the PR lists, which shifts the window): the wider `MERGE_SHA~COUNT..MERGE_SHA` window, with a note. Over-auditing is safe; a silently narrowed audit is not.

```bash
(
  PR=<PR_NUMBER>
  GUARD_PATH="${SILENT_REVERT_GUARD:-invariants/silent-revert-guard.sh}"
  BASE=$(gh pr view "$PR" --json baseRefName --jq .baseRefName)
  git fetch origin "$BASE" || { echo "AUDIT_UNRESOLVED: fetch of origin/$BASE failed; verify by hand before cleanup"; exit 1; }
  if ! git show "origin/$BASE:$GUARD_PATH" 2>/dev/null | grep -q SILENT_REVERT_RANGE; then
    echo "AUDIT_SKIPPED: no silent-revert guard honouring SILENT_REVERT_RANGE at origin/$BASE:$GUARD_PATH"
    exit 0
  fi
  MERGE_SHA=$(gh pr view "$PR" --json mergeCommit --jq '.mergeCommit.oid // empty')
  COUNT=$(gh pr view "$PR" --json commits --jq '.commits | length')
  if [ -z "$MERGE_SHA" ] || [ "${COUNT:-0}" -lt 1 ] || ! git cat-file -e "${MERGE_SHA}^{commit}" 2>/dev/null; then
    echo "AUDIT_UNRESOLVED: cannot resolve merge SHA/commit count; verify by hand before cleanup"
    exit 1
  fi
  PARENTS=$(( $(git rev-list --parents -n 1 "$MERGE_SHA" | wc -w) - 1 ))

  if [ "$PARENTS" -ge 2 ]; then
    TITLE=$(gh pr view "$PR" --json title --jq .title)
    [ -n "$TITLE" ] || { echo "AUDIT_UNRESOLVED: cannot read the PR title; verify by hand before cleanup"; exit 1; }
    EXEMPT=$(git log --format=%B "${MERGE_SHA}^1..${MERGE_SHA}^2" | grep -i '^[[:space:]]*silent-revert-ok:')
    if [ -n "$EXEMPT" ]; then set -- -m "$TITLE" -m "$EXEMPT"; else set -- -m "$TITLE"; fi
    MERGE_DATE=$(git log -1 --format=%cI "$MERGE_SHA")
    AUDIT_COMMIT=$(GIT_AUTHOR_NAME=integrate-pr GIT_AUTHOR_EMAIL=integrate-pr@localhost GIT_AUTHOR_DATE="$MERGE_DATE" \
      GIT_COMMITTER_NAME=integrate-pr GIT_COMMITTER_EMAIL=integrate-pr@localhost GIT_COMMITTER_DATE="$MERGE_DATE" \
      git commit-tree "${MERGE_SHA}^{tree}" -p "${MERGE_SHA}^1" "$@") \
      || { echo "AUDIT_UNRESOLVED: cannot build the first-parent audit commit; verify by hand before cleanup"; exit 1; }
    RANGE="${MERGE_SHA}^1..${AUDIT_COMMIT}"; HOW="merge commit, judged as its first-parent diff"
  elif [ "$COUNT" -eq 1 ]; then
    RANGE="${MERGE_SHA}~1..${MERGE_SHA}"; HOW="single-commit PR"
  else
    if ! git rev-parse -q --verify "${MERGE_SHA}~${COUNT}" >/dev/null; then
      echo "AUDIT_UNRESOLVED: ${MERGE_SHA}~${COUNT} does not exist; verify by hand before cleanup"
      exit 1
    fi
    # Compare the first 60 characters of each subject only.
    PR_SUBJECTS=$(gh pr view "$PR" --json commits --jq '.commits[].messageHeadline' | cut -c1-60)
    LANDED=$(git log --reverse --format=%s "${MERGE_SHA}~${COUNT}..${MERGE_SHA}" | cut -c1-60)
    PRIOR=$(git log --format=%s -n "$COUNT" "${MERGE_SHA}~1" | cut -c1-60)
    PR_COMMIT_ON_BASE=0
    while IFS= read -r SUBJECT; do
      [ -n "$SUBJECT" ] && printf '%s\n' "$PRIOR" | grep -qFx -- "$SUBJECT" && PR_COMMIT_ON_BASE=1
    done <<< "$PR_SUBJECTS"
    case "$(git log -1 --format=%s "$MERGE_SHA")" in *"(#$PR)") PR_SUFFIX=1 ;; *) PR_SUFFIX=0 ;; esac
    if [ "$PR_SUBJECTS" = "$LANDED" ]; then
      RANGE="${MERGE_SHA}~${COUNT}..${MERGE_SHA}"; HOW="rebase merge"
    elif [ "$PR_SUFFIX" -eq 1 ] && [ "$PR_COMMIT_ON_BASE" -eq 0 ]; then
      RANGE="${MERGE_SHA}~1..${MERGE_SHA}"; HOW="squash merge"
    else
      RANGE="${MERGE_SHA}~${COUNT}..${MERGE_SHA}"; HOW="unclassified landing, widest window"
      echo "NOTE: cannot tell how PR #$PR's $COUNT commit(s) landed; auditing the whole $COUNT-commit window, which may include other PRs' commits. Judge any flag on a commit that is not this PR's on its own merits."
    fi
  fi
  echo "Auditing $RANGE ($HOW, $(git rev-list --count "$RANGE") commit(s))"

  GUARD=$(mktemp)
  trap 'rm -f "$GUARD"' EXIT
  git show "origin/$BASE:$GUARD_PATH" > "$GUARD"
  if SILENT_REVERT_RANGE="0000000000000000000000000000000000000000..HEAD" bash "$GUARD" . "$BASE" >/dev/null 2>&1; then
    echo "GUARD_NOT_LIVE: the guard exited 0 on an unresolvable range"
    exit 3
  fi
  if SILENT_REVERT_RANGE="$RANGE" bash "$GUARD" . "$BASE"; then echo "AUDIT_PASS"; else echo "AUDIT_FAIL"; exit 4; fi
)
```

**Unless it printed `AUDIT_PASS` or `AUDIT_SKIPPED`, stop here.** Do not delete branches, remove worktrees or close the issue: they may be needed to investigate. For `AUDIT_UNRESOLVED` and `GUARD_NOT_LIVE`, report that the merge was not audited and must be checked by hand. For `AUDIT_FAIL`, never auto-revert (picking a last known-good SHA per flagged file is a judgment call): report the guard's output verbatim and hand the operator the restore recipe for each flagged file, to be applied on a fresh branch and PR:

```bash
git log --oneline -- <file>            # find the last known-good commit
git show <good-sha>:<file> > <file>    # restore its content
```

**If the audit passed or was skipped**, continue. Remove worktrees for this PR or issue first, since git refuses to delete a branch a worktree still has checked out. Do not pass `--force`: if a worktree has uncommitted changes, leave it in place and report it rather than discarding someone's work.

```bash
git worktree list
git worktree remove <WORKTREE_PATH>
git worktree prune
```

Then delete the merged branch, locally and on the remote:

```bash
BASE=<BASE_BRANCH>
# Local branch, if it still exists. After a rebase or squash merge, `-d`
# refuses with "not fully merged" even though the content landed.
if git show-ref --verify --quiet "refs/heads/<HEAD_BRANCH>"; then
  git branch -d <HEAD_BRANCH> || git diff --stat "origin/$BASE" <HEAD_BRANCH>
fi

# Delete remote branch if delete_branch = true (fallback: Phase 5 may have
# already handled this via --delete-branch, but covers merge-queue path and
# already-merged PRs where Phase 5 is skipped). Skip for a fork PR: HEAD_BRANCH
# names a branch, not a repository, and `origin` is the base repository, so
# deleting <HEAD_BRANCH> there could remove an unrelated branch that happens
# to share the fork contributor's branch name.
IS_FORK=$(gh pr view <PR_NUMBER> --json isCrossRepository --jq '.isCrossRepository')
if [ "$IS_FORK" != "true" ]; then
  if git ls-remote --exit-code --heads origin <HEAD_BRANCH> >/dev/null; then
    git push origin --delete <HEAD_BRANCH>
  fi
fi
```

If `-d` refuses ("not fully merged", expected after a rebase merge), confirm the diff against the base is empty (`BASE=$(gh pr view <PR_NUMBER> --json baseRefName --jq .baseRefName); git diff "origin/$BASE" <branch-name>`), then `git branch -D <branch-name>`. If the diff is non-empty only because the base branch moved on after the merge, compare against the merge commit instead (`git diff <MERGE_SHA> <branch-name>`); if that is not empty either, keep the branch and report it.

Handle the linked issue based on `post_merge_state` from `[integration]` config.

**GitHub projects** (`task_source` is `github`, the default):

- **"done"** (default): close the issue (`gh issue close <ISSUE_NUMBER>`)
- **"on_qa"**: add `agent:on-qa` label, keep the issue open
- **Other value**: log a warning and skip the state transition

**Jira projects** (`task_source.type = "jira"`):

The Jira connection settings resolve through the config loader as `cfg.task_source.jira_base_url`, `jira_email`, `jira_api_token`, and `jira_project_key`. Read them inside the script that calls Jira; never print `jira_api_token` to the terminal or into a log. Use the Jira REST API to transition the issue:

- **"done"**: trigger a Jira workflow transition matching "Done", "Closed", "Resolved", or "Close"
- **"on_qa"**: trigger a Jira workflow transition matching "On QA", "QA", "Verification", or "Ready for QA". Also add the `agent:on-qa` label.
- Check `cfg.task_source.jira_state_transitions` for custom transition name overrides (e.g., `on_qa = "Move to QA"` takes priority over the defaults)
- If no matching transition is available on the Jira board, log a warning and skip

Check for stale stashes that belong to the merged branch:

```bash
git stash list
```

If any stash entries reference the merged branch name, report them to the user (do not drop without confirmation).

Run the full issue-aware GC to clean up any remaining stale worktrees and branches across the project:

```bash
sova cleanup --all --project <PROJECT_DIR>
```

`sova cleanup --all` only removes a worktree once its own issue is confirmed closed on GitHub, no agent is actively using it, and its working tree is clean, so it will not touch other issues' in-progress work.

### Phase 7: Report

Note: this command makes no post-merge commits. Documentation and knowledge are captured by `/address-pr` on the branch; anything Phase 3 found missing was queued for the next branch rather than pushed.

Output a concise summary covering:

- PR number, title, and base branch it was merged into
- Whether a rebase was needed, and if so which conflicts were resolved
- Whether anything was pushed, and why (a push means a CI cycle was spent, so it must be accounted for)
- Silent-revert checks: the Phase 2b and Phase 6 results (pass, or skipped because no guard honouring an explicit range exists at the path checked), which replay Phase 2b used (rebase, merged, or rebase-fallback), and how Phase 6 classified the landing, including any wide-window note
- CI status (passed, retried, or skipped because nothing was re-pushed)
- Branches cleaned up (local + remote), and any worktree or branch kept and why
- Issue closed (or no linked issue)
- Documentation and knowledge: "already captured" or the items queued to `.claude/agent-control/pending-docs.md` for the next branch
- Stale stashes found (if any)
- **Manual post-merge actions** (omit if none apply), derived from `gh pr diff <PR_NUMBER> --name-only`:
  - service or unit files changed (systemd units, launchd plists, cron entries): they need install, reload and restart on the host, because the running copy is not the repo copy
  - services deployed outside the main pipeline: they need a manual rebuild and redeploy
  - new keys in env templates (`*.env.example` and similar): the host env files need updating
  - for any infrastructure change, the check that verifies the outcome on the host itself, since a merged PR is not proof the outcome is live

## Error Recovery

When the pipeline stops at any phase, report clearly:

- Which phase failed
- The specific error
- What to do next (resolve conflicts, fix CI, retry, etc.)

The user can fix the issue and re-run `/integrate-pr <PR_NUMBER>` to resume. The command is idempotent: it detects the current state and picks up from where it left off (e.g., if already rebased, it skips to CI; if already merged, it skips to cleanup and the post-merge audit).

## Cross-References

- **Replaces**: `/after-merge` (cleanup), built into Phase 6
- **Knowledge capture happens upstream**: `/address-pr` folds review learnings and documentation into the branch's own commits. This command only verifies and, if something was missed, queues it for the next branch.
- **Before this**: `/review-full` or `/address-pr` to prepare the PR
- **Next**: `/find-task` or `/standup` to pick up the next task

## Rules

- Never stop between phases unless there is a hard failure
- **Never push unless GitHub refuses to merge the PR as it stands.** The only justification is `mergeable: CONFLICTING` (or `BEHIND` under a strict status-check policy). Documentation and doc-count drift are never a reason to push from this command: queue them for the next branch instead.
- **Never skip Phase 2b or the Phase 6 audit when the project has a guard.** Both are hard-blocking with no override flag, even under `--with-docs`. A guard that passes the negative control is not live: stop.
- Never background a poll loop and stop to wait for a notification (see the Phase 4 note on foreground calls and timeouts)
- Use the merge method from `[integration]` config (default: auto, uses GitHub repo default)
- Handle merge queue when detected or configured
- Use `--force-with-lease` for pushes, never `--force`
- Never swallow a failed branch or worktree deletion with `|| true`: report what was kept and why
- Only record actionable, specific lessons in memory, not generic advice
- Do not duplicate existing memory entries
- NEVER use emojis in any output
