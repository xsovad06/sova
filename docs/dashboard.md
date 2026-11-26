# Dashboard

The web dashboard is SOVA's primary interface for monitoring and controlling agents.

```bash
sova dashboard --project /path/to/project    # http://localhost:8111
```

| Page | Purpose |
|------|---------|
| **Home** | Project list (command center) for multi-project installations |
| **Work** | What runs, what needs you and what to start next: a header (agents and their limit, run success and merged PRs over 7 days, spend, CodeRabbit, supervisor switch), a board (Up next, Queued, Running, In review, Needs you, Done today) with an issue drawer and Mine/All owner filter, the dependency chains, open PRs and run history (by issue, with its state and result, or every run). `/agents` redirects here |
| **Run Detail** | Per-run step pipeline, logs, and cost breakdown |
| **Lifecycle** | Issue lifecycle rail (development through post-merge) |
| **Costs** | Per-task and per-model cost tracking and aggregation |
| **Queue** | Batch triage/harden operations with progress tracking |
| **Roles** | View and edit custom workflow roles (DAG-based) |
| **Settings** | Runtime configuration management |
| **Setup** | Project onboarding wizard with directory browser |
| **Style Guide** | Design system reference with live component examples |

On the Work board, **Up next** ranks issues ready for an agent. **Queued** is the saved order the supervisor starts when it is on and a slot is free. **Running** includes active runs, rebases, and issues marked in progress. **In review** contains open PRs still in draft, CI, review, or changes requested, plus issues marked in review by the tracker; it does not imply unresolved review findings. The PR's thread count shows resolved/total, so the difference is unresolved. **Needs you** contains merge decisions, approvals, and failed runs. Mine matches an issue assignee to the configured identity for that issue's tracker, or a PR author or assignee to the GitHub user. The header figures remain project wide.

## Remote Access over Tailscale

The dashboard stays bound to loopback. To reach it from your own phone or laptop, publish it to your tailnet with Tailscale Serve (never Funnel, which would make it public) and tell SOVA which origin and which Tailscale login to accept:

```bash
tailscale serve --bg --https=443 http://127.0.0.1:8111
sova config set dashboard.proxy_origins https://mac.example.ts.net
sova config set dashboard.proxy_allowed_logins you@example.com
```

Replace `mac.example.ts.net` with this machine's tailnet name (`tailscale status --json` prints it as `Self.DNSName`, without the trailing dot) and `you@example.com` with your Tailscale login (`LoginName` of your entry under `User` in the same output; `tailscale status` truncates it). Keep the lines free of trailing `#` comments when you paste them into zsh: an interactive zsh does not treat `#` as a comment by default, so the text becomes arguments.

Both settings apply to the whole server and are read once at startup, so restart the server afterwards (`sova server restart`, after checking that no agent is running). They are read from the project the server serves. On the multi-project hub (`sova server start --multi`) that is the directory the server was started in, so run the `sova config set` lines there, or set `SOVA_DASHBOARD_PROXY_ORIGINS` and `SOVA_DASHBOARD_PROXY_ALLOWED_LOGINS` (JSON arrays) in the server environment; a value saved in any other project has no effect.

Enable HTTPS certificates in the Tailscale admin console first, and consider restricting the access policy to your own devices (`autogroup:self`). A request through the proxy origin without an allowed login, and any Funnel request, gets 403. If the first HTTPS request fails with a TLS error, `tailscale cert mac.example.ts.net` prints the underlying certificate error. The security model is in [Security Guidelines: Dashboard Security Model](security-guidelines.md#dashboard-security-model).

On a phone, open `/phone` (`/p/<slug>/phone` on the hub) instead of the Work page: one narrow column with what waits on you (handoffs and the supervisor's pending plan), the running agents with Stop, and a field to start an issue. It uses the same endpoints as the Work page, so it needs no extra setup.
