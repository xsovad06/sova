---
name: benchmark-report
description: Aggregate benchmark data and generate velocity comparison report.
user-invocable: true
category: management
inputs:
  - arguments (optional filters)
outputs:
  - benchmark_report
---

# Benchmark Report

Aggregate benchmark data from interactive sessions (JSONL logs) and SOVA autonomous runs (DB), then generate a velocity comparison report.

**Arguments**: $ARGUMENTS

## Instructions

### Step 1: Parse Arguments

Extract optional flags from `$ARGUMENTS`:
- `--since YYYY-MM-DD`: only include data from sessions after this date
- `--issues N,N,N`: only include these specific issue numbers (comma-separated)
- `--update-docs`: write the report to `docs/benchmark-results.md` (default: terminal output only)

If `$ARGUMENTS` is empty or contains none of these flags, use all available data and output to terminal only.

### Step 2: Collect Interactive Data (JSONL)

Find all benchmark log files:
```bash
ls .claude/benchmark/issue-*.jsonl 2>/dev/null
```

If no files exist, note "No interactive benchmark data found" and skip to Step 3.

For each file:
1. Extract the issue number from the filename. Files named `issue-null.jsonl` or with non-numeric IDs: count them in the file summary but exclude from per-issue metrics.
2. If `--issues` was specified, skip files whose issue number is not in the list.
3. If `--since` was specified, check the first `session_start` event's `ts` field; skip the file if it predates the cutoff.

For each included issue file, extract metrics using `jq`:

**Wall clock** (total across all sessions):
```bash
jq -s '
  [.[] | select(.event == "session_start") | .ts] | first as $start |
  [.[] | select(.event == "session_end" or .event == "session_summary") | .ts] | last as $end |
  {start: $start, end: $end}
' .claude/benchmark/issue-{N}.jsonl
```
For multi-session issues (multiple `session_start`/`session_end` pairs), sum the durations of each session separately.

**Human idle time**:
```bash
jq -s '
  [.[] | select(.event == "human_idle_start" or .event == "human_idle_end")]
  | [range(0; length; 2) as $i | (.[($i+1)].ts | split("T") | .[1] | split("Z") | .[0]) as $end_t |
     (.[$i].ts | split("T") | .[1] | split("Z") | .[0]) as $start_t |
     {start: .[$i].ts, end: .[($i+1)].ts}]
' .claude/benchmark/issue-{N}.jsonl
```
Compute human idle as the sum of all `human_idle_start` to `human_idle_end` intervals (in minutes). If an odd number of idle events exists (unclosed idle period), ignore the unpaired event.

**Active agent time**: wall clock minus human idle time.

**CI wait time**: sum of intervals from `ci_check_start` to `ci_passed` or `ci_failed`.

**Review rounds**: count of `review_start` events.

**Cost**: look for cost data in this priority order:
1. `session_end` events with `cost_usd` field (from session_end_hook.sh)
2. `session_cost` events
3. `cost_recorded` or `cost` events
Sum all cost values found. If none exist, mark as "N/A".

**Model**: extract from `model_set` events or `session_end` events with a `model` field. If multiple models were used, list them (e.g., "Opus 4.6 / Sonnet 4.6").

**PR number**: extract from `pr_created` events. Parse the `notes` field for patterns like `PR #560`, `pr_number=582`, or just a number.

Collect all extracted metrics into a structured list of interactive issues.

### Step 3: Collect SOVA Autonomous Data (DB)

Check if the database exists:
```bash
ls .claude/sova.db 2>/dev/null
```

If not found, note "No SOVA database found" and skip to Step 4.

Query for completed SOVA developer runs:
```bash
sqlite3 -json .claude/sova.db "
  SELECT tr.issue_number, tr.pr_number, tr.total_cost_usd,
         tr.started_at, tr.ended_at, tr.status
  FROM task_runs tr
  WHERE tr.role = 'developer'
    AND tr.status = 'done'
    AND tr.issue_number IS NOT NULL
  ORDER BY tr.started_at;
"
```

Apply `--since` and `--issues` filters to the results.

Exclude issues that also appear in the interactive JSONL data (same issue number in both sets means it was developed interactively, not autonomously).

For each SOVA issue, gather additional metrics:

**Total cost across all runs for the issue**:
```bash
sqlite3 -json .claude/sova.db "
  SELECT SUM(total_cost_usd) as total_cost
  FROM task_runs
  WHERE issue_number = '{ISSUE}'
    AND status IN ('done', 'failed');
"
```

**Review rounds**:
```bash
sqlite3 -json .claude/sova.db "
  SELECT COUNT(*) as review_rounds
  FROM task_runs
  WHERE issue_number = '{ISSUE}'
    AND role = 'reviewer';
"
```

**CI failures**:
```bash
sqlite3 -json .claude/sova.db "
  SELECT COUNT(*) as ci_failures
  FROM step_executions se
  JOIN task_runs tr ON se.task_run_id = tr.id
  WHERE tr.issue_number = '{ISSUE}'
    AND se.step_name = 'monitor_ci'
    AND se.status = 'failed';
"
```

**Active agent time**: sum of (`ended_at` - `started_at`) across all runs for the issue, in minutes.

**PR cycle time**: time from the earliest developer run's `started_at` to the latest run's `ended_at` for the issue, in minutes.

**Failed dev runs**:
```bash
sqlite3 -json .claude/sova.db "
  SELECT COUNT(*) as failed_runs
  FROM task_runs
  WHERE issue_number = '{ISSUE}'
    AND role = 'developer'
    AND status = 'failed';
"
```

**Model breakdown** (if CostRecord has model data):
```bash
sqlite3 -json .claude/sova.db "
  SELECT cr.model, SUM(cr.cost_usd) as model_cost,
         SUM(cr.input_tokens) as input_tokens,
         SUM(cr.output_tokens) as output_tokens
  FROM cost_records cr
  JOIN task_runs tr ON cr.task_run_id = tr.id
  WHERE tr.issue_number = '{ISSUE}'
    AND cr.model != ''
  GROUP BY cr.model;
"
```

### Step 4: Collect PR Metadata (GitHub)

For each issue (interactive and SOVA) that has a PR number:
```bash
gh pr view {PR_NUMBER} --json additions,deletions,changedFiles
```

- **LOC**: additions + deletions
- **Files changed**: changedFiles value

If `gh` fails (rate limit, auth issue, PR not found), mark LOC and Files as "N/A" for that issue and continue.

For issues without a known PR number, also mark as "N/A".

Fetch the issue title for the description column:
```bash
gh issue view {ISSUE_NUMBER} --json title --jq '.title'
```

If this fails, use "Issue #{N}" as the description.

### Step 5: Match Pairs

Match interactive issues to SOVA issues by LOC similarity for the head-to-head comparison:

1. From both groups, select only issues that have numeric LOC data.
2. Sort each group by LOC ascending.
3. For each interactive issue, find the closest unmatched SOVA issue by absolute LOC difference.
4. Record the pairing.

If either group is empty or no LOC data is available, skip pairing and report each group independently.

### Step 6: Compute Aggregates

For each group (interactive, SOVA autonomous), compute:

- **Average LOC changed**: mean of LOC values (where available)
- **Median LOC changed**: median of LOC values
- **Average active agent time**: mean of active agent minutes
- **LOC per active minute**: total LOC / total active minutes across the group
- **Average cost** (where recorded): mean of cost values, excluding N/A entries. Add "(where recorded)" qualifier if any entries lack cost data.
- **Average review rounds**: mean of review round counts
- **CI failure count**: sum across the group
- **Failed dev runs**: sum across the group (SOVA only)

### Step 7: Generate Report

Produce the report in markdown format matching the structure of `docs/benchmark-results.md`.

**Header**:
```
# Development Velocity Benchmark: SOVA Autonomous vs Interactive

**Date**: {today's date, YYYY-MM-DD}
**Model**: {list of models seen across both groups}
**Project**: SOVA (Python, ~{test count from make test output or "5000+"}  tests, CI via GitHub Actions + SonarCloud + CodeRabbit)
```

**Methodology section**: 2 paragraphs explaining both modes, matching the existing doc's tone.

**Interactive Issues table** (only if interactive data exists):

| Issue | Description | LOC | Files | Wall Clock | Active Agent | Human Idle | CI Wait | Review Rounds | Cost |
|-------|-------------|-----|-------|------------|--------------|------------|---------|---------------|------|

Format times as "N min". Format human idle with percentage: "N min (X%)". Format cost as "$X.XX" or "N/A".

**Matched SOVA Autonomous Issues table** (only if SOVA data exists):

| Issue | Description | LOC | Files | Active Agent | PR Cycle | Review Rounds | CI Failures | Cost |
|-------|-------------|-----|-------|--------------|----------|---------------|-------------|------|

Format times as "N min". Format cost as "$X.XX".

**Head-to-Head Comparison table** (only if both groups have data):

| Metric | Interactive (avg) | SOVA Autonomous (avg) | Difference |
|--------|------------------:|----------------------:|-----------:|

Include: LOC changed, active agent time, LOC per active minute, API cost, review rounds, CI failures, failed dev runs. Format difference as percentage or multiplier (e.g., "+15% interactive", "9.2x more expensive interactive").

**Time Allocation table** (interactive only):

| Component | Percentage | Minutes (avg) |
|-----------|:----------:|:-------------:|

Include: active agent work, human idle, CI pipeline waiting. Note overlap if applicable.

**Model Breakdown table** (only if CostRecord has model data across SOVA issues):

| Model | Issues | Total Cost | Avg Cost/Issue | Input Tokens | Output Tokens |
|-------|--------|------------|----------------|--------------|---------------|

**Analysis section**: generate 3 subsections based on the computed data:
- "Where SOVA autonomous wins": focus on cost efficiency and throughput
- "Where interactive wins": focus on wall-clock speed and review quality
- "Where both are equivalent": focus on code generation speed and CI reliability

Base observations on the actual numbers. Do not speculate beyond what the data shows. If the sample size is small (<5 in either group), note this limitation.

**Raw Data section**:
```
Benchmark event logs are stored in `.claude/benchmark/issue-{N}.jsonl`. SOVA autonomous metrics are derived from the project's SQLite database (`TaskRun`, `StepExecution`, `CostRecord` tables) and GitHub PR metadata.
```

List model usage per session if available.

### Step 8: Output the Report

**Terminal output** (always): display the full report as formatted markdown text.

**File output** (only with `--update-docs`): write the report to `docs/benchmark-results.md`, overwriting the existing content.

### Step 9: Summary

After generating the report, display a one-line summary:
```
Benchmark report generated: {N} interactive issues, {M} SOVA issues, {P} matched pairs.
```

If `--update-docs` was used, add: `Written to docs/benchmark-results.md`.

## Cross-References

- `/standup` for daily context
- `/find-task` for next work

## Rules

- NEVER use emojis in any output
- Use "N/A" for any missing data point, never leave cells empty or show errors
- All times in minutes, rounded to nearest integer
- All costs in USD with 2 decimal places (or 6 for small values under $0.01)
- Do not modify any source data (JSONL files, database). This command is read-only.
- When both data sources are missing, exit with a clear message instead of producing an empty report
