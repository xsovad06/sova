"""Source-level guards for the Agents page ES modules and the shared state display (#1210).

There is no browser or JS test harness here, so these assert on the shipped source: that every
dashboard script parses, that the Agents page wires its modules and delegated actions without
inline handlers, that its timers go through visibilityAwarePoll at their original cadence, and
that state labels and colors come from `work_state.py` instead of per-page tables. A missing
`node` skips locally but fails under CI, so a runner image without it cannot turn a JS syntax
error into a green build.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sova.adapters.base import Task, TaskState
from sova.dashboard.services import pr_service, work_state
from sova.dashboard.services.work_state import WorkItemState, state_color, state_label
from tests.conftest import AGENTS_PAGE_JS_DIR, DASHBOARD_DIR

_STATIC = DASHBOARD_DIR / "static"
_TEMPLATES = DASHBOARD_DIR / "templates"
_APP_JS = (_STATIC / "app.js").read_text(encoding="utf-8")

_APP_JS_HELPER_CALL = re.compile(r"\b(?:escapeHtml|timeAgo|esc|statusBadge|setText|renderSectionError)\(")
_PAGE_MODULES = [
    "work-items",
    "launch-modal",
    "pr-tracker",
    "agent-cards",
    "history",
    "log-viewer",
    "kanban",
    "live-status",
]


def _module_source(name: str) -> str:
    return (AGENTS_PAGE_JS_DIR / f"{name}.js").read_text(encoding="utf-8")


def _all_page_sources() -> dict[str, str]:
    return {p.stem: p.read_text(encoding="utf-8") for p in sorted(AGENTS_PAGE_JS_DIR.glob("*.js"))}


def _scripts() -> list[Path]:
    return sorted(p for p in _STATIC.glob("*.js") if not p.name.endswith(".min.js"))


def _modules() -> list[Path]:
    return sorted((_STATIC / "js").rglob("*.js"))


def _require_node(reason: str) -> None:
    if shutil.which("node") is not None:
        return
    if os.environ.get("CI"):
        pytest.fail(f"node is required in CI: {reason}")
    pytest.skip(reason)


def _node_check(path: Path, *, module: bool) -> subprocess.CompletedProcess[str]:
    _require_node("node is required to syntax-check the dashboard scripts")
    if module:
        return subprocess.run(
            ["node", "--input-type=module", "--check"],
            input=path.read_text(encoding="utf-8"),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    return subprocess.run(["node", "--check", str(path)], capture_output=True, text=True, timeout=30, check=False)


class TestDashboardScriptsParse:
    """The same check `make lint-js` runs, so a broken module fails the pytest run CI executes too."""

    @pytest.mark.parametrize("path", _scripts(), ids=lambda p: p.name)
    def test_static_script_parses(self, path: Path) -> None:
        proc = _node_check(path, module=False)
        assert proc.returncode == 0, proc.stderr

    @pytest.mark.parametrize("path", _modules(), ids=lambda p: p.name)
    def test_es_module_parses(self, path: Path) -> None:
        proc = _node_check(path, module=True)
        assert proc.returncode == 0, proc.stderr

    def test_makefile_lint_runs_the_js_check(self) -> None:
        makefile = (DASHBOARD_DIR.parents[1] / "Makefile").read_text(encoding="utf-8")
        assert re.search(r"^lint:.*\blint-js\b", makefile, re.MULTILINE)
        assert "node --input-type=module --check" in makefile

    def test_ci_static_checks_job_runs_the_js_check(self) -> None:
        """Without this step a JS syntax error would pass CI whenever the pytest legs skip the node tests."""
        ci = (DASHBOARD_DIR.parents[1] / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        job = ci[ci.index("  lint-static:") : ci.index("\n  python-lint:")]
        assert "run: make lint-js" in job

    @pytest.mark.parametrize("broken", ["app.js", "js/pages/agents/broken.js"])
    def test_make_lint_js_fails_on_a_syntax_error(self, tmp_path: Path, broken: str) -> None:
        """The target itself, run against a throwaway tree, exits non-zero for a bad script or module."""
        _require_node("node is required to run make lint-js")
        if shutil.which("make") is None:
            pytest.skip("make is required to run make lint-js")
        static = tmp_path / "sova" / "dashboard" / "static"
        (static / "js" / "pages" / "agents").mkdir(parents=True)
        (static / "ok.js").write_text("var ok = 1;\n", encoding="utf-8")
        (static / "js" / "pages" / "agents" / "ok.js").write_text("export var ok = 1;\n", encoding="utf-8")
        shutil.copy(DASHBOARD_DIR.parents[1] / "Makefile", tmp_path / "Makefile")

        clean = subprocess.run(
            ["make", "lint-js"], cwd=tmp_path, capture_output=True, text=True, timeout=60, check=False
        )
        assert clean.returncode == 0, clean.stdout + clean.stderr

        (static / broken).write_text("function (\n", encoding="utf-8")
        failed = subprocess.run(
            ["make", "lint-js"], cwd=tmp_path, capture_output=True, text=True, timeout=60, check=False
        )
        assert failed.returncode != 0
        assert "syntax error" in failed.stdout + failed.stderr


# Imports every page module, runs each init() against a stub DOM, then drives the delegated
# click dispatcher. Catches what the syntax check cannot: a circular import read before it is
# initialised, an init() that dereferences something missing, a handler name that never registered.
_SMOKE_HARNESS = r"""
import { pathToFileURL } from 'node:url';
import fs from 'node:fs';
import path from 'node:path';

const [, , staticDir, ...pages] = process.argv;
const failures = [];
const clickListeners = [];
const docListeners = [];

function el() {
  const target = function () {};
  return new Proxy(target, {
    get(t, p) {
      if (p === 'classList') return { add() {}, remove() {}, toggle() {}, contains: () => false };
      if (p === 'style' || p === 'dataset') return {};
      if (p === 'children' || p === 'childNodes') return [];
      if (p === 'value') return '';
      if (p === 'matches') return () => false;
      if (p === 'closest') return () => null;
      if (p === 'querySelectorAll') return () => [];
      if (p === 'addEventListener') return () => {};
      if (typeof p === 'symbol') return undefined;
      return () => el();
    },
    set() { return true; },
  });
}

const store = () => ({ getItem: () => null, setItem() {}, removeItem() {} });
globalThis.window = globalThis;
globalThis.addEventListener = () => {};
globalThis.removeEventListener = () => {};
globalThis.localStorage = store();
globalThis.sessionStorage = store();
globalThis.fetch = () => Promise.reject(new Error('offline'));
globalThis.document = {
  hidden: false,
  getElementById: () => el(),
  querySelector: () => null,
  querySelectorAll: () => [],
  createElement: () => el(),
  addEventListener: (type, fn) => docListeners.push([type, fn]),
  removeEventListener() {},
  body: { addEventListener: (type, fn) => clickListeners.push([type, fn]) },
};

// app.js globals: parse its top-level names with the real escapeHtml, everything else a no-op.
const appSrc = fs.readFileSync(path.join(staticDir, 'app.js'), 'utf8');
const names = [...appSrc.matchAll(/^(?:async )?function (\w+)|^(?:var|let|const) (\w+)/gm)].map(m => m[1] || m[2]);
for (const n of names) if (!(n in globalThis)) globalThis[n] = () => '';
const start = appSrc.indexOf('function escapeHtml(');
new Function(appSrc.slice(start, appSrc.indexOf('\n}\n', start) + 2) + '; globalThis.escapeHtml = escapeHtml;')();
globalThis.visibilityAwarePoll = (fn, ms) => { globalThis.__polls.push([fn.name, ms]); return { id: null }; };
globalThis.__polls = [];
globalThis.createSovaStatusChannel = () => ({ stop() {} });
globalThis.apiUrl = (p) => '/api' + p;
globalThis.LOG_LEVEL_COLORS = {};

const dir = path.join(staticDir, 'js', 'pages', 'agents');
const order = ['actions', ...pages];
const mods = {};
for (const name of order) {
  try { mods[name] = await import(pathToFileURL(path.join(dir, name + '.js')).href); }
  catch (e) { failures.push(`import ${name}: ${e.stack}`); }
}
for (const name of order) {
  try { mods[name] && mods[name].init(); }
  catch (e) { failures.push(`init ${name}: ${e.stack}`); }
}

// Drive the delegated dispatcher the way a click on rendered markup would.
function click(action, args) {
  const attrs = { 'data-action': action, 'data-args': args === undefined ? null : JSON.stringify(args) };
  const node = { getAttribute: (n) => attrs[n] ?? null };
  const event = { target: { closest: () => node }, stopPropagation() {} };
  for (const [type, fn] of clickListeners) if (type === 'click') fn(event);
}
const clicks = [['toggle-pr-tracker'], ['set-pr-filter', ['mine']], ['switch-tab', ['failed']], ['expand-tasks'],
  ['collapse-tasks'], ['history-next'], ['stop'], ['hide-launch-modal'], ['toggle-pr-group', ['open']]];
for (const [action, args] of clicks) {
  try { click(action, args); } catch (e) { failures.push(`click ${action}: ${e.stack}`); }
}
// A data-action named like an Object.prototype member must read as unregistered, never run that builtin.
const warnings = [];
console.warn = (...a) => warnings.push(a.map(String).join(' '));
for (const name of ['constructor', 'toString', 'hasOwnProperty', '__proto__']) {
  try { click(name); } catch (e) { failures.push(`click ${name}: ${e.stack}`); }
}
if (warnings.length !== 4) failures.push(`expected 4 unregistered-action warnings, got ${warnings.length}`);
// Malformed data-args must be reported and dropped, never thrown out of the body listener.
const errors = [];
console.error = (...a) => errors.push(a.map(String).join(' '));
try {
  const node = { getAttribute: (n) => ({ 'data-action': 'stop', 'data-args': '{not json' })[n] ?? null };
  const event = { target: { closest: () => node }, stopPropagation() { failures.push('handler ran on bad args'); } };
  for (const [type, fn] of clickListeners) if (type === 'click') fn(event);
} catch (e) { failures.push(`malformed data-args threw: ${e.stack}`); }
if (errors.length !== 1) failures.push(`expected one console.error for bad data-args, got ${errors.length}`);
process.stdout.write(JSON.stringify({ failures, polls: globalThis.__polls, bodyClick: clickListeners.length }));
"""


class TestAgentsModulesRunUnderNode:
    @pytest.mark.parametrize("pages", [_PAGE_MODULES, _PAGE_MODULES[::-1]], ids=["declared", "reversed"])
    def test_modules_import_and_init_against_a_stub_dom(self, tmp_path: Path, pages: list[str]) -> None:
        """Import order must not matter: the modules import each other in cycles, so one that read an
        imported binding at its top level would work in one order and crash or read undefined in another."""
        _require_node("node is required to run the dashboard page modules")
        script = tmp_path / "smoke.mjs"
        script.write_text(_SMOKE_HARNESS, encoding="utf-8")
        proc = subprocess.run(
            ["node", str(script), str(_STATIC), *pages],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        result = json.loads(proc.stdout)
        assert result["failures"] == []
        assert result["bodyClick"] == 1, "actions.init() must attach exactly one delegated click listener"
        assert len(result["polls"]) == 9


# Drives the log viewer through hide, show and reconnect with a fake EventSource, to prove that a line
# the agent writes while the backfill is in flight is shown exactly once and in order.
_LOG_HARNESS_PRELUDE = r"""
import { pathToFileURL } from 'node:url';
import fs from 'node:fs';
import path from 'node:path';

const [, , staticDir] = process.argv;
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

class FakeEventSource {
  static all = [];
  constructor(url) { this.url = url; this.closed = false; this.handlers = {}; FakeEventSource.all.push(this); }
  addEventListener(type, fn) { (this.handlers[type] ||= []).push(fn); }
  close() { this.closed = true; }
  emit(type, data) { for (const fn of this.handlers[type] || []) fn({ data }); }
}

const content = {
  html: '', scrollTop: 0, scrollHeight: 0,
  get innerHTML() { return this.html; }, set innerHTML(v) { this.html = v; },
  set textContent(v) { this.html = v; },
  insertAdjacentHTML(_, h) { this.html += h; },
};
const viewer = { classList: { add() {}, remove() {}, toggle() {}, contains: () => false } };
const doc = {
  hidden: false,
  // No tab strip: with none rendered updateLogTabs() returns early instead of closing a viewer that
  // has no matching running agent in this stub.
  getElementById: (id) => {
    if (id === 'log-content') return content;
    if (id === 'log-viewer') return viewer;
    return id === 'log-tabs' ? null : proxy();
  },
  querySelector: () => null, querySelectorAll: () => [], createElement: () => proxy(),
  addEventListener: (type, fn) => docListeners.push([type, fn]), removeEventListener() {},
  body: { addEventListener() {} },
};
globalThis.window = globalThis;
globalThis.document = doc;
globalThis.EventSource = FakeEventSource;
globalThis.localStorage = { getItem: () => null, setItem() {}, removeItem() {} };
globalThis.sessionStorage = globalThis.localStorage;
globalThis.addEventListener = () => {};
globalThis.fetch = () => Promise.reject(new Error('offline'));

const appSrc = fs.readFileSync(path.join(staticDir, 'app.js'), 'utf8');
const names = [...appSrc.matchAll(/^(?:async )?function (\w+)|^(?:var|let|const) (\w+)/gm)].map(m => m[1] || m[2]);
for (const n of names) if (!(n in globalThis)) globalThis[n] = () => '';
const start = appSrc.indexOf('function escapeHtml(');
new Function(appSrc.slice(start, appSrc.indexOf('\n}\n', start) + 2) + '; globalThis.escapeHtml = escapeHtml;')();
const renderStart = appSrc.indexOf('function renderIfChanged(');
new Function(appSrc.slice(renderStart, appSrc.indexOf('\n}\n', renderStart) + 2)
  + '; globalThis.renderIfChanged = renderIfChanged;')();
globalThis.visibilityAwarePoll = () => ({ id: null });
globalThis.createSovaStatusChannel = () => ({ stop() {} });
globalThis.apiUrl = (p) => '/api' + p;
globalThis.LOG_LEVEL_COLORS = {};

globalThis.__onFetch = () => Promise.reject(new Error('offline'));
globalThis.fetchAPI = (url) => globalThis.__onFetch(url);
const dir = path.join(staticDir, 'js', 'pages', 'agents');
const load = (n) => import(pathToFileURL(path.join(dir, n + '.js')).href);
await load('actions');
const viewerModule = await load('log-viewer');
for (const n of ['work-items', 'launch-modal', 'pr-tracker', 'agent-cards', 'history', 'kanban', 'live-status']) {
  await load(n);
}
viewerModule.init();

const fire = () => docListeners.filter(([t]) => t === 'visibilitychange').forEach(([, fn]) => fn());
const tick = () => new Promise((resolve) => setTimeout(resolve, 0));
const texts = () => [...content.html.matchAll(/<span class="text-gray-300">([^<]*)<\/span>/g)].map(m => m[1]);
// Opens run 7's logs and lets the first-open backfill (an empty run) land, so the viewer streams live. The
// helper answers that one backfill itself, then restores the scenario's own fetch handler.
async function openRunLive() {
  const previous = globalThis.__onFetch;
  globalThis.__onFetch = (url) => (url.includes('/output?since=0')
    ? Promise.resolve({ lines: [], total: 0, seq: 0 })
    : previous(url));
  viewerModule._autoOpenLogsIfIdle(7);
  FakeEventSource.all[0].emit('connected', '{"seq": 0}');
  await tick(); await tick();
  globalThis.__onFetch = previous;
}
const out = {};
"""

_LOG_RESUME_SCENARIO = r"""
let release;
const backfill = new Promise((resolve) => { release = resolve; });
const backfillUrls = [];
globalThis.__onFetch = (url) => {
  if (url.includes('/output?since=0')) { backfillUrls.push(url); return backfill; }
  return Promise.reject(new Error('offline'));
};



await openRunLive();
FakeEventSource.all[0].emit('output', 'live-1');
out.before = texts();

doc.hidden = true; fire();
out.closedOnHide = FakeEventSource.all[0].closed;

doc.hidden = false; fire();
const resumed = FakeEventSource.all[1];
out.reconnected = FakeEventSource.all.length === 2 && !resumed.closed;
out.backfillBeforeConnected = backfillUrls.length;
resumed.emit('connected', '{}');
await tick();
out.backfillAfterConnected = backfillUrls.length;
resumed.emit('output', 'l3');
resumed.emit('output', 'l4');
out.heldWhileInFlight = texts();
release({ lines: ['l1', 'l2', 'l3'], total: 3 });
await tick(); await tick();
out.afterBackfill = texts();
resumed.emit('output', 'l5');
out.afterLive = texts();
process.stdout.write(JSON.stringify(out));
"""

_LOG_POLL_SCENARIO = r"""
const pending = [];
globalThis.__onFetch = (url) => {
  if (!url.includes('/output?since=')) return Promise.reject(new Error('offline'));
  return new Promise((resolve) => pending.push(resolve));
};

// An SSE failure falls back to polling; poll 1 is then in flight when the tab is hidden and shown again.
viewerModule._autoOpenLogsIfIdle(7);
FakeEventSource.all[0].onerror();
out.firstPollInFlight = pending.length;
doc.hidden = true; fire();
doc.hidden = false; fire();
out.secondPollInFlight = pending.length;
pending[0]({ lines: ['a'], total: 1 });
await tick();
out.afterStalePoll = texts();
pending[1]({ lines: ['a'], total: 1 });
await tick();
out.afterCurrentPoll = texts();
out.chainsRescheduled = pending.length;
process.stdout.write(JSON.stringify(out));
"""


# The reconnect's three less usual endings. In each, the viewer opens run 7, the tab is hidden and shown, and the
# resumed stream (FakeEventSource.all[1]) says it is subscribed while the backfill fetch is still in flight.
_LOG_EXACT_OVERLAP_SCENARIO = r"""
let release;
globalThis.__onFetch = (url) => {
  if (url.includes('/output?since=0')) return new Promise((resolve) => { release = resolve; });
  return Promise.reject(new Error('offline'));
};

await openRunLive();
doc.hidden = true; fire();
doc.hidden = false; fire();
const resumed = FakeEventSource.all[1];
// Two lines were buffered when the stream subscribed; the snapshot, read later, holds four. The last two
// snapshot lines are therefore also in the stream, and the stream's third line is genuinely new, even
// though all three read "dot".
resumed.emit('connected', '{"seq": 2}');
await tick();
resumed.emit('output', 'dot'); resumed.emit('output', 'dot'); resumed.emit('output', 'dot');
release({ lines: ['p', 'dot', 'dot', 'dot'], total: 4, seq: 4 });
await tick(); await tick();
out.afterBackfill = texts();
process.stdout.write(JSON.stringify(out));
"""

_LOG_DONE_DURING_BACKFILL_SCENARIO = r"""
let release;
globalThis.__onFetch = (url) => {
  if (url.includes('/output?since=0')) return new Promise((resolve) => { release = resolve; });
  return Promise.reject(new Error('offline'));
};

await openRunLive();
doc.hidden = true; fire();
doc.hidden = false; fire();
const resumed = FakeEventSource.all[1];
resumed.emit('connected', '{}');
await tick();
// The run ends while the backfill is slow: the stream reports "done" before the snapshot lands.
resumed.emit('output', 'last');
resumed.emit('done', '{}');
out.closedOnDone = resumed.closed;
out.beforeBackfill = texts();
release({ lines: ['first'], total: 1 });
await tick(); await tick();
out.afterBackfill = texts();
process.stdout.write(JSON.stringify(out));
"""

_LOG_BACKFILL_FAILS_SCENARIO = r"""
let backfills = 0;
let resolvePoll;
globalThis.__onFetch = (url) => {
  if (!url.includes('/output?since=0')) return Promise.reject(new Error('offline'));
  backfills++;
  if (backfills === 1) return Promise.reject(new Error('backfill failed'));
  return new Promise((resolve) => { resolvePoll = resolve; });
};

await openRunLive();
FakeEventSource.all[0].emit('output', 'before-hide');
out.before = texts();
doc.hidden = true; fire();
doc.hidden = false; fire();
const resumed = FakeEventSource.all[1];
resumed.emit('connected', '{}');
resumed.emit('output', 'held-line');
await tick(); await tick();
out.streamClosed = resumed.closed;
out.afterFailure = texts();
out.fetchesAfterFailure = backfills;
resolvePoll({ lines: ['all-1', 'all-2'], total: 2 });
await tick();
out.afterPoll = texts();
// The poll chain reschedules itself and the stub rejects its next fetch, so end the process explicitly.
process.stdout.write(JSON.stringify(out), () => process.exit(0));
"""

_GITHUB_USER_SCENARIO = r"""
const workItems = await load('work-items');
let calls = 0;
workItems.onGithubUserChange(() => { throw new Error('a listener that throws'); });
workItems.onGithubUserChange(() => { calls++; });
globalThis.fetch = () => Promise.resolve({
  ok: true,
  json: () => Promise.resolve({ items: [], github_user: 'octocat', slots_available: 1, max_concurrent: 2 }),
});
const errors = [];
console.error = (...a) => errors.push(a.map(String).join(' '));
out.before = workItems.getGithubUser();
await workItems.loadWorkItems();
out.afterFirstLoad = [workItems.getGithubUser(), calls];
await workItems.loadWorkItems();
out.callsAfterSecondLoad = calls;
out.errorsLogged = errors.length;
process.stdout.write(JSON.stringify(out));
"""


# The Agents page's "N of M agents running": the running count is the pool's (agents_running), and the limit is a
# settingStepper for max_parallel_agents built once, then updated in place on each poll.
_AGENT_LIMIT_SCENARIO = r"""
const workItems = await load('work-items');
const els = {};
const baseGetElementById = doc.getElementById;
doc.getElementById = (id) => {
  if (id === 'agent-limit-running' || id === 'agent-limit-control') {
    return els[id] ||= { textContent: '', children: [], appendChild(c) { this.children.push(c); } };
  }
  return baseGetElementById(id);
};
const made = [];
const setValues = [];
globalThis.settingStepper = (key, opts) => {
  const ctl = { key, opts, sovaSetValue: (v) => setValues.push(v) };
  made.push({ key, value: opts.value, min: opts.min, step: opts.step });
  return ctl;
};
let reply = {
  items: [], github_user: 'octocat', running_count: 0, agents_running: 2, slots_available: 2, max_concurrent: 4,
};
globalThis.fetch = () => Promise.resolve({ ok: true, json: () => Promise.resolve(reply) });
await workItems.loadWorkItems();
out.running = els['agent-limit-running'].textContent;
out.made = made;
out.mounted = els['agent-limit-control'].children.length;
reply = Object.assign({}, reply, { agents_running: 1, max_concurrent: 3 });
await workItems.loadWorkItems();
out.runningAfterPoll = els['agent-limit-running'].textContent;
out.madeAfterPoll = made.length;
out.setValues = setValues;
// A save completed while the next poll was in flight: that poll's limit may predate the save and is not shown.
let saves = 0;
globalThis.settingSaveCount = () => saves;
reply = Object.assign({}, reply, { agents_running: 0, max_concurrent: 2 });
globalThis.fetch = () => { saves += 1; return Promise.resolve({ ok: true, json: () => Promise.resolve(reply) }); };
await workItems.loadWorkItems();
out.runningAfterStalePoll = els['agent-limit-running'].textContent;
out.setValuesAfterStalePoll = setValues;
process.stdout.write(JSON.stringify(out));
"""


# The first open of a running agent's logs backfills like a tab return does: the stream's lines are held until the
# snapshot of everything written before it connected has been rendered.
_LOG_FIRST_OPEN_SCENARIO = r"""
let release;
const backfillUrls = [];
globalThis.__onFetch = (url) => {
  if (url.includes('/output?since=0')) {
    backfillUrls.push(url);
    return new Promise((resolve) => { release = resolve; });
  }
  return Promise.reject(new Error('offline'));
};

viewerModule._autoOpenLogsIfIdle(7);
const stream = FakeEventSource.all[0];
out.backfillBeforeConnected = backfillUrls.length;
// Two lines were buffered when the stream subscribed; the snapshot, read later, holds three.
stream.emit('connected', '{"seq": 2}');
await tick();
out.backfillAfterConnected = backfillUrls.length;
stream.emit('output', 'l3');
out.heldWhileInFlight = texts();
release({ lines: ['l1', 'l2', 'l3'], total: 3, seq: 3 });
await tick(); await tick();
out.afterBackfill = texts();
stream.emit('output', 'l4');
out.afterLive = texts();
process.stdout.write(JSON.stringify(out));
"""

# A group collapsed on first render must expand on the first click and collapse on the next.
_PR_GROUP_TOGGLE_SCENARIO = r"""
globalThis.safeStorageGet = () => null;
globalThis.safeStorageSet = () => {};
globalThis.fetch = () => Promise.resolve({
  ok: true,
  json: () => Promise.resolve({ prs: [{
    number: 1, title: 'wip', computed_state: 'draft', ci_status: 'pending', age_seconds: 60,
    state_label: 'Draft PR', state_color: 'gray', is_draft: true,
  }] }),
});
const list = {
  html: '', style: {},
  classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
  get innerHTML() { return this.html; }, set innerHTML(v) { this.html = v; },
};
const groups = {};
// A group's content element, built from the markup the module rendered: its class list, and whether a bare
// `hidden` attribute (which no class toggle can clear) was rendered on it.
const groupEl = (id) => {
  if (groups[id]) return groups[id];
  const match = new RegExp('<div id="pr-group-' + id + '"([^>]*)>').exec(list.html);
  if (!match) return null;
  const classes = new Set(/class="([^"]*)"/.exec(match[1])[1].split(/\s+/));
  const bareHidden = /\shidden[\s>]/.test(match[1].replace(/class="[^"]*"/, ''));
  groups[id] = {
    style: {}, setAttribute() {},
    classList: {
      contains: (c) => classes.has(c),
      add: (c) => classes.add(c),
      remove: (c) => classes.delete(c),
    },
    visible: () => !classes.has('hidden') && !bareHidden,
  };
  return groups[id];
};
const baseGetElementById = doc.getElementById;
doc.getElementById = (id) => {
  if (id === 'pr-tracker-list') return list;
  const match = /^pr-group-(.+)$/.exec(id);
  if (match && !id.endsWith('-arrow') && !id.startsWith('pr-group-toggle-')) return groupEl(match[1]);
  return baseGetElementById(id);
};
const clickHandlers = [];
doc.body.addEventListener = (type, fn) => { if (type === 'click') clickHandlers.push(fn); };
(await load('actions')).init();
const polled = [];
globalThis.visibilityAwarePoll = (fn) => { polled.push(fn); return { id: null }; };
(await load('pr-tracker')).init();
await polled[0]();

const toggle = () => {
  const attrs = { 'data-action': 'toggle-pr-group', 'data-args': JSON.stringify(['draft-in-progress']) };
  const node = { getAttribute: (n) => attrs[n] ?? null };
  const event = { target: { closest: () => node }, stopPropagation() {} };
  clickHandlers.forEach((fn) => fn(event));
};
out.rendered = list.html.includes('pr-group-draft-in-progress');
const group = groupEl('draft-in-progress');
out.visible = [group.visible()];
for (let i = 0; i < 3; i++) { toggle(); out.visible.push(group.visible()); }
process.stdout.write(JSON.stringify(out));
"""

# The Agents task list buckets by work state, never by run history: every item the API returns must be
# reachable under "All", and an old failed run must not lift an issue above higher-priority work.
_TASK_BUCKETS_SCENARIO = r"""
globalThis._priorityRankMap = () => ({ critical: 0, high: 1, medium: 2, low: 3 });
const item = (n, state, extra) => Object.assign(
  { issue_number: n, title: 'T' + n, state, issue_priority: 'medium', priority: 0, assignees: [] }, extra || {});
const items = [
  item(1, 'agent_running', { running_agent: { role: 'developer', run_id: 'r1' }, assignees: ['octocat'] }),
  item(2, 'pr_awaiting_review', { pr_number: 20, pr_details: { author: 'someone', assignees: ['OctoCat'] } }),
  item(3, 'in_progress'),
  item(4, 'spec_review'),
  item(5, 'pr_draft', { pr_number: 50, pr_details: { author: 'someone', assignees: ['other'] } }),
  item(6, 'triaged', { issue_priority: 'low', last_run: { status: 'failed' } }),
  item(7, 'researched', { issue_priority: 'critical' }),
  item(8, 'backlog', { issue_priority: 'high', assignees: ['octocat'] }),
  item(9, 'needs_spec', { issue_priority: 'medium', assignees: ['octocat'] }),
  item(10, 'human_only', { issue_priority: 'low', assignees: ['octocat'] }),
  item(11, 'triaged', { issue_priority: 'medium', last_run: { status: 'interrupted' } }),
  item(12, '', { assignees: ['octocat'] }),
  item(13, 'some_future_state'),
  item(14, 'merged', { assignees: ['octocat'] }),
];
const grid = { html: '', get innerHTML() { return this.html; }, set innerHTML(v) { this.html = v; } };
const search = { value: '', addEventListener() {} };
const baseGetElementById = doc.getElementById;
doc.getElementById = (id) => (id === 'task-grid' ? grid : id === 'task-search' ? search : baseGetElementById(id));
const clickHandlers = [];
doc.body.addEventListener = (type, fn) => { if (type === 'click') clickHandlers.push(fn); };
globalThis.fetch = () => Promise.resolve({
  ok: true,
  json: () => Promise.resolve({ items, slots_available: 1, max_concurrent: 2, github_user: 'octocat' }),
});
(await load('actions')).init();
const workItems = await load('work-items');
workItems.init();
await workItems.loadWorkItems();

const click = (action, args) => {
  const attrs = { 'data-action': action, 'data-args': args === undefined ? null : JSON.stringify(args) };
  const event = { target: { closest: () => ({ getAttribute: (n) => attrs[n] ?? null }) }, stopPropagation() {} };
  clickHandlers.forEach((fn) => fn(event));
};
const shown = () => [...grid.html.matchAll(/>T(\d+)</g)].map((m) => Number(m[1]));
out.collapsed = shown();
out.hasShowMore = grid.html.includes('expand-suggested');
click('expand-suggested');
out.expanded = shown();
out.hasShowLess = grid.html.includes('collapse-suggested');
click('collapse-suggested');
out.recollapsed = shown();
// "Mine" renders its matches uncapped; "All" caps each block until expanded, so the criterion is that every
// item "Mine" shows stays reachable under "All" once both blocks are expanded.
click('set-task-assignee', 'mine');
out.mine = shown();
click('set-task-assignee', 'all');
click('expand-tasks');
click('expand-tasks');
click('expand-suggested');
out.allExpanded = shown();
process.stdout.write(JSON.stringify(out));
"""

# The 30s work-items poll lands on an unchanged payload almost every time: the grid must not be rewritten then
# (an open card menu survives), but every input that changes the markup, and the early-return views, must.
_TASK_GRID_REWRITE_SCENARIO = r"""
globalThis._priorityRankMap = () => ({ critical: 0, high: 1, medium: 2, low: 3 });
const items = [
  { issue_number: 1, title: 'T1', state: 'agent_running', issue_priority: 'medium', priority: 0, assignees: ['octocat'],
    running_agent: { role: 'developer', run_id: 'r1', elapsed_seconds: 10 } },
  { issue_number: 2, title: 'T2', state: 'triaged', issue_priority: 'low', priority: 0, assignees: [] },
];
let writes = 0;
const grid = { html: '', get innerHTML() { return this.html; }, set innerHTML(v) { writes++; this.html = v; } };
let onSearch = () => {};
const search = { value: '', addEventListener(_, fn) { onSearch = fn; } };
const baseGetElementById = doc.getElementById;
doc.getElementById = (id) => (id === 'task-grid' ? grid : id === 'task-search' ? search : baseGetElementById(id));
const clickHandlers = [];
doc.body.addEventListener = (type, fn) => { if (type === 'click') clickHandlers.push(fn); };
globalThis.fetch = () => Promise.resolve({
  ok: true,
  json: () => Promise.resolve({ items: JSON.parse(JSON.stringify(items)), slots_available: 1, max_concurrent: 2,
    github_user: 'octocat' }),
});
(await load('actions')).init();
const workItems = await load('work-items');
workItems.init();
await workItems.loadWorkItems();

const afterFirstLoad = writes;
// A running agent's elapsed_seconds moves on every fetch, but no card shows it: the grid must stay put.
items[0].running_agent.elapsed_seconds = 40;
await workItems.loadWorkItems();
out.writesOnIdenticalReload = writes - afterFirstLoad;

items[1].title = 'T2 renamed';
await workItems.loadWorkItems();
out.writesOnChangedData = writes - afterFirstLoad;

const beforeFilter = writes;
const type = (text) => { search.value = text; onSearch(); };
type('zzz');
out.noMatch = grid.html.includes('No matching issues');
type('');
out.restored = grid.html.includes('T1');
type('zzz');
out.noMatchAgain = grid.html.includes('No matching issues');
out.writesOnFilterChanges = writes - beforeFilter;
process.stdout.write(JSON.stringify(out));
"""

# Storage that throws (blocked cookies, some private-browsing modes) must not stop a module from evaluating:
# one that throws while the import graph loads leaves no init() running and the whole page blank.
_THROWING_STORAGE_SCENARIO = r"""
const blocked = () => { throw new Error('SecurityError'); };
globalThis.localStorage = { getItem: blocked, setItem: blocked };
globalThis.sessionStorage = globalThis.localStorage;
const storageStart = appSrc.indexOf('function safeStorageGet(');
const storageEnd = appSrc.indexOf('\n}\n', appSrc.indexOf('function safeStorageSet(')) + 3;
new Function(appSrc.slice(storageStart, storageEnd)
  + '; globalThis.safeStorageGet = safeStorageGet; globalThis.safeStorageSet = safeStorageSet;')();
for (const name of ['work-items', 'pr-tracker']) {
  try {
    await import(pathToFileURL(path.join(dir, name + '.js')).href + '?throwing-storage');
    out[name] = 'evaluated';
  } catch (e) { out[name] = String(e); }
}
process.stdout.write(JSON.stringify(out));
"""

# The interrupted-runs banner must load on page load and on tab return even when the status socket never opens.
_INTERRUPTED_WITHOUT_SOCKET_SCENARIO = r"""
const pollStart = appSrc.indexOf('function visibilityAwarePoll(');
const pollEnd = appSrc.indexOf('\n}\n', pollStart) + 3;
globalThis.visibilityAwarePoll = new Function(
  'document', 'setInterval', 'clearInterval', appSrc.slice(pollStart, pollEnd) + '; return visibilityAwarePoll;',
)(doc, () => 1, () => {});
const urls = [];
globalThis.__onFetch = (url) => {
  urls.push(url);
  if (url.includes('/agents/interrupted')) return Promise.resolve({ interrupted: [] });
  return Promise.reject(new Error('offline'));
};
const interruptedFetches = () => urls.filter((u) => u.includes('/agents/interrupted')).length;
// The stub createSovaStatusChannel never calls onOpen, which is what a socket that cannot connect looks like.
(await load('live-status')).init();
await tick();
out.afterLoad = interruptedFetches();
doc.hidden = true; fire();
doc.hidden = false; fire();
await tick();
out.afterTabReturn = interruptedFetches();
process.stdout.write(JSON.stringify(out));
"""


# The server reports a line it dropped for a slow reader as a "gap" event. The viewer must stop trusting the stream
# (the sequence arithmetic would be short by the dropped lines) and re-read the run from line 0 by polling.
_LOG_GAP_SCENARIO = r"""
const resolvers = [];
globalThis.__onFetch = (url) => {
  if (url.includes('/output?since=0')) return new Promise((resolve) => resolvers.push(resolve));
  return Promise.reject(new Error('offline'));
};

viewerModule._autoOpenLogsIfIdle(7);
const stream = FakeEventSource.all[0];
stream.emit('connected', '{"seq": 2}');
await tick();
out.backfillsBeforeGap = resolvers.length;
stream.emit('output', 'held-1');
stream.emit('gap', '{}');
out.streamClosed = stream.closed;
// The snapshot of the abandoned reconcile lands after the gap and must not be rendered over the re-read.
resolvers[0]({ lines: ['stale-1', 'stale-2'], total: 2, seq: 2 });
await tick(); await tick();
out.afterStaleSnapshot = texts();
out.rereadStarted = resolvers.length;
resolvers[1]({ lines: ['all-1', 'all-2', 'all-3'], total: 3 });
await tick();
out.afterReread = texts();
process.stdout.write(JSON.stringify(out), () => process.exit(0));
"""

# "connected" is what releases the held lines. A buffering proxy can delay it indefinitely without any error event,
# so the viewer gives the stream up for polling after a short wait instead of showing an empty panel for good.
_LOG_FAKE_WAIT_TIMER = r"""
const timers = [];
const realSetTimeout = globalThis.setTimeout;
globalThis.setTimeout = (fn, ms, ...rest) => {
  if (ms !== 3000) return realSetTimeout(fn, ms, ...rest);
  const handle = { ms, fn };
  timers.push(handle);
  return handle;
};
const realClearTimeout = globalThis.clearTimeout;
globalThis.clearTimeout = (handle) => {
  if (handle && handle.fn) handle.cleared = true;
  else realClearTimeout(handle);
};
const resolvers = [];
globalThis.__onFetch = (url) => {
  if (url.includes('/output?since=0')) return new Promise((resolve) => resolvers.push(resolve));
  return Promise.reject(new Error('offline'));
};
// What the browser does when the wait elapses, unless "connected" already cancelled it.
const fireWait = (i) => { if (!timers[i].cleared) timers[i].fn(); };
"""

_LOG_CONNECT_TIMEOUT_SCENARIO = (
    _LOG_FAKE_WAIT_TIMER
    + r"""
viewerModule._autoOpenLogsIfIdle(7);
const silent = FakeEventSource.all[0];
out.waitMs = timers[0].ms;
out.fetchesBeforeWait = resolvers.length;
fireWait(0);
out.silentStreamClosed = silent.closed;
out.fetchesAfterWait = resolvers.length;
resolvers[0]({ lines: ['a', 'b'], total: 2 });
await tick();
out.silentRendered = texts();
process.stdout.write(JSON.stringify(out), () => process.exit(0));
"""
)

_LOG_CONNECT_IN_TIME_SCENARIO = (
    _LOG_FAKE_WAIT_TIMER
    + r"""
viewerModule._autoOpenLogsIfIdle(7);
const live = FakeEventSource.all[0];
live.emit('connected', '{"seq": 0}');
out.waitCancelled = timers[0].cleared === true;
fireWait(0);
out.liveStreamClosed = live.closed;
await tick();
resolvers[0]({ lines: ['a'], total: 1, seq: 1 });
await tick(); await tick();
live.emit('output', 'b');
out.rendered = texts();
process.stdout.write(JSON.stringify(out), () => process.exit(0));
"""
)

# The PR tracker redraws when the GitHub user arrives, but not over a failed or not yet loaded list.
_PR_TRACKER_USER_ARRIVAL_SCENARIO = r"""
const workItems = await load('work-items');
const list = {
  html: '', style: {},
  classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
  get innerHTML() { return this.html; }, set innerHTML(v) { this.html = v; },
};
const baseGetElementById = doc.getElementById;
doc.getElementById = (id) => (id === 'pr-tracker-list' ? list : baseGetElementById(id));
const clickHandlers = [];
doc.body.addEventListener = (type, fn) => { if (type === 'click') clickHandlers.push(fn); };
(await load('actions')).init();
const polled = [];
globalThis.visibilityAwarePoll = (fn) => { polled.push(fn); return { id: null }; };
let prsOk = false;
globalThis.fetch = (url) => {
  if (url.includes('/prs/open')) {
    return prsOk
      ? Promise.resolve({ ok: true, json: () => Promise.resolve({ prs: [{
          number: 1, title: 'wip', computed_state: 'draft', ci_status: 'pending', age_seconds: 60,
          state_label: 'Draft PR', state_color: 'gray', is_draft: true, author: 'octocat',
        }] }) })
      : Promise.reject(new Error('github is down'));
  }
  return Promise.resolve({
    ok: true,
    json: () => Promise.resolve({ items: [], github_user: 'octocat', slots_available: 1, max_concurrent: 2 }),
  });
};
console.warn = () => {};
(await load('pr-tracker')).init();
const loadPrs = polled[0];
const click = (action, args) => {
  const attrs = { 'data-action': action, 'data-args': JSON.stringify(args) };
  const node = { getAttribute: (n) => attrs[n] ?? null };
  clickHandlers.forEach((fn) => fn({ target: { closest: () => node }, stopPropagation() {} }));
};

click('set-pr-filter', ['mine']);
out.beforeAnyLoad = list.html;
await loadPrs();
out.afterFailedLoad = list.html;
await workItems.loadWorkItems();
out.afterUserArrivesOverFailure = list.html;
click('set-pr-filter', ['mine']);
out.afterFilterClickOverFailure = list.html;
prsOk = true;
await loadPrs();
out.afterRecovery = list.html.includes('pr-group-draft-in-progress');
process.stdout.write(JSON.stringify(out));
"""


# The PR tracker's Mine filter keeps a PR the user authored or is assigned to, once each, in any letter case.
_PR_TRACKER_MINE_SCENARIO = r"""
const workItems = await load('work-items');
const list = {
  html: '', style: {},
  classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
  get innerHTML() { return this.html; }, set innerHTML(v) { this.html = v; },
};
const baseGetElementById = doc.getElementById;
doc.getElementById = (id) => (id === 'pr-tracker-list' ? list : baseGetElementById(id));
const clickHandlers = [];
doc.body.addEventListener = (type, fn) => { if (type === 'click') clickHandlers.push(fn); };
(await load('actions')).init();
const polled = [];
globalThis.visibilityAwarePoll = (fn) => { polled.push(fn); return { id: null }; };
const pr = (number, extra) => Object.assign({
  number, title: 't' + number, computed_state: 'draft', ci_status: 'pending', age_seconds: 60,
  state_label: 'Draft PR', state_color: 'gray', is_draft: true, author: 'someone', assignees: [],
}, extra);
globalThis.fetch = (url) => {
  if (url.includes('/prs/open')) {
    return Promise.resolve({ ok: true, json: () => Promise.resolve({ prs: [
      pr(1, { author: 'octocat' }),
      pr(2, { author: 'someone', assignees: ['OctoCat'] }),
      pr(3, { author: null, assignees: ['octocat'] }),
      pr(4, { author: 'OCTOCAT', assignees: ['octocat'] }),
      pr(5, { author: 'someone', assignees: ['other'] }),
      pr(6, { author: 'someone', assignees: undefined }),
      pr(7, { author: 'someone', assignees: null }),
    ] }) });
  }
  return Promise.resolve({
    ok: true,
    json: () => Promise.resolve({ items: [], github_user: 'octocat', slots_available: 1, max_concurrent: 2 }),
  });
};
console.warn = () => {};
(await load('pr-tracker')).init();
await workItems.loadWorkItems();
await polled[0]();
const click = (action, args) => {
  const attrs = { 'data-action': action, 'data-args': JSON.stringify(args) };
  const node = { getAttribute: (n) => attrs[n] ?? null };
  clickHandlers.forEach((fn) => fn({ target: { closest: () => node }, stopPropagation() {} }));
};
click('set-pr-filter', ['mine']);
out.mine = [];
for (let n = 1; n <= 7; n++) {
  const hits = list.html.split('PR #' + n + '<').length - 1;
  if (hits) out.mine.push([n, hits]);
}
process.stdout.write(JSON.stringify(out));
"""


class TestAgentsPageMarkup:
    def test_template_has_no_inline_script_logic_or_handlers(self) -> None:
        html = (_TEMPLATES / "agents.html").read_text(encoding="utf-8")
        assert not re.search(r"\son(?:click|change|input|keydown|error)=", html)
        assert "function " not in html
        scripts = re.findall(r"<script\b[^>]*>(.*?)</script>", html, re.DOTALL)
        assert len(scripts) == 1
        assert '<script type="module">' in html

    def test_the_inline_entry_script_parses(self) -> None:
        """make lint-js sees only files under static/, and this block is what imports and starts every module:
        a stray bracket in its init list would blank the page while the lint still passed."""
        _require_node("node is required to syntax-check the Agents page entry script")
        html = (_TEMPLATES / "agents.html").read_text(encoding="utf-8")
        match = re.search(r'<script type="module">(.*?)</script>', html, re.DOTALL)
        assert match, "agents.html has no module entry script"
        proc = subprocess.run(
            ["node", "--input-type=module", "--check"],
            input=match.group(1),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr

    @pytest.mark.parametrize("name", _PAGE_MODULES)
    def test_each_module_is_imported_and_initialised(self, name: str) -> None:
        html = (_TEMPLATES / "agents.html").read_text(encoding="utf-8")
        assert f"/static/js/pages/agents/{name}.js" in html
        assert "export function init() {" in _module_source(name)

    def test_each_init_runs_isolated_with_actions_first(self) -> None:
        """One module throwing during init() must not stop the modules after it from starting."""
        html = (_TEMPLATES / "agents.html").read_text(encoding="utf-8")
        script = html[html.index('<script type="module">') :]
        assert "try {" in script
        assert "console.error('agents page: ' + name + ' init failed', e);" in script
        order = re.findall(r"\['([\w-]+)', init\w+\]", script)
        assert order[0] == "actions", "the others register their click handlers into it"
        assert order == ["actions", *(m for m in _PAGE_MODULES)]
        assert not re.search(r"^init\w+\(\);$", script, re.MULTILINE), "a bare init call runs outside the guard"

    def test_render_utils_is_a_stateless_leaf_without_an_init(self) -> None:
        """The one page module exempt from the init() rule: pure helpers, nothing to start, no sibling imports."""
        source = _module_source("render-utils")
        assert "export function init(" not in source
        assert not re.search(r"^import\b", source, re.MULTILINE)
        assert "Stateless" in source

    def test_template_is_a_small_shell(self) -> None:
        assert len((_TEMPLATES / "agents.html").read_text(encoding="utf-8").splitlines()) < 400

    @pytest.mark.parametrize("name", ["control.html", "tasks.html", "overview.html", "runs.html"])
    def test_orphaned_templates_stay_deleted(self, name: str) -> None:
        assert not (_TEMPLATES / name).exists()

    def test_no_module_exports_a_mutable_binding(self) -> None:
        """Another module changing a value this one owns goes through a function here (a getter, or a
        prune/set helper), never through an exported `var`/`let` it can reassign or delete keys from."""
        for name, source in _all_page_sources().items():
            assert not re.search(r"^export\s+(?:var|let)\b", source, re.MULTILINE), name


class TestDelegatedActions:
    @staticmethod
    def _emitted_actions() -> set[str]:
        emitted: set[str] = set()
        html = (_TEMPLATES / "agents.html").read_text(encoding="utf-8")
        emitted.update(re.findall(r'data-action="([\w-]+)"', html))
        for source in _all_page_sources().values():
            emitted.update(re.findall(r"actionAttrs\('([\w-]+)'", source))
            emitted.update(re.findall(r'data-action="([\w-]+)"', source))
        return emitted

    @staticmethod
    def _registered_actions() -> set[str]:
        registered: set[str] = set()
        for source in _all_page_sources().values():
            for block in re.findall(r"registerActions\(\{(.*?)\n  \}\);", source, re.DOTALL):
                registered.update(re.findall(r"^    '([\w-]+)':", block, re.MULTILINE))
        return registered

    def test_every_emitted_action_has_a_handler(self) -> None:
        missing = self._emitted_actions() - self._registered_actions()
        assert not missing, f"data-action names with no registered handler: {sorted(missing)}"

    def test_history_rows_emit_only_actions_their_module_or_actions_js_owns(self) -> None:
        """A control inside a history row wins the click over the row's `navigate`, so every action it names must be
        one history.js registers itself or the generic `stop`/`navigate` pair actions.js owns (never a card handler).
        The one deliberate exception is `rerun`, registered by launch-modal.js: it reads `data-run-id` and stops
        propagation, so it is safe inside a row, and the assertion below pins that owner."""
        history = _module_source("history")
        emitted = set(re.findall(r"actionAttrs\('([\w-]+)'", history)) | set(
            re.findall(r'data-action="([\w-]+)"', history)
        )
        owned: set[str] = set()
        for block in re.findall(r"registerActions\(\{(.*?)\n  \}\);", history, re.DOTALL):
            owned.update(re.findall(r"^    '([\w-]+)':", block, re.MULTILINE))
        owned |= {"stop", "navigate"}
        assert emitted <= owned | {"rerun"}, sorted(emitted - owned)
        assert "'rerun':" in _module_source("launch-modal")
        actions = _module_source("actions")
        assert "'stop': function(el, e) { e.stopPropagation(); }" in actions

    def test_no_handler_is_registered_twice(self) -> None:
        names: list[str] = []
        for source in _all_page_sources().values():
            for block in re.findall(r"registerActions\(\{(.*?)\n  \}\);", source, re.DOTALL):
                names.extend(re.findall(r"^    '([\w-]+)':", block, re.MULTILINE))
        assert len(names) == len(set(names))

    def test_generated_markup_has_no_inline_handlers(self) -> None:
        for name, source in _all_page_sources().items():
            assert not re.search(r"\bon(?:click|change|input|keydown|error)=", source), name

    def test_handler_table_has_no_inherited_names(self) -> None:
        """A data-action of `constructor` or `toString` must not resolve to an Object.prototype member."""
        assert "var _handlers = Object.create(null);" in _module_source("actions")

    def test_dispatcher_resolves_the_closest_action_on_body(self) -> None:
        source = (AGENTS_PAGE_JS_DIR / "actions.js").read_text(encoding="utf-8")
        assert "e.target.closest('[data-action]')" in source
        assert "document.body.addEventListener('click', _dispatch)" in source

    def test_arguments_travel_as_escaped_json(self) -> None:
        source = (AGENTS_PAGE_JS_DIR / "actions.js").read_text(encoding="utf-8")
        assert "escapeHtml(JSON.stringify(args))" in source
        assert "JSON.parse(raw)" in source

    def test_malformed_arguments_are_reported_not_thrown(self) -> None:
        source = (AGENTS_PAGE_JS_DIR / "actions.js").read_text(encoding="utf-8")
        assert "console.error('Malformed data-args for data-action'" in source
        assert source.index("try {") < source.index("JSON.parse(raw)") < source.index("} catch (err) {")


class TestPollingUsesVisibilityAwarePoll:
    def test_no_raw_interval_timers_remain(self) -> None:
        for name, source in _all_page_sources().items():
            assert "setInterval(" not in source, name

    @pytest.mark.parametrize(
        ("module", "fn", "interval_ms"),
        [
            ("work-items", "loadWorkItems", 30000),
            ("work-items", "loadAgentsStatRow", 60000),
            ("pr-tracker", "loadPrTracker", 30000),
            ("pr-tracker", "_syncCrQuota", 300000),
            ("kanban", "loadKanban", 30000),
            ("live-status", "_pollDuringStatusOutage", 2000),
            ("live-status", "checkMemoryPressure", 5000),
            ("live-status", "checkInterrupted", 30000),
        ],
    )
    def test_cadence_is_unchanged(self, module: str, fn: str, interval_ms: int) -> None:
        source = _module_source(module)
        pattern = rf"visibilityAwarePoll\({fn}, (\w+)[,)]"
        match = re.search(pattern, source)
        assert match, f"{fn} is not polled through visibilityAwarePoll in {module}.js"
        value = match.group(1)
        if value == "_WS_FALLBACK_POLL_MS":
            value = re.search(r"var _WS_FALLBACK_POLL_MS = (\d+);", source).group(1)  # type: ignore[union-attr]
        assert int(value) == interval_ms

    def test_the_only_raw_timeouts_are_the_documented_exceptions(self) -> None:
        """Every poll goes through visibilityAwarePoll except pollLogs()'s backoff chain. The other raw
        timeouts are one-shot debounces and a fetch abort, none of which reschedules itself. A new
        self-rescheduling setTimeout elsewhere fails here instead of passing as an undocumented poller."""
        found = {
            (name, line.strip())
            for name, source in _all_page_sources().items()
            for line in source.splitlines()
            if "setTimeout(" in line and not line.lstrip().startswith("//")
        }
        assert found == {
            ("log-viewer", "logPollInterval = setTimeout(pollLogs, _logBackoffMs);"),
            ("log-viewer", "resume.connectTimer = setTimeout(function() {"),
            ("agent-cards", "_completedCleanupTimer = setTimeout(function() {"),
            ("live-status", "_workItemsRefreshTimer = setTimeout(function() {"),
            ("live-status", "_agentsRefreshTimer = setTimeout(function() {"),
            ("pr-tracker", "var tid = setTimeout(function() { ctrl.abort(); }, 10000);"),
            ("work-items", "var timer = setTimeout(function() { ctrl.abort(); }, _WORK_ITEMS_FETCH_TIMEOUT_MS);"),
            ("history", "_refreshTimer = setTimeout(function() {"),
            (
                "history",
                "_numberTimer = setTimeout(function() { _numberTimer = null; "
                "_state.number = typed; _reload(); }, 300);",
            ),
        }
        assert "pollLogs() below" in _module_source("log-viewer")

    def test_log_poll_drops_a_response_for_a_run_no_longer_shown(self) -> None:
        source = _module_source("log-viewer")
        body = source[source.index("async function pollLogs()") :]
        body = body[: body.index("\n}\n")]
        assert "var gen = _logPollGen;" in body
        assert "runId === activeLogRunId && gen === _logPollGen && !document.hidden" in body
        assert body.count("if (!isCurrent()) return;") == 2, "both the success and failure path must drop a stale poll"
        assert "if (isCurrent()) {" in body

    def test_every_poll_chain_stop_bumps_the_generation(self) -> None:
        """A fetch in flight cannot be cancelled, so the hide, close and restart paths must all invalidate it."""
        source = _module_source("log-viewer")
        cancel = source[source.index("function _cancelLogPolling()") :]
        cancel = cancel[: cancel.index("\n}\n")]
        assert "_logPollGen++" in cancel
        for fn in ("_startLogPolling", "closeLogViewer"):
            body = source[source.index(f"function {fn}()") :]
            assert "_cancelLogPolling();" in body[: body.index("\n}\n")], fn
        handler = source[source.index("document.addEventListener('visibilitychange'") :]
        hidden_branch = handler[: handler.index("} else {")]
        assert "_cancelLogPolling();" in hidden_branch

    def test_resume_opens_the_stream_before_the_backfill_lands(self) -> None:
        """The backfill runs only once the stream reports it is subscribed, and the stream holds its lines meanwhile."""
        source = _module_source("log-viewer")
        resume = source[source.index("function _resumeLogStream(") :]
        resume = resume[: resume.index("\n}\n")]
        assert "{ holding: true, held: [], connectedSeq: null, done: false, connectTimer: null }" in resume
        assert "_connectLogSSE(runId, resume);" in resume
        assert "since=0" not in resume
        connect = source[source.index("function _connectLogSSE(") :]
        connect = connect[: connect.index("\n}\n")]
        assert "addEventListener('connected'" in connect
        assert "resume.held.push(text)" in connect
        reconcile = source[source.index("async function _reconcileLogStream(") :]
        assert "/output?since=0" in reconcile
        assert "_logOverlap(snapshot, resume.held, resume.connectedSeq, snapshotSeq)" in reconcile

    def test_first_open_of_a_running_agent_backfills_like_a_tab_return(self) -> None:
        source = _module_source("log-viewer")
        view = source[source.index("function viewLogs(") :]
        view = view[: view.index("\n}\n")]
        assert "_resumeLogStream(runId);" in view
        assert "_connectLogSSE(runId)" not in view, "a bare connect would show only lines written after the click"

    @pytest.mark.parametrize(
        ("snapshot", "held", "connected_seq", "snapshot_seq", "expected"),
        [
            # No sequence numbers (a server that sends none, or an agent no longer in memory): text matching.
            (["a", "b", "c"], ["c", "d"], None, None, 1),
            (["x", "a", "b"], ["a", "b", "c"], None, None, 2),
            (["a", "b"], ["c"], None, None, 0),
            ([], ["a"], None, None, 0),
            (["a"], [], None, None, 0),
            (["a", "b"], ["a", "b"], None, None, 2),
            (["b", "a"], ["a"], 3, None, 1),
            (["b", "a"], ["a"], None, 3, 1),
            # Sequence numbers: exact, whatever the text. The snapshot holds every line before snapshot_seq and
            # the stream sent every line from connected_seq on.
            (["x", "a", "a"], ["a", "a"], 3, 3, 0),
            (["p", "dot", "dot", "dot"], ["dot", "dot", "dot"], 2, 4, 2),
            (["a", "b", "c"], ["c", "d"], 2, 3, 1),
            (["a"], ["b"], 5, 99, 1),
            (["a"], ["b", "c"], 5, 3, 0),
        ],
    )
    def test_overlap_between_backfill_tail_and_held_lines(
        self, snapshot: list[str], held: list[str], connected_seq: int | None, snapshot_seq: int | None, expected: int
    ) -> None:
        """A line written between the subscription and the snapshot is in both; only that one is dropped."""
        _require_node("node is required to run the log overlap helper")
        source = _module_source("log-viewer")
        start = source.index("function _logOverlap(")
        fn = source[start : source.index("\n}\n", start) + 2]
        args = ", ".join(json.dumps(v) for v in (snapshot, held, connected_seq, snapshot_seq))
        proc = subprocess.run(
            ["node", "-e", f"{fn}\nconsole.log(_logOverlap({args}));"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        assert int(proc.stdout.strip()) == expected

    def test_failed_pr_poll_keeps_the_list_but_marks_it_stale(self) -> None:
        source = _module_source("pr-tracker")
        body = source[source.index("async function loadPrTracker()") :]
        body = body[: body.index("\n}\n")]
        assert "markRefresh('pr-tracker', true," in body.split("} catch (e) {")[0]
        assert "markRefresh('pr-tracker', false," in body.split("} catch (e) {")[1]
        assert 'id="pr-tracker-stale"' in (_TEMPLATES / "agents.html").read_text(encoding="utf-8")

    def test_hidden_tab_handler_leaves_pollers_to_the_helper(self) -> None:
        source = _module_source("live-status")
        handler = source[source.index("document.addEventListener('visibilitychange'") :]
        for fn in ("loadPrTracker", "loadKanban", "_syncCrQuota", "checkMemoryPressure", "checkInterrupted"):
            assert fn not in handler


class TestStateDisplayHasOneSource:
    def test_work_item_states_all_have_a_label_and_a_token(self) -> None:
        for state in WorkItemState:
            assert state_label(state) == work_state._STATE_LABELS[state]
            assert state_color(state) == work_state._STATE_COLORS[state]

    def test_pr_and_tracker_states_resolve(self) -> None:
        assert state_label("approved_ci_green") == "Ready to Merge"
        assert state_color("approved_ci_green") == "green-strong"
        assert state_label("in_review") == "In Review"
        assert state_color("in_review") == "green"
        assert state_label("on_qa") == "On QA"
        assert state_color("on_qa") == "peach"

    def test_ready_to_merge_is_one_color_on_every_surface(self) -> None:
        """The PR tracker row (a PR state) and the task card's PR badge (a work item state) share a shade."""
        assert state_color("approved_ci_green") == state_color(WorkItemState.PR_READY_TO_MERGE)
        assert state_label("approved_ci_green") == state_label(WorkItemState.PR_READY_TO_MERGE)

    def test_approved_and_ready_to_merge_stay_apart(self) -> None:
        """An approved PR with CI not green must not look like one that can be merged."""
        assert state_color("approved") != state_color("approved_ci_green")
        assert (state_label("approved"), state_label("approved_ci_green")) == ("Approved", "Ready to Merge")

    @staticmethod
    def _canvas_hex(tokens: list[str]) -> list[str]:
        """stateHex() for each token, with SOVA_COLORS built by app.js's own initColors() from style.css values."""
        _require_node("node is required to run stateHex")
        css = (_STATIC / "style.css").read_text(encoding="utf-8")
        variables = dict(re.findall(r"(--ctp-[\w-]+):\s*(#[0-9a-fA-F]{6})", css))
        init = _APP_JS[
            _APP_JS.index("function initColors(") : _APP_JS.index("\n}\n", _APP_JS.index("function initColors("))
        ]
        hex_keys = _APP_JS[
            _APP_JS.index("var _STATE_HEX_KEYS") : _APP_JS.index("\n}\n", _APP_JS.index("function stateHex("))
        ]
        script = (
            f"globalThis.window = globalThis; const vars = {json.dumps(variables)};"
            "\nfunction getCSSVar(name) { return vars[name] || ''; }"
            f"\n{init}\n}}\n{hex_keys}\n}}\ninitColors();"
            f"\nconsole.log(JSON.stringify({json.dumps(tokens)}.map(stateHex)));"
        )
        proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30, check=False)
        assert proc.returncode == 0, proc.stderr
        return json.loads(proc.stdout)

    def test_states_that_must_be_told_apart_on_the_graph_get_different_canvas_colors(self) -> None:
        """The graph and its legend draw tokens as hex, so two tokens sharing one hex merge two states."""
        in_review, done = (state_color(s) for s in ("in_review", "done"))
        approved, ready = (state_color(s) for s in ("approved", "approved_ci_green"))
        first = self._canvas_hex([in_review, done])
        second = self._canvas_hex([approved, ready])
        assert first[0] != first[1], "a node still in review must not look finished"
        assert second[0] != second[1], "an approved PR must not look mergeable"

    def test_every_color_token_work_state_emits_has_its_own_canvas_hex(self) -> None:
        tokens = sorted(
            {
                *work_state._STATE_COLORS.values(),
                *(color for _, color in work_state._PR_STATE_META.values()),
                *(color for _, color in work_state._TASK_STATE_META.values()),
            }
        )
        hexes = self._canvas_hex(tokens)
        assert all(re.fullmatch(r"#[0-9a-fA-F]{6}", h) for h in hexes), dict(zip(tokens, hexes))
        assert len(set(hexes)) == len(tokens), dict(zip(tokens, hexes))

    def test_lifecycle_neighbours_do_not_share_a_color(self) -> None:
        assert len({state_color("in_progress"), state_color("in_review"), state_color("on_qa")}) == 3

    def test_no_label_is_drawn_in_two_colors(self) -> None:
        """ "In Review" is the tracker state and "Review Addressed" the PR state: one label, one color."""
        by_label: dict[str, set[str]] = {}
        for state in (*WorkItemState, *work_state._PR_STATE_META, *work_state._TASK_STATE_META):
            value = state.value if isinstance(state, WorkItemState) else state
            by_label.setdefault(state_label(value), set()).add(state_color(value))
        assert {label: colors for label, colors in by_label.items() if len(colors) > 1} == {}

    @pytest.mark.parametrize(
        ("pr_state", "work_item_state"),
        [
            ("draft", WorkItemState.PR_DRAFT),
            ("conflicted", WorkItemState.PR_CONFLICTED),
            ("ci_running", WorkItemState.PR_CI_RUNNING),
            ("ci_failed", WorkItemState.PR_CI_FAILED),
            ("changes_requested", WorkItemState.PR_EXTERNAL_CHANGES),
            ("approved_ci_green", WorkItemState.PR_READY_TO_MERGE),
            ("review_addressed", WorkItemState.PR_REVIEW_ADDRESSED),
            ("awaiting_review", WorkItemState.PR_AWAITING_REVIEW),
        ],
    )
    def test_pr_state_color_is_read_from_its_work_item_state(
        self, pr_state: str, work_item_state: WorkItemState
    ) -> None:
        assert state_color(pr_state) == state_color(work_item_state)

    @pytest.mark.parametrize(("pr_state", "work_item_state"), sorted(work_state._PR_STATE_WORK_ITEM.items()))
    def test_pr_state_label_is_read_from_its_work_item_state(
        self, pr_state: str, work_item_state: WorkItemState
    ) -> None:
        """The PR row and the task card showing that PR say the same thing."""
        assert state_label(pr_state) == state_label(work_item_state)

    def test_every_computed_pr_state_is_registered(self) -> None:
        for member in pr_service.ComputedPRState:
            assert member.value in work_state._PR_STATE_META, member

    def test_every_tracker_state_is_registered(self) -> None:
        for member in TaskState:
            assert work_state._state_meta(member.value) is not None, member

    def test_tracker_table_holds_exactly_the_states_work_item_state_lacks(self) -> None:
        """A tracker-only entry for a state the adapters never emit would read as if it flowed through the API."""
        lacking = {member.value for member in TaskState} - {member.value for member in WorkItemState}
        assert set(work_state._TASK_STATE_META) == lacking

    def test_unknown_state_is_visible_not_blank(self) -> None:
        assert state_label("dependency_graph_pseudo_state") == "Dependency Graph Pseudo State"
        assert state_color("dependency_graph_pseudo_state") == "gray"
        assert state_label("") == "Unknown"
        assert state_color("") == "gray"

    def test_unregistered_state_is_logged_once(self) -> None:
        import structlog

        work_state._reported_unknown_states.discard("brand_new_state")
        with structlog.testing.capture_logs() as logs:
            for _ in range(3):
                state_label("brand_new_state")
                state_color("brand_new_state")
        events = [entry for entry in logs if entry["event"] == "work_state.unregistered_state"]
        assert [entry["state"] for entry in events] == ["brand_new_state"]

    def test_empty_state_is_not_reported_as_unregistered(self) -> None:
        import structlog

        with structlog.testing.capture_logs() as logs:
            state_label("")
            state_color("")
        assert not [entry for entry in logs if entry["event"] == "work_state.unregistered_state"]

    def test_every_emitted_token_is_a_client_badge_class(self) -> None:
        match = re.search(r"var STATE_BADGE_CLASSES = \{(.*?)\n\};", _APP_JS, re.DOTALL)
        assert match
        client_tokens = set(re.findall(r"^\s*'([\w-]+)':", match.group(1), re.MULTILINE))
        emitted = set(work_state._STATE_COLORS.values())
        emitted |= {color for _, color in work_state._PR_STATE_META.values()}
        emitted |= {color for _, color in work_state._TASK_STATE_META.values()}
        emitted.add(work_state._FALLBACK_COLOR)
        assert emitted <= client_tokens, sorted(emitted - client_tokens)

    def test_every_token_resolves_to_a_hex_key(self) -> None:
        match = re.search(r"var _STATE_HEX_KEYS = \{(.*?)\n\};", _APP_JS, re.DOTALL)
        assert match
        hex_tokens = set(re.findall(r"'([\w-]+)':", match.group(1)))
        badge = re.search(r"var STATE_BADGE_CLASSES = \{(.*?)\n\};", _APP_JS, re.DOTALL)
        assert badge
        assert set(re.findall(r"^\s*'([\w-]+)':", badge.group(1), re.MULTILINE)) <= hex_tokens

    def test_pr_service_keeps_no_label_table_of_its_own(self) -> None:
        assert not hasattr(pr_service, "_STATE_LABELS")

    def test_client_keeps_no_state_color_tables(self) -> None:
        for name in ("PR_STATE_COLORS", "ISSUE_STATE_COLORS", "prStateBadgeColor", "issueStateColor"):
            assert name not in _APP_JS
        supervisor = (_TEMPLATES / "supervisor.html").read_text(encoding="utf-8")
        assert "function stateColor(" not in supervisor
        assert "function prStateColor(" not in supervisor
        assert "stateLabels" not in (_TEMPLATES / "queue.html").read_text(encoding="utf-8")


class TestStatePayloads:
    def test_pr_row_carries_label_and_color(self) -> None:
        computed = pr_service.ComputedPRState.CI_FAILED.value
        assert state_label(computed) == "CI Failed"
        assert state_color(computed) == "red"

    def test_graph_nodes_are_enriched(self) -> None:
        from sova.dashboard.routers.dependencies import _enrich_with_state_display

        graph = {
            "nodes": [
                {"id": 1, "state": "in_progress", "pr_state": "conflicted"},
                {"id": 2, "state": "closed"},
                {"id": 3, "state": "never_seen_before"},
            ]
        }
        _enrich_with_state_display(graph)
        first, second, third = graph["nodes"]
        assert (first["state_label"], first["state_color"]) == ("In Progress", "blue")
        assert (second["state_label"], second["state_color"]) == ("Closed", "gray")
        assert (third["state_label"], third["state_color"]) == ("Never Seen Before", "gray")

    def test_triaged_node_is_not_drawn_in_an_awaiting_color(self) -> None:
        """Graph dots come from the work item palette, and the legend no longer hard-codes a second meaning."""
        from sova.dashboard.routers.dependencies import _enrich_with_state_display

        graph = {"nodes": [{"id": 1, "state": "triaged"}, {"id": 2, "state": "researched"}]}
        _enrich_with_state_display(graph)
        triaged, researched = graph["nodes"]
        assert (triaged["state_label"], triaged["state_color"]) == ("Triaged", "yellow")
        assert (researched["state_label"], researched["state_color"]) == ("Researched", "purple")

    async def test_graph_pr_label_and_color_come_from_the_same_state(self) -> None:
        """A review_addressed PR reads "Review Addressed" in lavender, as on the Agents page, not the collapsed blue."""
        from sova.dashboard.routers.dependencies import _fetch_pr_map

        prs = [
            {
                "number": 42,
                "url": "u",
                "computed_state": "review_addressed",
                "state_label": "Stale Label",
                "linked_issues": [10],
            },
            {"number": 43, "url": "u", "computed_state": "approved", "linked_issues": [20]},
        ]
        with patch("sova.dashboard.services.pr_service.list_open_prs_with_state", return_value=prs):
            result = await _fetch_pr_map()

        assert result[10]["pr_state"] == "awaiting_review"
        assert result[10]["pr_state_label"] == state_label("review_addressed") == "Review Addressed"
        assert result[10]["pr_state_color"] == state_color("review_addressed") == "lavender"
        assert result[20]["pr_state_label"] == "Approved"
        assert result[20]["pr_state_color"] == state_color("approved")

    async def test_task_issue_list_carries_label_and_color(self) -> None:
        from sova.dashboard.routers import tasks as tasks_router

        adapter = MagicMock()
        adapter.list_tasks = AsyncMock(
            return_value=[
                Task(id="7", title="t", state=TaskState.TRIAGED),
                Task(id="8", title="u", state=TaskState.IN_REVIEW),
            ]
        )
        with (
            patch.dict(tasks_router._issues_cache, clear=True),
            patch("sova.config.loader.load_config"),
            patch("sova.adapters.create_adapter", return_value=adapter),
        ):
            payload = await tasks_router.list_issues()

        triaged, in_review = payload["issues"]
        assert (triaged["state"], triaged["state_label"], triaged["state_color"]) == ("triaged", "Triaged", "yellow")
        assert (in_review["state_label"], in_review["state_color"]) == ("In Review", "green")

    def test_work_item_pr_details_carry_a_color(self) -> None:
        from sova.dashboard.services.work_item_service import _format_pr_details

        details = _format_pr_details({"number": 7, "computed_state": "approved", "state_label": "Approved"})
        assert details["state_color"] == "green"

    def test_work_item_pr_details_without_a_computed_state_show_no_label(self) -> None:
        """ "Unknown" beside a PR with no state would read as a real state; the pre-existing blank is kept."""
        from sova.dashboard.services.work_item_service import _format_pr_details

        details = _format_pr_details({"number": 7})
        assert (details["computed_state"], details["state_label"], details["state_color"]) == ("", "", "")

    def test_work_item_pr_details_label_and_color_share_one_source(self) -> None:
        """A PR dict whose own state_label is missing or stale cannot pair a blank label with a valid color."""
        from sova.dashboard.services.work_item_service import _format_pr_details

        for pr in (
            {"number": 7, "computed_state": "review_addressed"},
            {"number": 7, "computed_state": "review_addressed", "state_label": "Stale"},
        ):
            details = _format_pr_details(pr)
            assert details["state_label"] == "Review Addressed"
            assert details["state_color"] == "lavender"


class TestSharedHelpersHaveOneDefinition:
    @pytest.mark.parametrize(
        "name", ["escapeHtml", "timeAgo", "setText", "renderSectionError", "statusBadge", "formatCpuPercent"]
    )
    def test_defined_once_in_app_js_and_nowhere_else(self, name: str) -> None:
        assert len(re.findall(rf"^function {name}\(", _APP_JS, re.MULTILINE)) == 1
        for path in [*_TEMPLATES.glob("*.html"), *_STATIC.glob("*.js"), *(_STATIC / "js").rglob("*.js")]:
            if path.name == "app.js":
                continue
            source = path.read_text(encoding="utf-8")
            assert not re.search(rf"function {name}\(", source), f"{path.name} redefines {name}"

    def test_esc_alias_is_declared_once_in_app_js(self) -> None:
        assert len(re.findall(r"^var esc = escapeHtml;$", _APP_JS, re.MULTILINE)) == 1
        for path in [*_TEMPLATES.glob("*.html"), *_STATIC.glob("*.js"), *(_STATIC / "js").rglob("*.js")]:
            if path.name == "app.js":
                continue
            source = path.read_text(encoding="utf-8")
            assert not re.search(r"\b(?:const|let|var)\s+esc\b", source), f"{path.name} redeclares esc"
            assert not re.search(r"function\s+esc\b", source), f"{path.name} redefines esc"

    @pytest.mark.parametrize(
        "name", ["esc", "escapeHtml", "timeAgo", "setText", "renderSectionError", "statusBadge", "safeStorageGet"]
    )
    def test_no_script_redeclares_an_app_js_global_lexically(self, name: str) -> None:
        """app.js is a classic script, so its top-level names are globals. A `const`/`let`/`class` of the same
        name in another classic script or inline template script is a redeclaration SyntaxError that stops that
        whole script, and `node --check` looks at one file at a time so it never sees the clash."""
        pattern = re.compile(rf"(?:^|[;{{}}\s])(?:const|let|class)\s+{name}\b")
        for path in [*_TEMPLATES.glob("*.html"), *_STATIC.glob("*.js"), *(_STATIC / "js").rglob("*.js")]:
            if path.name == "app.js":
                continue
            assert not pattern.search(path.read_text(encoding="utf-8")), f"{path.name} redeclares {name}"

    def test_time_ago_reads_a_future_timestamp_as_just_now(self) -> None:
        """Clock skew between server and browser: queue.html's former private copy showed nothing here."""
        _require_node("node is required to run the dashboard helper")
        start = _APP_JS.index("function timeAgo(")
        fn = _APP_JS[start : _APP_JS.index("\n}\n", start) + 2]
        script = (
            fn + "\nconst future = new Date(Date.now() + 60000).toISOString();"
            "\nconsole.log(JSON.stringify([timeAgo(future), timeAgo(future, '-'), timeAgo(null, '-'), timeAgo('x')]));"
        )
        proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30, check=False)
        assert proc.returncode == 0, proc.stderr
        assert json.loads(proc.stdout) == ["just now", "just now", "-", ""]

    def test_status_badge_returns_escaped_html_with_a_neutral_fallback(self) -> None:
        _require_node("node is required to run the dashboard helper")
        start = _APP_JS.index("var STATUS_BADGE_CLASSES")
        end = _APP_JS.index("function setText(")
        script = (
            _APP_JS[
                _APP_JS.index("function escapeHtml(") : _APP_JS.index("\n}\n", _APP_JS.index("function escapeHtml("))
                + 2
            ]
            + _APP_JS[start:end]
            + "\nconsole.log(JSON.stringify([statusBadge('done'), statusBadge('<b>'), statusBadge('x', {x: 'c1'}),"
            " statusBadge('constructor')]));"
        )
        proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30, check=False)
        assert proc.returncode == 0, proc.stderr
        done, unknown, mapped, inherited = json.loads(proc.stdout)
        assert "bg-accent-green/15 text-accent-green" in done
        assert unknown == '<span class="text-xs px-2 py-0.5 rounded bg-gray-700/30 text-gray-400">&lt;b&gt;</span>'
        assert 'rounded c1"' in mapped
        assert "bg-gray-700/30" in inherited, "an Object.prototype name must not pick up an inherited class"

    def test_spec_page_status_setter_is_not_a_second_status_badge(self) -> None:
        spec = (_TEMPLATES / "spec.html").read_text(encoding="utf-8")
        assert "function applySpecStatusBadge(" in spec
        assert "statusBadge(" not in spec.replace("applySpecStatusBadge(", "")

    def test_head_partial_no_longer_defines_escape_html(self) -> None:
        assert "function escapeHtml" not in (_TEMPLATES / "_head.html").read_text(encoding="utf-8")

    def test_every_page_with_the_head_partial_loads_app_js(self) -> None:
        for path in _TEMPLATES.glob("*.html"):
            source = path.read_text(encoding="utf-8")
            if '{% include "_head.html" %}' in source:
                assert "/static/app.js" in source, path.name

    @pytest.mark.parametrize(
        "path", sorted(p for p in _TEMPLATES.glob("*.html") if not p.name.startswith("_")), ids=lambda p: p.name
    )
    def test_no_inline_script_uses_an_app_js_helper_before_app_js_runs(self, path: Path) -> None:
        """escapeHtml and friends live in app.js, which loads last: an inline script ahead of it would
        call an undefined function (a page extending base.html is safe only inside its scripts block).
        Partials are skipped: `_head.html` only references escapeHtml inside a function body that runs
        after every script has loaded."""
        source = path.read_text(encoding="utf-8")
        if "{% extends" in source:
            marker = source.find("{% block scripts %}")
            early = source if marker == -1 else source[:marker]
        else:
            app = source.find("/static/app.js")
            early = source if app == -1 else source[:app]
        for script in re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", early, re.DOTALL):
            assert not _APP_JS_HELPER_CALL.search(script), (
                f"{path.name}: inline script calls an app.js helper too early"
            )

    def test_escape_html_escapes_quotes_and_tolerates_non_strings(self) -> None:
        _require_node("node is required to run the dashboard helper")
        start = _APP_JS.index("function escapeHtml(")
        fn = _APP_JS[start : _APP_JS.index("\n}\n", start) + 2]
        script = fn + '\nconsole.log(JSON.stringify([escapeHtml(`<a href="x">\'`), escapeHtml(0), escapeHtml(null)]));'
        proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30, check=False)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == '["&lt;a href=&quot;x&quot;&gt;&#39;","0",""]'


class TestSupervisorGraphLegend:
    """The graph legend is built from the labels and tokens the API attaches to nodes, not a second palette."""

    _SOURCE = (_TEMPLATES / "supervisor.html").read_text(encoding="utf-8")

    def _legend_block(self) -> str:
        start = self._SOURCE.index("Dependency Graph <span")
        return self._SOURCE[start : self._SOURCE.index('id="hide-done-btn"', start)]

    def test_state_entries_are_rendered_from_node_tokens(self) -> None:
        assert 'id="graph-state-legend"' in self._legend_block()
        assert "function renderStateLegend(" in self._SOURCE
        assert "stateHex(token)" in self._SOURCE
        assert "renderStateLegend(visibleNodes);" in self._SOURCE

    def test_graph_key_includes_the_display_fields_the_renderer_reads(self) -> None:
        """pr_state is collapsed (review_addressed reads awaiting_review, approved reads approved_ci_green),
        but the dot and the drawer draw the raw state's color and label, so the key must change with them."""
        key = self._SOURCE[self._SOURCE.index("function _computeGraphKey(") :]
        key = key[: key.index("\n}\n")]
        for field in ("n.pr_state_color", "n.pr_state_label", "n.state_color"):
            assert field in key, field

    def test_graph_key_changes_when_only_the_raw_pr_state_changes(self) -> None:
        _require_node("node is required to run the graph key")
        start = self._SOURCE.index("function _computeGraphKey(")
        fn = self._SOURCE[start : self._SOURCE.index("\n}\n", start) + 2]
        node = {"id": 1, "state": "in_review", "state_color": "green", "pr_state": "awaiting_review"}
        before = {**node, "pr_state_color": "blue", "pr_state_label": "Awaiting Review"}
        after = {**node, "pr_state_color": "lavender", "pr_state_label": "Review Addressed"}
        script = (
            fn + f"\nconst a = {json.dumps({'nodes': [before], 'edges': []})};"
            f"\nconst b = {json.dumps({'nodes': [after], 'edges': []})};"
            "\nconsole.log(JSON.stringify([_computeGraphKey(a) === _computeGraphKey(b),"
            " _computeGraphKey(a) === _computeGraphKey(a)]));"
        )
        proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30, check=False)
        assert proc.returncode == 0, proc.stderr
        assert json.loads(proc.stdout) == [False, True]

    def test_legend_does_not_hard_code_a_state_meaning(self) -> None:
        """A static "Awaiting" yellow dot is what made an ordinary triaged node read as waiting for approval."""
        assert "Awaiting" not in self._legend_block()

    def test_legend_has_no_hex_color_literal(self) -> None:
        legend = self._legend_block()
        assert not re.search(r"#[0-9a-fA-F]{6}", legend)
        assert 'style="background:#' not in legend

    def test_node_renderer_reads_state_colors_from_the_shared_palette(self) -> None:
        """Human, Interactive, Queued and the run-status dots are drawn in colors the legend also shows, so they
        come from stateHex()/SOVA_COLORS like the legend's. Priority colors and the milestone lane palette are
        not state colors and keep their literals; a state hex anywhere else fails here."""
        state_hexes = ("#f38ba8", "#fab387", "#a6e3a1", "#74c7ec")
        not_state = re.compile(r"^\s*(?:blocker|critical|high|medium|low|undefined):\s*\{ color: |^\s*\{ bg: '#")
        offenders = [
            line.strip()
            for line in self._SOURCE.splitlines()
            if any(h in line.lower() for h in state_hexes) and not not_state.match(line)
        ]
        assert offenders == []

    def test_pulse_keyframes_use_theme_variables(self) -> None:
        keyframes = self._SOURCE[self._SOURCE.index("@keyframes graph-node-pulse") :]
        keyframes = keyframes[: keyframes.index("}\n.graph-node-highlight")]
        assert not re.search(r"#[0-9a-fA-F]{6}", keyframes)


class TestAgentsModuleResponsibilities:
    """Each page module owns one group of behavior, and nothing depends on another module having initialised."""

    def test_log_viewer_holds_only_the_viewer(self) -> None:
        viewer = _module_source("log-viewer")
        for name in ("renderAgentCard", "renderCompletedCard", "executeHandoffAction", "triggerRebase", "stopAgent"):
            assert name not in viewer, name
        for name in ("loadHistory", "renderHistoryTable", "rerunAgent", "enhancePlannerCards"):
            assert name not in viewer, name
        assert "function formatLogLine(" in viewer
        assert "function viewLogs(" in viewer

    @pytest.mark.parametrize(
        ("module", "names"),
        [
            ("agent-cards", ["loadAgents", "renderAgentCard", "renderCompletedCard", "executeHandoffAction"]),
            ("agent-cards", ["triggerRebase", "stopAgent", "resumeFromApproval", "enhancePlannerCards"]),
            ("launch-modal", ["quickStart", "quickStartRole", "rerunAgent", "showLaunchModal"]),
            ("history", ["loadHistory", "renderHistoryTable", "switchTab", "refreshHistoryAfterRun"]),
        ],
    )
    def test_agent_cards_and_history_own_their_functions(self, module: str, names: list[str]) -> None:
        source = _module_source(module)
        for name in names:
            assert re.search(rf"function {name}\(", source), f"{name} is not in {module}.js"

    def test_run_command_lives_with_the_launch_actions_not_the_pr_tracker(self) -> None:
        """Task cards and PR rows both emit run-command; it launches an agent, so it sits beside quickStart."""
        launch = _module_source("launch-modal")
        assert "async function runCommand(" in launch
        assert "'run-command':" in launch
        assert "'run-command-with-merge-blockers':" in launch
        tracker = _module_source("pr-tracker")
        assert "runCommand" not in tracker
        assert "/agents/command" not in tracker

    def test_broken_avatar_handler_is_registered_once_by_the_shared_dispatcher_module(self) -> None:
        """Task rows and PR rows both emit data-hide-on-error, so no single page module may own the listener."""
        owners = [name for name, src in _all_page_sources().items() if "img[data-hide-on-error]" in src]
        assert owners == ["actions"]
        assert "document.addEventListener('error'" in _module_source("actions")

    def test_an_active_filter_does_not_fetch_llm_suggestions(self) -> None:
        """Filtering is a view change; a suggestion is an LLM call per PR and the fetch walks every item."""
        body = _module_source("work-items")
        body = body[body.index("function renderTaskCards()") :]
        body = body[: body.index("\n}\n")]
        filtered = body[body.index("if (hasActiveFilter) {") : body.index("var html = '';")]
        assert "fetchLlmSuggestions" not in filtered
        assert body.count("fetchLlmSuggestions()") == 1

    def test_history_tab_return_refreshes_the_visible_list(self) -> None:
        """One visibility-aware poll refreshes whichever tab is shown, since both tabs share one table; a
        separate visibilitychange handler would refresh it twice."""
        source = _module_source("history")
        assert "visibilityAwarePoll(_refreshHistory, _POLL_MS);" in source
        assert "document.addEventListener('visibilitychange'" not in source
        switch = source[source.index("function switchTab(") :]
        assert "_state.tab = " in switch[: switch.index("\n}\n")]

    def test_pr_stage_states_are_listed_once_and_cover_every_python_pr_state(self) -> None:
        """A PR work item state added in work_state.py must reach the PR-stage filter, sort tier and suggestions."""
        source = _module_source("work-items")
        assert "var _PR_STAGE_STATES = new Set(_STATE_FILTER_GROUPS.pr_stage);" in source
        group = source[source.index("pr_stage: [") :]
        listed = set(re.findall(r"'(pr_\w+)'", group[: group.index("],")]))
        assert listed == {m.value for m in WorkItemState if m.value.startswith("pr_")}

    @pytest.mark.parametrize(
        ("status", "finished"),
        [
            ("done", True),
            ("failed", True),
            ("rejected", True),
            ("interrupted", True),
            ("stopped", True),
            ("paused", False),
            ("awaiting_approval", False),
            ("running", False),
            ("pending", False),
        ],
    )
    def test_log_viewer_finished_runs_come_from_the_shared_terminal_set(self, status: str, finished: bool) -> None:
        """A paused run or one awaiting approval can still write, so it streams; the rest are read from line 0."""
        _require_node("node is required to run the log viewer helper")
        source = _module_source("log-viewer")
        assert "_TERMINAL" not in source.replace("_STATUS_TERMINAL", "")
        terminal = re.search(r"^var _STATUS_TERMINAL = .*;$", _APP_JS, re.MULTILINE)
        assert terminal
        start = source.index("function _isFinishedRun(")
        fn = source[start : source.index("\n}\n", start) + 2]
        proc = subprocess.run(
            ["node", "-e", f"{terminal.group(0)}\n{fn}\nconsole.log(_isFinishedRun({json.dumps(status)}));"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == str(finished).lower()

    def test_rerun_needs_an_issue_to_relaunch(self) -> None:
        cards = _module_source("agent-cards")
        completed = cards[cards.index("function renderCompletedCard(") :]
        completed = completed[: completed.index("\n}\n")]
        assert "var rerunBtn = agent.issue" in completed
        launch = _module_source("launch-modal")
        handler = launch[launch.index("'rerun': function(el, e) {") :]
        assert "if (!issue) return;" in handler[: handler.index("\n    },")]

    def test_rerun_is_registered_by_the_launch_modal_not_by_a_module_that_renders_the_button(self) -> None:
        """History, the kanban, the interrupted-runs banner and the agent cards all emit it; it opens the launch
        modal, so it is registered beside the other launch actions and no card module's init gates it."""
        owners = [name for name, src in _all_page_sources().items() if "'rerun': function" in src]
        assert owners == ["launch-modal"]
        emitters = {
            name
            for name, src in _all_page_sources().items()
            if "actionAttrs('rerun'" in src or 'data-action="rerun"' in src
        }
        assert {"history", "kanban", "live-status", "agent-cards"} <= emitters
        assert "launch-modal.js" not in _module_source("agent-cards")

    def test_pr_tracker_redraws_when_the_github_user_arrives(self) -> None:
        tracker = _module_source("pr-tracker")
        assert "onGithubUserChange(function() {" in tracker
        assert "if (_prFilter === 'mine' && _prTrackerLoaded === 'ok') _renderPrTracker();" in tracker
        render = tracker[tracker.index("function _renderPrTracker()") :]
        render = render[: render.index("\n}\n")]
        assert render.index("!getGithubUser()") < render.index("No open PRs")
        assert "GitHub user not known yet" in render

    def test_interrupted_check_runs_on_load_tab_return_and_socket_open(self) -> None:
        live = _module_source("live-status")
        assert "onOpen: checkInterrupted" in live, "a reconnect can follow a gap long enough to interrupt a run"
        assert "visibilityAwarePoll(checkInterrupted, 30000);" in live
        assert "immediate" not in live

    def test_poll_helper_runs_at_once_and_on_every_tab_return(self) -> None:
        _require_node("node is required to run the poll helper")
        start = _APP_JS.index("function visibilityAwarePoll(")
        fn = _APP_JS[start : _APP_JS.index("\n}\n", start) + 2]
        script = f"""
let listener; let calls = 0;
globalThis.document = {{ hidden: false, addEventListener: (t, f) => {{ listener = f; }} }};
globalThis.setInterval = () => 1; globalThis.clearInterval = () => {{}};
{fn}
visibilityAwarePoll(() => {{ calls++; }}, 1000);
const atStart = calls;
document.hidden = true; listener();
const whileHidden = calls;
document.hidden = false; listener();
console.log(JSON.stringify([atStart, whileHidden, calls]));
"""
        proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30, check=False)
        assert proc.returncode == 0, proc.stderr
        assert json.loads(proc.stdout) == [1, 1, 2]

    def test_secondary_action_without_handler_args_does_not_break_the_grid(self) -> None:
        """renderTaskCard throwing would make loadWorkItems replace every task with "Failed to load tasks"."""
        source = _module_source("work-items")
        assert "var ha = sa.handler_args || {};" in source
        assert "sa.handler_args." not in source

    def test_modules_read_storage_only_through_the_safe_helpers(self) -> None:
        """Both read it while the module evaluates, where a throwing localStorage blanks the whole page."""
        for name in ("work-items", "pr-tracker"):
            assert not re.search(r"\blocalStorage\.", _module_source(name)), name

    def test_dead_code_the_split_carried_over_is_gone(self) -> None:
        assert "_runningIssues" not in _module_source("work-items")
        assert "var prNum" not in _module_source("work-items")
        assert "agent-role" not in _module_source("launch-modal")

    def test_not_started_issues_sort_by_issue_priority_before_state(self) -> None:
        """A low researched issue used to sit above a high triaged one, burying the work that mattered."""
        _require_node("node is required to run the task sort")
        source = _module_source("work-items")
        start = source.index("var _NOT_STARTED_STATES")
        end = source.index("\n}\n", source.index("function _sortWorkItems(")) + 2
        script = f"""
var _STATE_FILTER_GROUPS = {{backlog: ['backlog', 'needs_spec', 'triaged', 'researched']}};
var _PR_STAGE_STATES = new Set(['pr_ci_running']);
function _priorityRankMap() {{ return {{blocker: 0, critical: 1, high: 2, medium: 3, low: 4, undefined: 5}}; }}
{source[start:end]}
const items = [
  {{n: 1, state: 'researched', priority: 0, issue_priority: 'low'}},
  {{n: 2, state: 'human_only', priority: 5, issue_priority: 'high'}},
  {{n: 3, state: 'triaged', priority: 2, issue_priority: 'high'}},
  {{n: 4, state: 'triaged', priority: 2, issue_priority: 'undefined'}},
  {{n: 5, state: 'researched', priority: 0, issue_priority: 'high'}},
  {{n: 6, state: 'pr_ci_running', priority: -2, issue_priority: 'low'}},
  {{n: 7, state: 'agent_running', priority: 2, issue_priority: 'low'}},
];
console.log(JSON.stringify(items.sort(_sortWorkItems).map(i => i.n)));
"""
        proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30, check=False)
        assert proc.returncode == 0, proc.stderr
        assert json.loads(proc.stdout) == [7, 6, 5, 3, 1, 4, 2]

    def test_suggested_tasks_use_the_shared_sort(self) -> None:
        source = _module_source("work-items")
        assert "_workItems.filter(_isNotStarted).sort(_sortSuggested);" in source
        assert "return _sortWorkItems(a, b);" in source, "ties on issue priority fall back to the shared sort"


class TestVisualAuditSkillChecksTheClientPatterns:
    """Both hand-authored copies (Claude Code and Codex) carry the same three mechanical greps."""

    @pytest.mark.parametrize("rel", [".claude/skills/visual-audit/SKILL.md", ".agents/skills/visual-audit/SKILL.md"])
    def test_skill_greps_for_small_text_raw_fetch_and_raw_set_interval(self, rel: str) -> None:
        skill = (DASHBOARD_DIR.parents[1] / rel).read_text(encoding="utf-8")
        assert r"text-\[(9|10|11)px\]" in skill
        assert "--include='*.js'" in skill, "the font-size check must cover JS-rendered markup"
        assert r"-E '\bfetch\('" in skill
        assert "fetchAPI" in skill
        assert "'setInterval('" in skill
        assert "visibilityAwarePoll" in skill


class TestLogViewerResumeAfterHiddenTab:
    def test_a_line_written_during_the_backfill_is_shown_once_and_in_order(self, tmp_path: Path) -> None:
        _require_node("node is required to run the log viewer")
        script = tmp_path / "resume.mjs"
        script.write_text(_LOG_HARNESS_PRELUDE + _LOG_RESUME_SCENARIO, encoding="utf-8")
        proc = subprocess.run(
            ["node", str(script), str(_STATIC)], capture_output=True, text=True, timeout=60, check=False
        )
        assert proc.returncode == 0, proc.stderr
        result = json.loads(proc.stdout)
        assert result["before"] == ["live-1"]
        assert result["closedOnHide"] is True
        assert result["reconnected"] is True
        assert result["backfillBeforeConnected"] == 0, "the backfill must wait for the stream to be subscribed"
        assert result["backfillAfterConnected"] == 1
        assert result["heldWhileInFlight"] == ["live-1"], "lines pushed during the backfill are held, not rendered"
        assert result["afterBackfill"] == ["l1", "l2", "l3", "l4"], "l3 is in the snapshot and the stream: once"
        assert result["afterLive"] == ["l1", "l2", "l3", "l4", "l5"]

    def test_a_poll_in_flight_when_the_tab_is_hidden_does_not_double_the_chain(self, tmp_path: Path) -> None:
        _require_node("node is required to run the log viewer")
        script = tmp_path / "poll.mjs"
        script.write_text(_LOG_HARNESS_PRELUDE + _LOG_POLL_SCENARIO, encoding="utf-8")
        proc = subprocess.run(
            ["node", str(script), str(_STATIC)], capture_output=True, text=True, timeout=60, check=False
        )
        assert proc.returncode == 0, proc.stderr
        result = json.loads(proc.stdout)
        assert result["firstPollInFlight"] == 1
        assert result["secondPollInFlight"] == 2, "returning to the tab restarts polling"
        assert result["afterStalePoll"] == [], "the pre-hide response must be dropped"
        assert result["afterCurrentPoll"] == ["a"], "the line is appended once, not once per chain"

    def _run(self, tmp_path: Path, scenario: str) -> dict:
        _require_node("node is required to run the log viewer")
        script = tmp_path / "scenario.mjs"
        script.write_text(_LOG_HARNESS_PRELUDE + scenario, encoding="utf-8")
        proc = subprocess.run(
            ["node", str(script), str(_STATIC)], capture_output=True, text=True, timeout=60, check=False
        )
        assert proc.returncode == 0, proc.stderr
        return json.loads(proc.stdout)

    def test_identical_repeated_lines_are_not_collapsed_when_the_stream_reports_its_sequence(
        self, tmp_path: Path
    ) -> None:
        """Text matching would drop all three "dot" lines the stream held; the sequence numbers say only two repeat."""
        result = self._run(tmp_path, _LOG_EXACT_OVERLAP_SCENARIO)
        assert result["afterBackfill"] == ["p", "dot", "dot", "dot", "dot"]

    def test_a_run_that_ends_during_the_backfill_still_renders_it(self, tmp_path: Path) -> None:
        result = self._run(tmp_path, _LOG_DONE_DURING_BACKFILL_SCENARIO)
        assert result["closedOnDone"] is True, "no further events are wanted from a finished run"
        assert result["beforeBackfill"] == []
        assert result["afterBackfill"] == ["first", "last"], "the snapshot and the held line must both appear"

    def test_a_failed_backfill_rereads_the_run_instead_of_hiding_the_gap(self, tmp_path: Path) -> None:
        result = self._run(tmp_path, _LOG_BACKFILL_FAILS_SCENARIO)
        assert result["before"] == ["before-hide"]
        assert result["streamClosed"] is True
        assert result["afterFailure"] == [], "stale pre-hide lines and the held line are dropped, not shown as complete"
        assert result["fetchesAfterFailure"] == 2, "polling restarts from line 0"
        assert result["afterPoll"] == ["all-1", "all-2"]

    def test_first_open_shows_the_lines_written_before_the_click(self, tmp_path: Path) -> None:
        result = self._run(tmp_path, _LOG_FIRST_OPEN_SCENARIO)
        assert result["backfillBeforeConnected"] == 0, "the backfill must wait for the stream to be subscribed"
        assert result["backfillAfterConnected"] == 1
        assert result["heldWhileInFlight"] == [], "lines pushed during the backfill are held, not rendered"
        assert result["afterBackfill"] == ["l1", "l2", "l3"], "l3 is in the snapshot and the stream: once"
        assert result["afterLive"] == ["l1", "l2", "l3", "l4"]

    def test_a_dropped_line_abandons_the_stream_for_a_full_reread(self, tmp_path: Path) -> None:
        result = self._run(tmp_path, _LOG_GAP_SCENARIO)
        assert result["backfillsBeforeGap"] == 1
        assert result["streamClosed"] is True, "the stream has a hole the viewer cannot locate"
        assert result["afterStaleSnapshot"] == [], "the abandoned reconcile must not render its snapshot"
        assert result["rereadStarted"] == 2, "polling restarts from line 0"
        assert result["afterReread"] == ["all-1", "all-2", "all-3"]

    def test_a_connected_event_that_never_arrives_falls_back_to_polling(self, tmp_path: Path) -> None:
        result = self._run(tmp_path, _LOG_CONNECT_TIMEOUT_SCENARIO)
        assert result["waitMs"] == 3000
        assert result["fetchesBeforeWait"] == 0, "the backfill still waits for the subscription point"
        assert result["silentStreamClosed"] is True
        assert result["fetchesAfterWait"] == 1, "polling reads the run from line 0"
        assert result["silentRendered"] == ["a", "b"], "the panel is not left empty"

    def test_a_connected_event_in_time_cancels_the_wait_and_keeps_the_stream(self, tmp_path: Path) -> None:
        result = self._run(tmp_path, _LOG_CONNECT_IN_TIME_SCENARIO)
        assert result["waitCancelled"] is True
        assert result["liveStreamClosed"] is False
        assert result["rendered"] == ["a", "b"]

    def test_pr_tracker_never_turns_a_failed_load_into_no_open_prs(self, tmp_path: Path) -> None:
        result = self._run(tmp_path, _PR_TRACKER_USER_ARRIVAL_SCENARIO)
        assert result["beforeAnyLoad"] == "", "a filter click before the first load paints nothing"
        assert "Failed to load open PRs" in result["afterFailedLoad"]
        assert result["afterUserArrivesOverFailure"] == result["afterFailedLoad"]
        assert result["afterFilterClickOverFailure"] == result["afterFailedLoad"]
        assert result["afterRecovery"] is True, "the next good poll renders the list"

    def test_pr_tracker_mine_matches_author_or_assignee_once_in_any_case(self, tmp_path: Path) -> None:
        result = self._run(tmp_path, _PR_TRACKER_MINE_SCENARIO)
        assert result["mine"] == [[1, 1], [2, 1], [3, 1], [4, 1]], "author or assignee, each PR once"

    def test_a_collapsed_pr_group_expands_and_collapses_on_successive_clicks(self, tmp_path: Path) -> None:
        """Collapsed on first render means a class togglePrGroup can clear, not a bare `hidden` attribute."""
        result = self._run(tmp_path, _PR_GROUP_TOGGLE_SCENARIO)
        assert result["rendered"] is True
        assert result["visible"] == [False, True, False, True]

    def test_task_list_follows_work_state_and_reaches_every_item(self, tmp_path: Path) -> None:
        result = self._run(tmp_path, _TASK_BUCKETS_SCENARIO)
        # Active work first (running, PR stage, spec review, in progress, draft PR); an empty, unknown or
        # terminal (merged) state lands here too, on purpose, instead of vanishing.
        assert result["collapsed"][:8] == [1, 2, 4, 3, 5, 12, 13, 14]
        assert result["collapsed"][8:] == [7, 8, 9, 11, 6], "only the first five Suggested items show collapsed"
        assert result["hasShowMore"] is True, "the Suggested block pages instead of capping"
        # Highest issue priority first, regardless of run history: 7 (critical) before 8 (high), then 9 and 11
        # (medium), then the low ones; the old failed run on 6 must not lift it.
        assert result["expanded"][8:] == [7, 8, 9, 11, 6, 10]
        assert sorted(result["expanded"]) == list(range(1, 15)), "All renders every item the API returned"
        assert result["hasShowLess"] is True
        assert result["recollapsed"] == result["collapsed"]
        # 2 is the user's through its PR's assignees alone (issue unassigned, PR authored by someone else).
        assert sorted(result["mine"]) == [1, 2, 8, 9, 10, 12, 14], "Mine lists only the user's items, uncapped"
        assert set(result["mine"]) <= set(result["allExpanded"]), "every item Mine shows is reachable under All"
        assert sorted(result["allExpanded"]) == list(range(1, 15))

    def test_task_grid_is_rewritten_only_when_something_it_renders_changed(self, tmp_path: Path) -> None:
        result = self._run(tmp_path, _TASK_GRID_REWRITE_SCENARIO)
        assert result["writesOnIdenticalReload"] == 0, "an unchanged poll must leave the DOM (and any open menu) alone"
        assert result["writesOnChangedData"] == 1
        assert result["noMatch"] is True
        assert result["restored"] is True
        assert result["noMatchAgain"] is True, "the early-return view must refresh the key, not leave it stale"
        assert result["writesOnFilterChanges"] == 3

    def test_modules_evaluate_when_storage_throws(self, tmp_path: Path) -> None:
        result = self._run(tmp_path, _THROWING_STORAGE_SCENARIO)
        assert result == {"work-items": "evaluated", "pr-tracker": "evaluated"}

    def test_interrupted_banner_loads_on_open_and_tab_return_without_a_socket(self, tmp_path: Path) -> None:
        result = self._run(tmp_path, _INTERRUPTED_WITHOUT_SOCKET_SCENARIO)
        assert result["afterLoad"] == 1
        assert result["afterTabReturn"] == 2

    def test_listeners_hear_the_github_user_once_and_a_throwing_one_does_not_break_the_load(
        self, tmp_path: Path
    ) -> None:
        result = self._run(tmp_path, _GITHUB_USER_SCENARIO)
        assert result["before"] == ""
        assert result["afterFirstLoad"] == ["octocat", 1]
        assert result["callsAfterSecondLoad"] == 1, "an unchanged user must not redraw the PR tracker on every poll"
        assert result["errorsLogged"] == 1

    def test_agent_limit_shows_the_pool_count_and_one_stepper_updated_in_place(self, tmp_path: Path) -> None:
        result = self._run(tmp_path, _AGENT_LIMIT_SCENARIO)
        assert result["running"] == "2", "the running count is agents_running, the number the limit check uses"
        assert result["made"] == [{"key": "max_parallel_agents", "value": 4, "min": 1, "step": 1}]
        assert result["mounted"] == 1
        assert result["runningAfterPoll"] == "1"
        assert result["madeAfterPoll"] == 1, "a poll must update the stepper, not replace it mid-save"
        assert result["setValues"] == [3]

    def test_a_poll_that_overlapped_a_save_does_not_reset_the_limit(self, tmp_path: Path) -> None:
        result = self._run(tmp_path, _AGENT_LIMIT_SCENARIO)
        assert result["runningAfterStalePoll"] == "0", "the running count still updates"
        assert result["setValuesAfterStalePoll"] == [3], "the limit from a response that predates a save is dropped"


class TestRenderIfChanged:
    def _run(self, body: str) -> object:
        _require_node("node is required to run renderIfChanged")
        start = _APP_JS.index("function renderIfChanged(")
        fn = _APP_JS[start : _APP_JS.index("\n}\n", start) + 2]
        script = f"{fn}\nconst c = {{ innerHTML: '' }};\nlet hooks = 0;\n{body}"
        proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30, check=False)
        assert proc.returncode == 0, proc.stderr
        return json.loads(proc.stdout)

    def test_writes_only_when_the_key_changes_and_runs_the_hook_after_each_write(self) -> None:
        result = self._run(
            """
const r1 = renderIfChanged(c, '<a>', 'k1', () => { hooks++; });
c.innerHTML = '<typed>';
const r2 = renderIfChanged(c, '<a>', 'k1', () => { hooks++; });
const r3 = renderIfChanged(c, '<b>', 'k2', () => { hooks++; });
const r4 = renderIfChanged(c, '<c>', 'k3');
console.log(JSON.stringify([r1, r2, r3, r4, c.innerHTML, hooks]));
"""
        )
        assert result == [True, False, True, True, "<c>", 2], "an unchanged key leaves even a foreign edit in place"

    def test_the_key_is_stored_per_container(self) -> None:
        result = self._run(
            """
const other = { innerHTML: '' };
renderIfChanged(c, '<a>', 'k');
console.log(JSON.stringify([renderIfChanged(other, '<a>', 'k'), other.innerHTML]));
"""
        )
        assert result == [True, "<a>"]


class TestInitInteractivePipelineIsIdempotent:
    def test_rewiring_the_same_container_leaves_one_click_handler(self) -> None:
        _require_node("node is required to run initInteractivePipeline")
        start = _APP_JS.index("function initInteractivePipeline(")
        fn = _APP_JS[start : _APP_JS.index("\n}\n", start) + 2]
        script = f"""
const handlers = new Set();
const seg = {{ classList: {{ add() {{}}, remove() {{}} }}, getAttribute: () => 'develop' }};
const container = {{
  addEventListener: (t, h) => handlers.add(h),
  removeEventListener: (t, h) => handlers.delete(h),
  querySelectorAll: () => [seg],
}};
const scrolled = [];
const scrollToStep = (s) => scrolled.push(s);
{fn}
initInteractivePipeline(container);
initInteractivePipeline(container);
initInteractivePipeline(container);
const click = {{ target: {{ closest: () => seg }} }};
handlers.forEach((h) => h(click));
console.log(JSON.stringify({{ handlers: handlers.size, scrolled }}));
"""
        proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30, check=False)
        assert proc.returncode == 0, proc.stderr
        assert json.loads(proc.stdout) == {"handlers": 1, "scrolled": ["develop"]}


class TestLiveRunDetailAndQueueRendering:
    """Source guards for the pages that patch in place instead of rebuilding on every update."""

    _RUN_DETAIL = (_TEMPLATES / "run_detail.html").read_text(encoding="utf-8")
    _QUEUE = (_TEMPLATES / "queue.html").read_text(encoding="utf-8")

    @staticmethod
    def _function(source: str, name: str) -> str:
        start = source.index(f"function {name}(")
        return source[start : source.index("\n}\n", start) + 2]

    def test_the_elapsed_ticker_lives_in_app_js_and_runs_once_at_one_second(self) -> None:
        assert "visibilityAwarePoll(tickElapsedTimers, 1000);" in _APP_JS
        assert "tickElapsedTimers" not in _module_source("live-status").replace("`tickElapsedTimers()` in app.js", "")
        assert "setInterval(" not in self._RUN_DETAIL

    def test_patching_never_rebuilds_the_step_list_that_owns_the_output_stream(self) -> None:
        defined = set(re.findall(r"function (\w+)\(", self._RUN_DETAIL))
        reachable: set[str] = set()
        pending = ["patchRunHeader"]
        while pending:
            name = pending.pop()
            if name in reachable:
                continue
            reachable.add(name)
            # Comments and the inline onclick markup of a row name functions without calling them here.
            body = re.sub(r"//[^\n]*", "", self._function(self._RUN_DETAIL, name))
            pending.extend(n for n in defined if n != name and re.search(rf"(?<![\w'\"]){n}\(", body))
        patch_helpers = {"_syncStepRow", "_stepRowElement", "_insertStepRow", "_renderPipelineBar", "_renderRunInfo"}
        assert patch_helpers <= reachable
        # _refreshRun and _handleRunStatusMessage drive the patch path but reach renderRunDetail only for the one
        # terminal render, so they are checked for a direct call.
        for name in sorted(reachable | {"_refreshRun", "_handleRunStatusMessage", "_checkWaitingRun"}):
            body = self._function(self._RUN_DETAIL, name)
            assert "renderStepList(" not in body, name
            assert "_closeOutputStream(" not in body, name
        assert "renderStepList(steps, run)" in self._function(self._RUN_DETAIL, "renderRunDetail")

    def test_run_detail_rides_the_shared_status_channel_and_resyncs_on_open(self) -> None:
        start = self._function(self._RUN_DETAIL, "_startRunStatusChannel")
        assert "createSovaStatusChannel({" in start
        assert "onOpen: _refreshRun" in start
        assert "onFallbackPoll: _refreshRun" in start
        assert "new WebSocket" not in self._RUN_DETAIL

    def test_only_a_run_seen_in_a_push_can_be_declared_finished_by_its_absence(self) -> None:
        _require_node("node is required to run the run detail push handler")
        handler = self._function(self._RUN_DETAIL, "_handleRunStatusMessage")
        script = f"""
let _RUN_ID = 7, _runSeenInPush = false, _pushedStatus = 'running', _pushedStep = 'develop', refreshes = 0;
let _runUnavailableStreak = 0, _runUnavailableToasted = false;
const _RUN_UNAVAILABLE_THRESHOLD = 3;
const showToast = () => {{}};
const els = {{ cost: {{ textContent: '' }}, duration: {{ innerHTML: 'x' }} }};
globalThis.document = {{
  getElementById: () => els.cost,
  querySelector: () => els.duration,
}};
const liveElapsed = (s) => 'live:' + s;
const _refreshRun = () => {{ refreshes++; }};
const _runRefreshBackoff = () => false;
{handler}
const push = (runs) => _handleRunStatusMessage({{ type: 'status_update', runs }});
const mine = (extra) => Object.assign({{ run_id: 7, status: 'running', current_step: 'develop', cost_usd: '1.5',
  elapsed_seconds: 60 }}, extra);
push([]);
const absentFirst = refreshes;
push([mine()]);
const unchanged = refreshes;
const duration = els.duration.innerHTML;
const cost = els.cost.textContent;
push([mine({{ current_step: 'validate' }})]);
const stepMoved = refreshes;
push([mine({{ current_step: 'validate', elapsed_seconds: null }})]);
const nullElapsed = els.duration.innerHTML;
push([]);
const absentAfterSeen = refreshes;
push([]);
const absentAgain = refreshes;
_handleRunStatusMessage({{ type: 'agent_finished', run_id: 8, status: 'done' }});
const otherFinished = refreshes;
_handleRunStatusMessage({{ type: 'agent_finished', run_id: 7, status: 'done' }});
const mineFinished = refreshes;
_handleRunStatusMessage({{ type: 'agent_finished', run_id: '7', status: 'done' }});
const stringIdFinished = refreshes;
push([mine({{ run_id: '7', current_step: 'assess' }})]);
const stringIdPush = refreshes;
console.log(JSON.stringify({{ stringIdFinished, stringIdPush, absentFirst, unchanged, duration, cost, stepMoved,
  nullElapsed, absentAfterSeen,
  absentAgain, otherFinished, mineFinished }}));
"""
        proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30, check=False)
        assert proc.returncode == 0, proc.stderr
        assert json.loads(proc.stdout) == {
            "absentFirst": 0,
            "unchanged": 0,
            "duration": "live:60",
            "cost": "$1.5000",
            "stepMoved": 1,
            "nullElapsed": "",
            "absentAfterSeen": 2,
            "absentAgain": 2,
            "otherFinished": 2,
            "mineFinished": 3,
            "stringIdFinished": 4,
            "stringIdPush": 5,
        }

    def _run_node(self, script: str, *, module: bool = False) -> object:
        _require_node("node is required to run the run detail helpers")
        argv = ["node", "--input-type=module", "-e", script] if module else ["node", "-e", script]
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=30, check=False)
        assert proc.returncode == 0, proc.stderr
        return json.loads(proc.stdout)

    def test_mark_failed_is_offered_only_for_a_run_the_endpoint_can_still_fail(self) -> None:
        """mark_run_failed() refuses every terminal status, the restartable ones included."""
        apply = self._function(self._RUN_DETAIL, "_applyActionButtons")
        terminal = self._terminal_decl()
        is_live = self._function(self._RUN_DETAIL, "_isLive")
        script = f"""
const _RESTARTABLE = ['failed', 'interrupted', 'paused', 'stopped'];
{terminal}
{is_live}
const btn = (id) => ({{ id, dataset: {{}}, hidden: true,
  classList: {{ toggle(cls, on) {{ btn.last[id] = on; }} }} }});
btn.last = {{}};
const buttons = {{ 'btn-resume': btn('btn-resume'), 'btn-mark-failed': btn('btn-mark-failed') }};
globalThis.document = {{ getElementById: (id) => buttons[id] }};
{apply}
const out = {{}};
for (const status of ['running', 'developing', 'pending', 'failed', 'interrupted', 'paused', 'stopped', 'done']) {{
  _applyActionButtons({{ status, issue_number: 1, role: 'developer', id: 2 }});
  out[status] = {{ markFailedHidden: btn.last['btn-mark-failed'], resumeHidden: btn.last['btn-resume'] }};
}}
console.log(JSON.stringify(out));
"""
        out = self._run_node(script)
        for status in ("running", "developing", "pending"):
            assert out[status] == {"markFailedHidden": False, "resumeHidden": True}, status
        for status in ("failed", "interrupted", "paused", "stopped"):
            assert out[status] == {"markFailedHidden": True, "resumeHidden": False}, status
        assert out["done"] == {"markFailedHidden": True, "resumeHidden": True}

    def test_mark_failed_reports_the_error_body_of_a_run_that_already_finished(self) -> None:
        mark = "async " + self._function(self._RUN_DETAIL, "markFailed").replace("{{ run_id }}", "7")
        script = f"""
const toasts = [];
let reloads = 0;
const sovaConfirm = async () => true;
const apiUrl = (p) => p;
const showToast = (msg, kind) => toasts.push([msg, kind]);
const loadRunDetail = () => {{ reloads++; }};
let refreshes = 0;
const _refreshRun = () => {{ refreshes++; }};
let _runStatusChannel = null;
let body = {{ error: 'Run is already stopped' }};
globalThis.fetch = async () => ({{ ok: true, json: async () => body }});
{mark}
await markFailed();
body = {{ id: 7, status: 'failed' }};
await markFailed();
// A 204 or proxy-rewritten body is not JSON: the mark still went through, so the page still reloads.
globalThis.fetch = async () => ({{ ok: true, json: async () => {{ throw new SyntaxError('no body'); }} }});
await markFailed();
_runStatusChannel = {{}};
await markFailed();
console.log(JSON.stringify({{ toasts, reloads, refreshes }}));
"""
        assert self._run_node(script, module=True) == {
            "toasts": [["Error: Run is already stopped", "error"]],
            "reloads": 3,
            "refreshes": 1,
        }

    def test_live_patch_updates_the_run_info_only_when_the_branch_or_pr_changed(self) -> None:
        render = self._function(self._RUN_DETAIL, "_renderRunInfo")
        script = f"""
const _isLive = (s) => s === 'running';
const escapeHtml = (s) => String(s);
const prLink = (n) => '<a>#' + n + '</a>';
const info = {{
  innerHTML: '', hidden: false, classList: {{ remove() {{ info.hidden = false; }}, add() {{ info.hidden = true; }} }},
  querySelector() {{
    const m = this.innerHTML.match(/data-field="duration">([^<]*)</);
    return m ? {{ innerHTML: m[1] }} : null;
  }},
}};
globalThis.document = {{ getElementById: () => info }};
{render}
const _renderInfoEmpty = () => _renderRunInfo({{ status: 'interrupted', branch_name: null, pr_number: null,
  duration_formatted: '' }}, false);
const run = {{ status: 'running', branch_name: 'feat/x', pr_number: null, duration_formatted: '' }};
_renderRunInfo(run, false);
const initial = info.innerHTML;
info.innerHTML = info.innerHTML.replace('data-field="duration">', 'data-field="duration">3m');
const ticked = info.innerHTML;
_renderRunInfo(run, true);
const unchanged = info.innerHTML === ticked;
_renderRunInfo(Object.assign({{}}, run, {{ pr_number: 42 }}), true);
const withPr = info.innerHTML;
_renderRunInfo({{ status: 'done', branch_name: 'feat/x', pr_number: 42, duration_formatted: '9m' }}, false);
const final = info.innerHTML;
_renderInfoEmpty();
console.log(JSON.stringify({{ initial, unchanged, withPr, final, emptied: info.innerHTML, hidden: info.hidden }}));
"""
        out = self._run_node(script)
        assert "PR:" not in out["initial"]
        assert out["unchanged"] is True
        assert "PR: <a>#42</a>" in out["withPr"]
        assert 'data-field="duration">3m<' in out["withPr"], "a live patch keeps the duration the push ticked in"
        assert 'data-field="duration">9m<' in out["final"], "the full render takes the run's own duration"
        assert out["emptied"] == "", "a terminal render with nothing to show drops the live duration slot"
        assert out["hidden"] is True

    def test_live_patch_adds_a_row_for_a_step_that_started_since_the_render(self) -> None:
        patch_fn = self._function(self._RUN_DETAIL, "patchRunHeader")
        insert = self._function(self._RUN_DETAIL, "_insertStepRow")
        sync = self._function(self._RUN_DETAIL, "_syncStepRow")
        element = self._function(self._RUN_DETAIL, "_stepRowElement")
        script = f"""
const order = [];
const rows = {{}};
const makeRow = (id) => {{
  const row = {{ id, querySelector: () => null,
    after(other) {{ order.splice(order.indexOf(id) + 1, 0, other.id); rows[other.id] = other; }} }};
  return row;
}};
const controls = {{ style: {{ display: 'none' }} }};
const list = {{
  innerHTML: '<p>No steps recorded</p>',
  querySelector: () => (order.length ? rows[order[0]] : null),
  insertBefore(row) {{ order.unshift(row.id); rows[row.id] = row; }},
}};
globalThis.document = {{
  getElementById: (id) => (id === 'step-list' ? list : id === 'step-list-controls' ? controls : rows[id] || null),
  createElement: () => {{
    const tpl = {{ content: {{}} }};
    Object.defineProperty(tpl, 'innerHTML', {{ set(html) {{ tpl.content.firstElementChild = makeRow(html); }} }});
    return tpl;
  }},
}};
const safeStepId = (n) => n;
const _stepRowHtml = (s, isTarget) => 'step-row-' + s.step_name + (isTarget ? ':target' : '');
const _stepStatusClasses = () => ({{ dot: '', text: '' }});
const formatDuration = () => '';
const _setRunStatusAndCost = () => {{}};
const _renderRunInfo = () => {{}};
const _applyActionButtons = () => {{}};
const _renderPipelineBar = () => {{}};
{element}
{insert}
{sync}
{patch_fn}
const run = {{ status: 'running' }};
patchRunHeader(run, [{{ step_name: 'sync', status: 'running' }}]);
const first = {{ order: order.slice(), placeholder: list.innerHTML, controls: controls.style.display }};
patchRunHeader(run, [{{ step_name: 'sync', status: 'done' }}, {{ step_name: 'develop', status: 'running' }}]);
patchRunHeader(run, [{{ step_name: 'sync', status: 'done' }}, {{ step_name: 'assess', status: 'done' }},
  {{ step_name: 'develop', status: 'running' }}]);
console.log(JSON.stringify({{ first, order }}));
"""
        assert self._run_node(script) == {
            "first": {"order": ["step-row-sync"], "placeholder": "", "controls": ""},
            "order": ["step-row-sync", "step-row-assess", "step-row-develop"],
        }

    def test_queue_label_dropdown_skips_unchanged_labels_and_reapplies_the_search(self) -> None:
        populate = self._function(self._QUEUE, "populateFilterDropdowns")
        assert "renderIfChanged(labelItemsContainer, labelHtml," in populate
        assert "activeFilters.labels" in populate[populate.index("renderIfChanged(") :]
        assert "function() { filterLabelDropdown(); }" in populate
        clear = self._function(self._QUEUE, "clearAllFilters")
        assert "labelSearch.value = ''" in clear
        assert "filterLabelDropdown();" in clear[clear.index("populateFilterDropdowns();") :]

    def test_a_refresh_asked_for_mid_fetch_runs_after_it_instead_of_being_dropped(self) -> None:
        _require_node("node is required to run the run detail refresh")
        # _function() starts at "function", so the async keyword is restored for the awaits inside.
        refresh = "async " + self._function(self._RUN_DETAIL, "_refreshRun").replace("{{ run_id }}", "7")
        script = f"""
let _runRefreshing = false, _runRefreshQueued = false, _runSeenInPush = false, _pushedStatus = null;
let _pushedStep = null, _runStatusChannel = {{}};
const _isLive = (s) => s === 'running';
let _runQueuedFromAbsence = false, _runQueuedForce = false, _runRenderSeq = 0, _runRefreshFailures = 0;
let _runRefreshRetryAt = 0;
const _RUN_REFRESH_TOAST_AFTER = 3;
const _runRefreshBackoff = () => false;
const showToast = () => {{}};
const statuses = ['running', 'done'];
const rendered = [];
let fetches = 0;
const apiUrl = (p) => p;
const fetchAPI = async () => {{
  const status = statuses[fetches++];
  await null;
  return {{ run: {{ status }}, steps: [] }};
}};
const patchRunHeader = (run) => {{ rendered.push('patch:' + run.status); }};
const renderRunDetail = (run) => {{ rendered.push('full:' + run.status); _runStatusChannel = null; _runRenderSeq++; }};
{refresh}
const first = _refreshRun();
await _refreshRun();
await first;
await new Promise((r) => setTimeout(r, 0));
console.log(JSON.stringify({{ fetches, rendered }}));
"""
        proc = subprocess.run(
            ["node", "--input-type=module", "-e", script], capture_output=True, text=True, timeout=30, check=False
        )
        assert proc.returncode == 0, proc.stderr
        assert json.loads(proc.stdout) == {"fetches": 2, "rendered": ["patch:running", "full:done"]}

    def _terminal_decl(self) -> str:
        match = re.search(r"var _TERMINAL = \[[^\]]*\];", self._RUN_DETAIL)
        assert match is not None
        return match.group(0)

    def _fn_src(self, source: str, name: str) -> str:
        body = self._function(source, name).replace("{{ run_id }}", "7")
        return ("async " if f"async function {name}(" in source else "") + body

    def test_the_page_terminal_list_matches_the_server_terminal_list(self) -> None:
        """One list decides liveness: a status the server pushes as live must not be terminal here."""
        from sova.core.state import TASK_RUN_TERMINAL

        match = re.search(r"var _TERMINAL = \[([^\]]*)\];", self._RUN_DETAIL)
        assert match is not None
        listed = {item.strip().strip("'") for item in match.group(1).split(",") if item.strip()}
        assert listed == set(TASK_RUN_TERMINAL)
        assert "_NON_TERMINAL" not in self._RUN_DETAIL

    def test_the_run_detail_channel_leaves_the_socket_and_its_watchdog_to_the_shared_hub(self) -> None:
        start = self._function(self._RUN_DETAIL, "_startRunStatusChannel")
        assert "wsPath" not in start
        assert "staleAfterMs" not in start
        # The hub's watchdog is the one liveness check: 20s of silence, tested every 5s (20 to 25s), the same
        # bound the run page had with its own staleAfterMs.
        assert "var _SOCKET_WATCHDOG_MS = 20000;" in _APP_JS
        assert "var _SOCKET_WATCHDOG_TICK_MS = 5000;" in _APP_JS
        assert "}, _SOCKET_WATCHDOG_TICK_MS);" in _APP_JS

    # A stub page for the run_detail script: every element is a permissive stub, so the real render, patch and
    # refresh functions run end to end and only the spies below record what they did.
    _RUN_PAGE = r"""
const makeEl = () => ({ innerHTML: '', textContent: '', className: '', dataset: {}, style: {}, _hidden: null,
  classList: { add() {}, remove() {}, toggle(cls, on) { this.owner._hidden = on; } },
  querySelector: () => null, querySelectorAll: () => [], after() {}, insertBefore() {}, firstChild: null });
const els = {};
const getEl = (id) => {
  if (id.startsWith('step-row-')) return null;
  if (!els[id]) {
    els[id] = makeEl();
    els[id].classList.owner = els[id];
  }
  return els[id];
};
getEl('step-list').querySelector = () => ({});
globalThis.document = { getElementById: getEl, querySelector: () => null,
  createElement: () => ({ content: { firstElementChild: makeEl() } }) };
console.error = (...a) => process.stderr.write(a.map((x) => (x && x.stack) || String(x)).join(' ') + '\n');
__TERMINAL__
const _RESTARTABLE = ['failed', 'interrupted', 'paused', 'stopped'];
let _runStatusChannel = { stop() { stops++; } }, stops = 0;
let _runRefreshing = false, _runRefreshQueued = false, _runQueuedFromAbsence = false, _runSeenInPush = false;
let _runQueuedForce = false, _waitingChecking = false;
const _WAITING = ['paused', 'awaiting_approval'];
let _pushedStatus = null, _pushedStep = null, _runRenderSeq = 0, _runRefreshFailures = 0, _runRefreshRetryAt = 0;
let _RUN_ID = 7;
let _runUnavailableStreak = 0, _runUnavailableToasted = false;
const _RUN_UNAVAILABLE_THRESHOLD = 3;
const _RUN_REFRESH_TOAST_AFTER = 3;
const toasts = [];
const showToast = (m) => toasts.push(m);
const spies = { renderStepList: 0, closeStream: 0 };
const renderStepList = () => { spies.renderStepList++; };
const _closeOutputStream = () => { spies.closeStream++; };
const _startRunStatusChannel = () => {};
const loadResources = () => {};
const loadRunChain = () => {};
const initInteractivePipeline = () => {};
const issueOrPrLink = () => '';
const roleColor = () => ({ bg: '', text: '' });
const formatRole = (r) => r;
const escapeHtml = (s) => String(s);
const statusColor = () => '';
const prLink = (n) => '#' + n;
const liveElapsed = (s) => 'live:' + s;
const formatDuration = () => '';
const safeStepId = (n) => n;
const _stepRowHtml = (s) => s.step_name;
const apiUrl = (p) => p;
const renderStepPipeline = (step) => 'bar:' + step;
const renderIfChanged = (c, html, key, after) => {
  if (c._renderKey === key) return false;
  c._renderKey = key;
  c.innerHTML = html;
  if (after) after(c);
  return true;
};
let nextPayload = null, gate = null, fetches = 0;
const fetchAPI = async () => {
  fetches++;
  const payload = nextPayload;
  if (gate) await gate;
  if (payload instanceof Error) throw payload;
  return payload;
};
const tick = () => new Promise((r) => setTimeout(r, 0));
const run = (status, step) => ({ id: 7, status, current_step: step, role: 'developer', total_cost_usd: '1',
  branch_name: 'feat/x', pr_number: null, duration_formatted: '' });
__FUNCTIONS__
"""

    def _run_page_script(self, body: str) -> str:
        names = (
            "_isLive _renderRunInfo _setRunStatusAndCost _renderPipelineBar _applyActionButtons _stepStatusClasses "
            "_stepRowElement _insertStepRow _syncStepRow patchRunHeader renderRunDetail _stopRunStatusChannel "
            "_runRefreshBackoff _refreshRun _checkWaitingRun "
            "_handleRunStatusMessage"
        ).split()
        funcs = "\n".join(self._fn_src(self._RUN_DETAIL, n) for n in names)
        page = self._RUN_PAGE.replace("__TERMINAL__", self._terminal_decl()).replace("__FUNCTIONS__", funcs)
        return page + body

    def test_a_live_refresh_never_closes_the_output_stream_and_only_the_terminal_one_renders_the_step_list(
        self,
    ) -> None:
        script = self._run_page_script(
            """
nextPayload = { run: run('running', 'develop'), steps: [{ step_name: 'develop', status: 'running' }] };
await _refreshRun();
nextPayload = { run: run('running', 'validate'), steps: [{ step_name: 'develop', status: 'done' },
  { step_name: 'validate', status: 'running' }] };
await _refreshRun();
const live = { ...spies, stops, channel: _runStatusChannel !== null, status: els['detail-status'].textContent };
nextPayload = { run: run('done', 'handoff'), steps: [{ step_name: 'develop', status: 'done' }] };
await _refreshRun();
console.log(JSON.stringify({ live, final: { ...spies, stops, channel: _runStatusChannel !== null } }));
"""
        )
        out = self._run_node(script, module=True)
        assert out["live"] == {"renderStepList": 0, "closeStream": 0, "stops": 0, "channel": True, "status": "running"}
        assert out["final"] == {"renderStepList": 1, "closeStream": 0, "stops": 1, "channel": False}

    def test_a_refresh_that_started_before_a_terminal_render_is_dropped(self) -> None:
        script = self._run_page_script(
            """
let release;
gate = new Promise((r) => { release = r; });
nextPayload = { run: run('running', 'develop'), steps: [] };
const stale = _refreshRun();
renderRunDetail(run('failed', 'develop'), []);
release();
await stale;
console.log(JSON.stringify({ status: els['detail-status'].textContent, markFailedHidden: els['btn-mark-failed']._hidden,
  resumeHidden: els['btn-resume']._hidden, bar: els['pipeline-bar'].innerHTML, channel: _runStatusChannel !== null }));
"""
        )
        out = self._run_node(script, module=True)
        assert out == {
            "status": "failed",
            "markFailedHidden": True,
            "resumeHidden": False,
            "bar": "bar:develop",
            "channel": False,
        }

    def test_refresh_failures_back_off_and_toast_once_and_keep_a_missed_absence_pending(self) -> None:
        script = self._run_page_script(
            """
nextPayload = new Error('500');
_runSeenInPush = true;
const push = (runs) => _handleRunStatusMessage({ type: 'status_update', runs });
push([]);
await tick();
const afterFirst = { fetches, seen: _runSeenInPush, failures: _runRefreshFailures };
push([]);
push([{ run_id: 7, status: 'running', current_step: 'validate', cost_usd: '1', elapsed_seconds: 5 }]);
await tick();
const inBackoff = { fetches, pushed: _pushedStatus };
await _refreshRun();
await _refreshRun(false, true);
const unforced = fetches;
const beforeToast = toasts.length;
for (let i = 0; i < 4; i++) await _refreshRun(false, true);
console.log(JSON.stringify({ afterFirst, inBackoff, unforced, beforeToast, toasts: toasts.length,
  delayMs: _runRefreshRetryAt - Date.now() > 50000 && _runRefreshRetryAt - Date.now() <= 60000 }));
"""
        )
        out = self._run_node(script, module=True)
        assert out["afterFirst"] == {"fetches": 1, "seen": True, "failures": 1}
        assert out["inBackoff"] == {"fetches": 1, "pushed": None}, "a push inside the backoff starts no fetch"
        assert out["unforced"] == 2, (
            "only a forced refresh fetches inside the backoff (the open, poll and tab return wait)"
        )
        assert out["beforeToast"] == 0, "two failures are not yet worth a toast"
        assert out["toasts"] == 1
        assert out["delayMs"] is True, "the wait is capped at 60s"

    def test_a_sustained_status_unavailable_run_refreshes_over_rest_and_toasts_once(self) -> None:
        script = self._run_page_script(
            """
nextPayload = { run: run('running', 'develop'), steps: [] };
const unavailable = () => _handleRunStatusMessage({ type: 'status_unavailable' });
unavailable();
unavailable();
await tick();
const belowThreshold = { fetches, toasts: toasts.length };
unavailable();
await tick();
unavailable();
await tick();
const sustained = { fetches, toasts: toasts.length, streak: _runUnavailableStreak };
_handleRunStatusMessage({ type: 'agent_finished', run_id: 99, status: 'done' });
const afterOtherFinished = _runUnavailableStreak;
_handleRunStatusMessage({ type: 'status_update', runs: [{ run_id: 7, status: 'running', current_step: 'develop',
  cost_usd: '1', elapsed_seconds: 5 }] });
const recovered = { streak: _runUnavailableStreak, toasted: _runUnavailableToasted };
nextPayload = new Error('500');
for (let i = 0; i < 3; i++) unavailable();
await tick();
const failing = fetches;
unavailable();
unavailable();
await tick();
console.log(JSON.stringify({ belowThreshold, sustained, afterOtherFinished, recovered, failing, inBackoff: fetches }));
"""
        )
        out = self._run_node(script, module=True)
        assert out["belowThreshold"] == {"fetches": 0, "toasts": 0}
        assert out["sustained"] == {"fetches": 2, "toasts": 1, "streak": 4}, "one toast, a refresh per further message"
        assert out["afterOtherFinished"] == 4, "an agent_finished mid-outage does not end it"
        assert out["recovered"] == {"streak": 0, "toasted": False}
        assert out["inBackoff"] == out["failing"], "a failing REST refresh backs off instead of retrying on each push"

    def test_a_queued_refresh_is_dropped_when_the_fetch_in_flight_failed(self) -> None:
        script = self._run_page_script(
            """
let release;
gate = new Promise((r) => { release = r; });
nextPayload = new Error('500');
const first = _refreshRun();
const queued = _refreshRun(true);
release();
await first;
await queued;
await tick();
console.log(JSON.stringify({ fetches, seen: _runSeenInPush, failures: _runRefreshFailures }));
"""
        )
        out = self._run_node(script, module=True)
        assert out == {"fetches": 1, "seen": True, "failures": 1}

    def test_a_forced_refresh_queued_behind_a_failing_fetch_still_runs(self) -> None:
        script = self._run_page_script(
            """
let release;
gate = new Promise((r) => { release = r; });
nextPayload = new Error('500');
const first = _refreshRun();
const queued = _refreshRun(false, true);
release();
gate = null;
nextPayload = { run: run('running', 'validate'), steps: [] };
await first;
await queued;
await tick();
console.log(JSON.stringify({ fetches, status: els['detail-status'].textContent, queuedForce: _runQueuedForce }));
"""
        )
        out = self._run_node(script, module=True)
        assert out == {"fetches": 2, "status": "running", "queuedForce": False}

    def test_a_waiting_run_that_goes_live_again_is_picked_up_and_reaches_running(self) -> None:
        script = self._run_page_script(
            """
let started = 0;
globalThis._startRunStatusChannel = () => { started++; };
_runStatusChannel = null;
renderRunDetail(run('awaiting_approval', 'review'), []);
const waiting = { status: els['detail-status'].textContent, pushed: _pushedStatus };
nextPayload = { run: run('awaiting_approval', 'review'), steps: [] };
await _checkWaitingRun();
const unchanged = { fetches, renders: spies.renderStepList };
nextPayload = { run: run('running', 'develop'), steps: [] };
await _checkWaitingRun();
const resumed = { status: els['detail-status'].textContent, pushed: _pushedStatus, renders: spies.renderStepList };
const before = fetches;
await _checkWaitingRun();
console.log(JSON.stringify({ waiting, unchanged, resumed, idleFetches: fetches - before }));
"""
        )
        out = self._run_node(script, module=True)
        assert out["waiting"] == {"status": "awaiting_approval", "pushed": "awaiting_approval"}
        assert out["unchanged"] == {"fetches": 1, "renders": 1}, "an unchanged waiting status costs no render"
        assert out["resumed"] == {"status": "running", "pushed": "running", "renders": 2}
        assert out["idleFetches"] == 0, "once the run is live the waiting check does nothing"

    def test_the_waiting_check_rides_visibility_aware_poll_and_the_waiting_list_is_not_live(self) -> None:
        assert "visibilityAwarePoll(_checkWaitingRun, _WAITING_POLL_MS);" in self._RUN_DETAIL
        match = re.search(r"var _WAITING = \[([^\]]*)\];", self._RUN_DETAIL)
        assert match is not None
        waiting = {item.strip().strip("'") for item in match.group(1).split(",") if item.strip()}
        assert waiting == {"paused", "awaiting_approval"}
        assert waiting <= set(re.findall(r"'(\w+)'", self._terminal_decl()))

    def test_a_live_patch_rebuilds_a_changed_step_row_and_keeps_the_output_embed(self) -> None:
        sync = self._function(self._RUN_DETAIL, "_syncStepRow")
        element = self._function(self._RUN_DETAIL, "_stepRowElement")
        script = f"""
function node(label) {{ return {{ label, parentNode: null, remove() {{ if (this.parentNode) this.parentNode.childNodes =
  this.parentNode.childNodes.filter((n) => n !== this); this.parentNode = null; }} }}; }}
function parent(children) {{
  const p = {{ childNodes: children,
    insertBefore(n, ref) {{ n.parentNode = p; p.childNodes.splice(p.childNodes.indexOf(ref), 0, n); }},
    appendChild(n) {{ n.parentNode = p; p.childNodes.push(n); }} }};
  children.forEach((c) => (c.parentNode = p));
  return p;
}}
const embedWrap = node('embed');
let removed = 0;
const origRemove = embedWrap.remove; embedWrap.remove = () => {{ removed++; origRemove.call(embedWrap); }};
const inner = parent([node('old-metrics'), embedWrap]);
const body = {{ firstElementChild: inner, hasEmbed: false,
  replaceChildren(...nodes) {{ body.replaced = nodes.map((n) => n.label || n); }},
  querySelector: (sel) => (sel === '#inline-output-content' && body.hasEmbed ? {{ parentNode: embedWrap }} : null) }};
const header = {{ innerHTML: 'old header', replaceChildren(...nodes) {{ header.innerHTML = nodes.join(''); }} }};
const row = {{ querySelector: (sel) => (sel === '.sova-step-row-header' ? header : body) }};
let builds = 0;
const _stepRowHtml = (s) => {{ builds++; return s.status; }};
globalThis.document = {{ createElement: () => {{
  const tpl = {{ content: {{}} }};
  Object.defineProperty(tpl, 'innerHTML', {{ set(html) {{
    tpl.content.firstElementChild = {{ querySelector: (sel) => (sel === '.sova-step-row-header'
      ? {{ childNodes: ['header:' + html] }}
      : {{ childNodes: [node('new:' + html)], firstElementChild: {{ childNodes: [node('new:' + html)] }} }}) }};
  }} }});
  return tpl;
}} }};
{element}
{sync}
_syncStepRow(row, {{ step_name: 'develop', status: 'failed', error_message: 'boom' }});
const changed = {{ header: header.innerHTML, builds, replaced: body.replaced }};
_syncStepRow(row, {{ step_name: 'develop', status: 'failed', error_message: 'boom' }});
const unchanged = builds;
body.hasEmbed = true;
_syncStepRow(row, {{ step_name: 'develop', status: 'done' }});
console.log(JSON.stringify({{ changed, unchanged, header: header.innerHTML, replaced: body.replaced,
  children: inner.childNodes.map((n) => n.label), embedRemoved: removed,
  embedStillThere: inner.childNodes.includes(embedWrap) }}));
"""
        out = self._run_node(script)
        assert out["changed"] == {"header": "header:failed", "builds": 1, "replaced": ["new:failed"]}
        assert out["unchanged"] == 1, "a row whose data did not change is left alone"
        assert out["header"] == "header:done"
        assert out["replaced"] == ["new:failed"], "a row holding the embed is not rebuilt through replaceChildren"
        assert out["children"] == ["new:done", "embed"], "the siblings are swapped around the embed"
        assert out["embedRemoved"] == 0, "the embed is never detached, so its scroll position survives"
        assert out["embedStillThere"] is True

    def test_the_first_render_seeds_the_step_row_keys_so_the_first_patch_leaves_rows_alone(self) -> None:
        render = self._function(self._RUN_DETAIL, "renderRunDetail")
        assert "_stepKey = JSON.stringify(s)" in render
        assert render.index("renderStepList(steps, run);") < render.index("_stepKey = JSON.stringify(s)")

    def test_the_output_stream_keeps_its_own_finished_list(self) -> None:
        match = re.search(r"var _OUTPUT_STREAM_DONE = \[([^\]]*)\];", self._RUN_DETAIL)
        assert match is not None
        listed = {item.strip().strip("'") for item in match.group(1).split(",") if item.strip()}
        assert listed == {"done", "failed", "rejected", "interrupted", "stopped"}, "paused runs still stream (#1052)"
        for name in ("tryLoadOutput", "_pollOutput"):
            assert "_TERMINAL" not in self._function(self._RUN_DETAIL, name), name
        visibility = self._RUN_DETAIL[self._RUN_DETAIL.index("addEventListener('visibilitychange'") :]
        assert "_TERMINAL" not in visibility

    def test_a_rendered_live_timer_registers_the_shared_ticker_once(self) -> None:
        _require_node("node is required to run the elapsed ticker")
        parts = []
        for name in ("liveElapsed", "tickElapsedTimers", "startElapsedTicker"):
            start = _APP_JS.index(f"function {name}(")
            parts.append(_APP_JS[start : _APP_JS.index("\n}\n", start) + 2])
        script = f"""
let polls = [];
const visibilityAwarePoll = (fn, ms) => polls.push([fn.name, ms]);
const formatElapsed = (s) => s + 's';
const nodes = [{{ textContent: '', getAttribute: () => String(Date.now() - 5000) }}];
globalThis.document = {{ querySelectorAll: () => nodes }};
var _elapsedTickerStarted = false;
{chr(10).join(parts)}
const none = liveElapsed(null);
const afterNull = polls.length;
const html = liveElapsed(5);
liveElapsed(9);
tickElapsedTimers();
console.log(JSON.stringify({{ none, afterNull, polls, html: html.includes('sova-status-elapsed'),
  tick: nodes[0].textContent }}));
"""
        out = self._run_node(script)
        assert out["none"] == "0s"
        assert out["afterNull"] == 0, "a static zero renders no timer, so it needs no ticker"
        assert out["polls"] == [["tickElapsedTimers", 1000]], "every timer shares the one ticker"
        assert out["html"] is True
        assert out["tick"] == "5s"

    def test_queue_label_dropdown_leaves_an_unchanged_set_alone_and_reapplies_the_search_on_a_rebuild(self) -> None:
        _require_node("node is required to run the queue label dropdown")
        populate = self._function(self._QUEUE, "populateFilterDropdowns")
        filt = self._function(self._QUEUE, "filterLabelDropdown")
        render = self._function(_APP_JS, "renderIfChanged")
        script = f"""
let writes = 0;
const container = {{ _html: '',
  get innerHTML() {{ return this._html; }},
  set innerHTML(v) {{ writes++; this._html = v; }} }};
const search = {{ value: '' }};
const plain = {{ innerHTML: '' }};
const optionEls = () => [...container._html.matchAll(/data-label="([^"]*)"/g)].map((m) => ({{
  style: {{}}, getAttribute: () => m[1] }}));
globalThis.document = {{
  getElementById: (id) => (id === 'label-dropdown-items' ? container : id === 'label-dropdown-search' ? search : plain),
  querySelectorAll: () => (lastOptions = optionEls()),
}};
let lastOptions = [];
const escapeHtml = (s) => String(s);
const stateGroupOrder = [];
let activeFilters = {{ labels: [] }};
let queueData = [{{ state: 's', labels: ['bug', 'docs', 'agent:ready'] }}];
{render}
{filt}
{populate}
populateFilterDropdowns();
const first = writes;
populateFilterDropdowns();
const unchanged = writes === first;
search.value = 'do';
populateFilterDropdowns();
const stillUnchanged = writes === first;
activeFilters = {{ labels: ['bug'] }};
queueData = [{{ state: 's', labels: ['bug', 'docs', 'ui'] }}];
populateFilterDropdowns();
const display = lastOptions.map((o) => o.style.display);
const checkedBug = /value="bug"[^>]*checked/.test(container._html);
console.log(JSON.stringify({{ unchanged, stillUnchanged, rebuilt: writes === first + 1, display, checkedBug }}));
"""
        out = self._run_node(script)
        assert out["unchanged"] is True
        assert out["stillUnchanged"] is True, "typing in the search survives a poll with the same labels"
        assert out["rebuilt"] is True
        assert out["display"] == ["none", "", "none"], "bug and ui are hidden by the search 'do', docs stays"
        assert out["checkedBug"] is True

    def test_task_grid_is_only_ever_written_through_the_helper(self) -> None:
        source = _module_source("work-items")
        assert "getElementById('task-grid').innerHTML" not in source
        assert "grid.innerHTML" not in source


_WORK_ITEMS_TIMEOUT_SCENARIO = r"""
const workItems = await load('work-items');
const grid = { html: '' };
const baseGetElementById = doc.getElementById;
doc.getElementById = (id) => (id === 'task-grid' ? grid : baseGetElementById(id));
globalThis.renderIfChanged = (container, html) => { container.html = html; return true; };
globalThis.getErrorDetail = async () => 'Failed to load tasks';
globalThis.settingSaveCount = () => 0;
const marks = [];
globalThis.markRefresh = (key, ok) => marks.push([key, ok]);
// Fire the 15s abort timer at once; every other timer is left alone.
const realSetTimeout = globalThis.setTimeout;
globalThis.setTimeout = (fn, ms, ...rest) => realSetTimeout(fn, ms === 15000 ? 0 : ms, ...rest);
let calls = 0;
globalThis.fetch = (url, opts) => {
  calls += 1;
  // A request that never answers, until its signal aborts.
  return new Promise((_, reject) => {
    opts.signal.addEventListener('abort', () => reject(Object.assign(new Error('aborted'), { name: 'AbortError' })));
  });
};
console.warn = () => {};
await workItems.loadWorkItems();
out.firstSettled = true;
out.firstMarks = marks.slice();
out.grid = grid.html;
await workItems.loadWorkItems();
out.calls = calls;
process.stdout.write(JSON.stringify(out));
"""

_KANBAN_SINGLE_FLIGHT_SCENARIO = r"""
globalThis.localStorage = {
  getItem: (k) => (k === 'sova-agents-view' ? 'kanban' : null), setItem() {}, removeItem() {},
};
const kanban = await import(pathToFileURL(path.join(dir, 'kanban.js')).href + '?kanban-view');
const subs = [];
const polls = [];
globalThis.subscribeSovaSocket = (options) => { subs.push(options); return { unsubscribe() {} }; };
globalThis.visibilityAwarePoll = (fn, ms) => { polls.push([fn.name, ms]); polls.fn = fn; return { id: null }; };
globalThis.markRefresh = () => {};
globalThis.getErrorDetail = async () => '';
const releases = [];
let fetches = 0;
globalThis.__onFetch = () => { fetches += 1; return new Promise((resolve) => releases.push(resolve)); };
kanban.init();
out.subscription = { keys: Object.keys(subs[0]).sort(), polls: polls.map((p) => p.join(':')) };
const first = polls.fn();
const onMessage = subs[0].onMessage;
onMessage({ type: 'work_items_changed' });
onMessage({ type: 'work_items_changed' });
onMessage({ type: 'work_items_changed' });
await tick();
out.whileInFlight = fetches;
releases[0]({ columns: [], failed: [] });
await tick(); await tick();
out.afterFirst = fetches;
releases[1]({ columns: [], failed: [] });
await first;
out.afterAll = fetches;
process.stdout.write(JSON.stringify(out));
"""


class TestRefreshPaths:
    def _run(self, tmp_path: Path, scenario: str) -> dict:
        _require_node("node is required to run the page modules")
        script = tmp_path / "scenario.mjs"
        script.write_text(_LOG_HARNESS_PRELUDE + scenario, encoding="utf-8")
        proc = subprocess.run(
            ["node", str(script), str(_STATIC)], capture_output=True, text=True, timeout=60, check=False
        )
        assert proc.returncode == 0, proc.stderr
        return json.loads(proc.stdout)

    def test_a_work_items_request_that_never_answers_ends_as_a_failed_refresh_and_does_not_block_the_next(
        self, tmp_path: Path
    ) -> None:
        out = self._run(tmp_path, _WORK_ITEMS_TIMEOUT_SCENARIO)
        assert out["firstMarks"] == [["work-items", False]]
        assert "timed out" in out["grid"]
        assert out["calls"] == 2

    def test_kanban_has_one_refresh_path_and_coalesces_overlapping_triggers_into_one_more_load(
        self, tmp_path: Path
    ) -> None:
        out = self._run(tmp_path, _KANBAN_SINGLE_FLIGHT_SCENARIO)
        # Messages only on the socket (no onOpen or onFallbackPoll: the one 30s timer is the fallback).
        assert out["subscription"] == {"keys": ["onMessage"], "polls": ["loadKanban:30000"]}
        assert out["whileInFlight"] == 1
        assert out["afterFirst"] == 2, "three triggers during a load coalesce into exactly one more"
        assert out["afterAll"] == 2

    def test_the_coderabbit_quota_sync_is_not_run_on_page_load(self) -> None:
        source = _module_source("pr-tracker")
        assert "visibilityAwarePoll(_syncCrQuota, 300000, {skipImmediate: true});" in source
        # The widget reads the cached value (a GET) at once instead.
        assert "  _loadCrQuota();\n" in source

    def test_the_sidebar_and_kanban_do_not_keep_a_residual_poll_for_what_the_socket_carries(self) -> None:
        kanban = _module_source("kanban")
        assert "onFallbackPoll" not in kanban
        assert kanban.count("visibilityAwarePoll(") == 1
        sidebar = re.search(r"function _initSidebarSocket\(\) \{(.*?)\n\}", _APP_JS, re.DOTALL)
        assert sidebar is not None
        assert "onFallbackPoll: _catchUpSidebar" in sidebar.group(1)
        start_fn = _APP_JS.split("function startSidebarPolling")[1].split("function _initSidebarSocket")[0]
        assert "/agents/active" not in start_fn
