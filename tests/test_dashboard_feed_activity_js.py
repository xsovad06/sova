"""The activity feed panel's client module (``static/js/feed/activity.js``) draws the server's narration and keeps
the unread badge. There is no browser here, so the module runs under node against stub globals. A missing `node`
skips locally but fails under CI (the same rule as `test_dashboard_agents_modules.py`)."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.conftest import DASHBOARD_DIR

_MODULE = DASHBOARD_DIR / "static" / "js" / "feed" / "activity.js"
_BASE_HTML = (DASHBOARD_DIR / "templates" / "base.html").read_text(encoding="utf-8")

_HARNESS = r"""
import { pathToFileURL } from 'node:url';

const out = {};
const storage = {};
const fetches = [];
const timers = [];
const elements = {};
const sockets = [];
let confirmAnswer = true;

function el(id, attrs = {}) {
  const classes = new Set(['hidden']);
  const listeners = {};
  let html = '';
  return {
    id, textContent: '', attrs: { ...attrs }, listeners, _fold: null, _focusable: [],
    // A render replaces the markup, so the background fold is a new closed one (as the browser would make it).
    get innerHTML() { return html; },
    set innerHTML(v) { html = v; this._fold = v.includes('feed-fold') ? { open: false } : null; },
    classList: { add: (c) => classes.add(c), remove: (c) => classes.delete(c), contains: (c) => classes.has(c) },
    setAttribute(k, v) { this.attrs[k] = v; },
    getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; },
    querySelector(sel) { return sel === '.feed-fold' ? this._fold : null; },
    querySelectorAll() { return this._focusable; },
    contains(node) { return !!node && node._in === true; },
    addEventListener(t, fn) { listeners[t] = fn; },
  };
}
elements['feed-list'] = el('feed-list');
elements['feed-activity'] = el('feed-activity', { 'data-load-failed': 'LOAD-FAILED-WORDS' });
elements['feed-badge'] = el('feed-badge');
elements['feed-fab'] = el('feed-fab', { 'data-label': 'FAB-WORDS', 'data-label-unread': 'FAB-WORDS, {count} unread' });
const docListeners = {};
const winListeners = {};
const bodyAttrs = {};

const g = globalThis;
g.window = { SOVA_PROJECT_SLUG: '', addEventListener: (t, fn) => { winListeners[t] = fn; } };
g.document = {
  hidden: false,
  activeElement: null,
  getElementById: (id) => elements[id] || null,
  addEventListener: (t, fn) => { docListeners[t] = fn; },
  body: { getAttribute: (k) => (k in bodyAttrs ? bodyAttrs[k] : null) },
};
g.apiUrl = (p) => '/api' + p;
g.escapeHtml = (s) => String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
g.stateHex = (token) => 'hex-' + token;
g.sovaBtn = (label, opts) => '<button data-label="' + escapeHtml(label) + '"' + (opts.disabled ? ' disabled' : '') +
  Object.entries(opts.attrs || {}).map(([k, v]) => ' ' + k + '="' + escapeHtml(v) + '"').join('') + '>' + escapeHtml(label) + '</button>';
g.safeStorageGet = (k) => (k in storage ? storage[k] : null);
g.safeStorageSet = (k, v) => { storage[k] = v; };
g.subscribeSovaSocket = (opts) => { sockets.push(opts); };
g.jsonCsrfHeaders = () => ({ 'Content-Type': 'application/json' });
g.getErrorDetail = async (_res, fallback) => fallback;
// app.js's actionKey, which the Work card uses too.
g.actionKey = (issue, pr, run) => (issue ? 'issue:' + issue : pr ? 'pr:' + pr : run ? 'run:' + run : '');
g.sovaConfirm = async () => confirmAnswer;
const tracked = [];
const actionStates = {};
const actionListeners = [];
g.onActionStateChange = (fn) => { actionListeners.push(fn); };
g.actionStateFor = (key) => actionStates[key] || null;
g.actionStatusHtml = (key) => (actionStates[key] ? '<span data-state="' + actionStates[key].status + '"></span>' : '');
g.runTrackedAction = async (key, label, fn) => {
  actionStates[key] = { status: 'running' };
  actionListeners.forEach((f) => f());
  let result;
  try { result = { ok: true, message: await fn() }; } catch (e) { result = { ok: false, message: e.message }; }
  actionStates[key] = { status: result.ok ? 'done' : 'failed' };
  tracked.push([key, label, result]);
  actionListeners.forEach((f) => f());
  return result;
};
g.setTimeout = (fn, ms) => { timers.push({ fn, ms }); return timers.length; };
const cleared = [];
g.clearTimeout = (id) => { cleared.push(id); };
g.fetch = (url, opts) => new Promise((resolve) => { fetches.push({ url, opts, resolve }); });
const flush = () => new Promise((r) => setImmediate(r));
const answer = async (i, body, ok = true) => {
  fetches[i].resolve({ ok, status: ok ? 200 : 500, clone() { return this; }, json: async () => body });
  await flush();
};

const mod = await import(pathToFileURL(process.argv[2]).href);

// The words the server's narration sends (feed_narration.labels()): the module draws them and writes none itself.
const labels = { attention: 'Needs you', today: 'Today', earlier: 'Earlier', open_in_work: 'Open in Work', no_activity: 'No activity yet' };
const attention = (identity, extra = {}) => ({
  identity, kind: 'failed', key: 'issue:1', issue_number: '1', pr_number: null, title: 'Fix <b>it</b>',
  text: 'I need you: it failed.', color: 'red', number: '1', action: null, ...extra,
});
const entry = (key, extra = {}) => ({
  key, issue_number: '2', pr_number: 20, title: 'Add thing', text: 'Merged.', state: 'merged',
  state_label: 'Merged', state_color: 'green-strong', running: false, started_at: 'x', number: '2', cost_usd: null, ...extra,
});

// Drawing.
const full = mod.activityHtml({
  attention: [attention('failed:issue:1'), attention('review_exhausted:issue:9', {
    kind: 'review_exhausted', issue_number: '9', number: '9', title: null,
    action: { id: 'integrate', label: 'Integrate', ref: '9', confirm: true } })],
  today: [entry('issue:2', { cost_usd: '8.580000' }), entry('issue:3', { running: true, text: 'Researching it now, 3 min in.', issue_number: null, pr_number: 30, number: '30' })],
  background: [{ kind: 'rate_limit', count: 2, text: '2 GitHub rate limit events.' }, { kind: 'other', count: 1, text: '1 other event.' }],
  periods: [
    { key: 'yesterday', text: 'Yesterday: I worked on 2 issues and merged 1, $3.50.', from: '2026-11-22', to: '2026-11-22' },
    { key: 'old', text: 'Old: no dates.' },
  ],
  empty_today_text: 'Nothing yet today.',
  background_summary: 'Background, 3 things',
  labels,
});
out.order = ['Needs you', 'Today', 'Background, 3 things', 'Earlier'].map((s) => full.indexOf(s));
out.escaped = full.includes('Fix &lt;b&gt;it&lt;/b&gt;') && !full.includes('<b>');
out.dotColors = ['hex-red', 'hex-green-strong'].every((c) => full.includes(c));
out.button = full.includes('data-feed-action="integrate"') && full.includes('data-feed-ref="9"') && full.includes('data-feed-confirm="1"');
out.openInWork = full.includes('Open in Work') && full.includes('class="feed-line-open" href="/work?card=1"');
const cardLinks = mod.activityHtml({ labels, attention: [
  attention('p', { issue_number: null, pr_number: 44, number: '44', key: 'pr:44' }),
  attention('j', { issue_number: 'PROJ-7', number: 'PROJ-7' }),
] });
out.cardLinks = [cardLinks.includes('class="feed-line-open" href="/work?card=pr%3A44"'),
  cardLinks.includes('class="feed-line-open" href="/work?number=PROJ-7"')];
const worded = mod.activityHtml({ today: [], attention: [attention('w')], background: [{ kind: 'k', count: 1, text: 't' }], periods: [{ text: 'p' }],
  background_summary: 'BG-WORDS', empty_today_text: 'ET-WORDS',
  labels: { attention: 'L-ATTN', today: 'L-TODAY', earlier: 'L-EARLIER', open_in_work: 'L-OPEN', no_activity: 'L-NONE' } });
out.serverWords = ['L-ATTN', 'L-TODAY', 'L-EARLIER', 'L-OPEN', 'BG-WORDS', 'ET-WORDS'].every((w) => worded.includes(w)) &&
  !worded.includes('Needs you') && !worded.includes('Background');
out.noLabelsNoWords = !mod.activityHtml({ today: [entry('issue:2')], attention: [attention('w')] }).includes('Needs you');
out.running = full.includes('feed-line-running') && full.includes('PR #30');
out.barePrLink = full.includes('href="/work?number=30"');
out.cost = full.includes('<span class="feed-line-cost">$8.58</span>') && (full.match(/feed-line-cost/g) || []).length === 1;
out.periodLink = full.includes('<a class="feed-period" href="/work?from=2026-11-22&amp;to=2026-11-22">') &&
  full.includes('<div class="feed-period">Old: no dates.</div>');

const quiet = mod.activityHtml({ labels, today: [], attention: [], background: [], periods: [{ text: 'Yesterday: x.' }], empty_today_text: 'Nothing yet today.' });
out.quiet = [quiet.indexOf('Nothing yet today.') > -1, quiet.indexOf('Nothing yet today.') < quiet.indexOf('Yesterday: x.')];
out.empty = mod.activityHtml({ labels, today: [], attention: [], background: [], periods: [] });

g.window.SOVA_PROJECT_SLUG = 'demo';
out.slugLink = mod.activityHtml({ today: [entry('issue:2')], attention: [], background: [], periods: [] }).includes('href="/p/demo/work?number=2"');
g.window.SOVA_PROJECT_SLUG = '';

// Init: one fetch with the browser's zone, the badge counts unseen attention lines.
mod.init();
out.firstFetch = fetches[0].url;
out.socketKeys = Object.keys(sockets[0]).sort();
out.badgeBeforeAnyPayload = elements['feed-badge'].classList.contains('hidden');
await answer(0, { labels, attention: [attention('a'), attention('b')], today: [], background: [], periods: [] });
out.badgeUnread = [elements['feed-badge'].textContent, elements['feed-badge'].classList.contains('hidden'), elements['feed-fab'].attrs['aria-label']];
out.rendered = elements['feed-activity'].innerHTML.includes('Needs you');

// Opening marks every current line seen (and persists it); the badge hides while open and stays 0 after closing.
docListeners['sova:feed-toggle']({ detail: { open: true } });
out.openBadgeHidden = elements['feed-badge'].classList.contains('hidden');
out.savedSeen = Object.keys(JSON.parse(storage['sova.feed.seen:_'])).sort();
out.oldKeyUnused = !('sova.feed.seen' in storage);
await answer(1, { labels, attention: [attention('a'), attention('b')], today: [], background: [], periods: [] });
docListeners['sova:feed-toggle']({ detail: { open: false } });
out.closedBadgeHidden = elements['feed-badge'].classList.contains('hidden');

// A new line while closed is unread. One missing from a payload stays seen (a line can be briefly absent), so it
// is not counted again when it returns; only after a day away is it forgotten.
let n = fetches.length;
mod.refresh(); await answer(n, { labels, attention: [attention('a'), attention('b'), attention('c')], today: [], background: [], periods: [] });
out.newUnread = elements['feed-badge'].textContent;
n = fetches.length;
mod.refresh(); await answer(n, { labels, attention: [attention('a')], today: [], background: [], periods: [] });
out.afterResolved = [elements['feed-badge'].classList.contains('hidden'), Object.keys(JSON.parse(storage['sova.feed.seen:_'])).sort()];
n = fetches.length;
mod.refresh(); await answer(n, { labels, attention: [attention('a'), attention('b'), attention('c')], today: [], background: [], periods: [] });
out.returned = elements['feed-badge'].textContent;
const realNow = Date.now;
Date.now = () => realNow() + 2 * 24 * 60 * 60 * 1000;
n = fetches.length;
mod.refresh(); await answer(n, { labels, attention: [attention('a')], today: [], background: [], periods: [] });
out.forgotAfterADay = Object.keys(JSON.parse(storage['sova.feed.seen:_'])).sort();
n = fetches.length;
mod.refresh(); await answer(n, { labels, attention: [attention('a'), attention('b')], today: [], background: [], periods: [] });
out.returnedAfterADay = elements['feed-badge'].textContent;
Date.now = realNow;
n = fetches.length;
mod.refresh();
await answer(n, { labels, attention: Array.from({ length: 12 }, (_, i) => attention('x' + i)), today: [], background: [], periods: [] });
out.badgeCap = elements['feed-badge'].textContent;

// A feed message schedules one refetch however many arrive; its body and other message types are ignored.
const onMessage = sockets[0].onMessage;
timers.length = 0;
for (let i = 0; i < 5; i++) onMessage({ type: 'feed', event: { id: i } });
onMessage({ type: 'activity' });
onMessage({ type: 'status_update', runs: [] });
onMessage(null);
out.timers = timers.map((t) => t.ms);
n = fetches.length;
timers[0].fn();
out.refetched = fetches.length - n;
await answer(fetches.length - 1, { labels, attention: [], today: [], background: [], periods: [] });
timers.length = 0;
onMessage({ type: 'feed' });
out.rescheduled = timers.length;

// A slower, older response never overwrites a newer one.
n = fetches.length;
mod.refresh(); mod.refresh();
await answer(n + 1, { labels, attention: [], today: [entry('issue:5', { text: 'Newer.' })], background: [], periods: [] });
await answer(n, { labels, attention: [], today: [entry('issue:5', { text: 'Older.' })], background: [], periods: [] });
out.staleDropped = elements['feed-activity'].innerHTML.includes('Newer.') && !elements['feed-activity'].innerHTML.includes('Older.');

// A failed refresh keeps the last good payload on screen.
n = fetches.length;
mod.refresh(); await answer(n, {}, false);
out.keptOnFailure = elements['feed-activity'].innerHTML.includes('Newer.');

// An action posts the handoff action for its ref, after a confirmation when it asks for one.
const click = (attrs) => {
  const btn = { disabled: false, getAttribute: (k) => attrs[k] ?? null };
  elements['feed-list'].listeners.click({ target: { closest: () => btn } });
  return btn;
};
n = fetches.length;
click({ 'data-feed-action': 'approve-spec', 'data-feed-ref': '8', 'data-feed-label': 'Approve', 'data-feed-confirm': '0',
  'data-feed-started-text': 'STARTED-WORDS', 'data-feed-failed-text': 'FAILED-WORDS' });
await flush();
out.post = { url: fetches[n].url, method: fetches[n].opts.method, body: JSON.parse(fetches[n].opts.body) };
// While it runs, the line shows the running state and its button is disabled.
const runningLine = mod.activityHtml({ labels, attention: [attention('s', { action: { id: 'approve-spec', label: 'Approve', ref: '8', confirm: false } })] });
out.runningShown = runningLine.includes('data-state="running"') && runningLine.includes(' disabled data-feed-action');
await answer(n, {});
await flush();
out.tracked = tracked[tracked.length - 1];
n = fetches.length;
click({ 'data-feed-action': 'merge', 'data-feed-ref': 'pr:3', 'data-feed-label': 'Merge', 'data-feed-confirm': '0',
  'data-feed-started-text': 'STARTED-WORDS', 'data-feed-failed-text': 'FAILED-WORDS' });
await flush();
// A PR with no issue posts its ref as ``pr:N``, which /handoff/execute reads as the PR (see its tests).
out.postPr = JSON.parse(fetches[n].opts.body);
await answer(n, {}, false);
await flush();
out.trackedFailure = tracked[tracked.length - 1];
n = fetches.length;
confirmAnswer = false;
const confirms = [];
g.sovaConfirm = async (text) => { confirms.push(text); return confirmAnswer; };
click({ 'data-feed-action': 'integrate', 'data-feed-ref': 'pr:7', 'data-feed-label': 'Merge', 'data-feed-confirm': '1',
  'data-feed-confirm-text': 'CONFIRM-WORDS' });
await flush();
out.declined = fetches.length - n;
out.confirmAsked = confirms;
elements['feed-list'].listeners.click({ target: { closest: () => null } });
out.stray = fetches.length - n;

// Each project has its own seen set: the same identity in another project is unread there, and a panel opened
// before the first payload writes nothing.
const fresh = async (slug, version) => {
  g.window.SOVA_PROJECT_SLUG = slug;
  const second = await import(pathToFileURL(process.argv[2]).href + '?v=' + version);
  const first = fetches.length;
  second.init();
  return { second, first };
};
let other = await fresh('proj-b', 'b');
await answer(other.first, { labels, attention: [attention('a')], today: [], background: [], periods: [] });
out.otherProjectUnread = elements['feed-badge'].textContent;
docListeners['sova:feed-toggle']({ detail: { open: true } });
await answer(fetches.length - 1, { labels, attention: [attention('a')], today: [], background: [], periods: [] });
out.otherProjectKeys = ['sova.feed.seen:_', 'sova.feed.seen:proj-b'].map((k) => Object.keys(JSON.parse(storage[k] || '{}')));
docListeners['sova:feed-toggle']({ detail: { open: false } });
other = await fresh('proj-c', 'c');
docListeners['sova:feed-toggle']({ detail: { open: true } });
out.openedBeforePayload = 'sova.feed.seen:proj-c' in storage;

// Two tabs on one project: a write merges what the other tab saved, and a `storage` event brings its marks in.
const tabs = async (version) => {
  const t = await fresh('tabs', version);
  t.toggle = docListeners['sova:feed-toggle'];
  t.storage = winListeners['storage'];
  return t;
};
const badge = () => [elements['feed-badge'].textContent, elements['feed-badge'].classList.contains('hidden')];
const payloadOf = (ids) => ({ labels, attention: ids.map((id) => attention(id)), today: [], background: [], periods: [] });
const tabA = await tabs('t-a');
const tabB = await tabs('t-b');
await answer(tabA.first, payloadOf(['x']));
await answer(tabB.first, payloadOf(['x']));
tabA.toggle({ detail: { open: true } });
await answer(fetches.length - 1, payloadOf(['x']));
tabA.toggle({ detail: { open: false } });
n = fetches.length;
tabB.second.refresh();
await answer(n, payloadOf(['x', 'y']));
out.tabBSeesATheMarks = badge();
tabB.toggle({ detail: { open: true } });
await answer(fetches.length - 1, payloadOf(['x', 'y']));
tabB.toggle({ detail: { open: false } });
out.storedAfterBoth = Object.keys(JSON.parse(storage['sova.feed.seen:tabs'])).sort();
n = fetches.length;
tabA.second.refresh();
await answer(n, payloadOf(['x', 'y']));
out.tabAKeepsBsMarks = badge();
const tabC = await tabs('t-c');
await answer(tabC.first, payloadOf(['x', 'y', 'z']));
out.tabCBefore = badge();
storage['sova.feed.seen:tabs'] = JSON.stringify({ ...JSON.parse(storage['sova.feed.seen:tabs']), z: Date.now() });
tabC.storage({ key: 'sova.feed.other' });
out.otherKeyIgnored = badge();
tabC.storage({ key: 'sova.feed.seen:tabs' });
out.tabCAfter = badge();

// A panel already open when the module starts (the shared side panel names it on the body) counts as an open event.
bodyAttrs['data-side-panel'] = 'feed';
const early = await fresh('early', 'e');
await answer(early.first, payloadOf(['q']));
out.alreadyOpen = [badge()[1], Object.keys(JSON.parse(storage['sova.feed.seen:early'] || '{}'))];
delete bodyAttrs['data-side-panel'];

// A running line asks again once a minute while the panel is open; a payload without one does not. Midnight always does.
const live = await fresh('live', 'l');
const liveToggle = docListeners['sova:feed-toggle'];
const running = { ...payloadOf([]), today: [entry('issue:5', { running: true, text: 'Researching it now, 1 min in.' })] };
let from = timers.length;
await answer(live.first, payloadOf([]));
out.closedNoRunning = timers.slice(from).map((t) => t.ms);
liveToggle({ detail: { open: true } });
from = timers.length;
await answer(fetches.length - 1, running);
out.openRunning = timers.slice(from).map((t) => t.ms);
const tickerId = from + 1 + timers.slice(from).findIndex((t) => t.ms === 60000);
from = timers.length;
live.second.refresh();
await answer(fetches.length - 1, payloadOf([]));
out.openNotRunning = timers.slice(from).map((t) => t.ms);
out.tickerCleared = cleared.includes(tickerId);
from = timers.length;
live.second.refresh();
await answer(fetches.length - 1, running);
const againId = from + 1 + timers.slice(from).findIndex((t) => t.ms === 60000);
n = fetches.length;
timers[againId - 1].fn();
out.tickerFetches = fetches.length - n;
await answer(fetches.length - 1, running);
liveToggle({ detail: { open: false } });
from = timers.length;
live.second.refresh();
await answer(fetches.length - 1, running);
out.closedRunning = timers.slice(from).map((t) => t.ms);
// Midnight: a refetch at the next local midnight.
const midnightTimer = timers.slice(from).find((t) => t.ms !== 60000);
out.midnightMs = midnightTimer.ms;
n = fetches.length;
midnightTimer.fn();
out.midnightFetches = fetches.length - n;
// Messages that mean a run changed schedule a refetch; the 2 s status push does not.
const liveMessage = sockets[sockets.length - 1].onMessage;
from = timers.length;
liveMessage({ type: 'status_update', runs: [] });
liveMessage({ type: 'agent_finished' });
out.finishedScheduled = timers.slice(from).map((t) => t.ms);

// A failed refresh from the minute ticker still re-arms it (and midnight), so a running line keeps its time true.
const tf = await fresh('tf', 'tf');
docListeners['sova:feed-toggle']({ detail: { open: true } });
from = timers.length;
await answer(fetches.length - 1, running);
const tfTicker = timers.slice(from).findIndex((t) => t.ms === 60000);
n = fetches.length;
timers[from + tfTicker].fn();
from = timers.length;
await answer(n, {}, false);
out.tickerFailureRearms = timers.slice(from).map((t) => t.ms).filter((ms) => ms === 60000).length;
const nope = await fresh('nope', 'nope');
await answer(nope.first, {}, false);
out.loadFailedWords = elements['feed-activity'].innerHTML;

// A refresh keeps the background fold the user opened, and the focused button.
const keep = await fresh('keep', 'keep');
const withFold = { ...payloadOf(['k']), background: [{ kind: 'other', count: 1, text: '1 other event.' }], background_summary: 'BG' };
withFold.attention = [attention('k', { action: { id: 'integrate', label: 'Integrate', ref: '9', confirm: false } })];
await answer(keep.first, withFold);
elements['feed-activity']._fold.open = true;
const oldButton = { _in: true, getAttribute: (k) => ({ 'data-feed-action': 'integrate', 'data-feed-ref': '9' })[k] ?? null };
const newButton = { focused: false, getAttribute: oldButton.getAttribute, focus() { this.focused = true; } };
g.document.activeElement = oldButton;
elements['feed-activity']._focusable = [newButton];
n = fetches.length;
keep.second.refresh();
await answer(n, withFold);
out.foldKeptOpen = elements['feed-activity']._fold.open;
out.focusKept = newButton.focused;
g.document.activeElement = null;
elements['feed-activity']._focusable = [];

// A line that never went away stays seen however long no tab was open: a mark two days old is kept while its line is
// in the payload. A mark whose line is absent is forgotten, so the same problem coming back later counts as new.
storage['sova.feed.seen:old'] = JSON.stringify({ stale: Date.now() - 2 * 24 * 60 * 60 * 1000, fresh: Date.now(), gone: Date.now() - 2 * 24 * 60 * 60 * 1000 });
const oldSeen = await fresh('old', 'old');
await answer(oldSeen.first, payloadOf(['stale', 'fresh']));
out.longPresentBadgeHidden = elements['feed-badge'].classList.contains('hidden');
out.goneMarkForgotten = !('gone' in JSON.parse(storage['sova.feed.seen:old']));
out.staleMarkKept = 'stale' in JSON.parse(storage['sova.feed.seen:old']);

// The socket's fallback poll and reconnect go through the coalescing, skip a hidden tab, and a closed panel that was
// rebuilt a moment ago.
const poll = await fresh('poll', 'poll');
await answer(poll.first, payloadOf([]));
const pollSocket = sockets[sockets.length - 1];
from = timers.length;
pollSocket.onFallbackPoll();
out.pollRecentClosed = timers.length - from;
Date.now = () => realNow() + 60 * 1000;
pollSocket.onFallbackPoll();
out.pollStaleClosed = timers.slice(from).map((t) => t.ms);
g.document.hidden = true;
from = timers.length;
pollSocket.onFallbackPoll();
out.pollHidden = timers.length - from;
g.document.hidden = false;
n = fetches.length;
pollSocket.onOpen();
out.openDirectFetches = fetches.length - n;
out.openScheduled = timers.slice(-1)[0].ms;
Date.now = realNow;

console.log(JSON.stringify(out));
"""  # noqa: E501 (embedded JavaScript, wrapped by its own style)


@pytest.fixture(scope="module")
def run(tmp_path_factory: pytest.TempPathFactory) -> dict:
    if shutil.which("node") is None:
        if os.environ.get("CI"):
            pytest.fail("node is required in CI: it runs the activity feed module tests")
        pytest.skip("node is required to run the activity feed module tests")
    script: Path = tmp_path_factory.mktemp("feed_activity") / "harness.mjs"
    script.write_text(_HARNESS, encoding="utf-8")
    proc = subprocess.run(["node", str(script), str(_MODULE)], capture_output=True, text=True, timeout=60, check=False)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


class TestDrawing:
    def test_sections_come_in_order_and_everything_is_escaped(self, run: dict) -> None:
        assert run["order"] == sorted(run["order"])
        assert min(run["order"]) >= 0
        assert run["escaped"]

    def test_lines_use_the_servers_colour_tokens(self, run: dict) -> None:
        assert run["dotColors"]

    def test_an_attention_line_has_its_button_or_a_link_to_the_work_page(self, run: dict) -> None:
        assert run["button"]
        assert run["openInWork"]

    def test_an_attention_line_without_a_button_links_to_the_issues_work_card_not_the_run_history(
        self, run: dict
    ) -> None:
        # An issue or a PR with no issue opens its card; an id the board cannot select falls back to the history.
        assert run["cardLinks"] == [True, True]

    def test_every_word_on_the_panel_comes_from_the_payload(self, run: dict) -> None:
        assert run["serverWords"]
        assert run["noLabelsNoWords"]

    def test_a_today_line_shows_its_cost_only_when_there_is_one(self, run: dict) -> None:
        assert run["cost"]

    def test_a_period_line_links_to_the_history_on_its_dates(self, run: dict) -> None:
        assert run["periodLink"]

    def test_a_running_line_is_marked_and_a_pr_only_entry_links_by_its_pr(self, run: dict) -> None:
        assert run["running"]
        assert run["barePrLink"]

    def test_links_carry_the_project_prefix(self, run: dict) -> None:
        assert run["slugLink"]

    def test_a_quiet_day_says_so_above_the_period_lines(self, run: dict) -> None:
        assert run["quiet"] == [True, True]

    def test_a_fresh_project_gets_an_empty_state(self, run: dict) -> None:
        assert run["empty"] == '<p class="feed-empty">No activity yet</p>'


class TestFetching:
    def test_the_first_fetch_sends_the_browser_zone_and_the_module_subscribes_to_the_shared_socket(
        self, run: dict
    ) -> None:
        assert run["firstFetch"].startswith("/api/feed/activity?tz=")
        assert run["socketKeys"] == ["onFallbackPoll", "onMessage", "onOpen"]

    def test_many_feed_messages_schedule_one_refetch_of_about_a_second(self, run: dict) -> None:
        assert run["timers"] == [1000]
        assert run["refetched"] == 1
        assert run["rescheduled"] == 1

    def test_a_slower_older_response_is_dropped(self, run: dict) -> None:
        assert run["staleDropped"]

    def test_a_failed_refresh_keeps_the_last_good_lines(self, run: dict) -> None:
        assert run["keptOnFailure"]


class TestUnreadBadge:
    def test_it_counts_attention_lines_not_seen_and_names_them_for_screen_readers(self, run: dict) -> None:
        assert run["badgeUnread"] == ["2", False, "FAB-WORDS, 2 unread"]
        assert run["rendered"]

    def test_opening_marks_the_current_lines_seen_and_persists_them(self, run: dict) -> None:
        assert run["openBadgeHidden"]
        assert run["savedSeen"] == ["a", "b"]
        assert run["oldKeyUnused"]
        assert run["closedBadgeHidden"]

    def test_a_line_missing_from_a_payload_stays_seen_and_is_forgotten_only_after_a_day(self, run: dict) -> None:
        assert run["newUnread"] == "1"
        assert run["afterResolved"] == [True, ["a", "b"]]
        # b was absent from that payload and is not counted again when it returns; c was never seen.
        assert run["returned"] == "1"
        assert run["forgotAfterADay"] == ["a"]
        assert run["returnedAfterADay"] == "1"

    def test_the_seen_set_is_per_project_and_an_early_open_writes_nothing(self, run: dict) -> None:
        # Identity "a" was seen in the first project; in another it is unread, and its marks stay separate.
        assert run["otherProjectUnread"] == "1"
        assert run["otherProjectKeys"] == [["a"], ["a"]]
        assert run["openedBeforePayload"] is False

    def test_a_long_count_reads_nine_plus(self, run: dict) -> None:
        assert run["badgeCap"] == "9+"


class TestSeenAcrossTabs:
    def test_a_write_merges_what_another_tab_saved_so_no_mark_is_lost(self, run: dict) -> None:
        # Tab B started before tab A marked x seen: B still counts only the unseen y, and both marks survive.
        assert run["tabBSeesATheMarks"] == ["1", False]
        assert run["storedAfterBoth"] == ["x", "y"]
        assert run["tabAKeepsBsMarks"][1] is True

    def test_a_storage_event_for_the_seen_key_brings_the_other_tabs_marks_in(self, run: dict) -> None:
        assert run["tabCBefore"] == ["1", False]
        assert run["otherKeyIgnored"] == ["1", False]
        assert run["tabCAfter"][1] is True

    def test_a_panel_already_open_at_start_marks_its_lines_seen(self, run: dict) -> None:
        assert run["alreadyOpen"] == [True, ["q"]]

    def test_the_badge_is_hidden_before_any_payload(self, run: dict) -> None:
        assert run["badgeBeforeAnyPayload"] is True


class TestKeepingTimeWordsTrue:
    def test_a_running_line_with_the_panel_open_schedules_a_minute_refetch_and_no_running_line_does_not(
        self, run: dict
    ) -> None:
        assert 60000 in run["openRunning"]
        assert 60000 not in run["closedNoRunning"]
        assert 60000 not in run["openNotRunning"]
        assert 60000 not in run["closedRunning"]
        assert run["tickerFetches"] == 1
        assert run["tickerCleared"]

    def test_the_next_local_midnight_always_schedules_a_refetch(self, run: dict) -> None:
        assert len(run["closedNoRunning"]) == 1
        assert 0 < run["midnightMs"] <= 24 * 60 * 60 * 1000 + 1000
        assert run["midnightFetches"] == 1

    def test_a_finished_run_message_schedules_a_refetch_and_the_status_push_does_not(self, run: dict) -> None:
        assert run["finishedScheduled"] == [1000]


class TestRobustness:
    def test_a_failed_minute_refresh_re_arms_the_timers_and_says_so_in_the_servers_words(self, run: dict) -> None:
        assert run["tickerFailureRearms"] == 1
        assert "LOAD-FAILED-WORDS" in run["loadFailedWords"]

    def test_a_refresh_keeps_the_open_background_fold_and_the_focused_button(self, run: dict) -> None:
        assert run["foldKeptOpen"] is True
        assert run["focusKept"] is True

    def test_a_line_that_stayed_present_is_still_seen_after_a_long_absence(self, run: dict) -> None:
        # "stale" was marked two days ago and its line is still in the payload: it does not re-badge. A mark whose
        # line is absent is the one that is forgotten.
        assert run["longPresentBadgeHidden"] is True
        assert run["staleMarkKept"] is True
        assert run["goneMarkForgotten"] is True

    def test_the_fallback_poll_and_reconnect_are_coalesced_and_skip_what_the_badge_does_not_need(
        self, run: dict
    ) -> None:
        assert run["pollRecentClosed"] == 0
        assert run["pollStaleClosed"] == [1000]
        assert run["pollHidden"] == 0
        assert run["openDirectFetches"] == 0
        assert run["openScheduled"] == 1000


class TestActions:
    def test_an_action_posts_its_handoff_action_for_the_ref(self, run: dict) -> None:
        assert run["post"] == {
            "url": "/api/handoff/execute",
            "method": "POST",
            "body": {"action_id": "approve-spec", "issue": "8"},
        }
        # Keyed like the Work card of the same issue, so both show one running state.
        assert run["tracked"] == ["issue:8", "Approve", {"ok": True, "message": "STARTED-WORDS"}]
        assert run["runningShown"]

    def test_a_pr_only_action_posts_the_pr_ref_the_handoff_endpoint_reads(self, run: dict) -> None:
        assert run["postPr"] == {"action_id": "merge", "issue": "pr:3"}

    def test_a_refused_action_is_shown_as_failed_on_its_line(self, run: dict) -> None:
        assert run["trackedFailure"] == ["pr:3", "Merge", {"ok": False, "message": "FAILED-WORDS"}]

    def test_a_declined_confirmation_posts_nothing_and_a_click_elsewhere_does_nothing(self, run: dict) -> None:
        assert run["declined"] == 0
        assert run["confirmAsked"] == ["CONFIRM-WORDS"]
        assert run["stray"] == 0


class TestWiring:
    def test_the_module_is_loaded_by_the_base_template_and_draws_into_the_activity_container(self) -> None:
        assert "/static/js/feed/activity.js" in _BASE_HTML
        assert 'id="feed-activity"' in _BASE_HTML
        assert 'id="feed-briefing-slot"' in _BASE_HTML

    def test_the_work_board_reads_the_card_link_the_feed_points_at(self) -> None:
        board = (DASHBOARD_DIR / "static" / "js" / "pages" / "work" / "board.js").read_text(encoding="utf-8")
        assert "get('card')" in board
        assert "_openLinkedCard();" in board
        assert "card:" in _MODULE.read_text(encoding="utf-8")

    def test_the_work_board_selects_a_linked_card_only_when_it_is_on_the_board(self) -> None:
        board = (DASHBOARD_DIR / "static" / "js" / "pages" / "work" / "board.js").read_text(encoding="utf-8")
        body = board[board.index("function _findCard") : board.index("export function setDragging")]
        # Found by comparing each card's args, selected only when found, else a toast.
        assert "JSON.parse(card.getAttribute('data-args'))[0] === key" in body
        assert body.index("if (!card)") < body.index("select(key);")
        assert "showToast(" in body
        assert "replaceState" in body

    def test_every_app_js_global_the_module_calls_is_declared_there(self) -> None:
        """app.js loads on every page, the module only where base.html imports it: a name defined by a Work-page module
        would make init() throw elsewhere and leave the feed empty, which the stubbed harness cannot see."""
        app = (DASHBOARD_DIR / "static" / "app.js").read_text(encoding="utf-8")
        used = (
            "actionKey actionStateFor actionStatusHtml onActionStateChange runTrackedAction stateHex sovaBtn "
            "sovaConfirm subscribeSovaSocket getErrorDetail jsonCsrfHeaders safeStorageGet safeStorageSet apiUrl "
            "escapeHtml"
        ).split()
        source = _MODULE.read_text(encoding="utf-8")
        for name in used:
            assert re.search(rf"\b{name}\(", source), f"{name} is no longer called: drop it from this list"
            assert re.search(rf"^(async )?function {name}\(", app, re.MULTILINE), (
                f"{name} is not a top-level app.js function"
            )

    def test_the_run_history_narrows_to_an_item_whose_card_the_board_could_not_open(self) -> None:
        history = (DASHBOARD_DIR / "static" / "js" / "pages" / "agents" / "history.js").read_text(encoding="utf-8")
        board = (DASHBOARD_DIR / "static" / "js" / "pages" / "work" / "board.js").read_text(encoding="utf-8")
        assert "'sova:linked-card-missing'" in history
        assert "'sova:linked-card-missing'" in board

    def test_the_module_is_started_by_a_dynamic_import_so_a_failed_load_is_caught(self) -> None:
        assert "import('/static/js/feed/activity.js')" in _BASE_HTML
        assert ".catch(" in _BASE_HTML

    def test_the_module_writes_no_sentence_of_its_own(self) -> None:
        # Every sentence is the server's (feed_narration.py); the module only draws it.
        source = _MODULE.read_text(encoding="utf-8")
        for words in ("Could not", " started'", " on ' +", "need you", "'Run'", "Activity Feed", "retries"):
            assert words not in source, words

    def test_the_module_opens_no_socket_of_its_own(self) -> None:
        source = _MODULE.read_text(encoding="utf-8")
        assert "new WebSocket" not in source
        assert "new EventSource" not in source
        assert "/feed/history" not in source
