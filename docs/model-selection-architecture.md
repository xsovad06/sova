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

2. **Task-type routing was wired to nothing** (fixed in PR4). `route_model(..., task_type=...)`
   and `_resolve_task_type_model` both existed, but two facts killed them: `invoke()` only
   loaded config when `model is None` and every step passes `model=ctx.resolved_model`, so the
   task_type branch was never reached; and `invoke_command()` did not accept a `task_type`
   parameter, so the six slash-command steps (develop, simplify, self_review, research,
   address_review, rearrange_commits) could not route at all. Both paths are live now: a
   configured `llm.routing[task_type]` outranks the passed model, and each of the six steps
   carries a `BaseStep.TASK_TYPE` tag it passes via `ctx.routing_task_type()` (`generate_tasks`
   makes seven). A third fact had to be fixed with them: every one of those steps except
   `research` passes `cwd=ctx.working_dir`, a linked worktree that holds neither `sova.toml` nor
   the gitignored `.claude/sova.db`, so `_try_load_config()` returned bare defaults and no route
   could ever match. `client.py:_config_root()` now resolves such a directory back to the primary
   checkout. That also restores `agent.fallback_models` and `compression` inside the pipeline,
   which the same lookup had been silently emptying.

   *Resolved by PR7 (#916):* `pr_body` ([create_pr.py:372](sova/core/steps/create_pr.py#L372)),
   `validate` ([validate.py:163](sova/core/steps/validate.py#L163)), `monitor_ci`
   ([monitor_ci.py:430](sova/core/steps/monitor_ci.py#L430)), and `review` (`reviewer.py:494,513`,
   tagged since PR5 but inert until the PR4 resolver flip) now route live. `triage` and
   `extraction` still resolve primarily through `_ROLE_MODEL_FIELDS` (`roles.triage_model`), not
   `llm.routing`, but `roles/triage.py` also carries a `task_type="triage"` tag for the config
   surface. `TASK_TYPE_KEYS` remains advisory and gates nothing at runtime, so an unconsumed key
   still fails silently: config accepts it and nothing reports that it was ignored.

3. **Role hardcoding.** Seven literal model names bypass all config:
   `reviewer.py:471,489` (`"sonnet"`), `panel_review.py:231` (`"sonnet"` default),
   `supervisor/planner.py:36` (`"sonnet"`), `develop.py:96` (`"haiku"` fallback),
   `knowledge/lifecycle.py:340` (`"haiku"`), and `llm_suggestion_service.py`
   (full version strings, via a direct httpx bypass of the abstraction; resolved by #924,
   which replaced both literals with the `"haiku"` tier resolved through `resolve_alias()`). Correction to the
   briefing: `RolesConfig` has `researcher_model` and `triage_model` but **no**
   `reviewer_model` ([models.py:288-297](sova/config/models.py#L288-L297)), so the reviewer
   literally has no config field to read even if we un-hardcode it.

   *Resolved for the first three by PR5 (#914):* `RolesConfig` now carries
   `reviewer_model` (`"sonnet"`), `developer_model` (`""`), and `planner_model` (`"sonnet"`),
   registered in `_ROLE_MODEL_FIELDS` under both the task-type key (`"review"`) and the role
   name (`"reviewer"`). The reviewer resolves once per review and reuses the result for every
   diff chunk, every schema retry, and as the panel's `default_model`; the supervisor planner
   resolves in `plan()` and passes the model into `_call_llm`. The `task_type="review"` tags
   added at the reviewer's two `invoke` sites were inert until PR4 flipped the resolver
   precedence (see 2.2); they route live now.
   `developer_model` defaults to empty on purpose: role config is consulted *before* complexity
   routing, so a non-empty default would silently pin every developer run to one model. The two
   `"haiku"` literals and the suggestion service remain, by design, for PR6 and a later PR.

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
  dispatches `claude-code` / `litellm` / `hybrid` / `anthropic` as base branches; `openai` /
  `ollama` / `vertex` alias into the `litellm` branch (see section 4).
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
| Complexity routing | [routing.py:26-32,97-127](sova/llm/routing.py#L97-L127) | works | yes |
| Config override (`llm.routing[tier]`) | [routing.py:120-124](sova/llm/routing.py#L120-L124) | works | yes |
| Task-type routing (`llm.routing[task_type]`) | [routing.py:130-148](sova/llm/routing.py#L130-L148), [client.py:_resolve_task_type_model](sova/llm/client.py) | works (PR4); PR7 extends the `BaseStep.TASK_TYPE` tag from the original seven steps to every remaining pipeline call site, all live immediately since the PR4 resolver already outranks an explicit model | yes (PR4) |
| Role config (`researcher_model`, `triage_model`) | [client.py:691-695](sova/llm/client.py#L691-L695) | works for those two roles | via `route_model` |

Correction to the briefing: task-type routing was *not* entirely dead code even before PR4.
`harden.py:116`, `batch_service.py:335`, and `planner.py:133` already passed `task_type`; it was
dead only in the pipeline steps, which all pass an explicit `model=`.

PR7 extends the `BaseStep.TASK_TYPE` tagging begun by the original seven steps
(`address_review`, `develop`, `generate_tasks`, `rearrange_commits`, `research`, `self_review`,
`simplify`) to every remaining pipeline `invoke()`/`invoke_command()` call site: `validate`,
`monitor_ci`, `spec`, `pr_body` (`create_pr.py`), `rebase`, `develop_fix`, and `triage`.
`address_external_findings.py` reuses the `address_review` key rather than getting one of its
own: both steps address reviewer findings against an open PR, and a second key would split one
routing decision across two config entries. The implementation-notes call in `develop.py` is
tagged `extraction` for the same reason: PR6 (#915) already routes it through
`resolve_extraction_model`, so a second key would split one routing decision across two config
entries. `invoke_command()` also gains the `task_type` parameter so the six slash-command steps
route too. Because PR4 already landed, every one of these tags is live selection immediately
rather than a future hook. Two call sites are deliberately left untagged and documented as such
in place: `cli/commands/pr.py` (dynamic user-invoked CLI command) and `mcp/tools.py`
(caller-supplied arbitrary command). Both inherit `config.agent.model` and client-level fallback
without task-type routing.

One known gap remains outside the pipeline: `roles/panel_review.py` tags each dimension with a
dynamic `review_{dimension}` key built from user-configurable `panel_config.dimensions`, so those
keys cannot be enumerated in `TASK_TYPE_KEYS`. A consumer that validates config keys against that
frozenset would reject them; resolving this needs prefix-aware matching, tracked separately. Its
static aggregate key, `review_panel`, is enumerated like any other fixed tag.

### 2.3 The pinning trap (critical)

Pinning exists precisely to stop the CLI resolving `"opus"` to a newer version the deployment
does not have ([routing.py:81-89](sova/llm/routing.py#L81-L89)). The task-type branch used to
return its override **without** `_apply_pin`, whereas the complexity branches pinned
([routing.py:119,122](sova/llm/routing.py#L119-L122)), so "just wire up task-type routing" would
have reintroduced the exact unavailable-version bug this whole effort exists to fix. **The
task-type path must also pin**, and as of PR4 both task-type paths do, through one shared
`routing.py:route_task_type()` that `route_model` and `client.py:_resolve_task_type_model` both
call. It pins against `agent.model` (the same source
[assess.py:48-60](sova/core/steps/assess.py#L48-L60) already feeds `route_model`).

Pinning is family-scoped by design, which has two consequences worth stating: a route to `haiku`
under an opus-pinned `agent.model` is intentionally *not* pinned, and a route to a non-family
model (`ollama/*`) is returned verbatim, so local-model offloading is never overridden by the
pin.

### 2.4 What `ctx.resolved_model` actually is

`AssessStep` sets it via `resolve_model(role, roles, complexity, llm_config, agent_model)`
([assess.py:48-60](sova/core/steps/assess.py#L48-L60)), which returns the role config first,
else the complexity route with pinning; `agent.model` is only the last-resort fallback
(line 59). So a MODERATE issue runs on `sonnet`, a COMPLEX issue on the pinned opus, a TRIVIAL
issue on `haiku`. Correction to the briefing and to designs 1 and 3: the backward-compat
invariant is **not** "opus everywhere". Any defaults-parity test must assert the correct model
**per complexity tier**, or it will mask a real routing regression.

Known gap opened by PR4: `ctx.resolved_model` is what `WorkflowEngine._update_step_execution`
writes into `CostRecord.model` and `CostRecord.model_selection_reason`
([workflow.py:862-865](sova/core/workflow.py#L862-L865)), but a task-type route is resolved
inside `client.py` and never travels back up to the context. So a step whose model came from
`llm.routing[task_type]` has its cost filed under the complexity-routed model instead of the
one that actually ran, and the `/costs` per-model breakdown is wrong for exactly those steps.
The dollar totals stay correct (they come from the provider result); only the attribution is
off. Inert under the default empty `llm.routing`. Closing it means carrying the winning model
back on `StepResult`, which is a cross-cutting change to every step and is deliberately not
part of PR4.

### 2.5 Invocation inventory (32 sites)

22 direct `invoke()` and 10 `invoke_command()` sites across `roles/`, `core/steps/`,
`supervisor/`, `dashboard/services/`, `git/`, `knowledge/`, `cli/commands/`, `mcp/`. One
deliberately bypasses the client abstraction: `git/rebase.py:146` (multi-model consensus
fan-out). `dashboard/services/llm_suggestion_service.py` was the second until #924 moved it onto
`create_provider()`, and a later pass moved it again onto `sova.llm.client.invoke()` itself (so
its cost is recorded and it counts against the runaway-call guard); it passes
`task_type="pr_suggestion"` (unrouted by default, since no `llm.routing` entry matches) and
`isolated=True` (so the default CLI provider runs it `--safe-mode --tools ""`, matching the
model-availability probe). The
batch path (`triage.py` -> `invoke_batch` -> `anthropic_batch.py`) used to send bare aliases to
an API that needs full model IDs; `invoke_batch()` now resolves `task_type` routing and expands
aliases via `models.py:resolve_model_alias()` before either backend is reached.

### 2.6 Cost and budget coupling

The rate card is Anthropic-only and returns `Decimal("0")` for unknown models
([models.py:78-119](sova/config/models.py#L78-L119)); the per-issue budget guard reads the
recorded cost. `--max-budget-usd` is a claude-code CLI-only per-run cap. Consequence: the
moment a non-Anthropic or unknown model runs, cost is recorded as `$0`, the dollar-budget
runaway guard goes blind, and switching off claude-code loses the per-run cap entirely. A
wall-clock / step-count runaway guard must exist **before** any non-Anthropic provider is
enabled.

### 2.7 Backend-aware alias resolution (#1033)

`LLMConfig.model_aliases` (Q3) resolves a generic tier name to a native ID, but the native ID a
deployment needs depends on which model-ID *dialect* the active endpoint actually speaks, not
only on `llm.provider`: the Claude CLI's own `CLAUDE_CODE_USE_VERTEX`/`CLAUDE_CODE_USE_BEDROCK`
env vars silently redirect `provider="claude-code"` to Vertex/Bedrock, which reject a bare tier
name and require a fully-qualified, dialect-specific ID. `sova/llm/backends.py` is a leaf module
(no intra-`sova` imports at runtime) adding this layer:

- `detect_backend(cfg: LLMConfig, env: Mapping[str, str] | None = None) -> Backend` maps
  `llm.provider` plus those two env vars to one of `firstparty` / `vertex` / `bedrock` /
  `litellm` (the permissive catch-all and fail-open default for an unrecognized provider).
  Reads `env` if given, else `os.environ` at call time (never cached). `sova/llm/client.py:
  resolve_alias()` is the one caller that must pass an explicit `env`: it runs in the *parent*
  process (dashboard/server/CLI), not inside the spawned Claude CLI child whose model it is
  resolving, and that child's environment is scrubbed by `scrub_agent_env()` (stripping
  `CLAUDE_CODE_USE_VERTEX`/`CLAUDE_CODE_USE_BEDROCK` unless `agent.env_passthrough` opts them
  back in). Reading the parent's raw, unscrubbed `os.environ` there could detect `vertex` from a
  routing var that happens to be set in the parent's environment but is never forwarded to the
  child, resolve an `@`-pinned Vertex ID, and hand it to a child that actually runs `firstparty`
  once scrubbed, where that ID is not servable (CodeRabbit, PR #1106). `resolve_alias()` therefore
  builds the same scrubbed environment `scrub_agent_env(passthrough=configured_passthrough())`
  would produce for the real spawn, but only when `cfg.provider == "claude-code"` and
  `routing_env_vars_present()` (a raw-`os.environ` pre-check with no config load) finds either var
  set, so the `agent.env_passthrough` config load behind `configured_passthrough()` is skipped
  for the common case where neither var is set at all.
- `backend_can_serve(backend, model_id) -> bool` is fail-open except for three known-wrong
  combinations: a bare tier name on vertex/bedrock, an `@`-pinned Vertex snapshot ID on
  firstparty, and a Bedrock-dialect ID (`anthropic.*`/`us.anthropic.*`) anywhere but bedrock.
- `tier_candidates_for(backend, tier, overrides)` returns the ordered, backend-servable candidate
  list for vertex/bedrock (built-in table, or `llm.tier_candidates` override, JSON-array- or
  comma-separated). The built-in table's entries are bare, unprefixed IDs (e.g.
  `claude-opus-4-6@20260401`), correct for the claude-code CLI's own env-based Vertex/Bedrock
  routing. firstparty has no candidate table here: `sova/llm/client.py:resolve_alias()`
  resolves a firstParty tier name through `sova.llm.models.resolve_model_alias()` instead (the
  same subscription-valid mapping the cost model and `normalize_model_name` already use), so SOVA
  resolves it itself rather than leaving a bare `"opus"`/`"sonnet"` for the Claude CLI to resolve;
  issue #619 is exactly a CLI-side alias-resolution change silently retargeting a config nobody
  edited. When `cfg.provider == "vertex"` the resolved candidate instead reaches
  `LiteLLMProvider`, which forwards the model ID to litellm unchanged; litellm's Vertex AI dialect
  requires the `vertex_ai/` prefix (`_VENDOR_MODEL_EXAMPLES` in `sova/config/models.py`), so
  `resolve_alias()` prefixes the resolved candidate in that case only (CodeRabbit, PR #1106) --
  never for `Backend.VERTEX` generally, since that also covers the CLI's own bare-ID routing.
- `tier_for_known_candidate(model_id)` reverse-looks-up a known pinned candidate ID to its tier,
  so a pinned ID reached *directly* (not via a tier alias, e.g. `agent.model` set to a Vertex
  snapshot ID) whose backend has since drifted is corrected the same way a bare tier name is.

`resolve_alias()`'s lookup order is unchanged at the top: `"{backend}:{model}"` in
`model_aliases`, then the bare `model` key, then *model* itself, still a single hop. The
backend-aware correction (gated by `llm.resolve_tier_aliases`, default on; `false` restores
byte-identical passthrough) applies only when the resolved value is a known-unservable tier name
or pinned ID for the detected backend; an explicit, already-servable override always wins and is
never second-guessed. `create_provider()` (`sova/llm/provider.py`) and `select_model()` share this
one function, so `llm.model`/`llm.fallback_model`/the primary `model=`/`agent.fallback_models`
chain are all backend-aware the same way. The `llm.model_alias` log line's `reason` field
(`explicit` / `backend_scoped` / `tier_candidate`; `passthrough` is never actually logged, since
an unchanged resolution is not logged at all) names which path fired.

Vertex/Bedrock-pinned candidate IDs in `sova/llm/backends.py` are a best-effort table, not
verified against a live deployment at the time of writing (PR9): per the pinning-verification
rule in `.claude/rules/architecture.md`, confirm availability (404 vs 429) before relying on them
in production, and update the table if they have drifted.

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

Shipped so far (PR8): `select_model` exists and applies the `llm.model_aliases` map only, but it
is already wired into every model-selection surface, not just the primary `model=` argument.
Concretely: `invoke()`, `invoke_streaming()` and `invoke_command()` alias the resolved primary
before the provider call; `invoke_batch()` aliases each `BatchRequest.model`; and
`_build_candidate_chain()` aliases every `agent.fallback_models` entry (not only the primary), so
a fallback hop never reaches the provider unmapped. `create_provider()` in `sova/llm/provider.py`
also resolves `llm.model`/`llm.fallback_model` through the same map (via the shared
`sova/llm/client.py:resolve_alias` helper) before constructing a `LiteLLMProvider` or
`AnthropicAPIProvider`, so a deployment can point those config fields at a generic tier name too.
The other resolution paths still live where they were (`_resolve_task_type_model` in `client.py`,
`resolve_model`/`route_model` in `llm/routing.py`); PR4 folds them in to complete the precedence
chain below.

```
explicit model= arg  >  llm.routing[task_type]  >  role config (_ROLE_MODEL_FIELDS)
  >  complexity route (route_model, with pinning)  >  ctx.resolved_model  >  agent.model
```

Two wiring facts had to be fixed for this to actually fire (both missed by design 1), and both
landed in PR4:
1. `invoke()` loads config even when `model` is provided, so a configured task_type route can
   override the passed `ctx.resolved_model`. This is a deliberate, documented semantic change;
   it is a no-op under the default empty `llm.routing`.
2. `invoke_command()` takes a `task_type` parameter and routes it through the resolver;
   otherwise the six slash-command steps get no routing.

And pinning is applied on the task_type branch (section 2.3).

One deviation from the precedence above, also PR4: `llm.routing[task_type]` outranks the explicit
`model=` argument rather than losing to it. Every pipeline step passes
`model=ctx.resolved_model or ctx.config.agent.model`, so a route that lost to an explicit model
could never fire. The exception is a fallback in flight: `ctx.routing_task_type()` returns `None`
once `fallback_model_index > 0`, so a configured route cannot pin a step back to the model the
engine just fell back *from*.

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

**Audit note (#924): `_advance_fallback` is a rollback path, not a provider bypass.**
`WorkflowEngine._advance_fallback()` (`sova/core/workflow.py:750-761`) only walks
`agent.fallback_models` and returns the next model-name string; it never invokes a provider
itself, so it does not join the inventory of direct-API bypasses that issue audited. It is
reachable only when `WorkflowEngine._has_fallback_models()` (`sova/core/workflow.py:731-748`)
returns `True`, which requires `llm.engine_owned_fallback=True`
(`sova/config/models.py:129`), which is `False` by default, matching the "neutered behind a single
flag" design above. With the flag off (the default), `_try_step_with_retries()` never produces
the `"billing_exhausted"` status that would trigger `_advance_fallback`, so the method is live
code guarded by a flag, not dead code: flipping `engine_owned_fallback` back on is the documented
rollback path to the legacy engine-driven advance, exercised by the
`llm={"engine_owned_fallback": True}` cases in `tests/test_core.py` and by
`tests/test_llm_fallback_loop.py`. It is kept, not removed, exactly per this file's Q5 design.

**Audit note (#924): rebase consensus resolution is gated on `is_anthropic_capable()`.**
`sova/git/rebase.py:_create_providers()` instantiates `LiteLLMProvider` directly, one per
`[conflict_resolution].models` entry, bypassing `sova.llm.provider.create_provider()` and the
operator's configured `llm.provider` entirely. Those model IDs are Anthropic model names by
convention (the consensus feature was designed around Claude), so running them unconditionally
would silently reach Anthropic even when the operator configured `llm.provider="openai"` or
another non-Anthropic backend. `_load_consensus_config()` (`sova/git/rebase.py`) now checks
`sova.llm.backends.is_anthropic_capable(cfg.llm)` and drops the entries
`is_anthropic_model_id()` recognizes when the configured provider cannot serve them. Entries
naming some other vendor survive: an explicitly listed `"gpt-5"` pair under
`llm.provider="openai"` is a coherent operator choice, not the implicit Anthropic assumption
this guards, so emptying the whole list would have broken a valid config. When fewer than two
entries survive, `rebase_with_conflict_resolution()`'s existing
`use_consensus = len(cr_models) >= 2` check degrades automatically to the single-model
`_resolve_conflicts_with_llm()` path, which calls `invoke_command()` and so is already routed
through the configured provider. A config-load failure still falls back to the pre-existing
defaults (empty models list), which was already fail-closed for this path.

**Audit note (#924): an advisory widget's model must be tier-named, not pinned.**
`llm_suggestion_service.py` pinned two literal IDs (`"claude-haiku-4-5-20251001"` for the
direct API, `"claude-haiku-4-5"` for Vertex) because it built both requests itself and so knew
which dialect each one spoke. Routing it through `create_provider()` (and, in a later pass,
`sova.llm.client.invoke()`) removes that knowledge: a per-call `model=` argument never passes
through `resolve_alias()` on its own (only `select_model()` and `create_provider()`'s own
`cfg.model`/`cfg.fallback_model` do), so a literal would reach the provider verbatim and only one
backend's dialect can be written down at a time. The service
therefore names the tier (`_TIER = "haiku"`) and resolves it through `resolve_alias(_TIER,
cfg.llm)`, which yields the firstParty ID on `claude-code`/`anthropic`, the `@`-pinned snapshot
on a `CLAUDE_CODE_USE_VERTEX`-routed CLI (which rejects the firstParty form), and the
`vertex_ai/`-prefixed snapshot on `llm.provider="vertex"` (without which litellm reads a bare
`claude-...` ID as Anthropic-direct and leaves the operator's Vertex project entirely).
`litellm`/`hybrid` also resolve the cheap tier (rather than deferring to the operator's own
configured model, which may be a deliberately expensive pin): the vendor prefix is taken from
`cfg.model` (e.g. `"anthropic/"`, `"vertex_ai/"`) and reapplied to the resolved tier, falling
back to `model=None` only when `cfg.model` is bare and carries no prefix to infer. That gate also treats
`llm.provider="vertex"` as conditional rather than Claude-only, since `vertex` is generic LiteLLM
Vertex routing whose documented example model is `vertex_ai/gemini-2.5-pro`.

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

Residual uncovered surface (explicitly decided, not hand-waved): `git/rebase.py`'s consensus
fan-out still does not pass through `client.py`, and is documented as an intentional exception
whose Anthropic-by-convention model IDs are gated on `detect_backend() is Backend.FIRSTPARTY`,
not just `is_anthropic_capable()` (#924): a route check, since `is_anthropic_capable()` alone
passes `llm.provider="vertex"`/a Vertex/Bedrock-redirected `claude-code` CLI, both of which the
bare, unprefixed consensus model IDs would still misroute to the direct Anthropic API.
`llm_suggestion_service.py`'s httpx bypass is gone: #924 moved it onto `create_provider()`, and a
later pass moved it again onto `sova.llm.client.invoke()`, so only the one exception above
qualifies "one authoritative place".

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
