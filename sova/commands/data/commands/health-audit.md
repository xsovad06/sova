---
name: health-audit
description: Deep technical health audit (architecture, modules, files, functions). Scored, prioritized, actionable.
user-invocable: true
category: core
inputs:
  - project_dir
outputs:
  - health_score
  - findings
---

# Deep Technical Health Audit

**Focus area**: $ARGUMENTS

## Role

You are the original architect and sole developer of this application, a senior
engineer with 20 years of experience who knows every design decision, shortcut and
deferred TODO. Produce an honest, thorough health report for (a) onboarding a new
senior engineer, (b) a due-diligence review, and (c) your own improvement backlog.

Be brutally honest: flag what is genuinely good (so it is preserved) and what is weak
(so it can be fixed). Every finding must reference concrete code,
files, or patterns in THIS codebase. No generic advice.

## Procedure

### Step 1: Staleness check

**Report path safety**: first, whether or not `docs/HEALTH-AUDIT.md` exists yet, run:

```bash
sova audit report-path
```

Record the verdict for Phase C. It blocks only a "withhold" or "file neutrally"
disclosure choice, never the audit itself:

- `safe`: the path is gitignored. No action.
- `unsafe-untracked`: warn now that withheld or neutrally filed exploit detail
  would land in a file git can commit; suggest adding `docs/HEALTH-AUDIT.md` to
  `.gitignore`. Continue.
- `unsafe-tracked`: same warning. Do not untrack it now: only a Withhold or File
  neutrally choice makes Phase C run `git rm --cached`. Continue.
- `unsafe-not-a-git-repo`: state that the path has no git protection. Phase C's block must ask for a different, protected path, not a
  `.gitignore` edit. Continue.

The verdict is the last line printed; a failed call or any other last line counts as
`unsafe-untracked`.

Next, check whether `docs/HEALTH-AUDIT.md` already exists. If it does, read its
`audit-tracker` comment and fetch every open issue number (the default page is 30, and
a tracked issue past it would be misread as closed):

```bash
gh issue list --state open --limit 1000 --json number --jq '.[].number'
```

A withheld group (the `withheld=` field, see Phase A) has no issue number, so it never
appears in that comparison. It is still open work: record it as a **carry-over group**,
which Phase C's pre-flight re-asks instead of resolving silently. Before anything
overwrites the report, capture each carry-over group's verbatim finding blocks (the
`#### [P{n}] {title}` blocks under "Findings by Level") into this run's working state,
keyed by slugified group name. Phase C renders a carry-over group from that capture,
because by the time Phase C executes, Phase A has already overwritten the file.

Compare against the tracked issue numbers:
- **All closed, no `withheld=`**: delete the file, tell the user the previous cycle is
  complete, and run a **full audit**.
- **All closed, `withheld=` present**: do not delete. List the withheld groups, capture
  their findings, record them as carry-over groups, and run a **full audit** (the
  withheld carry-over does not make this an incremental run); record mode = `full`.
  Phase A rewrites the file, so each carry-over group's name and findings must reach
  its header; an unresolved carry-over group is never silently dropped.
- **Some still open**: warn that N issues from the previous audit are still open, list
  them, and ask the user to choose **Full audit** (overwrite with a fresh analysis),
  **Incremental audit** (re-analyze only areas changed since the last audit, preserving
  still-open findings elsewhere), or **Abort**. If `withheld=` is present, also list
  and capture its groups as carry-over groups, whichever mode is picked. Record the
  mode (`full` or `incremental`): Step 4 depends on it.

### Step 2: Discover the project

Read these in order, skipping any that do not exist: `AGENTS.md`, `CLAUDE.md` and
`.claude/rules/*.md`; `README.md`; architecture docs (`docs/architecture.md`,
`docs/ARCHITECTURE.md`, `ARCHITECTURE.md`); coding standards (`CONTRIBUTING.md`,
`.editorconfig`, linter configs); the manifest (`pyproject.toml`, `package.json`,
`Cargo.toml`, `go.mod`); the top-level directory listing.

Note the project's purpose, tech stack, scale, stage (prototype to mature), key
domains, and architecture patterns.

Collect codebase stats in parallel: source and template line counts (excluding venv, migrations and `.claude/`), module count, and test count.

### Step 3: Run checks

Run the project's checks to verify current health: `{{ test_cmd }}`, `{{ lint_cmd }}`,
and `{{ check_cmd }}` when the project has one single CI-equivalent target. If those
targets do not exist, discover the real commands from the `Makefile`, `package.json`
scripts, or CI config.

### Step 4: Incremental mode check

Use the mode recorded in Step 1; an existing report alone does not select incremental
mode, since the user may have chosen a full audit to overwrite it.

For **incremental** audits, find what changed since the last audit date (from the file
header) with `git log --after="<last-audit-date>" --oneline --stat | head -100`,
re-analyze only those modules, and preserve findings for unchanged areas that are still
open issues. Still produce the full scorecard and executive summary.

For **full** audits, ignore any prior report and run Step 5 as a full analysis.

### Step 5: Parallel codebase walk

This is the most expensive step. **If a focus area was given** in `$ARGUMENTS`, skip the
fan-out: analyze that area deeply in the main context, still produce all 10 scorecard
dimensions, and score non-focus dimensions from Step 2 only, marked "(surface-level)".

**For full audits**, call the Agent tool 3 times in a single message (so they run
concurrently). Use `subagent_type="general-purpose"` for all three. Include the Step 2
project context in each prompt. Each agent reads concrete files and runs
concrete commands (`wc -l`, `grep`, `find`) for evidence, and returns at most 20
findings as severity (P0-P4), title, location, issue, evidence, recommendation, plus
3-5 strengths. If an agent fails or times out, continue with the rest, mark the report
partial, and score the missing dimensions "(not analyzed)".

- **Agent A, Architecture and Security**: dependency graph (circular imports), data
  model and migrations, security architecture (auth, input validation, secrets,
  injection, uploads, CSP, CORS), infrastructure readiness (caching, queues,
  connections, rate limiting, observability), API consistency, configuration.
- **Agent B, Module Quality and Patterns**: service layer consistency, god files (over
  500 lines), dead code, pattern consistency, type hint coverage, error handling (bare
  excepts, swallowed exceptions), template quality.
- **Agent C, Testing, Dependencies and Operations**: test quality and coverage gaps,
  dependency health, CI/CD quality, operational readiness (logging, monitoring, health
  endpoints, backups), deployment.

### Step 6: Synthesize and verify

1. **Collect** the findings from the completed agents.
2. **Deduplicate**: merge overlaps, keeping the most detailed evidence.
3. **Verify with a claims ledger**: for every finding, not a sample of them, re-derive
   each concrete claim (a line count, path, function or variable name, query count, an
   index assertion) before it enters the report.
   - **Order by priority first**: rank findings by (severity * blast radius), reusing
     the agents' P0-P4 labels, and verify in that order, so a budget shortfall only
     ever drops the least severe findings.
   - **Batch per file**: one targeted `grep`, `wc -l` or `Read` per file answers every
     claim about it, not one command per claim.
   - **Or delegate**: spawn a verification subagent with the findings and have it
     return a corrected-claims table.
   - Same value: keep. Different value: correct it to the measured value.
   - A claim inherently about reading code, not running it: keep it, marked "traced in
     code, not executed".
   - A claim that cannot be re-derived (tool unavailable, timeout, file gone): drop the
     claim entirely, with no "unverified" hedge. A finding left with no concrete
     evidence is dropped too and counted in the dropped tally below.
   - **Budget rule**: if verification cannot finish before the context runs out, drop
     every finding whose claims were not reached, from the report and the issue
     bodies, and state how many were dropped, that it was a context-budget limit and not
     a quality judgment, and their severities (e.g. "dropped 4 findings: 3 P3, 1 P4").
4. **Score** the 10 dimensions from verified findings and strengths.
5. **Rank** findings by (severity * blast radius).

### Step 7: Validate labels

Only use labels and milestones that exist in the project:

```bash
gh label list --limit 1000 --json name --jq '.[].name' | sort
gh api repos/:owner/:repo/milestones --paginate --jq '.[] | "\(.number)\t\(.title)"'
```

If a label is missing, create it with `gh label create` or use the closest existing one.
If the target milestone is missing, create it with
`gh api repos/:owner/:repo/milestones -X POST -f title="<name>"` before Phase C.
Confirm it appears in the list before passing it to `--milestone`.

## Analysis Levels

Produce findings at four levels, top to bottom, each surfacing what the levels above miss.

1. **Architecture and system design**: dependency graph, data model, security
   architecture, infrastructure readiness, scalability (N+1 queries, missing
   pagination, unbounded queries, blocking calls in async paths), configuration, API
   consistency.
2. **Module health**: single responsibility, interface boundaries, service layer
   coverage, test coverage and quality (behavior, not implementation), documentation
   accuracy.
3. **File quality**: god files, dead code and orphaned assets, inconsistent patterns
   across similar files, configuration drift, logic in templates.
4. **Function and class granularity**: long or multi-purpose functions, missing or
   wrong type hints, error handling gaps, naming inconsistencies, complex conditionals,
   docstring accuracy.

## Evaluation Dimensions

Score each dimension 1-10 with a one-line justification: **Correctness** (bugs, races,
data integrity), **Security** (OWASP Top 10, auth bypass, injection, data exposure),
**Performance** (queries, caching, rendering, payloads), **Maintainability**,
**Testability**, **Extensibility** (cost of a new module, feature or integration),
**Operability** (logging, error reporting, deployment, rollback, monitoring),
**Documentation** (onboarding path, accuracy), **Code Hygiene** (dead code, TODOs,
style, dependency freshness), **Production Readiness** (what blocks a confident deploy
today).

## Severity Classification

- **P0 (Critical)**: blocks a production deploy, data loss risk, security
  vulnerability. Fix before launch.
- **P1 (High)**: significant maintainability or reliability risk. Fix this milestone.
- **P2 (Medium)**: quality issue that compounds. Plan within 2 milestones.
- **P3 (Low)**: nice-to-have or minor inconsistency. Address opportunistically.
- **P4 (Note)**: observation. No action required.

## Output Format

- **Executive Summary** (5-10 sentences): overall health, biggest risk, strongest
  aspect, readiness for a second developer and for production.
- **Scorecard**: the 10 dimensions with scores and one-line justifications.
- **Findings by Level**: grouped under Levels 1-4, each finding as:

```
#### [P{n}] {Short title}
**Location**: {file(s) or module(s)}
**Issue**: {What is wrong and why it matters: concrete, not generic}
**Evidence**: {Code snippet, query count, or specific example}
**Recommendation**: {Specific action to fix, not "consider improving"}
```

- **Strengths**: 5-10 things done well to preserve.
- **Prioritized Action Plan**: top 10 findings ranked by (severity * effort-to-fix),
  with effort estimates. The "if you only have two weeks" list.
- **Onboarding Gap Analysis**: for a senior developer joining tomorrow with only the
  repo and its docs: what is clear, what confuses, what is undocumented but critical,
  and what they would break on their first PR.

## Phase A: Generate docs/HEALTH-AUDIT.md

Write the full audit to `docs/HEALTH-AUDIT.md` as a local work artifact. It must
include a tracking header, a title and date header with codebase stats (LOC, test
count, lint status), every Output Format section, a "Task Groups" section (Phase B) and
an "Action Plan" table with issue links (Phase C). Standard Markdown, no frontmatter,
no emojis.

### Tracking header

Put this at the very top of the file, before the title, so `/status` and `/standup` can
detect audit progress:

```markdown
<!-- audit-tracker: issues=68,69,70 withheld=auth-token-storage -->
```

- `issues=` lists every issue number created in Phase C.
- `withheld=` lists **slugified** group names (lowercase, each run of non-alphanumeric
  characters collapsed to one hyphen, no commas). Slugify before writing, and compare
  carry-over names against Phase B group names case- and hyphen-insensitively.
- **Phase A**: write `withheld=` verbatim as the carry-over set Step 1 surfaced,
  unchanged. Omit it only when Step 1 surfaced no carry-over groups.
- **Phase C**: rewrite `withheld=` as the union of every group whose pre-flight choice
  was "withhold" and any carry-over group still unresolved (not filed, not dropped).
  Omit the field when that union is empty.

An abort between the two writes therefore cannot drop a carry-over name. Step 1 of the
next run re-surfaces the field's groups.

A withheld group's findings still count in every total the report states (executive
summary, scorecard, action plan). State the split, e.g. "14 findings (12 filed, 2
withheld)".

## Phase B: Group Findings into Task Batches

Group all findings into session-sized batches (2-6 hours) that touch the same files or
module area and have clear dependency ordering. For each group give: name, findings
(severity and title), files touched, estimated hours, dependencies, and labels from
Step 7's validated taxonomy. Target 5-10 groups, never more than 10, each
self-contained (a developer can pick it up cold). A group with 1 finding is too small
unless it truly stands alone. Write the grouping, with a dependency diagram, into the
"Task Groups" section.

## Phase C: Create GitHub Issues

### Disclosure pre-flight

Run this before the general confirmation below; it changes what that confirmation
confirms. It does not change the scoring, only how a security-sensitive group is
published.

```bash
sova audit disclosure
```

The verdict is the last line printed: `private`, `public-policy <path>` or
`public-no-policy`. A failed or unrecognised visibility check is reported as public, so
the gate fails toward asking. A failed call or any other last line counts as
`public-no-policy`.

- **`private`** (the repo is private or internal): there is no public disclosure
  exposure, but still resolve every carry-over group regardless of visibility, since a
  private repo never becomes public enough later to re-trigger the pre-flight. Tell the
  user the repo has no public exposure, then ask once per carry-over group whether to
  file it now (from its Step 1 capture) or drop it from `withheld=`. Only once every
  carry-over group is resolved (or there were none), skip to "Choose an issue body
  template".
- **`public-policy <path>`**: read the policy and quote its private reporting
  instructions (advisory link, email, form) in the Withhold option of the question
  below, so withholding is not the only path to disclosure.
- **`public-no-policy`**: ask the same question without a policy quote.

For both public verdicts, the verdict changes the wording of the question, never
whether it is asked. A group needs the three-way question when it is
**security-sensitive** (any finding came from Agent A's security architecture
analysis or scored against the Security dimension, or any Issue/Evidence text names a
concrete exploit path such as a specific request, payload or call sequence), plus every
carry-over group Step 1 surfaced that is still unresolved. A carry-over group with no
Phase B group (its area was not re-analyzed) is asked anyway, from its Step 1 capture.

For each such group, show its findings and ask the user to choose exactly one:
1. **File as-is**: publish with full technical detail.
2. **File neutrally**: publish, but drop the exploit path and evidence (keep the "what"
   and "why it matters", not the "how to trigger it").
3. **Withhold**: file no issue this run. Record the group in `withheld=`; its findings
   stay in the report body and in every stated total.
4. **Drop** (carry-over groups only; this run's own security-sensitive groups keep the
   three-way choice, since a group just found cannot be waved off as resolved): the
   group is no longer relevant or was handled out of band. Remove it from `withheld=`
   without filing, and name it in the run summary. It is not re-surfaced on a future
   run.

If the choice is "Withhold" or "File neutrally", check the Step 1 report path verdict
before accepting it. These choices leave the exploit detail only in
`docs/HEALTH-AUDIT.md`, so any `unsafe-*` verdict means it is one commit from public
exposure. Phase A has already written the full report to that path, so the remedy is
remediation, not prevention. Stop, have the user resolve the path, and clean up the
written copy before continuing:
- `unsafe-tracked`: `git rm --cached docs/HEALTH-AUDIT.md` and add it to `.gitignore`.
  The working copy stays on disk for Phase C.
- `unsafe-untracked`: add `docs/HEALTH-AUDIT.md` to `.gitignore`.
- `unsafe-not-a-git-repo`: relocate the report outside the project's distribution
  surface and delete the copy at the old path once the move is confirmed.

"File as-is" needs no remediation. Re-ask the group's question only once the path is
safe or "File as-is" was picked. Record each group's choice for "Create the issues".

With **no security-sensitive or carry-over group on a public repo**, still ask one
confirmation to file all groups normally. A public repo never skips this gate.

### Choose an issue body template

Take the first that applies:

1. **`.github/ISSUE_TEMPLATE/*.md`**: if a template already has `## Detailed
   Description`, an `## Acceptance Criteria` section with a literal `- [ ]`, and an
   out-of-scope-style section (`## Out of Scope`, `## Constraints`), reuse its heading
   names and label.
2. **The `issue-template` skill**, if available.
3. **The canonical structure below**, the default for a portable project. It is what
   `compute_quality_score()` scores.

```markdown
## Objective
<1-2 sentence description of what this group achieves>

## Detailed Description
<Current behavior, desired behavior, and the mechanism between them, built from the
group's findings. For each finding name its file(s), the concrete issue, and why it
matters; cite severity inline, e.g. "[P1] ...".>

## Acceptance Criteria
- [ ] <Binary, testable outcome derived from the findings>
- [ ] All existing tests pass (`{{ check_cmd }}`)
- [ ] No new lint warnings (`{{ lint_cmd }}`)

## Files / Modules to Change
- [ ] `path/to/file1.py`: <what changes there>
- [ ] `path/to/file2.py`: <what changes there>

## Out of Scope / Constraints
- <Anything explicitly not covered by this group>

## Dependencies
<Links to prerequisite group issues, or "None">

## References
- Source: `docs/HEALTH-AUDIT.md` (Health Audit {date})
```

For a "file neutrally" group, omit the exploit path or payload from "Detailed
Description" and "Acceptance Criteria": describe the class of weakness and the fix.

### Pre-flight checks before creating issues

1. Run `gh issue list --state open --limit 1000` to check for duplicates.
2. Present the proposed issues (titles, labels, milestone, and the disclosure choice for
   any security-sensitive group) and wait for explicit user confirmation.
3. After creation, report all issue numbers and which groups were filed neutrally or
   withheld.

### Create the issues

For each Phase B group, plus every carry-over group the user chose to file (by either
the public three-way question or the private two-way one), whose choice is not
"withhold" or "drop", create an issue from the chosen template. A carry-over group
with no Phase B group is rendered from its Step 1 capture.

```bash
gh issue create --title "<type>(scope): <group description>" \
  --milestone "<milestone from Step 7>" \
  --label "<label1>" --label "<label2>" \
  --body "$(cat <<'EOF'
<rendered template from "Choose an issue body template", for this group>
EOF
)"
```

Then update the Action Plan table with issue numbers and links, update `issues=`, and
rewrite `withheld=` per its Phase C stage. A carry-over group the user chose to file
must gain an issue and drop out of `withheld=` in the same write, never one without the
other. A dropped group gets neither. Give each withheld group's Action Plan row a
`withheld` status with no issue link.

### Add issues to the project board

```bash
sova board add <issue-url> [<issue-url> ...]
```

It prints one line per URL (`added` or `failed (reason)`) and exits non-zero when any
URL failed. A `board:` line names the board used (with its title when known). With no
configured board it uses the owner's only board. Act on what it prints:

- **`skip: ...`**: skip board membership and say why.
- **`failed` with a missing project scope**: report it. If the line says to run
  `gh auth refresh -s project`, run it, then re-run `sova board add` with the failed
  URLs. If it names a token variable such as `GH_TOKEN`, `gh auth refresh` refuses to
  run, so ask for a token with the `project` scope instead.
- **`failed` with a missing read:org scope**: report it, ask for a token with `read:org`,
  and skip the board for this run.
- **Any other `failed`**: report it as "could not add to board", naming the owner and
  project the line names. Do not retry automatically.
- **A `failed:` line with no URL** is board-level: follow the bullet above its text
  matches. Any other non-zero exit with no per-URL line: report it and skip the board.

## Phase D: Verify

After creating issues, verify:
- Each issue is on the **resolved** board, not merely some board. `projectItems` holds
  each item's project title but no number, so check membership by the board's title:
  ```bash
  gh issue view <n> --json projectItems --jq '[.projectItems[].title]'
  ```
  Take the title from the `board:` line, else run `gh project view <number> --owner
  "$(gh repo view --json owner --jq '.owner.login')" --format json --jq '.title'` with the
  `board:` line's number or `sova config get task_source.github_project_number`.
  Skip this check when `sova board add` printed `skip`. An empty
  list, or one without that title, means not a member. A command failure is reported as
  "could not verify board membership", never read as "not on the board".
- Labels, milestones and dependencies are correct.
- Every "withhold" group has no issue and is listed in `withheld=`.
- Every "file neutrally" issue body has no exploit-path specifics.
- Every carry-over group ended in exactly one of three states: filed (has an issue, no
  longer in `withheld=`), still withheld (no issue, still in `withheld=`), or explicitly
  dropped (chose "Drop"; no issue, absent from `withheld=`, named in the run summary).
  A group in none of them is a bug: report it rather than resolving it either way.

## Constraints
- Do NOT fabricate findings. If unsure an issue exists, say so and name what you would
  check.
- Do NOT pad the report with generic advice. Ground every finding in code you can see,
  with file paths and line references where possible.
- Do NOT soften critical findings. DO acknowledge genuinely good patterns.
- Keep the report actionable: 30 well-prioritized findings beat a 200-finding dump.
- Maximum 10 task groups. Before creating issues, show the grouping and **ask for
  confirmation**.
