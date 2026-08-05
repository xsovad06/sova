# I Built an Autonomous Software Agent That Develops Features While I Sleep. Here's What 104 Issues Taught Me.

*What happens when you let an AI agent run your entire development pipeline unsupervised: TDD, self-review, PR creation, CI monitoring, and code review response? I measured everything.*

## The Setup

I'm a solo developer working on SOVA, a Python application with 5,576 tests, CI via GitHub Actions, SonarCloud quality gates, and CodeRabbit automated review. Over the past three months, I've been running two parallel development tracks on the same codebase:

**Interactive mode**: I sit at the keyboard and guide Claude Code through each step using slash commands. I write the spec, approve the design, trigger development, review the output, create the PR, address review feedback, and merge. Same AI, but I'm orchestrating.

**Autonomous mode**: SOVA picks up a GitHub issue, spawns Claude Code headlessly, and runs the full pipeline without me. TDD development, self-review, PR creation, CI monitoring, CodeRabbit feedback resolution, and handoff for human merge. I come back to a finished PR.

I developed 8 issues interactively with timestamped event logging, and 104 issues autonomously with full telemetry in the database. Same codebase. Same CI pipeline. Same model. Different levels of human involvement.

## The Numbers

### Cost: 15.6x cheaper autonomous

This was the headline finding. Interactive sessions averaged **$100.54** per issue (where cost was recorded, n=5). Autonomous runs averaged **$3.62** per issue across all 104 issues. The median autonomous cost was just **$1.58**.

The entire autonomous fleet of 104 issues cost **$376.66 total**. That's less than four interactive sessions.

Why the gap? It's not that the model is cheaper in autonomous mode. It's context window economics. Interactive sessions accumulate conversation history across every slash command invocation. By the time you've done `/spec`, `/develop-full`, `/review-full`, `/pr`, `/address-pr`, and `/integrate-pr`, the context window is carrying megabytes of prior output. SOVA's pipeline makes short, focused LLM calls with structured prompts, keeping each call's context minimal.

### Speed: 1.8x faster active agent time

Autonomous runs averaged **67 minutes** of active agent compute time per issue versus **123 minutes** interactive. The autonomous pipeline doesn't waste time re-establishing context between commands, doesn't wait for human decisions, and doesn't serialize work through a single conversation thread.

But wall-clock time tells a different story. Interactive sessions complete in 2-4 hours of focused time. Autonomous issues can take days end-to-end because they wait in queues, wait for CI, and wait for human review at the end. When something needs to ship today, interactive is faster.

### Throughput: 73,144 lines across 104 issues

The autonomous fleet produced 73,144 lines of code changes across 104 completed issues, averaging 710 LOC and 9 files per issue. These aren't toy tasks: they include dependency graph engines, fleet analytics dashboards, batch API submission paths, review automation, and supervisor-level orchestration.

### Reliability: comparable, with caveats

Both modes had zero CI failures in the head-to-head comparison. Across the full autonomous fleet, there were 31 CI step failures across 104 issues (0.3 per issue) and 62 failed developer runs (0.6 per issue). Interactive had zero failures, but the sample is smaller.

The failure rate is the price of autonomy. Without a human catching "that's not what I meant" early, some percentage of autonomous runs go down the wrong path. But at $3.62 per attempt, retrying is cheap.

## What I Actually Do Now

The benchmark revealed something I didn't expect: **in interactive sessions, I'm idle 41% of the time**. I'm waiting for the agent to finish a step, reviewing output, deciding what to do next, or taking breaks. My actual contribution is decision-making at gates, not code generation.

This reframed how I think about the two modes. Interactive development isn't "manual coding with AI help." It's supervised automation. The question isn't "should AI write my code?" It's "which tasks need supervision and which don't?"

My current workflow:

1. **Triage and spec the backlog** (human, ~5 min per issue). I write the issue, set priorities, and make sure the requirements are clear.
2. **Let SOVA run the backlog** (autonomous). 5-10 agents working in parallel overnight. I wake up to PRs.
3. **Review and merge** (human, ~10 min per PR). I read the diff, check the approach, merge or request changes.
4. **Interactive for hard problems** (human + AI). Architecturally sensitive work, novel features, anything where I need to steer mid-implementation.

The result: I'm developing at a pace that would require a small team, at a cost that's less than my coffee budget.

## The Honest Limitations

**Sample size**: 8 interactive issues is small. The cost comparison uses only 5 issues with recorded costs. These numbers are directional, not definitive.

**LOC is crude**: it doesn't distinguish between generated test boilerplate and nuanced business logic. A 700-LOC autonomous issue might be simpler than a 700-LOC interactive one.

**Model difference**: Interactive sessions primarily used Opus 4.6 (more expensive). SOVA runs used the default provider. This contributes to the cost gap, though not enough to explain a 15x difference.

**Self-developing a development tool**: SOVA is developing SOVA. The agent has deep context about the codebase it's working on. Results on a different project, especially one with less structured issues or weaker test coverage, would likely differ.

**Human review is still required**: autonomous PRs need a human to review and merge. The 10-minute review step is doing real work: I've caught architectural misalignments, unnecessary abstractions, and scope creep that the automated review missed.

## What This Means

The 15.6x cost difference is not an argument against interactive development. It's an argument for having both modes and knowing when to use each.

Autonomous development is not a replacement for engineering judgment. It's a multiplier. The 104 issues SOVA completed autonomously are issues I would have done interactively, one at a time, over months. Instead, they shipped in weeks, at a fraction of the cost, while I focused on the hard problems that actually needed human attention.

The real insight from this benchmark: the bottleneck in AI-assisted development is not code generation speed or model capability. It's the decision of what to build, how to specify it, and when to trust the output. Those are human problems. Everything else can be automated.

---

*SOVA is open source. The benchmark data, event logs, and analysis scripts are in the repository.*

*[Full benchmark results with methodology and raw data](benchmark-results.md)*
