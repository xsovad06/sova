import { actionAttrs, registerActions } from './actions.js';
import { getActiveAgents } from './agent-cards.js';

// The stream, its polling fallback and the row renderer are sovaOutputStream() and sovaOutputRenderer() in app.js,
// shared with the run page; this module owns only the viewer chrome (open, close, tabs, button labels).
// Timers: the one self-rescheduling setTimeout chain is poll() inside sovaOutputStream(). The only raw timers left
// on this page are one-shot debounces (live-status.js, agent-cards.js) and a fetch abort (pr-tracker.js);
// tests/test_dashboard_agents_modules.py pins that allowlist so a new poll loop cannot slip in.

var activeLogRunId = null;

var _logRenderer = sovaOutputRenderer();
var _logStream = sovaOutputStream(_logRenderer);

// The run whose logs are open, or null. The agent cards read it to label their button "Viewing Logs".
export function getActiveLogRunId() { return activeLogRunId; }

// Log Viewer

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
    // No tab to show, but a log opened from the board (often a finished run's) stays open until it is closed.
    tabs.innerHTML = '';
    return;
  }

  // Tell the stream the open run's current status, so it stops polling once the run finishes (a run that has left
  // the list is covered by the stream itself: it ends on an idle read of an agent that is no longer in memory).
  agents.forEach(function(a) {
    if (a.run_id === activeLogRunId) _logStream.ensure(a.run_id, a.status);
  });

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

// Opens a run's log (leaving it open when it already shows that run) and brings the viewer into view: the
// Work board's View log buttons sit far above it.
export function openLog(runId, runStatus) {
  if (activeLogRunId !== runId) viewLogs(runId, runStatus);
  var viewer = document.getElementById('log-viewer');
  if (viewer) viewer.scrollIntoView({ block: 'start' });
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
  activeLogRunId = runId;
  _logRenderer.setTarget(document.getElementById('log-content'));
  viewer.classList.remove('hidden');
  _setLogButtonLabel(runId, true);

  // A running run's stream does not replay output, so start() backfills it the way a tab return does. It goes
  // before updateLogTabs(), whose ensure() would otherwise open the stream first and start() tear it down again.
  _logStream.start(runId, runStatus);
  updateLogTabs(getActiveAgents());
}

function closeLogViewer() {
  var prevRunId = activeLogRunId;
  activeLogRunId = null;
  _logStream.stop();
  _logRenderer.clear();
  _logRenderer.setTarget(null);
  var viewer = document.getElementById('log-viewer');
  if (viewer) viewer.classList.add('hidden');
  if (prevRunId !== null) _setLogButtonLabel(prevRunId, false);
  updateLogTabs(getActiveAgents());
}

export function init() {
  registerActions({
    'view-logs': function(el, e, runId, runStatus) { viewLogs(runId, runStatus); },
    'close-log-viewer': function() { closeLogViewer(); },
  });
}
