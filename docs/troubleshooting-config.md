# Configuration Troubleshooting

## Agent Spawn Failures (`ModuleNotFoundError: No module named 'sova'`)

**Symptom**: Runs fail immediately with exit code 1 and no steps recorded. Agent stderr shows:
```
Traceback (most recent call last):
  File "/Users/.../Python/3.X/bin/sova", line 3, in <module>
    from sova.cli.app import app
ModuleNotFoundError: No module named 'sova'
```

**Root Cause**: Python was upgraded (e.g., 3.13 → 3.14) but SOVA was not reinstalled for the new version.

**Fix**:
```bash
python3 -m pip install --user --break-system-packages -e .
```

The `sova` command is installed to the system Python's bin directory, but the package itself must be installed in the site-packages. When Python upgrades, the old site-packages is replaced, and SOVA must be reinstalled.

---

## Config Load Errors (`Skipping setting 'X': invalid JSON value`)

**Symptom**: Commands print "Skipping setting 'agent.model': invalid JSON value" and continue or fail.

**Root Cause**: Database config values must be stored as valid JSON. Common mistakes:
- String without quotes: `claude-sonnet-5` (should be `"claude-sonnet-5"`)
- Unquoted array: `[item1, item2]` (should be `["item1","item2"]`)

**Diagnosis**:
```bash
sqlite3 .claude/sova.db "SELECT key, value FROM project_settings;"
```

Look for values that are NOT valid JSON (use `json_valid()` to check):
```bash
sqlite3 .claude/sova.db "SELECT key, value FROM project_settings WHERE json_valid(value) = 0;"
```

**Fix Examples**:

String values need quotes:
```bash
sqlite3 .claude/sova.db "UPDATE project_settings SET value = '\"claude-sonnet-5\"' WHERE key = 'agent.model';"
sqlite3 .claude/sova.db "UPDATE project_settings SET value = '\"github\"' WHERE key = 'task_source.type';"
sqlite3 .claude/sova.db "UPDATE project_settings SET value = '\"claude-code\"' WHERE key = 'agent.runtime';"
```

Arrays need proper JSON format:
```bash
sqlite3 .claude/sova.db "UPDATE project_settings SET value = '[\"claude-opus-5\",\"claude-sonnet-5\"]' WHERE key = 'agent.fallback_models';"
```

Numbers don't need quotes:
```bash
sqlite3 .claude/sova.db "UPDATE project_settings SET value = '20.0' WHERE key = 'agent.max_budget';"
sqlite3 .claude/sova.db "UPDATE project_settings SET value = 'true' WHERE key = 'enabled';"
```

---

## Database Corruption or Missing Table

**Symptom**: Commands fail with "no such column" or "no such table" errors.

**Check DB integrity**:
```bash
sqlite3 .claude/sova.db "PRAGMA integrity_check;"
```

**Reset config** (keep database history):
```bash
sqlite3 .claude/sova.db "DELETE FROM project_settings;"
sova setup /path/to/project  # Re-runs setup wizard
```

**Full reset** (loses all config history):
```bash
rm .claude/sova.db
sova setup /path/to/project
```

---

## Configuration Tiers (Priority Order)

1. **Environment variables** (highest priority): `SOVA_GITHUB_REPO=owner/repo`
2. **Database** (`.claude/sova.db`): set via `sqlite3` or dashboard Settings
3. **TOML file** (legacy): `sova.toml` (still supported but deprecated)
4. **Defaults**: built into `sova/config/models.py`

To override a setting, use the environment variable (case-insensitive, replace dots with underscores):
```bash
export SOVA_AGENT_MODEL="claude-opus-5"
sova run 42
```

---

## Config Migration (TOML → Database)

When you run `sova install /path/to/project`:
1. If `sova.toml` exists and the database is empty, it auto-migrates
2. All TOML values are converted to JSON and stored in the database
3. The `sova.toml` file is left unchanged (for reference)

To force a fresh install:
```bash
rm .claude/sova.db
sova install /path/to/project
```

To view merged config (TOML + DB + env):
```bash
python3 -c "from sova.config import load_config; import json; print(json.dumps(load_config().model_dump(), indent=2))"
```
