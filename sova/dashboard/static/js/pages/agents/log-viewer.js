import { actionAttrs, registerActions } from './actions.js';
import { getActiveAgents } from './agent-cards.js';

// Timers: the one self-rescheduling setTimeout chain on this page is pollLogs() below. It is outside
// visibilityAwarePoll() on purpose, because that helper only fires at a fixed interval and this chain backs
// off (1s up to 5s) while a run is quiet; it checks document.hidden itself, and _cancelLogPolling() stops it.
// The only other raw timers are one-shot debounces (live-status.js, agent-cards.js) and a fetch abort
// (pr-tracker.js); tests/test_dashboard_agents_modules.py pins that allowlist so a new poll loop cannot slip in.

var activeLogRunId = null;

var logCursor = 0;
var logPollInterval = null;
var _logPollGen = 0;
var _logEventSource = null;
// 'sse' or 'poll' while the viewer streams a run, null otherwise; lets the tab
// resume with the transport it had when it was hidden.
var _logTransport = null;
var _logBackoffMs = 1000;
var _LOG_BACKOFF_BASE = 1000;
var _LOG_BACKOFF_MAX = 5000;
// How long a backfilled stream waits for its "connected" event before giving up on SSE (see _resumeLogStream).
var _LOG_CONNECT_WAIT_MS = 3000;

// A run whose output is complete, so polling it from line 0 shows everything. Derived from app.js's shared
// terminal set, minus the two states a run resumes from: those can still write, so they keep streaming.
function _isFinishedRun(status) {
  return !!_STATUS_TERMINAL[status] && status !== 'paused' && status !== 'awaiting_approval';
}

// The run whose logs are open, or null. The agent cards read it to label their button "Viewing Logs".
export function getActiveLogRunId() { return activeLogRunId; }

// Log Viewer

var _LOG_PREFIX_RE = /^\[(stderr|stdout)\]\s?(.*)$/;
var _LOG_STRUCT_RE = /^(\d{4}-\d{2}-\d{2}T[\d:.]+Z?)\s+\[\s*(\w+)\s*\]\s*(.*)$/;
var _LOG_CTX_RE = /\s([A-Za-z_][\w.]*)=/;

// Formats one raw agent output line for display. Lines follow structlog's
// ConsoleRenderer layout (timestamp, level, event, then key=value context),
// prefixed with [stderr]/[stdout] by agent_output.py. Lines that don't match
// (plain prints, tracebacks) fall back to escaped plain text so nothing breaks.
function formatLogLine(raw) {
  var stream = '';
  var rest = raw;
  var pm = _LOG_PREFIX_RE.exec(raw);
  if (pm) { stream = pm[1]; rest = pm[2]; }
  var streamTag = stream ? '<span class="text-gray-600">' + stream + '</span> ' : '';

  var sm = _LOG_STRUCT_RE.exec(rest);
  if (!sm) {
    return '<div class="py-0.5">' + streamTag + '<span class="text-gray-300">' + escapeHtml(rest) + '</span></div>';
  }

  var ts = sm[1], level = sm[2].toUpperCase(), body = sm[3];
  var color = LOG_LEVEL_COLORS[level] || 'text-gray-400';
  var tsShort = ts.length >= 19 ? ts.substring(11, 19) : ts;

  var ctxMatch = _LOG_CTX_RE.exec(body);
  var event = ctxMatch ? body.slice(0, ctxMatch.index).trim() : body.trim();
  var context = ctxMatch ? body.slice(ctxMatch.index).trim() : '';

  return '<div class="py-0.5">' +
    streamTag +
    '<span class="text-gray-600">' + escapeHtml(tsShort) + '</span> ' +
    '<span class="' + color + '">' + escapeHtml(level.padEnd(8)) + '</span> ' +
    '<span class="text-gray-200">' + escapeHtml(event) + '</span>' +
    (context ? '  <span class="text-gray-500">' + escapeHtml(context) + '</span>' : '') +
  '</div>';
}

function _setLogButtonLabel(runId, isActive) {
  var btn = document.getElementById('log-btn-' + runId);
  if (!btn) return;
  btn.textContent = isActive ? 'Viewing Logs' : 'View Logs';
  var hint = isActive ? 'Hide agent output' : 'View agent output';
  btn.setAttribute('aria-label', hint);
  btn.setAttribute('data-tooltip', hint);
}

export function updateLogTabs(agents) {
  var tabs = document.getElementById('log-tabs');
  if (!tabs) return;

  if (agents.length === 0) {
    if (activeLogRunId !== null) closeLogViewer();
    return;
  }

  tabs.innerHTML = agents.map(function(a) {
    var colors = roleColor(a.role);
    var isActive = activeLogRunId === a.run_id;
    var activeClass = isActive ? 'bg-surface text-white border-b-2 border-accent' : 'text-gray-500 hover:text-gray-300';
    var tabLabel = a.issue ? '#' + a.issue : (a.pr_number ? 'PR #' + a.pr_number : formatRole(a.role, a.pipeline_variant));
    return '<button' + actionAttrs('view-logs', [a.run_id, a.status]) + ' class="px-4 py-2 text-xs transition-colors whitespace-nowrap ' + activeClass + '">' +
      escapeHtml(tabLabel) + ' <span class="' + colors.text + '">' + escapeHtml(formatRole(a.role, a.pipeline_variant)) + '</span>' +
    '</button>';
  }).join('');
}

// Auto-opens a freshly started run's logs only when no viewer is already
// open, so this never steals focus from a log panel the user is watching.
export function _autoOpenLogsIfIdle(runId) {
  if (runId && activeLogRunId === null) viewLogs(runId);
}

function viewLogs(runId, runStatus) {
  if (activeLogRunId === runId) {
    closeLogViewer();
    return;
  }

  // Closing here (rather than leaving the previous stream connected) means
  // switching tabs never shows a stale mix of two runs' output.
  closeLogViewer();

  var viewer = document.getElementById('log-viewer');
  var content = document.getElementById('log-content');
  activeLogRunId = runId;
  logCursor = 0;
  _logBackoffMs = _LOG_BACKOFF_BASE;
  content.textContent = '';
  viewer.classList.remove('hidden');
  _setLogButtonLabel(runId, true);
  updateLogTabs(getActiveAgents());

  if (runStatus && _isFinishedRun(runStatus)) {
    _startLogPolling();
  } else {
    // The stream does not replay output, so a bare connect would show only what the agent writes after the
    // click. Backfilling the same way a tab return does shows the run's earlier lines and drops the overlap exactly.
    _resumeLogStream(runId);
  }
}

function _appendLogLine(text) {
  var content = document.getElementById('log-content');
  if (!content) return;
  content.insertAdjacentHTML('beforeend', formatLogLine(text));
  content.scrollTop = content.scrollHeight;
}

// resume (optional) is the state of a stream that is backfilled (a first open or a reconnect after the tab
// was hidden): lines pushed while the backfill is still in flight are held in resume.held instead of rendered (see _reconcileLogStream).
function _connectLogSSE(runId, resume) {
  if (_logEventSource) { _logEventSource.close(); _logEventSource = null; }

  var url = apiUrl('/agents/' + runId + '/output/stream');
  var es = new EventSource(url);
  _logEventSource = es;
  _logTransport = 'sse';

  es.addEventListener('output', function(e) {
    if (activeLogRunId !== runId) return;
    var text = e.data.replace(/\\([nr\\])/g, function(_, c) { return c === 'n' ? '\n' : c === 'r' ? '\r' : '\\'; });
    if (resume && resume.holding) {
      resume.held.push(text);
      return;
    }
    _appendLogLine(text);
  });

  // The server subscribes before it sends "connected" and reports in it how many lines the run had
  // buffered by then (seq): everything written from here on is pushed to this stream, everything
  // before it is in the backfill.
  if (resume) {
    es.addEventListener('connected', function(e) {
      // A late "connected" after the wait timer already rendered the backfill has nothing left to reconcile.
      if (!resume.holding) return;
      clearTimeout(resume.connectTimer);
      resume.connectedSeq = _parseLogSeq(e.data);
      _reconcileLogStream(runId, es, resume);
    });
  }

  // The server dropped a line for this subscriber (its queue was full), so the stream has a hole the viewer
  // cannot locate: the sequence arithmetic in _logOverlap would be off by the missing lines, and the log would
  // show a gap with no sign of it. Re-read the whole run instead.
  es.addEventListener('gap', function() {
    if (activeLogRunId !== runId || es !== _logEventSource) return;
    _fallBackToLogPolling(es, runId);
  });

  es.addEventListener('done', function() {
    if (resume && resume.holding) {
      // The run ended while the backfill is in flight. This stays the live stream until
      // _reconcileLogStream has rendered the backfill and the held lines, and retires it then.
      resume.done = true;
      es.close();
      return;
    }
    if (es === _logEventSource) { es.close(); _logEventSource = null; _logTransport = null; }
  });

  es.onerror = function() {
    // SSE failed: fall back to polling with fresh cursor
    _fallBackToLogPolling(es, runId);
  };
}

// Drops the stream and re-reads the run from line 0 by polling. Used when the stream fails or a
// reconnect cannot be reconciled, so the viewer never keeps a log with a silent gap in it.
function _fallBackToLogPolling(es, runId) {
  if (es === _logEventSource) { es.close(); _logEventSource = null; }
  if (activeLogRunId === runId) {
    logCursor = 0;
    var el = document.getElementById('log-content');
    if (el) el.textContent = '';
    _startLogPolling();
  }
}

// The line count a "connected" event or an output response carries, or null when it carries none
// (a server that predates it, or an agent that is no longer in memory).
function _parseLogSeq(raw) {
  try {
    var seq = JSON.parse(raw).seq;
    return Number.isInteger(seq) ? seq : null;
  } catch (e) {
    return null;
  }
}

// Stops the poll chain. A fetch already in flight cannot be cancelled, so it also bumps the generation
// that pollLogs() captured at entry: the stale response then neither appends nor reschedules, and a chain
// restarted by _startLogPolling() is never doubled by an old one.
function _cancelLogPolling() {
  _logPollGen++;
  if (logPollInterval) { clearTimeout(logPollInterval); logPollInterval = null; }
}

function _startLogPolling() {
  _cancelLogPolling();
  _logTransport = 'poll';
  _logBackoffMs = _LOG_BACKOFF_BASE;
  pollLogs();
}

function closeLogViewer() {
  var prevRunId = activeLogRunId;
  activeLogRunId = null;
  logCursor = 0;
  _logTransport = null;
  _cancelLogPolling();
  if (_logEventSource) { _logEventSource.close(); _logEventSource = null; }
  var viewer = document.getElementById('log-viewer');
  if (viewer) viewer.classList.add('hidden');
  if (prevRunId !== null) _setLogButtonLabel(prevRunId, false);
  updateLogTabs(getActiveAgents());
}

// The SSE stream does not replay output, so the lines produced before it connected (while the tab was
// hidden, or before the viewer was opened) come from a full fetch. The stream is opened first and holds its lines back until that fetch lands: connecting only
// afterwards would lose whatever the agent wrote in between.
function _resumeLogStream(runId) {
  _logBackoffMs = _LOG_BACKOFF_BASE;
  var resume = { holding: true, held: [], connectedSeq: null, done: false, connectTimer: null };
  _connectLogSSE(runId, resume);
  // Every line is held until "connected" arrives, and onerror does not fire while a socket stays open, so a
  // proxy that buffers the first event would leave the panel empty for good. Past the wait the stream is
  // given up for polling, which reads the run from line 0 and needs no subscription point to be exact.
  var es = _logEventSource;
  resume.connectTimer = setTimeout(function() {
    if (resume.holding && resume.connectedSeq === null && es === _logEventSource && runId === activeLogRunId) {
      _fallBackToLogPolling(es, runId);
    }
  }, _LOG_CONNECT_WAIT_MS);
}

// How many leading held lines the backfill already contains: the lines written after the subscription
// but before the snapshot was read. The stream sent every line from connectedSeq on and the snapshot
// holds every line before snapshotSeq, so when both are known the answer is exact, repeated lines
// included, as long as the stream dropped nothing: the server reports a dropped line as a "gap" event, which
// abandons the stream for a full re-read before this arithmetic is trusted. Without them (an agent no longer in memory, or a server that sends no seq) it falls back to
// matching text, which cannot tell an identical line the agent wrote again from one it has already
// seen, so it may drop genuinely new repeated lines.
function _logOverlap(snapshot, held, connectedSeq, snapshotSeq) {
  if (connectedSeq !== null && snapshotSeq !== null) {
    return Math.max(0, Math.min(snapshotSeq - connectedSeq, held.length));
  }
  for (var m = Math.min(snapshot.length, held.length); m > 0; m--) {
    var tail = snapshot.length - m;
    var same = true;
    for (var i = 0; i < m && same; i++) same = held[i] === snapshot[tail + i];
    if (same) return m;
  }
  return 0;
}

async function _reconcileLogStream(runId, es, resume) {
  var snapshot = null;
  var snapshotSeq = null;
  try {
    var data = await fetchAPI(apiUrl('/agents/' + runId + '/output?since=0'));
    snapshot = data.lines || [];
    snapshotSeq = Number.isInteger(data.seq) ? data.seq : null;
  } catch (e) {
    console.warn('Log backfill failed', e);
  }
  // Switched runs, closed the viewer, or hid the tab again: this stream is no longer the live one.
  if (runId !== activeLogRunId || es !== _logEventSource) return;
  if (snapshot === null) {
    // The lines written while the tab was hidden are unknown, and appending the held ones to the old
    // content would hide the gap. Re-read the whole run instead.
    _fallBackToLogPolling(es, runId);
    return;
  }
  var content = document.getElementById('log-content');
  if (content) content.innerHTML = snapshot.map(formatLogLine).join('');
  var held = resume.held.slice(_logOverlap(snapshot, resume.held, resume.connectedSeq, snapshotSeq));
  resume.holding = false;
  resume.held = [];
  held.forEach(_appendLogLine);
  if (content) content.scrollTop = content.scrollHeight;
  if (resume.done) {
    _logEventSource = null;
    _logTransport = null;
  }
}

async function pollLogs() {
  if (activeLogRunId === null) return;
  // The viewer may switch runs, or the chain may be cancelled or restarted, while this fetch is in
  // flight; whichever poll owns the viewer now started its own chain, so a stale response must neither
  // append nor reschedule.
  var runId = activeLogRunId;
  var gen = _logPollGen;
  var isCurrent = function() { return runId === activeLogRunId && gen === _logPollGen && !document.hidden; };
  try {
    var data = await fetchAPI(apiUrl('/agents/' + runId + '/output?since=' + logCursor));
    if (!isCurrent()) return;
    if (data.lines && data.lines.length > 0) {
      var content = document.getElementById('log-content');
      content.insertAdjacentHTML('beforeend', data.lines.map(formatLogLine).join(''));
      logCursor = data.total;
      content.scrollTop = content.scrollHeight;
      _logBackoffMs = _LOG_BACKOFF_BASE;
    } else {
      _logBackoffMs = Math.min(_logBackoffMs * 1.5, _LOG_BACKOFF_MAX);
    }
  } catch (e) {
    if (!isCurrent()) return;
    _logBackoffMs = Math.min(_logBackoffMs * 1.5, _LOG_BACKOFF_MAX);
  }
  if (isCurrent()) {
    logPollInterval = setTimeout(pollLogs, _logBackoffMs);
  }
}

export function init() {
  registerActions({
    'view-logs': function(el, e, runId, runStatus) { viewLogs(runId, runStatus); },
    'close-log-viewer': function() { closeLogViewer(); },
  });

  // Close the log stream while the tab is hidden and resume it with the same
  // transport when the tab returns (polling from its cursor, SSE after a backfill).
  document.addEventListener('visibilitychange', function() {
    if (document.hidden) {
      if (_logEventSource) { _logEventSource.close(); _logEventSource = null; }
      _cancelLogPolling();
    } else {
      if (activeLogRunId !== null) {
        if (_logTransport === 'poll') {
          _startLogPolling();
        } else if (_logTransport === 'sse') {
          _resumeLogStream(activeLogRunId);
        }
      }
    }
  });
}
