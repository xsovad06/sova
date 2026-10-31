---
name: sova-find-task
description: "Triage the Jira or GitHub ticket backlog into AUTO/INTERACTIVE/BLOCKED/CLOSE, print one compact table, and record suitability (tracker label for self-assigned tickets, local record for all of them)."
---

# Find Next Task

Triage this project's open tickets (Jira or GitHub, whichever `task_source.type` is configured) into AUTO, INTERACTIVE, BLOCKED or CLOSE, print one compact table, and record each ticket's suitability: the matching `agent:` label for a self-assigned ticket, and a local record (`sova assess set`) for every AUTO/INTERACTIVE/BLOCKED ticket regardless of assignee. A ticket's suitability is its underlying mode (AUTO or INTERACTIVE); being blocked is recorded separately, and only for a hold.

**Scope**: the arguments provided when this skill is invoked

## Instructions

### 1. Determine the Task Source

```bash
sova config get task_source.type
```

Read every other setting through `sova config get` as well (never assume a default): `task_source.jira_project_key`, `task_source.jira_jql_filter`, `task_source.labels`, `task_source.milestone`, `triage.skip_title_prefixes`, `triage.skip_labels`, `triage.interactive_title_prefixes`, `triage.interactive_labels`.

### 2. Build the Ticket Pool

**Jira** (`task_source.type = "jira"`):

```bash
# Pickup states only, current user first, scoped to <jira_project_key> and with <jira_jql_filter> from step 1:
jira issue list -q "project = <jira_project_key> AND status in (Backlog, \"To Do\", New, \"Selected for Development\") AND assignee = currentUser() AND (<jira_jql_filter>)"
jira issue list -q "project = <jira_project_key> AND status in (Backlog, \"To Do\", New, \"Selected for Development\") AND assignee is EMPTY AND (<jira_jql_filter>)"
```

The `project = <jira_project_key>` clause is mandatory on both queries and is independent of `task_source.jira_jql_filter`: the account running this command can usually see several Jira projects, so without it an empty filter widens the pool to every accessible project and step 7 then writes `agent:` labels onto tickets this project does not own. If `task_source.jira_project_key` is empty, stop and report that rather than running an unscoped query.

**GitHub** (default, `task_source.type = "github"`):

```bash
gh issue list --assignee @me --state open --limit 50 --json number,title,labels,milestone,updatedAt,body
gh issue list --search "no:assignee" --state open --limit 30 --json number,title,labels,milestone,updatedAt,body
```

If `the arguments provided when this skill is invoked` contains filters (label, milestone), incorporate them into the query; otherwise apply the configured `task_source.labels` and `task_source.milestone` as the defaults, which is the pool SOVA's own adapter works from.

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
gh issue view <NUMBER> --json number,title,body,labels,assignees,author,comments

# GitHub blocking relationships (not exposed by `gh issue view`, so fetch them separately)
gh api "repos/<owner>/<repo>/issues/<NUMBER>/dependencies/blocked_by" --jq '.[] | "\(.number) \(.state) \(.title)"'
gh api "repos/<owner>/<repo>/issues/<NUMBER>/sub_issues" --jq '.[] | "\(.number) \(.state) \(.title)"'
```

Capture: description, reporter, linked/blocking issues, and the last 3 comments.

Reporter and blockers are retrieved, never inferred:

- The reporter is `author.login` from the `--json` call. `gh issue view` omits it unless `author` is in the field list, and a ticket's body rarely names its own reporter.
- GitHub holds blocking relationships in three independent places, and a ticket can use any one of them: its native issue dependencies (`dependencies/blocked_by`), its sub-issue tree (`sub_issues`, where an open child blocks the parent), and this project's own `## Dependencies` section of `#N` references in the body (the convention `parse_dependencies()` in `sova/supervisor/dependency_graph.py` reads). Check all three. An empty body section is not evidence that no blocker exists, only that none was written down.
- `<owner>/<repo>` comes from the configured repository (`sova config get github_repo`, a top-level key, not a `task_source.*` one), not from whatever the current directory's remote happens to be.
- If either `gh api` call fails, treat that ticket's blocker data as unknown and classify it INTERACTIVE rather than AUTO. A held ticket stays BLOCKED, and this applies to its underlying mode instead (step 5). A failed call must never be read as "no blockers": capture the exit status explicitly (`if ! out=$(gh api ...); then ...; fi`) instead of piping it straight into a counter or appending `|| true`, both of which turn a failure into a silent zero.

### 4. Run the Per-Ticket Checks

- **Blockers**: for each blocking ticket found in step 3 (Jira issue links, or GitHub `blocked_by` / open `sub_issues` / `## Dependencies` references), its current status and whether its linked PR is merged. A ticket whose blocker data could not be retrieved is not an unblocked ticket.
- **Comments**: does the last 3 comments contain an unanswered question from the current user, or a lead saying wait/hold?
- **Code**: `grep` this repo for any file, symbol, or dependency version the ticket names, to detect work already on the base branch or stale references. A grep match is a signal toward CLOSE, not proof; when ambiguous, prefer INTERACTIVE/human confirmation over asserting CLOSE.
- **Repo match**: does the ticket name a repo other than this project's configured `github_repo`?

### 5. Classify Into One Mode

- **AUTO**: change confined to this repo, verifiable by this repo's own tests and lint, acceptance criteria present, no unanswered question, blocker data successfully retrieved, and every blocker closed with its code already merged.
- **INTERACTIVE**: needs another repo (app-interface, a `*-actions` repo, IQE), needs a tool an autonomous agent cannot reach (bonfire or an ephemeral environment, stage or prod, `oc`, VPN, AWS, Splunk, quay), is an investigation or spike, or needs a human decision or conversation. A title or label matching `triage.interactive_title_prefixes`/`triage.interactive_labels` is also INTERACTIVE, unless it also matches `triage.skip_title_prefixes`/`triage.skip_labels`, in which case it is BLOCKED as human work (mirroring `sova triage`'s human_only-over-interactive precedence).
- **BLOCKED**: not pickable right now. Name the cause in the `Needs` column, because step 7 records each cause differently:
  - **Dependency**: an open blocker. The ticket waits on another ticket, not on a person.
  - **Hold**: a lead's hold comment, the current user's own unanswered question, or an `agent:on-hold` label already on the ticket (a lead can add it by hand) that no later comment lifts. A ticket can be both dependency-blocked and held.
  - **Human work**: a title or label matching `triage.skip_title_prefixes`/`triage.skip_labels`, or a ticket only a human can do. It waits on no one: the human doing it is the work. A match here wins over a hold.
- **Underlying mode** (BLOCKED tickets only, used by step 7): the mode the ticket would get if its dependency block and hold did not exist, AUTO or INTERACTIVE by the criteria above, with the blocker, hold comment and unanswered-question conditions treated as cleared. A ticket whose blocker data could not be retrieved is INTERACTIVE here too (step 3).
- **CLOSE**: the fix is already on the base branch.

### 6. Output Contract (and Nothing Else)

Print exactly this, in this order:

1. One table, columns `# | Key | Reporter | Repo | What | Status | Needs | Mode | Deps`, at most 12 rows, ordered: critical items blocking other teams first, then CLOSE, then AUTO by priority, then INTERACTIVE, then BLOCKED.
2. At most 3 one-line actions (for example "close RHCLOUD-123, PR merged" or "ping @lead on RHCLOUD-456").
3. One line: `Skipped: N` with counts by reason (epic, spike, held, other repo, ops).
4. One line: `Labeled: N  Recorded: M`, printed after the step 7 write-back completes.

These four items are the whole output. Do not print anything else: no prose summary, no per-ticket narration outside the table.

### 7. Write Back Suitability

After the table is printed, record each ticket's mode. The write-back splits by assignee: a self-assigned ticket keeps the existing tracker label plus a new local record; an unassigned ticket (no individual owns it, so this account cannot speak for it on the tracker) gets only the local record, never a label change. Every AUTO/INTERACTIVE/BLOCKED ticket this command evaluated gets the local record, including ones excluded from the printed table (spike, held, ops, other repo): only epics (never classified) and CLOSE (no suitability verdict, just "already done") are exempt from both the label and the local record. Only touch the four labels below (the three suitability labels and `agent:on-hold`); leave pipeline labels such as `agent:triaged` or `agent:in-review` untouched.

Mode-to-shorthand mapping (used for both the label and the local record):

- AUTO: `agent:ready` / local shorthand `ready`.
- INTERACTIVE: `agent:interactive-recommended` / local shorthand `interactive`.
- BLOCKED, dependency only: nothing for blocking. Record the ticket's underlying mode (AUTO or INTERACTIVE) as above. The dependency graph already reports the ticket as blocked live, and a stored blocking marker could only go stale when the blocker closes.
- BLOCKED, hold (with or without a dependency): for a self-assigned ticket, record the underlying mode as above and also add `agent:on-hold`. The label is not a pipeline state and leaves the ticket's real state untouched; the shared classifier reports it as `blocked`. There is no local shorthand for a hold, so an unassigned ticket, which gets no label, records `interactive` instead of its underlying mode: that keeps the supervisor from starting autonomous work on it while it is held. The run that finds the hold lifted records the underlying mode again.
- BLOCKED, human work: `agent:human-only` / local shorthand `human-only`. This is the only cause that writes `human-only`.
- CLOSE: no label, no local record.

For a **self-assigned** ticket, add the label for its recorded mode and remove the other two suitability labels (only if present). `agent:on-hold` is separate: add it for a hold in the same edit call as the mode label (a second `--label`/`--add-label`), so a failed call can never leave the mode label without the hold, and remove it only if the ticket currently has it and is no longer held (the lead said to proceed, or the user answered their own question):

```bash
# Jira
jira issue edit <KEY> --label=<add-label> --label=-<remove-label> --no-input

# GitHub
gh issue edit <NUMBER> --add-label "<add-label>" --remove-label "<remove-label>"
```

Only issue a `--label=-<remove>` (Jira) or `--remove-label` (GitHub) call for a label the ticket currently has; re-running this command must not error or double-count on a ticket already labeled correctly.

Jira labels are free-form, so `agent:on-hold` needs no creation step there. GitHub's `--add-label` fails when the label does not exist in the repository. Create the three suitability labels and `agent:on-hold` once before the first write-back (the dashboard Settings page creates SOVA's whole required set, or `gh label create "agent:interactive-recommended" --force`, and likewise `gh label create "agent:on-hold" --force`).

For **every** AUTO/INTERACTIVE/BLOCKED ticket (self-assigned or unassigned), also write the local record (the underlying mode's shorthand, `interactive` for an unassigned held ticket, or `human-only` for human work), which the supervisor's human-involvement gate consults as a fallback when the tracker carries no suitability label:

```bash
sova assess set <ISSUE_NUMBER> <shorthand>
```

`<ISSUE_NUMBER>` can be passed as-is in any form the tracker uses (`1234`, `#1234`, `RHCLOUD-1234`): the CLI normalizes it to the bare number. Re-running this command on the same ticket is safe: the local record is append-only and the latest write wins.

Print item 4 of the output contract, `Labeled: N  Recorded: M`: `N` counts only the self-assigned tickets whose tracker labels changed (a suitability label or `agent:on-hold`), and `M` counts every successful `sova assess set` call (self-assigned and unassigned alike). A ticket counts toward `M` only when the command exits 0; a failed call (for example a rejected tracker-key prefix) does not increment it, so a wholly failed write-back loop is visible as `Recorded: 0` rather than looking identical to a correct run.

### 8. When the User Selects a Ticket

- Assign it (if not already): Jira `jira issue assign <KEY> $(jira me)`, or GitHub `gh issue edit <NUMBER> --add-assignee @me`.
- Suggest creating a feature branch: `git checkout -b feat/<short-name> main`.

## Cross-References

- **After selecting a task**: Run the `sova-develop-full` skill <ISSUE_NUMBER> to start working
- **Want a deeper look at an issue?** Run the `sova-issue` skill <ISSUE_NUMBER>
- **Daily overview**: Run the `sova-standup` skill for full context

## Rules

- NEVER use emojis in any output
