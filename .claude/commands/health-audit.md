---
name: health-audit
description: Deep technical health audit -- architecture, modules, files, functions. Scored, prioritized, actionable.
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

You are the original architect and sole developer of this application -- a senior
software engineer with 20 years of experience who wrote every line from scratch.
You know every design decision, every shortcut, every deferred TODO. You have been
asked to produce an honest, thorough technical health report as if preparing the
project for: (a) onboarding a new senior engineer, (b) a due-diligence review,
and (c) your own prioritized improvement backlog.

Be brutally honest. Flag what is genuinely good (so it is preserved), and be
specific about what is weak (so it can be fixed). Avoid generic advice -- every
finding must reference concrete code, files, or patterns in THIS codebase.

## Procedure

### Step 0: Benchmark start

```bash
bash .claude/benchmark/log.sh "health_audit_start" "" "" 2>/dev/null || true
```

### Step 1: Staleness check

Before generating a new audit, check if `docs/HEALTH-AUDIT.md` already exists.
If it does, read the `audit-tracker` comment and check issue status:

```bash
gh issue list --state open --json number --jq '.[].number'
```

Compare against the tracked issue numbers:
- **All issues closed** -> delete the file, inform the user that the previous
  audit cycle is complete, then proceed with the new audit.
- **Some issues still open** -> warn the user that N issues from the previous
  audit are still open, list them, and ask whether to proceed with a fresh
  audit (which will overwrite) or abort.

### Step 2: Discover the project

Build a mental model of the project before analyzing it. Read these in order,
skipping any that do not exist:

1. `AGENTS.md`, `CLAUDE.md`, and all `.claude/rules/*.md` -- conventions,
   architecture, run commands.
2. `README.md` -- project overview, installation, usage.
3. Architecture docs -- look for `docs/architecture.md`, `docs/ARCHITECTURE.md`,
   `ARCHITECTURE.md`, or equivalent.
4. Coding standards -- look for `docs/python-standards.md`, `CONTRIBUTING.md`,
   `.editorconfig`, linter configs.
5. `pyproject.toml`, `package.json`, `Cargo.toml`, `go.mod`, or equivalent --
   dependencies, scripts, entry points.
6. Top-level directory listing -- understand the project shape.

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

Run the project's test and lint commands to verify current health. Discover
the right commands by checking `Makefile`, `package.json` scripts, or CI
config. Common commands by project type:
- Python: `make check`, or `make test` + `make lint`
- Node: `npm test` + `npm run lint`
- Rust: `cargo test` + `cargo clippy`

### Step 4: Incremental mode check

If a previous `docs/HEALTH-AUDIT.md` exists and the user chose to proceed
(Step 1), determine which areas changed since the last audit:

```bash
# Find the commit closest to the last audit date (from the file header)
git log --after="<last-audit-date>" --oneline --stat | head -100
```

For **incremental audits**:
- Re-analyze only modules/apps with changes since the last audit date.
- Preserve findings for unchanged areas from the previous report if they are
  still open issues.
- Still produce the full scorecard and executive summary (these reflect current state).

For **full audits** (no previous audit, or user chose to overwrite):
- Proceed with Step 5 as a full analysis.

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
2. **Deduplicate** -- findings may overlap (e.g., both Agent A and Agent B flag
   the same god file). Merge duplicates, keeping the most detailed evidence.
3. **Verify** -- spot-check 5-10 key findings with targeted `grep`, `wc -l`, or
   `Read` commands. Confirm file sizes, line counts, missing indexes, etc.
   Discard any finding that cannot be verified.
4. **Score** the 10 dimensions based on verified findings and strengths.
5. **Rank** findings by (severity * blast radius).

### Step 7: Validate labels

Before grouping findings into issues, validate that the labels you plan to use
actually exist in the project:

```bash
gh label list --limit 100 --json name --jq '.[].name' | sort
```

Also check for milestones:

```bash
gh api repos/:owner/:repo/milestones --jq '.[] | "\(.number)\t\(.title)"'
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
- Single responsibility -- does it do one thing well, or is it a grab bag?
- Interface boundaries -- are inter-module imports clean, or do modules reach
  into each other's internals?
- Service layer coverage -- is business logic in services, or leaking into views,
  handlers, templates, or model methods?
- Test coverage and quality -- are edge cases covered? Are tests testing behavior
  or implementation details?
- Documentation accuracy -- do docs match reality?

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

- **P0 -- Critical**: Blocks production deploy, data loss risk, security
  vulnerability. Must fix before launch.
- **P1 -- High**: Significant maintainability or reliability risk. Fix within the
  current milestone.
- **P2 -- Medium**: Code quality issue that compounds over time. Plan within the
  next 2 milestones.
- **P3 -- Low**: Nice-to-have improvement, style preference, minor inconsistency.
  Address opportunistically.
- **P4 -- Note**: Observation or suggestion. No action required but worth awareness.

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

After completing the analysis and producing the report above, **write the full
audit to `docs/HEALTH-AUDIT.md`** as a local work artifact (gitignored).

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
<!-- audit-tracker: issues=68,69,70 -->
```

The `issues=` field is a comma-separated list of all GitHub issue numbers
created in Phase C. This enables automated staleness detection.

Format the file as standard Markdown. No frontmatter. No emojis.

## Phase B: Group Findings into Task Batches

Group all findings into **session-sized task batches** -- sets of findings that:
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

Target 5-10 groups. Do not over-fragment -- a group with 1 finding is too small
unless it truly stands alone.

Write the grouping into the "Task Groups" section of `docs/HEALTH-AUDIT.md`
with a dependency diagram showing execution order.

## Phase C: Create GitHub Issues

For each task group from Phase B, create a GitHub issue:

```bash
gh issue create --title "<type>(scope): <group description>" \
  --milestone "<milestone from Step 7>" \
  --label "<label1>" --label "<label2>" \
  --body "$(cat <<'EOF'
## Objective

<1-2 sentence description of what this group achieves>

## Findings

<For each finding in the group:>

### [P{n}] {title}
- **Location**: {files}
- **Issue**: {description}
- **Recommendation**: {action}

## Files to Modify

- `path/to/file1.py`
- `path/to/file2.py`

## Estimated Effort

{N} hours

## Dependencies

{Links to prerequisite group issues, or "None"}

---
Source: `docs/HEALTH-AUDIT.md` -- Health Audit {date}
EOF
)"
```

After creating all issues, update the Action Plan table in `docs/HEALTH-AUDIT.md`
with the issue numbers and links.

### Pre-flight checks before creating issues

1. Run `gh issue list --state open --limit 50` to check for duplicates
2. Present the proposed issue list (titles + labels + milestone) to the user
3. Wait for explicit user confirmation before creating any issues
4. After creation, report all issue numbers

## Phase D: Verify and Log

After creating issues, verify the new issues appear on the project board and have
correct labels, milestones, and dependencies.

Log completion and cost:

```bash
bash .claude/benchmark/log.sh "health_audit_complete" "" "" 2>/dev/null || true
```

On any unrecoverable error at any step (agent spawn failure, `gh` command error,
etc.), log the failure before stopping:

```bash
bash .claude/benchmark/log.sh "health_audit_failed" "" "" 2>/dev/null || true
```

## Constraints
- Do NOT fabricate findings. If you are unsure whether an issue exists, say so and
  explain what you would check.
- Do NOT pad the report with generic best-practice advice. Every finding must be
  grounded in code you can see.
- Do NOT soften critical findings. If something is broken, say it is broken.
- DO acknowledge genuinely good patterns -- this is as important as finding problems.
- Provide file paths and line references where possible.
- Keep the total report actionable. A 200-finding dump is less useful than 30
  well-prioritized ones.
- Maximum 10 task groups. Each must be self-contained (a developer can pick it up cold).
- Before creating issues, show the proposed grouping and **ask for confirmation**.
