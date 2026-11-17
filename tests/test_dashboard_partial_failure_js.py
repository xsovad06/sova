"""Behavior of the two dashboard callers of a partially failed batch (#1212), run under node.

``POST /agents/planner/create-issues`` and ``POST /supervisor/plan/approve`` answer a partial failure with a 502
whose ``detail`` is ``{message, results}``. These tests run the shipped JavaScript (``agent-cards.js`` as an ES
module with its sibling modules stubbed, and ``_approvalFailure()`` cut out of ``supervisor.html``) against a fake
DOM and fetch, so they check what the page does, not how its source is written.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_DASHBOARD = Path(__file__).parent.parent / "sova" / "dashboard"
_CARDS_JS = _DASHBOARD / "static" / "js" / "pages" / "agents" / "agent-cards.js"
_SUPERVISOR_HTML = _DASHBOARD / "templates" / "supervisor.html"
_SETTINGS_WIDGETS_JS = _DASHBOARD / "static" / "js" / "pages" / "settings" / "widgets.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is a dev requirement for dashboard JS tests")

# Drives agent-cards.js with one planner run and the proposals in scenario.tasks. Each entry of scenario.answers is
# the fetch answer for the next POST to create-issues; the runner submits every proposal still drawn, waits for the
# redraw that follows, and reports what was sent, which proposals are drawn afterwards and how often the handoff
# was fetched.
_CARDS_RUNNER = r"""
import { pathToFileURL } from 'node:url';
const [modDir, scenarioJson] = process.argv.slice(2);
const scenario = JSON.parse(scenarioJson);

const nodes = new Map();
function node(id) {
  if (!nodes.has(id)) nodes.set(id, { id, innerHTML: '', classList: { toggle() {}, add() {}, remove() {} } });
  return nodes.get(id);
}
function drawn() {
  return [...node('planner-extra-7').innerHTML.matchAll(/data-idx="(\d+)"/g)].map((m) => m[1]);
}
globalThis.document = {
  getElementById: (id) => (id === 'planner-task-list-7'
    ? { querySelectorAll: () => drawn().map((i) => ({ checked: true, getAttribute: () => i })) }
    : node(id)),
};
globalThis.window = {};
globalThis.apiUrl = (p) => p;
globalThis.roleColor = () => ({ bg: '', text: '' });
globalThis.liveElapsed = () => '1s';
globalThis.issueOrPrLink = () => '';
globalThis.prLink = () => '';
globalThis.escapeHtml = (s) => String(s);
globalThis.formatRole = (r) => r;
globalThis.renderStatusBadge = () => '';
globalThis.priorityIconUrl = () => '';
globalThis.dismissHandoff = () => {};
globalThis.getErrorDetail = async () => 'error';
globalThis.showToast = () => {};
const handoff = {
  issue: 'planner',
  status: 'awaiting_action',
  details: { planned_tasks: scenario.tasks.map((t) => ({ title: t })) },
};
let handoffFetches = 0;
const posts = [];
const reply = (status, body) => ({ ok: status < 400, status, json: async () => body, clone() { return this; } });
const answers = scenario.answers.map((a) => reply(a.status, a.json));
globalThis.fetch = async (url, opts) => {
  if (String(url).startsWith('/handoff')) { handoffFetches++; return reply(200, { handoff }); }
  posts.push(JSON.parse(opts.body).tasks.map((t) => t.title));
  return answers.shift();
};
globalThis.fetchAPI = async () => ({
  agents: [],
  completed: [{ role: 'planner', run_id: 7, status: 'done', completed_seconds_ago: 5 }],
});
const cards = await import(pathToFileURL(`${modDir}/agents/agent-cards.js`).href);
const stubs = await import(pathToFileURL(`${modDir}/agents/actions.js`).href);
cards.init();
const settle = () => new Promise((r) => setTimeout(r, 30));
await settle();
const out = { steps: [] };
for (let i = 0; i < scenario.answers.length; i++) {
  stubs.actions['submit-planner-issues'](null, null, 7, 'planner');
  await settle();
  out.steps.push({ sent: posts[i], drawnAfter: drawn().map(Number) });
}
out.handoffFetches = handoffFetches;
console.log(JSON.stringify(out));
process.exit(0);
"""

_APPROVAL_RUNNER = r"""
const [fnSource, bodyJson] = process.argv.slice(2);
const body = JSON.parse(bodyJson);
globalThis.getErrorDetail = async () => 'generic';
const fn = new Function(`${fnSource}; return _approvalFailure;`)();
const res = { clone() { return this; }, json: async () => body };
console.log(JSON.stringify(await fn(res)));
"""

# Runs createMissingLabels() (cut out of widgets.js) against one fetch answer and reports the toast it shows.
_LABELS_RUNNER = r"""
const [fnSource, bodyJson] = process.argv.slice(2);
const body = JSON.parse(bodyJson);
const toasts = [];
globalThis.apiUrl = (p) => p;
globalThis.jsonCsrfHeaders = () => ({});
globalThis.getErrorDetail = async () => 'generic';
globalThis.showToast = (msg, kind) => toasts.push([msg, kind]);
globalThis.console.error = () => {};
globalThis.fetch = async () => ({ ok: false, status: 502, clone() { return this; }, json: async () => body });
const fn = new Function(`${fnSource}; return async (btn) => {
  globalThis.loadLabelsStatus = () => { globalThis.reloaded = (globalThis.reloaded || 0) + 1; };
  await createMissingLabels(btn);
};`)();
const btn = { disabled: false, textContent: '' };
await fn(btn);
console.log(JSON.stringify({ toasts, reloaded: globalThis.reloaded || 0, disabled: btn.disabled }));
"""


def _run_node(runner: str, args: list[str], cwd: Path) -> dict:
    script = cwd / "runner.mjs"
    script.write_text(runner, encoding="utf-8")
    done = subprocess.run(["node", str(script), *args], capture_output=True, text=True, timeout=60, check=False)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def _write_cards_modules(root: Path) -> None:
    """agent-cards.js as shipped, next to stubs for the three modules it imports."""
    (root / "agents").mkdir(parents=True)
    (root / "work").mkdir()
    (root / "agents" / "agent-cards.js").write_text(_CARDS_JS.read_text(encoding="utf-8"), encoding="utf-8")
    (root / "agents" / "actions.js").write_text(
        "export const actions = {};\n"
        "export function registerActions(map) { Object.assign(actions, map); }\n"
        "export function actionAttrs() { return ''; }\n",
        encoding="utf-8",
    )
    (root / "agents" / "log-viewer.js").write_text("export function updateLogTabs() {}\n", encoding="utf-8")
    (root / "work" / "board.js").write_text("export async function loadBoard() {}\n", encoding="utf-8")


def _results(*oks: bool) -> dict:
    """A create-issues 502 body: one result per submitted task, in request order."""
    return {
        "detail": {
            "message": "Some issues could not be created on the tracker",
            "results": [
                {"ok": ok, "title": f"T{i}", **({"number": 100 + i} if ok else {"error": "Issue creation failed"})}
                for i, ok in enumerate(oks)
            ],
        }
    }


class TestPlannerRetryAfterAPartialFailure:
    """A 502 lists every task in request order; after the redraw a retry must send only the failed tasks."""

    def test_a_retry_after_a_redraw_sends_only_the_tasks_that_failed(self, tmp_path: Path) -> None:
        modules = tmp_path / "modules"
        _write_cards_modules(modules)
        scenario = {
            "tasks": ["A", "B", "C"],
            "answers": [
                {"status": 502, "json": _results(True, False, True)},
                {"status": 502, "json": _results(False)},
            ],
        }

        out = _run_node(_CARDS_RUNNER, [str(modules), json.dumps(scenario)], tmp_path)

        first, second = out["steps"]
        assert first["sent"] == ["A", "B", "C"]
        assert first["drawnAfter"] == [0], "only the failed proposal is still offered"
        assert second["sent"] == ["B"], "the retry never re-submits a created task"
        assert out["handoffFetches"] == 1, "the redraw keeps the trimmed cache instead of refetching the proposals"


class TestSupervisorApprovalFailure:
    """The toast names every failed decision by its issue number, whichever key the result used."""

    @staticmethod
    def _function_source() -> str:
        source = _SUPERVISOR_HTML.read_text(encoding="utf-8")
        match = re.search(r"async function _approvalFailure\(res\) \{.*?\n\}\n", source, re.DOTALL)
        assert match, "_approvalFailure() not found in supervisor.html"
        return match.group(0)

    @pytest.mark.parametrize(
        "failed",
        [
            pytest.param({"ok": False, "issue_number": 43, "error": "Decision failed"}, id="normalized-result"),
        ],
    )
    def test_the_toast_names_the_failed_issue_never_undefined(self, tmp_path: Path, failed: dict) -> None:
        body = {"detail": {"message": "x", "results": [{"ok": True, "issue_number": 42}, failed]}}

        out = _run_node(_APPROVAL_RUNNER, [self._function_source(), json.dumps(body)], tmp_path)

        assert out == {"partial": True, "message": "1 started, 1 failed: #43: Decision failed"}

    def test_a_detail_without_results_is_not_a_partial_failure(self, tmp_path: Path) -> None:
        out = _run_node(_APPROVAL_RUNNER, [self._function_source(), json.dumps({"detail": "nope"})], tmp_path)

        assert out == {"partial": False, "message": "generic"}


class TestLabelCreationPartialFailure:
    @staticmethod
    def _function_source() -> str:
        source = _SETTINGS_WIDGETS_JS.read_text(encoding="utf-8")
        match = re.search(r"async function createMissingLabels\(btn\) \{.*?\n\}\n", source, re.DOTALL)
        assert match, "createMissingLabels() not found in widgets.js"
        return match.group(0)

    def test_the_toast_counts_the_created_and_the_failed_labels(self, tmp_path: Path) -> None:
        body = {"detail": {"message": "x", "created": 3, "errors": ["a: Failed", "b: Failed"], "errors_truncated": 1}}

        out = _run_node(_LABELS_RUNNER, [self._function_source(), json.dumps(body)], tmp_path)

        assert out["toasts"] == [["3 labels created, but 3 failed (see console)", "warning"]]
        assert out["reloaded"] == 1

    def test_an_error_without_results_keeps_the_button_for_a_retry(self, tmp_path: Path) -> None:
        out = _run_node(_LABELS_RUNNER, [self._function_source(), json.dumps({"detail": "nope"})], tmp_path)

        assert out["toasts"] == [["generic", "error"]]
        assert out["disabled"] is False
