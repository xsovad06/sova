# Model Selection Architecture Research Context

**Date**: 2026-09-02  
**Goal**: Provide Opus 4.8 with complete context for investigating and planning model selection fixes  
**Status**: Preparation document for deep investigation and architecture design

---

## Executive Summary: The Problem

SOVA agents **fail frequently** because the wrong models are selected or models that don't exist are chosen. The example error illustrates this:

```
Claude CLI failed (exit 1): Warning: Opus: Opus 5 not available — using Opus 4.8 for this session
```

**What's wrong**:
- SOVA specifies `--model opus` (a generic alias), Claude CLI resolves it to the **latest** Opus (Opus 5 in this case)
- But the user's environment (Vertex AI deployment, company policy, billing agreement) **only has Opus 4.8**
- The CLI falls back silently, but SOVA's error handling treats the warning exit code as failure
- The application has **no way to know** which exact models are available on the current deployment

**Why it matters**:
- **Cost impact**: Wrong model selection wastes $0.50-$1.00 per run on expensive higher-tier models when cheaper ones would suffice
- **Availability impact**: Agents fail on deployments missing certain model versions
- **Scalability impact**: Cannot migrate to OpenAI or other providers without rewriting all role/step code
- **Configuration impact**: Model selection is "theoretical" — complex routing code exists but doesn't actually control behavior in practice

---

## Current Architecture: What Actually Happens

### 1. Model Selection Layers (3 + Fallback)

#### Layer 1: Role/Task Level (`sova/roles/*.py`)
- **Hardcoded per role**: `ReviewerRole` invokes with `model="sonnet"` (hard-wired)
- **Panel review**: `PanelReviewRole` uses `dimension_models` dict from config (if enabled)
- **No dynamic selection** based on task type or complexity at this layer

```python
# sova/roles/reviewer.py (hardcoded)
result = await invoke_command(
    "/review",
    model="sonnet",  # ← HARDCODED, no fallback config
    fallback_model=ctx.get_cli_fallback_model(),
)
```

#### Layer 2: Complexity-Based Routing (`sova/llm/routing.py`)
- **Default routing table**: Maps `ComplexityTier` → model alias
  - TRIVIAL → "haiku"
  - SIMPLE → "sonnet"
  - MODERATE → "sonnet"
  - COMPLEX → "opus"
  - EPIC → "opus"

- **Config-driven overrides**: `llm.routing` dict in `sova.toml` can override per-tier
  - Example: `[llm.routing]` `complex = "sonnet"` (force down to cheaper model)

- **Task-type routing**: Can route specific task types to different models
  - Keys: `triage`, `extraction`, `pr_body`, `develop`, `review`, `validate`, etc.
  - Bypasses complexity-based logic entirely
  - **Not actually used in practice** — test file exists but no production code calls it

- **Pinning mechanism**: `route_model()` pins bare aliases to agent-configured model
  - If `agent.model = "claude-opus-4-6"` and routing says "opus", use the specific version
  - Prevents CLI from resolving "opus" to a never-available Opus 5

```python
# sova/llm/routing.py
def route_model(complexity, task_type=None, llm_config=None, agent_model=None):
    # Priority: task-type > config override > default routing
    # Returns (model_alias, reason) tuple
    # Pins aliases to agent_model if same family
```

#### Layer 3: Fallback Chain (`sova/config/models.py`)
- **Primary model**: `agent.model` (default: `"opus"`)
- **Fallback models**: `agent.fallback_models` list (default: empty `[]`)
  - Example: `fallback_models = ["sonnet", "haiku"]`
  - **Index tracking**: `ExecutionContext.fallback_model_index` advances on failure
  - Each LLM invocation passes `fallback_model = fallback_models[fallback_model_index]`
  - Claude CLI receives `--fallback-model` flag

```python
# sova/core/context.py
def get_cli_fallback_model(self) -> str | None:
    """Get the next fallback model from the chain."""
    if self.fallback_model_index >= len(self.config.agent.fallback_models):
        return None
    return self.config.agent.fallback_models[self.fallback_model_index]
```

#### Layer 4: Step-Level Invocation (`sova/core/steps/*.py`)
- Steps call `invoke_command(..., model=resolved_model, fallback_model=fallback)`
- **AssessStep** (complexity detector) sets `ctx.resolved_model` + `ctx.model_selection_reason`
- All other steps inherit `ctx.resolved_model` for that run

---

### 2. Why It Breaks in Practice

#### Gap 1: **No Model Availability Detection**
- SOVA has **zero code** that queries "what models are actually available"
- Assumes all model aliases (`"opus"`, `"sonnet"`, `"haiku"`) exist and are current versions
- When Claude CLI resolves `"opus"` to `"claude-opus-5"` and that doesn't exist on the user's deployment:
  - CLI exits with code 1 (warning)
  - Stderr contains `"Opus 5 not available — using Opus 4.8 for this session"`
  - SOVA's error handler sees non-zero exit and treats it as **fatal failure**
  - **No fallback triggered** because the exit code itself is treated as the error, not the underlying unavailability

**File**: `sova/llm/providers/claude_code.py:invoke()` lines 58-78
```python
# Current: treats any non-zero exit as failure, doesn't parse the stderr
if not result.success:
    detail = _extract_failure_detail(result)
    raise RuntimeError(f"Claude CLI failed (exit {result.returncode}): {detail}")
```

#### Gap 2: **Fallback Chain Doesn't Trigger on CLI Warnings**
- Fallback chain is designed for **explicit invoke() failures** (exceptions)
- But Claude CLI's `--fallback-model` flag is built-in to the CLI itself
- When the CLI handles its own fallback internally, SOVA never knows fallback was used
- Return code might be 0 (success with internal fallback) or 1 (warning with internal fallback)
- **No coordination between SOVA's fallback strategy and Claude CLI's internal fallback**

#### Gap 3: **Role/Task Hardcoding Ignores Configuration**
- `ReviewerRole` is hardcoded to `model="sonnet"` regardless of config
- Panel review respects `dimension_models` dict but only if `enabled=True`
- No mechanism for admins to say "use Haiku for triage, Sonnet for review" globally
- Task-type routing exists but isn't wired into any role/step

#### Gap 4: **No Provider Abstraction in LLM.Provider ABC**
- `LLMProvider.invoke()` takes `model`, `fallback_model` params
- But **the ABC doesn't expose model availability checking** in a structured way
- `check_available()` method exists but is binary (True/False) and doesn't report available models
- Makes it impossible to implement provider-agnostic code that queries available models

#### Gap 5: **Config System Doesn't Validate Model Names**
- `agent.model = "opus"` is accepted without checking if it's valid for the provider
- `agent.fallback_models = ["sonnet", "haiku"]` likewise
- `llm.provider = "anthropic"` but `agent.model = "opus"` (Anthropic models don't use this alias)
- No validation that the config makes sense for the selected provider

#### Gap 6: **Provider Selection Happens at Startup, Not Per-Request**
- `create_app()` calls `_init_llm_provider()` once per process
- Multi-project mode can't use different providers per project
- Can't dynamically switch providers or models based on budget/quota/availability per run
- Locking in provider/model at startup breaks flexibility

---

### 3. Current Config System

**Files**: `sova/config/models.py` (Pydantic), `sova/dashboard/settings_meta.py` (UI metadata)

```python
class LLMConfig(BaseSettings):
    provider: Literal["claude-code", "litellm", "hybrid", "anthropic"] = "claude-code"
    model: str = ""  # For litellm/anthropic (empty for claude-code)
    fallback_model: str = ""  # Single fallback (rarely used)
    api_base: str = ""  # Custom endpoint (litellm only)
    routing: dict[str, str] = {}  # Override by tier/task-type
    batch_eligible_tasks: list[str] = []
    batch_gcs_bucket: str = ""  # Vertex AI GCS bucket

class AgentConfig(BaseSettings):
    runtime: Literal["claude-code", "aider"] = "claude-code"
    model: str = "opus"  # Primary model (resolved by CLI)
    fallback_models: list[str] = []  # Fallback chain
    max_budget: Decimal = 10.00
    max_issue_budget: Decimal = 50.00
    step_timeout: int = 1800
    # ... other settings
```

**Issues with current config**:
1. **Two separate model configs** (`LLMConfig.model` vs `AgentConfig.model`) with unclear distinction
2. **LLMConfig.model** is only used by litellm/anthropic providers, ignored by claude-code
3. **AgentConfig.fallback_models** is a list of strings, not validated
4. **No schema** for what models each provider supports
5. **No way to specify provider-specific settings** (e.g., Vertex AI project, OpenAI API key, local Ollama URL)
6. **Routing** uses bare task-type/tier names, no validation they're recognized
7. **No per-role overrides** (only global agent.model and dimension_models for panel review)

---

### 4. Test Coverage Reveals the Gaps

#### What's Tested (`tests/test_model_*.py`)
1. **test_model_fallback_cli.py**: Fallback chain flows through invoke()
2. **test_model_routing_pinning.py**: Pinning aliases to agent.model works
3. **test_model_not_available_fallback.py**: Implicit (in routing tests)

#### What's NOT Tested
- [ ] Actual model availability detection
- [ ] CLI exit code + stderr parsing for model unavailability
- [ ] Provider-specific configuration validation
- [ ] Multi-provider model resolution (Anthropic vs OpenAI API IDs)
- [ ] Step-level task-type routing integration
- [ ] Role-level model overrides (beyond panel review)
- [ ] Provider switching at runtime (per-request, not per-process)
- [ ] Cost tracking per provider/model tier

---

### 5. Where Models Are Resolved

```
AssessStep.execute()
  → route_model(complexity, llm_config=ctx.config.llm, agent_model=ctx.config.agent.model)
    → returns (model_alias, reason)
    → ctx.resolved_model = model
    
DevelopStep.execute()
  → invoke_command(..., model=ctx.resolved_model, fallback_model=ctx.get_cli_fallback_model())
    → ClaudeCodeProvider.invoke()
      → _build_args() constructs CLI command: ['claude', '-p', '--model', 'opus', '--fallback-model', 'sonnet', ...]
      → run() executes subprocess
      → _parse_result() parses JSON output
      
ReviewerRole._run_review()
  → invoke_command("/review", model="sonnet", fallback_model=ctx.get_cli_fallback_model())
    → Same path as DevelopStep
```

**Model aliases passed to CLI**: `"opus"`, `"sonnet"`, `"haiku"`, or specific versions like `"claude-opus-4-6"`

---

### 6. Known Workarounds in Current Code

#### Workaround 1: Pinning to Specific Versions
- Users set `agent.model = "claude-opus-4-6"` instead of `"opus"`
- Prevents CLI from resolving to a newer version that might not exist
- But requires users to manually update config when versions change

#### Workaround 2: Reducing Complexity Tier
- Set `[llm.routing]` `complex = "sonnet"` to force down to cheaper model
- Workaround for unavailable Opus on some deployments
- But loses the benefit of using higher-tier models when needed

#### Workaround 3: Longer Fallback Chain
- Set `fallback_models = ["sonnet", "haiku"]` (but defaults to empty)
- Doesn't help if Opus is hardcoded in a role

#### Workaround 4: Disable Panel Review
- Panel review respects dimension_models if enabled, but defaults to disabled
- Most powerful config mechanism but hidden behind a feature flag

---

## Architecture Needed

### 1. Model Availability Registry

**Goal**: SOVA should know what models are available before attempting to use them.

```python
# New concept: ModelProvider capability matrix
class ModelAvailability:
    available_models: dict[str, ModelInfo]  # keyed by alias or version ID
    aliases: dict[str, str]  # "opus" → "claude-opus-4-6" (current binding)
    tiers: dict[str, list[str]]  # "fast" → ["haiku"], "smart" → ["opus"], "cheap" → ["haiku"]
    pricing: dict[str, PriceInfo]  # per model
    
# LLMProvider.get_availability() -> ModelAvailability
# Called once at startup per provider, cached
# Enables validation of user-specified models and intelligent fallback routing
```

### 2. Provider-Specific Configuration

**Goal**: Each provider (Anthropic, OpenAI, LiteLLM, Ollama) has different model naming, pricing, and capabilities.

```python
# New top-level config section
class ProvidersConfig:
    anthropic:
        api_key: str  # from env
        models_available: list[str]  # or query dynamically
    openai:
        api_key: str
        model_family: "gpt-4o"
        fallback_family: "gpt-4-turbo"
    vertex_ai:
        project_id: str
        location: str
        region: str
    ollama:
        base_url: str  # http://localhost:11434
        models: list[str]  # ["llama2", "mistral"]

# AgentConfig.model now provider-aware
# "claude-opus-4-6" for Anthropic
# "gpt-4o-2024-11-20" for OpenAI
# "llama2" for Ollama
```

### 3. Role/Task Model Routing Configuration

**Goal**: Admins specify "triage uses haiku, develop uses opus" globally, without code changes.

```python
class RolesConfig:
    # Already exists but underused
    developer_model: str = ""  # override agent.model for developer role
    researcher_model: str = ""
    
    # New: task-type routing (currently unused)
    task_routing: dict[str, str] = {
        "triage": "haiku",
        "research": "sonnet", 
        "develop": "opus",
        "review": "sonnet",
        "self_review": "haiku",
        "validate": "haiku",
        "monitor_ci": "sonnet",
    }
```

### 4. Provider-Agnostic LLM Interface

**Goal**: Same code path works for Anthropic, OpenAI, LiteLLM, Ollama without refactoring roles/steps.

```python
# Enhance LLMProvider ABC
class LLMProvider(ABC):
    async def get_available_models(self) -> dict[str, ModelInfo]:
        """Return available models and their capabilities."""
        ...
    
    async def is_model_available(self, model_id: str) -> bool:
        """Check if a specific model exists."""
        ...
    
    def resolve_model(
        self,
        requested: str,  # User request: "opus", "gpt-4", "claude-opus-4-6"
        complexity: ComplexityTier,
        task_type: str | None = None,
    ) -> str:
        """Resolve a generic request to a specific model ID, using availability."""
        # Returns actual model to invoke
        # May differ from requested if unavailable
```

### 5. Error Handling: Model Unavailability → Fallback

**Goal**: When "model not available", trigger fallback chain instead of hard failure.

```python
# In WorkflowEngine or provider
async def invoke_with_fallback(
    prompt: str,
    model: str,
    fallback_chain: list[str],
) -> LLMResult:
    for attempt, candidate in enumerate([model] + fallback_chain):
        try:
            return await provider.invoke(prompt, model=candidate)
        except ModelUnavailableError:
            if attempt == len([model] + fallback_chain) - 1:
                raise  # Last attempt
            # Log and continue to next
            log.warning(f"Model {candidate} unavailable, trying {fallback_chain[attempt]}")
```

### 6. Dynamic Model Selection per Request

**Goal**: Choose model based on runtime state (budget, quota, availability) not startup config.

```python
# New concept: ModelSelector service
class ModelSelector:
    async def select(
        self,
        task_type: str,
        complexity: ComplexityTier,
        role: str,
        issue_number: str,
    ) -> tuple[str, list[str], str]:  # (primary, fallback_chain, reason)
        """Select model + fallback chain based on:
        - Task type and complexity routing rules
        - Role-specific overrides
        - Runtime state: remaining budget, provider quota, available models
        - History: what worked/failed for this issue
        """
        # Returns both model ID and fallback chain for the entire request
```

---

## Investigation Questions for Opus 4.8

### Architecture
1. **Provider abstraction**: How do we model OpenAI (gpt-4o, gpt-4-turbo), Anthropic (opus-4-6, sonnet-4-6), and Ollama (llama2, mistral) in one abstraction?
2. **Configuration schema**: What is the minimal config needed to support all providers without boilerplate?
3. **Model availability**: Should we query it once at startup or cache with TTL? What about local Ollama that can add/remove models at runtime?
4. **Fallback orchestration**: Where should fallback logic live? (provider, client, workflow engine, step)?
5. **Multi-provider**: Can one SOVA instance use different providers for different roles/tasks?

### Gaps in Current Code
1. **Layer disconnect**: Why isn't task-type routing used? Is it dead code or incomplete?
2. **Role hardcoding**: Should `ReviewerRole` respect `roles.reviewer_model` or task-type routing?
3. **CLI warning handling**: How should we parse Claude CLI's fallback warnings?
4. **Provider initialization**: Should providers be created per-request instead of per-process?

### Testing Strategy
1. What models should the tests use? (Mock, real Anthropic, Vertex AI, local Ollama?)
2. How to test provider switching without adding test dependencies?
3. What error cases need coverage? (Model not available, billing error, network error, timeout with fallback)

### Migration Path
1. **Backward compatibility**: Should we keep `agent.model = "opus"` working?
2. **Gradual rollout**: Can we deploy provider abstraction without changing every role/step at once?
3. **OpenAI readiness**: What config changes are needed to support "use Anthropic for dev, OpenAI for triage"?

---

## Files to Review

**Core LLM Layer** (`sova/llm/`)
- `provider.py` — ABC and factory
- `client.py` — Global provider access
- `routing.py` — Complexity/task-type routing
- `complexity.py` — Complexity detection
- `providers/claude_code.py` — Claude CLI wrapper
- `providers/anthropic_api.py` — Direct Anthropic API (newer)
- `providers/litellm_provider.py` — LiteLLM multi-provider support

**Configuration** (`sova/config/`)
- `models.py` — Pydantic models
- `loader.py` — TOML + DB config loading
- `db_loader.py` — DB-backed config

**Workflow Steps** (`sova/core/steps/`)
- `assess.py` — Complexity detection + model routing
- `develop.py` — Dev work (hardcoded fallback path)
- `create_pr.py` — PR body generation (fallback to structured if LLM fails)
- `review.py` — Review step invocation

**Roles** (`sova/roles/`)
- `developer.py` — Developer role (uses DevelopStep)
- `reviewer.py` — Reviewer role (hardcoded `model="sonnet"`)
- `panel_review.py` — Panel review (respects config)
- `researcher.py`, `planner.py` — Other roles

**Tests** (`tests/test_model_*.py`)
- `test_model_fallback_cli.py` — Fallback chain passthrough
- `test_model_routing_pinning.py` — Pinning logic + model availability detection
- `test_model_not_available_fallback.py` — Implicit coverage
- `test_llm.py` — Basic provider tests

**Dashboard** (`sova/dashboard/`)
- `services/llm_suggestion_service.py` — LLM selection for PR suggestions (dual evaluation experiment)
- `settings_meta.py` — UI metadata for all config fields
- `routers/settings.py` — Settings UI backend

---

## Known Deployment Issues

### Issue #826: Model Unavailability on Vertex AI
- **Symptom**: Agents fail when Claude CLI resolves "opus" to a version not available on the Vertex deployment
- **Root cause**: SOVA doesn't know which models are available; CLI resolves internally
- **Workaround**: Pin to specific version `agent.model = "claude-opus-4-6"` or use exact version numbers everywhere
- **Test file**: `tests/test_model_routing_pinning.py` (added as fix)

### Cost Variance
- **Symptom**: Same issue sometimes costs $10, sometimes $50 depending on which model runs
- **Root cause**: Hardcoded roles (reviewer always uses sonnet, not haiku for simple issues)
- **Workaround**: None; requires code changes

### Future Provider Migration
- **Goal**: Move from Anthropic to OpenAI (company policy)
- **Blocker**: Role/step code hardcodes Anthropic model names; would need refactoring
- **Needed**: Provider-agnostic abstraction layer

---

## Success Criteria for Architecture Review

1. **Model availability is detectable** before invoke (not just fallback after failure)
2. **All provider types supported**: Anthropic CLI, Anthropic API, OpenAI, LiteLLM, local Ollama with same code paths
3. **Role/task routing is configured, not hardcoded** — admins can override via config without touching Python
4. **Fallback chain works for all failure modes**: unavailable model, rate limit, timeout, billing error
5. **Provider can be swapped** (Anthropic ↔ OpenAI) with config-only changes
6. **Backward compatible**: Existing `agent.model = "opus"` configs continue to work
7. **Testable without API keys**: Mock-friendly provider interface

---

## Summary for Opus 4.8

You have been given:
1. **The problem**: Model selection is ad-hoc, hardcoded, and fails frequently
2. **The current architecture**: Three-layer routing (role, complexity, fallback) but disconnected with no availability awareness
3. **The gaps**: Task-type routing unused, CLI warnings mishandled, roles hardcoded, no provider abstraction
4. **The config system**: Separate LLMConfig and AgentConfig with validation gaps and unclear semantics
5. **The tests**: Limited coverage of real error cases and provider switching
6. **The deployment reality**: Works only with specific model versions; breaks on provider migrations

**Your task**:
1. Map the entire model selection flow from config → role → step → provider → CLI
2. Identify architectural patterns for provider abstraction (study OpenAI SDK, Anthropic SDK, LiteLLM)
3. Design a configuration schema that's minimal, validated, and supports all providers
4. Plan the refactoring: What breaks? What stays compatible? What's safe to change?
5. Propose concrete tasks (PRs, tests) to implement the fix incrementally without destabilizing the codebase

Good luck, and feel free to ask for more specific code snippets or tests as you dig deeper.
