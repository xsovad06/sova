# Model Selection Task Plan

Status: proposal for review (author: Opus 4.8, 2026-09-02)
Companion: [docs/model-selection-architecture.md](docs/model-selection-architecture.md),
[docs/model-selection-risk-assessment.md](docs/model-selection-risk-assessment.md).

15 PRs in 5 phases, ordered by dependency. Each is independently mergeable, keeps `main` green,
and rolls back via a config or flag flip. Phase 1 delivers the user-visible fix (the crash and
unified fallback); Phases 3 and beyond unblock OpenAI/Ollama migration and can be paused after
Phase 2 if only the correctness fix is wanted.

Global acceptance gate for **every** PR: the existing suite (`test_llm.py`,
`test_model_routing_pinning.py`, `test_model_fallback_cli.py`, `test_assess_step_routing.py`)
passes **unmodified** under stock config, and `make check` is clean.

Scope labels: `[llm]` `[config]` `[core]` `[roles]` `[dashboard]` `[git]` `[docs]`.

---

## Phase 0: foundations (no behavior change)

### PR1: Typed LLM error hierarchy `[llm]`
Add `sova/llm/errors.py`: `LLMError(RuntimeError)` base with `ModelUnavailableError`,
`RateLimitError`, `BillingError`, `ProviderUnavailableError`, `LLMTimeoutError`,
`LLMInvocationError`, plus `classify_error(detail: str) -> type[LLMError]` and
`is_fallback_eligible(exc) -> bool`. Move the pattern table out of
[workflow.py:34-44](sova/core/workflow.py#L34-L44) into `errors.py` as the single source; have
`_is_billing_failure` delegate to it. Nothing raises the new types yet.

Acceptance criteria:
- `classify_error` maps `"is not available"|"model_not_available"|"not_available"` ->
  `ModelUnavailableError`; `" 429"|"rate_limit"|"overloaded"` -> `RateLimitError`;
  `"billing"|"budget_exhausted"|"insufficient_quota"|"credit"` -> `BillingError`; unknown ->
  `LLMInvocationError`. Scan order is terminal-first (mirrors the current `" 429"` care).
- Every new class is a subclass of `RuntimeError` (assert in a test).
- `workflow.py._is_billing_failure` behavior is byte-identical (delegation only).
- Unit tests for `classify_error` over the full pattern set.

### PR2: Providers raise typed errors `[llm]`
`ClaudeCodeProvider.invoke`/`invoke_streaming` raise `classify_error(detail)(msg)` instead of
the bare `RuntimeError` at [claude_code.py:76-78](sova/llm/providers/claude_code.py#L76-L78),
reusing `_extract_failure_detail` so the message format is unchanged. Map
`litellm_provider` and `anthropic_api` SDK exceptions onto the hierarchy. `workflow._is_billing_failure`
adds an `isinstance` fast path but **keeps the string table as the load-bearing classifier**
(exceptions are stringified into `StepResult.error` before the workflow layer sees them, so
`isinstance` alone is not sufficient there).

Acceptance criteria:
- A simulated `"...is not available..."` stderr yields `ModelUnavailableError`.
- The exit-1-with-valid-JSON-and-empty-stderr partial-success path
  ([claude_code.py:58-69](sova/llm/providers/claude_code.py#L58-L69)) still returns success and
  is unchanged (regression test).
- All existing `except RuntimeError` sites keep catching (no signature or catch changes).

---

## Phase 1: fix the crash and unify fallback (core value)

### PR3: One client-owned fallback loop with a shared deadline `[llm][core]`
Add `_invoke_with_fallback()` in `client.py`, wired into `invoke()` and `invoke_command()`
(mirroring the existing compression wiring). Build the chain once
(`[resolved primary] + agent.fallback_models`, alias-resolved, de-duped); walk it on
`is_fallback_eligible` typed errors under **one deadline** computed from the incoming step
timeout (subtract elapsed per attempt). Add a process-local `ModelAvailabilityCache`
(negative-TTL, JIT, fail-open, `reset()` hook). **In the same PR**, neuter
`WorkflowEngine._advance_fallback` / `_has_fallback_models` behind a single flag
(default: engine-owned fallback off). Keep `ctx.fallback_model_index` /
`get_cli_fallback_model` as thin shims that seed the chain.

Acceptance criteria:
- Directly fixes the reviewer/create_pr/supervisor crash-on-unavailable (they gain fallback
  with no per-site change).
- Execution-path tests (fake provider): primary raises `ModelUnavailableError` -> advances ->
  second succeeds; exhaustion -> terminal `LLMError`; empty `fallback_models` -> single attempt,
  no loop.
- Keystone test: a SIMPLE-tier task (multiplier 1.0) with a 2-model chain completes attempt #2
  inside the WorkflowEngine step timeout (proves no guillotine).
- No nested double fallback: with the flag off, `_advance_fallback` does not re-run the step.
- `ModelAvailabilityCache.reset()` is called in the test fixture; fail-open asserted on probe
  error.
- Availability check is strictly lazy/JIT and fail-open (mitigates R7: no network probes in
  `_init_llm_provider` callback or `spawn_direct` subprocess).

### PR4: Activate task-type routing correctly (with pinning) `[llm][core]`
Add a `task_type` parameter to `invoke_command()` and route it through the resolver. Change
`invoke()` so config is loaded even when `model` is set, and adjust `_resolve_task_type_model`
so a **configured** route can override the passed model (documented semantic change; no-op under
empty `llm.routing`). **Apply `_apply_pin` on the task-type branch** in
[routing.py:106-110](sova/llm/routing.py#L106-L110). Add a `TASK_TYPE` tag to each step and pass
it at the invoke site while keeping `model=ctx.resolved_model or ctx.config.agent.model` as the
default. Drop the stale "not referenced yet" comment.

Acceptance criteria:
- Setting `llm.routing.develop = "opus"` changes the develop model; unset leaves resolution
  identical to today (per-tier parity test).
- `llm.routing.review = "haiku"` with a pinned `agent.model` still returns the pinned version
  (pinning regression test on the task_type path).
- `invoke_command(..., task_type=...)` no longer raises `TypeError` and resolves the route.
- The six slash-command steps (develop, simplify, self_review, research, address_review,
  rearrange_commits) receive a `task_type`.

---

## Phase 2: de-hardcode roles and config

### PR5: `roles.reviewer_model` and de-hardcode config-driven roles `[config][roles]`
Add `RolesConfig.reviewer_model: str = "sonnet"` (and optionally `developer_model`,
`planner_model`) plus `settings_meta.py` entries; extend `_ROLE_MODEL_FIELDS` with
`"review"`. Repoint `reviewer.py:471,489`, `panel_review.py:231` default, and
`supervisor/planner.py:36` off literals onto `resolve_model`/task_type with the current literals
as defaults.

Acceptance criteria:
- Under stock config, reviewer/panel/planner resolve to the same model as today (parity).
- Setting `roles.reviewer_model = "haiku"` routes the reviewer to haiku.
- `reviewer_model` appears in the settings UI (settings_meta registered).

### PR6: De-hardcode the two `"haiku"` sub-task defaults (intentional behavior change) `[core][roles]`
Repoint `develop.py:96` (implementation notes) and `knowledge/lifecycle.py:340` (memory
consolidation) to `task_type="extraction"`. This is **not** a no-op: today `develop.py:96` runs
on the full resolved model (`ctx.resolved_model or "haiku"`), so on a COMPLEX issue it runs on
opus. Framing it as `extraction` (default haiku) is a deliberate cost reduction.

Acceptance criteria:
- Documented and tested as an intentional behavior change (before: resolved_model; after:
  `llm.routing["extraction"]` or haiku default).
- Setting `llm.routing.extraction` overrides both sites.

### PR7: Wire `task_type` into remaining step sites plus shared arg-builder `[core][llm]`
Tag the remaining `ctx.resolved_model` step sites (create_pr -> `pr_body`, validate ->
`validate`, monitor_ci -> `monitor_ci`, spec keeps `researcher_model`, etc.). Extract one shared
CLI arg-builder used by both `ClaudeCodeRuntime.spawn` and `ClaudeCodeProvider._build_args` so
`--model`/`--fallback-model` cannot drift (the "two CLI invocation paths must stay in sync"
rule). Add an anti-hardcoding grep-guard test.

Acceptance criteria:
- Grep-guard test fails on any literal `model="..."` in an `invoke`/`invoke_command` call.
- Shared arg-builder test: identical flags for the same inputs across runtime and provider.
- `pr.py:32` and `mcp/tools.py:175` are explicitly acknowledged (inherit client fallback; no
  task_type by design).

---

## Phase 3: multi-provider readiness (unblock migration)

### PR8: Client-side alias map plus `create_provider(LLMConfig)` `[llm][config]`
Add `LLMConfig.model_aliases: dict[str,str] = {}` (settings_meta entry) resolved client-side in
`select_model`. Change `create_provider` to accept the whole `LLMConfig` so new fields cannot
skip a call site, and update all four call sites including `reload_provider`.

Acceptance criteria:
- `model_aliases = {"smart": "ollama/llama3.1:70b"}` resolves `smart` for a LiteLLM provider.
- `reload_provider` honors a changed alias map after a settings change (regression test for the
  documented "config-driven provider selection must be wired at startup" gotcha).
- Empty `model_aliases` reproduces today's resolution exactly.

### PR9: Batch path alias normalization `[llm]`
Normalize aliases before building `BatchRequest`s (or give the batch provider an alias map) so
`triage.py:473` -> `invoke_batch` -> `anthropic_batch` sends full model IDs, and run task_type
resolution for the batch path.

Acceptance criteria:
- A bare `"sonnet"` triage batch resolves to a full model ID before the batch API call.
- Batch requests are rebuilt without mutating caller objects (mirrors the existing
  `dataclasses.replace` pattern in `client.invoke_batch`).

### PR10: Provider-capability runaway guard `[core][llm]`
Add a wall-clock and/or step-count runaway guard independent of dollar cost, and a startup
warning when the configured provider `capabilities.reports_cost` is false. This must land
**before** any non-Anthropic provider is enabled. Also enforce the R7 design principle
(availability never probed at startup).

Acceptance criteria:
- With a fake provider reporting `cost_usd = 0`, a runaway loop is still stopped by the
  wall-clock/step-count guard.
- A `reports_cost = false` provider logs a startup warning about budget-cap degradation.
- Verify no network calls in `_init_llm_provider` callback or `spawn_direct` subprocess
  (mitigates R7: startup availability probing).

### PR11: Per-provider cost population and budget capability gating `[llm][core]`
Populate `result.cost_usd` for LiteLLM/OpenAI/Ollama where possible (litellm.completion_cost,
extended rate card, or explicit `$0` with a documented caveat); capability-gate the loss of the
claude-code `--max-budget-usd` per-run cap.

Acceptance criteria:
- Non-Anthropic runs record a non-`$0` cost where the provider can report it; where it cannot,
  the runaway guard from PR10 is the documented safety net.
- Budget enforcement path is chosen by `capabilities.supports_budget_cap`.

### PR12: New provider types (openai / ollama / vertex) `[llm][docs]`
Add the three types to `create_provider` (LiteLLM under the hood), extending the `provider`
Literal **in the same PR** as the matching `create_provider` case (so `provider="openai"`
between PRs cannot crash startup). Availability stays lazy/JIT/fail-open. Add a migration guide.

Acceptance criteria:
- `provider = "ollama"` with `model_aliases` runs against a faked LiteLLM/Ollama backend in
  tests (no daemon required).
- Setting an unknown provider yields a clear config error, not a stack trace.
- Migration guide documents the OpenAI/Ollama/Vertex config shape.

---

## Phase 4: observability and cleanup

### PR13: Per-tier / per-reason cost aggregation `[dashboard]`
Add a query/endpoint aggregating `CostRecord` by model and by parsed complexity tier /
`model_selection_reason`, plus a dashboard surface. This is the feedback loop that lets routing
decisions be validated and savings measured (`model_selection_reason` is already persisted at
[cost.py:44](sova/llm/cost.py#L44)).

Acceptance criteria:
- Endpoint returns cost rolled up by model and by tier/reason.
- Dashboard surface renders it.

### PR14: Opt-in `sova doctor` model-name validation `[cli][config]`
Add a warn-only model-name check to `sova doctor` (never a Pydantic validator): for enumerable
providers, warn when `agent.model`, `llm.fallback_models`, `llm.routing` values, or
`roles.*_model` do not resolve to a known model. Skip enumeration for claude-code and offline
Ollama.

Acceptance criteria:
- `sova doctor` warns on a typo'd model for an enumerable provider; never blocks config load.
- No network calls or key requirements on the normal config-load path.

### PR15: Bypass-path decision and final cleanup `[git][dashboard][docs]`
Decide per path: bring `git/rebase.py:146` consensus fan-out and
`llm_suggestion_service.py` httpx bypass onto the abstraction, or document them as intentional
exceptions. Remove the `WorkflowEngine._advance_fallback` shim now that the client owns
fallback. Refresh `AGENTS.md`, `.claude/rules/architecture.md`, `CLAUDE.md`, and doc counts.

Acceptance criteria:
- Each bypass path is either on the abstraction (typed errors, alias resolution) or has a code
  comment stating why it is intentionally exempt.
- Docs reflect the new schema, choke point, and provider list; doc counts updated.

---

## Dependency graph

```
PR1 -> PR2 -> PR3 -> PR4 -> PR5 -> PR6 -> PR7   (correctness spine; stop here for the fix only)
                       \-> PR8 -> PR9
                             \-> PR10 -> PR11 -> PR12   (migration; requires PR8)
PR13, PR14  independent after PR4/PR5 (need routing + reviewer_model)
PR15  last (needs client-owned fallback proven, PR3)
```

Minimum viable fix for the reported failure: **PR1 through PR4** (typed errors, client-owned
fallback with shared deadline, correct task-type routing with pinning). PR5 through PR7 remove
the hardcoding. PR8 onward is the OpenAI/Ollama/Vertex unblock.
