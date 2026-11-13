import { actionAttrs, registerActions } from './actions.js';

// Run history: the History and Failed tabs share one filter bar, pager and table. Page one refreshes when a run
// finishes (refreshHistoryAfterRun(), from live-status.js) and every 60 s while the browser tab is visible; a later
// page stays put and offers a "new runs" button instead, so rows never move under the user.

var _PAGE_SIZE = 25;
var _POLL_MS = 60000;
var _RANGES = ['today', '7d', '30d', 'all', 'custom'];
var _DEFAULT_RANGE = '7d';

var _state = { tab: 'history', range: _DEFAULT_RANGE, from: '', to: '', action: '', status: '', number: '', page: 0 };
// The total behind the list on screen, so a check on a later page can count the runs that finished since. Rows sort
// by start time, so a long run that started before this page's rows may finish onto page one later than it started.
var _loadedTotal = 0;
var _newRuns = 0;
var _loadedBounds = { since: null, until: null };
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

function _query(limit, offset, bounds) {
  var params = ['limit=' + limit, 'offset=' + offset];
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
    data = await fetchAPI(apiUrl(_query(_PAGE_SIZE, _state.page * _PAGE_SIZE, bounds)));
  } catch (e) {
    if (seq !== _requestSeq) return;
    console.error('Failed to load history:', e);
    var table = _el('history-table');
    table._renderKey = null;
    renderSectionError('history-table', 'Could not load the run history. It retries on the next refresh.');
    return;
  }
  if (seq !== _requestSeq) return;
  _loadedTotal = (data && data.total) || 0;
  _loadedBounds = bounds;
  _newRuns = 0;
  _render(data || {});
}

// On a later page: count the runs that finished since it loaded, without moving its rows.
async function _checkForNewRuns() {
  var seq = _requestSeq;
  var data;
  try {
    data = await fetchAPI(apiUrl(_query(1, 0, _loadedBounds)));
  } catch (e) {
    return;
  }
  if (seq !== _requestSeq || _state.page === 0) return;
  _newRuns = Math.max(0, ((data && data.total) || 0) - _loadedTotal);
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

function _render(data) {
  var tasks = Array.isArray(data.tasks) ? data.tasks : [];
  var total = data.total || 0;
  var failedTab = _state.tab === 'failed';

  _el('tab-failed-count').textContent = data.failed_total > 0 ? '(' + _formatCount(data.failed_total) + ')' : '';

  var noun = failedTab ? (total === 1 ? ' failed run' : ' failed runs') : (total === 1 ? ' run' : ' runs');
  var scope = _rangeLabel();
  _el('history-totals').innerHTML = '<span class="font-semibold text-white">' + escapeHtml(_formatCount(total)) +
    '</span>' + escapeHtml(noun) + ', <span class="font-semibold text-white">' +
    escapeHtml(_formatCost(data.total_cost_usd)) + '</span> <span class="text-xs text-gray-500 ml-1">' +
    escapeHtml(scope) + '</span>';

  var first = _state.page * _PAGE_SIZE;
  _el('history-pager').classList.toggle('hidden', total <= _PAGE_SIZE && _state.page === 0);
  _el('history-page-info').textContent = tasks.length
    ? _formatCount(first + 1) + ' to ' + _formatCount(first + tasks.length) + ' of ' + _formatCount(total) : '';
  _el('history-prev').disabled = _state.page === 0;
  _el('history-next').disabled = first + tasks.length >= total;

  _renderNewRuns();
  var html = renderHistoryTable(tasks, failedTab);
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

// The shared status dot and the status word. On History a run's error shows on hover (dotted underline); the Failed
// tab prints it under the row instead.
function _statusCell(t, showError) {
  var hasTip = !showError && t.error_message;
  var tip = hasTip
    ? ' data-tooltip="' + escapeHtml(_clip(t.error_message, 300)) + '" data-tooltip-wide data-tooltip-pos="top"' : '';
  var cls = (t.status === 'failed' ? 'text-accent-red' : 'text-gray-300') +
    (hasTip ? ' underline decoration-dotted decoration-gray-500 underline-offset-4 cursor-help' : '');
  return '<span class="inline-flex items-center gap-1.5"' + tip + '>' +
    '<span class="w-2 h-2 rounded-full shrink-0 ' + statusDot(t.status) + '"></span>' +
    '<span class="' + cls + '">' + escapeHtml(_statusLabel(t.status)) + '</span></span>';
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

function renderHistoryTable(tasks, showError) {
  if (!tasks.length) return _emptyTable();
  var prefix = window.SOVA_PROJECT_SLUG ? '/p/' + window.SOVA_PROJECT_SLUG : '';
  var muted = '<span class="text-gray-500">--</span>';
  var td = 'px-3 py-2 whitespace-nowrap';
  // No overflow-hidden on the card: it would clip the cell tooltips.
  return '<div class="bg-surface-card rounded-lg border border-gray-700/50">' +
    '<table class="w-full text-sm tabular-nums">' +
    '<thead><tr class="text-gray-500 text-xs uppercase tracking-wider border-b border-gray-700/50">' +
      '<th class="text-left font-medium px-3 py-2.5 w-36">When</th>' +
      '<th class="text-left font-medium px-3 py-2.5 w-24">Issue</th>' +
      '<th class="text-left font-medium px-3 py-2.5">Action</th>' +
      '<th class="text-left font-medium px-3 py-2.5 w-40">Status</th>' +
      '<th class="text-left font-medium px-3 py-2.5 w-24">PR</th>' +
      '<th class="text-right font-medium px-3 py-2.5 w-28">Duration</th>' +
      '<th class="text-right font-medium px-3 py-2.5 w-24">Cost</th>' +
      '<th class="px-3 py-2.5 w-24"></th>' +
    '</tr></thead><tbody>' +
    tasks.map(function(t) {
      var colors = runActionColors(t.action_color);
      var row = '<tr class="border-t border-gray-700/30 hover:bg-surface-hover cursor-pointer transition-colors"' +
          actionAttrs('navigate', [prefix + '/work/' + t.id]) + '>' +
        '<td class="' + td + ' text-gray-300">' + _whenCell(t) + '</td>' +
        '<td class="' + td + '">' + (t.issue_number ? issueLink(t.issue_number) : muted) + '</td>' +
        '<td class="' + td + '"><a href="' + escapeHtml(prefix + '/work/' + t.id) + '" data-action="stop" ' +
          'class="inline-flex px-2 py-0.5 rounded text-xs font-medium hover:underline ' + colors.bg + ' ' +
          colors.text + '">' + escapeHtml(t.action_label || t.role || '') + '</a></td>' +
        '<td class="' + td + '">' + _statusCell(t, showError) + '</td>' +
        '<td class="' + td + '">' + (t.pr_number ? prLink(t.pr_number, { bare: true }) : muted) + '</td>' +
        '<td class="' + td + ' text-right text-gray-400">' + escapeHtml(t.duration_formatted || '--') + '</td>' +
        '<td class="' + td + ' text-right text-gray-200">' + _formatRunCost(t.total_cost_usd) + '</td>' +
        '<td class="px-3 py-1.5 text-right">' + _rerunButton(t) + '</td>' +
      '</tr>';
      if (showError && t.error_message) {
        row += '<tr><td></td><td colspan="7" class="px-3 pb-2 text-xs text-accent-red [overflow-wrap:anywhere]">' +
          escapeHtml(_clip(t.error_message, 300)) + '</td></tr>';
      }
      return row;
    }).join('') +
    '</tbody></table></div>';
}

export function init() {
  registerActions({
    'switch-tab': function(el, e, tab) { switchTab(tab); },
    'history-range': function(el, e, range) { _setRange(range); },
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
