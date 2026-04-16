---
name: rearrange-commits
description: Reorganize current branch commits into small, logical, well-documented steps.
user-invocable: true
---

# Rearrange Commits

Reorganize all commits on the current branch into clean, logical, atomic commits.

## Instructions

### Step 1: Analyze Current State

```bash
git branch --show-current
git log origin/main..HEAD --oneline
git diff origin/main...HEAD --stat
git status
```

### Step 2: Preserve All Changes

1. Stash uncommitted changes (if any):
   ```bash
   git stash push -m "Pre-rearrange stash"
   ```
2. Soft reset all commits back to origin/main:
   ```bash
   git reset --soft origin/main
   ```
3. Pop stashed changes (if any):
   ```bash
   git stash pop
   ```

### Step 3: Create Small, Logical Commits

Group changes by logical unit and commit in order:

1. Core infrastructure changes first (CLI, config)
2. Agent/orchestrator changes second
3. Commands/personas/knowledge third
4. Dashboard changes fourth
5. Documentation/misc last

### Commit Message Format

```
type(scope): short description

Detailed explanation of WHAT, WHY, and HOW.
```

Types: feat, fix, refactor, test, docs, chore, perf
Scopes: agent, dashboard, commands, personas, invariants, knowledge, cli, docs

### Step 4: Verify

```bash
git log origin/main..HEAD --oneline
git diff origin/main...HEAD --stat
```

Ensure:
- All changes are committed (nothing left uncommitted)
- Each commit is atomic and self-contained
- Commits are in logical order
- The total diff matches what it was before reorganization

## Cross-References

- **Before PR**: Run `/review` after rearranging, then `/pr`

## Rules

- Each commit = ONE logical change
- Earlier commits should not depend on later ones
- Keep commits small -- easier to review and revert
- NEVER use emojis in any output
