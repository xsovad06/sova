# Development Workflow

## Finding the Next Task

When the user asks to "start the next task", "what should we work on", or similar:

1. **Check the SOVA Roadmap project board** (project #2) for priority order:
   ```bash
   gh api graphql -f query='query { user(login:"xsovad06") { projectV2(number:2) { items(first:30) { nodes { content { ... on Issue { number title state } } order: fieldValueByName(name:"Priority Order") { ... on ProjectV2ItemFieldNumberValue { number } } phase: fieldValueByName(name:"Phase") { ... on ProjectV2ItemFieldSingleSelectValue { name } } } } } } }' --jq '.data.user.projectV2.items.nodes | sort_by(.order.number) | .[] | select(.content.state == "OPEN") | "\(.order.number)) #\(.content.number) [\(.phase.name)] \(.content.title)"'
   ```

2. **The lowest Priority Order number** with state OPEN is the next task to tackle.

3. **Check dependencies**: read the issue body for "Dependencies" section. If a dependency is still open, skip to the next issue.

4. **Before starting work**: read `.claude/rules/architecture.md` for architectural context.

## Git Safety Before Commits

- **Verify branch identity before committing or resetting**: always check `git branch --show-current` before committing or running `git reset --soft`. If a feature branch was already merged and you're on main, commits land on main and `reset --soft` detaches from `origin/main`. Run `git log main..HEAD --oneline` to confirm you're ahead of main on the intended branch. Fix: create a branch at HEAD, reset main back, switch to the new branch.

## Issue State Management

SOVA agents own issue state on the tracker. When working on an issue:
- **Starting**: assign yourself, move to "In Progress" on the project board
- **PR created**: move to "In Review"
- **Completed**: move to "Done", close the issue
- **Blocked**: post a comment explaining the blocker, do NOT close

