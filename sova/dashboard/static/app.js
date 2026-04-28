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

function escapeHtml(str) {
  if (!str) return '';
  return str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function formatDuration(ms) {
  if (!ms || ms <= 0) return '--';
  if (ms < 1000) return ms + 'ms';
  var s = Math.round(ms / 1000);
  if (s < 60) return s + 's';
  return Math.floor(s / 60) + 'm ' + (s % 60) + 's';
}

function statusColor(status) {
  switch (status) {
    case 'done': return 'text-accent-green';
    case 'failed': return 'text-accent-red';
    case 'developing': case 'running': return 'text-accent-yellow';
    case 'pending': return 'text-gray-400';
    default: return 'text-accent';
  }
}

function statusDot(status) {
  switch (status) {
    case 'done': return 'bg-accent-green';
    case 'failed': return 'bg-accent-red';
    case 'developing': case 'running': return 'bg-accent-yellow animate-pulse';
    case 'pending': return 'bg-gray-500';
    default: return 'bg-accent';
  }
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
   5. SIDEBAR POLLING & NOTIFICATIONS
   ============================================================ */

var _notifItems = [];
var _lastActivityState = null;
var _lastHandoffState = null;

function _dotClass(color, animate) {
  return 'absolute -bottom-0.5 -right-0.5 w-3 h-3 rounded-full ' + color + ' border-2 border-sidebar' + (animate ? ' animate-pulse' : '');
}

function startSidebarPolling() {
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
    if (running) {
      dot.className = _dotClass('bg-accent', true);
      if (_lastActivityState !== 'running') {
        _lastActivityState = 'running';
      }
    } else {
      if (_lastActivityState === 'running') {
        var completed = data.completed || [];
        var latest = completed.length > 0 ? completed[completed.length - 1] : null;
        if (latest && (latest.status === 'failed' || latest.status === 'paused')) {
          var label = latest.issue ? '#' + latest.issue : 'Agent';
          _addNotification(label + ' ' + latest.status + (latest.status === 'paused' ? ' -- needs attention' : ' -- check logs'), 'warning');
        } else {
          _addNotification('Agent completed', 'info');
        }
      }
      dot.className = _dotClass('bg-accent-green', false);
      _lastActivityState = 'idle';
    }
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

    if (data.has_handoff && data.handoff.status === 'awaiting_action') {
      banner.classList.remove('hidden');
      if (_lastHandoffState !== 'awaiting') {
        _lastHandoffState = 'awaiting';
        _addNotification('Handoff: action required', 'warning');
        var dot = document.getElementById('activity-dot');
        if (dot) dot.className = _dotClass('bg-accent-yellow', true);
      }
    } else {
      banner.classList.add('hidden');
      if (data.has_handoff && data.handoff.status !== _lastHandoffState) {
        _lastHandoffState = data.handoff.status;
      } else if (!data.has_handoff) {
        _lastHandoffState = null;
      }
    }
  } catch (e) { /* ignore */ }
}

function _addNotification(message, type) {
  _notifItems.unshift({ message: message, type: type, time: new Date() });
  if (_notifItems.length > 20) _notifItems.pop();
  _updateNotifBadge();
  _renderNotifList();
  showToast(message, type);
  if (type === 'warning') {
    sendBrowserNotification('SOVA -- Action Required', message);
  } else {
    sendBrowserNotification('SOVA', message);
  }
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
    return '<div class="px-4 py-3 border-b border-gray-700/30 last:border-0 border-l-2 ' + borderColor + ' hover:bg-surface-hover/50 transition-colors">' +
      '<p class="text-sm text-gray-200">' + escapeHtml(n.message) + '</p>' +
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
   6. PR LINK HELPER
   ============================================================ */

window.SOVA_GITHUB_REPO = null;

function _initGithubRepo() {
  fetchAPI(apiUrl('/settings/config')).then(function(data) {
    window.SOVA_GITHUB_REPO = (data.config && data.config.github_repo) || null;
  }).catch(function() {});
}

function prLink(prNumber) {
  if (!prNumber) return '--';
  var repo = window.SOVA_GITHUB_REPO;
  if (repo) {
    return '<a href="https://github.com/' + escapeHtml(repo) + '/pull/' + prNumber + '" target="_blank" rel="noopener" ' +
      'class="text-accent hover:underline" onclick="event.stopPropagation()">#' + prNumber + '</a>';
  }
  return '#' + prNumber;
}

/* ============================================================
   7. INITIALIZATION
   ============================================================ */

initColors();
_initGithubRepo();
if (document.getElementById('activity-dot')) {
  initBrowserNotifications();
  startSidebarPolling();
}

/* ============================================================
   8. ROLE COLORS
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

function formatRole(role) {
  if (!role) return 'auto';
  if (!role.startsWith('command:')) return role;
  var cmd = role.slice('command:'.length).trim();
  var name = cmd.split(/\s+/)[0].replace(/^\//, '');
  return name;
}

function _commandToRole(role) {
  var cmd = role.slice('command:'.length).trim().replace(/^\//, '');
  if (cmd.startsWith('address-pr')) return 'developer';
  if (cmd.startsWith('review')) return 'reviewer';
  if (cmd.startsWith('develop')) return 'developer';
  return 'auto';
}

/* ============================================================
   9. STEP PIPELINE BAR
   ============================================================ */

var PIPELINE_STEPS = [
  'sync', 'assess', 'create_worktree', 'develop', 'simplify',
  'self_review', 'commit', 'push', 'create_pr', 'monitor_ci',
  'automated_review', 'address_review', 'complete'
];

var STEP_LABELS = {
  sync: 'Sync', assess: 'Assess', create_worktree: 'Worktree',
  develop: 'Develop', simplify: 'Simplify', self_review: 'Review',
  commit: 'Commit', push: 'Push', create_pr: 'PR', monitor_ci: 'CI',
  automated_review: 'Auto Review', address_review: 'Address', complete: 'Done'
};

function renderStepPipeline(currentStep, role, compact) {
  var colors = roleColor(role);
  var idx = currentStep ? PIPELINE_STEPS.indexOf(currentStep) : -1;
  var segments = PIPELINE_STEPS.map(function(step, i) {
    var w = compact ? 'flex-1 h-1.5' : 'flex-1 h-2.5';
    var rounded = '';
    if (i === 0) rounded = ' rounded-l';
    if (i === PIPELINE_STEPS.length - 1) rounded = ' rounded-r';

    if (idx >= 0 && i < idx) {
      return '<div class="' + w + rounded + '" style="background:' + colors.hex + '" title="' + (STEP_LABELS[step] || step) + '"></div>';
    } else if (i === idx) {
      return '<div class="' + w + rounded + ' animate-pulse" style="background:' + colors.hex + ';opacity:0.7" title="' + (STEP_LABELS[step] || step) + ' (current)"></div>';
    } else {
      return '<div class="' + w + ' bg-gray-700' + rounded + '" title="' + (STEP_LABELS[step] || step) + '"></div>';
    }
  });

  var label = '';
  if (!compact && idx >= 0) {
    label = '<div class="text-xs text-gray-400 mt-1">' + (STEP_LABELS[currentStep] || currentStep) + ' (' + (idx + 1) + '/' + PIPELINE_STEPS.length + ')</div>';
  } else if (!compact) {
    label = '<div class="text-xs text-gray-500 mt-1">Initializing...</div>';
  }

  return '<div class="flex gap-0.5">' + segments.join('') + '</div>' + label;
}

function formatElapsed(seconds) {
  if (!seconds || seconds <= 0) return '0s';
  if (seconds < 60) return seconds + 's';
  if (seconds < 3600) return Math.floor(seconds / 60) + 'm ' + (seconds % 60) + 's';
  var h = Math.floor(seconds / 3600);
  var m = Math.floor((seconds % 3600) / 60);
  return h + 'h ' + m + 'm';
}

/* ============================================================
   10. RUNS TABLE (shared)
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
        '<td class="p-3 text-accent">#' + escapeHtml(r.issue_number) + '</td>' +
        '<td class="p-3 text-gray-300">' + escapeHtml(r.role) + '</td>' +
        '<td class="p-3"><span class="flex items-center gap-1.5"><span class="w-2 h-2 rounded-full ' + statusDot(r.status) + '"></span><span class="' + statusColor(r.status) + '">' + escapeHtml(r.status) + '</span></span></td>' +
        '<td class="p-3 text-gray-400">' + escapeHtml(r.current_step || '--') + '</td>' +
        '<td class="p-3 text-right text-accent-green">$' + r.total_cost_usd.toFixed(4) + '</td>' +
        '<td class="p-3 text-gray-500 text-xs">' + (r.started_at ? new Date(r.started_at).toLocaleString() : '--') + '</td>' +
        '</tr>';
    }).join('') + '</tbody></table>';
}
