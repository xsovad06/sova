# Model Selection Architecture

Status: proposal for review (author: Opus 4.8, 2026-09-02)
Grounding: every claim below is verified against the current code at file:line. Where the
original briefing (`MODEL_SELECTION_*.md`) was wrong, the correction is called out inline.
Companion documents: [MODEL_SELECTION_TASK_PLAN.md](MODEL_SELECTION_TASK_PLAN.md),
[MODEL_SELECTION_RISK_ASSESSMENT.md](MODEL_SELECTION_RISK_ASSESSMENT.md).

---

## 1. Executive summary

SOVA's model selection is not one broken thing; it is four disconnected mechanisms plus one
crash path. The verified root causes are:

1. **The unavailability crash.** `ClaudeCodeProvider.invoke()` has a partial-success recovery
   path (parse valid JSON even when the CLI exits 1) but it is gated on *empty stderr*
   ([claude_code.py:58](sova/llm/providers/claude_code.py#L58)). A "model not available"
   warning is written to stderr, which defeats the recovery, falls through to
   [claude_code.py:76-78](sova/llm/providers/claude_code.py#L76-L78), and raises a bare,
   untyped `RuntimeError`. No caller can distinguish "unavailable, try another model" from
   "real failure". (The briefing's claim that SOVA "logs it and moves on" is wrong; it hard
   crashes.)

2. **Task-type routing is wired to nothing.** `route_model(..., task_type=...)` exists
   ([routing.py:88-110](sova/llm/routing.py#L88-L110)) and `_resolve_task_type_model` exists
   ([client.py:122-145](sova/llm/client.py#L122-L145)), but two facts kill it: `invoke()`
   only loads config when `model is None` ([client.py:96](sova/llm/client.py#L96)) and every
   step passes `model=ctx.resolved_model`, so the task_type branch is never reached; and
   `invoke_command()` does not even accept a `task_type` parameter
   ([client.py:213-240](sova/llm/client.py#L213-L240)), so the six slash-command steps
   (develop, simplify, self_review, research, address_review, rearrange_commits) cannot route
   at all.

3. **Role hardcoding.** Seven literal model names bypass all config:
   `reviewer.py:471,489` (`"sonnet"`), `panel_review.py:231` (`"sonnet"` default),
   `supervisor/planner.py:36` (`"sonnet"`), `develop.py:96` (`"haiku"` fallback),
   `knowledge/lifecycle.py:340` (`"haiku"`), and `llm_suggestion_service.py:31-32`
   (full version strings, via a direct httpx bypass of the abstraction). Correction to the
   briefing: `RolesConfig` has `researcher_model` and `triage_model` but **no**
   `reviewer_model` ([models.py:288-297](sova/config/models.py#L288-L297)), so the reviewer
   literally has no config field to read even if we un-hardcode it.

   *Resolved for the first three by PR5 (#914):* `RolesConfig` now carries
   `reviewer_model` (`"sonnet"`), `developer_model` (`""`), and `planner_model` (`"sonnet"`),
   registered in `_ROLE_MODEL_FIELDS` under both the task-type key (`"review"`) and the role
   name (`"reviewer"`). The reviewer resolves once per review and reuses the result for every
   diff chunk, every schema retry, and as the panel's `default_model`; the supervisor planner
   resolves in `plan()` and passes the model into `_call_llm`. `developer_model` defaults to
   empty on purpose: role config is consulted *before* complexity routing, so a non-empty
   default would silently pin every developer run to one model. The two `"haiku"` literals and
   the suggestion service remain, by design, for PR6 and a later PR.

4. **Fallback is split across three layers that do not cooperate.** The Claude CLI's own
   `--fallback-model` (fast, Anthropic-only, opaque to SOVA), `WorkflowEngine._advance_fallback`
   ([workflow.py:340-358](sova/core/workflow.py#L340-L358)), and per-site
   `fallback_model=ctx.get_cli_fallback_model()` passing that only 7 of 14 step sites actually
   do. Steps that create PRs, validate, monitor CI, generate tasks, and write specs get no
   intra-session model fallback at all.

5. **No availability detection and no vendor-neutral config.** `check_available()` returns a
   binary `tuple[bool, str]` ([provider.py:178](sova/llm/provider.py#L178)); nothing can ask
   "which models exist?". `agent.model="opus"` is an Anthropic-only alias with no mapping layer,
   which is what blocks OpenAI/Ollama migration.

The recommended solution is the **minimal-change spine** (design 1) corrected by the two
adversarial critiques, with the provider-agnostic end-state (design 2) reached incrementally in
a later phase. We do **not** adopt the largest design wholesale (startup probing and per-request
provider caches that cannot reach pipeline subprocesses), and we drop load-time Pydantic
model-name validation (infeasible on the hot config path).

The single most important architectural rule that emerged from the critique: **fallback must
have exactly one owner, land in one atomic flag-gated PR, and walk its chain under one shared
deadline derived from the WorkflowEngine step timeout** (otherwise the outer
`asyncio.timeout` guillotines the second attempt and turns a recoverable failure into a hard
timeout, because the complexity multiplier is 1.0 for TRIVIAL/SIMPLE/MODERATE, so the outer
step budget equals a single inner attempt's budget:
[workflow.py:618-627](sova/core/workflow.py#L618-L627)).

---

## 2. Verified current-state map

### 2.1 The abstraction

- `LLMProvider` ABC ([provider.py](sova/llm/provider.py)): 3 abstract methods (`invoke`,
  `invoke_streaming`, `check_available`) plus concrete template-method defaults
  (`invoke_command`, `invoke_batch`, `normalize_model_name`). `create_provider` factory
  dispatches `claude-code` / `litellm` / `hybrid` / `anthropic`.
- Global singleton via `get_provider()` / `set_provider()` / `reload_provider()`
  ([client.py](sova/llm/client.py)). Created once at CLI startup (`_init_llm_provider`) and
  dashboard startup (`create_app`).
- `normalize_model_name` is **not in the invoke hot path**: only `anthropic_api.py` calls it.
  `ClaudeCodeProvider` and `LiteLLMProvider` never do, and `client.py` never does. Therefore
  provider-owned alias resolution is effectively dead today, and any design that "resolves
  aliases via normalize_model_name" is a no-op. Alias resolution must be an explicit
  client-side step.

### 2.2 The four selection mechanisms

| Mechanism | Location | State | Applies pinning? |
|---|---|---|---|
| Complexity routing | [routing.py:26-32,112-119](sova/llm/routing.py#L112-L119) | works | yes |
| Config override (`llm.routing[tier]`) | [routing.py:113-116](sova/llm/routing.py#L113-L116) | works | yes |
| Task-type routing (`llm.routing[task_type]`) | [routing.py:106-110](sova/llm/routing.py#L106-L110) | dead in the pipeline; live only via `harden.py`, `batch_service.py`, `planner.py` | **no (bug)** |
| Role config (`researcher_model`, `triage_model`, `reviewer_model`, `developer_model`, `planner_model`) | [client.py:601-616](sova/llm/client.py#L601-L616) | works | only on the complexity fallback, never on the role field itself |

Correction to the briefing: task-type routing is *not* entirely dead code. `harden.py:116`,
`batch_service.py:335`, `roles/planner.py:133`, and `supervisor/planner.py:416` pass
`task_type` today (`reviewer.py:494,513` joined them in PR5). It is dead only in the
developer/reviewer/validate pipeline steps.

### 2.3 The pinning trap (critical)

Pinning exists precisely to stop the CLI resolving `"opus"` to a newer version the deployment
does not have ([routing.py:99-102](sova/llm/routing.py#L99-L102)). But the task-type branch
returns its override **without** `_apply_pin` ([routing.py:106-110](sova/llm/routing.py#L106-L110)),
whereas the complexity branches do pin ([routing.py:116,119](sova/llm/routing.py#L116-L119)).
So "just wire up task-type routing" reintroduces the exact unavailable-version bug this whole
effort exists to fix. **The task-type path must also pin.**

### 2.4 What `ctx.resolved_model` actually is

`AssessStep` sets it via `resolve_model(role, roles, complexity, llm_config, agent_model)`
([assess.py:48-60](sova/core/steps/assess.py#L48-L60)), which returns the role config first,
else the complexity route with pinning; `agent.model` is only the last-resort fallback
(line 59). So a MODERATE issue runs on `sonnet`, a COMPLEX issue on the pinned opus, a TRIVIAL
issue on `haiku`. Correction to the briefing and to designs 1 and 3: the backward-compat
invariant is **not** "opus everywhere". Any defaults-parity test must assert the correct model
**per complexity tier**, or it will mask a real routing regression.

### 2.5 Invocation inventory (32 sites)

22 direct `invoke()` and 10 `invoke_command()` sites across `roles/`, `core/steps/`,
`supervisor/`, `dashboard/services/`, `git/`, `knowledge/`, `cli/commands/`, `mcp/`. Two
deliberately bypass the client abstraction: `git/rebase.py:146` (multi-model consensus fan-out)
and `dashboard/services/llm_suggestion_service.py` (direct httpx to Anthropic/Vertex). The
batch path (`triage.py:473` -> `invoke_batch` -> `anthropic_batch.py`) sends bare aliases to an
API that needs full model IDs and performs no alias normalization.

### 2.6 Cost and budget coupling

The rate card is Anthropic-only and returns `Decimal("0")` for unknown models
([models.py:78-119](sova/config/models.py#L78-L119)); the per-issue budget guard reads the
recorded cost. `--max-budget-usd` is a claude-code CLI-only per-run cap. Consequence: the
moment a non-Anthropic or unknown model runs, cost is recorded as `$0`, the dollar-budget
runaway guard goes blind, and switching off claude-code loses the per-run cap entirely. A
wall-clock / step-count runaway guard must exist **before** any non-Anthropic provider is
enabled.

---

## 3. Answers to the eight investigation questions

### Q1. How do we model different providers in one abstraction?

Keep the existing 3-abstract-method `LLMProvider` ABC and add capability introspection as
**concrete defaults** (never new abstract methods, so no existing or third-party provider
breaks):

- `capabilities -> ProviderCapabilities` (supports_cli_fallback, supports_budget_cap,
  reports_cost, dynamic_models). This is what lets the fallback and budget layers stop
  assuming Claude-CLI features.
- `list_models() -> list[ModelInfo]`, default `[]` meaning "cannot enumerate".
- `supports_model(model) -> tuple[bool | None, str]`, default derived from `list_models()`;
  `None` means "cannot verify, treat as available" (fail-open).

LiteLLM is the universal adapter for OpenAI / Ollama / Vertex (prefixed IDs like
`openai/gpt-5`, `ollama/llama3.1`, `vertex_ai/claude-...`), so adding those backends is
config plus one `create_provider` case, not new provider classes. `create_provider` should
accept the whole `LLMConfig` (not a growing positional signature) so a new field can never be
silently dropped by one of its four call sites (notably `reload_provider`, the config
hot-reload path).

### Q2. Availability: startup or just-in-time?

**Just-in-time and fail-open. Never probe in the CLI callback or in the `spawn_direct`
subprocess.** The critique caught a fatal flaw in startup probing: `_init_llm_provider` runs on
the Typer `@app.callback()` ([cli/app.py](sova/cli/app.py)), so it fires for *every* `sova`
subcommand, and every pipeline role is spawned as a fresh `sova run` subprocess
([runtime.py spawn_direct](sova/ipc/runtime.py)), which re-enters that callback. A network
probe there would run at the start of every agent spawn and every CLI command, add a hang
surface on the hot path, and break offline/CI use.

The strategy:
- A process-local `ModelAvailabilityCache` keyed by `(provider_identity, resolved_model_id)`
  with a short TTL and a shorter negative TTL, plus a `reset()` hook for test isolation.
- Populated reactively: when the client's fallback loop catches `ModelUnavailableError` for
  model X, it records X unavailable *before* trying the next candidate, so a bad model is tried
  at most once per process.
- Ollama's dynamic set is handled by a short TTL and a live `GET /api/tags` on cache miss;
  daemon-down maps to `ProviderUnavailableError` and fails open.
- Optional: a long-lived `sova server` process may warm the cache once at startup behind a hard
  (<= 2s) timeout. This is an optimization, never a correctness dependency.

Guarantee framing: "a bad model is tried at most once" is **per-process**, not per-issue
(process-local caches are lost on resume/restart). Document it as such.

### Q3. Minimal vendor-agnostic config schema

Both target sections (`llm`, `roles`) are already in `_NESTED_SECTIONS`, so no loader change is
needed; only `models.py` fields plus `settings_meta.py` entries (field-level triple
registration). Additions, all backward-compatible by default:

- `LLMConfig.model_aliases: dict[str, str] = {}`: the vendor-neutrality lever. Maps generic
  tiers (opus/sonnet/haiku/fast/smart/cheap) to provider-native IDs per deployment. Empty
  default preserves today's behavior. Resolved **client-side** (not via `normalize_model_name`).
- `RolesConfig.reviewer_model: str = "sonnet"` (and optionally `developer_model`,
  `planner_model`, default `""` = fall through). Register each in `_ROLE_MODEL_FIELDS`.
- Keep both model fields, now documented: `AgentConfig.model="opus"` is the runtime primary
  tier fed into pinning; `LLMConfig.model` is the provider-level default for non-CLI providers.

No Pydantic model-name validator (see Q6/Q3-rationale below). `dict[str,str]` renders in the
settings UI (precedent: `dimension_models`); verify `list`/enum value types render before
shipping those fields.

Correction to design 3: load-time model-name validation is **architecturally infeasible**.
`load_config()` runs per-invoke (`client.py:96`, `_resolve_timeout`, `maybe_compress`), often
offline and keyless (CI, tests). Enumerating provider catalogs synchronously in a Pydantic
`model_validator` would be slow, flaky, key-dependent, and would break the default claude-code
path (which cannot enumerate anyway). Validation belongs in an opt-in `sova doctor` check.

### Q4. Where should model selection happen?

One resolution choke point in `client.py` (`select_model`), unifying the three fragmented
paths with this precedence (most specific first):

```
explicit model= arg  >  llm.routing[task_type]  >  role config (_ROLE_MODEL_FIELDS)
  >  complexity route (route_model, with pinning)  >  ctx.resolved_model  >  agent.model
```

Two wiring facts must be fixed for this to actually fire (both missed by design 1):
1. `invoke()` must load config even when `model` is provided, so a configured task_type route
   can override the passed `ctx.resolved_model`. This is a deliberate, documented semantic
   change; it is a no-op under the default empty `llm.routing`.
2. `invoke_command()` must gain a `task_type` parameter and route it through the resolver;
   otherwise the six slash-command steps get no routing.

And pinning must be applied on the task_type branch (section 2.3).

### Q5. Fallback: SOVA-orchestrated or provider-delegated?

**Hybrid, but SOVA is the source of truth.** One client-owned fallback loop builds the chain
once (`[resolved primary] + agent.fallback_models`, alias-resolved and de-duped), walks it on
fallback-eligible error *categories*, records unavailable models in the cache, and re-raises a
terminal error only on exhaustion. The category comes from the exception's type once providers
raise the typed hierarchy, and from its message until then, so the loop is not dead while the
provider layer still raises bare `RuntimeError`. The Claude CLI's `--fallback-model` stays as a fast,
provider-internal, capability-gated inner layer: SOVA hands it the same next chain hop it would
pick, so CLI-internal and SOVA-level fallback agree, and cost is reconciled via `result.model`.
Cross-provider fallback (claude -> ollama) can only happen in the client loop.

Two non-negotiable constraints from the critique:
- The chain walk shares **one deadline** computed from the step timeout (subtract elapsed per
  attempt), so attempt #2 is never guillotined by the outer `asyncio.timeout`.
- The PR that adds client-level fallback **in the same commit** neuters
  `WorkflowEngine._advance_fallback` behind a single flag, to avoid nested N*N double fallback
  during the migration window.

**Accepted gap: `ctx.resolved_model` is not updated by the client-owned loop.** With
`llm.engine_owned_fallback=False` (the default), a step that recovers via a fallback candidate
does not write the winner back to `ExecutionContext`, so the next step still calls
`invoke(model=ctx.resolved_model or ctx.config.agent.model, ...)` with the original (possibly
still-unavailable) primary and relies on `ModelAvailabilityCache`'s TTL to skip it quickly. This
is deliberate, not an oversight: `LLMResult.model` is not a uniform signal to propagate, since
`providers/claude_code.py` echoes back the alias it was given while `providers/anthropic_api.py`
sets it to `response.model`, the concrete API model ID. Writing that back into
`ctx.resolved_model` unconditionally would work for claude-code but silently break every
alias-based comparison downstream (`_advance_fallback`, `route_model`, `_ROLE_MODEL_FIELDS`) the
moment a non-claude-code provider is active. Propagating the winner correctly needs client-side
alias resolution (`LLMConfig.model_aliases`, Q3) landing first so the loop can report "which
alias won" rather than "what the provider echoed"; that is PR8 scope. Until then, the practical
mitigation is the availability cache's TTL: keep it short enough that a dead model is not retried
across steps for long, and long enough to avoid re-probing a model that is actually still down.

### Q6. Keeping `agent.model="opus"` working

Guaranteed by: (a) generic aliases stay in the default alias set and resolve to native IDs;
(b) pinning behavior in `route_model` is preserved and now also applied on the task_type path;
(c) new exceptions subclass `RuntimeError`, so `except RuntimeError` sites
([create_pr.py:373](sova/core/steps/create_pr.py#L373)) and the WorkflowEngine string
classifier keep working; (d) all new config fields default to today's behavior; (e) the
existing suite (`test_llm.py`, `test_model_routing_pinning.py`, `test_model_fallback_cli.py`,
`test_assess_step_routing.py`) must pass **unmodified** as the acceptance gate. The parity
tests assert per-tier models (haiku/sonnet/opus), not "opus everywhere".

### Q7. Testing provider switching without heavyweight deps

- Typed-error classification: unit tests feeding real CLI stderr/stdout-JSON fixtures through
  `classify_error`, including the exit-1-with-valid-JSON-and-empty-stderr partial-success case
  (must still return success and must **not** trigger client fallback).
- Fallback **execution** path (not just parameter passthrough, which is all the current tests
  do): a fake provider whose first model raises `ModelUnavailableError` and whose second
  succeeds; assert advance, exhaustion-terminal, and empty-chain-no-op.
- The keystone test: a SIMPLE-tier task with a 2-model chain must complete attempt #2 inside
  the WorkflowEngine step timeout (proves the shared-deadline fix).
- Availability: inject fake `list_models` / `/api/tags` / `models.list()`; assert fail-open on
  probe error; reset the cache in the fixture.
- `reload_provider` honors a changed alias map (config hot-reload regression).
- Anti-hardcoding grep guard: a test that fails if any `invoke`/`invoke_command` call passes a
  string literal `model=`.

No Ollama daemon or API keys in CI; everything is mocked/faked.

### Q8. Migration order

See [MODEL_SELECTION_TASK_PLAN.md](MODEL_SELECTION_TASK_PLAN.md). In brief: typed errors ->
crash fix plus unified fallback -> routing wiring plus pinning -> de-hardcode roles ->
multi-provider config and cost/runaway guards -> new provider types -> observability and
cleanup. Every risky PR is a config or flag flip to roll back.

---

## 4. Target architecture (end state)

```
config (sova.db)
  llm.provider           claude-code | litellm | hybrid | anthropic | openai | ollama | vertex
  llm.model_aliases      { opus: claude-opus-4-8, smart: ollama/llama3.1:70b, ... }
  llm.routing            { develop: sonnet, review: haiku, complex: opus, ... }  (task_type + tier)
  agent.model            opus   (primary tier, drives pinning)
  agent.fallback_models  [sonnet, haiku]
  roles.reviewer_model   sonnet

        v
AssessStep -> ctx.resolved_model  (complexity route + pin; per tier)

        v
step / role  ->  invoke(prompt, model=ctx.resolved_model, task_type="<step>")
             ->  invoke_command("/cmd", args, model=..., task_type="<step>")

        v
sova/llm/client.py  (ONE choke point)
  guard_prompt -> maybe_compress -> select_model(precedence) -> alias map (client-side)
    -> _invoke_with_fallback(chain, shared_deadline):
         try candidate -> on fallback-eligible category: cache-unavailable, advance
         exhausted -> raise terminal

        v
provider.invoke(model=native_id, fallback_model=next_hop)   [typed errors, subclass RuntimeError]
  claude-code | litellm(openai/ollama/vertex) | anthropic-api | anthropic-batch

        v
ModelAvailabilityCache (process-local, JIT, fail-open, reset hook)
CostRecord (model, model_selection_reason)  ->  per-tier aggregation (dashboard)
```

Residual uncovered surface (explicitly decided, not hand-waved): `git/rebase.py` consensus
fan-out and `llm_suggestion_service.py` httpx bypass do not pass through `client.py`. They are
brought onto the abstraction (or documented as intentional exceptions) in the final cleanup PR,
so no plan overstates "one authoritative place" while these exist.

---

## 5. What we explicitly reject and why

- **Startup / provider-init availability probing** (designs 2 and 3 PR5): runs on every CLI
  command and every pipeline subprocess spawn; breaks offline/CI; adds a hot-path hang surface.
- **Load-time Pydantic model-name validation** (design 3): `load_config` is a per-invoke,
  often-offline hot path; a raised `ValueError` at load blocks all commands until the config is
  edited, and a valid-but-unlisted model (new release, fine-tune, not-yet-pulled Ollama tag)
  would fail startup even though it works. Use `sova doctor` instead.
- **Per-request provider cache as the multi-project fix** (design 2 PR8): it lives in the
  dashboard process and cannot reach `developer`/`researcher`/`planner`, which run as separate
  `sova run` subprocesses with their own cold provider. Per-project selection must be threaded
  into the subprocess via CLI flag or `SOVA_LLM_*` env at spawn time, not an in-process cache.
- **Relying on `normalize_model_name` as the alias choke point** (designs 1 and 2): it is not
  in the invoke hot path, so it would be dead code. Alias resolution is client-side.
- **A `[providers]` nested-object registry now** (design 2): the flat `SettingMeta` UI cannot
  render `dict[str, ProviderProfile]`; it needs new UI machinery. Defer.
