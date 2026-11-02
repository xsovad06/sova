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

## Interactive Development Location

- **Do non-trivial work in a dedicated worktree, not the primary checkout.** The primary checkout should stay on `main`; every other active branch in this repo already lives under `.claude/worktrees/<issue-number>` (`git worktree list` shows the current set). An interactive session is not exempt (full entry: docs/workflow-history.md, "Interactive Development Location"). Before starting feature work: create the branch if it doesn't exist, then `git worktree add .claude/worktrees/<issue-or-topic> <branch>` and do all editing from that directory. If work already started in the primary checkout and the branch is pushed, it relocates losslessly: `git checkout main` in the primary checkout (frees the branch), then `git worktree add .claude/worktrees/<id> <branch>` to attach it elsewhere.

## Git Safety Before Commits

- **Verify branch identity before committing or resetting**: always check `git branch --show-current` before committing or running `git reset --soft`. If a feature branch was already merged and you're on main, commits land on main and `reset --soft` detaches from `origin/main`. Run `git log main..HEAD --oneline` to confirm you're ahead of main on the intended branch. Fix: create a branch at HEAD, reset main back, switch to the new branch.

## Rebase Conflict Resolution

- **Module split conflicts: take the refactored facade, preserve functional changes**: when a PR branch predates a module split on main (e.g., `control_service.py` split into `agent_lifecycle.py` + `agent_output.py`), take main's version of the re-export facade (`git checkout HEAD -- file`). Then verify that the PR's functional changes (new functions, modified logic) are present in the correct submodule. If not, cherry-pick the functional changes into the submodule. Never take the incoming side's monolithic version: it lacks the split structure that downstream code depends on.
- **ABC signature conflicts: pick main's interface, adapt feature's implementations**: when both branches modified the same ABC (e.g., `LLMProvider` with different method signatures), take main's ABC as the authority. Then adapt the feature branch's implementations (new provider classes, tests) to match main's signature. The LLM auto-rebase fails on these because it resolves files independently without enforcing cross-file interface consistency.

## Command Maintenance

- **Mirror changes across SOVA/distributable command pairs**: apply a shared-section change to both `.claude/commands/` and `sova/commands/data/commands/`, then diff the pair; CodeRabbit never reviews `.claude/` (full entry: docs/workflow-history.md, "Mirrored Command Pairs").
- **`.agents/skills/` is rendered, never hand-edited**: every `sova-<name>/SKILL.md`, and every bare `<name>/SKILL.md` matching a directory in `sova/commands/data/skills/`, comes from `make skills-render` (drift-tested); edit the canonical file and re-render. Only `testing-patterns`, `database-patterns`, `dashboard-design` and `visual-audit` are hand-authored (full entry: docs/workflow-history.md, "Rendered .agents/skills/").
- **`.claude/skills/` has no render step or manifest**: an entry with no SOVA-specific override stays byte-identical to its `sova/commands/data/skills/` counterpart (`TestClaudeSkillsNoDrift`); `issue-template` and `testing-patterns` are deliberate variants (full entry: docs/workflow-history.md, "Unrendered .claude/skills/").
- **Never hand-edit a managed `.claude/commands/` file**: an entry marked `"managed": true` in `.sova-manifest.json` is re-rendered by the next sync, silently discarding the edit; port the change into `sova/commands/data/commands/` in the same PR (full entry: docs/workflow-history.md, "Managed .claude/commands/ edits are reverted by sync").
- **Each fenced bash block in a markdown command runs in its own shell**: a variable or function set in one block is gone in the next. Every later block that uses a value re-derives it from durable inputs (PR number, `gh pr view`, `headRefOid`) or re-assigns it as its first line, as `sova/commands/data/commands/pr.md` and `sova/commands/data/commands/address-pr.md` do for `BASE` (full entry: docs/workflow-history.md, "Fenced Bash Blocks").

## Agent Memory Location

- **`.claude/agent-memory/` is local-only (gitignored since PR #1116), and lives in the primary checkout.** Worktrees get a copy, and that copy is deleted with the worktree, so a lesson written there is silently lost (it happened to a PR #1180 review lesson). Commands write cookbook entries to `$PRIMARY_ROOT/.claude/agent-memory/` (resolve it from `git rev-parse --git-common-dir`), never commit or `git add -f` that directory, and promote confirmed lessons into tracked Tier 1 files instead. Layout: `cookbook/README.md` (domain index), `cookbook/<domain>.md` (one bullet per lesson, `(PR #N; confirmed: N)`), and `common-mistakes.md` (`(occurrences: N)`, read by `/review` as a checklist against every diff). Agents read only the domain files relevant to their change.

## Push/PR Approval Precedence

The cross-project default is to never push or open a PR without explicit user approval. Slash commands that document their own push/PR steps (`/pr`, `/integrate-pr`, `/address-pr`, etc.) are self-approving for exactly those steps: invoking the command IS the approval, so they push without pausing to ask again. The cross-project default still applies to any push made outside of a command's documented steps (ad-hoc `git push`, or an action a command doesn't itself call for).

## External Reviews

CodeRabbit does NOT auto-review this repository. Auto-review unlocks at 10+ GitHub stars; below that the plan is manual-only at 1 review/hour. A PR therefore gets no CodeRabbit review at all unless someone posts `@coderabbitai review` on it, and the green "CodeRabbit" status check does not mean a review happened. Post the trigger comment after the code is pushed (never before: it re-reviews unfixed code), then treat the result as a real reviewer per the address-review cycle in architecture.md.

## Issue State Management

SOVA agents own issue state on the tracker. When working on an issue:
- **Starting**: assign yourself, move to "In Progress" on the project board
- **PR created**: move to "In Review"
- **Completed**: move to "Done", close the issue
- **Blocked**: post a comment explaining the blocker, do NOT close
- **Add a newly created issue to the board**: `gh project item-add 2 --owner xsovad06 --url <issue-url>`; on a `project` or `read:org` scope error, apply the fix in the token doc rather than leaving the issue off the board (full entry: docs/github-token-setup.md, "Project Board Scopes").

