# Deferred Consolidation Work

This file records the work that audit-driven consolidation passes chose not to do, so a pull request
that lands one slice of an issue does not close the rest by accident. It lists what is outstanding and
the behaviour changes of the slices that landed; the commit log and the pull request bodies describe the
rest.

`docs/HEALTH-AUDIT.md`, which the audit issues cite, is a transient local artifact: it is gitignored and
deleted once every issue it tracks is resolved. It cannot ride a pull request, so what must outlive it
is recorded here instead.

## Shared helpers, UTF-8 file I/O and layering (issue #1213)

The first slice landed process liveness, the shared fence stripper, closing-keyword pattern and duration
helpers, `resolve_project_dir()`, explicit UTF-8 text I/O with the `file-encoding` invariant, unreadable
handoff reporting, the structlog migration, timezone-aware awareness timestamps and the event-loop fixes.
The issue stays open for the items below. A pull request for the first slice references the issue as
`Part of #1213`, never `Closes #1213`.

### Deferred items

| Item | Why it is deferred | What closes it |
|------|--------------------|----------------|
| Import-layering contract in CI with a baseline (`utils < config < db < llm/git/ipc < core < roles < supervisor < dashboard < cli`) | Adding `import-linter` is a dev dependency change, which AGENTS.md lists under "Ask first" | Explicit approval to add the dependency, a layers contract in `pyproject.toml`, the current violations (the module-scope `sova.dashboard.services` imports in `supervisor/gates/budget.py`, `supervisor/planner.py`, `supervisor/progression.py`, `supervisor/watchdog.py` and `supervisor/pr_monitor.py`) listed as the allowed baseline, and a CI step |
| Hoist the function-level `load_config` and `get_session` imports that have no import cycle | Only meaningful once the layering contract exists to keep them hoisted | The same change as the contract, one import at a time, each checked for a real cycle first |
| Move the Codex runtime from `sova/ipc/runtime.py` to `sova/ipc/codex_runtime.py` | A mechanical but large split (about 700 lines) that deserves its own reviewable change | A pure move with no behaviour change, `runtime.py` keeping the `AgentRuntime` ABC and the registry, and re-exports only where an importer cannot be updated |
| Consolidate `feed_service`, `work_state`, `work_verdict` and `pr_service` into a `sova/workflow/` package | A package move touching many importers and patch targets | A separate change that migrates the importers and deletes the old paths |
| Split the remaining god modules: `llm/client.py`, `dashboard/services/agent_recovery.py`, `core/workflow.py`, `dashboard/services/setup_service.py`, `git/worktree.py` | Each split needs its own seam analysis | One change per module |
| Decide the permanent status of `dashboard/services/control_service.py` | It is a thin re-export facade with 36 patch targets in tests | Either document it as the official API or migrate the importers and delete it |

### Behaviour changes in the first slice

These are intended, and each is pinned by a test, but the issue's "no behaviour change" constraint
means they are recorded here rather than left implicit.

- `is_process_alive()` treats a `PermissionError` as alive (the process exists, under another user).
- `CLOSING_KEYWORDS` covers all nine GitHub closing forms, so a PR body that writes `Fix #42` is a
  closing reference to every reader. A numeric issue needs the `#`; a Jira key does not.
- In single-project mode `GET /api/roles/commands` now reports the default project's installed commands
  and drift metadata, where it used to serve the canonical catalog alone. With no project selected it
  still ignores the working directory.
- A handoff file that parses as a JSON object but fails `DashboardHandoff` validation is still rendered
  by the dashboard, and is now also listed in `schema_invalid_handoffs` and named by the sidebar banner,
  because the supervisor and the auto-handoff skip it.
- The project registry refuses to rewrite a file that exists but cannot be read, and the dashboard
  reports that as a 409 naming the file. Writes go through `write_text_atomic()`.

### Known gaps in the first slice

- `extract_json()` still has its own search for a fence in the middle of prose, or for prose after the
  closing fence. A response that is wrapped in one fence goes through `strip_code_fences()`, so the two
  helpers agree on every wrapper shape; the prose-tolerant search is the one deliberate difference,
  because `strip_code_fences()` does not tolerate text outside its fence.
- A follow-up issue for the deferred items above is not yet filed.
