---
name: find-task
description: Triage the Jira or GitHub ticket backlog into AUTO/INTERACTIVE/BLOCKED/CLOSE, print one compact table, and write back suitability labels.
user-invocable: true
category: management
inputs:
  - project_board
outputs:
  - suggested_issue
---

# Find Next Task

Triage this project's open tickets (Jira or GitHub, whichever `task_source.type` is configured) into AUTO, INTERACTIVE, BLOCKED or CLOSE, print one compact table, and write the matching `agent:` label back to the tracker.

**Scope**: $ARGUMENTS

## Instructions

### 1. Determine the Task Source

```bash
sova config get task_source.type
```

Read every other setting through `sova config get` as well (never assume a default): `task_source.jira_jql_filter`, `task_source.labels`, `task_source.milestone`, `triage.skip_title_prefixes`, `triage.skip_labels`.

### 2. Build the Ticket Pool

**Jira** (`task_source.type = "jira"`):

```bash
# Pickup states only, current user first, with <jira_jql_filter> from step 1:
jira issue list -q "status in (Backlog, \"To Do\", New, \"Selected for Development\") AND assignee = currentUser() AND (<jira_jql_filter>)"
jira issue list -q "status in (Backlog, \"To Do\", New, \"Selected for Development\") AND assignee is EMPTY AND (<jira_jql_filter>)"
```

**GitHub** (default, `task_source.type = "github"`):

```bash
gh issue list --assignee @me --state open --limit 50 --json number,title,labels,milestone,updatedAt,body
gh issue list --search "no:assignee" --state open --limit 30 --json number,title,labels,milestone,updatedAt,body
```

If `$ARGUMENTS` contains filters (label, milestone), incorporate them into the query; otherwise apply the configured `task_source.labels` and `task_source.milestone` as the defaults, which is the pool SOVA's own adapter works from.

Drop a clause whose setting is empty rather than interpolating nothing: an unset `task_source.jira_jql_filter` would leave `AND ()`, which Jira rejects as a syntax error, and an empty `--label ""` is not the same as no label filter.

Pool rules:
- Self-assigned tickets first, then unassigned tickets.
- Never list a ticket assigned to another individual. A ticket assigned to a team or group (not a named person) counts as unassigned, since it is still pickable.
- The unassigned half of the GitHub pool must use `--search "no:assignee"`. `gh issue list --assignee ""` is silently ignored and returns every open issue, including other people's.
- Only pickup states: Jira `Backlog`, `To Do`, `New`, `Selected for Development`; GitHub `open`. If the Jira project does not define one of those four statuses, drop it from the `status in (...)` clause: Jira rejects the whole query for an unknown status value rather than ignoring it.

### 3. Read Each Ticket in Full

For every ticket in the pool, read more than the title:

```bash
# Jira
jira issue view <KEY> --plain

# GitHub
gh issue view <NUMBER> --json number,title,body,labels,assignees,comments
```

Capture: description, reporter, linked/blocking issues, and the last 3 comments.

### 4. Run the Per-Ticket Checks

- **Blockers**: for each linked blocking ticket, its current status and whether its linked PR is merged.
- **Comments**: does the last 3 comments contain an unanswered question from the current user, or a lead saying wait/hold?
- **Code**: `grep` this repo for any file, symbol, or dependency version the ticket names, to detect work already on the base branch or stale references. A grep match is a signal toward CLOSE, not proof; when ambiguous, prefer INTERACTIVE/human confirmation over asserting CLOSE.
- **Repo match**: does the ticket name a repo other than this project's configured `github_repo`?

### 5. Classify Into One Mode

- **AUTO**: change confined to this repo, verifiable by this repo's own tests and lint, acceptance criteria present, no unanswered question, every blocker closed with its code already merged.
- **INTERACTIVE**: needs another repo (app-interface, a `*-actions` repo, IQE), needs a tool an autonomous agent cannot reach (bonfire or an ephemeral environment, stage or prod, `oc`, VPN, AWS, Splunk, quay), is an investigation or spike, or needs a human decision or conversation.
- **BLOCKED**: an open blocker, a lead's hold comment, or the current user's own unanswered question.
- **CLOSE**: the fix is already on the base branch.

### 6. Output Contract (and Nothing Else)

Print exactly this, in this order:

1. One table, columns `# | Key | Reporter | Repo | What | Status | Needs | Mode | Deps`, at most 12 rows, ordered: critical items blocking other teams first, then CLOSE, then AUTO by priority, then INTERACTIVE, then BLOCKED.
2. At most 3 one-line actions (for example "close RHCLOUD-123, PR merged" or "ping @lead on RHCLOUD-456").
3. One line: `Skipped: N` with counts by reason (epic, spike, held, other repo, ops).

Do not print anything else: no prose summary, no per-ticket narration outside the table.

### 7. Write Back Suitability Labels

After the table is printed, apply the label matching each ticket's mode, and remove the other two of the three suitability labels (CLOSE gets no label change). Only touch these three labels; leave pipeline labels such as `agent:triaged` or `agent:in-review` untouched.

- AUTO: add `agent:ready`; remove `agent:interactive-recommended`, `agent:human-only` (only if present).
- INTERACTIVE: add `agent:interactive-recommended`; remove `agent:ready`, `agent:human-only` (only if present).
- BLOCKED: add `agent:human-only`; remove `agent:ready`, `agent:interactive-recommended` (only if present).
- CLOSE: no label change.

```bash
# Jira
jira issue edit <KEY> --label=<add-label> --label=-<remove-label> --no-input

# GitHub
gh issue edit <NUMBER> --add-label "<add-label>" --remove-label "<remove-label>"
```

Only issue a `--label=-<remove>` (Jira) or `--remove-label` (GitHub) call for a label the ticket currently has; re-running this command must not error or double-count on a ticket already labeled correctly.

Jira labels are free-form, but GitHub's `--add-label` fails when the label does not exist in the repository. Create the three suitability labels once before the first write-back (the dashboard Settings page creates SOVA's whole required set, or `gh label create "agent:interactive-recommended" --force`).

Print one line: `Labeled: N`.

### 8. When the User Selects a Ticket

- Assign it (if not already): Jira `jira issue assign <KEY> $(jira me)`, or GitHub `gh issue edit <NUMBER> --add-assignee @me`.
- Suggest creating a feature branch: `git checkout -b feat/<short-name> main`.

## Cross-References

- **After selecting a task**: Run `/develop-full <ISSUE_NUMBER>` to start working
- **Want a deeper look at an issue?** Run `/issue <ISSUE_NUMBER>`
- **Daily overview**: Run `/standup` for full context

## Rules

- NEVER use emojis in any output
