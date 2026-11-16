"""static/reconnect-flow.js: the shared driver for the claude, gh and google logins (issue #1121).

Runs the real helper in a node `vm` sandbox with a scripted fetch and a manual timer queue.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

_HELPER_JS = Path(__file__).resolve().parent.parent / "sova" / "dashboard" / "static" / "reconnect-flow.js"

_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const scenario = JSON.parse(process.argv[3]);
const timers = [];
const states = [];
const toasts = [];
let completed = 0;
const posts = [];
const statuses = scenario.statuses.slice();
const sandbox = {
  apiUrl: (p) => p,
  csrfHeaders: () => ({}),
  getErrorDetail: async () => 'boom',
  showToast: (m, t) => toasts.push([m, t]),
  fetchAPI: async () => {
    const next = statuses.length > 1 ? statuses.shift() : statuses[0];
    if (next.__error) throw new Error(next.__error);
    return next;
  },
  fetch: async (url) => { posts.push(url); return {ok: true, json: async () => scenario.startResponse}; },
  setTimeout: (fn) => { timers.push(fn); return timers.length; },
  clearTimeout: () => {},
  Math,
};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), sandbox);
(async () => {
  const flow = vm.runInContext('createReconnectFlow', sandbox)({
    flow: scenario.flow,
    successMessage: 'Done',
    onState: (s) => states.push(s),
    onCompleted: () => { completed += 1; },
  });
  await flow.start();
  for (let i = 0; i < scenario.ticks; i++) { const fn = timers.shift(); if (fn) await fn(); }
  process.stdout.write(JSON.stringify({states, toasts, completed, posts}));
})();
"""


def _run(tmp_path: Path, scenario: dict) -> dict:
    if shutil.which("node") is None:
        if os.environ.get("CI"):
            pytest.fail("node is required in CI to run reconnect-flow.js")
        pytest.skip("node is required to run reconnect-flow.js")
    harness = tmp_path / "harness.js"
    harness.write_text(_HARNESS, encoding="utf-8")
    proc = subprocess.run(
        ["node", str(harness), str(_HELPER_JS), json.dumps(scenario)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _scenario(flow: str, statuses: list[dict], ticks: int = 1, start: dict | None = None) -> dict:
    return {"flow": flow, "statuses": statuses, "ticks": ticks, "startResponse": start or {"status": "ok"}}


def test_claude_running_text_is_unchanged(tmp_path: Path) -> None:
    out = _run(tmp_path, _scenario("claude", [{"status": "running", "elapsed_seconds": 3.2}]))
    assert out["posts"] == ["/connections/reconnect/claude/start"]
    assert out["states"][-1] == {"phase": "running", "text": "Waiting for browser login... (3s)", "code": ""}


def test_code_is_shown_once_it_appears_and_never_as_an_empty_code(tmp_path: Path) -> None:
    running = {"status": "running", "elapsed_seconds": 1}
    out = _run(
        tmp_path,
        _scenario("gh", [{**running, "code": ""}, {**running, "code": "ABCD-1234"}], ticks=2),
    )
    waiting, with_code = out["states"][-2], out["states"][-1]
    assert "browser window opened on the server machine" in waiting["text"]
    assert waiting["code"] == ""
    assert with_code["code"] == "ABCD-1234"


def test_completed_toasts_once_and_reloads_the_caller(tmp_path: Path) -> None:
    out = _run(tmp_path, _scenario("gh", [{"status": "completed", "code": "", "elapsed_seconds": 9}]))
    assert out["states"][-1]["phase"] == "completed"
    assert out["toasts"] == [["Done", "success"]]
    assert out["completed"] == 1


@pytest.mark.parametrize("status", ["failed", "timeout"])
def test_a_terminal_failure_reports_the_server_detail(tmp_path: Path, status: str) -> None:
    out = _run(tmp_path, _scenario("google", [{"status": status, "detail": "why", "elapsed_seconds": 9}]))
    assert out["states"][-1] == {"phase": status, "text": "why", "code": ""}


def test_a_rejected_start_reports_the_reason_text_and_never_polls(tmp_path: Path) -> None:
    rejected = {"status": "error", "reason": "credentials_missing", "detail": "google_credentials.json not found"}
    out = _run(tmp_path, _scenario("google", [{"status": "idle"}], ticks=1, start=rejected))
    assert out["states"] == [{"phase": "failed", "text": "google_credentials.json not found", "code": ""}]


def test_a_failed_poll_keeps_the_code_visible_and_the_row_recovers(tmp_path: Path) -> None:
    running = {"status": "running", "elapsed_seconds": 1, "code": "ABCD-1234"}
    out = _run(
        tmp_path,
        _scenario(
            "gh", [running, {"__error": "net down"}, {"status": "completed", "code": "", "elapsed_seconds": 9}], ticks=3
        ),
    )
    errored = [s for s in out["states"] if "Failed to check" in s["text"]]
    assert errored and errored[0]["code"] == "ABCD-1234"
    assert out["states"][-1]["phase"] == "completed"
    assert out["completed"] == 1


def test_polling_gives_up_after_repeated_failures(tmp_path: Path) -> None:
    out = _run(tmp_path, _scenario("gh", [{"__error": "net down"}], ticks=20))
    errored = [s for s in out["states"] if "Failed to check" in s["text"]]
    assert len(errored) == 4
    # Giving up leaves a non-running phase, so the control offers Start again.
    assert out["states"][-1]["phase"] == "failed"
    assert "Lost contact with the server" in out["states"][-1]["text"]


_STOP_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const timers = [];
const states = [];
const toasts = [];
let completed = 0;
let release = null;
const sandbox = {
  apiUrl: (p) => p,
  csrfHeaders: () => ({}),
  getErrorDetail: async () => 'boom',
  showToast: (m, t) => toasts.push([m, t]),
  fetchAPI: () => new Promise((resolve) => { release = resolve; }),
  fetch: async () => ({ok: true, json: async () => ({status: 'ok'})}),
  setTimeout: (fn) => { timers.push(fn); return timers.length; },
  clearTimeout: () => {},
  Math,
};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), sandbox);
(async () => {
  const flow = vm.runInContext('createReconnectFlow', sandbox)({
    flow: 'gh',
    successMessage: 'Done',
    onState: (s) => states.push(s),
    onCompleted: () => { completed += 1; },
  });
  await flow.start();
  const tick = timers.shift();
  const pending = tick();
  const statesBefore = states.length;
  flow.stop();
  release(JSON.parse(process.argv[3]));
  await pending;
  process.stdout.write(JSON.stringify({
    newStates: states.length - statesBefore, timers: timers.length, toasts, completed,
  }));
})();
"""


@pytest.mark.parametrize(
    "response",
    [
        {"status": "running", "elapsed_seconds": 1, "code": "ABCD-1234"},
        {"status": "completed", "code": "", "elapsed_seconds": 9},
        {"status": "failed", "detail": "why", "elapsed_seconds": 9},
    ],
)
def test_stop_while_a_status_fetch_is_pending_leaves_the_runner_silent(tmp_path: Path, response: dict) -> None:
    if shutil.which("node") is None:
        if os.environ.get("CI"):
            pytest.fail("node is required in CI to run reconnect-flow.js")
        pytest.skip("node is required to run reconnect-flow.js")
    harness = tmp_path / "stop_harness.js"
    harness.write_text(_STOP_HARNESS, encoding="utf-8")
    proc = subprocess.run(
        ["node", str(harness), str(_HELPER_JS), json.dumps(response)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out == {"newStates": 0, "timers": 0, "toasts": [], "completed": 0}


_SIGN_IN_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const scenario = JSON.parse(process.argv[3]);
const created = [];
const slots = [];
const el = {
  dataset: {},
  set innerHTML(html) {
    this._html = html;
    this.rows = [...html.matchAll(/data-signin-flow="(\w+)"/g)].map((m) => {
      const slot = {textContent: '', innerHTML: ''};
      slots.push(slot);
      return {dataset: {signinFlow: m[1]}, querySelector: () => slot};
    });
  },
  get innerHTML() { return this._html; },
  querySelectorAll(sel) { return sel === '[data-signin-flow]' ? this.rows : []; },
};
const sandbox = {
  console,
  document: {querySelector: () => null},
  apiUrl: (p) => p,
  escapeHtml: (t) => String(t),
  fetchAPI: async (url) => {
    if (url.endsWith('/health/checks')) return {checks: scenario.checks};
    if (scenario.availabilityError) throw new Error('500');
    return {flows: scenario.flows};
  },
  createReconnectFlow: (opts) => { created.push(opts.flow); return {adopt() {}, stop() {}}; },
};
vm.createContext(sandbox);
const source = fs.readFileSync(process.argv[2], 'utf8').replace(/^export /gm, '');
vm.runInContext(source + '\nthis.loadHealthChecks = loadHealthChecks;', sandbox);
(async () => {
  await sandbox.loadHealthChecks(el);
  await new Promise((r) => setImmediate(r));
  process.stdout.write(JSON.stringify({
    created,
    slots: slots.map((s) => ({text: s.textContent, button: s.innerHTML.includes('data-act="signin-start"')})),
  }));
})();
"""

_WIDGETS_JS = _HELPER_JS.parent / "js" / "pages" / "settings" / "widgets.js"


def _run_sign_in(tmp_path: Path, scenario: dict) -> dict:
    if shutil.which("node") is None:
        if os.environ.get("CI"):
            pytest.fail("node is required in CI to run widgets.js")
        pytest.skip("node is required to run widgets.js")
    harness = tmp_path / "sign_in_harness.js"
    harness.write_text(_SIGN_IN_HARNESS, encoding="utf-8")
    proc = subprocess.run(
        ["node", str(harness), str(_WIDGETS_JS), json.dumps(scenario)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _check(name: str, passed: bool = False) -> dict:
    return {"name": name, "passed": passed, "required": False, "detail": ""}


_CAN = {"can_reconnect": True, "reason": "", "detail": ""}


def test_sign_in_is_offered_once_per_flow_across_gmail_and_gcal(tmp_path: Path) -> None:
    out = _run_sign_in(
        tmp_path,
        {
            "checks": [_check("gh authenticated"), _check("awareness: gmail"), _check("awareness: gcal")],
            "flows": {"gh": _CAN, "google": _CAN},
        },
    )
    assert sorted(out["created"]) == ["gh", "google"]
    assert [slot["button"] for slot in out["slots"]] == [True, True, False]


def test_a_passing_row_gets_no_control(tmp_path: Path) -> None:
    out = _run_sign_in(tmp_path, {"checks": [_check("gh authenticated", passed=True)], "flows": {"gh": _CAN}})
    assert out["created"] == [] and out["slots"] == []


@pytest.mark.parametrize(
    ("reason", "shown"),
    [
        ("cli_missing", False),
        ("not_configured", False),
        ("credentials_missing", True),
        ("env_token", True),
        ("config_error", True),
    ],
)
def test_an_unavailable_flow_shows_its_detail_only_for_non_silent_reasons(
    tmp_path: Path, reason: str, shown: bool
) -> None:
    flows = {"google": {"can_reconnect": False, "reason": reason, "detail": "why not"}}
    out = _run_sign_in(tmp_path, {"checks": [_check("awareness: gmail")], "flows": flows})
    assert out["created"] == []
    assert out["slots"] == [{"text": "why not" if shown else "", "button": False}]


def test_an_availability_error_renders_no_control(tmp_path: Path) -> None:
    out = _run_sign_in(tmp_path, {"checks": [_check("gh authenticated")], "flows": {}, "availabilityError": True})
    assert out["created"] == []
    assert out["slots"] == [{"text": "", "button": False}]


def test_an_already_running_session_is_adopted_instead_of_reported_as_failed(tmp_path: Path) -> None:
    rejected = {"status": "error", "reason": "already_in_progress", "detail": "A reconnect is already in progress"}
    running = {"status": "running", "elapsed_seconds": 4, "code": "ABCD-1234"}
    out = _run(tmp_path, _scenario("gh", [running], ticks=1, start=rejected))
    assert all(s["phase"] != "failed" for s in out["states"])
    assert out["states"][0] == {
        "phase": "running",
        "text": "Enter this one-time code on the page that opened in the browser. (4s)",
        "code": "ABCD-1234",
    }


def test_a_rejected_start_settles_on_a_session_that_already_finished(tmp_path: Path) -> None:
    rejected = {"status": "error", "reason": "already_in_progress", "detail": "A reconnect is already in progress"}
    done = {"status": "completed", "code": "", "elapsed_seconds": 9}
    out = _run(tmp_path, _scenario("gh", [done], ticks=1, start=rejected))
    assert out["states"][-1]["phase"] == "completed"
    assert out["toasts"] == [["Done", "success"]]
    assert out["completed"] == 1
