import { actionAttrs, registerActions } from './actions.js';
import { _stepElapsedSeconds, pruneStepStartTimes } from './live-status.js';
import { getAgentView } from './kanban.js';
import { loadTaskCards, loadWorkItems } from './work-items.js';
import { _autoOpenLogsIfIdle, getActiveLogRunId, updateLogTabs } from './log-viewer.js';

var _completedCleanupTimer = null;
var lastAgentFingerprint = null;
// Separate from lastAgentFingerprint: Kanban polls consume that one without
// rendering the list grid, so switching back to List must still re-render.
var _lastGridFingerprint = null;
var _lastActiveAgents = [];

// The last poll's running agents, for the log viewer's tab strip: a click on "View Logs" has no fetch of
// its own, and the strip must repaint at once. A getter, never an exported `var`: an importer cannot assign to one.
export function getActiveAgents() { return _lastActiveAgents; }

function agentFingerprint(data) {
  // cost_usd is deliberately excluded: it changes on nearly every poll while
  // a run is active, which would force a full grid re-render every tick and
  // defeat the point of only re-rendering on an actual state change. The
  // cost display itself still refreshes via _refreshCardMetrics(). The stuck
  // bucket below must still change on its own: without it, a card with
  // otherwise-unchanged status/current_step/pr_number/source is never
  // re-rendered once it crosses _STUCK_THRESHOLD_S, so renderStatusBadge()
  // never runs again and the stuck class never appears while polling.
  // step_index/total_steps are included too, so this fingerprint reads the
  // same field set as the WebSocket path's own change-detection snapshot
  // (see the status_update handler below): a pipeline renumbering that
  // changes step_index/total_steps without changing current_step would
  // otherwise go undetected here while still triggering a re-render there.
  var running = data.agents.map(function(a) {
    var stuckBucket = (a.elapsed_seconds != null && a.elapsed_seconds > _STUCK_THRESHOLD_S) ? '1' : '0';
    return a.run_id + ':' + a.status + ':' + a.current_step + ':' + a.step_index + '/' + a.total_steps + ':' + (a.pr_number || '') + ':' + (a.source || '') + ':' + stuckBucket;
  });
  var completed = data.completed.map(function(a) {
    return a.run_id + ':' + a.status + ':' + (a.pr_number || '');
  });
  return data.slots_available + '|' + running.join(',') + '|' + completed.join(',');
}

export async function loadAgents() {
  try {
    var data = await fetchAPI(apiUrl('/agents/active'));
    _lastActiveAgents = data.agents;

    // Mirrors the WS path's pruning of _stepStartTimes (see
    // _handleAgentStatusMessage) so a polling-only session (no WS messages
    // at all) doesn't accumulate an entry per run for the life of the page.
    var _loadedRunIds = {};
    data.agents.forEach(function(a) { _loadedRunIds[a.run_id] = 1; });
    pruneStepStartTimes(_loadedRunIds);

    var fp = agentFingerprint(data);
    var changed = fp !== lastAgentFingerprint;
    if (changed) lastAgentFingerprint = fp;

    if (changed) {
      updateLogTabs(data.agents);
    }

    // Skip DOM-heavy list rendering when kanban view is active
    if (getAgentView() === 'kanban') return;
    if (fp === _lastGridFingerprint) return;
    _lastGridFingerprint = fp;

    var grid = document.getElementById('agents-grid');
    if (data.agents.length === 0 && data.completed.length === 0) {
      grid.innerHTML = '<div class="col-span-2 text-gray-500 text-sm py-8 text-center">No agents running</div>';
      return;
    }
    var cards = [];

    // Running agents
    data.agents.forEach(function(agent) {
      cards.push(renderAgentCard(agent));
    });

    // Recently completed
    data.completed.forEach(function(agent) {
      cards.push(renderCompletedCard(agent));
    });

    grid.innerHTML = cards.join('');
    enhancePlannerCards(data.completed);

    // Schedule a refresh after server-side TTL expires to clear completed cards
    if (data.completed.length > 0) {
      var maxAge = 0;
      data.completed.forEach(function(agent) {
        var remaining = 30 - (agent.completed_seconds_ago || 0);
        if (remaining > maxAge) maxAge = remaining;
      });
      var delay = maxAge > 0 ? (maxAge + 1) * 1000 : 2000;
      if (_completedCleanupTimer) clearTimeout(_completedCleanupTimer);
      _completedCleanupTimer = setTimeout(function() {
        _completedCleanupTimer = null;
        loadAgents();
      }, delay);
    }
  } catch (e) {
    console.error('Failed to load agents:', e);
  }
}

function renderAgentCard(agent) {
  var colors = roleColor(agent.role);
  var pipeline = renderStepPipeline(agent.current_step, agent.role, false, agent.pipeline_variant);
  var cost = agent.cost_usd ? '$' + parseFloat(agent.cost_usd).toFixed(4) : '$0.00';
  var prefix = window.SOVA_PROJECT_SLUG ? '/p/' + window.SOVA_PROJECT_SLUG : '';
  var detailUrl = prefix + '/work/' + agent.run_id;

  var hasRef = agent.issue || agent.pr_number;
  var issueLabel = hasRef ? issueOrPrLink(agent.issue, agent.pr_number) : escapeHtml(formatRole(agent.role, agent.pipeline_variant));
  var prBadge = (agent.issue && agent.pr_number) ? '<span class="text-xs">' + prLink(agent.pr_number) + '</span>' : '';

  // Stuck detection uses run-level elapsed_seconds, consistently with the WS
  // path and with renderStatusBadge's own _STUCK_THRESHOLD_S semantics (5
  // minutes of run elapsed time, not time-in-step). time_in_step_ms/its
  // client-side approximation is a separate, distinctly-labeled "time in
  // current step" display field below and must never feed the stuck
  // computation.
  var stepElapsedSec = agent.current_step ? _stepElapsedSeconds(agent.run_id, agent.current_step) : null;

  return '<div id="agent-card-' + agent.run_id + '" class="bg-surface-card rounded-lg border ' + colors.border + ' p-4 transition-all">' +
    '<div class="flex items-center justify-between mb-3 cursor-pointer hover:opacity-80"' + actionAttrs('navigate', [detailUrl]) + '>' +
      '<div class="flex items-center gap-2">' +
        '<span class="text-sm font-medium">' + issueLabel + '</span>' +
        '<span class="px-1.5 py-0.5 rounded text-xs font-medium ' + colors.bg + ' ' + colors.text + '">' + escapeHtml(formatRole(agent.role, agent.pipeline_variant)) + '</span>' +
        (agent.source === 'external' ? '<span class="px-1.5 py-0.5 rounded text-xs font-medium bg-gray-700 text-gray-300">CLI</span>' : '') +
        prBadge +
        renderStatusBadge(agent.status, agent.current_step, agent.step_index, agent.total_steps, agent.elapsed_seconds) +
      '</div>' +
      '<div class="flex items-center gap-3 text-xs">' +
        '<span class="text-accent" data-field="cpu">' + (agent.cpu_percent != null ? 'CPU ' + agent.cpu_percent.toFixed(1) + '%' : 'CPU —') + '</span>' +
        '<span class="text-accent-green" data-field="mem">' + (agent.memory_rss_bytes != null ? (agent.memory_rss_bytes / 1048576).toFixed(0) + ' MB' : '— MB') + '</span>' +
        '<span class="text-gray-500" data-field="elapsed">' + liveElapsed(agent.elapsed_seconds) + '</span>' +
        '<span class="text-gray-600" data-field="step-elapsed" data-tooltip="Time in current step">' +
          (stepElapsedSec != null ? 'Step: ' + liveElapsed(stepElapsedSec) : 'Step: —') +
        '</span>' +
        '<span class="text-accent-green" data-field="cost">' + cost + '</span>' +
      '</div>' +
    '</div>' +
    '<div class="mb-3" data-pipeline-container>' + pipeline + '</div>' +
    '<div class="flex items-center gap-2 mt-3">' +
      '<button id="log-btn-' + agent.run_id + '"' + actionAttrs('view-logs', [agent.run_id, agent.status]) + ' class="px-3 py-1.5 rounded text-xs bg-surface-hover text-gray-300 hover:text-white transition-colors" aria-label="View agent output" data-tooltip="View agent output">' +
        (getActiveLogRunId() === agent.run_id ? 'Viewing Logs' : 'View Logs') +
      '</button>' +
      (agent.source !== 'external'
        ? '<button' + actionAttrs('stop-agent', [agent.run_id]) + ' class="px-3 py-1.5 rounded text-xs bg-accent-red/10 text-accent-red hover:bg-accent-red/20 transition-colors" aria-label="Stop this agent" data-tooltip="Stop this agent">Stop</button>'
        : '') +
    '</div>' +
  '</div>';
}

function renderCompletedCard(agent) {
  var colors = roleColor(agent.role);
  var borderColor = agent.status === 'done' ? 'border-accent-green/40' : 'border-accent-red/40';
  var cost = agent.cost_usd ? '$' + parseFloat(agent.cost_usd).toFixed(4) : '$0.00';
  // liveElapsed, not a static string: with no data-field for _refreshCardMetrics
  // to patch, a static "Ns ago" froze the moment an active run's cost churn
  // stopped forcing the whole grid to re-render on every tick. The shared 1s
  // .sova-status-elapsed ticker (_tickElapsedTimers() in live-status.js) drives it instead.
  var agoText = liveElapsed(agent.completed_seconds_ago) + ' ago';
  var prefix = window.SOVA_PROJECT_SLUG ? '/p/' + window.SOVA_PROJECT_SLUG : '';
  var detailUrl = prefix + '/work/' + agent.run_id;

  var hasRef = agent.issue || agent.pr_number;
  var issueLabel = hasRef ? issueOrPrLink(agent.issue, agent.pr_number) : escapeHtml(formatRole(agent.role));
  var prBadge = (agent.issue && agent.pr_number) ? '<span class="text-xs">' + prLink(agent.pr_number) + '</span>' : '';

  // A command or planner run has no issue, and a relaunch needs one: without it the button would send the
  // modal the string "null". The history table makes the same choice (issue_number ? button : '').
  var rerunBtn = agent.issue
    ? '<button class="rerun-btn px-3 py-1.5 rounded text-xs bg-accent-yellow/10 text-accent-yellow hover:bg-accent-yellow/20 transition-colors" data-action="rerun" aria-label="Re-run this task" data-tooltip="Re-run this task" ' +
      'data-issue="' + escapeHtml(String(agent.issue)) + '" data-role="' + escapeHtml(agent.role || '') + '" data-pr="' + escapeHtml(String(parseInt(agent.pr_number, 10) || '')) + '" ' +
      'data-run-id="' + escapeHtml(String(agent.run_id)) + '">Re-run</button>'
    : '';

  var isPlanner = agent.role === 'planner';
  var plannerExtra = isPlanner ? '<div id="planner-extra-' + agent.run_id + '"></div>' : '';
  var navigateAttrs = actionAttrs('navigate', [detailUrl]);
  var cardClass = isPlanner ? '' : ' cursor-pointer hover:opacity-90';
  var cardAttrs = isPlanner ? '' : navigateAttrs;
  var headerClass = isPlanner ? ' cursor-pointer hover:opacity-80' : '';
  var headerAttrs = isPlanner ? navigateAttrs : '';

  return '<div class="bg-surface-card rounded-lg border ' + borderColor + ' p-4 opacity-75 transition-all' + cardClass + '"' + cardAttrs + '>' +
    '<div class="flex items-center justify-between' + headerClass + '"' + headerAttrs + '>' +
      '<div class="flex items-center gap-2">' +
        '<span class="text-sm font-medium">' + issueLabel + '</span>' +
        '<span class="px-1.5 py-0.5 rounded text-xs font-medium ' + colors.bg + ' ' + colors.text + '">' + escapeHtml(formatRole(agent.role)) + '</span>' +
        renderStatusBadge(agent.status) +
        prBadge +
      '</div>' +
      '<div class="flex items-center gap-3 text-xs">' +
        rerunBtn +
        '<span class="text-accent-green">' + cost + '</span>' +
        '<span class="text-gray-500">' + agoText + '</span>' +
      '</div>' +
    '</div>' +
    plannerExtra +
  '</div>';
}

// Planner card enhancement

var _plannerHandoffCache = null;

async function enhancePlannerCards(completed) {
  var plannerAgents = (completed || []).filter(function(a) { return a.role === 'planner'; });
  if (plannerAgents.length === 0) return;

  // Fetch handoff only once; reuse cache for subsequent polls
  if (!_plannerHandoffCache) {
    try {
      var res = await fetch(apiUrl('/handoff?issue=planner'));
      if (!res.ok) return;
      var data = await res.json();
      if (!data.handoff || !data.handoff.details || !data.handoff.details.planned_tasks) return;
      _plannerHandoffCache = data.handoff;
    } catch (e) {
      return;
    }
  }

  try {
    var tasks = _plannerHandoffCache.details.planned_tasks;
    var handoffIssue = _plannerHandoffCache.issue || 'planner';

    // Only apply the handoff to the most recent planner run to avoid
    // showing stale proposals on older cards.
    var latestPlanner = plannerAgents[0];
    plannerAgents.forEach(function(agent) {
        var card = document.getElementById('planner-extra-' + agent.run_id);
        if (!card) return;
        if (agent.run_id !== latestPlanner.run_id) return;

        if (tasks.length === 0) {
          card.innerHTML = '<p class="text-xs text-gray-500 mt-2">No tasks proposed</p>';
          return;
        }

        var taskListHtml = tasks.map(function(t, i) {
          var iconUrl = priorityIconUrl(t.priority);
          return '<label class="flex items-start gap-2 py-1 text-xs cursor-pointer" data-action="stop">' +
            '<input type="checkbox" checked class="mt-0.5 planner-task-check" data-idx="' + i + '">' +
            '<span class="flex-1"><img src="' + iconUrl + '" alt="' + escapeHtml(t.priority || 'undefined') + ' priority" width="12" height="12" class="inline-block align-middle mr-1"> ' + escapeHtml(t.title) + '</span>' +
          '</label>';
        }).join('');

        var createBtnDisabled = _plannerHandoffCache.status !== 'awaiting_action' ? ' disabled' : '';
        var createBtnClass = _plannerHandoffCache.status === 'awaiting_action'
          ? 'bg-accent-green/20 text-accent-green hover:bg-accent-green/30 border-accent-green/30'
          : 'bg-gray-600/20 text-gray-500 border-gray-600/30 cursor-not-allowed';

        card.innerHTML =
          '<div class="mt-3 border-t border-gray-700/30 pt-3">' +
            '<p class="text-xs text-gray-400 mb-2">Proposed ' + tasks.length + ' tasks:</p>' +
            '<div class="max-h-40 overflow-y-auto space-y-0.5 mb-2" id="planner-task-list-' + agent.run_id + '">' +
              taskListHtml +
            '</div>' +
            '<button' + actionAttrs('submit-planner-issues', [agent.run_id, handoffIssue != null ? String(handoffIssue) : '']) + createBtnDisabled +
              ' class="px-3 py-1.5 rounded text-xs border transition-colors ' + createBtnClass + '"' +
              ' aria-label="Create selected issues on the tracker"' +
              ' data-tooltip="Create selected issues on the tracker">' +
              'Create Issues' +
            '</button>' +
          '</div>';
      });
  } catch (e) {
    // Handoff file may be cleared; silently ignore
  }
}

async function createPlannerIssues(tasks, issue) {
  try {
    var res = await fetch(apiUrl('/agents/planner/create-issues'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tasks: tasks }),
    });
    if (!res.ok) {
      showToast(await getErrorDetail(res, 'Failed to create issues'), 'error');
      return;
    }
    var data = await res.json();
    var createdCount = data.created ? data.created.length : 0;
    var errorCount = data.errors ? data.errors.length : 0;

    if (createdCount > 0) {
      var nums = data.created.map(function(c) { return '#' + c.number; }).join(', ');
      showToast('Created ' + createdCount + ' issues: ' + nums, 'success');
      var withDropped = data.created.filter(function(c) { return c.dropped_labels && c.dropped_labels.length; });
      withDropped.forEach(function(c) {
        showToast('#' + c.number + ': label(s) not applied: ' + c.dropped_labels.join(', '), 'warning');
      });
    }
    if (errorCount > 0) {
      var errTitles = data.errors.map(function(e) { return e.title; }).join(', ');
      showToast(errorCount + ' issues failed: ' + errTitles, 'error');
    }

    // Only clear cached handoff if all tasks succeeded
    if (errorCount === 0) {
      _plannerHandoffCache = null;
      if (issue) dismissHandoff(issue);
    }
    loadAgents();
    loadTaskCards();
  } catch (e) {
    showToast('Failed to create issues: ' + e.message, 'error');
  }
}

function _submitPlannerIssues(runId, handoffIssue) {
  var container = document.getElementById('planner-task-list-' + runId);
  if (!container) return;

  // Use cached tasks from enhancePlannerCards: no re-fetch needed
  if (!_plannerHandoffCache || !_plannerHandoffCache.details || !_plannerHandoffCache.details.planned_tasks) {
    showToast('Task data not available: try refreshing', 'error');
    return;
  }
  var allTasks = _plannerHandoffCache.details.planned_tasks;

  var checks = container.querySelectorAll('.planner-task-check');
  var selected = [];
  checks.forEach(function(cb) {
    if (cb.checked) {
      var idx = parseInt(cb.getAttribute('data-idx'), 10);
      var task = allTasks[idx];
      if (task) {
        // Labels are tracker-specific (Jira rejects space-containing labels),
        // so the server builds them from these raw fields rather than the
        // client constructing 'area: x' / 'priority: y' strings itself.
        selected.push({
          title: task.title,
          description: task.description,
          component: task.component || '',
          priority: task.priority || '',
        });
      }
    }
  });

  if (selected.length === 0) {
    showToast('No tasks selected', 'warning');
    return;
  }

  createPlannerIssues(selected, handoffIssue);
}

// Agent actions

async function stopAgent(runId) {
  try {
    await fetch(apiUrl('/agents/' + runId + '/stop'), { method: 'POST' });
    loadAgents();
  } catch (e) {
    showToast('Failed to stop agent: ' + e.message, 'error');
  }
}

// Handoff inline

async function executeHandoffAction(actionId, issue) {
  lastAgentFingerprint = null;

  try {
    var payload = { action_id: actionId };
    if (issue) payload.issue = issue;
    var res = await fetch(apiUrl('/handoff/execute'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (!res.ok) {
      showToast(await getErrorDetail(res, 'Failed to execute action'), 'error');
      loadAgents();
      return;
    }
    var result = await res.json();
    _autoOpenLogsIfIdle(result.run_id);
    loadAgents();
    loadTaskCards();
  } catch (e) {
    showToast('Failed to execute action: ' + e.message, 'error');
    loadAgents();
  }
}

async function resumeFromApproval(runId) {
  try {
    var res = await fetch(apiUrl('/agents/' + runId + '/resume-from-approval'), { method: 'POST' });
    if (!res.ok) {
      showToast(await getErrorDetail(res, 'Failed to resume'), 'error');
      return;
    }
    var result = await res.json();
    if (result.error) {
      showToast(result.detail || 'Failed to resume', 'error');
      return;
    }
    _autoOpenLogsIfIdle(result.run_id);
    loadAgents();
    loadTaskCards();
  } catch (e) {
    showToast('Failed to resume: ' + e.message, 'error');
  }
}

var _rebaseInFlight = new Set();

async function triggerRebase(btnEl, issue) {
  if (_rebaseInFlight.has(issue)) return;
  // Guarded before the in-flight marker is set: a rejected attempt must not
  // leave the issue permanently marked as having a rebase in progress.
  if (blockedByConnectivity('rebasing')) return;
  _rebaseInFlight.add(issue);
  if (btnEl) btnEl.disabled = true;
  try {
    var res = await fetch(apiUrl('/work/issue/' + encodeURIComponent(issue) + '/rebase'), { method: 'POST' });
    if (!res.ok) {
      showToast(await getErrorDetail(res, 'Failed to trigger rebase'), 'error');
      return;
    }
    var result = await res.json();
    if (result.status === 'failed') {
      showToast(result.error || 'Rebase failed', 'error');
    } else if (result.status === 'skipped') {
      showToast(result.reason || 'Rebase skipped', 'warning');
    } else {
      showToast('Rebase succeeded', 'success');
    }
    loadAgents();
    loadTaskCards();
  } catch (e) {
    showToast('Failed to trigger rebase: ' + e.message, 'error');
  } finally {
    _rebaseInFlight.delete(issue);
    if (btnEl) btnEl.disabled = false;
  }
}

// Handoff actions are now rendered inline in task cards via loadWorkItems()

async function dismissHandoff(issue) {
  try {
    var payload = issue ? { issue: issue } : {};
    await fetch(apiUrl('/handoff/clear'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
  } catch (e) { /* ignore */ }
  loadWorkItems();
}

export function init() {
  registerActions({
    'stop-agent': function(el, e, runId) { stopAgent(runId); },
    'execute-handoff-action': function(el, e, actionId, issue) { executeHandoffAction(actionId, issue); },
    'resume-from-approval': function(el, e, runId) { resumeFromApproval(runId); },
    'trigger-rebase': function(el, e, issue) { triggerRebase(el, issue); },
    'submit-planner-issues': function(el, e, runId, handoffIssue) { _submitPlannerIssues(runId, handoffIssue); },
  });

  loadAgents();
}
