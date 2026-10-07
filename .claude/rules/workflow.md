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

- **Do non-trivial work in a dedicated worktree, not the primary checkout.** The primary checkout should stay on `main`; every other active branch in this repo already lives under `.claude/worktrees/<issue-number>` (`git worktree list` shows the current set). An interactive session is not exempt just because no autonomous SOVA agent spawned it: on 2026-10-02 a full feature (5 commits, PR #1116) was developed and pushed entirely from the primary checkout before the user noticed and asked for it to be relocated. Before starting feature work: create the branch if it doesn't exist, then `git worktree add .claude/worktrees/<issue-or-topic> <branch>` and do all editing from that directory. If work already started in the primary checkout and the branch is pushed, it relocates losslessly: `git checkout main` in the primary checkout (frees the branch), then `git worktree add .claude/worktrees/<id> <branch>` to attach it elsewhere.

## Git Safety Before Commits

- **Verify branch identity before committing or resetting**: always check `git branch --show-current` before committing or running `git reset --soft`. If a feature branch was already merged and you're on main, commits land on main and `reset --soft` detaches from `origin/main`. Run `git log main..HEAD --oneline` to confirm you're ahead of main on the intended branch. Fix: create a branch at HEAD, reset main back, switch to the new branch.

## Rebase Conflict Resolution

- **Module split conflicts: take the refactored facade, preserve functional changes** -- when a PR branch predates a module split on main (e.g., `control_service.py` split into `agent_lifecycle.py` + `agent_output.py`), take main's version of the re-export facade (`git checkout HEAD -- file`). Then verify that the PR's functional changes (new functions, modified logic) are present in the correct submodule. If not, cherry-pick the functional changes into the submodule. Never take the incoming side's monolithic version -- it lacks the split structure that downstream code depends on.
- **ABC signature conflicts: pick main's interface, adapt feature's implementations** -- when both branches modified the same ABC (e.g., `LLMProvider` with different method signatures), take main's ABC as the authority. Then adapt the feature branch's implementations (new provider classes, tests) to match main's signature. The LLM auto-rebase fails on these because it resolves files independently without enforcing cross-file interface consistency.

## Command Maintenance

- **Mirror changes across SOVA/distributable command pairs**: when a command exists in both `.claude/commands/` (SOVA-specific) and `commands/` (distributable), changes to shared sections must be applied to both files. CodeRabbit only reviews `commands/` (`.claude/` is excluded via path filters), so inconsistencies in the SOVA variant go undetected. After editing one, always diff the pair.
- **`.agents/skills/sova-*/SKILL.md` is a rendered artifact of `commands/*.md` and `skills/*/SKILL.md`, exactly like `.claude/commands/`**: it is produced by `make skills-render` (`sova/commands/skill_render.py`) and guarded by `tests/test_skill_render_drift.py`, so a hand edit there fails CI rather than vanishing silently, but it still must never be the place a change is authored. Edit the canonical `commands/` or `skills/` file and re-render. Unprefixed directories in that tree (`testing-patterns`, `database-patterns`, `dashboard-design`, `visual-audit`) are deliberately NOT rendered artifacts: they are hand-authored and the renderer never touches them.
- **A `.claude/commands/` file edited directly (bypassing `commands/`) is silently reverted by the next sync**: every entry in `.claude/commands/.sova-manifest.json` marked `"managed": true` is treated as a rendered artifact of the matching `commands/*.md` template. If `.claude/commands/health-audit.md` or `.claude/commands/pr.md` gains functionality (a staleness check, a post-push self-review loop) that is never back-ported into `commands/health-audit.md`/`commands/pr.md`, the two drift silently: CodeRabbit's path filters mean nothing catches it, and the file looks correct for months. The next `sova commands sync` / `make marketplace` run then re-renders the SOVA copy from the stale canonical template, discarding the never-mirrored functionality with no error, no diff review, and no indication anything was lost: it reads as a routine sync, not a regression. Confirmed twice: a generic case flagged in PR #582 (dual-file pattern, `sova commands sync` overwrites SOVA customizations), and concretely on PR #1087 (issue #1028): an unrelated stdin-delivery PR's sync step silently reverted `.claude/commands/health-audit.md` by ~150 lines (losing the parallel 3-agent fan-out, staleness detection, and incremental-audit logic) and `.claude/commands/pr.md` by ~50 lines (losing the entire Post-Push CI+self-review section), both caught only by SOVA's own self-review since `.claude/**` is invisible to CodeRabbit. Fix in that instance: `git checkout origin/main -- <file>` plus restoring the matching `.sova-manifest.json` hash entries, since the changes were never part of the PR's actual scope. The durable fix is upstream of any single PR: any manual edit made directly to a `"managed": true` file under `.claude/commands/` must be ported into its `commands/` template in the same change, or it will eventually vanish without warning.

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

