import { actionAttrs, registerActions } from './actions.js';
import { _updateApiHealthBanner } from './live-status.js';
import { diffSizeColor, diffSizeLabel, formatAgeBadge, renderAssigneeAvatar, renderJiraInitial } from './render-utils.js';

// Task browser

var _agentHistory = [];
var _suggested = [];
var _runningAgents = {};
var _lastLoadedRunIds = null;

// State other modules read goes out through a getter, never an exported `var`: an importer sees a live
// binding it cannot assign to, and a value read at an importer's top level would be undefined or stale
// depending on module evaluation order.
export function getLastLoadedRunIds() { return _lastLoadedRunIds; }

function stateTag(state, stateLabel, stateColor) {
  var label = stateLabel || state;
  return '<span class="px-2 py-0.5 rounded-full text-xs whitespace-nowrap ' + stateBadgeClasses(stateColor) + '">' + escapeHtml(label) + '</span>';
}

var _TIER2_PAGE_SIZE = 15;
var _MIN_BEFORE_COLLAPSE = 8;
var _expansionTier = 0; // 0=top 8, 1=+15 more, 2=show all
var _cardMenuOpen = false;
var _workItems = [];
var _githubUser = '';
var _githubUserListeners = [];
export function getGithubUser() { return _githubUser; }
// A module that renders from the GitHub user (the PR tracker's "Mine" filter) cannot watch a getter, and
// the user only arrives with the first work-items response. It registers here to redraw once it does.
export function onGithubUserChange(fn) { _githubUserListeners.push(fn); }
function _setGithubUser(user) {
  if (!user || user === _githubUser) return;
  _githubUser = user;
  _githubUserListeners.forEach(function(fn) {
    try {
      fn();
    } catch (e) {
      console.error('GitHub user listener failed', e);
    }
  });
}
var _jiraDisplayName = '';
// safeStorageGet/Set, not localStorage directly: a throwing storage (blocked cookies, some private modes)
// at module evaluation would fail the whole import graph and leave the Agents page blank.
var _taskAssigneeFilter = safeStorageGet('sova-task-assignee-filter') || 'all';
var _taskStateFilter = safeStorageGet('sova-task-state-filter') || '';

var _STATE_FILTER_GROUPS = {
  running: ['agent_running'],
  pr_stage: ['pr_draft', 'pr_conflicted', 'pr_ci_running', 'pr_ci_failed', 'pr_awaiting_review',
    'pr_sova_pending', 'pr_sova_changes', 'pr_external_changes',
    'pr_review_addressed', 'pr_ready_to_merge', 'pr_review_exhausted'],
  ci_failed: ['pr_ci_failed'],
  needs_review: ['pr_awaiting_review', 'pr_sova_pending'],
  changes_requested: ['pr_sova_changes', 'pr_external_changes'],
  ready_to_merge: ['pr_ready_to_merge', 'pr_review_exhausted'],
  backlog: ['backlog', 'triaged', 'researched', 'needs_spec']
};

// LLM suggestion experiment: keyed by issueKey (issue_number or "pr:N")
// Value: {action_id, action_label, reasoning, for_state, done, pr_number, issue_number,
//         deterministic_action_id, disagrees} | null (null = unavailable/error)
var _llmSuggestions = {};
var _flaggedWrong = new Map();  // issueKey -> state that was flagged
var _llmSuggestionFetching = new Set();

// Built from the filter group so the PR stages are listed once in this module: a stage added to one list
// and not the other would filter, sort and suggest differently.
var _PR_STAGE_STATES = new Set(_STATE_FILTER_GROUPS.pr_stage);

export async function loadWorkItems() {
  try {
    var res = await fetch(apiUrl('/agents/work-items'));
    if (!res.ok) return;
    var data = await res.json();
    _workItems = data.items || [];
    _setGithubUser(data.github_user);
    _jiraDisplayName = data.jira_display_name || '';
    _updateApiHealthBanner(data.api_health);

    var slotsInfo = document.getElementById('slots-info');
    slotsInfo.textContent = data.slots_available + '/' + data.max_concurrent + ' slots';

    _runningAgents = {};
    _workItems.forEach(function(item) {
      if (item.running_agent && item.issue_number) {
        _runningAgents[String(item.issue_number)] = {
          role: item.running_agent.role,
          run_id: item.running_agent.run_id,
        };
      }
    });
    // Snapshot of currently-running run_ids as the Tasks list itself knows
    // them, so the WebSocket's first status_update after connect can tell a
    // genuinely new run apart from the common case of the socket opening
    // moments after this same data already loaded.
    _lastLoadedRunIds = Object.keys(_runningAgents).map(function(k) {
      return _runningAgents[k].run_id;
    }).sort().join(',');

    _agentHistory = _workItems.filter(function(i) { return i.last_run || i.pr_number; });
    _suggested = _workItems.filter(function(i) {
      return !i.last_run && !i.pr_number && (i.state === 'triaged' || i.state === 'researched');
    }).sort(_sortWorkItems);

    var attentionCount = _workItems.filter(function(i) {
      return i.state === 'spec_review' ||
        i.state === 'pr_ready_to_merge' ||
        i.state === 'pr_review_exhausted';
    }).length;
    var badge = document.getElementById('tasks-attention-badge');
    if (badge) {
      if (attentionCount > 0) {
        badge.textContent = attentionCount;
        badge.classList.remove('hidden');
      } else {
        badge.classList.add('hidden');
      }
    }

    renderTaskCards();
  } catch (e) {
    document.getElementById('task-grid').innerHTML =
      '<p class="text-accent-red text-sm py-6 text-center">Failed to load tasks</p>';
  }
}

export function loadTaskCards() { loadWorkItems(); }

var _STAT_ACCENTS = {
  'stat-today-cost': 'text-accent-green',
  'stat-invocations': '',
  'stat-completion': 'text-accent-purple'
};

async function loadAgentsStatRow() {
  try {
    var overview = await fetchAPI(apiUrl('/overview'));
    var costs = overview.costs;
    var runs = overview.runs;

    _restoreAgentsStatRow();

    document.getElementById('stat-today-cost').textContent =
      '$' + parseFloat(costs.today_cost_usd || 0).toFixed(2);
    document.getElementById('stat-invocations').textContent =
      costs.total_invocations != null ? costs.total_invocations : '--';

    var finished = runs.done + runs.failed;
    var completionEl = document.getElementById('stat-completion');
    completionEl.textContent = finished > 0 ? Math.round((runs.done / finished) * 100) + '%' : 'N/A';
    completionEl.setAttribute('data-tooltip', runs.done + ' done, ' + runs.failed + ' failed');
    completionEl.setAttribute('data-tooltip-pos', 'bottom');
  } catch (e) {
    console.error('Failed to load stat row:', e);
    // A failed refresh must not leave the previous poll's numbers on screen
    // looking current. Use a distinct "Error" marker rather than the "--"
    // placeholder: "--" also means "hasn't loaded yet", so reusing it here
    // would make a persistent fetch failure indistinguishable from a page
    // that simply hasn't finished its first load.
    _clearAgentsStatRow();
  }
}

function _restoreAgentsStatRow() {
  // stat-completion's tooltip is set by the success path itself below, not here.
  Object.keys(_STAT_ACCENTS).forEach(function(id) {
    var el = document.getElementById(id);
    if (!el) return;
    el.classList.remove('text-red-400');
    if (id !== 'stat-completion') el.removeAttribute('data-tooltip');
    if (_STAT_ACCENTS[id]) el.classList.add(_STAT_ACCENTS[id]);
  });
}

function _clearAgentsStatRow() {
  Object.keys(_STAT_ACCENTS).forEach(function(id) {
    var el = document.getElementById(id);
    if (!el) return;
    el.textContent = 'Error';
    el.setAttribute('data-tooltip', 'Failed to load');
    if (_STAT_ACCENTS[id]) el.classList.remove(_STAT_ACCENTS[id]);
    el.classList.add('text-red-400');
  });
}

function filterTaskCards() {
  renderTaskCards();
}

function setTaskAssigneeFilter(value) {
  _taskAssigneeFilter = value;
  safeStorageSet('sova-task-assignee-filter', value);
  _updateTaskAssigneeToggleUI();
  _expansionTier = 0;
  renderTaskCards();
}

function setTaskStateFilter(value) {
  _taskStateFilter = value;
  safeStorageSet('sova-task-state-filter', value);
  _expansionTier = 0;
  renderTaskCards();
}

function _updateTaskAssigneeToggleUI() {
  document.querySelectorAll('#task-assignee-toggle button').forEach(function(btn) {
    var val = btn.getAttribute('data-task-assignee');
    if (val === _taskAssigneeFilter) {
      btn.className = 'px-2.5 py-1 transition-colors bg-accent/20 text-accent font-medium';
      btn.setAttribute('aria-pressed', 'true');
    } else {
      btn.className = 'px-2.5 py-1 transition-colors text-gray-500 hover:text-gray-300';
      btn.setAttribute('aria-pressed', 'false');
    }
  });
}

function _initTaskFilters() {
  _updateTaskAssigneeToggleUI();
  var stateSelect = document.getElementById('task-state-filter');
  if (stateSelect && _taskStateFilter) {
    stateSelect.value = _taskStateFilter;
  }
}

function _applyTaskFilters(items) {
  var filtered = items;

  if (_taskAssigneeFilter === 'mine' && _githubUser) {
    var user = _githubUser.toLowerCase();
    var jiraUser = _jiraDisplayName ? _jiraDisplayName.toLowerCase() : '';
    filtered = filtered.filter(function(t) {
      var assignees = (t.assignees || []).map(function(a) { return a.toLowerCase(); });
      var author = ((t.pr_details || {}).author || '').toLowerCase();
      return assignees.indexOf(user) !== -1 || author === user ||
        (jiraUser && assignees.indexOf(jiraUser) !== -1);
    });
  }

  if (_taskStateFilter && _STATE_FILTER_GROUPS[_taskStateFilter]) {
    var allowed = _STATE_FILTER_GROUPS[_taskStateFilter];
    filtered = filtered.filter(function(t) {
      return allowed.indexOf(t.state) !== -1;
    });
  }

  var q = (document.getElementById('task-search').value || '').trim().toLowerCase().replace(/^#/, '');
  if (q) {
    filtered = filtered.filter(function(t) {
      return (t.issue_number && t.issue_number.toString().includes(q)) ||
        (t.pr_number && t.pr_number.toString().includes(q)) ||
        t.title.toLowerCase().includes(q) ||
        (t.jira_key && t.jira_key.toLowerCase().includes(q)) ||
        (t.assignees || []).some(function(a) { return a.toLowerCase().includes(q); }) ||
        (t.labels || []).some(function(l) { return l.toLowerCase().includes(q); }) ||
        (t.components || []).some(function(c) { return c.toLowerCase().includes(q); }) ||
        (t.state_label && t.state_label.toLowerCase().includes(q));
    });
  }

  return filtered;
}

// Issues no agent has started yet. Within this tier the issue's own priority
// outranks how far triage or research got, so a high triaged issue sits above a
// low researched one. Human-only and finished items sort after it.
var _NOT_STARTED_STATES = new Set(_STATE_FILTER_GROUPS.backlog);
var _NOT_STARTED_TIER = 6;

function _stateSortTier(item) {
  if (item.state === 'agent_running') return 0;
  if (item.state === 'pr_draft') return 5;
  if (_PR_STAGE_STATES.has(item.state)) return 2;
  if (item.state === 'spec_review') return 3;
  if (item.state === 'in_progress') return 4;
  if (_NOT_STARTED_STATES.has(item.state)) return _NOT_STARTED_TIER;
  return 7;
}

function _issuePriorityRank(item) {
  var rank = _priorityRankMap()[item.issue_priority];
  return rank === undefined ? 99 : rank;
}

function _sortWorkItems(a, b) {
  var ta = _stateSortTier(a), tb = _stateSortTier(b);
  if (ta !== tb) return ta - tb;
  var ra = _issuePriorityRank(a), rb = _issuePriorityRank(b);
  if (ta === _NOT_STARTED_TIER && ra !== rb) return ra - rb;
  if (a.priority !== b.priority) return a.priority - b.priority;
  return ra - rb;
}

function expandTasks() {
  _expansionTier++;
  renderTaskCards();
}

function collapseTasks() {
  _expansionTier = 0;
  renderTaskCards();
}

function _renderHandoffBtn(ha, issueNumber) {
  var cls = _actionStyles[ha.style] || _actionStyles.warning;
  return '<button' + actionAttrs('execute-handoff-action', [ha.id || ha.action || ha.label, String(issueNumber || '')]) +
    ' class="text-xs px-3 py-1 rounded transition-colors whitespace-nowrap ' + cls + '">' +
    escapeHtml(ha.label) + '</button>';
}

var _actionStyles = {
  success: 'bg-accent-green/20 text-accent-green hover:bg-accent-green/30',
  warning: 'bg-accent-yellow/20 text-accent-yellow hover:bg-accent-yellow/30',
  danger: 'bg-accent-red/20 text-accent-red hover:bg-accent-red/30',
  primary: 'bg-accent/20 text-accent hover:bg-accent/30',
  purple: 'bg-accent-purple/20 text-accent-purple hover:bg-accent-purple/30',
  approve: 'bg-accent-green/20 text-accent-green hover:bg-accent-green/30',
  neutral: 'bg-gray-600/30 text-gray-400 hover:bg-gray-600/50',
};

function _renderActionBtn(action, issueKey) {
  if (!action) return '';
  var cls = _actionStyles[action.style] || _actionStyles.primary;
  var args = action.handler_args || {};
  var attrs;
  if (action.handler === 'start_agent') {
    var issueArg = args.issue ? String(args.issue) : '';
    var roleArgs = [issueArg, args.role || 'developer'];
    if (args.pr) roleArgs.push(Number(args.pr));
    attrs = actionAttrs('quick-start-role', roleArgs);
  } else if (action.handler === 'run_command') {
    var cmdArgs = {};
    if (args.issue) cmdArgs.issue = String(args.issue);
    if (args.pr) cmdArgs.pr = Number(args.pr);
    attrs = actionAttrs('run-command', [args.command || '', cmdArgs]);
  } else if (action.handler === 'resume_from_approval') {
    attrs = actionAttrs('resume-from-approval', [Number(args.run_id)]);
  } else if (action.handler === 'handoff_execute') {
    attrs = actionAttrs('execute-handoff-action', [action.id || '', String(issueKey)]);
  } else if (action.handler === 'trigger_rebase') {
    attrs = actionAttrs('trigger-rebase', [String(args.issue || '')]);
  } else {
    return '';
  }
  return '<button' + attrs +
    ' class="text-xs px-3 py-1 rounded transition-colors whitespace-nowrap ' + cls + '">' +
    escapeHtml(action.label) + '</button>';
}

function renderTaskCard(item) {
  var issueKey = item.issue_number || ('pr:' + item.pr_number);
  var isRunning = item.state === 'agent_running';
  var p = item.issue_priority;
  var priorityIcon = '<img src="' + priorityIconUrl(p) + '" alt="' + escapeHtml(priorityDisplayName(p)) + ' priority" width="16" height="16" class="inline-block align-middle shrink-0">';

  var handoffActions = item.handoff_actions || [];
  var isSpecReview = item.state === 'spec_review';

  var specPrimaryBtns = '';
  var specMenuActions = [];
  if (isSpecReview) {
    var prefix = window.SOVA_PROJECT_SLUG ? '/p/' + window.SOVA_PROJECT_SLUG : '';
    var specUrl = prefix + '/spec/' + (item.issue_number || '');
    var viewBtn = '<a href="' + specUrl + '" data-action="stop" ' +
      'class="text-xs px-3 py-1 rounded transition-colors whitespace-nowrap bg-accent/20 text-accent hover:bg-accent/30">View Spec</a>';
    var approveBtn = '';
    handoffActions.forEach(function(ha) {
      var aid = ha.id || ha.action || '';
      if (aid === 'approve-spec') {
        approveBtn = _renderHandoffBtn(ha, issueKey);
      } else {
        specMenuActions.push(ha);
      }
    });
    specPrimaryBtns = viewBtn + approveBtn;
  }

  var actionBtn;
  if (isRunning && item.running_agent) {
    actionBtn = '<span class="flex items-center gap-1.5 text-xs text-accent-yellow whitespace-nowrap">' +
      '<span class="w-1.5 h-1.5 rounded-full bg-accent-yellow animate-pulse"></span>' +
      escapeHtml(item.running_agent.role_label || 'Running') + '</span>';
  } else if (isSpecReview) {
    actionBtn = specPrimaryBtns;
  } else {
    actionBtn = _renderActionBtn(item.primary_action, issueKey);
  }

  var failedDot = item.last_failed
    ? '<span class="w-1.5 h-1.5 rounded-full bg-accent-red shrink-0" data-tooltip="Last run failed"></span>'
    : '';

  var prBadge = '';
  if (item.pr_details) {
    var prStateClass = stateBadgeClasses(item.pr_details.state_color);
    var prUrl = item.pr_details.url || '';
    var isLink = prUrl.startsWith('https://');
    var openTag = isLink
      ? 'a href="' + escapeHtml(prUrl) + '" target="_blank" rel="noopener" data-action="stop" data-tooltip="Open PR on GitHub"'
      : 'span';
    var closeTag = isLink ? 'a' : 'span';
    prBadge = '<' + openTag + ' class="text-xs px-2 py-0.5 rounded-full ' + prStateClass +
      ' whitespace-nowrap shrink-0' + (isLink ? ' cursor-pointer hover:brightness-125' : '') + '">' +
      'PR #' + escapeHtml(String(item.pr_details.number)) + '</' + closeTag + '>';
  } else if (item.pr_number) {
    prBadge = '<span class="text-xs px-2 py-0.5 rounded-full bg-gray-600/30 text-gray-400 whitespace-nowrap shrink-0">' +
      prLink(item.pr_number) + '</span>';
  }

  var needsAttention = item.state === 'spec_review';
  var attentionDot = needsAttention
    ? '<span class="w-2 h-2 rounded-full bg-accent-peach animate-pulse shrink-0"></span>'
    : '';

  var issueDisplay = item.jira_key || item.issue_number;
  var issueRef = (item.issue_number || item.jira_key)
    ? '<span class="text-xs font-medium shrink-0">' + issueLink(issueDisplay, item.url) + '</span>'
    : '<span class="text-xs font-medium shrink-0">' + prLink(item.pr_number) + '</span>';

  var menuIssue = item.issue_number || ('pr-' + item.pr_number);

  var rowCls = needsAttention
    ? 'group hover:bg-surface-hover transition-colors relative border-l-2 border-accent-peach'
    : 'group hover:bg-surface-hover transition-colors relative';

  // Metadata indicators
  var assigneeHtml = '';
  var assignees = item.assignees || [];
  var prAuthor = (item.pr_details || {}).author || '';
  var prAssignees = ((item.pr_details || {}).assignees || []);
  if (assignees.length) {
    var isGithub = !item.jira_key;
    var avatarHtml;
    if (isGithub) {
      avatarHtml = renderAssigneeAvatar(assignees[0]);
    } else {
      var ghLogin = prAuthor || (prAssignees.length ? prAssignees[0] : '') || _githubUser;
      avatarHtml = ghLogin ? renderAssigneeAvatar(ghLogin) : renderJiraInitial(assignees[0]);
    }
    assigneeHtml = '<span class="flex items-center shrink-0">' + avatarHtml +
      (assignees.length > 1 ? '<span class="text-[9px] text-gray-500 ml-0.5">+' + (assignees.length - 1) + '</span>' : '') +
      '</span>';
  } else if (prAuthor) {
    assigneeHtml = '<span class="flex items-center shrink-0">' + renderAssigneeAvatar(prAuthor) + '</span>';
  }

  var pd = item.pr_details || {};

  var ageHtml = '';
  var ageSource = item.created_at || '';
  if (ageSource) {
    var ageMs = Date.now() - new Date(ageSource).getTime();
    if (!isNaN(ageMs) && ageMs > 0) {
      ageHtml = formatAgeBadge(Math.floor(ageMs / 1000), ageSource);
    }
  } else if (pd.age_seconds > 0) {
    ageHtml = formatAgeBadge(pd.age_seconds, null);
  }

  var diffHtml = '';
  if (pd.additions || pd.deletions) {
    var sz = diffSizeLabel(pd.additions, pd.deletions);
    diffHtml = '<span class="text-xs shrink-0 ' + diffSizeColor(pd.additions, pd.deletions) + '" ' +
      'data-tooltip="+' + pd.additions + ' / -' + pd.deletions + ' in ' + (pd.changed_files || '?') + ' files">' + sz + '</span>';
  }

  var draftHtml = '';
  if (pd.is_draft) {
    draftHtml = '<span class="text-xs px-1.5 py-0.5 rounded bg-gray-600/30 text-gray-400 shrink-0">Draft</span>';
  }

  var threadHtml = '';
  if (pd.thread_total > 0) {
    var trColor = pd.thread_resolved >= pd.thread_total ? 'text-accent-green' : 'text-accent-yellow';
    threadHtml = '<span class="text-xs shrink-0 ' + trColor + '" ' +
      'data-tooltip="' + pd.thread_resolved + '/' + pd.thread_total + ' review threads resolved">' +
      pd.thread_resolved + '/' + pd.thread_total + '</span>';
  }

  var conflictHtml = '';
  if (pd.mergeable === 'CONFLICTING') {
    var cUrl = pd.url ? escapeHtml(pd.url) + '/conflicts' : '#';
    conflictHtml = '<a href="' + cUrl + '" target="_blank" rel="noopener" ' +
      'data-action="stop" ' +
      'class="px-2 py-0.5 rounded-full text-xs bg-accent-red/20 text-accent-red hover:bg-accent-red/30 shrink-0" ' +
      'data-tooltip="This PR has merge conflicts that must be resolved">Conflicts</a>';
  }

  var behindHtml = '';
  if (pd.merge_state === 'BEHIND') {
    behindHtml = '<span class="px-2 py-0.5 rounded-full text-xs bg-accent-lavender/20 text-accent-lavender shrink-0" ' +
      'data-tooltip="Base branch moved on; Integrate rebases first">Behind base</span>';
  }

  var metaBadges = [assigneeHtml, ageHtml, diffHtml, draftHtml, threadHtml, conflictHtml, behindHtml].join('');
  var metaStrip = metaBadges
    ? '<span class="flex items-center gap-1.5 shrink-0">' + metaBadges + '</span>'
    : '';

  var reasonChain = item.reason_chain || [];
  var reasonHtml = '';
  if (reasonChain.length) {
    // The tooltip host must not be the truncating element: `truncate` sets
    // overflow:hidden, which clips the absolutely-positioned ::after tooltip.
    reasonHtml = '<span class="max-w-[200px] min-w-0" ' +
      'data-tooltip="' + escapeHtml(reasonChain.join(' → ')) + '" data-tooltip-wide>' +
      '<span class="block text-xs text-gray-500 truncate">' +
      escapeHtml(item.matched_reason || reasonChain[reasonChain.length - 1]) + '</span></span>';
  }

  var mainRow = '<div class="flex items-center gap-3 px-4 py-2.5">' +
    (needsAttention ? attentionDot : priorityIcon) +
    issueRef +
    '<span class="text-sm text-gray-300 truncate flex-1">' + escapeHtml(item.title) + '</span>' +
    reasonHtml +
    metaStrip +
    failedDot +
    '<div class="flex items-center gap-2 shrink-0" style="width:340px">' +
      '<div class="w-20 flex justify-end">' + prBadge + '</div>' +
      '<div class="flex justify-end">' + stateTag(item.state, item.state_label, item.state_color) + '</div>' +
      '<div class="flex-1 flex justify-end gap-1.5">' + actionBtn + '</div>' +
    '</div>' +
    '<button id="card-menu-toggle-' + menuIssue + '"' + actionAttrs('toggle-card-menu', [String(menuIssue)]) + ' ' +
      'class="opacity-0 group-hover:opacity-100 text-gray-600 hover:text-gray-400 transition-all p-0.5 shrink-0 w-4" ' +
      'aria-label="More options" ' +
      'aria-expanded="false" ' +
      'aria-controls="card-menu-' + menuIssue + '" ' +
      'data-tooltip="More options">' +
      '<svg class="w-3.5 h-3.5" fill="currentColor" viewBox="0 0 20 20"><circle cx="10" cy="4" r="1.5"/><circle cx="10" cy="10" r="1.5"/><circle cx="10" cy="16" r="1.5"/></svg>' +
    '</button>' +
    '<div id="card-menu-' + menuIssue + '" class="hidden absolute right-4 top-8 z-50 bg-surface border border-gray-700 rounded-lg shadow-xl py-1 min-w-[160px]">' +
      (isRunning
        ? '<button' + actionAttrs('stop-agent', [item.running_agent ? item.running_agent.run_id : 0]) + ' class="w-full text-left px-3 py-1.5 text-xs text-accent-red hover:bg-surface-hover transition-colors">Stop Agent</button>'
        : (isSpecReview && specMenuActions.length
          ? specMenuActions.map(function(ha) {
              var aid = ha.id || ha.action || '';
              var menuColor = (aid === 'reject-spec') ? 'text-accent-red' : 'text-gray-300';
              return '<button' + actionAttrs('execute-handoff-action', [aid || ha.label, String(issueKey)]) +
                ' class="w-full text-left px-3 py-1.5 text-xs ' + menuColor + ' hover:bg-surface-hover transition-colors">' +
                escapeHtml(ha.label) + '</button>';
            }).join('')
          : (item.issue_number
            ? '<button' + actionAttrs('quick-start', [String(item.issue_number), true]) + ' class="w-full text-left px-3 py-1.5 text-xs text-gray-300 hover:bg-surface-hover transition-colors">Start with --force</button>' +
              '<button' + actionAttrs('quick-start-role', [String(item.issue_number), 'reviewer']) + ' class="w-full text-left px-3 py-1.5 text-xs text-gray-300 hover:bg-surface-hover transition-colors">Start as Reviewer</button>' +
              '<button' + actionAttrs('quick-start-role', [String(item.issue_number), 'triage']) + ' class="w-full text-left px-3 py-1.5 text-xs text-gray-300 hover:bg-surface-hover transition-colors">Start as Triage</button>'
            : ''))) +
      ((item.secondary_actions || []).length && !isRunning && !isSpecReview
        ? (function() {
            var sep = item.issue_number ? '<hr class="border-gray-700/50 my-1">' : '';
            return sep + (item.secondary_actions || []).map(function(sa) {
              if (/run_command/.test(sa.handler)) {
                // One guarded read: an action without handler_args must not throw inside renderTaskCard,
                // where loadWorkItems' catch would replace the whole grid with "Failed to load tasks".
                var ha = sa.handler_args || {};
                var cmd = ha.command || '';
                var args = {};
                if (ha.issue) args.issue = String(ha.issue);
                if (ha.pr) args.pr = ha.pr;
                // actionAttrs JSON-encodes the arguments, so newlines, control
                // characters, and quotes in the interpolated backend strings
                // (merge_blockers carries free-form GitHub merge states and verdict
                // text) cannot break out of the data-args attribute.
                return '<button' + actionAttrs('run-command-with-merge-blockers', [cmd, args, sa.merge_blockers || []]) + ' class="w-full text-left px-3 py-1.5 text-xs text-gray-300 hover:bg-surface-hover transition-colors">' + escapeHtml(sa.label) + '</button>';
              }
              return '';
            }).join('');
          })()
        : '') +
    '</div>' +
  '</div>';

  var expandedRow = '';
  if (isSpecReview && item.handoff_summary) {
    expandedRow = '<div class="flex items-center gap-3 px-4 pb-2.5 pl-12">' +
      '<span class="text-xs text-gray-500 truncate flex-1">' + escapeHtml(item.handoff_summary) + '</span>' +
    '</div>';
  }

  var llmRow = _renderLlmSuggestionWidget(item, issueKey);
  return '<div class="' + rowCls + '">' + mainRow + expandedRow + llmRow + '</div>';
}

function renderTaskCards() {
  _cardMenuOpen = false;
  var grid = document.getElementById('task-grid');
  var prefix = window.SOVA_PROJECT_SLUG ? '/p/' + window.SOVA_PROJECT_SLUG : '';

  var filtered = _applyTaskFilters(_workItems);
  var hasActiveFilter = _taskAssigneeFilter !== 'all' || _taskStateFilter ||
    (document.getElementById('task-search').value || '').trim();

  if (hasActiveFilter) {
    if (!filtered.length) {
      grid.innerHTML = '<p class="text-gray-500 text-sm py-6 text-center">No matching issues</p>';
      return;
    }
    grid.innerHTML = filtered.slice().sort(_sortWorkItems).map(renderTaskCard).join('');
    return;
  }

  var html = '';
  var hasHistory = _agentHistory.length > 0;
  var hasSuggested = _suggested.length > 0;

  if (!hasHistory && !hasSuggested) {
    grid.innerHTML = '<p class="text-gray-500 text-sm py-6 text-center">' +
      'No tasks available. <a href="' + prefix + '/queue" class="text-accent hover:underline">Visit the Priority Queue</a> to triage new issues.</p>';
    return;
  }

  if (hasHistory) {
    var sorted = _agentHistory.slice().sort(_sortWorkItems);

    var visible;
    var hiddenCount;
    var initialCount = Math.min(sorted.length, _MIN_BEFORE_COLLAPSE);
    if (_expansionTier === 0) {
      visible = sorted.slice(0, initialCount);
      hiddenCount = sorted.length - initialCount;
    } else if (_expansionTier === 1) {
      visible = sorted.slice(0, initialCount + _TIER2_PAGE_SIZE);
      hiddenCount = Math.max(0, sorted.length - visible.length);
    } else {
      visible = sorted;
      hiddenCount = 0;
    }

    html += visible.map(renderTaskCard).join('');

    if (hiddenCount > 0) {
      var showMoreLabel;
      if (_expansionTier >= 1) {
        // tier=1 -> tier=2 reveals everything; label must match
        showMoreLabel = 'Show all ' + hiddenCount + ' remaining';
      } else if (hiddenCount <= _TIER2_PAGE_SIZE) {
        showMoreLabel = 'Show ' + hiddenCount + ' more';
      } else {
        showMoreLabel = 'Show ' + _TIER2_PAGE_SIZE + ' more (' + hiddenCount + ' remaining)';
      }
      html += '<div class="px-4 py-2 border-t border-gray-700/30">' +
        '<button' + actionAttrs('expand-tasks') + ' class="text-xs text-gray-500 hover:text-gray-300 transition-colors">' +
        showMoreLabel + '</button></div>';
    } else if (_expansionTier > 0) {
      html += '<div class="px-4 py-2 border-t border-gray-700/30">' +
        '<button' + actionAttrs('collapse-tasks') + ' class="text-xs text-gray-500 hover:text-gray-300 transition-colors">' +
        'Show less</button></div>';
    }
  }

  if (hasSuggested) {
    var suggestedVisible = _suggested.slice(0, 5);
    html += '<div class="flex items-center gap-2 px-4 py-2 border-t border-gray-700/30">' +
      '<span class="text-xs text-gray-500">Suggested</span>' +
      '<div class="flex-1 border-t border-gray-700/20"></div>' +
    '</div>';
    html += '<div class="opacity-80">' + suggestedVisible.map(renderTaskCard).join('') + '</div>';
    if (_suggested.length > 5) {
      html += '<div class="px-4 py-2 border-t border-gray-700/30">' +
        '<a href="' + prefix + '/queue" class="text-xs text-gray-500 hover:text-gray-300 transition-colors">' +
        (_suggested.length - 5) + ' more in Priority Queue</a></div>';
    }
  }

  grid.innerHTML = html;
  fetchLlmSuggestions();
}

// --- LLM Suggestion Experiment ---

function _sanitizeIssueKey(issueKey) {
  // Collisions are impossible: issueKey is either a numeric issue_number
  // (e.g., "42") or "pr:<number>" (e.g., "pr:123"). Neither format
  // contains characters that would collide after sanitization.
  return issueKey.replace(/[^a-zA-Z0-9-]/g, '-');
}

function _findItemByKey(issueKey) {
  return _workItems.find(function(i) {
    return (i.issue_number || ('pr:' + i.pr_number)) === issueKey;
  });
}

function _renderLlmSuggestionWidget(item, issueKey) {
  if (!_PR_STAGE_STATES.has(item.state)) return '';
  if (!item.pr_number || !item.primary_action) return '';
  var safeKey = _sanitizeIssueKey(issueKey);
  var flagged = _flaggedWrong.get(issueKey) === item.state;
  var sugg = _llmSuggestions[issueKey];
  var hasDisagreement = sugg && !sugg.done && sugg.disagrees && sugg.for_state === item.state;

  if (!hasDisagreement && flagged) return '';

  var html = '<div class="flex items-center gap-2 pt-1.5 border-t border-gray-700/20 flex-wrap">';

  if (hasDisagreement) {
    var detLabel = (item.primary_action && item.primary_action.label) || 'current';
    html +=
      '<span class="text-xs text-accent-yellow font-medium whitespace-nowrap">LLM:</span>' +
      '<span class="text-xs text-gray-300 font-medium whitespace-nowrap">' + escapeHtml(sugg.action_label) + '</span>' +
      (sugg.reasoning
        ? '<span class="text-xs text-gray-500 italic truncate min-w-0">' + escapeHtml(sugg.reasoning) + '</span>'
        : '') +
      '<div class="flex gap-1.5 shrink-0 ml-auto" id="llm-feedback-' + safeKey + '">' +
        '<button' + actionAttrs('llm-feedback', [String(issueKey), 'deterministic']) +
          ' class="text-xs px-2 py-0.5 rounded bg-surface-hover text-gray-400 hover:text-gray-200 transition-colors">' +
          'Keep: ' + escapeHtml(detLabel) + '</button>' +
        '<button' + actionAttrs('llm-feedback', [String(issueKey), 'llm']) +
          ' class="text-xs px-2 py-0.5 rounded bg-surface-hover text-gray-400 hover:text-gray-200 transition-colors">' +
          'Use: ' + escapeHtml(sugg.action_label) + '</button>';
    if (!flagged) {
      html +=
        '<span class="text-gray-600 text-xs">|</span>' +
        '<button' + actionAttrs('flag-wrong', [String(issueKey)]) + ' id="flag-wrong-' + safeKey + '" ' +
          'class="text-xs px-2 py-0.5 rounded bg-surface-hover text-gray-500 hover:text-accent-yellow transition-colors">' +
          'State is wrong</button>';
    }
    html += '</div>';
  } else if (!flagged) {
    html +=
      '<button' + actionAttrs('flag-wrong', [String(issueKey)]) + ' id="flag-wrong-' + safeKey + '" ' +
        'class="text-xs px-2 py-0.5 rounded bg-surface-hover text-gray-500 hover:text-accent-yellow transition-colors">' +
        'State is wrong</button>';
  }

  html += '</div>';
  return '<div class="px-4 pb-2.5 pl-12">' + html + '</div>';
}

async function fetchLlmSuggestions() {
  var toFetch = [];
  _workItems.forEach(function(item) {
    if (!_PR_STAGE_STATES.has(item.state)) return;
    if (!item.pr_number || !item.primary_action) return;
    var key = item.issue_number || ('pr:' + item.pr_number);
    // Clear stale suggestion when the card state changed
    if (key in _llmSuggestions && _llmSuggestions[key] && _llmSuggestions[key].for_state !== item.state) {
      delete _llmSuggestions[key];
    }
    if (!(key in _llmSuggestions) && !_llmSuggestionFetching.has(key)) {
      toFetch.push(item);
    }
  });
  if (!toFetch.length) return;

  toFetch.forEach(function(item) {
    _llmSuggestionFetching.add(item.issue_number || ('pr:' + item.pr_number));
  });

  var anyNew = false;
  await Promise.all(toFetch.map(async function(item) {
    var key = item.issue_number || ('pr:' + item.pr_number);
    try {
      var prDetails = item.pr_details || {};
      var sovaCtx = item.sova_context || {};
      var body = {
        deterministic_state: item.state,
        deterministic_action_id: item.primary_action.id,
        pr_computed_state: prDetails.computed_state || '',
        has_sova_review: sovaCtx.has_sova_review || false,
        sova_verdict: sovaCtx.verdict || null,
        mergeable: prDetails.mergeable || 'UNKNOWN',
        merge_state: prDetails.merge_state || 'UNKNOWN',
        review_decision: prDetails.review_decision || null,
        ci_passed: prDetails.ci_status === 'passed' || prDetails.ci_status === 'none',
        external_reviews_enabled: true
      };
      var resp = await fetch(apiUrl('/prs/' + item.pr_number + '/suggestion'), {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(body)
      });
      if (resp.ok && resp.status !== 204) {
        var data = await resp.json();
        _llmSuggestions[key] = {
          action_id: data.action_id,
          action_label: data.action_label,
          reasoning: data.reasoning,
          for_state: item.state,
          done: false,
          pr_number: item.pr_number,
          issue_number: item.issue_number || null,
          deterministic_action_id: item.primary_action.id,
          disagrees: data.disagrees
        };
        if (data.disagrees) anyNew = true;
      } else {
        _llmSuggestions[key] = null;
      }
    } catch (e) {
      _llmSuggestions[key] = null;
    }
    _llmSuggestionFetching.delete(key);
  }));

  if (anyNew) {
    renderTaskCards();
  }
}

async function submitLlmFeedback(event, issueKey, choice) {
  event.stopPropagation();
  var sugg = _llmSuggestions[issueKey];
  if (!sugg) return;
  sugg.done = true;
  var safeKey = _sanitizeIssueKey(issueKey);
  var feedbackDiv = document.getElementById('llm-feedback-' + safeKey);
  if (feedbackDiv) {
    feedbackDiv.innerHTML = '<span class="text-xs text-gray-500 italic">Recorded. Thanks!</span>';
  }
  try {
    await fetch(apiUrl('/prs/feedback'), {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        pr_number: sugg.pr_number,
        issue_number: sugg.issue_number,
        deterministic_state: sugg.for_state,
        deterministic_action_id: sugg.deterministic_action_id,
        llm_action_id: sugg.action_id,
        llm_reasoning: sugg.reasoning,
        user_choice: choice
      })
    });
  } catch (e) { /* feedback failure is non-fatal */ }
}

async function submitFlagWrong(event, issueKey) {
  event.stopPropagation();
  var item = _findItemByKey(issueKey);
  if (!item) return;
  if (!item.primary_action) return;
  if (_flaggedWrong.get(issueKey) === item.state) return;
  _flaggedWrong.set(issueKey, item.state);
  var safeKey = _sanitizeIssueKey(issueKey);
  var flagDiv = document.getElementById('flag-wrong-' + safeKey);
  if (flagDiv) {
    flagDiv.innerHTML = '<span class="text-xs text-gray-500 italic">Flagged. Thanks!</span>';
  }

  var sugg = _llmSuggestions[issueKey];
  try {
    // fetch resolves on 4xx/5xx, so a non-ok status is a failure too
    var resp = await fetch(apiUrl('/prs/feedback'), {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        pr_number: item.pr_number,
        issue_number: item.issue_number || null,
        deterministic_state: item.state,
        deterministic_action_id: item.primary_action.id,
        llm_action_id: (sugg && sugg.action_id) || '',
        llm_reasoning: (sugg && sugg.reasoning) || '',
        user_choice: 'wrong'
      })
    });
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
  } catch (e) {
    console.warn('submitFlagWrong: failed to record feedback', e);
    _flaggedWrong.delete(issueKey);
    if (flagDiv) {
      flagDiv.innerHTML = '<span class="text-xs text-red-400 italic">Failed to record. Try again.</span>';
    }
  }
}

// --- End LLM Suggestion Experiment ---

function _closeAllCardMenus() {
  document.querySelectorAll('[id^="card-menu-"]:not([id^="card-menu-toggle-"])').forEach(function(m) { m.classList.add('hidden'); });
  document.querySelectorAll('[id^="card-menu-toggle-"]').forEach(function(t) { t.setAttribute('aria-expanded', 'false'); });
  _cardMenuOpen = false;
}

function toggleCardMenu(event, issue) {
  event.stopPropagation();
  var menu = document.getElementById('card-menu-' + issue);
  var toggle = document.getElementById('card-menu-toggle-' + issue);
  if (!menu) return;
  var wasHidden = menu.classList.contains('hidden');
  _closeAllCardMenus();
  if (wasHidden) {
    menu.classList.remove('hidden');
    if (toggle) toggle.setAttribute('aria-expanded', 'true');
    _cardMenuOpen = true;
  }
}

export function init() {
  registerActions({
    'toggle-card-menu': function(el, e, issue) { toggleCardMenu(e, issue); },
    'expand-tasks': function() { expandTasks(); },
    'collapse-tasks': function() { collapseTasks(); },
    'llm-feedback': function(el, e, issueKey, choice) { submitLlmFeedback(e, issueKey, choice); },
    'flag-wrong': function(el, e, issueKey) { submitFlagWrong(e, issueKey); },
    'set-task-assignee': function(el, e, value) { setTaskAssigneeFilter(value); },
  });

  document.addEventListener('click', function() {
    if (!_cardMenuOpen) return;
    _closeAllCardMenus();
  });

  document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape' && _cardMenuOpen) {
      _closeAllCardMenus();
    }
  });

  document.getElementById('task-state-filter').addEventListener('change', function() {
    setTaskStateFilter(this.value);
  });
  document.getElementById('task-search').addEventListener('input', filterTaskCards);

  _initTaskFilters();
  visibilityAwarePoll(loadWorkItems, 30000);
  visibilityAwarePoll(loadAgentsStatRow, 60000);
}
