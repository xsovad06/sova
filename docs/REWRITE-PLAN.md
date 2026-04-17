# SOVA: Complete Rewrite Plan

## Context

PAK (Project Automation Kit) is being rebranded and rewritten as **SOVA** -- **Software Orchestration Via Agents**. The current orchestrator is a 3,600-line Bash script. While it works, the project's ambition -- 24/7 server operation, multi-user support, a team of specialized agents, robust failure handling, and intelligent task triage -- has outgrown what Bash can professionally deliver.

The current system has a critical flaw visible in the issue #60 log: the agent produced zero code during development, yet blindly continued through simplify/review/push steps. The pipeline has no gate checks, no output validation, and no way for users to understand what went wrong.

### Why "SOVA"

The name **SOVA** was chosen through a structured naming process (full story in [naming-journey.md](naming-journey.md)):

- **S.O.V.A. -- Software Orchestration Via Agents** -- a perfect acronym describing what the system does
- The creator's actual surname (Sova = "owl" in Czech/Slovak) -- legacy built into the product
- Zero conflicts in developer tooling, AI agents, or any adjacent space
- 4 letters, clean, professional
- `sova run 42`, `sova triage`, `sova server start` -- beautiful CLI ergonomics
- The owl symbolism: wisdom, night work, vigilance -- perfect for agents that work 24/7

**CLI command**: `sova`. **PyPI package**: `sova`.

---

## Technology Stack

| Concern | Choice | Why |
|---------|--------|-----|
| Language | **Python 3.12+** | Already used for dashboard, AI ecosystem is Python-first, great subprocess/async support |
| CLI | **Typer** (built on Click) | Auto help/completion, type validation, rich output |
| Web | **FastAPI** (keep) | Already in use, async-native, WebSocket support |
| Database | **SQLite** (default) + **PostgreSQL** (multi-user) | SQLite = zero config for single dev; PG for team servers. SQLAlchemy 2.0 async ORM for both |
| Config | **Pydantic Settings v2 + TOML** | `sova.toml` per project, env var overrides, validated at startup |
| State Machine | **transitions** library | Lightweight FSM for task lifecycle |
| Logging | **structlog** | Structured JSON logging, streamable to dashboard |
| Terminal UI | **rich** | Tables, progress bars, status panels |
| Testing | **pytest + pytest-asyncio** | Unified test suite |
| Migrations | **Alembic** | Schema versioning for SQLite/PG |

---

## Agent Roles (Team of Agents)

SOVA introduces **role-based agents** -- each role has a different workflow, prompt strategy, and set of capabilities. This replaces the current single-purpose developer agent.

### Role Definitions

| Role | Purpose | Workflow | Tracker Output |
|------|---------|----------|----------------|
| **Triage** | Evaluates backlog issues for agent suitability | Scan issues -> assess complexity/context -> label/tag | Moves Backlog -> Triaged. Labels: `agent:ready`, `agent:needs-spec`, `agent:needs-research`, `agent:human-only` |
| **Researcher** | Investigates issues, assesses feasibility, defines approach | Analyze issue -> explore codebase -> write assessment -> create sub-tasks | Moves Triaged -> Researched. Posts spec comment. Creates sub-issues if needed. |
| **Developer** | Writes code via TDD | 11-step pipeline: assess -> worktree -> develop -> simplify -> push -> PR -> CI | Moves Researched -> In Progress -> In Review -> Done. Assigns self. Creates branch + PR. |
| **Reviewer** | Reviews PRs (the current "Koda" reviewer) | Fetch PR diff -> review -> post findings | Posts GitHub PR review. Approves or requests changes. |

### Role Architecture

Roles have functional names in code. Users can optionally assign display nicknames in config (e.g., `[roles.nicknames] reviewer = "Koda"`), shown in the dashboard UI.

```python
# sova/roles/base.py
class AgentRole(ABC):
    """Base class for all agent roles."""
    name: str
    description: str
    required_context: list[str]  # What the role needs to start

    @abstractmethod
    async def execute(self, ctx: ExecutionContext) -> RoleResult: ...

    @abstractmethod
    async def assess_task(self, task: Task) -> TaskAssessment: ...
```

### Task Assessment (every role does this)

Before any agent starts work, it runs `assess_task()`:

```python
class TaskAssessment(BaseModel):
    """Assessment of whether an agent can handle a task."""
    suitability: Literal["ready", "needs_spec", "needs_research", "human_only"]
    confidence: float  # 0.0 - 1.0
    reasoning: str
    missing_context: list[str]  # What's missing to make this actionable
    estimated_complexity: Literal["trivial", "simple", "moderate", "complex", "epic"]
    suggested_role: str  # Which role should handle this
    sub_tasks: list[str]  # If the task should be broken down
```

### Triage Command

`sova triage` -- scans the backlog and labels issues:

```
sova triage                          # Assess all open issues
sova triage --label                  # Also apply GitHub labels
sova triage --issue 42               # Assess a single issue
```

The triage agent:
1. Fetches all open issues (via task adapter)
2. For each issue, calls `assess_task()` with a Claude prompt that analyzes:
   - Is the description specific enough? (acceptance criteria, steps, expected behavior)
   - Does it reference specific files/functions?
   - Is the scope bounded? (single feature vs epic)
   - Does it require domain knowledge the agent doesn't have?
3. Labels each issue: `agent:ready`, `agent:needs-spec`, `agent:needs-research`, `agent:human-only`
4. For `needs-spec` issues: posts a comment listing what's missing
5. For `needs-research` issues: suggests dispatching the Researcher role

---

## Task Lifecycle: Agents Own the Issue State

A core principle of SOVA: **agents manage issue state on the tracker**, not just internally. Every state transition is visible on the GitHub project board (or JIRA/Linear). The user sees exactly where every issue is and which agent is handling it.

### Issue States (Project Board Columns)

```
Backlog -> Triaged -> Researched -> In Progress -> In Review -> Done
                |          |
                |          +-> Needs Spec (blocked, waiting for human input)
                +-> Human Only (agent cannot handle this)
```

| State | Who moves it here | What happens |
|-------|-------------------|--------------|
| **Backlog** | Human (creates issue) | New issue, unassessed. No agent has looked at it yet. |
| **Triaged** | Triage agent | Triage assessed the issue. Labels applied: `agent:ready`, `agent:needs-spec`, `agent:needs-research`, `agent:human-only`. Issues labeled `agent:needs-research` are queued for the Researcher. |
| **Researched** | Researcher agent | Researcher investigated the codebase, confirmed feasibility, wrote a spec/assessment comment, identified affected files and approach. Issue is now **ready for development**. This is the "green light" for the Developer. |
| **In Progress** | Developer agent | Developer picked up the issue, assigned themselves, created a worktree, and is actively coding. Only issues in "Researched" state can be picked up -- **this gate is enforced**. |
| **In Review** | Developer agent | PR created, CI running, automated review in progress. The Reviewer agent handles feedback cycles. |
| **Done** | Developer agent (or human) | PR merged, issue closed. Agent records learnings. |
| **Needs Spec** | Triage or Researcher | Issue is underspecified. Agent posted a comment listing what's missing. Blocked until a human provides more detail, then returns to Backlog for re-triage. |
| **Human Only** | Triage agent | Agent determined this issue requires human judgment, domain expertise, or access the agent doesn't have. Will not be picked up autonomously. |

### The Mandatory Pipeline

```
          GATE 1                    GATE 2                    GATE 3
            |                         |                         |
  Backlog --+--> Triaged --+-------+--> Researched -----+--> In Progress --> In Review --> Done
            |              |       |                    |
         Triage         needs-     |                 Developer
         agent          research?  |                 checks:
         assesses       |          |                 "Is this in
         suitability    v          |                 Researched
                     Researcher    |                 state?"
                     investigates  |                 If not: REJECT
                     writes spec --+
```

**Gate 1 (Triage)**: Every new issue must be triaged before any other agent touches it. The Triage agent decides: can an agent handle this at all?

**Gate 2 (Research)**: Issues marked `agent:ready` or `agent:needs-research` go through the Researcher. The Researcher explores the codebase, verifies the approach is feasible, and writes a concrete assessment. Only then does the issue move to "Researched".

**Gate 3 (Development)**: The Developer agent **refuses** to pick up any issue not in "Researched" state. This prevents the old failure mode where the agent blindly started work on underspecified issues and produced nothing.

### What Each Agent Does to the Tracker

**Triage Agent** (`sova triage`):
- Reads: all issues in Backlog
- Writes: moves issue to "Triaged", applies labels (`agent:ready`, `agent:needs-spec`, `agent:needs-research`, `agent:human-only`), posts assessment comment

**Researcher Agent** (`sova research <issue>`):
- Reads: issues labeled `agent:needs-research` or `agent:ready` in "Triaged" state
- Writes: moves issue to "Researched", posts detailed assessment comment (affected files, suggested approach, complexity estimate, sub-tasks if needed), updates labels

**Developer Agent** (`sova run <issue>`):
- Reads: issues in "Researched" state only
- Writes: assigns self, moves to "In Progress", then "In Review" after PR creation, then "Done" after merge
- On failure: moves back to "Researched" with a failure comment explaining what went wrong

**Reviewer Agent** (triggered automatically after PR):
- Reads: PRs linked to issues in "In Review"
- Writes: posts review comments, approves/requests changes

### Tracker Adapter Interface

The adapter must support state management, not just read/write:

```python
class TaskAdapter(ABC):
    # Existing
    async def list_tasks(self, filters) -> list[Task]: ...
    async def get_task(self, task_id) -> Task: ...

    # NEW: State management
    async def transition_state(self, task_id: str, new_state: TaskState) -> None: ...
    async def assign(self, task_id: str, agent_role: str) -> None: ...
    async def add_label(self, task_id: str, label: str) -> None: ...
    async def remove_label(self, task_id: str, label: str) -> None: ...
    async def post_comment(self, task_id: str, body: str) -> None: ...
    async def get_state(self, task_id: str) -> TaskState: ...
```

For GitHub, `transition_state()` moves the issue between project board columns. For JIRA, it triggers workflow transitions. For Linear, it updates the issue status.

### Override: Manual Mode

The mandatory pipeline can be bypassed with `--force`:

```bash
sova run 42 --force    # Skip the "Researched" gate check
```

This is useful for quick fixes where the developer knows exactly what to do. The agent still runs its own internal assessment but won't refuse based on tracker state.

---

## Agent Lifecycle: Spawn, Handoff, Die

Agents are **ephemeral**. Each agent is a Claude CLI session that spawns, does its job, writes a handoff, and dies. There is no "pause" -- Claude CLI processes don't persist state between invocations.

### Why Ephemeral Beats Persistent

| Concern | Persistent (keep alive) | Ephemeral (spawn/die) |
|---------|------------------------|----------------------|
| **Idle cost** | Holds memory/tokens while waiting for CI, review, human input | Zero cost while waiting |
| **Wait times** | CI: minutes. Review: hours. Human input: days. Expensive to hold. | Spawns only when there's work to do |
| **Context quality** | Degrades over long conversations (token bloat, attention drift) | Fresh context window every time |
| **Failure recovery** | Process crash = lost state | Handoff in DB = survives any crash |
| **Resource scaling** | One process per task, always running | One process per task, only when active |

**Decision**: agents die after completing their phase. The handoff protocol carries context forward.

### The Developer-Reviewer Cycle

```
[Developer spawns]
    |-- Reads: issue + researcher assessment + agent memory
    |-- Works: develop -> simplify -> self-review -> push
    |-- Creates PR, moves issue to "In Review"
    |-- Writes handoff: what was built, key decisions, known risks
    |-- DIES (process exits)
    |
    v
[SOVA orchestrator detects: PR created, needs review]
    |
    v
[Reviewer spawns]
    |-- Reads: PR diff + handoff + project review guidelines
    |-- Reviews: code quality, correctness, style, test coverage
    |-- Posts review on PR (approve / request changes)
    |-- Writes handoff: findings summary, severity, suggestions
    |-- DIES
    |
    v
[If changes requested: SOVA orchestrator detects review feedback]
    |
    v
[Developer spawns (NEW instance)]
    |-- Reads: PR diff + review comments + previous handoff + agent memory
    |-- Addresses each finding (fix, explain, or defer with justification)
    |-- Pushes fixes, re-requests review
    |-- Writes handoff: what was changed, which findings addressed
    |-- DIES
    |
    v
[Cycle repeats until approved or max_rounds reached]
    |
    v
[If approved: SOVA orchestrator merges, moves issue to Done]
[If max_rounds hit: pauses, sends notification to human]
```

### What Makes a Good Handoff

The handoff is stored in the DB (`TaskRun.handoff` JSON field) and captures enough context that a fresh agent can pick up without re-reading the entire codebase:

```python
class AgentHandoff(BaseModel):
    """Context passed between agent spawns."""
    # Who wrote this
    role: str                          # "developer", "reviewer"
    phase: str                         # "development", "review", "address_review"

    # What happened
    summary: str                       # 2-3 sentence summary of what was done
    key_decisions: list[str]           # Why certain approaches were chosen
    files_changed: list[str]           # Which files were modified
    tests_added: list[str]             # Which tests were added/modified

    # What's next
    next_action: str                   # "await_review", "address_findings", "await_ci"
    pending_findings: list[dict]       # Review findings not yet addressed
    blockers: list[str]                # What's blocking progress (if anything)
    needs_human: bool                  # Should a human be notified?
    human_message: str | None          # Message to show the human

    # References
    pr_number: int | None
    branch_name: str
    commit_shas: list[str]             # Commits this agent made
```

### Human-in-the-Loop: Notification and Wait

When the Developer encounters something it can't resolve autonomously:

1. **Agent posts a comment** on the PR/issue explaining what it needs
2. **Agent sends notification** (desktop notification, Slack, email -- configurable)
3. **Agent writes handoff** with `needs_human=True` and a clear `human_message`
4. **Agent DIES** (does not wait in a loop burning tokens)
5. **SOVA orchestrator watches** for human response (PR comment, issue update)
6. **When human responds**: orchestrator spawns a new Developer with the handoff + human's input

Scenarios that trigger human-in-the-loop:
- Review findings the agent disagrees with but can't override
- Test failures the agent can't diagnose after N retries
- Merge conflicts requiring judgment calls
- Access/permissions issues (missing secrets, protected branches)
- Budget limit approaching (`max_budget` in config)
- `max_rounds` of review cycles exceeded

### Orchestrator: The Immortal Process

While agents are ephemeral, the **SOVA orchestrator** (`sova server`) is the long-lived process that:
- Watches for state changes (new issues, PR events, review comments, CI results)
- Decides when to spawn which agent role
- Passes handoffs between agent spawns
- Enforces the mandatory pipeline (Triage -> Research -> Develop)
- Tracks costs and budgets across agent spawns
- Sends notifications when agents need human help

The orchestrator itself does NO LLM calls -- it's pure Python logic reading state from the DB and tracker.

---

## Failure Handling & Gate Checks

The current system's biggest flaw: no validation between steps. The new system adds **gate checks** -- each step must prove it produced valid output before the next step starts.

### Step Gate Checks

```python
# sova/core/steps/base.py
class BaseStep(ABC):
    name: str
    max_retries: int = 0

    @abstractmethod
    async def execute(self, ctx: ExecutionContext) -> StepResult: ...

    @abstractmethod
    async def validate_output(self, ctx: ExecutionContext) -> GateCheckResult: ...

    @abstractmethod
    async def can_skip(self, ctx: ExecutionContext) -> bool: ...
```

Specific gate checks:

| Step | Gate Check | On Failure |
|------|-----------|------------|
| Step 4 (Develop) | `git diff --stat` shows changed files; at least 1 non-config file modified | PAUSE with "Development produced no code changes" |
| Step 5 (Simplify) | Diff is still non-empty after simplification | PAUSE if all changes were reverted |
| Step 6 (Push) | Tests pass; invariants pass; branch has commits ahead of base | PAUSE with specific failure reason |
| Step 7 (PR) | PR number was extracted from `gh pr create` output | RETRY or PAUSE |
| Step 8 (CI) | At least one check ran; timeout is enforced | PAUSE with "CI never ran" or "CI timed out" |

### Failure Recording

Every failure is recorded in the DB with full context:

```python
class FailureRecord(Base):
    __tablename__ = "failure_records"
    id: int
    task_run_id: int  # FK to TaskRun
    step_name: str
    failure_type: str  # "gate_check", "exception", "timeout", "budget_exceeded"
    message: str
    context: dict  # JSON: stdout/stderr snippets, env state, config
    resolved: bool
    resolved_by: str | None  # "auto_retry", "user", "agent"
    created_at: datetime
```

### Dashboard Failure View

The dashboard gets a dedicated **Run Detail** page showing:
- Timeline of steps executed (with duration, cost, status)
- For failed steps: the exact error, relevant log output, and what the agent was trying to do
- Suggested next action (retry, modify issue, assign to human)
- One-click actions: "Retry from step N", "Pause and investigate", "Reassign to researcher"

---

## Architecture: Package Layout

```
project-automation-kit/
  sova/                               # Python package
    __init__.py
    __main__.py                       # python -m sova

    cli/                              # Typer CLI
      app.py                          # Main app, subcommand registration
      commands/
        run.py                        # sova run, sova watch, sova parallel
        project.py                    # sova install, sova setup
        pr.py                         # sova address-pr, maintain-pr, review-pr, learn-from-pr
        triage.py                     # sova triage
        memory.py                     # sova memory search/prune
        admin.py                      # sova status, costs, cleanup

    core/                             # Domain logic
      workflow.py                     # WorkflowEngine (state machine orchestrator)
      context.py                      # ExecutionContext (replaces bash globals)
      state.py                        # TaskStatus enum + FSM transitions
      steps/                          # One module per workflow step
        base.py                       # BaseStep ABC + GateCheck
        sync.py                       # Step 1: Sync main
        select_task.py                # Step 2: Task selection + assessment
        create_worktree.py            # Step 3: Worktree creation
        develop.py                    # Step 4: TDD development
        simplify.py                   # Step 5: Code quality
        self_review.py                # Step 5b: Self-review
        push.py                       # Step 6: Push + invariants
        create_pr.py                  # Step 7: PR creation
        monitor_ci.py                 # Step 8: CI monitoring
        automated_review.py           # Step 8b: Reviewer
        address_review.py             # Step 8c: Fix review findings
        complete.py                   # Step 9: Learn + complete

    roles/                            # Agent roles
      base.py                         # AgentRole ABC + TaskAssessment
      developer.py                    # Developer role (11-step workflow)
      researcher.py                   # Researcher role (investigate + assess)
      triage.py                       # Triage role (backlog assessment)
      reviewer.py                     # Reviewer role (PR review)
      dispatcher.py                   # Routes tasks to appropriate roles

    adapters/                         # Task source plugins
      base.py                         # TaskAdapter ABC (list, get, transition_state, assign, comment)
      github.py                       # GitHub Issues + Project Board state management
      jira.py                         # JIRA (workflow transitions)
      linear.py                       # Linear (status updates)
      manual.py                       # Manual/stdin

    llm/                              # LLM interaction
      claude.py                       # Claude CLI async wrapper
      prompt_builder.py               # Prompt assembly from templates
      cost_tracker.py                 # Cost recording + budget enforcement

    knowledge/                        # Knowledge & learning
      memory.py                       # SQLite FTS5 memory store
      tiers.py                        # 4-tier knowledge management
      personas.py                     # Tech stack detection + loading
      review_patterns.py              # Reviewer preference tracking

    git/                              # Git operations
      worktree.py                     # Worktree lifecycle
      operations.py                   # Push, rebase, branch management
      invariants.py                   # Pre-push checks (calls existing bash scripts)

    config/                           # Configuration
      models.py                       # Pydantic config models (~50 settings)
      loader.py                       # TOML loading + legacy .conf compat
      registry.py                     # Project registry (~/.config/sova/)

    ipc/                              # Inter-process communication
      control.py                      # Agent status, requests, responses
      handoff.py                      # Handoff protocol
      notifications.py                # Desktop + Slack notifications

    scheduler/                        # 24/7 operation
      watch.py                        # Watch mode (priority scan loop)
      parallel.py                     # Parallel task execution
      server.py                       # Combined dashboard + scheduler daemon

    dashboard/                        # Web UI (evolved from current)
      app.py                          # FastAPI app factory
      middleware.py                   # ProjectContextMiddleware
      routers/                        # API routes (existing 8 + new run-detail)
      services/                       # Business logic (use new DB models)
      templates/                      # Jinja2 HTML (reuse + enhance)
      static/                         # JS + CSS (reuse + enhance)

    db/                               # Database layer
      models.py                       # SQLAlchemy ORM models
      session.py                      # Async session factory
      migrations/                     # Alembic

    utils/                            # Shared utilities
      logging.py                      # structlog setup
      shell.py                        # Safe subprocess helpers
      formatting.py                   # Slug generation, text utils

  agent/                              # EXISTING: Bash agent (kept during migration)
  commands/                           # KEEP: Markdown commands (unchanged)
  personas/                           # KEEP: Persona files (unchanged)
  invariants/                         # KEEP: Bash invariant scripts (unchanged)
  templates/                          # KEEP: Project templates (unchanged)
  knowledge/                          # KEEP: Knowledge docs (unchanged)
  pyproject.toml                      # Package config
  sova.toml.default                   # Default project config template
```

---

## Core Abstractions

### ExecutionContext (replaces bash globals)
```python
@dataclass
class ExecutionContext:
    project_dir: Path
    config: ProjectConfig
    issue_number: int
    role: AgentRole              # Which agent role is executing
    branch_name: str = ""
    worktree_dir: Path | None = None
    pr_number: int | None = None
    session_id: str | None = None
    persona: Persona | None = None
    cost_usd: Decimal = Decimal("0")
    task_assessment: TaskAssessment | None = None
```

### Task State Machine
```
PENDING -> ASSESSING -> SELECTING -> WORKTREE_CREATED -> DEVELOPING
-> SIMPLIFYING -> REVIEWING -> PUSHING -> PR_CREATED -> CI_MONITORING
-> AUTOMATED_REVIEW -> ADDRESSING_REVIEW -> DONE
Any state -> PAUSED (user/error) or FAILED (unrecoverable)
PAUSED -> resume to last state
ASSESSING -> REJECTED (task not suitable for agent)
```

### Database Models

- **TaskRun**: audit trail per task execution (issue, status, step, branch, PR, cost, role, assessment, timestamps)
- **StepExecution**: record per step within a run (status, cost, duration, output, gate_check_result)
- **FailureRecord**: every failure with full context (step, type, message, context JSON, resolution)
- **CostRecord**: individual LLM invocation costs (phase, model, tokens, cost_usd)
- **Memory**: agent memories with FTS5 search (content, tags, tier)
- **TaskAssessmentRecord**: stored assessments for backlog issues (suitability, confidence, reasoning)

---

## Key Design Decisions

1. **Python unifies the stack** -- one language for CLI, agent, and dashboard. No bash/Python boundary.
2. **Role-based agents** -- instead of one monolithic agent, specialized roles handle different types of work. The dispatcher routes tasks to the right role based on assessment.
3. **Gate checks between steps** -- every step validates its output before the next step starts. This prevents the "empty development but continued to push" failure mode.
4. **Task assessment before work** -- every task goes through an assessment phase. The agent won't start development on a task it determines is underspecified, too complex, or outside its capabilities.
5. **Async throughout** -- CI polling, parallel Claude invocations (step 5), watch mode, WebSocket streaming all benefit from async.
6. **SQLite default, PostgreSQL optional** -- zero-config for single developer. Team servers set `SOVA_DATABASE_URL=postgresql://...`.
7. **Invariants stay as bash scripts** -- they're small, focused, and work. Python calls them via subprocess.
8. **Claude CLI remains the agent runtime** -- SOVA orchestrates (which task, which role, which step, when to retry) but Claude CLI does the actual coding.
9. **TOML config** -- typed, validated at startup, env var overrides via Pydantic Settings. Loader reads legacy `.conf` if `sova.toml` absent.
10. **24/7 server mode** -- `sova server start` runs dashboard + scheduler in one async process. Per-project watch loops as asyncio tasks.
11. **Full failure observability** -- every run, step, and failure is recorded in the DB. Dashboard shows timeline view with drill-down into failures.

---

## Data Flow: How a Task Moves Through the System

### Full Autonomous Pipeline (watch mode)

```
New issue created by human
    |
    v
[Triage Agent] Scans Backlog issues
    |-- Assess suitability with Claude
    |-- Label issue (agent:ready / needs-spec / needs-research / human-only)
    |-- Move to "Triaged" on project board
    |-- needs-spec? -> Post comment, STOP (wait for human)
    |-- human-only? -> STOP
    |
    v
[Researcher Agent] Picks up Triaged issues (agent:ready or agent:needs-research)
    |-- Explore codebase (read files, understand architecture)
    |-- Write assessment: affected files, approach, complexity, risks
    |-- Post assessment as issue comment
    |-- Create sub-issues if task is too large
    |-- Move to "Researched" on project board
    |
    v
[Developer Agent] Picks up "Researched" issues ONLY (Gate 3 enforced)
    |-- Assign self, move to "In Progress"
    |
    v
Step 1: Sync main
    |-- Gate: base branch is up to date
    v
Step 2: Select task (already selected)
    |-- Gate: task details loaded, researcher assessment available
    v
Step 3: Create worktree
    |-- Gate: worktree exists and is clean
    v
Step 4: Develop (Claude CLI)
    |-- Gate: git diff shows changed non-config files
    |-- FAIL? -> Record failure, move back to "Researched", notify dashboard
    v
Step 5: Simplify
    |-- Gate: changes still exist after simplification
    v
Step 5b: Self-review
    |-- Gate: review completed, findings addressed
    v
Step 6: Push
    |-- Gate: tests pass, invariants pass, push succeeded
    v
Step 7: Create PR
    |-- Gate: PR number extracted
    |-- Move to "In Review" on project board
    v
Step 8: Monitor CI
    |-- Gate: all checks passed (or classified + fixed)
    v
Step 8b: Automated review (Reviewer Agent)
    |-- Gate: review posted
    v
Step 8c: Address review
    |-- Gate: all findings resolved or deferred
    v
Step 9: Complete
    |-- Record learnings, update memory, notify
    |-- Move to "Done" on project board, close issue
    v
DONE
```

### Manual Mode (skip pipeline)

```
User runs: sova run 42 --force
    |
    v
[Developer Agent] Skips tracker state check, runs internal assessment only
    |-- If assessment says "not feasible" -> warn but proceed (user chose --force)
    v
(Steps 1-9 as above)
```

---

## Migration Phases

During migration, `sova` is the new CLI entry point (installed via pip). The old `pak` bash script continues to work. Both can read the same `.claude/` directory.

### Phase 0: Foundation -- COMPLETE
- `pyproject.toml`, `sova/config/`, `sova/db/`, `sova/utils/`, `sova/cli/app.py`
- 21 tests passing, `sova --version` works, config loads, DB initializes

### Phase 1: Adapters + LLM + Git Layer
GitHub Issues: #36, #37, #38

- Implement `sova/adapters/` -- port GitHub adapter (#38), add JIRA/Linear stubs
- Implement `sova/llm/claude.py` -- async Claude CLI wrapper with cost tracking (#37)
- Implement `sova/llm/prompt_builder.py` -- prompt assembly from command markdown
- Implement `sova/git/` -- worktree management, git operations, invariant runner (#36)
- Tests with mocked subprocess calls
- **Deliverable**: adapters list/get tasks, Claude can be invoked, worktrees can be created

### Phase 2: Core Workflow + Roles + IPC
GitHub Issues: #35, #39, #40, #41

- Implement `sova/core/state.py` -- FSM with transitions library (#35)
- Implement `sova/core/context.py` -- ExecutionContext (#35)
- Implement each step in `sova/core/steps/` with gate checks (#35)
- Implement `sova/core/workflow.py` -- WorkflowEngine (#35)
- Implement `sova/roles/` -- base role, developer, researcher, triage, reviewer, dispatcher (#39)
- Implement task assessment (`assess_task()` with Claude-based analysis) (#39)
- Implement `sova/ipc/` -- control files, handoff protocol (#40)
- Implement `sova/knowledge/` -- memory, tiers, personas, review patterns (#41)
- **Deliverable**: `sova run 42` assesses task, selects role, executes full workflow with gate checks

### Phase 3: CLI + Triage Command
GitHub Issue: #43

- Implement all Typer commands in `sova/cli/commands/`
- Implement `sova triage` -- backlog assessment and labeling
- Port remaining commands: watch, parallel, install, setup, address-pr, etc.
- **Deliverable**: feature parity with bash `pak` CLI + new triage command

### Phase 4: Dashboard Migration + Observability
GitHub Issue: #42

- Move dashboard into `sova/dashboard/` with app factory pattern
- Port services to use new DB models
- Add **Run Detail** page (step timeline, failure drill-down, suggested actions)
- Add **Triage View** (backlog with assessments, suitability labels)
- Enhance Control page with role-aware controls and failure context
- **Deliverable**: `sova dashboard` serves full UI with run observability

### Phase 5: Scheduler + Server Mode
GitHub Issue: #44

- Implement `sova/scheduler/watch.py` -- async watch loop with role dispatch
- Implement `sova/scheduler/parallel.py` -- concurrent task execution
- Implement `sova/scheduler/server.py` -- combined dashboard + scheduler
- Write systemd unit file and launchd plist
- **Deliverable**: `sova server start` runs 24/7 with auto-triage + development

### Phase 6: Cutover
GitHub Issue: #45

- Remove old bash `pak` script and `agent/` directory
- Migration script: convert `.conf` -> `sova.toml`
- Migration script: import JSONL cost data into SQLite
- Update all documentation (README, project name, branding)
- Update repository name if desired
- **Deliverable**: fully migrated, single Python codebase under the SOVA brand

---

## Verification Plan

- **Phase 0**: `sova --version`, `pytest sova/config/ sova/db/` pass, DB tables created
- **Phase 1**: `pytest sova/adapters/ sova/llm/ sova/git/` pass, integration test: list GitHub issues
- **Phase 2**: `pytest sova/core/ sova/roles/` pass, end-to-end: `sova run <test-issue>` with gate checks
- **Phase 3**: `sova --help` shows all commands, `sova triage --help` works
- **Phase 4**: dashboard serves all pages including run detail view
- **Phase 5**: `sova server start` runs watch + dashboard, survives restart
- **Phase 6**: `make check` passes, old `pak` removed, docs updated

Cross-cutting: `make check` runs `ruff check + ruff format --check + pytest` on `sova/` at every phase.

---

## What Stays the Same

- `commands/*.md` -- markdown command files (loaded by prompt_builder)
- `personas/*.md` -- persona files (loaded by personas.py)
- `invariants/*.sh` -- bash invariant scripts (called via subprocess)
- `templates/` -- project scaffolding templates
- `knowledge/KNOWLEDGE.md` -- knowledge system spec
- `.claude/` directory structure in target projects (backward compatible)
- Dashboard UI templates and static assets (reused, gradually enhanced)
