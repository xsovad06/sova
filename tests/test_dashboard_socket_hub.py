"""Behavior of app.js's shared socket hub, stale markers and step-less run badge (issue #1459).

Runs the real `static/app.js` under node in a `vm` sandbox with a fake WebSocket, a manual clock and a stubbed DOM,
so the shipped functions are exercised rather than a copy of their logic.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

APP_JS = Path(__file__).parent.parent / "sova" / "dashboard" / "static" / "app.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is required to run app.js")

_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const [appJsPath, scenarioName] = process.argv.slice(1);

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

// Manual clock: timers fire only through advance().
const clock = { now: 1000000, nextId: 1, timers: new Map() };
function addTimer(fn, ms, repeat) {
  const id = clock.nextId++;
  clock.timers.set(id, { fn, at: clock.now + ms, ms, repeat });
  return id;
}
function advance(ms) {
  const end = clock.now + ms;
  for (;;) {
    let next = null;
    for (const [id, t] of clock.timers) if (t.at <= end && (!next || t.at < next.t.at)) next = { id, t };
    if (!next) break;
    clock.now = next.t.at;
    if (next.t.repeat) next.t.at += next.t.ms; else clock.timers.delete(next.id);
    next.t.fn();
  }
  clock.now = end;
}

class FakeDate extends Date {
  constructor(...a) { if (a.length) super(...a); else super(clock.now); }
  static now() { return clock.now; }
}

const sockets = [];
class FakeWebSocket {
  constructor(url) { this.url = url; this.readyState = 0; this.closed = false; sockets.push(this); }
  // Like a browser: CLOSING at once, and onclose arrives later, often after the hub has built a replacement socket.
  close() {
    if (this.closed) return;
    this.closed = true;
    this.readyState = 2;
    addTimer(() => { this.readyState = 3; if (this.onclose) this.onclose(); }, 0, false);
  }
  open() { if (this.closed) return; this.readyState = 1; if (this.onopen) this.onopen(); }
  push(msg) { this.onmessage({ data: JSON.stringify(msg) }); }
  drop() { this.readyState = 3; this.onclose(); }   // the connection died: onclose fires with no close() call
}
const liveSockets = () => sockets.filter((s) => !s.closed && s.readyState !== 3);

const listeners = [];
const elements = {};
function element(id) {
  if (!elements[id]) {
    const toggle = (_c, force) => { elements[id].hidden = !!force; };
    elements[id] = { hidden: true, textContent: '', classList: { toggle } };
  }
  return elements[id];
}
// The sidebar elements are exposed only after load, so app.js does not start its sidebar subscription.
const sidebarIds = new Set([
  'activity-dot', 'checkpoint-banner', 'handoff-unknown-banner', 'handoff-unreadable-banner',
]);
let exposeSidebar = false;
const tab = { hidden: false };
const docBase = absorb();
const doc = new Proxy(docBase, {
  get(t, p) {
    if (p === 'hidden') return tab.hidden;
    if (p === 'addEventListener') return (type, fn) => listeners.push([type, fn]);
    if (p === 'getElementById') {
      // No activity dot: app.js must not start the sidebar subscription on load.
      return (id) => {
        if (id.endsWith('-stale')) return element(id);
        if (sidebarIds.has(id)) return exposeSidebar ? element(id) : null;
        return absorb();
      };
    }
    return t[p];
  },
});
const setHidden = (hidden) => {
  tab.hidden = hidden;
  listeners.filter(([t]) => t === 'visibilitychange').forEach(([, fn]) => fn());
};

const ctx = {
  console: { log() {}, warn() {}, error() {} },
  Promise, JSON, Object, Array, Date: FakeDate, Math, Map, Set, URL, Headers, Response, Request,
  encodeURIComponent, decodeURIComponent,
  setTimeout: (fn, ms) => addTimer(fn, ms || 0, false),
  clearTimeout: (id) => clock.timers.delete(id),
  setInterval: (fn, ms) => addTimer(fn, ms, true),
  clearInterval: (id) => clock.timers.delete(id),
  requestAnimationFrame() {},
  localStorage: absorb(), sessionStorage: absorb(), navigator: absorb(),
  location: { protocol: 'http:', host: 'localhost:8111' },
  WebSocket: FakeWebSocket,
  EventSource: function () { return absorb(); },
  getComputedStyle: () => ({ getPropertyValue: () => '' }),
  fetch: () => new Promise(() => {}),
};
ctx.document = doc;
ctx.window = ctx;
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(appJsPath, 'utf8'), ctx);
// Drop any timer app.js itself started at load, so a scenario counts only its own.
clock.timers.clear();

function recorder() {
  const r = { opens: 0, polls: 0, messages: [] };
  r.options = {
    onOpen: () => { r.opens += 1; },
    onFallbackPoll: () => { r.polls += 1; },
    onMessage: (m) => r.messages.push(m.type),
  };
  return r;
}

const scenarios = {
  twoSubscribersShareOneSocketUntilTheLastLeaves() {
    const a = recorder(); const b = recorder();
    const subA = ctx.subscribeSovaSocket(a.options);
    const subB = ctx.subscribeSovaSocket(b.options);
    sockets[0].open();
    sockets[0].push({ type: 'status_update' });
    const constructed = sockets.length;
    subA.unsubscribe();
    const openAfterFirstLeaves = liveSockets().length;
    subB.unsubscribe();
    const openAfterLastLeaves = liveSockets().length;
    return { constructed, delivered: [a.messages, b.messages], openAfterFirstLeaves, openAfterLastLeaves };
  },

  joiningAnOpenSocketRunsTheCatchUp() {
    ctx.subscribeSovaSocket(recorder().options);
    sockets[0].open();
    const late = recorder();
    ctx.subscribeSovaSocket(late.options);
    advance(0);
    return { opens: late.opens, constructed: sockets.length };
  },

  aDropStartsTheFallbackAtOnceAndAReconnectStopsIt() {
    const r = recorder();
    ctx.subscribeSovaSocket(r.options);
    sockets[0].open();
    sockets[0].drop();
    const pollsAtDrop = r.polls;
    advance(30000 * 2);
    const pollsWhileDown = r.polls;
    // The first reconnect fires after 1s, but this fake never opens it: still down, so polling continues.
    const reconnectAttempts = sockets.length;
    sockets[sockets.length - 1].open();
    const opensAfterReconnect = r.opens;
    // A live socket delivers every 2s; pushing keeps the liveness watchdog from closing this one.
    for (let i = 0; i < 6; i++) { advance(10000); sockets[sockets.length - 1].push({ type: 'status_update' }); }
    return { pollsAtDrop, pollsWhileDown, reconnectAttempts, opensAfterReconnect, pollsAfterReconnect: r.polls };
  },

  hidingClosesTheSocketAndCancelsAPendingReconnect() {
    const r = recorder();
    ctx.subscribeSovaSocket(r.options);
    sockets[0].open();
    sockets[0].drop();
    setHidden(true);
    advance(60000);
    return { constructedWhileHidden: sockets.length, pollsWhileHidden: r.polls };
  },

  rapidHideAndShowNeverHoldsTwoSockets() {
    const r = recorder();
    ctx.subscribeSovaSocket(r.options);
    sockets[0].open();
    // advance(0) delivers the old socket's late onclose after the replacement exists: it must change nothing.
    for (let i = 0; i < 5; i++) { setHidden(true); setHidden(false); advance(0); }
    advance(10000);   // past the reconnect backoff, inside the watchdog window
    const current = !sockets[sockets.length - 1].closed;
    return { live: liveSockets().length, constructed: sockets.length, polls: r.polls, current };
  },

  returningToTheTabReconnectsAndCatchesUp() {
    const r = recorder();
    ctx.subscribeSovaSocket(r.options);
    sockets[0].open();
    setHidden(true);
    const liveWhileHidden = liveSockets().length;
    setHidden(false);
    advance(0);
    sockets[sockets.length - 1].open();
    advance(0);
    return { liveWhileHidden, opens: r.opens, live: liveSockets().length, polls: r.polls, constructed: sockets.length };
  },

  aSilentSocketIsClosedAndReopened() {
    ctx.subscribeSovaSocket(recorder().options);
    sockets[0].open();
    advance(19000);
    const closedAt19s = sockets[0].closed;
    advance(7000);
    return { closedAt19s, firstClosed: sockets[0].closed, constructed: sockets.length };
  },

  aFailedRefreshShowsTheAgeAtOnceAndASuccessClearsIt() {
    ctx.markRefresh('work-items', true, 90000);
    advance(60000);
    const beforeFailure = element('work-items-stale').hidden;
    ctx.markRefresh('work-items', false, 90000);
    const early = { hidden: element('work-items-stale').hidden, text: element('work-items-stale').textContent };
    advance(35000);
    ctx.markRefresh('work-items', false, 90000);
    const late = { hidden: element('work-items-stale').hidden, text: element('work-items-stale').textContent };
    ctx.markRefresh('work-items', true, 90000);
    return { beforeFailure, early, late, afterRecovery: element('work-items-stale').hidden };
  },

  silenceAloneShowsTheMarkerOnlyAfterTheThreshold() {
    ctx.markRefresh('work-items', true, 90000);
    advance(60000);
    const early = element('work-items-stale').hidden;
    advance(35000);
    return { early, late: element('work-items-stale').hidden };
  },

  aChannelThatNeverSucceededShowsNoMarker() {
    ctx.markRefresh('kanban', false, 1000);
    advance(600000);
    ctx.markRefresh('kanban', false, 1000);
    return { hidden: element('kanban-stale').hidden };
  },

  socketMessagesKeepAFreshnessKeyCurrentButNotWhileTheServerFetchFails() {
    ctx.subscribeSovaSocket({ freshnessKey: 'kanban', onMessage() {} });
    sockets[0].open();
    ctx.markRefresh('kanban', true, 60000);
    const beat = (type, n) => { for (let i = 0; i < n; i++) { advance(10000); sockets[0].push({ type }); } };
    beat('status_update', 10);
    const afterLiveMessages = element('kanban-stale').hidden;
    beat('status_unavailable', 7);
    return { afterLiveMessages, afterUnavailable: element('kanban-stale').hidden };
  },

  otherMessageTypesNeverFreshenAStatusKeyDuringAnOutage() {
    ctx.subscribeSovaSocket({ freshnessKey: 'agent-cards', onMessage() {} });
    sockets[0].open();
    ctx.markRefresh('agent-cards', true, 60000);
    // The server's status fetch keeps failing while feed events, activity changes and *_unavailable messages flow.
    const types = ['feed', 'status_unavailable', 'activity', 'handoff_unavailable', 'work_items_changed'];
    for (let i = 0; i < 7; i++) { advance(10000); types.forEach((type) => sockets[0].push({ type })); }
    const marker = element('agent-cards-stale');
    const duringOutage = { hidden: marker.hidden, text: marker.textContent };
    advance(10000);
    sockets[0].push({ type: 'status_update' });
    return { duringOutage, afterStatus: element('agent-cards-stale').hidden };
  },

  aCustomFreshnessTypeListIsHonoured() {
    ctx.subscribeSovaSocket({ freshnessKey: 'work-items', freshnessTypes: ['work_items_changed'], onMessage() {} });
    sockets[0].open();
    ctx.markRefresh('work-items', true, 60000);
    for (let i = 0; i < 7; i++) { advance(10000); sockets[0].push({ type: 'status_update' }); }
    const ignoredStatus = element('work-items-stale').hidden;
    sockets[0].push({ type: 'work_items_changed' });
    return { ignoredStatus, afterOwnType: element('work-items-stale').hidden };
  },

  silenceWhileTheTabIsHiddenIsNotAFailure() {
    ctx.markRefresh('work-items', true, 90000);
    setHidden(true);
    advance(300000);
    const whileHidden = element('work-items-stale').hidden;
    setHidden(false);
    ctx.markRefresh('work-items', true, 90000);
    return { whileHidden, afterReturn: element('work-items-stale').hidden };
  },

  anAwaitingHandoffKeepsTheDotThroughAnActivityChange() {
    exposeSidebar = true;
    const dot = () => element('activity-dot').className;
    ctx._applyActivity({ agents: [] });
    ctx._applyHandoff({ handoffs: [{ status: 'awaiting_action' }] });
    const awaiting = dot();
    ctx._applyActivity({ agents: [{ run_id: 1 }] });
    const afterActivity = dot();
    ctx._applyHandoff({ handoffs: [] });
    return {
      awaiting: awaiting.includes('bg-accent-yellow'),
      afterActivity: afterActivity.includes('bg-accent-yellow'),
      afterClear: dot().includes(' bg-accent ') && !dot().includes('bg-accent-yellow'),
      bannerHidden: element('checkpoint-banner').hidden,
    };
  },

  async aHandoffUnavailableMessageMarksTheStateUnknownWithoutRefetching() {
    exposeSidebar = true;
    const dot = () => element('activity-dot').className;
    const requested = [];
    ctx.fetch = (url) => {
      requested.push(url);
      return Promise.resolve({ ok: true, json: async () => ({ handoffs: [] }) });
    };
    ctx._applyActivity({ agents: [] });
    ctx._applyHandoff({ handoffs: [{ status: 'awaiting_action' }], unreadable_handoffs: ['/x/handoff-1.json'] });
    const awaiting = dot().includes('bg-accent-yellow');
    element('handoff-unreadable-banner').hidden = false;
    ctx._handleSidebarMessage({ type: 'handoff_unavailable' });
    advance(10000);
    await new Promise((resolve) => setImmediate(resolve));
    return {
      awaiting,
      requests: requested.length,
      // The activity push still says no agent is running: the dot keeps that, the unknown banner names the handoff.
      unknownDot: dot().includes('bg-accent-green') && !dot().includes('bg-accent-yellow'),
      checkpointHidden: element('checkpoint-banner').hidden,
      unreadableHidden: element('handoff-unreadable-banner').hidden,
      unknownBannerShown: !element('handoff-unknown-banner').hidden,
    };
  },

  async aCatchUpAnswerThatLandsAfterANewerPushIsDropped() {
    exposeSidebar = true;
    const resolvers = {};
    ctx.fetch = (url) => new Promise((resolve) => {
      resolvers[url.includes('/handoff') ? 'handoff' : 'active'] = resolve;
    });
    const dot = () => element('activity-dot').className;
    ctx._catchUpSidebar();
    // A newer state arrives over the socket while both HTTP answers are still in flight.
    ctx._handleSidebarMessage({ type: 'activity', data: { agents: [{ run_id: 1 }] } });
    ctx._handleSidebarMessage({ type: 'handoff', data: { handoffs: [{ status: 'awaiting_action' }] } });
    resolvers.active({ ok: true, json: async () => ({ agents: [] }) });
    resolvers.handoff({ ok: true, json: async () => ({ handoffs: [] }) });
    await new Promise((resolve) => setImmediate(resolve));
    await new Promise((resolve) => setImmediate(resolve));
    const held = ctx.window._sovaActiveAgents.data.agents.length;
    return { keptPush: dot().includes('bg-accent-yellow'), activityKept: held === 1 };
  },

  statusTicksRefreshTheHeldActivityTimestampUnlessItsLastAnswerFailed() {
    exposeSidebar = true;
    ctx._applyActivity({ agents: [] });
    const first = ctx.window._sovaActiveAgents.ts;
    advance(30000);
    ctx._handleSidebarMessage({ type: 'status_update', runs: [] });
    const refreshed = ctx.window._sovaActiveAgents.ts - first;
    ctx._handleSidebarMessage({ type: 'activity_unavailable' });
    const stamp = ctx.window._sovaActiveAgents.ts;
    advance(30000);
    ctx._handleSidebarMessage({ type: 'status_update', runs: [] });
    return { refreshed, unchangedAfterFailure: ctx.window._sovaActiveAgents.ts === stamp };
  },

  anUnknownHandoffDoesNotHideARunningAgentAndRecovers() {
    exposeSidebar = true;
    const dot = () => element('activity-dot').className;
    ctx._applyActivity({ agents: [{ run_id: 1 }] });
    ctx._setHandoffUnknown();
    const runningWhileUnknown = dot().includes(' bg-accent ');
    ctx._setActivityDot(null);
    const grayWhenActivityUnknown = dot().includes('bg-gray-500');
    ctx._applyActivity({ agents: [] });
    ctx._applyHandoff({ handoffs: [] });
    return {
      runningWhileUnknown,
      grayWhenActivityUnknown,
      recovered: dot().includes('bg-accent-green'),
      unknownBannerHidden: element('handoff-unknown-banner').hidden,
    };
  },

  theSidebarPollsNothingWhileTheSocketIsUpAndFallsBackEvery30sWhenItDrops() {
    const requested = [];
    ctx.fetch = (url) => { requested.push(url); return new Promise(() => {}); };
    const count = (needle) => requested.filter((u) => u.includes(needle)).length;
    ctx._initSidebarSocket();
    sockets[0].open();
    advance(0);
    const catchUp = { active: count('/agents/active'), handoff: count('/handoff') };
    for (let i = 0; i < 6; i++) { advance(10000); sockets[0].push({ type: 'status_update' }); }
    const whileUp = { active: count('/agents/active'), handoff: count('/handoff') };
    sockets[0].drop();
    advance(60000);
    const afterDrop = { active: count('/agents/active'), handoff: count('/handoff') };
    return { catchUp, whileUp, afterDrop };
  },

  visibilityAwarePollCanSkipItsImmediateRuns() {
    let skipped = 0; let immediate = 0;
    ctx.visibilityAwarePoll(() => { skipped += 1; }, 1000, { skipImmediate: true });
    ctx.visibilityAwarePoll(() => { immediate += 1; }, 1000);
    const atStart = { skipped, immediate };
    setHidden(true); setHidden(false);
    const afterReturn = { skipped, immediate };
    advance(1000);
    return { atStart, afterReturn, afterInterval: { skipped, immediate } };
  },

  badge() {
    const label = (...a) => ctx._computeBadgeLabel(...a);
    return {
      stepless: label('running', null, 'command:review-pr', undefined, 125),
      steplessAgentStep: label('running', 'agent', 'developer', undefined, 5),
      noStartTime: label('running', null, 'command:review-pr', undefined, null),
      // renderStatusBadge: the elapsed time is a ticking span next to the label, not text baked into it.
      rendered: ctx.renderStatusBadge('running', null, 0, 0, 125, null, 'command:review-pr'),
      renderedNoElapsed: ctx.renderStatusBadge('running', null, 0, 0, null, null, 'command:review-pr'),
      renderedStepped: ctx.renderStatusBadge('running', 'develop', 1, 5, 125, null, 'developer'),
      renderedPending: ctx.renderStatusBadge('pending', null, 0, 0, 5, null, 'developer'),
      noRole: label('running', null, undefined, undefined, 12),
      withStep: label('running', 'develop', 'developer', undefined, 125),
      terminal: label('done', null, 'developer', undefined, 125),
      pending: label('pending', null, 'developer', undefined, 5),
      pendingAgentStep: label('pending', 'agent', 'developer', undefined, 5),
    };
  },
};

Promise.resolve(scenarios[scenarioName]()).then((out) => process.stdout.write(JSON.stringify(out)));
"""


def _run(name: str) -> dict:
    result = subprocess.run(
        ["node", "-e", _HARNESS, str(APP_JS), name],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


class TestSharedSocket:
    def test_subscribers_share_one_socket_until_the_last_one_leaves(self) -> None:
        out = _run("twoSubscribersShareOneSocketUntilTheLastLeaves")
        assert out["constructed"] == 1
        assert out["delivered"] == [["status_update"], ["status_update"]]
        assert out["openAfterFirstLeaves"] == 1
        assert out["openAfterLastLeaves"] == 0

    def test_joining_an_open_socket_runs_the_catch_up_without_a_second_socket(self) -> None:
        assert _run("joiningAnOpenSocketRunsTheCatchUp") == {"opens": 1, "constructed": 1}

    def test_a_silent_socket_is_closed_and_reopened(self) -> None:
        out = _run("aSilentSocketIsClosedAndReopened")
        # Silent for 20s is dead; the 5s check closes it at 25s, not up to 40s later.
        assert out["closedAt19s"] is False
        assert out["firstClosed"] is True
        assert out["constructed"] >= 2


class TestFallbackPolling:
    def test_a_drop_polls_at_once_then_every_30s_and_a_reconnect_stops_it(self) -> None:
        out = _run("aDropStartsTheFallbackAtOnceAndAReconnectStopsIt")
        assert out["pollsAtDrop"] == 1
        assert out["pollsWhileDown"] == 3
        assert out["opensAfterReconnect"] == 2
        assert out["pollsAfterReconnect"] == out["pollsWhileDown"]


class TestTabVisibility:
    def test_hiding_closes_the_socket_and_cancels_a_pending_reconnect_and_the_polls(self) -> None:
        out = _run("hidingClosesTheSocketAndCancelsAPendingReconnect")
        assert out == {"constructedWhileHidden": 1, "pollsWhileHidden": 1}

    def test_rapid_hide_and_show_never_holds_two_sockets(self) -> None:
        out = _run("rapidHideAndShowNeverHoldsTwoSockets")
        assert out["live"] == 1
        assert out["current"] is True, "a late onclose of a closed socket must not clear the current one"
        assert out["polls"] == 0, "a late onclose of a closed socket must not start the fallback poll"
        assert out["constructed"] == 6, "no reconnect may be scheduled by a late onclose"

    def test_returning_reconnects_and_runs_the_catch_up(self) -> None:
        out = _run("returningToTheTabReconnectsAndCatchesUp")
        assert out == {"liveWhileHidden": 0, "opens": 2, "live": 1, "polls": 0, "constructed": 2}


class TestStaleMarker:
    def test_a_failed_refresh_shows_the_age_at_once_and_a_success_clears_it(self) -> None:
        out = _run("aFailedRefreshShowsTheAgeAtOnceAndASuccessClearsIt")
        assert out["beforeFailure"] is True
        assert out["early"] == {"hidden": False, "text": "Updated 1m 0s ago, retrying"}
        assert out["late"] == {"hidden": False, "text": "Updated 1m 35s ago, retrying"}
        assert out["afterRecovery"] is True

    def test_silence_alone_shows_the_marker_only_after_the_threshold(self) -> None:
        assert _run("silenceAloneShowsTheMarkerOnlyAfterTheThreshold") == {"early": True, "late": False}

    def test_a_channel_that_never_succeeded_shows_nothing(self) -> None:
        assert _run("aChannelThatNeverSucceededShowsNoMarker") == {"hidden": True}

    def test_a_status_unavailable_message_does_not_count_as_fresh(self) -> None:
        out = _run("socketMessagesKeepAFreshnessKeyCurrentButNotWhileTheServerFetchFails")
        assert out == {"afterLiveMessages": True, "afterUnavailable": False}

    def test_only_the_subscribers_own_message_type_freshens_its_key_during_an_outage(self) -> None:
        out = _run("otherMessageTypesNeverFreshenAStatusKeyDuringAnOutage")
        # Feed, activity, *_unavailable and change messages interleaved with status_unavailable keep the age growing.
        assert out["duringOutage"] == {"hidden": False, "text": "Updated 1m 10s ago, retrying"}
        assert out["afterStatus"] is True

    def test_a_subscriber_can_name_its_own_freshness_types(self) -> None:
        assert _run("aCustomFreshnessTypeListIsHonoured") == {"ignoredStatus": False, "afterOwnType": True}

    def test_silence_while_the_tab_is_hidden_is_not_a_failure(self) -> None:
        assert _run("silenceWhileTheTabIsHiddenIsNotAFailure") == {"whileHidden": True, "afterReturn": True}


class TestSidebarDot:
    def test_an_awaiting_handoff_is_not_painted_over_by_an_activity_change(self) -> None:
        out = _run("anAwaitingHandoffKeepsTheDotThroughAnActivityChange")
        assert out == {"awaiting": True, "afterActivity": True, "afterClear": True, "bannerHidden": True}

    def test_a_handoff_unavailable_message_marks_the_state_unknown_without_refetching(self) -> None:
        out = _run("aHandoffUnavailableMessageMarksTheStateUnknownWithoutRefetching")
        # No request: every open tab refetching would hit the failing dependency at once, and the answer could come
        # from the server's last good result. The unreadable-files banner must not stay up beside the unknown one.
        assert out == {
            "awaiting": True,
            "requests": 0,
            "unknownDot": True,
            "checkpointHidden": True,
            "unreadableHidden": True,
            "unknownBannerShown": True,
        }

    def test_a_catch_up_answer_that_lands_after_a_newer_push_is_dropped(self) -> None:
        out = _run("aCatchUpAnswerThatLandsAfterANewerPushIsDropped")
        assert out == {"keptPush": True, "activityKept": True}

    def test_a_status_tick_refreshes_the_held_activity_timestamp_unless_its_last_answer_failed(self) -> None:
        out = _run("statusTicksRefreshTheHeldActivityTimestampUnlessItsLastAnswerFailed")
        assert out == {"refreshed": 30000, "unchangedAfterFailure": True}

    def test_an_unknown_handoff_does_not_hide_a_running_agent_and_recovers(self) -> None:
        assert _run("anUnknownHandoffDoesNotHideARunningAgentAndRecovers") == {
            "runningWhileUnknown": True,
            "grayWhenActivityUnknown": True,
            "recovered": True,
            "unknownBannerHidden": True,
        }

    def test_the_sidebar_polls_nothing_while_the_socket_is_up_and_every_30s_when_it_is_down(self) -> None:
        out = _run("theSidebarPollsNothingWhileTheSocketIsUpAndFallsBackEvery30sWhenItDrops")
        assert out["catchUp"] == {"active": 1, "handoff": 1}
        assert out["whileUp"] == out["catchUp"]
        # At the drop, then 30s and 60s later.
        assert out["afterDrop"] == {"active": 4, "handoff": 4}


class TestStepLessRunBadge:
    def test_it_names_the_action_and_keeps_step_labels(self) -> None:
        out = _run("badge")
        assert out["stepless"] == out["noStartTime"]
        assert not out["stepless"].startswith("Starting")
        assert not out["steplessAgentStep"].startswith("Starting")
        assert not any(ch.isdigit() for ch in out["stepless"])
        assert out["noRole"] == "Starting..."
        assert out["withStep"] == "Running: Developing"
        assert out["terminal"] == "Done"
        # A queued run has produced nothing, and a step-based pipeline has not reached its first step.
        assert out["pending"] == "Starting..."
        assert out["pendingAgentStep"] == "Starting..."

    def test_the_elapsed_time_is_a_ticking_span_beside_the_label(self) -> None:
        out = _run("badge")
        assert 'class="sova-status-elapsed"' in out["rendered"]
        assert out["stepless"] in out["rendered"]
        assert "sova-status-elapsed" not in out["renderedNoElapsed"]
        assert "sova-status-elapsed" not in out["renderedStepped"]
        assert "sova-status-elapsed" not in out["renderedPending"]


class TestVisibilityAwarePoll:
    def test_skip_immediate_defers_the_first_run_by_one_interval_and_ignores_tab_returns(self) -> None:
        out = _run("visibilityAwarePollCanSkipItsImmediateRuns")
        assert out["atStart"] == {"skipped": 0, "immediate": 1}
        assert out["afterReturn"] == {"skipped": 0, "immediate": 2}
        assert out["afterInterval"] == {"skipped": 1, "immediate": 3}
