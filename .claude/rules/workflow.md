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

## Rebase Conflict Resolution

- **Module split conflicts: take the refactored facade, preserve functional changes** -- when a PR branch predates a module split on main (e.g., `control_service.py` split into `agent_lifecycle.py` + `agent_output.py`), take main's version of the re-export facade (`git checkout HEAD -- file`). Then verify that the PR's functional changes (new functions, modified logic) are present in the correct submodule. If not, cherry-pick the functional changes into the submodule. Never take the incoming side's monolithic version -- it lacks the split structure that downstream code depends on.
- **ABC signature conflicts: pick main's interface, adapt feature's implementations** -- when both branches modified the same ABC (e.g., `LLMProvider` with different method signatures), take main's ABC as the authority. Then adapt the feature branch's implementations (new provider classes, tests) to match main's signature. The LLM auto-rebase fails on these because it resolves files independently without enforcing cross-file interface consistency.

## Command Maintenance

- **Mirror changes across SOVA/distributable command pairs**: when a command exists in both `.claude/commands/` (SOVA-specific) and `commands/` (distributable), changes to shared sections must be applied to both files. CodeRabbit only reviews `commands/` (`.claude/` is excluded via path filters), so inconsistencies in the SOVA variant go undetected. After editing one, always diff the pair.
- **`plugins/sova/commands/` is a third, deliberately divergent variant**: it packages a subset of `commands/` (develop, spec, review, pr, debug, test) as a standalone Claude Code plugin for the AI Helpers Marketplace, adapted to run without SOVA installed. It must not byte-match `commands/`: no `{{ template_vars }}`, no `sova.toml`/`sova install`/"SOVA pipeline" references (enforced by `tests/test_marketplace_plugin.py`), and its own frontmatter shape (`description`, `argument-hint`, required `## Name`/`## Synopsis`/`## Description`/`## Implementation` sections). When a `commands/*.md` behavioral change is significant enough to matter standalone, port the *intent* into the matching `plugins/sova/commands/*.md` file rather than the literal diff.

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

