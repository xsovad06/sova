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

### Step 1: Discover the project

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

### Step 2: Walk the codebase

Systematically walk each top-level module, app, or package:
- If a focus area was specified, go deeper on that area.
- Otherwise, cover all modules proportionally to their size and complexity.
- Cross-reference what is documented against what actually exists.

### Step 3: Run checks

Run the project's test and lint commands to verify current health:
- `make test` for tests
- `make lint` for linting
- If neither is configured, look for `Makefile`, `package.json` scripts, or
  CI config to find the right commands.

### Step 4: Produce the report

Use the output format below.

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

### Staleness detection and auto-cleanup

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
- **Labels**: GitHub labels from the project's label taxonomy

Target 5-10 groups. Do not over-fragment -- a group with 1 finding is too small
unless it truly stands alone.

Write the grouping into the "Task Groups" section of `docs/HEALTH-AUDIT.md`
with a dependency diagram showing execution order.

## Phase C: Create GitHub Issues

For each task group from Phase B, create a GitHub issue:

```bash
gh issue create --title "<type>(scope): <group description>" \
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
2. Present the proposed issue list (titles + labels) to the user
3. Wait for explicit user confirmation before creating any issues
4. After creation, report all issue numbers

## Phase D: Generate Wave Prompts

After creating the issues, generate **one self-contained prompt per execution
wave**. A wave is a set of task groups that can be worked on in parallel because
they have no dependencies on each other. Each prompt is designed to be pasted
into a separate Claude Code session (or run as parallel agents).

### Wave structure

Build waves from the dependency graph produced in Phase B:

- **Wave 1**: groups with no dependencies (the critical path root)
- **Wave 2**: groups whose dependencies are all in Wave 1
- **Wave 3**: groups whose dependencies are all in Waves 1-2
- ...continue until all groups are assigned

Groups within the same wave can be run in **parallel sessions**.

### Prompt format

For each wave, generate a single fenced code block containing a prompt that:

1. **Opens with context**: which wave this is, which issues it covers, what was
   already completed in prior waves (if any).
2. **Lists every finding** in the wave's groups with:
   - The issue number and title
   - Each finding's location, issue description, and recommended fix
   - The exact files to modify
3. **Includes verification**: run the project's test and lint commands after all
   changes, ensure 0 test failures and 0 lint warnings.
4. **Ends with commit instructions**: use conventional commits, one commit per
   logical change, no fix-on-fix.

If a wave contains multiple groups that can run in parallel, the prompt should
instruct Claude Code to use the Agent tool to spawn parallel sub-agents -- one
per group -- so all groups in the wave execute concurrently within a single
session.

### Output

Present the wave prompts to the user as numbered, fenced blocks they can
copy-paste:

```
## Wave 1 (sequential prerequisite)

Paste this into a Claude Code session:

\`\`\`
<prompt content>
\`\`\`

## Wave 2 (parallel -- 3 groups)

Paste this into a Claude Code session (spawns parallel agents internally):

\`\`\`
<prompt content>
\`\`\`
```

Also append the wave prompts to `docs/HEALTH-AUDIT.md` under a "## Wave Prompts"
section so they are preserved alongside the findings.

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
