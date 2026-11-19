"""The Work page (#1470) and the action state behind its buttons (#1146).

Covers the page route and the /agents redirect, the board columns coming from GET /api/work/board with no
state-to-column table in the page, the fields the cards and drawer read, the board refreshing only from the
shared socket, Start falling back to the queue when no slot is free, the server's list of running actions
(rebase in the background, the Stop answer for a run the dashboard did not start), and the app.js helper
that shows an action's running state and result on its card. The page modules run under node against stubs:
there is no browser in the test suite, so the look of the page was checked by hand against the mockup.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from sova.adapters.base import Task, TaskState
from sova.adapters.priority import Priority
from sova.dashboard.services import action_registry, work_board_service
from sova.dashboard.services.work_state import WorkItemState, WorkStage, stage_color, stage_label
from sova.supervisor.dependency_graph import DependencyGraph
from tests.conftest import DASHBOARD_DIR, csrf_request_headers

_STATIC = DASHBOARD_DIR / "static"
_WORK_JS = _STATIC / "js" / "pages" / "work"
_TEMPLATES = DASHBOARD_DIR / "templates"
PROJECT = Path("/tmp/work-page-test-project")


def _require_node() -> None:
    if shutil.which("node") is None:
        if os.environ.get("CI"):
            pytest.fail("node is required in CI to run the Work page modules")
        pytest.skip("node is required to run the Work page modules")


def _work_sources() -> dict[str, str]:
    return {p.name: p.read_text(encoding="utf-8") for p in sorted(_WORK_JS.glob("*.js"))}


@pytest.fixture
async def client(dashboard_app) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=dashboard_app), base_url="http://test") as ac:
        ac.headers.update(csrf_request_headers(ac))
        yield ac


@pytest.fixture(autouse=True)
def _fresh_state() -> Iterator[None]:
    action_registry.reset_actions()
    work_board_service.reset_board_cache()
    yield
    action_registry.reset_actions()
    work_board_service.reset_board_cache()


# Page, redirect, sidebar ----------------------------------------------------


class TestPage:
    async def test_work_serves_the_page_with_header_board_chains_and_drawer(self, client: AsyncClient) -> None:
        resp = await client.get("/work")

        assert resp.status_code == 200
        for marker in ('id="work-header"', 'id="work-board"', 'id="work-chains"', 'id="work-drawer"'):
            assert marker in resp.text
        assert "<title>Work - SOVA</title>" in resp.text

    async def test_agents_redirects_to_work(self, client: AsyncClient) -> None:
        resp = await client.get("/agents", follow_redirects=False)

        assert resp.status_code == 301
        assert resp.headers["location"] == "/work"

    async def test_the_sidebar_names_the_page_work(self, client: AsyncClient) -> None:
        html = (await client.get("/work")).text

        assert 'href="/work"' in html
        assert 'aria-label="Work"' in html
        assert 'aria-label="Agents"' not in html

    def test_the_old_task_list_and_kanban_are_gone_everywhere(self) -> None:
        agents_dir = _STATIC / "js" / "pages" / "agents"
        assert not (agents_dir / "work-items.js").exists()
        assert not (agents_dir / "kanban.js").exists()
        sources = [*_TEMPLATES.glob("*.html"), *(_STATIC / "js").rglob("*.js"), _STATIC / "app.js"]
        for path in sources:
            text = path.read_text(encoding="utf-8")
            assert "work-items.js" not in text, path.name
            assert "kanban.js" not in text, path.name
            assert "switch-agent-view" not in text, path.name

    def test_the_supervisor_page_links_to_work_instead_of_its_queue_panel(self) -> None:
        html = (_TEMPLATES / "supervisor.html").read_text(encoding="utf-8")

        assert 'id="queue-list"' not in html
        assert "function renderQueue" not in html
        assert "/work" in html


# Columns come from the API --------------------------------------------------


def _item(number: int, state: str, **extra: object) -> dict:
    return {
        "issue_number": str(number),
        "pr_number": extra.get("pr_number"),
        "title": f"Issue {number}",
        "url": f"https://example.test/{number}",
        "state": state,
        "issue_priority": "medium",
        "last_run": extra.get("last_run"),
        "suitability": {"mode": extra.get("mode", "autonomous"), "reasons": extra.get("reasons", []), "blocked_by": []},
        "pr_details": extra.get("pr_details"),
        "running_agent": extra.get("running_agent"),
        "created_at": extra.get("created_at", ""),
        "primary_action": extra.get("primary_action"),
        "secondary_actions": extra.get("secondary_actions", []),
    }


def _task(number: int, *, depends_on: list[int] | None = None, milestone: str = "") -> Task:
    body = "## Dependencies\n" + "".join(f"- #{dep}\n" for dep in depends_on) if depends_on else ""
    return Task(
        id=str(number),
        title=f"Issue {number}",
        state=TaskState.TRIAGED,
        labels=["agent:ready"],
        priority=Priority.MEDIUM,
        body=body,
        milestone=milestone,
        url=f"https://example.test/{number}",
    )


async def _build(
    items: list[dict], tasks: list[Task], *, agents: dict | None = None, queue: list[int] | None = None
) -> dict:
    graph = DependencyGraph(tasks)
    items_data = {"items": items, **(agents or {"agents_running": 1, "max_concurrent": 3})}
    with (
        patch.object(work_board_service, "load_ranking_graph", AsyncMock(return_value=graph)),
        patch.object(work_board_service, "get_work_items", AsyncMock(return_value=items_data)),
        patch.object(work_board_service, "next_task_exclusions", AsyncMock(return_value=(set(), set()))),
        patch.object(work_board_service, "load_task_queue", AsyncMock(return_value=list(queue or []))),
        patch.object(work_board_service, "load_hidden_issues", AsyncMock(return_value=[])),
        patch.object(work_board_service, "_active_runs", AsyncMock(return_value={})),
        patch.object(work_board_service, "_done_today", AsyncMock(return_value=[])),
    ):
        return await work_board_service.build_board(PROJECT)


class TestColumns:
    async def test_columns_follow_the_stage_order_with_the_server_labels_and_colors(self) -> None:
        board = await _build([], [])

        assert [c["stage"] for c in board["columns"]] == [stage.value for stage in WorkStage]
        assert [c["stage"] for c in board["columns"]] == board["stages"]
        for column in board["columns"]:
            stage = WorkStage(column["stage"])
            assert column["label"] == stage_label(stage)
            assert column["color"] == stage_color(stage)
            assert column["entries"] in board, f"{column['entries']} names a payload key"

    async def test_the_payload_carries_the_whole_saved_queue_order(self) -> None:
        """A card's Move up and Move down read its place in the saved order, not in the Queued column."""
        board = await _build([], [_task(1), _task(2), _task(3)], queue=[3, 1, 2])

        assert board["queue_order"] == [3, 1, 2]

    async def test_your_own_work_sits_in_needs_you_and_blocked_has_no_column(self) -> None:
        board = await _build([], [])
        placement = {c["stage"]: c["column"] for c in board["columns"]}

        assert placement["yours"] == "needs"
        assert placement["blocked"] is None
        assert all(placement[s] == s for s in ("next", "queued", "running", "review", "needs", "done"))

    def test_no_page_module_maps_a_state_to_a_column(self) -> None:
        """The modules may name stages (the API's own values) but never a work-item or tracker state."""
        stages = {stage.value for stage in WorkStage}
        states = ({s.value for s in WorkItemState} | {s.value for s in TaskState}) - stages
        for name, source in _work_sources().items():
            for state in sorted(states):
                assert not re.search(rf"""['"]{re.escape(state)}['"]""", source), f"{name} names the state {state!r}"

    def test_the_board_draws_its_columns_from_the_payload(self) -> None:
        source = (_WORK_JS / "board.js").read_text(encoding="utf-8")

        assert "_board.columns" in source
        assert "col.label" in source
        assert "col.color" in source
        assert "_board[col.entries]" in source


class TestEntryFields:
    async def test_running_and_needs_cards_carry_the_run_their_buttons_act_on(self) -> None:
        items = [
            _item(1, "agent_running", running_agent={"run_id": 70, "role": "developer", "elapsed_seconds": 5}),
            _item(2, "triaged", last_run={"id": 80, "status": "failed", "role": "researcher"}),
        ]
        board = await _build(items, [_task(1), _task(2)])

        assert board["running"][0]["run_id"] == 70
        failed = board["needs"][0]
        assert (failed["kind"], failed["run_id"], failed["role"]) == ("failed_run", 80, "researcher")
        assert failed["last_run"]["run_id"] == 80

    async def test_a_card_with_an_open_pr_carries_what_its_pr_line_shows(self) -> None:
        """Size, files, threads, draft and a base that moved on: the old task list's PR details, kept on the card."""
        pr = {
            "number": 9,
            "url": "https://example.test/pull/9",
            "computed_state": "pr_review_exhausted",
            "ci_status": "passed",
            "mergeable": "MERGEABLE",
            "merge_state": "BEHIND",
            "is_draft": True,
            "thread_resolved": 6,
            "thread_total": 7,
            "additions": 900,
            "deletions": 300,
            "changed_files": 12,
            "author": "octocat",
            "assignees": ["dsova"],
        }
        items = [_item(1, "pr_review_exhausted", pr_number=9, pr_details=pr, created_at="2026-11-01T10:00:00Z")]
        board = await _build(items, [_task(1)])

        entry = board["needs"][0]
        assert entry["created_at"] == "2026-11-01T10:00:00Z"
        assert entry["pr"] == {
            "number": 9,
            "url": "https://example.test/pull/9",
            "state_label": "",
            "state_color": "",
            "ci_state": "passed",
            "mergeable": "MERGEABLE",
            "merge_state": "BEHIND",
            "is_draft": True,
            "threads_resolved": 6,
            "threads_total": 7,
            "additions": 900,
            "deletions": 300,
            "changed_files": 12,
            "author": "octocat",
            "assignees": ["dsova"],
        }

    async def test_an_entry_carries_its_work_items_runnable_actions_primary_first(self) -> None:
        """The drawer offers the work item's other actions (Address PR beside Integrate), as the server lists them."""
        integrate = {
            "id": "integrate",
            "label": "Integrate",
            "style": "success",
            "handler": "run_command",
            "handler_args": {"command": "integrate-pr", "issue": "1", "pr": 9},
            "merge_blockers": ["1 review thread unresolved"],
        }
        address = {
            "id": "address_pr",
            "label": "Address PR",
            "style": "neutral",
            "handler": "run_command",
            "handler_args": {"command": "address-pr", "issue": "1", "pr": 9},
        }
        unknown = {"id": "x", "label": "X", "style": "neutral", "handler": "open_modal", "handler_args": {}}
        items = [
            _item(1, "pr_review_exhausted", pr_number=9, primary_action=integrate, secondary_actions=[address, unknown])
        ]
        board = await _build(items, [_task(1)])

        assert board["needs"][0]["actions"] == [integrate, address]

    def test_the_drawer_lists_only_the_actions_its_card_buttons_do_not_offer(self) -> None:
        board = (_WORK_JS / "board.js").read_text(encoding="utf-8")
        drawer = (_WORK_JS / "drawer.js").read_text(encoding="utf-8")
        actions = (_WORK_JS / "actions.js").read_text(encoding="utf-8")

        assert "covers: covers" in board
        assert "covered.indexOf(actionSignature(a)) === -1" in drawer
        assert "'work-run-action'" in actions
        assert "merge_blockers" in actions  # an Integrate on a PR not fully ready asks first

    def test_cards_show_priority_as_its_icon_and_the_pr_size_as_a_letter(self) -> None:
        source = (_WORK_JS / "board.js").read_text(encoding="utf-8")

        assert "priorityIconUrl(entry.priority)" in source
        assert "diffSizeLabel(pr.additions, pr.deletions)" in source
        assert "formatRole(last.role)" in source

    async def test_every_card_says_what_it_waits_for_and_what_waits_for_it(self) -> None:
        tasks = [_task(1), _task(2, depends_on=[1]), _task(3, depends_on=[2])]
        items = [
            _item(1, "triaged"),
            _item(2, "triaged", mode="blocked"),
            _item(3, "triaged", mode="blocked"),
        ]
        board = await _build(items, tasks)
        entries = {e["number"]: e for col in board["columns"] for e in board[col["entries"]] if e["number"]}

        assert entries[1]["dependents"] == [2]
        assert entries[2]["depends_on"] == [1]
        assert entries[2]["dependents"] == [3]
        assert entries[3]["depends_on"] == [2]

    async def test_interactive_work_carries_the_session_command(self) -> None:
        reasons = [{"source": "label", "value": "agent:interactive-recommended"}]
        board = await _build([_item(7, "triaged", mode="interactive", reasons=reasons)], [_task(7)])

        yours = board["yours"][0]
        assert yours["reason"] == "agent:interactive-recommended"
        assert yours["session_command"] == 'claude "/develop-full 7"'

    async def test_the_board_names_its_milestones_and_agent_slots(self) -> None:
        board = await _build([], [_task(1, milestone="M1"), _task(2, milestone="M2"), _task(3)])

        assert board["milestones"] == ["M1", "M2"]
        assert board["agents"] == {"running": 1, "limit": 3}


# No polling: the board refreshes from the socket ------------------------------


class TestNoPolling:
    def test_no_work_module_starts_a_timer_of_its_own(self) -> None:
        for name, source in _work_sources().items():
            assert "setInterval(" not in source, name
            assert "visibilityAwarePoll(" not in source, name

    def test_the_board_subscribes_to_the_shared_socket(self) -> None:
        source = (_WORK_JS / "board.js").read_text(encoding="utf-8")

        assert "subscribeSovaSocket(" in source
        assert "'work_items_changed'" in source
        assert "new WebSocket(" not in source


# Node harness ------------------------------------------------------------------

_HARNESS = r"""
import { pathToFileURL } from 'node:url';
import fs from 'node:fs';
import path from 'node:path';

const [, , staticDir, scenario] = process.argv;
const docListeners = [];
const proxy = () => new Proxy(function () {}, {
  get(t, p) {
    if (p === 'classList') return { add() {}, remove() {}, toggle() {}, contains: () => false };
    if (p === 'style' || p === 'dataset') return {};
    if (p === 'children' || p === 'childNodes') return [];
    if (p === 'value') return '';
    if (p === 'querySelectorAll') return () => [];
    if (p === 'addEventListener') return () => {};
    if (typeof p === 'symbol') return undefined;
    return () => proxy();
  },
  set() { return true; },
});
globalThis.window = globalThis;
globalThis.document = {
  hidden: false,
  getElementById: () => proxy(),
  querySelector: () => null, querySelectorAll: () => [], createElement: () => proxy(),
  addEventListener: (type, fn) => docListeners.push([type, fn]), removeEventListener() {},
  body: { addEventListener() {}, classList: { toggle() {}, add() {}, remove() {} } },
};
globalThis.localStorage = { getItem: () => null, setItem() {}, removeItem() {} };
globalThis.sessionStorage = globalThis.localStorage;
globalThis.addEventListener = () => {};

const appSrc = fs.readFileSync(path.join(staticDir, 'app.js'), 'utf8');
const names = [...appSrc.matchAll(/^(?:async )?function (\w+)|^(?:var|let|const) (\w+)/gm)].map(m => m[1] || m[2]);
for (const n of names) if (!(n in globalThis)) globalThis[n] = () => '';
function real(name, prefix = 'function ') {
  const start = appSrc.indexOf(prefix + name + '(');
  new Function(appSrc.slice(start, appSrc.indexOf('\n}\n', start) + 2) + '; globalThis.' + name + ' = ' + name + ';')();
}
real('escapeHtml');
real('getErrorDetail', 'async function ');
globalThis.apiUrl = (p) => '/api' + p;
globalThis.jsonCsrfHeaders = () => ({ 'Content-Type': 'application/json' });
globalThis.safeStorageGet = () => '';
globalThis.settingSaveCount = () => 0;

// fetch: each scenario answers by "METHOD /path"; every request is logged.
const requests = [];
let routes = {};
const response = (status, body) => ({
  ok: status >= 200 && status < 300, status,
  json: async () => body, clone() { return response(status, body); },
});
globalThis.fetch = async (url, opts) => {
  const key = ((opts && opts.method) || 'GET') + ' ' + url.split('?')[0];
  requests.push({ key, url, body: opts && opts.body ? JSON.parse(opts.body) : null });
  const answer = routes[key];
  if (!answer) return response(404, { detail: 'no route ' + key });
  return response(answer[0], answer[1]);
};
globalThis.fetchAPI = async (url, opts) => (await globalThis.fetch(url, opts)).json();
const tick = () => new Promise((resolve) => setTimeout(resolve, 0));
const out = {};
const work = (n) => import(pathToFileURL(path.join(staticDir, 'js', 'pages', 'work', n + '.js')).href);

if (scenario === 'start-no-slot') {
  const actions = await work('actions');
  routes = {
    'POST /api/agents/start': [409, {
      detail: { code: 'no_free_slot', message: 'Maximum concurrent agents reached (3)' },
    }],
    'POST /api/supervisor/queue': [201, { queue: [5, 42] }],
  };
  out.message = await actions.startOrQueue(42, 'developer');
  out.requests = requests.map(r => r.key);
  out.queued = requests.find(r => r.key === 'POST /api/supervisor/queue').body;

  routes['POST /api/agents/start'] = [200, { run_id: 9 }];
  requests.length = 0;
  out.startedMessage = await actions.startOrQueue(43, 'developer');
  out.startedRequests = requests.map(r => r.key);

  routes['POST /api/agents/start'] = [409, {
    detail: { code: 'suitability_blocked', message: 'Epic: not for an agent' },
  }];
  requests.length = 0;
  try { await actions.startOrQueue(44, 'developer'); out.otherError = null; } catch (e) { out.otherError = e.message; }
  out.otherRequests = requests.map(r => r.key);

  // A developer start refused for the issue's state is not a full pool: queueing it would reproduce #1418
  // through the supervisor, so it is reported (naming the agent the issue needs) and never queued.
  routes['POST /api/agents/start'] = [409, {
    detail: {
      code: 'state_not_ready', state: 'triaged', next_role: 'researcher',
      message: 'Issue #45 is triaged, not ready for development. '
        + 'It has no research or spec yet: run the researcher first.',
    },
  }];
  requests.length = 0;
  try {
    await actions.startOrQueue(45, 'developer');
    out.notReadyError = null;
  } catch (e) { out.notReadyError = e.message; }
  out.notReadyRequests = requests.map(r => r.key);
} else if (scenario === 'card-start') {
  document.getElementById = () => null;  // no icon template: buttons render their labels alone
  const board = await work('board');
  const start = (role, label) => ({ id: label.toLowerCase(), label, style: 'primary', handler: 'start_agent',
    handler_args: { role, issue: '1418' } });
  const research = start('researcher', 'Research');
  const develop = start('developer', 'Develop');
  out.nextTriaged = board.cardParts({ number: 1418, rank: 1, start: research }, 'next');
  out.nextNoStart = board.cardParts({ number: 7, rank: 2, start: null }, 'next');
  out.queuedResearched = board.cardParts({ number: 1418, start: develop }, 'queued');
  const failed = (next) => ({ number: 1418, kind: 'failed_run', role: 'developer', run_id: 5, start: next });
  out.failedRefused = board.cardParts(failed(research), 'needs');
  out.failedSame = board.cardParts(failed(develop), 'needs');
  const stale = { number: 1029, kind: 'stale_label', reason: 'PR no longer open, issue still labeled in review' };
  out.longReason = board.cardParts(stale, 'needs').body;
  out.rebaseRunning = board.cardParts({ number: 1066, role: 'supervisor:rebase', run_id: 7 }, 'running').buttons;
} else if (scenario === 'pr-card') {
  const els = {
    'work-board': { innerHTML: '', scrollLeft: 0, contains: () => false, querySelectorAll: () => [] },
    'work-drawer': { innerHTML: '', hidden: true, querySelector: () => null },
    'work-board-error': { textContent: '', classList: { toggle() {} } },
  };
  document.getElementById = (id) => els[id] || null;
  const command = (name) => ({
    id: name, label: name, style: 'neutral', handler: 'run_command', handler_args: { command: name, pr: 1495 },
  });
  const prEntry = {
    number: null, pr_number: 1495, title: 'A PR with no issue',
    state: 'pr_sova_changes', state_label: 'Changes Requested',
    pr: {
      number: 1495, url: 'https://github.com/o/r/pull/1495', ci_state: 'passed', threads_total: 8, threads_resolved: 2,
    },
    actions: [command('review-pr'), command('address-pr')],
  };
  routes = { 'GET /api/work/board': [200, {
    columns: [{ stage: 'review', label: 'In review', color: 'purple', entries: 'review', column: 'review' }],
    review: [prEntry], chains: [], hidden: [],
  }] };
  const selection = await work('selection');
  const board = await work('board');
  const drawer = await work('drawer');
  drawer.init();
  await board.loadBoard();
  out.boardHtml = els['work-board'].innerHTML;
  out.key = selection.selectionKey(prEntry);
  selection.select(out.key);
  out.selected = selection.getSelected();
  out.drawerHidden = els['work-drawer'].hidden;
  out.drawerHtml = els['work-drawer'].innerHTML;
  out.selectedAgain = selection.isSelected('pr:1495');
  const parts = board.cardParts(prEntry, 'review');
  out.reviewButtons = parts.buttons;
  out.reviewCovers = parts.covers;
  const issueCard = (pr) => ({ number: 7, pr_number: 70, pr: Object.assign({ number: 70 }, pr), actions: [] });
  out.fallbackButtons = board.cardParts(issueCard({ threads_total: 3, threads_resolved: 1 }), 'review').buttons;
  const resolved = board.cardParts(issueCard({ threads_total: 3, threads_resolved: 3 }), 'review');
  out.resolvedButtons = resolved.buttons;
  out.resolvedCovers = resolved.covers;
  out.noThreadsButtons = board.cardParts(issueCard({ threads_total: 0, threads_resolved: 0 }), 'review').buttons;
  // A PR title is the tracker's text: the PR-only drawer header must escape it.
  const hostile = Object.assign({}, prEntry, { pr_number: 1496, title: '<img src=x onerror=alert(1)>',
    pr: Object.assign({}, prEntry.pr, { number: 1496, url: 'https://github.com/o/r/pull/1496' }) });
  routes['GET /api/work/board'][1].review = [hostile];
  await board.loadBoard();
  selection.select('pr:1496');
  out.hostilePrDrawer = els['work-drawer'].innerHTML;
} else if (scenario === 'owner-filter') {
  const els = {
    'work-board': { innerHTML: '', scrollLeft: 0, contains: () => false, querySelectorAll: () => [] },
    'work-board-error': { textContent: '', classList: { toggle() {} } },
  };
  document.getElementById = (id) => els[id] || null;
  const entries = [
    { number: 42, jira_key: 'ARVAC-42', title: 'Assigned Jira issue', assignees: ['Damian Sova'],
      pr: { number: 100, author: 'teammate' } },
    { number: 43, title: 'My PR', assignees: [], pr: { number: 101, author: 'dsova' } },
    { number: 44, title: 'Unrelated', assignees: ['teammate'], pr: { number: 102, author: 'teammate' } },
  ];
  routes = { 'GET /api/work/board': [200, {
    columns: [{ stage: 'review', column: 'review', label: 'In review', color: 'purple', entries: 'review' }],
    review: entries, github_user: 'dsova', jira_display_name: 'Damian Sova',
  }] };
  const board = await work('board');
  await board.loadBoard();
  out.allHtml = els['work-board'].innerHTML;
  board.setOwnerFilter('mine');
  await board.loadBoard();
  out.mineHtml = els['work-board'].innerHTML;
  out.mineRequest = requests.filter(r => r.key === 'GET /api/work/board').at(-1).url;
} else if (scenario === 'owner-filter-race') {
  const els = {
    'work-board': { innerHTML: '', scrollLeft: 0, contains: () => false, querySelectorAll: () => [] },
    'work-board-error': { textContent: '', classList: { toggle() {} } },
  };
  document.getElementById = (id) => els[id] || null;
  let releaseAll;
  let calls = 0;
  globalThis.fetch = async (url) => {
    calls++;
    if (calls === 1) return new Promise((resolve) => { releaseAll = () => resolve(response(200, {
      columns: [{ stage: 'review', column: 'review', label: 'In review', color: 'purple', entries: 'review' }],
      review: [{ number: 1, title: 'All only', mine: false }], github_user: 'dsova',
    })); });
    if (url.includes('owner=mine')) return response(200, {
      columns: [{ stage: 'review', column: 'review', label: 'In review', color: 'purple', entries: 'review' }],
      review: [{ number: 2, title: 'Mine only', mine: true }], github_user: 'dsova',
    });
    return response(500, { detail: 'offline' });
  };
  const board = await work('board');
  const first = board.loadBoard();
  await tick();
  board.setOwnerFilter('mine');
  releaseAll();
  await first;
  out.mineHtml = els['work-board'].innerHTML;
  board.setOwnerFilter('all');
  out.loadingHtml = els['work-board'].innerHTML;
  await board.loadBoard();
  out.afterFailure = els['work-board'].innerHTML;
  out.error = els['work-board-error'].textContent;
} else if (scenario === 'drawer-redraw') {
  // The drawer's priority picker is "busy" while its menu is open or its save runs: a board refresh must not
  // replace it then, and the redraw lands once the picker is idle again.
  let busy = false;
  let writes = 0;
  let html = '';
  const drawerEl = {
    hidden: true,
    querySelector: () => (busy ? {} : null),
    get innerHTML() { return html; },
    set innerHTML(v) { html = v; writes += 1; },
  };
  const els = {
    'work-board': { innerHTML: '', scrollLeft: 0, contains: () => false, querySelectorAll: () => [] },
    'work-drawer': drawerEl,
    'work-board-error': { textContent: '', classList: { toggle() {} } },
  };
  document.getElementById = (id) => els[id] || null;
  const entry = (title) => ({ number: 7, title, state: 'triaged', priority: 'high', actions: [] });
  const serve = (title) => {
    routes = { 'GET /api/work/board': [200, {
      columns: [{ stage: 'next', label: 'Next', color: 'gray', entries: 'up_next', column: 'next' }],
      up_next: [entry(title)], chains: [], hidden: [],
    }] };
  };
  const selection = await work('selection');
  const board = await work('board');
  const drawer = await work('drawer');
  drawer.init();
  serve('First title');
  await board.loadBoard();
  selection.select(7);
  out.drawnFirst = html.includes('First title');
  const before = writes;
  busy = true;
  serve('Second title');
  await board.loadBoard();
  out.heldWrites = writes - before;
  out.heldHtmlKept = html.includes('First title');
  busy = false;
  serve('Third title');
  await board.loadBoard();
  out.idleRedrawn = html.includes('Third title');
} else if (scenario === 'drawer-close') {
  // With no board loaded the drawer's render would hide it; on the page it is shown while an issue is selected.
  const drawerEl = { innerHTML: '', contains: (t) => !!(t && t.inDrawer), querySelector: () => null };
  Object.defineProperty(drawerEl, 'hidden', { get: () => false, set() {} });
  document.getElementById = (id) => (id === 'work-drawer' ? drawerEl : null);
  const selection = await work('selection');
  const drawer = await work('drawer');
  drawer.init();
  // A fake event target: `matches` lists the selectors closest() finds it inside.
  const target = (matches, opts = {}) => ({
    isConnected: opts.detached ? false : true,
    inDrawer: !!opts.inDrawer,
    closest(sel) { return sel.split(',').some((s) => matches.includes(s.trim())) ? this : null; },
  });
  const fire = (type, t) => docListeners.filter(([kind]) => kind === type).forEach(([, fn]) => fn({ target: t }));
  const click = (t, pressTarget) => { fire('pointerdown', pressTarget || t); fire('click', t); };
  const after = (t, pressTarget) => { selection.select(5); click(t, pressTarget); return selection.getSelected(); };
  out.outside = after(target([]));
  out.card = after(target(['[data-action="work-select"]']));
  out.search = after(target(['#work-search']));
  out.dialog = after(target(['.sova-modal-backdrop']));
  out.logViewer = after(target(['#log-viewer']));
  out.toast = after(target(['.sova-toast']));
  out.ariaDialog = after(target(['[role="dialog"]']));
  out.optIn = after(target(['[data-keeps-drawer]']));
  out.inside = after(target(['#work-drawer'], { inDrawer: true }));
  out.detached = after(target([], { detached: true }));
  out.pressBeganInside = after(target([]), target(['#work-drawer'], { inDrawer: true }));
  out.closeButton = after(target(['#work-drawer-close', '#work-drawer'], { inDrawer: true }));
  selection.select(null);
  click(target([]));
  out.nothingSelected = selection.getSelected();
} else if (scenario === 'drawer-offboard') {
  // The search's entry stands in for a card only while it is current: another selection, a column of the board
  // showing the issue, or a re-read from the search replaces it, so its `start` never outlives its state.
  let html = '';
  const drawerEl = { hidden: true, querySelector: () => null,
    get innerHTML() { return html; }, set innerHTML(v) { html = v; } };
  const els = {
    'work-board': { innerHTML: '', scrollLeft: 0, contains: () => false, querySelectorAll: () => [] },
    'work-drawer': drawerEl,
    'work-board-error': { textContent: '', classList: { toggle() {} } },
  };
  document.getElementById = (id) => els[id] || null;
  const action = (label) => ({ id: label, label, style: 'primary', handler: 'start_agent',
    handler_args: { role: label.toLowerCase(), issue: '2' } });
  const searched = (label) => ({
    number: 2, title: 'Searched two', state: 'triaged', start: action(label), actions: [action(label)],
  });
  const nextEntry = (n) => ({
    number: n, title: 'Next ' + n, state: 'researched', priority: 'high', start: null, actions: [],
  });
  const serve = (upNext) => {
    routes['GET /api/work/board'] = [200, {
      columns: [{ stage: 'next', label: 'Next', color: 'gray', entries: 'up_next', column: 'next' }],
      up_next: upNext, chains: [{ issues: [{ number: 2, title: 'Chain two', stage: 'blocked' }] }], hidden: [],
    }];
  };
  const selection = await work('selection');
  const board = await work('board');
  const drawer = await work('drawer');
  drawer.init();
  serve([nextEntry(1)]);
  await board.loadBoard();

  drawer.showOffBoard(searched('Research'));
  selection.select(2);
  out.shownWhileSelected = html.includes('Research');

  selection.select(1);
  selection.select(2);
  out.droppedOnOtherSelection = !html.includes('Research');
  out.chainShownInstead = html.includes('Chain two');

  drawer.showOffBoard(searched('Research'));
  selection.select(null);
  selection.select(2);
  serve([nextEntry(1), nextEntry(2)]);
  await board.loadBoard();
  await tick();
  out.droppedWhenBoardShowsIt = !html.includes('Research');
  out.boardEntryShownInstead = html.includes('Next 2');

  serve([nextEntry(1)]);
  await board.loadBoard();
  drawer.showOffBoard(searched('Research'));
  selection.select(null);
  selection.select(2);
  routes['GET /api/work/search'] = [200, { results: [searched('Develop')], more: 0 }];
  await board.loadBoard();
  await tick();
  await tick();
  out.refreshedFromSearch = html.includes('Develop') && !html.includes('Research');

  routes['GET /api/work/search'] = [200, { results: [], more: 0 }];
  await board.loadBoard();
  await tick();
  await tick();
  out.droppedWhenSearchNoLongerReturnsIt = !html.includes('Develop');
  out.chainShownAfterSearchDrops = html.includes('Chain two');
  drawer.showOffBoard(searched('Research'));
  out.savedPriorityReachesTheEntry = drawer.applySavedPriority(2, 'low').priority;
  out.savedPriorityOfAnotherIssueIgnored = drawer.applySavedPriority(9, 'low');
} else if (scenario === 'drawer-entry') {
  const drawer = await work('drawer');
  const board = { entry: { number: 1, start: 'board' }, stage: 'next' };
  const chain = { entry: { number: 2, stage: 'blocked' }, stage: 'blocked', chain: true };
  const index = new Map([[1, board], [2, chain]]);
  const searched = (n) => ({ number: n, start: 'search', priority: 'high' });
  out.boardWins = drawer.resolveEntry(index, 1, searched(1));
  out.chainReplaced = drawer.resolveEntry(index, 2, searched(2));
  out.chainKeptForOther = drawer.resolveEntry(index, 2, searched(9));
  out.offBoardAlone = drawer.resolveEntry(index, 3, searched(3));
  out.nothing = drawer.resolveEntry(index, 4, null);
} else if (scenario === 'search-input') {
  const listeners = {};
  const input = {
    value: '', hasAttribute: () => false,
    addEventListener(type, fn) { listeners[type] = fn; }, setAttribute() {}, removeAttribute() {},
  };
  const list = { hidden: true, innerHTML: '', addEventListener() {}, querySelectorAll: () => [] };
  document.getElementById = (id) => (id === 'work-search-input' ? input : id === 'work-search-results' ? list : null);
  document.querySelector = () => null;
  const selection = await work('selection');
  const search = await work('search');
  search.init();
  const answers = [];
  globalThis.fetchAPI = () => new Promise((resolve) => answers.push(resolve));
  const type = (text) => { input.value = text; listeners.input(); };
  const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const settle = () => wait(search.debounceMs() + 50);
  type('123');
  await settle();
  answers[0]({ results: [{ number: 123, title: 'Old search', state: 'triaged' }], more: 0 });
  await tick();
  out.shown = !list.hidden;
  type('456');
  out.hiddenOnEdit = list.hidden;
  out.clearedOnEdit = list.innerHTML;
  listeners.keydown({ key: 'Enter', preventDefault() {}, stopPropagation() {} });
  out.selectedAfterEnter = selection.getSelected();
  type('789');
  await settle();
  type('78');
  answers[1]({ results: [{ number: 789, title: 'Late answer', state: 'triaged' }], more: 0 });
  await tick();
  out.hiddenAfterLateAnswer = list.hidden;
  out.htmlAfterLateAnswer = list.innerHTML;
  // Two requests in flight, the older one answered last.
  await settle();
  type('7890');
  await settle();
  const older = answers.length - 1;
  type('7891');
  await settle();
  const newer = answers.length - 1;
  answers[newer]({ results: [{ number: 7891, title: 'Newer answer', state: 'triaged' }], more: 0 });
  await tick();
  answers[older]({ results: [{ number: 7890, title: 'Older answer', state: 'triaged' }], more: 0 });
  await tick();
  out.raceHidden = list.hidden;
  out.raceHtml = list.innerHTML;
  // A title is the tracker's text, so anyone who can open an issue controls it.
  type('hostile');
  await settle();
  answers[answers.length - 1]({
    results: [{ number: 5, title: '<img src=x onerror=alert(1)>', state: 'triaged' }], more: 0,
  });
  await tick();
  out.hostileResults = list.innerHTML;
} else if (scenario === 'search-index-once') {
  const listeners = {};
  const input = {
    value: '', hasAttribute: () => false,
    addEventListener(type, fn) { listeners[type] = fn; }, setAttribute() {}, removeAttribute() {},
  };
  const list = { hidden: true, innerHTML: '', addEventListener() {}, querySelectorAll: () => [] };
  document.getElementById = (id) => (id === 'work-search-input' ? input : id === 'work-search-results' ? list : null);
  document.querySelector = () => null;
  routes['GET /api/work/board'] = [200, {
    columns: [{ stage: 'next', label: 'Up next', color: 'blue', entries: 'up_next', column: 'next' }],
    up_next: [{ number: 1, title: 'One', state: 'researched', actions: [] }], chains: [], hidden: [],
  }];
  const board = await work('board');
  const search = await work('search');
  await board.loadBoard();
  let builds = 0;
  // The board's index reads `hidden` once per build.
  Object.defineProperty(board.getBoard(), 'hidden', { get() { builds += 1; return []; } });
  search.init();
  globalThis.fetchAPI = async () => ({
    results: [1, 2, 3, 4].map((n) => ({ number: n, title: 'Issue ' + n, state: 'triaged' })), more: 0,
  });
  input.value = '12';
  listeners.input();
  await new Promise((resolve) => setTimeout(resolve, search.debounceMs() + 50));
  out.builds = builds;
  out.rows = (list.innerHTML.match(/data-search-index=/g) || []).length;
  out.onBoardPill = list.innerHTML.includes('Up next');
  out.offBoardNote = list.innerHTML.includes('not on the board');
} else if (scenario === 'board-socket') {
  const subs = [];
  globalThis.subscribeSovaSocket = (o) => { subs.push(o); return { unsubscribe() {} }; };
  const timers = [];
  globalThis.visibilityAwarePoll = (fn, ms) => { timers.push(['poll', ms]); return { id: null }; };
  const realSetInterval = globalThis.setInterval;
  globalThis.setInterval = (fn, ms) => { timers.push(['interval', ms]); return realSetInterval(() => {}, 1e9); };
  routes = {
    'GET /api/work/board': [200, {
      columns: [], stages: [], agents: {}, up_next: [], queued: [], needs: [], yours: [],
    }],
    'GET /api/work/actions': [200, { actions: [] }],
  };
  const board = await work('board');
  board.init();
  out.subscribers = subs.length;
  const sub = subs[0];
  const boardFetches = () => requests.filter(r => r.key === 'GET /api/work/board').length;
  sub.onOpen(); await tick(); await tick();
  out.afterOpen = boardFetches();
  sub.onMessage({ type: 'work_items_changed' }); await tick(); await tick();
  out.afterChange = boardFetches();
  sub.onMessage({ type: 'feed', event: {} }); await tick(); await tick();
  out.afterFeed = boardFetches();
  sub.onMessage({ type: 'status_update', runs: [{ run_id: 1, status: 'running', current_step: 'develop' }] });
  sub.onMessage({ type: 'status_update', runs: [{ run_id: 1, status: 'running', current_step: 'develop' }] });
  await tick(); await tick();
  out.afterSameRuns = boardFetches();
  sub.onMessage({ type: 'status_update', runs: [{ run_id: 1, status: 'running', current_step: 'validate' }] });
  await tick(); await tick();
  out.afterStepChange = boardFetches();
  out.timers = timers;
  out.fallbackIsTheHubs = typeof sub.onFallbackPoll === 'function';
} else if (scenario === 'board-refresh') {
  // The board host and its error line record what loadBoard() leaves on screen.
  const els = {};
  const el = (id) => els[id] || (els[id] = {
    id, innerHTML: '', textContent: '', scrollLeft: 0, hiddenClass: true,
    classList: {
      toggle(c, on) { if (c === 'hidden') el(id).hiddenClass = !!on; }, add() {}, remove() {}, contains: () => false,
    },
  });
  // Every other element (icons, header tiles) is absent, which the modules handle.
  document.getElementById = (id) => (id === 'work-board' || id === 'work-board-error') ? el(id) : null;
  const good = {
    columns: [{ stage: 'next', column: 'next', label: 'Up next', color: 'blue', entries: 'up_next' }],
    up_next: [{ number: 5, title: 'Five' }], queued: [], needs: [], yours: [], agents: {},
  };
  let mode = 'fail';
  let boardFetches = 0;
  const gates = [];
  globalThis.fetch = (url, opts) => {
    if (!url.startsWith('/api/work/board')) return Promise.resolve(response(200, { actions: [] }));
    boardFetches += 1;
    if (mode === 'fail') return Promise.resolve(response(500, { detail: 'boom' }));
    if (mode === 'hang') {
      return new Promise((resolve, reject) => opts.signal.addEventListener('abort', () => {
        const e = new Error('aborted'); e.name = 'AbortError'; reject(e);
      }));
    }
    if (mode === 'gate') return new Promise((resolve) => gates.push(() => resolve(response(200, good))));
    return Promise.resolve(response(200, good));
  };
  // The board's 30 s request timeout fires at once while a request hangs.
  const realSetTimeout = globalThis.setTimeout;
  globalThis.setTimeout = (fn, ms, ...rest) => realSetTimeout(fn, mode === 'hang' && ms === 30000 ? 0 : ms, ...rest);
  const board = await work('board');
  const host = el('work-board');
  const error = el('work-board-error');

  await board.loadBoard();
  out.emptyHost = host.innerHTML;
  out.emptyError = error.textContent;

  mode = 'hang';
  await board.loadBoard();
  out.timeoutError = error.textContent;
  mode = 'ok';
  const before = boardFetches;
  await board.loadBoard();
  out.nextLoadFetched = boardFetches - before;
  out.loadedHost = host.innerHTML;
  out.errorHiddenAfterLoad = error.hiddenClass;
  out.loadError = error.textContent;

  mode = 'gate';
  const start = boardFetches;
  const first = board.loadBoard();
  const second = board.loadBoard();
  const third = board.loadBoard();
  out.sharedPromise = first === second && second === third;
  out.inFlight = boardFetches - start;
  gates.shift()();
  while (!gates.length) await tick();
  gates.shift()();
  await first;
  out.coalescedFetches = boardFetches - start;

  mode = 'fail';
  await board.loadBoard();
  out.keptHost = host.innerHTML;
  out.staleError = error.textContent;
} else if (scenario === 'action-state') {
  // The real #1146 helper from app.js, section 6c.
  const start = appSrc.indexOf('var ACTION_SUCCESS_SHOW_MS');
  const end = appSrc.indexOf('/* ============================================================\n   7. SIDEBAR');
  const exportsList = [
    'runTrackedAction', 'actionStateFor', 'actionIsRunning', 'actionStatusHtml', 'setServerActions', 'actionKey',
    'dismissActionState',
  ];
  new Function(appSrc.slice(start, end) + exportsList.map(n => 'globalThis.' + n + ' = ' + n + ';').join(''))();
  let release;
  const pending = runTrackedAction('issue:5', 'Rebasing', () => new Promise((resolve) => { release = resolve; }));
  out.runningState = actionStateFor('issue:5').status;
  out.runningDisabled = actionIsRunning('issue:5');
  out.runningHtml = actionStatusHtml('issue:5');
  release('Rebased');
  out.result = await pending;
  out.doneState = actionStateFor('issue:5').status;
  out.failed = await runTrackedAction('issue:6', 'Stopping', async () => {
    throw new Error('Run 3 was started outside the dashboard');
  });
  out.failedHtml = actionStatusHtml('issue:6');
  setServerActions([{
    id: 'a1', issue: 7, pr: null, kind: 'rebase', label: 'Rebasing', status: 'running', started_at: Date.now() / 1000,
  }]);
  out.serverRunning = actionIsRunning('issue:7');
  out.serverHtml = actionStatusHtml('issue:7');
  out.prKey = actionKey(null, 12);
  // A rebase: the click's own result ("Rebase started") is newer than the server's running action, which wins.
  setServerActions([{
    id: 'r1', issue: 5, pr: null, kind: 'rebase', label: 'Rebasing', status: 'running',
    started_at: (Date.now() - 60000) / 1000,
  }]);
  out.serverRunningBeatsLocalDone = actionIsRunning('issue:5');
  // A click whose response was lost fails locally (stamped when it ended); the server's rebase then succeeds
  // later. The later result wins, though the server's start time is the earlier of the two.
  out.runKey = actionKey(null, null, 31);
  const lost = await runTrackedAction('issue:9', 'Rebasing', async () => { throw new Error('response lost'); });
  const afterLost = Date.now();
  setServerActions([{
    id: 'r9', issue: 9, pr: null, kind: 'rebase', label: 'Rebasing', status: 'done', message: 'Rebased',
    started_at: (afterLost - 5000) / 1000, ended_at: (afterLost + 1000) / 1000,
  }]);
  out.laterServerResultWins = actionStateFor('issue:9').status;
  // Two failures on one row: one Dismiss deletes both.
  const deleted = [];
  globalThis.fetch = async (url) => { deleted.push(url); return response(200, {}); };
  setServerActions([
    { id: 'f1', issue: 8, pr: null, status: 'failed', label: 'Stopping', started_at: 1, ended_at: 2 },
    { id: 'f2', issue: 8, pr: null, status: 'failed', label: 'Stopping', started_at: 3, ended_at: 4 },
  ]);
  await dismissActionState('issue:8');
  out.dismissed = deleted.sort();
}
process.stdout.write(JSON.stringify(out));
"""


def _run_node(tmp_path: Path, scenario: str) -> dict:
    _require_node()
    script = tmp_path / "work_page.mjs"
    script.write_text(_HARNESS, encoding="utf-8")
    proc = subprocess.run(
        ["node", str(script), str(_STATIC), scenario],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


class TestStartRunsTheAgentTheStageNeeds:
    """Start, Start now and a failed card's button run the issue's `start` action, not always a developer (#1418)."""

    def test_start_is_named_for_and_runs_the_stages_agent(self, tmp_path: Path) -> None:
        out = _run_node(tmp_path, "card-start")

        triaged = out["nextTriaged"]
        assert "Research</button>" in triaged["buttons"]
        assert 'data-args="[1418,&quot;researcher&quot;]"' in triaged["buttons"]
        assert triaged["covers"] == ["start_agent:researcher"]
        assert "Develop now</button>" in out["queuedResearched"]["buttons"]

    def test_a_card_with_no_run_for_its_state_has_no_start(self, tmp_path: Path) -> None:
        out = _run_node(tmp_path, "card-start")

        assert "work-start" not in out["nextNoStart"]["buttons"]
        assert "Queue</button>" in out["nextNoStart"]["buttons"]

    def test_a_failed_run_of_the_wrong_agent_offers_the_right_one_instead_of_retry(self, tmp_path: Path) -> None:
        out = _run_node(tmp_path, "card-start")

        refused = out["failedRefused"]
        assert "Research</button>" in refused["buttons"]
        assert "Retry" not in refused["buttons"]
        assert "View log" in refused["buttons"]
        assert sorted(refused["covers"]) == ["start_agent:developer", "start_agent:researcher"]

    def test_a_failed_run_of_the_right_agent_still_offers_retry(self, tmp_path: Path) -> None:
        out = _run_node(tmp_path, "card-start")

        assert "Retry</button>" in out["failedSame"]["buttons"]
        assert "work-start" not in out["failedSame"]["buttons"]

    def test_a_long_reason_wraps_inside_the_card(self, tmp_path: Path) -> None:
        body = _run_node(tmp_path, "card-start")["longReason"]

        assert "PR no longer open, issue still labeled in review" in body
        assert "break-words" in body
        assert "whitespace-nowrap" not in body

    def test_a_rebase_run_has_its_log_without_an_unavailable_stop_button(self, tmp_path: Path) -> None:
        buttons = _run_node(tmp_path, "card-start")["rebaseRunning"]

        assert "View log" in buttons
        assert "Stop" not in buttons


class TestPrOnlyCards:
    """A PR with no issue opens the drawer like an issue does, and In review cards offer Address PR."""

    def test_a_pr_with_no_issue_selects_by_its_pr_and_opens_a_pr_drawer(self, tmp_path: Path) -> None:
        out = _run_node(tmp_path, "pr-card")

        assert out["key"] == "pr:1495"
        assert 'data-action="work-select" data-args="[&quot;pr:1495&quot;]"' in out["boardHtml"]
        assert 'role="button"' in out["boardHtml"]
        assert out["selected"] == "pr:1495"
        assert out["selectedAgain"] is True, "a second click on the card toggles it off through isSelected()"
        assert out["drawerHidden"] is False
        drawer = out["drawerHtml"]
        assert "PR #1495" in drawer
        assert "A PR with no issue" in drawer
        assert "https://github.com/o/r/pull/1495" in drawer, "the header links the PR when there is no issue URL"
        for issue_only in ("Priority", "Waits for", "Unblocks", "Milestone", "Context"):
            assert issue_only not in drawer, issue_only

    def test_review_cards_offer_address_pr_through_the_server_action_or_the_command(self, tmp_path: Path) -> None:
        out = _run_node(tmp_path, "pr-card")

        assert "Review</button>" in out["reviewButtons"]
        assert "Address PR</button>" in out["reviewButtons"]
        assert 'data-action="work-run-action"' in out["reviewButtons"], "the work item's own action when it has one"
        assert "run_command:address-pr" in out["reviewCovers"], "so the drawer does not list it twice"
        assert 'data-action="work-address" data-args="[7,70]"' in out["fallbackButtons"]

    def test_the_fallback_address_pr_needs_an_unresolved_review_thread(self, tmp_path: Path) -> None:
        """A click spends a full agent run, so a PR with every thread resolved (or none) is not offered one."""
        out = _run_node(tmp_path, "pr-card")

        assert "Address PR" not in out["resolvedButtons"]
        assert "Address PR" not in out["noThreadsButtons"]
        assert "run_command:address-pr" not in out["resolvedCovers"], "so the drawer can still list a server action"


class TestBoardOwnerFilter:
    def test_mine_shows_the_jira_assignee_and_github_pr_author_with_avatars(self, tmp_path: Path) -> None:
        out = _run_node(tmp_path, "owner-filter")

        assert "Unrelated" in out["allHtml"]
        assert "Assigned Jira issue" in out["mineHtml"]
        assert "ARVAC-42" in out["mineHtml"]
        assert "My PR" in out["mineHtml"]
        assert "avatars.githubusercontent.com/dsova" in out["mineHtml"]
        assert "Unrelated" not in out["mineHtml"]
        assert "owner=mine" in out["mineRequest"]

    def test_filter_change_during_first_load_and_failed_refresh_never_mislabels_cards(self, tmp_path: Path) -> None:
        out = _run_node(tmp_path, "owner-filter-race")

        assert "Mine only" in out["mineHtml"]
        assert "All only" not in out["mineHtml"]
        assert "Loading all work" in out["loadingHtml"]
        assert "Mine only" not in out["afterFailure"]
        assert "offline" in out["error"]


class TestDrawerHoldsARedrawWhileThePriorityPickerIsBusy:
    def test_a_board_refresh_waits_for_an_open_picker_and_lands_when_it_is_idle(self, tmp_path: Path) -> None:
        out = _run_node(tmp_path, "drawer-redraw")

        assert out["drawnFirst"] is True
        assert out["heldWrites"] == 0, "a refresh must not replace a picker whose menu is open or save is running"
        assert out["heldHtmlKept"] is True
        assert out["idleRedrawn"] is True, "an idle picker lets the refreshed board redraw the drawer"


class TestDrawerClosesOnAClickOutside:
    def test_only_a_click_outside_the_drawer_and_its_keep_open_targets_closes_it(self, tmp_path: Path) -> None:
        out = _run_node(tmp_path, "drawer-close")

        assert out["outside"] is None, "a click on the page outside the drawer closes it"
        assert out["closeButton"] is None
        assert out["card"] == 5, "a card click selects instead (the card's own handler toggles)"
        assert out["search"] == 5
        assert out["dialog"] == 5, "a confirm the drawer opened must not close it"
        assert out["logViewer"] == 5, "the log viewer View log opens is part of the drawer's work"
        assert out["toast"] == 5, "a toast's dismiss button must not close the drawer behind it"
        assert out["ariaDialog"] == 5
        assert out["optIn"] == 5, "an overlay marked data-keeps-drawer keeps it open"
        assert out["inside"] == 5
        assert out["detached"] == 5, "a target a redraw removed was inside the drawer"
        assert out["pressBeganInside"] == 5, "a text selection released outside is not a click outside"
        assert out["nothingSelected"] is None


class TestDrawerOffBoardEntryStaysCurrent:
    def test_the_search_entry_is_dropped_or_refreshed_instead_of_outliving_its_state(self, tmp_path: Path) -> None:
        out = _run_node(tmp_path, "drawer-offboard")

        assert out["shownWhileSelected"] is True
        assert out["droppedOnOtherSelection"] is True, "another selection clears it"
        assert out["chainShownInstead"] is True, "and the chain's own record takes over, not an empty drawer"
        assert out["droppedWhenBoardShowsIt"] is True, "a column of the board is authoritative"
        assert out["boardEntryShownInstead"] is True, "and its board entry is what the drawer shows"
        assert out["refreshedFromSearch"] is True, "an issue only a chain shows is re-read on a board reload"
        assert out["droppedWhenSearchNoLongerReturnsIt"] is True
        assert out["chainShownAfterSearchDrops"] is True, "what the board still knows of it is drawn"

    def test_a_priority_saved_for_the_searched_issue_updates_its_entry(self, tmp_path: Path) -> None:
        out = _run_node(tmp_path, "drawer-offboard")

        assert out["savedPriorityReachesTheEntry"] == "low"
        assert out["savedPriorityOfAnotherIssueIgnored"] is None


class TestSearchIndexesTheBoardOnce:
    def test_the_results_share_one_board_lookup(self, tmp_path: Path) -> None:
        out = _run_node(tmp_path, "search-index-once")

        assert out["rows"] == 4
        assert out["builds"] == 1, "the board is indexed once per render, then asked for each row"
        assert out["onBoardPill"] is True, "a result the board shows names its column"
        assert out["offBoardNote"] is True, "one it does not show says so"


class TestDrawerEntryChoice:
    def test_a_chain_entry_gives_way_to_the_search_entry_that_carries_the_stage_action(self, tmp_path: Path) -> None:
        out = _run_node(tmp_path, "drawer-entry")

        assert out["boardWins"]["entry"]["start"] == "board", "a board entry stays the first choice"
        assert out["chainReplaced"]["entry"]["start"] == "search"
        assert out["chainReplaced"]["stage"] == "blocked", "the chain's stage is kept"
        assert "start" not in out["chainKeptForOther"]["entry"], "a search entry for another issue is ignored"
        assert out["offBoardAlone"]["entry"]["start"] == "search"
        assert out["nothing"] is None


class TestSearchRetiresStaleAnswers:
    def test_an_edit_drops_the_earlier_results_and_refuses_a_late_reply(self, tmp_path: Path) -> None:
        out = _run_node(tmp_path, "search-input")

        assert out["shown"] is True
        assert out["hiddenOnEdit"] is True, "the old results are not selectable while the new text is pending"
        assert out["selectedAfterEnter"] is None, "Enter cannot open an issue from the earlier search"
        assert out["hiddenAfterLateAnswer"] is True, "a reply to text that has since changed is refused"
        assert "Late answer" not in out["htmlAfterLateAnswer"]

    def test_the_newest_request_wins_when_an_older_reply_lands_after_it(self, tmp_path: Path) -> None:
        out = _run_node(tmp_path, "search-input")

        assert out["raceHidden"] is False
        assert "Newer answer" in out["raceHtml"]
        assert "Older answer" not in out["raceHtml"], "two requests in flight: the older reply must not overwrite"

    def test_a_title_from_the_tracker_is_escaped_in_the_results_and_the_pr_drawer(self, tmp_path: Path) -> None:
        search = _run_node(tmp_path, "search-input")
        pr = _run_node(tmp_path, "pr-card")

        assert "&lt;img" in search["hostileResults"]
        assert "<img" not in search["hostileResults"]
        assert "&lt;img" in pr["hostilePrDrawer"]
        assert "<img" not in pr["hostilePrDrawer"]


class TestBoardRefreshUnderNode:
    def test_the_board_reloads_on_socket_events_and_starts_no_timer(self, tmp_path: Path) -> None:
        out = _run_node(tmp_path, "board-socket")

        assert out["subscribers"] == 1
        assert out["afterOpen"] == 1
        assert out["afterChange"] == 2, "work_items_changed reloads the board"
        assert out["afterFeed"] == 2, "an unrelated message does not"
        assert out["afterSameRuns"] == 2, "an unchanged set of runs does not"
        assert out["afterStepChange"] == 3, "a run moving to a new step does"
        assert out["timers"] == []
        assert out["fallbackIsTheHubs"] is True

    def test_the_board_survives_failed_hung_and_overlapping_refreshes(self, tmp_path: Path) -> None:
        out = _run_node(tmp_path, "board-refresh")

        assert "boom" in out["emptyHost"], "with nothing loaded yet, the board shows the failure"
        assert out["emptyError"] == "boom"
        assert out["timeoutError"] == "Loading the board timed out"
        assert out["nextLoadFetched"] == 1, "a hung request ends as a failure and does not block the next load"
        assert "#5" in out["loadedHost"]
        assert out["loadError"] == ""
        assert out["errorHiddenAfterLoad"] is True, "a good load clears the error line"
        assert out["sharedPromise"] is True, "a load asked for while one runs joins it"
        assert out["inFlight"] == 1
        assert out["coalescedFetches"] == 2, "three overlapping asks cost one extra fetch, not two"
        assert "#5" in out["keptHost"], "a failed refresh keeps the last good board on screen"
        assert out["staleError"] == "boom. Showing the last board loaded."


class TestRunsWithNoCard:
    def test_a_running_run_no_card_shows_is_listed_with_what_stop_needs(self) -> None:
        planner = {
            "id": 7,
            "issue_number": "",
            "pr_number": None,
            "role": "planner",
            "status": "running",
            "current_step": "scan_project",
            "elapsed_seconds": 30,
            "started_at": "2026-11-17T10:00:00Z",
        }
        on_card = {"id": 8, "issue_number": "42", "role": "developer", "status": "running", "started_at": "x"}
        paused = {"id": 9, "issue_number": "43", "role": "developer", "status": "paused", "started_at": "y"}
        runs = {"run:7": planner, "issue:": planner, "run:8": on_card, "issue:42": on_card, "run:9": paused}

        others = work_board_service._other_runs(runs, [{"number": 42, "run_id": 8}])

        assert [(r["run_id"], r["issue_number"], r["role"], r["step_name"]) for r in others] == [
            (7, None, "planner", "scan_project")
        ]

    def test_the_running_column_draws_them_with_view_log_and_stop(self) -> None:
        source = (_WORK_JS / "board.js").read_text(encoding="utf-8")
        card = source[source.index("function _otherRunCard(run)") : source.index("function _columnFooter(col)")]

        assert "'work-stop', [run.run_id" in card
        assert "'work-view-log', [run.run_id, 'running']" in card
        assert "_board.other_runs" in source


class TestStartWithNoFreeSlot:
    def test_start_queues_the_issue_and_says_so(self, tmp_path: Path) -> None:
        out = _run_node(tmp_path, "start-no-slot")

        assert out["requests"] == ["POST /api/agents/start", "POST /api/supervisor/queue"]
        assert out["queued"] == {"issue_number": 42}
        assert "No free slot" in out["message"]
        assert "#42 is queued at position 2" in out["message"]

    def test_a_start_with_a_free_slot_does_not_queue(self, tmp_path: Path) -> None:
        out = _run_node(tmp_path, "start-no-slot")

        assert out["startedRequests"] == ["POST /api/agents/start"]
        assert out["startedMessage"] == "Started the developer agent on #43"

    def test_any_other_refusal_is_reported_and_does_not_queue(self, tmp_path: Path) -> None:
        out = _run_node(tmp_path, "start-no-slot")

        assert out["otherError"] == "Epic: not for an agent"
        assert out["otherRequests"] == ["POST /api/agents/start"]

    def test_a_state_not_ready_refusal_names_the_agent_and_never_queues(self, tmp_path: Path) -> None:
        out = _run_node(tmp_path, "start-no-slot")

        assert "run the researcher first" in out["notReadyError"]
        assert out["notReadyRequests"] == ["POST /api/agents/start"], "queueing a developer reproduces #1418"

    async def test_the_server_marks_a_full_pool_refusal_with_its_code(self, client: AsyncClient) -> None:
        refused = {"error": "Maximum concurrent agents reached (3)", "code": "no_free_slot", "running": 3}
        with patch("sova.dashboard.routers.agents.control_service.start_agent", AsyncMock(return_value=refused)):
            resp = await client.post("/api/agents/start", json={"issue": "42", "role": "developer"})

        assert resp.status_code == 409
        assert resp.json()["detail"] == {
            "code": "no_free_slot",
            "message": "Maximum concurrent agents reached (3)",
            "running": 3,
        }
        # The page queues the issue instead, so no failure is left on its card.
        assert action_registry.list_actions(None) == []


class TestActionStateHelper:
    def test_an_action_shows_running_then_its_result_and_an_error_stays(self, tmp_path: Path) -> None:
        out = _run_node(tmp_path, "action-state")

        assert out["runningState"] == "running"
        assert out["runningDisabled"] is True
        assert "Rebasing..." in out["runningHtml"]
        assert out["result"] == {"ok": True, "message": "Rebased"}
        assert out["doneState"] == "done"
        assert out["failed"]["ok"] is False
        assert "started outside the dashboard" in out["failedHtml"]
        assert "data-sova-dismiss-action" in out["failedHtml"]

    def test_a_running_server_action_survives_a_reload(self, tmp_path: Path) -> None:
        out = _run_node(tmp_path, "action-state")

        assert out["serverRunning"] is True
        assert "Rebasing..." in out["serverHtml"]
        assert out["prKey"] == "pr:12"

    def test_a_running_server_action_outranks_the_clicks_own_result(self, tmp_path: Path) -> None:
        out = _run_node(tmp_path, "action-state")

        assert out["serverRunningBeatsLocalDone"] is True, "the card stays running while the rebase runs"

    def test_the_later_result_wins_on_the_same_time_basis_and_a_planner_run_has_a_key(self, tmp_path: Path) -> None:
        out = _run_node(tmp_path, "action-state")

        assert out["laterServerResultWins"] == "done", "a lost response's local failure must not outlast the rebase"
        assert out["runKey"] == "run:31"

    def test_one_dismiss_clears_every_failure_on_the_row(self, tmp_path: Path) -> None:
        out = _run_node(tmp_path, "action-state")

        assert out["dismissed"] == ["/api/work/actions/f1", "/api/work/actions/f2"]

    def test_a_pr_only_cards_actions_are_keyed_by_its_pr(self) -> None:
        source = (_WORK_JS / "actions.js").read_text(encoding="utf-8")

        assert "runTrackedAction(actionKey(issue, pr, run), label, fn)" in source
        assert "}, prNumber);" in source
        assert "}, args.pr);" in source

    def test_a_run_with_neither_issue_nor_pr_is_keyed_by_the_run(self) -> None:
        actions = (_WORK_JS / "actions.js").read_text(encoding="utf-8")
        board = (_WORK_JS / "board.js").read_text(encoding="utf-8")

        assert "}, pr, runId);" in actions, "a planner's Stop keeps its pending state and its refusal"
        assert "actionKey(run.issue_number, run.pr_number, run.run_id)" in board

    def test_an_action_that_ends_reloads_the_board(self) -> None:
        source = (_WORK_JS / "board.js").read_text(encoding="utf-8")
        start = source.index("async function _loadActions()")
        body = source[start : source.index("\n}\n", start)]

        assert "if (finished.length) loadBoard();" in body
        assert "_reportedFinishedIds" in body, "each finished action reloads the board once, seen running or not"

    def test_a_superseded_action_list_response_is_dropped(self) -> None:
        source = (_WORK_JS / "board.js").read_text(encoding="utf-8")
        start = source.index("async function _loadActions()")
        body = source[start : source.index("\n}\n", start)]

        assert body.index("if (request !== _actionsRequest) return;") < body.index("setServerActions(actions)")


# The server's list of actions (#1146) -----------------------------------------


class TestActionRegistry:
    async def test_a_tracked_action_runs_then_records_its_result(self) -> None:
        async with action_registry.tracked_action(PROJECT, kind="start", label="Starting", issue=5) as action:
            assert action_registry.list_actions(PROJECT)[0]["status"] == "running"
            action.message = "Started"

        [entry] = action_registry.list_actions(PROJECT)
        assert (entry["status"], entry["message"], entry["issue"]) == ("done", "Started", 5)

    async def test_a_failure_keeps_the_http_detail_until_dismissed(self) -> None:
        from fastapi import HTTPException

        with pytest.raises(HTTPException):
            async with action_registry.tracked_action(PROJECT, kind="stop", label="Stopping", issue=6):
                raise HTTPException(status_code=409, detail="Run 3 was started outside the dashboard")

        [entry] = action_registry.list_actions(PROJECT)
        assert entry["status"] == "failed"
        assert entry["message"] == "Run 3 was started outside the dashboard"
        assert await action_registry.dismiss(PROJECT, entry["id"]) is True
        assert action_registry.list_actions(PROJECT) == []

    async def test_a_success_leaves_the_list_after_its_time(self) -> None:
        action_id = await action_registry.begin(PROJECT, kind="start", label="Starting", issue=5)
        await action_registry.finish(PROJECT, action_id, ok=True, message="Started")
        later = action_registry.time.time() + action_registry.SUCCESS_TTL_SECONDS + 1

        with patch.object(action_registry.time, "time", return_value=later):
            assert action_registry.list_actions(PROJECT) == []

    async def test_an_action_with_no_project_belongs_to_the_default_project_and_reaches_its_sockets(self) -> None:
        # Single-project mode: an unprefixed request names no project, and the status socket is keyed under the
        # default project dir, so the registry must key and broadcast there too.
        broadcast = AsyncMock()
        with (
            patch("sova.dashboard.services.agent_pool.get_default_project_dir", return_value=PROJECT),
            patch("sova.dashboard.routers.agents._ws_manager.broadcast_event", broadcast),
        ):
            await action_registry.begin(None, kind="rebase", label="Rebasing", issue=5)

            assert [a["issue"] for a in action_registry.list_actions(PROJECT)] == [5]
        broadcast.assert_awaited_once_with("actions_changed", PROJECT)

    async def test_a_forgotten_refusal_leaves_nothing_on_the_card(self) -> None:
        from fastapi import HTTPException

        with pytest.raises(HTTPException):
            async with action_registry.tracked_action(PROJECT, kind="start", label="Starting", issue=7) as action:
                action.forget = True
                raise HTTPException(status_code=409, detail="Maximum concurrent agents reached (3)")

        assert action_registry.list_actions(PROJECT) == []

    async def test_a_block_left_without_an_exception_of_its_own_never_stays_running(self) -> None:
        manager = action_registry.tracked_action(PROJECT, kind="start", label="Starting", issue=9)
        await manager.__aenter__()
        await manager.__aexit__(GeneratorExit, GeneratorExit(), None)

        [entry] = action_registry.list_actions(PROJECT)
        assert entry["status"] == "failed"

    async def test_a_registration_cancelled_while_announcing_is_recorded_as_failed(self) -> None:
        with patch.object(action_registry, "_notify", AsyncMock(side_effect=asyncio.CancelledError)):
            with pytest.raises(asyncio.CancelledError):
                await action_registry.begin(PROJECT, kind="rebase", label="Rebasing", issue=5)

        [entry] = action_registry.list_actions(PROJECT)
        assert entry["status"] == "failed"
        assert action_registry.running_action(PROJECT, "rebase", 5) is None, "a retry is not blocked"

    async def test_a_running_action_cannot_be_dismissed(self) -> None:
        action_id = await action_registry.begin(PROJECT, kind="rebase", label="Rebasing", issue=5)

        assert await action_registry.dismiss(PROJECT, action_id) is False
        assert action_registry.running_action(PROJECT, "rebase", 5)["id"] == action_id


class TestRebaseInTheBackground:
    async def test_the_request_returns_at_once_and_the_action_runs_until_the_rebase_ends(
        self, client: AsyncClient
    ) -> None:
        gate = asyncio.Event()

        async def slow_rebase(issue: int, _project: object, _factory: object, *, retry_failed: bool = False) -> dict:
            assert retry_failed is True
            await gate.wait()
            return {"status": "success", "pr_number": 40}

        invalidate = MagicMock()
        with (
            patch("sova.supervisor.rebase.attempt_auto_rebase", slow_rebase),
            patch("sova.db.session.get_session_factory", AsyncMock(return_value=MagicMock())),
            patch("sova.dashboard.services.agent_finalize.invalidate_work_item_caches", invalidate),
        ):
            resp = await client.post("/api/work/issue/5/rebase")
            assert resp.status_code == 202
            assert resp.json()["status"] == "started"

            running = (await client.get("/api/work/actions")).json()["actions"]
            assert [(a["issue"], a["status"], a["label"]) for a in running] == [(5, "running", "Rebasing")]
            again = await client.post("/api/work/issue/5/rebase")
            assert again.json()["status"] == "running", "a second click gets the running action back"

            gate.set()
            for _ in range(50):
                await asyncio.sleep(0.01)
                actions = (await client.get("/api/work/actions")).json()["actions"]
                if actions and actions[0]["status"] != "running":
                    break

        assert [(a["status"], a["message"]) for a in actions] == [("done", "Rebased")]
        invalidate.assert_called_once()

    async def test_the_rebase_runs_in_the_resolved_default_project_not_the_working_directory(
        self, client: AsyncClient
    ) -> None:
        # Single-project mode: the request names no project, yet the database, the git commands and the cache
        # invalidation must all use the default project, never None (the global database, the server's cwd).
        factory = AsyncMock(return_value=MagicMock())
        rebase = AsyncMock(return_value={"status": "success", "pr_number": 40})
        invalidate = MagicMock()
        with (
            patch("sova.dashboard.services.agent_pool.get_default_project_dir", return_value=PROJECT),
            patch("sova.supervisor.rebase.attempt_auto_rebase", rebase),
            patch("sova.db.session.get_session_factory", factory),
            patch("sova.dashboard.services.agent_finalize.invalidate_work_item_caches", invalidate),
        ):
            await client.post("/api/work/issue/5/rebase")
            for _ in range(50):
                await asyncio.sleep(0.01)
                if rebase.await_count:
                    break

        factory.assert_awaited_once_with(PROJECT)
        assert rebase.await_args.args[1] == PROJECT
        invalidate.assert_called_once_with(PROJECT, 40)

    async def test_no_project_is_an_error_and_schedules_nothing(self, client: AsyncClient) -> None:
        rebase = AsyncMock()
        with (
            patch("sova.dashboard.services.agent_pool.get_default_project_dir", return_value=None),
            patch("sova.supervisor.rebase.attempt_auto_rebase", rebase),
        ):
            resp = await client.post("/api/work/issue/5/rebase")

        assert resp.status_code == 503
        rebase.assert_not_awaited()
        assert (await client.get("/api/work/actions")).json()["actions"] == []

    async def test_a_failed_rebase_is_recorded_with_its_error(self, client: AsyncClient) -> None:
        failed = AsyncMock(return_value={"status": "failed", "error": "conflict in app.py"})
        with (
            patch("sova.supervisor.rebase.attempt_auto_rebase", failed),
            patch("sova.db.session.get_session_factory", AsyncMock(return_value=MagicMock())),
            patch("sova.dashboard.services.agent_finalize.invalidate_work_item_caches", MagicMock()),
        ):
            await client.post("/api/work/issue/5/rebase")
            for _ in range(50):
                await asyncio.sleep(0.01)
                actions = (await client.get("/api/work/actions")).json()["actions"]
                if actions and actions[0]["status"] != "running":
                    break

        assert [(a["status"], a["message"]) for a in actions] == [("failed", "Rebase failed: conflict in app.py")]


class TestStopARunTheDashboardDidNotStart:
    async def test_stop_says_the_run_was_started_elsewhere(self, client: AsyncClient) -> None:
        run = MagicMock(status="running", issue_number="5", pr_number=None)
        session = MagicMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)
        session.get = AsyncMock(return_value=run)
        not_held = {"status": "not_found", "message": "No agent with run_id 3"}
        with (
            patch("sova.dashboard.routers.agents.control_service.stop_agent", AsyncMock(return_value=not_held)),
            patch("sova.dashboard.routers.agents.get_session", AsyncMock(return_value=session)),
        ):
            resp = await client.post("/api/agents/3/stop")

        assert resp.status_code == 409
        assert "started outside the dashboard" in resp.json()["detail"]
        [entry] = [a for project in action_registry._actions.values() for a in project.values()]
        assert entry.status == "failed"

    async def test_stop_of_an_ended_run_says_it_ended(self, client: AsyncClient) -> None:
        run = MagicMock(status="done", issue_number="5", pr_number=None)
        session = MagicMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)
        session.get = AsyncMock(return_value=run)
        not_held = {"status": "not_found", "message": "No agent with run_id 3"}
        with (
            patch("sova.dashboard.routers.agents.control_service.stop_agent", AsyncMock(return_value=not_held)),
            patch("sova.dashboard.routers.agents.get_session", AsyncMock(return_value=session)),
        ):
            resp = await client.post("/api/agents/3/stop")

        assert resp.status_code == 409
        assert resp.json()["detail"] == "Run 3 has already ended (done)"


# The priority picker holds nothing page-specific ------------------------------


class TestPriorityPicker:
    def test_the_picker_module_holds_no_page_specific_code(self) -> None:
        source = (_WORK_JS / "priority-picker.js").read_text(encoding="utf-8")

        assert "import " not in source, "it depends on no page module"
        assert "work-" not in source, "it names no Work page element or action"
        assert "/issues/' + encodeURIComponent(issue) + '/priority'" in source
        assert "getErrorDetail(" in source, "a failed save shows the API's own message"

    def test_each_level_shows_its_priority_icon_not_a_native_select(self) -> None:
        source = (_WORK_JS / "priority-picker.js").read_text(encoding="utf-8")
        drawer = (_WORK_JS / "drawer.js").read_text(encoding="utf-8")

        assert "createElement('select')" not in source, "a native select cannot show the icons"
        assert "priorityIconUrl(level)" in source
        assert "role', 'listbox'" in source
        assert "role', 'option'" in source
        assert "'Escape'" in source, "Escape closes the menu"
        assert "[data-open]" in drawer, "the drawer holds a redraw while the menu is open"
        assert "[data-saving]" in drawer, "the drawer holds a redraw while a save is in flight"
