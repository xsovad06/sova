# Authentication UI Design Document

## Overview

This document outlines the architecture for a dashboard-based authentication system that will replace manual terminal-based provider setup. The goal is to reduce setup friction and make SOVA accessible to users unfamiliar with environment variables and CLI tooling.

## Current Pain Points

1. **Multiple configuration methods**: SQLite, env vars, `.claude/settings.json`, shell profiles
2. **Silent failures**: inherited Vertex AI env vars cause cryptic "model not found" errors
3. **No visibility**: users can't see auth status or know when re-authentication is needed
4. **Terminal-heavy**: OAuth flow (Claude Code login) requires manual browser interaction outside the UI
5. **No validation**: API keys accepted without testing; errors only appear in server logs

## Proposed Solution

### Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    SOVA Dashboard (Frontend)                │
├─────────────────────────────────────────────────────────────┤
│  ┌──────────────────┐  ┌──────────────────────┐             │
│  │ Auth Wizard Modal│  │ Settings → Auth Page │             │
│  │ (Setup Flow)     │  │ (Status + Management)│             │
│  └────────┬─────────┘  └──────────┬───────────┘             │
│           │                       │                          │
│           └───────────┬───────────┘                          │
│                       │                                       │
├───────────────────────┼───────────────────────────────────────┤
│                       ▼                                        │
│           FastAPI Backend (sova/dashboard)                    │
├───────────────────────┬───────────────────────────────────────┤
│                       │                                        │
│   Auth Management API (routers/auth.py)                       │
│   ├── POST /api/auth/setup          (wizard initiation)       │
│   ├── POST /api/auth/login          (OAuth redirects)         │
│   ├── POST /api/auth/validate-key   (API key test)            │
│   ├── GET  /api/auth/status         (current provider)        │
│   ├── POST /api/auth/switch         (change provider)         │
│   └── POST /api/auth/logout         (clear credentials)       │
│                                                                │
│   Auth Service (services/auth_manager.py)                     │
│   ├── Credential storage (keyring)                            │
│   ├── Provider detection                                      │
│   ├── Status polling                                          │
│   └── Re-auth prompts                                         │
│                                                                │
│   LLM Provider Layer (llm/provider.py)                        │
│   ├── ClaudeCodeProvider (OAuth via subprocess)               │
│   ├── AnthropicProvider (API key)                             │
│   └── VertexProvider (GCP ADC)                                │
│                                                                │
│   Keyring Store (llm/keyring_store.py)                        │
│   └── OS-level secure storage (Keychain/Credential Manager)   │
│                                                                │
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

### Backend API Endpoints

#### `GET /api/auth/status`

Returns current authentication status.

```bash
curl http://127.0.0.1:8111/api/auth/status
```

Response:
```json
{
  "authenticated": true,
  "provider": "claude-code",
  "email": "damian.sova@gmail.com",
  "subscription_type": "max",
  "models": ["claude-sonnet-5", "claude-haiku-4-5"],
  "last_checked": "2026-09-03T17:30:00Z",
  "expires_at": null,
  "needs_reauth": false,
  "warnings": []
}
```

#### `POST /api/auth/setup`

Initiates authentication wizard. Returns provider-specific next steps.

Request:
```json
{
  "provider": "claude-code"
}
```

Response:
```json
{
  "status": "pending",
  "provider": "claude-code",
  "step": 1,
  "steps_total": 2,
  "action": "open_browser",
  "url": "https://claude.com/cai/oauth/authorize?...",
  "instructions": "Please log in and approve Claude Code access"
}
```

#### `POST /api/auth/validate-key`

Tests an API key without storing it. Returns validation result.

Request:
```json
{
  "provider": "anthropic",
  "key": "sk-ant-..."
}
```

Response:
```json
{
  "valid": true,
  "provider": "anthropic",
  "subscription_info": {
    "model_access": ["claude-sonnet-5", "claude-opus-5"],
    "usage_based": true,
    "monthly_spend_estimate": "~$20-50"
  }
}
```

#### `POST /api/auth/switch`

Changes provider and credentials.

Request:
```json
{
  "provider": "anthropic",
  "credentials": {
    "api_key": "sk-ant-..."
  }
}
```

Response:
```json
{
  "status": "success",
  "message": "Switched to Anthropic API",
  "provider": "anthropic",
  "restart_required": true
}
```

### Database Schema

**New table: `credentials` (encrypted)**

```sql
CREATE TABLE credentials (
  id INTEGER PRIMARY KEY,
  provider TEXT NOT NULL, : "claude-code", "anthropic", "vertex"
  encrypted_data TEXT NOT NULL, : JSON encrypted with salt
  created_at TIMESTAMP DEFAULT NOW(),
  updated_at TIMESTAMP DEFAULT NOW(),
  expires_at TIMESTAMP,
  UNIQUE(provider)
);
```

**New table: `auth_events` (audit log)**

```sql
CREATE TABLE auth_events (
  id INTEGER PRIMARY KEY,
  provider TEXT NOT NULL,
  event_type TEXT NOT NULL, : "login", "logout", "reauth", "failure"
  status TEXT, : "success", "failed"
  error_message TEXT,
  ip_address TEXT, : for multi-user audit
  created_at TIMESTAMP DEFAULT NOW()
);
```

### Implementation Phases

#### Phase 1: Core Auth Management (Weeks 1-2)

- [ ] Create `AuthManager` service with keyring integration
- [ ] Implement FastAPI auth endpoints
- [ ] Add credential storage to database
- [ ] Create simple auth status component
- [ ] Write unit tests for auth flows

#### Phase 2: UI Components (Weeks 2-3)

- [ ] Build Auth Wizard modal
- [ ] Build Settings → Auth page
- [ ] Add status sidebar widget
- [ ] Implement form validation and error messages
- [ ] Add browser integration for OAuth redirect

#### Phase 3: Environment Detection (Week 4)

- [ ] Add Vertex AI env var detection
- [ ] Create "Auto-Clean" tool
- [ ] Write cleanup guide
- [ ] Test on macOS, Linux, Windows

#### Phase 4: Polish & Testing (Week 4-5)

- [ ] E2E testing (all 3 providers)
- [ ] Error scenario testing (invalid keys, network failures)
- [ ] Documentation
- [ ] User acceptance testing

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

1. **Credential Storage**
   - Use OS keychain (not plaintext in DB)
   - Encrypt database credentials field with per-user salt
   - Never log credentials (even redacted)

2. **OAuth Flow**
   - Use state parameter to prevent CSRF
   - Validate redirect URI against whitelist
   - Scope OAuth tokens to minimum required permissions

3. **API Key Handling**
   - Test with minimal request (list models) not full invocation
   - Never display full API key in UI (mask after first 8 chars)
   - Clear on logout/provider switch

4. **Audit Trail**
   - Log all auth events (login, logout, reauth, failures)
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
