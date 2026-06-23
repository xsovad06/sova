# Agent Cookbook

Actionable patterns discovered during development, organized by domain. Entries marked `[promoted]` live in Tier 1 (`.claude/rules/`) -- kept here as one-liners for traceability.

## Promoted to Tier 1 (traceability index)

These entries are fully documented in `.claude/rules/architecture.md` or `.claude/rules/workflow.md`. One-line references only.

**Git / Rebase**: verify branch identity before `git reset --soft`; `git stash` removes uncommitted edits; module split conflicts take refactored facade. See `workflow.md`.
**Git / Hooks**: `core.hooksPath` doesn't survive `git clone`; three layers auto-configure it. See `architecture.md`.
**Testing**: dashboard service tests must isolate project dir via `monkeypatch.setattr`.
**Documentation**: doc counts drift after refactors; stale references persist after renames.
**Dashboard**: polling must clear stale UI on negative path; polling innerHTML refresh kills open dropdowns (reset interactive state flags in all grid-replacing functions); auto-handoff must clear file before spawning; SOVA review state lives in DB not GitHub; pipeline variant detection gates on `current_step`; `start_agent()` lifecycle hooks are role/mode-aware; DB-only status updates must also kill the process; `_finalize_task_run` guards against already-terminal runs; queue Phase badges come from issue milestones not Projects V2 fields (see `docs/issue-organization.md`).
**Workflow**: state-adopting steps replicate all side effects; CI fix loops for cross-boundary recovery; guard no-op pushes in LLM fix loops; seed cross-agent data before clearing handoff; headless agents told not to ask questions; headless prompts frame CLI commands as bash blocks; per-issue handoff files for parallel isolation; `recover_stale_runs` checks external state for merge-role runs; address-review independent worktree discovery; address-review finding loading uses three fallback sources.
**Config**: new config sections need triple registration.
**External tools**: `all([]) == True` trap in polling loops; `list[-0:]` returns full list (3 occurrences); exception hierarchy in except tuples; only resolve threads after confirmed fixes; CodeRabbit CHANGES_REQUESTED persists across force-pushes; reply before resolving; `/address-pr` fix-before-reply; dismissing review and resolving threads are separate ops.
**GitHub API**: `gh pr comment` posts conversation comments not inline; GitHub rejects REQUEST_CHANGES/APPROVE on own PRs; `gh auth switch` does not persist across subprocesses; `GH_TOKEN` overrides `gh auth switch`; `pull_request_target` reads workflow from base branch (tamper-proof for fork gating).
**Other promoted**: config-driven provider selection wired at startup; SonarCloud `pull_request_target` security model; SonarCloud scanner needs explicit PR args; SonarCloud quality gate needs `statuses: write`; distributable commands need dual install; dispatch shell mocks by command args; file-based handoff persisted to DB at finalization; `if not handoff:` vs `if handoff is None:` for JSON columns; CI fix loop returns on poll timeout without new failure data; check external state before marking multi-phase runs failed; config nesting at most two levels deep; mirror changes across SOVA/distributable command pairs.

## Testing

- **Assert context side effects, not just return values** -- when a step's `execute()` updates both the return `StepResult` and the shared `ExecutionContext` (e.g., `ctx.add_cost()`), test both. A regression in context mutation passes if only `result.cost_usd` is asserted. PR #99 CodeRabbit. [confirmed: 0]

## Dashboard / Frontend

- **Clickable card pattern: cursor-pointer + stopPropagation on nested buttons** -- add `cursor-pointer hover:opacity-80` on clickable element with `onclick`. All nested interactive elements must call `event.stopPropagation()`. File: `sova/dashboard/templates/agents.html`. [confirmed: 1]
- **Update redirect chains when consolidating pages** -- when page A merges into B and old routes C,D redirected to A, update C,D to redirect to B. Also update sidebar nav, back-links, "View all" links, architecture.md page counts, and delete dead templates. [confirmed: 1]
- **Diff against base after large CSS edits** -- easy to accidentally delete pre-existing rules. After multi-line CSS edits, run `diff <(git show main:path) <(cat path)`. [confirmed: 0]
- **Static file paths must be absolute `/static/...`, not prefix-relative** -- multi-project mode prefixes cause 404s with relative paths. [confirmed: 0]
- **Skip validation on draft creation, validate on save** -- POST creates scaffold, PUT validates. Otherwise drafts are rejected by their own skeleton. [confirmed: 0]
- **Request validators must not be stricter than backend dispatch** -- Pydantic validators that restrict to a hardcoded set (e.g., `BUILTIN_ROLE_NAMES`) block backend features the downstream dispatcher handles (custom roles, nicknames). Validate structure (non-empty, normalized) without restricting values, or let the service layer validate. PR #173 review. [confirmed: 0]
- **Paginated endpoints must return `{items, total}` dict, not bare list** -- adding pagination to a list endpoint is a breaking change for all callers. [confirmed: 0]
- **Immutable spawn fields (role) don't need `current_step` gating** -- `role` is set at spawn and never changes, so `role == "researcher"` is safe as a sufficient condition for all steps. Only `pr_number`-based detection needs the gate. [confirmed: 0]
- **Track notified IDs, not state transitions, for completion alerts** -- track seen `run_id`s to avoid missed/duplicate completion notifications. [confirmed: 1]
- **Type URL path params as `int` when they are IDs** -- prevents XSS via JS escape sequences in Jinja2 `<script>` blocks. [confirmed: 0]
- **Disable async action buttons during operations** -- set `button.disabled = true` at function start, re-enable in `finally`. [confirmed: 1]
- **No nested `<a>` tags: check if parent container is already a link** -- `issueLink()` / `prLink()` produce `<a>` elements; if the surrounding card is also an `<a>` (e.g., dashboard agent strip), the browser breaks the outer link prematurely. Use plain text inside `<a>` containers, links inside `<div onclick>` containers. File: `sova/dashboard/templates/dashboard.html`. [confirmed: 1]
- **marked.js v15 heading renderer uses token object, not positional args** -- `heading({ tokens, depth })` with `this.parser.parseInline(tokens)`, not `heading(text, level)`. Configure via `marked.use({ renderer: { heading(token) { ... } } })` not `new Renderer()` + `setOptions()`. File: `sova/dashboard/templates/spec.html`. [confirmed: 1]
- **Guard CDN-loaded libraries at all usage sites, not just initialization** -- `if (window.lib)` must wrap BOTH `lib.initialize()` AND every `lib.parse()`/`lib.run()` call. Provide fallback (e.g., `escapeHtml()` for markdown). A guard only on init still breaks when individual calls reference the undefined global. PR #184 CodeRabbit. [confirmed: 1]
- **Place DOM hydration before early-return cache checks in polling functions** -- polling functions often cache `_lastUpdated` and early-return when data hasn't changed. If DOM hydration (e.g., `issueLink()`) depends on async state (`SOVA_GITHUB_REPO`) that isn't ready on the first call, the early-return blocks all future hydration attempts. Move hydration before the cache check. PR #184. [confirmed: 1]
- **Grep for removed DOM element IDs in template JS after redesigns** -- when a template redesign removes an HTML element (e.g., a select box), `getElementById()` returns null and `.value` throws TypeError. Grep the template's `<script>` for the element ID. PR #134 `agent-role` select. [confirmed: 0]
- **Frontend state groups must include all backend actionable states** -- `_ACTIONABLE_STATES` on the backend and `stateGroupOrder` on the frontend must stay in sync. Missing states cause items to silently disappear from the UI. PR #134 `human_only`. [confirmed: 0]
- **Use `escapeJsStr()` not `escapeHtml()` for JS string context** -- `escapeHtml()` escapes HTML entities but not single quotes or backslashes. Inline event handlers like `onclick="fn('...')"` need JS string escaping. File: `sova/dashboard/static/app.js`. [confirmed: 0]
- **Polling-based `innerHTML` refresh kills open dropdowns** -- [promoted] to `.claude/rules/architecture.md` (Dashboard JS polling corollary). Track `_cardMenuOpen`, skip re-renders while open, reset flag in all grid-replacing functions.

- **Display filter vs sort key divergence hides broken logic** -- `indexOf('priority:')` prefix check matches both `"priority:high"` and `"priority: high"`, but dict exact match `label in {"priority:high": 1}` only hits one format. When filters hide a field from display, sort/logic bugs become invisible. Normalize before matching: `label.replace(" ", "")`. File: `sova/dashboard/services/queue_service.py:_extract_label_priority()`. [confirmed: 1]
- **Modal focus management requires three fixes for `display: none` toggling** -- (1) `offsetParent` returns `null` for elements inside `position: fixed` containers, so `el.offsetParent !== null` filters out ALL modal children -- use `!el.disabled` only; (2) synchronous `.focus()` after removing Tailwind `hidden` class silently fails before browser layout -- wrap in `setTimeout(fn, 0)`; (3) keydown listener on the modal element only fires when a descendant already has focus -- use `document.addEventListener` with a `!modal.contains(document.activeElement)` guard to pull escaped focus back. PR #178. [confirmed: 1]

## Issueless / Optional-Issue Runs

- **Sanitize `role_name` before worktree/branch construction** -- `run_label` derived from CLI `--role` is used unsanitized in `f"feat/{ctx.run_label}"` (branch) and `worktree_id` (path). Characters like `/` or `..` cause `ValueError` in `create_worktree()` but `CreateWorktreeStep` only catches `RuntimeError`. Either validate at CLI entry or catch both exceptions. Files: `sova/core/steps/create_worktree.py`, `sova/cli/commands/run.py`. PR #183 CodeRabbit. [confirmed: 0]
- **Worktree rediscovery must work for issueless address-review runs** -- `_discover_address_review_context()` gates on `ctx.has_issue`, blocking worktree recovery for issueless resumed runs. Use `ctx.run_label` as fallback `worktree_id` when no issue. File: `sova/roles/developer.py`. PR #183 CodeRabbit. [confirmed: 0]

## Persona / Detection

- **Persona detection order: frameworks before languages** -- `detect_persona()` must check framework markers (Django, FastAPI, Odoo) before generic language markers (Python). Module: `sova/knowledge/personas.py`. [confirmed: 0]

## LLM / Provider Abstraction

- **Global singleton init at startup breaks multi-project** -- `set_provider()` at dashboard startup sets one process-global provider. Gate behind `if not is_multi:` or resolve per-request. [confirmed: 0]
- **Subprocess streaming must drain stderr and check exit code** -- `_start_streaming_process()` reads stdout but never consumes stderr. Concurrently drain stderr, check exit code after completion. [confirmed: 0]
- **Provider factory must forward ALL config fields to constructor** -- `create_provider("litellm")` silently ignored config fields. Always pass config fields through. [confirmed: 0]
- **Unused API parameters silently violate contracts** -- `invoke()` accepts `max_tokens` but never passes it through. Either forward, raise, or remove from signature. [confirmed: 0]
- **Streaming and non-streaming paths must have matching fallback behavior** -- `invoke()` retries with fallback, `invoke_streaming()` raises immediately. [confirmed: 0]
- **Cost tracking errors must fail-safe, not fail-silent** -- returning `Decimal('0')` on error defeats budget enforcement. Return sentinel or raise. [confirmed: 0]
- **Provider timeout defaults must be consistent** -- use `timeout_seconds or 600` consistently across providers. [confirmed: 0]
- **Restrict fallback retries to transient API errors** -- check `status_code in {408, 429, 500, 502, 503, 504}`, not broad `except Exception`. [confirmed: 0]
- **MCP tool servers must bind project context at creation, not per-call** -- close over project path in `register_tools()`, never expose in tool schemas. [confirmed: 0]
- **CLI subcommand `--project` may mismatch global provider init** -- `_init_llm_provider()` loads config from cwd, not the `--project` path. [confirmed: 0]
- **Validate command names before building file paths** -- `command.lstrip("/")` allows `..` and `/` to escape `.claude/commands/`. Reject names containing path separators or `..` before `Path()` construction. File: `sova/llm/provider.py:_assert_command_exists()`. PR #174 CodeRabbit + Koda. [confirmed: 1]
- **`claude -p` treats missing slash commands as plain text** -- if `/research` is not in `.claude/commands/`, `claude -p "/research 30"` sends it as a conversational prompt, returns near-zero tokens, and silently does nothing. Always validate command file exists before `invoke_command()`. File: `sova/llm/provider.py:_assert_command_exists()`. [confirmed: 1]
- **Target projects desync from canonical commands** -- [promoted] to `.claude/rules/architecture.md`. Worktrees inherit at creation time; missing commands waste budget on self-recovery.

## Settings / Config UI

- **Decouple display metadata from config models** -- create `SettingMeta` dataclass registry rather than embedding display concerns in config models. Module: `sova/dashboard/settings_meta.py`. [confirmed: 1]
- **Disable inline editing for non-scalar config types** -- list/object settings saved through scalar edit path corrupt TOML structure. Gate behind `isStructured` check. [confirmed: 1]

## Command Design

- **Use `--body-file` for `gh issue edit` with multi-line content** -- `--body "..."` breaks on shell quoting. Use `--body-file /tmp/body.md` or `--body-file -`. [confirmed: 1]

## GitHub API

- **`pull_request_target` reads workflow from base branch, not PR branch** -- [promoted] to `.claude/rules/architecture.md`. Full entry with `author_association` gating and security model.
- **Required status checks with `integration_id` reject user-posted statuses** -- workaround: temporarily remove check from ruleset via API, merge, re-add. Needs OAuth keyring token. [confirmed: 1]
- **Auto-approve fork PR CI via file-safety gating** -- `pull_request_target` workflow checks changed files against sensitive patterns (`.github/workflows/*`, `pyproject.toml`, `Makefile`, `invariants/*`, dependency files). Safe PRs: auto-approve via `POST /actions/runs/{id}/approve` with retry loop. Sensitive PRs: post review checklist comment. Gate on `author_association` in `(NONE, FIRST_TIMER, FIRST_TIME_CONTRIBUTOR)`. File: `.github/workflows/fork-pr-gate.yml`. [confirmed: 0]
- **Empty `github_user` in sova.toml causes silent empty queue** -- `resolve_gh_env()` falls back to wrong account. Always verify during `sova install`. [confirmed: 0]
- **GitHub project board Phase field is independent of milestones** -- updating milestone does NOT update Projects V2 board Phase field. Separate API mutations required. [confirmed: 1]
- **Always use `_gh()` helper in GitHubAdapter** -- never call `run()` directly; `_gh()` resolves per-project auth. [confirmed: 1]
- **Use `urlparse` before splitting API URLs for ID extraction** -- `details_url` can include query params. Use `urlparse(url).path` first. File: `sova/git/pr.py:_parse_run_id()`. [confirmed: 1]
- **Reset file context at `diff --git` boundaries in diff parsers** -- git metadata lines get misclassified as context lines without reset. [confirmed: 0]
- **Retry at both adapter AND caller levels** -- adapter retry handles common case, caller retry handles mocked adapters and inter-attempt failures. [confirmed: 0]
- **CI `fetch-depth: 0` alone doesn't fetch other branches** -- `actions/checkout@v4` fetches full history of the current ref only. Invariant scripts that use `origin/main..HEAD` need explicit `git fetch origin main:refs/remotes/origin/main` step, otherwise they silently pass (empty commit range). [confirmed: 0]

## Workflow / Pipeline

- **Approval-then-spawn endpoints must order: spawn first, clear state second** -- if `clear_handoff()` runs before `start_agent()` and the spawn fails, the system is left in an inconsistent state (handoff cleared, no agent running). Call `start_agent()` first, verify success, then clear the handoff. PR #182 CodeRabbit. [confirmed: 0]
- **Dashboard finalization must separate cost from status writes** -- write cost unconditionally, THEN check terminal guard for status. File: `sova/dashboard/services/agent_db.py`. [confirmed: 0]
- **Always pass `project_dir` to `get_session()` in dashboard services** -- production code needs it for multi-project; tests use `_ignore_project_dir` pattern. [confirmed: 1]
- **`_get_last_runs_by_issue` must look across ALL runs for pr_number** -- latest run may be a reviewer run with `pr_number=None`. Use second SQL query as fallback. [confirmed: 1]
- **CommitStep must detect pipeline variant for appropriate message prefix** -- use `fix:` prefix for address-review context instead of `feat:`. File: `sova/core/steps/commit.py`. [confirmed: 1]
- **Security validation must fail closed, not open** -- return RESTRICTIVE default on transient errors. [confirmed: 1]
- **Multi-handoff action IDs must be disambiguated by owner** -- accept `issue` parameter alongside `action_id` to filter handoffs. [confirmed: 1]
- **Handoff chain must propagate branch_name even for read-only roles** -- reviewer must carry `branch_name` through handoff for address-review to find worktree. [confirmed: 0]
- **Gate checks must use before/after HEAD comparison, not base..HEAD** -- capture HEAD before LLM invocation, compare after. [confirmed: 0]
- **Address-review pipeline needs MonitorCIStep** -- without CI verification after push, pipeline declares "ready to integrate" with red CI. [confirmed: 0]
- **AssessStep must guard against duplicate developer runs when a PR exists** -- rejects with error suggesting address-review. Bypass: `--force` or `--pr`. [confirmed: 1]
- **CI poll must validate PR head SHA after force-push** -- pass `expected_sha` to `_poll_ci()`, verify via `gh pr view --json headRefOid`. [confirmed: 1]
- **ResolveExternalReviewsStep must include github_user threads** -- filter by both CodeRabbit bot logins AND `ctx.config.github_user`. [confirmed: 0]
- **Guard all DB writes in retry loops with try/except** -- `_create_step_execution()` and `_update_step_execution()` can fail (connection lost, disk full). Unguarded DB failures crash the workflow; wrap in try/except, log with `exc_info=True`, and continue. PR #201. [confirmed: 0]
- **Prefer budget over wall-clock timeout as the primary agent governor** -- `agent.step_timeout` (1800s) is configurable; budget is the better control. [confirmed: 1]
- **Multi-project dashboard requires project-scoped URLs** -- APIs and pages live under `/p/{slug}/...` in multi-project mode. [confirmed: 1]
- **`start_command` vs `start_agent` produce different handoff behavior** -- `start_command("review-pr")` does NOT write `DashboardHandoff`. [confirmed: 1]

## Review Verdict / Handoff Chain

- **Verdict queries must include all terminal statuses with valid handoff** -- match `status IN ('done', 'failed', 'interrupted')` with `handoff_json IS NOT NULL`. [confirmed: 1]
- **Status filters in finding queries must match verdict queries** -- `_load_review_findings_by_issue()` and `get_sova_review_verdict()` must use consistent status filters. [confirmed: 1]

## Adapters / Task Sources

- **JQL values must be sanitized before interpolation** -- use `re.sub(r'["\\\x00-\x1f]', "", value)` to strip dangerous characters. File: `sova/adapters/jira.py`. [confirmed: 1]
- **Lazy httpx.AsyncClient needs explicit close()** -- add `async def close()` to prevent connection pool leaks. File: `sova/adapters/jira.py`. [confirmed: 0]
- **Secret Pydantic fields need `repr=False`** -- fields with API tokens should use `Field("", repr=False)`. [confirmed: 0]

## Refactoring / Code Quality

- **Narrow `except Exception` to specific types in non-fatal blocks** -- catch `(ImportError, RuntimeError, OSError)` instead. [confirmed: 0]
- **Import shared constants instead of duplicating literal sets** -- duplicates drift silently. [confirmed: 0]
- **Extracted helpers must use concrete types, not `object`** -- use `TYPE_CHECKING` imports. [confirmed: 0]
- **Slug lookup must reject non-conforming input, not sanitize it** -- use `re.fullmatch()` to reject bad slugs. [confirmed: 1]
- **Cancel-then-await: use `asyncio.gather(return_exceptions=True)`** -- avoids SonarCloud S7497. [confirmed: 0]

## Command Distribution

- **Command instructions must give explicit sources, not vague conditionals** -- tell the agent WHERE to find information, not just WHAT to look for. [confirmed: 1]
- **Post-merge doc updates are blocked by branch protection** -- move doc/memory updates to pre-merge phase on feature branch. [confirmed: 1]
- **`sova commands sync` overwrites SOVA-specific customizations** -- exclude SOVA from sync, or revert with `git checkout -- .claude/commands/` after. [confirmed: 1]
- **Guidelines and commands follow parallel dual-file patterns** -- `docs/*-guidelines.md` = SOVA-specific (detailed, file paths, class names); `guidelines/*.md` = distributable templates (generalized, `{{ variable }}` syntax). Same duality as `.claude/commands/` (SOVA) vs `commands/` (distributable). When updating guidelines, check both files. Distribution uses separate manifests per target dir (`.claude/rules/.sova-manifest.json`). [confirmed: 1]

## Database / Migrations

(All promoted to Tier 1: dispose engine after Alembic migrations; self-heal corrupted `alembic_version`.)

## External Review Tools

- **External API queries must distinguish failure (`None`) from empty result (`[]`)** -- [promoted] to `.claude/rules/architecture.md`. Includes `_GH_STATE_MAP` fallback trap.
- **`dismiss_stale_reviews_on_push` prevents CHANGES_REQUESTED accumulation** -- set in GitHub ruleset. [confirmed: 0]
- **Redact CI logs before injecting into LLM prompts** -- strip tokens/API keys with regex. [confirmed: 0]
- **GraphQL queries in command docs must include all referenced fields** -- pagination instructions need `pageInfo { hasNextPage endCursor }` in selection set. [confirmed: 0]
- **`--force-with-lease` fails against unfetched fork refs** -- `git fetch <fork-url> <branch>` before push. [confirmed: 1]
- **SonarCloud CE Task failures are transient infrastructure errors** -- "CE Task finished abnormally with status: FAILED" means the SonarCloud server-side processing crashed, not a code quality issue. The scanner uploaded successfully but server processing failed. Fix: amend the top commit to change the SHA and force-push to trigger a fresh `pull_request_target` run. PAT may lack `actions: write` to `gh run rerun` directly. [confirmed: 0]
- **Wait for CodeRabbit to finish before merging after /address-pr** -- CodeRabbit shows as `pending` StatusContext during review. Dismissing the old CHANGES_REQUESTED and merging while CodeRabbit is still reviewing the new push means its new findings land post-merge. Poll `gh pr checks` until CodeRabbit is no longer pending, then also verify `gh pr view --json reviewDecision` -- `gh pr checks` monitors CI status only, not review decisions. PRs #134, #172, #189. [confirmed: 2]

## Rebase / Git Workflow

- **`git checkout --theirs` for complete file rewrites during garbled rebase conflicts** -- when a feature branch rewrites a file and main added features to the old version, the diff algorithm produces cross-function conflict markers. Take incoming version with `--theirs`, then manually re-add main's features. [confirmed: 0]
- **Review fix commits conflict with themselves after earlier rebase integration** -- when rebase conflict resolution already incorporates a fix (e.g., adding `harden` to `_actionLabels`), the later fix commit tries to add the same thing and conflicts. Resolve by keeping the already-integrated version. [confirmed: 0]
- **Test assertions drift after rebase onto main** -- feature branch tests may assert pre-rebase default values. After rebase, run tests and update assertions to match main's current state (e.g., `"harden"` -> `"spec"` for NEEDS_SPEC action). [confirmed: 0]
- **Fork-based PRs require fetching from the correct remote** -- `gh pr view --json headRepositoryOwner` reveals which remote hosts the branch. Use `git fetch <fork-remote> <branch>` before checkout. [confirmed: 0]
- **`git rebase --continue` rejects `--no-edit`** -- use `GIT_EDITOR=true git rebase --continue` to skip the editor. [confirmed: 0]

## Input Normalization

- **Strip prefixes from string IDs before DB queries** -- `str(args.get("issue", handoff.issue))` may preserve `#` prefix (e.g., `"#123"`), but DB fields store plain integers/strings. Always `lstrip("#")` or cast to `int()` before comparisons. PR #177 CodeRabbit. [confirmed: 0]

## CI / GitHub Actions

- **GitHub `gh pr checks --json` returns `"SKIPPED"` not `"SKIPPING"`** -- the text-format output shows "skipping" but the JSON `state` field is `"SKIPPED"` (past tense). State maps must include both. Unmapped states silently default to `IN_PROGRESS`, causing 900s poll timeouts. File: `sova/git/pr.py:_GH_STATE_MAP`. [confirmed: 0]
- **`not is_passed` is not the same as `is_failed` for CI checks** -- skipped, cancelled, and neutral checks are not passed but not failures either. Use an explicit `is_failed` property checking only `FAILURE` and `TIMED_OUT` conclusions. File: `sova/git/pr.py:CICheck.is_failed`. [confirmed: 0]
- **GitHub CI workflows silently skip when a PR has merge conflicts** -- `mergeStateStatus: "DIRTY"` prevents workflow triggers even when `paths-ignore` doesn't apply. The checks never appear (not pending, just absent). Fix: rebase to resolve conflicts, then push -- CI triggers on the clean commit. Diagnose with `gh pr view N --json mergeStateStatus,mergeable`. [confirmed: 0]
- **Dual `push` + `pull_request_target` triggers cause SonarCloud race failures** -- both fire on feature branch pushes, submitting two reports. SonarCloud processes them sequentially; the loser is rejected with "a newer report has already been processed." Fix: remove feature branch patterns from `push` trigger; `pull_request_target` covers all PR analysis, `push: [main]` covers post-merge. File: `.github/workflows/sonarcloud.yml`. PR #179. [confirmed: 0]

## Dashboard Recovery

- **Handoff clearing must be after all preflight checks that can abort** -- clearing handoff before budget check or run record creation means the "Action Required" panel disappears even when the start fails. Place `clear_handoff` after `_create_task_run` succeeds (start_agent) or after prompt/pr resolution (start_command), but before `AgentProcess.spawn`. PR #179. [confirmed: 0]

- **`recover_stale_runs` must check handoff files before marking interrupted** -- if the agent wrote a handoff with `status: "awaiting_action"` after the run started, the agent completed its work. Mark as "done" and extract cost from `details.cost_usd`. File: `sova/dashboard/services/agent_recovery.py`. [confirmed: 0]
- **`has_projects()` silently triggers multi-project mode** -- when the project registry has entries, `create_app()` enters multi-project mode even without `--project`, skipping `set_project_dir()` on services. Services must fall back to `Path.cwd()` when `_default_project_dir` is None. File: `sova/dashboard/services/handoff_service.py:_resolve_project_dir()`. [confirmed: 0]
- **History step counts must use variant-specific pipeline lengths** -- hardcoding `len(DEVELOPER_PIPELINE)` shows "0/15" for triage (no pipeline), researcher (3 steps), and reviewer (no pipeline). Use `_PIPELINE_LENGTHS.get(variant)` and render "--" for None. File: `sova/dashboard/services/work_service.py`. [confirmed: 0]
- **Handoff files must be cleared by `start_agent`/`start_command`, not just `/handoff/execute`** -- task card buttons and manual start bypass the execute endpoint. Without clearing, stale "Action Required" panels appear inside running agent cards. File: `sova/dashboard/services/agent_lifecycle.py`. [confirmed: 0]

## Runtime / Agent Backend Abstraction

- **Runtime-specific prompt formatting is not abstracted** -- `start_agent()` builds prompts for Claude Code (bash-fenced CLI commands), but alternative runtimes (Aider) expect task descriptions, not shell commands. Prompt construction should be runtime-aware or delegated to the runtime itself. PR #181 SOVA review. [confirmed: 0]
- **`start_command` is Claude Code-specific but routes through generic runtime** -- Claude commands (`/{command}`, `.claude/commands/`) are meaningless to non-Claude runtimes. Guard with `runtime.name != "claude-code"` check before building command prompts. PR #181 CodeRabbit. [confirmed: 0]
- **Config metadata descriptions must match validation constraints** -- `step_timeout` description said "0 = no limit" but validator used `gt=0` (rejecting 0). Keep metadata and Pydantic constraints in sync. PR #181 CodeRabbit. [confirmed: 0]
- **Subprocess version checks must verify exit code** -- `_check_cli_available()` returned `True` regardless of `proc.returncode`. Non-zero exit means the tool is broken. PR #181 CodeRabbit. [confirmed: 0]

## Common Mistakes (tracked by occurrence)

- **`except X as exc:` + `log.warning(..., exc_info=True)` leaves `exc` unused** -- remove `as exc` or ruff F841 fires. (occurrences: 2) [confirmed: 1]
