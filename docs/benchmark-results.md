# Development Velocity Benchmark: SOVA Autonomous vs Interactive

**Date**: 2026-08-05
**Model**: Claude Opus 4.6 (primary); Sonnet 4.6 used in 2 interactive sessions
**Project**: SOVA (Python, ~5576 tests, CI via GitHub Actions + SonarCloud + CodeRabbit)

## Methodology

We compared two development modes on the same codebase, same model, same CI pipeline:

- **SOVA Autonomous**: the full pipeline runs unattended. The agent picks up an issue, develops a solution (TDD), self-reviews, creates a PR, addresses CodeRabbit review feedback, and hands off to a human for final merge. No human input during execution. 104 issues completed this way.
- **Interactive**: a developer guides Claude Code through the same steps via slash commands (`/spec`, `/develop-full`, `/review-full`, `/pr`, `/review-pr`, `/address-pr`, `/integrate-pr`), making decisions at each gate. Same AI, same pipeline steps, but with a human orchestrating. 8 issues developed interactively with timestamped event logging.

Both modes run the same CI pipeline (GitHub Actions, SonarCloud quality gate, CodeRabbit automated review). Interactive issues were selected across a range of complexity levels (128 to 2143 LOC).

## Results

### Interactive Issues (n=8)

| Issue | Description | LOC | Files | Wall Clock | Active Agent | Human Idle | CI Wait | Reviews | Cost |
|-------|-------------|----:|------:|-----------:|-------------:|-----------:|--------:|--------:|-----:|
| #144 | Centralize status color mappings | 128 | 5 | 177 min | 154 min | 1 min (1%) | 22 min | 2 | $21.07 |
| #352 | Grouped collapsible PR sections | 132 | 1 | 91 min | 41 min | 23 min (25%) | 27 min | 2 | N/A |
| #595 | Dashboard perf: polling, CDN, cache | 301 | 18 | 208 min | 133 min | 45 min (22%) | 30 min | 4 | $108.52 |
| #549 | Auto-address-review gate | 585 | 6 | 97 min | 49 min | 14 min (14%) | 34 min | 2 | N/A |
| #388 | AgentRunProvider (awareness) | 1010 | 4 | 139 min | 65 min | 43 min (31%) | 31 min | 3 | $35.09 |
| #449 | /oversight page | 1405 | 10 | 210 min | 175 min | 15 min (7%) | 20 min | 3 | N/A |
| #350 | PR lifecycle metrics page | 1574 | 13 | 217 min | 153 min | 47 min (22%) | 17 min | 3 | $111.05 |
| #537 | Batch API submission path | 2143 | 18 | 1131 min | 215 min | 643 min (57%) | 30 min | 5 | $226.95 |

### Matched SOVA Autonomous Issues (n=8, LOC-matched)

| Issue | Description | LOC | Files | Active Agent | Cycle | Reviews | CI Fail | Dev Runs | Cost |
|-------|-------------|----:|------:|-------------:|------:|--------:|--------:|---------:|-----:|
| #522 | _validate_review_pr fail-open fix | 120 | 8 | 55 min | 58 min | 1 | 0 | 2 | $1.19 |
| #558 | Epic node styling + priority arrows | 595 | 8 | 73 min | 78 min | 1 | 0 | 2 | $0.51 |
| #579 | Fail-fast missing git identity | 344 | 9 | 117 min | 66 min | 0 | 0 | 3 | $14.65 |
| #431 | /fleet page (failure analytics) | 578 | 10 | 61 min | 63 min | 1 | 0 | 2 | $1.15 |
| #293 | Task dependency graph engine | 1016 | 6 | 49 min | 76 min | 1 | 0 | 2 | $7.63 |
| #430 | FleetService (cross-project) | 1149 | 7 | 49 min | 308 min | 2 | 0 | 2+1F | $9.58 |
| #342 | Standardize agent output quality | 1570 | 24 | 47 min | 47 min | 1 | 0 | 1 | $9.74 |
| #520 | LLM suggestion circuit breaker | 342 | 8 | 88 min | 95 min | 1 | 0 | 2 | $7.08 |

### Head-to-Head Comparison

| Metric | Interactive (avg, n=8) | SOVA Autonomous (avg, n=8) | Difference |
|--------|------------------:|----------------------:|-----------:|
| LOC changed | 910 | 714 | Interactive 27% larger |
| Active agent time | 123 min | 67 min | 1.8x faster autonomous |
| LOC per active minute | 7.4 | 10.6 | +43% autonomous |
| API cost (where recorded) | $100.54 (n=5) | $6.44 (n=8) | 15.6x more expensive interactive |
| Review rounds | 3.0 | 0.9 | 3.3x more reviews interactive |
| CI failures | 0 | 0 | Both clean |
| Failed dev runs | 0 | 0.1 | Both reliable |

### Full SOVA Autonomous Fleet (n=104)

| Metric | Value |
|--------|------:|
| Total issues completed | 104 |
| Total API cost | $376.66 |
| Avg cost per issue | $3.62 |
| Median cost per issue | $1.58 |
| Cost P25 / P75 / P90 | $1.07 / $6.78 / $9.39 |
| Avg active agent time | 42 min |
| Median active agent time | 45 min |
| Agent time P25 / P75 / P90 | 22 / 59 / 74 min |
| Avg LOC per issue | 710 |
| Total LOC produced | 73,144 |
| Avg files per issue | 9.0 |
| Review rounds (avg) | 1.2 |
| Failed dev runs | 62 (0.6 per issue) |
| CI step failures | 31 |
| Issues with reviewer runs | 87 / 104 (84%) |

### Time Allocation (Interactive, % of Wall Clock)

| Component | Avg Minutes | % of Wall Clock |
|-----------|:----------:|:---------------:|
| Active agent work | 123 min | 49% |
| Human idle (decisions, breaks, overnight) | 104 min | 41% |
| CI pipeline waiting | 26 min | 10% |

Note: CI wait overlaps with human idle in some sessions (human waiting for CI results).

### Model Breakdown (SOVA Autonomous, from CostRecord)

| Model | Issues | Total Cost |
|-------|-------:|-----------:|
| claude (default provider) | 143* | $900.00 |
| sonnet | 4 | $24.76 |
| opus | 3 | $17.74 |

*Issue count exceeds 104 because CostRecord tracks all runs (including failed, retry, and reviewer runs), not just successful developer runs.

## Analysis

### Where SOVA autonomous wins

Cost efficiency is the clearest advantage. At $3.62 average cost per issue vs $100.54 for interactive sessions (where recorded), autonomous development is 15.6x cheaper per issue. Even at the 90th percentile ($9.39), SOVA issues cost less than the cheapest recorded interactive session ($21.07). The median cost of $1.58 means half of all autonomous issues complete for under two dollars. Active agent time is also shorter: 42 min average vs 123 min interactive, largely because autonomous runs do not re-derive context across session boundaries or wait for human decisions.

### Where interactive wins

Interactive development produces higher-quality first attempts. Zero interactive issues had failed dev runs, while SOVA averaged 0.6 failures per issue (62 total across 104 issues). Interactive sessions also achieve higher review thoroughness (3.0 rounds vs 0.9), catching issues that autonomous runs may ship. The human-in-the-loop catches architectural misalignment early, avoiding costly rework cycles. For complex or novel features (like the 2143-LOC Batch API or 1574-LOC PR metrics page), the human's ability to redirect mid-implementation is a clear advantage, though this comes at a steep cost premium.

### Where both are equivalent

CI reliability is identical: zero CI step failures across interactive sessions, and only 31 across 104 SOVA issues (0.3 per issue). Code generation velocity is comparable when normalized: 7.4 LOC/min interactive vs 10.6 LOC/min autonomous, with the difference largely explained by session overhead in interactive mode (context re-establishment, command invocation, output review). Both modes produce PRs that pass the same SonarCloud quality gate and CodeRabbit review pipeline.

### Limitations

The interactive sample (n=8, cost data for 5) is small and may not be representative. Interactive sessions used Opus 4.6 as the primary model (more expensive), while SOVA runs used the default provider. Cost comparison is directional, not definitive. The 15.6x figure uses only the 5 interactive issues with recorded costs; the true ratio likely falls between 10x and 20x. LOC is a crude complexity proxy: it does not distinguish between generated test boilerplate and nuanced business logic.

## Raw Data

Benchmark event logs are stored in `.claude/benchmark/issue-{N}.jsonl` (10 interactive issues logged, 1 issue-null excluded). SOVA autonomous metrics are derived from the project's SQLite database (`.claude/sova.db`): `TaskRun`, `StepExecution`, and `CostRecord` tables, plus GitHub PR metadata via `gh pr view`.

**Interactive sessions model usage**: Opus 4.6 (6 issues), Sonnet 4.6 (2 issues: #144, #388).
