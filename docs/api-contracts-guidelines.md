# API Contracts Guidelines

API contract patterns, request/response models, and integration conventions used across SOVA.

## Dashboard API (FastAPI)

### Router Organization

33 API routers in `sova/dashboard/routers/`, mounted under `/api` via `_register_api_routers()` in `sova/dashboard/app.py`:

| Router | Purpose |
|--------|---------|
| a2a | A2A protocol: Agent Card discovery and task management |
| agents | Multi-agent start, stop, status, output streaming, work items, and ranked next tasks |
| auth | LLM provider/agent runtime auth status and the Connections page |
| briefing | Dashboard page backing the awareness briefing |
| costs | Cost tracking from the database |
| dependencies | DAG visualization, ranked ready tasks, and chain queries |
| dependency_health | Outdated, deprecated, and vulnerable dependency tracking |
| feed | SSE stream, history, and briefing endpoints |
| fleet_insights | Cross-project analytics from FleetService |
| fleet_manager | Live fleet status and slot adjustment |
| handoff | Read, execute, and clear agent handoff actions |
| health | Read-only diagnostic checks for the settings page panel |
| issues | Change one tracker issue (its priority) |
| lifecycle | Issue lifecycle tracking and phase control |
| mcp | HTTP+SSE endpoint for agent self-inspection tools |
| memory | Agent memory entries from the database |
| models | Read-only LLM/runtime model enumeration |
| oversight | Status, runs, findings, run-now trigger, and finding actions |
| overview | Aggregated dashboard data |
| prs | PR tracker |
| queue | Priority-sorted issue queue |
| quota | CodeRabbit quota: rate-limit status, sync, and PR queue |
| reliability | Success rate, failure taxonomy, spend by outcome, and per-role monthly and per-step usage averages |
| resources | Resource monitoring data for agent runs |
| roles | CRUD for workflow definitions and role introspection |
| runs | Task run history from the database |
| settings | Config (grouped with default, stored and environment flags), reset to default, invariants, detected stack, installation status |
| setup | Project onboarding, scanning, and installation |
| spec | Read, approve, revise, skip, reject specs |
| supervisor | Status, manual poll trigger, decision log queries, approval plan, and persona |
| tasks | Open issues from the task source |
| telemetry | Receives run summaries from remote SOVA instances |
| work | Unified active tasks, run history, the Work board, and hidden issues |

### Response Format

Flat dicts with resource-centric keys, no global envelope:

```python
# Collections
return {"roles": [...], "builtin": [...]}
return {"queue": items, "total": len(items)}

# Action confirmations
return {"status": "ok", "slug": slug}
return {"status": "deleted", "name": name}

# Composite
return {"has_handoff": True, "handoff": data, "handoffs": [data]}
```

### Nullable Live CPU Reading

The live `cpu_percent` of an agent (`/api/resources/{run_id}/live`, the `agents` entries of `/api/resources/system`, and the agents listing) is `null` until its collector has a second sample: the first one has no CPU baseline, and `0.0` would be a reading never taken. The unit is psutil's: 100 is one full core and a multi-core process tree exceeds it, so no consumer clamps it to 100. Every consumer treats `null` as "no reading yet": the client renders a placeholder (`CPU —`, or `--` through the shared `formatCpuPercent()` in `app.js`), and the run chart leaves a gap at a null sample.

Stored readings use NULL the same way (migration 042 made `resource_samples.cpu_percent` and `resource_summaries.peak_cpu_percent`/`avg_cpu_percent` nullable). `/api/resources/{run_id}/samples` serves a `null` `cpu_percent` for the first sample, which is stored with its valid memory, I/O and thread data. A summary of a run that ended before its second sample keeps its true `sample_count` and its real memory and thread peaks, with `peak_cpu_percent`/`avg_cpu_percent` `null` (`/api/resources/{run_id}/summary`, a run's `peak_cpu_percent`, shown as `--` on the run page); `sample_count` is never a flag for "no CPU reading". The capacity recommendation reads only summaries whose CPU columns are non-null.

Fleet totals (`_aggregate_totals()`) sum only the numeric readings and say how partial the sum is: `agents_with_cpu_reading` of `agents_running`. `total_agent_cpu_percent` is `null` when agents run but none has a reading yet (and `0.0` only when no agent runs), which the capacity recommendation reads as no load from other projects.

CPU is measured from the CPU-time deltas of processes alive across two samples, so a short-lived descendant that starts and exits inside one sampling interval (ruff, git, a quick pytest worker) never contributes: the reading undercounts a tool-heavy agent. A new consumer of `ResourceSample.cpu_percent` must handle `None`; `tests/test_resource_dashboard.py::TestNoCpuReadingYet` and `TestCpuRenderingInTheClient` cover each current one.

### State Display Fields

Any payload that carries a `state` string for the UI also carries `state_label` and `state_color`, both produced by `state_label()`/`state_color()` in `sova/dashboard/services/work_state.py` (work items and their `pr_details`, open PRs, queue items, `/api/tasks/issues`, dependency-graph nodes, and `pr_state_label`/`pr_state_color` for a node's PR). A PR's label and color are always built from the same raw computed state, never one from the collapsed graph `pr_state`, so the two cannot name different states. `state_color` is a token (`gray`, `gray-dim`, `yellow`, `peach`, `green`, `green-strong`, `purple`, `blue`, `red`, `lavender`), never CSS or a hex: `app.js` maps a token to badge classes (`stateBadgeClasses`) or a hex (`stateHex`). A state the module does not know comes back humanized and `gray` rather than blank, so register a new state there instead of adding a per-page table. `stateHex` gives each token that needs its own fill a distinct hex (`green-strong` reads `--ctp-green-strong`, a deeper shade than `green`), so a graph node still in review and a finished one, or an Approved PR and a Ready to Merge one, never share a dot; only `gray-dim` is drawn as plain `gray` on a canvas, where it differs from `gray` by badge text shade alone. A client that caches a render keyed on a node's fields must key on `state_color`, `pr_state_label` and `pr_state_color` as well as `pr_state`, which is the collapsed value (`supervisor.html`'s `_computeGraphKey()`).

### Next Tasks and Work Item Suitability

`rank_next_tasks()` (`sova/supervisor/suitability.py`) is the one pure, LLM-free function that orders the issues an agent should pick up next. Two endpoints read it, both through `work_item_service.py` (`rank_graph_entries()` and `ranked_task_payload()`), so they cannot disagree on order or entry shape:

- `GET /api/agents/next-tasks?limit=5` (`limit` 1 to 50) returns `{tasks, unclassified_count}`.
- `GET /api/dependencies/ready[?milestone=]` returns `{ready}`, unbounded. **Breaking change**: it used to list every `autonomous` or `unclassified` issue in any non-excluded state in id order. It now lists only `autonomous` issues in `triaged` or `researched` state with no running agent and no open PR, ranked. Nothing in-repo outside tests read the old shape. With `?milestone=X` (a number or a title, as `gh issue list --milestone` takes it) the list holds only the issues that fetch returned, which the graph records as `requested_issues`; the endpoint never compares `X` with an issue's `milestone` string, so a number or a differently-cased title cannot turn the list empty. The filtered graph also carries the out-of-milestone dependencies it fetched individually (so blockers classify), and those are never offered. `unblocks` then counts dependents inside that milestone's subgraph, so it can be lower than the same issue's count from `next-tasks`, which ranks over the full graph.

Both share one entry shape: `id`, `title`, `state`, `state_label`, `state_color`, `milestone`, `url`, `priority` (the level), `mode`, `rank` (1-based position in the full ordering, even when `limit` truncates), `why`. Order is priority rank (`Priority.rank`, no local table), then most transitive dependents unblocked, then milestone phase (`sova/adapters/milestone.py`, `Phase 0` before `Phase 1`, no milestone last), then issue number, so the order is total and independent of input order. `why` is an ordered token list: `priority:<level>` always, `unblocks:<n>` only when n is above 0, `phase:<n>` only when the milestone parses, `state:<state>` always. Unblock counting skips `DONE` and epic dependents and does not walk through them, and is cycle-safe.

`unclassified_count` covers exactly the unclassified issues in the ranker's own pool: `triaged` or `researched`, no running agent, no open PR. Each one joins `tasks` once it gains `agent:ready`. Backlog and needs-spec issues are not counted; a count for those would be a separate field.

**Local assessment records are not read.** An issue with only a `sova assess set` or `/find-task` record and no `agent:ready` label classifies `unclassified`, matching its graph node. If local records are ever supported, the graph node and the ranker must change together, both checking `local_assessment_applies()`.

A graph failure is an error, never an empty list: `next-tasks` and `ready` return 503 for a config or adapter failure and 500 otherwise, because an empty list means "nothing to do". Both endpoints take their running-agent and open-PR exclusions from one helper, `next_task_exclusions(project_dir)` in `work_item_service.py`, called with the explicit project directory, so only the shared ranker decides what they return. Its lookups fail open on any exception (state-only filtering, logged), parse an agent's issue whether it is stored as `507` or `#507`, and an open PR excludes every issue it links to (`linked_issues`), not only its primary one. An empty pool is 200 with `tasks: []` and `unclassified_count: 0`.

Every task entry in `GET /api/agents/work-items` carries `suitability: {mode, reasons, blocked_by}`, classified by `assess_suitability()` from the cached dependency graph's own `Task` for that issue, so it matches the graph node by construction. `mode` reflects labels and dependencies only and ignores task state. An issue the classifier cannot resolve dependencies for classifies `blocked`, never `autonomous`, and the reason says why: `assess_suitability()` reports every such case as `{"source": "graph", "value": "unavailable"}`, and the work-items endpoint (`_classify_queue_item()`, the only place that knows whether a graph exists) refines one of them to `{"source": "graph", "value": "missing"}`: a graph was built but lacks the issue (one created since it was cached, or a closed issue outside the closed-issue window). `unavailable` therefore means there is no graph (it failed to load, was never built, or is in its failure backoff) or the id does not parse as an issue number, so a reader can tell a tracker outage from a gap. A label reason (epic, human-only, on-hold) is never replaced. Every open issue the tracker lists is a node of a freshly built graph, however old: the build lists the 500 most recent issues (open and closed) and then always lists every open issue on its own, paginated, merging in the ones the window left out, so a ready issue older than the window is still ranked. The second listing is not conditional on a full window because the adapter, not the build, decides how many issues a listing returns (Jira's unpaginated listing returns at most 50 whatever limit is asked for), so a short window is no proof it is whole. A failed second listing fails the build, and so does an empty one while the window holds an open issue (the Jira paginated path returns `[]` for a failed page), since a graph that silently lacks open issues reads as a correct answer. Label-driven modes (epic, human, on-hold) still take precedence. Standalone PR items have no issue to classify, so their `suitability` is `null`. The graph load behind `work-items` is bounded because the Agents page polls it: the poll waits at most 3 seconds, concurrent polls share one build per project, a build slower than that keeps running in the background and warms the 180s graph cache, and meanwhile the response classifies from the previous build's graph (kept per project until a build fails), so the cache expiring or an agent spawn invalidating it does not flip every task card to `blocked` for one poll; only a project that has never built a graph, or whose last build failed, classifies `blocked` with the `unavailable` reason. A build is capped at 120 seconds (a build that hits the cap is logged as `work_items.graph_failed`, not as the routine `work_items.graph_timeout` of a poll's own 3 second wait running out), and a failed build is not retried for 30 seconds so a tracker outage does not repeat the full issue fetch on every poll. The poll costs tracker calls: the server rebuilds the work items about once a minute while a tab has a socket open (the Agents page no longer polls `work-items` itself), and that rebuild starts a graph build whenever the cache has expired, so an open page adds one build per 180 seconds per project (about 20 an hour, plus one after each agent spawn or epic close), each a 500-issue listing, the open-issue listing, and one `gh issue view` per dependency outside the listing. The shared build and the cache bound this to one build per project whichever consumers ask, but the budget is the tracker's own API rate limit, shared with every agent. `next-tasks` and `ready` (without `?milestone=`) await that same shared build through `load_ranking_graph()`, so a cold-cache work-items poll and a ranking request run one fetch between them. They have no wait limit of their own beyond the 120 second build cap, and within the 30 second backoff they raise a new error chained to the recorded failure (`GraphConfigError` stays a `GraphConfigError`, so it remains a 503; anything else becomes a `RuntimeError`, a 500) instead of fetching again, since they must fail loudly rather than degrade. The error is new on every call because re-raising the one stored exception would add each request's frames to its shared traceback. At shutdown the lifespan calls `cancel_graph_builds()`, so a build stuck in a tracker call does not outlive it. A `?milestone=` graph is a rare manual build that bypasses the graph cache, so it is built on its own.

### Work Board

`GET /api/work/board[?milestone=&owner=all|mine]` returns `{stages, columns, agents, api_health, github_user, jira_display_name, milestones, up_next, more_ready, queued, queue_order, running, other_runs, review, needs, yours, blocked, done_today, chains, other_blocked, hidden, unavailable}` from `work_board_service.build_board()`. `columns` lists every stage in board order as `{stage, label, color, description, entries, column}`: `entries` names the payload key holding the stage's entries and `column` the stage whose column shows them (`yours` shows in `needs`, `blocked` in none). `agents` is `{running, limit}` (the pool count a start is checked against), `milestones` the milestones of the issues ranked for Up next. Every entry also carries `depends_on` and `dependents` (open, non-epic issue numbers) and `milestone`; work-item entries add `mode`, `assignees`, `jira_key`, `created_at` (when the issue was opened, or `null`), `pr` (`{number, url, state, state_label, state_color, ci_state, mergeable, merge_state, is_draft, threads_resolved, threads_total, additions, deletions, changed_files, author, assignees}` or `null`), `last_run` (`{run_id, role, status, ended_at}` or `null`) and `actions` (the work item's primary and secondary actions for its state, primary first, each `{id, label, style, handler, handler_args}` plus `merge_blockers` on an Integrate action, limited to the handlers the page runs: `start_agent`, `run_command`, `trigger_rebase`). Every issue is in exactly one column; each entry carries `number, title, url, stage, state, state_label, state_color, priority, start` (`number` is `null` for a PR with no issue), and the client keeps no stage table. `start` is `work_state.start_action()`: the `start_agent` action the issue's state calls for (Triage on backlog, Research on triaged, Develop on researched, Resume on in_progress), or `null` for a state with a PR, a parked, human-only or done state; Start, Start now and a failed run's card run it, so a triaged issue gets the researcher, not a developer its Gate 3 would refuse. `up_next` holds at most 5 entries (the unchanged `rank_graph_entries()` order, filtered to issues whose stage is `next`, not hidden, and assigned to the configured owner when `owner=mine`) with `rank`, `milestone`, `unblocks` and `why`; `more_ready` counts the ready issues beyond them and is never negative. `milestone` limits the ranking only. `running` adds `run_id, role, step_name, step_index, total_steps, ci_state, elapsed_seconds, cost_usd`; `other_runs` lists the running runs no `running` entry shows (a planner run, or one on an issue the work items leave out) as `{run_id, issue_number, pr_number, role, pipeline_variant, run_label, step_name, elapsed_seconds, started_at}`, oldest first, so each still has a View log and Stop when supported; `review` adds `pr_number, ci_state, threads_resolved, threads_total`; `needs` adds `kind` (`merge`, `review_budget`, `spec`, `stale_label`, `failed_run`), `reason`, `pr_number` and the latest run's `run_id` and `role`; `yours` adds `reason` and `reason_source` (the label or record the classifier reported), and `session_command` for an interactive issue; `blocked` adds `blocked_by`; `done_today` entries (one per issue a PR merged since server-local midnight finished, plus one number-less entry per PR whose merge left its issue open, newest first) add `pr_number` and `merged_at`. `chains` lists only the dependency chains (two or more open, non-epic issues, dependencies first) containing an issue on the board, and `other_blocked` counts open issues still waiting on a dependency that are neither on the board nor in a listed chain. A graph failure is a 503 (config) or 500, never an empty board; the sources that only enrich it fail open and `unavailable` names them (`done_today`, `open_prs` when the open-PR lookup failed, so a PR that links an issue second cannot keep it out of Done, `active_runs` when the run lookup failed, so `running` entries lack step, progress and cost details, and `queue_prune` when removing closed issues from the saved queue failed, so the board hides them but the stored queue still holds them). `queue_order` is the whole saved queue (closed issues pruned) as issue numbers, so a card's Move up and Move down read its place in the order a move acts on, including a queued issue another column shows. A queued issue is never offered in `up_next`, even when the work items lack it, and takes the stage `work_stage()` gives its graph state (a queued issue the graph shows in progress is in `running`, not `queued`). An issue two PRs merged today link appears once in `done_today`, with the most recent merge, and an issue in `done_today` is in no other column, since `done` is the first stage that applies and the cached work items can lag a merge. That holds only when a merged PR finished the issue: an issue the graph still lists as open (on_qa counts as finished, and so does an issue a PR closes, or whose own work-item PR merges, within `GRAPH_CACHE_TTL` plus a 60 second margin for the graph build (about 240 seconds) of the merge, since the graph lags that close; after that window the graph is trusted, so a reopened issue keeps its own column, and an issue the graph shows closed is done even when an open PR links it second), or that an open PR other than the merged ones links (a "Part of" phase PR; an open PR counts for every issue it links, not only its first), keeps its own column, and so does an issue the graph lacks whose work item is still open. Each of its merged PRs shows in `done_today` as its own entry with `number` `null`, the PR link as `url` and no `jira_key`, once per PR even when the PR links several such issues, and not at all when the same PR also finished another issue it links (that issue's numbered entry shows the merge).

With `owner=mine`, Up next filters before the five-card limit; each entry's `mine` flag is computed by the server and used by the client for other columns and dependency chains. Issue assignees match the configured identity for their tracker; PR authors and assignees match the GitHub user. GitHub PR authors and assignees appear as avatars, while Jira assignees appear by display name. Active tracked runs, including rebases, put an issue with an open PR in Running before PR review state is considered. A follow-up run suppresses the Done today card of an issue a merge finished; a merge on an issue that is still open shows as a number-less entry even while a run owns the issue. In review includes open PRs awaiting CI or review, draft or conflicted PRs, changes requested, and tracker issues labeled in review; it does not imply an unresolved review thread. On a Jira board, open and merged PRs must link a full key from the configured project; a numeric suffix from another Jira project cannot create a card or exclude an Up next issue. Jira linked PR cards use the tracker issue title and URL when the graph has the issue. The Mine filter keeps the prior board hidden while an owner change loads, including when the refresh fails.

`GET /api/work/search?q=` returns `{results, more}`: the open issues matching `q`, on the board or not, from the dependency graph the board reads (no tracker call of its own). A number (`1418` or `#1418`) matches that issue first, then the issues whose number starts with it; words match the titles holding every one, ignoring case; newest first within each, at most 20 (`more` counts the rest). Each result is a board entry without `stage` (the state alone cannot place an issue the way the board does, so the board's columns are the authority; `start` is included, and `actions` holds it) plus `depends_on`, `dependents` and `milestone`, so the drawer can open an issue the board does not show. `q` is at most 200 characters (a longer one is a 422). A missing or empty `q` returns no results without reading the graph; a graph failure is a 503 (config) or 500, never an empty list.

`POST /api/agents/start` with `role: "developer"` (or no role, checked only when `dispatch()` would route the issue to the developer: BACKLOG, TRIAGED and IN_REVIEW still start triage, the researcher and the reviewer), no PR and no `force` reads the issue first and refuses one whose state `DeveloperRole` would refuse (`check_developer_state()`) with a 409 whose `detail` is `{code: "state_not_ready", message, state, next_role}`, `next_role` being the role `role_for_state()` gives the state (`researcher` for triaged) or `null`. An unreadable issue fails open to the role's own gate.

`PUT /api/supervisor/queue` takes `{issue_numbers, based_on?}`. Without `based_on` it replaces the stored queue outright. With `based_on` (the queue the new order was computed from, as the Work board's Move up and Move down send it) the write is rebased onto the stored queue, so a concurrent add or removal is kept, and the response carries the queue as saved.

`GET /api/work/actions` returns `{actions: [...]}`, the dashboard actions still running and the recent results (`{id, kind, label, issue, pr, status, message, started_at, ended_at}`, `status` one of `running`, `done`, `failed`, and a failure may add `fix`, the work item action that remedies it in `work_state`'s `{id, label, style, handler, handler_args}` shape, which the card shows as a button); a success stays 20 s, a failure until `DELETE /api/work/actions/{id}` (404 for a running or unknown action). A tracked action that fails with anything but an `HTTPException` records "Internal error", the catch-all's text. `POST /api/work/issue/{n}/rebase` answers 202 `{status: "started", action_id}` at once and runs the rebase in the background, or `{status: "running", action}` while one already runs for the issue. Its failure message comes from the result's `cause` (`services/rebase_outcome.py`: one fixed explanation per `AutoRebaseFailure` and `RebaseFailure` value, the unresolved files named), never from its `error`, and its `fix` is a Retry rebase for a transient cause, an Address PR run for one the auto-rebase cannot get past (conflicts, the commit cap, a git stop, failing pre-push checks), and none when only the user can act (changes left in the stash). `POST /api/agents/{run_id}/stop` answers 409 with the reason when the dashboard does not hold the run (started elsewhere, or ended). A start refused because every slot is taken is a 409 whose `detail` is `{code: "no_free_slot", message, running}`, and it leaves no failed action, since the page queues the issue instead. An action from an unprefixed request (single-project mode) belongs to the default project, the key the status socket uses, so `actions_changed` reaches its tabs.

`POST /api/work/hidden/{number}` and `DELETE /api/work/hidden/{number}` add or remove an issue in `dashboard.hidden_issues` and return `{hidden: [...]}`; both are idempotent and accept an issue the tracker no longer has. Hiding changes no label, tracker state, queue entry or supervisor decision. `PUT /api/issues/{number}/priority` takes `{priority}` (one of `blocker, critical, high, medium, low, undefined`): 200 on success, 400 with the adapter's message when the tracker has no such level (Jira with no matching name among the priorities the issue's project allows, and always for `undefined`), 502 on a tracker failure, 503 when config is unavailable. It drops the graph, queue and shared-build caches so the next board request ranks with the new priority. On GitHub the whole read, add and delete runs under one lock per repository and issue shared by every adapter instance in the process, and label names match without regard to case, as GitHub does.

### Status Socket Messages

`/ws/agents/status` (also `/p/{slug}/api/ws/agents/status`) is the one socket a browser tab opens; `app.js` shares it between the sidebar and every page. Each message is a JSON object with a `type`:

| `type` | Body | Sent |
|--------|------|------|
| `status_update` | `runs: [...]` | every 2 s |
| `status_unavailable` | none | the status fetch failed; the socket is up but proves nothing is fresh |
| `agent_finished`, `graph_invalidated` | per `broadcast_event()` | on the event |
| `feed` | `event`: a feed event as `FeedEvent.to_dict()` | each event the process emits, in order (process-wide: a feed event carries no project, so every project's sockets receive every event, as with `/feed/stream`) |
| `activity` | `data`: the `GET /agents/active` payload | when it differs from the last one sent |
| `activity_unavailable` | none | the activity build failed; the client marks its value unknown |
| `handoff` | `data`: the `GET /handoff` payload | when it differs from the last one sent (checked every 6 s) |
| `handoff_unavailable` | none | the handoff build failed; the client shows a gray "Checkpoint status unknown" banner in place of the checkpoint banner without refetching (the dot keeps the activity state) |
| `actions_changed` | none | a dashboard action started or ended (`services/action_registry.py`); the client re-fetches `GET /work/actions` |
| `work_items_changed` | none | a rebuild of the work-items snapshot produced a different payload (or recovered from a failed rebuild); the client re-fetches `GET /agents/work-items` |
| `handoff_changed` | none | reserved: the client re-fetches `/handoff` on receipt; the server does not emit it (the `handoff` message carries the payload) |

A change message carries no payload: the HTTP endpoint stays the single source of payload shape. `activity` and `handoff` payloads are encoded once with `jsonable_encoder()`, the form the HTTP endpoint serves, and the same object is both digested and sent. `GET /agents/work-items` and the project-wide `GET /handoff` answer from a last-result snapshot (`snapshot_service.py`): the last good payload at once, a background rebuild (one in flight per kind and project) when it is older than 5 s, and an explicit error, never an empty payload, when the very first build fails. `GET /agents/work-items` adds `snapshot: {age_seconds, refresh_failed, last_error}` (kept out of the change digest, as are the clock-driven `elapsed_seconds`, `age_seconds`, `down_seconds` and `cooldown_seconds`); `refresh_failed` means the latest rebuild failed and the payload is older than the refresh interval. A handoff write (`/handoff/execute`, `/handoff/clear`) drops the handoff snapshot so the next read waits for a fresh build. A read for one issue (`GET /handoff?issue=`) is built on the spot. A message never replaces a connect-time catch-up: a new socket receives nothing until the next change, so a client fetches the current value over HTTP in its `onOpen`. `GET /feed/stream` (SSE) remains for non-browser clients; the browser no longer opens it.

### Agent Output Sequence Numbers

`GET /agents/{run_id}/output` returns `seq` next to `lines` and `total`, and the `connected` event of `GET /agents/{run_id}/output/stream` carries `{"seq": N}`. Both are `AgentState.output_seq`, the count of lines the in-memory agent has buffered over its life (the 5000-line deque evicts, so its length cannot stand in). `_buffer_line()` counts and publishes a line in one step, with no `await` between, and the endpoints read the number with no `await` before they read the lines or subscribe, so a snapshot at `seq` and a stream connected at `seq` overlap by exactly the difference. The log viewer uses that to drop the lines its backfill and the stream both carry, on a first open as well as a tab return. The field is `null`/absent when the agent is no longer in memory (the log then comes from disk), and a client must fall back to text matching there. The field is additive: a client that ignores it behaves as before. The count is only exact if the stream delivered every line, and `OutputStreamService.publish()` drops a line for a subscriber whose queue is full, so the stream also emits an `event: gap` (empty JSON object) ahead of the next line after any drop; a client that relies on `seq` must treat it as "this stream has a hole" and re-read the run instead of continuing (the log viewer abandons the stream and polls from line 0).

### Request Models (Pydantic v2)

All POST/PUT endpoints use `BaseModel` subclasses with validators:

```python
class StartAgentRequest(BaseModel):
    issue: str = ""
    role: str | None = None
    force: bool = False
    resume_run_id: int | None = None
    pr_number: int | None = None

    @field_validator("issue")
    @classmethod
    def validate_issue(cls, v: str) -> str:
        value = v.strip()
        if value and not value.isdigit():
            raise ValueError("issue must be a numeric string or empty for issue-less runs")
        return value
```

### Error Handling

An error is a non-2xx status with a `{"detail": ...}` body, raised as `HTTPException`; never a 200 body carrying `{"error": ...}`. An `ok: false` field is only for a check whose answer is "unavailable" (a connection test whose probe ran and returned that the component is down: a 200), never for a failure to run the request (a probe that raised, or a component that could not be built: a 502 or 400 with a fixed detail). An unexpected exception is left to propagate: the catch-all handler in `create_app()` logs it and answers 500 `{"detail": "Internal error"}` (full rules: docs/error-handling-guidelines.md, "Dashboard Route Failures"). `POST /agents/planner/create-issues` answers `{"created": [...]}` on success (no `errors` key; the page was its only consumer), and a partial failure is a 502 whose `detail` is `{message, results}`. `POST /settings/labels/create` follows the same rule: a 502 whose `detail` is `{message, created, errors, errors_truncated}` when any label could not be created (the others stay created, each error a fixed per-label reason, never `gh` output), and `POST /supervisor/plan/approve` answers 503 when the engine cannot be built (nothing ran, and the taken decisions are merged back into the plan, never written over a plan the daemon saved meanwhile).

```python
raise HTTPException(status_code=404, detail="Role not found: {name}")
raise HTTPException(status_code=409, detail="Run already in terminal state")
raise HTTPException(status_code=502, detail="Failed to create the issue on the tracker")
```

**SonarCloud S8415**: every endpoint that raises `HTTPException(status_code=N)` must declare `responses={N: {"description": "..."}}` in the route decorator. Without it, the quality gate fails.

```python
@router.post("/fleet-insights/propose-issue", responses={502: {"description": "Tracker rejected the issue"}})
async def propose_issue(req: ProposeIssueRequest):
    ...
    raise HTTPException(status_code=502, detail="Failed to create the issue on the tracker") from exc
```

### HTTP Status Codes

| Code | When |
|------|------|
| 200 | Success (default) |
| 400 | Invalid request beyond Pydantic scope (bad DAG, missing project), a missing or unparsable `sova.toml`, or a component the config cannot build (`ComponentMisconfiguredError.user_message`), or no opener for the editor |
| 404 | Resource not found (run, role, spec, batch) |
| 409 | State conflict (run already terminal, role name taken, the task queue changed since the edit read it) |
| 422 | Pydantic validation failure (auto-generated by FastAPI), or a project config failing validation (the detail names the fields) |
| 500 | Unexpected failure, from the catch-all handler only (fixed body `{"detail": "Internal error"}`) |
| 502 | The route was asked to act on an external system and that call failed or was rejected (a tracker write, a `gh` command, a provider probe that raised or a component that failed to build; a probe that ran and reported the component down is the 200 `ok: false` answer), with a fixed detail naming it |
| 503 | The route serves data from a source it could not read or write, so a retry may succeed (task source or fleet scan reads, provider enumeration, the project config or database) |

The 502 and 503 split is one rule, stated in docs/error-handling-guidelines.md, "Dashboard Route Failures": 502 is an action that failed, 503 is a data source that is unavailable.

## gh CLI Integration

### JSON Parsing Rule

Always wrap `json.loads(result.stdout)` in try/except. `gh` can return empty stdout on success:

```python
try:
    data = json.loads(result.stdout)
except (json.JSONDecodeError, TypeError):
    return []
```

### `gh pr create` Returns URL, Not JSON

Unlike `gh pr view --json`, `gh pr create` outputs the PR URL as plain text:

```python
pr_url = result.stdout.strip()
pr_number = int(pr_url.rstrip("/").split("/")[-1])
```

### PR Reviews vs Comments

| Method | API | Result |
|--------|-----|--------|
| `post_pr_comment()` | `gh pr comment` | Conversation-level comment in timeline |
| `post_pr_review()` | `gh api repos/{repo}/pulls/{pr}/reviews` | Formal review with inline code comments |

`post_pr_review()` sends JSON via stdin (`--input -`). If inline comments fail (422 from GitHub), it retries body-only. GitHub returns 422 for `APPROVE`/`REQUEST_CHANGES` on own PRs; always use `event="COMMENT"` and state the verdict in the body text.

### GraphQL

Use `gh api graphql -f` for string parameters:

```python
result = await self._gh("api", "graphql", "-f", f"query={mutation}", "-f", f"projectId={id}")
```

## Adapter ABC Contract

`TaskAdapter` in `sova/adapters/base.py` defines 22 abstract methods (`set_priority` takes a `Priority` level and raises `PriorityUnavailableError` when the tracker has no equivalent):

| Category | Methods |
|----------|---------|
| Read | `list_tasks`, `get_task`, `get_state`, `get_pr_reviews` |
| State | `transition_state`, `assign`, `add_label`, `remove_label`, `get_available_transitions` |
| Content | `post_comment`, `post_pr_comment`, `post_pr_review`, `edit_body`, `link_pr` |
| Create | `create_issue` |
| Priority | `set_priority` |

Constructor: `__init__(self, repo: str, github_user: str = "")`.

Supporting dataclasses: `Task`, `TaskFilters`, `PRReview` (with `is_bot` field).

## LLM Output Formats

Two CLI output modes for Claude Code (`sova/llm/providers/claude_code.py`):

| Mode | Flag | Format |
|------|------|--------|
| Batch | `--output-format json` | `{result, total_cost_usd, usage, session_id}` |
| Stream | `--output-format stream-json` | JSONL: `type=assistant`, `content_block_delta`, `result` |

Stream parsing in `sova/dashboard/services/agent_output.py`:
- `assistant` messages: concatenate `content[].text` blocks
- `content_block_delta`: extract `delta.text`
- `result`: extract cost for tracking
- Malformed lines: return raw text (graceful degradation)

## Handoff Models

Two Pydantic models for inter-agent state (`sova/ipc/handoff.py`):

**HandoffAction**: button definition:
- `id`, `label`, `style` (`approve|neutral|danger`), `mode` (`agent|claude-command|shell`)
- `command`, `args`, `auto_execute`

**DashboardHandoff**: file-based state:
- `source`, `status` (`awaiting_action|completed|failed`), `issue`, `pr_number`
- `summary`, `next_actions` (list of HandoffAction)

Written to `handoff-{issue}.json` per issue for parallel isolation.
