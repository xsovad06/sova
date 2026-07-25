/* SOVA Dashboard -- shared JS utilities */

/* ============================================================
   1. CSS VARIABLE COLOR SYSTEM
   ============================================================ */

function getCSSVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

window.SOVA_COLORS = {};

function initColors() {
  window.SOVA_COLORS = {
    accent:  getCSSVar('--ctp-blue'),
    green:   getCSSVar('--ctp-green'),
    red:     getCSSVar('--ctp-red'),
    yellow:  getCSSVar('--ctp-yellow'),
    purple:  getCSSVar('--ctp-mauve'),
    surface: getCSSVar('--ctp-surface0'),
    muted:   getCSSVar('--ctp-overlay1'),
  };
}

/* ============================================================
   2. SHARED UTILITIES
   ============================================================ */

function apiUrl(path) {
  return (window.SOVA_API_PREFIX || '/api') + path;
}

async function fetchAPI(url) {
  var res = await fetch(url);
  if (!res.ok) throw new Error('API error: ' + res.status);
  return res.json();
}

async function getErrorDetail(res, fallback) {
  try { var body = await res.json(); return body.detail || fallback; }
  catch (_e) { return fallback; }
}

function escapeHtml(str) {
  if (!str) return '';
  return str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function escapeJsStr(str) {
  if (!str) return '';
  return str.replace(/\\/g, '\\\\').replace(/'/g, "\\'");
}

function formatDuration(ms) {
  if (!ms || ms <= 0) return '--';
  if (ms < 1000) return ms + 'ms';
  var s = Math.round(ms / 1000);
  if (s < 60) return s + 's';
  return Math.floor(s / 60) + 'm ' + (s % 60) + 's';
}

var _WAITING_COLOR = { dot: 'bg-accent-peach', text: 'text-accent-peach', bg: 'bg-accent-peach/20' };

var STATUS_COLORS = {
  pending:           { dot: 'bg-gray-500',        text: 'text-gray-400',         bg: 'bg-gray-500/20' },
  assessing:         { dot: 'bg-accent-green',    text: 'text-accent-green',     bg: 'bg-accent-green/20' },
  researched:        { dot: 'bg-accent-green',    text: 'text-accent-green',     bg: 'bg-accent-green/20' },
  in_progress:       { dot: 'bg-accent-green',    text: 'text-accent-green',     bg: 'bg-accent-green/20' },
  developing:        { dot: 'bg-accent-green',    text: 'text-accent-green',     bg: 'bg-accent-green/20' },
  simplifying:       { dot: 'bg-accent-green',    text: 'text-accent-green',     bg: 'bg-accent-green/20' },
  reviewing:         { dot: 'bg-accent-green',    text: 'text-accent-green',     bg: 'bg-accent-green/20' },
  committing:        { dot: 'bg-accent-green',    text: 'text-accent-green',     bg: 'bg-accent-green/20' },
  addressing_review: { dot: 'bg-accent-green',    text: 'text-accent-green',     bg: 'bg-accent-green/20' },
  pushing:           { dot: 'bg-accent-green',    text: 'text-accent-green',     bg: 'bg-accent-green/20' },
  pr_created:        { dot: 'bg-accent-green',    text: 'text-accent-green',     bg: 'bg-accent-green/20' },
  ci_monitoring:     _WAITING_COLOR,
  automated_review:  _WAITING_COLOR,
  awaiting_approval: _WAITING_COLOR,
  done:              { dot: 'bg-accent',          text: 'text-accent',           bg: 'bg-accent/20' },
  failed:            { dot: 'bg-accent-red',      text: 'text-accent-red',       bg: 'bg-accent-red/20' },
  rejected:          { dot: 'bg-accent-red',      text: 'text-accent-red',       bg: 'bg-accent-red/20' },
  paused:            { dot: 'bg-accent-purple',   text: 'text-accent-purple',    bg: 'bg-accent-purple/20' },
  interrupted:       { dot: 'bg-accent-red',      text: 'text-accent-red',       bg: 'bg-accent-red/20' },
  running:           { dot: 'bg-accent-green',    text: 'text-accent-green',     bg: 'bg-accent-green/20' },
};

var _STATUS_TERMINAL = { done: 1, failed: 1, rejected: 1, interrupted: 1, paused: 1, awaiting_approval: 1 };
var _STUCK_THRESHOLD_S = 300;

var _STEP_LABELS = {
  sync: 'Syncing',
  assess: 'Assessing',
  create_worktree: 'Creating worktree',
  capture_baseline: 'Capturing baseline',
  develop: 'Developing',
  simplify: 'Simplifying',
  self_review: 'Self-reviewing',
  commit: 'Committing',
  validate: 'Validating',
  push: 'Pushing',
  create_pr: 'Creating PR',
  monitor_ci: 'CI checks',
  extract_memory: 'Extracting memory',
  rebase: 'Rebasing',
  address_review: 'Addressing review',
  resolve_external_reviews: 'Resolving reviews',
  address_external_findings: 'Addressing findings',
  ensure_worktree: 'Ensuring worktree',
  fetch_task: 'Fetching task',
  research: 'Researching',
  spec: 'Writing spec',
  scan_project: 'Scanning project',
  generate_tasks: 'Generating tasks',
  validate_tasks: 'Validating tasks',
  wait_for_external_reviews: 'Reviews',
  handoff_to_reviewer: 'Handoff',
  handoff_to_user: 'Handoff',
};

var _WAITING_STEPS = {
  monitor_ci: 1,
  wait_for_external_reviews: 1,
  handoff_to_reviewer: 1,
  handoff_to_user: 1,
};

function formatElapsed(seconds) {
  if (!seconds) return '0s';
  seconds = Math.floor(seconds);
  if (seconds < 60) return seconds + 's';
  if (seconds < 3600) return Math.floor(seconds / 60) + 'm ' + (seconds % 60) + 's';
  return Math.floor(seconds / 3600) + 'h ' + Math.floor((seconds % 3600) / 60) + 'm';
}

function statusColor(status) {
  return (STATUS_COLORS[status] || STATUS_COLORS.pending).text;
}

function statusDot(status) {
  var c = STATUS_COLORS[status] || STATUS_COLORS.pending;
  var isActive = !_STATUS_TERMINAL[status];
  return c.dot + (isActive ? ' animate-pulse' : '');
}

function _computeBadgeLabel(status, currentStep) {
  if (_STATUS_TERMINAL[status]) {
    var label = status.replace(/_/g, ' ');
    return label.charAt(0).toUpperCase() + label.slice(1);
  }
  if (!currentStep || currentStep === 'agent') {
    return 'Starting...';
  }
  var rawLabel = currentStep.replace(/_/g, ' ');
  var stepLabel = _STEP_LABELS[currentStep] || (rawLabel.charAt(0).toUpperCase() + rawLabel.slice(1));
  var prefix = _WAITING_STEPS[currentStep] ? 'Waiting: ' : 'Running: ';
  return prefix + stepLabel;
}

function _getBadgeColors(status, currentStep) {
  if (_STATUS_TERMINAL[status]) return STATUS_COLORS[status] || STATUS_COLORS.pending;
  if (currentStep && _WAITING_STEPS[currentStep]) {
    return _WAITING_COLOR;
  }
  return STATUS_COLORS[status] || STATUS_COLORS.pending;
}

function renderStatusBadge(status, currentStep, stepIndex, totalSteps, elapsedSeconds, aggregatedLabel) {
  var colors = _getBadgeColors(status, currentStep);
  var isTerminal = !!_STATUS_TERMINAL[status];
  var isStuck = !isTerminal && elapsedSeconds != null && elapsedSeconds > _STUCK_THRESHOLD_S;
  var stuckClass = isStuck ? ' sova-status-stuck' : '';
  var pulseClass = isTerminal ? '' : ' animate-pulse';

  var label = escapeHtml(aggregatedLabel || _computeBadgeLabel(status, currentStep));

  return '<span class="sova-status-badge ' + colors.bg + ' ' + colors.text + stuckClass + '">' +
    '<span class="sova-status-dot ' + colors.dot + pulseClass + '"></span>' +
    '<span>' + label + '</span>' +
  '</span>';
}

function liveElapsed(elapsedSeconds) {
  if (elapsedSeconds == null) return formatElapsed(0);
  var startTs = Date.now() - (elapsedSeconds * 1000);
  return '<span class="sova-status-elapsed" data-start="' + startTs + '">' + formatElapsed(elapsedSeconds) + '</span>';
}

/* ============================================================
   3. BROWSER NOTIFICATION API
   ============================================================ */

var _browserNotifPermission = 'default';

function initBrowserNotifications() {
  if (!('Notification' in window)) return;
  _browserNotifPermission = Notification.permission;

  if (_browserNotifPermission === 'default' && !localStorage.getItem('sova-notif-dismissed')) {
    var banner = document.getElementById('notif-permission-banner');
    if (banner) {
      banner.classList.remove('hidden');
      banner.classList.add('flex');
    }
  }
}

function requestNotificationPermission() {
  if (!('Notification' in window)) return;
  Notification.requestPermission().then(function(result) {
    _browserNotifPermission = result;
    var banner = document.getElementById('notif-permission-banner');
    if (banner) banner.classList.add('hidden');
  });
}

function dismissPermissionBanner() {
  localStorage.setItem('sova-notif-dismissed', '1');
  var banner = document.getElementById('notif-permission-banner');
  if (banner) banner.classList.add('hidden');
}

function sendBrowserNotification(title, body) {
  if (_browserNotifPermission !== 'granted') return;
  if (document.hasFocus()) return;
  try {
    var notif = new Notification(title, {
      body: body,
      icon: '/static/favicon-32x32.png',
      tag: 'sova-' + Date.now(),
    });
    notif.onclick = function() {
      window.focus();
      notif.close();
    };
  } catch (e) { /* ignore */ }
}

/* ============================================================
   4. TOAST NOTIFICATIONS
   ============================================================ */

var _toastContainer = null;
var MAX_TOASTS = 3;

function _ensureToastContainer() {
  if (_toastContainer) return;
  _toastContainer = document.createElement('div');
  _toastContainer.id = 'toast-container';
  document.body.appendChild(_toastContainer);
}

function showToast(message, type, duration) {
  _ensureToastContainer();

  while (_toastContainer.children.length >= MAX_TOASTS) {
    _toastContainer.removeChild(_toastContainer.firstChild);
  }

  var typeClass = type === 'warning' ? 'sova-toast-warning' :
                  type === 'error'   ? 'sova-toast-error' :
                  type === 'success' ? 'sova-toast-success' :
                                       'sova-toast-info';

  var toast = document.createElement('div');
  toast.className = 'sova-toast ' + typeClass;
  toast.innerHTML =
    '<div style="flex:1;min-width:0">' +
      '<p class="text-sm text-gray-200">' + escapeHtml(message) + '</p>' +
      '<p class="text-xs text-gray-500 mt-0.5">' + new Date().toLocaleTimeString() + '</p>' +
    '</div>' +
    '<button onclick="this.parentElement.remove()" class="text-gray-500 hover:text-gray-300 text-sm shrink-0 ml-2" aria-label="Dismiss">&times;</button>';

  _toastContainer.appendChild(toast);

  requestAnimationFrame(function() {
    requestAnimationFrame(function() {
      toast.classList.add('sova-toast-visible');
    });
  });

  setTimeout(function() {
    toast.classList.remove('sova-toast-visible');
    setTimeout(function() {
      if (toast.parentElement) toast.remove();
    }, 300);
  }, duration || 6000);
}

/* ============================================================
   5. CONFIRMATION MODAL
   ============================================================ */

var _sovaModalOpen = false;

/**
 * sovaConfirm(message, options) -> Promise<boolean>
 *
 * options: { title, confirmText, confirmClass, cancelText }
 * confirmClass: 'danger' for destructive (red), default is accent (teal)
 */
function sovaConfirm(message, options) {
  var opts = options || {};
  var title = opts.title || 'Confirm';
  var confirmText = opts.confirmText || 'Confirm';
  var cancelText = opts.cancelText || 'Cancel';
  var isDanger = opts.confirmClass === 'danger';

  if (_sovaModalOpen) {
    return Promise.resolve(false);
  }
  _sovaModalOpen = true;

  return new Promise(function(resolve) {
    var backdrop = document.createElement('div');
    backdrop.className = 'sova-modal-backdrop';

    var confirmBtnClass = isDanger
      ? 'bg-accent-red/20 text-accent-red hover:bg-accent-red/30'
      : 'bg-accent/20 text-accent hover:bg-accent/30';

    backdrop.innerHTML =
      '<div class="sova-modal-dialog">' +
        '<div class="px-5 pt-5 pb-4">' +
          '<p class="text-sm font-medium text-gray-200 mb-2">' + escapeHtml(title) + '</p>' +
          '<p class="text-sm text-gray-400">' + escapeHtml(message) + '</p>' +
        '</div>' +
        '<div class="flex justify-end gap-2 px-5 pb-4">' +
          '<button class="sova-modal-cancel px-3 py-1.5 text-sm text-gray-400 hover:text-gray-200 rounded transition-colors">' +
            escapeHtml(cancelText) +
          '</button>' +
          '<button class="sova-modal-confirm px-3 py-1.5 text-sm font-medium rounded transition-colors ' + confirmBtnClass + '">' +
            escapeHtml(confirmText) +
          '</button>' +
        '</div>' +
      '</div>';

    function close(result) {
      _sovaModalOpen = false;
      if (backdrop.parentElement) backdrop.remove();
      document.removeEventListener('keydown', onKey);
      resolve(result);
    }

    var confirmBtn = backdrop.querySelector('.sova-modal-confirm');

    function onKey(e) {
      if (e.key === 'Escape') close(false);
      if (e.key === 'Enter') { e.preventDefault(); close(true); }
    }

    backdrop.addEventListener('click', function(e) {
      if (e.target === backdrop) close(false);
    });
    backdrop.querySelector('.sova-modal-cancel').addEventListener('click', function() {
      close(false);
    });
    confirmBtn.addEventListener('click', function() {
      close(true);
    });
    document.addEventListener('keydown', onKey);

    document.body.appendChild(backdrop);
    requestAnimationFrame(function() {
      requestAnimationFrame(function() {
        backdrop.classList.add('sova-modal-visible');
      });
    });

    setTimeout(function() {
      if (backdrop.parentNode) confirmBtn.focus();
    }, 0);
  });
}

/* ============================================================
   6. VISIBILITY-AWARE POLLING
   ============================================================ */

function visibilityAwarePoll(fn, intervalMs) {
  fn();
  var state = { id: setInterval(fn, intervalMs) };
  document.addEventListener('visibilitychange', function() {
    if (document.hidden) {
      clearInterval(state.id);
      state.id = null;
    } else {
      fn();
      state.id = setInterval(fn, intervalMs);
    }
  });
  return state;
}

/* ============================================================
   7. SIDEBAR POLLING & NOTIFICATIONS
   ============================================================ */

var _lastHandoffState = null;

function _dotClass(color, animate) {
  return 'absolute -bottom-0.5 -right-0.5 w-3 h-3 rounded-full ' + color + ' border-2 border-sidebar' + (animate ? ' animate-pulse' : '');
}

var _activityPollInterval = null;
var _handoffPollInterval = null;

function startSidebarPolling() {
  _pollActivity();
  _pollHandoff();
  _activityPollInterval = setInterval(_pollActivity, 3000);
  _handoffPollInterval = setInterval(_pollHandoff, 5000);
  _initFeedSSE();
}

document.addEventListener('visibilitychange', function() {
  if (document.hidden) {
    if (_activityPollInterval) { clearInterval(_activityPollInterval); _activityPollInterval = null; }
    if (_handoffPollInterval) { clearInterval(_handoffPollInterval); _handoffPollInterval = null; }
    if (_globalBatchPollInterval) { clearInterval(_globalBatchPollInterval); _globalBatchPollInterval = null; }
  } else {
    _pollActivity();
    _pollHandoff();
    if (!_activityPollInterval) _activityPollInterval = setInterval(_pollActivity, 3000);
    if (!_handoffPollInterval) _handoffPollInterval = setInterval(_pollHandoff, 5000);
    if (_globalBatchId && !_globalBatchPollInterval) {
      _pollGlobalBatch();
      _globalBatchPollInterval = setInterval(_pollGlobalBatch, 2000);
    }
  }
});

async function _pollActivity() {
  try {
    var data = await fetchAPI(apiUrl('/agents/active'));
    var dot = document.getElementById('activity-dot');
    if (!dot) return;

    var running = data.agents && data.agents.length > 0;
    dot.className = running
      ? _dotClass('bg-accent', true)
      : _dotClass('bg-accent-green', false);
  } catch (e) {
    var dot = document.getElementById('activity-dot');
    if (dot) dot.className = _dotClass('bg-gray-500', false);
  }
}

async function _pollHandoff() {
  try {
    var res = await fetch(apiUrl('/handoff'));
    if (!res.ok) return;
    var data = await res.json();
    var banner = document.getElementById('checkpoint-banner');
    if (!banner) return;

    var handoffs = (data.handoffs || []).filter(function(h) {
      return h.status === 'awaiting_action';
    });

    if (handoffs.length > 0) {
      banner.classList.remove('hidden');
      var dot = document.getElementById('activity-dot');
      if (dot) dot.className = _dotClass('bg-accent-yellow', true);
      _lastHandoffState = 'awaiting';
    } else {
      banner.classList.add('hidden');
      if (!data.has_handoff) {
        _lastHandoffState = null;
      }
    }
  } catch (e) { /* ignore */ }
}

/* ============================================================
   7b. ACTIVITY FEED (SSE)
   ============================================================ */

var _feedEvents = [];
var _feedUnread = 0;
var _feedLastId = 0;
var _feedSeenIds = {};
var _feedEventSource = null;
var _feedPanelOpen = false;
var _feedRenderPending = false;
var _FEED_MAX_EVENTS = 500;

function _initFeedSSE() {
  var url = (window.SOVA_PROJECT_SLUG ? '/p/' + window.SOVA_PROJECT_SLUG : '') + '/api/feed/stream';
  _feedEventSource = new EventSource(url);

  _feedEventSource.addEventListener('feed', function(e) {
    try {
      var event = JSON.parse(e.data);
      _feedAddEvent(event);
    } catch (err) { /* ignore parse errors */ }
  });

  _feedEventSource.addEventListener('open', function() {
    if (_feedLastId > 0) {
      _feedGapFill();
    }
  });

  _feedEventSource.addEventListener('error', function() {
    // EventSource auto-reconnects; no action needed
  });
}

function _feedGapFill() {
  var url = apiUrl('/feed/history?since_id=' + _feedLastId);
  fetch(url).then(function(r) { return r.json(); }).then(function(data) {
    if (data.gap_detected) {
      showToast('Activity feed gap detected -- some events were missed', 'warning');
    }
    var events = data.events || [];
    events.forEach(function(event) {
      if (event.id > _feedLastId) {
        _feedAddEvent(event);
      }
    });
  }).catch(function() { /* ignore */ });
}

function _feedAddEvent(event) {
  // Dedup by ID (set handles out-of-order gap-fill events)
  if (_feedSeenIds[event.id]) return;
  _feedSeenIds[event.id] = true;
  if (event.id > _feedLastId) _feedLastId = event.id;
  _feedEvents.push(event);
  if (_feedEvents.length > _FEED_MAX_EVENTS) {
    var removed = _feedEvents.shift();
    if (removed) delete _feedSeenIds[removed.id];
  }

  if (!_feedPanelOpen) {
    _feedUnread++;
    _updateFeedBadge();
  }

  // Toast + browser notification for non-info events
  if (event.severity !== 'info') {
    showToast(event.title, event.severity);
    var browserTitle = event.severity === 'error' ? 'SOVA -- Error' :
                       event.severity === 'warning' ? 'SOVA -- Warning' : 'SOVA';
    sendBrowserNotification(browserTitle, event.title);
  }

  // Coalesce DOM updates with rAF
  if (!_feedRenderPending) {
    _feedRenderPending = true;
    requestAnimationFrame(function() {
      _feedRenderPending = false;
      _renderFeedList();
    });
  }
}

function _updateFeedBadge() {
  var badge = document.getElementById('feed-badge');
  if (!badge) return;
  if (_feedUnread > 0) {
    badge.textContent = _feedUnread > 9 ? '9+' : String(_feedUnread);
    badge.classList.remove('hidden');
    badge.classList.add('flex');
  } else {
    badge.classList.add('hidden');
    badge.classList.remove('flex');
  }
}

function _renderFeedList() {
  var list = document.getElementById('feed-list');
  if (!list) return;
  if (_feedEvents.length === 0) {
    list.innerHTML = '<p class="text-xs text-gray-500 text-center py-6">No activity yet</p>';
    return;
  }
  list.innerHTML = _feedEvents.map(function(e) {
    var borderColor = e.severity === 'error'   ? 'border-l-accent-red' :
                      e.severity === 'warning' ? 'border-l-accent-yellow' :
                      e.severity === 'success' ? 'border-l-accent-green' :
                                                  'border-l-accent';
    var timeStr = (typeof e.timestamp === 'number') ? new Date(e.timestamp * 1000).toLocaleTimeString() : 'Unknown time';
    var categoryBadge = '<span class="text-[10px] px-1.5 py-0.5 rounded bg-surface-hover text-gray-500">' + escapeHtml(e.category) + '</span>';
    var detailHtml = '';
    if (e.detail) {
      detailHtml = '<details class="mt-1"><summary class="text-xs text-gray-500 cursor-pointer hover:text-gray-400">Details</summary>' +
        '<p class="text-xs text-gray-400 mt-1 whitespace-pre-wrap">' + escapeHtml(e.detail) + '</p></details>';
    }
    var metaHtml = '';
    if (e.metadata && e.metadata.cost_usd != null) {
      metaHtml = '<span class="text-xs text-accent-green ml-2">$' + parseFloat(e.metadata.cost_usd || 0).toFixed(2) + '</span>';
    }
    return '<div class="px-4 py-3 border-b border-gray-700/30 last:border-0 border-l-2 ' + borderColor + ' hover:bg-surface-hover/50 transition-colors">' +
      '<div class="flex items-center justify-between gap-2">' +
        '<p class="text-sm text-gray-200 flex-1 min-w-0">' + escapeHtml(e.title) + metaHtml + '</p>' +
        categoryBadge +
      '</div>' +
      detailHtml +
      '<p class="text-xs text-gray-500 mt-1">' + timeStr + '</p>' +
    '</div>';
  }).join('');

  // Auto-scroll to bottom only if user is already near the bottom
  var atBottom = (list.scrollHeight - list.scrollTop - list.clientHeight) < 60;
  if (atBottom) list.scrollTop = list.scrollHeight;
}

function toggleFeedPanel() {
  var panel = document.getElementById('feed-panel');
  var main = document.getElementById('main-content');
  if (!panel) return;

  _feedPanelOpen = !_feedPanelOpen;

  if (_feedPanelOpen) {
    panel.classList.remove('hidden');
    if (main) main.classList.add('feed-panel-push');
    _feedUnread = 0;
    _updateFeedBadge();
    _renderFeedList();
  } else {
    panel.classList.add('hidden');
    if (main) main.classList.remove('feed-panel-push');
  }
}

function clearFeedBadge() {
  _feedUnread = 0;
  _updateFeedBadge();
}

/* ============================================================
   8. PR & ISSUE LINK HELPERS
   ============================================================ */

window.SOVA_GITHUB_REPO = null;
window.SOVA_JIRA_BASE_URL = null;
window.SOVA_JIRA_PROJECT_KEY = null;

function _initGithubRepo() {
  fetchAPI(apiUrl('/settings/config')).then(function(data) {
    var cfg = data.config || {};
    window.SOVA_GITHUB_REPO = cfg.github_repo || null;
    window.SOVA_JIRA_BASE_URL = cfg['task_source.jira_base_url'] || null;
    window.SOVA_JIRA_PROJECT_KEY = cfg['task_source.jira_project_key'] || null;
  }).catch(function() {});
}

function prLink(prNumber) {
  if (!prNumber) return '--';
  var safe = escapeHtml(String(prNumber));
  var repo = window.SOVA_GITHUB_REPO;
  if (repo) {
    return '<a href="https://github.com/' + escapeHtml(repo) + '/pull/' + safe + '" target="_blank" rel="noopener" ' +
      'class="text-accent-lavender hover:underline" onclick="event.stopPropagation()">PR #' + safe + '</a>';
  }
  return 'PR #' + safe;
}

function issueLink(issueNumber, url) {
  if (!issueNumber) return '--';
  var safe = escapeHtml(String(issueNumber));
  var jiraBase = window.SOVA_JIRA_BASE_URL;
  var jiraKey = window.SOVA_JIRA_PROJECT_KEY;

  if (url) {
    if (!/^https?:\/\//i.test(url)) url = '';
    if (!url) return safe;
    return '<a href="' + escapeHtml(url) + '" target="_blank" rel="noopener" ' +
      'class="text-accent hover:underline" onclick="event.stopPropagation()">' + safe + '</a>';
  }
  if (jiraBase && jiraKey) {
    var key = /^\d+$/.test(String(issueNumber)) ? jiraKey + '-' + safe : safe;
    return '<a href="' + escapeHtml(jiraBase) + '/browse/' + escapeHtml(key) + '" target="_blank" rel="noopener" ' +
      'class="text-accent hover:underline" onclick="event.stopPropagation()">' + escapeHtml(key) + '</a>';
  }
  if (!/^\d+$/.test(String(issueNumber))) {
    return safe;
  }
  var repo = window.SOVA_GITHUB_REPO;
  if (repo) {
    return '<a href="https://github.com/' + escapeHtml(repo) + '/issues/' + safe + '" target="_blank" rel="noopener" ' +
      'class="text-accent hover:underline" onclick="event.stopPropagation()">#' + safe + '</a>';
  }
  return '#' + safe;
}

function issueOrPrLink(issueNumber, prNumber, issueUrl) {
  if (issueNumber) return issueLink(issueNumber, issueUrl);
  if (prNumber) return prLink(prNumber);
  return '--';
}

function issueOrPrLabel(issueNumber, prNumber) {
  if (issueNumber) return '<span class="text-accent">#' + escapeHtml(String(issueNumber)) + '</span>';
  if (prNumber) return '<span class="text-accent-lavender">PR #' + escapeHtml(String(prNumber)) + '</span>';
  return '<span class="text-gray-500">--</span>';
}

/* ============================================================
   9. GLOBAL BATCH PROGRESS
   ============================================================ */

var _globalBatchId = null;
var _globalBatchPollInterval = null;

function _batchStorageKey() {
  var slug = window.SOVA_PROJECT_SLUG || '';
  return slug ? 'sova-batch-id-' + slug : 'sova-batch-id';
}

function initGlobalBatch() {
  var stored = sessionStorage.getItem(_batchStorageKey());
  if (stored) {
    _resumeGlobalBatchPolling(stored);
  } else {
    _discoverActiveBatch();
  }
}

async function _discoverActiveBatch() {
  try {
    var data = await fetchAPI(apiUrl('/queue/batch/active'));
    if (data.active && data.batch) {
      _resumeGlobalBatchPolling(data.batch.batch_id);
    }
  } catch (e) { /* ignore */ }
}

function startGlobalBatch(batchId, action, total) {
  _globalBatchId = batchId;
  sessionStorage.setItem(_batchStorageKey(), batchId);
  _showGlobalBatchBar(action, total);
  _globalBatchPollInterval = setInterval(_pollGlobalBatch, 2000);
}

function _resumeGlobalBatchPolling(batchId) {
  _globalBatchId = batchId;
  sessionStorage.setItem(_batchStorageKey(), batchId);
  _showGlobalBatchBar('batch', 0);
  _globalBatchPollInterval = setInterval(_pollGlobalBatch, 2000);
  _pollGlobalBatch();
}

function _showGlobalBatchBar(action, total) {
  var bar = document.getElementById('global-batch-bar');
  var label = document.getElementById('global-batch-label');
  var progressBar = document.getElementById('global-batch-progress-bar');
  var progressText = document.getElementById('global-batch-progress-text');
  if (!bar) return;

  bar.classList.remove('hidden');
  label.textContent = action.charAt(0).toUpperCase() + action.slice(1);
  progressBar.style.width = '0%';
  progressText.textContent = total > 0 ? 'Starting ' + action + '...' : 'Reconnecting...';
}

async function _pollGlobalBatch() {
  if (!_globalBatchId) return;

  try {
    var data = await fetchAPI(apiUrl('/queue/batch/' + _globalBatchId + '/status'));

    if (data.error) {
      _clearGlobalBatch();
      return;
    }

    var label = document.getElementById('global-batch-label');
    var progressBar = document.getElementById('global-batch-progress-bar');
    var progressText = document.getElementById('global-batch-progress-text');
    if (!progressBar) return;

    var pct = data.total > 0 ? Math.round((data.completed / data.total) * 100) : 0;
    progressBar.style.width = pct + '%';
    label.textContent = data.action.charAt(0).toUpperCase() + data.action.slice(1);

    if (data.status === 'running') {
      var running = data.results.filter(function(r) { return r.status === 'running'; });
      var runningIds = running.map(function(r) { return '#' + r.issue_id; }).join(', ');
      var runningText = runningIds ? ' ' + runningIds : '';
      progressText.textContent = data.completed + '/' + data.total + runningText;
    } else {
      if (_globalBatchPollInterval) {
        clearInterval(_globalBatchPollInterval);
        _globalBatchPollInterval = null;
      }

      var failed = data.failed || 0;
      var done = data.completed - failed;
      var actionLabel = data.action.charAt(0).toUpperCase() + data.action.slice(1);

      var summary = actionLabel + ': ' + done + '/' + data.total + ' succeeded';
      if (data.status === 'cancelled') summary += ' (cancelled)';
      progressText.textContent = done + ' done' + (failed > 0 ? ', ' + failed + ' failed' : '');

      progressBar.style.width = '100%';
      if (failed > 0) {
        progressBar.classList.remove('bg-accent');
        progressBar.classList.add('bg-accent-yellow');
      } else {
        progressBar.classList.remove('bg-accent');
        progressBar.classList.add('bg-accent-green');
      }

      showToast(summary, failed > 0 ? 'warning' : 'info');

      setTimeout(function() {
        _clearGlobalBatch();
      }, 3000);
    }
  } catch (e) {
    console.error('Failed to poll global batch:', e);
    _clearGlobalBatch();
  }
}

function cancelGlobalBatch() {
  if (!_globalBatchId) return;
  var id = _globalBatchId;
  _clearGlobalBatch();
  fetch(apiUrl('/queue/batch/' + id + '/cancel'), { method: 'POST' }).catch(function(e) {
    console.error('Failed to cancel batch:', e);
  });
}

function _clearGlobalBatch() {
  if (_globalBatchPollInterval) {
    clearInterval(_globalBatchPollInterval);
    _globalBatchPollInterval = null;
  }
  _globalBatchId = null;
  sessionStorage.removeItem(_batchStorageKey());

  var bar = document.getElementById('global-batch-bar');
  var progressBar = document.getElementById('global-batch-progress-bar');
  if (bar) bar.classList.add('hidden');
  if (progressBar) {
    progressBar.style.width = '0%';
    progressBar.classList.remove('bg-accent-green', 'bg-accent-yellow');
    progressBar.classList.add('bg-accent');
  }

  if (typeof _onGlobalBatchCleared === 'function') _onGlobalBatchCleared();
}

/* ============================================================
   10. COLLAPSIBLE SIDEBAR
   ============================================================ */

function initSidebarCollapse() {
  var btn = document.getElementById('sidebar-toggle');
  if (!btn) return;
  btn.addEventListener('click', toggleSidebar);
  if (document.body.dataset.sidebarCollapsed === 'true') {
    btn.setAttribute('data-tooltip', 'Expand');
  }
}

function toggleSidebar() {
  var isCollapsed = document.body.dataset.sidebarCollapsed === 'true';
  var btn = document.getElementById('sidebar-toggle');
  if (isCollapsed) {
    delete document.body.dataset.sidebarCollapsed;
    document.documentElement.style.removeProperty('--sidebar-width');
    localStorage.removeItem('sova-sidebar-collapsed');
    if (btn) btn.setAttribute('data-tooltip', 'Collapse');
  } else {
    document.body.dataset.sidebarCollapsed = 'true';
    document.documentElement.style.setProperty('--sidebar-width', '64px');
    localStorage.setItem('sova-sidebar-collapsed', '1');
    if (btn) btn.setAttribute('data-tooltip', 'Expand');
  }
}

/* ============================================================
   11. INITIALIZATION
   ============================================================ */

initColors();
_initGithubRepo();
initSidebarCollapse();
if (document.getElementById('activity-dot')) {
  initBrowserNotifications();
  startSidebarPolling();
}
initGlobalBatch();

/* ============================================================
   12. ROLE COLORS
   ============================================================ */

function _roleHex(key) {
  var map = { developer: 'accent', triage: 'yellow', researcher: 'purple', planner: 'purple', reviewer: 'green', auto: 'muted' };
  return (window.SOVA_COLORS && window.SOVA_COLORS[map[key]]) || _ROLE_HEX_FALLBACK[key];
}

var _ROLE_HEX_FALLBACK = {
  developer: '#89b4fa', triage: '#f9e2af', researcher: '#cba6f7', planner: '#cba6f7', reviewer: '#a6e3a1', auto: '#585b70',
};

var ROLE_COLORS = {
  developer:  { bg: 'bg-accent/20',        text: 'text-accent',        dot: 'bg-accent',        border: 'border-accent/40',        get hex() { return _roleHex('developer'); } },
  triage:     { bg: 'bg-accent-yellow/20',  text: 'text-accent-yellow', dot: 'bg-accent-yellow', border: 'border-accent-yellow/40', get hex() { return _roleHex('triage'); } },
  researcher: { bg: 'bg-accent-purple/20',  text: 'text-accent-purple', dot: 'bg-accent-purple', border: 'border-accent-purple/40', get hex() { return _roleHex('researcher'); } },
  planner:    { bg: 'bg-accent-purple/20',  text: 'text-accent-purple', dot: 'bg-accent-purple', border: 'border-accent-purple/40', get hex() { return _roleHex('planner'); } },
  reviewer:   { bg: 'bg-accent-green/20',   text: 'text-accent-green',  dot: 'bg-accent-green',  border: 'border-accent-green/40',  get hex() { return _roleHex('reviewer'); } },
  auto:       { bg: 'bg-gray-500/20',       text: 'text-gray-400',      dot: 'bg-gray-500',      border: 'border-gray-600',         get hex() { return _roleHex('auto'); } },
};

function roleColor(role) {
  if (!role) return ROLE_COLORS.auto;
  var key = role.startsWith('command:') ? _commandToRole(role) : role;
  return ROLE_COLORS[key] || ROLE_COLORS.auto;
}

function formatRole(role, pipelineVariant) {
  if (!role) return 'auto';
  var base;
  if (!role.startsWith('command:')) {
    base = role;
  } else {
    var cmd = role.slice('command:'.length).trim();
    base = cmd.split(/\s+/)[0].replace(/^\//, '');
  }
  if (pipelineVariant === 'address_review' && role === 'developer') {
    return base + ' - address pr';
  }
  return base;
}

function _commandToRole(role) {
  var cmd = role.slice('command:'.length).trim().replace(/^\//, '');
  if (cmd.startsWith('address-pr')) return 'developer';
  if (cmd.startsWith('review')) return 'reviewer';
  if (cmd.startsWith('develop')) return 'developer';
  return 'auto';
}

/* ============================================================
   13. STEP PIPELINE BAR
   ============================================================ */

var PIPELINE_STEPS = [
  'sync', 'assess', 'create_worktree', 'develop', 'simplify',
  'self_review', 'commit', 'validate', 'push', 'create_pr',
  'wait_for_external_reviews', 'address_external_findings',
  'monitor_ci', 'extract_memory', 'handoff_to_reviewer'
];

var STEP_LABELS = {
  sync: 'Sync', assess: 'Assess', create_worktree: 'Worktree',
  develop: 'Develop', simplify: 'Simplify', self_review: 'Review',
  commit: 'Commit', validate: 'Validate', push: 'Push', create_pr: 'PR',
  wait_for_external_reviews: 'Ext Reviews', address_external_findings: 'Address Ext',
  monitor_ci: 'CI', extract_memory: 'Memory', handoff_to_reviewer: 'Handoff',
  rebase: 'Rebase', address_review: 'Address', resolve_external_reviews: 'Resolve',
  handoff_to_user: 'Handoff', fetch_task: 'Fetch', research: 'Research', spec: 'Spec'
};

var STEP_STATUS_COLORS = {
  done:        { bg: 'var(--ctp-green)',    text: 'text-accent-green' },
  passed:      { bg: 'var(--ctp-green)',    text: 'text-accent-green' },
  failed:      { bg: 'var(--ctp-red)',      text: 'text-accent-red' },
  error:       { bg: 'var(--ctp-red)',      text: 'text-accent-red' },
  gate_failed: { bg: 'var(--ctp-yellow)',   text: 'text-accent-yellow' },
  running:     { bg: 'var(--ctp-blue)',     text: 'text-accent' },
  in_progress: { bg: 'var(--ctp-blue)',     text: 'text-accent' },
  skipped:     { bg: 'var(--ctp-surface1)', text: 'text-gray-500' }
};

function renderStepPipeline(currentStep, role, compact, pipelineVariant, opts) {
  var colors = roleColor(role);
  var interactive = opts && opts.interactive;
  var stepData = (opts && opts.stepData) || [];

  if (pipelineVariant === 'command') {
    var cmdName = (role || '').replace(/^command:/, '').replace(/^\//, '');
    var label = '';
    if (!compact) {
      label = '<div class="text-xs text-gray-400 mt-1">' + escapeHtml(cmdName || 'Running') + '</div>';
    }
    return '<div class="flex gap-0.5"><div class="flex-1 ' + (compact ? 'h-1.5' : 'h-2.5') + ' rounded animate-pulse" style="background:' + colors.hex + ';opacity:0.7"></div></div>' + label;
  }

  if (currentStep === 'agent') {
    var agentLabel = '';
    if (!compact) {
      agentLabel = '<div class="text-xs text-gray-400 mt-1">Running...</div>';
    }
    return '<div class="flex gap-0.5"><div class="flex-1 ' + (compact ? 'h-1.5' : 'h-2.5') + ' rounded animate-pulse" style="background:' + colors.hex + ';opacity:0.5"></div></div>' + agentLabel;
  }

  var steps = PIPELINE_STEPS;
  var labels = STEP_LABELS;
  if (pipelineVariant === 'address_review') {
    steps = ['rebase', 'address_review', 'commit', 'validate', 'push', 'monitor_ci', 'resolve_external_reviews', 'extract_memory', 'handoff_to_user'];
    labels = {rebase: 'Rebase', address_review: 'Address', commit: 'Commit', validate: 'Validate', push: 'Push', monitor_ci: 'CI', resolve_external_reviews: 'Resolve', extract_memory: 'Memory', handoff_to_user: 'Handoff'};
  } else if (pipelineVariant === 'researcher') {
    steps = ['fetch_task', 'research', 'spec', 'extract_memory'];
    labels = {fetch_task: 'Fetch', research: 'Research', spec: 'Spec', extract_memory: 'Memory'};
  }

  // Build lookup from step execution data
  var stepMap = {};
  stepData.forEach(function(s) { stepMap[s.step_name] = s; });

  var idx = currentStep ? steps.indexOf(currentStep) : -1;
  var segments = steps.map(function(step, i) {
    var h = compact ? 'h-1.5' : (interactive ? 'h-5' : 'h-2.5');
    var w = 'flex-1 ' + h;
    var rounded = '';
    if (i === 0) rounded = ' rounded-l';
    if (i === steps.length - 1) rounded = ' rounded-r';

    var stepLabel = labels[step] || step;
    var exec = stepMap[step];
    var bg, cls = '', tooltip = stepLabel;

    if (interactive && exec) {
      var sc = STEP_STATUS_COLORS[exec.status];
      bg = sc ? sc.bg : 'var(--ctp-surface1)';
      if (exec.status === 'running' || exec.status === 'in_progress') {
        cls = ' sova-step-pulse';
      }
      tooltip = stepLabel + ' (' + exec.status + ')';
    } else if (interactive && !exec && idx === i) {
      bg = colors.hex;
      cls = ' sova-step-pulse';
      tooltip = stepLabel + ' (current)';
    } else if (interactive && !exec) {
      bg = null;
      tooltip = stepLabel + ' (pending)';
    } else if (idx >= 0 && i < idx) {
      bg = colors.hex;
    } else if (i === idx) {
      bg = colors.hex;
      cls = compact ? ' animate-pulse' : ' sova-step-pulse';
    } else {
      bg = null;
    }

    var style = bg ? 'background:' + bg : '';
    var bgClass = bg ? '' : ' bg-gray-700';
    var interClass = interactive ? ' sova-step-seg' : '';

    var labelHtml = '';
    if (interactive && !compact) {
      labelHtml = '<span class="sova-step-label">' + escapeHtml(stepLabel) + '</span>';
    }

    var dataAttr = interactive ? ' data-step="' + step + '"' : '';

    return '<div class="' + w + rounded + cls + interClass + bgClass +
      '" style="' + style + '" data-tooltip="' + escapeHtml(tooltip) + '"' + dataAttr +
      '>' + labelHtml + '</div>';
  });

  var stepLabelHtml = '';
  if (!compact && !interactive && idx >= 0) {
    stepLabelHtml = '<div class="text-xs text-gray-400 mt-1">' + escapeHtml(labels[currentStep] || currentStep) + ' (' + (idx + 1) + '/' + steps.length + ')</div>';
  } else if (!compact && !interactive) {
    stepLabelHtml = '<div class="text-xs text-gray-500 mt-1">Initializing...</div>';
  }

  var barHtml = '<div class="flex gap-0.5' + (interactive ? ' items-stretch' : '') + '">' + segments.join('') + '</div>' + stepLabelHtml;

  return barHtml;
}

function initInteractivePipeline(container) {
  if (container._sovaPipelineHandler) {
    container.removeEventListener('click', container._sovaPipelineHandler);
  }

  container._sovaPipelineHandler = function(e) {
    var seg = e.target.closest('.sova-step-seg');
    if (!seg) return;
    var step = seg.getAttribute('data-step');
    if (!step) return;

    var segs = container.querySelectorAll('.sova-step-seg');
    segs.forEach(function(s) { s.classList.remove('sova-step-active'); });
    seg.classList.add('sova-step-active');

    if (typeof scrollToStep === 'function') {
      scrollToStep(step);
    }
  };

  container.addEventListener('click', container._sovaPipelineHandler);
}

/* ============================================================
   14. RUNS TABLE (shared)
   ============================================================ */

function formatBytes(bytes, compact) {
  if (bytes == null) return compact ? '--' : 'N/A';
  if (!compact && bytes < 1024) return bytes + ' B';
  if (bytes < 1048576) return (bytes / 1024).toFixed(compact ? 0 : 1) + (compact ? 'K' : ' KB');
  if (bytes < 1073741824) return (bytes / 1048576).toFixed(compact ? 0 : 1) + (compact ? 'M' : ' MB');
  return (bytes / 1073741824).toFixed(compact ? 1 : 2) + (compact ? 'G' : ' GB');
}

function renderRunsTable(runs, targetId) {
  var el = document.getElementById(targetId);
  if (runs.length === 0) {
    el.innerHTML = '<p class="text-gray-500 text-sm p-4">No runs recorded</p>';
    return;
  }
  var hasResources = runs.some(function(r) { return r.peak_cpu_percent != null; });
  el.innerHTML = '<table class="w-full text-sm">' +
    '<thead><tr class="text-gray-500 text-xs uppercase">' +
    '<th class="text-left p-3">Issue / PR</th>' +
    '<th class="text-left p-3">Role</th>' +
    '<th class="text-left p-3">Status</th>' +
    '<th class="text-left p-3">Step</th>' +
    '<th class="text-right p-3">Cost</th>' +
    (hasResources ? '<th class="text-right p-3">Peak CPU</th><th class="text-right p-3">Peak Mem</th>' : '') +
    '<th class="text-left p-3">Started</th>' +
    '</tr></thead>' +
    '<tbody>' + runs.map(function(r) {
      var prefix = window.SOVA_PROJECT_SLUG ? '/p/' + window.SOVA_PROJECT_SLUG : '';
      return '<tr class="border-t border-gray-700/30 hover:bg-surface-hover cursor-pointer" onclick="window.location=\'' + prefix + '/runs/' + r.id + '\'">' +
        '<td class="p-3">' + issueOrPrLink(r.issue_number, r.pr_number) + '</td>' +
        '<td class="p-3 text-gray-300">' + escapeHtml(r.role) + '</td>' +
        '<td class="p-3"><span class="flex items-center gap-1.5"><span class="w-2 h-2 rounded-full ' + statusDot(r.status) + '"></span><span class="' + statusColor(r.status) + '">' + escapeHtml(r.status) + '</span></span></td>' +
        '<td class="p-3 text-gray-400">' + escapeHtml(r.current_step || '--') + '</td>' +
        '<td class="p-3 text-right text-accent-green">$' + (parseFloat(r.total_cost_usd) || 0).toFixed(4) + '</td>' +
        (hasResources ? '<td class="p-3 text-right text-accent">' + (r.peak_cpu_percent != null ? parseFloat(r.peak_cpu_percent).toFixed(1) + '%' : '--') + '</td>' +
        '<td class="p-3 text-right text-accent-green">' + formatBytes(r.peak_memory_rss_bytes, true) + '</td>' : '') +
        '<td class="p-3 text-gray-500 text-xs">' + (r.started_at ? new Date(r.started_at).toLocaleString() : '--') + '</td>' +
        '</tr>';
    }).join('') + '</tbody></table>';
}
