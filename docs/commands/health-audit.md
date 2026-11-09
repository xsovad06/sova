# Health Audit Command: Maintainer Rationale

This is maintainer reference for `sova/commands/data/commands/health-audit.md`. It is
not distributed: the command text carries only what the agent acts on, and the reasoning
behind each gate lives here. The command's size is capped by
`tests/test_command_size_budget.py`, so rationale added to the command instead of this
file fails that test.

## Where each gate lives

| Decision | Home | Pinned by |
|----------|------|-----------|
| Report path safety (tracked, ignored, untracked, not a git repo) | `sova audit report-path` | `tests/test_audit_cli.py` |
| Visibility and disclosure policy lookup | `sova audit disclosure` | `tests/test_audit_cli.py` |
| Project board membership and scope remedies | `sova board add` | `tests/test_board_cli.py` |
| What each verdict means for the run | one instruction per verdict in the command | `tests/test_health_audit_command.py` |
| Withheld and carry-over lifecycle, claims ledger, disclosure choices | command text | `tests/test_health_audit_command.py` |

## Report path confidentiality

A "withhold" or "file neutrally" disclosure choice deliberately keeps the full exploit
path in the report body. Publishing the report through a commit defeats the point of
withholding, so the path's status is checked before the audit starts, whether or not
the file exists yet. This repository gitignores the file, but the distributable copy of
the command runs in arbitrary projects where `docs/` is normally tracked.

The verdict is recorded in Step 1 and enforced in Phase C, not in Step 1, for two
reasons. A fresh project is `unsafe-untracked` by default, and stopping there would
abort the audit before the expensive fan-out whether or not any finding turns out to be
security-sensitive. And Phase A has already written the report by the time the block
fires, so the remedy is remediation of an existing exposure, not prevention. A tracked
file must leave version control, not only be gitignored, because its history still
carries the committed content.

The verdict command fails closed: only a path that is untracked and ignored is `safe`,
and an unexpected git error maps to `unsafe-untracked`. The command applies the same rule
when the call itself fails (no `sova` on PATH, an older install without `audit`, a crash)
or prints something else: no verdict counts as `unsafe-untracked`. Step 1 does not
untrack a tracked report; only a Withhold or File neutrally choice makes Phase C do it, so
a project that deliberately tracks its report is not touched by an audit that withholds
nothing.

## Disclosure pre-flight

A failed or unrecognised visibility check maps to public, so a transient `gh` failure
makes the gate ask rather than silently skip it. The same holds when the `sova audit
disclosure` call itself fails or prints something else: it counts as `public-no-policy`.
The verdict is the last line printed, and the CLI prints nothing else of its own (a failed
lookup is logged, not echoed), so a note above the verdict cannot be mistaken for "other
output" and drop a policy the CLI found.
A policy file counts at any of the three places GitHub honours (`SECURITY.md`, `.github/SECURITY.md`, `docs/SECURITY.md`).
A found policy changes the wording of the question, never whether it is asked, and its
private reporting channel gives "Withhold" a path that is not just a silent entry in
`withheld=`.

A private or internal repo does not end the pre-flight unconditionally. A carry-over
group never becomes public enough later to re-trigger it, so every carry-over group is
resolved on the spot or it would stay stuck in `withheld=` permanently.

## Withheld and carry-over lifecycle

`withheld=` is written twice because Phase A writes the file before Phase C's pre-flight
has decided anything. Phase A writes the carry-over set from Step 1 unchanged, a
fail-safe placeholder that cannot lose information. Phase C rewrites it as the union of
this run's "withhold" choices and any still-unresolved carry-over group. The field only
ever holds the placeholder or its superset, so an abort or bail-out between the two
writes cannot drop a carry-over name.

A carry-over group's finding blocks are captured in Step 1 because Phase A overwrites
the report before Phase C needs them. "Drop" exists for carry-over groups only: without
it a public repo can only re-ask forever, since "Withhold" re-adds the name. A group this
run's own analysis just found cannot be waved off as resolved, so it keeps the three-way
choice.

The pre-flight folds a group into an open issue that already covers its files instead of
filing a duplicate, and caps new issues at 5 per run, deferring the rest into
`deferred=`. A fold publishes on a public issue, so it follows the group's disclosure
choice: a withheld group is never folded, and a "file neutrally" group's comment omits
the exploit path. A carry-over group the user chose to file is exempt from both the cap
and deferral (an inconclusive search files it): deferring it would leave it unfiled, a
state outside Phase D's three carry-over end states (filed, still withheld or deferred,
dropped), and the user has already answered for it once.

Group names are slugified before they go into the comma-delimited field, since a name
with spaces or commas would be unparseable, or merge with its neighbour, on the next
run's Step 1, the one consumer that has to round-trip it.

## Claims ledger

Every finding carries claims (line counts, paths, names, query counts) that end up in an
issue body no one re-checked. A 5-10 item spot-check is not enough, because a claim that
is never re-derived is a guess wearing the evidence format. Three agents at up to 20
findings each can reach about 60 findings, and verifying them one claim at a time would
mean 100 to 200 sequential calls in the context the fan-out exists to protect. Hence
batching per file or delegating to a verification subagent.

Verification runs in priority order so that when the budget runs out, the findings left
unverified are the least severe ones, never a P0 behind a verified P4. An unverifiable
claim is dropped rather than hedged: an unverifiable claim is not a weaker claim, it is
not a claim.

## Project board membership

The board belongs to the repository owner `gh` resolves as the base repository, which is
not necessarily the authenticated `github_user`, and in a fork workflow not necessarily
the remote the agent operates against. `sova board add` resolves it with
`gh repo view --json owner,name` and names the owner and project number in any failure
line, so a mismatch between the configured board and the resolved owner shows up as such
instead of being misread as a scope problem.

A token can lack `project` or `read:org`, and they need different remedies:

- **`project`**: `gh auth refresh -s project`. With `GH_TOKEN`, `GITHUB_TOKEN` or
  `GH_ENTERPRISE_TOKEN` set, `gh auth refresh` refuses to run, so a replacement token is
  needed instead, and the line names the variable. A token with no project scope at all
  is reported by gh as missing `read:project` (or `read:org read:project`), not `project`,
  so either name selects this case, and it is checked before `read:org`. The GraphQL
  fallback needs this scope too, so it is not attempted. Board discovery applies the same
  classification to a failed listing. `gh api graphql` words the same failure differently
  ("requires one of the following scopes: ['read:project']"), and the fallback's lookups
  and mutation are classified from that wording too. A `failed:` line with no URL is
  board-level, so the command follows the remedy the line names rather than one fixed route.
- **`read:org`**: `gh`'s owner-type resolution inside `project item-add` can demand it
  even for a user-owned board. The GraphQL mutation with an explicit `user(login:)` query
  skips that resolution, so it works without `read:org`. For an org-owned board the query
  would be `organization(login:)`, which needs `read:org` itself, so the only real fix
  there is a token carrying that scope. When the fallback cannot resolve the project id
  its failure line carries gh's own first error line and a conditional remedy (the board
  may be org-owned, which needs `read:org`, or the project number may be wrong), so a
  transient error or a wrong number is not blamed on the token. Once `item-add` reports
  the missing scope, later URLs go straight to the fallback instead of failing the same
  way and waiting out a timeout each. A board listing that fails for `read:org` has no
  GraphQL fallback, so its `failed:` line says to set `task_source.github_project_number`
  or use a token with `read:org`.

`sova board add` reads the configured board number from the project root, as `sova audit` does (the nearest
ancestor holding `.claude/sova.db` or `sova.toml`, else the git root), so a subdirectory cwd
or a SOVA project nested in a larger repo cannot make it discover a different board. Any load error (a
malformed or unreadable `sova.toml`, a locked database) falls back to discovery with the
error on stderr. Issue URLs on any host are accepted, GitHub Enterprise included.

`gh project view` hits the same owner-type resolution as `item-add`, so Phase D cannot
count on it where `read:org` is missing. The fallback's project query therefore also
fetches the board title and `sova board add` prints it on the `board:` line (`board:
owner#N "Title"`); Phase D reads the title there and only calls `gh project view` when
the line is absent. When no single board can be chosen, one `skip:` line replaces the
per-URL lines.

The fallback resolves the issue in the repository the issue URL names, not the one `gh repo
view` resolved, so a cross-repo URL (a fork checkout filing upstream) adds the issue the URL
points at, as the `item-add` path does. Only the board owner comes from `gh repo view`.

Re-adding an item already on a board succeeds in GitHub's API (`addProjectV2ItemById` is
idempotent), so the subcommand has no `exists` status: any other `item-add` failure is a
`failed` line, never matched against error text.

On a GraphQL error, `gh api graphql --jq ...` exits non-zero but can still have printed a
raw error response to stdout. The subcommand therefore keys on the exit status and never
trusts the output of a failed call.

With `task_source.github_project_number` of `0` (the documented sentinel for "no board"),
the subcommand keeps the decision the command used to make: it runs `gh project list
--owner <repo-owner> --limit 1000 --format json` and uses the board when exactly one
exists, printing a `board:` line that names it so Phase D can verify against the same
board. With none or several it prints a `skip:` line giving the reason and exits 0, so a
repo without a single clear board does not look like a failed audit. A failed or unreadable
listing is a `failed` line and exits 1, never a skip.

The fallback resolves its project id from the repo owner `gh repo view` returns, not the
configured `github_user` (a deliberate departure from the original spec's decision 7): it
keeps the fallback on the same board as `item-add` and Phase D.

A configuration that cannot be loaded does not drop board membership: the load error goes
to stderr and the subcommand discovers the board as if the number were `0`, as the command
did before the cut. A `failed:` board line with no URL has its own bullet in the command.

Phase D checks membership by project title, because `gh issue view --json projectItems`
returns no project number: a non-empty list alone only proves the issue sits on some
board. A failed query is "could not verify", never "not on the board", the same
fail-toward-asking rule as the visibility check.

## Issue body template order

The repository's own `.github/ISSUE_TEMPLATE/*.md` wins when it carries the scored
headings, then the `issue-template` skill, then the canonical structure. The canonical
structure is the default for a portable project, not a last resort: it is exactly what
`compute_quality_score()` scores, so a filed issue needs no triage rewrite. A separate,
lower-scoring fallback template is deliberately absent, because an earlier revision left
the scorer-matching structure attached to no branch at all.

## Folding into open issues

An audit run used to file every group as a new issue after a bare duplicate check with
no matching rule. The 2026-11-04 run overlapped issues that were already open in 7 of
its 10 groups, and audit issues were the slowest to close of any source (median 4.1
days, half still open after a month). Phase C therefore matches each group's files
against the open issues and comments the findings on a match instead of filing. The
comment is posted only in "Create the issues", after the user confirms the plan, because
a pre-flight comment would publish findings before the confirmation gate.

A fold target is an issue the audit does not own, so it is tracked apart from created
issues: `folded=<slug>:<N>` records it and `issues=` never holds it. Step 1's staleness
decision reads created issues only, so a long-lived umbrella issue cannot hold the cycle
open. A closed fold target proves nothing about the folded findings (it may have closed
for its own scope), so its group becomes a carry-over group instead of counting as
resolved. Fold targets are also exempt from the board add and the board check.

The 5-issue cap keeps one run from outpacing what the project closes. A group past the
cap (or one whose search was inconclusive) goes into its own `deferred=` field, which
makes it a carry-over group the next audit re-asks: a report-only `unfiled` state lost
the group whenever Step 1 deleted the report after the tracked issues closed. It is not
`withheld=`, which means only a disclosure hold: a deferred group with no exploit detail
is asked file now, defer again or drop, never the disclosure question, and never
triggers the report-path remediation. The same search-and-fold rule applies to every issue
filed by hand through the `issue-template` skill.

The search is a duplicate guard, so any answer it cannot trust defers the group rather
than filing: a failed call looks like "no match", and a result of exactly 1000 may have
dropped the matching issue. It searches titles as well as bodies (an issue can name the
file only in its title), and a group that names no file is searched by its module or
concern. The path is quoted as a phrase, since unquoted `a/b/c.py` splits into terms and
matches any issue naming `a`, `b` and `c`. Step 1's staleness check lists open issues
once and looks up only the tracked numbers the list omits, for the same reason: a
1000-issue list can omit a tracked open issue, which would delete the report while work
remains. A lookup that fails is reported as `could not verify #N`, never counted as open,
so a deleted or transferred issue cannot keep the cycle open forever.

A fold is a publication like filing: the comment lands on a public issue, so it follows
the group's disclosure choice. A withheld group is never folded, and a "file neutrally"
group's comment drops the exploit path exactly as its issue body would. A deferred carry-over group
stays in `deferred=`, one of Phase D's three carry-over end states, so the cap cannot
drop it.

## Tracking header and mode selection

The `audit-tracker` comment sits above the title because `/status` and `/standup` read
it to report audit progress. An existing report does not select incremental mode on its
own: the user may have chosen a full audit precisely to overwrite it, so Step 4 uses the
mode Step 1 recorded.

## Pagination limits

`gh issue list` and `gh label list` default to 30 results, and a
tracked issue past that default would be misread as closed. Every such call in the
command passes `--limit 1000`, and the milestones call paginates.
