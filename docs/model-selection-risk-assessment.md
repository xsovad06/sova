# Model Selection Risk Assessment and Migration Path

Status: proposal for review (author: Opus 4.8, 2026-09-02)
Companion: [MODEL_SELECTION_ARCHITECTURE.md](MODEL_SELECTION_ARCHITECTURE.md),
[MODEL_SELECTION_TASK_PLAN.md](MODEL_SELECTION_TASK_PLAN.md).

These risks were surfaced by two adversarial critique passes over three candidate designs and
then verified against the real code. Each is rated by severity and paired with the mitigation
already folded into the task plan.

---

## 1. Critical risks

### R1: Fallback loop guillotined by the outer step timeout
The WorkflowEngine wraps each step in `asyncio.timeout(_step_timeout(step))`
([workflow.py](sova/core/workflow.py)). For TRIVIAL/SIMPLE/MODERATE the complexity multiplier is
**1.0** ([workflow.py:618-627](sova/core/workflow.py#L618-L627)), so the outer step budget
equals a single inner attempt's timeout. A naive client-side fallback loop that gives each
attempt the full timeout would let attempt #1 consume the entire budget; the outer timeout fires
before attempt #2, and the "recoverable" failure becomes a hard `step_hard_timeout` that even
commits WIP partial work. This converts the fix into a new failure mode.
- Mitigation: the chain walk shares **one deadline** derived from the step timeout, subtracting
  elapsed per attempt (PR3). Keystone test: SIMPLE-tier + 2-model chain completes attempt #2
  inside the step window.

### R2: Nested double fallback during the migration window
If a client-side fallback loop is added while `WorkflowEngine._advance_fallback`
([workflow.py:340-358](sova/core/workflow.py#L340-L358)) is still active, a single
billing/unavailable failure triggers both layers: the client walks its chain and exhausts, the
engine catches the (stringified) error, advances `ctx.resolved_model`, and re-runs the whole
step, which walks the chain again. Worst case is roughly N*N invocations and N times the token
cost. Tests still pass (both layers "work"), so the regression is invisible to the suite but
real in production cost.
- Mitigation: the PR that adds client fallback neuters the engine's model-advance in the **same
  commit**, behind a single flag (PR3). Rollback = flip the flag back to engine-owned.

---

## 2. High risks

### R3: Task-type routing reintroduces the unavailable-version bug
The task-type branch of `route_model` returns its override **without pinning**
([routing.py:106-110](sova/llm/routing.py#L106-L110)), while the complexity branches pin. Pinning
is the one mechanism that currently stops the CLI resolving `"opus"` to a version the deployment
lacks, which is the original reported failure. Wiring task-type routing without pinning would
re-open that exact hole.
- Mitigation: apply `_apply_pin` on the task-type path (PR4), with a dedicated regression test.

### R4: Typed-error `isinstance` checks are dead at the workflow layer
Steps stringify exceptions into `StepResult.error` before the workflow layer sees them
([workflow.py:452-454](sova/core/workflow.py#L452-L454)), and several steps catch `RuntimeError`
internally (e.g. [create_pr.py:373](sova/core/steps/create_pr.py#L373)). So the exception object
never reaches `_handle_step_failure_result` as an object; only a string does. A design that
deletes the string pattern table trusting `isinstance` would silently break workflow-level
billing detection.
- Mitigation: keep the string table as the load-bearing classifier at the workflow layer; if
  structured classification is needed there, propagate a `StepResult.error_category` field, not
  an exception type (PR1/PR2).

### R5: `normalize_model_name` is not in the invoke hot path
Only `anthropic_api` calls it; `claude_code`, `litellm`, and `client.py` never do. Any design
that "resolves aliases via `normalize_model_name`" is dead code.
- Mitigation: alias resolution is an explicit client-side step (PR8), with a test that `smart`/
  `cheap` resolve for a LiteLLM provider.

### R6: Budget guard goes blind on non-Anthropic / unknown models
The rate card returns `Decimal("0")` for unknown models
([models.py:78-119](sova/config/models.py#L78-L119)); the per-issue budget guard reads recorded
cost; `--max-budget-usd` is claude-code-only. Enabling Ollama/OpenAI without addressing this
removes the primary runaway-loop stop for exactly the providers the migration adds.
- Mitigation: a wall-clock/step-count runaway guard lands **before** any non-Anthropic provider
  (PR10); per-provider cost population and capability-gated budget enforcement follow (PR11).

### R7: Startup availability probing on every command and every subprocess
`_init_llm_provider` runs on the Typer callback, so it fires for every `sova` subcommand, and
every pipeline role is a fresh `sova run` subprocess that re-enters it. A startup network probe
would run on every spawn, add a hot-path hang surface, and break offline/CI use.
- Mitigation: availability is strictly lazy/JIT and fail-open; never probed in the CLI callback
  or the `spawn_direct` subprocess. Any optional warm-up is confined to the long-lived
  `sova server` behind a hard timeout (architecture Q2).

---

## 3. Medium risks

### R8: Backward-compat invariant mis-grounded as "opus everywhere"
`ctx.resolved_model` is complexity-routed with pinning, and `agent.model` is only the last-resort
fallback ([assess.py:48-60](sova/core/steps/assess.py#L48-L60)). A parity test asserting "opus
everywhere" would be wrong and would mask a routing regression.
- Mitigation: parity tests assert the correct model **per tier** (haiku/sonnet/opus) (PR4/PR5).

### R9: `invoke_command()` cannot route today
It has no `task_type` parameter ([client.py:213-240](sova/llm/client.py#L213-L240)), so tagging
the six slash-command steps would be a `TypeError`. And `invoke()` skips config load when `model`
is set ([client.py:96](sova/llm/client.py#L96)), so a configured route never overrides
`ctx.resolved_model`.
- Mitigation: add the parameter and load config even when `model` is set (PR4). Both are
  no-ops under empty `llm.routing`.

### R10: `develop.py:96` reroute is a real behavior change, not cleanup
Today it runs on `ctx.resolved_model or "haiku"`, i.e. the full model on a COMPLEX issue.
Rerouting to `extraction` (haiku default) changes cost/quality.
- Mitigation: framed and tested as an intentional change, excluded from the defaults-parity
  guarantee (PR6).

### R11: Load-time Pydantic model validation is infeasible
`load_config` runs per-invoke, often offline/keyless; enumerating catalogs in a validator would
be slow, flaky, and would block all commands on a raised `ValueError`, including valid-but-
unlisted models.
- Mitigation: model-name validation is an opt-in warn-only `sova doctor` check (PR14). Design 3's
  `strict_validation=raise` default is rejected.

### R12: New config fields silently no-op unless every `create_provider` call site is updated
`create_provider` is called from `_init_llm_provider`, dashboard `create_app`, and
`reload_provider` (the last passes only model/fallback/api_base). A new field missed at
`reload_provider` would stop applying after a settings hot-reload.
- Mitigation: `create_provider` takes the whole `LLMConfig` (PR8), with a `reload_provider`
  regression test.

### R13: Per-request provider selection cannot reach pipeline subprocesses
An in-dashboard-process provider/availability cache does not affect `developer`/`researcher`/
`planner`, which run as separate `sova run` processes with their own cold provider. Claiming it
"fixes the multi-project debt" is only half true.
- Mitigation: scope the claim to in-process calls; thread per-project selection into the
  subprocess via CLI flag or `SOVA_LLM_*` env at spawn time if/when needed (out of scope for the
  correctness fix; noted for the migration phase).

### R14: Batch and consensus/httpx bypass paths escape the choke point
`invoke_batch` sends bare aliases to an API needing full IDs; `git/rebase.py:146` and
`llm_suggestion_service.py` bypass `client.py` entirely. "One authoritative place" is overstated
until these are handled.
- Mitigation: batch alias normalization (PR9); explicit per-path decision for the two bypasses
  (PR15).

### R15: Reclassifying stdout can turn CLI-internal-fallback successes into spurious SOVA fallbacks
The partial-success guard ([claude_code.py:58-69](sova/llm/providers/claude_code.py#L58-L69))
swallows the CLI's own fallback warning-exit. If classification inspects stdout `result` text
for "not available" **before** this guard, a run the CLI already self-healed is reclassified as
`ModelUnavailableError` and triggers a second, redundant SOVA fallback (extra cost, drifting
cost attribution).
- Mitigation: preserve the guard ordering; classify **only** at the true error path
  ([claude_code.py:76-78](sova/llm/providers/claude_code.py#L76-L78)) (PR2), with a regression
  test that exit-1 + valid JSON + empty stderr still returns success and does not fall back.

---

## 4. Low risks (tracked, not blocking)

- Process-local availability/fallback state is lost on resume/restart, so "try a bad model at
  most once" is per-process, not per-issue. Document the guarantee (architecture Q2).
- `agent.model` pinning silently disables across families: migrating `agent.model` to a
  non-Anthropic ID makes the router emit bare tier aliases, so the run then depends on a complete
  `model_aliases` map covering every default-routing output. Mitigation: `sova doctor` warns when
  the alias map does not cover all tiers for a non-claude-code provider (PR14).
- New availability cache is another mutable global; it needs a `reset()` hook and mock-only tests
  (no Ollama daemon or keys in CI). Folded into PR3.
- `pr.py:32` and `mcp/tools.py:175` receive client-level fallback but no task_type routing;
  acknowledged explicitly so "32 sites covered" stays honest (PR7).

---

## 5. Migration path

Sequence is `PR1 -> ... -> PR15` (see the task plan dependency graph). Three natural stopping
points:

1. **After PR4**: the reported failure is fixed. The unavailability crash is gone (typed errors
   plus one client-owned fallback with a shared deadline), and task-type routing works with
   pinning intact. Nothing new is enabled; behavior under stock config is per-tier identical.
2. **After PR7**: all seven hardcoded literals are removed and every step routes by task_type;
   the system is fully config-driven but still Anthropic-only.
3. **After PR12**: OpenAI/Ollama/Vertex are selectable by config, with cost/runaway safety in
   place.

Behavior-preservation contract enforced at every step:
- New exceptions subclass `RuntimeError`; existing `except RuntimeError` and the workflow string
  classifier keep working.
- All new config fields and routes default to today's behavior; the existing suite is the gate.
- Provider default stays `claude-code`; the `provider` Literal is only ever extended alongside
  its matching `create_provider` case.
- Each PR ships a defaults-parity test keyed to per-tier resolution.

---

## 6. Rollback strategy

Every risky change is a config or flag flip, not a code revert:
- Fallback ownership is behind a single flag (PR3): flip back to engine-owned.
- Typed errors are additive and subclass `RuntimeError`: old catches still work if a later PR is
  reverted.
- `llm.routing`, `model_aliases`, `roles.reviewer_model` default to empty/current values: unset
  them to restore prior resolution.
- `provider` Literal extensions are opt-in and always shipped with their `create_provider` case,
  so a half-applied migration cannot crash startup for a user who set `provider="openai"`.
- The availability cache is fail-open and process-local: disabling it (or a probe failing)
  degrades to "try then route around", never to a block.

The one irreversible-by-flip change is PR6 (the `develop.py:96` / `lifecycle.py:340` haiku
reroute), which is a deliberate behavior change; roll it back by reverting that single small PR
or by setting `llm.routing.extraction` to the previous model.

---

## 7. Cost and effort estimate

- Phases 0 to 2 (PR1 to PR7, the correctness fix and de-hardcoding): the bulk of the value, all
  low-risk additive or flag-gated changes concentrated in `sova/llm/`, `sova/core/`, and
  `sova/roles/`. Highest-risk single PR is PR3 (fallback consolidation); it carries the keystone
  test.
- Phase 3 (PR8 to PR12, migration unblock): broader surface across `config/`, `llm/`, and cost
  tracking; gated by the runaway guard (PR10) before any non-Anthropic provider goes live.
- Phase 4 (PR13 to PR15): observability and cleanup; low risk.

The highest-leverage, lowest-risk slice is **PR1 to PR4**. If only the reported failure needs
fixing, that is the whole job; everything after is the provider-agnostic investment.
