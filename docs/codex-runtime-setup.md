# Codex Runtime Setup Guide

How to authenticate and configure OpenAI's Codex CLI as SOVA's agent runtime
(`agent.runtime = "codex"`). This is a separate concern from
[AI Provider Setup Guide](ai-provider-setup-guide.md), which configures the
LLM backend the step pipeline calls in-process (`llm.provider`): Codex is a
coding-agent *runtime* (`sova/ipc/runtime.py:CodexRuntime`), the subprocess
SOVA spawns to execute a pipeline step's instructions with real tool use
(file edits, shell commands), the same role `"claude-code"`
(`ClaudeCodeRuntime`, the default) plays. `agent.runtime` and `[codex]` in
`sova.toml` determine whether a project's agent spawns go through it;
nothing here changes which LLM a non-tool-using step call uses. One other
setting reaches the Codex CLI independently of `agent.runtime`:
`review.repo_context_agent` (default off) makes the reviewer role run its
own read-only `codex exec` repo-context sub-call, which reads `codex.model`
even while `agent.runtime = "claude-code"` (see
`ReviewerRole._gather_repo_context()`).

Command surface verified against `@openai/codex` 0.162.1 (npm, checked
2026-10-12). Codex CLI releases are external to SOVA: if a command below no
longer matches `codex --help` / `codex login --help` on your installed
version, trust the installed CLI's own `--help` output over this document,
and see "CLI version drift" below.

## One Supported Setup Path

There is exactly one recommended path, with two documented fallbacks for
environments that cannot use it, and one separate, narrower pattern for
trusted automation. Do not mix them: pick the browser login unless its
prerequisite (an interactive terminal plus a reachable browser) is
unavailable.

### 1. Install the Codex CLI

```bash
npm install -g @openai/codex
codex --version
```

### 2. Recommended: browser login, stored in the OS keyring

Codex stores the credential wherever its own `cli_auth_credentials_store`
setting points, and that setting defaults to `"file"`: a plaintext
`$CODEX_HOME/auth.json` (`~/.codex/auth.json`), not the OS keyring. Opt into
keyring storage before logging in, in Codex's own `~/.codex/config.toml`:

```toml
# ~/.codex/config.toml (Codex's own config, not sova.toml)
cli_auth_credentials_store = "keyring"   # or "auto"
```

The four accepted values are `"file"` (plaintext `auth.json`, the default),
`"keyring"` (OS credential store, fails when no backend is available),
`"auto"` (keyring when available, `auth.json` otherwise), and `"ephemeral"`
(in memory, for the current process only). `"keyring"` or `"auto"` is the
right choice for a workstation or a long-running server; if you leave the
default `"file"`, treat `~/.codex/auth.json` like a password, because it
holds a live credential in plaintext.

Then log in:

```bash
codex login
```

This opens a browser-based ChatGPT sign-in flow and hands the resulting
credential to the store selected above. SOVA never reads, copies, or
persists it: SOVA's only interaction is a read-only readiness probe,
`codex login status` (never a model request; see
`CodexRuntime.check_available()` and `_probe_codex_auth()` in
`sova/ipc/runtime.py`).

Verify:

```bash
codex login status
```

### 3. Fallback: no browser for the sign-in flow

`codex login`'s browser flow needs an interactive terminal and a reachable
browser. A headless or remote box has neither, so use one of the two
documented alternatives instead.

Still a ChatGPT sign-in, completed on another device:

```bash
codex login --device-auth
```

Or, for a machine that should authenticate with an API key rather than a
ChatGPT account, pipe the key into Codex's own login command (it reads the
key from stdin, so the value never reaches argv):

```bash
printenv OPENAI_API_KEY | codex login --with-api-key
```

Both paths still go through Codex's own login and credential-storage layer,
not SOVA, and both honour the `cli_auth_credentials_store` setting above.
`"keyring"` fails outright where no keyring backend is available (no
`gnome-keyring`/`kwallet`/`libsecret`, no D-Bus session), so a box like that
needs `"auto"` or the default `"file"`; with `"file"`, the credential lands
in plaintext `~/.codex/auth.json`.

### Trusted automation only: `CODEX_API_KEY`

This is a distinct pattern from the setup paths above, not another
alternative for routine local setup. The Codex CLI reads `CODEX_API_KEY`
from its process environment and, when set, that value **takes precedence
over every other auth method, including a keyring login already in
place** (`read_codex_api_key_from_env()` in Codex's own
`codex-rs/login/src/auth/manager.rs`). Setting it anywhere ambient (a shell
profile, a system-wide `launchctl` environment, a CI runner's persistent
env) silently overrides every `codex` invocation on that machine, not just
SOVA's, for as long as it stays set.

Use it only for a trusted automation context that deliberately wants
environment-based auth scoped to one process, never as a substitute for
`codex login` on a workstation or long-running server. SOVA's own use of it
is itself this narrow: `CodexRuntime.spawn()` re-injects it only into the
immediate `codex exec` child, scrubbing it from every other spawned runtime
by default. See "Codex/OpenAI Authentication" in
[security-guidelines.md](security-guidelines.md) for the full scrub/re-inject
mechanics, and never set it in a shared or persistent environment.

### Remediation: an already-set `CODEX_API_KEY` or `OPENAI_API_KEY`

If you previously followed guidance (including an earlier draft of this
project's own docs) that set `CODEX_API_KEY` or `OPENAI_API_KEY` globally,
remove it from every place it could persist, not just the current shell:

```bash
# Shell startup files
grep -rn "CODEX_API_KEY\|OPENAI_API_KEY" ~/.zshrc ~/.zprofile ~/.bashrc ~/.bash_profile ~/.profile 2>/dev/null

# macOS: a per-user launchd environment (affects GUI apps and every new
# shell, not just interactive ones)
launchctl getenv CODEX_API_KEY
launchctl getenv OPENAI_API_KEY
launchctl unsetenv CODEX_API_KEY
launchctl unsetenv OPENAI_API_KEY
```

Delete any matching `export CODEX_API_KEY=...` / `export OPENAI_API_KEY=...`
line the `grep` finds, open a fresh shell, and re-run `codex login status`
to confirm the CLI falls through to your saved login credential again.
Because the env var wins over a saved login unconditionally, a stray global
value is invisible until something changes which credential it was shadowing (e.g. a
revoked API key), at which point every `codex` invocation on the machine
fails, not just SOVA's.

## Configure SOVA

Configuration is database-backed (`.claude/sova.db`), and `load_config()`
resolves a key as env > database > `sova.toml` > default, so write these
through `sova config set` (or the dashboard settings page) rather than
hand-editing `sova.toml`: on a project whose TOML was already auto-migrated
into the database, a later TOML edit is shadowed by the stored row and
silently has no effect.

```bash
sova config set agent.runtime codex          # default: claude-code
sova config set codex.sandbox workspace-write   # or: read-only
sova config set codex.model ""               # empty: Codex CLI's own configured default
sova config set codex.reader_drain_timeout 10.0
sova config set codex.allow_unenforced_budget true  # required: Codex CLI cannot enforce a dollar cap
sova config set codex.billing_mode subscription     # or: unknown (default)
```

Codex has no mechanism to pass a dollar budget cap to `codex exec`, unlike
Claude Code's `--max-budget-usd`. Because every pipeline step requests a
budget (`agent.max_budget - ctx.cost_usd`, always positive), `agent.runtime
= "codex"` fails at the earliest pipeline step (`AssessStep`, before any
worktree is created) unless `codex.allow_unenforced_budget = true` is set,
accepting that cost runs unbounded (logged at warning level on every
spawn). `codex.billing_mode` records whether an authenticated session is
billed against a flat-rate ChatGPT subscription (`"subscription"`, no
incremental per-call dollar cost) or metered per token; it defaults to
`"unknown"` because neither a set `CODEX_API_KEY` nor `codex login
status`'s logged-in signal is positive evidence either way, and only an
explicit operator declaration promotes the recorded `CostSource` to
`SUBSCRIPTION`. A `CODEX_API_KEY` env var, when set, still always wins as
confirmed metered billing, overriding this setting.

`sova config`'s summary table does not list these keys; `sova doctor` (see
"Verify" below) reports the resolved runtime, and the dashboard settings page
shows every `codex.*` value.

A project with no database yet (pre-`sova install`) reads the same keys from
`sova.toml`:

```toml
# sova.toml
[agent]
runtime = "codex"   # default: "claude-code"

[codex]
# model = ""             # empty: use Codex CLI's own configured default
# sandbox = "workspace-write"   # "read-only" | "workspace-write" (never "danger-full-access")
# reader_drain_timeout = 10.0
# allow_unenforced_budget = true   # required: Codex CLI cannot enforce a dollar cap
# billing_mode = "subscription"    # or: "unknown" (default)
```

Either source is overridable per process by environment variable, which wins
over both: `SOVA_AGENT_RUNTIME=codex`, and `SOVA_CODEX_` for `[codex]` fields
(e.g. `SOVA_CODEX_SANDBOX`).

`sandbox` is deliberately restricted to `"read-only"` and `"workspace-write"`
(a Pydantic `Literal`, see `CodexConfig` in `sova/config/models.py`):
`"danger-full-access"` is not a valid value, so a SOVA-spawned Codex agent
can never be configured with unrestricted filesystem/network access.
`"workspace-write"` is the default pipeline sandbox (confined to the
worktree, network per Codex's own policy); `"read-only"` denies every write
and all network access for the whole process tree, reserved for a role that
needs nothing but reads.

## Verify

```bash
sova doctor
```

`sova doctor` runs the same `check_available()` SOVA's own dashboard
Connections page uses (it probes whichever runtime `agent.runtime` names, so
set that to `"codex"` first): CLI presence (`codex --version`), then
`codex login status` to distinguish "not installed" from "installed but
logged out" from "installed and authenticated". No model request is made by
either check.

## Rollback to Claude Code

One setting change, no code change and no data migration:

```bash
sova config set agent.runtime claude-code
```

`SOVA_AGENT_RUNTIME=claude-code` does the same for a single process, and
`"claude-code"` is the implicit default if the setting is cleared entirely.
Restart a running `sova server` for the change to take effect in it.
Existing Codex credentials are untouched either way: SOVA never stored any in
the first place.

## Mid-Flight Rotation

Rotating or revoking a credential (keyring re-login, or changing
`CODEX_API_KEY`) does not affect a Codex agent process already spawned and
running: `CodexRuntime.spawn()` resolves the credential once, at spawn time,
and the already-running `codex exec` child keeps whatever environment and
cached login state it started with. An in-flight run is unaffected by a
rotation until it exits and a new one spawns; there is no live credential
refresh inside a single `codex exec` invocation triggered by SOVA.

## Edge Cases Summary

| Situation | What to do |
|---|---|
| No browser or interactive terminal (remote/headless) | `codex login --device-auth`, or `printenv OPENAI_API_KEY \| codex login --with-api-key` for API-key auth |
| No OS keyring backend on this machine | Leave `cli_auth_credentials_store` at `"auto"`/`"file"`; with `"file"` the credential is plaintext in `~/.codex/auth.json` |
| `codex` commands in this guide don't match the installed CLI | Trust `codex --help` / `codex login --help`; see "CLI version drift" |
| `CODEX_API_KEY`/`OPENAI_API_KEY` set globally from old guidance | See "Remediation" above; unset from shell profiles and `launchctl` |
| Credential rotated while an agent run is in progress | That run keeps its original credential/env until it exits; unaffected until the next spawn |
| Rolling back from Codex to Claude Code | `sova config set agent.runtime claude-code`; no other change needed |

## CLI Version Drift

`codex exec`'s flag names (`--sandbox`, `--model`, `--json`) are hardcoded
in `_codex_exec_args()` (`sova/ipc/runtime.py`), so a CLI release that
renames or drops one of them surfaces as a failing `codex exec` invocation
at spawn time, with the CLI's own usage error in the agent's stderr; the fix
is a code change to that one argv builder, not a config toggle.

Only one part of the invocation is probed at runtime instead of assumed:
whether the installed CLI reads an omitted PROMPT positional from stdin.
`_probe_codex_stdin_prompt_support()` parses the installed `codex exec
--help` output itself rather than keying off a version cutoff, which would
silently become wrong on the next release. A future CLI release that
restructures that `--help` output in a way the probe can no longer recognize
raises `CodexStdinProbeAmbiguousError` at spawn time rather than silently
falling back to argv delivery, so that breaking change also surfaces as a
loud error instead of a silently wrong prompt-delivery path. The pytest consistency
test (`tests/test_codex_runtime_setup_docs.py`) guards the runtime name,
sandbox literal values, and install/check commands documented above against
the actual implementation; it cannot verify the *installed* CLI's own flag
set, only that this document's claims about SOVA's own code are current.
