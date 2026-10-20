# Authentication UI Design Document

## Overview

This document outlines the architecture for a dashboard-based authentication system that will replace manual terminal-based provider setup. The goal is to reduce setup friction and make SOVA accessible to users unfamiliar with environment variables and CLI tooling.

## Implementation Status

This is the original design proposal. The issues that delivered it (#929, #930, #931, #932, #933) are all closed and merged; #921, often listed alongside them, is closed as well but added the `openai`/`ollama`/`vertex` provider types rather than any part of this auth flow. What shipped is simpler than the modal wizard and OAuth-in-dashboard flow described below: a single, always-visible **Connections** page plus a read-only status endpoint, with credentials resolved through an OS keyring rather than a new database schema. The **Backend API**, **Credential Storage**, and **Implementation Phases** sections, and both layers of the architecture diagram immediately below (frontend and provider), have been corrected to describe the actual shipped behavior. Every other section below (**Current Pain Points**, **User Flows**, **UI Components**, **Testing Strategy**, **Security Considerations**, **Metrics**, **Future Enhancements**, **Rollout Plan**, and **Questions & Open Items**) is design history describing the original, unbuilt modal-wizard concept, not current behavior, with two exceptions: items in **Current Pain Points** and **Security Considerations** that have since been resolved are annotated in place with what actually shipped, and **UI Components** subsection 5, "Status Sidebar Widget," describes a widget that did ship as `sova/dashboard/static/provider-status-widget.js` (shown on every dashboard page, per the setup guide linked below). See [AI Provider Setup Guide](ai-provider-setup-guide.md#connections-page-dashboard) for how setup actually works today.

## Current Pain Points

1. **Multiple configuration methods**: SQLite, env vars, `.claude/settings.json`, shell profiles
2. **Silent failures**: inherited Vertex AI env vars caused cryptic "model not found" errors. Resolved by automatic env scrubbing at the subprocess spawn boundary (`sova/utils/env.py`; see [Backend API](#backend-api-what-actually-shipped) below), not by a dashboard detection UI.
3. **No visibility**: users can't see auth status or know when re-authentication is needed. Resolved by the provider status widget and `GET /api/auth/status` (see [Backend API](#backend-api-what-actually-shipped) below).
4. **Terminal-heavy**: OAuth flow (Claude Code login) requires manual browser interaction outside the UI
5. **No validation**: API keys accepted without testing; errors only appear in server logs. Resolved by `POST /api/connections/llm/test`, a separate, explicitly billable `invoke()` the operator triggers from the Connections page; activation itself only re-runs the free `check_available()` probe (see [Backend API](#backend-api-what-actually-shipped) below).

## Proposed Solution

### Architecture

```
┌───────────────────────────────────────────────────────────────┐
│          SOVA Dashboard (Frontend): what actually shipped     │
├───────────────────────────────────────────────────────────────┤
│ ┌───────────────────────────┐  ┌───────────────────────────┐  │
│ │ Connections page          │  │ Provider status           │  │
│ │ (/connections)            │  │ widget (every page)       │  │
│ └───────────────────────────┘  └───────────────────────────┘  │
│           │                       │                           │
│           └───────────┬───────────┘                           │
│                       │                                       │
├───────────────────────────────────────────────────────────────┤
│                       ▼                                       │
│           FastAPI Backend (sova/dashboard)                    │
├───────────────────────────────────────────────────────────────┤
│                                                               │
│   Auth API (routers/auth.py): what actually shipped           │
│   ├── GET  /api/auth/status (read-only probe)                 │
│   ├── GET  /api/connections (provider/runtime)                │
│   ├── POST /api/connections/llm/validate (free probe)         │
│   ├── POST /api/connections/llm/test (billable test)          │
│   ├── POST /api/connections/llm/activate (persist choice)     │
│   ├── POST /api/connections/runtime/validate (free probe)     │
│   ├── POST /api/connections/runtime/activate (persist choice) │
│   └── POST /api/connections/reconnect/* (claude auth login)   │
│                                                               │
│   Connections/auth logic (services/setup_service.py)          │
│   ├── get_auth_status() / get_connection_catalog()            │
│   ├── validate/test/activate_llm_candidate()                  │
│   └── start/cancel_reconnect() (claude auth login subprocess) │
│                                                               │
│   LLM Provider Layer (llm/provider.py)                        │
│   ├── ClaudeCodeProvider (claude CLI subprocess, OAuth login) │
│   ├── AnthropicAPIProvider (API key)                          │
│   └── LiteLLMProvider (vertex/openai/ollama/litellm/hybrid)   │
│                                                               │
│   Keyring Store (llm/keyring_store.py)                        │
│   └── OS-level secure storage (Keychain/Credential Manager)   │
│                                                               │
└───────────────────────────────────────────────────────────────┘
```

### User Flows

#### Flow 1: First-Time Setup

```
User opens dashboard (http://127.0.0.1:8111)
        ↓
Dashboard detects no auth configured
        ↓
Shows "Welcome! Let's set up your AI provider" modal
        ├─ Select provider (Claude Code / Anthropic API / Vertex AI)
        ↓ (Claude Code selected)
Shows "Step 1: Click to Login"
        ├─ Button: "Open Claude Code Login"
        ├─ Opens browser → https://claude.com/cai/oauth/authorize
        ├─ User approves → redirects back to dashboard
        ├─ Dashboard captures auth token
        ↓
Shows "Step 2: Verify Login"
        ├─ Runs `claude auth status`
        ├─ Shows: Logged in as you@example.com, subscription: Max
        ↓
Shows "Setup Complete!"
        ├─ Stores credentials in OS keychain
        ├─ Starts SOVA server
        ├─ Redirects to dashboard home
```

#### Flow 2: Switch Provider (In Settings)

```
User clicks Settings → Authentication
        ↓
Shows current provider status
        ├─ Provider: Claude Code CLI
        ├─ Email: you@example.com
        ├─ Subscription: Max
        ├─ Available models: Sonnet 5, Haiku 4.5
        ↓
User clicks "Switch Provider"
        ├─ Clears current credentials
        ├─ Shows provider selection
        ↓ (Anthropic API selected)
Shows API key input
        ├─ Paste field: [sk-ant-________________]
        ├─ Button: "Validate Key"
        ↓
Dashboard tests key with small API call
        ├─ If valid: Key accepted
        ├─ If invalid: Key rejected, try again
        ├─ Shows subscription info: Usage-based, $X per month
        ↓
Stores in keyring, restarts agents
```

#### Flow 3: Re-Authentication Prompt

```
Agent spawns and hits API error: "Invalid authentication"
        ↓
Dashboard detects auth failure
        ↓
Shows alert: "Your AI provider needs re-authentication"
        ├─ Icon: warning
        ├─ Button: "Re-Authenticate"
        ↓ (User clicks)
Shows login flow again (same as Flow 1)
        ↓
After re-auth, agent automatically retries
```

### UI Components

#### 1. Auth Wizard Modal

**Location:** Displayed on dashboard startup if no auth configured

```
┌──────────────────────────────────────────────┐
│  Welcome to SOVA                        [x]  │
├──────────────────────────────────────────────┤
│                                              │
│  Let's set up your AI provider               │
│                                              │
│  Choose one:                                 │
│                                              │
│  ┌─────────────────────────────────────────┐ │
│  │ Claude Code CLI (Recommended)            │ │
│  │ Personal subscription • No API keys      │ │
│  │ Instant setup • Your Anthropic account   │ │
│  │ [Select]                                 │ │
│  └─────────────────────────────────────────┘ │
│                                              │
│  ┌─────────────────────────────────────────┐ │
│  │ Anthropic API                            │ │
│  │ Usage-based billing • API key required   │ │
│  │ Good for production • Full control       │ │
│  │ [Select]                                 │ │
│  └─────────────────────────────────────────┘ │
│                                              │
│  ┌─────────────────────────────────────────┐ │
│  │ Google Vertex AI                         │ │
│  │ GCP billing • Service account needed     │ │
│  │ Enterprise • Multi-cloud ready           │ │
│  │ [Select]                                 │ │
│  └─────────────────────────────────────────┘ │
│                                              │
│                              [Skip for now]  │
└──────────────────────────────────────────────┘
```

#### 2. Claude Code Login Step

```
┌──────────────────────────────────────────────┐
│  Claude Code Login                      [x]  │
├──────────────────────────────────────────────┤
│                                              │
│  Step 1 of 2                                 │
│                                              │
│  Ready to log in with your Anthropic        │
│  account?                                    │
│                                              │
│  Your subscription tier will be detected     │
│  automatically (Max, Pro, Standard, Free).   │
│                                              │
│  [Open Claude Code Login]                    │
│                                              │
│  Note: a browser window will open. Approve   │
│  Claude Code access and come back here.      │
│                                              │
│  Still waiting... [Cancel]                  │
│                                              │
└──────────────────────────────────────────────┘
```

#### 3. Anthropic API Key Step

```
┌──────────────────────────────────────────────┐
│  Anthropic API Setup                    [x]  │
├──────────────────────────────────────────────┤
│                                              │
│  Paste your API key from                     │
│  https://console.anthropic.com/api-keys      │
│                                              │
│  Key: [sk-ant-________________]              │
│        [hidden]                              │
│                                              │
│  Note: keys starting with sk-ant- are valid  │
│  Key format looks correct                    │
│                                              │
│  [Validate & Continue]  [Back]               │
│                                              │
│  Your key is never logged or transmitted     │
│  to SOVA servers. Stored locally in your OS  │
│  keychain.                                   │
│                                              │
└──────────────────────────────────────────────┘
```

#### 4. Settings → Authentication Page

```
┌────────────────────────────────────────────────────────────┐
│  Settings                                                  │
├────────────────────────────────────────────────────────────┤
│                                                            │
│  Authentication & Providers                               │
│                                                            │
│  Current Provider                                          │
│  ┌──────────────────────────────────────────────────────┐ │
│  │ Claude Code CLI                             [OK]  │ │
│  │                                                      │ │
│  │ Email:           damian.sova@gmail.com              │ │
│  │ Subscription:    Max                                │ │
│  │ Status:          Authenticated                      │ │
│  │ Last checked:    2 minutes ago                       │ │
│  │ Available models: Sonnet 5, Haiku 4.5               │ │
│  │                                                      │ │
│  │ [View Details] [Re-Authenticate] [Switch Provider]  │ │
│  └──────────────────────────────────────────────────────┘ │
│                                                            │
│  Environment Health Check                                  │
│  ┌──────────────────────────────────────────────────────┐ │
│  │ Warning: Vertex AI environment variables detected   │ │
│  │                                                      │ │
│  │ The following may interfere with your chosen        │ │
│  │ provider:                                            │ │
│  │ • ANTHROPIC_VERTEX_PROJECT_ID                        │ │
│  │ • CLAUDE_CODE_USE_VERTEX=1                           │ │
│  │ • GCP_PROJECT_ID                                     │ │
│  │                                                      │ │
│  │ These are likely from an old Vertex AI setup.       │ │
│  │ Remove them to avoid conflicts.                      │ │
│  │                                                      │ │
│  │ [Auto-Clean] [Learn More]                           │ │
│  └──────────────────────────────────────────────────────┘ │
│                                                            │
│  Saved Providers (Multiple Account Support - Future)       │
│  ┌──────────────────────────────────────────────────────┐ │
│  │ • personal@gmail.com (Claude Code CLI) [Active]     │ │
│  │ • org-api-key (Anthropic API) [Inactive]            │ │
│  │ • GCP Project (Vertex AI) [Inactive]                │ │
│  │                                                      │ │
│  │ [Add Another Account] [Manage]                      │ │
│  └──────────────────────────────────────────────────────┘ │
│                                                            │
└────────────────────────────────────────────────────────────┘
```

#### 5. Status Sidebar Widget

```
Shown on all pages (top-right)

┌────────────────────────────────┐
│  AI Provider                   │
├────────────────────────────────┤
│  Claude Code CLI  [OK]         │
│  you@example.com                │
│  Max subscription               │
│                                │
│  Status:    Ready               │
│  Updated:   Just now            │
│                                │
│  [Settings] [Switch]           │
└────────────────────────────────┘
```

### Backend API: What Actually Shipped

The four endpoints originally proposed here (`/api/auth/setup`, `/api/auth/login`, `/api/auth/validate-key`, `/api/auth/switch`) were not built as a stateful setup wizard. What shipped is simpler and stateless: a read-only status endpoint plus a provider/runtime catalog with free validate/test/activate actions (`sova/dashboard/routers/auth.py`, mounted under `/api`).

#### `GET /api/auth/status`

Returns the current provider's auth status, cached per project directory for `_AUTH_STATUS_CACHE_TTL` seconds (60; `sova/dashboard/services/setup_service.py:get_auth_status()`). The response's `cached` field is a per-response boolean, not the TTL itself: it reports whether that particular reply was served from the cache or came from a fresh probe. The response shape, and the `account` versus `authenticated` contract (the two are not interchangeable, and neither alone proves a login state), are documented in the [AI Provider Setup Guide](ai-provider-setup-guide.md#connections-page-dashboard) rather than duplicated here.

`routing_warnings` lists any of `PROVIDER_ROUTING_VARS` (`sova/utils/env.py`) currently present in the server process's own environment, as a diagnostic only; it does not mean one reached (or will reach) a spawned agent, since those are scrubbed independently at the subprocess spawn boundary (see `.claude/rules/architecture.md`'s "Environment scrubbing at the spawn boundary..." entry).

#### Connections catalog and actions

The remaining endpoints back the Connections dashboard page (`sova/dashboard/templates/connections.html`), served at `GET /connections`: `GET /api/connections` (the provider and runtime catalog), `POST /api/connections/llm/validate`, `/test` and `/activate`, `POST /api/connections/runtime/validate` and `/activate`, and `POST /api/connections/reconnect/start`, `GET .../reconnect/status` and `POST .../reconnect/cancel`. Each is described in the [AI Provider Setup Guide](ai-provider-setup-guide.md#connections-page-dashboard).

None of these persist a setup "session" the way the original `/api/auth/setup` wizard proposal implied: each call is a single, idempotent probe or activation. Every `POST` among them is guarded by `require_same_origin_csrf` (issue #931), which is what replaced the proposed `/api/auth/logout` and credential-mutation access model.

There is also no connections endpoint for credentials. The page's "Save key" button posts to the ordinary settings write path, `POST /api/settings/config` with `{"key": "llm.api_key", "value": ..., "provider": ...}`, which routes keys in `RESOLVED_SECRET_KEYS` into the keyring; the proposed `/api/auth/validate-key` (test a key without storing it) was not built, since `POST /api/connections/llm/test` covers the same need against an already-saved key.

### Credential Storage: What Actually Shipped

No `credentials` or `auth_events` table was added. Secrets stay in the existing `project_settings` table, resolved through `sova/llm/keyring_store.py`:

- `RESOLVED_SECRET_KEYS` (today: `llm.api_key`) names which settings move into the OS keyring instead of the plaintext database value.
- `scoped_secret_name(name, project_dir, provider)` builds a keyring entry scoped to `(name, provider, project_dir)`, so two projects, or two providers configured within the same project over time, never collide in the one shared OS keychain namespace (`SERVICE_NAME = "sova"`).
- `resolve_secret()` reads the scoped keyring entry first, then an unscoped legacy keyring entry (read-only, never written), then the plaintext database value, unless that value is the sentinel string `"keyring:stored"` (meaning it was migrated and the keychain entry is the only remaining copy).
- `migrate_plaintext_to_keyring()` writes to the keyring and verifies the round-trip before the caller replaces the plaintext database value with the sentinel; a failed round-trip leaves the plaintext value in place rather than losing the secret.
- When no keyring backend is usable (`is_keyring_available()` is `False`: package not installed, or `keyring.get_keyring()` returns the null `fail.Keyring` backend, e.g. headless Linux with no Secret Service), the keyring accessors (`get_secret`, `set_secret`, `delete_secret`, `migrate_plaintext_to_keyring`) degrade to returning `None`/`False`, and `resolve_secret()` falls through to the plaintext database value (or `""` when there is none). No exception escapes this module.
- `sova doctor` reports a `keyring: <key>` check failure when a secret's database value is the sentinel but no matching keychain entry can be read back (`sova/utils/diagnostics.py:_check_keyring_secrets()`), e.g. after copying the database to another machine without its keychain.

`RESOLVED_SECRET_KEYS` holds exactly one key, and that bounds what SOVA stores: there is no SOVA-held credential for `llm.provider = "claude-code"` at all. The `claude` CLI owns its own OAuth credential in the OS keychain and refreshes it itself, so the original plan's "persist credentials securely" step does not apply to the default provider. Reconnect spawns `claude auth login`, observes only its exit code, and never reads the token; the login state is re-derived from a fresh `claude auth status --json` probe instead. The same holds for `agent.runtime = "codex"`, whose credential is owned by the Codex CLI's own keyring storage.

There is no audit log of auth events (login/logout/reauth/failure) anywhere in the codebase today.

### Implementation Phases: What Actually Shipped

What shipped, by original phase (the issue list and the structural divergence from this plan are summarized under [Implementation Status](#implementation-status) above):

- **Phase 1 (core auth management)**: `GET /api/auth/status` and the `sova/llm/keyring_store.py` credential resolution described under [Backend API](#backend-api-what-actually-shipped) and [Credential Storage](#credential-storage-what-actually-shipped) above, not a new `AuthManager` service or database table.
- **Phase 2 (UI components)**: the Connections page (`sova/dashboard/templates/connections.html`), a single always-visible settings page rather than a first-run modal wizard. No OAuth redirect handling in the dashboard: reconnect starts a local `claude auth login` subprocess and polls its status instead.
- **Phase 3 (environment detection)**: superseded by automatic scrubbing at the subprocess spawn boundary (`sova/utils/env.py`), which runs on every spawn unless a variable is explicitly opted back in via `agent.env_passthrough`. What was not built is the proposed detection banner and the profile-rewriting "Auto-Clean" tool; read-only detection did ship, as `routing_warnings` in `GET /api/auth/status` (`setup_service.py`) and the `routing_backend` field on the `claude-code` row of `GET /api/connections` (`setup_service.py`).
- **Phase 4 (polish and testing)**: covered by the existing dashboard test suite (`tests/test_dashboard.py`) and `sova doctor`'s provider/keyring checks, not a dedicated E2E suite.

Not built, and not currently planned: an audit log of auth events, multiple saved accounts per provider, or team credential sharing. These appear under [Future Enhancements](#future-enhancements) (items 1, 2, and 6) if ever revisited; whether team accounts share one credential or each have a separate one is also an open question (see [Questions & Open Items](#questions--open-items) below).

### Testing Strategy

#### Unit Tests

```python
# tests/test_auth_manager.py
def test_store_and_retrieve_credentials():
    """Credentials stored in keyring should be retrievable"""

def test_provider_detection():
    """Correct provider should be detected from credentials"""

def test_keyring_fallback():
    """Should fall back to env vars if keyring unavailable"""

def test_api_key_validation():
    """Valid/invalid API keys should be detected"""
```

#### Integration Tests

```python
# tests/test_auth_endpoints.py
def test_auth_status_endpoint():
    """GET /api/auth/status should return current provider"""

def test_setup_flow_claude_code():
    """Full Claude Code login flow should work"""

def test_setup_flow_anthropic():
    """Full Anthropic API setup flow should work"""

def test_provider_switch():
    """Switching providers should clear old credentials"""
```

#### E2E Tests (Selenium)

```python
# tests/e2e/test_auth_wizard.py
def test_first_time_setup():
    """New user should complete setup via UI"""

def test_invalid_api_key_rejected():
    """Invalid keys should show error message"""

def test_reauth_prompt_on_failure():
    """Agent failure should trigger reauth prompt"""
```

### Security Considerations

1. **Credential Storage** (see [Credential Storage: What Actually Shipped](#credential-storage-what-actually-shipped) above)
   - OS keychain first, scoped per `(setting, provider, project_dir)`; plaintext database value only as a fallback, replaced with the `"keyring:stored"` sentinel once migrated. No per-user salt or database-level encryption: the database never holds the live secret once migration succeeds.
   - Never log credentials (even redacted)

2. **OAuth Flow**
   - Use state parameter to prevent CSRF
   - Validate redirect URI against whitelist
   - Scope OAuth tokens to minimum required permissions

3. **API Key Handling**
   - Test with minimal request (list models) not full invocation. Resolved differently from the original plan: `POST /api/connections/llm/test` deliberately runs one full, billable `invoke()` call instead, since it is explicitly the one action on the Connections page labeled as spending money (see [Credential Storage](#credential-storage-what-actually-shipped) above); it is never triggered implicitly by validate or activate.
   - Never display full API key in UI (mask after first 8 chars)
   - Clear on logout/provider switch

4. **Audit Trail**
   - Log all auth events (login, logout, reauth, failures). Not built: there is no audit log of auth events anywhere in the codebase today (see [Credential Storage](#credential-storage-what-actually-shipped) above).
   - Include timestamp, provider, and error (if failed)
   - Do NOT include credentials or secrets

---

## Metrics & Success Criteria

| Metric | Target | Measurement |
|--------|--------|-------------|
| Setup time | < 2 minutes | User time from dashboard open to first agent run |
| Auth errors | 0 terminal-based | No more "model not found" from env vars |
| Reauthentication | < 30 seconds | Time to click "Re-Authenticate" and resume |
| User satisfaction | 4.5/5 | Feedback survey post-setup |
| Support tickets | 50% reduction | Auth-related issues vs. v1 |

---

## Future Enhancements

1. **Multiple Account Support**: save and switch between different personal accounts
2. **Team Credentials**: share API keys securely across team members
3. **Budget Monitoring**: track spend per provider and alert on limits
4. **Model Selection UI**: choose which models to use per task (Haiku for triage, Sonnet for dev)
5. **Provider Failover**: automatically fall back to secondary provider on quota limit
6. **Audit Dashboard**: view all auth events, failed logins, provider switches

---

## Rollout Plan

1. **Internal Beta** (1 week): Damian tests with all 3 providers
2. **Early Access** (1 week): Share with 5 collaborators for feedback
3. **Production Release**: Deploy to main branch with feature flag
4. **Documentation**: Update onboarding and troubleshooting guides
5. **Migration Guide**: Help existing users migrate from env-based setup

---

## Questions & Open Items

- [ ] Should we support OAuth for Anthropic API (currently key-based only)?
- [ ] Do we need password-protect the keyring on headless servers?
- [ ] Should we auto-detect provider from existing config files?
- [ ] How to handle service account keys for Vertex AI (upload vs. ADC)?
- [ ] Should team accounts share one credential or each have separate?
