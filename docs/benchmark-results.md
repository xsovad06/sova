# Development Velocity Benchmark: SOVA Autonomous vs Interactive

**Date**: 2026-07-30
**Model**: Claude Opus 4.6 (both modes; 2 interactive sessions used Sonnet 4.6)
**Project**: SOVA (Python, ~5200 tests, CI via GitHub Actions + SonarCloud + CodeRabbit)

## Methodology

We compared two development modes on the same codebase, same model, same CI pipeline:

- **SOVA Autonomous**: the full pipeline runs unattended. The agent picks up an issue, develops a solution (TDD), self-reviews, creates a PR, addresses CodeRabbit review feedback, and hands off to a human for final merge. No human input during execution.
- **Interactive**: a developer guides Claude Code through the same steps via slash commands (`/spec`, `/develop-full`, `/review-full`, `/pr`, `/review-pr`, `/address-pr`, `/integrate-pr`), making decisions at each gate. Same AI, same pipeline steps, but with a human orchestrating.

Five issues were developed interactively with timestamped event logging. Each was matched to a SOVA-developed issue of comparable size (LOC).

## Results

### Interactive Issues

| Issue | Description | LOC | Files | Wall Clock | Active Agent | Human Idle | CI Wait | Review Rounds | Cost |
|-------|-------------|-----|-------|------------|--------------|------------|---------|---------------|------|
| #549 | Auto-address-review gate | 585 | 6 | 100 min | 91 min | 9 min (9%) | 28 min | 1 | N/A |
| #352 | Collapsible PR sections | 132 | 1 | 91 min | 72 min | 20 min (22%) | 30 min | 1 | N/A |
| #350 | PR lifecycle metrics page | 1574 | 13 | 216 min | 175 min | 41 min (19%) | 11 min | 2 | $111.05 |
| #388 | AgentRunProvider | 1010 | 4 | 139 min | 78 min | 60 min (43%) | 46 min | 2 | $35.09 |
| #144 | Centralize status colors | 128 | 5 | 177 min | 154 min | 1 min (1%) | 22 min | 1 | $21.07 |

### Matched SOVA Autonomous Issues

| Issue | Description | LOC | Files | Active Agent | PR Cycle | Review Rounds | CI Failures | Cost |
|-------|-------------|-----|-------|--------------|----------|---------------|-------------|------|
| #431 | Fleet analytics page | 578 | 10 | 73 min | 38 min | 1 | 0 | $1.29 |
| #505 | Dependency unblock fix | 134 | 5 | 191 min | 224 min | 2 | 0 | $3.71 |
| #342 | Standardize agent output | 1570 | 24 | 89 min | 2871 min | 0 | 0 | $15.00 |
| #293 | Task dependency graph | 1016 | 6 | 71 min | 1452 min | 1 | 0 | $7.84 |
| #522 | Cross-machine PR state | 120 | 8 | 71 min | 933 min | 1 | 0 | $2.37 |

### Head-to-Head Comparison

| Metric | Interactive (avg) | SOVA Autonomous (avg) | Difference |
|--------|------------------:|----------------------:|-----------:|
| LOC changed | 686 | 684 | matched |
| Active agent time | 114 min | 99 min | +15% interactive |
| LOC per active minute | 6.0 | 6.9 | +15% autonomous |
| API cost (where recorded) | $55.74 | $6.04 | 9.2x more expensive interactive |
| Review rounds | 1.4 | 1.0 | similar |
| CI failures | 0 | 0 | both clean |
| Failed dev runs | 0 | 1 | both reliable |

### Time Allocation (Interactive, % of Wall Clock)

| Component | Percentage | Minutes (avg) |
|-----------|:----------:|:-------------:|
| Active agent work | 79% | 114 min |
| Human idle (waiting for decisions) | 18% | 26 min |
| CI pipeline waiting | 19% | 27 min |

Note: CI wait overlaps with active time when the developer works while CI runs.

## Analysis

### Where SOVA autonomous wins

**Cost efficiency is the standout advantage.** SOVA's autonomous pipeline costs $6 per issue on average versus $56 for interactive development: a 9x difference. This isn't about the AI being cheaper in autonomous mode. It's about context window economics. Interactive sessions accumulate conversation history across all slash commands, resulting in 27-41M cache-read tokens per session. SOVA's pipeline makes short, focused LLM calls with structured prompts, keeping context tight.

**Throughput at scale.** SOVA autonomous runs require zero human attention during execution. While PR cycle times look long (hours to days), that's wall-clock time where the human is doing other work. The actual agent compute time (99 min average) is comparable to interactive. A developer can have 5-10 SOVA agents working in parallel across different issues while they focus on architecture, product decisions, or manual work.

**Consistency.** Zero CI failures, zero human intervention needed during development. The pipeline's gate checks (validate, self-review, CI monitor) catch issues mechanically.

### Where interactive wins

**Wall-clock speed for urgent work.** Interactive development completes issue-to-merged-PR in 2-3 hours of focused session time. SOVA autonomous issues take longer end-to-end because they wait in queues, wait for human review, and wait for merge approval. When something needs to ship today, interactive is faster.

**Spec quality through dialogue.** The interactive spec phase, though short (4-15 min), allows the developer to answer ambiguity and steer the design. SOVA's researcher produces specs without this dialogue, which occasionally leads to missed requirements caught only at review time.

**Review depth.** Interactive review found and fixed issues in real-time (e.g., circuit breaker plumbing bug in #549, duplicate imports in #350). The developer's domain knowledge guides the review to places the automated reviewer misses.

### Where both are equivalent

**Code generation speed.** Both modes produce 6-7 LOC per active minute. The underlying model does the same quality work regardless of who orchestrates it.

**CI reliability.** Neither mode produced CI failures in this sample. Both write tests, both pass lint, both handle pre-push hooks.

**Review effort.** Both averaged 1-2 review rounds before merge-ready. CodeRabbit catches the same kinds of issues in both modes.

### The real insight

The benchmark reveals that **human-in-the-loop development with AI is not "manual coding"**. It's supervised automation. The developer's actual contribution is decision-making (18% of wall clock), not code generation. The question isn't "AI vs human" but "supervised vs unsupervised automation."

For a solo developer or small team, the optimal strategy is hybrid: use SOVA autonomous for the backlog (high throughput, low cost), and interactive sessions for urgent or architecturally sensitive work (faster turnaround, higher review quality).

## Raw Data

Benchmark event logs are stored in `.claude/benchmark/issue-{N}.jsonl`. SOVA autonomous metrics are derived from the project's SQLite database (`TaskRun`, `StepExecution`, `CostRecord` tables) and GitHub PR metadata.

Interactive sessions #549, #352, and #350 used Claude Opus 4.6. Sessions #388 and #144 used Claude Sonnet 4.6 for session 2 (review/address/integrate phase). All SOVA autonomous runs used Claude Opus 4.6 via the Claude Code CLI default.
