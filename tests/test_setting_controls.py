"""Behavior of app.js's shared setting controls: settingSwitch() and settingStepper() (#1461).

Both controls save one key through POST /settings/config, show a pending state while the
request runs, read the stored value back afterwards, and on failure return to the stored
value and surface the server's message. The Agents, Supervisor, Oversight, Dependencies and
Costs pages use them, and the Settings redesign (#1463) reuses them for the same keys.

The tests run the real `static/app.js` under node in a `vm` sandbox with a small fake DOM,
so they exercise the shipped controls rather than a copy of their logic.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

APP_JS = Path(__file__).parent.parent / "sova" / "dashboard" / "static" / "app.js"

# Loads app.js into a sandbox. document.createElement returns small fake elements that keep
# attributes, classes, children and listeners; any other DOM access is absorbed. fetch answers
# POST /settings/config with scenario.post and GET /settings/config with scenario.config, and
# runs scenario.onPost (a snapshot of the control while the save is in flight).
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

class FakeEl {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase();
    this.attrs = {}; this.children = []; this.listeners = {}; this.dataset = {};
    this.className = ''; this.textContent = ''; this.type = ''; this.value = '';
    this.disabled = false; this.checked = false;
    const self = this;
    this.classList = {
      add(...c) {
        const s = new Set(self.className.split(/\s+/).filter(Boolean));
        c.forEach((x) => s.add(x));
        self.className = [...s].join(' ');
      },
      remove(...c) { self.className = self.className.split(/\s+/).filter((x) => x && !c.includes(x)).join(' '); },
      contains(c) { return self.className.split(/\s+/).includes(c); },
      toggle(c, on) { if (on === undefined) on = !this.contains(c); on ? this.add(c) : this.remove(c); return on; },
    };
  }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  removeAttribute(k) { delete this.attrs[k]; }
  hasAttribute(k) { return k in this.attrs; }
  appendChild(c) { this.children.push(c); return c; }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
}

const sent = [];
const toasts = [];
const snapshots = [];
let probe = null;
const ctx = {
  console: { log() {}, warn() {}, error() {} },
  Promise, JSON, Object, Array, Date, Math, Map, Set, URL, Headers, Response, Request, Number, String, isNaN,
  encodeURIComponent, decodeURIComponent,
  setTimeout: (fn) => { return 0; }, clearTimeout() {}, setInterval: () => 0, clearInterval() {},
  requestAnimationFrame() {},
  localStorage: absorb(), sessionStorage: absorb(), navigator: absorb(), location: absorb(),
  EventSource: function () { return absorb(); },
  getComputedStyle: () => ({ getPropertyValue: () => '' }),
  fetch: (input, init) => {
    const url = String(input);
    const method = ((init && init.method) || 'GET').toUpperCase();
    if (url.endsWith('/settings/config') && method === 'POST') {
      sent.push(JSON.parse(init.body));
      if (probe) snapshots.push(probe());
      if (scenario.networkError) return Promise.reject(new TypeError('Failed to fetch'));
      const r = scenario.post;
      const headers = { 'content-type': 'application/json' };
      return Promise.resolve(new Response(r.body, { status: r.status, headers }));
    }
    if (url.endsWith('/settings/config') && method === 'GET') {
      if (ctx.__failNextConfigRead) {
        ctx.__failNextConfigRead = false;
        return Promise.resolve(new Response('{}', { status: 500 }));
      }
      if (scenario.configError) return Promise.resolve(new Response('{}', { status: 500 }));
      return Promise.resolve(new Response(JSON.stringify({ config: scenario.config || {} }), { status: 200 }));
    }
    return new Promise(() => {});  // app.js's own startup polling
  },
};
const doc = absorb();
ctx.document = new Proxy(doc, {
  get(t, p) {
    if (p === 'cookie') return '';
    if (p === 'createElement') return (tag) => new FakeEl(tag);
    return t[p];
  },
});
ctx.window = ctx;
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(appJsPath, 'utf8'), ctx);
ctx.showToast = (message, type) => { toasts.push({ message, type }); return { isConnected: true }; };

async function fire(el, type) {
  if (type === 'change' && el.type === 'checkbox') el.checked = !el.checked;
  const event = { target: el, preventDefault() {}, stopPropagation() {} };
  await Promise.all((el.listeners[type] || []).map((fn) => fn(event)));
  for (let i = 0; i < 5; i++) await new Promise((r) => setImmediate(r));
}
function state(el) {
  const child = (c) => ({
    tag: c.tagName, text: c.textContent, disabled: c.disabled, checked: c.checked, attrs: { ...c.attrs },
  });
  return { className: el.className, attrs: { ...el.attrs }, children: el.children.map(child) };
}

(async () => {
  const errors = [];
  const saved = [];
  const body = 'return (async () => {' + scenario.script + '})();';
  const fn = new Function('ctx', 'fire', 'state', 'setProbe', 'errors', 'saved', body);
  const result = await fn(ctx, fire, state, (p) => { probe = p; }, errors, saved);
  process.stdout.write(JSON.stringify({ result, sent, toasts, snapshots, errors, saved }));
})().catch((e) => { process.stderr.write(String(e && e.stack || e)); process.exit(1); });
"""


def _run(tmp_path: Path, script: str, *, post: dict | None = None, config: dict | None = None, **extra: object) -> dict:
    if shutil.which("node") is None:
        pytest.skip("node is required to run app.js")
    harness = tmp_path / "harness.js"
    harness.write_text(_HARNESS, encoding="utf-8")
    scenario = json.dumps({"script": script, "post": post or _ok({}), "config": config or {}, **extra})
    proc = subprocess.run(
        ["node", str(harness), str(APP_JS), scenario],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _ok(body: dict) -> dict:
    return {"status": 200, "body": json.dumps({"status": "ok", **body})}


def _http_error(status: int, detail: str) -> dict:
    return {"status": status, "body": json.dumps({"detail": detail})}


_SWITCH = """
const el = ctx.settingSwitch('supervisor.auto_triage', {
  label: 'Auto-triage', value: false,
  onError: (m) => errors.push(m), onSaved: (v) => saved.push(v),
});
const input = el.children[0];
setProbe(() => state(el));
await fire(input, 'change');
return state(el);
"""

_STEPPER = """
const el = ctx.settingStepper('max_parallel_agents', {
  label: 'Agent limit', value: VALUE, min: 1, step: 1,
  onError: (m) => errors.push(m), onSaved: (v) => saved.push(v),
});
setProbe(() => state(el));
const before = state(el);
await fire(el.children[CLICK], 'click');
return { before, after: state(el) };
"""


class TestSettingSwitch:
    def test_saves_the_key_and_shows_the_stored_value(self, tmp_path: Path) -> None:
        out = _run(tmp_path, _SWITCH, config={"supervisor.auto_triage": True})

        assert out["sent"] == [{"key": "supervisor.auto_triage", "value": True}]
        assert out["result"]["children"][0]["checked"] is True
        assert out["saved"] == [True]
        assert out["errors"] == []

    def test_is_pending_while_saving_and_not_after(self, tmp_path: Path) -> None:
        out = _run(tmp_path, _SWITCH, config={"supervisor.auto_triage": True})

        during = out["snapshots"][0]
        assert "sova-setting-pending" in during["className"]
        assert during["attrs"].get("aria-busy") == "true"
        assert during["children"][0]["disabled"] is True
        after = out["result"]
        assert "sova-setting-pending" not in after["className"]
        assert after["children"][0]["disabled"] is False

    def test_an_outside_update_during_a_save_is_ignored(self, tmp_path: Path) -> None:
        """A page poll calls sovaSetValue(); while a save is in flight it must not flip the switch back."""
        script = """
const el = ctx.settingSwitch('supervisor.auto_triage', { label: 'Auto-triage', value: false });
setProbe(() => { el.sovaSetValue(false); return state(el); });
await fire(el.children[0], 'change');
const afterSave = state(el);
el.sovaSetValue(false);
return { afterSave, afterPoll: state(el) };
"""
        out = _run(tmp_path, script, config={"supervisor.auto_triage": True})

        assert out["snapshots"][0]["children"][0]["checked"] is True
        assert out["result"]["afterSave"]["children"][0]["checked"] is True
        assert out["result"]["afterPoll"]["children"][0]["checked"] is False, "outside updates apply once idle"

    def test_http_error_with_only_detail_reverts_and_shows_the_message(self, tmp_path: Path) -> None:
        detail = "supervisor.auto_triage: Input should be a valid boolean"
        out = _run(tmp_path, _SWITCH, post=_http_error(422, detail))

        assert out["result"]["children"][0]["checked"] is False
        assert out["errors"] == [detail]
        assert {"message": detail, "type": "error"} in out["toasts"]
        assert out["saved"] == []

    def test_error_in_a_200_answer_reverts_and_shows_the_message(self, tmp_path: Path) -> None:
        out = _run(tmp_path, _SWITCH, post={"status": 200, "body": json.dumps({"error": "Unknown setting"})})

        assert out["result"]["children"][0]["checked"] is False
        assert out["errors"] == ["Unknown setting"]

    def test_network_failure_reverts(self, tmp_path: Path) -> None:
        out = _run(tmp_path, _SWITCH, networkError=True)

        assert out["result"]["children"][0]["checked"] is False
        assert len(out["errors"]) == 1
        assert "supervisor.auto_triage" in out["errors"][0]

    def test_restart_required_is_reported(self, tmp_path: Path) -> None:
        out = _run(tmp_path, _SWITCH, post=_ok({"restart_required": True}), config={"supervisor.auto_triage": True})

        assert any(t["type"] == "warning" and "Restart" in t["message"] for t in out["toasts"])

    def test_no_restart_notice_for_a_live_setting(self, tmp_path: Path) -> None:
        out = _run(tmp_path, _SWITCH, config={"supervisor.auto_triage": True})

        assert not any("Restart" in t["message"] for t in out["toasts"])

    def test_lifecycle_error_is_reported(self, tmp_path: Path) -> None:
        out = _run(
            tmp_path,
            _SWITCH,
            post=_ok({"lifecycle_error": "Config saved but supervisor reload failed"}),
            config={"supervisor.auto_triage": True},
        )

        assert {"message": "Config saved but supervisor reload failed", "type": "warning"} in out["toasts"]

    def test_loads_its_value_when_none_is_given(self, tmp_path: Path) -> None:
        script = """
const el = ctx.settingSwitch('oversight.enabled', { label: 'Oversight' });
for (let i = 0; i < 5; i++) await new Promise((r) => setImmediate(r));
return state(el);
"""
        out = _run(tmp_path, script, config={"oversight.enabled": True})

        assert out["result"]["children"][0]["checked"] is True
        assert out["result"]["children"][0]["disabled"] is False

    def test_a_failed_load_stays_focusable_and_says_why(self, tmp_path: Path) -> None:
        script = """
const el = ctx.settingSwitch('oversight.enabled', { label: 'Oversight' });
for (let i = 0; i < 5; i++) await new Promise((r) => setImmediate(r));
return state(el);
"""
        out = _run(tmp_path, script, configError=True)

        input_ = out["result"]["children"][0]
        assert input_["disabled"] is False, "a disabled input cannot take keyboard focus"
        assert input_["attrs"]["aria-disabled"] == "true"
        assert "could not load oversight.enabled" in input_["attrs"]["aria-label"]
        assert out["result"]["attrs"]["data-tooltip"].startswith("Could not load oversight.enabled")
        assert out["result"]["attrs"]["data-tooltip-pos"] == "left"
        assert "data-tooltip-wide" in out["result"]["attrs"]

    def test_activating_a_switch_that_failed_to_load_retries_the_load(self, tmp_path: Path) -> None:
        script = """
ctx.__failNextConfigRead = true;
const el = ctx.settingSwitch('oversight.enabled', { label: 'Oversight' });
for (let i = 0; i < 5; i++) await new Promise((r) => setImmediate(r));
const failed = state(el);
await fire(el.children[0], 'change');
return { failed, retried: state(el) };
"""
        out = _run(tmp_path, script, config={"oversight.enabled": True})

        assert out["result"]["failed"]["children"][0]["attrs"]["aria-disabled"] == "true"
        retried = out["result"]["retried"]
        assert retried["children"][0]["checked"] is True
        assert "aria-disabled" not in retried["children"][0]["attrs"]
        assert retried["children"][0]["attrs"]["aria-label"] == "Oversight"
        assert "data-tooltip" not in retried["attrs"]
        assert out["sent"] == [], "the retry loads, it does not save"

    def test_a_config_load_error_in_a_200_answer_is_a_failed_load(self, tmp_path: Path) -> None:
        script = """
const el = ctx.settingSwitch('oversight.enabled', { label: 'Oversight' });
for (let i = 0; i < 5; i++) await new Promise((r) => setImmediate(r));
return state(el);
"""
        out = _run(tmp_path, script, config={"_error": "No configuration found"})

        assert out["result"]["children"][0]["attrs"]["aria-disabled"] == "true"
        assert out["result"]["children"][0]["checked"] is False

    def test_a_failed_save_shows_what_a_sibling_reported_meanwhile(self, tmp_path: Path) -> None:
        """Switch B fails its save while switch A saves the same key; B ends on the server's value, not its old one."""
        script = """
const a = ctx.settingSwitch('oversight.enabled', { label: 'Oversight', value: false });
const b = ctx.settingSwitch('oversight.enabled', { label: 'Oversight', value: false });
setProbe(() => { b.sovaSetValue(true); return null; });
await fire(b.children[0], 'change');
return state(b).children[0].checked;
"""
        out = _run(tmp_path, script, post=_http_error(422, "rejected"))

        assert out["result"] is True


class TestSettingStepper:
    def test_plus_saves_the_next_value_and_shows_the_stored_one(self, tmp_path: Path) -> None:
        out = _run(tmp_path, _STEPPER.replace("VALUE", "4").replace("CLICK", "2"), config={"max_parallel_agents": 5})

        assert out["sent"] == [{"key": "max_parallel_agents", "value": 5}]
        assert out["result"]["after"]["children"][1]["text"] == "5"
        assert out["saved"] == [5]

    def test_minus_is_disabled_at_the_minimum_and_does_not_save(self, tmp_path: Path) -> None:
        out = _run(tmp_path, _STEPPER.replace("VALUE", "1").replace("CLICK", "0"))

        assert out["result"]["before"]["children"][0]["disabled"] is True
        assert out["sent"] == []

    def test_buttons_are_disabled_while_saving(self, tmp_path: Path) -> None:
        out = _run(tmp_path, _STEPPER.replace("VALUE", "4").replace("CLICK", "0"), config={"max_parallel_agents": 3})

        during = out["snapshots"][0]
        assert "sova-setting-pending" in during["className"]
        assert during["children"][0]["disabled"] is True
        assert during["children"][2]["disabled"] is True
        assert out["result"]["after"]["children"][2]["disabled"] is False

    def test_failure_returns_to_the_stored_value(self, tmp_path: Path) -> None:
        detail = "max_parallel_agents: Input should be greater than or equal to 1"
        out = _run(tmp_path, _STEPPER.replace("VALUE", "4").replace("CLICK", "2"), post=_http_error(422, detail))

        assert out["result"]["after"]["children"][1]["text"] == "4"
        assert out["errors"] == [detail]

    def test_a_failed_load_makes_plus_a_retry(self, tmp_path: Path) -> None:
        script = """
ctx.__failNextConfigRead = true;
const el = ctx.settingStepper('max_parallel_agents', { label: 'Agent limit', min: 1 });
for (let i = 0; i < 5; i++) await new Promise((r) => setImmediate(r));
const failed = state(el);
await fire(el.children[2], 'click');
return { failed, retried: state(el) };
"""
        out = _run(tmp_path, script, config={"max_parallel_agents": 4})

        failed = out["result"]["failed"]
        assert failed["children"][2]["disabled"] is False
        assert "could not load max_parallel_agents" in failed["children"][2]["attrs"]["aria-label"]
        assert failed["children"][1]["text"] == "--"
        retried = out["result"]["retried"]
        assert retried["children"][1]["text"] == "4"
        assert retried["children"][2]["attrs"]["aria-label"] == "Increase Agent limit"
        assert "data-tooltip" not in retried["attrs"]
        assert out["sent"] == []

    def test_a_failed_save_shows_what_a_sibling_reported_meanwhile(self, tmp_path: Path) -> None:
        script = """
const a = ctx.settingStepper('max_parallel_agents', { label: 'Agent limit', value: 4, min: 1 });
const b = ctx.settingStepper('max_parallel_agents', { label: 'Agent limit', value: 4, min: 1 });
setProbe(() => { b.sovaSetValue(5); return null; });
await fire(b.children[0], 'click');
return state(b).children[1].text;
"""
        out = _run(tmp_path, script, post=_http_error(422, "rejected"))

        assert out["result"] == "5"

    def test_decimal_string_value_with_step_and_format(self, tmp_path: Path) -> None:
        """agent.max_issue_budget arrives as the string '75' (a Decimal) and steps by 5 USD, never below 5."""
        script = """
const el = ctx.settingStepper('agent.max_issue_budget', {
  label: 'Per-issue spending limit', value: '75', min: 5, step: 5, format: (v) => '$' + v,
});
await fire(el.children[0], 'click');
return state(el);
"""
        out = _run(tmp_path, script, config={"agent.max_issue_budget": "70"})

        assert out["sent"] == [{"key": "agent.max_issue_budget", "value": 70}]
        assert out["result"]["children"][1]["text"] == "$70"

    def test_minus_stops_at_the_minimum_for_a_stepped_value(self, tmp_path: Path) -> None:
        script = """
const el = ctx.settingStepper('agent.max_issue_budget', { label: 'Limit', value: '5', min: 5, step: 5 });
await fire(el.children[0], 'click');
return state(el);
"""
        out = _run(tmp_path, script)

        assert out["sent"] == []
        assert out["result"]["children"][0]["disabled"] is True


class TestSharedKeyAndMounting:
    def test_saving_one_control_updates_every_control_of_that_key(self, tmp_path: Path) -> None:
        """A page can show one key twice (the Settings Overview repeats a row); a save updates both."""
        script = """
const a = ctx.settingSwitch('oversight.enabled', { label: 'Oversight', value: false });
const b = ctx.settingSwitch('oversight.enabled', { label: 'Oversight', value: false });
await fire(a.children[0], 'change');
return { a: state(a).children[0].checked, b: state(b).children[0].checked };
"""
        out = _run(tmp_path, script, config={"oversight.enabled": True})

        assert out["result"] == {"a": True, "b": True}

    def test_a_control_no_longer_on_the_page_is_dropped_not_updated(self, tmp_path: Path) -> None:
        """A re-rendered row (the Settings redesign, #1463) leaves its old control detached; a save must neither
        update it nor keep it in the registry, or the registry grows with every re-render."""
        script = """
const a = ctx.settingSwitch('oversight.enabled', { label: 'Oversight', value: false });
const old = ctx.settingSwitch('oversight.enabled', { label: 'Oversight', value: false });
old.isConnected = false;
let oldUpdates = 0;
const realSet = old.sovaSetValue;
old.sovaSetValue = (v) => { oldUpdates++; realSet(v); };
await fire(a.children[0], 'change');
await fire(a.children[0], 'change');
return { oldUpdates, oldChecked: state(old).children[0].checked };
"""
        out = _run(tmp_path, script, config={"oversight.enabled": True})

        assert out["result"] == {"oldUpdates": 0, "oldChecked": False}

    def test_mount_turns_placeholders_into_controls(self, tmp_path: Path) -> None:
        script = """
const ph = (data) => {
  const el = ctx.document.createElement('span');
  Object.assign(el.dataset, data);
  if (data.tooltip) el.setAttribute('data-tooltip', data.tooltip);
  if (data.tooltipPos) el.setAttribute('data-tooltip-pos', data.tooltipPos);
  return el;
};
const sw = ph({ settingSwitch: 'supervisor.auto_review', label: 'Auto-review', tooltip: 'Spawn the reviewer' });
const st = ph({
  settingStepper: 'agent.max_issue_budget', label: 'Per-issue limit', min: '5', step: '5', prefix: '$',
  tooltip: 'Most one issue may cost', tooltipPos: 'bottom',
});
const root = { querySelectorAll: () => [sw, st] };
const made = ctx.mountSettingControls(root, { 'supervisor.auto_review': { value: true } });
const again = ctx.mountSettingControls(root);
for (let i = 0; i < 5; i++) await new Promise((r) => setImmediate(r));
const swCtl = sw.children[0], stCtl = st.children[0];
await fire(stCtl.children[0], 'click');
return {
  keys: Object.keys(made), againKeys: Object.keys(again),
  swClass: swCtl.className, swTooltip: swCtl.attrs['data-tooltip'], swChecked: swCtl.children[0].checked,
  swTooltipPos: swCtl.attrs['data-tooltip-pos'], swTooltipWide: 'data-tooltip-wide' in swCtl.attrs,
  stTooltipPos: stCtl.attrs['data-tooltip-pos'],
  placeholderTooltips: ['data-tooltip', 'data-tooltip-pos'].filter((a) => a in sw.attrs || a in st.attrs),
  stText: stCtl.children[1].textContent, mountedTwice: sw.children.length + st.children.length,
};
"""
        out = _run(tmp_path, script, config={"agent.max_issue_budget": "70", "supervisor.auto_review": True})

        res = out["result"]
        assert res["keys"] == ["supervisor.auto_review", "agent.max_issue_budget"]
        assert res["againKeys"] == [], "a second mount must not add duplicate controls"
        assert res["mountedTwice"] == 2
        assert "sova-toggle" in res["swClass"]
        assert res["swTooltip"] == "Spawn the reviewer"
        # Controls sit at the right edge of their row or header: the tooltip wraps and opens to the left, so its
        # hidden box never widens the page.
        assert res["swTooltipPos"] == "left"
        assert res["swTooltipWide"] is True
        assert res["stTooltipPos"] == "bottom", "data-tooltip-pos on the placeholder overrides the default"
        assert res["placeholderTooltips"] == [], "the tooltip moves onto the control; two would show at once"
        assert res["swChecked"] is True
        # The stepper loaded 70 from config, so minus sends 65; the read-back still answers 70 and is shown.
        assert out["sent"] == [{"key": "agent.max_issue_budget", "value": 65}]
        assert res["stText"] == "$70"
