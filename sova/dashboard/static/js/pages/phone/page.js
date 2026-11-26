// Phone page (Home) entry point: wires the sections and keeps them current.
//
// The glance tiles and Triage all are the Work header's own (work/header.js, the same element ids), fed with the
// board payload by phone/summary.js, which also names every issue by its title; the search is the Work page's
// (work/search.js, started through phone/start.js). The rest are this page's sections: Waiting on you, Running and
// Finished today.
//
// The shared status socket (app.js subscribeSovaSocket) says when something changed; the HTTP endpoints stay the
// source of every payload, so a message only triggers a reload. The socket's catch-up fetch (onOpen) and its
// fallback poll (which runs only while the socket is down) reload everything, so nothing waits on a push.
//
// Some things change with no message: the supervisor's pending plan has no socket message of its own, and a running
// agent's elapsed time and cost are kept out of the socket's change digest. So the page also reloads those two
// sections on a timer while it is visible. "Finished today" follows the local date the same way: the day turning over
// under an open page sends nothing, so the timer reloads it when the date changes.

import { init as initActions } from '../agents/actions.js';
import { init as initHeader } from '../work/header.js';
import { init as initWaiting, loadWaiting } from './waiting.js';
import { init as initRunning, loadRunning } from './running.js';
import { init as initStart } from './start.js';
import { loadFinished } from './finished.js';
import { loadSummary } from './summary.js';

var _REFRESH_MS = 15000;
var _day = '';   // the local date "Finished today" was last read for

// What each socket message type reloads. Unavailable messages reload too: the fetch then shows the section's error.
// The board summary follows the server's rebuilt board (work_items_changed), which a run starting or ending also sends.
var _RELOAD_ON = {
  handoff: [loadWaiting],
  handoff_unavailable: [loadWaiting],
  activity: [loadRunning],
  activity_unavailable: [loadRunning],
  actions_changed: [loadWaiting, loadRunning],
  work_items_changed: [loadWaiting, loadSummary],
  agent_finished: [loadFinished],
};

function _reloadAll() {
  _day = new Date().toDateString();
  loadSummary();
  loadWaiting();
  loadRunning();
  loadFinished();
}

function _onMessage(msg) {
  var loaders = Object.prototype.hasOwnProperty.call(_RELOAD_ON, msg && msg.type) ? _RELOAD_ON[msg.type] : [];
  loaders.forEach(function(load) { load(); });
}

export function init() {
  initActions();
  // Each init runs on its own: the header reaches into app.js's setting controls, and one module throwing must not
  // leave the sections after it unwired.
  [initHeader, initWaiting, initRunning, initStart].forEach(function(fn) {
    try {
      fn();
    } catch (e) {
      console.error('phone page: an init failed', e);
    }
  });
  subscribeSovaSocket({ onMessage: _onMessage, onOpen: _reloadAll, onFallbackPoll: _reloadAll });
  setInterval(function() {
    if (!document.hidden) {
      loadWaiting();
      loadRunning();
      var today = new Date().toDateString();
      if (today !== _day) {
        _day = today;
        loadFinished();
      }
    }
  }, _REFRESH_MS);
  _reloadAll();
}
