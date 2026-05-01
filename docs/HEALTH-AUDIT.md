# SOVA Technical Health Audit

**Last updated**: 2026-04-30
**Codebase**: 13,500 LOC Python (115 modules), 643 tests passing
**Test status**: 643 passed, 1 skipped, 2 warnings (68s runtime)
**Lint status**: shellcheck passing; ruff not on PATH (see finding #1)

---

## Scorecard

| Dimension | Score | Justification |
|---|---|---|
| Correctness | 7/10 | Schema drift in migrations; dead code in worktree compose check; otherwise solid |
| Security | 7/10 | Safe subprocess exec everywhere; path traversal risk in worktree issue_id; incomplete AppleScript escaping |
| Performance | 8/10 | Proper async throughout; Semaphore concurrency; mtime caching on file reads; no hot-path DB waste |
| Maintainability | 6/10 | control_service.py is an 874-line god file; rest of codebase is clean and well-structured |
| Testability | 8/10 | 643 tests, good mock hygiene, proper AsyncMock patterns; CLI and individual steps lack direct unit tests |
| Extensibility | 9/10 | Adding a new adapter/role/step is clean ABC extension; command distribution is template-driven |
| Operability | 8/10 | Structlog throughout; PID lifecycle; stale run recovery; daemon management with systemd/launchd |
| Documentation | 7/10 | AGENTS.md and architecture.md mostly accurate (minor count drift); architecture decisions well-recorded |
| Code Hygiene | 7/10 | No dead imports or orphaned functions; some dead code in worktree.py; config missing validation |
| Release Readiness | 6/10 | Schema drift, missing ruff in PATH, config validation gaps, god file all need addressing |

**Overall**: 7.3 / 10

---

## Prioritized Action Plan

Top 10 findings ranked by severity and effort-to-fix. This is the "if you only have two weeks" list.

| # | Issue | Finding | Severity | Effort | Status |
|---|-------|---------|----------|--------|--------|
| 1 | [#52](https://github.com/xsovad06/project-automation-kit/issues/52) | Install ruff / fix `make check` | P1 | 15 min | OPEN |
| 2 | [#53](https://github.com/xsovad06/project-automation-kit/issues/53) | Add migration 003 for `project_slug` column | P1 | 30 min | OPEN |
| 3 | [#54](https://github.com/xsovad06/project-automation-kit/issues/54) | Add Pydantic `Field` constraints to config models | P2 | 1 hour | OPEN |
| 4 | [#55](https://github.com/xsovad06/project-automation-kit/issues/55) | Validate `issue_id` in worktree path construction | P2 | 30 min | OPEN |
| 5 | [#56](https://github.com/xsovad06/project-automation-kit/issues/56) | Fix session leak pattern in control_service.py | P2 | 1 hour | OPEN |
| 6 | [#57](https://github.com/xsovad06/project-automation-kit/issues/57) | Improve suspicious file guard to check path components | P2 | 30 min | OPEN |
| 7 | [#58](https://github.com/xsovad06/project-automation-kit/issues/58) | Remove dead code in worktree.py line 180-181 | P4 | 5 min | OPEN |
| 8 | [#59](https://github.com/xsovad06/project-automation-kit/issues/59) | Add missing DB indexes (project_slug, superseded_by) | P2 | 30 min | OPEN |
| 9 | [#60](https://github.com/xsovad06/project-automation-kit/issues/60) | Fix test warnings (unawaited coroutine mocks) | P3 | 30 min | OPEN |
| 10 | [#61](https://github.com/xsovad06/project-automation-kit/issues/61) | Split control_service.py into 3 modules | P2 | 4 hours | OPEN |
| 11 | [#62](https://github.com/xsovad06/project-automation-kit/issues/62) | Use proper HTTP status codes in API routers | P3 | 2 hours | OPEN |
| 12 | [#63](https://github.com/xsovad06/project-automation-kit/issues/63) | Improve AppleScript notification escaping | P3 | 30 min | OPEN |
| 13 | [#64](https://github.com/xsovad06/project-automation-kit/issues/64) | Add logging to reviewer parse_findings failure | P3 | 15 min | OPEN |
| 14 | [#65](https://github.com/xsovad06/project-automation-kit/issues/65) | Narrow adapter config Literal to implemented types | P2 | 30 min | OPEN |

**Total for items 1-9**: ~5 hours. Item 10 is a larger refactor best done as a standalone PR.

---

## Findings

### Level 1: Architecture & System Design

#### [P1] #1 -- ruff not installed / not on PATH
**Location**: Makefile `lint-py` target
**Issue**: `make check` (the CI-equivalent command) fails immediately at `ruff check sova/ tests/` with `No such file or directory`. The full CI-equivalent check cannot run locally, and Python lint errors could accumulate undetected.
**Evidence**: `make check` output: `make: ruff: No such file or directory`
**Recommendation**: Install ruff (`pip install --user --break-system-packages ruff`) and ensure `~/Library/Python/3.14/bin` is on PATH (or add ruff to pyproject.toml dev dependencies).

#### [P1] #2 -- Schema drift: `project_slug` missing from Alembic migration
**Location**: `sova/db/models.py:138` vs `sova/db/migrations/versions/001_initial_schema.py:104-116`
**Issue**: `TaskAssessmentRecord` model declares `project_slug: Mapped[str] = mapped_column(String(100), default="")` but migration 001 creates `task_assessments` without this column. Migration 002 only adds `output_file_path` to `task_runs`. On a fresh database created via Alembic (the production path), any INSERT or SELECT involving `project_slug` will raise `OperationalError`.
**Evidence**: Migration 001 lines 104-116 create the table with 10 columns; model lines 131-151 define 11 columns including `project_slug`.
**Recommendation**: Create migration `003_add_project_slug_to_assessments.py` adding the column with `server_default=""`.

#### [P2] #3 -- Config models accept invalid values for operational fields
**Location**: `sova/config/models.py:37-101`
**Issue**: Numeric fields like `max_budget`, `poll_interval`, `max_wait`, `ttl_done_days`, `interval_active`, `min_confidence` accept negative or zero values. A user setting `SOVA_CI_POLL_INTERVAL=-60` or `SOVA_AGENT_MAX_BUDGET=-5` would silently break polling loops or budget enforcement.
**Evidence**: All `int` and `Decimal` fields use bare type annotations without `Field(gt=0)` or `Field(ge=0)`.
**Recommendation**: Add Pydantic `Field` constraints:
```python
max_budget: Decimal = Field(Decimal("10.00"), gt=0)
poll_interval: int = Field(60, gt=0)
min_confidence: float = Field(0.7, ge=0, le=1)
```

#### [P2] #4 -- Path construction uses unvalidated `issue_id` in worktree creation
**Location**: `sova/git/worktree.py:51`
**Issue**: `worktree_path = project_dir / WORKTREE_DIR / issue_id` uses the raw `issue_id` parameter in path construction. If `issue_id` contains `../`, the resulting path escapes the worktree directory. While `issue_id` currently comes from GitHub issue numbers (safe), the interface accepts any string.
**Evidence**: Line 51 -- no validation before path join.
**Recommendation**: Add validation before path construction:
```python
if ".." in issue_id or "/" in issue_id:
    raise ValueError(f"Invalid issue_id: {issue_id}")
```

#### [P2] #8 -- Adapter contract claims four implementations, only one exists
**Location**: `sova/adapters/base.py:47-101`, `sova/adapters/__init__.py`
**Issue**: `TaskAdapter` docstring mentions GitHub, JIRA, and Linear. `create_adapter()` factory accepts `"github"`, `"jira"`, `"linear"`, `"manual"` in its docstring but raises `ValueError` for anything except `"github"`. `TaskSourceConfig` has `type: Literal["github", "jira", "linear", "manual"]` allowing users to configure adapters that don't exist.
**Evidence**: Only `github.py` exists under `sova/adapters/`.
**Recommendation**: Narrow the `Literal` in config to `"github"` only. Update docstrings. Add `NotImplementedError` stubs with clear messages for future adapters.

---

### Level 2: Module Health

#### [P2] #10 -- God file: `control_service.py` (874 lines, 26 functions, 5+ responsibilities)
**Location**: `sova/dashboard/services/control_service.py`
**Issue**: This module handles agent process lifecycle, output streaming/parsing, DB persistence (TaskRun CRUD), GH auth resolution, issue state transitions, cost tracking, auto-handoff orchestration, and stale process recovery. At 874 lines with 26 functions, it is nearly 2x the size of any other file in the codebase (next largest: `github.py` at 449). Changes in any one responsibility risk breaking others.
**Evidence**: Line count, function count, and distinct import clusters (asyncio, json, DB, IPC, git).
**Recommendation**: Extract into 3 modules:
- `agent_lifecycle.py` -- start/stop/wait/slots
- `agent_output.py` -- streaming, parsing, file persistence
- `agent_recovery.py` -- stale run detection, PID checks
Keep `control_service.py` as a thin facade.

#### [P2] #5 -- Session leak in control_service.py helper functions
**Location**: `sova/dashboard/services/control_service.py:185-190`, `sova/dashboard/services/control_service.py:653-679`
**Issue**: Several functions use `session = await get_session()` + manual `await session.close()` without `try/finally`. If `session.begin()` or any query raises, `session.close()` is skipped. This contrasts with the correct pattern used in routers: `async with await get_session() as session:`.
**Evidence**: `_fetch_run_states()` (line 185-190): `session.close()` on line 190 is outside any finally block. `recover_stale_runs()` (line 653-679): same pattern.
**Recommendation**: Replace with `async with await get_session() as session:` consistently, matching the router pattern.

#### [P3] #11 -- Inconsistent router error responses (always HTTP 200)
**Location**: `sova/dashboard/routers/agents.py`, `sova/dashboard/routers/control.py`, `sova/dashboard/routers/handoff.py`
**Issue**: All API routers return HTTP 200 with error details embedded in the JSON body. No router uses 4xx/5xx status codes for actual errors. This violates REST conventions and makes client-side error detection require JSON parsing.
**Evidence**: Router catch blocks return `{"error": str(e)}` with implicit 200 status.
**Recommendation**: Use `raise HTTPException(status_code=500, detail=str(e))` for server errors, 400 for bad input. Dashboard JS fetch wrappers already handle non-200 responses.

---

### Level 3: File-Level Quality

#### [P2] #6 -- Suspicious file guard only checks exact filenames, not path components
**Location**: `sova/git/operations.py:131-160`
**Issue**: `_SUSPICIOUS_PATHS` checks `f.strip() in _SUSPICIOUS_PATHS`, matching only exact filenames. Files at nested paths like `src/.env` or `vendor/credentials.json` pass through undetected.
**Evidence**: The set contains `.env`, `.venv`, etc. but the check is an equality test, not a path-component check.
**Recommendation**: Change to:
```python
bad = [f for f in staged if any(part in _SUSPICIOUS_PATHS for part in Path(f.strip()).parts)]
```

#### [P3] #12 -- Incomplete AppleScript escaping in notifications
**Location**: `sova/ipc/notifications.py:64-68`
**Issue**: Desktop notification escaping only handles `\` and `"`. AppleScript strings can be broken by other characters. While `run()` uses `create_subprocess_exec` (no shell injection), the AppleScript itself is constructed via f-string and could contain injection within the AppleScript interpreter. Risk is limited to local desktop notifications whose content comes from GitHub issue titles.
**Evidence**: Lines 65-67 -- only two character replacements.
**Recommendation**: Use `shlex.quote()` on the entire title/message, or call `osascript` with JXA (JavaScript) which handles JSON payloads safely.

#### [P3] #9 -- Two RuntimeWarnings in test suite about unawaited coroutines
**Location**: `tests/test_dashboard.py` (TestDuplicateAgentPrevention)
**Issue**: `RuntimeWarning: coroutine 'AsyncMockMixin._execute_mock_call' was never awaited` in two tests. The mock for `stdout_lines()` and `stderr_lines()` is not configured as an async iterator properly.
**Evidence**: pytest output shows warnings originating from `control_service.py:513` and `control_service.py:564`.
**Recommendation**: Configure the mock to return a proper async iterator via `MagicMock(return_value=AsyncIteratorMock([]))`.

#### [P4] #7 -- Dead code: always-false condition in worktree compose check
**Location**: `sova/git/worktree.py:180-181`
**Issue**: `if stripped.startswith("name:") and not stripped.startswith("name:", 0):` is always False because `startswith(s, 0)` is equivalent to `startswith(s)`. The function still works correctly because line 182 catches the same case.
**Evidence**: Lines 180-183 -- two redundant checks, first is dead code.
**Recommendation**: Remove lines 180-181, keeping only lines 182-183.

---

### Level 4: Function / Class Granularity

#### [P2] #8 -- Missing DB indexes on `project_slug` and `superseded_by`
**Location**: `sova/db/models.py:125-128`, `sova/db/models.py:148-151`
**Issue**: `TaskAssessmentRecord.__table_args__` defines indexes on `issue_number` and `suitability` but not on `project_slug`, which is used in multi-project filtering. `Memory.__table_args__` is missing an index on `superseded_by`, which is filtered in every `search()` call via `Memory.superseded_by.is_(None)`.
**Evidence**: Lines 125-128 and 148-151 -- missing indexes.
**Recommendation**: Add `Index("ix_assessments_project_slug", "project_slug")` and `Index("ix_memories_superseded_by", "superseded_by")` to respective `__table_args__`.

#### [P3] #13 -- Reviewer `_parse_findings()` swallows parse failure diagnostics
**Location**: `sova/roles/reviewer.py:118-127`
**Issue**: When JSON parsing fails twice (raw and substring extraction), the function returns empty findings with `"Failed to parse review response"` but does not log the failed text or exception. This makes debugging LLM response format changes impossible.
**Evidence**: Two `except json.JSONDecodeError:` blocks with no logging.
**Recommendation**: Add `log.warning("parse_findings.failed", text_preview=text[:200], exc_info=True)` before returning.

---

## Strengths (preserve these)

1. **Safe subprocess execution everywhere**: All 40+ subprocess calls use `asyncio.create_subprocess_exec()` with list arguments. Zero `shell=True` usage. Eliminates an entire class of injection vulnerabilities.

2. **Workflow engine with gate checks**: Every code-producing step validates output via `validate_output()` checking unstaged diffs, staged diffs, AND commits ahead of base. Prevents empty PRs and ensures LLM agents actually produced work.

3. **Resumable pipelines**: `completed_steps` tracking + `can_skip()` pattern means crashed agents resume from where they left off. `resumed_from_id` audit chain preserves history.

4. **Clean ABC interfaces**: `TaskAdapter` (11 methods), `AgentRole` (state gates + execute), `BaseStep` (execute/validate_output/can_skip) -- all fully implemented by concrete classes with proper type hints.

5. **Dual handoff persistence**: File-based (fast polling for dashboard) + DB-backed (reliable for scheduler history). Smart tradeoff for different read patterns.

6. **Stale run recovery**: `recover_stale_runs()` detects dead PIDs on startup and marks them interrupted. Dismiss endpoint clears the banner. Thoughtful operational feature.

7. **Structlog throughout**: Consistent structured logging with component context (`get_logger(component="...")`). JSON or console output modes. File rotation support. No bare `print()` statements.

8. **Non-fatal side effects pattern**: `try/except` with `exc_info=True` wrapping optional operations (notifications, board moves, journaling). Primary operations never fail due to side effect errors.

9. **Test quality**: 643 passing tests with proper `AsyncMock` patterns, `tmp_path` isolation, and realistic seed data. Test-to-code ratio of 72% (9,749 test LOC / 13,500 source LOC).

10. **Extensibility architecture**: Adding a new adapter: implement 11 ABC methods + register in factory. New step: implement `BaseStep.execute()` + `validate_output()` + add to pipeline list. New dashboard page: template + router + service. Clean separation.

---

## Onboarding Gap Analysis

**What a new contributor would understand immediately:**
- Project structure is well-documented in AGENTS.md and architecture.md
- The ABC pattern for adapters/roles/steps is self-explanatory
- Test patterns are consistent and well-organized by module
- Conventional commits format is clear
- `make check` is the single CI-equivalent command (once ruff is installed)

**What would confuse them:**
- Dual TaskRun write paths (dashboard outer vs workflow inner) -- documented in architecture.md but the distinction is subtle and easy to miss
- Why `control_service.py` is 874 lines when everything else is under 450
- The `async with await get_session() as session:` vs `session = await get_session()` inconsistency -- which pattern is correct? (both work, but the context manager form is preferred)
- The relationship between `AgentHandoff` (DB) and `DashboardHandoff` (file) -- why two persistence mechanisms?

**What is undocumented but critical:**
- `ruff` must be installed separately; it is not in pyproject.toml dependencies
- The migration chain has a gap (`project_slug`) that will bite on fresh installs
- Dashboard service tests MUST monkeypatch `get_project_dir` (mentioned in architecture.md but easy to miss)
- The `--force` flag on `sova run` bypasses the mandatory Triage->Research->Develop pipeline

**What they would break on their first PR:**
- Adding a new config field without `Field(gt=0)` constraints (following existing patterns)
- Writing a new dashboard service function that uses `session.close()` without try/finally (following `_fetch_run_states` pattern)
- Adding a new step without proper `validate_output()` gate checks (if they copy from a non-production step like `assessment`)
- Creating a test that doesn't monkeypatch `get_project_dir`, causing it to read real project files

---

## Largest Files (potential split candidates)

| File | Lines | Notes |
|---|---|---|
| `sova/dashboard/services/control_service.py` | 874 | God file -- split recommended |
| `sova/adapters/github.py` | 449 | Appropriate for scope |
| `sova/roles/reviewer.py` | 417 | Prompt templates inflate size; acceptable |
| `sova/git/operations.py` | 405 | Covers many git operations; acceptable |
| `sova/dashboard/app.py` | 403 | App factory + route registration; borderline |
| `sova/core/workflow.py` | 400 | Complex orchestration; acceptable |
| `sova/dashboard/services/batch_service.py` | 316 | Batch ops are inherently complex; acceptable |

---

## Test Coverage Gaps

| Module | Test File | Status |
|---|---|---|
| `sova/cli/commands/*` (10 files) | None | No direct CLI tests |
| `sova/core/steps/*` (16 steps) | Via `test_core.py` | Integration-tested only |
| `sova/db/models.py`, `session.py` | `test_migrate.py` (1 test) | Minimal |
| `sova/scheduler/*` (3 files) | `test_scheduler.py` (20 tests) | Adequate |
| `sova/commands/*` (4 files) | `test_commands.py` | Has tests |
| `sova/config/*` | Via other test files | No dedicated test file |

---

## Documentation Accuracy

| Document | Accuracy | Issues |
|---|---|---|
| AGENTS.md | 90% | Lists 7 CLI commands, actual count is 10 (missing harden, server, commands) |
| `.claude/rules/architecture.md` | 95% | Service count says 12, actual is 13 (missing output_service) |
| `docs/VISION.md` | 85% | Header says "Pre-implementation" but phases 0-6 are complete |
| `docs/REWRITE-PLAN.md` | 100% | Phase statuses all accurate |

---

## Revision History

| Date | Author | Changes |
|---|---|---|
| 2026-04-30 | Initial audit | Full audit across all 4 levels, 13 findings, 10 strengths |
