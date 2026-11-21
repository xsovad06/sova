// Phone page entry point: wires the three sections and keeps them current.
//
// The shared status socket (app.js subscribeSovaSocket) says when something changed; the HTTP endpoints stay the
// source of every payload, so a message only triggers a reload. The socket's catch-up fetch (onOpen) and its
// fallback poll (which runs only while the socket is down) reload everything, so nothing waits on a push.
//
// Some things change with no message: the supervisor's pending plan has no socket message of its own, and a running
// agent's elapsed time is kept out of the socket's change digest. So the page also reloads both sections on a timer
// while it is visible.

import { init as initActions } from '../agents/actions.js';
import { init as initWaiting, loadWaiting } from './waiting.js';
import { init as initRunning, loadRunning } from './running.js';
import { init as initStart } from './start.js';

var _REFRESH_MS = 15000;

// What each socket message type reloads. Unavailable messages reload too: the fetch then shows the section's error.
var _RELOAD_ON = {
  handoff: [loadWaiting],
  handoff_unavailable: [loadWaiting],
  activity: [loadRunning],
  activity_unavailable: [loadRunning],
  actions_changed: [loadWaiting, loadRunning],
  work_items_changed: [loadWaiting],
};

function _reloadAll() {
  loadWaiting();
  loadRunning();
}

function _onMessage(msg) {
  var loaders = Object.prototype.hasOwnProperty.call(_RELOAD_ON, msg && msg.type) ? _RELOAD_ON[msg.type] : [];
  loaders.forEach(function(load) { load(); });
}

export function init() {
  initActions();
  initWaiting();
  initRunning();
  initStart();
  subscribeSovaSocket({ onMessage: _onMessage, onOpen: _reloadAll, onFallbackPoll: _reloadAll });
  setInterval(function() { if (!document.hidden) _reloadAll(); }, _REFRESH_MS);
  _reloadAll();
}
