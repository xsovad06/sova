# AI Provider Setup Guide

This guide explains how to configure SOVA to use different AI providers (Claude Code CLI, Anthropic API, Vertex AI) and how to handle authentication.

## Table of Contents

1. [Supported Providers](#supported-providers)
2. [Switching Backends Without Editing Model Fields](#switching-backends-without-editing-model-fields)
3. [Personal Subscription (Claude Code CLI)](#personal-subscription-claude-code-cli)
4. [Anthropic API Direct](#anthropic-api-direct)
5. [Google Vertex AI](#google-vertex-ai)
6. [Troubleshooting](#troubleshooting)
7. [Connections Page (Dashboard)](#connections-page-dashboard)

---

## Supported Providers

`llm.provider` (`sova/config/models.py`) accepts seven values: `claude-code`, `anthropic`, `vertex`, `openai`, `ollama`, `litellm`, and `hybrid`. This guide covers the three most common individual setups; `openai`, `ollama`, `litellm`, and `hybrid` all route through the same `LiteLLMProvider` and are covered in [docs/model-selection-migration-guide.md](model-selection-migration-guide.md).

| Provider | Auth Method | Cost | Use Case | Config |
|----------|-------------|------|----------|--------|
| **Claude Code CLI** | Personal subscription login | Per-message billing | Recommended for individuals | `llm.provider = "claude-code"` |
| **Anthropic API** | API key (`llm.api_key`, falls back to `ANTHROPIC_API_KEY`) | Usage-based billing | Production deployments | `llm.provider = "anthropic"` |
| **Google Vertex AI** | GCP ADC + `ANTHROPIC_VERTEX_PROJECT_ID` | GCP billing | Enterprise multi-cloud, Vertex-hosted models (typically Gemini) | `llm.provider = "vertex"` |

**Vertex AI is three unrelated mechanisms, not one.** Mixing these up is the most common source of confusion in this guide's history:

1. **`llm.provider = "vertex"`** (row above): a real provider choice, routed through `LiteLLMProvider`, for SOVA's own LLM calls against Vertex-hosted models. See [Google Vertex AI](#google-vertex-ai) below.
2. **`CLAUDE_CODE_USE_VERTEX` / `CLAUDE_CODE_USE_BEDROCK`**: environment variables that redirect the Claude Code CLI subprocess itself to a Vertex or Bedrock deployment, independent of `llm.provider`. This is not a provider choice SOVA makes; it's inherited process environment that silently overrides `llm.provider = "claude-code"` when present. See [4. Environment Variable Scrubbing](#4-environment-variable-scrubbing) below.
3. **`ANTHROPIC_VERTEX_PROJECT_ID` for the batch backend** (`sova/llm/providers/anthropic_batch.py`): a third, independent mechanism used only by the Anthropic Batch API path (`invoke_batch()`, e.g. triage), requiring that same env var plus `llm.batch_gcs_bucket`. It is selected independently of `llm.provider`: switching to or away from `llm.provider = "vertex"` neither enables nor disables it. Note that this is independence of configuration keys, not of environment: it reads the same `ANTHROPIC_VERTEX_PROJECT_ID` and `CLOUD_ML_REGION` that mechanism 1 does, so setting those for one arms the other too once a GCS bucket is configured (see [4. Set Environment Variables](#4-set-environment-variables) under Vertex AI).

---

## Switching Backends Without Editing Model Fields

Switching the backend under a Claude tier name does not require rewriting that tier name by hand,
for the one value this mechanism actually touches. Routing the Claude Code CLI to Vertex or Bedrock
via `CLAUDE_CODE_USE_VERTEX`/`CLAUDE_CODE_USE_BEDROCK` (mechanism 2 above) redirects the CLI to a
deployment that rejects a bare `opus`/`sonnet`/`haiku`, and `sova/llm/client.py:resolve_alias()`
absorbs that for the per-call model each pipeline step resolves through
`prepare_invocation()`/`select_model()` (`sova/llm/client.py:1146,1193`), plus any tier a
`llm.routing[task_type]` entry names: a bare tier there is detected against the active backend
(`sova/llm/backends.py:detect_backend()`) and substituted with the dialect-specific ID that backend
can actually serve, from `tier_candidates_for()`'s per-backend candidate list.

`llm.model` and `llm.fallback_model` are **not** part of this absorption when `llm.provider =
"claude-code"`, because they are not read at all on that provider: `create_provider()`
(`sova/llm/provider.py`) returns a bare `ClaudeCodeProvider()` with no model arguments for
`provider == "claude-code"`, and `roles/reviewer.py`'s tier fallback explicitly skips
`resolve_alias()`/`llm.model` for `provider in ("claude-code", "anthropic")` too. The two fields
matter for different `llm.provider` sets: `llm.model` is resolved on `anthropic` and every
litellm-backed vendor (`vertex`, `openai`, `ollama`, `litellm`, `hybrid`); `llm.fallback_model` is
read only by the litellm/vendor branch (`create_provider()`, `sova/llm/provider.py`), not by the
`anthropic` branch, which constructs `AnthropicAPIProvider(model=..., api_key=...)` with no
fallback argument at all, so it is as inert on `llm.provider = "anthropic"` as it is on
`claude-code`. This is the separate topic the next section covers.

`agent.model` is a third, mostly unrelated path: `start_agent()` and
`start_command()` (`sova/dashboard/services/agent_lifecycle.py`) read `cfg.agent.model` raw via
`_resolve_config_model()` and hand it straight to `runtime.spawn(model=...)` → the Claude CLI's
`--model` flag, with no call to `resolve_alias()` anywhere in between; a pipeline step is different,
since `ctx.config.agent.model` passed through `dispatch_command()` (e.g. `develop.py`) does reach
`select_model()`/`resolve_alias()` like `llm.model` does. So a bare `agent.model = "opus"` (the
shipped default, `AgentConfig.model`, `sova/config/models.py:156`) survives a backend switch inside
the pipeline, but reproduces the #619 failure on the dashboard/CLI top-level spawn path, and only
once the routing variable actually reaches the spawned CLI: `runtime.spawn()` scrubs
`CLAUDE_CODE_USE_VERTEX`/`CLAUDE_CODE_USE_BEDROCK` at the spawn boundary (`sova/ipc/runtime.py`)
unless it is also listed in `agent.env_passthrough` or set in `~/.claude/settings.json`'s `env`
block, which scrubbing cannot reach (see
[4. Environment Variable Scrubbing](#4-environment-variable-scrubbing) below). Pin `agent.model` to
an explicit ID in `sova.toml` regardless, since the spawn-path gap is real whenever passthrough is
configured. `resolve_alias()` detects the backend from that same scrubbed-then-passed-through child
environment, not the parent shell's, which is why setting the routing variable only in your own
shell does not exercise this path either. Set `llm.resolve_tier_aliases = false` to restore
byte-identical passthrough for every
`resolve_alias()` caller (`select_model()`, `create_provider()`, `llm_suggestion_service`, and the
reviewer's tier fallback), not just `llm.model`/`llm.fallback_model`. Two limits are worth knowing:
the per-backend candidate lists (`tier_candidates_for()`) exist for the `vertex` and `bedrock`
backends only; on the `firstparty` backend a bare tier name is instead resolved by SOVA itself via
`sova.llm.models.resolve_model_alias()` so it never reaches the CLI unresolved, and `backend_can_serve()`
is fail-open on every backend, rejecting only three known-wrong cases (`sova/llm/backends.py`): a
bare tier name on `vertex`/`bedrock`, an `@`-pinned Vertex snapshot ID on `firstparty`, and a
Bedrock-dialect ID anywhere but `bedrock`. This covers both tier names and a pinned ID the candidate
table already recognizes through `tier_for_known_candidate()` (the #1029 backend-drift fix below
corrects both), but not an unrecognized pinned ID for a model the target backend does not host,
which passes through unchanged.

This absorption is a backstop for an already-deployed bare alias surviving a backend switch, not a
license to write a bare alias in any of these fields (`llm.model`, `llm.fallback_model`, or
`agent.model`, which has no such backstop at all) going forward: `.claude/rules/architecture.md`'s
"Always pin explicit model IDs in `sova.toml`, never generic aliases" rule (#619, #828) still applies
to all of them, since a generic alias is resolved differently depending on which backend happens to
be active, not pinned to a specific deployment.

**Changing `llm.provider`, by contrast, can require a model field.** `llm.provider` values `vertex`,
`openai`, and `ollama` have no shared default model, so `llm.model` is mandatory for them
(`LLMConfig._default_model_for_litellm`, `sova/config/models.py`): leaving it empty is a config
validation error, not a fallback. Set `llm.model` before `llm.provider`, as described under
[Google Vertex AI](#google-vertex-ai) below.

Model discovery (`GET /api/models/available?layer=llm`, see
`docs/model-selection-architecture.md` section 2.9) is what keeps those model fields typo-proof: the
`layer` parameter here names the endpoint's own axis (LLM-layer enumeration vs. `layer="runtime"`,
the active agent runtime's own catalog, #1039), not the axis of every setting it feeds. It enumerates what the configured
`llm.provider` can actually reach and feeds the grouped dropdowns on the settings page and the
launch modal's per-run override, for the fields that are themselves LLM-layer values (`llm.model`,
`llm.fallback_model`, and similar; `llm.routing` is object-valued and has no enumerable editor
today, per `.claude/rules/architecture.md`). The split is per-consumer, not per-field: a model
routed through `prepare_invocation()` reaches the AgentRuntime for any pipeline step with
`requires_tools = True` (`sova/core/agent_dispatch.py`), so `roles.*_model` (`developer_model`,
`reviewer_model`, etc., all `options_source="llm"`) and any `llm.routing[task_type]` value are
dual-axis: a non-Claude ID (`openai/...`, `ollama/...`) chosen there breaks tool-using steps under
`agent.runtime = "claude-code"` with `AgentRuntimeUnavailableError` from
`_sanitize_model_for_claude_cli()`. To choose the coding agent's model for those steps without
that risk, set `roles.developer_runtime_model` instead: its dropdown is sourced from
`layer="runtime"`, and it replaces the routed model for tool-using developer steps only, leaving
text-only calls on their LLM-layer routing. `agent.model` and `agent.fallback_models` are the purely
runtime-axis pair: they go to the coding agent runtime's `--model` flag (see above), not to
`llm.provider`, so on a non-Anthropic `llm.provider` (`ollama`, `openai`) their dropdowns can offer
IDs the runtime will reject outright; use the "Custom..." escape hatch for `agent.model` (a
single-string field), or for `agent.fallback_models` (list-valued) just type the Claude ID into its
plain comma-separated text input, which stays the source of truth alongside its picker. It is
advisory for a persisted setting, never a gate there: the settings-page editor mounts a plain text
input first and only upgrades to a dropdown once discovery resolves, and the single-string editor
keeps a "Custom..." free-text escape hatch, so a valid-but-unlisted ID (a new release, a fine-tune,
an unpulled Ollama tag) is never blocked. The launch modal's per-run override is different: it is a
dropdown-only `<select>` with no free-text option, so it is only selectable from discovered IDs, and
it is simply disabled (falling back to the configured model for that run) when discovery yields no
real answer. See `.claude/rules/architecture.md`'s "Model Selection System" section and
`docs/model-selection-architecture.md` section 2.7 for the full design.

**Not every model family is reachable through every `llm.provider`:**

- **Claude (Anthropic)**: `claude-code` (including its Vertex/Bedrock-routed forms), `anthropic`,
  `vertex`, and `litellm`/`hybrid` (whose default model is a Claude ID).
- **Gemini**: *discovery* only ever lists these from the Vertex `google` publisher catalog, so a
  typo-proof dropdown for them appears only on a `vertex`-configured deployment (or `litellm`/
  `hybrid` with `ANTHROPIC_VERTEX_PROJECT_ID` set). Reaching Gemini itself does not require Vertex:
  `llm.provider = "litellm"`/`"hybrid"` with a hand-pinned, vendor-prefixed `llm.model` (e.g.
  `gemini/gemini-2.5-pro`) reaches it directly, provided that vendor's own credential (a Google AI
  Studio key, not GCP ADC) is present in the environment; see
  [docs/model-selection-migration-guide.md](model-selection-migration-guide.md). Not reachable
  through `claude-code` or `anthropic`.
- **Proprietary GPT / o-series (OpenAI)**: reachable via `llm.provider = "openai"`, but **not**
  through Vertex. Vertex does list an `openai` publisher catalog, and it serves the open-weight
  `gpt-oss` family only, so discovery filters the proprietary entries out instead of offering IDs
  no Vertex credential grants access to.

When discovery cannot reach the provider it fails open to a static curated list rather than
raising, and both consumers treat that answer as "no answer" rather than showing it. The
observable symptom is therefore a missing dropdown, not a wrong or empty one: on the settings page
the model field stays a plain text input, and in the launch modal the override select is disabled
reading "Model discovery is unavailable for the current provider". That means an auth or config
problem, not a provider with no models. Fix the underlying issue (see
[Troubleshooting](#troubleshooting) below) and the dropdown appears the next time you open that
field for editing (or reopen the launch modal), with no restart required: a curated answer is
deliberately never cached, server-side or client-side, so neither consumer polls in the background,
they only re-fetch on that next open.

---

## Personal Subscription (Claude Code CLI)

### Claude Code: What You Need

- Claude Code CLI installed: `claude --version` (v2.1.0+)
- Active Anthropic subscription (free or paid)
- No API keys or service accounts required

### Claude Code: Setup Steps

#### 1. Install Claude Code CLI

```bash
# macOS
brew install anthropic/claude-code/claude-code

# Verify
claude --version
```

#### 2. Authenticate with Your Personal Account

```bash
claude auth login
```

This opens a browser window. Sign in with your Anthropic account, approve Claude Code, and you're done.

**Verify authentication:**
```bash
claude auth status
```

Expected output:
```json
{
  "loggedIn": true,
  "authMethod": "claude.ai",
  "apiProvider": "firstParty",
  "subscriptionType": "max" | "pro" | "standard",
  "email": "you@example.com"
}
```

#### 3. Configure SOVA to Use Claude Code

**Option A: `sova config set` (recommended)**

```bash
sova config set llm.provider claude-code
sova config set llm.model ""   # let Claude Code choose
```

This validates the value before writing it to `.claude/sova.db`. As a last resort, the same change can be made directly against the database (see CLAUDE.md's "Troubleshooting Config Load Errors"), but `sova config set` is the supported path.

**Option B: Environment Variables (Temporary)**

```bash
export SOVA_LLM_PROVIDER="claude-code"
export SOVA_LLM_MODEL=""
```

#### 4. Environment Variable Scrubbing

You do **not** need to edit your shell profile to scrub the provider-routing variables for SOVA's own agents. Every subprocess SOVA spawns, whether a long-lived agent (`sova/ipc/runtime.py`) or a per-step LLM invocation (`sova/llm/providers/claude_code.py`), has the variables listed in `PROVIDER_ROUTING_VARS` (`sova/utils/env.py`) scrubbed from its environment before it starts, unless a variable is explicitly listed in `agent.env_passthrough` (default: empty). That set includes `CLAUDE_CODE_USE_VERTEX`, `CLAUDE_CODE_USE_BEDROCK`, `ANTHROPIC_VERTEX_PROJECT_ID`, `ANTHROPIC_BASE_URL`, `ANTHROPIC_MODEL`, and several related variables, regardless of where they came from: your shell profile or the terminal that started `sova server`.

This does **not** cover the model-alias pins `ANTHROPIC_DEFAULT_OPUS_MODEL`, `ANTHROPIC_DEFAULT_SONNET_MODEL`, and `ANTHROPIC_DEFAULT_HAIKU_MODEL`: they are not members of `PROVIDER_ROUTING_VARS`, so a shell-profile export of one is inherited by `sova server` and passed unscrubbed to every spawned agent, silently repinning `agent.model` when it is a bare alias like `opus`. This is a real reason to clean your shell profile; see ["Model Not Found" Error](#model-not-found-error) below, which this now also lists as a source to check.

This scrubbing acts on the *inherited process environment* SOVA hands to the child; it cannot reach `~/.claude/settings.json`. The Claude CLI reads its own `env` block out of that file and applies it to itself after exec. No invocation path (`build_claude_cli_args()`, `sova/llm/cli_args.py`) ever passes `--restricted` or `--setting-sources`, the flags that make the CLI skip its settings files, so `~/.claude/settings.json` is always loaded, on the normal agent spawn path and on isolated, tool-free probe calls alike. (`isolated=True` only adds `--safe-mode --tools ""`, which disables CLAUDE.md auto-discovery, hooks, MCP servers, and custom commands/agents; per `claude --help`, auth and model selection are explicitly unaffected, so a settings.json `env` block still applies.) A routing variable set there still reaches every SOVA-spawned agent no matter what `agent.env_passthrough` says. See [Claude Code Shows "Vertex API"](#claude-code-shows-vertex-api) under Troubleshooting for how to check and clean that file.

**What shell-profile cleanup is actually for**: removing a `PROVIDER_ROUTING_VARS` entry such as `CLAUDE_CODE_USE_VERTEX=1` from `~/.zshrc`/`~/.bashrc` will NOT change how SOVA's own agents behave; they already have it scrubbed. It only matters for that variable if you also run `claude` interactively in a terminal outside of SOVA: an interactively-run `claude` command inherits your shell's environment directly and is untouched by SOVA's scrubbing, so an old variable still reaches it. Removing a model-alias pin (`ANTHROPIC_DEFAULT_SONNET_MODEL` and friends) is different: that one does matter for SOVA's own agents, since it is never scrubbed. Clean up a routing variable in your shell profile only if you also want plain interactive `claude` sessions to use your personal subscription; clean up an alias pin regardless.

If you genuinely need a routing variable inside a SOVA-spawned agent (e.g. you intentionally want `llm.provider = "claude-code"` agents to go through a Vertex-backed Claude deployment), opt it back in explicitly:

```bash
sova config set agent.env_passthrough '["CLAUDE_CODE_USE_VERTEX", "ANTHROPIC_VERTEX_PROJECT_ID"]'
```

#### 5. Start SOVA Server

```bash
sova server start --multi
```

No wrapper script or manual `unset` is needed: the scrubbing described above runs automatically for every spawned agent, regardless of what the server process itself inherited.

#### 6. Verify in the Dashboard

Open http://127.0.0.1:8111 and go to **Connections**. The "AI provider for SOVA calls" list shows a row for every supported provider, not just the active one: each row carries its own live readiness probe (all of them run concurrently on page load) plus Validate and Activate actions, and the active one is badged. The `claude-code` row additionally renders a "Start reconnect (claude auth login)" button. That button is not gated on whether `claude-code` is currently authenticated: it appears whenever this dashboard session could actually complete a browser login, and is replaced by terminal instructions when it could not (non-loopback access, a headless server process with no display to open a browser on, or a CLI routed through Vertex/Bedrock, where reconnecting means refreshing Google ADC instead). Its presence is therefore not a signal that you are logged out. On the headless case, the Connections page's fallback text points at plain `claude auth login` on a machine with a browser; it is the separate provider status widget shown on other dashboard pages (`sova/dashboard/static/provider-status-widget.js`), not this page, whose "Recovery command" block offers `claude setup-token` as the headless/remote alternative. The provider status widget, the account row in the sidebar footer of every dashboard page (click it for the status popover; a collapsed sidebar shows only its status dot, and hovering the dot spells out the provider and state, for example `claude-code: authenticated`), calls `GET /api/auth/status`, which reports `authenticated`, the account identity (email, subscription tier) when confirmable, and any inherited routing variables under `routing_warnings`. See [Connections Page (Dashboard)](#connections-page-dashboard) below for the exact contract.

---

## Anthropic API Direct

### Anthropic API: What You Need

- Anthropic API key from https://console.anthropic.com/api-keys
- API key stored in the OS keyring via `llm.api_key` (recommended) or exported as `ANTHROPIC_API_KEY` (fallback); see [3. Set API Key](#3-set-api-key) below

### Anthropic API: Setup Steps

#### 1. Create API Key

1. Go to https://console.anthropic.com/api-keys
2. Click "Create Key"
3. Copy the key (starts with `sk-ant-`)
4. **Store securely** (never commit to git)

#### 2. Configure SOVA

```bash
sova config set llm.provider anthropic
sova config set llm.model claude-sonnet-5
```

#### 3. Set API Key

**Option A: Connections page or `sova config set` (recommended)**

```bash
sova config set llm.api_key "sk-ant-..."
```

`llm.api_key` is stored in the OS keyring (Keychain/Credential Manager/Secret Service) when a usable backend exists, scoped to this project and provider (`sova/llm/keyring_store.py`); otherwise it falls back to the plaintext `project_settings` database column. It is the first-class credential source and takes precedence over the environment variable below.

The provider in that scope is whichever `llm.provider` is active when the key is written, not whichever provider you later switch to: `sova config set` always resolves the scope from the currently active provider. Run step 2 (set `llm.provider`) before this step, as shown above. If you switch providers later, re-run `sova config set llm.api_key "sk-ant-..."` after the switch; the key saved under the old provider's scope is not carried over, and the Connections page's per-provider card avoids this entirely by saving its own `provider` in the request.

**Option B: Environment variable (temporary, this session only)**

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
```

Only used when `llm.api_key` is not configured.

#### 4. Start SOVA

```bash
sova server start --multi
```

---

## Google Vertex AI

### Vertex AI: What You Need

- Google Cloud project with Vertex AI enabled
- Service account with Vertex AI permissions
- Application Default Credentials (ADC) configured
- `pip install "sova[litellm,vertex]"`: `llm.provider = "vertex"` routes through `LiteLLMProvider`, which requires the LiteLLM extra (see `docs/model-selection-migration-guide.md`); the `vertex` extra supplies `google-auth`, which `check_available()` and model enumeration both need to mint an ADC bearer token (`VertexTokenProvider`, `sova/llm/gcp_auth.py`). A missing `google-auth` does not surface as an import error: `check_available()`'s broad exception handling reports it as "litellm X but Google Application Default Credentials are not available," which reads as a credentials problem even when ADC itself is configured correctly and the only thing missing is the Python package.
- A GCS bucket is only needed for the separate batch backend (`llm.batch_gcs_bucket`, see [Supported Providers](#supported-providers) above), not for `llm.provider = "vertex"` itself

### Vertex AI: Setup Steps

#### 1. Enable Vertex AI

```bash
gcloud services enable aiplatform.googleapis.com --project=YOUR_PROJECT_ID
```

#### 2. Set Up Service Account

```bash
# Create service account
gcloud iam service-accounts create sova-agent \
  --project=YOUR_PROJECT_ID

# Grant Vertex AI permissions
gcloud projects add-iam-policy-binding YOUR_PROJECT_ID \
  --member="serviceAccount:sova-agent@YOUR_PROJECT_ID.iam.gserviceaccount.com" \
  --role="roles/aiplatform.user"

# Create and download key
gcloud iam service-accounts keys create ~/sova-key.json \
  --iam-account=sova-agent@YOUR_PROJECT_ID.iam.gserviceaccount.com
```

#### 3. Configure SOVA

`llm.provider = "vertex"` routes through `LiteLLMProvider`, using LiteLLM's generic `vertex_ai/<model>` routing for Vertex-hosted models (typically Gemini). It has no shared default model, so `llm.model` is mandatory. See the "Vertex AI" section of [docs/model-selection-migration-guide.md](model-selection-migration-guide.md#vertex-ai) for the exact config shape and what `sova doctor` checks. Do not set `llm.routing` here: that field maps task types to model tiers and is unrelated to Vertex project configuration (see `.claude/rules/architecture.md`'s "Model Selection System").

`sova config set` validates each key against the whole resulting config (`_validate_config_consistency()`, `sova/dashboard/services/settings_service.py`), so set `llm.model` first: setting `llm.provider = "vertex"` while `llm.model` is still empty is rejected outright, since the combined state at that point has a model-required provider with no model. The same ordering applies to the `openai`/`ollama` shortcuts linked under [Supported Providers](#supported-providers) above. The Connections page's Activate button is not affected by this ordering, since it writes both keys together through `update_config_many()`, which validates the combined state in one call.

```bash
sova config set llm.model "vertex_ai/gemini-2.5-pro"
sova config set llm.provider vertex
```

#### 4. Set Environment Variables

```bash
export GOOGLE_APPLICATION_CREDENTIALS="$HOME/sova-key.json"
export ANTHROPIC_VERTEX_PROJECT_ID="YOUR_PROJECT_ID"
```

`CLOUD_ML_REGION` defaults to `us-east5` if unset; set it explicitly if your Vertex project uses a different region. Both of these variables are read by this provider (`sova/llm/litellm_provider.py`: `_vertex_project_id()`, `_vertex_region()`) and, with the same names and the same `us-east5` default, by the separate batch backend (`sova/llm/providers/anthropic_batch.py:create_batch_provider()`). What makes the two independent is how each is selected, not which variables they read: the batch backend ignores `llm.provider` entirely and activates only once `llm.batch_gcs_bucket` is also set. So exporting these two variables to configure `llm.provider = "vertex"` will also point the batch backend at that same project and region if a GCS bucket is configured. See [Supported Providers](#supported-providers) above.

**Two of these exports do not reach spawned agents.** `ANTHROPIC_VERTEX_PROJECT_ID` and `CLOUD_ML_REGION` are both members of `PROVIDER_ROUTING_VARS` (`sova/utils/env.py`), so `scrub_agent_env()` strips them from every subprocess SOVA spawns unless explicitly opted back in. `GOOGLE_APPLICATION_CREDENTIALS` is not a provider-routing variable and is not scrubbed at all; it reaches every spawned agent untouched, so it needs no `agent.env_passthrough` entry. This matters for `llm.provider = "vertex"` in a way it does not for `llm.provider = "claude-code"`: a developer/researcher/planner pipeline run is spawned directly as `sova run N` (`sova/ipc/runtime.py`'s `spawn_direct()`), and that subprocess constructs its own `LiteLLMProvider` in-process, where a scrubbed `ANTHROPIC_VERTEX_PROJECT_ID` makes `_vertex_project_id()` return `""`. Exporting these variables and starting the server is enough for in-server calls, but every spawned pipeline agent needs the scrubbed one(s) passed through explicitly. Only include `CLOUD_ML_REGION` if you actually set it above; `_vertex_region()` already defaults to `us-east5`, so it needs no passthrough entry otherwise:

```bash
sova config set agent.env_passthrough '["ANTHROPIC_VERTEX_PROJECT_ID", "CLOUD_ML_REGION"]'
```

`agent.env_passthrough` is a single list shared by every spawn path across every runtime and provider, and `sova config set` replaces the whole list rather than appending to it. If you also followed [4. Environment Variable Scrubbing](#4-environment-variable-scrubbing)'s Claude Code snippet (`CLAUDE_CODE_USE_VERTEX`, `ANTHROPIC_VERTEX_PROJECT_ID`), running the command above without it drops that entry. Merge the variables you actually need into one list instead of running each snippet in isolation, e.g. `'["CLAUDE_CODE_USE_VERTEX", "ANTHROPIC_VERTEX_PROJECT_ID", "CLOUD_ML_REGION"]'`.

#### 5. Start SOVA

```bash
sova server start --multi
```

---

## Troubleshooting

### Claude Code Shows "Vertex API"

**Problem:** `claude auth status` shows `"apiProvider": "vertex"` even though you want personal subscription.

**Where this can happen:**

1. **Inside a SOVA-spawned agent, via `agent.env_passthrough`**: SOVA scrubs `CLAUDE_CODE_USE_VERTEX`/`CLAUDE_CODE_USE_BEDROCK` (and the rest of `PROVIDER_ROUTING_VARS`, `sova/utils/env.py`) from every agent and LLM-invocation subprocess it spawns, from its own inherited environment. If you still see it, check for an entry re-admitting one of these variables: `sqlite3 .claude/sova.db "SELECT value FROM project_settings WHERE key='agent.env_passthrough';"` (or the Settings page in the dashboard).
2. **Inside a SOVA-spawned agent, via `~/.claude/settings.json`**: SOVA's scrubbing only filters the process environment it hands to the child; it cannot touch the `env` block the Claude CLI itself reads from `~/.claude/settings.json` after it starts, since no invocation path passes `--restricted` or `--setting-sources` (the flags that skip settings files). `--safe-mode`, used only for isolated probe calls, does not disable settings.json either: it disables CLAUDE.md/hooks/MCP/commands, not the `env` block. A `CLAUDE_CODE_USE_VERTEX` entry there reaches every SOVA-spawned agent regardless of `agent.env_passthrough`. Check and clean step 3 below.
3. **In your own interactive terminal**, running `claude` directly rather than through SOVA: SOVA's scrubbing only applies to its own spawned subprocesses. This is also fixed by the steps below.

**Solution:** steps 1, 2, 4, and 5 address the interactive-terminal case (cause 3 above); step 3 (`~/.claude/settings.json`) fixes cause 2 as well, since that file reaches both an interactive `claude` session and every SOVA-spawned agent. Cause 1 (`agent.env_passthrough`) is fixed separately, by removing the re-admitted variable from that config list.

1. Check for old env vars:
   ```bash
   env | grep -i "VERTEX\|GCP_PROJECT\|CLAUDE_CODE_USE"
   ```

2. Comment out gcloud SDK initialization in `~/.zshrc`:
   ```bash
   # if [ -f '/Users/YOUR_USER/google-cloud-sdk/path.zsh.inc' ]; then . ...; fi
   ```

3. Remove Vertex config from `~/.claude/settings.json`:
   ```json
   {
     "env": {
       // Delete these:
       // "CLAUDE_CODE_USE_VERTEX": "1",
       // "ANTHROPIC_VERTEX_PROJECT_ID": "...",
       // "GCP_PROJECT_ID": "..."
     }
   }
   ```

4. Log out and back in:
   ```bash
   claude auth logout
   claude auth login
   ```

   On a headless machine or a second machine with no browser, use `claude setup-token` instead of `claude auth login` (see the recovery hint rendered by `sova/dashboard/static/provider-status-widget.js`). Issue #873 covers headless/second-machine authentication setup in more depth; this guide only notes the command exists.

5. Verify:
   ```bash
   claude auth status --json | jq .apiProvider
   # Should show: "firstParty"
   ```

### "Model Not Found" Error

**Problem:** SOVA tries to use `claude-sonnet-4-5@20250929` which doesn't exist in your subscription.

**Solution:**

Check the `ANTHROPIC_DEFAULT_SONNET_MODEL` in your `~/.claude/settings.json` and remove it (let Claude Code choose):

```json
{
  "env": {
    // Remove this line:
    // "ANTHROPIC_DEFAULT_SONNET_MODEL": "claude-sonnet-4-5@20250929"
  }
}
```

Also check your shell profile (`~/.zshrc`/`~/.bashrc`) for the same variable (or `ANTHROPIC_DEFAULT_OPUS_MODEL`/`ANTHROPIC_DEFAULT_HAIKU_MODEL`): unlike the provider-routing variables covered in [4. Environment Variable Scrubbing](#4-environment-variable-scrubbing) above, these alias pins are never scrubbed from spawned agents, so a profile export is just as likely a cause as the settings file.

Then restart SOVA:

```bash
sova server stop
sova server start --multi
```

### API Key Rejected

**Problem:** the configured Anthropic API key is invalid, revoked, or not found.

**Solution:**

1. Check `llm.api_key` first: it is keyring-resolved (`_resolve_scoped_api_key()`, `sova/llm/provider.py`) and takes precedence over `ANTHROPIC_API_KEY`, so an env var alone is not the whole picture (see [3. Set API Key](#3-set-api-key) above):
   ```bash
   sova config set llm.api_key "sk-ant-..."
   ```
   or set it from the Anthropic API card on the Connections page. If you recently switched `llm.provider`, re-run this command after the switch: the keyring entry is scoped to whichever provider was active when it was written, so a key saved under the old provider is not read for the new one.

2. Run `sova doctor`. Its `keyring: llm.api_key` check catches the case where the database holds the `"keyring:stored"` migration sentinel but the matching keychain entry can't be read back, e.g. after copying `.claude/sova.db` to another machine without its keychain.

3. Test the key directly rather than guessing: there is no `claude` CLI flag for this (`--api-key` does not exist; `claude --help` lists no such option). Use the Connections page's "Test" button (`POST /api/connections/llm/test`), which runs one real, billable `invoke()` call against the configured key, or `sova doctor`.

4. Check it's not been revoked in https://console.anthropic.com/api-keys.

5. If still unresolved and an env var is in play, verify it's actually set (note this is only the fallback source, checked above `llm.api_key`):
   ```bash
   echo $ANTHROPIC_API_KEY | head -c 10
   ```

---

## Connections Page (Dashboard)

The manual, terminal-based workflow above has a dashboard equivalent: **Connections**, in the main nav. It replaced the modal setup wizard originally proposed for this doc with a simpler, always-visible page: there is no first-run modal, no server-driven OAuth flow, and no "Switch Provider" wizard. The issues behind it (#929 env scrubbing, #930 `check_available()` reporting authentication, #931 access control for credential-mutating endpoints, #932 keyring storage for `llm.api_key`, #933 the status widget and guided setup) are all closed and merged; this is what shipped. #921 is closed too, but it is a different thing: it added the `openai`/`ollama`/`vertex` provider types this page now lists, not any part of the dashboard auth flow.

- **`GET /api/connections`** (`sova/dashboard/routers/auth.py`, `setup_service.get_connection_catalog()`): lists every `llm.provider` and `agent.runtime` value with its readiness state, for the "AI provider for SOVA calls" and "Coding agent" lists on the page.
- **`POST /api/connections/llm/validate`**: a free readiness probe for a candidate provider (CLI presence, daemon reachability, credential presence). Persists nothing.
- **`POST /api/connections/llm/test`**: runs one real, billable `invoke()` call against a candidate provider.
- **`POST /api/connections/llm/activate`**: persists a candidate as `llm.provider`, after re-validating it.
- **`POST /api/connections/runtime/validate`** / **`/activate`**: the same validate/activate pattern for `agent.runtime`.
- **`POST /api/connections/reconnect/start`**, **`GET .../reconnect/status`**, **`POST .../reconnect/cancel`**: drives a `claude auth login` subprocess for local reconnect, polled from the page.
- **`GET /api/auth/status`**: read-only status for the provider-status widget in the sidebar footer of every dashboard page.
- **`POST /api/settings/config`** (`sova/dashboard/routers/settings.py`): not an auth endpoint, but the one the page's "Save key" button calls, with `{"key": "llm.api_key", "value": ..., "provider": ...}`. There is no `/api/connections` endpoint for credentials; secrets reuse the ordinary settings write path, which routes `RESOLVED_SECRET_KEYS` into the keyring.

Every `POST` above is guarded by `require_same_origin_csrf`, so these are driven by the dashboard page rather than by hand: a bare `curl` without the matching origin and CSRF headers is rejected. The three `GET` endpoints (`/api/auth/status`, `/api/connections`, `/api/connections/reconnect/status`) require no CSRF token or Origin match and have no side effects. That does not make them safe from every angle: they are also unauthenticated, since the dashboard has no login system (`sova/dashboard/security.py`), so on a non-loopback bind (which that module explicitly supports once a shared CSRF secret is configured) any caller who can reach the port can read them, including `/api/auth/status`'s `account` block (email, org name/ID, subscription tier). This matches the dashboard's single-user, loopback-by-default security model rather than a bug specific to these three endpoints.

```bash
curl http://127.0.0.1:8111/api/auth/status
```

```json
{
  "provider": "claude-code",
  "authenticated": true,
  "detail": "2.1.0 (you@example.com)",
  "account": {
    "email": "you@example.com",
    "authMethod": "claude.ai",
    "apiProvider": "firstParty",
    "subscriptionType": "max"
  },
  "api_key_configured": false,
  "routing_warnings": [],
  "checked_at": "2026-10-12T17:30:00Z",
  "cached": false
}
```

`account` is a whitelisted subset of the `claude auth status --json` payload, filtered to `_ACCOUNT_FIELDS` (`sova/dashboard/services/setup_service.py`): `email`, `authMethod`, `apiProvider`, `orgId`, `orgName`, `subscriptionType`. Only the keys the probe actually returned are present, so the example above (a personal subscription) has no `orgId`/`orgName`. `account` is `None` whenever the provider has no `get_auth_details()` method, or that probe can't confirm a logged-in identity (CLI missing, unparseable `claude auth status --json`, `loggedIn` not `true`). `authenticated` is a separate field: for `claude-code`, `get_auth_status()` overrides it to `account is not None` so a fail-open `check_available()` result ("auth state unknown", used so an unconfirmed probe never blocks agent spawning) is never reported as a confirmed login; for every other provider (no `get_auth_details()`), `authenticated` is the raw `check_available()` result and can be `True` while `account` stays `None` forever. Never treat a populated `account` as the sole proof of being logged in, or a null one as proof of being logged out: check `authenticated` for that. `routing_warnings` lists any `PROVIDER_ROUTING_VARS` present in the server process's own environment as a diagnostic only; it does not mean one reached (or will reach) a spawned agent, since those are scrubbed independently at the spawn boundary (see [4. Environment Variable Scrubbing](#4-environment-variable-scrubbing) above).

An API key entered on the Connections page goes through the same OS-keyring-backed storage used by the CLI steps above (`sova/llm/keyring_store.py`), via `POST /api/settings/config` with `key: "llm.api_key"` and the card's `provider` for keyring scoping. There is no separate credentials database table and no audit log of auth events.

**The Claude Code credential is not SOVA's to store.** `llm.provider = "claude-code"` has no `llm.api_key`: the `claude` CLI owns its own OAuth credential in the OS keychain and refreshes it itself. `RESOLVED_SECRET_KEYS` (`sova/llm/keyring_store.py`) contains only `llm.api_key`, so nothing in SOVA's keyring namespace corresponds to a Claude Code login. The reconnect endpoints spawn `claude auth login` and observe only its exit code; SOVA never reads, copies, or persists the resulting token, and re-derives the login state afterwards by re-running the `claude auth status --json` probe.

## Summary

| Step | Manual (terminal) | Connections page (dashboard) |
|------|--------------------|-------------------------------|
| 1. Install Claude Code | Terminal | Terminal (prerequisite either way) |
| 2. Authenticate | `claude auth login` (or `claude setup-token` headless) | "Start reconnect (claude auth login)" button |
| 3. Configure provider | `sova config set llm.provider ...` | Row selection + "Activate" button |
| 4. Clean env vars | For `llm.provider = "claude-code"`: shell-profile vars not needed (scrubbed automatically), but `~/.claude/settings.json` is not scrubbed and still needs manual cleanup (see [Troubleshooting](#claude-code-shows-vertex-api)). For `llm.provider = "vertex"`: the opposite, since spawned pipeline agents need `agent.env_passthrough` set to actually use Vertex (see [4. Set Environment Variables](#4-set-environment-variables) under Vertex AI) | Same per-provider distinction as the manual column |
| 5. Start server | `sova server start --multi` | Same; the dashboard does not start the server for you |
| 6. Monitor auth | `GET /api/auth/status` | Provider status widget, in the sidebar footer of every page |

Both paths reach the same configured state; the Connections page is a UI for the operations the CLI/`sova config set` steps above already perform, not a separate credential system.
