# Awareness Subsystem Setup

The awareness subsystem aggregates information from Gmail, Google Calendar, GitHub PRs, Apple Reminders, and SOVA agent runs into a unified briefing. This guide covers initial setup and configuration.

## Quick Start

Set the following via `sova config set` or the dashboard settings page (shown here in TOML form):

```toml
[awareness]
enabled = true
providers = ["gmail", "gcal", "pr_status", "agent_runs"]
gmail_token_path = "/Users/<username>/.config/sova/google_token.pickle"
gmail_lookback_hours = 24
gcal_calendars = ["primary"]
gcal_lookahead_hours = 36
pr_github_user = "your-github-username"
```

Replace `/Users/<username>` with your actual home directory path, or use the default token location at `~/.config/sova/google_token.pickle`.

## Provider Overview

| Provider | Description | Requires |
|----------|-------------|----------|
| `gmail` | Unread emails, threads needing replies | Google OAuth2 token |
| `gcal` | Today's schedule, upcoming meetings | Google OAuth2 token (same as gmail) |
| `reminders` | Apple Reminders tasks | macOS with Reminders app |
| `pr_status` | Open PRs across all your projects | `gh` CLI auth |
| `agent_runs` | Recent SOVA agent activity | SOVA database |

## Google OAuth2 Setup (Gmail + Calendar)

The `gmail` and `gcal` providers share a single OAuth2 token.

### Option A: Use Existing Google Workspace MCP Token

If you already have the [Google Workspace MCP server](https://github.com/anthropics/team-productivity-utils/tree/main/google-workspace-mcp) installed, you can reuse its token:

```toml
[awareness]
gmail_token_path = "/Users/<username>/Code/team-productivity-utils/google-workspace-mcp/.config/google_token.pickle"
```

### Option B: Create SOVA-Managed Token

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project (or select an existing one)
3. Enable APIs:
   - [Gmail API](https://console.cloud.google.com/apis/library/gmail.googleapis.com)
   - [Google Calendar API](https://console.cloud.google.com/apis/library/calendar-json.googleapis.com)
4. Create OAuth Desktop credentials:
   - APIs & Services > Credentials > Create Credentials > OAuth client ID
   - Application type: Desktop app
   - Name: "SOVA Awareness"
   - Download JSON
5. Save the credentials file:
   ```bash
   mkdir -p ~/.config/sova
   mv ~/Downloads/client_secret_*.json ~/.config/sova/google_credentials.json
   ```
6. Run the OAuth flow:
   ```bash
   sova briefing
   ```
   This will:
   - Print an authorization URL
   - Open your browser (or you can copy the URL manually)
   - Ask you to authorize the app
   - Save the token to `~/.config/sova/google_token.pickle`

The token is persisted and auto-refreshed. You only need to authorize once.

## Configuration Reference

### Gmail Provider

```toml
[awareness]
gmail_token_path = "/Users/<username>/.config/sova/google_token.pickle"
gmail_lookback_hours = 24                           # How far back to scan for emails
gmail_ignore_labels = ["SPAM", "TRASH"]             # Labels to skip
```

**What it surfaces:**
- Unread emails (urgency based on age and sender)
- Threads where you're mentioned but haven't replied
- Emails with calendar invites

### Google Calendar Provider

```toml
[awareness]
gcal_calendars = ["primary"]                        # Which calendars to include
gcal_lookahead_hours = 36                           # How far ahead to scan
```

**What it surfaces:**
- Today's schedule (with timestamps)
- Upcoming meetings in the next 36 hours
- Meetings with no agenda (flagged as needs-attention)
- All-day events

### PR Status Provider

```toml
[awareness]
pr_github_user = "your-github-username"
```

**What it surfaces:**
- Open PRs authored by you
- PRs awaiting your review
- Recently merged PRs
- PRs with CI failures or pending reviews

**Requirements:** `gh` CLI must be authenticated (`gh auth status`).

### Agent Runs Provider

No configuration needed. Automatically surfaces:
- Failed agent runs
- Interrupted runs (recovered after dashboard restart)
- Recently completed runs
- Runs with warnings or errors

### Apple Reminders Provider

```toml
[awareness]
reminders_lists = ["Reminders"]                     # Which lists to scan
```

**What it surfaces:**
- Incomplete tasks
- Overdue reminders
- Tasks due today or tomorrow

**Requirements:** macOS with Reminders app. Uses AppleScript (`osascript`).

## Usage

### CLI

```bash
# Full briefing
sova briefing

# Only show items needing attention
sova briefing --quiet

# JSON output for scripting
sova briefing --json
```

### Dashboard

The briefing appears in the activity feed (owl icon in bottom-right corner):
1. Open `http://localhost:8111`
2. Click the floating owl button
3. Scroll to the top for the briefing summary

### Cron (Planned)

```bash
# Get a briefing every morning at 8 AM
sova cron add "morning-briefing" --schedule "0 8 * * *" --command "briefing"
```

## Troubleshooting

### "No awareness providers configured"

Run `sova config set awareness.enabled true` or use the dashboard settings page.

### "gmail.auth_failed"

**Cause:** Google OAuth2 token is missing, expired, or invalid.

**Fix:**
1. Delete the old token: `rm ~/.config/sova/google_token.pickle`
2. Run `sova briefing` again to trigger the OAuth flow
3. Authorize the app in your browser

### "pr_status: no oauth token found for github.com account X"

**Cause:** `gh` CLI is not authenticated, or the wrong account is active.

**Fix:**
```bash
# Check current auth status
gh auth status

# Switch to the correct account
gh auth switch --user your-github-username

# Or log in
gh auth login
```

### Timezone issues in CLI output

If timestamps show incorrect relative times (e.g., "2h ago" when it should be "just now"), your system timezone may not match the calendar's timezone.

**Fix:** The CLI renderer auto-detects timezone-aware timestamps. If you still see issues, file a bug with your system timezone (`date +%Z`) and calendar timezone.

## Privacy & Security

- **Google OAuth2 token:** Stored at `~/.config/sova/google_token.pickle` (or custom path). This is a local-only file with read permissions for your user only.
- **Scopes:** SOVA requests `gmail.readonly` and `calendar.readonly` — no write access.
- **Data retention:** Briefing data is fetched on-demand and not persisted. The dashboard caches the last briefing for 5 minutes.
- **Multi-user:** Each user runs their own SOVA instance with their own OAuth token.

## Advanced Configuration

### Disable Specific Providers

```toml
[awareness]
enabled = true
providers = ["pr_status", "agent_runs"]  # No gmail or gcal
```

### Custom Email Filters

```toml
[awareness]
gmail_ignore_labels = ["SPAM", "TRASH", "CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL"]
```

### Cross-Project PR Aggregation

The `pr_status` provider scans all registered SOVA projects (not just the current one). To register a project:

```bash
sova install /path/to/project
```

Projects are tracked in `~/.config/sova/projects.json`. The briefing includes PRs from all registered projects.

### Per-Project Opt-Out

To exclude a project from PR/agent-run aggregation:

```toml
# In that project's sova.toml
[awareness]
enabled = false
```

This disables awareness for that project only. Other projects still contribute to the briefing.

## Related Documentation

- [Awareness Implementation Spec](specs/awareness-implementation-spec.md) — Architecture and design
- [Awareness Use Cases](specs/awareness-use-cases.md) — User stories and workflows
- [JARVIS Vision (Google Doc)](https://docs.google.com/document/d/1vX0GRPioJzgmhaGfmMD5yoydTuGC9zWvGpoDrwE9KJg/edit) — Future conversational interface
