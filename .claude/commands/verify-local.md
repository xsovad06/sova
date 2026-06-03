---
name: verify-local
description: Run a full smoke test of SOVA CLI, install, sync, adapters, and dashboard on the local machine.
user-invocable: true
---

# Verify Local

Run a 6-phase smoke test to verify SOVA works end-to-end on this machine. Exercises real CLI commands, creates a temp project, checks adapter creation, and briefly starts the dashboard.

Context: $ARGUMENTS

## Instructions

### Phase 1: CLI Entrypoints

Verify the CLI loads and new subcommands are registered:

```bash
sova --help
sova --version
sova doctor --help
sova commands sync --help
```

All four must exit 0. If any fails, report which one and stop.

### Phase 2: sova doctor

Run the doctor on the SOVA project itself:

```bash
sova doctor --project .
```

Expected: exit 0, all required checks pass (Python, git, gh, claude). Report the output table.

### Phase 3: Fresh Project Install

Test the full install pipeline on a disposable temp project:

```bash
tmp=$(mktemp -d)
git init "$tmp/smoke-test"
```

Then run:
```bash
sova install "$tmp/smoke-test"
```

Verify these artifacts exist:
- `$tmp/smoke-test/sova.toml`
- `$tmp/smoke-test/.claude/commands/` (non-empty)
- `$tmp/smoke-test/.claude/agent-memory/MEMORY.md`
- `$tmp/smoke-test/.claude/sova.db`

Run doctor on the new project:
```bash
sova doctor --project "$tmp/smoke-test"
```

Clean up:
```bash
rm -rf "$tmp/smoke-test"
```

Report PASS if all artifacts exist and install exits 0.

### Phase 4: Command Sync

Test multi-project command synchronization:

```bash
sova commands sync
```

If registered projects exist: verify it reports per-project results and a summary line.
If no projects registered: verify it prints the "no projects" message and exits 0. Report as PASS (expected on fresh machines).

### Phase 5: Adapter Factory

Verify the adapter factory creates both adapter types correctly:

```bash
python3 -c "
from sova.config.models import ProjectConfig, TaskSourceConfig
from sova.adapters import create_adapter

# GitHub adapter
cfg = ProjectConfig(github_repo='test/repo', github_user='testuser')
a = create_adapter(cfg)
assert a.__class__.__name__ == 'GitHubAdapter', f'Expected GitHubAdapter, got {a.__class__.__name__}'
assert a.github_user == 'testuser'
print('  GitHub adapter: OK')

# Jira adapter
cfg = ProjectConfig(task_source=TaskSourceConfig(
    type='jira',
    jira_base_url='https://test.atlassian.net',
    jira_email='test@example.com',
    jira_api_token='test-token',
    jira_project_key='TEST',
))
a = create_adapter(cfg)
assert a.__class__.__name__ == 'JiraAdapter', f'Expected JiraAdapter, got {a.__class__.__name__}'
assert a.project_key == 'TEST'
print('  Jira adapter: OK')

# Unknown type raises ValueError
try:
    ts = TaskSourceConfig()
    ts.type = 'unknown'
    create_adapter(ProjectConfig(task_source=ts))
    assert False, 'Should have raised ValueError'
except ValueError as e:
    assert 'Unknown adapter type' in str(e)
    print('  Unknown type rejection: OK')

print('  All adapter checks passed')
"
```

Exit 0 = PASS.

### Phase 6: Dashboard Smoke Test

Start the dashboard, verify it responds, then shut it down:

```bash
sova dashboard --project . &
DASH_PID=$!
```

Wait for startup (max 5 seconds), then check:
```bash
sleep 3
curl -sf http://localhost:8111/api/overview > /dev/null 2>&1
CURL_EXIT=$?
kill $DASH_PID 2>/dev/null
wait $DASH_PID 2>/dev/null
```

If curl exits 0: PASS. If not, retry once after 2 more seconds. If still failing: FAIL.

If port 8111 is already in use (dashboard already running), just curl it without starting a new one.

### Report

Print a summary table:

```
## Verify Local -- Results

Phase 1: CLI entrypoints ............ PASS/FAIL
Phase 2: sova doctor ................ PASS/FAIL
Phase 3: Fresh project install ...... PASS/FAIL
Phase 4: Command sync ............... PASS/SKIP (N projects)
Phase 5: Adapter factory ............ PASS/FAIL
Phase 6: Dashboard smoke test ....... PASS/FAIL

N/6 phases passed.
```

If any phase fails, list the failure details below the table.

## Rules

- NEVER use emojis in any output
- Clean up all temp files and background processes, even on failure
- Do NOT modify the SOVA project or its config -- this is read-only verification
- If a phase fails, continue to the next phase (report all results, not just first failure)
- Phase 4 (sync) is SKIP, not FAIL, when no projects are registered
