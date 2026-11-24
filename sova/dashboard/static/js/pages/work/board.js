import { actionAttrs } from '../agents/actions.js';
import { _updateApiHealthBanner } from '../agents/live-status.js';
import { diffSizeColor, diffSizeLabel, formatAgeBadge } from '../agents/render-utils.js';
import { renderHeader } from './header.js';
import { clearSelection, getSelected, onSelectionChange, relationChip, relationClass, select, selectionKey, setSelectionBoard } from './selection.js';

// The Work board: GET /api/work/board, drawn in the order of the payload's `columns`, with each column's
// label and color token and the payload key holding its entries. A stage whose `column` names another stage
// is a collapsed group inside that column ("Your own work" in Needs you); one with no column (blocked) shows
// only in the chains. The page keeps no stage table of its own: which stage an issue is in, what a stage is
// called, its color and where it shows all come from the server (work_state.py, work_board_service.py).
//
// Refreshes come from the shared socket only (#1459): work_items_changed, agent_finished and
// graph_invalidated reload the board, a handoff whose content changed reloads it (the first one after a socket
// opens only records, since the open's catch-up already loads), every status_update patches the running cards'
// time in step and last output in place, actions_changed reloads the per-card action states, and the hub's own
// fallback runs while the socket is down. A run that starts, ends or moves step reloads nothing here: the server
// rebuilds the board snapshot for it and sends work_items_changed once the rebuilt board differs (#1534), so the
// page never refetches the board from before the change. The server rebuilds for a changed handoff too, so a
// reload that still read the previous snapshot is followed by work_items_changed. The only timer this module
// starts is the one-shot retry after a failed load (below).
//
// The page tells the server whether it is visible (work_visible, on every socket open, visibility change and
// pagehide): only while a visible Work tab is connected does the server probe GitHub for changes made there
// (#1534). Leaving the Work page is a full page load (the dashboard has no client-side router), which closes this
// page's socket and clears its flag on the server; pagehide reports it hidden first, and a page restored from the
// back-forward cache reports again through the reopened socket's onOpen.

var _board = null;
var _boardOwner = null;
var _milestone = safeStorageGet('sova-work-milestone') || '';
var _ownerFilter = safeStorageGet('sova-work-owner-filter') === 'mine' ? 'mine' : 'all';
var _listeners = [];
var _openGroups = {};      // stage -> true while its collapsed group is open
var _showHidden = false;
var _dragging = false;     // a drag in progress: a redraw would drop the dragged card
var _redrawAfterDrag = false;

// One load at a time: a refresh asked for while one is in flight runs once more after it, so a burst of
// socket messages costs at most one extra fetch and the newest change is never dropped.
var _load = null;
var _reloadAfter = false;
var _LOAD_TIMEOUT_MS = 30000;

// The age marker shows only after a failure: the board is not polled, so its age alone says nothing.
var _STALE_AFTER_MS = Infinity;

// A failed load is retried once after this delay, because the marker tells the user it is "retrying" and a quiet
// tracker may send no change message for a long time. At most one retry is pending: a retry that fails arms the
// next one, a success cancels it, and a retry that fires while the tab is hidden is dropped (the hub reloads the
// board when the tab returns).
var _RETRY_AFTER_MS = 15000;
var _retryTimer = null;

function _cancelRetry() {
  if (_retryTimer === null) return;
  clearTimeout(_retryTimer);
  _retryTimer = null;
}

function _scheduleRetry() {
  if (_retryTimer !== null) return;
  _retryTimer = setTimeout(function() {
    _retryTimer = null;
    if (document.hidden) return;
    loadBoard();
  }, _RETRY_AFTER_MS);
}

// The marker is bookkeeping: a missing or throwing helper must never turn a rendered board into a reported failure.
function _markRefresh(ok) {
  if (typeof markRefresh !== 'function') return;
  try {
    markRefresh('work-board', ok, _STALE_AFTER_MS);
  } catch (e) {
    console.warn('work board refresh marker failed', e);
  }
}

// What the board says about its own data after a good load: details that could not be loaded, and a ranking that
// is older than the refresh interval because the tracker refresh behind it failed.
function _boardNotice(board) {
  var parts = [];
  if (board.unavailable && board.unavailable.length) {
    parts.push('Some details could not be loaded (' + board.unavailable.join(', ').replace(/_/g, ' ') + ')');
  }
  var graph = board.graph;
  if (graph && graph.graph_refresh_failed) {
    parts.push('The ranking is ' + formatElapsed(graph.graph_age_seconds || 0) + ' old: refreshing it from the tracker failed');
  }
  return parts.join('. ');
}

export function getBoard() { return _board; }

// Another module (chains, drawer) redraws whenever a new board arrives.
export function onBoard(fn) { _listeners.push(fn); }

// Called after renderBoard() has replaced the cards, so a listener that measures them never sees a stale node.
var _renderedListeners = [];
export function onBoardRendered(fn) { _renderedListeners.push(fn); }

// Where a smooth scroll of the board is headed, until it has finished: a redraw in that window restores this
// position instead of the mid-animation one. It belongs to the selection it revealed (`key`): another selection
// starts from wherever the board is. The hold ends with the animation (the host's `scrollend`) or with the user
// taking the scroll over (wheel, touch, pointer, keys); a timer is only the fallback where `scrollend` does not
// exist, and a generous cap otherwise.
var _scrollHold = null;
var _HOLD_FALLBACK_MS = 600;
var _HOLD_CAP_MS = 3000;
export function holdBoardScroll(left, key) {
  var hasScrollEnd = typeof window !== 'undefined' && window && 'onscrollend' in window;
  _scrollHold = { left: left, until: Date.now() + (hasScrollEnd ? _HOLD_CAP_MS : _HOLD_FALLBACK_MS), key: key };
}
function _releaseScrollHold() { _scrollHold = null; }

export function loadBoard() {
  if (_load) {
    _reloadAfter = true;
    return _load;
  }
  _load = (async function() {
    try {
      do {
        _reloadAfter = false;
        await _fetchBoard();
      } while (_reloadAfter);
    } finally {
      _load = null;
    }
  })();
  return _load;
}

async function _fetchBoard() {
  var ctrl = new AbortController();
  var timer = setTimeout(function() { ctrl.abort(); }, _LOAD_TIMEOUT_MS);
  var savesBefore = settingSaveCount();
  var requestedOwner = _ownerFilter;
  var previousBoard = _board;
  var previousOwner = _boardOwner;
  try {
    var params = new URLSearchParams();
    if (_milestone) params.set('milestone', _milestone);
    if (_ownerFilter === 'mine') params.set('owner', 'mine');
    var url = '/work/board' + (params.toString() ? '?' + params.toString() : '');
    var res = await fetch(apiUrl(url), { signal: ctrl.signal });
    if (!res.ok) throw new Error(await getErrorDetail(res, 'Failed to load the board'));
    var response = await res.json();
    if (requestedOwner !== _ownerFilter) {
      _reloadAfter = true;
      return;
    }
    _board = response;
    _boardOwner = requestedOwner;
    setSelectionBoard(_board);
    _updateApiHealthBanner(_board.api_health);
    renderHeader(_board, settingSaveCount() !== savesBefore);
    renderBoard();
    _openLinkedCard();
    // Only a board that rendered counts as a refresh: a throw above is a failure, not a success then a failure.
    _cancelRetry();
    _markRefresh(true);
    _setError(_boardNotice(_board));
    _listeners.forEach(function(fn) {
      try {
        fn(_board);
      } catch (e) {
        console.error('work board listener failed', e);
      }
    });
  } catch (e) {
    // A failed refresh keeps the last good board on screen and says so; it never shows an empty board.
    var message = e && e.name === 'AbortError' ? 'Loading the board timed out' : (e && e.message) || 'Failed to load the board';
    if (_board !== previousBoard) {
      // The new response failed to render, so the board on screen is still the previous one.
      _board = previousBoard;
      _boardOwner = previousOwner;
    }
    _setError(_board && _boardOwner === _ownerFilter ? message + '. Showing the last board loaded.' : message);
    _markRefresh(false);
    _scheduleRetry();
    if (!_board) {
      var host = document.getElementById('work-board');
      if (host) host.innerHTML = '<p class="text-accent-red text-sm py-6">' + escapeHtml(message) + '</p>';
    }
  } finally {
    clearTimeout(timer);
  }
}

// The ids of the finished server actions the board has already reloaded for: an action that ended (a background
// rebase) changed its PR, so the board reloads once for it, even when no load ever saw it running, instead of
// waiting for the next work_items_changed.
var _reportedFinishedIds = new Set();
// The latest request's number: a slower, older answer must not overwrite a newer one's state.
var _actionsRequest = 0;

async function _loadActions() {
  var request = ++_actionsRequest;
  try {
    var data = await fetchAPI(apiUrl('/work/actions'));
    if (request !== _actionsRequest) return;
    var actions = data.actions || [];
    var finished = actions.filter(function(a) { return a.status !== 'running' && !_reportedFinishedIds.has(a.id); });
    finished.forEach(function(a) { _reportedFinishedIds.add(a.id); });
    setServerActions(actions);
    if (finished.length) loadBoard();
  } catch (e) {
    console.warn('Failed to load the running actions', e);
  }
}

// The error and the age marker (markRefresh(), #work-board-stale) share one line, which shows while there is an error.
function _setError(message) {
  var el = document.getElementById('work-board-error');
  if (el) el.textContent = message;
  var line = document.getElementById('work-board-status');
  if (line) line.classList.toggle('hidden', !message);
}

// Rendering ----------------------------------------------------------------

var _PREFIX = window.SOVA_PROJECT_SLUG ? '/p/' + window.SOVA_PROJECT_SLUG : '';

export function iconHtml(name) {
  var tpl = document.getElementById('work-icons');
  var el = tpl && tpl.content.querySelector('[data-icon="' + name + '"]');
  return el ? el.innerHTML : '';
}

// The button that remedies the card's failed action (a failed rebase names a retry or an Address PR run), beside
// the failure line: the failure says why, and the button is the way on, so the user never has to work out what next.
function _fixButton(key, issue) {
  var state = actionStateFor(key);
  if (!state || state.status !== 'failed' || !state.fix) return '';
  return _btn('warning', state.fix.label, 'work-run-action', [issue, state.fix], { icon: 'play', disabled: actionIsRunning(key) });
}

function _btn(variant, label, action, args, opts) {
  opts = opts || {};
  var cls = variant ? 'sova-btn sova-btn-' + variant : 'sova-btn sova-work-btn-quiet text-gray-400 hover:text-gray-200 hover:bg-surface-hover';
  return '<button type="button" class="' + cls + '"' + actionAttrs(action, args) +
    (opts.disabled ? ' disabled' : '') +
    (opts.ariaLabel ? ' aria-label="' + escapeHtml(opts.ariaLabel) + '"' : '') + '>' +
    (opts.icon ? iconHtml(opts.icon) : '') + (label ? escapeHtml(label) : '') + '</button>';
}

function _chip(text, cls) {
  return '<span class="px-1.5 rounded text-xs whitespace-nowrap ' + (cls || 'bg-surface-hover text-gray-400') + '">' + escapeHtml(text) + '</span>';
}

// The priority as its icon, named in a tooltip. A merged entry (Done today) carries no priority and shows none.
function _priorityIcon(entry, stage) {
  if (stage === 'done') return '';
  var name = priorityDisplayName(entry.priority) + ' priority';
  return '<span class="inline-flex shrink-0" data-tooltip="' + escapeHtml(name) + '">' +
    '<img src="' + priorityIconUrl(entry.priority) + '" alt="' + escapeHtml(name) + '" width="16" height="16"></span>';
}

// The issue's state as the server names and colors it (work_state.py), as a pill.
export function statePill(entry) {
  if (!entry.state_label) return '';
  return '<span class="px-1.5 rounded-full text-xs whitespace-nowrap ' + stateBadgeClasses(entry.state_color) + '">' +
    escapeHtml(entry.state_label) + '</span>';
}

// A sentence-long note (why an issue needs you, why it is yours) as a pill that wraps inside the card instead of
// running past its edge: a 13rem column cannot fit "PR no longer open, issue still labeled in review" on one line.
function _notePill(text, cls) {
  return '<span class="px-1.5 rounded-md text-xs leading-snug break-words min-w-0 ' + cls + '">' + escapeHtml(text) + '</span>';
}

// `end` marks a pill that sits at the right of its row: inside the drawer's body its tooltip grows leftward from the
// pill's right edge (data-tooltip-end), where a tooltip on a pill at the left grows rightward. Nothing outside the
// drawer reads the attribute, so only the drawer asks for it.
function _pill(text, cls, tooltip, end) {
  return '<span class="px-1.5 rounded-full text-xs whitespace-nowrap ' + cls + '"' +
    (tooltip ? ' data-tooltip="' + escapeHtml(tooltip) + '"' + (end ? ' data-tooltip-end' : '') : '') + '>' + escapeHtml(text) + '</span>';
}

// The PR's number in its state's color, a link to GitHub.
export function prPill(pr) {
  var tip = (pr.state_label ? pr.state_label + '. ' : '') + 'Open on GitHub';
  return /^https:\/\//.test(pr.url || '')
    ? '<a href="' + escapeHtml(pr.url) + '" target="_blank" rel="noopener" data-action="stop" data-tooltip="' + escapeHtml(tip) + '"' +
      ' class="px-1.5 rounded-full text-xs whitespace-nowrap hover:brightness-125 ' + stateBadgeClasses(pr.state_color) + '">PR #' + pr.number + '</a>'
    : _pill('PR #' + pr.number, stateBadgeClasses(pr.state_color), pr.state_label);
}

// Review threads resolved of all, green when every one is resolved, yellow while some are open.
export function threadsHtml(pr, long) {
  if (!(pr.threads_total > 0)) return long ? '<span class="text-gray-500">No review threads</span>' : '';
  var allDone = pr.threads_resolved >= pr.threads_total;
  return '<span class="inline-flex items-center gap-0.5 ' + (allDone ? 'text-accent-green' : 'text-accent-yellow') + '"' +
    ' data-tooltip="' + pr.threads_resolved + ' of ' + pr.threads_total + ' review threads resolved">' +
    iconHtml('chat-bubble') + pr.threads_resolved + '/' + pr.threads_total + (long ? ' resolved' : '') + '</span>';
}

// The diff size as a colored letter (S to XXL); `long` adds the line and file counts.
export function sizeHtml(pr, long) {
  var label = diffSizeLabel(pr.additions, pr.deletions);
  if (!label) return '';
  var counts = '+' + (pr.additions || 0) + ' / -' + (pr.deletions || 0) + ' in ' + (pr.changed_files || '?') + ' files';
  return '<span class="font-semibold ' + diffSizeColor(pr.additions, pr.deletions) + '"' + (long ? '' : ' data-tooltip="' + counts + '"') + '>' +
    label + '</span>' + (long ? ' <span class="text-gray-400">' + counts + '</span>' : '');
}

// Conflicts, a base that moved on, and draft, as pills when they apply. Each flag names the PR state (`pr.state`,
// the server's computed state, a ComputedPRState value) it repeats, or null when no state says the same.
// `opts.stateShown` is true beside a PR state tag (the drawer's): a flag repeating that state is left out, since a PR
// in the conflicted state is also CONFLICTING. The comparison is on the state key, never on display text.
// `opts.end` anchors the pills' tooltips at their right edge (the drawer's PR row).
export function prFlags(pr, opts) {
  var stateShown = !!(opts && opts.stateShown);
  var end = !!(opts && opts.end);
  return [
    [pr.mergeable === 'CONFLICTING', 'conflicted', 'Conflicts', 'bg-accent-red/20 text-accent-red', 'The PR has merge conflicts to resolve'],
    [pr.merge_state === 'BEHIND', null, 'Behind base', 'bg-accent-lavender/20 text-accent-lavender', 'The base branch moved on; Integrate rebases first'],
    [pr.is_draft, 'draft', 'Draft', 'bg-gray-600/30 text-gray-400'],
  ].filter(function(f) { return f[0] && !(stateShown && f[1] !== null && f[1] === pr.state); })
    .map(function(f) { return _pill(f[2], f[3], f[4], end); }).join('');
}

// The open PR at a glance, as the old task list showed it: its number in its state's color (a link to
// GitHub), CI, review threads resolved, size, and conflicts, a base that moved on or a draft when they apply.
function _prStrip(entry) {
  var pr = entry.pr;
  if (!pr) return entry.pr_number ? _sub([_span('PR #' + entry.pr_number)]) : '';
  return _sub([prPill(pr), ciHtml(pr.ci_state), threadsHtml(pr), sizeHtml(pr), prFlags(pr)]);
}

var _RUN_STATUS_CLASSES = {
  done: 'bg-accent-green/15 text-accent-green',
  failed: 'bg-accent-red/15 text-accent-red',
  error: 'bg-accent-red/15 text-accent-red',
  running: 'bg-accent/15 text-accent',
};

// The issue's latest run: its name and color from the run-action table, its status as a pill, and when.
export function lastRunHtml(entry, bare) {
  var last = entry.last_run;
  if (!last || !last.role) return '';
  return '<span class="inline-flex flex-wrap items-center gap-1">' + (bare ? '' : 'Last run ') +
    '<span class="' + roleColor(last.role).text + '">' + escapeHtml(formatRole(last.role)) + '</span>' +
    statusBadge(last.status || 'unknown', _RUN_STATUS_CLASSES) +
    (last.ended_at ? '<span class="text-gray-500">' + escapeHtml(timeAgo(last.ended_at)) + '</span>' : '') + '</span>';
}

function _age(entry) {
  if (!entry.created_at) return '';
  var ms = Date.now() - new Date(entry.created_at).getTime();
  return !isNaN(ms) && ms > 0 ? formatAgeBadge(Math.floor(ms / 1000), entry.created_at) : '';
}

function _ciText(ci) {
  if (ci === 'pending' || ci === 'running') return ['CI running', 'bg-accent-yellow'];
  if (ci === 'passed' || ci === 'success') return ['CI green', 'bg-accent-green'];
  if (ci === 'failed' || ci === 'failure') return ['CI failed', 'bg-accent-red'];
  return null;
}

export function ciHtml(ci) {
  var t = _ciText(ci);
  if (!t) return '';
  return '<span class="inline-flex items-center gap-1"><span class="w-1.5 h-1.5 rounded-full ' + t[1] + '"></span>' + t[0] + '</span>';
}

function _chips(parts) {
  var html = parts.filter(Boolean).join('');
  return html ? '<div class="flex flex-wrap items-center gap-1">' + html + '</div>' : '';
}

function _sub(parts) {
  var html = parts.filter(Boolean).join('');
  return html ? '<div class="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-xs text-gray-400">' + html + '</div>' : '';
}

function _ownedByMe(entry) {
  if (_ownerFilter === 'all') return true;
  if (typeof entry.mine === 'boolean') return entry.mine;
  var github = ((_board && _board.github_user) || '').toLowerCase();
  var jira = ((_board && _board.jira_display_name) || '').toLowerCase();
  var assignees = entry.assignees || [];
  var pr = entry.pr || {};
  return !!(entry.jira_key && jira && assignees.some(function(name) { return (name || '').toLowerCase() === jira; }) ||
    github && ((!entry.jira_key && assignees.some(function(name) { return (name || '').toLowerCase() === github; })) ||
      (pr.author || entry.pr_author || '').toLowerCase() === github ||
      (pr.assignees || entry.pr_assignees || []).some(function(name) { return (name || '').toLowerCase() === github; })));
}

function _contributors(entry) {
  var pr = entry.pr || {};
  var people = [];
  function add(login, role) {
    if (login && !people.some(function(p) { return p.login.toLowerCase() === login.toLowerCase(); })) {
      people.push({ login: login, role: role });
    }
  }
  add(pr.author || entry.pr_author, 'PR author');
  (pr.assignees || entry.pr_assignees || []).forEach(function(login) { add(login, 'PR assignee'); });
  if (!entry.jira_key) (entry.assignees || []).forEach(function(login) { add(login, 'Issue assignee'); });
  var jiraAssignee = entry.jira_key && (entry.assignees || [])[0];
  if (!people.length && !jiraAssignee) return '';
  return '<div class="flex items-center gap-1.5 text-xs text-gray-500 min-w-0">' +
    (jiraAssignee ? '<span class="truncate" data-tooltip="Jira assignee: ' + escapeHtml(jiraAssignee) + '">' +
      escapeHtml(jiraAssignee) + '</span>' : '') +
    '<span class="inline-flex items-center gap-1 ml-auto shrink-0" aria-label="GitHub contributors">' +
    people.slice(0, 3).map(function(p) {
      return '<span tabindex="0" data-tooltip="' + escapeHtml(p.role + ': ' + p.login) + '">' +
        '<img src="https://avatars.githubusercontent.com/' + encodeURIComponent(p.login) + '?size=32"' +
        ' alt="' + escapeHtml(p.role + ': ' + p.login) + '" width="20" height="20" loading="lazy"' +
        ' class="w-5 h-5 rounded-full" data-hide-on-error></span>';
    }).join('') + (people.length > 3 ? '<span class="text-xs text-gray-500">+' + (people.length - 3) + '</span>' : '') +
    '</span></div>';
}

function _span(text) { return '<span>' + escapeHtml(text) + '</span>'; }

function _localTime(iso) {
  if (!iso) return '';
  var d = new Date(iso);
  return isNaN(d.getTime()) ? '' : d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function _humanStep(name) { return (name || '').replace(/_/g, ' '); }

// The role the issue's stage runs next (its `start` action, from work_state.start_action() on the server):
// researcher on a triaged issue, developer on a researched one; '' when no agent runs on it in its state.
function _startRole(entry) {
  return (entry.start && entry.start.handler_args && entry.start.handler_args.role) || '';
}

// The button that starts that agent, named for it (Research, Develop, Triage, Resume), '' when there is none.
function _startBtn(entry, variant, suffix, off) {
  var role = _startRole(entry);
  if (!role) return '';
  return _btn(variant, entry.start.label + (suffix || ''), 'work-start', [entry.number, role], Object.assign({ icon: 'play' }, off));
}

// What a server action does, as `handler:command` or `handler:role`: how the drawer leaves out an action a
// card button already offers, without the page knowing which state offers which action.
export function actionSignature(action) {
  var a = (action && action.handler_args) || {};
  return (action && action.handler || '') + ':' + (a.command || a.role || '');
}

function _serverAction(entry, signature) {
  return (entry.actions || []).find(function(a) { return actionSignature(a) === signature; }) || null;
}

export function entryKey(entry) { return actionKey(entry.number, entry.pr_number || (entry.pr && entry.pr.number)); }

// An issue's place in the saved queue and the queue's length, as [position, length]; position 0 when not
// queued. Read from the full order (queue_order), not the Queued column: a queued issue that sits in another
// column (blocked) still takes a place, and a Move up or down acts on the saved order.
export function queuePosition(number) {
  var order = (_board && _board.queue_order) || [];
  var i = order.indexOf(number);
  return i === -1 ? [0, order.length] : [i + 1, order.length];
}

// The card's top-right note, extra lines and buttons, for the stage it is in, and the server actions those
// buttons already offer (`covers`, so the drawer lists only the others). Buttons are disabled while one
// of the card's actions runs (actionIsRunning), so a second click cannot start a duplicate.
export function cardParts(entry, stage) {
  var n = entry.number;
  var busy = actionIsRunning(entryKey(entry));
  var off = { disabled: busy };
  var right = '';
  var body = '';
  var buttons = '';
  var covers = [];  // the server actions (actionSignature) these buttons already offer
  if (stage === 'next') {
    covers = entry.start ? [actionSignature(entry.start)] : [];
    right = entry.rank != null
      ? '<span class="px-1.5 rounded text-xs font-bold text-accent bg-accent/15 tabular-nums" aria-label="Rank ' + entry.rank + '">' + entry.rank + '</span>'
      : '';
    body = _chips([statePill(entry), entry.unblocks > 0 ? _chip('Unblocks ' + entry.unblocks, 'sova-work-chip-unblocks') : '',
      entry.milestone ? _chip(entry.milestone) : '', _age(entry)]) + _prStrip(entry);
    buttons = _startBtn(entry, 'primary', '', off) +
      _btn('', 'Queue', 'work-queue', [n], Object.assign({ icon: 'queue' }, off)) +
      _btn('', 'Hide', 'work-hide', [n], off);
  } else if (stage === 'queued') {
    var pos = queuePosition(n);
    covers = entry.start ? [actionSignature(entry.start)] : [];
    right = pos[0] ? pos[0] + ' of ' + pos[1] : '';
    body = _chips([statePill(entry), entry.milestone ? _chip(entry.milestone) : '', _age(entry)]) + _prStrip(entry);
    buttons = _startBtn(entry, 'primary', ' now', off) +
      _btn('', '', 'work-queue-move', [n, -1], { icon: 'arrow-up', ariaLabel: 'Move #' + n + ' up', disabled: busy || pos[0] <= 1 }) +
      _btn('', '', 'work-queue-move', [n, 1], { icon: 'arrow-down', ariaLabel: 'Move #' + n + ' down', disabled: busy || pos[0] >= pos[1] }) +
      _btn('', '', 'work-unqueue', [n], Object.assign({ icon: 'close', ariaLabel: 'Remove #' + n + ' from the queue' }, off));
  } else if (stage === 'running') {
    var total = entry.total_steps || 0;
    var index = entry.step_index != null ? entry.step_index : null;
    // Halfway through the current step: step 1 of 1 is under way, not 0% done.
    var pct = total && index != null ? Math.min(100, Math.round((index + 0.5) / total * 100)) : null;
    right = pct != null ? pct + '%' : '';
    var stepText = [entry.role ? formatRole(entry.role) : '', _humanStep(entry.step_name), total && index != null ? 'step ' + Math.min(index + 1, total) + ' of ' + total : '']
      .filter(Boolean).join(' · ');
    body = (pct != null ? '<div class="h-1 rounded bg-surface-hover overflow-hidden"><div class="h-full bg-accent-yellow" style="width:' + pct + '%"></div></div>' : '') +
      _sub([stepText ? _span(stepText) : '']) + _prStrip(entry) +
      _sub([entry.elapsed_seconds != null ? '<span>' + liveElapsed(entry.elapsed_seconds) + ' running</span>' : '',
        entry.cost_usd != null ? _span('$' + parseFloat(entry.cost_usd || 0).toFixed(2) + ' so far') : '']) +
      _runActivity(entry.run_id);
    if (entry.run_id) {
      if (entry.role !== 'supervisor:rebase') {
        buttons = _btn('', 'Stop', 'work-stop', [entry.run_id, n, entry.pr_number || null], Object.assign({ icon: 'stop' }, off));
      }
      buttons += _btn('', 'View log', 'work-view-log', [entry.run_id, 'running'], {});
    }
  } else if (stage === 'review') {
    covers = ['start_agent:reviewer', 'run_command:review-pr', 'trigger_rebase:'];
    right = statePill(entry);
    body = _prStrip(entry) + _sub([entry.pr && !entry.pr.threads_total ? _span('No review threads yet') : '', lastRunHtml(entry)]);
    if (entry.pr_number) {
      buttons = _btn('primary', 'Review', 'work-review', [n, entry.pr_number], Object.assign({ icon: 'play' }, off));
      // Address PR beside Review: the work item's own action when the server offers one (it applies its own
      // conditions), else the command itself, but only while the PR has an unresolved review thread, so a click
      // never spends a full agent run on a PR with nothing to address.
      var address = _serverAction(entry, 'run_command:address-pr');
      var unresolved = entry.pr && entry.pr.threads_total > (entry.pr.threads_resolved || 0);
      if (address) {
        covers.push('run_command:address-pr');
        buttons += _btn('', 'Address PR', 'work-run-action', [n, address], Object.assign({ icon: 'play' }, off));
      } else if (unresolved) {
        covers.push('run_command:address-pr');
        buttons += _btn('', 'Address PR', 'work-address', [n, entry.pr_number], Object.assign({ icon: 'play' }, off));
      }
    }
    if (entry.pr && entry.pr.mergeable === 'CONFLICTING') buttons += _btn('warning', 'Rebase', 'work-rebase', [n], off);
  } else if (stage === 'needs') {
    body = _chips([entry.reason ? _notePill(entry.reason, 'bg-accent-peach/15 text-accent-peach') : '']) +
      _prStrip(entry) + _sub([lastRunHtml(entry)]);
    if (entry.kind === 'merge' || entry.kind === 'review_budget') {
      covers = ['run_command:integrate-pr'];
      var integrate = _serverAction(entry, 'run_command:integrate-pr');
      if (integrate) buttons = _btn('success', 'Integrate', 'work-run-action', [n, integrate], Object.assign({ icon: 'check' }, off));
      else if (entry.pr_number) buttons = _btn('success', 'Integrate', 'work-integrate', [n, entry.pr_number], Object.assign({ icon: 'check' }, off));
    } else if (entry.kind === 'spec') {
      if (entry.run_id) buttons = _btn('success', 'Approve and start', 'work-approve', [entry.run_id, n], Object.assign({ icon: 'check' }, off));
      if (n != null) buttons += '<a class="sova-btn text-gray-400 hover:text-gray-200 hover:bg-surface-hover" href="' + _PREFIX + '/spec/' + n + '" data-action="stop">Read spec</a>';
    } else if (entry.kind === 'failed_run') {
      // When the issue's stage calls for another agent than the one that failed (a developer refused on a triaged
      // issue), that agent is the way on, and retrying the failed one would only be refused again.
      var failedRole = entry.role || 'developer';
      var nextRole = _startRole(entry);
      if (nextRole && nextRole !== failedRole) {
        covers = ['start_agent:' + failedRole, actionSignature(entry.start)];
        buttons = _startBtn(entry, 'primary', '', off);
      } else {
        covers = ['start_agent:' + failedRole];
        buttons = _btn('warning', 'Retry', 'work-retry', [n, failedRole, entry.pr_number || null], Object.assign({ icon: 'play' }, off));
      }
      if (entry.run_id) buttons += _btn('', 'View log', 'work-view-log', [entry.run_id, 'failed'], {});
    }
  } else if (stage === 'yours') {
    var source = entry.reason_source && entry.reason_source !== 'label' ? entry.reason_source + ': ' : '';
    covers = ['start_agent:triage'];
    body = _chips([entry.reason ? _notePill(source + entry.reason, 'bg-accent-purple/15 text-accent-purple') : '', statePill(entry), _age(entry)]) +
      _prStrip(entry);
    if (entry.session_command) {
      buttons = _btn('', 'Copy session command', 'work-copy-command', [n, entry.session_command], {});
    } else if (entry.mode === 'unclassified') {
      buttons = _btn('primary', 'Triage', 'work-triage', [n], Object.assign({ icon: 'play' }, off));
    }
  } else if (stage === 'done') {
    right = _localTime(entry.merged_at);
    body = _sub([entry.pr_number ? _span('PR #' + entry.pr_number + ' merged') : '']);
  } else if (stage === 'hidden') {
    body = _chips([statePill(entry), _age(entry)]);
    buttons = _btn('', 'Unhide', 'work-unhide', [n], off);
  }
  return { right: right, body: body, buttons: _fixButton(entryKey(entry), entry.number) + buttons, covers: covers };
}

function _card(entry, stage) {
  var parts = cardParts(entry, stage);
  var n = entry.number;
  var ref = n != null ? (entry.jira_key || '#' + n) : (entry.pr_number ? 'PR #' + entry.pr_number : '');
  // A PR with no issue selects by 'pr:N', so its card opens the drawer too.
  var key = selectionKey(entry);
  var rel = key != null ? relationClass(key) : '';
  var selectable = key != null;
  var draggable = stage === 'next'
    ? ' draggable="true" data-drag-issue="' + n + '" data-start-role="' + escapeHtml(_startRole(entry)) + '"'
    : '';
  var status = actionStatusHtml(entryKey(entry));
  return '<div class="sova-work-card ' + rel + '" data-work-card' + draggable +
    (selectable ? actionAttrs('work-select', [key]) + ' tabindex="0" role="button" aria-label="' + escapeHtml(ref + ': ' + (entry.title || '')) + ', show details"' : '') + '>' +
    '<div class="flex items-center gap-1.5 min-w-0">' +
      _priorityIcon(entry, stage) +
      '<span class="text-xs font-bold text-accent tabular-nums shrink-0">' + escapeHtml(ref) + '</span>' +
      (n != null ? relationChip(n) : '') +
      '<span class="ml-auto flex items-center gap-1.5 text-xs text-gray-500 shrink-0">' + parts.right + '</span>' +
    '</div>' +
    '<div class="text-sm text-gray-200 line-clamp-2">' + escapeHtml(entry.title || '') + '</div>' +
    _contributors(entry) + parts.body + status +
    (parts.buttons ? '<div class="flex flex-wrap items-center gap-0.5">' + parts.buttons + '</div>' : '') +
  '</div>';
}

function _colHeader(col, count, extra) {
  return '<div class="flex items-center gap-1.5 px-0.5 min-h-[1.5rem] text-xs font-semibold uppercase tracking-wider text-gray-400"' +
    '>' +
    '<span class="w-2 h-2 rounded-full shrink-0" style="background:' + stateHex(col.color) + '"></span>' +
    '<span class="whitespace-nowrap">' + escapeHtml(col.label) + '</span> <span class="font-medium text-gray-500">' + count + '</span>' +
    (col.description ? '<span tabindex="0" role="note" aria-label="' + escapeHtml(col.label + ': ' + col.description) +
      '" data-tooltip="' + escapeHtml(col.description) + '" class="normal-case text-gray-500">?</span>' : '') +
    (extra ? '<span class="ml-auto min-w-0 normal-case tracking-normal font-normal">' + extra + '</span>' : '') +
  '</div>';
}

function _empty(text) { return '<p class="text-xs text-gray-500 px-1 py-2">' + escapeHtml(text) + '</p>'; }

var _EMPTY = {
  next: 'Nothing is ready for an agent.',
  queued: 'Drag a card from Up next, or press Queue.',
  running: 'No agent is running. Press Start on an Up next card, or drop one here.',
  review: 'No open PR waits on CI or review.',
  needs: 'Nothing waits for you.',
  done: 'Nothing merged yet today.',
};

function _milestoneSelect() {
  var options = (_board.milestones || []).slice();
  if (_milestone && options.indexOf(_milestone) === -1) options.unshift(_milestone);
  return '<select id="work-milestone" class="bg-surface border border-gray-700 rounded px-1.5 py-0.5 text-xs text-gray-300 w-full max-w-[8rem] focus:border-accent focus:outline-none" aria-label="Show Up next for one milestone">' +
    '<option value="">All milestones</option>' +
    options.map(function(m) {
      return '<option value="' + escapeHtml(m) + '"' + (m === _milestone ? ' selected' : '') + '>' + escapeHtml(m) + '</option>';
    }).join('') + '</select>';
}

function _columnExtra(col) {
  if (col.stage === 'next') return _milestoneSelect();
  if (col.stage === 'queued') return '<span class="text-gray-500">drag here</span>';
  if (col.stage === 'running' && _board.agents && _board.agents.limit != null && _board.agents.running != null) {
    return '<span class="text-gray-500">' + Math.max(_board.agents.limit - _board.agents.running, 0) + ' free</span>';
  }
  return '';
}

// A running run no card shows (a planner, or an issue the work items leave out): what it is, its step and
// elapsed time, with View log and Stop, so every running agent can be stopped from this page.
function _otherRunCard(run) {
  var ref = run.issue_number ? (run.jira_key || '#' + run.issue_number) : (run.pr_number ? 'PR #' + run.pr_number : '');
  var key = actionKey(run.issue_number, run.pr_number, run.run_id);
  var busy = actionIsRunning(key);
  return '<div class="sova-work-card">' +
    '<div class="flex items-center gap-1.5 min-w-0">' +
      (ref ? '<span class="text-xs font-bold text-accent tabular-nums shrink-0">' + escapeHtml(ref) + '</span>' : '') +
      '<span class="text-xs ' + roleColor(run.role).text + '">' + escapeHtml(formatRole(run.role, run.pipeline_variant)) + '</span>' +
      (run.elapsed_seconds != null ? '<span class="ml-auto text-xs text-gray-500 shrink-0">' + liveElapsed(run.elapsed_seconds) + '</span>' : '') +
    '</div>' +
    (run.run_label ? '<div class="text-sm text-gray-200 line-clamp-2">' + escapeHtml(run.run_label) + '</div>' : '') +
    _contributors(run) +
    _sub([run.step_name ? _span(_humanStep(run.step_name)) : '']) +
    _runActivity(run.run_id) +
    actionStatusHtml(key) +
    '<div class="flex flex-wrap items-center gap-0.5">' +
      (run.role === 'supervisor:rebase' ? '' :
        _btn('', 'Stop', 'work-stop', [run.run_id, run.issue_number || null, run.pr_number || null], { icon: 'stop', disabled: busy })) +
      _btn('', 'View log', 'work-view-log', [run.run_id, 'running'], {}) +
    '</div>' +
  '</div>';
}

function _columnFooter(col) {
  var html = '';
  if (col.stage === 'running') html += (_board.other_runs || []).filter(function(run) {
    return _ownerFilter === 'all' || _ownedByMe(run);
  }).map(_otherRunCard).join('');
  if (col.stage === 'next') {
    if (_board.more_ready > 0) {
      html += '<a class="text-xs text-accent hover:underline px-1" href="' + _PREFIX + '/supervisor">' +
        _board.more_ready + ' more ready: dependency graph</a>';
    }
    var hidden = (_board.hidden || []).filter(_ownedByMe);
    if (hidden.length) {
      html += '<button type="button" class="flex items-center gap-1 text-xs text-gray-500 hover:text-gray-300 transition-colors px-1"' +
        actionAttrs('work-toggle-hidden') + ' aria-expanded="' + _showHidden + '">' +
        iconHtml(_showHidden ? 'chevron-down' : 'chevron-right') + 'Hidden (' + hidden.length + ')</button>';
      if (_showHidden) html += hidden.map(function(e) { return _card(e, 'hidden'); }).join('');
    }
  }
  return html;
}

function _group(col) {
  var entries = (_board[col.entries] || []).filter(_ownedByMe);
  var open = !!_openGroups[col.stage];
  var html = '<button type="button" class="flex items-center gap-1.5 w-full px-0.5 pt-1 text-xs font-semibold uppercase tracking-wider text-gray-400 hover:text-gray-200 transition-colors"' +
    actionAttrs('work-toggle-group', [col.stage]) + ' aria-expanded="' + open + '">' +
    iconHtml(open ? 'chevron-down' : 'chevron-right') + escapeHtml(col.label) +
    ' <span class="font-medium text-gray-500">' + entries.length + '</span></button>';
  if (open) html += entries.map(function(e) { return _card(e, col.stage); }).join('') || _empty('Nothing here.');
  return html;
}

export function renderBoard() {
  var host = document.getElementById('work-board');
  if (!host || !_board) return;
  if (_boardOwner !== _ownerFilter) {
    host.innerHTML = '<p class="text-sm text-gray-400 py-6">Loading ' + (_ownerFilter === 'mine' ? 'your work' : 'all work') + '...</p>';
    return;
  }
  if (_dragging) {
    _redrawAfterDrag = true;
    return;
  }
  if (_ownerFilter === 'mine' && !_board.github_user && !_board.jira_display_name) {
    host.innerHTML = '<p class="text-sm text-gray-400 py-6">Set the project GitHub user or Jira display name to show your work.</p>';
    return;
  }
  var cols = _board.columns || [];
  var html = cols.filter(function(col) { return col.column === col.stage; }).map(function(col) {
    var entries = (_board[col.entries] || []).filter(_ownedByMe);
    var otherRunCount = col.stage === 'running' ? (_board.other_runs || []).filter(_ownedByMe).length : 0;
    var groups = cols.filter(function(g) { return g.column === col.stage && g.stage !== col.stage; });
    var drop = col.stage === 'queued' || col.stage === 'running' ? ' data-drop="' + col.stage + '"' : '';
    return '<div class="sova-work-col"' + drop + ' aria-label="' + escapeHtml(col.label) + '">' +
      _colHeader(col, entries.length + otherRunCount, _columnExtra(col)) +
      (entries.map(function(e) { return _card(e, col.stage); }).join('') ||
        (otherRunCount ? '' : _empty(_EMPTY[col.stage] || 'Nothing here.'))) +
      _columnFooter(col) +
      groups.map(_group).join('') +
    '</div>';
  }).join('');
  // A smooth scroll the drawer's reveal started is still running: restore where it is headed, not the point it has
  // reached, since assigning scrollLeft cancels the animation and would leave the card partly under the drawer.
  // Closing the drawer ends the hold with it.
  if (getSelected() === null) _scrollHold = null;
  var held = _scrollHold && Date.now() < _scrollHold.until && _scrollHold.key === getSelected();
  var scroll = held ? _scrollHold.left : host.scrollLeft;
  var focused = _focusedControl(host);
  host.innerHTML = '<div class="sova-work-track">' + html + '</div>';
  host.scrollLeft = scroll;
  _restoreFocus(host, focused);
  _renderedListeners.forEach(function(fn) {
    try {
      fn();
    } catch (e) {
      console.error('work board render listener failed', e);
    }
  });
  // Pages below the board (the run history) wait for its first render before scrolling to a linked row. The marker
  // covers one that armed its listener after this render already fired the event.
  if (host && typeof host.setAttribute === 'function') host.setAttribute('data-rendered', '1');
  if (typeof document.dispatchEvent === 'function') document.dispatchEvent(new CustomEvent('sova:board-rendered'));
}

// The board redraws from scratch on every change (a selection, an action's state, a reload), which removes the
// element the keyboard was on. The control is identified by its action and arguments (the same markup comes
// back), plus its card for one inside a card, so focus returns to the same control after the redraw.
function _focusedControl(host) {
  var el = document.activeElement;
  if (!el || el === document.body || !host.contains(el)) return null;
  var control = el.closest('[data-action]');
  if (!control) return null;
  var card = control.parentElement && control.parentElement.closest('[data-action="work-select"]');
  return {
    action: control.getAttribute('data-action'),
    args: control.getAttribute('data-args'),
    cardArgs: card ? card.getAttribute('data-args') : null,
  };
}

function _restoreFocus(host, focused) {
  if (!focused) return;
  var match = Array.prototype.find.call(host.querySelectorAll('[data-action]'), function(el) {
    if (el.getAttribute('data-action') !== focused.action || el.getAttribute('data-args') !== focused.args) return false;
    var card = el.parentElement && el.parentElement.closest('[data-action="work-select"]');
    return (card ? card.getAttribute('data-args') : null) === focused.cardArgs;
  });
  // A control the new board no longer has (its card moved on, or the button left with the state) is not
  // replaced by another one: focus then falls back to the page, as it would for any removed element.
  if (match && typeof match.focus === 'function') match.focus({ preventScroll: true });
}

// A link from the activity feed (`/work?card=N`, or `card=pr:N` for a PR with no issue) selects that card, which
// opens its details, and brings it into view. Once per page load: later refreshes leave the user's selection alone.
var _linkedCardDone = false;

// The card element for a selection key, found by comparing each card's args (a PR key's quotes cannot go in a selector).
function _findCard(key) {
  var cards = document.querySelectorAll('#work-board [data-action="work-select"]');
  return Array.prototype.find.call(cards, function(card) {
    try {
      return JSON.parse(card.getAttribute('data-args'))[0] === key;
    } catch (_e) {
      return false;
    }
  }) || null;
}

function _openLinkedCard() {
  if (_linkedCardDone) return;
  _linkedCardDone = true;
  var params = new URLSearchParams((window.location && window.location.search) || '');
  var wanted = params.get('card');
  if (!wanted) return;
  // Applied once: a reload or a bookmark must not reselect it over what the user has done since.
  params.delete('card');
  var rest = params.toString();
  try {
    window.history.replaceState(null, '', window.location.pathname + (rest ? '?' + rest : '') + window.location.hash);
  } catch (_e) { /* a restricted context keeps the parameter */ }
  var key = /^pr:\d+$/.test(wanted) ? wanted : (/^\d+$/.test(wanted) ? Number(wanted) : null);
  var card = key === null ? null : _findCard(key);
  if (!card) {
    // Closed, filtered out or in a hidden column: say so instead of opening an empty details pane, and narrow the
    // run history to the item (the link carries no `number`), which is where its runs are.
    var number = String(wanted).replace(/^pr:/, '');
    if (/^\d+$/.test(number) && typeof document.dispatchEvent === 'function') {
      document.dispatchEvent(new CustomEvent('sova:linked-card-missing', { detail: { number: number } }));
    }
    showToast('That item is not on the board. Its runs are in the history below.', 'info');
    return;
  }
  select(key);
  card.scrollIntoView({ block: 'nearest', inline: 'nearest' });
}

export function setDragging(on) {
  _dragging = on;
  if (!on && _redrawAfterDrag) {
    _redrawAfterDrag = false;
    renderBoard();
  }
}

export function toggleGroup(stage) {
  _openGroups[stage] = !_openGroups[stage];
  renderBoard();
}

export function toggleHidden() {
  _showHidden = !_showHidden;
  renderBoard();
}

export function setMilestone(value) {
  _milestone = value || '';
  safeStorageSet('sova-work-milestone', _milestone);
  return loadBoard();
}

export function setOwnerFilter(value) {
  var next = value === 'mine' ? 'mine' : 'all';
  if (next !== _ownerFilter) clearSelection();
  _ownerFilter = next;
  safeStorageSet('sova-work-owner-filter', _ownerFilter);
  document.querySelectorAll('#work-owner-filter [data-owner-filter]').forEach(function(button) {
    var active = button.getAttribute('data-owner-filter') === _ownerFilter;
    button.setAttribute('aria-pressed', active ? 'true' : 'false');
    button.classList.toggle('bg-gray-700', active);
    button.classList.toggle('text-white', active);
  });
  renderBoard();
  if (_board || _load) loadBoard();
}

// Socket ----------------------------------------------------------------------

// The visibility last reported on the current socket, so a repeated visibilitychange sends nothing new.
var _reportedVisible = null;
// The last handoff content seen on the current socket, as one string; null until the first one after an open.
var _handoffSignature = null;
// Clock-driven handoff fields: they advance without the handoff changing, so the signature leaves them out. The
// same set as _VOLATILE_HANDOFF_KEYS in routers/agents.py, where the server's own change digest drops them.
var _VOLATILE_HANDOFF_KEYS = ['age_seconds', 'elapsed_seconds', 'elapsed_formatted', 'snapshot'];

function _handoffContent(value) {
  if (Array.isArray(value)) return value.map(_handoffContent);
  if (value === null || typeof value !== 'object') return value;
  var out = {};
  Object.keys(value).forEach(function(key) {
    if (_VOLATILE_HANDOFF_KEYS.indexOf(key) === -1) out[key] = _handoffContent(value[key]);
  });
  return out;
}
// The latest status_update run per run id, for the running cards' time in step and last output.
var _runStatus = {};
// When each _runStatus entry was received, so a redraw between pushes can advance its durations.
var _runStatusAt = {};

// The cached status with both durations advanced by the time since it was received (advanceRunStatus, app.js).
function _advancedRun(runId) {
  return advanceRunStatus(_runStatus[runId], _runStatusAt[runId]);
}

// A running card's time in step and last output line, filled from the latest status_update and patched in place by
// every later one (_patchRunActivity), so the 2 s push never redraws the board. Empty until the first push.
function _runActivity(runId) {
  if (!runId) return '';
  return '<div class="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-xs text-gray-400" data-run-activity="' +
    escapeHtml(String(runId)) + '">' + runActivityHtml(_advancedRun(runId)) + '</div>';
}

function _patchRunActivity() {
  document.querySelectorAll('[data-run-activity]').forEach(function(el) {
    el.innerHTML = runActivityHtml(_advancedRun(el.getAttribute('data-run-activity')));
  });
}

function _handleMessage(msg) {
  if (msg.type === 'work_items_changed' || msg.type === 'agent_finished' || msg.type === 'graph_invalidated') {
    loadBoard();
    return;
  }
  if (msg.type === 'actions_changed') {
    _loadActions();
    return;
  }
  if (msg.type === 'handoff') {
    var signature = JSON.stringify(_handoffContent(msg.data === undefined ? null : msg.data));
    var changed = _handoffSignature !== null && signature !== _handoffSignature;
    _handoffSignature = signature;
    if (changed) loadBoard();
    return;
  }
  if (msg.type === 'status_update' && Array.isArray(msg.runs)) {
    _runStatus = {};
    _runStatusAt = {};
    var receivedAt = Date.now();
    msg.runs.forEach(function(r) { _runStatus[r.run_id] = r; _runStatusAt[r.run_id] = receivedAt; });
    _patchRunActivity();
  }
}

function _catchUp() {
  loadBoard();
  _loadActions();
}

function _sendVisibility(visible) {
  if (visible === _reportedVisible) return;
  if (sendSovaSocket({ type: 'work_visible', visible: visible })) _reportedVisible = visible;
}

function _reportVisibility() {
  _sendVisibility(!document.hidden);
}

function _onPageHide() {
  _sendVisibility(false);
}

function _onSocketOpen() {
  _reportedVisible = null;  // a new socket has no visibility on the server yet
  _handoffSignature = null;  // nor has this page seen a handoff on it; the catch-up below loads the board
  _reportVisibility();
  _catchUp();
}

export function init() {
  setOwnerFilter(_ownerFilter);
  var ownerFilter = document.getElementById('work-owner-filter');
  if (ownerFilter) ownerFilter.addEventListener('click', function(event) {
    var button = event.target.closest('[data-owner-filter]');
    if (button) setOwnerFilter(button.getAttribute('data-owner-filter'));
  });
  onActionStateChange(renderBoard);
  onSelectionChange(renderBoard);
  var host = document.getElementById('work-board');
  if (host) {
    ['scrollend', 'wheel', 'touchstart', 'pointerdown', 'keydown'].forEach(function(type) {
      host.addEventListener(type, _releaseScrollHold, { passive: true });
    });
    host.addEventListener('change', function(e) {
      if (e.target && e.target.id === 'work-milestone') setMilestone(e.target.value);
    });
    // Enter or Space on a focused card selects it, like a click.
    host.addEventListener('keydown', function(e) {
      if ((e.key === 'Enter' || e.key === ' ') && e.target.classList && e.target.classList.contains('sova-work-card')) {
        e.preventDefault();
        e.target.click();
      }
    });
  }
  document.addEventListener('visibilitychange', _reportVisibility);
  if (typeof window.addEventListener === 'function') window.addEventListener('pagehide', _onPageHide);
  subscribeSovaSocket({
    onMessage: _handleMessage,
    onOpen: _onSocketOpen,
    onFallbackPoll: _catchUp,
  });
}
