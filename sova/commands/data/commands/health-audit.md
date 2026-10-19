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
software engineer with 20 years of experience who wrote every line from scratch.
You know every design decision, every shortcut, every deferred TODO. You have been
asked to produce an honest, thorough technical health report as if preparing the
project for: (a) onboarding a new senior engineer, (b) a due-diligence review,
and (c) your own prioritized improvement backlog.

Be brutally honest. Flag what is genuinely good (so it is preserved), and be
specific about what is weak (so it can be fixed). Avoid generic advice. Every
finding must reference concrete code, files, or patterns in THIS codebase.

## Procedure

### Step 1: Staleness check

**Report path confidentiality**: before anything else, and regardless of
whether `docs/HEALTH-AUDIT.md` exists yet, check whether writing to it is
safe. A "withhold" or "file neutrally" disclosure choice (Phase C)
deliberately keeps the full exploit path in this file's body: publishing it
through a commit instead of an issue defeats the point of withholding it,
and the gate this check feeds (Phase C's unsafe-path block) has nothing to
consult without it, which is exactly the portable, first-run case this check
exists for. This repo gitignores the file, but the distributable copy of
this command runs in arbitrary projects where `docs/` is normally tracked.
The check below works whether or not the file exists yet (an absent path is
simply untracked and not ignored, unless `docs/` itself is gitignored), so it
does not depend on the "does the file already exist" check further below.

```bash
if git ls-files --error-unmatch docs/HEALTH-AUDIT.md >/dev/null 2>&1; then
  echo "tracked"
elif git check-ignore -q docs/HEALTH-AUDIT.md; then
  echo "ignored"
elif git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "untracked-and-not-ignored"
else
  echo "not-a-git-repo"
fi
```

- **`ignored`**: safe, no action needed.
- **`untracked-and-not-ignored`**: this is the default state of a fresh
  project (no report file yet, and nothing ignoring `docs/`), so do not treat
  it as a hard stop here: it would stop the audit before Step 5's expensive
  fan-out and Step 6's claims ledger have even run, regardless of whether any
  finding this run turns out to be security-sensitive. Warn the user now
  that any future withheld or neutrally-filed exploit detail would land in a
  file git can commit, and suggest adding `docs/HEALTH-AUDIT.md` to
  `.gitignore` before continuing. Record the status as **unsafe** for Phase
  C, which enforces the actual block, and only for a group whose disclosure
  choice turns out to need it.
- **`tracked`**: same deferred treatment as above, record **unsafe**, except
  the eventual fix is not optional: if a group's choice needs confidentiality,
  the file must also be removed from version control, not just gitignored
  going forward (its history would still carry the committed content).
- **`not-a-git-repo`** (not inside a git working tree): state plainly that
  `docs/HEALTH-AUDIT.md` has no git protection in this project at all (there
  is no `.gitignore` to edit). Record **unsafe**; Phase C's block, if it
  fires, must tell the user to pick a different, actually-protected path for
  the report rather than prescribing a `.gitignore` edit.

Carry the recorded safety status (**safe** for `ignored`, **unsafe** for the
other three) into Phase C.

Next, check if `docs/HEALTH-AUDIT.md` already exists. If it does, read the
`audit-tracker` comment and check issue status. Fetch the complete set of
open issues, not just the first page: `gh issue list` defaults to 30 results,
and a tracked issue past that default would be misread as closed:

```bash
gh issue list --state open --limit 1000 --json number --jq '.[].number'
```

A withheld group (tracked in the `withheld=` field, see Phase A) has no issue
number, so it never appears in the "tracked issue numbers" comparison below.
Treat its presence as still-open work in its own right, independent of
`issues=`: never let it be silently carried forward or silently dropped just
because every numbered issue happened to close. The mechanism for that is the
**carry-over group**: a withheld group name that Step 1 passes into this
run's Phase C pre-flight (see "Disclosure pre-flight" below) so it gets
re-asked, not silently resolved either way.

**Carry-over group content**: a name alone is not enough to file a carry-over
group whose area this run does not re-analyze, since Phase C's pre-flight
needs its findings to render an issue body from. Whenever Step 1 surfaces a
carry-over group name below, before anything later overwrites the existing
report, also extract and retain that group's verbatim finding blocks (the
`#### [P{n}] {title}` / **Location**/**Issue**/**Evidence**/**Recommendation**
text under "Findings by Level" in the existing `docs/HEALTH-AUDIT.md`) into
this run's working state, keyed by the slugified group name. Phase C's
pre-flight and "Create the issues" render a carry-over group with no matching
Phase B group from this captured text, not from re-reading the file at that
point: by the time Phase C executes, Phase A has already overwritten it.

Compare against the tracked issue numbers:
- **All issues closed and no `withheld=` field** -> delete the file, inform
  the user that the previous audit cycle is complete, then proceed with the
  new audit as a **full audit**.
- **All issues closed but a `withheld=` field is present** -> the file is not
  yet eligible for the deletion path above. List the withheld group names,
  capture each one's finding blocks as described above, and record them as
  **carry-over groups** for this run: Phase C's pre-flight re-runs the
  disclosure three-way question for each one, on top of whatever new
  security-sensitive groups this run's own analysis produces. Note that
  Phase A rewrites `docs/HEALTH-AUDIT.md` unconditionally this run regardless
  of this branch, so nothing here is actually protecting the file from being
  overwritten: what is protected is the carry-over group's *name and
  captured findings*, which must reach Phase A's new tracking header (as a
  union with this run's own withhold decisions, see Phase A below) so an
  unresolved carry-over group is never silently dropped just because the
  file was regenerated. Then proceed with the new audit as a **full audit**
  (the withheld carry-over does not make this an incremental run; only the
  user explicitly choosing incremental in the branch below does); record
  mode = `full`.
- **Some issues still open** -> warn the user that N issues from the previous
  audit are still open, list them, and ask the user to choose one of:
  - **Full audit**: overwrite the prior report with a fresh full analysis.
  - **Incremental audit**: re-analyze only areas changed since the last audit,
    preserving still-open findings for unchanged areas.
  - **Abort**.

  If a `withheld=` field is also present, additionally list those group names,
  capture each one's finding blocks as described above, and record them as
  carry-over groups for this run's Phase C pre-flight (same handling as the
  branch above), regardless of which audit mode (full/incremental) the user
  picks.

  Record the chosen mode (`full` or `incremental`): Step 4 depends on it.

### Step 2: Discover the project

Build a mental model of the project before analyzing it. Read these in order,
skipping any that do not exist:

1. `AGENTS.md`, `CLAUDE.md`, and all `.claude/rules/*.md`: conventions,
   architecture, run commands.
2. `README.md`: project overview, installation, usage.
3. Architecture docs: look for `docs/architecture.md`, `docs/ARCHITECTURE.md`,
   `ARCHITECTURE.md`, or equivalent.
4. Coding standards: look for `docs/python-standards.md`, `CONTRIBUTING.md`,
   `.editorconfig`, linter configs.
5. `pyproject.toml`, `package.json`, `Cargo.toml`, `go.mod`, or equivalent:
   dependencies, scripts, entry points.
6. Top-level directory listing: understand the project shape.

From this, note:
- **Project name and purpose**
- **Tech stack** (language, framework, database, frontend, infra)
- **Scale** (number of modules/apps, test count, coverage gates)
- **Stage** (prototype, pre-launch, production, mature)
- **Key domains** the code models
- **Architecture patterns** in use (service layer, adapters, DDD, etc.)

Collect codebase stats in parallel:

```bash
# Run these in parallel -- they are independent
# LOC counts (adjust extensions for the project's language)
find . -name "*.py" -not -path "./.venv/*" -not -path "*/__pycache__/*" -not -path "*/migrations/*" -not -path "./.claude/*" | xargs wc -l | tail -1
find . -name "*.html" -not -path "./.venv/*" | xargs wc -l | tail -1
# Module/app count
ls -d apps/*/ 2>/dev/null | wc -l  # Django
ls -d src/*/ 2>/dev/null | wc -l   # generic
# Test count
grep -rc "def test_" tests/ apps/*/tests/ 2>/dev/null | awk -F: '{s+=$2} END {print s}'
```

### Step 3: Run checks

Run the project's test and lint commands to verify current health:
- `{{ test_cmd }}` for tests
- `{{ lint_cmd }}` for linting
- `{{ check_cmd }}` for a single CI-equivalent target, when the project has one

If those targets do not exist, discover the real commands from `Makefile`,
`package.json` scripts, or CI config. Typical shapes by ecosystem:
- Node: `npm test` plus `npm run lint`
- Rust: `cargo test` plus `cargo clippy`
- Go: `go test ./...` plus `golangci-lint run`

### Step 4: Incremental mode check

Use the mode recorded in Step 1. A previous `docs/HEALTH-AUDIT.md` existing is
not itself sufficient to select incremental mode: the user may have chosen
a full audit specifically to overwrite it.

For **incremental audits** (user explicitly chose incremental in Step 1),
determine which areas changed since the last audit:

```bash
# Find the commit closest to the last audit date (from the file header)
git log --after="<last-audit-date>" --oneline --stat | head -100
```

- Re-analyze only modules/apps with changes since the last audit date.
- Preserve findings for unchanged areas from the previous report if they are
  still open issues.
- Still produce the full scorecard and executive summary (these reflect current state).

For **full audits** (no previous audit, all previous issues closed, or the
user explicitly chose to overwrite):
- Proceed with Step 5 as a full analysis, ignoring any prior report.

### Step 5: Parallel codebase walk

This is the most expensive step. Use the **Agent tool to spawn 3 parallel
analysis agents**, each covering a different dimension. This reduces wall-clock
time by ~3x compared to sequential analysis.

**If a focus area was specified** in `$ARGUMENTS`, skip the parallel fan-out.
Instead, analyze only the specified area deeply in the main context. Still
produce all 10 scorecard dimensions but score non-focus areas based on Step 2
discovery only (no deep read). Mark non-focus scores as "(surface-level)" in
the scorecard.

**For full audits**, call the Agent tool 3 times in a single message (so they
run concurrently). Use `subagent_type="general-purpose"` for all three. Include
the project context from Step 2 (name, tech stack, scale, stage) in each prompt.

If any agent fails or times out, proceed with the available results. Mark the
report as partial and note which dimension(s) were not analyzed. Score missing
dimensions as "(not analyzed)" in the scorecard.

#### Agent A: Architecture and Security
Include the project context from Step 2 (tech stack, scale, stage)
and instruct the agent to analyze:
- Dependency graph health (circular imports between modules)
- Data model integrity (schema design, migration hygiene, index coverage)
- Security architecture (auth, input validation, secret handling, injection
  surfaces, file upload safety, CSP, CORS)
- Infrastructure readiness (caching, task queues, connection management, rate
  limiting, monitoring/observability gaps)
- API design consistency (endpoint naming, error responses, versioning)
- Configuration management (secrets handling, environment separation)

Tell it to check concrete files: settings, middleware, models, Dockerfile,
docker-compose, CI config, deployment scripts. Output format: severity
(P0-P4), title, location, issue, evidence, recommendation. Maximum 20 findings.
Also list 3-5 architectural strengths.

#### Agent B: Module Quality and Patterns
Prompt the agent with the project structure and instruct it to analyze:
- Service layer consistency (for each module: is business logic in services or views?)
- God files (files over 500 lines that do too much)
- Dead code (unused imports, unreferenced templates/assets, views not in URLs)
- Pattern consistency (factories in tests, README per module, form validation)
- Type hint coverage (sample key service files)
- Error handling (bare excepts, swallowed exceptions)
- Template quality (business logic in templates, missing i18n)

Tell it to run concrete commands (`wc -l`, `grep`, `find`) to gather evidence.
Output format: same as Agent A. Maximum 20 findings. List 3-5 code quality
strengths.

#### Agent C: Testing, Dependencies, and Operations
Prompt the agent with the project test infrastructure and instruct it to analyze:
- Test quality and coverage gaps (tests per module vs code size, factory usage)
- Dependency health (pinning strategy, known problematic packages, dev vs prod split)
- CI/CD quality (workflow config, pre-commit hooks, coverage reporting)
- Operational readiness (logging patterns, error monitoring, health endpoints,
  backup strategy, management commands)
- Deployment (Dockerfile quality, environment variable handling, migration state)

Tell it to run concrete commands and read specific files. Output format: same
as Agents A and B. Maximum 20 findings. List 3-5 operational strengths.

### Step 6: Synthesize and verify

After the agents complete (or partially fail):

1. **Collect** all findings from the completed agents. If any agent failed,
   note the gap and continue with available results.
2. **Deduplicate**: findings may overlap (e.g., both Agent A and Agent B flag
   the same god file). Merge duplicates, keeping the most detailed evidence.
3. **Verify with a claims ledger**: every finding carries claims (a line count,
   a file path, a function or variable name, a query count, a missing-index
   assertion) that will be published to an issue body an LLM wrote and no one
   has re-checked. A 5-10 item spot-check is not enough: a claim that is never
   re-derived is a guess wearing the evidence format. For every finding, not a
   sample of them, re-derive each concrete number, path, and name before it is
   allowed into the report. Fit this to the main context's budget instead of
   one tool call per claim: three agents at up to 20 findings each can reach
   ~60 findings, and verifying them one claim at a time would be 100-200
   sequential calls in the same context Step 5's fan-out exists to protect.
   - **Order by priority first**: before batching, rank all collected
     findings by (severity * blast radius), reusing the agents' own severity
     labels (P0-P4) rather than inventing a new scale, and verify in that
     descending order. This is not the final Rank in step 5 below, it only
     orders verification work, but it guarantees that if the budget runs out
     partway through, the findings left unverified (and dropped, per the
     budget rule below) are the least severe and smallest-blast-radius ones,
     never a P0 sitting after an already-verified P4.
   - **Batch per file**: group claims by the file they concern and issue one
     targeted `grep`, `wc -l`, or `Read` command per file that answers every
     claim about it at once, instead of one command per claim.
   - **Or delegate**: spawn a dedicated verification subagent, mirroring Step
     5's fan-out, with the collected findings, and have it return a
     corrected-claims table instead of re-deriving sequentially here.
   - A claim that re-derives to the same value: keep it as-is.
   - A claim that re-derives to a different value: correct it to the measured
     value, never the agent's original wording.
   - A claim that is inherently about reading code rather than running it
     (e.g., "this function has no docstring", "this handler has no auth
     check", a code path traced by reading but never actually run): keep it,
     but mark it explicitly with the words "traced in code, not executed", so
     a reader knows it was confirmed by inspection, not by a command.
   - A claim that cannot be re-derived at all in this context (the tool it
     needs is unavailable, the command times out, the referenced file no
     longer exists): drop the claim from the report entirely. Do not publish
     it with a hedge like "unverified" or "approximately": an unverifiable
     claim is not a weaker claim, it is not a claim. If dropping its claims
     leaves a finding with no concrete evidence at all (an empty or
     all-prose **Evidence** field with nothing re-derived to back it), drop
     the finding too, and count it in the dropped-findings tally the Budget
     rule below reports. A finding reduced to an empty Evidence field is not
     publishable.
   - **Budget rule**: if verification cannot be completed for every finding
     before the context runs out, do not silently verify only a prefix and
     call it done. Stop, drop every finding whose claims were not reached
     from both the issue bodies and the report, and state in the report how
     many findings were dropped this way and why (a context-budget limit, not
     a quality judgment on those findings), listing the dropped findings'
     severities (e.g., "dropped 4 findings: 3 P3, 1 P4") so a reader can
     confirm the priority-ordered verification above actually protected the
     higher-severity findings.
4. **Score** the 10 dimensions based on verified findings and strengths.
5. **Rank** findings by (severity * blast radius).

### Step 7: Validate labels

Before grouping findings into issues, validate that the labels you plan to use
actually exist in the project:

```bash
gh label list --limit 1000 --json name --jq '.[].name' | sort
```

Also check for milestones:

```bash
gh api repos/:owner/:repo/milestones --paginate --jq '.[] | "\(.number)\t\(.title)"'
```

Only use labels and milestones that exist. If an appropriate label does not
exist, either create it with `gh label create` or use the closest existing one.
If the target milestone for issue creation does not exist in the list, create it
with `gh api repos/:owner/:repo/milestones -X POST -f title="<name>"` before
Phase C. Verify the milestone number is valid before using `--milestone`.

## Analysis Levels

Produce findings at four levels, top to bottom. Each level should surface issues
the levels above might miss.

### Level 1: Architecture & System Design
- Dependency graph health (circular deps, tight coupling between modules)
- Data model integrity (schema design, migration hygiene, index coverage)
- Security architecture (auth, input validation, secret handling, injection
  surfaces, file upload safety)
- Infrastructure readiness (caching, task queues, connection management, rate
  limiting, monitoring/observability gaps)
- Scalability bottlenecks (N+1 queries, missing pagination, unbounded queries,
  synchronous blocking in async paths)
- Configuration management (secrets handling, environment separation, feature flags)
- API design consistency (endpoint naming, error responses, versioning)

### Level 2: Module / App Health
For each module or app, evaluate:
- Single responsibility: does it do one thing well, or is it a grab bag?
- Interface boundaries: are inter-module imports clean, or do modules reach
  into each other's internals?
- Service layer coverage: is business logic in services, or leaking into views,
  handlers, templates, or model methods?
- Test coverage and quality: are edge cases covered? Are tests testing behavior
  or implementation details?
- Documentation accuracy: do docs match reality?

### Level 3: File-Level Quality
- Files that are too large / do too much (God files)
- Dead code, unused imports, orphaned templates or assets
- Inconsistent patterns across similar files (e.g., one view uses a service
  layer, another has inline logic)
- Configuration drift (settings that shadow each other, duplicated constants)
- Template/view quality (logic in templates that belongs in services)

### Level 4: Function / Class Granularity
- Functions that are too long or have too many responsibilities
- Missing or incorrect type hints
- Error handling gaps (bare excepts, swallowed exceptions, missing validation)
- Naming inconsistencies
- Complex conditionals that should be extracted or simplified
- Docstring accuracy vs actual behavior

## Evaluation Dimensions

Score each dimension 1-10 with a one-line justification:

| Dimension              | What to evaluate                                                   |
|------------------------|--------------------------------------------------------------------|
| **Correctness**        | Bugs, logic errors, race conditions, data integrity risks          |
| **Security**           | OWASP Top 10, auth bypass, injection, data exposure                |
| **Performance**        | Query efficiency, caching, rendering speed, payload sizes          |
| **Maintainability**    | Code clarity, consistent patterns, cognitive complexity            |
| **Testability**        | Coverage quality, test isolation, ease of adding new tests         |
| **Extensibility**      | How hard is it to add a new module, feature, or integration?       |
| **Operability**        | Logging, error reporting, deployment, rollback, monitoring         |
| **Documentation**      | Onboarding path, accuracy, completeness, developer experience      |
| **Code Hygiene**       | Dead code, TODOs, style consistency, dependency freshness          |
| **Production Readiness** | What would block a confident production deploy today?            |

## Severity Classification

Classify every finding:

- **P0 (Critical)**: Blocks production deploy, data loss risk, security
  vulnerability. Must fix before launch.
- **P1 (High)**: Significant maintainability or reliability risk. Fix within the
  current milestone.
- **P2 (Medium)**: Code quality issue that compounds over time. Plan within the
  next 2 milestones.
- **P3 (Low)**: Nice-to-have improvement, style preference, minor inconsistency.
  Address opportunistically.
- **P4 (Note)**: Observation or suggestion. No action required but worth awareness.

## Output Format

### Executive Summary (5-10 sentences)
Overall health assessment. What is the single biggest risk? What is the strongest
aspect? Is this codebase ready for a second developer? For production?

### Scorecard
Table of the 10 dimensions with scores and one-line justifications.

### Findings by Level
Group findings under Level 1-4 headers. Each finding:

```
#### [P{n}] {Short title}
**Location**: {file(s) or module(s)}
**Issue**: {What is wrong and why it matters -- concrete, not generic}
**Evidence**: {Code snippet, query count, or specific example}
**Recommendation**: {Specific action to fix, not "consider improving"}
```

### Strengths (what to preserve)
List 5-10 things done well that a new developer should understand and maintain.

### Prioritized Action Plan
Top 10 findings ranked by (severity * effort-to-fix), with rough effort estimates
(hours/days). This is the "if you only have two weeks" list.

### Onboarding Gap Analysis
If a senior developer joined tomorrow with only the repo and its docs:
- What would they understand immediately?
- What would confuse them?
- What is undocumented but critical to know?
- What would they break on their first PR?

## Phase A: Generate docs/HEALTH-AUDIT.md

The report path's confidentiality was already checked in Step 1 (**safe** or
**unsafe**, carried forward from there). It is not re-checked here: nothing
written in this phase is a withhold/neutral disclosure decision yet (that is
Phase C's pre-flight), so there is nothing here to block on. An **unsafe**
status only turns into an actual block in Phase C, at the point a group's
disclosure choice is "withhold" or "file neutrally" and a safe path still
has not been provided.

After completing the analysis and producing the report above, **write the
full audit to `docs/HEALTH-AUDIT.md`** as a local work artifact (gitignored
in this repo; checked in Step 1 for every other project).

The document must include:
- A tracking header with issue numbers for staleness detection (see below)
- Header with title, date, codebase stats (LOC, test count, lint status)
- All sections from the Output Format above
- A "Task Groups" section (populated in Phase B)
- An "Action Plan" table with issue links (populated in Phase C)

### Tracking header

Add this metadata block at the very top of the file (before the title) so
other commands (`/status`, `/standup`) can detect audit progress:

```markdown
<!-- audit-tracker: issues=68,69,70 withheld=auth-token-storage -->
```

The `issues=` field is a comma-separated list of all GitHub issue numbers
created in Phase C. This enables automated staleness detection.

The `withheld=` field's value is a comma-separated list of **slugified**
group names: lowercase, spaces (and any other run of non-alphanumeric
characters) collapsed to a single hyphen, no commas, e.g.
`withheld=db-session-management,auth-token-storage` for the groups "DB
session management" and "auth-token-storage". A group name (Phase B) is
free-form prose and may contain spaces, so writing it unslugified into a
comma-delimited field would make it unparseable, or silently merge with the
next name, on the very next run's Step 1, the one consumer that must
round-trip this field. Whenever a group name is written into `withheld=` (Phase A
or Phase C), slugify it first. Whenever a carry-over name read back from
`withheld=` is matched against this run's Phase B group names (Step 1,
Phase C's pre-flight), compare case- and hyphen-insensitively (slugify both
sides before comparing) rather than requiring an exact string match.

The `withheld=` field's value has two lifecycle stages, because this phase
(Phase A) runs, and writes the file, before Phase C's disclosure pre-flight
has decided anything: at the moment this phase executes, no "withhold"
choice for this run exists yet, so a definition in terms of this run's
pre-flight choices cannot be satisfied here.

- **Phase A (now)**: write `withheld=` verbatim as the carry-over set Step 1
  surfaced from the previous header, unchanged. This is a fail-safe
  placeholder: since no pre-flight decision exists yet, the only value that
  cannot lose information is the one already known to be unresolved. Omit
  the field only when Step 1 surfaced no carry-over groups.
- **Phase C (after the pre-flight runs)**: rewrite `withheld=` as the union
  of (a) every group whose choice in this run's pre-flight was "withhold"
  and (b) any carry-over group the user still left unresolved (not filed,
  not explicitly dropped) by the end of this run. This is the write that
  actually records this run's decisions; see "Create the issues" below.

Because the field only ever holds the Phase A placeholder or Phase C's
superset of it, an abort, error, or user bail-out between the two writes
cannot drop a carry-over name: at worst it leaves the Phase A value in
place, never an empty one.

It is a peer of `issues=`, not a replacement for it: a run can produce both
fields at once when some groups are filed and others withheld. Omit the
field entirely when nothing is withheld after Phase C's union. Step 1 of the
next run reads it and re-surfaces those groups rather than silently carrying
them forward or dropping them.

A withheld group's findings still count in every total the report states (the
executive summary's finding count, the scorecard, the Prioritized Action
Plan): state the split explicitly, e.g. "14 findings (12 filed, 2 withheld)",
so the report's own math does not quietly understate how much was found while
implying the count is complete.

Format the file as standard Markdown. No frontmatter. No emojis.

## Phase B: Group Findings into Task Batches

Group all findings into **session-sized task batches**: sets of findings that:
1. Touch the same files or module area
2. Can be completed together in a single development session (2-6 hours)
3. Have clear dependency ordering between groups

For each group, define:
- **Group name**: short, descriptive (e.g., "DB session management")
- **Findings**: list of included findings with severity and title
- **Files touched**: concrete file paths
- **Estimated effort**: hours
- **Dependencies**: which groups must be completed first
- **Labels**: GitHub labels from the project's validated label taxonomy (Step 7)

Target 5-10 groups. Do not over-fragment: a group with 1 finding is too small
unless it truly stands alone.

Write the grouping into the "Task Groups" section of `docs/HEALTH-AUDIT.md`
with a dependency diagram showing execution order.

## Phase C: Create GitHub Issues

### Disclosure pre-flight

Run this before the general confirmation below: its per-group decisions change
what that confirmation is confirming. It does not change the ten scoring
dimensions or which findings exist, only how a security-sensitive group is
published.

```bash
gh repo view --json visibility --jq '.visibility'
```

- **Command fails** (no `gh` auth, network error): treat visibility as
  `PUBLIC`. A failed check must fail toward asking, never toward silently
  skipping the gate.
- **`PRIVATE` or `INTERNAL`**: no public disclosure exposure, but this does
  not end the pre-flight unconditionally: any carry-over group Step 1
  surfaced from the previous run's `withheld=` field still has an unresolved
  decision, and skipping it here would leave it stuck permanently, since a
  private/internal repo never becomes "public enough" later to trigger the
  rest of this pre-flight on its own. Before skipping, resolve every
  carry-over group regardless of visibility: tell the user this repo has no
  public disclosure exposure, then ask once per carry-over group whether to
  file it now (using the findings captured for it in Step 1) or drop it from
  `withheld=`. Only once every carry-over group is resolved
  (or there were none), skip the rest of this pre-flight (there is no
  disclosure question left to ask on a private/internal repo) and go to
  "Choose an issue body template" below.
- **`PUBLIC`** (including the failed-check default): continue.

For a public repo, check for a disclosure policy at every location GitHub
itself honours (treat all three missing, or the check failing, the same as
"absent"):

```bash
for p in SECURITY.md .github/SECURITY.md docs/SECURITY.md; do
  test -f "$p" && { echo "present: $p"; break; }
done
```

The result changes the wording of the question asked next, never whether it
is asked. When a policy file is found, read it and quote its private
reporting instructions (a security-advisory link, an email address, a
dedicated form) in the three-way question below, as a path available through
the **Withhold** choice: withholding with a stated private channel means the
finding can still reach the maintainers outside the public tracker, not just
disappear into `withheld=`. When no policy file exists at any of the three
locations, the question proceeds exactly as before.

Identify which groups need the three-way question this run: every one of
Phase B's groups that is **security-sensitive** (a group qualifies if any of
its findings came from Agent A's Security architecture analysis (Level 1
"Security architecture", or scored against the Security dimension), or if any
finding's Issue/Evidence text names a concrete exploit path (a specific
request, payload, or call sequence that triggers it), regardless of which
agent or level produced it; do not invent a new category for this: reuse the
categorization the agents and dimensions already carry), **plus every
carry-over group name Step 1 surfaced from the prior run's `withheld=` field
that the user has not yet resolved**. A carry-over group may have no matching
Phase B group at all (its area was not re-analyzed this run); ask the
three-way question for it anyway, using the findings captured for it in
Step 1.

- **At least one security-sensitive or carry-over group**: for each one, show
  its findings and ask the user to choose exactly one:
  1. **File as-is**: publish the group's issue with full technical detail,
     same as any other group.
  2. **File neutrally**: publish the issue but drop the specific exploit
     path/evidence (keep the "what" and "why it matters", not the "how to
     trigger it"), so the public body does not double as an attack guide.
  3. **Withhold**: do not file an issue for this group in this run. Record
     the group name in the tracking header's `withheld=` field (Phase A);
     its findings stay in the report body and in every total the report
     states. If a disclosure policy was found above, mention its private
     reporting channel when presenting this option, so withholding is not
     the only path to disclosure.
  4. **Drop** (carry-over groups only; this run's own security-sensitive
     groups keep the three-way choice above, since a group this run's own
     analysis just found cannot legitimately be waved off as resolved): this
     carry-over group is no longer relevant, or was handled out of band
     since the previous run. Remove its name from `withheld=` without filing
     an issue, and name it in the run summary so the drop is visible. Unlike
     Withhold, this ends the group's carry-over lifecycle: it is not
     re-surfaced on a future run.

  If this group's choice comes back **"Withhold"** or **"File neutrally"**,
  check the report path safety status recorded in Step 1 before accepting
  it: these two choices are the ones that leave the full exploit detail
  sitting only in `docs/HEALTH-AUDIT.md`'s body rather than published
  elsewhere, so an **unsafe** status (tracked, untracked-and-not-ignored, or
  not a git repo) means that detail is one commit away from public exposure.
  Phase A has already written the full report, exploit detail included, to
  that unsafe path by the time this check runs (it runs before this
  pre-flight), so the remedy here is remediation of an existing exposure, not
  prevention of one. On **unsafe**, stop and have the user resolve the path,
  then clean up the already-written copy before continuing, per the status
  recorded in Step 1:
  - **`tracked`**: `git rm --cached docs/HEALTH-AUDIT.md` and add it to
    `.gitignore`. The working copy on disk still has the content (so Phase C
    can keep reading it), but it is no longer staged for a commit.
  - **`untracked-and-not-ignored`**: add `docs/HEALTH-AUDIT.md` to
    `.gitignore`. Nothing is staged yet, so there is no history to clean.
  - **`not-a-git-repo`**: relocate the report to a path outside the project's
    distribution surface, and delete the copy at the old, exposed path once
    the move is confirmed.
  Or choose "File as-is" instead, which carries no added risk (that content
  is going public through the issue regardless) and needs no remediation.
  Re-ask the three-way question for this group only once the path is safe or
  the user has picked "File as-is".

  Record each group's choice. It determines how "Create the issues" below
  handles that group.
- **Zero security-sensitive or carry-over groups on a public repo**: still
  ask a single confirmation question (e.g. "This repo is public and none of
  the findings look security-sensitive; proceed with filing all groups
  normally?") before continuing. A public repo never skips this gate
  entirely, even with no disclosure policy file and no sensitive group: that
  is the floor, not an exemption.

### Choose an issue body template

Resolve the issue body template in this order, taking the first that applies:

1. **`.github/ISSUE_TEMPLATE/*.md`**: if one of this repo's templates already
   has `## Detailed Description`, an `## Acceptance Criteria` section with a
   literal `- [ ]`, and an out-of-scope-style section (`## Out of Scope`,
   `## Constraints`, or similar), reuse its heading names and label.
2. **The `issue-template` skill**, if available in this session: SOVA-specific,
   produces the same scored structure directly.
3. **The canonical structure below**, when neither of the above is available
   or matches. This is the default for a portable (non-SOVA) project, not a
   last resort: it is exactly what `compute_quality_score()` scores, so a
   filed issue built from it needs no triage rewrite.

Canonical structure (this is what SOVA's own triage quality gate scores; using
it means a filed issue needs no triage rewrite):

```markdown
## Objective
<1-2 sentence description of what this group achieves>

## Detailed Description
<The current behavior, the desired behavior, and the mechanism that gets from
one to the other, built from the group's findings. For each finding, name its
file(s), the concrete issue, and why it matters; cite severity inline, e.g.
"[P1] ...".>

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
- Source: `docs/HEALTH-AUDIT.md` -- Health Audit {date}
```

When a group's disclosure choice is "file neutrally", omit the specific
exploit path or payload from "Detailed Description" and "Acceptance Criteria":
describe the class of weakness and the fix, not the trigger.

### Pre-flight checks before creating issues

1. Run `gh issue list --state open --limit 1000` to check for duplicates
2. Present the proposed issue list (titles + labels + milestone + the
   disclosure choice for any security-sensitive group) to the user
3. Wait for explicit user confirmation before creating any issues
4. After creation, report all issue numbers, and which groups (if any) were
   filed neutrally or withheld

### Create the issues

For each task group from Phase B, plus every carry-over group the user chose
to file in the pre-flight above (by either the public three-way question or
the private/internal two-way one), whose disclosure choice is not "withhold"
or "drop", create a GitHub issue using the template chosen above. A
carry-over group with no matching Phase B group (its area was not
re-analyzed this run) has no fresh findings to build a body from: render its
template from the findings captured for it in Step 1, exactly as the
pre-flight presented them.

```bash
gh issue create --title "<type>(scope): <group description>" \
  --milestone "<milestone from Step 7>" \
  --label "<label1>" --label "<label2>" \
  --body "$(cat <<'EOF'
<rendered template from "Choose an issue body template", for this group>
EOF
)"
```

After creating all issues, update the Action Plan table in `docs/HEALTH-AUDIT.md`
with the issue numbers and links, update the tracking header's `issues=`
field, and rewrite `withheld=` per its Phase C lifecycle stage (see "Tracking
header" under Phase A above): the union of every group marked "withhold" this
run and any carry-over group still left unresolved (not filed, not
explicitly dropped). A carry-over group the user chose to file is resolved,
not left unresolved: it must gain an issue above and drop out of `withheld=`
in the same write, never one without the other. A carry-over group the user
chose to drop is also resolved, but with neither an issue nor a `withheld=`
entry: see "Create the issues" above and Phase D below. Give each withheld
group's Action Plan row a `withheld` status with no issue link rather than
leaving the row blank.

### Add issues to the project board

The board belongs to the **repository owner that `gh` resolves as the base
repo**, which is not necessarily the authenticated `github_user` that opens
issues and PRs (in this repo, the board owner is `xsovad06`, not the
`dsova06` account issues are filed under), and in a fork workflow is not
necessarily the remote the agent is operating against either: `gh repo view`
reports whichever repo `gh` picks as upstream/base, which may be the fork's
owner rather than the original project's, if that is how `gh`'s own remote
resolution (or `GH_REPO`/`gh repo set-default`) is configured in this
checkout. Resolve it once and reuse it everywhere `<repo-owner>` appears
below:

```bash
gh repo view --json owner --jq '.owner.login'
```

```bash
sova config get task_source.github_project_number 2>/dev/null
```

- **A printed value other than `0`**: that is the board number. (A value of
  exactly `0` is `TaskSourceConfig.github_project_number`'s default, meaning
  "not configured"; an unknown key makes `sova config get` exit 1 with no
  output, which belongs to the fallback branch below, not this one.) Add each
  issue created above:
  ```bash
  gh project item-add <number> --owner <repo-owner> --url <issue-url>
  ```
  - **Fails on a missing scope**: a token can lack `project` or `read:org`,
    and the two need different remedies, not one shared hint.
    - **Missing `project` scope**: report the failure and run
      `gh auth refresh -s project`, then retry `item-add`. If `GH_TOKEN` is
      set in the environment, `gh auth refresh` refuses to run while it
      supplies auth; in that case say so and ask for a replacement token
      with the `project` scope instead.
    - **Missing `read:org` scope**: `gh`'s own owner-type resolution inside
      `project item-add` can demand `read:org` even to add an item to a
      user-owned board (this repo's board is owned by `xsovad06`, a user,
      not an org). Falling back to the GraphQL mutation with an explicit
      `user(login:$owner)` query skips that resolution step entirely, so it
      works without `read:org` for a user-owned board. The mutation itself
      (`addProjectV2ItemById` is a write) still needs the `project` scope,
      so this fallback does not help a token missing that scope instead. For
      a genuinely **org-owned** board, swap in `organization(login:$owner)`:
      that variant performs the same org-scoped read `item-add` was blocked
      on, so it needs `read:org` too, and the only real fix there is a token
      carrying that scope.
      ```bash
      if ! PROJECT_ID=$(gh api graphql -f query='
        query($owner:String!,$number:Int!){
          user(login:$owner){ projectV2(number:$number){ id } }
        }' -f owner="<repo-owner>" -F number=<number> \
        --jq '.data.user.projectV2.id // empty'); then
        echo "could not resolve project id"
        PROJECT_ID=""
      fi
      if ! ISSUE_ID=$(gh api graphql -f query='
        query($owner:String!,$name:String!,$num:Int!){
          repository(owner:$owner,name:$name){ issue(number:$num){ id } }
        }' -f owner="<repo-owner>" -f name="<repo-name>" -F num=<issue-number> \
        --jq '.data.repository.issue.id // empty'); then
        echo "could not resolve issue id"
        ISSUE_ID=""
      fi
      if [ -z "${PROJECT_ID:-}" ] || [ -z "${ISSUE_ID:-}" ]; then
        echo "could not add to board: missing project or issue node id"
      else
        gh api graphql -f query='
          mutation($project:ID!,$item:ID!){
            addProjectV2ItemById(input:{projectId:$project, contentId:$item}){ item { id } }
          }' -F project="$PROJECT_ID" -F item="$ISSUE_ID"
      fi
      ```
      `<repo-name>` is the bare repo name, from
      `gh repo view --json name --jq '.name'`. On a GraphQL error, `gh api
      graphql --jq ...` exits non-zero but still prints the raw error
      response to stdout, which the `if !` capture would otherwise leave
      sitting in `PROJECT_ID`/`ISSUE_ID`: the explicit `PROJECT_ID=""` /
      `ISSUE_ID=""` in each failure branch (not just the echo) is what makes
      the emptiness check below actually catch that case, on top of the
      `// empty` jq fallback for a null-without-error result. Treat a failed
      capture or an empty id as "could not add to board", reported to the
      user and not retried automatically: retrying the same unchecked
      command only reproduces the same misleading "Could not resolve to a
      node" error instead of the real cause.
  - **Fails for any other reason** (the configured project number does not
    exist for the resolved `<repo-owner>`, the issue is already on the
    board, or any other `item-add` error that is not a `project`/`read:org`
    scope message): this is not a scope problem and neither remedy above
    applies. Report it as "could not add to board" and name the resolved
    owner and project number in that message (per the note above, the owner
    `gh` resolved may be a fork owner rather than the upstream project's),
    so a mismatch between the configured board and the resolved owner is
    visible rather than being misread as a scope issue.
- **`sova config` unavailable** (not a SOVA project) **or the value is `0`**
  (disabled): fall back to discovering the board directly:
  ```bash
  gh project list --owner <repo-owner> --limit 1000 --format json
  ```
  - Exactly one project returned: use it.
  - Zero projects, or more than one with no unambiguous match: skip board
    membership for this run and state the reason (no board configured for
    this project, or N candidate boards found with no way to choose). Do not
    guess.

## Phase D: Verify

After creating issues, verify:
- Each created issue appears on the **resolved** project board (when one was
  found above), not merely on some board: `gh issue view --json projectItems`
  returns each item as `{"status": ..., "title": <project title>}` with no
  project number, so a non-empty result alone only proves the issue sits on
  *some* board, which could be a different project or one an unrelated
  automation added it to. Resolve the target board's title alongside its
  number (already in the `gh project list --format json` output from "Add
  issues to the project board" above, or via
  `gh project view <number> --owner <repo-owner> --format json --jq '.title'`),
  then check membership by title for every issue number:
  ```bash
  gh issue view <n> --json projectItems --jq '[.projectItems[].title]'
  ```
  Confirm the resolved board's title is present in that list. An empty list,
  or a list that does not contain that title, means not a board member yet.
  A command failure (e.g., a missing `read:org` scope on this query too) is
  reported as "could not verify board membership", never read as "not on the
  board": the same fail-toward-asking rule as the disclosure pre-flight's
  visibility check, not a silent pass.
- They have correct labels, milestones, and dependencies
- Every group marked "withhold" has no corresponding issue and is listed in
  the tracking header's `withheld=` field
- Every group marked "file neutrally" has no exploit-path specifics in its
  published issue body
- Every carry-over group ended the run in exactly one of three states: filed
  (chosen to file in the pre-flight, by either three-way question; has a
  corresponding issue and no longer appears in `withheld=`), still withheld
  (no issue, still listed in `withheld=`), or explicitly dropped (chosen
  "Drop" in the pre-flight; no issue, absent from `withheld=`, and named in
  the run summary so the drop is visible). A carry-over group in none of
  these three states is a bug: report it rather than silently resolving it
  either way.

## Constraints
- Do NOT fabricate findings. If you are unsure whether an issue exists, say so and
  explain what you would check.
- Do NOT pad the report with generic best-practice advice. Every finding must be
  grounded in code you can see.
- Do NOT soften critical findings. If something is broken, say it is broken.
- DO acknowledge genuinely good patterns. This is as important as finding problems.
- Provide file paths and line references where possible.
- Keep the total report actionable. A 200-finding dump is less useful than 30
  well-prioritized ones.
- Maximum 10 task groups. Each must be self-contained (a developer can pick it up cold).
- Before creating issues, show the proposed grouping and **ask for confirmation**.
