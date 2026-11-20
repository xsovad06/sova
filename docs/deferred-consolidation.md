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
The second slice landed the import-layering contract (`import-linter`, `[tool.importlinter]` in
`pyproject.toml`, run by `make lint` and CI; layout in `docs/architecture-deep-dive.md`, "Target layout"),
the hoist of the lazy `load_config` and `get_session` imports that guard no cycle, the move of the Codex
runtime to `sova/ipc/codex_runtime.py`, and an `Allowed imports:` line in every `sova/` `__init__.py`.
The issue stays open for the items below. A pull request for a slice references the issue as
`Part of #1213`, never `Closes #1213`.

### Deferred items

| Item | Why it is deferred | What closes it |
|------|--------------------|----------------|
| Consolidate `feed_service`, `work_state`, `work_verdict` and `pr_service` into a workflow package | A package move touching many importers and patch targets | #1272, as `sova/work/`: a separate change that migrates the importers and deletes the old paths |
| Remove the upward imports left in the `ignore_imports` baseline | Most are `supervisor`, `roles` and `core` importing `dashboard.services`, which only the package move above untangles | #1274: delete each baseline entry as its import goes (an entry that no longer matches fails CI, so the baseline can only shrink) |
| Split the remaining god modules: `llm/client.py`, `dashboard/services/agent_recovery.py`, `core/workflow.py`, `dashboard/services/setup_service.py`, `git/worktree.py` | Each split needs its own seam analysis | One change per module; #1386 covers `agent_recovery.py` and the `agent_*` decomposition |
| Delete `dashboard/services/control_service.py` | It is a thin re-export facade with 36 patch targets in tests; the decision is to delete it, not document it as an API | #1273: migrate the importers to the owning modules and delete the facade |
| Give the underscore-private `sova.ipc` names other packages import public names: the Codex helpers `sova/roles/reviewer.py` imports (`_codex_exec_args`, `_resolve_codex_api_key`, `_resolve_codex_cost_source`), `_codex_extra_env` from `sova.ipc.codex_runtime` and `_PIPELINE_ROLES` from `sova.ipc.runtime`, both imported by `sova/dashboard/services/agent_lifecycle.py` | They are underscore-private names reached across a package boundary, a coupling that predates the Codex move; the move left it as is | A change that renames them in `sova/ipc/codex_runtime.py` and `sova/ipc/runtime.py` (or exposes one public function each caller uses) and updates `reviewer.py`, `agent_lifecycle.py` and their tests |

### Behaviour changes in the second slice

None intended: the hoists, the Codex move and the docstrings change no logic, and the Codex runtime keeps
its `ipc.runtime` log component. Two test-facing changes follow from them. A test that patches
`sova.config.loader.load_config` or `sova.db.session.get_session` must patch the importing module once that
module hoists the import (`tests/test_patch_targets.py` fails on a stale target), and the Codex names
(`CodexRuntime`, `_codex_exec_args`, `_probe_codex_auth` and the rest) are importable from
`sova.ipc.codex_runtime`, not `sova.ipc.runtime`.

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
- The package move, `control_service` and the remaining upward imports are tracked as #1272, #1273 and #1274.
