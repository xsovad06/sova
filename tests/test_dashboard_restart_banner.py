"""Behavior of app.js's restart banner poll and getErrorDetail() status suffix (issue #1409).

Runs the real `static/app.js` under node in a `vm` sandbox with a stubbed DOM and a scripted
`fetch`, so the shipped functions are exercised rather than a copy of their logic.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

APP_JS = Path(__file__).parent.parent / "sova" / "dashboard" / "static" / "app.js"
TEMPLATES = Path(__file__).parent.parent / "sova" / "dashboard" / "templates"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is required to run app.js")

_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const [appJsPath, scenarioJson] = process.argv.slice(1);
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

const banner = {
  hidden: scenario.bannerHidden !== false,
  classList: { toggle(_c, force) { banner.hidden = !!force; } },
};
const detail = { textContent: '' };
const ctx = {
  console: { log() {}, warn() {}, error() {} },
  Promise, JSON, Object, Array, Date, Math, Map, Set, URL, Headers, Response, Request,
  encodeURIComponent, decodeURIComponent,
  setTimeout: () => 0, clearTimeout() {}, setInterval: () => 0, clearInterval() {},
  requestAnimationFrame() {},
  localStorage: absorb(), sessionStorage: absorb(), navigator: absorb(), location: absorb(),
  EventSource: function () { return absorb(); },
  getComputedStyle: () => ({ getPropertyValue: () => '' }),
  fetch: () => new Promise(() => {}),
};
const doc = absorb();
ctx.document = new Proxy(doc, {
  get(t, p) {
    if (p === 'getElementById') {
      return (id) => (id === 'restart-banner' ? banner : id === 'restart-banner-detail' ? detail : absorb());
    }
    return t[p];
  },
});
ctx.window = ctx;
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(appJsPath, 'utf8'), ctx);

(async () => {
  const out = {};
  if (scenario.errorDetail) {
    const { status, body } = scenario.errorDetail;
    const res = new Response(body, { status });
    out.detail = await ctx.getErrorDetail(res, 'Failed to do it');
  }
  if (scenario.health) {
    const { status, body } = scenario.health;
    ctx.fetch = () => Promise.resolve(new Response(body, { status }));
    await ctx._pollRestartRequired();
    out.hidden = banner.hidden;
    out.text = detail.textContent;
  }
  process.stdout.write(JSON.stringify(out));
})();
"""


def _run(scenario: dict) -> dict:
    result = subprocess.run(
        ["node", "-e", _HARNESS, str(APP_JS), json.dumps(scenario)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


class TestGetErrorDetail:
    @pytest.mark.parametrize("body", ["not json", "", "{}", '{"detail": ""}', '{"detail": []}'])
    def test_fallback_gains_the_http_status(self, body: str) -> None:
        out = _run({"errorDetail": {"status": 502, "body": body}})
        assert out["detail"] == "Failed to do it (HTTP 502)"

    def test_a_detail_is_returned_unchanged(self) -> None:
        out = _run({"errorDetail": {"status": 503, "body": json.dumps({"detail": "Restart the server"})}})
        assert out["detail"] == "Restart the server"

    def test_a_structured_detail_message_is_returned_unchanged(self) -> None:
        body = json.dumps({"detail": {"message": "Budget exceeded"}})
        assert _run({"errorDetail": {"status": 402, "body": body}})["detail"] == "Budget exceeded"


class TestRestartBannerPoll:
    def test_shows_when_restart_is_required_and_abbreviates_the_shas(self) -> None:
        body = json.dumps({"restart_required": True, "code_sha": "a" * 40, "checkout_sha": "b" * 40})
        out = _run({"health": {"status": 200, "body": body}})
        assert out["hidden"] is False
        assert "aaaaaaa" in out["text"]
        assert "bbbbbbb" in out["text"]
        assert "a" * 8 not in out["text"]

    def test_hides_when_restart_is_not_required(self) -> None:
        out = _run({"health": {"status": 200, "body": json.dumps({"restart_required": False})}})
        assert out["hidden"] is True

    def test_reads_the_json_body_of_a_503_degraded_response(self) -> None:
        body = json.dumps(
            {"status": "degraded", "restart_required": True, "code_sha": "a" * 40, "checkout_sha": "b" * 40}
        )
        out = _run({"health": {"status": 503, "body": body}})
        assert out["hidden"] is False

    def test_a_null_json_body_hides_the_banner_without_throwing(self) -> None:
        out = _run({"health": {"status": 200, "body": "null"}})
        assert out["hidden"] is True

    @pytest.mark.parametrize("hidden", [True, False])
    def test_a_non_json_body_leaves_the_banner_alone(self, hidden: bool) -> None:
        out = _run({"bannerHidden": hidden, "health": {"status": 502, "body": "<html>bad gateway</html>"}})
        assert out["hidden"] is hidden


class TestBannerTemplates:
    def test_partial_declares_the_elements_the_poll_updates(self) -> None:
        from jinja2 import Environment, FileSystemLoader

        html = Environment(loader=FileSystemLoader(TEMPLATES)).get_template("_restart_banner.html").render()
        assert 'id="restart-banner"' in html
        assert 'id="restart-banner-detail"' in html

    @pytest.mark.parametrize("template", ["base.html", "home.html", "setup.html"])
    def test_every_page_shell_includes_the_banner(self, template: str) -> None:
        # home.html and setup.html do not extend base.html, so each includes the partial itself.
        html = (TEMPLATES / template).read_text(encoding="utf-8")
        assert '{% include "_restart_banner.html" %}' in html
