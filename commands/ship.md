---
name: ship
description: Run prepush CI checks, push PR, wait for CI pipeline, self-review, address findings. Autonomous CI + review loop.
user-invocable: true
category: core
---

# Ship

Run local CI checks, push to PR, wait for CI, self-review the PR diff, address findings, and present for approval. Stops before merge -- use `/integrate-pr` to merge.

PR or context: $ARGUMENTS

## Instructions

### Phase 1: Verify Branch State

```bash
git branch --show-current
git status
git log origin/main..HEAD --oneline
```

- Ensure you're NOT on main/master. If on main, stop and suggest creating a feature branch first.
- If there are uncommitted changes, ask the user whether to commit them first.
- If there are no commits ahead of main and no uncommitted changes, stop -- nothing to ship.

### Phase 2: Run Local CI Checks (Prepush)

Run the same checks the CI pipeline will run. See CLAUDE.md for the exact commands.

Common patterns:
```bash
make check   # or: make ci, make lint && make test, npm run check
```

If any check fails:
1. Analyze the failure
2. Fix the code
3. Amend to the relevant commit: `git add <files> && git commit --amend --no-edit`
4. Re-run the failing check
5. Repeat until all checks pass (max 3 attempts per check)

If you cannot fix a failure after 3 attempts, stop and ask the user for guidance.

### Phase 2.5: Visual Verification (if applicable)

If the project has a `/verify-local` command AND changes affect UI (templates, CSS, JS, views -- not test-only or migration-only):

Follow the `/verify-local` procedure to run visual checks.

If verification reveals issues, fix them, re-run CI checks (Phase 2), and retry verification.

Skip this phase if no `/verify-local` command exists.

### Phase 3: Update Documentation

Follow the `/update-docs` workflow to ensure all documentation matches the current code state:

1. Update tracked docs -- these will be committed
2. Update local docs (CLAUDE.md files) -- these stay on disk for dev context
3. Commit any tracked doc changes as part of the branch before proceeding

### Phase 4: Push and Create/Update PR

Check if a PR already exists:
```bash
gh pr list --head $(git branch --show-current) --json number,url,state
```

**Rebase onto latest main first:**
```bash
git fetch origin
git rebase origin/main
```

If rebase has conflicts, stop and ask the user for help.

**If no PR exists:**
1. Push: `git push -u origin $(git branch --show-current)`
2. Create PR targeting `main`:
   - Concise title based on changes
   - Summary, changes, and testing sections in body
   - Link task if applicable
   - Assign to self: `gh pr edit <NUMBER> --add-assignee @me`
   - Use `--base main` when creating the PR

**If PR already exists:**
1. Force push: `git push --force-with-lease`
2. Update PR description if the commit structure changed significantly

Report the PR URL.

### Phase 5: Wait for CI Pipeline

Poll CI status until it completes:
```bash
gh run list --branch $(git branch --show-current) --limit 1 --json databaseId,status,conclusion
```

If `gh run watch` is available:
```bash
gh run watch <run_id> --exit-status
```

Otherwise poll every 30 seconds. Max wait: 15 minutes.

### Phase 6: Handle CI Result

**If CI fails:**
1. Fetch the failing check logs:
   ```bash
   gh run view <run_id> --log-failed
   ```
2. Analyze the root cause
3. Fix the code
4. Amend to the relevant commit(s)
5. Force push: `git push --force-with-lease`
6. Go back to Phase 5 (max 3 CI retry cycles total)

If CI fails 3 times, stop and ask the user for guidance.

**If CI is still pending after max wait:**
- Report current status and the PR URL
- Suggest the user check back later and stop here

**If CI passes:** continue to Phase 7.

### Phase 7: Self-Review the PR Diff

Run the `/review-pr` workflow against this PR to review the actual diff that will be merged:

1. Fetch the PR number from the branch
2. Execute the full `/review-pr` analysis (fetch diff, read files, deep analysis)
3. Post the review on GitHub
4. If the verdict has no findings >= 3/10: skip Phase 8, go to Phase 9
5. If there are findings >= 3/10: continue to Phase 8

### Phase 8: Address Review Findings

Run the `/address-pr` workflow to fix the findings:

1. Score and address each finding (fix or acknowledge)
2. Commit fixes
3. Reply to review comments on GitHub
4. Resolve threads
5. Force push: `git push --force-with-lease`
6. Wait for CI again (go back to Phase 5 logic, max 3 total CI cycles across the entire ship)

### Phase 9: Report

```
## Ship Summary

Branch: <branch>
PR: <url>
CI: passed (attempt N)
Review: approved / N findings addressed
Status: ready for /integrate-pr
```

## Cross-References

- **Before shipping**: `/review-full` for local simplify + review + commit organization
- **After this**: `/integrate-pr` to merge, cleanup, extract knowledge

## Rules

- NEVER merge the PR -- that happens via `/integrate-pr`
- Use `--force-with-lease` for force pushes, never `--force`
- NEVER skip CI checks or use `--no-verify`
- If CI fails 3 times, stop and ask the user for guidance
- Do NOT ask for or request reviewers -- the user handles reviews themselves
- NEVER include AI references in commits or PRs
- NEVER use emojis in any output
