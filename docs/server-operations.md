# Server Management

SOVA can run as an always-on service (dashboard + scheduler daemon):

```bash
# Start the server
sova server start --host 127.0.0.1 --port 8111

# Restart the server
sova server restart

# Stop the server
sova server stop

# Check server status
sova server status

# View activity summary
sova server digest --hours 24

# Install as system service (systemd on Linux, launchd on macOS)
sova server install-service --type systemd --project /path/to/project

# Or install the multi-project hub (every registered project), started from a fixed working directory
sova server install-service --type launchd --multi [--workdir ~/sova-hub]
```

After installing as a service:

```bash
# systemd
systemctl --user enable sova-server
systemctl --user start sova-server
systemctl --user status sova-server

# launchd
launchctl load ~/Library/LaunchAgents/com.sova.server.plist
launchctl start com.sova.server
launchctl list | grep sova
```

The generated plist sets `PATH` to the directories of `sova`, `claude`, `gh`, `git`, `codex`, `terminal-notifier` and `uvx` as found at install time, then launchd's default (`/usr/bin:/bin:/usr/sbin:/sbin`, which has none of the first three); re-run `install-service --force` after moving one. For a Mac left on to run SOVA unattended (sleep settings, login and FileVault, what happens to agents across restarts and reboots, phone access), see [Always-On Mac Setup](always-on-mac-setup.md).

The systemd unit is a user unit (`systemctl --user`) and lists the resolved project
directory in `ReadWritePaths`, since agents write `{project}/.git` as well as
`{project}/.claude`; `~/.claude.json` and `~/.codex` are listed with a leading `-`
(the CLIs' own state, optional on a host without one). A project path containing
whitespace is refused: systemd splits those directives on whitespace, so adapt
`deploy/sova-server.service` by hand instead. The unit is `Type=notify` with
`WatchdogSec=60`: the server sends `READY=1` once startup recovery has finished and uvicorn has bound the port, then pings
the watchdog every 15s on a fixed schedule while the event loop answers (stdlib `sd_notify`, a no-op off
systemd), so a wedged server is restarted by systemd (`WatchdogSignal=SIGTERM`, no core dump). The ping is
deliberately not gated on `/api/health` staying 200 or on the database probe: a tracker outage, an expired
token or an unreachable database turns that `degraded` (HTTP 503), and a restart cannot fix any of them.

**Restarts keep agents running.** Agents run in their own session with output
redirected to `.claude/agent-output/<run_id>.stdout`, and the unit sets
`KillMode=process`, so `systemctl --user restart sova-server` stops only the server.
On startup the new server adopts each live agent it launched (PID alive and process
creation time matching the run's `started_at`) and keeps managing it; output capture
resumes from the end of the agent's `.stdout` file, so what the agent wrote while no
server was running stays in that file rather than in the dashboard log. Adoption is a
claim held with a lock file (`.claude/agent-output/<run_id>.adopt.lock`), so two servers on
one project database cannot both manage, finalize and hand off the same run. It only reads
the agent's worktree and never repairs or recreates it. A dead PID is recorded from what the
run left behind (a PR handoff, or for a command run the final `result` line in its `.stdout`:
`done` or `failed`, else `interrupted`); a live PID that is not the run's process (PID reuse)
is marked `interrupted`. Neither is ever signalled. An adopted agent's exit code is not available, so it is derived from the run's
status, its handoff or the final `result` line in its `.stdout`; when none of them says
how it ended, the run is recorded `interrupted` rather than `failed`. The previous server's pre-run baseline is gone, so an adopted
`address-pr`, `rearrange-commits` or `integrate-pr` run is not diff-guarded (no silent-revert or pin-downgrade
check; `adopt.diff_guard_baseline_unavailable` is logged). An `address-pr` command without its pre-run PR head
baseline is recorded as interrupted rather than being accepted from a synchronized branch or output text.
Set `recovery.kill_orphans = true` to kill identity-matched orphans at
startup instead. Agents are left running only when a service manager runs the server
(systemd, or the launchd plist): because of `KillMode=process`, `systemctl --user stop` also
leaves agents running until the next start adopts them, so stop them from the dashboard first,
or kill them by hand. An interactive `sova server start` or `sova dashboard` (Ctrl+C) still
stops its agents on shutdown.

*Unverified on Linux:* the Claude CLI rewrites `~/.claude.json` by
writing a temp file and renaming it over the original. A single-file read-write bind
mount may not allow that rename, and sibling files at the home root (backups, locks)
stay read-only under `ProtectHome=read-only`. If agents cannot save state under the
unit, relax `ProtectHome` for it.

On macOS, `install-service --type launchd` also writes
`~/.config/sova/sova-server.newsyslog.conf` and prints a `sudo cp` command for it:
launchd never rotates its own `StandardOutPath`/`StandardErrorPath`, and newsyslog is
what does. It holds those descriptors open for the life of the process, so each entry
sends SIGHUP to the server's PID after rotation. The server reopens the files named by
`SOVA_SERVER_LOG` and `SOVA_SERVER_ERR_LOG` (set in the generated plist) in place: no
restart, so running agents are undisturbed. The server takes over SIGHUP only when
one of those variables is set (a service manager owns the log files); an interactive
`sova server start` keeps the default, so closing its terminal still stops it. When
stdout is a terminal, the reopen is skipped with a log line. SIGHUP is ignored while the
server is starting or stopping, so a rotation in that window is skipped, never fatal. SOVA's own
`.claude/sova.log` is rotated in-process and needs none of this.

**Health monitoring**: The server exposes health and digest endpoints for external monitoring:
- `GET /api/health`: `status` (`ok` or `degraded`), `database` (`ok` or `unreachable`, a probe bounded to 2s), uptime, scheduler state, active agent count, package version and git SHA. Answers HTTP 503 (same JSON body) when `degraded`: the watch loop's last completed scan is older than three times `watch.interval_idle` while no task is in flight, the scheduler is enabled on a single-project server and its watch loop crashed or is still missing 30s after it was started, or the database probe failed. On the multi-project hub, `databases` holds one probe per registered project (a removed project directory, or one with no `.claude/`, is `unreachable` without a probe), `database` is `ok` only when every one is (and with no project registered), and `registry` is `unreadable`, making it `degraded`, when the registry file cannot be read. `code_sha` (full HEAD when the server started), `checkout_sha` (full HEAD now, read from `.git` and cached for 5s) and `restart_required` (both known and different) report a server running older code than its checkout; informational only, it never changes `status` or the HTTP code, and every dashboard page shows a restart banner while it is true. An `ImportError` escaping any route answers 503 with a detail telling the operator to restart.
- `GET /api/scheduler/health`: uptime, scan count, error count, agent slots, DB status
- `GET /api/scheduler/digest?hours=24`: tasks completed/failed, cost summary

**Log rotation**: The server and every `sova run` write `.claude/sova.log`, each through its own rotating handler built from the same two settings (10 MB max, 5 backups by default), so they agree on when to roll it. The server reads them at startup, so a change takes effect on the next restart. Configure via:

```toml
[server]
log_max_bytes = 10485760    # 10 MB
log_backup_count = 5
```
