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

Requires `OPENAI_API_KEY` in the environment, plus the LiteLLM extra (`pip install sova[litellm]`).
No local daemon is needed. Note that `sova doctor`'s "llm provider" check only confirms that LiteLLM
is importable (`LiteLLMProvider.check_available()` reports the LiteLLM version); it does not validate
`OPENAI_API_KEY` or the model name. A bad key surfaces on the first real invocation.

## Ollama (local, no daemon needed for tests)

```toml
[llm]
provider = "ollama"
model = "ollama/llama3.1"
```

Requires a local `ollama serve` daemon, the model pulled (`ollama pull llama3.1`), and the LiteLLM
extra (`pip install sova[litellm]`). No API key needed. `sova doctor` scans `llm.model`,
`llm.fallback_model`, `llm.routing`, and `llm.model_aliases` for `ollama/`-prefixed values and
reports whether the daemon is running and the model is pulled.

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
variables LiteLLM's Vertex integration expects.

## Verifying a switch locally

```bash
sova doctor
```

`_check_llm_provider` reports backend availability for whichever provider is configured (for the
three LiteLLM-backed types this means "LiteLLM is installed", not "your credentials work");
`_check_ollama` additionally verifies the daemon and pulled models when an `ollama/`-prefixed
model is in play.

Selecting one of these providers from the dashboard settings dropdown without also setting
`llm.model` is rejected at save time rather than persisted, so the settings page cannot lock
itself out of the field needed to fix it.

## Setting an unknown provider

`llm.provider` accepts only the seven values above (`claude-code`, `litellm`, `hybrid`,
`anthropic`, `openai`, `ollama`, `vertex`). Setting anything else fails config loading with a
readable `RuntimeError` (CLI: printed to stderr with a clean exit, not a stack trace) instead of a
raw Pydantic traceback. See `docs/troubleshooting-config.md` for diagnosis and fix steps.
