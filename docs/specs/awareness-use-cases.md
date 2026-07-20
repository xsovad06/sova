# Use Case Specification: SOVA Awareness Subsystem

**Status**: Draft
**Date**: 2026-07-20
**Related**:
- [Implementation Specification](awareness-implementation-spec.md)
- [Hackathon Plan (Google Doc)](https://docs.google.com/document/d/1vX0GRPioJzgmhaGfmMD5yoydTuGC9zWvGpoDrwE9KJg/edit)

## 1. Problem Statement

A developer managing multiple projects spends 20-30 minutes every morning assembling context: checking email for action items, reviewing calendar for meetings and prep time, scanning PR statuses across repos, checking CI pipelines, reading JIRA/GitHub issue updates, and triaging what to work on first. This context is scattered across 5-8 browser tabs and tools with no unified view.

The cost is not just time. Switching between sources breaks concentration, important items get buried, and the developer starts coding without full awareness of what changed overnight (a merged PR that invalidates their branch, a failing CI pipeline, a meeting in 30 minutes that needs prep).

## 2. Personas

### Primary: Solo Developer with Multiple Projects

- Maintains 2-5 active projects across GitHub and JIRA
- Uses SOVA to run autonomous agents on some of those projects
- Has a work email (Gmail) with 20-50 messages/day
- Has a work calendar (Google Calendar) with 3-6 meetings/day
- Uses Apple Reminders for personal task tracking
- Works from a Mac, usually starts the day with a terminal and browser open

### Secondary: Team Lead with Fleet Oversight

- Same as above, plus responsible for a team's output
- Cares about: are PRs getting stuck, is anyone blocked, are agents productive
- Needs a roll-up view across projects, not per-project dashboards

## 3. Use Cases

### UC-1: Morning Briefing (CLI)

**Actor**: Developer, starting their work day
**Trigger**: Developer opens terminal, runs `sova briefing`
**Precondition**: At least one awareness provider is configured
**MVP**: Phase 1

**Main Flow**:

1. Developer runs `sova briefing`
2. System fetches fresh data from all configured providers (Gmail, Calendar, GitHub PRs, Apple Reminders, SOVA agent runs)
3. System categorizes items by urgency:
   - **Needs attention**: PRs awaiting your review, CI failures on your branches, direct-mention emails, overdue reminders, agent runs waiting for handoff
   - **Informational**: PR approvals, merged PRs, team thread emails, calendar events, completed agent runs
4. System renders a structured text summary to the terminal using Rich:
   - Greeting with date and day-of-week
   - "Needs attention" items (count + details)
   - Today's schedule (chronological, with free blocks)
   - Per-project pulse (1-line status: open PRs, agent activity, last CI)
   - Top emails needing action (sender, subject, snippet)
   - Due/overdue reminders
5. Developer reads the summary and knows where to start

**Alternate Flows**:

- **No providers configured**: system prints a setup hint (global config location, `sova briefing --help`) and exits
- **Provider auth expired**: system prints which provider failed, continues with remaining providers
- **No items found**: system prints "Nothing needs attention. Your backlog is clear."
- **Subset mode**: `sova briefing --providers gmail,pr_status` fetches only the named providers, overriding config
- **Machine output**: `sova briefing --json` outputs structured JSON (for piping to other tools or cron jobs)
- **Attention-only mode**: `sova briefing --quiet` shows only "needs attention" items, skips informational sections

**Acceptance Criteria**:

- [ ] `sova briefing` completes in under 10 seconds with cached data, under 30 seconds on cold fetch
- [ ] Output is scannable: a developer can identify the top 3 action items within 5 seconds of reading
- [ ] Each item has enough context to decide act/skip without opening another tool (sender, subject, snippet for emails; repo, title, CI status for PRs)
- [ ] Partial provider failure does not block the briefing; failed sources show a warning line with the provider name and error
- [ ] CLI output uses no emojis (project convention); urgency is conveyed through text markers and Rich formatting (color, bold, indentation)
- [ ] `--json` output follows a stable schema suitable for scripting
- [ ] `--providers` flag accepts a comma-separated list matching provider names in config

### UC-2: Morning Briefing (Dashboard)

**Actor**: Developer, starting their work day via browser
**Trigger**: Developer opens the SOVA dashboard
**MVP**: Phase 3

**Main Flow**:

1. Developer navigates to the SOVA dashboard `/briefing` page
2. Page renders a structured view with live data:
   - **Needs your attention** section at top (PRs to review, CI failures, urgent emails, pending agent handoffs)
   - **Today's schedule** (visual timeline with meeting blocks and free time)
   - **Project pulse** (card per project: open PRs, agent status, last CI result)
   - **Email highlights** (top emails with sender, subject, snippet)
   - **Reminders** (due today and overdue)
3. Each item is actionable: clicking an email opens Gmail, clicking a PR opens GitHub, clicking an agent goes to the SOVA run detail page
4. Page auto-refreshes every 90 seconds, merging new data into the DOM

**Alternate Flows**:

- **First visit, no config**: page shows a setup guide with steps to configure awareness providers (which providers to enable, where to put OAuth credentials, link to `sova doctor`)
- **Stale data**: items older than the poll interval show a "last updated X min ago" indicator so the user knows the data is not live
- **Provider partially down**: provider status badges at the top show green/yellow/red per provider; clicking a yellow/red badge shows the error message

**Acceptance Criteria**:

- [ ] Page loads in under 2 seconds with cached data
- [ ] "Needs attention" count is visible without scrolling
- [ ] Every item links to its source (Gmail, GitHub, JIRA, Calendar)
- [ ] Dashboard works with any subset of providers (Gmail-only, GitHub-only, all)
- [ ] Auto-refresh does not reset scroll position or disrupt interaction (DOM merge, not full re-render)
- [ ] "Briefing" appears as the first item in the sidebar nav, above Dashboard
- [ ] Stale items show "last updated" timestamp when data is older than 3 minutes

### UC-3: Mid-Day Context Recovery

**Actor**: Developer, returning from a meeting or break
**Trigger**: Developer wants to know what changed in the last 1-2 hours
**MVP**: Phase 4

**Main Flow**:

1. Developer runs `sova briefing --since 2h` (or uses a time-range filter on the dashboard)
2. System shows only items that arrived or changed since the specified window:
   - New emails
   - PRs that changed state (approved, merged, CI passed/failed)
   - Agent runs that completed or failed
   - New calendar events added
3. Developer quickly catches up without re-reading the full morning briefing

**Acceptance Criteria**:

- [ ] `--since` flag accepts human-readable durations (1h, 2h, 30m, 4h)
- [ ] Dashboard equivalent: a time-range dropdown or "since last visit" toggle
- [ ] Items clearly indicate what changed (new vs. updated) through visual distinction or text labels

### UC-4: Meeting Prep

**Actor**: Developer, 10 minutes before a meeting
**Trigger**: Developer sees an upcoming meeting in the briefing and wants to prepare
**MVP**: Post-hackathon (deferred to future extension)

**Main Flow**:

1. Calendar event in the briefing shows: title, time, attendees, description/agenda
2. If the meeting is about a specific project or PR (detected from title or description), the briefing cross-references:
   - Relevant PR status and recent CI results
   - Recent commits or agent activity on related issues
   - Unread email threads with the same attendees
3. Developer has context without manually searching across tools

**Acceptance Criteria**:

- [ ] Calendar events show attendee names (not just count)
- [ ] Cross-referencing is best-effort: no match is fine, false positives are not
- [ ] Meeting prep context appears inline with the calendar item, not on a separate page

**Implementation note**: Cross-referencing is the most complex feature. Deferred to post-hackathon. Phase 2 delivers calendar events with attendee names and metadata; cross-referencing is layered on later.

### UC-5: PR Triage Across Projects

**Actor**: Developer responsible for reviewing PRs on multiple repos
**Trigger**: Developer wants to see all PRs needing their attention in one place
**MVP**: Phase 2

**Main Flow**:

1. Briefing aggregates PRs across all registered SOVA projects
2. PRs are categorized:
   - **Review requested**: PRs where the developer is an assigned reviewer
   - **CI failing on your PRs**: the developer's own PRs with red CI
   - **Ready to merge**: approved PRs with green CI, waiting for merge
   - **SOVA-created PRs**: PRs created by SOVA agents, needing human review/merge
3. Each PR shows: repo name, title, author, CI status, age, review state

**Acceptance Criteria**:

- [ ] PRs from all registered projects appear (GitHub repos)
- [ ] "Review requested" only shows PRs where the user is explicitly requested, not all open PRs
- [ ] Stale PRs (> 7 days with no activity) are visually distinct (different text marker or color in CLI, muted styling in dashboard)
- [ ] Clicking a PR opens it on GitHub

**Scope note**: JIRA activity tracking (ticket state changes, new comments, sprint changes) is deferred to post-MVP. The PRStatusProvider covers GitHub PRs only via the `gh` CLI. JIRA-linked repos show PR status but not JIRA ticket activity.

### UC-6: Agent Activity Summary

**Actor**: Developer using SOVA agents across projects
**Trigger**: Developer wants to see what agents did overnight or during a period
**MVP**: Phase 2

**Main Flow**:

1. Briefing includes an "Agent Activity" section showing:
   - Completed runs: which issues were worked on, outcomes (PR created, review done, failed)
   - Active runs: currently running agents and their progress
   - Pending handoffs: agents waiting for human action (merge, review feedback)
2. Each item links to the SOVA run detail page

**Acceptance Criteria**:

- [ ] Shows activity from all registered SOVA projects
- [ ] Completed runs show the outcome, not just "completed" (e.g., "PR #42 created", "review: 3 findings")
- [ ] Pending handoffs are highlighted as "needs attention" items in the top section (not buried in informational)
- [ ] Failed runs appear in "needs attention" with the failure reason

### UC-7: Dismiss / Mark as Handled

**Actor**: Developer who has acted on a briefing item
**Trigger**: Developer reviewed a PR, replied to an email, or completed a task
**MVP**: Phase 3 (manual dismiss), Phase 4 (auto-dismiss)

**Main Flow**:

1. Developer clicks "dismiss" on a briefing item (dashboard) or the item naturally disappears when the source state changes (PR merged, email read)
2. Dismissed items do not reappear in subsequent briefings
3. A "dismissed today" section at the bottom shows what was handled (optional, collapsed by default)

**Alternate Flows**:

- **Auto-dismiss**: when a provider re-fetches and the item is no longer actionable at the source (PR merged, email marked as read in Gmail, reminder completed), it automatically moves to dismissed without user action
- **Expiry**: dismissed items auto-expire after 7 days (configurable via `DismissedItem.expires_at`) to prevent unbounded table growth
- **CLI equivalent**: items that are no longer actionable at the source simply don't appear in subsequent `sova briefing` runs (no explicit dismiss in CLI; the provider's fetch logic handles it)

**Acceptance Criteria**:

- [ ] Manual dismiss is available on all item types in the dashboard
- [ ] Source-state changes (PR merged, email read externally) automatically remove items on next fetch
- [ ] Dismissed items survive page refresh (persisted in DB via `DismissedItem` model, not just client-side)
- [ ] Dismissed items auto-expire after a configurable period (default 7 days)
- [ ] CLI: items that are no longer actionable at the source simply don't appear

**Implementation note**: Auto-dismiss (detecting external state changes) is a Phase 4 enhancement. Phase 3 delivers manual dismiss only. The provider's `fetch_items()` naturally excludes resolved items (read emails, merged PRs), so CLI auto-dismiss is inherent in the provider logic.

### UC-8: Provider Configuration

**Actor**: Developer setting up SOVA awareness for the first time
**Trigger**: Developer wants to enable email/calendar/reminder integration
**MVP**: Phase 1

**Main Flow**:

1. Developer creates the global awareness config at `~/.config/sova/awareness.toml`:
   ```toml
   [awareness]
   enabled = true
   providers = ["gmail", "gcal", "reminders", "pr_status", "agent_runs"]
   pr_github_user = "dsova06"
   ```
2. Developer runs `sova briefing`
3. For providers requiring OAuth (Gmail, Google Calendar), system checks for a token at `~/.config/sova/google_token.pickle`. If missing, opens a browser for OAuth consent (requires `~/.config/sova/google_credentials.json` from GCP)
4. For local providers (Apple Reminders), system uses osascript with no setup. On non-macOS systems, the provider is silently skipped with a debug log
5. For GitHub-based providers (PR status), system uses existing `gh` CLI auth
6. Once configured, providers are remembered and work on subsequent runs

**Alternate Flows**:

- **Per-project override**: a project's `sova.toml` can set `awareness.enabled = false` to exclude that project from PR/agent-run aggregation
- **Missing Google credentials file**: system prints a step-by-step setup guide (create GCP project, enable APIs, download credentials JSON) and exits
- **Missing dependencies**: if `google-api-python-client` is not installed, system prints `pip install sova[awareness]` and exits
- **`sova doctor` check**: `sova doctor` reports awareness provider status (configured/connected/error) alongside existing health checks

**Acceptance Criteria**:

- [ ] Configuration is global (`~/.config/sova/awareness.toml`), not per-project. Email and calendar are the same regardless of which project you're in
- [ ] Per-project `sova.toml` can override `awareness.enabled = false` to opt out of aggregation
- [ ] Each provider can be enabled/disabled independently via the `providers` list
- [ ] Auth failure on one provider does not block others; the briefing shows which providers succeeded/failed
- [ ] Dashboard settings page shows provider status (connected/disconnected/error) with re-auth button
- [ ] `sova doctor` includes awareness provider health checks
- [ ] Awareness dependencies are optional: `pip install sova[awareness]` adds Google API libs; base `pip install sova` works without them

### UC-9: Programmatic / Scripted Briefing

**Actor**: Developer or automation tool consuming briefing data
**Trigger**: Script, cron job, or external tool needs structured briefing data
**MVP**: Phase 1 (CLI `--json`), Phase 3 (dashboard API)

**Main Flow**:

1. Script runs `sova briefing --json` and receives a stable JSON schema
2. Script parses the JSON to extract specific items (e.g., count of attention items, PR list, upcoming meetings)
3. Script takes action based on the data (post to Slack, send email digest, update a status board)

**Alternate Flows**:

- **Dashboard API**: `GET /api/briefing` returns the same data structure as `--json`, accessible from any HTTP client
- **Filtered output**: `sova briefing --json --quiet` returns only attention items
- **Provider subset**: `sova briefing --json --providers gmail` returns only Gmail items

**Acceptance Criteria**:

- [ ] `--json` output schema is documented and stable across patch versions
- [ ] JSON output includes provider statuses, fetch timings, and the `since` window used
- [ ] Dashboard `GET /api/briefing` returns the same schema as `--json`
- [ ] Zero non-JSON output on stdout when `--json` is used (warnings go to stderr)

## 4. Out of Scope (Future / Post-Hackathon)

These capabilities are part of the long-term JARVIS vision but are not in scope for the initial awareness subsystem:

- **Voice interface**: macOS `say` for spoken briefings, voice input
- **Slack integration**: reading/posting to Slack channels (blocked by corporate policy)
- **Proactive notifications**: push-based alerts ("CI just failed on your PR") via background polling
- **Email sending**: `sova briefing --email` to send yourself a digest
- **AI triage**: LLM-powered email prioritization and suggested responses
- **Cross-team awareness**: seeing other team members' activity
- **Mobile app**: briefing on phone
- **JIRA activity**: ticket state changes, new comments, sprint changes (PRStatusProvider covers GitHub PRs only)
- **Meeting prep cross-referencing**: correlating calendar events with PRs, emails, and commits (UC-4)

## 5. Priority Order

For hackathon or incremental delivery, build in this order:

| Priority | Use Case | Phase | Rationale |
|----------|----------|-------|-----------|
| 1 | UC-1 (CLI briefing) + UC-8 (config) + UC-9 (JSON) | Phase 1 | Foundation. If these work, you have a demo. |
| 2 | UC-5 (PR triage) + UC-6 (agent activity) | Phase 2 | Highest value per effort; uses existing SOVA data and adapters. |
| 3 | UC-2 (dashboard) + UC-7 manual dismiss | Phase 3 | Visual wow factor for demos; builds on UC-1's data layer. |
| 4 | UC-3 (context recovery) + UC-7 auto-dismiss | Phase 4 | Incremental improvement (add `--since` flag, dismiss expiry). |
| 5 | UC-4 (meeting prep) | Future | Cross-referencing is the hardest part; save for post-hackathon. |

## 6. Glossary

- **Provider**: a source of awareness data (Gmail, Google Calendar, Apple Reminders, GitHub PRs, SOVA agent runs)
- **Briefing**: the aggregated, prioritized output from all providers
- **Awareness item**: a single data point from a provider (one email, one PR, one calendar event)
- **Attention item**: an awareness item that requires developer action (category: `needs_attention`)
- **Project pulse**: a 1-line status summary for a registered SOVA project
- **Global config**: `~/.config/sova/awareness.toml`, shared across all projects on this machine
- **Provider registry**: the factory in `sova/awareness/__init__.py` that maps provider names to classes
