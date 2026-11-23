import { actionAttrs, registerActions } from './actions.js';

// Run history: the History and Failed tabs share one filter bar, pager and table, in two views. "By issue" (the
// default) asks the server for groups (GET /work/history?group=issue): one row per issue, its state judged by the
// server from all of its runs, expandable to its runs. "Every run" is the flat list, one row per run under day headers.
// Page one refreshes when a run finishes (refreshHistoryAfterRun(), from live-status.js) and every 60 s while the
// browser tab is visible; a later page stays put and offers a "new runs" button instead, so rows never move under
// the user. A refresh asks for the current view with the current filters and keeps expanded groups expanded.

var _PAGE_SIZE = 25;
var _POLL_MS = 60000;
var _RANGES = ['today', '7d', '30d', 'all', 'custom'];
var _DEFAULT_RANGE = '7d';

var _VIEWS = ['issue', 'runs'];
// The chosen view is remembered per browser (app.js's storage helpers survive disabled storage).
var _VIEW_KEY = 'sova.history.view';
var _state = {
  tab: 'history', view: 'issue', range: _DEFAULT_RANGE, from: '', to: '', action: '', status: '', number: '', page: 0,
};
// Group keys the user opened; kept across refreshes and page loads, so a refresh never closes a group being read.
var _expanded = Object.create(null);
// The last answer on screen, so opening a group redraws without asking the server again.
var _lastData = null;
// The total behind the list on screen (runs, or groups in the group view), which the pager pages over.
var _loadedTotal = 0;
// The runs behind the list on screen, so a check on a later page can count the runs that finished since. Rows sort
// by start time, so a long run that started before this page's rows may finish onto page one later than it started.
var _loadedRuns = 0;
var _newRuns = 0;
var _loadedBounds = { since: null, until: null };
// The day totals the server gave for the days a page splits (day key to { runs, cost }, or null when they could not be
// read), so a day header never shows a page's share as the day's count and cost.
var _dayTotals = Object.create(null);
// Every page load takes a number; an answer that arrives after a newer request was sent is dropped.
var _requestSeq = 0;
var _refreshTimer = null;
var _numberTimer = null;

var _WHEN = new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' });
var _WHEN_WITH_YEAR = new Intl.DateTimeFormat(undefined, {
  year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
});
var _FULL = new Intl.DateTimeFormat(undefined, {
  weekday: 'short', year: 'numeric', month: 'short', day: 'numeric',
  hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23', timeZoneName: 'short',
});
var _DAY = new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric' });
var _DAY_WITH_YEAR = new Intl.DateTimeFormat(undefined, { year: 'numeric', month: 'short', day: 'numeric' });

function _el(id) { return document.getElementById(id); }

function _localMidnight(date, dayOffset) {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate() + dayOffset);
}

function _parseLocalDate(value) {
  var parts = String(value || '').split('-').map(Number);
  if (parts.length !== 3 || parts.some(isNaN)) return null;
  return new Date(parts[0], parts[1] - 1, parts[2]);
}

// The chosen range as local midnights, `until` exclusive: "Today" starts at the user's own midnight, and the server
// receives instants, so it needs no time zone.
function _windowBounds() {
  var now = new Date();
  if (_state.range === 'today') return { since: _localMidnight(now, 0), until: null };
  if (_state.range === '7d') return { since: _localMidnight(now, -6), until: null };
  if (_state.range === '30d') return { since: _localMidnight(now, -29), until: null };
  if (_state.range === 'custom') {
    var to = _parseLocalDate(_state.to);
    return { since: _parseLocalDate(_state.from), until: to ? _localMidnight(to, 1) : null };
  }
  return { since: null, until: null };
}

function _numberFilter() {
  return _state.number.trim().replace(/^#/, '').trim();
}

// `grouped` false asks for the flat list whatever the view: the new-run probe counts runs, which a group count hides.
function _query(limit, offset, bounds, grouped) {
  var params = ['limit=' + limit, 'offset=' + offset];
  if (grouped) params.push('group=issue');
  if (bounds.since) params.push('since=' + encodeURIComponent(bounds.since.toISOString()));
  if (bounds.until) params.push('until=' + encodeURIComponent(bounds.until.toISOString()));
  if (_state.action) params.push('action=' + encodeURIComponent(_state.action));
  var status = _state.tab === 'failed' ? 'failed' : _state.status;
  if (status) params.push('status=' + encodeURIComponent(status));
  var number = _numberFilter();
  if (number) params.push('number=' + encodeURIComponent(number));
  return '/work/history?' + params.join('&');
}

async function loadHistory() {
  var seq = ++_requestSeq;
  var data;
  // Pinned for the check on a later page: Today, 7d and 30d are recomputed from the clock, and a window that
  // slid past midnight would shrink the total and hide a new run.
  var bounds = _windowBounds();
  try {
    data = await fetchAPI(apiUrl(_query(_PAGE_SIZE, _state.page * _PAGE_SIZE, bounds, _state.view === 'issue')));
  } catch (e) {
    if (seq !== _requestSeq) return;
    console.error('Failed to load history:', e);
    var table = _el('history-table');
    table._renderKey = null;
    renderSectionError('history-table', 'Could not load the run history. It retries on the next refresh.');
    return;
  }
  if (seq !== _requestSeq) return;
  var dayTotals = _state.view === 'issue' ? Object.create(null) : await _splitDayTotals(data, bounds);
  if (seq !== _requestSeq) return;
  _dayTotals = dayTotals;
  _loadedTotal = (data && data.total) || 0;
  _loadedRuns = _runsOf(data);
  _loadedBounds = bounds;
  _newRuns = 0;
  _render(data || {});
}

// A page of the flat list is sorted by start time, so only its first row (on a later page) and its last row (when more
// follow) can share a day with rows on another page. For those days, ask the server for the day's own count and cost
// under the same filters, instead of summing the rows on screen. A day it cannot read maps to null: its header then
// shows the date only, never a page's share passed off as the day's total.
async function _splitDayTotals(data, bounds) {
  var totals = Object.create(null);
  var tasks = data && Array.isArray(data.tasks) ? data.tasks : [];
  if (!tasks.length) return totals;
  var edges = [];
  if (_state.page > 0) edges.push(tasks[0]);
  if (_state.page * _PAGE_SIZE + tasks.length < (data.total || 0)) edges.push(tasks[tasks.length - 1]);
  await Promise.all(edges.map(async function(t) {
    var key = _dayKey(t);
    if (!key || key in totals) return;
    totals[key] = null;
    var start = new Date(t.started_at);
    var day = { since: _localMidnight(start, 0), until: _localMidnight(start, 1) };
    try {
      var answer = await fetchAPI(apiUrl(_query(1, 0, day, false)));
      if (answer && typeof answer.total === 'number') {
        totals[key] = { runs: answer.total, cost: parseFloat(answer.total_cost_usd || 0) };
      }
    } catch (e) {
      console.error('Failed to load the day total:', e);
    }
  }));
  return totals;
}

function _runsOf(data) {
  if (!data) return 0;
  return (_state.view === 'issue' ? data.runs_total : data.total) || 0;
}

// On a later page: count the runs that finished since it loaded, without moving its rows.
async function _checkForNewRuns() {
  var seq = _requestSeq;
  var data;
  try {
    data = await fetchAPI(apiUrl(_query(1, 0, _loadedBounds, false)));
  } catch (e) {
    return;
  }
  if (seq !== _requestSeq || _state.page === 0) return;
  _newRuns = Math.max(0, ((data && data.total) || 0) - _loadedRuns);
  _renderNewRuns();
}

function _refreshHistory() {
  if (_state.page === 0) loadHistory();
  else _checkForNewRuns();
}

// A run finished (the agent_finished status message). Debounced: a batch finishing sends one message per agent.
export function refreshHistoryAfterRun() {
  if (_refreshTimer) clearTimeout(_refreshTimer);
  _refreshTimer = setTimeout(function() {
    _refreshTimer = null;
    _refreshHistory();
  }, 300);
}

function _reload() {
  if (_numberTimer) { clearTimeout(_numberTimer); _numberTimer = null; }
  _state.page = 0;
  _newRuns = 0;
  // The old total belongs to the old filters: zero it so Next cannot advance on it until the new result arrives.
  _loadedTotal = 0;
  _el('history-prev').disabled = true;
  _el('history-next').disabled = true;
  _updateFilterControls();
  loadHistory();
}

function switchTab(tab) {
  _state.tab = tab === 'failed' ? 'failed' : 'history';
  ['history', 'failed'].forEach(function(t) {
    _el('tab-' + t).className = t === _state.tab
      ? 'px-4 py-2.5 text-sm transition-colors border-b-2 border-accent text-white'
      : 'px-4 py-2.5 text-sm transition-colors border-b-2 border-transparent text-gray-500 hover:text-gray-300';
  });
  _reload();
}

function _setView(view) {
  if (_VIEWS.indexOf(view) === -1 || view === _state.view) return;
  _state.view = view;
  safeStorageSet(_VIEW_KEY, view);
  _lastData = null;
  _reload();
}

function _toggleGroup(key) {
  if (_expanded[key]) delete _expanded[key]; else _expanded[key] = true;
  if (_lastData) _render(_lastData);
}

function _setRange(range) {
  if (_RANGES.indexOf(range) === -1) return;
  _state.range = range;
  if (range === 'custom' && !_state.from && !_state.to) {
    var now = new Date();
    _state.from = _isoDate(_localMidnight(now, -6));
    _state.to = _isoDate(now);
    _el('history-from').value = _state.from;
    _el('history-to').value = _state.to;
  }
  _reload();
}

// An inverted custom range (From after To) would only ever show an empty table: swap the two dates.
function _orderRange() {
  if (!_state.from || !_state.to || _state.from <= _state.to) return;
  var from = _state.from;
  _state.from = _state.to;
  _state.to = from;
  _el('history-from').value = _state.from;
  _el('history-to').value = _state.to;
}

function _isoDate(d) {
  return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
}

function _clearFilters() {
  _state.range = _DEFAULT_RANGE;
  _state.from = ''; _state.to = ''; _state.action = ''; _state.status = ''; _state.number = '';
  ['history-from', 'history-to', 'filter-action', 'filter-status', 'filter-number'].forEach(function(id) {
    _el(id).value = '';
  });
  _reload();
}

function _hasFilter() {
  return _state.range !== _DEFAULT_RANGE || !!_state.action || !!_numberFilter()
    || (!!_state.status && _state.tab === 'history');
}

function _updateFilterControls() {
  _VIEWS.forEach(function(v) {
    _el('history-view-' + v).setAttribute('aria-pressed', v === _state.view ? 'true' : 'false');
  });
  _RANGES.forEach(function(r) {
    _el('history-range-' + r).setAttribute('aria-pressed', r === _state.range ? 'true' : 'false');
  });
  _el('history-custom-range').classList.toggle('hidden', _state.range !== 'custom');
  _el('filter-status').classList.toggle('hidden', _state.tab === 'failed');
  _el('history-clear').classList.toggle('hidden', !_hasFilter());
}

function _fillActionOptions() {
  var table = window.SOVA_RUN_ACTIONS;
  var actions = (table && table.actions) || {};
  _el('filter-action').innerHTML = '<option value="">All actions</option>' + Object.keys(actions).map(
    (key) => `<option value="${escapeHtml(key)}">${escapeHtml(actions[key].label)}</option>`
  ).join('');
}

function _formatCount(n) {
  return Number(n || 0).toLocaleString('en-US');
}

// One money format for the header total and every row: thousands separators and two decimals, and a cost above zero
// that rounds to zero reads "<$0.01" rather than a misleading $0.00. Plain text: the caller escapes it.
function _formatCost(value) {
  var n = parseFloat(value || 0);
  if (n > 0 && n < 0.005) return '<$0.01';
  return '$' + (n > 0 ? n : 0).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function _formatRunCost(value) {
  var text = _formatCost(value);
  return text === '$0.00' ? '<span class="text-gray-500">$0.00</span>' : escapeHtml(text);
}

function _rangeLabel() {
  if (_state.range === 'today') return 'today';
  if (_state.range === '7d') return 'in the last 7 days';
  if (_state.range === '30d') return 'in the last 30 days';
  if (_state.range !== 'custom') return '';
  var from = _parseLocalDate(_state.from);
  var to = _parseLocalDate(_state.to);
  if (from && to && from.getTime() === to.getTime()) return 'on ' + _DAY.format(from);
  if (from && to) return _DAY.format(from) + ' to ' + _DAY.format(to);
  if (from) return 'since ' + _DAY.format(from);
  if (to) return 'until ' + _DAY.format(to);
  return '';
}

function _plural(n, noun) {
  return _formatCount(n) + ' ' + noun + (n === 1 ? '' : 's');
}

function _totalsHtml(data, total) {
  var bold = function(text) { return '<span class="font-semibold text-white">' + escapeHtml(text) + '</span>'; };
  var scope = ' <span class="text-xs text-gray-500 ml-1">' + escapeHtml(_rangeLabel()) + '</span>';
  var cost = ', ' + bold(_formatCost(data.total_cost_usd));
  if (_state.view !== 'issue') {
    return bold(_formatCount(total)) + escapeHtml(_state.tab === 'failed' ? (total === 1 ? ' failed run' : ' failed runs')
      : (total === 1 ? ' run' : ' runs')) + cost + scope;
  }
  if (_state.tab === 'failed') {
    return bold(_formatCount(total)) + escapeHtml(total === 1 ? ' issue or PR with failed runs' : ' issues and PRs with failed runs') +
      cost + scope;
  }
  var sum = data.summary || {};
  // A row is an issue, or a PR with no issue (a Dependabot merge), or a lone run, so the count is named for all of them.
  return bold(_formatCount(total)) + escapeHtml(total === 1 ? ' issue or PR' : ' issues and PRs') + ', ' +
    bold(_formatCount(sum.merged)) + ' merged, ' + bold(_formatCount(sum.need_you)) + ' need you, ' +
    bold(_formatCount(sum.failed)) + ' failed' + cost + scope;
}

function _render(data) {
  _lastData = data;
  var grouped = _state.view === 'issue';
  var list = grouped ? data.groups : data.tasks;
  var items = Array.isArray(list) ? list : [];
  var total = data.total || 0;

  _el('tab-failed-count').textContent = data.failed_total > 0 ? '(' + _formatCount(data.failed_total) + ')' : '';
  // Built only from escaped text and literals (_totalsHtml), drawn the way the table is.
  var totals = _totalsHtml(data, total);
  renderIfChanged(_el('history-totals'), totals, totals);

  var first = _state.page * _PAGE_SIZE;
  _el('history-pager').classList.toggle('hidden', total <= _PAGE_SIZE && _state.page === 0);
  _el('history-page-info').textContent = items.length
    ? _formatCount(first + 1) + ' to ' + _formatCount(first + items.length) + ' of ' + _formatCount(total) : '';
  _el('history-prev').disabled = _state.page === 0;
  _el('history-next').disabled = first + items.length >= total;

  _renderNewRuns();
  var html = !items.length ? _emptyTable() : grouped ? renderGroups(items) : renderHistoryTable(items, true);
  renderIfChanged(_el('history-table'), html, html);
}

function _renderNewRuns() {
  var show = _newRuns > 0 && _state.page > 0;
  _el('history-new-runs').classList.toggle('hidden', !show);
  _el('history-new-runs-btn').textContent = show
    ? _newRuns + (_newRuns === 1 ? ' run' : ' runs') + ' finished since this page loaded, back to page 1' : '';
}

function _statusLabel(status) {
  var s = String(status || '').replace(/_/g, ' ');
  return s.charAt(0).toUpperCase() + s.slice(1);
}

function _clip(text, max) {
  var s = String(text);
  return s.length > max ? s.slice(0, max - 3) + '...' : s;
}

function _whenCell(t) {
  if (!t.started_at) return '<span class="text-gray-500">--</span>';
  var start = new Date(t.started_at);
  var end = t.ended_at ? new Date(t.ended_at) : null;
  var label = (start.getFullYear() === new Date().getFullYear() ? _WHEN : _WHEN_WITH_YEAR).format(start);
  var tip = 'Started: ' + _FULL.format(start) + '\nEnded: ' + (end ? _FULL.format(end) : 'not recorded');
  return '<span data-tooltip="' + escapeHtml(tip) + '" data-tooltip-pos="right" data-tooltip-multiline>' +
    escapeHtml(label) + '</span>';
}

// A failed run reads as a red outlined pill, never a status column: its result line is red too, and Re-run sits beside it.
function _pillBorder(failed) {
  return failed ? 'border border-accent-red' : 'border border-transparent';
}

function _rerunButton(t) {
  var canRerun = (t.status === 'failed' || t.status === 'interrupted' || t.status === 'stopped') && t.issue_number;
  if (!canRerun) return '';
  return '<button type="button" class="rerun-btn sova-btn sova-btn-warning" data-action="rerun" ' +
    'data-issue="' + escapeHtml(String(t.issue_number)) + '" data-role="' + escapeHtml(t.role || '') + '" ' +
    'data-pr="' + escapeHtml(String(parseInt(t.pr_number, 10) || '')) + '" ' +
    'data-run-id="' + escapeHtml(String(t.id)) + '" data-tooltip="Re-run this task">Re-run</button>';
}

function _emptyTable() {
  return '<div class="bg-surface-card rounded-lg border border-gray-700/50 p-8 flex flex-col items-center gap-3">' +
    '<p class="text-gray-500 text-sm">No runs match these filters.</p>' +
    (_hasFilter() ? '<button type="button" class="sova-btn sova-btn-primary" data-action="history-clear">Clear filters</button>' : '') +
    '</div>';
}

// One line of text that truncates with an ellipsis; the full text is the tooltip. The tooltip sits on a wrapper
// that does not clip, because the truncating element's own overflow would cut the bubble off. Below the sm
// breakpoint the table scrolls inside an overflow-x-auto box, which would clip the bubble, so there the text wraps
// in full and the tooltip is off (data-tooltip-desktop in style.css).
function _truncated(text, cls) {
  return '<span class="block min-w-0" data-tooltip="' + escapeHtml(_clip(text, 600)) +
    '" data-tooltip-align="start" data-tooltip-desktop><span class="block truncate ' + cls + '">' +
    escapeHtml(text) + '</span></span>';
}

function _resultColor(failed) {
  return failed ? 'text-accent-red' : 'text-gray-400';
}

// A run's local calendar day, '' for a run with no start time. Days are the viewer's, not UTC.
function _dayKey(t) {
  return t.started_at ? _isoDate(new Date(t.started_at)) : '';
}

function _dayLabel(t) {
  var d = new Date(t.started_at);
  var now = new Date();
  if (_isoDate(d) === _isoDate(now)) return 'Today';
  if (_isoDate(d) === _isoDate(_localMidnight(now, -1))) return 'Yesterday';
  return (d.getFullYear() === now.getFullYear() ? _DAY : _DAY_WITH_YEAR).format(d);
}

var _COLUMNS = 7;

function _runRows(tasks, withDays) {
  var prefix = window.SOVA_PROJECT_SLUG ? '/p/' + window.SOVA_PROJECT_SLUG : '';
  var muted = '<span class="text-gray-500">--</span>';
  var td = 'px-3 py-2 whitespace-nowrap';
  var day = null;
  // Each day header carries the day's run count and cost: summed from the rows on this page for a day the page holds
  // whole, and the server's own totals (_dayTotals) for a day that continues on another page.
  var days = Object.create(null);
  if (withDays) {
    tasks.forEach(function(t) {
      var d = days[_dayKey(t)] || (days[_dayKey(t)] = { runs: 0, cost: 0 });
      d.runs++;
      d.cost += parseFloat(t.total_cost_usd || 0);
    });
    Object.keys(_dayTotals).forEach(function(key) { days[key] = _dayTotals[key]; });
  }
  return tasks.map(function(t) {
    var colors = runActionColors(t.action_color);
    var nav = actionAttrs('navigate', [prefix + '/work/' + t.id]);
    var out = '';
    var key = _dayKey(t);
    if (withDays && key !== day) {
      day = key;
      if (day) {
        out += '<tr><td colspan="' + _COLUMNS + '" class="px-3 pt-3 pb-1 text-xs font-medium uppercase tracking-wider ' +
          'text-gray-500 border-t border-gray-700/30">' + escapeHtml(_dayLabel(t)) +
          (days[day] ? '<span class="ml-2 normal-case tracking-normal font-normal text-gray-600">' +
            escapeHtml(_plural(days[day].runs, 'run') + ', ' + _formatCost(days[day].cost)) + '</span>' : '') +
          '</td></tr>';
      }
    }
    var hasResult = !!t.outcome;
    out += '<tr class="' + (hasResult ? '' : 'border-b border-transparent ') +
        'border-t border-gray-700/30 hover:bg-surface-hover cursor-pointer transition-colors"' + nav + '>' +
      '<td class="' + td + ' text-gray-300">' + _whenCell(t) + '</td>' +
      '<td class="' + td + '">' + (t.issue_number ? issueLink(t.issue_number) : muted) + '</td>' +
      '<td class="' + td + '"><a href="' + escapeHtml(prefix + '/work/' + t.id) + '" data-action="stop" ' +
        'class="inline-flex px-2 py-0.5 rounded text-xs font-medium hover:underline ' + _pillBorder(t.status === 'failed') +
        ' ' + colors.bg + ' ' + colors.text + '">' + escapeHtml(t.action_label || t.role || '') + '</a></td>' +
      '<td class="' + td + '">' + (t.pr_number ? prLink(t.pr_number, { bare: true }) : muted) + '</td>' +
      '<td class="' + td + ' text-right text-gray-400">' + escapeHtml(t.duration_formatted || '--') + '</td>' +
      '<td class="' + td + ' text-right text-gray-200">' + _formatRunCost(t.total_cost_usd) + '</td>' +
      '<td class="px-3 py-1.5 text-right">' + _rerunButton(t) + '</td>' +
    '</tr>';
    if (hasResult) {
      out += '<tr class="hover:bg-surface-hover cursor-pointer transition-colors"' + nav + '>' +
        '<td colspan="' + _COLUMNS + '" class="px-3 pb-2 pt-0 text-xs max-w-0">' +
        _truncated(t.outcome, _resultColor(t.status === 'failed')) + '</td></tr>';
    }
    return out;
  }).join('');
}

// The flat run list, under day headers. `card` false inside an expanded group, which already sits in the group's card
// and shows no day headers.
function renderHistoryTable(tasks, card) {
  if (!tasks.length) return _emptyTable();
  var th = 'font-medium px-3 py-2.5';
  var table = '<table class="w-full text-sm tabular-nums">' +
    '<thead><tr class="text-gray-500 text-xs uppercase tracking-wider border-b border-gray-700/50">' +
      '<th class="text-left ' + th + ' w-36">When</th>' +
      '<th class="text-left ' + th + ' w-24">Issue</th>' +
      '<th class="text-left ' + th + '">Action</th>' +
      '<th class="text-left ' + th + ' w-24">PR</th>' +
      '<th class="text-right ' + th + ' w-28">Duration</th>' +
      '<th class="text-right ' + th + ' w-24">Cost</th>' +
      '<th class="px-3 py-2.5 w-24"></th>' +
    '</tr></thead><tbody>' + _runRows(tasks, card) + '</tbody></table>';
  // No overflow-hidden on the card: it would clip the cell tooltips. A phone scrolls the table inside its own box
  // instead of the page.
  return card
    ? '<div class="bg-surface-card rounded-lg border border-gray-700/50 overflow-x-auto sm:overflow-visible">' + table + '</div>'
    : '<div class="overflow-x-auto sm:overflow-visible">' + table + '</div>';
}

var _REVIEW_ROUND = { 'review': true, 'address-review': true, 'rebase': true };

// The runs of a group, oldest first, as pills. A stretch of three or more consecutive Review, Address review and
// Rebase runs that holds at least one Review collapses into one "N review rounds" pill, N the Review runs in it; a
// stretch with no Review run (N would be 0) and every other action stay one pill per run. A different action in between splits stretches, each judged on its own.
function _segments(runsNewestFirst) {
  var runs = runsNewestFirst.slice().reverse();
  var segments = [];
  var i = 0;
  while (i < runs.length) {
    var j = i;
    while (j < runs.length && _REVIEW_ROUND[runs[j].action]) j++;
    if (j === i) { segments.push({ run: runs[i] }); i++; continue; }
    var stretch = runs.slice(i, j);
    var reviews = stretch.filter(function(r) { return r.action === 'review'; }).length;
    if (stretch.length >= 3 && reviews > 0) {
      segments.push({ chain: stretch, reviews: reviews });
    } else {
      stretch.forEach(function(r) { segments.push({ run: r }); });
    }
    i = j;
  }
  return segments;
}

function _pill(label, color, tip, failed) {
  var colors = runActionColors(color);
  return '<span class="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium ' + _pillBorder(failed) + ' ' +
    colors.bg + ' ' + colors.text + '"' + (tip ? ' data-tooltip="' + escapeHtml(tip) + '" data-tooltip-align="start"' : '') +
    '>' + escapeHtml(label) + '</span>';
}

function _pills(runs) {
  return _segments(runs).map(function(seg) {
    if (seg.chain) {
      var names = seg.chain.map(function(r) { return r.action_label || r.role || ''; }).join(', ');
      var color = seg.chain.filter(function(r) { return r.action === 'review'; })[0].action_color;
      var failed = seg.chain.some(function(r) { return r.status === 'failed'; });
      return _pill(_plural(seg.reviews, 'review round'), color, _plural(seg.chain.length, 'run') + ': ' + names, failed);
    }
    var r = seg.run;
    return _pill(r.action_label || r.role || '', r.action_color, _statusLabel(r.status), r.status === 'failed');
  }).join('');
}

function _groupLabel(g) {
  if (g.issue_number) return issueLink(g.issue_number);
  if (g.pr_number) return prLink(g.pr_number);
  var run = g.runs[0] || {};
  return '<span class="text-gray-300">' + escapeHtml(run.action_label || run.role || 'Run') + '</span>';
}

// What a group row stands for, from its key: an issue, a PR, or a lone run.
function _groupNoun(key) {
  if (key.indexOf('issue:') === 0) return 'issue';
  if (key.indexOf('pr:') === 0) return 'PR';
  return 'run';
}

function _groupRow(g) {
  var open = !!_expanded[g.key];
  var latest = g.runs[0] || {};
  var result = g.result || {};
  var meta = [
    escapeHtml(_plural(g.run_count, 'run')),
    escapeHtml(g.duration_formatted || '--'),
    _formatRunCost(g.total_cost_usd),
    g.last_started_at ? escapeHtml(_WHEN.format(new Date(g.last_started_at))) : '',
  ].filter(Boolean).join('<span class="text-gray-700">/</span>');
  var rerun = _rerunButton(latest);
  var toggle = actionAttrs('history-toggle', [g.key]);
  return '<div class="border-t first:border-t-0 border-gray-700/30" data-group="' + escapeHtml(g.key) + '">' +
    '<div class="px-3 py-2.5 hover:bg-surface-hover cursor-pointer transition-colors"' + toggle + '>' +
      '<div class="flex items-center gap-2 min-w-0">' +
        '<button type="button" class="shrink-0 w-5 text-gray-500 hover:text-gray-200" aria-expanded="' + open + '" ' +
          'aria-label="' + (open ? 'Hide' : 'Show') + ' the runs of this ' + _groupNoun(g.key) + '"' + toggle + '>' +
          '<span class="inline-block w-1.5 h-1.5 border-r-2 border-b-2 border-current ' + (open ? 'rotate-45' : '-rotate-45') +
          '"></span></button>' +
        '<span class="shrink-0 text-sm font-medium">' + _groupLabel(g) + '</span>' +
        (g.title ? '<span class="min-w-0 flex-1 text-sm text-gray-300">' + _truncated(g.title, '') + '</span>'
          : '<span class="flex-1"></span>') +
        '<span class="shrink-0 px-2 py-0.5 rounded text-xs font-medium ' + stateBadgeClasses(g.state_color) + '">' +
          escapeHtml(g.state_label) + '</span>' +
      '</div>' +
      '<div class="flex flex-wrap items-center gap-x-3 gap-y-1.5 mt-1.5 pl-7">' +
        '<span class="inline-flex flex-wrap items-center gap-1">' + _pills(g.runs) + '</span>' +
        '<span class="inline-flex flex-wrap items-center gap-x-2 text-xs text-gray-500 tabular-nums">' + meta + '</span>' +
        (rerun ? '<span class="ml-auto">' + rerun + '</span>' : '') +
      '</div>' +
      (result.text ? '<div class="mt-1 pl-7 text-xs">' + _truncated(result.text, _resultColor(result.color === 'red')) +
        '</div>' : '') +
    '</div>' +
    (open ? '<div class="border-t border-gray-700/30 bg-surface pl-4">' + renderHistoryTable(g.runs, false) + '</div>' : '') +
  '</div>';
}

// The grouped view: one card of rows, one row per issue (or PR, or lone run), newest activity first.
function renderGroups(groups) {
  return '<div class="bg-surface-card rounded-lg border border-gray-700/50">' + groups.map(_groupRow).join('') + '</div>';
}

// A start-aligned bubble opens from its host's left edge and is at least 14rem wide (style.css), so a small host in the
// right half of a narrow screen would push it past the viewport edge. On hover or focus, flip it to open from the
// host's right edge (data-tooltip-flip) when it would not fit on the right but would on the left.
var _TIP_FLOOR = 224;
var _TIP_MAX = 320;
var _TIP_MARGIN = 8;

function _flipTooltip(e) {
  var host = e.target && e.target.closest ? e.target.closest('[data-tooltip-align="start"]') : null;
  if (!host || !host.getBoundingClientRect) return;
  var rect = host.getBoundingClientRect();
  var width = Math.min(_TIP_MAX, Math.max(rect.width, _TIP_FLOOR));
  var overflowsRight = rect.left + width > window.innerWidth - _TIP_MARGIN;
  var fitsLeft = rect.right - width >= _TIP_MARGIN;
  if (overflowsRight && fitsLeft) host.setAttribute('data-tooltip-flip', ''); else host.removeAttribute('data-tooltip-flip');
}

export function init() {
  registerActions({
    'switch-tab': function(el, e, tab) { switchTab(tab); },
    'history-range': function(el, e, range) { _setRange(range); },
    'history-view': function(el, e, view) { _setView(view); },
    'history-toggle': function(el, e, key) { _toggleGroup(key); },
    'history-clear': function() { _clearFilters(); },
    'history-prev': function() {
      if (_state.page > 0) { _state.page--; loadHistory(); }
    },
    'history-next': function() {
      if ((_state.page + 1) * _PAGE_SIZE < _loadedTotal) { _state.page++; loadHistory(); }
    },
    'history-first-page': function() { _reload(); },
  });

  _fillActionOptions();
  var savedView = safeStorageGet(_VIEW_KEY);
  if (_VIEWS.indexOf(savedView) !== -1) _state.view = savedView;
  _updateFilterControls();
  _el('history-table').addEventListener('mouseover', _flipTooltip);
  _el('history-table').addEventListener('focusin', _flipTooltip);
  _el('filter-action').addEventListener('change', function(e) { _state.action = e.target.value; _reload(); });
  _el('filter-status').addEventListener('change', function(e) { _state.status = e.target.value; _reload(); });
  _el('history-from').addEventListener('change', function(e) { _state.from = e.target.value; _orderRange(); _reload(); });
  _el('history-to').addEventListener('change', function(e) { _state.to = e.target.value; _orderRange(); _reload(); });
  _el('filter-number').addEventListener('input', function(e) {
    // Copied into _state only when the debounce fires, so a new-run check inside the window still queries with
    // the filter the loaded page was fetched with.
    var typed = e.target.value;
    if (_numberTimer) clearTimeout(_numberTimer);
    _numberTimer = setTimeout(function() { _numberTimer = null; _state.number = typed; _reload(); }, 300);
  });

  // Loads page one now, refreshes it every 60 s and on the tab's return, and pauses while the tab is hidden.
  visibilityAwarePoll(_refreshHistory, _POLL_MS);
}
