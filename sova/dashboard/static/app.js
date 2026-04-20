/* SOVA Dashboard -- shared JS utilities */

function apiUrl(path) {
  return (window.SOVA_API_PREFIX || '/api') + path;
}

async function fetchAPI(url) {
  const res = await fetch(url);
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

/* --- Sidebar: activity dot, checkpoint banner, notifications --- */

var _notifItems = [];
var _lastActivityState = null;
var _lastHandoffState = null;

function startSidebarPolling() {
  _pollActivity();
  _pollHandoff();
  setInterval(_pollActivity, 3000);
  setInterval(_pollHandoff, 5000);
}

async function _pollActivity() {
  try {
    var data = await fetchAPI(apiUrl('/control/status'));
    var dot = document.getElementById('activity-dot');
    if (!dot) return;

    if (data.running) {
      dot.className = 'absolute -bottom-0.5 -right-0.5 w-3 h-3 rounded-full bg-accent border-2 border-sidebar animate-pulse';
      if (_lastActivityState !== 'running') {
        _lastActivityState = 'running';
      }
    } else {
      if (_lastActivityState === 'running') {
        // Agent just finished -- add notification
        _addNotification('Agent completed', 'info');
      }
      dot.className = 'absolute -bottom-0.5 -right-0.5 w-3 h-3 rounded-full bg-accent-green border-2 border-sidebar';
      _lastActivityState = 'idle';
    }
  } catch (e) {
    var dot = document.getElementById('activity-dot');
    if (dot) dot.className = 'absolute -bottom-0.5 -right-0.5 w-3 h-3 rounded-full bg-gray-500 border-2 border-sidebar';
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
        // Update activity dot to yellow
        var dot = document.getElementById('activity-dot');
        if (dot) dot.className = 'absolute -bottom-0.5 -right-0.5 w-3 h-3 rounded-full bg-accent-yellow border-2 border-sidebar animate-pulse';
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
    list.innerHTML = '<p class="text-[10px] text-gray-600 text-center py-2">No notifications</p>';
    return;
  }
  list.innerHTML = _notifItems.map(function(n) {
    var color = n.type === 'warning' ? 'text-accent-yellow' : n.type === 'error' ? 'text-accent-red' : 'text-gray-300';
    var timeStr = n.time.toLocaleTimeString();
    return '<div class="py-1.5 px-1 border-b border-gray-700/30 last:border-0">' +
      '<p class="text-[10px] ' + color + '">' + escapeHtml(n.message) + '</p>' +
      '<p class="text-[8px] text-gray-600">' + timeStr + '</p>' +
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

// Auto-start sidebar polling on page load
if (document.getElementById('activity-dot')) {
  startSidebarPolling();
}

/* --- Runs table (shared) --- */

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
