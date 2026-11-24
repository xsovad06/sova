# Always-On Mac Setup

How to keep SOVA running on a Mac that stays on at a desk, and supervise it from a phone. This guide links to the existing reference material rather than repeating it.

Tested between 2026-11-21 and 2026-11-24 on a MacBook Pro (Apple silicon, on AC power, FileVault on) with macOS 26.7.3, Tailscale 1.104.6, ntfy 2.38.1 and SOVA 0.1.0. Claude Code was 2.1.318 at the start and updated itself to 2.1.331 during the testing; the Claude Code behavior below was checked again on 2.1.331, and the launchd Remote Control test ran on it. A statement that comes from code or vendor documentation rather than a test on that Mac says so.

## Contents

1. [Keep the Mac Awake](#keep-the-mac-awake)
2. [Start the Server at Login](#start-the-server-at-login)
3. [What Happens to Running Agents](#what-happens-to-running-agents)
4. [Steer Sessions from the Phone with Remote Control](#steer-sessions-from-the-phone-with-remote-control)
5. [Reach the Dashboard Privately with Tailscale](#reach-the-dashboard-privately-with-tailscale)
6. [Push Notifications with ntfy](#push-notifications-with-ntfy)
7. [Tokens](#tokens)

## Keep the Mac Awake

Agents make progress only while the Mac is awake: sleep suspends every process, the server and its agents included. On power, let the display sleep but not the Mac:

```bash
sudo pmset -c sleep 0
pmset -g custom
```

The second command should list `sleep 0` under "AC Power". `-c` changes only the power-adapter profile, so the Mac still sleeps on battery, and `displaysleep` can keep its value. The same setting is in System Settings, Battery, Options: "Prevent automatic sleeping on power adapter when the display is off". Tested: turning it on sets `sleep 0` under "AC Power" and leaves the battery profile at `sleep 1`.

**Closing the lid.** `sleep 0` does not keep a closed MacBook awake by itself. Tested on power with no external display: closing the lid put the Mac to sleep within seconds (`pmset -g log` showed "Entering Sleep state due to 'Clamshell Sleep'"), and sleep suspends every process (the server, any agents, Remote Control) until the lid is opened again; the Mac woke briefly on its own for network traffic, but nothing ran continuously. Tested with an external display connected and power plugged in: with the lid closed for about a minute, the external display kept showing the desktop and `pmset -g log` recorded no sleep, and `ioreg` reported `AppleClamshellCausesSleep = No` while the display was attached. So an always-on MacBook needs either the lid open, or the lid closed with an external display and power connected. Whether it stays awake for hours, or after the display is switched off or unplugged, was not tested.

## Start the Server at Login

```bash
sova config set server.scheduler_enabled false
sova server install-service --type launchd --project /path/to/project
launchctl load ~/Library/LaunchAgents/com.sova.server.plist
curl -s http://127.0.0.1:8111/api/health
```

Run these in the project directory. The first line is optional and stops the server from starting issues on its own (see "One project, and the scheduler" below). The [README's Server Management section](../README.md#server-management) covers the service commands, log rotation with newsyslog and health monitoring. What matters for an unattended Mac:

- **It starts at login, not at boot.** The plist is a LaunchAgent, so launchd starts it in your login session. With FileVault on, the disk stays locked after a restart until someone logs in at the login window, so after a reboot (an update, a power cut) SOVA stays down until you log in once. This guide does not cover auto-login or a LaunchDaemon.
- **PATH.** launchd starts a LaunchAgent with `PATH=/usr/bin:/bin:/usr/sbin:/sbin`, which misses `~/.local/bin` (where `claude` installs) and `/opt/homebrew/bin` (`gh`, Homebrew's `git`). The generated plist sets `PATH` to the directory of each of `claude`, `node` (an npm-installed `claude` needs it), `gh`, `git`, `codex`, `terminal-notifier` and `uvx` found at install time, then `sova`'s own directory, then that default. Re-run `install-service --force` and reload after moving one of them, for example after a Python upgrade moves `sova`. Under a plist generated before SOVA added this, `sova doctor` fails `claude CLI`, `gh CLI`, `llm provider` and `agent runtime`, and `git` resolves to Apple's `/usr/bin/git`.
- **Credentials.** The service runs in your login session, so `claude` uses your `claude auth login` credential from the login keychain and `gh` its own keyring login. Put no token in the plist. On the test Mac a server started from the generated plist spawned a Claude agent that ran `gh pr list`, `gh issue list` and `gh api graphql` and finished `done`.
- **One project, and the scheduler.** The plist runs `sova server start --project <dir>`, which is single-project mode: the dashboard is at `/work` and the phone page at `/phone`, and the multi-project hub's `/p/<slug>/` paths return 404. Single-project mode also runs the scheduler's watch loop when `server.scheduler_enabled` is on (the default). On the test Mac it picked the top `agent:ready` issue within a minute of starting, waited the 30-second veto window and started a developer run. To start runs only yourself, set `server.scheduler_enabled` to `false` before loading the plist (it is read at startup). Running the multi-project hub (`sova server start --multi`) under launchd needs a hand-edited plist and is not covered here.

To restart the service, run `launchctl kickstart -k gui/$(id -u)/com.sova.server`; to stop it until the next login, `launchctl bootout gui/$(id -u)/com.sova.server`.

## What Happens to Running Agents

| Event | Agent started from the dashboard, phone page or MCP tools | Run the watch loop started |
|-------|------------------------------------------------------------|----------------------------|
| Service restart (`launchctl kickstart -k`, or launchd restarting a crashed server) | Keeps running; the next server adopts it and records how it ends (tested: an adopted command run finished `done`) | Ends with the server, since the watch loop runs it inside the server process; the next server's startup recovery marks it `interrupted` (tested) |
| Interactive `sova server start` or `sova dashboard` exits (Ctrl+C) | Stopped on the way out (from code: `agents_outlive_server()` is false without a service manager) | Stopped |
| Reboot or power loss | Killed; at the next start, recovery records it from what it left behind (a PR handoff, a final `result` line) or as `interrupted` (from code) | Marked `interrupted` |
| Sleep | Paused with every other process until the Mac wakes (a lid-close sleep was tested; sleep suspends every process) | Paused |

The adoption rules (process identity check, the adopt lock file, what is not diff-guarded after adoption) are in the [README's Server Management section](../README.md#server-management).

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

- **Push notifications** are two switches in `/config`: "Push when Claude decides" and "Push when actions required" (both present under these names, tested). The phone also needs notifications allowed for the Claude app in the iPhone's Settings; with that off, the first test produced no push at all. Tested with "actions required" on: a question put to the session from the phone, with the phone locked, produced a push from the Claude app, but it appeared only after the phone was unlocked, not on the locked screen. The cause was not determined; the first things to check are the app's Lock Screen setting under Settings, Notifications, Claude, and any Focus mode. A permission prompt as the trigger was not tested.
- **The SOVA operator tools.** Flags given before `remote-control` (such as `--mcp-config`) are not passed to the sessions server mode spawns, and its own `--settings` flag needs Claude Code 2.1.321 or later. Register `sova mcp serve` with `claude mcp add` instead, as the [README describes](../README.md#registering-the-mcp-server-in-claude-code), so every session started in that directory loads it.

Tested with a session started by `claude remote-control --name rc-terminal` in a terminal: it appeared in the Claude iPhone app, and a call to the registered `sova_status` and `sova_pending` tools came back with live data after approving the tool on the phone.

**Usage limits.** Not measured. `/usage` read 85% before and after a few short prompts, which is too coarse to show a difference. Claude runs on the Mac under your login, so these calls are ordinary Claude Code usage; Anthropic's Remote Control page does not say so either way.

**Spawn mode.** Starting `claude remote-control` stores `remoteControlSpawnMode` for the folder in `~/.claude.json`; on the test Mac it is `worktree`, so each session started from the phone gets its own git worktree. A second `claude remote-control` for a folder already served is refused ("This folder is already served by a terminal `claude remote-control` on this device").

**Under launchd, with no terminal.** Tested on Claude Code 2.1.331: a LaunchAgent running `claude remote-control --name rc-launchd` in an already-trusted folder, with the same `PATH` the generated SOVA plist uses and no token in its environment, connected ("Connected", then "Ready", capacity 0/32). A new session started from the iPhone app against that environment answered, and the server created an isolated git worktree for it under `.claude/worktrees/bridge-<session id>` on a `worktree-bridge-<session id>` branch. Things to know:

- **The first launch in a folder needs a terminal**, as described above: run `claude remote-control` there once before handing it to launchd. The launchd test used a folder already trusted; an untrusted one was not tested.
- **A stopped session can be handed back.** After a `claude remote-control` in a terminal was stopped with Ctrl+C, the launchd process kept being handed that same ended session at start, and its pre-created session failed immediately ("Session failed: Process exited with error"), so nothing appeared on the phone. The process stayed up, and a new session started from the phone worked. If the phone shows nothing, start a new session there instead of waiting for the pre-created one.
- **Remote Control can change the repository's git config.** Creating the session worktree set the repository's `core.hooksPath` to the absolute path of the primary checkout's `.githooks`. SOVA's own repository expects the relative `.githooks`, and `sova doctor` fails its `git hooks` row until `git config core.hooksPath .githooks` is run again. Check it after a Remote Control session in a repository that uses `.githooks`.
- **Remove what a session leaves behind.** The session's worktree is locked while its session runs. After stopping it, `git worktree remove -f -f <path>` and `git branch -D <branch>` clean up an unused one.
- **Offline behavior is from the docs.** The roughly 10-minute exit when offline was not reproduced here; keep the restart loop (or launchd's `KeepAlive`, which was not tested) in front of `claude remote-control`.

## Reach the Dashboard Privately with Tailscale

The dashboard stays bound to `127.0.0.1`; Tailscale Serve publishes it to your own tailnet devices only, with Funnel off. Setup, the two settings it needs and the tailnet login check are in the [README's Remote Access over Tailscale section](../README.md#remote-access-over-tailscale); the security model is in [docs/security-guidelines.md](security-guidelines.md), "Dashboard Security Model".

On the test Mac, `tailscale serve status` reports:

```text
https://mac.example.ts.net (tailnet only)
|-- / proxy http://127.0.0.1:8111
```

with `dashboard.proxy_origins` set to that origin and `dashboard.proxy_allowed_logins` to the owner's Tailscale login. The phone page is at `/phone` on a single-project server (including the launchd service above) and at `/p/<slug>/phone` on the multi-project hub. Opening `https://mac.example.ts.net/p/<slug>/phone` in the iPhone's browser showed the page (handoffs waiting on you, running agents, a start field), tested on 2026-11-24.

**If the phone shows `ERR_NAME_NOT_RESOLVED`**, Tailscale is switched off on the phone: it cannot look up the machine's tailnet name. `tailscale status` on the Mac then lists the phone as offline. Turn Tailscale on in the phone's Tailscale app (the Settings app's VPN switch follows) and reload. Check this first when the page stops loading.

## Push Notifications with ntfy

SOVA pushes "needs you" and "failed" events to a self-hosted ntfy server; the settings and the reasons to self-host rather than use the public ntfy.sh are in the [README's Notifications section](../README.md#notifications). This setup was tested on 2026-11-24 with ntfy 2.38.1 and the ntfy iPhone app.

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

**The phone.** With Tailscale connected, install the ntfy app, add the user `phone` for `https://mac.example.ts.net:8443` under Settings, Users, then subscribe to the topic `sova` with "Use another server" set to that address. Tested: pushes arrived as iPhone notifications, and tapping one opened the Work page. Delivery with the app closed and the phone locked for a while was not tested separately.

**What a push looks like.** The title names the event and the body holds the project and the detail, for example "Developer failed #1557" over "sova | Step 'validate' failed: 2 tests failed". A symbol before the title marks the kind: a bell for "needs you", a warning sign for a failure, a check mark for a finish. On iOS every push shows the ntfy app's icon; ntfy's per-message `icon` URL did not change it (tested).

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

Run it as `store-ntfy-token.sh /path/to/project`. The `SOVA_NOTIFICATION_NTFY_TOKEN` environment variable, if set, overrides the stored token.
