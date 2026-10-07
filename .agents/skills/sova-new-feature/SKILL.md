---
name: sova-new-feature
description: "Set up a new feature branch and prepare for development."
---

# Start New Feature

Set up a new feature branch and prepare for development.

**Feature name/description**: the arguments provided when this skill is invoked

## Workflow

### 1. Ensure Clean Starting Point
- Verify we are on `main`: `git branch --show-current`
- Ensure working tree is clean: `git status`
- Pull latest: `git pull origin main`

### 2. Create Feature Branch
- Derive a branch name from the description (lowercase, hyphenated)
  - Features: `feat/<name>` (e.g., `feat/income-crud`)
  - Fixes: `fix/<name>` (e.g., `fix/currency-conversion`)
  - Refactors: `refactor/<name>` (e.g., `refactor/auth-flow`)
- Create and switch: `git checkout -b <branch-name>`

### 3. Review Context
- Read CLAUDE.md and AGENTS.md for project conventions
- Read agent memory files if they exist (`.claude/agent-memory/`)
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
