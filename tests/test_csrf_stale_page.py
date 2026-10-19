"""Behavior of app.js's CSRF fetch() wrapper when the server rejects a stale page.

A tab opened before a dashboard restart or deploy can lack the `sova_csrf` cookie, so
every mutating call comes back 403 with `CSRF_REJECTED_DETAIL`. Callers surface that
detail verbatim ("Failed to start agent: Missing or invalid CSRF token"), which reads
as a server fault when the fix is a page reload. The wrapper rewrites that detail and
offers a reload once per page.

These tests run the real `static/app.js` under node in a `vm` sandbox with a stubbed
DOM, so they exercise the shipped wrapper rather than a copy of its logic.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from sova.dashboard.security import CSRF_REJECTED_DETAIL

APP_JS = Path(__file__).parent.parent / "sova" / "dashboard" / "static" / "app.js"

# Loads app.js into a sandbox whose DOM is an "absorbing" proxy (any property read or
# call returns another proxy), replaces the network fetch with a scripted one, then runs
# the scenario JSON from argv against the patched window.fetch and prints the results.
_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const [appJsPath, scenarioJson] = process.argv.slice(2);
const scenario = JSON.parse(scenarioJson);

function absorb() {
  return new Proxy(function () {}, {
    get(_t, p) {
      if (p === 'then') return undefined;
      if (p === Symbol.toPrimitive) return () => '';
      return absorb();
    },
    apply() { return absorb(); },
    construct() { return absorb(); },
    set() { return true; },
  });
}

const sent = [];
const toasts = [];
let nextReply = null;
const ctx = {
  console: { log() {}, warn() {}, error() {} },
  Promise, JSON, Object, Array, Date, Math, Map, Set, URL, Headers, Response, Request,
  encodeURIComponent, decodeURIComponent,
  setTimeout: () => 0, clearTimeout() {}, setInterval: () => 0, clearInterval() {},
  requestAnimationFrame() {},
  localStorage: absorb(), sessionStorage: absorb(), navigator: absorb(), location: absorb(),
  EventSource: function () { return absorb(); },
  getComputedStyle: () => ({ getPropertyValue: () => '' }),
  fetch: (input, init) => {
    const headers = new Headers((init && init.headers) || {});
    sent.push({ url: String(input), csrf: headers.get('X-CSRF-Token') });
    if (!nextReply) return new Promise(() => {});  // app.js's own startup polling
    const r = nextReply;
    return Promise.resolve(new Response(r.body, { status: r.status, headers: r.headers || {} }));
  },
};
const doc = absorb();
ctx.document = new Proxy(doc, {
  get(t, p) { return p === 'cookie' ? (scenario.cookie || '') : t[p]; },
});
ctx.window = ctx;
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(appJsPath, 'utf8'), ctx);
let lastToast = null;
ctx.showToast = (message, type, duration, action) => {
  toasts.push({ message, type, duration, action: action ? action.label : null });
  lastToast = { isConnected: true };
  return lastToast;
};

(async () => {
  const results = [];
  for (const step of scenario.steps) {
    if (step.dismissToast && lastToast) lastToast.isConnected = false;
    nextReply = step.reply;
    sent.length = 0;
    const res = step.asRequest
      ? await ctx.fetch(new Request('http://dashboard' + step.url, step.init))
      : await ctx.fetch(step.url, step.init || {});
    const text = await res.text();
    results.push({
      status: res.status, body: text, csrf: sent.length ? sent[0].csrf : null,
      headers: Object.fromEntries(res.headers.entries()),
    });
  }
  process.stdout.write(JSON.stringify({ results, toasts }));
})().catch((e) => { process.stderr.write(String(e && e.stack || e)); process.exit(1); });
"""


def _run(tmp_path: Path, steps: list[dict], cookie: str = "") -> dict:
    if shutil.which("node") is None:
        pytest.skip("node is required to run app.js")
    harness = tmp_path / "harness.js"
    harness.write_text(_HARNESS, encoding="utf-8")
    scenario = json.dumps({"cookie": cookie, "steps": steps})
    proc = subprocess.run(
        ["node", str(harness), str(APP_JS), scenario],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _reply(status: int, detail: str | None = None, body: str | None = None) -> dict:
    payload = body if body is not None else json.dumps({"detail": detail})
    return {"status": status, "body": payload, "headers": {"content-type": "application/json"}}


_POST = {"method": "POST", "headers": {"Content-Type": "application/json"}, "body": "{}"}


def test_app_js_matches_server_rejection_detail() -> None:
    """The wrapper keys on the server's exact detail string; a rename on either side must fail here."""
    assert f"var CSRF_REJECTED_DETAIL = '{CSRF_REJECTED_DETAIL}';" in APP_JS.read_text(encoding="utf-8")


def test_csrf_rejection_is_rewritten_into_a_reload_hint(tmp_path: Path) -> None:
    out = _run(tmp_path, [{"url": "/api/agents/start", "init": _POST, "reply": _reply(403, CSRF_REJECTED_DETAIL)}])

    result = out["results"][0]
    assert result["status"] == 403
    detail = json.loads(result["body"])["detail"]
    assert CSRF_REJECTED_DETAIL not in detail
    assert "reload" in detail.lower()
    assert len(out["toasts"]) == 1
    toast = out["toasts"][0]
    assert toast["action"] == "Reload"
    assert toast["duration"] == 0, "the reload hint must stay until acted on, not vanish after 6s"


def test_reload_hint_is_not_stacked_while_visible(tmp_path: Path) -> None:
    step = {"url": "/api/agents/start", "init": _POST, "reply": _reply(403, CSRF_REJECTED_DETAIL)}
    out = _run(tmp_path, [step, step, step])

    assert [r["status"] for r in out["results"]] == [403, 403, 403]
    assert len(out["toasts"]) == 1


def test_reload_hint_returns_after_it_was_dismissed(tmp_path: Path) -> None:
    """Dismissing the toast (or MAX_TOASTS evicting it) must not silence the next rejection."""
    step = {"url": "/api/agents/start", "init": _POST, "reply": _reply(403, CSRF_REJECTED_DETAIL)}
    out = _run(tmp_path, [step, {**step, "dismissToast": True}])

    assert len(out["toasts"]) == 2


def test_rewritten_response_carries_only_a_json_content_type(tmp_path: Path) -> None:
    """The original Content-Length describes the old body; copying it onto the new one would be a lie."""
    reply = _reply(403, CSRF_REJECTED_DETAIL)
    reply["headers"]["content-length"] = "999"
    reply["headers"]["x-original"] = "1"
    out = _run(tmp_path, [{"url": "/api/agents/start", "init": _POST, "reply": reply}])

    assert out["results"][0]["headers"] == {"content-type": "application/json"}


@pytest.mark.parametrize(
    ("reply", "init"),
    [
        pytest.param(_reply(403, "Origin check failed"), _POST, id="other-403-detail"),
        pytest.param(_reply(403, body="<html>Forbidden</html>"), _POST, id="non-json-403"),
        pytest.param(_reply(500, CSRF_REJECTED_DETAIL), _POST, id="not-a-403"),
        pytest.param(_reply(403, CSRF_REJECTED_DETAIL), {"method": "GET"}, id="safe-method"),
    ],
)
def test_unrelated_responses_pass_through_untouched(tmp_path: Path, reply: dict, init: dict) -> None:
    out = _run(tmp_path, [{"url": "/api/x", "init": init, "reply": reply}])

    result = out["results"][0]
    assert (result["status"], result["body"]) == (reply["status"], reply["body"])
    assert out["toasts"] == []


def test_mutating_fetch_still_echoes_the_cookie(tmp_path: Path) -> None:
    """Regression guard for the existing double-submit behavior the rewrite wraps around."""
    out = _run(
        tmp_path,
        [{"url": "/api/agents/start", "init": _POST, "reply": _reply(200, body="{}")}],
        cookie="other=1; sova_csrf=tok%3D1",
    )

    assert out["results"][0]["csrf"] == "tok=1"
    assert out["toasts"] == []


def test_request_object_input_is_guarded_and_rewritten(tmp_path: Path) -> None:
    """fetch(new Request(...)) carries its method on the Request, not in init; both halves must still apply."""
    out = _run(
        tmp_path,
        [{"url": "/api/agents/start", "init": _POST, "asRequest": True, "reply": _reply(403, CSRF_REJECTED_DETAIL)}],
        cookie="sova_csrf=tok",
    )

    assert out["results"][0]["csrf"] == "tok"
    assert "reload" in json.loads(out["results"][0]["body"])["detail"].lower()
    assert len(out["toasts"]) == 1
