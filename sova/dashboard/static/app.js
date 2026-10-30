/* SOVA Dashboard: shared JS utilities */

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
    greenStrong: getCSSVar('--ctp-green-strong'),
    red:     getCSSVar('--ctp-red'),
    yellow:  getCSSVar('--ctp-yellow'),
    purple:  getCSSVar('--ctp-mauve'),
    peach:    getCSSVar('--ctp-peach'),
    lavender: getCSSVar('--ctp-lavender'),
    sapphire: getCSSVar('--ctp-sapphire'),
    gray:     getCSSVar('--ctp-overlay0'),
    grayDim:  getCSSVar('--ctp-surface2'),
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

async function fetchAPI(url, options) {
  var res = await fetch(url, options);
  if (!res.ok) throw new Error('API error: ' + res.status);
  return res.json();
}

async function getErrorDetail(res, fallback) {
  try {
    var body = await res.json();
    if (!body.detail) return fallback;
    if (Array.isArray(body.detail)) {
      return body.detail.map(function (item) {
        return (item && typeof item === 'object' && item.msg) ? item.msg : JSON.stringify(item);
      }).join('; ') || fallback;
    }
    return body.detail;
  }
  catch (_e) { return fallback; }
}

/* Double-submit CSRF header for a state-changing fetch(): reads the
   sova_csrf cookie (set by issue_csrf_cookie() on a page's own GET route)
   and echoes it back as X-CSRF-Token, matching require_same_origin_csrf()'s
   comparison. Empty on a page that never issued the cookie, which the guard
   rejects the same as a mismatch: there is no silent bypass either way. */
function csrfHeaders() {
  var match = document.cookie.match(/(?:^|; )sova_csrf=([^;]*)/);
  return match ? {'X-CSRF-Token': decodeURIComponent(match[1])} : {};
}

/* Headers for a state-changing fetch() that sends a JSON body: every guarded
   POST in the dashboard needs both the content type and the CSRF echo. */
function jsonCsrfHeaders() {
  return Object.assign({'Content-Type': 'application/json'}, csrfHeaders());
}

/* Router-level CSRF enforcement (#1204) means every mutating route is guarded
   now, not just the handful that used to opt in individually, so every
   mutating fetch() in the dashboard needs the X-CSRF-Token header. Patching
   fetch() once here covers every existing call site (and any added later)
   without requiring each one to call csrfHeaders() itself. A call that
   already set the header explicitly is left alone.

   A rejected pair means the page itself is stale (opened before a dashboard
   restart or deploy, so it has no cookie), and only a reload fixes it. Callers
   show the 403 detail verbatim, so the wrapper rewrites that one detail into a
   reload hint and keeps one sticky toast with a Reload button on screen (never
   an automatic reload, which would discard an open form). The detail string
   must match CSRF_REJECTED_DETAIL in sova/dashboard/security.py. */
var CSRF_REJECTED_DETAIL = 'Missing or invalid CSRF token';
var STALE_PAGE_DETAIL = 'This page is out of date (the dashboard was restarted or updated). Reload the page and try again.';
var _stalePageToast = null;

function _notifyStalePage() {
  if (_stalePageToast && _stalePageToast.isConnected) return;
  _stalePageToast = showToast(STALE_PAGE_DETAIL, 'warning', 0, {
    label: 'Reload',
    onClick: function () { window.location.reload(); },
  });
}

function _rewriteCsrfRejection(res) {
  if (res.status !== 403) return res;
  return res.clone().json().then(function (body) {
    if (!body || body.detail !== CSRF_REJECTED_DETAIL) return res;
    _notifyStalePage();
    return new Response(JSON.stringify({detail: STALE_PAGE_DETAIL}), {
      status: res.status,
      statusText: res.statusText,
      headers: {'Content-Type': 'application/json'},
    });
  }, function () { return res; });
}

(function patchFetchForCsrf() {
  var MUTATING_METHODS = ['POST', 'PUT', 'PATCH', 'DELETE'];
  var originalFetch = window.fetch.bind(window);
  window.fetch = function (input, init) {
    init = init || {};
    var request = (typeof Request !== 'undefined' && input instanceof Request) ? input : null;
    var method = (init.method || (request && request.method) || 'GET').toUpperCase();
    if (MUTATING_METHODS.indexOf(method) === -1) return originalFetch(input, init);
    var headers = new Headers(init.headers || (request ? request.headers : {}));
    if (!headers.has('X-CSRF-Token')) {
      var csrf = csrfHeaders();
      if (csrf['X-CSRF-Token']) headers.set('X-CSRF-Token', csrf['X-CSRF-Token']);
    }
    init = Object.assign({}, init, {headers: headers});
    return originalFetch(input, init).then(_rewriteCsrfRejection);
  };
})();

function escapeHtml(text) {
  return String(text == null ? '' : text)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

// Short alias used by the page templates and role-editor.js; defined once here so no page redeclares it.
var esc = escapeHtml;

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

// Model family grouping (shared by every grouped model dropdown)

// Vendor-facing labels for optgroup headers, and the order groups render in.
// Lives here rather than in one page's inline script because three dropdowns
// across two pages (settings' single and list model editors, the Agents launch
// modal's per-run LLM override) build the same grouping from
// GET /api/models/available, and a per-page copy would let the same family
// show two different labels. Families not listed here (future ModelFamily
// additions) are appended after the known ones, sorted alphabetically, so a
// new family never goes missing, just unordered until this map catches up.
var MODEL_FAMILY_LABELS = {
  anthropic: 'Claude',
  google: 'Gemini',
  openai: 'OpenAI',
  'openai-oss': 'gpt-oss',
  local: 'Local',
  unknown: 'Other',
};
var MODEL_FAMILY_ORDER = ['anthropic', 'google', 'openai', 'openai-oss', 'local', 'unknown'];

function modelFamilyLabel(family) {
  return MODEL_FAMILY_LABELS[family] || family;
}

function groupModelsByFamily(models) {
  var byFamily = {};
  (models || []).forEach(function(m) {
    var family = m.family || 'unknown';
    if (!byFamily[family]) byFamily[family] = [];
    byFamily[family].push(m);
  });
  return byFamily;
}

function sortedModelFamilies(byFamily) {
  var known = MODEL_FAMILY_ORDER.filter(function(f) { return byFamily[f]; });
  var rest = Object.keys(byFamily)
    .filter(function(f) { return MODEL_FAMILY_ORDER.indexOf(f) === -1; })
    .sort();
  return known.concat(rest);
}

// Append one <optgroup> of model options per family to *select*. Returns
// whether *selectedId* matched a listed model, which the settings editor uses
// to decide if a saved value is still in the catalog; callers that pre-select
// nothing pass no id and ignore the result.
function appendModelOptgroups(select, byFamily, selectedId) {
  var matched = false;
  sortedModelFamilies(byFamily).forEach(function(family) {
    var group = document.createElement('optgroup');
    group.label = modelFamilyLabel(family);
    byFamily[family].forEach(function(m) {
      var o = document.createElement('option');
      o.value = m.id;
      o.textContent = m.display_name || m.id;
      if (selectedId && m.id === selectedId) {
        o.selected = true;
        matched = true;
      }
      group.appendChild(o);
    });
    select.appendChild(group);
  });
  return matched;
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
  stopped:           { dot: 'bg-accent-purple',   text: 'text-accent-purple',    bg: 'bg-accent-purple/20' },
  running:           { dot: 'bg-accent-green',    text: 'text-accent-green',     bg: 'bg-accent-green/20' },
};

var _STATUS_TERMINAL = { done: 1, failed: 1, rejected: 1, interrupted: 1, paused: 1, awaiting_approval: 1, stopped: 1 };
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
  refresh_pr_body: 'Refreshing PR body',
  monitor_ci: 'CI checks',
  confidence_score: 'Scoring confidence',
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

// A timestamp in the future (clock skew) reads "just now". queue.html's old private copy returned
// an empty string for it; the shared helper deliberately does not special-case one page.
function timeAgo(isoStr, emptyLabel) {
  var empty = emptyLabel === undefined ? '' : emptyLabel;
  if (!isoStr) return empty;
  var d = new Date(isoStr);
  if (isNaN(d.getTime())) return empty;
  var diff = Math.floor((Date.now() - d.getTime()) / 1000);
  if (diff < 0) return 'just now';
  if (diff < 60) return diff + 's ago';
  if (diff < 3600) return Math.floor(diff / 60) + 'm ago';
  if (diff < 86400) return Math.floor(diff / 3600) + 'h ago';
  return Math.floor(diff / 86400) + 'd ago';
}

var STATUS_BADGE_CLASSES = {
  done:    'bg-accent-green/15 text-accent-green',
  error:   'bg-accent-red/15 text-accent-red',
  running: 'bg-accent/15 text-accent',
};

// Run/status pill as an HTML string. Pass a classMap to override the default done/error/running colors;
// an unmapped status renders neutral gray, with its text escaped either way.
function statusBadge(status, classMap) {
  var map = classMap || STATUS_BADGE_CLASSES;
  var cls = Object.prototype.hasOwnProperty.call(map, status) ? map[status] : 'bg-gray-700/30 text-gray-400';
  return '<span class="text-xs px-2 py-0.5 rounded ' + cls + '">' + escapeHtml(status) + '</span>';
}

function setText(id, text) {
  var el = document.getElementById(id);
  if (el) el.textContent = text;
}

function renderSectionError(containerId, message) {
  var el = document.getElementById(containerId);
  if (!el) return;
  el.innerHTML = '';
  var p = document.createElement('p');
  p.className = 'text-red-400 text-sm';
  p.textContent = message;
  el.appendChild(p);
}

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

/* duration: milliseconds before auto-dismiss (default 6000); 0 keeps the toast
   until the user dismisses it. action: optional {label, onClick} rendered as a
   button beside the dismiss control. Returns the toast element. */
function showToast(message, type, duration, action) {
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

  if (action) {
    var actionBtn = document.createElement('button');
    actionBtn.className = 'sova-btn sova-btn-warning shrink-0 ml-2';
    actionBtn.textContent = action.label;
    actionBtn.addEventListener('click', action.onClick);
    toast.insertBefore(actionBtn, toast.lastChild);
  }

  _toastContainer.appendChild(toast);

  requestAnimationFrame(function() {
    requestAnimationFrame(function() {
      toast.classList.add('sova-toast-visible');
    });
  });

  if (duration === 0) return toast;
  setTimeout(function() {
    toast.classList.remove('sova-toast-visible');
    setTimeout(function() {
      if (toast.parentElement) toast.remove();
    }, 300);
  }, duration !== undefined && duration !== null ? duration : 6000);
  return toast;
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

/* Runs fn at once on start and each time the tab becomes visible again, then every intervalMs while the tab
   is visible. */
function visibilityAwarePoll(fn, intervalMs) {
  var state = { id: null };
  if (!document.hidden) {
    fn();
    state.id = setInterval(fn, intervalMs);
  }
  document.addEventListener('visibilitychange', function() {
    clearInterval(state.id);
    if (document.hidden) {
      state.id = null;
    } else {
      fn();
      state.id = setInterval(fn, intervalMs);
    }
  });
  return state;
}

/* ============================================================
   6a. FLOATING WIDGET TOGGLE (shared by the corner widgets)
   ============================================================ */

/* Wires a corner widget's expand/collapse button: click toggles the panel,
   the state persists in localStorage under storageKey, and a storage event
   keeps other tabs in sync. Storage access is wrapped: private-browsing modes
   and disabled storage can make localStorage throw, and a widget must still
   initialize (just without persisted state) rather than fail entirely. */
function safeStorageGet(key) {
  try {
    return localStorage.getItem(key);
  } catch (e) {
    return null;
  }
}

function safeStorageSet(key, value) {
  try {
    localStorage.setItem(key, value);
  } catch (e) {
    /* storage unavailable; toggle state simply won't persist */
  }
}

function initWidgetToggle(toggleId, panelId, storageKey) {
  var toggle = document.getElementById(toggleId);
  var panel = document.getElementById(panelId);

  function syncChevron(expanded) {
    var chevron = toggle && toggle.querySelector('.resource-widget-chevron');
    if (chevron) chevron.style.transform = expanded ? 'rotate(180deg)' : '';
  }

  if (toggle && panel) {
    toggle.addEventListener('click', function () {
      var nowHidden = panel.classList.toggle('hidden');
      safeStorageSet(storageKey, nowHidden ? '0' : '1');
      syncChevron(!nowHidden);
    });
    if (safeStorageGet(storageKey) === '1') {
      panel.classList.remove('hidden');
      syncChevron(true);
    }
  }

  window.addEventListener('storage', function (e) {
    if (e.key === storageKey && panel) {
      var show = e.newValue === '1';
      panel.classList.toggle('hidden', !show);
      syncChevron(show);
    }
  });
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
var _connectivityPollInterval = null;

function startSidebarPolling() {
  _pollActivity();
  _pollHandoff();
  _pollConnectivity();
  _activityPollInterval = setInterval(_pollActivity, 3000);
  _handoffPollInterval = setInterval(_pollHandoff, 5000);
  _connectivityPollInterval = setInterval(_pollConnectivity, 15000);
  _initFeedSSE();
}

// Connectivity banner
//
// window._connectivityDown is the flag spawn handlers check via
// blockedByConnectivity(). The backend gate is the real enforcement; this only
// spares the operator a doomed click and the wall of 1-second failures it
// produces. Not project-prefixed: reachability is machine-wide, so the endpoint
// is registered on the app rather than in a per-project router.
window._connectivityDown = false;

async function _pollConnectivity() {
  var data;
  try {
    data = await fetchAPI('/api/health/connectivity');
  } catch (_e) {
    // The dashboard itself being unreachable is a different failure than the
    // internet being down, and this banner must not claim the latter on the
    // strength of the former. Leave the last known state alone.
    return;
  }

  window._connectivityDown = !!data.is_down;
  var banner = document.getElementById('connectivity-banner');
  if (!banner) return;

  if (!data.is_down) {
    banner.classList.add('hidden');
    return;
  }

  var detail = document.getElementById('connectivity-banner-detail');
  if (detail) {
    var mins = Math.floor((data.down_seconds || 0) / 60);
    var forStr = mins > 0 ? ' for ' + mins + 'm' : '';
    detail.textContent =
      'Agent spawning is paused' + forStr +
      '. Interrupted runs resume automatically once the connection returns.';
  }
  banner.classList.remove('hidden');
}

function blockedByConnectivity(action) {
  if (!window._connectivityDown) return false;
  showToast('No internet connection: ' + (action || 'this action') + ' would fail immediately.', 'error');
  return true;
}

document.addEventListener('visibilitychange', function() {
  if (document.hidden) {
    if (_activityPollInterval) { clearInterval(_activityPollInterval); _activityPollInterval = null; }
    if (_handoffPollInterval) { clearInterval(_handoffPollInterval); _handoffPollInterval = null; }
    if (_connectivityPollInterval) { clearInterval(_connectivityPollInterval); _connectivityPollInterval = null; }
    if (_globalBatchPollInterval) { clearInterval(_globalBatchPollInterval); _globalBatchPollInterval = null; }
  } else {
    _pollActivity();
    _pollHandoff();
    _pollConnectivity();
    if (!_activityPollInterval) _activityPollInterval = setInterval(_pollActivity, 3000);
    if (!_handoffPollInterval) _handoffPollInterval = setInterval(_pollHandoff, 5000);
    if (!_connectivityPollInterval) _connectivityPollInterval = setInterval(_pollConnectivity, 15000);
    if (_globalBatchId && !_globalBatchPollInterval) {
      _pollGlobalBatch();
      _globalBatchPollInterval = setInterval(_pollGlobalBatch, 2000);
    }
  }
});

async function _pollActivity() {
  try {
    var data = await fetchAPI(apiUrl('/agents/active'));
    window._sovaActiveAgents = { data: data, ts: Date.now() };
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
    var unreadableBanner = document.getElementById('handoff-unreadable-banner');

    if (unreadableBanner) {
      var unreadable = data.unreadable_handoffs || [];
      // Rendered by the dashboard (legacy shapes are accepted) but skipped by the supervisor and the
      // auto-handoff, so the page must not present them as healthy.
      var schemaInvalid = data.schema_invalid_handoffs || [];
      unreadableBanner.classList.toggle('hidden', !(unreadable.length || schemaInvalid.length));
      var unreadableLabel = document.getElementById('handoff-unreadable-label');
      if (unreadableLabel && (unreadable.length || schemaInvalid.length)) {
        // File names in the label, full paths on hover: the banner sits in a narrow sidebar.
        var baseName = function(p) { return p.split(/[\\/]/).pop(); };
        var parts = [];
        if (unreadable.length) parts.push('Handoff file unreadable: ' + unreadable.map(baseName).join(', '));
        if (schemaInvalid.length) parts.push('Handoff file ignored by the pipeline (invalid schema): ' + schemaInvalid.map(baseName).join(', '));
        unreadableLabel.textContent = parts.join('; ');
        unreadableLabel.title = unreadable.concat(schemaInvalid).join('\n');
      }
    }

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

var _feedEvents = [];          // ascending by id (oldest first), as rendered
var _feedUnread = 0;
var _feedLastId = 0;           // highest id seen (newest)
var _feedOldestId = 0;         // lowest id currently loaded (pagination cursor)
var _feedSeenIds = {};
var _feedEventSource = null;
var _feedPanelOpen = false;
var _feedRenderPending = false;
var _feedHasMoreHistory = true;
var _feedHistoryLoading = false;
var _feedHistoryLoaded = false;
var _feedBriefingLoaded = false;
var _feedBriefingHtml = '';
var _feedScrollBound = false;
var _FEED_MAX_EVENTS = 500;
var _FEED_PAGE_SIZE = 50;

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
      showToast('Activity feed gap detected: some events were missed', 'warning');
    }
    var events = data.events || [];
    events.forEach(function(event) {
      if (event.id > _feedLastId) {
        _feedAddEvent(event);
      }
    });
  }).catch(function() { /* ignore */ });
}

// Insert a live event (from SSE). Appends to the tail (newest).
function _feedAddEvent(event) {
  // Dedup by ID (set handles out-of-order gap-fill events)
  if (_feedSeenIds[event.id]) return;
  _feedSeenIds[event.id] = true;
  if (event.id > _feedLastId) _feedLastId = event.id;
  if (_feedOldestId === 0 || event.id < _feedOldestId) _feedOldestId = event.id;
  _feedEvents.push(event);
  if (_feedEvents.length > _FEED_MAX_EVENTS) {
    var removed = _feedEvents.shift();
    if (removed) {
      delete _feedSeenIds[removed.id];
      if (_feedEvents.length > 0) _feedOldestId = _feedEvents[0].id;
    }
  }

  if (!_feedPanelOpen) {
    _feedUnread++;
    _updateFeedBadge();
  }

  // Toast + browser notification for non-info events
  if (event.severity !== 'info') {
    showToast(event.title, event.severity);
    var browserTitle = event.severity === 'error' ? 'SOVA: Error' :
                       event.severity === 'warning' ? 'SOVA: Warning' : 'SOVA';
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
  var fab = document.getElementById('feed-fab');
  if (!badge) return;
  if (_feedUnread > 0) {
    badge.textContent = _feedUnread > 9 ? '9+' : String(_feedUnread);
    badge.classList.remove('hidden');
    if (fab) fab.setAttribute('aria-label', 'Activity Feed, ' + _feedUnread + ' unread');
  } else {
    badge.classList.add('hidden');
    if (fab) fab.setAttribute('aria-label', 'Activity Feed');
  }
}

// chat rendering helpers

function _feedDayKey(ts) {
  var d = new Date(ts * 1000);
  return d.getFullYear() + '-' + d.getMonth() + '-' + d.getDate();
}

function _feedDayLabel(ts) {
  var d = new Date(ts * 1000);
  var now = new Date();
  var today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  var that = new Date(d.getFullYear(), d.getMonth(), d.getDate());
  var diffDays = Math.round((today - that) / 86400000);
  if (diffDays === 0) return 'Today';
  if (diffDays === 1) return 'Yesterday';
  return d.toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric' });
}

function _feedBubbleHtml(e) {
  var sev = (e.severity === 'error' || e.severity === 'warning' || e.severity === 'success') ? e.severity : 'info';
  var timeStr = (typeof e.timestamp === 'number')
    ? new Date(e.timestamp * 1000).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
    : '';
  var costHtml = '';
  if (e.metadata && e.metadata.cost_usd != null) {
    costHtml = '<span class="feed-bubble-cost">$' + parseFloat(e.metadata.cost_usd || 0).toFixed(2) + '</span>';
  }
  var detailHtml = '';
  if (e.detail) {
    detailHtml = '<details class="feed-bubble-detail"><summary>Details</summary>' +
      '<div class="feed-bubble-detail-body">' + escapeHtml(e.detail) + '</div></details>';
  }
  return '<div class="feed-msg">' +
    '<span class="feed-avatar feed-avatar-owl feed-msg-avatar"><img src="/static/owl-avatar.png" alt="SOVA"></span>' +
    '<div class="feed-msg-stack">' +
      '<div class="feed-bubble feed-bubble-' + sev + '">' +
        '<div class="feed-bubble-title">' + escapeHtml(e.title) + '</div>' +
        detailHtml +
        '<div class="feed-bubble-meta">' +
          (timeStr ? '<span class="feed-bubble-time">' + escapeHtml(timeStr) + '</span>' : '') +
          '<span class="feed-bubble-cat">' + escapeHtml(e.category || 'system') + '</span>' +
          costHtml +
        '</div>' +
      '</div>' +
    '</div>' +
  '</div>';
}

function _renderFeedList() {
  var list = document.getElementById('feed-list');
  if (!list) return;

  var atBottom = (list.scrollHeight - list.scrollTop - list.clientHeight) < 80;
  var parts = [];

  // Load-older control (only when more history exists)
  if (_feedHasMoreHistory && _feedEvents.length > 0) {
    parts.push('<button type="button" class="feed-load-older" onclick="loadOlderFeed()"' +
      (_feedHistoryLoading ? ' disabled' : '') + '>' +
      (_feedHistoryLoading ? 'Loading...' : 'Load earlier activity') + '</button>');
  } else if (!_feedHasMoreHistory && _feedBriefingHtml) {
    // At the very top of the timeline: show the morning briefing card.
    parts.push(_feedBriefingHtml);
  }

  if (_feedEvents.length === 0) {
    if (_feedBriefingHtml && parts.indexOf(_feedBriefingHtml) === -1) parts.push(_feedBriefingHtml);
    list.innerHTML = parts.join('') +
      '<p id="feed-empty" class="text-xs text-gray-500 text-center py-8">No activity yet</p>';
    return;
  }

  var lastDay = null;
  for (var i = 0; i < _feedEvents.length; i++) {
    var e = _feedEvents[i];
    if (typeof e.timestamp === 'number') {
      var dk = _feedDayKey(e.timestamp);
      if (dk !== lastDay) {
        lastDay = dk;
        parts.push('<div class="feed-date-sep">' + escapeHtml(_feedDayLabel(e.timestamp)) + '</div>');
      }
    }
    parts.push(_feedBubbleHtml(e));
  }

  list.innerHTML = parts.join('');

  if (atBottom) list.scrollTop = list.scrollHeight;
}

function feedScrollToBottom() {
  var list = document.getElementById('feed-list');
  if (list) list.scrollTop = list.scrollHeight;
  var pill = document.getElementById('feed-jump-latest');
  if (pill) pill.classList.add('hidden');
}

// history pagination (infinite scroll upward)

function loadOlderFeed() {
  if (_feedHistoryLoading || !_feedHasMoreHistory) return;
  _feedHistoryLoading = true;
  _renderFeedList();
  var cursor = _feedOldestId || 0;
  var url = apiUrl('/feed/history?before_id=' + cursor + '&limit=' + _FEED_PAGE_SIZE);
  var list = document.getElementById('feed-list');
  var prevHeight = list ? list.scrollHeight : 0;
  fetch(url).then(function(r) { return r.json(); }).then(function(data) {
    var events = data.events || [];
    _feedHasMoreHistory = !!data.has_more;
    // Prepend oldest-first, skipping dupes.
    var prepend = [];
    events.forEach(function(ev) {
      if (!_feedSeenIds[ev.id]) {
        _feedSeenIds[ev.id] = true;
        prepend.push(ev);
        if (_feedOldestId === 0 || ev.id < _feedOldestId) _feedOldestId = ev.id;
        if (ev.id > _feedLastId) _feedLastId = ev.id;
      }
    });
    _feedEvents = prepend.concat(_feedEvents);
    _feedHistoryLoading = false;
    _renderFeedList();
    // Preserve scroll position after prepending older content.
    if (list) {
      var newHeight = list.scrollHeight;
      list.scrollTop = newHeight - prevHeight;
    }
  }).catch(function() {
    _feedHistoryLoading = false;
    _renderFeedList();
  });
}

// First-open history + briefing bootstrap.
function _feedBootstrapHistory() {
  if (_feedHistoryLoaded) return;
  _feedHistoryLoaded = true;
  var url = apiUrl('/feed/history?before_id=999999999&limit=' + _FEED_PAGE_SIZE);
  fetch(url).then(function(r) { return r.json(); }).then(function(data) {
    var events = data.events || [];
    _feedHasMoreHistory = !!data.has_more;
    events.forEach(function(ev) {
      if (!_feedSeenIds[ev.id]) {
        _feedSeenIds[ev.id] = true;
        _feedEvents.push(ev);
        if (_feedOldestId === 0 || ev.id < _feedOldestId) _feedOldestId = ev.id;
        if (ev.id > _feedLastId) _feedLastId = ev.id;
      }
    });
    // Keep ascending order in case live events arrived before history.
    _feedEvents.sort(function(a, b) { return a.id - b.id; });
    _renderFeedList();
    feedScrollToBottom();
  }).catch(function() { _renderFeedList(); });

  _feedLoadBriefing();
}

function _feedLoadBriefing() {
  if (_feedBriefingLoaded) return;
  _feedBriefingLoaded = true;
  fetch(apiUrl('/feed/briefing')).then(function(r) { return r.json(); }).then(function(b) {
    _feedBriefingHtml = _feedBuildBriefing(b);
    if (_feedBriefingHtml) _renderFeedList();
  }).catch(function() { /* ignore */ });
}

function _feedBuildBriefing(b) {
  if (!b) return '';
  var attention = b.attention_items || [];
  var info = b.informational_items || [];
  var schedule = b.schedule || [];
  if (attention.length === 0 && info.length === 0 && schedule.length === 0) return '';

  function itemRow(it, dotColor) {
    var safeLink = escapeHtml(it.title);
    if (it.source_url) {
      try {
        var proto = new URL(it.source_url).protocol;
        if (proto === 'http:' || proto === 'https:') {
          safeLink = '<a href="' + escapeHtml(it.source_url) + '" target="_blank" rel="noopener">' + escapeHtml(it.title) + '</a>';
        }
      } catch(e) { /* invalid URL, render as plain text */ }
    }
    return '<div class="feed-briefing-item"><span class="feed-briefing-dot" style="background:' + dotColor + '"></span><span>' + safeLink + '</span></div>';
  }

  var sections = '';
  if (attention.length) {
    sections += '<div class="feed-briefing-section-label">Needs attention</div>' +
      attention.map(function(it) { return itemRow(it, 'var(--color-accent-red)'); }).join('');
  }
  if (info.length) {
    sections += '<div class="feed-briefing-section-label">Updates</div>' +
      info.map(function(it) { return itemRow(it, 'var(--color-accent)'); }).join('');
  }
  if (schedule.length) {
    sections += '<div class="feed-briefing-section-label">Schedule</div>' +
      schedule.map(function(it) { return itemRow(it, 'var(--color-accent-yellow)'); }).join('');
  }

  return '<div id="feed-briefing-card" class="feed-briefing">' +
    '<div class="feed-briefing-head">' +
      '<span class="feed-avatar feed-avatar-owl feed-avatar-sm"><img src="/static/owl-avatar.png" alt="SOVA"></span>' +
      '<span class="feed-briefing-title">Morning briefing</span>' +
    '</div>' + sections + '</div>';
}

function toggleFeedPanel() {
  var panel = document.getElementById('feed-panel');
  var main = document.getElementById('main-content');
  var fab = document.getElementById('feed-fab');
  if (!panel) return;

  _feedPanelOpen = !_feedPanelOpen;

  if (_feedPanelOpen) {
    panel.classList.remove('hidden');
    document.body.classList.add('feed-open');
    if (main) main.classList.add('feed-panel-push');
    if (fab) fab.setAttribute('aria-expanded', 'true');
    _feedUnread = 0;
    _updateFeedBadge();
    _feedBootstrapHistory();
    _renderFeedList();
    feedScrollToBottom();
    _feedBindScroll();
  } else {
    panel.classList.add('hidden');
    document.body.classList.remove('feed-open');
    if (main) main.classList.remove('feed-panel-push');
    if (fab) fab.setAttribute('aria-expanded', 'false');
  }
}

function _feedBindScroll() {
  if (_feedScrollBound) return;
  var list = document.getElementById('feed-list');
  if (!list) return;
  _feedScrollBound = true;
  list.addEventListener('scroll', function() {
    // Auto-load older when near the top.
    if (list.scrollTop < 40 && _feedHasMoreHistory && !_feedHistoryLoading) {
      loadOlderFeed();
    }
    // Toggle the jump-to-latest pill.
    var pill = document.getElementById('feed-jump-latest');
    if (pill) {
      var farFromBottom = (list.scrollHeight - list.scrollTop - list.clientHeight) > 200;
      pill.classList.toggle('hidden', !farFromBottom);
    }
  });
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

/* A collapsed sidebar's hover tooltips are position: fixed (the scrolling nav list would clip an
   absolute one), so each hovered element hands its own vertical centre to the stylesheet. */
function initSidebarTooltips() {
  var sidebar = document.querySelector('.sidebar');
  if (!sidebar) return;
  sidebar.addEventListener('mouseover', function (e) {
    var el = e.target.closest ? e.target.closest('[data-tooltip]') : null;
    if (!el || !sidebar.contains(el)) return;
    var rect = el.getBoundingClientRect();
    el.style.setProperty('--sidebar-tip-y', (rect.top + rect.height / 2) + 'px');
  });
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
initSidebarTooltips();
if (document.getElementById('activity-dot')) {
  initBrowserNotifications();
  startSidebarPolling();
}
initGlobalBatch();

/* ============================================================
   12. PRIORITY ICONS
   ============================================================ */

// Sourced from window.SOVA_PRIORITIES (rendered server-side from the single
// Priority registry in sova/adapters/priority.py), not a hand-kept copy.
// Built once: the callers are sort comparators, so rebuilding per comparison
// would walk the registry O(n log n) times per render. Only a non-empty
// result is cached: home.html/setup.html load this script without
// base.html's SOVA_PRIORITIES bootstrap, so an early call there must not
// permanently pin an empty map for the rest of the page's lifetime.
var _priorityRanks = null;
var _priorityNames = null;
var _priorityIcons = null;

function _priorityRankMap() {
  if (_priorityRanks) return _priorityRanks;
  var m = {};
  (window.SOVA_PRIORITIES || []).forEach(function(entry) { m[entry.value] = entry.rank; });
  if (Object.keys(m).length) _priorityRanks = m;
  return m;
}

function _priorityDisplayNameMap() {
  if (_priorityNames) return _priorityNames;
  var m = {};
  (window.SOVA_PRIORITIES || []).forEach(function(entry) { m[entry.value] = entry.display_name; });
  if (Object.keys(m).length) _priorityNames = m;
  return m;
}

function _priorityIconMap() {
  if (_priorityIcons) return _priorityIcons;
  var m = {};
  (window.SOVA_PRIORITIES || []).forEach(function(entry) { m[entry.value] = entry.icon; });
  if (Object.keys(m).length) _priorityIcons = m;
  return m;
}

function priorityIconUrl(priority) {
  var p = (priority || '').toString().toLowerCase().trim();
  var icons = _priorityIconMap();
  var stem = Object.prototype.hasOwnProperty.call(icons, p) ? icons[p] : 'undefined';
  return '/static/priority/' + stem + '.svg';
}

function priorityDisplayName(priority) {
  var p = (priority || '').toString().toLowerCase().trim();
  var names = _priorityDisplayNameMap();
  return Object.prototype.hasOwnProperty.call(names, p) ? names[p] : 'Undefined';
}

/* ============================================================
   13. ROLE COLORS
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
  'sync', 'assess', 'create_worktree', 'capture_baseline', 'develop', 'simplify',
  'self_review', 'commit', 'validate', 'push', 'create_pr',
  'wait_for_external_reviews', 'address_external_findings',
  'monitor_ci', 'confidence_score', 'extract_memory', 'handoff_to_reviewer'
];

var STEP_LABELS = {
  sync: 'Sync', assess: 'Assess', create_worktree: 'Worktree',
  capture_baseline: 'Baseline', develop: 'Develop', simplify: 'Simplify', self_review: 'Review',
  commit: 'Commit', validate: 'Validate', push: 'Push', create_pr: 'PR',
  wait_for_external_reviews: 'Ext Reviews', address_external_findings: 'Address Ext',
  monitor_ci: 'CI', confidence_score: 'Confidence', extract_memory: 'Memory', handoff_to_reviewer: 'Handoff',
  rebase: 'Rebase', address_review: 'Address', resolve_external_reviews: 'Resolve', refresh_pr_body: 'PR Body',
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

/* State colors. The API decides which token a state gets (state_color, from
   sova/dashboard/services/work_state.py); these two lookups only turn a token into
   badge classes or a hex, so no page keeps its own state-to-color table. */
var STATE_BADGE_CLASSES = {
  'gray':         'bg-gray-600/30 text-gray-400',
  'gray-dim':     'bg-gray-600/30 text-gray-500',
  'yellow':       'bg-accent-yellow/20 text-accent-yellow',
  'peach':        'bg-accent-peach/20 text-accent-peach',
  'green':        'bg-accent-green/20 text-accent-green',
  'green-strong': 'bg-accent-green/30 text-accent-green',
  'purple':       'bg-accent-purple/20 text-accent-purple',
  'blue':         'bg-accent/20 text-accent',
  'red':          'bg-accent-red/20 text-accent-red',
  'lavender':     'bg-accent-lavender/20 text-accent-lavender',
};

function stateBadgeClasses(token) {
  return Object.prototype.hasOwnProperty.call(STATE_BADGE_CLASSES, token) ? STATE_BADGE_CLASSES[token] : STATE_BADGE_CLASSES.gray;
}

var _STATE_HEX_KEYS = {
  'gray': 'gray', 'gray-dim': 'grayDim', 'yellow': 'yellow', 'peach': 'peach', 'green': 'green',
  'green-strong': 'greenStrong', 'purple': 'purple', 'blue': 'accent', 'red': 'red', 'lavender': 'lavender',
};

function stateHex(token) {
  var key = Object.prototype.hasOwnProperty.call(_STATE_HEX_KEYS, token) ? _STATE_HEX_KEYS[token] : 'gray';
  return (window.SOVA_COLORS && window.SOVA_COLORS[key]) || '#6c7086';
}

var PHASE_STATUS_COLORS = {
  completed: { bg: 'bg-accent-green',  text: 'text-accent-green',  ring: 'ring-accent-green/30' },
  active:    { bg: 'bg-accent',        text: 'text-accent',        ring: 'ring-accent/30' },
  failed:    { bg: 'bg-accent-red',    text: 'text-accent-red',    ring: 'ring-accent-red/30' },
  stopped:   { bg: 'bg-accent-purple', text: 'text-accent-purple', ring: 'ring-accent-purple/30' },
  skipped:   { bg: 'bg-gray-600',      text: 'text-gray-500',      ring: 'ring-gray-600/30' },
  'default': { bg: 'bg-gray-700',      text: 'text-gray-500',      ring: 'ring-gray-700/30' },
};

function phaseStatusColors(status) {
  return Object.prototype.hasOwnProperty.call(PHASE_STATUS_COLORS, status) ? PHASE_STATUS_COLORS[status] : PHASE_STATUS_COLORS['default'];
}

var SPEC_STATUS_CONFIG = {
  draft:    { label: 'Draft',    badge: 'bg-accent-yellow/20 text-accent-yellow', border: 'border-accent-yellow/30' },
  approved: { label: 'Approved', badge: 'bg-accent-green/20 text-accent-green',   border: 'border-accent-green/30' },
  rejected: { label: 'Rejected', badge: 'bg-accent-red/20 text-accent-red',       border: 'border-accent-red/30' },
};

function specStatusConfig(status) {
  return Object.prototype.hasOwnProperty.call(SPEC_STATUS_CONFIG, status) ? SPEC_STATUS_CONFIG[status] : SPEC_STATUS_CONFIG.draft;
}

var SPEC_COMPLEXITY_COLORS = {
  trivial:  'bg-gray-600/30 text-gray-400',
  simple:   'bg-accent-green/20 text-accent-green',
  moderate: 'bg-accent-yellow/20 text-accent-yellow',
  complex:  'bg-accent-red/20 text-accent-red',
  unknown:  'bg-gray-600/30 text-gray-500',
};

function specComplexityColor(complexity) {
  return Object.prototype.hasOwnProperty.call(SPEC_COMPLEXITY_COLORS, complexity) ? SPEC_COMPLEXITY_COLORS[complexity] : SPEC_COMPLEXITY_COLORS.unknown;
}

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
    steps = ['ensure_worktree', 'rebase', 'address_review', 'rearrange_commits', 'validate', 'push', 'refresh_pr_body', 'monitor_ci', 'resolve_external_reviews', 'extract_memory', 'handoff_to_reviewer'];
    labels = {ensure_worktree: 'Worktree', rebase: 'Rebase', address_review: 'Address', rearrange_commits: 'Rearrange', validate: 'Validate', push: 'Push', refresh_pr_body: 'PR Body', monitor_ci: 'CI', resolve_external_reviews: 'Resolve', extract_memory: 'Memory', handoff_to_reviewer: 'Handoff'};
  } else if (pipelineVariant === 'researcher') {
    steps = ['fetch_task', 'research', 'spec', 'extract_memory'];
    labels = {fetch_task: 'Fetch', research: 'Research', spec: 'Spec', extract_memory: 'Memory'};
  } else if (pipelineVariant === 'planner') {
    steps = ['scan_project', 'generate_tasks', 'validate_tasks', 'extract_memory'];
    labels = {scan_project: 'Scan', generate_tasks: 'Generate', validate_tasks: 'Validate', extract_memory: 'Memory'};
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

function formatTokenCount(tokens) {
  if (tokens == null) return '--';
  var n = Number(tokens);
  var abs = Math.abs(n);
  if (abs >= 1e9) return (n / 1e9).toFixed(1).replace(/\.0$/, '') + 'B';
  if (abs >= 1e6) return (n / 1e6).toFixed(1).replace(/\.0$/, '') + 'M';
  if (abs >= 1e3) return (n / 1e3).toFixed(1).replace(/\.0$/, '') + 'K';
  return String(n);
}

/* ============================================================
   SOVA STATUS CHANNEL (WebSocket + polling fallback)
   ============================================================ */

/**
 * createSovaStatusChannel(options) -> { stop: function() }
 *
 * Shared real-time status helper: owns a WebSocket connection, a single
 * reconnect policy, and the fallback-to-polling path, so every page wiring
 * up a status feed shares the exact same connection semantics instead of
 * each reimplementing (and subtly disagreeing on) its own.
 *
 * The reconnect/backoff counter resets on a successfully parsed message, not
 * merely on the socket opening, so a connection that flaps (closes and
 * reopens) repeatedly does not accumulate an ever-growing backoff delay from
 * stale earlier attempts. That reset alone does not prove the connection is
 * live: a socket that stays open but stops delivering anything (a dead
 * proxy, a half-open socket after a laptop sleep/network switch, a server
 * hung awaiting a locked DB) never fires onclose on its own, so nothing
 * would otherwise trigger a reconnect. A separate liveness watchdog covers
 * that case: it tracks the time of the last message (or connect) and forces
 * the socket closed once it goes quiet longer than staleAfterMs, which
 * routes into the normal onclose -> fallback/reconnect path.
 * Once MAX_RECONNECT_ATTEMPTS is exhausted, the channel does not give up:
 * the polling fallback also retries the WebSocket on a slow, indefinitely
 * repeating interval (MAX_RECONNECT_DELAY_MS), so a channel left in fallback
 * for hours still recovers once the server comes back, without accumulating
 * a runaway backoff delay.
 *
 * options:
 *   wsPath                API-relative WebSocket path, e.g. apiUrl('/ws/agents/status')
 *   onMessage(msg)         called with the parsed JSON message on every WS push
 *   onOpen()               optional: called every time the socket (re)connects, before
 *                          any message has arrived. For state that can go stale during
 *                          a gap the socket itself cannot narrate (e.g. a run reaching a
 *                          terminal status the periodic push never carries).
 *   onFallbackPoll()       called on an interval while no WS connection is open
 *   pollIntervalMs         fallback polling cadence (default 2000)
 *   staleAfterMs           liveness watchdog threshold: close and reconnect a
 *                          socket that has delivered nothing for this long
 *                          (default max(20000, 10x pollIntervalMs); sized off
 *                          a generous worst case, not off pollIntervalMs
 *                          alone, since the producer's actual inter-message
 *                          gap is its own broadcast interval PLUS however
 *                          long its server-side status fetch takes, which is
 *                          unbounded and independent of the client's poll
 *                          cadence. A caller that knows its server's real
 *                          broadcast interval should pass staleAfterMs
 *                          explicitly rather than rely on this default.
 *   auxiliaryPollFn()      optional: a second, independent interval started and
 *                          stopped alongside this channel's own lifecycle,
 *                          regardless of WS/fallback state
 *   auxiliaryPollIntervalMs  cadence for auxiliaryPollFn (default 5000)
 */
function createSovaStatusChannel(options) {
  var wsPath = options.wsPath;
  var onMessage = options.onMessage;
  var onOpen = options.onOpen || null;
  var onFallbackPoll = options.onFallbackPoll;
  var pollIntervalMs = options.pollIntervalMs || 2000;
  var auxiliaryPollFn = options.auxiliaryPollFn || null;
  var auxiliaryPollIntervalMs = options.auxiliaryPollIntervalMs || 5000;
  var staleAfterMs = options.staleAfterMs || Math.max(20000, pollIntervalMs * 10);

  var RECONNECT_BASE_MS = 1000;
  var MAX_RECONNECT_DELAY_MS = 16000;
  var MAX_RECONNECT_ATTEMPTS = 10;

  var ws = null;
  var reconnectDelay = RECONNECT_BASE_MS;
  var reconnectAttempts = 0;
  var pollInterval = null;
  var fallbackRetryInterval = null;
  var reconnectTimer = null;
  var auxiliaryInterval = null;
  var watchdogInterval = null;
  var lastMessageAt = null;
  var stopped = false;

  function startWatchdog() {
    if (watchdogInterval) return;
    watchdogInterval = setInterval(function() {
      if (ws !== null && lastMessageAt !== null && (Date.now() - lastMessageAt) > staleAfterMs) {
        console.warn('SovaStatusChannel: socket stale, forcing reconnect');
        ws.close();
      }
    }, staleAfterMs);
  }

  function startFallback() {
    if (!pollInterval) {
      pollInterval = setInterval(onFallbackPoll, pollIntervalMs);
    }
    if (!fallbackRetryInterval) {
      fallbackRetryInterval = setInterval(function() {
        if (ws === null) connect();
      }, MAX_RECONNECT_DELAY_MS);
    }
  }

  function stopFallback() {
    if (pollInterval) { clearInterval(pollInterval); pollInterval = null; }
    if (fallbackRetryInterval) { clearInterval(fallbackRetryInterval); fallbackRetryInterval = null; }
  }

  function connect() {
    // Guards against a race between the exponential-backoff retry
    // (reconnectTimer) and the indefinite fallback retry (fallbackRetryInterval)
    // both firing close together and opening two live sockets.
    if (stopped || ws !== null) return;
    var proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    var wsUrl = proto + '//' + location.host + wsPath;
    var socket;
    try {
      socket = new WebSocket(wsUrl);
    } catch (e) {
      console.warn('SovaStatusChannel: connect failed:', e);
      startFallback();
      return;
    }
    ws = socket;
    lastMessageAt = Date.now();

    socket.onopen = function() {
      if (onOpen) {
        try {
          onOpen();
        } catch (e) {
          console.error('SovaStatusChannel: onOpen handler error:', e);
        }
      }
    };

    socket.onmessage = function(event) {
      lastMessageAt = Date.now();
      reconnectDelay = RECONNECT_BASE_MS;
      reconnectAttempts = 0;
      stopFallback();
      var msg;
      try {
        msg = JSON.parse(event.data);
      } catch (e) {
        console.warn('SovaStatusChannel: message parse error:', e);
        return;
      }
      try {
        onMessage(msg);
      } catch (e) {
        console.error('SovaStatusChannel: message handler error:', e);
      }
    };

    socket.onclose = function() {
      if (ws !== socket) return;
      ws = null;
      startFallback();
      reconnectAttempts++;
      if (reconnectAttempts <= MAX_RECONNECT_ATTEMPTS) {
        reconnectTimer = setTimeout(connect, reconnectDelay);
        reconnectDelay = Math.min(reconnectDelay * 2, MAX_RECONNECT_DELAY_MS);
      }
      // Beyond MAX_RECONNECT_ATTEMPTS, fallbackRetryInterval (started above)
      // keeps retrying at MAX_RECONNECT_DELAY_MS instead of stopping.
    };

    socket.onerror = function() {
      if (ws === socket) socket.close();
    };
  }

  connect();
  startWatchdog();
  if (auxiliaryPollFn) {
    auxiliaryInterval = setInterval(auxiliaryPollFn, auxiliaryPollIntervalMs);
  }

  return {
    stop: function() {
      stopped = true;
      if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
      if (auxiliaryInterval) { clearInterval(auxiliaryInterval); auxiliaryInterval = null; }
      if (watchdogInterval) { clearInterval(watchdogInterval); watchdogInterval = null; }
      stopFallback();
      if (ws) { ws.close(); ws = null; }
    }
  };
}
