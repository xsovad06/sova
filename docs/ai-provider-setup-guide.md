# AI Provider Setup Guide

This guide explains how to configure SOVA to use different AI providers (Claude Code CLI, Anthropic API, Vertex AI) and how to handle authentication.

## Table of Contents

1. [Supported Providers](#supported-providers)
2. [Personal Subscription (Claude Code CLI)](#personal-subscription-claude-code-cli)
3. [Anthropic API Direct](#anthropic-api-direct)
4. [Google Vertex AI](#google-vertex-ai)
5. [Troubleshooting](#troubleshooting)
6. [Future: UI-Based Authentication Wizard](#future-ui-based-authentication-wizard)

---

## Supported Providers

SOVA supports three LLM provider backends:

| Provider | Auth Method | Cost | Use Case | Config |
|----------|-------------|------|----------|--------|
| **Claude Code CLI** | Personal subscription login | Per-message billing | Recommended for individuals | `llm.provider = "claude-code"` |
| **Anthropic API** | API key (`ANTHROPIC_API_KEY`) | Batch/streaming | Production deployments | `llm.provider = "anthropic"` |
| **Google Vertex AI** | GCP service account (ADC) | GCP billing | Enterprise multi-cloud | `llm.provider = "vertex"` + env vars |

---

## Personal Subscription (Claude Code CLI)

### What You Need

- Claude Code CLI installed: `claude --version` (v2.1.0+)
- Active Anthropic subscription (free or paid)
- No API keys or service accounts required

### Setup Steps

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

**Option A: Database Configuration (Recommended)**

SOVA stores config in `.claude/sova.db`:

```bash
sqlite3 .claude/sova.db << 'EOF'
UPDATE project_settings SET value = '"claude-code"' WHERE key = 'llm.provider';
UPDATE project_settings SET value = '""' WHERE key = 'llm.model'; -- Let Claude Code choose
EOF
```

**Option B: Environment Variables (Temporary)**

```bash
export SOVA_LLM_PROVIDER="claude-code"
export SOVA_LLM_MODEL=""
```

#### 4. Clean Environment (Important!)

Remove any old Vertex AI or GCP environment variables that Claude Code might inherit:

**Edit `~/.zshrc` (or `~/.bashrc`):**

```bash
# REMOVE or COMMENT OUT these lines:
# export CLAUDE_CODE_USE_VERTEX=1
# export ANTHROPIC_VERTEX_PROJECT_ID="..."
# export GCP_PROJECT_ID="..."
# export GOOGLE_CLOUD_QUOTA_PROJECT="..."
```

Also edit `~/.claude/settings.json`:

```json
{
  "env": {
    "GH_TOKEN": ""
    // Remove ANTHROPIC_VERTEX_PROJECT_ID and CLAUDE_CODE_USE_VERTEX
  }
}
```

#### 5. Start SOVA Server

```bash
# Use the provided wrapper to ensure clean environment
sova-clean server start --multi

# Or manually unset vars, then start normally
unset ANTHROPIC_VERTEX_PROJECT_ID GCP_PROJECT_ID CLAUDE_CODE_USE_VERTEX GOOGLE_CLOUD_QUOTA_PROJECT
sova server start --multi
```

#### 6. Verify in Dashboard

Open http://127.0.0.1:8111, go to **Settings** → **LLM Provider**, and confirm it shows:
- Provider: Claude Code CLI
- Auth Status: Authenticated (you@example.com)
- Available Models: Shows your subscription tier

---

## Anthropic API Direct

### What You Need

- Anthropic API key from https://console.anthropic.com/api-keys
- API key stored securely (env var or secrets file)

### Setup Steps

#### 1. Create API Key

1. Go to https://console.anthropic.com/api-keys
2. Click "Create Key"
3. Copy the key (starts with `sk-ant-`)
4. **Store securely** (never commit to git)

#### 2. Configure SOVA

```bash
sqlite3 .claude/sova.db << 'EOF'
UPDATE project_settings SET value = '"anthropic"' WHERE key = 'llm.provider';
UPDATE project_settings SET value = '"claude-sonnet-5"' WHERE key = 'llm.model';
EOF
```

#### 3. Set API Key

```bash
# Option A: Environment variable (temporary, this session only)
export ANTHROPIC_API_KEY="sk-ant-..."

# Option B: Claude Code settings (persistent)
# Edit ~/.claude/settings.json:
# {
#   "env": {
#     "ANTHROPIC_API_KEY": "sk-ant-..."
#   }
# }

# Option C: .env file in project root (git-ignored)
echo "ANTHROPIC_API_KEY=sk-ant-..." >> .env
```

#### 4. Start SOVA

```bash
sova server start --multi
```

---

## Google Vertex AI

### What You Need

- Google Cloud project with Vertex AI enabled
- Service account with Vertex AI permissions
- Application Default Credentials (ADC) configured
- GCS bucket for batch processing (optional)

### Setup Steps

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

```bash
sqlite3 .claude/sova.db << 'EOF'
UPDATE project_settings SET value = '"vertex"' WHERE key = 'llm.provider';
UPDATE project_settings SET value = '"claude-sonnet-5"' WHERE key = 'llm.model';
UPDATE project_settings SET value = '"YOUR_PROJECT_ID"' WHERE key = 'llm.routing';
EOF
```

#### 4. Set Environment Variables

```bash
export GOOGLE_APPLICATION_CREDENTIALS="$HOME/sova-key.json"
export ANTHROPIC_VERTEX_PROJECT_ID="YOUR_PROJECT_ID"
export GOOGLE_CLOUD_QUOTA_PROJECT="YOUR_PROJECT_ID"
```

#### 5. Start SOVA

```bash
sova server start --multi
```

---

## Troubleshooting

### Claude Code Shows "Vertex API"

**Problem:** `claude auth status` shows `"apiProvider": "vertex"` even though you want personal subscription.

**Solution:**

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

5. Verify:
   ```bash
   claude auth status | jq .apiProvider
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

Then restart SOVA:

```bash
sova-clean server stop
sova-clean server start --multi
```

### API Key Rejected

**Problem:** `ANTHROPIC_API_KEY` is invalid or missing.

**Solution:**

1. Verify the key is correct (starts with `sk-ant-`):
   ```bash
   echo $ANTHROPIC_API_KEY | head -c 10
   ```

2. Test the key directly:
   ```bash
   claude -p "Test message" --api-key "$ANTHROPIC_API_KEY"
   ```

3. Check it's not been revoked in https://console.anthropic.com/api-keys

---

## Future: UI-Based Authentication Wizard

**Current workflow is manual and terminal-based.** We're planning a dashboard UI that will:

1. **Detect missing authentication** on startup
2. **Show an auth wizard** with provider selection
3. **Handle OAuth flows** (Claude Code login, Anthropic API key input)
4. **Persist credentials securely** using the OS keychain/secrets manager
5. **Prompt for re-authentication** when tokens expire
6. **Display provider status** in Settings → Auth

### Proposed Features (Issues to Create)

See [Authentication UI Epic](#authentication-ui-epic) below.

---

## Authentication UI Epic

### Issue Template: Auth Wizard Dashboard Component

```markdown
## Title: Authentication Wizard UI

### Description
Add a UI-based authentication setup wizard to the SOVA dashboard that guides users through provider selection and credential management.

### Acceptance Criteria
- [ ] Dashboard detects missing/invalid auth on startup
- [ ] Shows provider selection modal (Claude Code CLI, Anthropic API, Vertex AI)
- [ ] Claude Code CLI flow: "Click here to log in" → opens browser auth
- [ ] Anthropic API flow: Text input for API key with validation
- [ ] Vertex AI flow: GCP project ID + service account setup guide
- [ ] Credentials stored securely (OS keychain on macOS/Linux, Credential Manager on Windows)
- [ ] Settings page shows current provider and auth status
- [ ] Alerts when tokens are expiring or invalid
- [ ] "Switch Provider" button for changing auth method

### Implementation Notes
- Use FastAPI endpoint to check auth status at startup
- Store sensitive data in OS keychain via `python-keyring` library
- Decrypt from keychain when spawning agents
- No secrets stored in `sova.db` or `.env` files
- Handle provider-specific flows:
  - Claude Code: subprocess `claude auth status`, detect browser prompt
  - Anthropic: simple API key validation via test request
  - Vertex AI: GCP CLI integration or manual key upload

### Subtasks
1. Create `sova/llm/auth_manager.py` for credential storage/retrieval
2. Create FastAPI endpoint `POST /api/auth/setup`
3. Create dashboard component `AuthWizard.vue` or similar
4. Integrate keyring storage in `ClaudeCodeProvider`, `AnthropicProvider`
5. Add Settings page section for auth management
6. Add startup health check that redirects to auth if needed
```

### Issue Template: Environment Variable Cleanup

```markdown
## Title: Automatically Clean Inherited Environment Variables

### Description
Claude Code can inherit Vertex AI env vars from the parent shell or previous configurations, causing authentication to fail silently. Implement automatic cleanup and detection.

### Acceptance Criteria
- [ ] SOVA detects Vertex AI env vars at startup and warns user
- [ ] Provides one-click button to remove env vars from shell config
- [ ] Offers to update `~/.zshrc`, `~/.bashrc`, `~/.claude/settings.json` automatically
- [ ] Suggests `sova-clean` wrapper for starting server with clean env
- [ ] Documents cleanup steps in dashboard Settings

### Implementation Notes
- Parse `~/.zshrc` for Vertex-related exports and comment them out
- Parse `~/.claude/settings.json` and remove Vertex config
- Show diff before applying changes
- Add "Auto-Clean" button in Settings → Troubleshooting

### Subtasks
1. Create detection logic in `sova/llm/provider.py` that checks for Vertex vars
2. Create `sova/utils/env_cleanup.py` for file modification
3. Add Settings page warning banner when Vertex vars detected
4. Document cleanup guide in docs/
```

### Issue Template: Provider Status Monitor

```markdown
## Title: Real-Time Provider Health Check and Status Display

### Description
Monitor AI provider health, quota, and authentication status. Display in dashboard sidebar or status panel.

### Acceptance Criteria
- [ ] Dashboard displays current provider (Claude Code / Anthropic / Vertex)
- [ ] Shows authentication status (logged in, expiring, failed)
- [ ] Displays subscription tier (Max/Pro/Standard for personal, usage for API)
- [ ] Shows available models for the provider
- [ ] Alerts when API quota is low or rate-limited
- [ ] Provides "Re-Authenticate" button when needed
- [ ] Logs provider errors to UI (not just server logs)

### Implementation Notes
- Periodically call `claude auth status` for CLI provider
- Call Anthropic API `/models` endpoint for API provider
- Track rate limit headers from API responses
- Store status in dashboard state with 5-minute TTL

### Subtasks
1. Create `sova/dashboard/services/auth_status_service.py`
2. Add FastAPI endpoint `GET /api/auth/status`
3. Create dashboard component `ProviderStatusPanel.vue`
4. Integrate with Settings page
5. Add WebSocket feed for real-time alerts
```

### Issue Template: Secure Credential Storage

```markdown
## Title: Implement Secure Credential Storage with Keychain Integration

### Description
Move from environment variables to OS-level secure storage (Keychain on macOS, Credential Manager on Windows, pass/Secret Service on Linux).

### Acceptance Criteria
- [ ] API keys stored in OS keychain, not in environment
- [ ] Keychain entries encrypted and isolated per user
- [ ] Dashboard can encrypt/decrypt without prompting user
- [ ] Graceful fallback to env vars if keyring unavailable
- [ ] Credentials cleared on logout/provider switch
- [ ] No secrets in `.env` files or database

### Implementation Notes
- Use `python-keyring` library for cross-platform support
- Store as: `sova_provider_auth` → JSON with `{provider, key/token, expires_at}`
- On agent spawn, retrieve from keyring and pass to subprocess
- Disable keyring in tests (use env vars instead)

### Subtasks
1. Add `keyring` to `pyproject.toml` dependencies
2. Create `sova/llm/keyring_store.py` for abstraction
3. Update all providers to use keyring store
4. Migrate existing env var configs to keyring on first startup
5. Update test fixtures to mock keyring
```

---

## Summary

| Step | Current (Manual) | Future (UI) |
|------|------------------|-------------|
| 1. Install Claude Code | Terminal | Terminal (pre-req) |
| 2. Authenticate | `claude auth login` in terminal | "Click here" button in dashboard |
| 3. Configure provider | Edit `sova.db` with SQL | Dropdown selection in Settings |
| 4. Clean env vars | Edit `~/.zshrc` manually | One-click "Auto-Clean" button |
| 5. Start server | `sova-clean server start` | Start via dashboard button |
| 6. Monitor auth | Check logs | Real-time status panel |

The UI-based workflow will reduce setup time from 15+ minutes to < 2 minutes and eliminate environment variable issues entirely.
