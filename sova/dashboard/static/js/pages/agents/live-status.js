import { actionAttrs, registerActions } from './actions.js';
import { getLastLoadedRunIds, loadWorkItems } from './work-items.js';
import { loadAgents } from './agent-cards.js';

// Real-time agent status: WebSocket with polling fallback, via the shared
// SovaStatusChannel helper (app.js).
var _statusChannel = null;
var _wsLastRunIds = null;
var _wsLastRunStatuses = null;
var _stepStartTimes = {};
var _workItemsRefreshTimer = null;
var _agentsRefreshTimer = null;

// Matches the pollIntervalMs passed to createSovaStatusChannel below, and
// doubles as the repeating fallback cadence once a sustained outage is
// detected (_pollDuringStatusOutage).
var _WS_FALLBACK_POLL_MS = 2000;

// Consecutive status_unavailable messages: the socket itself can stay open
// and "healthy" (createSovaStatusChannel resets its reconnect backoff on any
// parsed message, this one included) while the server-side status fetch
// keeps failing, so a sustained outage must be surfaced and must fall back
// to REST polling rather than leaving the grid frozen with no refresher at
// all.
var _wsUnavailableStreak = 0;
var _WS_UNAVAILABLE_STREAK_THRESHOLD = 3;
var _wsUnavailableIndicatorShown = false;
// A real repeating interval, not a one-shot debounced refresh: the socket's
// own reconnect backoff resets on every status_unavailable message (it
// parsed successfully) and stopFallback() runs on it too, so nothing else
// refreshes the grid for the rest of a sustained outage without this.
var _wsOutageFallbackActive = false;

// Client-side baseline for "time in current step": the WebSocket's
// status_update carries an authoritative time_in_step_ms (gated on
// time_in_step_known, see _handleAgentStatusMessage), but the REST
// /agents/active payload (get_unified_agents(), consumed by loadAgents() and
// the polling fallback) does not carry it at all. _stepStartTimes is only
// ever populated from that authoritative WS value, never fabricated here:
// a run this browser session has not yet received one for (e.g. right after
// a page load, before the first WebSocket message arrives) reports null
// rather than a fabricated "Step: 0s". The shared elapsed-timer interval
// (the `.sova-status-elapsed` ticker, `_tickElapsedTimers()` below) does the
// ticking once a baseline exists.
// Drop the baselines of runs a server payload no longer reports. The one place another module may
// change _stepStartTimes: it stays private here so no importer mutates it behind this module's back.
// keepRunIds is an object keyed by run id.
export function pruneStepStartTimes(keepRunIds) {
  Object.keys(_stepStartTimes).forEach(function(id) {
    if (!keepRunIds[id]) delete _stepStartTimes[id];
  });
}

export function _stepElapsedSeconds(runId, step) {
  var entry = _stepStartTimes[runId];
  if (!entry || entry.step !== step) return null;
  return Math.max(0, Math.round((Date.now() - entry.startedAtMs) / 1000));
}

// Refetch the Tasks list shortly after an agent changes state, instead of
// waiting for the 30 s poll. Debounced so a burst of WebSocket messages (or an
// agent_finished event arriving alongside a status change) costs one request.
function _scheduleWorkItemsRefresh() {
  if (_workItemsRefreshTimer) clearTimeout(_workItemsRefreshTimer);
  _workItemsRefreshTimer = setTimeout(function() {
    _workItemsRefreshTimer = null;
    loadWorkItems();
  }, 300);
}

// Same debounce for the Agents list: a parallel batch finishing fires one
// agent_finished per agent, and without this each exit costs its own
// unthrottled GET /api/agents/active.
function _scheduleAgentsRefresh() {
  if (_agentsRefreshTimer) clearTimeout(_agentsRefreshTimer);
  _agentsRefreshTimer = setTimeout(function() {
    _agentsRefreshTimer = null;
    loadAgents();
  }, 300);
}

function _handleAgentStatusMessage(msg) {
  if (msg.type === 'status_unavailable') {
    // The producer's status fetch failed server-side (e.g. a transient DB
    // hiccup); a single miss is "no update this tick", not "no active runs",
    // so none of the per-run tracking state below is pruned on it. But a
    // *sustained* run of misses means status_update stops carrying any data
    // while the socket itself stays open and looks healthy (its reconnect
    // backoff resets on any parsed message, this one included), so without
    // this counter the grid would freeze silently: no polling fallback, no
    // reconnect, nothing but the cosmetic cpu/mem/cost/elapsed tickers.
    // After a few consecutive misses, surface that and start a REAL repeating
    // fallback poll (not a one-shot debounced refresh: every later
    // status_unavailable past the threshold would otherwise be swallowed by
    // the indicator-shown guard) that keeps running for the rest of the
    // outage. The fallback still degrades current_step to "agent" per
    // _fetch_run_states's own DB-outage fallback, but a stale-looking card
    // beats a frozen one.
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
    // Only a status_update (the signal the producer actually recovered) ends
    // the outage: an agent_finished arriving mid-outage must not reset the
    // streak or it would postpone detection every time an agent happens to
    // exit during one.
    _wsUnavailableStreak = 0;
    _wsUnavailableIndicatorShown = false;
    _wsOutageFallbackActive = false;
  }
  if (msg.type === 'agent_finished') {
    // A dashboard-managed agent exited and the server dropped its cached
    // task, PR, and verdict state: refresh both views now. This covers
    // supervisor-spawned runs too, since those go through the same
    // dashboard agent pool (agent_lifecycle.start_agent()/start_command())
    // and so emit this event like any other. A genuine CLI `sova run`
    // started outside the dashboard does not: for that case the
    // status_update branch below is what eventually catches the change,
    // and only once the server's own queue/PR caches have expired on
    // their TTL, since nothing invalidates them for an external run.
    //
    // This is also the only place a terminal-state browser notification can
    // fire from: get_all_agent_statuses() (the status_update producer) only
    // ever selects non-terminal TaskRuns, so a finished run's terminal
    // status never appears in a status_update broadcast for the client to
    // observe a transition into.
    if (msg.status && _STATUS_TERMINAL[msg.status] && msg.status !== 'paused') {
      var ref = msg.issue ? '#' + msg.issue : (msg.pr_number ? 'PR #' + msg.pr_number : 'Run ' + msg.run_id);
      var _notifTitles = { done: 'Agent completed', failed: 'Agent failed', interrupted: 'Agent interrupted', rejected: 'Agent rejected', stopped: 'Agent stopped' };
      var title = _notifTitles[msg.status] || 'Agent ' + msg.status;
      sendBrowserNotification('SOVA: ' + title, ref + ' (' + (msg.role || 'agent') + ')');
      // A run transitioning into interrupted (e.g. the server's periodic
      // liveness sweep marking it so) must show up in the banner without
      // waiting for a page reload or the next poll tick.
      if (msg.status === 'interrupted') checkInterrupted();
    }
    _scheduleAgentsRefresh();
    _scheduleWorkItemsRefresh();
    return;
  }
  if (msg.type === 'status_update' && Array.isArray(msg.runs)) {
    // Any run changing status, starting, or leaving the active set can
    // change a task's state.
    var runStatuses = msg.runs.map(function(r) { return r.run_id + ':' + r.status; }).sort().join(',');
    if (_wsLastRunStatuses === null) {
      // First snapshot since connect: the Tasks list may have loaded
      // before this connection opened, so it can carry a run's changed
      // status the page hasn't seen yet. But the far more common case is
      // the normal page-load sequence, where loadWorkItems() already ran
      // moments ago and reports the exact same active runs: only
      // refresh here if the two actually disagree.
      var activeRunIds = msg.runs
        .filter(function(r) { return !_STATUS_TERMINAL[r.status]; })
        .map(function(r) { return r.run_id; })
        .sort()
        .join(',');
      if (activeRunIds !== getLastLoadedRunIds()) {
        _scheduleWorkItemsRefresh();
      }
    } else if (runStatuses !== _wsLastRunStatuses) {
      _scheduleWorkItemsRefresh();
    }
    _wsLastRunStatuses = runStatuses;
    // Terminal-state browser notifications fire from the agent_finished event
    // instead (above): get_all_agent_statuses() only ever selects non-terminal
    // TaskRuns, so msg.runs here never carries a terminal status to transition
    // into.
    var _activeRunIds = {};
    msg.runs.forEach(function(run) { _activeRunIds[run.run_id] = 1; });
    // Prune entries for runs no longer reported by the server
    pruneStepStartTimes(_activeRunIds);

    // Resync the client-side "time in current step" baseline
    // (_stepElapsedSeconds, used by renderAgentCard for the REST
    // initial/polling-fallback render) to the authoritative time_in_step_ms,
    // for every run, before either render path below reads it. Doing this up
    // here rather than inside the per-card patch loop means a step
    // transition that triggers the full loadAgents() rebuild just below
    // still lands on the correct baseline on the same tick, instead of a
    // freshly rendered card showing a blank "Step:" field until the next
    // broadcast up to 2s later.
    //
    // Stuck detection uses run.elapsed_seconds, consistently with the REST
    // path and with renderStatusBadge's own _STUCK_THRESHOLD_S semantics.
    // time_in_step_ms is a separate, distinctly-labeled "time in current
    // step" baseline and must never feed stuck detection. Gated on
    // time_in_step_known: a run with no in-progress StepExecution (e.g. a
    // command-role run, or the window before its first step row exists) has
    // time_in_step_ms fall back to run-elapsed, which would duplicate the
    // adjacent "elapsed" field under this separate label.
    msg.runs.forEach(function(run) {
      var stepSec = (run.time_in_step_known && run.time_in_step_ms != null) ? Math.floor(run.time_in_step_ms / 1000) : null;
      if (stepSec != null) {
        _stepStartTimes[run.run_id] = { step: run.current_step, startedAtMs: Date.now() - run.time_in_step_ms };
      } else {
        delete _stepStartTimes[run.run_id];
      }
    });

    // Build a snapshot of display-relevant fields to detect any change
    var snapshot = msg.runs.map(function(r) {
      return r.run_id + ':' + r.status + ':' + (r.current_step || '') + ':' + r.step_index + '/' + r.total_steps + ':' + (r.is_stuck ? '1' : '0');
    }).sort().join(',');
    if (_wsLastRunIds !== null && snapshot !== _wsLastRunIds) {
      _wsLastRunIds = snapshot;
      loadAgents();
      return;
    }
    _wsLastRunIds = snapshot;
    // Apply all display-relevant field deltas inline
    msg.runs.forEach(function(run) {
      var card = document.getElementById('agent-card-' + run.run_id);
      if (!card) return;

      // Re-render status badge with current values
      var badgeEl = card.querySelector('.sova-status-badge');
      if (badgeEl) {
        var newBadge = renderStatusBadge(run.status, run.current_step, run.step_index, run.total_steps, run.elapsed_seconds);
        badgeEl.outerHTML = newBadge;
      }

      // Update step pipeline
      var pipelineEl = card.querySelector('[data-pipeline-container]');
      if (pipelineEl) {
        pipelineEl.innerHTML = renderStepPipeline(run.current_step, run.role, false, run.pipeline_variant);
      }

      // Update the run-elapsed display with a live ticker
      var elapsedEl = card.querySelector('[data-field="elapsed"]');
      if (elapsedEl) {
        elapsedEl.innerHTML = run.elapsed_seconds != null ? liveElapsed(run.elapsed_seconds) : '';
      }

      // Update the separate "time in current step" display. Reads back
      // through _stepElapsedSeconds/_stepStartTimes (resynced above) rather
      // than a local variable, since that resync now runs in its own loop
      // ahead of the possible loadAgents() early return.
      var stepElapsedEl = card.querySelector('[data-field="step-elapsed"]');
      if (stepElapsedEl) {
        var liveStepSec = run.current_step ? _stepElapsedSeconds(run.run_id, run.current_step) : null;
        stepElapsedEl.innerHTML = liveStepSec != null ? ('Step: ' + liveElapsed(liveStepSec)) : 'Step: —';
      }
    });
  }
}

function _startAgentStatusChannel() {
  if (_statusChannel) { _statusChannel.stop(); _statusChannel = null; }
  _statusChannel = createSovaStatusChannel({
    wsPath: apiUrl('/ws/agents/status'),
    onMessage: _handleAgentStatusMessage,
    // A (re)connect can follow a gap long enough for a run to have been
    // interrupted and dropped from the banner's stale local state.
    onOpen: checkInterrupted,
    onFallbackPoll: loadAgents,
    pollIntervalMs: _WS_FALLBACK_POLL_MS,
    // The server's own push cadence is _STATUS_BROADCAST_INTERVAL (2s,
    // sova/dashboard/routers/agents.py), but each tick also runs
    // get_all_agent_statuses(), an unbounded per-run DB fetch. 20s is
    // generous enough to absorb that without misdiagnosing a slow-but-alive
    // server as a dead socket; see createSovaStatusChannel's staleAfterMs doc.
    staleAfterMs: 20000,
    auxiliaryPollFn: _refreshCardMetrics,
    auxiliaryPollIntervalMs: 5000
  });
}

function _refreshCardMetrics() {
  if (!document.querySelector('[id^="agent-card-"]')) return;
  // The shared activity poll in app.js (a classic script on every page) publishes this cache; it is not
  // owned by any page module, and it refreshes every 3s, so the reader keeps the 5s freshness guard below.
  var cached = window._sovaActiveAgents;
  if (!cached || !cached.data || (Date.now() - cached.ts) > 5000) return;
  var data = cached.data;
  (data.agents || []).forEach(function(agent) {
    var card = document.getElementById('agent-card-' + agent.run_id);
    if (!card) return;
    var cpuEl = card.querySelector('[data-field="cpu"]');
    if (cpuEl) {
      cpuEl.textContent = agent.cpu_percent != null ? 'CPU ' + agent.cpu_percent.toFixed(1) + '%' : 'CPU —';
    }
    var memEl = card.querySelector('[data-field="mem"]');
    if (memEl) {
      memEl.textContent = agent.memory_rss_bytes != null ? (agent.memory_rss_bytes / 1048576).toFixed(0) + ' MB' : '— MB';
    }
    var costEl = card.querySelector('[data-field="cost"]');
    if (costEl) {
      costEl.textContent = '$' + parseFloat(agent.cost_usd || 0).toFixed(4);
    }
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

// Shared 1s tick that keeps every .sova-status-elapsed timer on the page current
function _tickElapsedTimers() {
  var els = document.querySelectorAll('.sova-status-elapsed');
  if (els.length === 0) return;
  var now = Date.now();
  els.forEach(function(el) {
    var start = parseInt(el.getAttribute('data-start'), 10);
    if (!start) return;
    var secs = Math.max(0, Math.round((now - start) / 1000));
    el.textContent = formatElapsed(secs);
  });
}

// Repeating REST refresh for as long as the status channel reports a sustained outage
function _pollDuringStatusOutage() {
  if (!_wsOutageFallbackActive) return;
  loadAgents();
  loadWorkItems();
}

export function init() {
  registerActions({
    'dismiss-interrupted': function() { dismissInterrupted(); },
    'dismiss-memory-pressure': function() { dismissMemoryPressure(); },
  });

  visibilityAwarePoll(_tickElapsedTimers, 1000);
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
