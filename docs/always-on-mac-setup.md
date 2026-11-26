# Always-On Mac Setup

How to keep SOVA running on a Mac that stays on at a desk, and supervise it from a phone. This guide links to the existing reference material rather than repeating it.

Tested between 2026-11-21 and 2026-11-24 on a MacBook Pro (Apple silicon, on AC power, FileVault on) with macOS 26.7.3, Tailscale 1.104.6, ntfy 2.38.1 and SOVA 0.1.0. Claude Code was 2.1.318 at the start and updated itself to 2.1.331 during the testing; the Claude Code behavior below was checked again on 2.1.331, and the launchd Remote Control test ran on it. A statement that comes from code or vendor documentation rather than a test on that Mac says so.

A second test on 2026-11-24 and 2026-11-25 used an Apple silicon MacBook on macOS 27.0.1 with the lid closed on an external display, a keep-awake app, Claude Code 2.1.314 and the multi-project hub, with an existing shared ntfy server instead of a local one. Statements from it are marked "second test". Claude Code updated itself to 2.1.331 during that test, and a running `claude remote-control` keeps the binary it started with.

## Contents

1. [Keep the Mac Awake](#keep-the-mac-awake)
2. [Start the Server at Login](#start-the-server-at-login)
3. [What Happens to Running Agents](#what-happens-to-running-agents)
4. [Steer Sessions from the Phone with Remote Control](#steer-sessions-from-the-phone-with-remote-control)
5. [Reach the Dashboard Privately with Tailscale](#reach-the-dashboard-privately-with-tailscale)
6. [Push Notifications with ntfy](#push-notifications-with-ntfy)
7. [Several Projects and a Second Machine](#several-projects-and-a-second-machine)
8. [Tokens](#tokens)

## Keep the Mac Awake

Agents make progress only while the Mac is awake: sleep suspends every process, the server and its agents included. On power, let the display sleep but not the Mac:

```bash
sudo pmset -c sleep 0
pmset -g custom
```

The second command should list `sleep 0` under "AC Power". `-c` changes only the power-adapter profile, so the Mac still sleeps on battery, and `displaysleep` can keep its value. The same setting is in System Settings, Battery, Options: "Prevent automatic sleeping on power adapter when the display is off". Tested: turning it on sets `sleep 0` under "AC Power" and leaves the battery profile at `sleep 1`.

**Closing the lid.** `sleep 0` does not keep a closed MacBook awake by itself. Tested on power with no external display: closing the lid put the Mac to sleep within seconds (`pmset -g log` showed "Entering Sleep state due to 'Clamshell Sleep'"), and sleep suspends every process (the server, any agents, Remote Control) until the lid is opened again; the Mac woke briefly on its own for network traffic, but nothing ran continuously. Tested with an external display connected and power plugged in: with the lid closed for about a minute, the external display kept showing the desktop and `pmset -g log` recorded no sleep, and `ioreg` reported `AppleClamshellCausesSleep = No` while the display was attached. So an always-on MacBook needs either the lid open, or the lid closed with an external display and power connected. Whether it stays awake for hours, or after the display is switched off or unplugged, was not tested.

**A keep-awake app instead of `pmset`.** A menu-bar app that holds a power assertion keeps the Mac awake without changing `pmset`: `pmset -g assertions` lists it as `PreventUserIdleSystemSleep` under the app's name. Second test, with one running, the lid closed and an external display attached: `ioreg` reported `AppleClamshellCausesSleep = No` and the Mac stayed up through the whole session. Two cautions:

- If you switch the app off at night so the Mac sleeps, leave `sleep` at its default. `sudo pmset -c sleep 0` would keep the Mac awake all night, which defeats that choice.
- While the Mac sleeps nothing runs: the server, the agents, Remote Control, and the pushes they send. A session started with a timer, or one that does not start again at login, ends or goes missing silently and leaves the Mac free to sleep.

## Start the Server at Login

```bash
sova config set server.scheduler_enabled false
sova server install-service --type launchd --project /path/to/project
launchctl load ~/Library/LaunchAgents/com.sova.server.plist
curl -s http://127.0.0.1:8111/api/health
```

Run these in the project directory. The first line is optional and stops the server from starting issues on its own (see "One project, and the scheduler" below). The [Server Management guide](server-operations.md) covers the service commands, log rotation with newsyslog and health monitoring. What matters for an unattended Mac:

- **It starts at login, not at boot.** The plist is a LaunchAgent, so launchd starts it in your login session. With FileVault on, the disk stays locked after a restart until someone logs in at the login window, so after a reboot (an update, a power cut) SOVA stays down until you log in once. This guide does not cover auto-login or a LaunchDaemon.
- **PATH.** launchd starts a LaunchAgent with `PATH=/usr/bin:/bin:/usr/sbin:/sbin`, which misses `~/.local/bin` (where `claude` installs) and `/opt/homebrew/bin` (`gh`, Homebrew's `git`). The generated plist sets `PATH` to the directory of each of `claude`, `node` (an npm-installed `claude` needs it), `gh`, `git`, `codex`, `terminal-notifier` and `uvx` found at install time, then `sova`'s own directory, then that default. Re-run `install-service --force` and reload after moving one of them, for example after a Python upgrade moves `sova`. Under a plist generated before SOVA added this, `sova doctor` fails `claude CLI`, `gh CLI`, `llm provider` and `agent runtime`, and `git` resolves to Apple's `/usr/bin/git`.
- **Credentials.** The service runs in your login session, so `claude` uses your `claude auth login` credential from the login keychain and `gh` its own keyring login. Put no token in the plist. On the test Mac a server started from the generated plist spawned a Claude agent that ran `gh pr list`, `gh issue list` and `gh api graphql` and finished `done`.
- **One project, and the scheduler.** The plist runs `sova server start --project <dir>`, which is single-project mode: the dashboard is at `/work` and the phone page at `/phone`, and the multi-project hub's `/p/<slug>/` paths return 404. Single-project mode also runs the scheduler's watch loop when `server.scheduler_enabled` is on (the default). On the test Mac it picked the top `agent:ready` issue within a minute of starting, waited the 30-second veto window and started a developer run. To start runs only yourself, set `server.scheduler_enabled` to `false` before loading the plist (it is read at startup). To run the multi-project hub under launchd instead, see [Start the Hub at Login](#start-the-hub-at-login).

To restart the service, run `launchctl kickstart -k gui/$(id -u)/com.sova.server`; to stop it until the next login, `launchctl bootout gui/$(id -u)/com.sova.server`.

## What Happens to Running Agents

| Event | Agent started from the dashboard, phone page or MCP tools | Run the watch loop started |
|-------|------------------------------------------------------------|----------------------------|
| Service restart (`launchctl kickstart -k`, or launchd restarting a crashed server) | Keeps running; the next server adopts it and records how it ends (tested: an adopted command run finished `done`) | Ends with the server, since the watch loop runs it inside the server process; the next server's startup recovery marks it `interrupted` (tested) |
| Interactive `sova server start` or `sova dashboard` exits (Ctrl+C) | Stopped on the way out (from code: `agents_outlive_server()` is false without a service manager) | Stopped |
| Reboot or power loss | Killed; at the next start, recovery records it from what it left behind (a PR handoff, a final `result` line) or as `interrupted` (from code) | Marked `interrupted` |
| Sleep | Paused with every other process until the Mac wakes (a lid-close sleep was tested; sleep suspends every process) | Paused |

The adoption rules (process identity check, the adopt lock file, what is not diff-guarded after adoption) are in the [Server Management guide](server-operations.md).

## Steer Sessions from the Phone with Remote Control

Claude Code Remote Control shows a Claude Code session running on the Mac in the Claude mobile app or at claude.ai/code. Claude and its tools keep running on the Mac. Anthropic's [Remote Control docs](https://code.claude.com/docs/en/remote-control) are the reference.

- **It needs the `claude auth login` credential.** A `claude setup-token` login is refused: `Error: Remote Control requires a full-scope login token.` (tested; see [Claude Code: Headless and Second-Machine Login](ai-provider-setup-guide.md#claude-code-headless-and-second-machine-login)). Do not export `CLAUDE_CODE_OAUTH_TOKEN` in the shell that starts it, since the CLI prefers that token to the login.
- **Interactive session or server mode.** Typing `/remote-control` in a running `claude` session (or starting it with `claude --remote-control`) shares that one session, and per Anthropic's docs it keeps retrying through a network outage. `claude remote-control` is server mode: it accepts several sessions in the directory it runs in, and per the same docs it exits after about 10 minutes without network, so run it in a restart loop.
- **It needs a terminal the first time.** Server mode asks "Enable Remote Control?" once, and asks to trust a directory not trusted yet; with no terminal to answer, it exits (`claude remote-control --help`: "with no terminal to ask on, run `claude` there once first"). Keep it running in a terminal multiplexer: `screen` ships with macOS.

  ```bash
  cd /path/to/project
  screen -S remote-control
  while true; do claude remote-control --name sova; sleep 10; done
  ```

  Detach with Ctrl+A then D, and reattach with `screen -r remote-control`.

  If you have not used `screen` before:

  - `screen -S remote-control` opens a second shell and the terminal looks emptied. That is normal: type the `cd` and the loop there, not in the shell you came from.
  - After Ctrl+A then D, `screen -ls` must list the session as `(Detached)`. `echo $STY` prints the session name inside a screen and nothing outside one.
  - To stop Remote Control, reattach and press Ctrl+C. Whether the loop then ends or starts `claude` again after the 10 second `sleep` was not tested: if it comes back, press Ctrl+C again while it waits, or end the whole screen with `screen -S remote-control -X quit`. To move a running Remote Control onto a newer `claude` binary, stop it that way and start the loop again.
  - Second test: a Remote Control started outside a screen was gone a few minutes later; the cause was not determined. After detaching, confirm that the process hangs under the screen with `ps -o pid,ppid,command -p <pid of claude remote-control>`, following the parent chain up to `SCREEN`.

- **Push notifications** are two switches in `/config`: "Push when Claude decides" and "Push when actions required" (both present under these names, tested). The phone also needs notifications allowed for the Claude app in the iPhone's Settings; with that off, the first test produced no push at all. Tested with "actions required" on: a question put to the session from the phone, with the phone locked, produced a push from the Claude app, but it appeared only after the phone was unlocked, not on the locked screen. The cause was not determined; the first things to check are the app's Lock Screen setting under Settings, Notifications, Claude, and any Focus mode. A permission prompt as the trigger was not tested.
- **The SOVA operator tools.** Flags given before `remote-control` (such as `--mcp-config`) are not passed to the sessions server mode spawns, and its own `--settings` flag needs Claude Code 2.1.321 or later. Register `sova mcp serve` with `claude mcp add` instead, as the [CLI reference describes](cli-reference.md#registering-the-mcp-server-in-claude-code), so every session started in that directory loads it. The registration is stored per folder, so each folder you serve with Remote Control needs its own `claude mcp add`, bound to that project with `--project` (see [Several Projects and a Second Machine](#several-projects-and-a-second-machine)). Second test: a phone session in each of two folders called `sova_status` and got live data for its own project.

Tested with a session started by `claude remote-control --name rc-terminal` in a terminal: it appeared in the Claude iPhone app, and a call to the registered `sova_status` and `sova_pending` tools came back with live data after approving the tool on the phone.

**Usage limits.** Not measured. `/usage` read 85% before and after a few short prompts, which is too coarse to show a difference. Claude runs on the Mac under your login, so these calls are ordinary Claude Code usage; Anthropic's Remote Control page does not say so either way.

**Spawn mode.** Starting `claude remote-control` stores `remoteControlSpawnMode` for the folder in `~/.claude.json`; on the test Mac it is `worktree`, so each session started from the phone gets its own git worktree. The first run in a folder asks for it (second test, Claude Code 2.1.314): "Spawn mode for this project: [1] same-dir (default), [2] worktree", saves the answer for the folder, and `--spawn=same-dir` or `--spawn=worktree` sets it without the question. A second `claude remote-control` for a folder already served is refused ("This folder is already served by a terminal `claude remote-control` on this device").

**Under launchd, with no terminal.** Tested on Claude Code 2.1.331: a LaunchAgent running `claude remote-control --name rc-launchd` in an already-trusted folder, with the same `PATH` the generated SOVA plist uses and no token in its environment, connected ("Connected", then "Ready", capacity 0/32). A new session started from the iPhone app against that environment answered, and the server created an isolated git worktree for it under `.claude/worktrees/bridge-<session id>` on a `worktree-bridge-<session id>` branch. Things to know:

- **The first launch in a folder needs a terminal**, as described above: run `claude remote-control` there once before handing it to launchd. The launchd test used a folder already trusted; an untrusted one was not tested.
- **A stopped session can be handed back.** After a `claude remote-control` in a terminal was stopped with Ctrl+C, the launchd process kept being handed that same ended session at start, and its pre-created session failed immediately ("Session failed: Process exited with error"), so nothing appeared on the phone. The process stayed up, and a new session started from the phone worked. If the phone shows nothing, start a new session there instead of waiting for the pre-created one.
- **Remote Control can change the repository's git config.** Creating the session worktree set the repository's `core.hooksPath` to the absolute path of the primary checkout's `.githooks`. SOVA's own repository expects the relative `.githooks`, and `sova doctor` fails its `git hooks` row until `git config core.hooksPath .githooks` is run again. Check it after a Remote Control session in a repository that uses `.githooks`. It was not reproduced in the second test: a phone session that only called a read-only SOVA tool created no worktree and left `core.hooksPath` unchanged in the two repositories checked.
- **Remove what a session leaves behind.** The session's worktree is locked while its session runs. After stopping it, `git worktree remove -f -f <path>` and `git branch -D <branch>` clean up an unused one.
- **Offline behavior is from the docs.** The roughly 10-minute exit when offline was not reproduced here; keep the restart loop (or launchd's `KeepAlive`, which was not tested) in front of `claude remote-control`.

## Reach the Dashboard Privately with Tailscale

The dashboard stays bound to `127.0.0.1`; Tailscale Serve publishes it to your own tailnet devices only, with Funnel off. Setup, the two settings it needs and the tailnet login check are in [Remote Access over Tailscale](dashboard.md#remote-access-over-tailscale); the security model is in [Security Guidelines: Dashboard Security Model](security-guidelines.md#dashboard-security-model).

On the test Mac, `tailscale serve status` reports:

```text
https://mac.example.ts.net (tailnet only)
|-- / proxy http://127.0.0.1:8111
```

with `dashboard.proxy_origins` set to that origin and `dashboard.proxy_allowed_logins` to the owner's Tailscale login. The phone page is at `/phone` on a single-project server (including the launchd service above) and at `/p/<slug>/phone` on the multi-project hub. Opening `https://mac.example.ts.net/p/<slug>/phone` in the iPhone's browser showed the page (handoffs waiting on you, running agents, a start field), tested on 2026-11-24.

**If the phone shows `ERR_NAME_NOT_RESOLVED`**, Tailscale is switched off on the phone: it cannot look up the machine's tailnet name. `tailscale status` on the Mac then lists the phone as offline. Turn Tailscale on in the phone's Tailscale app (the Settings app's VPN switch follows) and reload. Check this first when the page stops loading.

## Push Notifications with ntfy

SOVA pushes "needs you" and "failed" events to a self-hosted ntfy server; the settings and the reasons to self-host rather than use the public ntfy.sh are in the [Notifications guide](notifications.md). This setup was tested on 2026-11-24 with ntfy 2.38.1 and the ntfy iPhone app.

**Run the server in Docker.** ntfy's own docs say only its CLI is supported on macOS, and Homebrew's `ntfy` has no `serve` command. The official image works. Keep its port on loopback and let Tailscale Serve publish it, as with the dashboard:

```yaml
# ~/.config/ntfy/server.yml
base-url: "https://mac.example.ts.net:8443"
listen-http: ":80"
behind-proxy: true
cache-file: "/var/cache/ntfy/cache.db"
auth-file: "/var/lib/ntfy/user.db"
auth-default-access: "deny-all"
upstream-base-url: "https://ntfy.sh"
```

```bash
mkdir -p ~/.local/share/ntfy/cache ~/.local/share/ntfy/lib
docker run -d --name ntfy --restart unless-stopped -p 127.0.0.1:8090:80 \
  -v "$HOME/.config/ntfy:/etc/ntfy:ro" \
  -v "$HOME/.local/share/ntfy/cache:/var/cache/ntfy" \
  -v "$HOME/.local/share/ntfy/lib:/var/lib/ntfy" \
  binwiederhier/ntfy:v2.38.1 serve
tailscale serve --bg --https=8443 http://127.0.0.1:8090
```

`auth-default-access: "deny-all"` makes every topic private (an anonymous publish gets 403). `upstream-base-url` is what lets the iPhone app receive pushes while closed: per ntfy's docs the server forwards only the message ID and a hash of the topic URL to ntfy.sh, which wakes the app through Apple's push service, and the app then fetches the message itself over the tailnet. The container restarts on its own only while Docker runs, so turn on "Start Docker Desktop when you sign in" in Docker Desktop's settings; with it off, no push arrives after a reboot until Docker Desktop is opened.

**Accounts.** One read-only login for the phone and one write-only account for SOVA (each `user add` asks for a password you choose):

```bash
docker exec -it ntfy ntfy user add phone
docker exec -it ntfy ntfy user add sova
docker exec ntfy ntfy access phone sova read-only
docker exec ntfy ntfy access sova sova write-only
docker exec ntfy ntfy token add --label sova-server sova
```

SOVA signs in with the access token the last command prints. Store it with the script under [Tokens](#tokens), then point SOVA at the server, in the project the notifications come from:

```bash
sova config set notification.ntfy_url https://mac.example.ts.net:8443
sova config set notification.ntfy_topic sova
sova config set notification.link_base_url https://mac.example.ts.net/p/<slug>
```

On a single-project server, leave `/p/<slug>` off the link. These are read when a notification is sent, so no restart is needed.

**The phone.** With Tailscale connected, install the ntfy app, add the user `phone` for `https://mac.example.ts.net:8443` under Settings, Users, then subscribe to the topic `sova` with "Use another server" set to that address. Tested: pushes arrived as iPhone notifications, and tapping one opened the Work page. Delivery with the app closed and the phone locked for a while was not tested separately. Second test: with an iOS Sleep Focus active, a push did not surface until the ntfy app was opened, and the message was waiting there. A Focus mode holds notifications back, so rule it out before suspecting delivery. A push to a locked phone with the app closed and no Focus active was not tested.

**What a push looks like.** The title names the event and the body holds the project and the detail, for example "Developer failed #1557" over "sova | Step 'validate' failed: 2 tests failed". A symbol before the title marks the kind: a bell for "needs you", a warning sign for a failure, a check mark for a finish. On iOS every push shows the ntfy app's icon; ntfy's per-message `icon` URL did not change it (tested). Second test: a `needs_human` push showed the bell.

**Send a test push.** `send_ntfy_notification` never raises. A failed publish prints a `notify.ntfy_failed` warning with the HTTP status (second test: 403 for a missing token), the server host and the error type (no traceback, since one would carry the token), and returns normally; a successful one prints nothing at the default log level, and neither does a project whose `ntfy_url` or `ntfy_topic` is empty. A quiet run is therefore not proof of delivery: the phone is. Run it in a project's primary checkout, with the three notification settings and a stored token for that path:

```bash
cd /path/to/project
"$(sed -n '1s/^#!//p' "$(command -v sova)")" - <<'PY'
import asyncio
from pathlib import Path

from sova.config import load_config
from sova.ipc.notifications import NotificationKind, send_ntfy_notification

project = Path.cwd()
n = load_config(project).notification
asyncio.run(
    send_ntfy_notification(
        n,
        "SOVA test push",
        "Test message. Tap to open the Work page.",
        kind=NotificationKind.NEEDS_HUMAN,
        click_url=f"{n.link_base_url.rstrip('/')}/work",
        project_dir=project,
    )
)
PY
```

The push should appear within seconds under the topic with a bell, and tapping it should open that project's Work page. In a project with an empty `ntfy_url` or `ntfy_topic` the call returns without sending anything.

## Several Projects and a Second Machine

The multi-project hub (`sova server start --multi`) serves every project in `~/.config/sova/projects.json`, each with its own `.claude/sova.db`, at `/p/<slug>/`. Several settings still belong to one project or one folder:

| What | Scope | Where to set it |
|------|-------|-----------------|
| SOVA operator tools for Claude Code (`claude mcp add sova -- sova mcp serve --project /path/to/project`) | One registration per folder | Run it in each folder |
| Remote Control | One `claude remote-control` per folder, so one screen per folder | Serve only the folders you steer from the phone |
| `notification.ntfy_url`, `ntfy_topic`, `link_base_url` | The project's database | `sova config set` inside that project's checkout; `link_base_url` ends in that project's `/p/<slug>` |
| The ntfy token | A keychain entry named by the project path | The [Tokens](#tokens) script, once per project |
| `dashboard.proxy_origins`, `dashboard.proxy_allowed_logins` | The whole hub, read from the directory it was started in (the service's working directory) | Once, in that project |
| `server.scheduler_enabled` | One project, but only a single-project server runs the watch loop (from code: `scheduler_enabled and not multi_project`) | The hub never runs it; `sova_status` shows `scheduler_running: false` |
| Agent watchdog (`watchdog.*`, `memory_guard.*`) | One watchdog per registered project, from that project's own settings, acting on that project's agents only. Free memory is machine-wide, so under memory pressure each project with `memory_guard.runtime_action = "stop_newest"` stops its own newest run at the same moment: with N such projects one pressure event can stop N runs, so enable `stop_newest` on only the projects whose runs you can afford to lose | In each project; a project registered after the hub started gets none until the hub restarts |
| LLM provider and agent runtime | Each project, from its own settings: the hub builds one provider and one runtime per project (`provider_for()`, `runtime_for()`) for its in-process calls (supervisor planner, batch harden, oversight) and the commands it spawns, and rebuilds one when that project's `llm`, `agent.runtime` or `codex` settings change. A call that names no project uses the provider of the directory the hub was started in, which a settings save for that project also switches without a restart | In each project |
| Oversight agent (`oversight.*`) | The whole hub, read from the directory it was started in | Once, in that project |
| Watch loop, startup worktree cleanup (`dashboard.gc_on_startup`), cross-project metrics snapshot | A single-project server only; the hub logs `hub.component_skipped` for each at startup | Not available on the hub |
| `SOVA_DATABASE_URL` | Every project at once, ahead of each project's own `.claude/sova.db` | Leave it unset for the hub: with it set and more than one project registered, the hub refuses to start and registering another project is refused |

`sova config` reads the project from the current directory, so run it in the project you mean. A linked git worktree reads the primary checkout's settings (from `sova/config/root.py`), but the token entry is named by the path the code runs in: second test, a test push sent from a worktree reached the server and was rejected with 403, because no token is stored under the worktree's path. Run the token script and the test push from the primary checkout. Second test: pushes from two projects reached the same topic with the project name leading the body, and tapping one opened the Work page of the project whose `link_base_url` it carried (checked for one of the two).

### Start the Hub at Login

```bash
sova server install-service --type launchd --multi --workdir /path/to/project
launchctl load ~/Library/LaunchAgents/com.sova.server.plist
curl -s http://127.0.0.1:8111/api/health
```

From code; not yet tested on the test Macs:

- **Fixed working directory.** The plist runs `sova server start --multi` (no `--project`) with launchd's `WorkingDirectory` set to `--workdir`, or to your home directory when it is left off. The rows above that are read "from the directory it was started in" come from that directory, so name the project whose proxy settings, provider and oversight settings the hub should use. They no longer depend on which terminal ran the last restart. With the home directory, the hub keeps its log and settings in `~/.claude/`.
- **It replaces the single-project service.** Both use the label `com.sova.server` and the same file. Run `launchctl bootout gui/$(id -u)/com.sova.server` first, add `--force` to `install-service`, then load the new plist. Restart and stop it with the commands in [Start the Server at Login](#start-the-server-at-login). A restart also picks up projects registered since the last start.
- **One database per project.** Each project keeps its own `.claude/sova.db`. While `SOVA_DATABASE_URL` is set (an empty value counts as unset) and more than one project is registered, the hub exits at startup with a message saying so, and the dashboard refuses to register another project.
- **Linux.** `--type systemd --multi` writes the matching user unit: `WorkingDirectory=` instead of `--project`, and every registered project in `ReadWritePaths=`, since agents commit there. A project registered later is not writable until you re-run `install-service --multi --force` and restart. A registered project path with whitespace, a backslash or a percent sign is refused, as the single-project unit refuses its project path.
- **Health.** `/api/health` on the hub probes every registered project's database and lists the results under `databases`. One unreachable database, or a registered directory that no longer exists or has no `.claude/` (reported without opening a database there, and logged once), makes it `degraded` (HTTP 503), as does a registry file that cannot be read (`registry: "unreadable"`). A hub with no registered project probes nothing and reports `database: "ok"`.

### Check the Server from Outside

A server that has crashed, or stays up but `degraded`, sends no push about itself. A second launchd job can poll `/api/health` every five minutes and publish to ntfy when the answer is not 200 (`curl --fail-with-body` treats the 503 that `degraded` returns as a failure, as it does no answer at all, and keeps the JSON body, so the push says which check failed: a database, a dead scheduler, a stale scan, or on the hub an unreadable registry). It pushes once when the check starts failing and once when it recovers. Save it as `~/.local/bin/sova-health-check.sh` and make it executable:

```bash
#!/usr/bin/env bash
set -euo pipefail

# Publish to ntfy when the SOVA server's /api/health stops answering 200, and again when it recovers.
health_url="${SOVA_HEALTH_URL:-http://127.0.0.1:8111/api/health}"
ntfy_url="${NTFY_URL:?set NTFY_URL}"
ntfy_topic="${NTFY_TOPIC:?set NTFY_TOPIC}"
state_file="$HOME/Library/Caches/sova-health-check.failed"

publish() {
  local title="$1" body="$2" token
  # A missing or locked keychain item must be visible: under set -e it would otherwise end every run silently.
  token="$(security find-generic-password -s sova-health-check -a ntfy -w)" \
    || { logger -t sova-health-check "ntfy token missing from the keychain"; exit 1; }
  curl --silent --show-error --fail --max-time 10 \
    -H "Authorization: Bearer $token" -H "Title: $title" -H "Priority: high" \
    --data-binary "$body" "$ntfy_url/$ntfy_topic" > /dev/null
}

# --fail-with-body (curl 7.76+, which macOS ships) still fails on a 503 but keeps the "degraded" JSON.
if body="$(curl --silent --show-error --fail-with-body --max-time 10 "$health_url" 2>&1)"; then
  if [[ -f "$state_file" ]]; then
    publish "SOVA server recovered" "$health_url answers 200 again."
    rm -f "$state_file"
  fi
  exit 0
fi

if [[ ! -f "$state_file" ]]; then
  publish "SOVA health check failed" "$health_url: ${body:0:500}"
  touch "$state_file"
fi
```

Store the ntfy token for it once, with `security add-generic-password -s sova-health-check -a ntfy -w` (it prompts for the token with echo off). The item is created by `security` itself, so the script reads it back without a keychain prompt. Then add `~/Library/LaunchAgents/com.sova.health-check.plist`, with your home directory in the script path (launchd does not expand `~`) and the server address and topic from [Push Notifications with ntfy](#push-notifications-with-ntfy):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.sova.health-check</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>/Users/you/.local/bin/sova-health-check.sh</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>NTFY_URL</key>
        <string>https://mac.example.ts.net:8443</string>
        <key>NTFY_TOPIC</key>
        <string>sova</string>
    </dict>
    <key>StartInterval</key>
    <integer>300</integer>
    <key>RunAtLoad</key>
    <true/>
    <key>StandardErrorPath</key>
    <string>/tmp/sova-health-check.err</string>
</dict>
</plist>
```

Load it with `launchctl load ~/Library/LaunchAgents/com.sova.health-check.plist`, and test it by stopping the server: a push should arrive within five minutes, and another once the server is back. Run on the server's own Mac, the check cannot report that Mac sleeping or losing power, since it stops with it. Not yet tested on the test Macs.

### Use an Existing ntfy Server from Another Machine

One ntfy server can serve several machines, so a second machine needs no Docker and no second server. On the server, give each machine its own write-only account and its own topic, and let the phone's read-only account read that topic:

```bash
docker exec -it ntfy ntfy user add second-mac
docker exec ntfy ntfy access second-mac second-mac write-only
docker exec ntfy ntfy access phone second-mac read-only
docker exec ntfy ntfy token add --label second-mac second-mac
```

Store the token on the second machine with the [Tokens](#tokens) script, then point each project at the server's address and the new topic, and subscribe the phone's ntfy app to that topic:

```bash
sova config set notification.ntfy_url https://server-mac.example.ts.net:8443
sova config set notification.ntfy_topic second-mac
sova config set notification.link_base_url https://mac.example.ts.net/p/<slug>
```

Replace `<slug>` with the project's slug before you run the last line.

Check the server from the second machine before you configure anything. The first command should print `{"healthy":true,...}`; the second reads nothing and publishes nothing, and prints `403` when the server is private (`auth-default-access: "deny-all"`):

```bash
curl -s https://server-mac.example.ts.net:8443/v1/health
curl -s -o /dev/null -w '%{http_code}\n' "https://server-mac.example.ts.net:8443/second-mac/json?poll=1"
```

Every push from every machine goes through the server's machine, so a push arrives only while that machine is awake and reachable on the tailnet, whatever the state of the machine that sent it.

### Run the Hub by Hand

Without launchd (see [Start the Hub at Login](#start-the-hub-at-login)), the hub is whatever terminal started it. Closing that terminal, pressing Ctrl+C in it, or a reboot stops the dashboard, and it does not come back on its own; the table in [What Happens to Running Agents](#what-happens-to-running-agents) says what happens to agents. To restart it:

```bash
sova server status
curl -s http://127.0.0.1:8111/api/agents/active
```

Check that no agent is running, then run `sova server restart` **in the directory the hub was started in**. A restart stops the server and starts the new one in the terminal that runs the command, and it reads its configuration from that terminal's current directory (from `sova/cli/commands/server.py`). Leave `--project` off for the hub: it selects single-project mode, which starts the scheduler. Restarted from another project's folder, the hub should come back without the proxy settings above, so the tailnet address would answer 403 (from the code and the Dashboard guide's description of where the settings are read; not tested). To find the directory of a hub you did not start yourself, take the PID from `sova server status`:

```bash
lsof -a -p 12345 -d cwd -Fn
```

The line that starts with `n` is the directory.

## Tokens

Enter any token with a script that reads it with echo off and stores it in the login keychain, never through chat, a config file or a plist. For a `claude setup-token` token, use the script in [Claude Code: Headless and Second-Machine Login](ai-provider-setup-guide.md#claude-code-headless-and-second-machine-login).

The ntfy token goes where SOVA reads it: service `sova`, account `notification.ntfy_token:ntfy:<project path>`. Write it through SOVA's own Python, so the server can read it back without a keychain access prompt (a prompt on a Mac nobody is watching would block the push):

```bash
#!/usr/bin/env bash
set -euo pipefail

project="$(cd "${1:?usage: store-ntfy-token.sh /path/to/project}" && pwd -P)"
python="$(sed -n '1s/^#!//p' "$(command -v sova)")"

printf 'Paste the ntfy access token (input hidden): ' >&2
IFS= read -rs token
printf '\n' >&2
if [[ -z "$token" ]]; then
  echo "No token entered; nothing stored." >&2
  exit 1
fi

printf '%s' "$token" | "$python" -c '
import sys
from sova.llm.keyring_store import NTFY_SCOPE, NTFY_TOKEN_KEY, scoped_secret_name, set_secret
name = scoped_secret_name(NTFY_TOKEN_KEY, sys.argv[1], NTFY_SCOPE)
sys.exit(0 if set_secret(name, sys.stdin.read().strip()) else 1)
' "$project"
unset token
echo "Stored the ntfy token for $project." >&2
```

Run it as `store-ntfy-token.sh /path/to/project`, once for every project that sends pushes: the keychain entry is named by the project path. The `SOVA_NOTIFICATION_NTFY_TOKEN` environment variable, if set, overrides the stored token.
