---
name: audit-alignment
description: Audit alignment between GitHub Issues, project board, VISION.md, and actual implementation state.
user-invocable: true
---

# Project Alignment Audit

Verify that the project board, issue tracker, vision document, and codebase are consistent and up to date.

**Focus area**: $ARGUMENTS

## Role

You are a project manager auditing the SOVA project for consistency across its
planning artifacts (GitHub Issues, project board, VISION.md) and actual
implementation state. Your job is to find drift: issues that are done but open,
milestones that should be closed, vision sections that describe planned features
as if they don't exist yet when they do, stale naming ("PAK" instead of "SOVA"),
and any other inconsistencies.

Be thorough and specific. Every finding must reference a concrete issue number,
milestone name, file path, or line range.

## Phase 1: Gather State

Run all of these in parallel to build a complete picture.

### 1a. GitHub Issues

```bash
gh issue list --state all --limit 100 --json number,title,state,labels,milestone,createdAt,closedAt,body
```

Extract:
- All **open** issues: number, title, labels, milestone, age (days since creation)
- All **closed** issues with `status: superseded` label (just count them)
- Any closed issue that references "PAK" in title or body

### 1b. Project Board

```bash
gh api graphql -f query='query { user(login:"xsovad06") { projectV2(number:2) { items(first:50) { nodes { content { ... on Issue { number title state } } order: fieldValueByName(name:"Priority Order") { ... on ProjectV2ItemFieldNumberValue { number } } phase: fieldValueByName(name:"Phase") { ... on ProjectV2ItemFieldSingleSelectValue { name } } status: fieldValueByName(name:"Status") { ... on ProjectV2ItemFieldSingleSelectValue { name } } } } } } }'
```

Extract:
- Board items where issue is CLOSED but board status is not "Done"
- Board items where issue is OPEN but board status is "Done"
- Items with no Priority Order assigned
- Phase distribution (count per phase)

### 1c. Milestones

```bash
gh api repos/xsovad06/sova/milestones --jq '.[] | {title, state, open_issues, closed_issues}'
```

Flag milestones that have 0 open issues but are still in "open" state.

### 1d. Codebase Metrics

Collect current counts to cross-reference against documentation:

```bash
# Test count
find tests/ -name "test_*.py" -exec grep -c "def test_" {} + | awk -F: '{sum+=$2} END {print sum}'

# Command count
ls commands/*.md | wc -l

# Dashboard services
ls sova/dashboard/services/*.py | grep -v __init__ | wc -l

# Dashboard routers
ls sova/dashboard/routers/*.py | grep -v __init__ | wc -l

# Dashboard templates
ls sova/dashboard/templates/*.html | wc -l

# Core steps
ls sova/core/steps/*.py | grep -v __init__ | wc -l

# Adapter methods (ABC)
grep -c "async def \|def " sova/adapters/base.py

# CLI subcommands
grep -c "app\.\|typer\." sova/cli/app.py

# Personas
ls personas/*.md | wc -l

# Adapters implemented
ls sova/adapters/*.py | grep -v __init__ | grep -v base | grep -v factory
```

### 1e. Vision Document

Read `docs/VISION.md` fully. Note:
- The "Last updated" date
- The "Status" line
- Every section that says "Planned", "New", "TODO", or similar
- Any reference to "PAK" (should be "SOVA")
- Numeric claims (step counts, service counts, adapter counts)

### 1f. Documentation Cross-Check

Read `AGENTS.md` and `.claude/rules/architecture.md`. Note any numeric claims
(test count, service count, step count, CLI subcommand list) and compare against
the codebase metrics from step 1d.

## Phase 2: Analyze

For each category, classify findings by severity:

- **STALE**: factually wrong or outdated (e.g., "11-step workflow" when it's 13)
- **DRIFT**: planning artifact doesn't match implementation (e.g., milestone open with 0 issues)
- **NAMING**: old name "PAK" used instead of "SOVA"
- **MISSING**: something exists in code but isn't tracked/documented
- **ORPHAN**: something is tracked but no longer exists in code

### 2a. Issue Relevance Check

For each open issue, assess:
1. Is the work described already done? (check codebase for the feature)
2. Is the issue body still accurate? (naming, counts, references)
3. Is the milestone still appropriate?
4. Are labels current?

### 2b. Vision-vs-Reality Check

For each VISION.md section:
1. Does the described architecture match the actual directory structure?
2. Do numeric claims match codebase metrics?
3. Are "Planned" items still planned, or have they been implemented?
4. Are phase statuses accurate?

### 2c. Doc Count Drift

Compare AGENTS.md and architecture.md numeric claims against actual counts from
Phase 1d. Flag any that are off by more than 10%.

## Phase 3: Report

Present findings in this format:

### Executive Summary

One paragraph: overall alignment health (good/fair/poor), top 3 issues.

### Issues Audit

| # | Title | Finding | Severity | Recommended Action |
|---|-------|---------|----------|--------------------|

### Milestones Audit

| Milestone | State | Open/Closed | Finding | Action |
|-----------|-------|-------------|---------|--------|

### Vision Alignment

| Section | Line(s) | Finding | Severity |
|---------|---------|---------|----------|

### Documentation Drift

| File | Claim | Actual | Delta |
|------|-------|--------|-------|

### Recommended Actions

Numbered list, ordered by priority:
1. Quick fixes (close issues, close milestones, rename titles)
2. Content updates (issue bodies, vision sections)
3. Structural changes (milestone reorganization, new issues needed)

## Phase 4: Fix (Optional)

If `$ARGUMENTS` contains "fix" or "auto-fix":

1. **Close completed issues** that should be closed
2. **Close empty milestones** via GitHub API
3. **Update issue titles** to replace "PAK" with "SOVA"
4. **Update issue bodies** with corrected counts and naming
5. **Update VISION.md** with accurate state

For each fix, report what was changed. Confirm with the user before making
GitHub API writes (issue closes, milestone closes, title edits).

If `$ARGUMENTS` does not contain "fix", just report findings without making changes.

## Cross-References

- **Project board browsing**: Run `/find-task`
- **Daily status**: Run `/standup`
- **Full codebase audit**: Run `/health-audit`
- **Vision document**: `docs/VISION.md`
- **Architecture reference**: `.claude/rules/architecture.md`

## Rules

- NEVER use emojis in any output
- Always show concrete evidence (issue numbers, line numbers, exact counts)
- Compare against actual codebase state, not documentation claims
- Flag stale naming ("PAK" -> "SOVA") in every artifact checked
- When recommending issue closure, verify the feature exists in code first
