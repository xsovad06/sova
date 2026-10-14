---
name: sova-new-feature
description: "Set up a new feature branch and prepare for development."
---

# Start New Feature

Set up a new feature branch and prepare for development.

**Feature name/description**: the arguments provided when this skill is invoked

## Workflow

### 1. Ensure Clean Starting Point
- Resolve the default branch: `BASE=$(gh repo view --json defaultBranchRef --jq .defaultBranchRef.name)`
- Fetch it: `git fetch origin "$BASE"`
- Ensure the current working tree is clean: `git status`

### 2. Create Feature Branch
- Derive a branch name from the description (lowercase, hyphenated)
  - Features: `feat/<name>` (e.g., `feat/income-crud`)
  - Fixes: `fix/<name>` (e.g., `fix/currency-conversion`)
  - Refactors: `refactor/<name>` (e.g., `refactor/auth-flow`)
- **If the project keeps active branches in worktrees** (`.claude/worktrees/` exists, or its rules say so), create the branch in a new worktree so the primary checkout stays on the base branch, then do all further work from that directory:
  ```bash
  BASE=$(gh repo view --json defaultBranchRef --jq .defaultBranchRef.name)
  git worktree add ".claude/worktrees/<issue-or-topic>" -b "<branch-name>" "origin/$BASE"
  ```
- **Otherwise**, from a checkout on the base branch: `git pull origin "$BASE"`, then `git checkout -b <branch-name>`

### 3. Review Context
- Read CLAUDE.md and AGENTS.md for project conventions
- Read agent memory files if they exist (`.claude/agent-memory/`; for a split cookbook in `.claude/agent-memory/cookbook/`, read its `README.md` index and only the relevant domain files)
- Identify which existing modules/files will be affected

### 4. Plan
- Break the feature into concrete implementation tasks
- Create a task checklist with the tasks
- Identify any dependencies or prerequisites

### 5. Report
Tell the user:
- Branch name created
- High-level implementation plan
- Which files/modules will be affected
- Any questions or ambiguities to resolve before starting
- Suggest running the `sova-develop` skill <description> to begin implementation

## Cross-References

- **Ready to implement?** Run the `sova-develop` skill <description> or the `sova-develop-full` skill <description>
- **Need to understand approaches first?** Run the `sova-develop-explain` skill <description>

## Rules

- NEVER use emojis in any output
