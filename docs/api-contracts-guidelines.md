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
| lifecycle | Issue lifecycle tracking and phase control |
| logs | Filterable log viewer |
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
| settings | Config, invariants, personas, installation status |
| setup | Project onboarding, scanning, and installation |
| spec | Read, approve, revise, skip, reject specs |
| supervisor | Status, manual poll trigger, decision log queries, approval plan, and persona |
| tasks | Open issues from the task source |
| telemetry | Receives run summaries from remote SOVA instances |
| work | Unified active tasks and run history |

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

Every task entry in `GET /api/agents/work-items` carries `suitability: {mode, reasons, blocked_by}`, classified by `assess_suitability()` from the cached dependency graph's own `Task` for that issue, so it matches the graph node by construction. `mode` reflects labels and dependencies only and ignores task state. An issue the classifier cannot resolve dependencies for classifies `blocked`, never `autonomous`, and the reason says why: `assess_suitability()` reports every such case as `{"source": "graph", "value": "unavailable"}`, and the work-items endpoint (`_classify_queue_item()`, the only place that knows whether a graph exists) refines one of them to `{"source": "graph", "value": "missing"}`: a graph was built but lacks the issue (one created since it was cached, or a closed issue outside the closed-issue window). `unavailable` therefore means there is no graph (it failed to load, was never built, or is in its failure backoff) or the id does not parse as an issue number, so a reader can tell a tracker outage from a gap. A label reason (epic, human-only, on-hold) is never replaced. Every open issue the tracker lists is a node of a freshly built graph, however old: the build lists the 500 most recent issues (open and closed) and then always lists every open issue on its own, paginated, merging in the ones the window left out, so a ready issue older than the window is still ranked. The second listing is not conditional on a full window because the adapter, not the build, decides how many issues a listing returns (Jira's unpaginated listing returns at most 50 whatever limit is asked for), so a short window is no proof it is whole. A failed second listing fails the build, and so does an empty one while the window holds an open issue (the Jira paginated path returns `[]` for a failed page), since a graph that silently lacks open issues reads as a correct answer. Label-driven modes (epic, human, on-hold) still take precedence. Standalone PR items have no issue to classify, so their `suitability` is `null`. The graph load behind `work-items` is bounded because the Agents page polls it: the poll waits at most 3 seconds, concurrent polls share one build per project, a build slower than that keeps running in the background and warms the 180s graph cache, and meanwhile the response classifies from the previous build's graph (kept per project until a build fails), so the cache expiring or an agent spawn invalidating it does not flip every task card to `blocked` for one poll; only a project that has never built a graph, or whose last build failed, classifies `blocked` with the `unavailable` reason. A build is capped at 120 seconds (a build that hits the cap is logged as `work_items.graph_failed`, not as the routine `work_items.graph_timeout` of a poll's own 3 second wait running out), and a failed build is not retried for 30 seconds so a tracker outage does not repeat the full issue fetch on every poll. The poll costs tracker calls: the Agents page requests `work-items` every 30 seconds while it is visible, and that request starts a graph build whenever the cache has expired, so an open page adds one build per 180 seconds per project (about 20 an hour, plus one after each agent spawn or epic close), each a 500-issue listing, the open-issue listing, and one `gh issue view` per dependency outside the listing. The shared build and the cache bound this to one build per project whichever consumers ask, but the budget is the tracker's own API rate limit, shared with every agent. `next-tasks` and `ready` (without `?milestone=`) await that same shared build through `load_ranking_graph()`, so a cold-cache work-items poll and a ranking request run one fetch between them. They have no wait limit of their own beyond the 120 second build cap, and within the 30 second backoff they raise a new error chained to the recorded failure (`GraphConfigError` stays a `GraphConfigError`, so it remains a 503; anything else becomes a `RuntimeError`, a 500) instead of fetching again, since they must fail loudly rather than degrade. The error is new on every call because re-raising the one stored exception would add each request's frames to its shared traceback. At shutdown the lifespan calls `cancel_graph_builds()`, so a build stuck in a tracker call does not outlive it. A `?milestone=` graph is a rare manual build that bypasses the graph cache, so it is built on its own.

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

**Pattern 1 (preferred): HTTPException**

```python
raise HTTPException(status_code=404, detail="Role not found: {name}")
raise HTTPException(status_code=409, detail="Run already in terminal state")
```

**Pattern 2 (legacy): dict with "error" key, 200 OK**

```python
return {"error": "No lifecycle found", "issue_number": issue_number}
```

New endpoints should use Pattern 1.

**SonarCloud S8415**: every endpoint that raises `HTTPException(status_code=N)` must declare `responses={N: {"description": "..."}}` in the route decorator. Without it, the quality gate fails.

```python
@router.get("/quota/pr-queue", responses={500: {"description": "Failed to fetch PR queue status"}})
async def pr_queue_status():
    ...
    raise HTTPException(status_code=500, detail="Failed to fetch PR queue status")
```

### HTTP Status Codes

| Code | When |
|------|------|
| 200 | Success (default) |
| 400 | Invalid request beyond Pydantic scope (bad DAG, missing project) |
| 404 | Resource not found (run, role, spec, batch) |
| 409 | State conflict (run already terminal, role name taken) |
| 422 | Pydantic validation failure (auto-generated by FastAPI) |

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

`TaskAdapter` in `sova/adapters/base.py` defines 15 abstract methods:

| Category | Methods |
|----------|---------|
| Read | `list_tasks`, `get_task`, `get_state`, `get_pr_reviews` |
| State | `transition_state`, `assign`, `add_label`, `remove_label`, `get_available_transitions` |
| Content | `post_comment`, `post_pr_comment`, `post_pr_review`, `edit_body`, `link_pr` |
| Create | `create_issue` |

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
