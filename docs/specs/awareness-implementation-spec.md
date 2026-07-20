# Awareness Subsystem — Implementation Specification

**Status**: Ready for development
**Date**: 2026-07-20
**Depends on**: [Use Case Specification](awareness-use-cases.md)
**Related**: [JARVIS Vision](../../morning-agent/JARVIS.md), [Hackathon Plan](https://docs.google.com/document/d/1vX0GRPioJzgmhaGfmMD5yoydTuGC9zWvGpoDrwE9KJg/edit)

## 1. Problem Summary

SOVA is currently blind to everything outside the development pipeline. It knows about code, PRs, CI, and JIRA tickets — but it has no idea what's in your email, what's on your calendar, or which communication threads need your attention. A developer's morning context assembly takes 20-30 minutes of manual tool-hopping. This subsystem eliminates that overhead.

**Goal**: When a developer runs `sova briefing` or opens the dashboard's Briefing page, they see a unified, prioritized view of everything that needs their attention — across email, calendar, PRs, JIRA, agent activity, and reminders — without opening any other tool.

## 2. Architecture Overview

```
┌──────────────────────────────────────────────────────────────┐
│                        SOVA                                  │
│                                                              │
│  Existing                          New (Awareness)           │
│  ┌──────────────┐                  ┌──────────────────────┐  │
│  │ TaskAdapter   │                  │ AwarenessProvider    │  │
│  │  (ABC)        │                  │  (ABC)               │  │
│  │              │                  │                      │  │
│  │ ├─ GitHub    │                  │ ├─ GmailProvider     │  │
│  │ └─ Jira      │                  │ ├─ CalendarProvider  │  │
│  │              │                  │ ├─ RemindersProvider │  │
│  └──────┬───────┘                  │ ├─ PRStatusProvider  │  │
│         │                          │ └─ AgentRunProvider  │  │
│         │                          └──────────┬───────────┘  │
│         │                                     │              │
│         │         ┌───────────────────────┐    │              │
│         └────────>│   BriefingService     │<───┘              │
│                   │                       │                   │
│                   │  - fetch all providers │                   │
│                   │  - categorize items    │                   │
│                   │  - prioritize          │                   │
│                   │  - render              │                   │
│                   └───────────┬───────────┘                   │
│                               │                               │
│                   ┌───────────┼───────────┐                   │
│                   │           │           │                   │
│              ┌────▼───┐  ┌───▼────┐  ┌───▼─────┐             │
│              │  CLI   │  │ Dash   │  │ Cron    │             │
│              │briefing│  │/brief  │  │(future) │             │
│              └────────┘  └────────┘  └─────────┘             │
└──────────────────────────────────────────────────────────────┘
```

Key design decision: **AwarenessProvider is a new ABC, separate from TaskAdapter.** TaskAdapter manages issue lifecycles (create, transition, assign, comment). AwarenessProvider is read-only — it fetches items from a source, categorizes them, and reports what needs attention. The two ABCs share no methods. They intersect at BriefingService, which consumes both.

## 3. New Package Structure

```
sova/awareness/                          # New package
  __init__.py                            # Provider registry, create_providers()
  base.py                               # AwarenessProvider ABC, AwarenessItem, ItemCategory
  briefing.py                            # BriefingService (aggregation + prioritization)
  providers/
    __init__.py
    gmail.py                             # GmailProvider (Google Gmail API)
    gcal.py                              # CalendarProvider (Google Calendar API)
    reminders.py                         # RemindersProvider (Apple Reminders via osascript)
    pr_status.py                         # PRStatusProvider (reuses existing gh CLI + pr_service)
    agent_runs.py                        # AgentRunProvider (reads SOVA DB)
  auth/
    __init__.py
    google_oauth.py                      # OAuth2 flow for Gmail + Calendar (shared token)
  rendering/
    __init__.py
    cli_renderer.py                      # Rich terminal output for `sova briefing`
    models.py                            # Briefing, BriefingSection, rendered data structures
```

## 4. Core Abstractions

### 4.1 AwarenessItem

```python
@dataclass
class AwarenessItem:
    """A single piece of awareness data from any provider."""

    id: str                              # unique across providers (e.g., "gmail:19f6f645")
    provider: str                        # provider name: "gmail", "gcal", "reminders", etc.
    category: ItemCategory               # needs_attention | informational | dismissed
    title: str                           # human-readable summary (email subject, PR title, etc.)
    body: str = ""                       # detail text (email snippet, PR description, etc.)
    source_url: str = ""                 # link to the item in its native tool
    timestamp: datetime | None = None    # when the item was created/received
    metadata: dict = field(default_factory=dict)  # provider-specific data
    urgency: int = 0                     # 0=normal, 1=high, 2=critical
    action_hint: str = ""               # what the user should do ("review PR", "reply to email")
```

### 4.2 ItemCategory

```python
class ItemCategory(StrEnum):
    NEEDS_ATTENTION = "needs_attention"   # requires developer action
    INFORMATIONAL = "informational"      # good to know, no action needed
    DISMISSED = "dismissed"              # user dismissed or source resolved
```

### 4.3 AwarenessProvider ABC

```python
class AwarenessProvider(ABC):
    """Base class for awareness data sources."""

    name: str = ""                       # provider identifier ("gmail", "gcal", etc.)
    display_name: str = ""               # human-readable ("Gmail", "Google Calendar")

    @abstractmethod
    async def fetch_items(
        self,
        since: datetime | None = None,   # None = provider's default lookback
    ) -> list[AwarenessItem]:
        """Fetch awareness items from this source.

        Args:
            since: Only return items created/changed after this time.
                   If None, use provider's default (e.g., last 24h for email,
                   today+tomorrow for calendar).

        Returns:
            List of awareness items, categorized by the provider.
        """

    @abstractmethod
    async def is_configured(self) -> bool:
        """Check if this provider has valid credentials/config."""

    async def health_check(self) -> tuple[bool, str]:
        """Check if the provider can connect. Returns (ok, message)."""
        try:
            configured = await self.is_configured()
            if not configured:
                return False, f"{self.display_name}: not configured"
            return True, f"{self.display_name}: ok"
        except Exception as e:
            return False, f"{self.display_name}: {e}"
```

## 5. Provider Implementations

### 5.1 GmailProvider

**Source**: Google Gmail API via `google-api-python-client`
**Auth**: OAuth2 (shared with CalendarProvider)
**Default lookback**: 24 hours
**Reusable code**: `~/Code/team-productivity-utils/google-workspace-mcp/services/gmail.py` (280 lines, tested and working as of 2026-07-17)

**Categorization logic**:
- `NEEDS_ATTENTION`: emails where user is in To/CC, from a human (not automated), unread
- `NEEDS_ATTENTION` (high urgency): emails with user's name mentioned in body, from manager, or replies to user's own emails
- `INFORMATIONAL`: automated notifications (GitHub, JIRA, CI), mailing list digests, already-read emails

**What to port from the MCP service**:
The Gmail MCP service at `google-workspace-mcp/services/gmail.py` already implements:
- `list_messages(query, max_results, label)` — Gmail search with metadata-format results
- `get_message(message_id)` — full body extraction with MIME tree walking
- `get_thread(thread_id)` — full conversation threads
- `search_messages(query, max_results)` — cross-label search
- `get_unread_count()` — inbox stats
- `_extract_body(payload)` — handles text/plain, text/html with fallback HTML stripping
- `_extract_headers(msg)` — common header extraction
- `_parse_message_metadata(msg)` — summary dict builder

The OAuth token at `google-workspace-mcp/.config/google_token.pickle` already includes `gmail.readonly` scope (granted 2026-07-17). The GmailProvider should either share this token or have SOVA manage its own OAuth flow.

**Key design choice**: The GmailProvider should NOT depend on the MCP server. Copy and adapt the Gmail API client code into `sova/awareness/providers/gmail.py`. This keeps SOVA self-contained — colleagues can install it without setting up the MCP server.

### 5.2 CalendarProvider

**Source**: Google Calendar API via `google-api-python-client`
**Auth**: OAuth2 (shared token with GmailProvider)
**Default lookback**: today + next 24 hours
**Scope**: `https://www.googleapis.com/auth/calendar.readonly`

**Categorization logic**:
- `NEEDS_ATTENTION`: meetings starting within 30 minutes, meetings with no agenda/prep
- `INFORMATIONAL`: meetings later today, all-day events, tomorrow's schedule

**Item metadata** should include:
- `attendees`: list of attendee names/emails
- `location`: meeting room or video link
- `calendar_link`: direct link to event
- `is_organizer`: whether the user organized this meeting
- `response_status`: accepted/tentative/needs-action

### 5.3 RemindersProvider

**Source**: Apple Reminders via osascript (JXA)
**Auth**: None (macOS system access)
**Default lookback**: due today + overdue

**Categorization logic**:
- `NEEDS_ATTENTION` (critical urgency): overdue reminders
- `NEEDS_ATTENTION`: due today
- `INFORMATIONAL`: due tomorrow

**Implementation note**: Use JXA (JavaScript for Automation) via `osascript -l JavaScript` for structured output. AppleScript returns unstructured text; JXA returns JSON-like objects.

**Portability**: This provider only works on macOS. The provider registry should gracefully skip it on Linux with a debug log, not an error.

### 5.4 PRStatusProvider

**Source**: GitHub API via `gh` CLI (reuses existing SOVA infrastructure)
**Auth**: `gh` CLI auth (already configured per project)
**Default lookback**: all open PRs

**Categorization logic**:
- `NEEDS_ATTENTION` (high urgency): PRs where review is requested from user
- `NEEDS_ATTENTION`: user's PRs with failing CI, user's PRs with new review comments
- `NEEDS_ATTENTION`: SOVA-created PRs ready for human merge
- `INFORMATIONAL`: user's PRs with passing CI, recently merged PRs

**Implementation**: This provider is cross-project. It should iterate all registered SOVA projects (`~/.config/sova/projects.json`), not just the current project. Reuse `sova/dashboard/services/pr_service.py` patterns.

### 5.5 AgentRunProvider

**Source**: SOVA's SQLite database (TaskRun, IssueLifecycle models)
**Auth**: None (local DB)
**Default lookback**: 24 hours

**Categorization logic**:
- `NEEDS_ATTENTION`: runs waiting for human handoff (status=WAITING_HANDOFF)
- `NEEDS_ATTENTION`: runs that failed (status=FAILED)
- `INFORMATIONAL`: runs completed successfully, runs in progress

**Implementation**: Query the `task_run` and `issue_lifecycle` tables directly. Cross-project: iterate all registered projects and query each project's database.

## 6. BriefingService

The central aggregator. Fetches from all providers, merges, deduplicates, sorts, and renders.

```python
class BriefingService:
    """Aggregates awareness items from all providers into a unified briefing."""

    def __init__(self, providers: list[AwarenessProvider]):
        self.providers = providers

    async def generate_briefing(
        self,
        since: datetime | None = None,
    ) -> Briefing:
        """Fetch all providers and build a prioritized briefing.

        Steps:
        1. Fetch items from all providers concurrently (asyncio.gather)
        2. Merge into a single list
        3. Categorize: needs_attention vs informational
        4. Sort needs_attention by urgency (critical > high > normal), then by timestamp
        5. Sort informational by timestamp (newest first)
        6. Build Briefing object with sections
        """
```

**Briefing data structure**:

```python
@dataclass
class Briefing:
    generated_at: datetime
    attention_items: list[AwarenessItem]    # sorted by urgency desc, then timestamp desc
    informational_items: list[AwarenessItem]  # sorted by timestamp desc
    schedule: list[AwarenessItem]           # calendar events, chronological
    project_pulses: list[ProjectPulse]      # 1-line per project
    provider_statuses: list[ProviderStatus] # which providers succeeded/failed
    since: datetime | None                  # the lookback window used

@dataclass
class ProjectPulse:
    project_slug: str
    open_prs: int
    agent_status: str                       # "idle" | "running (2)" | "waiting handoff"
    last_ci: str                            # "passing" | "failing" | "unknown"

@dataclass
class ProviderStatus:
    name: str
    ok: bool
    message: str
    items_fetched: int
    fetch_time_ms: int
```

## 7. Configuration

### 7.1 Config Model

Add to `sova/config/models.py`:

```python
class AwarenessConfig(BaseSettings):
    """Awareness subsystem configuration."""

    enabled: bool = False
    providers: list[str] = Field(default_factory=list)

    # Gmail
    gmail_token_path: str = ""            # path to OAuth token; empty = ~/.config/sova/google_token.pickle
    gmail_lookback_hours: int = 24
    gmail_ignore_labels: list[str] = Field(default_factory=lambda: ["SPAM", "TRASH"])

    # Google Calendar
    gcal_calendars: list[str] = Field(default_factory=lambda: ["primary"])
    gcal_lookahead_hours: int = 36

    # Apple Reminders
    reminders_lists: list[str] = Field(default_factory=lambda: ["Reminders"])

    # PRs (cross-project)
    pr_github_user: str = ""              # GitHub username for "review requested" detection

    model_config = SettingsConfigDict(env_prefix="SOVA_AWARENESS_")
```

Add `awareness: AwarenessConfig = Field(default_factory=AwarenessConfig)` to `ProjectConfig`.

### 7.2 sova.toml Example

```toml
[awareness]
enabled = true
providers = ["gmail", "gcal", "reminders", "pr_status", "agent_runs"]

# Optional overrides
gmail_lookback_hours = 24
gmail_ignore_labels = ["SPAM", "TRASH", "CATEGORY_PROMOTIONS"]
gcal_lookahead_hours = 36
pr_github_user = "dsova06"
```

### 7.3 Global vs Per-Project

Awareness config is **per-installation, not per-project**. Rationale: your Gmail inbox is the same regardless of which project you're working on. Calendar events span all projects.

**Implementation**: Store the `[awareness]` section in a global config file (`~/.config/sova/awareness.toml`) that is loaded separately from per-project `sova.toml`. The `sova briefing` command reads this global config. Per-project `sova.toml` can override `awareness.enabled = false` to exclude a project from PR/agent-run aggregation.

## 8. Google OAuth2 Flow

### 8.1 Credentials Setup

SOVA needs its own OAuth2 credentials (or can reuse existing ones from the Google Workspace MCP server).

**Option A: Shared credentials (for personal use)**
- Point `gmail_token_path` to `~/Code/team-productivity-utils/google-workspace-mcp/.config/google_token.pickle`
- No new OAuth setup needed
- Couples SOVA to the MCP server installation

**Option B: SOVA-managed credentials (for team distribution)**
- SOVA manages its own OAuth client at `~/.config/sova/google_credentials.json` and `~/.config/sova/google_token.pickle`
- Colleagues set up their own GCP project or the team shares one OAuth client ID
- `sova briefing` triggers the OAuth flow on first run if token is missing

**Recommendation**: Implement Option B for portability. Support Option A via the `gmail_token_path` config override.

### 8.2 Required Scopes

```python
AWARENESS_GOOGLE_SCOPES = [
    'https://www.googleapis.com/auth/gmail.readonly',
    'https://www.googleapis.com/auth/calendar.readonly',
]
```

### 8.3 Auth Flow

```python
# sova/awareness/auth/google_oauth.py

def authenticate_google(config: AwarenessConfig) -> Credentials:
    """Run OAuth2 flow for Gmail + Calendar.

    1. Check for existing token at gmail_token_path or default location
    2. If token exists and has required scopes, refresh if expired
    3. If no token or missing scopes, run InstalledAppFlow (opens browser)
    4. Save token to disk
    """
```

### 8.4 Gmail API Enablement

Colleagues setting this up will need to:
1. Create a GCP project (or use an existing one)
2. Enable the Gmail API and Google Calendar API
3. Create OAuth Desktop credentials
4. Download the credentials JSON to `~/.config/sova/google_credentials.json`
5. Run `sova briefing` — it will open a browser for consent

This is a one-time setup per user. Document it in the README and in the `sova doctor` output.

## 9. CLI Command

### 9.1 `sova briefing`

```
Usage: sova briefing [OPTIONS]

  Show a prioritized summary of what needs your attention.

Options:
  --since TEXT    Only show items from the last N hours (e.g., "2h", "30m")
  --providers TEXT  Comma-separated list of providers to include (overrides config)
  --json          Output as JSON instead of formatted text
  --quiet         Only show "needs attention" items, skip informational
```

### 9.2 CLI Output Format (Rich)

```
╭──────────────────────────────────────────────────────────────╮
│  Good morning! Sunday, July 20, 2026                        │
│  3 items need your attention                                 │
╰──────────────────────────────────────────────────────────────╯

NEEDS ATTENTION
  [!!] PR Review requested: [insights-rbac#3199] refactor(models): optimize query fields
       From: lpichler - 3 hours ago - CI: passing
  [!!] PR Review requested: [insights-rbac#3200] fix(ci): always run JIRA ticket check
       From: lpichler - 3 hours ago - CI: passing
  [!]  Email: Device management next step: Returning devices
       From: Nikki Jacobs <njacobs@redhat.com> - Jul 16

TODAY'S SCHEDULE
  10:00  Kessel Scrum of Scrums (30m), 5 attendees
  14:00  Free block (3h)
  17:00  EOD

PROJECT PULSE
  insights-rbac    2 PRs open - agents idle - CI passing
  sova             0 PRs open - agents idle - CI passing

RECENT EMAILS (5 new)
  > Rehor: Re: [insights-rbac#3199] SQL queries to validate... (3h ago)
  > Libor Pichler: Review requested on #3199 and #3200 (3h ago)
  > Nikki Jacobs: Device management next step (Jul 16)
  > Gemini: Notes: "Kessel Scrum of Scrums" Jul 16 (Jul 16)
  > [2 more informational]

Reminders: Nothing due today
```

## 10. Dashboard Page

### 10.1 Router

New file: `sova/dashboard/routers/briefing.py`

```python
# API endpoints:
# GET /api/briefing              — fetch briefing JSON
# GET /api/briefing/providers    — provider health status
# POST /api/briefing/dismiss     — dismiss an item

# Page route:
# GET /briefing                  — render briefing.html template
```

### 10.2 Template

New file: `sova/dashboard/templates/briefing.html`

Layout:
- Top banner: greeting, date, attention count
- Left column (2/3 width): Needs Attention cards, Email highlights
- Right column (1/3 width): Today's Schedule timeline, Project Pulse cards
- Bottom: Informational items (collapsed by default)

Auto-refresh: poll `/api/briefing` every 90 seconds, merge into DOM without scroll reset.

### 10.3 Navigation

Add "Briefing" as the first item in the sidebar nav (`base.html`), above Dashboard. Use a sun/morning icon.

## 11. Database Changes

### 11.1 New Model: DismissedItem

```python
class DismissedItem(Base):
    """Tracks items the user has dismissed from briefings."""

    __tablename__ = "dismissed_item"

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[str] = mapped_column(String(256), index=True)  # e.g., "gmail:19f6f645"
    provider: Mapped[str] = mapped_column(String(64))
    dismissed_at: Mapped[datetime] = mapped_column(default=func.now())
    expires_at: Mapped[datetime | None] = mapped_column(nullable=True)  # auto-cleanup
```

### 11.2 Migration

Add an Alembic migration for the `dismissed_item` table.

## 12. Dependencies

Add to `pyproject.toml`:

```toml
[project.optional-dependencies]
awareness = [
    "google-api-python-client>=2.100.0",
    "google-auth>=2.20.0",
    "google-auth-oauthlib>=1.2.0",
    "google-auth-httplib2>=0.2.0",
]
```

Make awareness deps optional so colleagues who don't want Google integration can skip them. The `sova briefing` command checks for missing deps and prints an install hint.

## 13. Implementation Phases

### Phase 1: Foundation + Gmail (MVP)

**Deliverables**: `sova briefing` works with Gmail only.
**Effort**: ~2 days

| # | Task | Details |
|---|------|---------|
| 1.1 | Create `sova/awareness/` package | `base.py` with ABC, `AwarenessItem`, `ItemCategory` |
| 1.2 | Implement `GmailProvider` | Port Gmail API client from MCP service, adapt to AwarenessProvider interface |
| 1.3 | Implement Google OAuth in SOVA | `sova/awareness/auth/google_oauth.py`, token management |
| 1.4 | Add `AwarenessConfig` to config | Config model, loader update, sova.toml section |
| 1.5 | Implement `BriefingService` | Aggregation, categorization (single provider for now) |
| 1.6 | Implement CLI renderer | Rich terminal output via `sova/awareness/rendering/cli_renderer.py` |
| 1.7 | Add `sova briefing` CLI command | Register in `cli/app.py`, wire to BriefingService |
| 1.8 | Tests | Provider tests (mocked API), BriefingService tests, CLI output tests |

### Phase 2: Calendar + Reminders + PR Status

**Deliverables**: Full provider suite, `sova briefing` shows emails + calendar + reminders + PRs.
**Effort**: ~2 days

| # | Task | Details |
|---|------|---------|
| 2.1 | Implement `CalendarProvider` | Google Calendar API, shared OAuth token with Gmail |
| 2.2 | Implement `RemindersProvider` | Apple Reminders via osascript/JXA, macOS-only |
| 2.3 | Implement `PRStatusProvider` | Cross-project PR aggregation via `gh` CLI |
| 2.4 | Implement `AgentRunProvider` | Query SOVA DB across registered projects |
| 2.5 | Update BriefingService | Multi-provider concurrent fetch, project pulse generation |
| 2.6 | Update CLI renderer | Schedule timeline, project pulse, provider status indicators |
| 2.7 | Tests | Per-provider tests, integration test with all providers |

### Phase 3: Dashboard Page

**Deliverables**: `/briefing` dashboard page with live data.
**Effort**: ~1-2 days

| # | Task | Details |
|---|------|---------|
| 3.1 | Create briefing router | API endpoints for briefing data and dismiss |
| 3.2 | Create briefing service (dashboard) | Async wrapper around awareness BriefingService |
| 3.3 | Create briefing template | Jinja2 page matching dashboard design system |
| 3.4 | Add sidebar nav entry | "Briefing" at top of sidebar |
| 3.5 | Auto-refresh | JS polling with DOM merge, no scroll reset |
| 3.6 | Dismiss functionality | POST endpoint + DismissedItem model + migration |

### Phase 4: Polish + Context Recovery

**Deliverables**: `--since` flag, dismiss persistence, provider config in dashboard settings.
**Effort**: ~1 day

| # | Task | Details |
|---|------|---------|
| 4.1 | `--since` flag | Parse human-readable durations, pass to providers |
| 4.2 | Dashboard settings page | Provider enable/disable toggles, OAuth status, re-auth button |
| 4.3 | `sova doctor` integration | Check awareness providers in doctor output |
| 4.4 | Documentation | README section, sova.toml.default update, setup guide for colleagues |

## 14. GitHub Issues to Create

For the SOVA repo roadmap, create these issues:

1. **`feat(awareness): AwarenessProvider ABC and package structure`** — Phase 1.1-1.2
   Labels: `enhancement`, `awareness`

2. **`feat(awareness): Google OAuth2 flow for Gmail + Calendar`** — Phase 1.3
   Labels: `enhancement`, `awareness`, `auth`

3. **`feat(awareness): AwarenessConfig and sova.toml support`** — Phase 1.4
   Labels: `enhancement`, `awareness`, `config`

4. **`feat(awareness): BriefingService aggregation engine`** — Phase 1.5
   Labels: `enhancement`, `awareness`

5. **`feat(awareness): CLI briefing command with Rich output`** — Phase 1.6-1.7
   Labels: `enhancement`, `awareness`, `cli`

6. **`feat(awareness): GmailProvider implementation`** — Phase 1.2 (details)
   Labels: `enhancement`, `awareness`, `provider`

7. **`feat(awareness): CalendarProvider implementation`** — Phase 2.1
   Labels: `enhancement`, `awareness`, `provider`

8. **`feat(awareness): RemindersProvider (Apple Reminders via JXA)`** — Phase 2.2
   Labels: `enhancement`, `awareness`, `provider`

9. **`feat(awareness): PRStatusProvider (cross-project PR aggregation)`** — Phase 2.3
   Labels: `enhancement`, `awareness`, `provider`

10. **`feat(awareness): AgentRunProvider (SOVA activity aggregation)`** — Phase 2.4
    Labels: `enhancement`, `awareness`, `provider`

11. **`feat(awareness): Dashboard briefing page`** — Phase 3
    Labels: `enhancement`, `awareness`, `dashboard`

12. **`feat(awareness): Dismiss items + DismissedItem DB model`** — Phase 3.6 + 4
    Labels: `enhancement`, `awareness`, `dashboard`, `database`

13. **`docs(awareness): Setup guide and sova.toml documentation`** — Phase 4.4
    Labels: `documentation`, `awareness`

## 15. Hackathon Scope

If this is the hackathon project, the demo target is:

**Phase 1 + partial Phase 2 + partial Phase 3** = a working `sova briefing` CLI command that shows Gmail + PRs + agent activity, plus a basic dashboard page.

**Demo script**:
1. Open terminal, run `sova briefing`
2. Show prioritized output: "3 items need attention"
3. Show email highlights with subjects and senders
4. Show PR status across projects
5. Open dashboard, show `/briefing` page with same data in a visual layout
6. Click an email — opens Gmail. Click a PR — opens GitHub.
7. Dismiss an item — it disappears from the next refresh.

**Time estimate**: 2-3 focused days for the demo-ready MVP.

## 16. Portability for Colleagues

For a colleague to use this:

1. `pip install sova[awareness]` (installs Google API dependencies)
2. Set up GCP OAuth credentials (one-time, documented in README)
3. Add `[awareness]` section to their `sova.toml`
4. Run `sova briefing` — browser opens for OAuth consent
5. Done. Subsequent runs use cached token.

**No dependency on the Google Workspace MCP server.** SOVA manages its own OAuth flow and API clients. The MCP service code we built is the reference implementation — the awareness provider is a clean reimplementation within SOVA's architecture.

## 17. Future Extensions (Post-MVP)

These are explicitly out of scope for initial implementation but documented for roadmap:

- **Slack integration**: Read channel messages, DMs. Blocked by Red Hat policy — revisit when/if corporate Slack app approval is possible.
- **AI-powered triage**: Use Claude to summarize emails, suggest which are urgent, draft responses. Requires LLM calls per briefing — cost consideration.
- **Voice interface**: `sova briefing --speak` using macOS `say` command. Low effort, high demo value.
- **Proactive notifications**: Background polling that pushes desktop notifications when something urgent arrives (not just when you run `sova briefing`).
- **Email sending**: `sova briefing --email` sends yourself a formatted digest.
- **Meeting prep context**: Cross-reference calendar attendees with recent emails and PRs for pre-meeting context (UC-4 in the use case spec).
- **JIRA activity**: Track JIRA ticket state changes, new comments, sprint changes.
