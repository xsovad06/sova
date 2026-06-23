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

var STATUS_COLORS = {
  pending:           { dot: 'bg-gray-500',        text: 'text-gray-400',         bg: 'bg-gray-500/20' },
  assessing:         { dot: 'bg-accent',          text: 'text-accent',           bg: 'bg-accent/20' },
  researched:        { dot: 'bg-accent',          text: 'text-accent',           bg: 'bg-accent/20' },
  in_progress:       { dot: 'bg-accent-yellow',   text: 'text-accent-yellow',    bg: 'bg-accent-yellow/20' },
  developing:        { dot: 'bg-accent-yellow',   text: 'text-accent-yellow',    bg: 'bg-accent-yellow/20' },
  simplifying:       { dot: 'bg-accent-yellow',   text: 'text-accent-yellow',    bg: 'bg-accent-yellow/20' },
  reviewing:         { dot: 'bg-accent-yellow',   text: 'text-accent-yellow',    bg: 'bg-accent-yellow/20' },
  committing:        { dot: 'bg-accent-yellow',   text: 'text-accent-yellow',    bg: 'bg-accent-yellow/20' },
  addressing_review: { dot: 'bg-accent-yellow',   text: 'text-accent-yellow',    bg: 'bg-accent-yellow/20' },
  pushing:           { dot: 'bg-accent',          text: 'text-accent',           bg: 'bg-accent/20' },
  pr_created:        { dot: 'bg-accent',          text: 'text-accent',           bg: 'bg-accent/20' },
  ci_monitoring:     { dot: 'bg-accent-yellow',   text: 'text-accent-yellow',    bg: 'bg-accent-yellow/20' },
  automated_review:  { dot: 'bg-accent-yellow',   text: 'text-accent-yellow',    bg: 'bg-accent-yellow/20' },
  done:              { dot: 'bg-accent-green',    text: 'text-accent-green',     bg: 'bg-accent-green/20' },
  failed:            { dot: 'bg-accent-red',      text: 'text-accent-red',       bg: 'bg-accent-red/20' },
  rejected:          { dot: 'bg-accent-red',      text: 'text-accent-red',       bg: 'bg-accent-red/20' },
  paused:            { dot: 'bg-accent-purple',   text: 'text-accent-purple',    bg: 'bg-accent-purple/20' },
  interrupted:       { dot: 'bg-accent-red',      text: 'text-accent-red',       bg: 'bg-accent-red/20' },
  running:           { dot: 'bg-accent-yellow',   text: 'text-accent-yellow',    bg: 'bg-accent-yellow/20' },
};

var _STATUS_TERMINAL = { done: 1, failed: 1, rejected: 1, interrupted: 1, paused: 1 };
var _STUCK_THRESHOLD_S = 300;

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

function renderStatusBadge(status, currentStep, stepIndex, totalSteps, elapsedSeconds, aggregatedLabel) {
  var colors = STATUS_COLORS[status] || STATUS_COLORS.pending;
  var isTerminal = !!_STATUS_TERMINAL[status];
  var isStuck = !isTerminal && elapsedSeconds != null && elapsedSeconds > _STUCK_THRESHOLD_S;
  var stuckClass = isStuck ? ' sova-status-stuck' : '';
  var pulseClass = isTerminal ? '' : ' animate-pulse';

  var label = escapeHtml(aggregatedLabel || status);
  if (currentStep && totalSteps) {
    var idx = parseInt(stepIndex, 10);
    label += ' (' + escapeHtml(currentStep) + ', ' + (isNaN(idx) ? 0 : idx) + '/' + parseInt(totalSteps, 10) + ')';
  } else if (currentStep) {
    label += ' (' + escapeHtml(currentStep) + ')';
  }

  var elapsedHtml = '';
  if (!isTerminal && elapsedSeconds != null) {
    var startTs = Date.now() - (elapsedSeconds * 1000);
    elapsedHtml = ' <span class="sova-status-elapsed" data-start="' + startTs + '">' + formatElapsed(elapsedSeconds) + '</span>';
  }

  return '<span class="sova-status-badge ' + colors.bg + ' ' + colors.text + stuckClass + '">' +
    '<span class="sova-status-dot ' + colors.dot + pulseClass + '"></span>' +
    '<span>' + label + '</span>' +
    elapsedHtml +
  '</span>';
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
      backdrop.classList.remove('sova-modal-visible');
      setTimeout(function() {
        if (backdrop.parentElement) backdrop.remove();
      }, 150);
      document.removeEventListener('keydown', onKey);
      resolve(result);
    }

    function onKey(e) {
      if (e.key === 'Escape') close(false);
    }

    backdrop.addEventListener('click', function(e) {
      if (e.target === backdrop) close(false);
    });
    backdrop.querySelector('.sova-modal-cancel').addEventListener('click', function() {
      close(false);
    });
    backdrop.querySelector('.sova-modal-confirm').addEventListener('click', function() {
      close(true);
    });
    document.addEventListener('keydown', onKey);

    document.body.appendChild(backdrop);
    requestAnimationFrame(function() {
      requestAnimationFrame(function() {
        backdrop.classList.add('sova-modal-visible');
      });
    });

    backdrop.querySelector('.sova-modal-confirm').focus();
  });
}

/* ============================================================
   6. SIDEBAR POLLING & NOTIFICATIONS
   ============================================================ */

var _notifItems = [];
var _lastHandoffState = null;
var _notifiedHandoffIds = {};
var _notifiedRunIds = {};
var _dismissedTimestamps = {};
var _DISMISSED_STORAGE_KEY = 'sova_dismissed_runs';
var _NOTIFIED_HANDOFFS_KEY = 'sova_notified_handoffs';
var _DISMISSED_TTL_MS = 120000;

function _loadDismissedRuns() {
  try {
    var raw = localStorage.getItem(_DISMISSED_STORAGE_KEY);
    if (!raw) return;
    var map = JSON.parse(raw);
    var now = Date.now();
    Object.keys(map).forEach(function(k) {
      if (now - map[k] < _DISMISSED_TTL_MS) {
        _notifiedRunIds[k] = true;
        _dismissedTimestamps[k] = map[k];
      }
    });
  } catch (e) { /* ignore corrupt data */ }
}

function _saveDismissedRuns() {
  try {
    var now = Date.now();
    Object.keys(_notifiedRunIds).forEach(function(k) {
      if (!_dismissedTimestamps[k]) _dismissedTimestamps[k] = now;
    });
    localStorage.setItem(_DISMISSED_STORAGE_KEY, JSON.stringify(_dismissedTimestamps));
  } catch (e) { /* ignore */ }
}

function _dotClass(color, animate) {
  return 'absolute -bottom-0.5 -right-0.5 w-3 h-3 rounded-full ' + color + ' border-2 border-sidebar' + (animate ? ' animate-pulse' : '');
}

function startSidebarPolling() {
  _loadDismissedRuns();
  _loadNotifiedHandoffs();
  _pollActivity();
  _pollHandoff();
  setInterval(_pollActivity, 3000);
  setInterval(_pollHandoff, 5000);
}

async function _pollActivity() {
  try {
    var data = await fetchAPI(apiUrl('/agents/active'));
    var dot = document.getElementById('activity-dot');
    if (!dot) return;

    var running = data.agents && data.agents.length > 0;
    dot.className = running
      ? _dotClass('bg-accent', true)
      : _dotClass('bg-accent-green', false);

    var completed = data.completed || [];
    var hadNew = false;
    completed.forEach(function(agent) {
      if (_notifiedRunIds[agent.run_id]) return;
      _notifiedRunIds[agent.run_id] = true;
      hadNew = true;

      var label = agent.issue ? '#' + agent.issue : 'Agent';
      var role = agent.role ? ' ' + agent.role.charAt(0).toUpperCase() + agent.role.slice(1) : '';
      var pr = agent.pr_number ? ' (PR #' + agent.pr_number + ')' : '';

      if (agent.status === 'failed' || agent.status === 'paused') {
        var hint = agent.status === 'paused' ? 'needs attention' : 'check logs';
        _addNotification(label + role + ' ' + agent.status + pr + ' -- ' + hint, 'warning');
      } else {
        var cost = agent.cost_usd ? ' $' + agent.cost_usd.toFixed(2) : '';
        _addNotification(label + role + ' completed' + pr + cost, 'info');
      }
    });
    if (hadNew) _saveDismissedRuns();
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

      var hadNew = false;
      handoffs.forEach(function(h) {
        var hid = h.id || ('issue-' + (h.issue || 'unknown'));
        if (_notifiedHandoffIds[hid]) return;
        _notifiedHandoffIds[hid] = true;
        hadNew = true;
        var label = h.issue ? '#' + h.issue : 'Agent';
        _addNotification(label + ': action required', 'warning');
      });
      if (hadNew) _saveNotifiedHandoffs();
    } else {
      banner.classList.add('hidden');
      if (!data.has_handoff) {
        _lastHandoffState = null;
        _notifiedHandoffIds = {};
        _saveNotifiedHandoffs();
      }
    }
  } catch (e) { /* ignore */ }
}

function _loadNotifiedHandoffs() {
  try {
    var raw = localStorage.getItem(_NOTIFIED_HANDOFFS_KEY);
    if (!raw) return;
    _notifiedHandoffIds = JSON.parse(raw);
  } catch (e) { /* ignore */ }
}

function _saveNotifiedHandoffs() {
  try {
    localStorage.setItem(_NOTIFIED_HANDOFFS_KEY, JSON.stringify(_notifiedHandoffIds));
  } catch (e) { /* ignore */ }
}

function _addNotification(message, type, details) {
  var item = { message: message, type: type, time: new Date() };
  if (details && details.length > 0) item.details = details;
  _notifItems.unshift(item);
  if (_notifItems.length > 20) _notifItems.pop();
  _updateNotifBadge();
  _renderNotifList();
  showToast(message, type);
  var browserTitle = type === 'warning' ? 'SOVA -- Action Required' : 'SOVA';
  var browserBody = message;
  if (details && details.length > 0) {
    browserBody += '\n' + details.map(function(d) { return d.text; }).join('\n');
  }
  sendBrowserNotification(browserTitle, browserBody);
}

function _updateNotifBadge() {
  var badge = document.getElementById('notif-badge');
  if (!badge) return;
  var count = _notifItems.length;
  if (count > 0) {
    badge.textContent = count > 9 ? '9+' : String(count);
    badge.classList.remove('hidden');
    badge.classList.add('flex');
  } else {
    badge.classList.add('hidden');
    badge.classList.remove('flex');
  }
}

function _renderNotifList() {
  var list = document.getElementById('notif-list');
  if (!list) return;
  if (_notifItems.length === 0) {
    list.innerHTML = '<p class="text-xs text-gray-500 text-center py-6">No notifications</p>';
    return;
  }
  list.innerHTML = _notifItems.map(function(n) {
    var borderColor = n.type === 'warning' ? 'border-l-accent-yellow' :
                      n.type === 'error'   ? 'border-l-accent-red' :
                                              'border-l-accent';
    var timeStr = n.time.toLocaleTimeString();
    var detailsHtml = '';
    if (n.details && n.details.length > 0) {
      detailsHtml = '<div class="mt-1.5 space-y-0.5">' +
        n.details.map(function(d) {
          var color = d.failed ? 'text-accent-red' : 'text-gray-400';
          return '<p class="text-xs ' + color + '">' + escapeHtml(d.text) + '</p>';
        }).join('') +
      '</div>';
    }
    return '<div class="px-4 py-3 border-b border-gray-700/30 last:border-0 border-l-2 ' + borderColor + ' hover:bg-surface-hover/50 transition-colors">' +
      '<p class="text-sm text-gray-200">' + escapeHtml(n.message) + '</p>' +
      detailsHtml +
      '<p class="text-xs text-gray-500 mt-1">' + timeStr + '</p>' +
    '</div>';
  }).join('');
}

function toggleNotifPanel() {
  var panel = document.getElementById('notif-panel');
  if (panel) panel.classList.toggle('hidden');
}

function clearNotifBadge() {
  _notifItems = [];
  _saveDismissedRuns();
  _updateNotifBadge();
  _renderNotifList();
}

// Click-outside to close notification panel
document.addEventListener('click', function(e) {
  var panel = document.getElementById('notif-panel');
  var bell = document.getElementById('notif-bell');
  if (panel && !panel.classList.contains('hidden') && !panel.contains(e.target) && bell && !bell.contains(e.target)) {
    panel.classList.add('hidden');
  }
});

/* ============================================================
   7. PR & ISSUE LINK HELPERS
   ============================================================ */

window.SOVA_GITHUB_REPO = null;

function _initGithubRepo() {
  fetchAPI(apiUrl('/settings/config')).then(function(data) {
    window.SOVA_GITHUB_REPO = (data.config && data.config.github_repo) || null;
  }).catch(function() {});
}

function prLink(prNumber) {
  if (!prNumber) return '--';
  var safe = escapeHtml(String(prNumber));
  var repo = window.SOVA_GITHUB_REPO;
  if (repo) {
    return '<a href="https://github.com/' + escapeHtml(repo) + '/pull/' + safe + '" target="_blank" rel="noopener" ' +
      'class="text-accent hover:underline" onclick="event.stopPropagation()">#' + safe + '</a>';
  }
  return '#' + safe;
}

function issueLink(issueNumber) {
  if (!issueNumber) return '--';
  var safe = escapeHtml(String(issueNumber));
  if (!/^\d+$/.test(String(issueNumber))) {
    return '#' + safe;
  }
  var repo = window.SOVA_GITHUB_REPO;
  if (repo) {
    return '<a href="https://github.com/' + escapeHtml(repo) + '/issues/' + safe + '" target="_blank" rel="noopener" ' +
      'class="text-accent hover:underline" onclick="event.stopPropagation()">#' + safe + '</a>';
  }
  return '#' + safe;
}

/* ============================================================
   8. GLOBAL BATCH PROGRESS
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

      var details = data.results
        .filter(function(r) { return r.status !== 'pending' && r.status !== 'running'; })
        .map(function(r) {
          var prefix = '#' + r.issue_id;
          if (r.status === 'failed') {
            return { text: prefix + ' failed' + (r.detail ? ' -- ' + r.detail : ''), failed: true };
          } else if (r.status === 'skipped') {
            return { text: prefix + ' skipped' + (r.detail ? ' -- ' + r.detail : ''), failed: false };
          } else {
            return { text: prefix + (r.detail ? ' -- ' + r.detail : ' done'), failed: false };
          }
        });

      _addNotification(summary, failed > 0 ? 'warning' : 'info', details);

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
   9. COLLAPSIBLE SIDEBAR
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
   10. INITIALIZATION
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
   11. ROLE COLORS
   ============================================================ */

function _roleHex(key) {
  var map = { developer: 'accent', triage: 'yellow', researcher: 'purple', reviewer: 'green', auto: 'muted' };
  return (window.SOVA_COLORS && window.SOVA_COLORS[map[key]]) || _ROLE_HEX_FALLBACK[key];
}

var _ROLE_HEX_FALLBACK = {
  developer: '#89b4fa', triage: '#f9e2af', researcher: '#cba6f7', reviewer: '#a6e3a1', auto: '#585b70',
};

var ROLE_COLORS = {
  developer:  { bg: 'bg-accent/20',        text: 'text-accent',        dot: 'bg-accent',        border: 'border-accent/40',        get hex() { return _roleHex('developer'); } },
  triage:     { bg: 'bg-accent-yellow/20',  text: 'text-accent-yellow', dot: 'bg-accent-yellow', border: 'border-accent-yellow/40', get hex() { return _roleHex('triage'); } },
  researcher: { bg: 'bg-accent-purple/20',  text: 'text-accent-purple', dot: 'bg-accent-purple', border: 'border-accent-purple/40', get hex() { return _roleHex('researcher'); } },
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
   12. STEP PIPELINE BAR
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
    var h = compact ? 'h-1.5' : (interactive ? 'h-3' : 'h-2.5');
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
      '" style="' + style + '" title="' + escapeHtml(tooltip) + '"' + dataAttr +
      '>' + labelHtml + '</div>';
  });

  var stepLabelHtml = '';
  if (!compact && !interactive && idx >= 0) {
    stepLabelHtml = '<div class="text-xs text-gray-400 mt-1">' + escapeHtml(labels[currentStep] || currentStep) + ' (' + (idx + 1) + '/' + steps.length + ')</div>';
  } else if (!compact && !interactive) {
    stepLabelHtml = '<div class="text-xs text-gray-500 mt-1">Initializing...</div>';
  }

  var barHtml = '<div class="flex gap-0.5' + (interactive ? ' items-stretch' : '') + '">' + segments.join('') + '</div>' + stepLabelHtml;

  if (interactive) {
    barHtml += '<div class="sova-step-detail" id="step-detail-panel"></div>';
  }

  return barHtml;
}

function _stepStatusColor(status) {
  var sc = STEP_STATUS_COLORS[status];
  return sc ? sc.text : 'text-gray-500';
}

function _renderStepDetailPanel(stepName, stepMap) {
  var exec = stepMap ? stepMap[stepName] || null : null;
  var label = STEP_LABELS[stepName] || stepName;

  if (!exec) {
    return '<div class="bg-surface-hover rounded p-3 text-sm">' +
      '<span class="text-gray-400">' + escapeHtml(label) + '</span>' +
      '<span class="text-xs text-gray-600 ml-2">pending</span>' +
    '</div>';
  }

  var statusCls = _stepStatusColor(exec.status);
  var rows = [];
  rows.push('<div class="flex items-center gap-3 mb-2">' +
    '<span class="font-medium text-sm text-gray-200">' + escapeHtml(label) + '</span>' +
    '<span class="text-xs font-medium ' + statusCls + '">' + escapeHtml(exec.status) + '</span>' +
  '</div>');

  var details = [];
  if (exec.duration_formatted || exec.duration_ms) {
    details.push('<span class="text-gray-500">Duration:</span> <span class="text-gray-300">' +
      escapeHtml(exec.duration_formatted || formatDuration(exec.duration_ms)) + '</span>');
  }
  if (exec.cost_usd != null) {
    details.push('<span class="text-gray-500">Cost:</span> <span class="text-accent-green">$' +
      exec.cost_usd.toFixed(4) + '</span>');
  }
  if (exec.retry_count > 0) {
    details.push('<span class="text-gray-500">Retries:</span> <span class="text-accent-yellow">' +
      exec.retry_count + '</span>');
  }
  if (details.length > 0) {
    rows.push('<div class="flex flex-wrap gap-x-4 gap-y-1 text-xs">' + details.join('') + '</div>');
  }

  if (exec.error_message) {
    rows.push('<div class="mt-2 text-xs text-accent-red bg-accent-red/5 rounded p-2 max-h-24 overflow-y-auto">' +
      escapeHtml(exec.error_message) + '</div>');
  }
  if (exec.gate_check_result) {
    var gateText = typeof exec.gate_check_result === 'string' ? exec.gate_check_result : JSON.stringify(exec.gate_check_result);
    rows.push('<div class="mt-2 text-xs text-accent-yellow bg-accent-yellow/5 rounded p-2 max-h-24 overflow-y-auto">' +
      '<span class="text-gray-500">Gate:</span> ' + escapeHtml(gateText) + '</div>');
  }
  if (exec.output_summary) {
    rows.push('<div class="mt-2 text-xs text-gray-400 bg-surface-hover rounded p-2 max-h-24 overflow-y-auto whitespace-pre-wrap">' +
      escapeHtml(exec.output_summary) + '</div>');
  }

  return '<div class="bg-surface-hover rounded-lg p-3 border border-gray-700/50">' + rows.join('') + '</div>';
}

function initInteractivePipeline(container, stepData) {
  var _openStep = null;
  var panel = container.querySelector('#step-detail-panel');
  if (!panel) return;

  var stepMap = {};
  stepData.forEach(function(s) { stepMap[s.step_name] = s; });

  var segs = container.querySelectorAll('.sova-step-seg');
  segs.forEach(function(seg) {
    seg.addEventListener('click', function() {
      var step = seg.getAttribute('data-step');
      if (!step) return;

      if (_openStep === step) {
        panel.classList.remove('sova-step-detail-open');
        panel.innerHTML = '';
        _openStep = null;
        segs.forEach(function(s) { s.classList.remove('sova-step-active'); });
        return;
      }

      _openStep = step;
      panel.innerHTML = _renderStepDetailPanel(step, stepMap);
      panel.classList.add('sova-step-detail-open');
      segs.forEach(function(s) { s.classList.remove('sova-step-active'); });
      seg.classList.add('sova-step-active');
    });
  });
}

/* ============================================================
   13. RUNS TABLE (shared)
   ============================================================ */

function renderRunsTable(runs, targetId) {
  var el = document.getElementById(targetId);
  if (runs.length === 0) {
    el.innerHTML = '<p class="text-gray-500 text-sm p-4">No runs recorded</p>';
    return;
  }
  el.innerHTML = '<table class="w-full text-sm">' +
    '<thead><tr class="text-gray-500 text-xs uppercase">' +
    '<th class="text-left p-3">Issue</th>' +
    '<th class="text-left p-3">Role</th>' +
    '<th class="text-left p-3">Status</th>' +
    '<th class="text-left p-3">Step</th>' +
    '<th class="text-right p-3">Cost</th>' +
    '<th class="text-left p-3">Started</th>' +
    '</tr></thead>' +
    '<tbody>' + runs.map(function(r) {
      var prefix = window.SOVA_PROJECT_SLUG ? '/p/' + window.SOVA_PROJECT_SLUG : '';
      return '<tr class="border-t border-gray-700/30 hover:bg-surface-hover cursor-pointer" onclick="window.location=\'' + prefix + '/runs/' + r.id + '\'">' +
        '<td class="p-3">' + issueLink(r.issue_number) + '</td>' +
        '<td class="p-3 text-gray-300">' + escapeHtml(r.role) + '</td>' +
        '<td class="p-3"><span class="flex items-center gap-1.5"><span class="w-2 h-2 rounded-full ' + statusDot(r.status) + '"></span><span class="' + statusColor(r.status) + '">' + escapeHtml(r.status) + '</span></span></td>' +
        '<td class="p-3 text-gray-400">' + escapeHtml(r.current_step || '--') + '</td>' +
        '<td class="p-3 text-right text-accent-green">$' + r.total_cost_usd.toFixed(4) + '</td>' +
        '<td class="p-3 text-gray-500 text-xs">' + (r.started_at ? new Date(r.started_at).toLocaleString() : '--') + '</td>' +
        '</tr>';
    }).join('') + '</tbody></table>';
}
