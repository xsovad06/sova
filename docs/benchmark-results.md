# Development Velocity Benchmark: SOVA Autonomous vs Interactive

**Date**: 2026-08-10
**Models**: Opus 4.6 (interactive primary), Sonnet 4.6 (interactive secondary), Claude Code CLI (SOVA autonomous)
**Project**: SOVA (Python, ~5700 tests, CI via GitHub Actions + SonarCloud + CodeRabbit)

## Methodology

We compared two development modes on the same codebase, same CI pipeline:

- **SOVA Autonomous**: the full pipeline runs unattended. The agent picks up an issue, develops a solution (TDD), self-reviews, creates a PR, addresses CodeRabbit review feedback, and hands off to a human for final merge. No human input during execution. 105 issues completed this way.
- **Interactive**: a developer guides Claude Code through the same steps via slash commands (`/spec`, `/develop-full`, `/review-full`, `/pr`, `/review-pr`, `/address-pr`, `/integrate-pr`), making decisions at each gate. Same AI, same pipeline steps, but with a human orchestrating. 17 issues developed interactively with timestamped event logging.

Both modes run the same CI pipeline (GitHub Actions, SonarCloud quality gate, CodeRabbit automated review). Interactive issues were selected across a range of complexity levels (59 to 2143 LOC). Interactive sessions primarily used Opus 4.6; SOVA autonomous runs used Claude Code CLI with default model selection.

## Results

### Interactive Issues (n=17)

| Issue | Description | LOC | Files | Wall Clock | Active Agent | Human Idle | CI Wait | Reviews | Cost |
|------:|-------------|----:|------:|-----------:|-------------:|-----------:|--------:|--------:|-----:|
| #144 | Centralize status color mappings | 128 | 5 | 177 min | 154 min | 1 min (1%) | 22 min | 2 | $21.07 |
| #350 | PR lifecycle metrics page | 1574 | 13 | 216 min | 170 min | 46 min (21%) | 17 min | 3 | $111.05 |
| #352 | Grouped collapsible PR sections | 132 | 1 | 91 min | 71 min | 20 min (22%) | 30 min | 2 | N/A |
| #388 | AgentRunProvider (awareness) | 1010 | 4 | 139 min | 78 min | 61 min (44%) | 31 min | 3 | $35.09 |
| #449 | /oversight page | 1405 | 10 | 211 min | 169 min | 41 min (19%) | N/A | 1 | N/A |
| #537 | Batch API submission path | 2143 | 18 | 1171 min | 528 min | 643 min (55%) | N/A | 2 | $226.95 |
| #549 | Auto-address-review gate | 585 | 6 | 97 min | 49 min | 14 min (14%) | 34 min | 2 | N/A |
| #595 | Dashboard perf: polling, CDN, cache | 301 | 18 | 1047 min | 164 min | 883 min (84%) | N/A | 3 | $169.99 |
| #600 | Persona config for LLM planning | 538 | 8 | 143 min | 143 min | N/A | N/A | 1 | $98.81 |
| #601 | CI minutes budget tracking | 988 | 10 | 453 min | 106 min | 347 min (77%) | N/A | 2 | $160.24 |
| #603 | Action buttons on graph nodes | N/A | N/A | 152 min | 60 min | 92 min (61%) | N/A | 2 | $52.58 |
| #605 | Human-required badges on graph | N/A | N/A | 24 min | 24 min | N/A | N/A | 1 | $36.72 |
| #606 | Priority icons in dependency graph | N/A | N/A | 10 min | 10 min | N/A | N/A | 0 | $16.90 |
| #608 | CHECKPOINT_NEEDED filter fix | 268 | 3 | 173 min | 64 min | 109 min (63%) | N/A | 2 | $84.97 |
| #609 | Rate limit tracking in git/pr | 469 | 12 | 482 min | 372 min | 110 min (23%) | N/A | 2 | $255.79 |
| #617 | Epic deps should not block children | 59 | 5 | 70 min | 18 min | 52 min (74%) | N/A | 1 | N/A |
| #628 | Orphaned step finalization | 298 | 6 | N/A | N/A | N/A | N/A | 2 | $246.88 |

### Head-to-Head Comparison

| Metric | Interactive (avg, n=17) | SOVA Autonomous (avg, n=105) | Difference |
|--------|------------------:|----------------------:|-----------:|
| LOC changed | 707 | 710 | Comparable |
| Active agent time | 136 min | 90 min | 1.5x more interactive |
| LOC per active minute | 4.5 | 4.0 | +13% interactive |
| API cost | $116.70 (n=13) | $6.31 (n=105) | 18.5x more expensive interactive |
| Review rounds | 1.8 | 1.2 | 1.5x more reviews interactive |
| Failed dev runs | 0 | 0.6 per issue | Interactive 100% first-try |
| Issues completed | 17 | 105 | 6.2x more autonomous |

### Full SOVA Autonomous Fleet (n=105)

| Metric | Value |
|--------|------:|
| Total issues completed | 105 |
| Total API cost | $662.61 |
| Avg cost per issue | $6.31 |
| Median cost per issue | $5.05 |
| Avg active agent time | 90 min |
| Avg LOC per issue | 710 |
| Total LOC produced | 73,930 |
| Avg files per issue | 9 |
| Review rounds (avg) | 1.2 |
| Failed dev runs | 62 (0.6 per issue) |
| CI step failures | 22 |
| Issues needing 0 retries | 71 (68%) |
| Issues with reviewer runs | 89 / 105 (85%) |

### Cost Distribution (SOVA Autonomous)

| Range | Count | Percentage |
|-------|------:|-----------:|
| $0-$1 | 6 | 6% |
| $1-$3 | 32 | 30% |
| $3-$5 | 14 | 13% |
| $5-$10 | 32 | 30% |
| $10-$15 | 15 | 14% |
| $15-$25 | 5 | 5% |
| $25-$50 | 1 | 1% |

### Time Allocation (Interactive, % of Wall Clock)

| Component | Avg Minutes | % of Wall Clock |
|-----------|:----------:|:---------------:|
| Active agent work | 136 min | 47% |
| Human idle (decisions, breaks, overnight) | 186 min | 64% |
| CI pipeline waiting | 26 min | 9% |

Note: percentages are of average wall clock (291 min). CI wait overlaps with human idle in some sessions (human waiting for CI results). Human idle exceeds 50% because several multi-session issues had overnight gaps.

### Top 30 SOVA Autonomous Issues by LOC

| Issue | Description | LOC | Files | Active Agent | PR Cycle | Reviews | CI Fails | Failed Runs | Cost |
|------:|-------------|----:|------:|-------------:|---------:|--------:|---------:|------------:|-----:|
| #109 | Adapter-based PR review state | 2533 | 20 | 101 min | 2481 min | 1 | 3 | 5 | $11.66 |
| #169 | Clear current_step in ReviewerRole | 2205 | 33 | 74 min | 1007 min | 1 | 2 | 3 | $10.65 |
| #356 | Resource exhaustion guard | 2144 | 19 | 229 min | 2134 min | 0 | 0 | 1 | $0.56 |
| #75 | Issue Lifecycle Control | 1735 | 12 | 56 min | 1424 min | 1 | 0 | 0 | $7.88 |
| #342 | Standardize agent output quality | 1570 | 24 | 109 min | 4578 min | 1 | 0 | 0 | $15.00 |
| #110 | SpecStep with dashboard approval | 1471 | 14 | 91 min | 181 min | 1 | 1 | 1 | $10.72 |
| #32 | Team knowledge sharing | 1377 | 9 | 82 min | 4903 min | 2 | 0 | 3 | $13.93 |
| #165 | Planning pipeline steps | 1332 | 11 | 80 min | 990 min | 1 | 1 | 1 | $10.21 |
| #446 | LLM pattern analysis (oversight) | 1331 | 12 | 116 min | 9942 min | 0 | 0 | 0 | $1.30 |
| #291 | CodeRabbit rate limit tracker | 1304 | 14 | 234 min | 32786 min | 0 | 0 | 1 | $22.88 |
| #298 | Cross-project fleet manager | 1241 | 9 | 602 min | 1106 min | 1 | 0 | 0 | $5.10 |
| #255 | Resource monitoring: DB persistence | 1215 | 6 | 125 min | 501 min | 0 | 1 | 1 | $11.24 |
| #295 | Background PR lifecycle monitor | 1212 | 7 | 81 min | 2532 min | 1 | 0 | 0 | $9.36 |
| #246 | Provenance threading | 1191 | 13 | 64 min | 992 min | 1 | 0 | 0 | $1.28 |
| #430 | FleetService (cross-project) | 1149 | 7 | 256 min | 1277 min | 2 | 0 | 1 | $32.67 |
| #188 | Milestone creation during onboarding | 1086 | 13 | 97 min | 265 min | 1 | 2 | 2 | $11.51 |
| #220 | Jira lifecycle enrichment | 1084 | 15 | 103 min | 1280 min | 1 | 1 | 2 | $13.70 |
| #344 | Runtime watchdog | 1063 | 7 | 91 min | 941 min | 1 | 0 | 1 | $10.93 |
| #225 | Memory relationship graph | 1025 | 8 | 74 min | 235 min | 1 | 0 | 0 | $9.27 |
| #152 | Agent status aggregator | 1019 | 4 | 91 min | 2280 min | 1 | 0 | 5 | $7.49 |
| #293 | Task dependency graph engine | 1016 | 6 | 80 min | 7262 min | 1 | 0 | 0 | $7.84 |
| #239 | Expand scheduler/knowledge test coverage | 993 | 3 | 56 min | 326 min | 1 | 0 | 0 | $1.29 |
| #256 | Resource monitoring: dashboard metrics | 990 | 11 | 70 min | 1018 min | 1 | 0 | 0 | $2.49 |
| #254 | Resource monitoring: psutil integration | 987 | 9 | 104 min | 1071 min | 1 | 0 | 0 | $8.66 |
| #111 | Issueless planning agent role | 981 | 24 | 93 min | 684 min | 1 | 1 | 3 | $10.37 |
| #250 | Activity feed notification panel | 941 | 13 | 104 min | 9383 min | 1 | 0 | 1 | $6.03 |
| #116 | Agent Runtime abstraction | 938 | 12 | 81 min | 173 min | 1 | 0 | 0 | $10.58 |
| #258 | Resource monitoring: cross-project | 922 | 8 | 72 min | 2910 min | 0 | 0 | 1 | $11.29 |
| #233 | Regression tracking: test snapshots | 914 | 16 | 158 min | 2691 min | 1 | 0 | 0 | $7.42 |
| #234 | Multi-perspective review panel | 911 | 5 | 72 min | 1190 min | 1 | 0 | 0 | $2.32 |

*75 additional issues omitted (full data in SQLite database).*

### Model Breakdown (from CostRecord)

| Model | Total Cost | Invocations |
|-------|------------|-------------|
| claude (default CLI provider) | $1,165.41 | 1,180 |
| opus (direct API calls) | $35.85 | 19 |
| sonnet (direct API calls) | $35.28 | 24 |

Note: CLI provider invocations track session-level cost without token decomposition. Opus and sonnet entries are from direct API calls (PR body generation, LLM suggestions) with per-call cost tracking.

## Analysis

### Where SOVA autonomous wins

Cost efficiency is the clearest advantage. At $6.31 average cost per issue vs $116.70 for interactive sessions (where recorded), autonomous development is 18.5x cheaper per issue. The median autonomous cost of $5.05 means half of all issues complete for under five dollars. The entire autonomous fleet of 105 issues cost $662.61 total, compared to $1,517.04 across just 13 interactive issues with cost data.

Throughput is the second major advantage. SOVA completed 105 issues autonomously while the developer worked on 17 issues interactively. The autonomous pipeline requires zero human attention during execution, allowing the developer to work on other tasks simultaneously. At 68% first-try success rate (71 of 105 issues with zero failed dev runs), the pipeline is reliable enough to run overnight unsupervised.

### Where interactive wins

Interactive sessions have a 100% success rate: all 17 issues completed without failed dev runs, while SOVA autonomous had 62 failed dev runs across 34 issues. The human-in-the-loop catches architectural misalignment early, avoiding costly rework cycles.

Interactive sessions achieve higher review thoroughness (1.8 rounds vs 1.2), catching issues that autonomous runs may ship. For complex or novel features (like the 2143-LOC Batch API or 1574-LOC PR metrics page), the human's ability to redirect mid-implementation is a clear advantage, though this comes at a steep cost premium.

LOC productivity per active minute is slightly higher for interactive (4.5 vs 4.0), showing that human-guided sessions generate code more efficiently when actively working, though the difference is modest.

### Where both are equivalent

Average LOC per issue is nearly identical: 707 interactive vs 710 autonomous. Both modes produce substantial features, not just trivial fixes. Both paths go through the same CI pipeline (GitHub Actions + SonarCloud + CodeRabbit), ensuring equivalent quality gates. CI failure rates are low in both modes (22 total across 105 autonomous issues), indicating the test suite and linting catch most issues before push.

### Limitations

The interactive sample (n=17, cost data for 13) is larger than the previous report (n=8, cost data for 5) but still smaller than the autonomous sample (n=105). Interactive sessions used Opus 4.6 as the primary model (more expensive), while SOVA runs used the default CLI provider. The model difference contributes to the cost gap. LOC is a crude complexity proxy: it does not distinguish between generated test boilerplate and nuanced business logic. The interactive sample skews toward recent, complex supervisor/dashboard features, while the autonomous sample spans the full project history including simpler fixes and refactors.

## Raw Data

Benchmark event logs are stored in `.claude/benchmark/issue-{N}.jsonl` (21 files, 17 issues with sufficient data for analysis). SOVA autonomous metrics are derived from the project's SQLite database (`.claude/sova.db`): `TaskRun`, `StepExecution`, and `CostRecord` tables, plus GitHub PR metadata via `gh pr view`.

**Interactive sessions model usage**: Opus 4.6 (11 issues), Sonnet 4.6 (2 issues: #144, #388), unknown/unrecorded (4 issues).
