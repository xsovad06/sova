# Spec: /benchmark-report command for cross-session velocity analysis

**Issue**: #571
**Status**: approved
**Created**: 2026-08-03
**Complexity**: moderate

## Problem

Regenerating the SOVA vs interactive benchmark comparison report (currently at `docs/benchmark-results.md`) requires manually querying the DB, parsing JSONL logs, and matching issue pairs in a single session. As more instrumented sessions accumulate (#568) and CostRecord data gets richer (#570), this should be a single slash command.

## Solution

Create a Claude Code slash command `.claude/commands/benchmark-report.md` that instructs Claude to: (1) parse all `.claude/benchmark/issue-*.jsonl` files for interactive session metrics, (2) query the SQLite DB for SOVA autonomous run metrics, (3) fetch PR metadata from GitHub for LOC matching, (4) compute aggregates, and (5) output a comparison report to the terminal and optionally write to `docs/benchmark-results.md`.

This is a markdown instruction file, not Python code. Claude Code executes the steps using bash, sqlite3, gh CLI, and inline computation when `/benchmark-report` is invoked.

## Pattern Reference

- `.claude/commands/standup.md` (lines 1-57): imperative command style, bash code blocks, structured output sections
- `.claude/commands/spec.md` (lines 1-265): multi-step command with `$ARGUMENTS` parsing, conditional sections
- `.claude/benchmark/session_end_hook.sh`: JSONL schema reference (event types, token fields, cost_usd)
- `.claude/benchmark/log.sh` (lines 1-50): canonical event names and JSONL format
- `docs/benchmark-results.md`: target output format (tables, analysis sections, raw data pointers)
- `sova/db/models.py:33-88` (TaskRun), `sova/db/models.py:106-124` (StepExecution), `sova/db/models.py:145-168` (CostRecord): DB schema for SOVA autonomous data

## Implementation Plan

### Step 1: Create the command file

Create `.claude/commands/benchmark-report.md` with YAML frontmatter:

```yaml
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
```

### Step 2: Argument parsing section

Parse `$ARGUMENTS` for three optional flags:
- `--since YYYY-MM-DD`: scope data to sessions after this date
- `--issues N,N,N`: scope to specific issue numbers
- `--update-docs`: write output to `docs/benchmark-results.md` (default: terminal only)

Instruct Claude to extract these from `$ARGUMENTS` using simple string parsing at the top of execution.

### Step 3: Interactive data collection (JSONL)

Instruct Claude to:

1. List all `.claude/benchmark/issue-*.jsonl` files
2. For each file, extract the issue number from the filename (`issue-(\d+).jsonl`)
3. Files matching `issue-null.jsonl` or non-numeric issue IDs: include in raw session count but exclude from per-issue aggregates
4. Parse each file to extract per-issue metrics:
   - **Wall clock**: timestamp of first `session_start` to last `session_end` (summed across sessions)
   - **Active agent time**: wall clock minus `human_idle` intervals (time between `human_idle_start` and `human_idle_end` pairs)
   - **Human idle time**: sum of `human_idle_start` to `human_idle_end` intervals
   - **CI wait time**: sum of `ci_check_start` to `ci_passed`/`ci_failed` intervals
   - **Review rounds**: count of `review_start` events
   - **Cost**: sum of `cost_usd` from `session_end` events that have it, or sum of `session_cost`/`cost`/`cost_recorded` events. N/A if no cost data exists.
   - **Model**: from `model_set` or `session_end` events
   - **Phase durations**: `spec_start` to `spec_complete`, `develop_start` to `develop_complete`, etc.
5. Apply `--since` filter by comparing first `session_start` timestamp
6. Apply `--issues` filter by matching extracted issue numbers

Use `jq` for JSON parsing. Provide the specific jq commands inline (e.g., `jq -s '[.[] | select(.event == "session_start")] | first | .ts'`).

### Step 4: SOVA autonomous data collection (DB)

Instruct Claude to query `.claude/sova.db` via `sqlite3`:

1. Find the DB path: `.claude/sova.db` in the project root
2. Query merged SOVA issues:
   ```sql
   SELECT DISTINCT tr.issue_number, tr.role, tr.status, tr.total_cost_usd,
          tr.started_at, tr.ended_at, tr.pr_number
   FROM task_runs tr
   WHERE tr.role = 'developer'
     AND tr.status = 'done'
     AND tr.issue_number IS NOT NULL
   ORDER BY tr.started_at;
   ```
3. For each issue, aggregate across all related TaskRuns (developer + reviewer + address_review):
   - **Active agent time**: sum of (`ended_at` - `started_at`) across all runs for the issue
   - **Cost**: sum of `total_cost_usd` across all runs for the issue. Also query CostRecord for model-level breakdown:
     ```sql
     SELECT cr.model, SUM(cr.cost_usd) as model_cost,
            SUM(cr.input_tokens) as input_tokens,
            SUM(cr.output_tokens) as output_tokens
     FROM cost_records cr
     JOIN task_runs tr ON cr.task_run_id = tr.id
     WHERE tr.issue_number = ?
     GROUP BY cr.model;
     ```
   - **Review rounds**: count of TaskRuns with `role = 'reviewer'` for the issue
   - **CI failures**: count of StepExecution records with `step_name = 'monitor_ci'` and `status = 'failed'` for the issue
   - **PR cycle time**: time from first developer run's `started_at` to last run's `ended_at`
   - **Failed dev runs**: count of developer TaskRuns with `status = 'failed'`
4. Apply `--since` filter on `started_at`
5. Apply `--issues` filter on `issue_number`
6. Graceful handling: if `.claude/sova.db` does not exist, report "No SOVA DB found" and skip this section

### Step 5: PR metadata collection (GitHub)

For each issue (both interactive and SOVA) that has a PR number:

```bash
gh pr view {PR_NUMBER} --json additions,deletions,changedFiles
```

- **LOC**: `additions + deletions`
- **Files changed**: `changedFiles`
- If `gh` fails or PR number is unknown, mark LOC as "N/A"
- For interactive issues, find PR number from `pr_created` events in JSONL (parse `notes` field for `PR #N` or `pr_number=N`)
- For SOVA issues, use `pr_number` from TaskRun

### Step 6: Pair matching

Match interactive issues to SOVA issues by LOC similarity:
1. Sort both groups by LOC
2. For each interactive issue, find the closest unmatched SOVA issue by absolute LOC difference
3. If no SOVA issues exist or LOC data is unavailable, skip pairing and report groups separately

### Step 7: Aggregate computation

Compute for each group (interactive, SOVA autonomous):
- Average and median LOC changed
- Average and median active agent time
- LOC per active minute (LOC / active_minutes)
- Average cost (where recorded)
- Average review rounds
- CI failure rate

### Step 8: Report generation

Output the report following the exact structure of `docs/benchmark-results.md`:

1. **Header**: title, date, model, project description
2. **Methodology**: brief description of both modes
3. **Interactive Issues table**: Issue, Description, LOC, Files, Wall Clock, Active Agent, Human Idle, CI Wait, Review Rounds, Cost
4. **SOVA Autonomous Issues table**: Issue, Description, LOC, Files, Active Agent, PR Cycle, Review Rounds, CI Failures, Cost
5. **Head-to-Head Comparison table**: metric-by-metric averages with difference column
6. **Time Allocation table**: interactive-only breakdown of wall clock components
7. **Model Breakdown table** (if CostRecord has model data): cost and token usage by model tier
8. **Analysis section**: auto-generate key observations based on the data (which mode wins on cost, speed, reliability)
9. **Raw Data section**: pointer to JSONL files and DB tables

Format tables as GitHub-flavored markdown. Use `N/A` for any missing data point.

### Step 9: Output

- **Terminal**: display the full report as formatted text
- **`--update-docs`**: write to `docs/benchmark-results.md`, overwriting the existing file. Confirm before writing: "Writing report to docs/benchmark-results.md (this overwrites the existing file)."

### Step 10: Cross-references footer

Include pointers to related commands:
- `/standup` for daily context
- `/find-task` for next work

## Design Decisions

1. **Why a markdown command, not a Python CLI subcommand?** The user specified this is a Claude Code slash command. It runs in the developer's interactive session where Claude has full tool access. No compiled infrastructure needed.

2. **Why sqlite3 CLI instead of Python ORM?** The command runs as Claude Code instructions. Claude executes shell commands, so `sqlite3 -json` is the natural query interface. No Python imports or async session management needed.

3. **Why LOC-based pair matching?** LOC is the most objective measure of task size. Matching by LOC controls for complexity when comparing cost and time metrics between modes. This is the same methodology used in the original #6 benchmark.

4. **Why not auto-detect PR numbers from GitHub search?** Searching `gh pr list --search "issue #N"` is unreliable (free-text matching). Using explicit PR numbers from JSONL events and DB records is authoritative.

5. **How to handle issue-null.jsonl?** Include it in the total session count and raw data summary (e.g., "7 JSONL files found, 6 with issue numbers") but exclude from per-issue aggregates and pair matching since there is no issue to match.

6. **Cost data availability?** Early interactive sessions (#549, #352) have no cost data (predates #570). The command must handle this gracefully by showing "N/A" and computing averages only over issues with cost data. The "where recorded" qualifier in the comparison table communicates this clearly.

## Scope Boundaries

- Do NOT build a Python CLI subcommand (`sova benchmark-report`). This is a `.claude/commands/` markdown file only.
- Do NOT create a distributable version in `commands/`. This command is SOVA-specific (references SOVA's own DB and benchmark logs).
- Do NOT modify the JSONL logging format or session hooks. The command reads existing data as-is.
- Do NOT add new dependencies. The command uses only `jq`, `sqlite3`, `gh`, and standard shell tools.
- Out of scope: automated benchmark scheduling, CI integration, historical trend charts.

## Edge Cases

1. **No JSONL files exist**: report "No interactive benchmark data found" and proceed with SOVA-only stats
2. **No DB exists**: report "No SOVA database found" and proceed with interactive-only stats
3. **Neither data source exists**: report "No benchmark data found" and exit
4. **JSONL file with no `session_start` event**: skip the file with a warning
5. **Issue with no PR number**: LOC shows "N/A", excluded from pair matching
6. **`gh` CLI not authenticated**: warn and mark all PR data as "N/A"
7. **Multiple sessions per issue** (e.g., #549 has session 1 and session 2): sum all sessions together for the issue's total metrics
8. **`--issues` references a non-existent issue**: include in output with all metrics as "N/A"
9. **CostRecord.model is empty/null** (pre-#570 records): group under "unknown" in model breakdown
10. **SOVA issue with no successful developer run**: exclude from SOVA autonomous group (only include `status = 'done'` runs)

## Testing Strategy

No automated tests needed (this is a markdown command file, not compiled code). Validation:

1. Invoke `/benchmark-report` in a Claude Code session and verify it reads all 6 JSONL files
2. Verify SOVA DB queries return sensible results against the live `.claude/sova.db`
3. Test `--issues 549,350` filter produces a 2-issue report
4. Test `--since 2026-07-30` scopes correctly
5. Test `--update-docs` writes to `docs/benchmark-results.md`
6. Test with missing DB (rename temporarily) to verify graceful fallback
7. Compare output format against existing `docs/benchmark-results.md` for structural consistency

## Dependencies

- `.claude/benchmark/log.sh`: defines the JSONL event vocabulary (lines 26-42)
- `.claude/benchmark/session_end_hook.sh`: defines `session_end` event schema with token/cost fields
- `sova/db/models.py:33-88`: TaskRun schema (issue_number, role, status, total_cost_usd, pr_number, started_at, ended_at)
- `sova/db/models.py:106-124`: StepExecution schema (step_name, status, duration_ms)
- `sova/db/models.py:145-168`: CostRecord schema (model, cost_usd, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens)
- `docs/benchmark-results.md`: target output format reference
- External tools: `jq`, `sqlite3`, `gh` CLI
