---
name: health-audit
description: Deep technical health audit -- architecture, modules, files, functions. Scored, prioritized, actionable.
user-invocable: true
category: core
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
- `{{ test_cmd }}` for tests
- `{{ lint_cmd }}` for linting
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
