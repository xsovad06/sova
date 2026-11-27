// The activity feed panel's content: the server's narration of today, what needs the user, and the past periods.
//
// GET /feed/activity builds the whole view (every sentence included), so this module only draws it. A `feed`
// message on the shared status socket says something changed: its event body is ignored and the payload is
// fetched again, at most once a second. Nothing here keeps a state or colour table: each line carries the colour
// token the server chose, and the lines are drawn through app.js's stateHex().
//
// The badge on the floating button counts attention lines the user has not seen. A line's identity is its kind, its
// issue, PR or run key and the run it is about (the server builds it), so a new failure is a new line. Opening the
// panel marks every current line seen. The seen set is kept per project, with the time each identity was last seen:
// a refresh never drops one (a line can be briefly missing from a payload), and one whose line has been absent from
// the payload for a day is forgotten when the payload is next read, so the same problem coming back much later counts
// as new. A line that stays present stays seen, however long no tab was open. Two tabs on one project share the stored set:
// a write merges what is stored first (newest time per identity), and a `storage` event from the other tab does too.
//
// The server words "12 min in" and "now", so the client keeps them true by asking again: on a `feed` or
// `agent_finished` message, once a minute while the panel is open on a running line, and at the next local midnight
// (when today becomes yesterday).

var _REFETCH_MS = 1000;
var _SEEN_KEY_PREFIX = 'sova.feed.seen:';
var _SEEN_KEEP_MS = 24 * 60 * 60 * 1000;
var _SEEN_REFRESH_MS = 60 * 60 * 1000;   // a still-current identity's time is rewritten at most this often
var _RUNNING_REFRESH_MS = 60 * 1000;     // a running line's elapsed time is asked again this often while the panel is open
var _MIDNIGHT_SLACK_MS = 1000;           // the midnight refetch waits this long past the day edge

var _FALLBACK_MIN_MS = 30 * 1000;        // a closed panel is rebuilt at most this often by a fallback poll

var _payload = null;      // the last good payload; null before the first one
var _lastFetchAt = 0;     // when the last good payload arrived (ms since the epoch)
var _failed = false;      // the latest fetch failed
var _open = false;
var _seen = {};           // identity -> when it was last seen (ms since the epoch)
var _seq = 0;             // a response older than a newer request is dropped
var _timer = 0;           // the pending refetch, 0 for none
var _ticker = 0;          // the pending once-a-minute refetch for a running line, 0 for none
var _midnight = 0;        // the pending refetch at the next local midnight, 0 for none

// The browser's IANA zone ("Europe/Bratislava"); empty when it cannot say, which the server reads as UTC.
function _zone() {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || '';
  } catch (_e) {
    return '';
  }
}

// The Work page, its run history narrowed by `params` (`number`, or a `from`/`to` date range) when given.
function _workUrl(params) {
  var slug = window.SOVA_PROJECT_SLUG || '';
  var query = Object.keys(params || {}).filter(function(k) { return params[k]; }).map(function(k) {
    return k + '=' + encodeURIComponent(params[k]);
  }).join('&');
  return (slug ? '/p/' + slug : '') + '/work' + (query ? '?' + query : '');
}

// The action-state key of an attention line's button (runTrackedAction shows its running state and result). It is the
// key the Work card of the same issue (or PR) uses (app.js actionKey), so both show one running state.
function _actionKey(ref) {
  var pr = /^pr:(.+)$/.exec(ref);
  return pr ? actionKey('', pr[1]) : actionKey(ref, '');
}

// Identities are only unique within a project, so each project has its own set.
function _seenKey() {
  return _SEEN_KEY_PREFIX + (window.SOVA_PROJECT_SLUG || '_');
}

// The stored marks. None is dropped by age here: a line that never went away stays seen however long the browser was
// closed. Only a mark whose line is absent from the payload being synced expires (_syncSeen), after a day.
function _readStoredSeen() {
  var found = {};
  try {
    var stored = JSON.parse(safeStorageGet(_seenKey()) || '{}');
    if (stored && typeof stored === 'object' && !Array.isArray(stored)) {
      Object.keys(stored).forEach(function(id) {
        if (typeof stored[id] === 'number') found[id] = stored[id];
      });
    }
  } catch (_e) { /* an unreadable set means nothing is seen yet */ }
  return found;
}

function _loadSeen() {
  _seen = _readStoredSeen();
}

// Fold the stored set (another tab may have written it since) into the one in memory, keeping the newest time per
// identity (a mark _syncSeen forgot is gone from the store, so it stays forgotten).
function _mergeStoredSeen() {
  var stored = _readStoredSeen();
  Object.keys(stored).forEach(function(id) {
    if (_seen[id] === undefined || stored[id] > _seen[id]) _seen[id] = stored[id];
  });
}

function _saveSeen() {
  safeStorageSet(_seenKey(), JSON.stringify(_seen));
}

function _identities() {
  return ((_payload && _payload.attention) || []).map(function(item) { return item.identity; });
}

export function unreadCount() {
  return _identities().filter(function(id) { return _seen[id] === undefined; }).length;
}

// With `markAll`, mark every current identity seen. An identity absent from the payload keeps its mark for a day.
// Nothing runs before the first good payload, so an early call cannot write an empty set.
function _syncSeen(markAll) {
  if (!_payload) return;
  _mergeStoredSeen();
  var now = Date.now();
  var changed = false;
  _identities().forEach(function(id) {
    var at = _seen[id];
    if (at === undefined ? markAll : (markAll || now - at > _SEEN_REFRESH_MS)) {
      _seen[id] = now;
      changed = true;
    }
  });
  var current = {};
  _identities().forEach(function(id) { current[id] = true; });
  Object.keys(_seen).forEach(function(id) {
    if (!current[id] && now - _seen[id] > _SEEN_KEEP_MS) {
      delete _seen[id];
      changed = true;
    }
  });
  if (changed) _saveSeen();
}

function _updateBadge() {
  var badge = document.getElementById('feed-badge');
  var fab = document.getElementById('feed-fab');
  if (!badge) return;
  var unread = _open ? 0 : unreadCount();
  if (unread > 0) {
    badge.textContent = unread > 9 ? '9+' : String(unread);
    badge.classList.remove('hidden');
    // The wording is the server's (rendered into the button by base.html); only the count is filled in.
    var unreadLabel = fab && fab.getAttribute('data-label-unread');
    if (unreadLabel) fab.setAttribute('aria-label', unreadLabel.replace('{count}', String(unread)));
  } else {
    badge.classList.add('hidden');
    var label = fab && fab.getAttribute('data-label');
    if (label) fab.setAttribute('aria-label', label);
  }
}

function _refLabel(line) {
  if (line.issue_number) return /^\d+$/.test(String(line.issue_number)) ? '#' + line.issue_number : String(line.issue_number);
  return line.pr_number ? 'PR #' + line.pr_number : '';
}

function _dot(token) {
  return '<span class="feed-line-dot" style="background:' + escapeHtml(stateHex(token)) + '" aria-hidden="true"></span>';
}

function _heading(text, extra) {
  return '<div class="feed-section-label">' + escapeHtml(text) + (extra || '') + '</div>';
}

function _lineHead(line) {
  var ref = _refLabel(line);
  if (!ref && !line.title) return '';
  var label = ref + (line.title ? (ref ? ' ' : '') + line.title : '');
  return '<a class="feed-line-ref" href="' + escapeHtml(_workUrl({ number: line.number })) + '">' + escapeHtml(label) + '</a>';
}

// The Work board card of an issue (or a PR with no issue), which opens its details; the run history when there is no card.
function _cardUrl(item) {
  if (item.issue_number && /^\d+$/.test(String(item.issue_number))) return _workUrl({ card: item.issue_number });
  if (!item.issue_number && item.pr_number) return _workUrl({ card: 'pr:' + item.pr_number });
  return _workUrl({ number: item.number });
}

// "Ask about this": opens a conversation bound to the line's issue (js/feed/conversations.js handles the click).
// The key pattern is the server's (normalize_issue_key): an issue number or a tracker key such as PROJ-123.
var _ASK_KEY_RE = /^[A-Za-z0-9][A-Za-z0-9_-]{0,49}$/;
function _askHtml(item, labels) {
  if (!item.issue_number || !_ASK_KEY_RE.test(String(item.issue_number)) || !labels.ask_about) return '';
  return '<button type="button" class="feed-line-ask" data-feed-ask="' + escapeHtml(String(item.issue_number)) + '">' +
    escapeHtml(labels.ask_about) + '</button>';
}

function _actionHtml(item, labels) {
  if (item.action) {
    var a = item.action;
    var key = _actionKey(a.ref);
    var state = actionStateFor(key);
    return '<div class="feed-line-actions">' + actionStatusHtml(key) + _askHtml(item, labels) + sovaBtn(a.label, {
      variant: 'secondary', size: 'sm', disabled: !!(state && state.status === 'running'),
      attrs: {
        'data-feed-action': a.id, 'data-feed-ref': a.ref, 'data-feed-label': a.label,
        'data-feed-confirm': a.confirm ? '1' : '0', 'data-feed-confirm-text': a.confirm_text || '',
        'data-feed-started-text': a.started_text || '', 'data-feed-failed-text': a.failed_text || ''
      }
    }) + '</div>';
  }
  // No action to run: point at the card where the user can decide.
  return '<div class="feed-line-actions"><a class="feed-line-open" href="' + escapeHtml(_cardUrl(item)) +
    '">' + escapeHtml(labels.open_in_work || '') + '</a>' + _askHtml(item, labels) + '</div>';
}

function _cost(value) {
  var amount = parseFloat(value);
  return amount > 0 ? '<span class="feed-line-cost">$' + escapeHtml(amount.toFixed(2)) + '</span>' : '';
}

function _todayLine(entry, labels) {
  var ask = _askHtml(entry, labels || {});
  return '<div class="feed-line' + (entry.running ? ' feed-line-running' : '') + '">' + _dot(entry.state_color) +
    '<div class="feed-line-main">' + _lineHead(entry) +
    '<div class="feed-line-text">' + escapeHtml(entry.text) + '</div>' +
    (ask ? '<div class="feed-line-actions">' + ask + '</div>' : '') + '</div>' + _cost(entry.cost_usd) + '</div>';
}

// A past period opens the run history on its dates (local, both inclusive, as the history's custom range takes).
function _periodLine(p) {
  var text = escapeHtml(p.text);
  if (!p.from || !p.to) return '<div class="feed-period">' + text + '</div>';
  return '<a class="feed-period" href="' + escapeHtml(_workUrl({ from: p.from, to: p.to })) + '">' + text + '</a>';
}

function _attentionLine(item, labels) {
  return '<div class="feed-line feed-line-attention">' + _dot(item.color) +
    '<div class="feed-line-main">' + _lineHead(item) +
    '<div class="feed-line-text">' + escapeHtml(item.text) + '</div>' + _actionHtml(item, labels) + '</div></div>';
}

function _backgroundFold(background, summary) {
  if (!background.length) return '';
  return '<details class="feed-fold"><summary>' + escapeHtml(summary || '') +
    '</summary>' + background.map(function(b) {
      return '<div class="feed-fold-line">' + escapeHtml(b.text) + '</div>';
    }).join('') + '</details>';
}

// The whole panel body for one payload, every interpolation escaped. Pure, so a test can call it.
export function activityHtml(data) {
  var parts = [];
  var attention = data.attention || [];
  var today = data.today || [];
  var periods = data.periods || [];
  var background = data.background || [];
  // Every word comes from the server's narration (feed_narration.py), none is written here.
  var labels = data.labels || {};
  if (!attention.length && !today.length && !background.length && !periods.length) {
    return '<p class="feed-empty">' + escapeHtml(labels.no_activity || '') + '</p>';
  }
  if (attention.length) {
    parts.push(_heading(labels.attention || '') + attention.map(function(item) { return _attentionLine(item, labels); }).join(''));
  }
  if (today.length) {
    parts.push(_heading(labels.today || '') + today.map(function(entry) { return _todayLine(entry, labels); }).join(''));
  } else {
    parts.push(_heading(labels.today || '') + '<p class="feed-quiet">' + escapeHtml(data.empty_today_text || '') + '</p>');
  }
  parts.push(_backgroundFold(background, data.background_summary));
  if (periods.length) {
    parts.push(_heading(labels.earlier || '') + periods.map(_periodLine).join(''));
  }
  return parts.join('');
}

// What the user is in the middle of in the panel: the background fold they opened and the control with focus. A render
// replaces the markup, so both are put back after it (it runs on every refresh, at least once a minute while a line runs).
function _uiState(list) {
  var fold = list.querySelector ? list.querySelector('.feed-fold') : null;
  var active = document.activeElement;
  var focus = null;
  if (active && list.contains && list.contains(active) && active.getAttribute) {
    focus = { action: active.getAttribute('data-feed-action'), ref: active.getAttribute('data-feed-ref'),
              href: active.getAttribute('href') };
  }
  return { foldOpen: !!(fold && fold.open), focus: focus };
}

function _restoreUiState(list, state) {
  if (!list.querySelector) return;
  var fold = list.querySelector('.feed-fold');
  if (fold && state.foldOpen) fold.open = true;
  var want = state.focus;
  if (!want || !list.querySelectorAll) return;
  var match = Array.prototype.find.call(list.querySelectorAll('[data-feed-action], a[href]'), function(node) {
    if (want.action) return node.getAttribute('data-feed-action') === want.action && node.getAttribute('data-feed-ref') === want.ref;
    return want.href && node.getAttribute('href') === want.href;
  });
  if (match && match.focus) match.focus();
}

function _render() {
  var list = document.getElementById('feed-activity');
  if (!list) return;
  var state = _payload ? _uiState(list) : null;
  if (_payload) {
    list.innerHTML = activityHtml(_payload);
    _restoreUiState(list, state);
  } else if (_failed) {
    // The wording is the server's: base.html renders it onto the container.
    list.innerHTML = '<p class="feed-empty">' + escapeHtml(list.getAttribute('data-load-failed') || '') + '</p>';
  }
}

export async function refresh() {
  var seq = ++_seq;
  var data;
  try {
    var res = await fetch(apiUrl('/feed/activity?tz=' + encodeURIComponent(_zone())));
    if (!res.ok) throw new Error('feed activity: ' + res.status);
    data = await res.json();
  } catch (_e) {
    // A failure still re-arms the timers from the last good payload, so a running line's minutes and the midnight
    // rollover do not stop for one bad fetch.
    if (seq === _seq) { _failed = true; _render(); _scheduleTimers(); }
    return;
  }
  if (seq !== _seq) return;
  _failed = false;
  _lastFetchAt = Date.now();
  _payload = data;
  _syncSeen(_open);
  _render();
  _updateBadge();
  _scheduleTimers();
}

// At most one refetch a second however many events arrive: the pending one reads the newest state.
function _scheduleRefresh() {
  if (_timer) return;
  _timer = setTimeout(function() { _timer = 0; refresh(); }, _REFETCH_MS);
}

// A message that says a run's status or the feed changed: a finished run emits `agent_finished` and the rest `feed`.
// The 2 s `status_update` and the other change messages are not it.
function _onMessage(msg) {
  if (msg && (msg.type === 'feed' || msg.type === 'agent_finished')) _scheduleRefresh();
}

// The socket is down and the shared poll ticked: the badge needs a coarse cadence only, so a hidden tab skips it and a
// closed panel is rebuilt at most every _FALLBACK_MIN_MS. It goes through the one-second coalescing like a message.
function _onFallbackPoll() {
  if (document.hidden) return;
  if (!_open && _payload && Date.now() - _lastFetchAt < _FALLBACK_MIN_MS) return;
  _scheduleRefresh();
}

export function hasRunningLine(data) {
  return ((data && data.today) || []).some(function(entry) { return !!entry.running; });
}

function _msUntilMidnight() {
  var now = new Date();
  var next = new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1);
  return next.getTime() - now.getTime() + _MIDNIGHT_SLACK_MS;
}

// After each good payload: the once-a-minute refetch exists only while the panel is open on a running line (a closed
// panel refetches on open), and the midnight one always does. A hidden tab skips its fetch and waits for the next tick.
function _scheduleTimers() {
  if (_ticker) { clearTimeout(_ticker); _ticker = 0; }
  if (_midnight) { clearTimeout(_midnight); _midnight = 0; }
  if (_open && hasRunningLine(_payload)) {
    _ticker = setTimeout(function() {
      _ticker = 0;
      if (document.hidden) _scheduleTimers(); else refresh();
    }, _RUNNING_REFRESH_MS);
  }
  _midnight = setTimeout(function() { _midnight = 0; refresh(); }, _msUntilMidnight());
}

// Every word of the confirm prompt and the result comes with the action (feed_narration.py).
async function _runAction(button) {
  var label = button.getAttribute('data-feed-label') || '';
  var ref = button.getAttribute('data-feed-ref') || '';
  if (!ref) return;
  if (button.getAttribute('data-feed-confirm') === '1' && !(await sovaConfirm(button.getAttribute('data-feed-confirm-text') || '', {
    title: label, confirmText: label, confirmClass: 'danger'
  }))) return;
  button.disabled = true;
  // The line shows the running state and the result (a failure stays until dismissed), as a Work card does.
  await runTrackedAction(_actionKey(ref), label, async function() {
    var res = await fetch(apiUrl('/handoff/execute'), {
      method: 'POST',
      headers: jsonCsrfHeaders(),
      body: JSON.stringify({ action_id: button.getAttribute('data-feed-action'), issue: ref })
    });
    if (!res.ok) throw new Error(await getErrorDetail(res.clone(), button.getAttribute('data-feed-failed-text') || ''));
    return button.getAttribute('data-feed-started-text') || '';
  });
  button.disabled = false;
  refresh();
}

function _onListClick(e) {
  var button = e.target && e.target.closest ? e.target.closest('[data-feed-action]') : null;
  if (button) _runAction(button);
}

// The panel opened or closed (js/feed/panel.js dispatches this from the shared side panel): opening marks every line
// seen and fetches the current payload.
function _onToggle(e) {
  _open = !!(e.detail && e.detail.open);
  if (_open) {
    _syncSeen(true);
    _render();
    refresh();
  }
  _updateBadge();
  _scheduleTimers();
}

// Another tab saved its seen marks: take them, so a line seen there is not unread here.
function _onStorage(e) {
  if (!e || e.key !== _seenKey()) return;
  _mergeStoredSeen();
  _updateBadge();
}

// The panel may already be open when this module starts (it loads after js/feed/panel.js, and the owl button can be
// pressed before it does): read the state instead of waiting for an event that has gone by. The shared side panel
// names the open panel on the body.
function _panelOpenNow() {
  var body = document.body;
  return !!(body && body.getAttribute && body.getAttribute('data-side-panel') === 'feed');
}

export function init() {
  _loadSeen();
  _open = _panelOpenNow();
  if (window.addEventListener) window.addEventListener('storage', _onStorage);
  var list = document.getElementById('feed-list');
  if (list) list.addEventListener('click', _onListClick);
  document.addEventListener('sova:feed-toggle', _onToggle);
  onActionStateChange(_render);
  subscribeSovaSocket({ onMessage: _onMessage, onOpen: _scheduleRefresh, onFallbackPoll: _onFallbackPoll });
  refresh();
}
