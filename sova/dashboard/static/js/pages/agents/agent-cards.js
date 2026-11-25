import { actionAttrs, registerActions } from './actions.js';
import { updateLogTabs } from './log-viewer.js';
import { loadBoard } from '../work/board.js';

// What the Work page shows about runs outside the board: the log viewer's tab per running agent, and each
// finished planner run with its proposed issues and the "Create issues" step. Issue runs are on the board
// (work/board.js); this module renders no agent card for them.

var _completedCleanupTimer = null;
var _lastFingerprint = null;
// Set when the planner cards must be drawn again though no run changed: after the issues were created (the old
// Create Issues button must go) and after a handoff fetch failed (so the next load retries it).
var _plannerStale = false;
var _lastActiveAgents = [];

// The last load's running agents, for the log viewer's tab strip: a click on "View log" has no fetch of its
// own, and the strip must repaint at once. A getter, never an exported `var`: an importer cannot assign to one.
export function getActiveAgents() { return _lastActiveAgents; }

export async function loadAgents() {
  try {
    var data = await fetchAPI(apiUrl('/agents/active'));
    _lastActiveAgents = data.agents;
    var planners = (data.completed || []).filter(function(a) { return a.role === 'planner'; });
    var fp = data.agents.map(function(a) { return a.run_id + ':' + a.status; }).join(',') + '|' +
      planners.map(function(a) { return a.run_id + ':' + a.status; }).join(',');
    if (fp === _lastFingerprint && !_plannerStale) return;
    _lastFingerprint = fp;
    _plannerStale = false;
    updateLogTabs(data.agents);

    var host = document.getElementById('planner-results');
    if (!host) return;
    host.innerHTML = planners.map(renderCompletedCard).join('');
    host.classList.toggle('hidden', planners.length === 0);
    enhancePlannerCards(planners);

    // The server keeps a finished run in `completed` for 30s: load again when the newest one expires.
    if (planners.length > 0) {
      var maxAge = 0;
      planners.forEach(function(agent) {
        var remaining = 30 - (agent.completed_seconds_ago || 0);
        if (remaining > maxAge) maxAge = remaining;
      });
      if (_completedCleanupTimer) clearTimeout(_completedCleanupTimer);
      _completedCleanupTimer = setTimeout(function() {
        _completedCleanupTimer = null;
        loadAgents();
      }, maxAge > 0 ? (maxAge + 1) * 1000 : 2000);
    }
  } catch (e) {
    console.error('Failed to load agents:', e);
  }
}

function renderCompletedCard(agent) {
  var colors = roleColor(agent.role);
  var borderColor = agent.status === 'done' ? 'border-accent-green/40' : 'border-accent-red/40';
  var cost = agent.cost_usd ? '$' + parseFloat(agent.cost_usd).toFixed(4) : '$0.00';
  // liveElapsed, not a static string: with no data-field for _refreshCardMetrics
  // to patch, a static "Ns ago" froze the moment an active run's cost churn
  // stopped forcing the whole grid to re-render on every tick. The shared 1s
  // .sova-status-elapsed ticker (tickElapsedTimers() in app.js) drives it instead.
  var agoText = liveElapsed(agent.completed_seconds_ago) + ' ago';
  var prefix = window.SOVA_PROJECT_SLUG ? '/p/' + window.SOVA_PROJECT_SLUG : '';
  var detailUrl = prefix + '/work/' + agent.run_id;

  var hasRef = agent.issue || agent.pr_number;
  var issueLabel = hasRef ? issueOrPrLink(agent.issue, agent.pr_number) : escapeHtml(formatRole(agent.role));
  var prBadge = (agent.issue && agent.pr_number) ? '<span class="text-xs">' + prLink(agent.pr_number) + '</span>' : '';

  // A command or planner run has no issue, and a relaunch needs one: without it the button would send the
  // modal the string "null". The history table makes the same choice (issue_number ? button : '').
  var rerunBtn = agent.issue
    ? '<button class="rerun-btn sova-btn sova-btn-warning" data-action="rerun" aria-label="Re-run this task" data-tooltip="Re-run this task" ' +
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
      if (!res.ok) {
        _plannerStale = true;
        return;
      }
      var data = await res.json();
      if (!data.handoff || !data.handoff.details || !data.handoff.details.planned_tasks) return;
      _plannerHandoffCache = data.handoff;
    } catch (e) {
      _plannerStale = true;
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
          // Not emptyStateHtml(): a one-line note under a run card, where page-level padding would dwarf the card.
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

        card.innerHTML =
          '<div class="mt-3 border-t border-gray-700/30 pt-3">' +
            '<p class="text-xs text-gray-400 mb-2">Proposed ' + tasks.length + ' tasks:</p>' +
            '<div class="max-h-40 overflow-y-auto space-y-0.5 mb-2" id="planner-task-list-' + agent.run_id + '">' +
              taskListHtml +
            '</div>' +
            '<button' + actionAttrs('submit-planner-issues', [agent.run_id, handoffIssue != null ? String(handoffIssue) : '']) + createBtnDisabled +
              ' class="sova-btn sova-btn-success"' +
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

async function createPlannerIssues(tasks, issue, proposalIdxs) {
  try {
    var res = await fetch(apiUrl('/agents/planner/create-issues'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tasks: tasks }),
    });
    // A partial failure is a 502 whose structured detail carries a per-task results list; any other
    // error detail (a string, or a validation list) is shown as is.
    var data;
    var createdIdxs = [];
    if (res.ok) {
      data = await res.json();
    } else {
      var body = null;
      try { body = await res.clone().json(); } catch (_e) { /* not JSON: falls through to getErrorDetail() */ }
      var results = body && body.detail && Array.isArray(body.detail.results) ? body.detail.results : null;
      if (!results) {
        showToast(await getErrorDetail(res, 'Failed to create issues'), 'error');
        return;
      }
      data = {
        created: results.filter(function(r) { return r.ok; }),
        errors: results.filter(function(r) { return !r.ok; }),
      };
      // `results` is in request order, so result i answers the proposal at proposalIdxs[i].
      createdIdxs = [];
      results.forEach(function(r, i) { if (r.ok) createdIdxs.push(proposalIdxs[i]); });
    }
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
      _plannerStale = true;
      if (issue) dismissHandoff(issue);
    } else if (createdCount > 0) {
      // A partial batch already created some issues: drop them from the proposals so a retry
      // submits only the failed ones and never duplicates a created issue.
      _dropCreatedPlannerTasks(createdIdxs);
    }
    loadAgents();
    loadBoard();
  } catch (e) {
    showToast('Failed to create issues: ' + e.message, 'error');
  }
}

function _dropCreatedPlannerTasks(createdIdxs) {
  if (!_plannerHandoffCache || !_plannerHandoffCache.details) return;
  // By position in the cached proposals, never by title: the tracker's echo of a title can differ from the one
  // submitted. The cache itself is kept (only `_plannerStale` is set), so the redraw shows the trimmed list.
  _plannerHandoffCache.details.planned_tasks = _plannerHandoffCache.details.planned_tasks.filter(function(_t, i) {
    return createdIdxs.indexOf(i) === -1;
  });
  _plannerStale = true;
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
  var selectedIdxs = [];
  checks.forEach(function(cb) {
    if (cb.checked) {
      var idx = parseInt(cb.getAttribute('data-idx'), 10);
      var task = allTasks[idx];
      if (task) {
        // Labels are tracker-specific (Jira rejects space-containing labels),
        // so the server builds them from these raw fields rather than the
        // client constructing 'area: x' / 'priority: y' strings itself.
        selectedIdxs.push(idx);
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

  createPlannerIssues(selected, handoffIssue, selectedIdxs);
}

export function init() {
  registerActions({
    'submit-planner-issues': function(el, e, runId, handoffIssue) { _submitPlannerIssues(runId, handoffIssue); },
  });

  loadAgents();
}
