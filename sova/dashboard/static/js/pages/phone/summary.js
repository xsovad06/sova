// Phone page, what the Work board knows: GET /work/board, the Work page's own source, read for two things.
//
// The glance tiles' agent count and limit go through the Work header's renderHeader() (work/header.js), so Home and
// the Work page show the same figures from the same payload. And every issue the board lists (any column, the hidden
// ones, the chains) gives its title, so a handoff or a running agent is named by what it is about: titleOf() looks
// one up, and onTitles() redraws a section when a newer board arrives.
//
// The board answers from the server's last build (snapshot_service), so a read never waits on GitHub after the first.
// A failed read shows the agent figures as unknown ('--'), never an earlier count as current, keeps the titles it
// already had (a title does not go stale the way a count does) and reads again on a timer, since an open socket with
// no change to push would otherwise leave the figures unknown.

import { renderHeader } from '../work/header.js';

var _RETRY_MS = 15000;

var _seq = 0;
var _retry = 0;        // the pending retry timer after a failed read, 0 for none
var _titles = {};      // '42' -> title, 'pr:7' -> title for a PR with no issue
var _listeners = [];

export function titleOf(issue, pr) {
  var number = issue == null ? '' : String(issue).replace(/^#/, '').trim();
  var key = number || (pr != null ? 'pr:' + pr : '');
  return key && Object.prototype.hasOwnProperty.call(_titles, key) ? _titles[key] : '';
}

export function onTitles(fn) { _listeners.push(fn); }

// Every entry in the payload's lists that carries a title: the columns, the hidden list and the runs no card shows,
// and the issues inside each chain (an open issue only a chain lists), all name the issue the same way.
function _index(board) {
  var titles = {};
  function add(entry) {
    if (!entry || typeof entry !== 'object' || !entry.title) return;
    if (entry.number != null) titles[String(entry.number)] = entry.title;
    else if (entry.pr_number != null) titles['pr:' + entry.pr_number] = entry.title;
  }
  Object.keys(board || {}).forEach(function(key) {
    if (Array.isArray(board[key])) board[key].forEach(add);
  });
  ((board && board.chains) || []).forEach(function(chain) {
    ((chain && chain.issues) || []).forEach(add);
  });
  return titles;
}

export async function loadSummary() {
  var seq = ++_seq;
  var board;
  try {
    board = await fetchAPI(apiUrl('/work/board'));
  } catch (e) {
    if (seq !== _seq) return;
    renderHeader(null);
    if (!_retry) _retry = setTimeout(function() { _retry = 0; loadSummary(); }, _RETRY_MS);
    return;
  }
  if (seq !== _seq) return;
  renderHeader(board);
  _titles = _index(board);
  _listeners.forEach(function(fn) {
    try {
      fn();
    } catch (err) {
      console.error('phone page: a title listener failed', err);
    }
  });
}
