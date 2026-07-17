# Agent Cookbook

Actionable patterns discovered during development, organized by domain. Entries marked `[promoted]` live in Tier 1 (`.claude/rules/`) -- kept here as one-liners for traceability.

## Promoted to Tier 1 (traceability index)

These entries are fully documented in `.claude/rules/architecture.md` or `.claude/rules/workflow.md`. One-line references only.

**Git / Rebase**: verify branch identity before `git reset --soft`; `git stash` removes uncommitted edits; module split conflicts take refactored facade. See `workflow.md`.
**Git / Hooks**: `core.hooksPath` doesn't survive `git clone`; three layers auto-configure it. See `architecture.md`.
**Testing**: dashboard service tests must isolate project dir via `monkeypatch.setattr`.
**Documentation**: doc counts drift after refactors; stale references persist after renames.
**Dashboard**: polling must clear stale UI on negative path; polling innerHTML refresh kills open dropdowns (reset interactive state flags in all grid-replacing functions); auto-handoff must clear file before spawning; SOVA review state lives in DB not GitHub; pipeline variant detection gates on `current_step`; `start_agent()` lifecycle hooks are role/mode-aware; DB-only status updates must also kill the process; `_finalize_task_run` guards against already-terminal runs; queue Phase badges come from issue milestones not Projects V2 fields (see `docs/issue-organization.md`); WebSocket incremental updates must cover all display fields or add separate refresh; handle sentinel step values explicitly in rendering; pipeline roles validated at exit: `current_step="agent"` + 0 steps = bypass, downgrade to "failed".
**Workflow**: state-adopting steps replicate all side effects; CI fix loops for cross-boundary recovery; guard no-op pushes in LLM fix loops; seed cross-agent data before clearing handoff; headless agents told not to ask questions; headless prompts frame CLI commands as bash blocks; per-issue handoff files for parallel isolation; `recover_stale_runs` checks external state for merge-role runs; address-review independent worktree discovery (both `start_agent` and `start_command` resolve worktrees via shared `_resolve_branch_name()` helper); address-review finding loading uses three fallback sources.
**Config**: new config sections need triple registration.
**External tools**: `all([]) == True` trap in polling loops; `list[-0:]` returns full list (3 occurrences); exception hierarchy in except tuples; only resolve threads after confirmed fixes; CodeRabbit CHANGES_REQUESTED persists across force-pushes; reply before resolving; `/address-pr` fix-before-reply; dismissing review and resolving threads are separate ops.
**GitHub API**: `gh pr comment` posts conversation comments not inline; GitHub rejects REQUEST_CHANGES/APPROVE on own PRs; `gh auth switch` does not persist across subprocesses; `GH_TOKEN` overrides `gh auth switch`; `pull_request_target` reads workflow from base branch (tamper-proof for fork gating).
**Other promoted**: config-driven provider selection wired at startup; SonarCloud `pull_request_target` security model; SonarCloud scanner needs explicit PR args; SonarCloud quality gate needs `statuses: write`; distributable commands need dual install; dispatch shell mocks by command args; file-based handoff persisted to DB at finalization; `if not handoff:` vs `if handoff is None:` for JSON columns; CI fix loop returns on poll timeout without new failure data; check external state before marking multi-phase runs failed; config nesting at most two levels deep; mirror changes across SOVA/distributable command pairs.

## Testing

- **Functions locally-imported in a helper (`from x import y as z` inside a function) must be patched at the import source, not at the module** -- `run_shell` imported as `from sova.utils.shell import run as run_shell` inside `_check_pr_branch_pushed()` creates a local name that `monkeypatch.setattr("sova.dashboard.services.agent_db.run_shell", ...)` cannot reach. Patch `sova.utils.shell.run` instead. PR #347. [confirmed: 1]
- **Add direct unit tests for functions that are mocked in higher-level tests, or SonarCloud will flag 0% coverage** -- if `_is_issue()` and `_check_pr_branch_pushed()` are always mocked in integration tests, SonarCloud sees 0% new coverage even though the feature is tested indirectly. Add `TestIsIssue` / `TestCheckPrBranchPushed` with direct calls to cover the actual implementation paths. PR #347. [confirmed: 1]
- **Runtime/stress/chaos tests excluded from CI via pytest markers** -- `tests/runtime/` uses `@pytest.mark.runtime`, `@pytest.mark.stress`, `@pytest.mark.chaos`. `make test-py` adds `-m "not runtime and not stress and not chaos"` to keep CI fast. `make test-runtime` runs only heavy tests with 120s timeout. New process lifecycle tests go in `tests/runtime/`, not `tests/test_dashboard.py`. PR #341. [confirmed: 1]

## Dashboard / Frontend

- **Badges use `rounded-full`, buttons use `rounded`** -- visual distinction between read-only state indicators (pills) and clickable actions (rectangles). Badges: `px-2 py-0.5 rounded-full`. Buttons: `px-3 py-1 rounded`. Without this, users can't tell what's clickable. [confirmed: 1]
- **Fixed-width columns for right-aligned badge/button groups** -- wrap PR badge, state badge, and action button in a fixed-width container (e.g., `style="width:340px"`) with internal fixed-width divs. Without this, variable text widths cause ragged vertical alignment across rows. [confirmed: 1]
- **Multi-action handoffs expand into a sub-row, not cramped inline** -- single-action handoffs (e.g., "Integrate PR") render on the main row. Multi-action handoffs (e.g., spec review with 4 buttons) expand into a second line below the title with summary text + buttons. [confirmed: 1]
- **Clickable card pattern: cursor-pointer + stopPropagation on nested buttons** -- add `cursor-pointer hover:opacity-80` on clickable element with `onclick`. All nested interactive elements must call `event.stopPropagation()`. File: `sova/dashboard/templates/agents.html`. [confirmed: 1]
- **Extract shared CAS patterns to avoid SonarCloud duplication gate** -- when multiple functions share validation + atomic CAS update logic (e.g., `resume_from_approval` and `reject_spec`), extract a shared helper parameterized by target status. Without this, the duplicated block triggers SonarCloud's 3% new code duplication threshold. PR #334. [confirmed: 1]
- **DB-level tests needed for functions mocked in router tests** -- router tests that mock service functions (e.g., `reject_spec`, `_find_awaiting_approval_run`) provide integration coverage for the router but leave the service function at 0% coverage. Add separate DB-level tests that seed TaskRun records and call the service directly. PR #334: coverage went from 35.8% to 95.6%. [confirmed: 1]
- **Update redirect chains when consolidating pages** -- when page A merges into B and old routes C,D redirected to A, update C,D to redirect to B. Also update sidebar nav, back-links, "View all" links, architecture.md page counts, and delete dead templates. [confirmed: 1]
- **Track notified IDs, not state transitions, for completion alerts** -- track seen `run_id`s to avoid missed/duplicate completion notifications. [confirmed: 1]
- **Disable async action buttons during operations** -- set `button.disabled = true` at function start, re-enable in `finally`. [confirmed: 1]
- **Wrap all API cost values with `parseFloat()` before `.toFixed()`** -- `decimal_to_json()` serializes `Decimal` as strings for precision, but JavaScript's `.toFixed()` only exists on `Number`. When the agents/active API returned `cost_usd: "0.000000"` (string), `(a.cost_usd || 0).toFixed(2)` threw TypeError because the truthy string bypassed the `|| 0` fallback. This crashed `loadDashboard()` before summary cards rendered, leaving the entire page stuck at "Loading...". Pattern: always `parseFloat(value || 0).toFixed(N)` for API cost fields. Files: `dashboard.html`, `agents.html`, `costs.html`, `lifecycle.html`, `overview.html`, `tasks.html`, `app.js`. [confirmed: 1]
- **No nested `<a>` tags: check if parent container is already a link** -- `issueLink()` / `prLink()` produce `<a>` elements; if the surrounding card is also an `<a>` (e.g., dashboard agent strip), the browser breaks the outer link prematurely. Use plain text inside `<a>` containers, links inside `<div onclick>` containers. File: `sova/dashboard/templates/dashboard.html`. [confirmed: 1]
- **marked.js v15 heading renderer uses token object, not positional args** -- `heading({ tokens, depth })` with `this.parser.parseInline(tokens)`, not `heading(text, level)`. Configure via `marked.use({ renderer: { heading(token) { ... } } })` not `new Renderer()` + `setOptions()`. File: `sova/dashboard/templates/spec.html`. [confirmed: 1]
- **Guard CDN-loaded libraries at all usage sites, not just initialization** -- `if (window.lib)` must wrap BOTH `lib.initialize()` AND every `lib.parse()`/`lib.run()` call. Provide fallback (e.g., `escapeHtml()` for markdown). A guard only on init still breaks when individual calls reference the undefined global. PR #184 CodeRabbit. [confirmed: 1]
- **Place DOM hydration before early-return cache checks in polling functions** -- polling functions often cache `_lastUpdated` and early-return when data hasn't changed. If DOM hydration (e.g., `issueLink()`) depends on async state (`SOVA_GITHUB_REPO`) that isn't ready on the first call, the early-return blocks all future hydration attempts. Move hydration before the cache check. PR #184. [confirmed: 1]
- **Use `escapeJsStr()` not `escapeHtml()` for JS string context** -- `escapeHtml()` escapes HTML entities but not single quotes or backslashes. Inline event handlers like `onclick="fn('...')"` need JS string escaping. File: `sova/dashboard/static/app.js`. PR #228 review. [confirmed: 1]
- **Polling-based `innerHTML` refresh kills open dropdowns** -- [promoted] to `.claude/rules/architecture.md` (Dashboard JS polling corollary). Track `_cardMenuOpen`, skip re-renders while open, reset flag in all grid-replacing functions.

- **WebSocket incremental updates must cover all display fields or add a separate refresh** -- the WebSocket snapshot fingerprint (`run_id:status:current_step:step_index:is_stuck`) excludes resource metrics (CPU, memory, cost). Incremental DOM patches only update fields in the fingerprint. Cards freeze on metrics not in the update path. Fix: add a separate 5-second poll (`_refreshCardMetrics`) that patches resource fields via `data-field` selectors alongside the WebSocket. Files: `sova/dashboard/templates/agents.html`, `sova/dashboard/static/app.js`. PR #341. [confirmed: 1]
- **Handle sentinel step values explicitly in rendering, not as unknown** -- `current_step="agent"` is a valid sentinel (set at TaskRun creation, persists when the agent bypasses WorkflowEngine). `steps.indexOf("agent")` returns -1, falling through to "Initializing..." forever. Detect sentinel values early and render a distinct UI (pulsing bar + "Running...") instead of the generic fallback. File: `sova/dashboard/static/app.js:renderStepPipeline()`. PR #341. [confirmed: 1]
- **PR widget role actions must use `quickStartRole`, not `runCommand`** -- `runCommand` routes through the command endpoint which can't resolve issue numbers from PR context, causing all actions to share `issue="address-pr"` and conflict. Role-based actions (address-pr, develop) must use `quickStartRole(issue, role, prNum)` which routes through the agent endpoint with proper issue dedup. Command-based actions (integrate-pr, review-pr) can use `runCommand` but should pass `linked_issue` in args. PR #204. [confirmed: 1]
- **Context menu PR commands must use server-provided action list, not hardcode** -- unconditional "Ship PR" button for all PR-backed tasks bypasses the server state machine, exposing invalid actions (ship on draft/failed PRs). Gate on `secondary_actions.length` and render only server-provided entries. PR #218 CodeRabbit. [confirmed: 1]
- **Display filter vs sort key divergence hides broken logic** -- `indexOf('priority:')` prefix check matches both `"priority:high"` and `"priority: high"`, but dict exact match `label in {"priority:high": 1}` only hits one format. When filters hide a field from display, sort/logic bugs become invisible. Normalize before matching: `label.replace(" ", "")`. File: `sova/dashboard/services/queue_service.py:_extract_label_priority()`. [confirmed: 1]
- **Modal focus management requires three fixes for `display: none` toggling** -- (1) `offsetParent` returns `null` for elements inside `position: fixed` containers, so `el.offsetParent !== null` filters out ALL modal children -- use `!el.disabled` only; (2) synchronous `.focus()` after removing Tailwind `hidden` class silently fails before browser layout -- wrap in `setTimeout(fn, 0)`; (3) keydown listener on the modal element only fires when a descendant already has focus -- use `document.addEventListener` with a `!modal.contains(document.activeElement)` guard to pull escaped focus back. PR #178. [confirmed: 1]

## Issueless / Optional-Issue Runs

- **Non-issue PR commands must use PR-scoped pseudo-issue, not command name** -- `_resolve_command_context()` fell back to `issue = command` (e.g., `"address-pr"`) for PRs without linked issues. Two concurrent address-pr commands on different PRs shared the same pseudo-issue, triggering conflict detection. Fix: `issue = f"pr-{pr_number}" if pr_number else command`. The `"pr-N"` format is not all-digits so `_resolve_issue_worktree()` correctly skips numeric path and falls through to branch lookup. File: `sova/dashboard/services/agent_lifecycle.py:_resolve_command_context()`. [confirmed: 1]

## Spec Provenance

- **Spec lookup must use `ctx.project_dir`, not `ctx.working_dir`** -- specs live in the main project `.claude/specs/`, not in worktrees. Steps running in worktrees (DevelopStep, AddressReviewStep, ExtractMemoryStep) must use `ctx.project_dir` or the fallback `ctx.working_dir or ctx.project_dir`. ReviewerRole already uses the fallback pattern. [confirmed: 1]
- **`git diff --stat HEAD` misses committed changes** -- captures only uncommitted working tree changes. Use `git diff --stat {base_branch}..HEAD` to capture all changes since branching, including commits. Critical for LLM context in implementation summaries. [confirmed: 1]

## Persona / Detection


## LLM / Provider Abstraction

- **LLM reasoning text leaks into external outputs without preamble stripping** -- the harden command produced "Now I have all the context needed. Let me produce the enriched issue body." in issue #296. Same pattern in spec files (#254, #255, #293, #296). `_strip_code_fences()` only removes markdown fences. Fix (issue #342): add `_strip_preamble()` that removes text before the first heading, plus anti-leak prompt instructions. Affects: `sova/cli/commands/harden.py`, `sova/dashboard/services/batch_service.py`, spec/research outputs. [confirmed: 1]

- **Global singleton init at startup breaks multi-project** -- [promoted] to `.claude/rules/architecture.md`. Gate `set_provider()` behind `if not is_multi:` or resolve per-request.
- **Validate command names before building file paths** -- `command.lstrip("/")` allows `..` and `/` to escape `.claude/commands/`. Reject names containing path separators or `..` before `Path()` construction. File: `sova/llm/provider.py:_assert_command_exists()`. PR #174 CodeRabbit + Koda. [confirmed: 1]
- **`claude -p` treats missing slash commands as plain text** -- if `/research` is not in `.claude/commands/`, `claude -p "/research 30"` sends it as a conversational prompt, returns near-zero tokens, and silently does nothing. Always validate command file exists before `invoke_command()`. File: `sova/llm/provider.py:_assert_command_exists()`. [confirmed: 1]
- **Target projects desync from canonical commands** -- [promoted] to `.claude/rules/architecture.md`. Worktrees inherit at creation time; missing commands waste budget on self-recovery.

## Settings / Config UI

- **Decouple display metadata from config models** -- create `SettingMeta` dataclass registry rather than embedding display concerns in config models. Module: `sova/dashboard/settings_meta.py`. [confirmed: 1]
- **Disable inline editing for non-scalar config types** -- list/object settings saved through scalar edit path corrupt TOML structure. Gate behind `isStructured` check. [confirmed: 1]

## Command Design

- **Use `--body-file` for `gh issue edit` with multi-line content** -- `--body "..."` breaks on shell quoting. Use `--body-file /tmp/body.md` or `--body-file -`. [confirmed: 1]

## GitHub API

- **Label names use `area: X` format (space after colon)** -- `gh issue create --label "type:fix"` fails because the actual label is `"type: fix"`. Always check `gh label list` for exact names. Available type labels: `type: feature`, `type: task`, `type: infra`, `bug`. Area labels: `area: dashboard`, `area: orchestrator`, `area: sova-db`, `area: sova-cli`, `area: adapters`, `area: commands`, `area: knowledge`, `area: sova`, `area: security`. Priority labels: `priority: critical/high/medium/low`. [confirmed: 1]
- **`pull_request_target` reads workflow from base branch, not PR branch** -- [promoted] to `.claude/rules/architecture.md`. Full entry with `author_association` gating and security model.
- **Required status checks with `integration_id` reject user-posted statuses** -- workaround: temporarily remove check from ruleset via API, merge, re-add. Needs OAuth keyring token. [confirmed: 1]
- **Auto-approve fork PR CI via file-safety gating** -- `pull_request_target` workflow checks changed files against sensitive patterns (`.github/workflows/*`, `pyproject.toml`, `Makefile`, `invariants/*`, dependency files). Safe PRs: auto-approve via `POST /actions/runs/{id}/approve` with retry loop. Sensitive PRs: post review checklist comment. Gate on `author_association` in `(NONE, FIRST_TIMER, FIRST_TIME_CONTRIBUTOR)`. File: `.github/workflows/fork-pr-gate.yml`. [confirmed: 0]
- **Empty `github_user` in sova.toml causes silent empty queue** -- `resolve_gh_env()` falls back to wrong account. Always verify during `sova install`. [confirmed: 0]
- **GitHub project board Phase field is independent of milestones** -- updating milestone does NOT update Projects V2 board Phase field. Separate API mutations required. [confirmed: 1]
- **Always use `_gh()` helper in GitHubAdapter** -- never call `run()` directly; `_gh()` resolves per-project auth. [confirmed: 1]
- **Use `urlparse` before splitting API URLs for ID extraction** -- `details_url` can include query params. Use `urlparse(url).path` first. File: `sova/git/pr.py:_parse_run_id()`. [confirmed: 1]
- **CI `fetch-depth: 0` alone doesn't fetch other branches** -- `actions/checkout@v4` fetches full history of the current ref only. Invariant scripts that use `origin/main..HEAD` need explicit `git fetch origin main:refs/remotes/origin/main` step, otherwise they silently pass (empty commit range). [confirmed: 0]

## Workflow / Pipeline

- **Address-pr loops on pre-existing CI failures waste cycles** -- the address-pr agent correctly fixes review findings but gets re-triggered when CI stays red due to unrelated test failures (e.g., date-sensitive tests broken on `main`). The agent doesn't compare failing tests against main's CI baseline or check whether failures are in files it changed. Diagnostic: run `gh run list --branch main` to verify main is also red with the same tests. Gwym PR #247 wasted 3 cycles (~$7.50) on this. A baseline CI comparison before entering the fix loop would prevent it. [confirmed: 0]
- **Cross-step directory references must be consistent** -- if step A saves a file to `ctx.worktree_dir` and step B reads it from `ctx.working_dir`, the file is silently not found when the two differ. Use the same context field in both steps, or fall back: `ctx.worktree_dir or ctx.working_dir`. Test fixtures that set both to the same value mask this bug. PR #288 review. [confirmed: 0]
- **Approval-then-spawn endpoints must order: spawn first, clear state second** -- if `clear_handoff()` runs before `start_agent()` and the spawn fails, the system is left in an inconsistent state (handoff cleared, no agent running). Call `start_agent()` first, verify success, then clear the handoff. PR #182 CodeRabbit. [confirmed: 1]
- **Use atomic CAS guard for resume/approval endpoints** -- two concurrent callers can both observe `AWAITING_APPROVAL` and both spawn agents. Transition status to `running` BEFORE calling `start_agent()`, revert on failure. Pattern: `task_run.status = TaskStatus.RUNNING` as a claim, then spawn, revert to `AWAITING_APPROVAL` if spawn fails. Also pass `_skip_handoff_clear=True` to `start_agent()` and clear handoff only after confirmed spawn. PR #261 CodeRabbit. [confirmed: 1]
- **Always pass `project_dir` to `get_session()` in dashboard services** -- production code needs it for multi-project; tests use `_ignore_project_dir` pattern. [confirmed: 1]
- **`_get_last_runs_by_issue` must look across ALL runs for pr_number** -- latest run may be a reviewer run with `pr_number=None`. Use second SQL query as fallback. [confirmed: 1]
- **CommitStep must detect pipeline variant for appropriate message prefix** -- use `fix:` prefix for address-review context instead of `feat:`. File: `sova/core/steps/commit.py`. [confirmed: 1]
- **Security validation must fail closed, not open** -- return RESTRICTIVE default on transient errors. [confirmed: 1]
- **Multi-handoff action IDs must be disambiguated by owner** -- accept `issue` parameter alongside `action_id` to filter handoffs. [confirmed: 1]
- **AssessStep must guard against duplicate developer runs when a PR exists** -- rejects with error suggesting address-review. Bypass: `--force` or `--pr`. [confirmed: 1]
- **CI poll must validate PR head SHA after force-push** -- pass `expected_sha` to `_poll_ci()`, verify via `gh pr view --json headRefOid`. [confirmed: 1]
- **ResolveExternalReviewsStep must include github_user threads** -- filter by both CodeRabbit bot logins AND `ctx.config.github_user`. [confirmed: 0]
- **Pipeline roles must be validated at exit, not trusted on exit code alone** -- a dashboard-spawned developer/researcher/planner can bypass WorkflowEngine entirely (e.g., sync step fails, agent works directly). Exit code 0 with `current_step="agent"` (sentinel never cleared) and 0 `StepExecution` records means the pipeline never ran. `_validate_pipeline_outcome()` detects this and downgrades "done" to "failed". Secondary check (developer-only): `create_pr`/`push` step completed but `pr_number` is None. `_PIPELINE_ROLES` excludes `reviewer` (it's in `_STANDALONE_ROLES`, runs as command). Called from `_wait_and_finalize()` after `_validate_command_outcome()` (mutually exclusive). File: `sova/dashboard/services/agent_db.py`. PR #341. [confirmed: 1]
- **StepExecution status queries must use `STEP_DONE_STATUSES`** -- legacy WorkflowEngine wrote `"passed"`, current writes `"done"`. Use `STEP_DONE_STATUSES = frozenset({"done", "passed"})` from `sova.core.state` in all step completion queries. Hardcoding `status == "done"` drops 843 legacy records. PR #228. [confirmed: 1]
- **Prefer budget over wall-clock timeout as the primary agent governor** -- `agent.step_timeout` (1800s) is configurable; budget is the better control. [confirmed: 1]
- **Multi-project dashboard requires project-scoped URLs** -- APIs and pages live under `/p/{slug}/...` in multi-project mode. [confirmed: 1]
- **`start_command` vs `start_agent` produce different handoff behavior** -- `start_command("review-pr")` does NOT write `DashboardHandoff`. [confirmed: 1]

## Review Verdict / Handoff Chain

- **Verdict queries must include all terminal statuses with valid handoff** -- match `status IN ('done', 'failed', 'interrupted')` with `handoff_json IS NOT NULL`. [confirmed: 1]
- **Status filters in finding queries must match verdict queries** -- `_load_review_findings_by_issue()` and `get_sova_review_verdict()` must use consistent status filters. [confirmed: 1]

## Adapters / Task Sources

- **JQL values must be sanitized before interpolation** -- use `re.sub(r'["\\\x00-\x1f]', "", value)` to strip dangerous characters. File: `sova/adapters/jira.py`. [confirmed: 1]
- **Secret Pydantic fields need `repr=False`** -- fields with API tokens should use `Field("", repr=False)`. Also applies to request models in routers, not just config models. [confirmed: 1]

## Refactoring / Code Quality

- **Slug lookup must reject non-conforming input, not sanitize it** -- use `re.fullmatch()` to reject bad slugs. [confirmed: 1]

## Command Distribution

- **Command instructions must give explicit sources, not vague conditionals** -- tell the agent WHERE to find information, not just WHAT to look for. [confirmed: 1]
- **Post-merge doc updates are blocked by branch protection** -- move doc/memory updates to pre-merge phase on feature branch. [confirmed: 1]
- **`sova commands sync` overwrites SOVA-specific customizations** -- exclude SOVA from sync, or revert with `git checkout -- .claude/commands/` after. [confirmed: 1]
- **Guidelines and commands follow parallel dual-file patterns** -- `docs/*-guidelines.md` = SOVA-specific (detailed, file paths, class names); `guidelines/*.md` = distributable templates (generalized, `{{ variable }}` syntax). Same duality as `.claude/commands/` (SOVA) vs `commands/` (distributable). When updating guidelines, check both files. Distribution uses separate manifests per target dir (`.claude/rules/.sova-manifest.json`). [confirmed: 1]

## Database / Migrations

(All promoted to Tier 1: dispose engine after Alembic migrations; self-heal corrupted `alembic_version`.)

- **Migration file names starting with digits break `pkgutil.resolve_name`** -- Python 3.12.13+ and 3.14 route `unittest.mock.patch()` string targets through `pkgutil.resolve_name()`, which rejects module name components starting with digits (e.g., `011_add_model_selection_reason`). Use `patch.object()` with pre-imported module references instead of string-based `patch()`. For importing such modules at runtime, use `importlib.util.spec_from_file_location()` which bypasses name validation. PR #267 CI failure. [confirmed: 1]

- **`init_db()` must register engine in `_engines` dict** -- `get_session(project_dir=...)` resolves the DB URL via `_get_database_url()` then looks it up in `_engines`. If `init_db()` only stores in global `_session_factory` but not `_engines`, `get_session(project_dir=...)` creates a second engine for the same URL -- silently splitting data across two in-memory DBs in tests. Fix: `_engines[url] = (_engine, _session_factory)` in `init_db()`. File: `sova/db/session.py`. PR #243. [confirmed: 1]

## External Review Tools

- **External API queries must distinguish failure (`None`) from empty result (`[]`)** -- [promoted] to `.claude/rules/architecture.md`. Includes `_GH_STATE_MAP` fallback trap.
- **Enable `external_reviews` in sova.toml for repos with CodeRabbit/SonarCloud** -- `enabled` defaults to `false`, silently skipping steps 11-12 (wait + address findings). Without it, the LLM reviewer (step 15) runs on unvalidated code, duplicating issues static tools would catch for free. Deterministic-before-LLM ordering only works when enabled. [confirmed: 0]
- **`--force-with-lease` fails against unfetched fork refs** -- `git fetch <fork-url> <branch>` before push. [confirmed: 1]
- **Wait for CodeRabbit to finish before merging after /address-pr** -- CodeRabbit shows as `pending` StatusContext during review. Dismissing the old CHANGES_REQUESTED and merging while CodeRabbit is still reviewing the new push means its new findings land post-merge. Poll `gh pr checks` until CodeRabbit is no longer pending, then also verify `gh pr view --json reviewDecision` -- `gh pr checks` monitors CI status only, not review decisions. PRs #134, #172, #189. [confirmed: 2]

## Unified State / Work Items

- **Check `conclusion` before `status` in GitHub CI rollup parsing** -- GitHub API can return `status: "IN_PROGRESS"` with `conclusion: "SUCCESS"` for completed checks. Checking status first causes stale "CI Running" display. File: `sova/dashboard/services/pr_service.py:_summarize_ci()`. [confirmed: 1]
- **Handoff action lookup must check all field name variants** -- pipeline-written handoffs use `"id"`, manually-written use `"action"`, some use `"command"`. Match against `{id, action, command, label}`. ALL consumers of action IDs (lookup, state classification, execution) must use the same field set -- `compute_work_item_state()` initially missed `command`, causing spec actions to stay `handoff_pending`. File: `sova/dashboard/routers/handoff.py`, `sova/dashboard/services/work_item_service.py`. [confirmed: 2]
- **Infer handoff mode from action name when mode field is missing** -- `build_action_command()` defaults to `"unknown"` type when `mode` is empty. If `action` or `command` field has a value, infer `"claude-command"`. File: `sova/dashboard/services/handoff_service.py`. [confirmed: 1]
- **Server-side state computation eliminates client-side join inconsistencies** -- three independent widgets (task browser, PR tracker, handoff panel) computing state independently contradict each other. Single `compute_work_item_state()` with priority cascade (running > handoff > PR > label) replaces all three. File: `sova/dashboard/services/work_item_service.py`. [confirmed: 2]
- **PR review state must override stale handoff actions** -- when `compute_work_item_state()` finds `handoff.status == "awaiting_action"` with an "Integrate PR" action, but the PR has `CHANGES_REQUESTED` from a reviewer that posted after the handoff was written, the PR state must win. Without this, the dashboard shows "Integrate PR" instead of "Address" for PRs with unresolved review comments. Gate: skip handoff-pending when `pr_computed in ("changes_requested", "ci_failed")`. File: `sova/dashboard/services/work_item_service.py`. [confirmed: 1]
- **All PRs use `integrate-pr` for the full pipeline** -- `integrate-pr` runs rebase, push, CI wait, merge, knowledge extraction, and review ingestion. [confirmed: 1]
- **Null guard all `getElementById` calls in event handlers** -- standalone PR rows may not have dropdown elements. `document.getElementById('card-menu-...')` returns null, `.classList` throws TypeError, killing all subsequent JS on the page. Guard with `if (!menu) return;`. [confirmed: 1]
- **New identifier formats must propagate to ALL lookup/index/matching sites** -- introducing `pr:<N>` as work-item key required updates in 5 places: handoff router lookup, `_index_handoffs()`, `_index_running_agents()`, handoff button onclick, and `_renderActionBtn()`. Grep for all consumers of the old format before shipping. PR #218 CodeRabbit rounds 2-3. [confirmed: 1]
- **Builder functions with default-empty collections hide missing data** -- `_build_item(handoff_actions=[], handoff_summary="")` meant PR-only items always showed empty handoff despite `compute_work_item_state()` returning `handoff_pending`. Every caller must explicitly pass computed values; default-empty masks the bug. PR #218 CodeRabbit. [confirmed: 1]
- **All PR states must include "Address PR" in secondary actions** -- non-blocking reviews (GitHub `COMMENTED` state from SOVA reviewer) don't change `reviewDecision` to `CHANGES_REQUESTED`, so `PR_READY_TO_MERGE` can have unaddressed findings with no way to trigger address-pr from the dashboard. Every PR state in `_get_actions()` where review comments could exist should include `address` in secondary. File: `sova/dashboard/services/work_item_service.py`. [confirmed: 1]

## Git Worktree Management


## Config System


## CI / GitHub Actions

- **GitHub `gh pr checks --json` returns `"SKIPPED"` not `"SKIPPING"`** -- the text-format output shows "skipping" but the JSON `state` field is `"SKIPPED"` (past tense). State maps must include both. Unmapped states silently default to `IN_PROGRESS`, causing 900s poll timeouts. File: `sova/git/pr.py:_GH_STATE_MAP`. [confirmed: 0]
- **`not is_passed` is not the same as `is_failed` for CI checks** -- skipped, cancelled, and neutral checks are not passed but not failures either. Use an explicit `is_failed` property checking only `FAILURE` and `TIMED_OUT` conclusions. File: `sova/git/pr.py:CICheck.is_failed`. [confirmed: 0]
- **GitHub CI workflows silently skip when a PR has merge conflicts** -- `mergeStateStatus: "DIRTY"` prevents workflow triggers even when `paths-ignore` doesn't apply. The checks never appear (not pending, just absent). Fix: rebase to resolve conflicts, then push -- CI triggers on the clean commit. Diagnose with `gh pr view N --json mergeStateStatus,mergeable`. Also: force-push amends that don't change the diff may not re-trigger CI -- use a regular push (even empty commit) instead. [confirmed: 1]

## Resource Monitoring

- **Use `mem.total - mem.available`, not `mem.used`, for memory display** -- on macOS, `psutil.virtual_memory().used` only counts "active" memory (~5GB), excluding wired/compressed. But `mem.percent` uses `(total - available) / total`. Using both together produces contradicting numbers (e.g., "5.35 GB / 16 GB 79.1%"). Always use `total - available` to match Activity Monitor and be consistent with `percent`. File: `sova/dashboard/services/resource_service.py`. PR #337. [confirmed: 1]
- **Multi-page app charts need server-side history buffers** -- JS-side arrays (e.g., `cpuHistory`) are destroyed on page navigation. Use a module-level `deque(maxlen=N)` in the service layer to accumulate snapshots, expose via a `/history` endpoint, and seed the JS arrays from it on `init()`. CPython `deque.append` is GIL-protected so thread-safe with `asyncio.to_thread`. File: `sova/dashboard/services/resource_service.py`, `sova/dashboard/static/resource-widget.js`. PR #339. [confirmed: 1]

## Dashboard Recovery

- **Handoff clearing must be after all preflight checks that can abort** -- clearing handoff before budget check or run record creation means the "Action Required" panel disappears even when the start fails. Place `clear_handoff` after `_create_task_run` succeeds (start_agent) or after prompt/pr resolution (start_command), but before `AgentProcess.spawn`. PR #179. [confirmed: 0]

- **`TASK_RUN_TERMINAL` must include "paused" to prevent sweep reclassification** -- when a gate check fails, WorkflowEngine sets status to "paused" and the process exits normally. Without "paused" in `TASK_RUN_TERMINAL`, the liveness sweep finds the dead PID with a non-terminal status and reclassifies it as "interrupted" with misleading error "Agent process died unexpectedly". The startup `recover_stale_runs` had an ad-hoc workaround (`_SKIP_RECOVERY = _TERMINAL | {"paused"}`), but the sweep did not. Adding "paused" to the canonical set fixes both consistently. File: `sova/core/state.py`. PR #339. [confirmed: 1]
- **`recover_stale_runs` must check handoff files before marking interrupted** -- [promoted] to `.claude/rules/architecture.md`. Mark as "done" if handoff exists with `status: "awaiting_action"`.
- **Lifespan shutdown must cancel ALL five task categories** -- [promoted] to `.claude/rules/architecture.md`. Five categories: (1) per-agent I/O + ResourceCollectors via `cancel_background_tasks()`, (2) WebSocket producers via `_ws_manager.cancel_all()`, (3) batch tasks via `cancel_all_batches()`, (4) PR throttle/monitor, (5) sweep. Internal 3s gather timeout + outer 5s `wait_for`. Missing any category causes reload freezes. PR #341.
- **WebSocket `send_json` needs timeout to prevent shutdown freeze** -- `await ws.send_json(data)` with no timeout blocks indefinitely on half-closed sockets during uvicorn reload. Use `asyncio.wait_for(ws.send_json(data), timeout=2.0)`. Also: `_produce_loop` must catch `CancelledError` explicitly and return cleanly (not propagate). File: `sova/dashboard/routers/agents.py`. PR #341. [confirmed: 1]
- **`_wait_and_finalize` must handle CancelledError by terminating the process** -- during shutdown, `cancel_background_tasks()` cancels wait tasks but `process.wait()` leaves the subprocess orphaned. Catch `CancelledError`, call `process.stop(timeout=3.0)`, then re-raise. Without this, orphaned agent processes accumulate after reloads. File: `sova/dashboard/services/agent_lifecycle.py`. PR #341. [confirmed: 1]
- **`start_agent()` must resolve worktrees, not just `start_command()`** -- `start_agent()` unconditionally set `cwd=pa.project_dir`, so agents (especially address-pr) modified files in the watched `sova/dashboard/` directory, triggering uvicorn reload cascades. Now resolves worktrees via `get_pr_branch()` + `_resolve_issue_worktree()` matching `start_command()`'s pattern. DB operations use `project_dir`; only `spawn()` uses the worktree `cwd`. File: `sova/dashboard/services/agent_lifecycle.py`. PR #337. [confirmed: 1]

## Egress Filter

- **Egress patterns must cover all GitHub token formats** -- the `gh[pousr]_\w{36,}` regex missed fine-grained PATs (`github_pat_\w{82,}`). Bearer headers (`Authorization: Bearer <token>`) were missed because the keyword patterns required `=` or `:` separators, not spaces. Added dedicated `bearer_token` pattern with `(?i)\b(?:authorization[:\s]+)?bearer\s+([A-Za-z0-9_\-/.+=]{8,})\b`. Place specific patterns (Bearer) before generic ones (token=value) to avoid false category matches. File: `sova/llm/egress.py`. [confirmed: 1]

## Security / Input Validation

- **DB-backed writers must seed sequence from existing records on re-adoption** -- `OutputWriter._next_line_number` starts at 0, but re-adopted runs may already have persisted rows. Query `MAX(line_number)` on first flush and continue from there. Without this, duplicate composite key violations occur. File: `sova/core/output.py`. PR #243. [confirmed: 1]
## Background Workers / Queue Processing


## Common Mistakes (tracked by occurrence)

- **`except X as exc:` + `log.warning(..., exc_info=True)` leaves `exc` unused** -- remove `as exc` or ruff F841 fires. (occurrences: 2) [confirmed: 1]

## Supervisor / Progression Engine

- **commit-format invariant must include all top-level sova/ directories as valid scopes** -- when adding a new module directory (supervisor, mcp, monitoring, db), add the scope to `VALID_SCOPES` in `invariants/commit-format.sh` and to AGENTS.md's Scopes list. Missing scopes cause push failures even for obviously correct scope names. PR #345. [confirmed: 1]
- **CHECKPOINT_NEEDED distinguishes "human approval needed" from "no action possible"** -- WAIT means the state genuinely has no transition (BACKLOG, IN_PROGRESS, DONE). CHECKPOINT_NEEDED means a transition is possible but an auto flag is disabled. Return it from _determine_transition() when state is actionable but the config flag is off; filter it in execute_decisions() alongside WAIT and BLOCKED. Without this distinction, the progression engine can't surface "waiting for human approval" vs. "nothing to do". PR #345. [confirmed: 1]
- **Evaluate_all should load config once and pass it through to gate methods** -- calling load_config() inside _check_quota_gate() and then again in evaluate_all() means 2+ TOML parses per cycle. Load once, pass via cfg= kwarg. Gate methods fall back to self-loading when cfg=None (for single-task eval path). PR #345. [confirmed: 1]
- **Sync gates in asyncio.gather produce GC warnings** -- when a method is changed from async to sync (e.g., _check_dependency_gate), patch.object mocks using new_callable=AsyncMock create coroutines that get garbage-collected without being awaited. Use bare return_value= patching for sync methods. PR #345. [confirmed: 1]
