/* PAK Agent Dashboard — shared utilities */

function apiUrl(path) {
  // Prefix API paths with project scope when active
  const prefix = (window.PAK_API_PREFIX || '/api');
  return prefix + path;
}

async function fetchAPI(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`API error: ${res.status}`);
  return res.json();
}

function escapeHtml(str) {
  if (!str) return '';
  return str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function levelColor(level) {
  switch (level) {
    case 'ERROR': return 'text-red-400';
    case 'WARN':  return 'text-yellow-400';
    case 'INFO':  return 'text-blue-400';
    default:      return 'text-gray-400';
  }
}

/* ─── Toast Notification System ─────────────────────────────────────────── */

const toastCategoryStyles = {
  success: { border: '#a6e3a1', bg: 'rgba(166,227,161,0.08)', icon: '\u2713', color: '#a6e3a1' },
  action:  { border: '#f9e2af', bg: 'rgba(249,226,175,0.08)', icon: '\u25cf', color: '#f9e2af' },
  info:    { border: '#89b4fa', bg: 'rgba(137,180,250,0.08)', icon: '\u2192', color: '#89b4fa' },
  warning: { border: '#f38ba8', bg: 'rgba(243,139,168,0.08)', icon: '\u26a0', color: '#f38ba8' },
};

const toastDurations = { success: 6000, info: 6000, action: 12000, warning: 12000 };

function _ensureToastContainer() {
  let c = document.getElementById('toast-container');
  if (!c) {
    c = document.createElement('div');
    c.id = 'toast-container';
    c.className = 'fixed top-4 right-4 z-50 flex flex-col gap-2 pointer-events-none';
    c.style.maxWidth = '380px';
    document.body.appendChild(c);
  }
  return c;
}

function showToast(message, category) {
  category = category || 'info';
  const style = toastCategoryStyles[category] || toastCategoryStyles.info;
  const duration = toastDurations[category] || 6000;
  const container = _ensureToastContainer();

  const toast = document.createElement('div');
  toast.className = 'pointer-events-auto toast-enter';
  toast.style.cssText = `
    background: ${style.bg}; border-left: 3px solid ${style.border};
    border-radius: 0.5rem; padding: 0.75rem 1rem; position: relative;
    backdrop-filter: blur(12px); box-shadow: 0 4px 12px rgba(0,0,0,0.3);
  `;
  toast.innerHTML = `
    <div style="display:flex;align-items:flex-start;gap:0.5rem;">
      <span style="color:${style.color};font-size:1rem;line-height:1.25rem;flex-shrink:0;">${style.icon}</span>
      <span style="color:#cdd6f4;font-size:0.8125rem;line-height:1.25rem;flex:1;">${escapeHtml(message)}</span>
      <button onclick="this.closest('.pointer-events-auto').remove()" style="color:#6c7086;font-size:0.75rem;cursor:pointer;flex-shrink:0;background:none;border:none;">\u2715</button>
    </div>
    <div class="toast-progress" style="position:absolute;bottom:0;left:0;height:2px;background:${style.border};border-radius:0 0 0 0.5rem;animation:toast-progress ${duration}ms linear forwards;"></div>
  `;
  container.appendChild(toast);

  setTimeout(() => {
    toast.classList.remove('toast-enter');
    toast.classList.add('toast-exit');
    toast.addEventListener('animationend', () => toast.remove());
  }, duration);
}

function categorizeNotification(message) {
  const msg = (message || '').toLowerCase();
  if (/merged|ready|finished|posted|applied|complete/.test(msg)) return 'success';
  if (/needs|needed|pick|consider|help|manual|waiting/.test(msg)) return 'action';
  if (/fail|stop|error|unrelated ci/.test(msg)) return 'warning';
  return 'info';
}

/* ─── Browser Notification API ──────────────────────────────────────────── */

function requestNotifyPermission() {
  if ('Notification' in window && Notification.permission === 'default') {
    Notification.requestPermission();
  }
}

function browserNotify(message) {
  if ('Notification' in window && Notification.permission === 'granted' && document.hidden) {
    new Notification('Morning Agent', { body: message, icon: '/static/agent-icon.png' });
  }
}

/* ─── Tab Title Flash ───────────────────────────────────────────────────── */

let _titleFlashInterval = null;
const _originalTitle = document.title;

function flashTitle(message) {
  if (_titleFlashInterval) return;
  _titleFlashInterval = setInterval(() => {
    document.title = document.title === _originalTitle ? '(!) ' + message : _originalTitle;
  }, 1500);
  document.addEventListener('visibilitychange', function _restore() {
    if (!document.hidden && _titleFlashInterval) {
      clearInterval(_titleFlashInterval);
      _titleFlashInterval = null;
      document.title = _originalTitle;
      document.removeEventListener('visibilitychange', _restore);
    }
  });
}

/* ─── Global Checkpoint + Notification Polling ──────────────────────────── */

let _globalNotifCursor = 0;
let _globalNotifCount = 0;

async function globalPoll() {
  if (window._globalPollDisabled) return;
  try {
    // Check for pending checkpoint (update global banner)
    const reqData = await fetchAPI(apiUrl('/agent/request'));
    const banner = document.getElementById('global-checkpoint-banner');
    if (banner) {
      if (reqData.pending) {
        banner.classList.remove('hidden');
        const msgEl = banner.querySelector('.checkpoint-msg');
        if (msgEl) msgEl.textContent = reqData.request.message || 'Waiting for input';
      } else {
        banner.classList.add('hidden');
      }
    }

    // Check for new notifications (update bell badge)
    const notifData = await fetchAPI(apiUrl(`/agent/notifications?since=${_globalNotifCursor}`));
    if (notifData.notifications && notifData.notifications.length > 0) {
      _globalNotifCount += notifData.notifications.length;
      _globalNotifCursor = notifData.total;
      updateNotifBadge();
      // Show toasts for new notifications (only on non-control pages — control page handles its own via WS)
      const isControlPage = window.location.pathname === '/control';
      if (!isControlPage) {
        notifData.notifications.forEach(n => {
          showToast(n.message, categorizeNotification(n.message));
          if (document.hidden) browserNotify(n.message);
        });
      }
    }
  } catch (e) { /* ignore */ }
}

function updateNotifBadge() {
  const badge = document.getElementById('notif-badge');
  if (!badge) return;
  if (_globalNotifCount > 0) {
    badge.textContent = _globalNotifCount > 99 ? '99+' : _globalNotifCount;
    badge.classList.remove('hidden');
  } else {
    badge.classList.add('hidden');
  }
}

function clearNotifBadge() {
  _globalNotifCount = 0;
  updateNotifBadge();
}

async function loadNotifHistory() {
  const list = document.getElementById('notif-history-list');
  if (!list) return;
  try {
    const data = await fetchAPI(apiUrl('/agent/notifications?since=0'));
    const notifs = data.notifications || [];
    if (notifs.length === 0) {
      list.innerHTML = '<p class="text-gray-500 text-xs px-3 py-2">No notifications yet</p>';
      return;
    }
    list.innerHTML = notifs.slice(-20).reverse().map(n => {
      const cat = categorizeNotification(n.message);
      const style = toastCategoryStyles[cat] || toastCategoryStyles.info;
      const time = n.ts ? new Date(n.ts).toLocaleTimeString() : '';
      return `<div class="px-3 py-2 border-b border-gray-700/30 last:border-0">
        <div class="flex items-start gap-2">
          <span style="color:${style.color}">${style.icon}</span>
          <div class="flex-1 min-w-0">
            <p class="text-xs text-gray-300 truncate">${escapeHtml(n.message)}</p>
            <p class="text-[10px] text-gray-500 mt-0.5">${time}</p>
          </div>
        </div>
      </div>`;
    }).join('');
  } catch (e) {
    list.innerHTML = '<p class="text-gray-500 text-xs px-3 py-2">Error loading notifications</p>';
  }
}

function toggleNotifPanel() {
  const panel = document.getElementById('notif-panel');
  if (!panel) return;
  const isHidden = panel.classList.contains('hidden');
  panel.classList.toggle('hidden');
  if (isHidden) {
    loadNotifHistory();
    clearNotifBadge();
  }
}

// Start global polling on every page
setInterval(globalPoll, 3000);
requestNotifyPermission();
