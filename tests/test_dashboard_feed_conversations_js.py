"""The feed panel's conversation tabs (``static/js/feed/conversations.js``, issue #1544), run under node against stub
globals: the tab strip, a conversation's messages, the shared status socket and streamed deltas. A missing `node`
skips locally but fails under CI (the same rule as `test_dashboard_feed_activity_js.py`)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.conftest import DASHBOARD_DIR

_MODULE = DASHBOARD_DIR / "static" / "js" / "feed" / "conversations.js"

_HARNESS = r"""
import { pathToFileURL } from 'node:url';

const out = {};
const sockets = [];
const fetches = [];
const elements = {};
function el(id) {
  const listeners = {};
  return { id, hidden: false, innerHTML: '', value: '', placeholder: '', scrollTop: 0, scrollHeight: 0, clientHeight: 0,
           disabled: false, listeners, attrs: {}, addEventListener(t, fn) { listeners[t] = fn; }, focus() {},
           setAttribute(k, v) { this.attrs[k] = v; } };
}
for (const id of ['feed-panel', 'feed-input-form', 'feed-tabs', 'feed-list', 'feed-conversation', 'feed-past', 'feed-input']) {
  elements[id] = el(id);
}
const g = globalThis;
g.window = { SOVA_PROJECT_SLUG: '' };
g.document = { getElementById: (id) => elements[id] || null };
g.apiUrl = (p) => '/api' + p;
g.escapeHtml = (s) => String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
g.sovaBtn = (label, opts) => '<button' + Object.entries(opts.attrs || {}).map(([k, v]) => ' ' + k + '="' + v + '"').join('') + '>' + label + '</button>';
g.safeStorageGet = () => null;
g.safeStorageSet = () => {};
g.showToast = () => {};
g.sovaConfirm = async () => true;
g.subscribeSovaSocket = (opts) => { sockets.push(opts); return { unsubscribe() {} }; };
const conversation = {
  id: 7, title: 'Why <b>failed</b>?', issue_number: null, status: 'open', running: true, total_cost_usd: '0',
  messages: [
    { id: 1, conversation_id: 7, role: 'user', content: 'Why <b>failed</b>?', tools: [], status: 'done', cost_usd: '0' },
    { id: 2, conversation_id: 7, role: 'assistant', content: '', tools: [], status: 'running', cost_usd: '0' },
  ],
};
g.fetch = (url, opts) => {
  fetches.push([url, opts && opts.method]);
  const body = url === '/api/conversations'
    ? { open: [{ ...conversation, messages: undefined }], past: [], running: 1 }
    : JSON.parse(JSON.stringify(conversation));
  return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body) });
};

const m = await import(pathToFileURL(process.argv[2]).href);
const tabs = m.tabsHtml([{ id: 7, title: '<img>', issue_number: '42', running: true }], 7, 2);
out.tabsEscaped = !tabs.includes('<img>') && tabs.includes('&lt;img&gt;');
out.tabOrder = [tabs.indexOf('data-conv-tab="activity"'), tabs.indexOf('data-conv-tab="7"'), tabs.indexOf('data-conv-tab="past"')];
out.runningTab = tabs.includes('is-running');
out.tabsWired = tabs.includes('id="feed-tab-7" aria-controls="feed-conversation"')
  && tabs.includes('id="feed-tab-activity" aria-controls="feed-list"') && tabs.includes('id="feed-tab-past" aria-controls="feed-past"');
const broken = m.conversationHtml({ meta: conversation, messages: null, loadError: 'Request failed (503)' });
out.loadErrorShown = broken.includes('Request failed (503)') && broken.includes('data-conv-action="retry"') && !broken.includes('Loading...');
const running = m.conversationHtml({ meta: conversation, messages: conversation.messages });
out.stopShown = running.includes('data-conv-action="stop"');
out.messageEscaped = !running.includes('<b>failed</b>');
const failed = m.conversationHtml({ meta: conversation, messages: [{ ...conversation.messages[1], status: 'failed', error: 'Timed out after 600s' }] });
out.failureShown = failed.includes('Timed out after 600s');

m.init();
out.subscribed = sockets.length;
sockets[0].onOpen();
await new Promise((r) => setTimeout(r, 20));
elements['feed-tabs'].innerHTML.includes('data-conv-tab="7"') && (out.tabRestored = true);
elements['feed-panel'].listeners.click({ target: { closest: (sel) => sel === '[data-conv-action]'
  ? { getAttribute: (k) => ({ 'data-conv-action': 'show', 'data-conv-tab': '7' })[k] } : null }, preventDefault() {} });
await new Promise((r) => setTimeout(r, 20));
out.active = m.activeTab();
sockets[0].onMessage({ type: 'conversation', event: 'tool', conversation_id: 7, message_id: 2, text: '[mcp__sova__sova_status]' });
sockets[0].onMessage({ type: 'conversation', event: 'text', conversation_id: 7, message_id: 2, text: 'Streamed answer' });
sockets[0].onMessage({ type: 'conversation', event: 'text', conversation_id: 99, message_id: 5, text: 'Other conversation' });
const view = elements['feed-conversation'].innerHTML;
out.panelLabelled = elements['feed-conversation'].attrs['aria-labelledby'] === 'feed-tab-7';
// Two detail requests race: the older one (a running snapshot) answers last and must not replace the newer one.
const pending = [];
g.fetch = (url) => new Promise((resolve) => pending.push({ url, resolve }));
sockets[0].onMessage({ type: 'conversation', event: 'turn_finished', conversation_id: 7, message_id: 2 });
sockets[0].onMessage({ type: 'conversation', event: 'turn_finished', conversation_id: 7, message_id: 2 });
const details = pending.filter((p) => p.url === '/api/conversations/7');
const reply = (body) => ({ ok: true, status: 200, json: () => Promise.resolve(body) });
const done = JSON.parse(JSON.stringify(conversation));
done.messages[1] = { ...done.messages[1], status: 'done', content: 'Final answer' };
details[1].resolve(reply(done));
await new Promise((r) => setTimeout(r, 20));
details[0].resolve(reply(JSON.parse(JSON.stringify(conversation))));
await new Promise((r) => setTimeout(r, 20));
out.staleIgnored = elements['feed-conversation'].innerHTML.includes('Final answer');
out.streamed = view.includes('Streamed answer') && view.includes('[mcp__sova__sova_status]');
out.otherIgnored = !view.includes('Other conversation');
// A second submit while the first request is pending sends nothing more.
const posts = [];
let finishPost;
g.fetch = (url, opts) => {
  if (opts && opts.method === 'POST') {
    posts.push(url);
    return new Promise((resolve) => { finishPost = () => resolve({ ok: false, status: 409, json: () => Promise.resolve({ detail: 'busy' }) }); });
  }
  return Promise.resolve(reply(JSON.parse(JSON.stringify(done))));
};
elements['feed-input'].value = 'hello';
elements['feed-input-form'].listeners.submit({ preventDefault() {} });
elements['feed-input-form'].listeners.submit({ preventDefault() {} });
out.sentOnce = posts.length === 1;
finishPost();
await new Promise((r) => setTimeout(r, 20));
// A stop request that fails gives its button back.
const stopButton = { disabled: false, getAttribute: (k) => ({ 'data-conv-action': 'stop', 'data-conv-id': '7' })[k] };
g.fetch = () => Promise.resolve({ ok: false, status: 500, json: () => Promise.resolve({ detail: 'down' }) });
elements['feed-panel'].listeners.click({ target: { closest: (sel) => sel === '[data-conv-action]' ? stopButton : null }, preventDefault() {} });
out.stopDisabledWhilePending = stopButton.disabled === true;
await new Promise((r) => setTimeout(r, 20));
out.stopReenabled = stopButton.disabled === false;
console.log(JSON.stringify(out));
"""  # noqa: E501 (embedded JavaScript, wrapped by its own style)


@pytest.fixture(scope="module")
def run(tmp_path_factory: pytest.TempPathFactory) -> dict:
    if shutil.which("node") is None:
        if os.environ.get("CI"):
            pytest.fail("node is required in CI: it runs the conversation tab module tests")
        pytest.skip("node is required to run the conversation tab module tests")
    script: Path = tmp_path_factory.mktemp("feed_conversations") / "harness.mjs"
    script.write_text(_HARNESS, encoding="utf-8")
    proc = subprocess.run(["node", str(script), str(_MODULE)], capture_output=True, text=True, timeout=60, check=False)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_the_strip_puts_activity_first_and_past_last_and_escapes_titles(run: dict) -> None:
    first, conversation, past = run["tabOrder"]
    assert 0 <= first < conversation < past
    assert run["tabsEscaped"]
    assert run["runningTab"]


def test_each_tab_controls_its_panel_and_the_shown_conversation_names_its_tab(run: dict) -> None:
    assert run["tabsWired"]
    assert run["panelLabelled"]


def test_a_failed_first_load_shows_the_reason_and_a_retry(run: dict) -> None:
    assert run["loadErrorShown"]


def test_an_older_detail_response_never_replaces_a_newer_one(run: dict) -> None:
    assert run["staleIgnored"]


def test_a_running_turn_offers_stop_and_a_failed_one_shows_its_reason(run: dict) -> None:
    assert run["stopShown"]
    assert run["messageEscaped"]
    assert run["failureShown"]


def test_the_module_uses_the_shared_socket_and_restores_open_tabs(run: dict) -> None:
    assert run["subscribed"] == 1
    assert run["tabRestored"]
    assert run["active"] == 7


def test_deltas_apply_only_to_the_conversation_they_name(run: dict) -> None:
    assert run["streamed"]
    assert run["otherIgnored"]


def test_a_second_submit_while_one_is_pending_sends_nothing_more(run: dict) -> None:
    assert run["sentOnce"]


def test_a_failed_stop_gives_its_button_back(run: dict) -> None:
    assert run["stopDisabledWhilePending"]
    assert run["stopReenabled"]
