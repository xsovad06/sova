# LLM Provider Migration Guide

This guide covers switching `llm.provider` to a non-default backend: `openai`, `ollama`, or
`vertex`. All three route through the same `LiteLLMProvider` class that already backs
`litellm`/`hybrid` (see `.claude/rules/architecture.md`, "Model Selection System"): they exist
as first-class `provider` values purely for config clarity, not new runtime capability. Everything
below already works today via `llm.provider = "litellm"` plus a vendor-prefixed `llm.model`; the
three new values just make that intent explicit and give each vendor its own settings-UI option
and `sova doctor` messaging.

## Common requirement: `llm.model` is mandatory

Unlike `litellm`/`hybrid` (which default to a Claude model when `llm.model` is empty),
`openai`, `ollama`, and `vertex` have no shared default model. Leaving `llm.model` empty for one
of these three raises a config validation error at load time rather than silently picking an
unrelated vendor's model.

## OpenAI

```toml
[llm]
provider = "openai"
model = "gpt-5"
```

Requires a credential, plus the LiteLLM extra (`pip install sova[litellm]`). `llm.api_key` (set via the
Connections page or `sova config set llm.api_key <key>`, stored in the OS keyring when available) is the
first-class source and takes precedence; an `OPENAI_API_KEY` exported in the environment is only a
fallback when `llm.api_key` is not configured. No local daemon is needed. `sova doctor`'s "llm provider"
check (`LiteLLMProvider.check_available()`) confirms both that LiteLLM is importable and that a credential
is configured (either source); it does not validate that the key is actually valid or that the model name
exists. A bad key or model surfaces on the first real invocation.

## Ollama (local, no daemon needed for tests)

```toml
[llm]
provider = "ollama"
model = "ollama/llama3.1"
```

Requires a local `ollama serve` daemon, the model pulled (`ollama pull llama3.1`), and the LiteLLM
extra (`pip install sova[litellm]`). No API key needed. `LiteLLMProvider.check_available()` reports
whether the daemon answers `GET /api/tags` at `llm.api_base` (default `http://localhost:11434`).
`sova doctor` additionally scans `llm.model`, `llm.fallback_model`, `llm.routing`, and
`llm.model_aliases` for `ollama/`-prefixed values and reports whether the specific model is pulled.

The `ollama/` prefix is mandatory: LiteLLM routes on the model prefix, not on SOVA's `llm.provider`
name, so `model = "llama3.1"` under `provider = "ollama"` reaches a different vendor entirely.
`sova doctor` reports this as a failing "ollama model prefix" check.

In tests, the `ollama` provider (like `litellm`/`hybrid`) can be exercised entirely against a
faked LiteLLM backend (no daemon required). See `tests/test_llm.py`'s `mock_litellm` fixture and
`TestLiteLLMProvider.test_ollama_provider_round_trip_no_daemon` for the pattern: patch `sys.modules`
with a mocked `litellm` module and assert `litellm.acompletion` was called with the expected
`ollama/<model>` string.

## Vertex AI

```toml
[llm]
provider = "vertex"
model = "vertex_ai/gemini-2.5-pro"
```

This is LiteLLM's generic `vertex_ai/<model>` routing (Gemini or any other Vertex-hosted model)
for the synchronous `invoke()`/`invoke_streaming()` path. This is **not** the same integration as
`ANTHROPIC_VERTEX_PROJECT_ID` in `sova/llm/providers/anthropic_batch.py`, which is a separate,
Anthropic-only backend used exclusively by the batch API path (`invoke_batch()` for
batch-eligible task types like triage). Configuring `llm.provider = "vertex"` here has no effect
on that batch backend, and vice versa.

Requires the LiteLLM extra (`pip install sova[litellm]`) and Google Cloud auth for LiteLLM's Vertex
integration: either
`GOOGLE_APPLICATION_CREDENTIALS` pointing at a service account key, or Application Default
Credentials (`gcloud auth application-default login`), plus the project/region environment
variables LiteLLM's Vertex integration expects. `LiteLLMProvider.check_available()` confirms
`ANTHROPIC_VERTEX_PROJECT_ID` is set and that a token can actually be minted from those credentials.

## Verifying a switch locally

```bash
sova doctor
```

`_check_llm_provider` reports backend availability for whichever provider is configured, including
the vendor-specific credential check described above for `openai`/`ollama`/`vertex`; `_check_ollama`
additionally verifies the daemon and pulled models when an `ollama/`-prefixed model is in play.

Selecting one of these providers from the dashboard settings dropdown without also setting
`llm.model` is rejected at save time rather than persisted, so the settings page cannot lock
itself out of the field needed to fix it.

## Setting an unknown provider

`llm.provider` accepts only the seven values above (`claude-code`, `litellm`, `hybrid`,
`anthropic`, `openai`, `ollama`, `vertex`). Setting anything else fails config loading with a
readable `RuntimeError` (CLI: printed to stderr with a clean exit, not a stack trace) instead of a
raw Pydantic traceback. See `docs/troubleshooting-config.md` for diagnosis and fix steps.
