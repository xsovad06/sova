import { actionAttrs, registerActions } from './actions.js';
import { loadAgents } from './agent-cards.js';
import { refreshHistoryAfterRun } from './history.js';

// Agent status for the parts of the Work page outside the board: the log viewer's tabs and the planner
// results (loadAgents), terminal-state browser notifications, the interrupted-runs, memory-pressure and
// API-health banners, and the run history. WebSocket with a polling fallback, via the shared
// SovaStatusChannel helper (app.js). The board subscribes to the same socket itself (work/board.js).
var _statusChannel = null;
var _wsLastRunIds = null;
var _agentsRefreshTimer = null;

// Matches the pollIntervalMs passed to createSovaStatusChannel below, and
// doubles as the repeating fallback cadence once a sustained outage is
// detected (_pollDuringStatusOutage).
var _WS_FALLBACK_POLL_MS = 2000;

// Consecutive status_unavailable messages: the socket itself can stay open
// and "healthy" (createSovaStatusChannel resets its reconnect backoff on any
// parsed message, this one included) while the server-side status fetch
// keeps failing, so a sustained outage must be surfaced and must fall back
// to REST polling rather than leaving the log tabs frozen with no refresher
// at all.
var _wsUnavailableStreak = 0;
var _WS_UNAVAILABLE_STREAK_THRESHOLD = 3;
var _wsUnavailableIndicatorShown = false;
var _wsOutageFallbackActive = false;

// A parallel batch finishing fires one agent_finished per agent; without this debounce each exit costs its
// own unthrottled GET /api/agents/active.
function _scheduleAgentsRefresh() {
  if (_agentsRefreshTimer) clearTimeout(_agentsRefreshTimer);
  _agentsRefreshTimer = setTimeout(function() {
    _agentsRefreshTimer = null;
    loadAgents();
  }, 300);
}

function _handleAgentStatusMessage(msg) {
  if (msg.type === 'status_unavailable') {
    // A single miss is "no update this tick", not "no active runs". A sustained run of misses means
    // status_update stops carrying data while the socket stays open, so after a few, say so and poll.
    _wsUnavailableStreak++;
    if (_wsUnavailableStreak >= _WS_UNAVAILABLE_STREAK_THRESHOLD) {
      if (!_wsUnavailableIndicatorShown) {
        _wsUnavailableIndicatorShown = true;
        showToast('Real-time agent status unavailable: falling back to periodic refresh', 'warning');
      }
      _wsOutageFallbackActive = true;
    }
    return;
  }
  if (msg.type === 'status_update') {
    // Only a status_update (the producer recovered) ends the outage.
    _wsUnavailableStreak = 0;
    _wsUnavailableIndicatorShown = false;
    _wsOutageFallbackActive = false;
  }
  if (msg.type === 'agent_finished') {
    // The only place a terminal-state browser notification can fire from: status_update only ever carries
    // non-terminal runs, so a finished run's status never appears there.
    if (msg.status && _STATUS_TERMINAL[msg.status] && msg.status !== 'paused') {
      var ref = msg.issue ? '#' + msg.issue : (msg.pr_number ? 'PR #' + msg.pr_number : 'Run ' + msg.run_id);
      var _notifTitles = { done: 'Agent completed', failed: 'Agent failed', interrupted: 'Agent interrupted', rejected: 'Agent rejected', stopped: 'Agent stopped' };
      var title = _notifTitles[msg.status] || 'Agent ' + msg.status;
      sendBrowserNotification('SOVA: ' + title, ref + ' (' + (msg.role || 'agent') + ')');
      // A run the liveness sweep marked interrupted shows in the banner without waiting for a reload.
      if (msg.status === 'interrupted') checkInterrupted();
    }
    _scheduleAgentsRefresh();
    // The finished run joins the history: page one reloads, a later page offers a "new runs" button.
    refreshHistoryAfterRun();
    return;
  }
  if (msg.type === 'status_update' && Array.isArray(msg.runs)) {
    // A run starting, ending or changing status changes the log viewer's tabs.
    var snapshot = msg.runs.map(function(r) { return r.run_id + ':' + r.status; }).sort().join(',');
    if (_wsLastRunIds !== null && snapshot !== _wsLastRunIds) _scheduleAgentsRefresh();
    _wsLastRunIds = snapshot;
  }
}

function _startAgentStatusChannel() {
  if (_statusChannel) { _statusChannel.stop(); _statusChannel = null; }
  _statusChannel = createSovaStatusChannel({
    onMessage: _handleAgentStatusMessage,
    // A (re)connect can follow a gap long enough for a run to have been
    // interrupted and dropped from the banner's stale local state.
    onOpen: checkInterrupted,
    onFallbackPoll: loadAgents,
    pollIntervalMs: _WS_FALLBACK_POLL_MS,
  });
}
// Interrupted Runs Banner

var _interruptedRunIds = [];

export async function checkInterrupted() {
  try {
    var data = await fetchAPI(apiUrl('/agents/interrupted'));
    var runs = data.interrupted || [];
    _interruptedRunIds = runs.map(function(run) { return run.id; });
    var banner = document.getElementById('interrupted-banner');
    if (runs.length === 0) {
      banner.classList.add('hidden');
      banner.innerHTML = '';
      return true;
    }

    var rowsHtml = runs.map(function(run) {
      var started = run.started_at ? new Date(run.started_at).toLocaleString() : '';
      var roleLabel = run.role && run.role !== 'auto' ? ' (' + escapeHtml(run.role) + ')' : '';
      var rerunHtml = run.issue_number
        ? '<button type="button" class="rerun-btn text-xs px-3 py-1 rounded bg-accent/20 text-accent hover:bg-accent/30 transition-colors" data-action="rerun" ' +
          'aria-label="Restart interrupted run" data-issue="' + escapeHtml(String(run.issue_number)) + '" ' +
          'data-role="' + escapeHtml(run.role || '') + '" data-pr="' + escapeHtml(String(run.pr_number || '')) + '" ' +
          'data-run-id="' + escapeHtml(String(run.id)) + '">Restart</button>'
        : '';
      return '<div class="flex items-center justify-between bg-surface rounded px-3 py-2 border border-gray-700/30">' +
        '<span class="text-sm text-gray-300">' + issueOrPrLink(run.issue_number, run.pr_number) + roleLabel +
          ' <span class="text-xs text-gray-500">' + escapeHtml(started) + '</span></span>' +
        rerunHtml +
      '</div>';
    }).join('');

    var totalHint = data.total && data.total > runs.length
      ? ' <span class="text-xs text-gray-600">(showing ' + runs.length + ' of ' + data.total + ')</span>'
      : '';

    banner.innerHTML = '<div class="bg-accent-yellow/10 border border-accent-yellow/30 rounded-lg p-4">' +
      '<div class="flex items-center justify-between mb-2">' +
        '<h3 class="text-sm font-medium text-accent-yellow">Interrupted Runs' + totalHint + '</h3>' +
        '<button type="button"' + actionAttrs('dismiss-interrupted') + ' class="text-xs text-gray-500 hover:text-gray-300 transition-colors">Dismiss</button>' +
      '</div>' +
      '<p class="text-xs text-gray-500 mb-3">These agent runs were interrupted when the dashboard was restarted.</p>' +
      '<div class="space-y-2">' + rowsHtml + '</div>' +
    '</div>';
    banner.classList.remove('hidden');
    return true;
  } catch (e) {
    return false; /* best-effort: next poll resolves it */
  }
}

async function dismissInterrupted() {
  var clicked = _interruptedRunIds.slice();
  if (clicked.length === 0) return;
  var requestFailed = false;
  try {
    var resp = await fetch(apiUrl('/agents/interrupted/dismiss'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ run_ids: clicked })
    });
    if (!resp.ok) requestFailed = true;
  } catch (e) {
    requestFailed = true;
  }
  // Re-fetch the real state rather than trusting the response's "dismissed"
  // count: the server only dismisses runs still interrupted at update time,
  // so a lower count than clicked.length can mean another tab (or the
  // recovery sweep) already cleared one, which is a success, not a failure.
  // Report an error only when a clicked id is still stuck afterward. If the
  // refresh itself fails, that's unverifiable, not a failure: stay silent
  // and let the next 30s poll resolve it rather than asserting failure for
  // an operation that may have fully succeeded.
  var refreshed = await checkInterrupted();
  if (requestFailed) {
    showToast('Failed to dismiss interrupted runs', 'error');
  } else if (refreshed && clicked.some(function(id) { return _interruptedRunIds.indexOf(id) !== -1; })) {
    showToast('Some runs could not be dismissed', 'error');
  }
}

// Memory Pressure Banner

var _memPressureSeeded = false;
// The last /resources/system/metrics response and when it arrived. Module-private: only the banner reads it.
var _systemMetrics = null;
async function _seedMemoryPressure() {
  if (_memPressureSeeded) return;
  _memPressureSeeded = true;
  try {
    var data = await fetchAPI(apiUrl('/resources/system/metrics'));
    _systemMetrics = { data: data, ts: Date.now() };
    _applyMemoryPressure(data);
  } catch (e) { /* best-effort */ }
}

function checkMemoryPressure() {
  var cached = _systemMetrics;
  if (!cached || !cached.data || (Date.now() - cached.ts) > 10000) {
    _seedMemoryPressure();
    return;
  }
  _applyMemoryPressure(cached.data);
}

function _applyMemoryPressure(data) {
  var banner = document.getElementById('memory-pressure-banner');
  try {
    if (!data.available || !data.system) { banner.classList.add('hidden'); return; }
    var pressure = data.system.memory_pressure;
    var availGb = data.system.memory_available_gb;
    if (!pressure || pressure === 'ok') {
      banner.classList.add('hidden');
      sessionStorage.removeItem('sova-memory-pressure-dismissed');
      return;
    }

    var isCritical = pressure === 'critical';

    // Only allow dismissing warnings, never critical banners
    if (!isCritical && sessionStorage.getItem('sova-memory-pressure-dismissed')) {
      banner.classList.add('hidden');
      return;
    }

    var colorClass = isCritical ? 'accent-red' : 'accent-yellow';
    var label = isCritical ? 'Critical: agent spawning blocked' : 'Warning: memory is low';
    var detail = availGb != null ? availGb.toFixed(1) + ' GB available' : '';

    var dismissBtn = isCritical ? '' :
      '<button' + actionAttrs('dismiss-memory-pressure') + ' class="px-2 py-1 rounded text-xs text-gray-500 hover:text-gray-300 hover:bg-surface-hover transition-colors">Dismiss</button>';

    banner.classList.remove('hidden');
    banner.innerHTML = '<div class="bg-' + colorClass + '/10 border border-' + colorClass + '/30 rounded-lg p-4">' +
      '<div class="flex items-center justify-between">' +
        '<div class="flex items-center gap-2">' +
          '<span class="text-sm font-medium text-' + colorClass + '">' + escapeHtml(label) + '</span>' +
          '<span class="text-xs text-gray-500">' + escapeHtml(detail) + '</span>' +
        '</div>' +
        dismissBtn +
      '</div>' +
    '</div>';
  } catch (e) { /* best-effort */ }
}

function dismissMemoryPressure() {
  sessionStorage.setItem('sova-memory-pressure-dismissed', '1');
  document.getElementById('memory-pressure-banner').classList.add('hidden');
}

// API Health Banner (rate limit)

export function _updateApiHealthBanner(health) {
  var banner = document.getElementById('api-health-banner');
  if (!banner) return;
  if (!health || health.status === 'ok') {
    banner.classList.add('hidden');
    return;
  }
  if (health.status === 'unreachable') {
    banner.classList.remove('hidden');
    banner.innerHTML = '<div class="bg-accent-red/10 border border-accent-red/30 rounded-lg p-4">' +
      '<div class="flex items-center gap-2">' +
        '<span class="text-sm font-medium text-accent-red">No internet connection</span>' +
        '<span class="text-xs text-gray-500">' +
          escapeHtml(health.detail || '') +
          ' Interrupted runs resume automatically once the connection returns.' +
        '</span>' +
      '</div>' +
    '</div>';
    return;
  }

  var cooldown = health.cooldown_seconds || 0;
  var minutes = Math.floor(cooldown / 60);
  var seconds = cooldown % 60;
  var timeStr = minutes > 0 ? minutes + 'm ' + seconds + 's' : seconds + 's';

  banner.classList.remove('hidden');
  banner.innerHTML = '<div class="bg-accent-yellow/10 border border-accent-yellow/30 rounded-lg p-4">' +
    '<div class="flex items-center justify-between">' +
      '<div class="flex items-center gap-2">' +
        '<span class="text-sm font-medium text-accent-yellow">GitHub API rate limit exceeded</span>' +
        '<span class="text-xs text-gray-500">Data may be stale. Cooldown: ' + escapeHtml(timeStr) + '</span>' +
      '</div>' +
    '</div>' +
  '</div>';
}

// Repeating REST refresh for as long as the status channel reports a sustained outage
function _pollDuringStatusOutage() {
  if (!_wsOutageFallbackActive) return;
  loadAgents();
}

export function init() {
  registerActions({
    'dismiss-interrupted': function() { dismissInterrupted(); },
    'dismiss-memory-pressure': function() { dismissMemoryPressure(); },
  });

  visibilityAwarePoll(_pollDuringStatusOutage, _WS_FALLBACK_POLL_MS);
  visibilityAwarePoll(checkMemoryPressure, 5000);
  // Fires at once on page load and on every tab return, not only from the status channel's onOpen: when the
  // socket never opens (a proxy that strips Upgrade, a server-side WS failure) the channel runs only its REST
  // fallback (loadAgents), and the banner would otherwise wait out the first 30s interval. When the socket does
  // open, onOpen adds one cheap duplicate GET.
  visibilityAwarePoll(checkInterrupted, 30000);

  _startAgentStatusChannel();

  // Clean up on page navigation
  window.addEventListener('beforeunload', function() {
    if (_statusChannel) { _statusChannel.stop(); _statusChannel = null; }
  });

  // The pollers above pause themselves while the tab is hidden; the status channel
  // is closed with it and reopened, with a full refresh, when the tab returns.
  document.addEventListener('visibilitychange', function() {
    if (document.hidden) {
      if (_statusChannel) { _statusChannel.stop(); _statusChannel = null; }
      _wsOutageFallbackActive = false;
      _wsUnavailableStreak = 0;
      _wsUnavailableIndicatorShown = false;
    } else {
      _wsLastRunIds = null;
      _startAgentStatusChannel();
      loadAgents();
    }
  });
}
