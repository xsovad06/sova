# Spec: LLM Planning Step for Supervisor (#602)

## Summary

Add a `SupervisorPlanner` that runs before `TaskProgressionEngine` each supervisor cycle. It assembles a resource snapshot and work state, loads the supervisor persona (#600), calls the LLM (Sonnet via direct Anthropic API), and returns a structured `PlanResult`. The deterministic engine then filters its decisions against the approved plan. Deterministic gates remain hard stops that the LLM cannot override.

## Motivation

The current supervisor fires actions mechanically when gate conditions pass. It cannot reason about trade-offs: "I have 2 CodeRabbit reviews left: should I push 3 PRs now or wait?" or "3 issues are researched and ready, but CI budget is 80% used: I'll start only one developer." The planner adds resource-aware reasoning without weakening the deterministic safety guarantees.

## Affected Files

| File | Action | Purpose |
|------|--------|---------|
| `sova/supervisor/planner.py` | Create | `SupervisorPlanner` class: context assembly, LLM call, response parsing |
| `sova/supervisor/daemon.py` | Modify | Call `planner.plan()` before `engine.evaluate_all()` in `_poll_progression()` |
| `sova/supervisor/progression.py` | Modify | `evaluate_all()` accepts optional `plan: PlanResult`; filters decisions against it |
| `sova/config/models.py` | Modify | Add `llm_planning: bool = False` to `SupervisorConfig` |
| `sova/dashboard/settings_meta.py` | Modify | Register `supervisor.llm_planning` setting |
| `sova/dashboard/services/supervisor_service.py` | Modify | Store `PlanResult.reasoning` alongside pending plan |
| `sova/dashboard/templates/supervisor.html` | Modify | Show reasoning + deferred list in pending actions panel |
| `sova/dashboard/routers/supervisor.py` | Modify | Include reasoning and deferred items in `GET /api/supervisor/plan` response |
| `tests/test_supervisor_planner.py` | Create | Unit tests for planner |
| `tests/test_progression_plan_filter.py` | Create | Tests for plan-filtered evaluate_all() |

## Detailed Design

### 1. Data Models (`sova/supervisor/planner.py`)

```python
@dataclass(frozen=True, slots=True)
class PlannedAction:
    action: str      # must match ProgressionAction enum values
    issue: int       # issue number
    priority: int    # 1 = highest
    reason: str      # LLM's reasoning for this action

@dataclass(frozen=True, slots=True)
class DeferredAction:
    action: str      # what would have been done
    issue: int       # issue number
    reason: str      # why it was deferred

@dataclass(frozen=True, slots=True)
class PlanResult:
    reasoning: str                    # overall reasoning narrative
    actions: tuple[PlannedAction, ...]   # approved actions, priority-ordered
    deferred: tuple[DeferredAction, ...]  # explicitly deferred with reasons
```

Tuples (not lists) for frozen dataclass compatibility and immutability.

### 2. SupervisorPlanner Class (`sova/supervisor/planner.py`)

```python
class SupervisorPlanner:
    def __init__(self, config: ProjectConfig, project_dir: Path,
                 session_factory: async_sessionmaker) -> None:
        ...

    async def plan(self, adapter: TaskAdapter) -> PlanResult | None:
        """Assemble context, call LLM, return structured plan.

        Returns None when:
        - ANTHROPIC_API_KEY is absent (logged once per process)
        - LLM call times out (30s)
        - LLM returns unparseable response
        - Any other error (logged, never raised)
        """
```

#### 2a. Context Assembly

The `plan()` method assembles a `_PlannerContext` (internal, not exported) from existing codebase sources:

| Data | Source | Call |
|------|--------|------|
| GitHub API quota | `sova/supervisor/github_quota.py` | `get_github_quota_tracker(identity).get_status()` |
| CodeRabbit quota | `sova/supervisor/coderabbit_quota.py` | `get_quota_status(session, config.coderabbit_quota)` |
| CI minutes budget | `sova/supervisor/ci_budget.py` | `get_ci_budget_tracker(identity).get_budget(repo, identity)` |
| Active agent count | `sova/supervisor/progression.py` | Reuse `_get_alive_count()` pattern (DB query for non-terminal TaskRuns with alive PIDs) |
| Agent slot limit | `sova/config/models.py` | `config.max_parallel_agents` |
| Open PRs with state | `gh pr list` | `asyncio.create_subprocess_exec("gh", "pr", "list", "--json", "number,title,reviewDecision,statusCheckRollup,mergeable,headRefName", "--limit", "20")` via subprocess |
| Issue counts by state | `adapter.list_tasks()` | Group by `agent:*` label to get counts per `TaskState` |
| Priority queue | `config.supervisor.task_queue` | Direct config read (already available) |
| Recent failures (24h) | DB query | `SELECT issue_number, role, status, error_message FROM task_runs WHERE status='failed' AND created_at > now()-24h` |
| Supervisor persona | `sova/supervisor/persona.py` | `load_persona(config.supervisor.persona_path)` |

The context is serialized to a structured text prompt. Each section is labeled and formatted for the LLM. The persona content is placed in the system message.

**Timeout and error handling for context assembly**: each data source is fetched with individual error handling. If a source fails (e.g., `gh pr list` times out), that section is omitted from the prompt with a note ("PR data unavailable"). Context assembly never blocks the cycle.

#### 2b. LLM Call

Follow the pattern from `sova/dashboard/services/llm_suggestion_service.py` (lines 95-141):

```python
_ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
_MODEL = "claude-sonnet-4-20250514"
_warned_no_key = False

async def _call_llm(self, system_prompt: str, user_prompt: str) -> dict | None:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        global _warned_no_key
        if not _warned_no_key:
            log.info("planner.no_api_key", detail="ANTHROPIC_API_KEY not set; LLM planning disabled")
            _warned_no_key = True
        return None

    async with httpx.AsyncClient() as client:
        response = await client.post(
            _ANTHROPIC_API_URL,
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": _MODEL,
                "max_tokens": 1024,
                "system": system_prompt,
                "messages": [{"role": "user", "content": user_prompt}],
            },
            timeout=30.0,
        )
        response.raise_for_status()
        data = response.json()
        raw_text = data["content"][0]["text"]
        return json.loads(raw_text)
```

**Key differences from `llm_suggestion_service.py`**:
- Model: Sonnet (not Haiku) for stronger reasoning about resource trade-offs
- `max_tokens`: 1024 (not 200) to accommodate reasoning + multiple actions
- System message: persona content (Haiku service uses user-only messages)
- Timeout: 30s (not 10s) for deeper reasoning
- No TTL cache: each cycle gets fresh context, caching would serve stale plans

#### 2c. Response Parsing and Validation

The LLM is instructed to return JSON matching this schema:

```json
{
  "reasoning": "string",
  "actions": [{"action": "string", "issue": 42, "priority": 1, "reason": "string"}],
  "deferred": [{"action": "string", "issue": 23, "reason": "string"}]
}
```

Validation rules:
1. `actions[].action` must be a valid `ProgressionAction` value (`spawn_researcher`, `spawn_developer`, `spawn_integrate`, `spawn_address_review`, `spawn_rebase`). Invalid values are dropped with a warning log.
2. `actions[].issue` must be a positive integer.
3. `actions[].priority` must be a positive integer. Defaults to list index + 1 if missing.
4. `deferred` is optional (defaults to empty).
5. `reasoning` is required; if missing, the entire response is treated as invalid (return None).

On any parse/validation failure, log a warning with the raw response text and return `None`.

### 3. Integration into Daemon (`sova/supervisor/daemon.py`)

Modify `_poll_progression()` (line 136):

```python
async def _poll_progression(self, adapter: TaskAdapter) -> tuple[dict, object]:
    try:
        from sova.supervisor.progression import NON_ACTIONABLE_ACTIONS, TaskProgressionEngine

        # --- NEW: LLM planning step ---
        plan = None
        if self._config.supervisor.llm_planning:
            from sova.supervisor.planner import SupervisorPlanner
            planner = SupervisorPlanner(
                config=self._config,
                project_dir=self._project_dir,
                session_factory=self._session_factory,
            )
            plan = await planner.plan(adapter)
        # --- END NEW ---

        engine = TaskProgressionEngine(
            config=self._config.supervisor,
            adapter=adapter,
            project_dir=self._project_dir,
            session_factory=self._session_factory,
        )
        decisions = await engine.evaluate_all(plan=plan)  # modified signature

        # ... rest unchanged (logging, approval/execution) ...
```

When `plan` is not None and `require_approval` is True, store the reasoning alongside the pending plan (see section 5).

### 4. Plan Filtering in Progression Engine (`sova/supervisor/progression.py`)

Modify `evaluate_all()` signature:

```python
async def evaluate_all(self, *, plan: PlanResult | None = None) -> list[ProgressionDecision]:
```

Filtering logic after the existing evaluation loop:

```python
if plan is not None:
    approved_set = {(a.action, a.issue) for a in plan.actions}
    filtered = []
    for d in decisions:
        if d.action in NON_ACTIONABLE_ACTIONS:
            # WAIT/BLOCKED/CHECKPOINT pass through unchanged
            filtered.append(d)
        elif (d.action.value, d.issue_number) in approved_set:
            filtered.append(d)
        else:
            log.info(
                "progression.plan_filtered",
                issue=d.issue_number,
                action=d.action.value,
                detail="not in LLM plan; skipped",
            )
            # Convert to WAIT so it appears in the decision log
            filtered.append(ProgressionDecision(
                issue_number=d.issue_number,
                action=ProgressionAction.WAIT,
                reason=f"not in LLM plan (deterministic: {d.action.value})",
                blocked_by=d.blocked_by,
                pr_number=d.pr_number,
            ))
    decisions = filtered
```

**Critical safety invariant**: filtering happens AFTER gate collection. A decision that was already BLOCKED or WAIT stays that way. The plan can only REMOVE actionable decisions, never ADD or UNBLOCK them. If the LLM approves `spawn_developer` for issue #17 but the memory pressure gate blocked it, the decision remains BLOCKED with the gate's `blocked_by` reasons.

The plan also cannot change the action type. If the engine says `spawn_researcher` for issue #17 and the plan says `spawn_developer` for #17, the match fails (different action) and the researcher spawn is filtered out.

### 5. Supervisor Service Changes (`sova/dashboard/services/supervisor_service.py`)

Add plan reasoning storage:

```python
_plan_reasoning: str | None = None
_plan_deferred: list[dict] = []

def set_pending_plan(
    decisions: list["ProgressionDecision"],
    *,
    reasoning: str | None = None,
    deferred: list[dict] | None = None,
) -> None:
    global _pending_plan, _plan_reasoning, _plan_deferred
    _pending_plan = list(decisions)
    _plan_reasoning = reasoning
    _plan_deferred = list(deferred) if deferred else []

def get_plan_reasoning() -> str | None:
    return _plan_reasoning

def get_plan_deferred() -> list[dict]:
    return list(_plan_deferred)
```

The daemon passes `plan.reasoning` and `plan.deferred` (serialized) when calling `set_pending_plan()`.

### 6. API Changes (`sova/dashboard/routers/supervisor.py`)

Extend `GET /api/supervisor/plan` response:

```python
@router.get("/plan")
async def get_plan() -> dict:
    from sova.dashboard.services.supervisor_service import (
        get_pending_plan, get_plan_reasoning, get_plan_deferred,
    )
    plan = get_pending_plan()
    return {
        "reasoning": get_plan_reasoning(),
        "pending": [
            {
                "issue_number": d.issue_number,
                "action": d.action.value,
                "role": d.role,
                "reason": d.reason,
                "blocked_by": [...],
            }
            for d in plan
        ],
        "deferred": get_plan_deferred(),
    }
```

No new endpoints. The existing approve/skip endpoints work unchanged since they operate on the same `_pending_plan` list.

### 7. Dashboard UI Changes (`sova/dashboard/templates/supervisor.html`)

Extend the existing pending actions panel (lines 81-93). No new page or component.

#### 7a. Reasoning section

When `reasoning` is non-null, render it above the action items:

```html
<div id="plan-reasoning" class="hidden mb-3 p-3 rounded bg-surface-overlay text-sm text-gray-300 leading-relaxed whitespace-pre-wrap border-l-2 border-accent-blue/40"></div>
```

#### 7b. Deferred section

Below the action items, show deferred actions when present:

```html
<div id="plan-deferred" class="hidden mt-3 pt-3 border-t border-gray-700/50">
  <h4 class="text-xs font-medium text-gray-500 uppercase tracking-wider mb-2">Deferred</h4>
  <div id="plan-deferred-items" class="space-y-1"></div>
</div>
```

Each deferred item rendered as a compact row with action, issue number, and reason. No action buttons (deferred items are informational).

#### 7c. JavaScript changes

In `loadPlan()` (line 1202):
- Read `response.reasoning` and populate `#plan-reasoning` (show/hide based on presence)
- Read `response.deferred` and render items in `#plan-deferred-items`
- Existing pending item rendering unchanged

### 8. Config Registration

#### 8a. `sova/config/models.py` (line ~540, inside `SupervisorConfig`)

```python
llm_planning: bool = False
```

No loader change needed: `supervisor` is already in `_NESTED_SECTIONS`.

#### 8b. `sova/dashboard/settings_meta.py`

Add to the supervisor settings block (after `supervisor.task_queue`, around line 1049):

```python
SettingMeta(
    key="supervisor.llm_planning",
    label="LLM Planning",
    description="Enable LLM-based planning before each supervisor cycle. "
    "Requires ANTHROPIC_API_KEY. When disabled, the supervisor runs in "
    "purely deterministic mode.",
    group="supervisor",
    value_type="boolean",
),
```

### 9. Prompt Design

#### System message

```
You are a supervisor planning agent for a software development fleet.
Your job is to decide which actions to take this cycle, given the current
resource constraints and work state.

{persona_content}

Respond with a JSON object (no markdown fences, no commentary):
{
  "reasoning": "Your chain-of-thought explanation of the plan",
  "actions": [
    {"action": "<action_type>", "issue": <number>, "priority": <number>, "reason": "Why this action now"}
  ],
  "deferred": [
    {"action": "<action_type>", "issue": <number>, "reason": "Why this is deferred"}
  ]
}

Valid action types: spawn_researcher, spawn_developer, spawn_integrate,
spawn_address_review, spawn_rebase.

Rules:
- Only include actions that make sense given available resources
- Prioritize actions that consume no scarce resources (merging approved PRs)
- Consider CodeRabbit review limits when deciding how many developers to spawn
- If CI budget is low, prefer merging over starting new work
- Deferred list should explain WHY each item is held back
- Empty actions list is valid (means "do nothing this cycle")
```

#### User message

Structured context assembled from section 2a, formatted as labeled sections:

```
## Resource Snapshot
- GitHub API: {remaining} calls remaining (limited: {yes/no})
- CodeRabbit: {used}/{limit} reviews this hour, next available in {minutes}m
- CI Budget: {used}/{total} minutes ({pct}% used)
- Agent Slots: {active}/{max} in use

## Open PRs
| # | Title | Review | CI | Mergeable |
...

## Issue Counts by State
- Backlog: N
- Triaged: N
- Researched: N (ready for development)
- In Progress: N
- In Review: N

## Priority Queue
{ordered list from config or empty}

## Recent Failures (24h)
{list of failed runs with issue, role, error summary}
```

### 10. Safety Guarantees

1. **Deterministic gates are inviolable**: the plan filters decisions AFTER gates have already blocked them. A blocked decision stays blocked.
2. **Plan can only subtract**: the plan removes actionable decisions, never adds new ones the engine didn't produce.
3. **Default off**: `supervisor.llm_planning = false` means zero LLM calls, zero cost, zero behavior change.
4. **No API key = silent skip**: logged once per process lifetime, then the planner returns `None` every cycle.
5. **Timeout = fallback**: 30s timeout on the LLM call. On timeout, return `None` and the engine runs in current deterministic mode.
6. **Parse failure = fallback**: if the LLM returns garbage, log and return `None`.
7. **No new failure modes**: `_poll_progression()` wraps the planner call in the existing try/except. A planner crash is logged and the cycle continues deterministically.

### 11. Logging

All log calls use `sova.utils.logging.get_logger(component="supervisor.planner")`:

| Event | Level | Fields |
|-------|-------|--------|
| `planner.no_api_key` | info (once) | - |
| `planner.context_assembled` | debug | section_count, persona_loaded |
| `planner.llm_call_start` | debug | model, prompt_length |
| `planner.llm_call_complete` | info | duration_ms, actions_count, deferred_count |
| `planner.llm_call_timeout` | warning | timeout_seconds |
| `planner.llm_call_error` | warning | error, status_code |
| `planner.parse_error` | warning | raw_response (truncated) |
| `planner.invalid_action` | warning | action, issue |
| `progression.plan_filtered` | info | issue, action, detail |

### 12. Cost

Sonnet with ~2000 token input + 1024 max output: ~$0.02 per cycle. At 120s poll interval, ~$0.60/hour if running continuously. Cost is only incurred when `llm_planning=true` AND `ANTHROPIC_API_KEY` is set.

### 13. Testing Strategy

#### Unit tests (`tests/test_supervisor_planner.py`)

1. **Context assembly**: mock all data sources, verify prompt contains expected sections
2. **LLM call**: mock httpx, verify request format (headers, model, system/user messages)
3. **Response parsing**: valid JSON with all fields, missing optional fields, invalid action names, malformed JSON
4. **No API key**: verify returns None, logs once
5. **Timeout**: mock httpx to raise `httpx.TimeoutException`, verify returns None
6. **API error**: mock httpx 500/429 responses, verify returns None

#### Integration tests (`tests/test_progression_plan_filter.py`)

1. **Plan filters actionable decisions**: engine produces 3 actionable, plan approves 1, verify 1 actionable + 2 WAIT in output
2. **Plan preserves blocked decisions**: blocked decisions pass through regardless of plan
3. **Plan=None means no filtering**: all decisions pass through (backward compatible)
4. **Action-type mismatch**: plan approves `spawn_developer` for issue, engine says `spawn_researcher` for same issue, verify researcher is filtered out
5. **Plan with empty actions**: all actionable decisions converted to WAIT

#### Dashboard tests

1. **API response**: verify `GET /api/supervisor/plan` includes `reasoning` and `deferred` fields
2. **Backward compatibility**: when no plan reasoning exists, `reasoning` is null and `deferred` is empty list

## Non-Goals

- No new dashboard page (extends existing supervisor page only)
- No persistent plan storage in DB (ephemeral, rebuilt each cycle like current pending plan)
- No plan history or diff between cycles (future work)
- No LLM-driven gate override (gates are deterministic, period)
- No batching/streaming of the LLM call
- No Anthropic SDK dependency (raw httpx, matching existing pattern)

## Dependencies

- #600 (persona config): merged
- #601 (CI minutes tracking): merged
