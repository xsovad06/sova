# Agent Spawn Troubleshooting Guide

When a SOVA agent fails to start, you'll see:
- **Run status**: `failed`
- **Cost**: `$0.0000` (no LLM was invoked)
- **Steps**: None recorded
- **Exit code**: 1

This guide helps diagnose and fix these early-stage failures.

## Step 1: Check Agent Output

Agent output is logged to `.claude/agent-output/{run_id}.stderr` and `.claude/agent-output/{run_id}.stdout`.

```bash
# Find the run ID in the dashboard or from git log
cat .claude/agent-output/2235.stderr
```

## Common Errors

### 1. `ModuleNotFoundError: No module named 'sova'`

**Cause**: Python was upgraded or the sova package is not installed.

**Fix**:
```bash
python3 -m pip install --user --break-system-packages -e /path/to/sova
```

Verify installation:
```bash
python3 -c "from sova.cli.app import app; print('OK')"
```

---

### 2. `Skipping setting 'X': invalid JSON value`

**Cause**: Database config has non-JSON values.

**Diagnosis**:
```bash
sqlite3 .claude/sova.db "SELECT key, value FROM project_settings WHERE json_valid(value) = 0;"
```

**Fix**: See [Configuration Troubleshooting](troubleshooting-config.md#config-load-errors-skipping-setting-x-invalid-json-value).

---

### 3. `No such column: project_dir` or other database errors

**Cause**: Database schema mismatch (old SOVA version, incomplete migration).

**Check**:
```bash
sqlite3 .claude/sova.db ".schema project_settings"
```

**Fix**:
```bash
# Option 1: Clear and reinit (keeps history)
sqlite3 .claude/sova.db "DELETE FROM project_settings;"
sova setup /path/to/project

# Option 2: Full reset (loses all config history)
rm .claude/sova.db
sova setup /path/to/project
```

---

### 4. `Permission denied` or other OS errors

**Cause**: Working directory, venv, or Git worktree has permission issues.

**Check**:
```bash
ls -ld . .claude .claude/worktrees
git status
```

**Fix**:
```bash
# Fix ownership
sudo chown -R $USER:$(id -gn) .

# Or reset worktrees
rm -rf .claude/worktrees/*
```

---

### 5. `Failed to acquire lock` (SQLite)

**Cause**: Another process is holding a database lock (often dashboard startup).

**Check**:
```bash
lsof .claude/sova.db
```

**Fix**:
```bash
# Wait for other process to finish, or
pkill -f "sova server"
# Then retry
```

---

## Step 2: Check Environment

Verify prerequisites are installed:

```bash
# Python version
python3 --version  # Should be 3.10+

# SOVA is in PATH
which sova

# Git is available
git --version

# GitHub CLI is available and authenticated
gh auth status

# Claude Code CLI (if using non-pipeline roles)
which claude
claude --version
```

---

## Step 3: Verify Configuration

Check that config loads correctly:

```bash
# Try loading config directly
python3 -c "from sova.config import load_config; cfg = load_config(); print(f'Model: {cfg.agent.model}')"

# Check database integrity
sqlite3 .claude/sova.db "PRAGMA integrity_check;"

# List all settings
sqlite3 .claude/sova.db "SELECT key, value FROM project_settings ORDER BY key;"
```

---

## Step 4: Run with Increased Diagnostics

```bash
# Run with debug logging
SOVA_DEBUG=1 sova run 42

# Or check logs
sova server status
tail -100 ~/.local/share/sova/logs/  # Check for log directory
```

---

## Step 5: Test the Subprocess Directly

If dashboard spawning fails but `sova run 42` works from the terminal, the issue is likely:
- Dashboard process manager (check `.claude/agent-output/` for actual error)
- Worktree creation/navigation
- CWD/project resolution in subprocess

```bash
# Manually trigger what the dashboard does
sova run 42 --role developer --force

# Watch the output
tail -f .claude/agent-output/*.stdout
```

---

## Escalation Path

If the above steps don't resolve the issue:

1. **Collect diagnostics**:
   ```bash
   sova doctor  # Full system check
   cat .claude/agent-output/{run_id}.{stdout,stderr}
   sqlite3 .claude/sova.db "SELECT key, value FROM project_settings;" > /tmp/config.txt
   git log --oneline | head -20 > /tmp/recent-commits.txt
   ```

2. **File an issue** at [github.com/xsovad06/sova/issues](https://github.com/xsovad06/sova/issues) with:
   - Error message from agent output
   - Output of `sova doctor`
   - Recent config changes (if any)
   - Python version
   - Platform (macOS/Linux/Windows)
