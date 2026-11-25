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
    teal:    getCSSVar('--ctp-teal'),
    peach:    getCSSVar('--ctp-peach'),
    lavender: getCSSVar('--ctp-lavender'),
    sapphire: getCSSVar('--ctp-sapphire'),
    gray:     getCSSVar('--ctp-overlay0'),
    grayDim:  getCSSVar('--ctp-surface2'),
    surface: getCSSVar('--ctp-surface0'),
    muted:   getCSSVar('--ctp-overlay1'),
    base:      getCSSVar('--ctp-base'),
    mantle:    getCSSVar('--ctp-mantle'),
    crust:     getCSSVar('--ctp-crust'),
    surface1:  getCSSVar('--ctp-surface1'),
    overlay2:  getCSSVar('--ctp-overlay2'),
    subtext0:  getCSSVar('--ctp-subtext0'),
    subtext1:  getCSSVar('--ctp-subtext1'),
    text:      getCSSVar('--ctp-text'),
    sky:       getCSSVar('--ctp-sky'),
    flamingo:  getCSSVar('--ctp-flamingo'),
    pink:      getCSSVar('--ctp-pink'),
    rosewater: getCSSVar('--ctp-rosewater'),
    maroon:    getCSSVar('--ctp-maroon'),
  };
}

// style.css values for every SOVA_COLORS key, used when initColors() has not run or a token is missing from it
// (tests/test_dashboard_component_vocabulary.py keeps this table equal to style.css).
var _SOVA_COLOR_FALLBACK = {
  accent: '#89b4fa', green: '#a6e3a1', greenStrong: '#40a02b', red: '#f38ba8', yellow: '#f9e2af',
  purple: '#cba6f7', teal: '#94e2d5', peach: '#fab387', lavender: '#b4befe', sapphire: '#74c7ec',
  gray: '#6c7086', grayDim: '#585b70', surface: '#313244', muted: '#7f849c',
  base: '#1e1e2e', mantle: '#181825', crust: '#11111b', surface1: '#45475a', overlay2: '#9399b2',
  subtext0: '#a6adc8', subtext1: '#bac2de', text: '#cdd6f4', sky: '#89dceb', flamingo: '#f2cdcd',
  pink: '#f5c2e7', rosewater: '#f5e0dc', maroon: '#eba0ac',
};

// A SOVA_COLORS token as a hex string: never undefined, so an SVG attribute or inline style stays valid.
function sovaColor(key) {
  return (window.SOVA_COLORS && window.SOVA_COLORS[key]) || _SOVA_COLOR_FALLBACK[key] || _SOVA_COLOR_FALLBACK.gray;
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
  // A bodiless or detail-less error would otherwise show only the caller's generic text; the status
  // tells the operator whether the server is erroring (5xx) or rejecting the request (4xx).
  var withStatus = fallback + ' (HTTP ' + res.status + ')';
  try {
    var body = await res.json();
    if (!body.detail) return withStatus;
    if (Array.isArray(body.detail)) {
      return body.detail.map(function (item) {
        return (item && typeof item === 'object' && item.msg) ? item.msg : JSON.stringify(item);
      }).join('; ') || withStatus;
    }
    // A structured detail (budget_exceeded, suitability_blocked) carries its readable text as `message`.
    if (typeof body.detail === 'object' && body.detail.message) return body.detail.message;
    // An object detail without a `message` has no readable text: never render it as '[object Object]'.
    return typeof body.detail === 'string' ? body.detail : withStatus;
  }
  catch (_e) { return withStatus; }
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

// A run whose output is complete, so reading it from line 0 shows everything: TASK_RUN_TERMINAL (sova/core/state.py,
// which _STATUS_TERMINAL copies) minus paused and awaiting_approval, the two states a run resumes from, which can
// still write and so keep streaming. The Work page log viewer and the run page both decide "finished" here.
function sovaOutputFinished(status) {
  return Object.prototype.hasOwnProperty.call(_STATUS_TERMINAL, status) && status !== 'paused' && status !== 'awaiting_approval';
}
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
  resolve_review_threads: 'Resolving threads',
  address_external_findings: 'Addressing findings',
  address_sonarcloud_findings: 'SonarCloud findings',
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

// The system-wide CPU reading: a share of the whole machine, so 100 is every core busy.
// null or undefined is "no reading yet", never 0%.
function formatCpuPercent(value) {
  return value != null ? Number(value).toFixed(1) + '%' : '--';
}

// An agent's CPU reading, given in the API's unit (percent of one core: 100 is one full core, and a multi-core
// process tree exceeds it), shown as the number of cores it keeps busy so it never reads as a share of the
// machine. Under one core keeps two decimals, and a nonzero reading too small for them is "<0.01", not idle.
// null or undefined is "no reading yet", never 0.
function formatCpuCores(value) {
  if (value == null) return '--';
  var cores = Number(value) / 100;
  if (cores > 0 && cores < 0.005) return '<0.01 cores';
  // 0.995 and up rounds to 1.00, so it takes the one-decimal form as 1.0 itself does.
  return cores.toFixed(cores < 0.995 ? 2 : 1) + ' cores';
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

function _isStepLessRunning(status, currentStep, role) {
  return !!role && status === 'running' && (!currentStep || currentStep === 'agent');
}

function _computeBadgeLabel(status, currentStep, role, pipelineVariant, elapsedSeconds) {
  if (_STATUS_TERMINAL[status]) {
    var label = status.replace(/_/g, ' ');
    return label.charAt(0).toUpperCase() + label.slice(1);
  }
  if (!currentStep || currentStep === 'agent') {
    // A run with output but no pipeline steps (a command or custom role) never reports a step: name the action
    // (renderStatusBadge adds the ticking elapsed time) rather than showing "Starting..." for its whole life.
    // Only a running run has started producing output: a pending or queued one has not, so it keeps "Starting...".
    if (!_isStepLessRunning(status, currentStep, role)) return 'Starting...';
    return formatRole(role, pipelineVariant);
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

function renderStatusBadge(status, currentStep, stepIndex, totalSteps, elapsedSeconds, aggregatedLabel, role, pipelineVariant) {
  var colors = _getBadgeColors(status, currentStep);
  var isTerminal = !!_STATUS_TERMINAL[status];
  var isStuck = !isTerminal && elapsedSeconds != null && elapsedSeconds > _STUCK_THRESHOLD_S;
  var stuckClass = isStuck ? ' sova-status-stuck' : '';
  var pulseClass = isTerminal ? '' : ' animate-pulse';

  var label = escapeHtml(aggregatedLabel || _computeBadgeLabel(status, currentStep, role, pipelineVariant, elapsedSeconds));
  // A step-less run's label never changes, so its elapsed time is a ticking span (outside the escaped label), not
  // text frozen at the value of the render.
  if (!aggregatedLabel && elapsedSeconds != null && _isStepLessRunning(status, currentStep, role)) {
    label += ' ' + liveElapsed(elapsedSeconds);
  }

  return '<span class="sova-status-badge ' + colors.bg + ' ' + colors.text + stuckClass + '">' +
    '<span class="sova-status-dot ' + colors.dot + pulseClass + '"></span>' +
    '<span>' + label + '</span>' +
  '</span>';
}

var _elapsedTickerStarted = false;

/* A timer that counts up from elapsedSeconds ago. */
function liveElapsed(elapsedSeconds) {
  if (elapsedSeconds == null) return formatElapsed(0);
  startElapsedTicker();
  var startTs = Date.now() - (elapsedSeconds * 1000);
  return '<span class="sova-status-elapsed" data-start="' + startTs + '">' + formatElapsed(elapsedSeconds) + '</span>';
}

/* How long a running agent has been in its current step and since its last output line, from a status_update
   run, as ticking timers (empty when the run is unknown). Only the facts are shown, with no judgment of them: a
   develop step routinely runs for twenty minutes, and a single long command can print nothing for minutes. Time in
   step shows only when it is a real step time (time_in_step_known); the last-output segment is omitted until the run
   has a stored output line. */
function runActivityHtml(run) {
  if (!run) return '';
  var parts = [];
  if (run.time_in_step_known && run.time_in_step_ms != null) {
    parts.push('<span>' + liveElapsed(Math.round(run.time_in_step_ms / 1000)) + ' in step</span>');
  }
  if (run.last_output_age_seconds != null) {
    parts.push('<span>last output ' + liveElapsed(run.last_output_age_seconds) + ' ago</span>');
  }
  return parts.join('<span class="text-gray-600 mx-1">·</span>');
}

/* A pushed status_update run with its time in step and last output age advanced by the time since receivedAt (a
   Date.now() value), so a redraw or a fallback reload between pushes keeps the timers running instead of restarting
   them from the pushed values. Returns run itself when nothing has elapsed or run is unknown. */
function advanceRunStatus(run, receivedAt) {
  if (!run) return run;
  var elapsedMs = Date.now() - (receivedAt || Date.now());
  if (elapsedMs <= 0) return run;
  var advanced = Object.assign({}, run);
  if (advanced.time_in_step_ms != null) advanced.time_in_step_ms += elapsedMs;
  if (advanced.last_output_age_seconds != null) advanced.last_output_age_seconds += Math.round(elapsedMs / 1000);
  return advanced;
}

/* Shared 1s tick that keeps every .sova-status-elapsed timer on the page current. liveElapsed() starts it, so every
   page that renders a timer gets it without starting a second ticker. */
function tickElapsedTimers() {
  var els = document.querySelectorAll('.sova-status-elapsed');
  if (els.length === 0) return;
  var now = Date.now();
  els.forEach(function(el) {
    var start = parseInt(el.getAttribute('data-start'), 10);
    if (!start) return;
    var secs = Math.max(0, Math.round((now - start) / 1000));
    el.textContent = formatElapsed(secs);
  });
}

/* Registered the first time a page renders a timer (not at load), so a page without one runs no 1s query, and
   once, so every later liveElapsed() call shares the one ticker. */
function startElapsedTicker() {
  if (_elapsedTickerStarted) return;
  _elapsedTickerStarted = true;
  visibilityAwarePoll(tickElapsedTimers, 1000);
}

/* Writes html into container only when key differs from the key of the last write, so a poll that returns the
   same data leaves the DOM alone (focus, caret, scroll, open menus and in-progress selections survive). key is
   any string covering every input that changes html. afterRender(container), when given, runs after each real
   write, for state the markup cannot carry (a client-side filter over the rebuilt nodes). Returns true when the
   DOM was written. The key lives on the container, so one helper serves any container. */
function renderIfChanged(container, html, key, afterRender) {
  if (container._renderKey === key) return false;
  container._renderKey = key;
  container.innerHTML = html;
  if (afterRender) afterRender(container);
  return true;
}

/* ============================================================
   2b. COMPONENT HELPERS (JS twins of the _components.html macros)
   ============================================================ */

// Each table and class string below must equal the macro's in templates/_components.html:
// tests/test_dashboard_component_vocabulary.py renders both and compares them.
var SOVA_BTN_VARIANTS = ['primary', 'success', 'danger', 'warning', 'purple', 'secondary', 'ghost'];
var SOVA_BTN_SIZES = ['xs', 'md', 'icon'];
var SOVA_STAT_SIZES = { md: 'sova-stat-value-md', sm: 'sova-stat-value-sm' };

// ' name="value"' for each entry, both escaped; the caller's attrs never become markup. `skip` names attributes the
// helper owns (class, and type on a button): they are left out here so the element never gets a second, ignored one.
function _attrsHtml(attrs, skip) {
  return Object.keys(attrs || {}).filter(function (k) {
    return k !== 'class' && (skip || []).indexOf(k) === -1;
  }).map(function (k) {
    return ' ' + escapeHtml(k) + '="' + escapeHtml(attrs[k]) + '"';
  }).join('');
}

// The extra classes a caller adds (opts.className plus a stray attrs.class, merged rather than emitted twice),
// as ' a b' or ''. Escaped: a class string is data.
function _extraClass(opts) {
  var extra = [opts.className, opts.attrs && opts.attrs['class']].filter(Boolean).join(' ').trim();
  return extra ? ' ' + escapeHtml(extra) : '';
}

// The component classes for opts.variant and opts.size: sovaBtn()'s, and a button built with DOM calls (sovaConfirm).
function _sovaBtnClass(opts) {
  var variant = SOVA_BTN_VARIANTS.indexOf(opts.variant) !== -1 ? opts.variant : 'primary';
  return 'sova-btn sova-btn-' + variant + (SOVA_BTN_SIZES.indexOf(opts.size) !== -1 ? ' sova-btn-' + opts.size : '');
}

// opts: {variant, size (a SOVA_BTN_SIZES entry), id, type, tooltip, disabled, attrs, iconHtml, className (extra
// classes such as 'ml-auto' or 'hidden', appended after the component's own)}. An unknown variant
// renders as primary and an unknown size as the default. The label is always escaped text. iconHtml is trusted
// markup (an icon() or ICONS svg, never user data) placed before the label; an icon-only ('icon') button passes
// iconHtml, an empty label and an aria-label in attrs.
function sovaBtn(label, opts) {
  opts = opts || {};
  return '<button type="' + escapeHtml(opts.type || 'button') + '" class="' + _sovaBtnClass(opts) + _extraClass(opts) + '"' +
    (opts.id ? ' id="' + escapeHtml(opts.id) + '"' : '') +
    (opts.tooltip ? ' data-tooltip="' + escapeHtml(opts.tooltip) + '"' : '') +
    (opts.disabled ? ' disabled' : '') +
    _attrsHtml(opts.attrs, ['type']) + '>' + (opts.iconHtml || '') + escapeHtml(label) + '</button>';
}

// opts: {color (a state color token, as stateBadgeClasses() takes; unknown renders as gray), attrs, className}.
function badgeHtml(label, opts) {
  opts = opts || {};
  return '<span class="sova-badge ' + stateBadgeClasses(opts.color) + _extraClass(opts) + '"' + _attrsHtml(opts.attrs) + '>' +
    escapeHtml(label) + '</span>';
}

// Plain strings only: both are escaped. A caller that needs markup inside builds its own block.
function emptyStateHtml(title, hint) {
  return '<div class="sova-empty"><p>' + escapeHtml(title) + '</p>' +
    (hint ? '<p class="sova-empty-hint">' + escapeHtml(hint) + '</p>' : '') + '</div>';
}

// The SOVA owl, whole (the owl_avatar macro's twin): the circle is a backdrop and the image is never clipped, so the
// ears stick out past it. size: 'sm' | 'md' | 'lg'; anything else is 'md'.
var SOVA_OWL_SIZES = ['sm', 'md', 'lg'];
function owlAvatarHtml(size) {
  var s = SOVA_OWL_SIZES.indexOf(size) !== -1 ? size : 'md';
  return '<span class="sova-owl sova-owl-' + s + '" aria-hidden="true"><img src="/static/owl-avatar.png" alt=""></span>';
}

// opts: {size ('md' | 'sm'; anything else is the default size), valueClass, detailId, detail (fixed text for the line
// under the value, escaped)}. The value shows '--' until filled; detailId or detail renders the detail line.
function statCardHtml(label, valueId, opts) {
  opts = opts || {};
  var sizeClass = Object.prototype.hasOwnProperty.call(SOVA_STAT_SIZES, opts.size) ? ' ' + SOVA_STAT_SIZES[opts.size] : '';
  var detailLine = opts.detailId || opts.detail
    ? '<p class="sova-stat-detail"' + (opts.detailId ? ' id="' + escapeHtml(opts.detailId) + '"' : '') + '>' +
      escapeHtml(opts.detail || '') + '</p>'
    : '';
  return '<div class="sova-stat"><p class="sova-stat-label">' + escapeHtml(label) + '</p>' +
    '<p class="sova-stat-value' + sizeClass + (opts.valueClass ? ' ' + escapeHtml(opts.valueClass) : '') +
    '" id="' + escapeHtml(valueId) + '">--</p>' + detailLine + '</div>';
}

// A palette colour utility: a text or background colour (`text-accent-red`, `bg-gray-600/30`, `text-white`,
// `text-red-400`) or an accent border colour. Size, alignment and surface classes (`text-xs`, `text-right`,
// `bg-surface-card`) and neutral borders (`border-gray-700/50`, part of a card's shape) are not colours here.
var _COLOR_CLASS_RE = /^(?:(?:text|bg)-(?:accent(?:-[a-z]+)?|white|black|(?:gray|red|green|yellow|blue|orange|sky|purple|teal|pink)-\d{2,3})|border-accent(?:-[a-z]+)?)(?:\/\d{1,3})?$/;

// Swap the colour of an element built by a component (a stat value, a badge): every palette colour class
// (_COLOR_CLASS_RE) is removed and colorClasses ('text-accent-red', or a stateBadgeClasses() pair; '' for none) added,
// so the component's own classes (sova-stat-value and its size, sova-badge, text-xs, ml-2) survive the update a
// wholesale className write would drop.
function setColorClasses(el, colorClasses) {
  if (!el) return;
  Array.prototype.slice.call(el.classList).forEach(function(c) {
    if (_COLOR_CLASS_RE.test(c)) el.classList.remove(c);
  });
  (colorClasses || '').split(/\s+/).filter(Boolean).forEach(function(c) { el.classList.add(c); });
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
    if (banner) banner.classList.toggle('hidden', true);
  });
}

function dismissPermissionBanner() {
  localStorage.setItem('sova-notif-dismissed', '1');
  var banner = document.getElementById('notif-permission-banner');
  if (banner) banner.classList.toggle('hidden', true);
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
      '<p class="text-xs text-gray-500 mt-0.5">' + escapeHtml(new Date().toLocaleTimeString()) + '</p>' +
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
   5. MODAL DIALOGS
   ============================================================ */

/* The one modal component behind every dashboard dialog (#1224). A modal is a `.sova-modal-backdrop` holding one
   `.sova-modal-dialog`: a page keeps its markup in the template with the `hidden` attribute, and sovaConfirm()
   builds one per call. sovaModalOpen() shows it (fade in, role="dialog" and aria-modal, focus inside) and, while
   it is the topmost open modal, gives it the keyboard: Escape closes it, Tab and Shift+Tab cycle inside it. A
   click on the backdrop closes it. Closing fades it out, hides it again and returns focus to whatever had it when
   the modal opened. Enter is never intercepted: it presses the focused button, as anywhere else. */

var _sovaModalStack = [];   // open modals, topmost last: only the topmost one handles keys
var _sovaModalSeq = 0;      // generated ids for aria-labelledby
var _SOVA_MODAL_FADE_MS = 150;  // the backdrop's opacity transition in style.css
var _sovaModalHideTimers = new WeakMap();  // backdrop -> its pending hide after a fade-out
var _sovaModalBound = new WeakSet();        // backdrops whose click handlers are bound

// Focusable elements inside the dialog, limited to what is rendered: a field the modal hides (the Launch
// modal's model picker for a role that has none) must not take focus.
function _sovaModalFocusables(dialog) {
  return Array.prototype.filter.call(dialog.querySelectorAll(
    'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'
  ), function(el) {
    return !el.disabled && el.type !== 'hidden' && el.getClientRects().length > 0;
  });
}

function _sovaModalIndex(backdrop) {
  for (var i = 0; i < _sovaModalStack.length; i++) {
    if (_sovaModalStack[i].backdrop === backdrop) return i;
  }
  return -1;
}

function sovaModalIsOpen(backdrop) {
  return backdrop ? _sovaModalIndex(backdrop) !== -1 : _sovaModalStack.length > 0;
}

// role, aria-modal and a label, set at open so no page has to remember them. The label is the dialog's first
// heading (or the element marked data-modal-title) unless the markup already names one.
function _sovaModalAria(dialog) {
  if (!dialog.getAttribute('role')) dialog.setAttribute('role', 'dialog');
  dialog.setAttribute('aria-modal', 'true');
  if (dialog.getAttribute('aria-labelledby') || dialog.getAttribute('aria-label')) return;
  var heading = dialog.querySelector('[data-modal-title], h1, h2, h3, h4');
  if (!heading) return;
  if (!heading.id) heading.id = 'sova-modal-title-' + (++_sovaModalSeq);
  dialog.setAttribute('aria-labelledby', heading.id);
}

// Closes on a click that both started and ended on the backdrop: selecting text in a field and releasing
// outside the dialog is not a click outside it.
function _sovaModalBindBackdrop(backdrop) {
  if (_sovaModalBound.has(backdrop)) return;
  _sovaModalBound.add(backdrop);
  var pressedOnBackdrop = false;
  backdrop.addEventListener('mousedown', function(e) { pressedOnBackdrop = e.target === backdrop; });
  backdrop.addEventListener('click', function(e) {
    var fromBackdrop = pressedOnBackdrop && e.target === backdrop;
    pressedOnBackdrop = false;
    if (!fromBackdrop) return;
    var i = _sovaModalIndex(backdrop);
    if (i !== -1 && _sovaModalStack[i].opts.closeOnBackdrop !== false) sovaModalClose(backdrop, 'backdrop');
  });
}

/* Opens `backdrop` (a .sova-modal-backdrop element; appended to the body when it is not in the document).
   opts:
     initialFocus  element (or selector inside the dialog) to focus; default the first focusable element
     onClose       function(reason) called once on close: 'escape', 'backdrop', or the reason the page passed
                   to sovaModalClose()
     closeOnBackdrop  false keeps it open on a backdrop click (default true)
     returnFocus      element to focus on close; default the element focused when the modal opened. A modal that
                      opens after an async step passes the trigger it captured before it, since focus may have moved
     removeOnClose    true removes the backdrop from the document once it has faded out (a built-per-call modal)
   Opening a modal that is already open does nothing. */
function sovaModalOpen(backdrop, opts) {
  if (!backdrop || sovaModalIsOpen(backdrop)) return;
  var dialog = backdrop.querySelector('.sova-modal-dialog') || backdrop;
  var entry = { backdrop: backdrop, dialog: dialog, opts: opts || {}, trigger: (opts && opts.returnFocus) || document.activeElement };
  clearTimeout(_sovaModalHideTimers.get(backdrop));
  _sovaModalHideTimers.delete(backdrop);

  _sovaModalAria(dialog);
  _sovaModalBindBackdrop(backdrop);
  if (!backdrop.isConnected) document.body.appendChild(backdrop);
  backdrop.hidden = false;
  void backdrop.offsetWidth;  // lay it out at opacity 0 first, so adding the class fades it in
  backdrop.classList.add('sova-modal-visible');
  _sovaModalStack.push(entry);

  var target = entry.opts.initialFocus;
  if (typeof target === 'string') target = dialog.querySelector(target);
  if (!target) target = _sovaModalFocusables(dialog)[0];
  if (!target) {
    dialog.setAttribute('tabindex', '-1');
    target = dialog;
  }
  target.focus();
}

/* Closes `backdrop` if it is open: fades it out, then hides (or removes) it, returns focus to the element that
   had it on open, and calls the modal's onClose(reason). */
function sovaModalClose(backdrop, reason) {
  var i = _sovaModalIndex(backdrop);
  if (i === -1) return;
  var entry = _sovaModalStack.splice(i, 1)[0];
  backdrop.classList.remove('sova-modal-visible');
  _sovaModalHideTimers.set(backdrop, setTimeout(function() {
    _sovaModalHideTimers.delete(backdrop);
    if (entry.opts.removeOnClose) backdrop.remove();
    else backdrop.hidden = true;
  }, _SOVA_MODAL_FADE_MS));
  var trigger = entry.trigger;
  if (trigger && trigger.isConnected && typeof trigger.focus === 'function') trigger.focus();
  if (entry.opts.onClose) entry.opts.onClose(reason);
}

// Capture phase on the document, so the open modal sees a key before any page handler: Escape closes the modal
// and nothing else (the Work drawer and the feed panel also close on Escape), and Tab never leaves it.
function _sovaModalKeydown(e) {
  var top = _sovaModalStack[_sovaModalStack.length - 1];
  if (!top) return;
  if (e.key === 'Escape') {
    e.preventDefault();
    e.stopPropagation();
    sovaModalClose(top.backdrop, 'escape');
    return;
  }
  if (e.key !== 'Tab') return;
  var items = _sovaModalFocusables(top.dialog);
  if (!items.length) { e.preventDefault(); return; }
  var first = items[0];
  var last = items[items.length - 1];
  var active = document.activeElement;
  if (items.indexOf(active) === -1) {  // focus is outside the dialog, or on something in it that is not focusable
    e.preventDefault();
    (e.shiftKey ? last : first).focus();
  } else if (e.shiftKey && active === first) {
    e.preventDefault();
    last.focus();
  } else if (!e.shiftKey && active === last) {
    e.preventDefault();
    first.focus();
  }
}

document.addEventListener('keydown', _sovaModalKeydown, true);

var _sovaConfirmOpen = false;
var _SOVA_CONFIRM_VARIANTS = { danger: true, warning: true };

/**
 * sovaConfirm(message, options) -> Promise<boolean>
 *
 * options: { title, confirmText, cancelText, confirmClass, details }
 * confirmClass: 'danger' for a destructive action (red), 'warning' for one that proceeds past a known problem
 *   (yellow); anything else is the primary style. Both open with Cancel focused, so an Enter carried over from
 *   the trigger cancels; the primary style opens with Confirm focused.
 * details: plain strings listed under the message (the merge blockers), escaped like the message.
 * One confirm at a time: a call while one is open resolves false at once.
 */
function sovaConfirm(message, options) {
  var opts = options || {};
  if (_sovaConfirmOpen) return Promise.resolve(false);
  _sovaConfirmOpen = true;

  var variant = _SOVA_CONFIRM_VARIANTS[opts.confirmClass] ? opts.confirmClass : 'primary';
  var details = (Array.isArray(opts.details) ? opts.details : []).filter(function(d) {
    return d !== null && d !== undefined && String(d) !== '';
  });
  var id = 'sova-confirm-' + (++_sovaModalSeq);

  // Text goes in with textContent and ids with setAttribute: only the fixed skeleton is markup.
  function button(label, buttonVariant, cls) {
    var b = document.createElement('button');
    b.type = 'button';
    b.className = _sovaBtnClass({ variant: buttonVariant, size: 'md' }) + ' ' + cls;
    b.textContent = label;
    return b;
  }

  return new Promise(function(resolve) {
    var backdrop = document.createElement('div');
    backdrop.className = 'sova-modal-backdrop';
    backdrop.hidden = true;
    backdrop.innerHTML =
      '<div class="sova-modal-dialog" role="alertdialog">' +
        '<div class="px-5 pt-5 pb-4 overflow-y-auto">' +
          '<p class="text-sm font-medium text-gray-200 mb-2">' + escapeHtml(opts.title || 'Confirm') + '</p>' +
          '<p class="text-sm text-gray-400">' + escapeHtml(message) + '</p>' +
        '</div>' +
        '<div class="flex justify-end gap-2 px-5 pb-4"></div>' +
      '</div>';
    var dialog = backdrop.children[0];
    var text = dialog.children[0];
    text.children[0].id = id + '-title';
    text.children[1].id = id + '-message';
    var describedBy = [id + '-message'];
    if (details.length) {
      var list = document.createElement('ul');
      list.id = id + '-details';
      list.className = 'sova-modal-details';
      details.forEach(function(d) {
        var item = document.createElement('li');
        item.textContent = String(d);
        list.appendChild(item);
      });
      text.appendChild(list);
      describedBy.push(list.id);
    }
    dialog.setAttribute('aria-labelledby', id + '-title');
    dialog.setAttribute('aria-describedby', describedBy.join(' '));

    var cancelBtn = button(opts.cancelText || 'Cancel', 'ghost', 'sova-modal-cancel');
    var confirmBtn = button(opts.confirmText || 'Confirm', variant, 'sova-modal-confirm');
    dialog.children[1].appendChild(cancelBtn);
    dialog.children[1].appendChild(confirmBtn);
    cancelBtn.addEventListener('click', function() { sovaModalClose(backdrop, 'cancel'); });
    confirmBtn.addEventListener('click', function() { sovaModalClose(backdrop, 'confirm'); });

    sovaModalOpen(backdrop, {
      initialFocus: variant === 'primary' ? confirmBtn : cancelBtn,
      removeOnClose: true,
      onClose: function(reason) {
        _sovaConfirmOpen = false;
        resolve(reason === 'confirm');
      },
    });
  });
}

/* ============================================================
   6. VISIBILITY-AWARE POLLING
   ============================================================ */

/* Runs fn at once on start and each time the tab becomes visible again, then every intervalMs while the tab
   is visible. With options.skipImmediate the immediate runs are dropped (the first run is one interval after the
   tab is visible), for work that must not fire on page load, such as a write to an external service. */
function visibilityAwarePoll(fn, intervalMs, options) {
  var immediate = !(options && options.skipImmediate);
  var state = { id: null };
  if (!document.hidden) {
    if (immediate) fn();
    state.id = setInterval(fn, intervalMs);
  }
  document.addEventListener('visibilitychange', function() {
    clearInterval(state.id);
    if (document.hidden) {
      state.id = null;
    } else {
      if (immediate) fn();
      state.id = setInterval(fn, intervalMs);
    }
  });
  return state;
}

/* ============================================================
   6b. SHARED SOCKET HUB (one WebSocket per tab) AND STALE MARKERS
   ============================================================ */

/* Every page and the sidebar share one /ws/agents/status socket: subscribeSovaSocket() registers a subscriber, the
   first one opens the socket and the last one to leave closes it. The socket also closes while the tab is hidden
   (and a pending reconnect is cancelled), reopening on return. While it is down each subscriber with an
   onFallbackPoll runs it at once and then every pollIntervalMs (default 30s); a (re)connect stops those polls and
   runs each subscriber's onOpen, which is its catch-up fetch.

   options:
     onMessage(msg)       every parsed message, for every subscriber
     onOpen()             the socket (re)opened, or this subscriber joined a socket that is already open
     onFallbackPoll()     polled while the socket is down (and the tab visible)
     pollIntervalMs       fallback cadence (default SOVA_SOCKET_FALLBACK_MS)
     freshnessKey         markRefresh(key, true) on each message whose type is in freshnessTypes: only the type that
                          carries this subscriber's own data proves that data is current
     freshnessTypes       message types that count as fresh (default ['status_update']); a feed, activity or
                          *_unavailable message never refreshes a key it does not carry data for
   Returns { unsubscribe: function }. */
var SOVA_SOCKET_FALLBACK_MS = 30000;
var _SOCKET_RECONNECT_BASE_MS = 1000;
var _SOCKET_RECONNECT_MAX_MS = 16000;
// The server pushes a status_update every 2s, so a socket silent for this long is dead (half-open after a sleep or
// network switch) even though it never fired onclose; closing it routes into the normal reconnect path.
var _SOCKET_WATCHDOG_MS = 20000;
// How often the watchdog checks, so a silent socket is closed 20 to 25s after its last message, not up to 40s.
var _SOCKET_WATCHDOG_TICK_MS = 5000;

var _socketHub = {
  subs: [],
  ws: null,               // non-null from construction until close, so it is also the single pending-connect flag
  reconnectTimer: null,
  reconnectDelay: _SOCKET_RECONNECT_BASE_MS,
  watchdog: null,
  lastMessageAt: null,
  visibilityBound: false
};

function subscribeSovaSocket(options) {
  var hub = _socketHub;
  var sub = {
    onMessage: options.onMessage || null,
    onOpen: options.onOpen || null,
    onFallbackPoll: options.onFallbackPoll || null,
    pollIntervalMs: options.pollIntervalMs || SOVA_SOCKET_FALLBACK_MS,
    freshnessKey: options.freshnessKey || null,
    freshnessTypes: options.freshnessTypes || ['status_update'],
    pollTimer: null
  };
  hub.subs.push(sub);
  if (!hub.visibilityBound) {
    hub.visibilityBound = true;
    document.addEventListener('visibilitychange', _socketOnVisibility);
  }
  if (hub.ws !== null && hub.ws.readyState === 1) {
    // Joining an open socket: no open event is coming, so run the catch-up fetch now.
    setTimeout(function() { _socketCall(sub.onOpen); }, 0);
  } else if (hub.ws === null) {
    _socketConnect();
    // No socket and none being opened (a hidden tab): nothing arrives, so a visible tab polls until one opens.
    if (hub.ws === null && !document.hidden) _socketStartFallback(sub);
  }
  return {
    unsubscribe: function() {
      var i = hub.subs.indexOf(sub);
      if (i === -1) return;
      hub.subs.splice(i, 1);
      _socketStopFallback(sub);
      if (hub.subs.length === 0) _socketSuspend();
    }
  };
}

/* Sends one JSON message on the shared socket; false when it is not open (the page sends again from its onOpen). */
function sendSovaSocket(message) {
  var ws = _socketHub.ws;
  if (ws === null || ws.readyState !== 1) return false;
  try {
    ws.send(JSON.stringify(message));
    return true;
  } catch (e) {
    console.warn('SovaSocket: send failed:', e);
    return false;
  }
}

function _socketCall(fn, arg) {
  if (!fn) return;
  try {
    fn(arg);
  } catch (e) {
    console.error('SovaSocket: subscriber error:', e);
  }
}

function _socketStartFallback(sub) {
  if (!sub.onFallbackPoll || sub.pollTimer) return;
  _socketCall(sub.onFallbackPoll);
  sub.pollTimer = setInterval(function() { _socketCall(sub.onFallbackPoll); }, sub.pollIntervalMs);
}

function _socketStopFallback(sub) {
  if (sub.pollTimer) { clearInterval(sub.pollTimer); sub.pollTimer = null; }
}

function _socketConnect() {
  var hub = _socketHub;
  if (hub.ws !== null || hub.subs.length === 0 || document.hidden) return;
  if (hub.reconnectTimer) { clearTimeout(hub.reconnectTimer); hub.reconnectTimer = null; }
  var proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  var socket;
  try {
    socket = new WebSocket(proto + '//' + location.host + apiUrl('/ws/agents/status'));
  } catch (e) {
    console.warn('SovaSocket: connect failed:', e);
    _socketScheduleReconnect();
    hub.subs.forEach(_socketStartFallback);
    return;
  }
  hub.ws = socket;
  hub.lastMessageAt = Date.now();
  if (!hub.watchdog) {
    hub.watchdog = setInterval(function() {
      if (hub.ws !== null && (Date.now() - hub.lastMessageAt) > _SOCKET_WATCHDOG_MS) {
        console.warn('SovaSocket: socket silent, forcing reconnect');
        hub.ws.close();
      }
    }, _SOCKET_WATCHDOG_TICK_MS);
  }

  socket.onopen = function() {
    if (hub.ws !== socket) return;
    hub.lastMessageAt = Date.now();
    hub.subs.forEach(_socketStopFallback);
    hub.subs.slice().forEach(function(sub) { _socketCall(sub.onOpen); });
  };

  socket.onmessage = function(event) {
    if (hub.ws !== socket) return;
    hub.lastMessageAt = Date.now();
    hub.reconnectDelay = _SOCKET_RECONNECT_BASE_MS;
    var msg;
    try {
      msg = JSON.parse(event.data);
    } catch (e) {
      console.warn('SovaSocket: message parse error:', e);
      return;
    }
    hub.subs.slice().forEach(function(sub) {
      // Only a message of the subscriber's own data type is proof of fresh data: an unrelated message (a feed event,
      // an activity change) or a *_unavailable failure says the socket is up, not that this value is current.
      if (sub.freshnessKey && sub.freshnessTypes.indexOf(msg.type) !== -1) markRefresh(sub.freshnessKey, true);
      _socketCall(sub.onMessage, msg);
    });
  };

  socket.onclose = function() {
    if (hub.ws !== socket) return;
    hub.ws = null;
    hub.subs.forEach(_socketStartFallback);
    _socketScheduleReconnect();
  };

  socket.onerror = function() {
    if (hub.ws === socket) socket.close();
  };
}

function _socketScheduleReconnect() {
  var hub = _socketHub;
  if (hub.reconnectTimer || document.hidden || hub.subs.length === 0) return;
  hub.reconnectTimer = setTimeout(function() {
    hub.reconnectTimer = null;
    _socketConnect();
  }, hub.reconnectDelay);
  hub.reconnectDelay = Math.min(hub.reconnectDelay * 2, _SOCKET_RECONNECT_MAX_MS);
}

/* Closes the socket and stops every timer; the reconnect and the polls resume on return (visible) or on subscribe. */
function _socketSuspend() {
  var hub = _socketHub;
  if (hub.reconnectTimer) { clearTimeout(hub.reconnectTimer); hub.reconnectTimer = null; }
  if (hub.watchdog) { clearInterval(hub.watchdog); hub.watchdog = null; }
  hub.subs.forEach(_socketStopFallback);
  var socket = hub.ws;
  hub.ws = null;   // before close(), so the close handler ignores this deliberate close
  if (socket) socket.close();
}

function _socketOnVisibility() {
  var hub = _socketHub;
  if (document.hidden) {
    _socketSuspend();
    return;
  }
  hub.reconnectDelay = _SOCKET_RECONNECT_BASE_MS;
  _socketConnect();
  if (hub.ws === null) hub.subs.forEach(_socketStartFallback);
}

/* Stale marker: a refresh that keeps failing, or a channel that stops delivering, must not leave old data looking
   current. markRefresh(key, ok, staleAfterMs) records an outcome for the channel named key; an element with id
   key + '-stale' shows "Updated 45s ago, retrying" as soon as a refresh has failed (markRefresh(key, false)) and
   until the next success, and also once the last success is older than staleAfterMs (default 90s, one rebuild
   period plus build time), which is how a channel that merely goes silent is caught. A channel that never
   succeeded shows nothing: the page's own load error covers that. */
var _refreshMarks = {};
var _refreshMarkTimer = null;
var _REFRESH_MARK_TICK_MS = 5000;

function markRefresh(key, ok, staleAfterMs) {
  var mark = _refreshMarks[key];
  if (!mark) mark = _refreshMarks[key] = { lastOk: null, failed: false, staleAfterMs: 90000 };
  if (staleAfterMs) mark.staleAfterMs = staleAfterMs;
  if (ok) mark.lastOk = Date.now();
  mark.failed = !ok;
  _renderRefreshMark(key);
  if (!_refreshMarkTimer) {
    _refreshMarkTimer = setInterval(function() {
      Object.keys(_refreshMarks).forEach(_renderRefreshMark);
    }, _REFRESH_MARK_TICK_MS);
  }
}

function _renderRefreshMark(key) {
  var el = document.getElementById(key + '-stale');
  var mark = _refreshMarks[key];
  if (!el || !mark) return;
  var age = mark.lastOk === null ? 0 : Date.now() - mark.lastOk;
  // Silence alone is not a failure while the tab is hidden: its polls and socket are suspended on purpose, and the
  // refresh on return settles the marker. An explicit failure still shows.
  var silent = !document.hidden && age > mark.staleAfterMs;
  var stale = mark.lastOk !== null && (mark.failed || silent);
  el.classList.toggle('hidden', !stale);
  if (stale) el.textContent = 'Updated ' + formatElapsed(age / 1000) + ' ago, retrying';
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
   6b. SETTING CONTROLS (settingSwitch, settingStepper)
   ============================================================ */

/* One on/off switch or one number, bound to a setting key and saved through
   POST /settings/config, so a page can change a setting without opening
   Settings. Both controls show a pending state while the save runs, read the
   stored value back afterwards, and on failure return to the stored value and
   show the server's message, whether it came as an HTTP error's `detail` or as
   a 200 answer's `error`. Options shared by both:
     label     accessible name, also used in messages
     value     the current value; omitted, the control loads it itself
     tooltip   data-tooltip text (wraps; opens to the left unless tooltipPos says otherwise)
     onSaved   called with the stored value after a successful save, and with the whole save result
               ({value, restartRequired, lifecycleError, warning}) as its second argument
     onError   called with the message after a failed save (also toasted)
     onValue   called with the shown value whenever it changes */

var _settingValues = null;
var _settingControls = {};  // key -> every control on the page bound to it
var _settingSaves = 0;      // saves completed on this page

/* How many saves have completed. A poll that reads a setting remembers it before the fetch and drops its
   value for that setting when it changed, since the response may predate the save. */
function settingSaveCount() { return _settingSaves; }

function _registerSettingControl(key, root) {
  (_settingControls[key] = _settingControls[key] || []).push(root);
}

/* The flat project config (GET /settings/config), shared by every control on
   the page. `force` re-reads it, as each save does to read its value back. */
function loadSettingValues(force) {
  if (!_settingValues || force) {
    _settingValues = fetch(apiUrl('/settings/config')).then(function (res) {
      if (!res.ok) throw new Error('HTTP ' + res.status);
      return res.json();
    }).then(function (body) {
      // GET /settings/config answers 200 with {config: {_error}} when the config cannot be loaded: that is a
      // failed load, never a set of values to cache.
      var cfg = body.config || {};
      if (cfg._error) throw new Error(String(cfg._error));
      return cfg;
    });
    _settingValues.catch(function () { _settingValues = null; });
  }
  return _settingValues;
}

/* Saves of one key run one after another, so two quick toggles cannot have their read-backs answered out of order
   and leave the page showing the first value while the database holds the second. */
var _settingSaveQueue = {};

/* Saves one key. Resolves to {ok: true, value, restartRequired, lifecycleError, warning}
   with the value as stored, or {ok: false, message}; never rejects. */
function saveSetting(key, value) {
  var previous = _settingSaveQueue[key] || Promise.resolve();
  var run = previous.then(function () { return _saveSettingNow(key, value); });
  var tail = run.catch(function () {});
  _settingSaveQueue[key] = tail;
  tail.then(function () { if (_settingSaveQueue[key] === tail) delete _settingSaveQueue[key]; });
  return run;
}

async function _saveSettingNow(key, value) {
  var res;
  try {
    res = await fetch(apiUrl('/settings/config'), {
      method: 'POST',
      headers: jsonCsrfHeaders(),
      body: JSON.stringify({key: key, value: value}),
    });
  } catch (_e) {
    return {ok: false, message: 'Could not reach the dashboard to save ' + key};
  }
  if (!res.ok) return {ok: false, message: await getErrorDetail(res, 'Could not save ' + key)};
  var body = await res.json().catch(function () { return {}; });
  if (body.error) return {ok: false, message: String(body.error)};
  var stored = value;
  try {
    var cfg = await loadSettingValues(true);
    if (Object.prototype.hasOwnProperty.call(cfg, key)) stored = cfg[key];
  } catch (_e) { /* the save succeeded; show the value that was sent */ }
  _settingSaves++;
  return {
    ok: true,
    value: stored,
    restartRequired: !!body.restart_required,
    lifecycleError: body.lifecycle_error ? String(body.lifecycle_error) : '',
    warning: body.warning ? String(body.warning) : '',
  };
}

/* Controls sit at the right edge of a row or header, so their tooltip wraps and opens to the left by default:
   a centered nowrap tooltip's hidden box would reach past the edge and widen the page. */
function _settingTooltip(root, opts) {
  if (!opts.tooltip) return;
  root.setAttribute('data-tooltip', opts.tooltip);
  root.setAttribute('data-tooltip-wide', '');
  root.setAttribute('data-tooltip-pos', opts.tooltipPos || 'left');
}

/* Puts back the tooltip the control was built with, after a failed load replaced it with the failure. */
function _settingTooltipReset(root, opts) {
  if (opts.tooltip) {
    _settingTooltip(root, opts);
    return;
  }
  ['data-tooltip', 'data-tooltip-wide', 'data-tooltip-pos'].forEach(function (a) { root.removeAttribute(a); });
}

function _settingPending(root, controls, pending) {
  root.classList.toggle('sova-setting-pending', pending);
  if (pending) root.setAttribute('aria-busy', 'true');
  else root.removeAttribute('aria-busy');
  controls.forEach(function (c) { c.disabled = pending; });
}

function _settingReport(key, result, opts) {
  if (!result.ok) {
    showToast(result.message, 'error');
    if (opts.onError) opts.onError(result.message);
    return;
  }
  if (result.lifecycleError) showToast(result.lifecycleError, 'warning');
  if (result.restartRequired) showToast((opts.label || key) + ' saved. Restart required: restart the server to apply it.', 'warning');
  // Another control for the same key (the Settings Overview repeats rows) shows the new value too. A control no
  // longer on the page (a re-rendered row) is dropped from the registry instead.
  var live = (_settingControls[key] || []).filter(function (c) { return c.isConnected !== false; });
  _settingControls[key] = live;
  live.forEach(function (c) { if (c !== opts._root) c.sovaSetValue(result.value); });
  if (opts.onSaved) opts.onSaved(result.value, result);
}

/* An on/off switch (.sova-toggle) for a boolean setting. Returns the <label>;
   its sovaSetValue(v) updates the shown value from outside (e.g. a poll). */
function settingSwitch(key, opts) {
  opts = opts || {};
  var root = document.createElement('label');
  root.className = 'sova-toggle sova-setting-switch';
  root.dataset.settingKey = key;
  _settingTooltip(root, opts);
  var input = document.createElement('input');
  input.type = 'checkbox';
  input.setAttribute('aria-label', opts.label || key);
  var track = document.createElement('span');
  track.className = 'sova-toggle-track';
  root.appendChild(input);
  root.appendChild(track);

  var stored = false;
  var deferred = null;  // a value another control or a poll reported while this one was saving
  var loadFailed = false;
  var label = opts.label || key;
  function show(v) {
    stored = v === true || v === 'true';
    input.checked = stored;
    if (opts.onValue) opts.onValue(stored);
  }
  root.sovaSetValue = function (v) {
    if (root.classList.contains('sova-setting-pending')) deferred = {value: v};
    else show(v);
  };
  _registerSettingControl(key, root);
  opts = Object.assign({}, opts, {_root: root});

  // A failed load leaves the switch focusable (aria-disabled, not disabled) with the failure in its accessible
  // name, so a keyboard user hears it; activating it tries the load again.
  function load(force) {
    input.disabled = true;
    loadSettingValues(force).then(function (cfg) {
      if (loadFailed) _settingTooltipReset(root, opts);
      loadFailed = false;
      input.removeAttribute('aria-disabled');
      input.setAttribute('aria-label', label);
      show(cfg[key]);
      input.disabled = false;
    }, function () {
      loadFailed = true;
      input.disabled = false;
      input.setAttribute('aria-disabled', 'true');
      input.setAttribute('aria-label', label + ': could not load ' + key + '. Activate to try again.');
      input.checked = false;
      _settingTooltip(root, {tooltip: 'Could not load ' + key + '. Click to try again.', tooltipPos: opts.tooltipPos});
    });
  }

  if (opts.value !== undefined) {
    show(opts.value);
  } else {
    load(false);
  }

  input.addEventListener('change', async function () {
    if (loadFailed) {
      input.checked = false;
      load(true);
      return;
    }
    var wanted = input.checked;
    deferred = null;
    _settingPending(root, [input], true);
    var result = await saveSetting(key, wanted);
    _settingPending(root, [input], false);
    if (result.ok) show(result.value);
    else if (deferred) show(deferred.value);
    else input.checked = stored;
    deferred = null;
    _settingReport(key, result, opts);
  });
  return root;
}

/* A number with minus and plus buttons for a numeric setting. Extra options:
     min, max  bounds; the button that would cross one is disabled
     step      change per click (default 1)
     format    turns the value into the shown text (default String)
   Accepts the value as a number or a numeric string (a Decimal setting such as
   agent.max_issue_budget arrives as '75'). Returns the wrapper element; its
   sovaSetValue(v) updates the shown value from outside. */
function settingStepper(key, opts) {
  opts = opts || {};
  var step = opts.step || 1;
  var format = opts.format || function (v) { return String(v); };
  var root = document.createElement('div');
  root.className = 'sova-stepper';
  root.dataset.settingKey = key;
  root.setAttribute('role', 'group');
  root.setAttribute('aria-label', opts.label || key);
  _settingTooltip(root, opts);
  var minus = document.createElement('button');
  minus.type = 'button';
  minus.className = 'sova-stepper-btn';
  minus.setAttribute('aria-label', 'Decrease ' + (opts.label || key));
  minus.textContent = '−';
  var output = document.createElement('output');
  output.className = 'sova-stepper-value';
  output.setAttribute('aria-live', 'polite');
  var plus = document.createElement('button');
  plus.type = 'button';
  plus.className = 'sova-stepper-btn';
  plus.setAttribute('aria-label', 'Increase ' + (opts.label || key));
  plus.textContent = '+';
  root.appendChild(minus);
  root.appendChild(output);
  root.appendChild(plus);

  var stored = null;
  var deferred = null;  // a value another control or a poll reported while this one was saving
  var loadFailed = false;
  var label = opts.label || key;
  function canMove(delta) {
    if (stored === null) return false;
    var next = stored + delta;
    if (opts.min !== undefined && next < opts.min) return false;
    if (opts.max !== undefined && next > opts.max) return false;
    return true;
  }
  function syncButtons() {
    minus.disabled = !canMove(-step);
    // After a failed load the plus button stays focusable and retries the load.
    plus.disabled = loadFailed ? false : !canMove(step);
  }
  function show(v) {
    var n = Number(v);
    stored = (v === null || v === undefined || v === '' || isNaN(n)) ? null : n;
    output.textContent = stored === null ? '--' : format(stored);
    syncButtons();
    if (opts.onValue && stored !== null) opts.onValue(stored);
  }
  root.sovaSetValue = function (v) {
    if (root.classList.contains('sova-setting-pending')) deferred = {value: v};
    else show(v);
  };
  _registerSettingControl(key, root);
  opts = Object.assign({}, opts, {_root: root});

  // A failed load shows '--' and makes the plus button a retry whose accessible name says what failed.
  function load(force) {
    loadSettingValues(force).then(function (cfg) {
      if (loadFailed) _settingTooltipReset(root, opts);
      loadFailed = false;
      plus.setAttribute('aria-label', 'Increase ' + label);
      show(cfg[key]);
    }, function () {
      loadFailed = true;
      plus.setAttribute('aria-label', label + ': could not load ' + key + '. Activate to try again.');
      _settingTooltip(root, {tooltip: 'Could not load ' + key + '. Click + to try again.', tooltipPos: opts.tooltipPos});
      syncButtons();
    });
  }

  if (opts.value !== undefined) {
    show(opts.value);
  } else {
    show(null);
    load(false);
  }

  async function move(delta) {
    if (loadFailed) {
      load(true);
      return;
    }
    if (!canMove(delta)) return;
    var wanted = stored + delta;
    output.textContent = format(wanted);
    deferred = null;
    _settingPending(root, [minus, plus], true);
    var result = await saveSetting(key, wanted);
    _settingPending(root, [minus, plus], false);
    // A failed save returns to what the server last reported: the value shown before the click, unless a
    // sibling control or a poll reported a newer one while this save was in flight.
    show(result.ok ? result.value : (deferred ? deferred.value : stored));
    deferred = null;
    _settingReport(key, result, opts);
  }
  minus.addEventListener('click', function () { return move(-step); });
  plus.addEventListener('click', function () { return move(step); });
  return root;
}

/* Turns placeholders in `root` into controls, so a template can declare them:
     <span data-setting-switch="supervisor.auto_triage" data-label="Auto-triage" data-tooltip="..."
           data-tooltip-pos="bottom"></span>
     <span data-setting-stepper="agent.max_issue_budget" data-label="..." data-min="5" data-step="5"
           data-max="..." data-prefix="$"></span>
   `overrides` maps a key to extra options (value, onSaved, onValue, ...). A
   placeholder is mounted once, however often this runs. Returns {key: control}
   for the controls it created. */
function mountSettingControls(root, overrides) {
  root = root || document;
  overrides = overrides || {};
  var made = {};
  Array.prototype.forEach.call(root.querySelectorAll('[data-setting-switch], [data-setting-stepper]'), function (host) {
    var d = host.dataset;
    if (d.settingMounted) return;
    var key = d.settingSwitch || d.settingStepper;
    var opts = {label: d.label || key};
    if (d.tooltip) opts.tooltip = d.tooltip;
    if (d.tooltipPos) opts.tooltipPos = d.tooltipPos;
    // The tooltip moves onto the control: left on the placeholder too, both would show at once.
    host.removeAttribute('data-tooltip');
    host.removeAttribute('data-tooltip-pos');
    if (d.settingStepper) {
      if (d.min !== undefined) opts.min = Number(d.min);
      if (d.max !== undefined) opts.max = Number(d.max);
      if (d.step !== undefined) opts.step = Number(d.step);
      if (d.prefix) { var prefix = d.prefix; opts.format = function (v) { return prefix + v; }; }
    }
    opts = Object.assign(opts, overrides[key] || {});
    var control = d.settingSwitch ? settingSwitch(key, opts) : settingStepper(key, opts);
    host.appendChild(control);
    d.settingMounted = '1';
    made[key] = control;
  });
  return made;
}

/* ============================================================
   6c. ACTION STATE (one helper for every action's running and result state)
   ============================================================ */

/* An action button shows at once that its action runs, and its row (a card, a list row) shows how it ended:
   a success briefly, an error until dismissed. A toast is never the only signal. State is kept per row key
   ('issue:42', 'pr:7'), outside the DOM, so a re-render keeps it, and the server's list of the actions it is
   running (GET /work/actions, setServerActions) is merged in, so a reload keeps a long action such as a
   rebase on its row too.

     runTrackedAction(key, label, fn)  marks the row running with `label`, awaits fn(); fn resolves to the
                                       success message (or nothing) and rejects with an Error whose message
                                       is shown. Resolves to {ok, message}; never rejects.
     actionStateFor(key)               {status: 'running'|'done'|'failed', label, message} or null; a server
                                       failure may also carry `fix`, the work item action that remedies it
     actionIsRunning(key)              whether the row's buttons should be disabled
     actionStatusHtml(key)             the row's status line ('' when there is nothing to show)
     setServerActions(actions)         the server's list; a running server action shows like a local one
     actionKey(issue, pr, run)         the row key for an issue (or a PR without one, or a run with neither)
     onActionStateChange(fn)           called after every change, so the page redraws its rows */

var ACTION_SUCCESS_SHOW_MS = 6000;
var _localActions = {};    // key -> {status, label, message, at (ms)}
var _serverActions = {};   // key -> {status, label, message, at (ms), id, failedIds}
var _actionStateListeners = [];

function actionKey(issue, pr, run) {
  if (issue !== null && issue !== undefined && issue !== '') return 'issue:' + issue;
  if (pr !== null && pr !== undefined && pr !== '') return 'pr:' + pr;
  // A run with neither (a planner): keyed by the run, so its Stop shows its pending state and any refusal.
  if (run !== null && run !== undefined && run !== '') return 'run:' + run;
  return '';
}

function onActionStateChange(fn) { _actionStateListeners.push(fn); }

function _actionStateChanged() {
  _actionStateListeners.forEach(function (fn) {
    try {
      fn();
    } catch (e) {
      console.error('action state listener failed', e);
    }
  });
}

function _actionShown(state, now) {
  if (!state) return false;
  if (state.status !== 'done') return true;
  return now - (state.endedAt || state.at) < ACTION_SUCCESS_SHOW_MS;
}

function _actionEnded(state) { return state.endedAt || state.at; }

function actionStateFor(key) {
  if (!key) return null;
  var now = Date.now();
  var local = _localActions[key];
  var server = _serverActions[key];
  if (local && local.status === 'running') return local;
  // A server action still running (a background rebase) outranks the click's own result, which only says the
  // request was accepted: the row stays running, and its buttons disabled, until the action ends.
  if (server && server.status === 'running') return server;
  var shown = [local, server].filter(function (s) { return _actionShown(s, now); });
  if (!shown.length) return null;
  // The one that finished last, both on the same basis (when it ended, else when it started): a reload keeps
  // only the server's, and a click's failure must not outlast a later server result such as a finished rebase.
  return shown.reduce(function (a, b) { return (_actionEnded(b) > _actionEnded(a)) ? b : a; });
}

function actionIsRunning(key) {
  var state = actionStateFor(key);
  return !!(state && state.status === 'running');
}

function setServerActions(actions) {
  var next = {};
  (actions || []).forEach(function (a) {
    var key = actionKey(a.issue, a.pr);
    if (!key) return;
    var state = {
      status: a.status,
      label: a.label || '',
      message: a.message || '',
      at: (a.started_at || 0) * 1000,
      endedAt: a.ended_at ? a.ended_at * 1000 : null,
      id: a.id,
      // A failure the server knows how to remedy (a failed rebase) names the work item action that fixes it.
      fix: a.status === 'failed' && a.fix ? a.fix : null,
    };
    // The newest action per row; one still running wins over an older one that ended.
    var prev = next[key];
    // Every failure on the row, so one Dismiss clears them all rather than revealing the next older one.
    state.failedIds = (prev ? prev.failedIds : []).concat(a.status === 'failed' && a.id ? [a.id] : []);
    if (!prev || prev.status !== 'running' && (state.status === 'running' || state.at > prev.at)) next[key] = state;
    else prev.failedIds = state.failedIds;
  });
  _serverActions = next;
  _actionStateChanged();
}

async function runTrackedAction(key, label, fn) {
  _localActions[key] = { status: 'running', label: label, message: '', at: Date.now() };
  _actionStateChanged();
  var result;
  try {
    var message = await fn();
    result = { ok: true, message: message || '' };
  } catch (e) {
    result = { ok: false, message: (e && e.message) || String(e) || 'Failed' };
  }
  var now = Date.now();
  _localActions[key] = { status: result.ok ? 'done' : 'failed', label: label, message: result.message, at: now, endedAt: now };
  _actionStateChanged();
  if (result.ok) {
    // Redraw once more when the success has been shown long enough, so it leaves the row on its own.
    setTimeout(_actionStateChanged, ACTION_SUCCESS_SHOW_MS + 50);
  }
  return result;
}

async function dismissActionState(key) {
  delete _localActions[key];
  var server = _serverActions[key];
  delete _serverActions[key];
  _actionStateChanged();
  var ids = server ? (server.failedIds || []).slice() : [];
  if (server && server.id && server.status !== 'running' && ids.indexOf(server.id) === -1) ids.push(server.id);
  await Promise.all(ids.map(async function (id) {
    try {
      await fetch(apiUrl('/work/actions/' + encodeURIComponent(id)), { method: 'DELETE', headers: jsonCsrfHeaders() });
    } catch (_e) { /* the server drops a dismissed result on its own when the dashboard restarts */ }
  }));
}

function actionStatusHtml(key) {
  var state = actionStateFor(key);
  if (!state) return '';
  if (state.status === 'running') {
    return '<span class="sova-action-status flex items-center gap-1.5 text-xs text-accent-yellow" role="status">' +
      '<span class="w-3 h-3 rounded-full border-2 border-accent-yellow/30 border-t-accent-yellow animate-spin shrink-0"></span>' +
      escapeHtml(state.label) + '...</span>';
  }
  if (state.status === 'done') {
    return '<span class="sova-action-status flex items-center gap-1.5 text-xs text-accent-green" role="status">' +
      '<span class="w-1.5 h-1.5 rounded-full bg-accent-green shrink-0"></span>' +
      escapeHtml(state.message || 'Done') + '</span>';
  }
  return '<span class="sova-action-status flex items-start gap-1.5 text-xs text-accent-red" role="alert">' +
    '<span class="min-w-0 break-words">' + escapeHtml(state.message || 'Failed') + '</span>' +
    '<button type="button" class="ml-auto shrink-0 text-gray-500 hover:text-gray-300 transition-colors" ' +
    'data-sova-dismiss-action="' + escapeHtml(key) + '" aria-label="Dismiss this error">Dismiss</button></span>';
}

// The dismiss button lives in markup any page renders, so it is handled here, not by a page's dispatcher. The
// listener stops the click so the row under it (a selectable card) does not also react.
document.addEventListener('click', function (e) {
  var btn = e.target && e.target.closest ? e.target.closest('[data-sova-dismiss-action]') : null;
  if (!btn) return;
  e.stopPropagation();
  dismissActionState(btn.getAttribute('data-sova-dismiss-action'));
}, true);

/* ============================================================
   7. SIDEBAR POLLING & NOTIFICATIONS
   ============================================================ */

// The sidebar dot combines two pushes that now arrive independently and only on change: a handoff awaiting action
// (yellow) outranks the running state, so an activity change must not paint over it until the handoff clears.
var _handoffAwaiting = false;
var _activityRunning;           // undefined until the first activity answer; null when the last one failed

function _dotClass(color, animate) {
  return 'absolute -bottom-0.5 -right-0.5 w-3 h-3 rounded-full ' + color + ' border-2 border-sidebar' + (animate ? ' animate-pulse' : '');
}

var _connectivityPollInterval = null;
var _restartPollInterval = null;

function startSidebarPolling() {
  _pollConnectivity();
  _pollRestartRequired();
  _connectivityPollInterval = setInterval(_pollConnectivity, 15000);
  _restartPollInterval = setInterval(_pollRestartRequired, 15000);
  _initSidebarSocket();
}

// Activity, handoff and feed events arrive over the shared socket. A (re)connect, and each fallback poll while the
// socket is down, fetches the current activity and handoff and fills any feed gap.
function _initSidebarSocket() {
  subscribeSovaSocket({
    onMessage: _handleSidebarMessage,
    onOpen: _catchUpSidebar,
    onFallbackPoll: _catchUpSidebar
  });
}

function _catchUpSidebar() {
  _pollActivity();
  _pollHandoff();
  if (_feedLastId > 0) _feedGapFill();
}

// Bumped whenever a push is applied. A catch-up fetch remembers the value when it starts and drops its answer if a
// push landed meanwhile: the slower HTTP response would otherwise overwrite newer pushed state with older data,
// and the server sends nothing more until the value changes again.
var _activitySeq = 0;
var _handoffSeq = 0;

function _handleSidebarMessage(msg) {
  if (msg.type === 'activity') { _activitySeq++; _applyActivity(msg.data); }
  else if (msg.type === 'activity_unavailable') { _activitySeq++; _setActivityDot(null); }
  else if (msg.type === 'handoff') { _handoffSeq++; _applyHandoff(msg.data); }
  // The server could not rebuild the handoff: mark it unknown here rather than refetch, which would make every open
  // tab hit the failing dependency at once and could be answered from the last good result. The next good push or
  // catch-up clears it.
  else if (msg.type === 'handoff_unavailable') { _handoffSeq++; _setHandoffUnknown(); }
  else if (msg.type === 'handoff_changed') _pollHandoff();
  else if (msg.type === 'feed') _feedAddEvent(msg.event);
  else if (msg.type === 'status_update') _touchActivity();
}

// The producer is alive and sends an activity push only on change, so each status_update proves the held activity
// value is still current: refresh its timestamp (meaning "last verified") unless its last answer was a failure.
function _touchActivity() {
  if (window._sovaActiveAgents && _activityRunning !== null && _activityRunning !== undefined) {
    window._sovaActiveAgents.ts = Date.now();
  }
}

// Restart banner
//
// /api/health reports restart_required when the checkout's git HEAD has moved since the server
// started, i.e. the running code is older than the files on disk. Not fetchAPI(): health answers
// 503 when degraded, and the body is still the JSON this banner needs. Not project-prefixed:
// /api/health is process-wide.
async function _pollRestartRequired() {
  var data;
  try {
    var res = await fetch('/api/health');
    data = await res.json();
  } catch (_e) {
    // No reachable server or a non-JSON body (a plain `sova dashboard` has no /api/health): leave
    // the last known state alone rather than claim anything.
    return;
  }

  var banner = document.getElementById('restart-banner');
  if (!banner) return;
  var required = !!(data && data.restart_required);
  banner.classList.toggle('hidden', !required);
  if (!required) return;

  var detail = document.getElementById('restart-banner-detail');
  if (detail) {
    var from = (data.code_sha || '').slice(0, 7);
    var to = (data.checkout_sha || '').slice(0, 7);
    detail.textContent = 'Running ' + from + ', checkout is at ' + to + '. Restart to load the new code.';
  }
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
    if (_connectivityPollInterval) { clearInterval(_connectivityPollInterval); _connectivityPollInterval = null; }
    if (_restartPollInterval) { clearInterval(_restartPollInterval); _restartPollInterval = null; }
    if (_globalBatchPollInterval) { clearInterval(_globalBatchPollInterval); _globalBatchPollInterval = null; }
  } else {
    _pollConnectivity();
    _pollRestartRequired();
    if (!_connectivityPollInterval) _connectivityPollInterval = setInterval(_pollConnectivity, 15000);
    if (!_restartPollInterval) _restartPollInterval = setInterval(_pollRestartRequired, 15000);
    if (_globalBatchId && !_globalBatchPollInterval) {
      _pollGlobalBatch();
      _globalBatchPollInterval = setInterval(_pollGlobalBatch, _GLOBAL_BATCH_POLL_MS);
    }
  }
});

function _setActivityDot(running) {
  _activityRunning = running;
  _renderActivityDot();
}

function _renderActivityDot() {
  var dot = document.getElementById('activity-dot');
  if (!dot) return;
  // A known activity state shows through an unknown handoff one (the banner says the handoff is unknown), so
  // gray means the activity source itself failed.
  if (_handoffAwaiting === true) dot.className = _dotClass('bg-accent-yellow', true);
  else if (_activityRunning === undefined) return;
  else if (_activityRunning === null) dot.className = _dotClass('bg-gray-500', false);
  else dot.className = _activityRunning ? _dotClass('bg-accent', true) : _dotClass('bg-accent-green', false);
}

function _applyActivity(data) {
  window._sovaActiveAgents = { data: data, ts: Date.now() };
  _setActivityDot(!!(data.agents && data.agents.length > 0));
}

async function _pollActivity() {
  var seq = _activitySeq;
  try {
    var data = await fetchAPI(apiUrl('/agents/active'));
    if (seq !== _activitySeq) return;
    _applyActivity(data);
  } catch (e) {
    if (seq === _activitySeq) _setActivityDot(null);
  }
}

async function _pollHandoff() {
  var seq = _handoffSeq;
  try {
    var res = await fetch(apiUrl('/handoff'));
    if (!res.ok) { if (seq === _handoffSeq) _setHandoffUnknown(); return; }
    var data = await res.json();
    if (seq !== _handoffSeq) return;
    _applyHandoff(data);
  } catch (e) {
    if (seq === _handoffSeq) _setHandoffUnknown();
  }
}

// A handoff state that cannot be read must not leave the last value showing as current: hide the checkpoint
// banner and show the "unknown" one until a fetch or a push succeeds. The dot keeps the activity state.
function _setHandoffUnknown() {
  _handoffAwaiting = null;
  var banner = document.getElementById('checkpoint-banner');
  if (banner) banner.classList.toggle('hidden', true);
  var unreadable = document.getElementById('handoff-unreadable-banner');
  if (unreadable) unreadable.classList.toggle('hidden', true);
  var unknown = document.getElementById('handoff-unknown-banner');
  if (unknown) unknown.classList.toggle('hidden', false);
  _renderActivityDot();
}

function _applyHandoff(data) {
  var banner = document.getElementById('checkpoint-banner');
  var unreadableBanner = document.getElementById('handoff-unreadable-banner');
  var unknownBanner = document.getElementById('handoff-unknown-banner');
  if (unknownBanner) unknownBanner.classList.toggle('hidden', true);

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
      // On the link, not the truncating label: the label's overflow would clip the bubble.
      unreadableLabel.closest('a').setAttribute('data-tooltip', unreadable.concat(schemaInvalid).join('\n'));
    }
  }

  if (!banner) return;

  var handoffs = (data.handoffs || []).filter(function(h) {
    return h.status === 'awaiting_action';
  });

  _handoffAwaiting = handoffs.length > 0;
  banner.classList.toggle('hidden', !_handoffAwaiting);
  _renderActivityDot();
}

/* ============================================================
   7b. ACTIVITY FEED (shared socket)
   ============================================================ */

// The panel is js/feed/panel.js (open state, width, briefing card) on the shared side panel, and its body is drawn by
// js/feed/activity.js (it also owns the unread badge). This section keeps what is neither: a toast for each live
// warning or error and the gap fill after a reconnect.
var _feedLastId = 0;           // highest id seen (newest)
var _feedSeenIds = {};
var _feedSeenCount = 0;
var _FEED_SEEN_MAX = 1000;

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

// Remember an id so a live event and the gap fill that overlaps it toast once; only the newest ids are kept.
function _feedRememberId(id) {
  _feedSeenIds[id] = true;
  if (++_feedSeenCount <= _FEED_SEEN_MAX) return;
  var keep = {};
  var kept = 0;
  Object.keys(_feedSeenIds).forEach(function(key) {
    if (Number(key) > _feedLastId - _FEED_SEEN_MAX / 2) { keep[key] = true; kept++; }
  });
  _feedSeenIds = keep;
  _feedSeenCount = kept;
}

// A live event (from the socket or the gap fill): a toast and a browser notification for a warning or an error.
function _feedAddEvent(event) {
  if (_feedSeenIds[event.id]) return;
  if (event.id > _feedLastId) _feedLastId = event.id;
  _feedRememberId(event.id);

  if (event.severity !== 'info') {
    showToast(event.title, event.severity);
    var browserTitle = event.severity === 'error' ? 'SOVA: Error' :
                       event.severity === 'warning' ? 'SOVA: Warning' : 'SOVA';
    sendBrowserNotification(browserTitle, event.title);
  }
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

// opts.bare drops the "PR " prefix, for a column whose header already says PR.
function prLink(prNumber, opts) {
  if (!prNumber) return '--';
  var safe = escapeHtml(String(prNumber));
  var text = (opts && opts.bare ? '#' : 'PR #') + safe;
  var repo = window.SOVA_GITHUB_REPO;
  if (repo) {
    return '<a href="https://github.com/' + escapeHtml(repo) + '/pull/' + safe + '" target="_blank" rel="noopener" ' +
      'class="text-accent-lavender hover:underline" onclick="event.stopPropagation()">' + text + '</a>';
  }
  return text;
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
// The status route is limited per route and client (dashboard.rate_limit_per_minute, default 60): at 5 s one tab
// polls 12 times a minute, so several tabs on one device stay under the default budget.
var _GLOBAL_BATCH_POLL_MS = 5000;

// Whether this tab is following a batch (started here, resumed after a reload, or found running on the server). A
// control that starts a batch is disabled while it is: the `sova:batch-change` event says when that changes.
function globalBatchRunning() { return _globalBatchId !== null; }

function _announceGlobalBatch() {
  document.dispatchEvent(new CustomEvent('sova:batch-change', { detail: { running: _globalBatchId !== null } }));
}

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
  _globalBatchPollInterval = setInterval(_pollGlobalBatch, _GLOBAL_BATCH_POLL_MS);
  _announceGlobalBatch();
}

function _resumeGlobalBatchPolling(batchId) {
  _globalBatchId = batchId;
  sessionStorage.setItem(_batchStorageKey(), batchId);
  _showGlobalBatchBar('batch', 0);
  _globalBatchPollInterval = setInterval(_pollGlobalBatch, _GLOBAL_BATCH_POLL_MS);
  _announceGlobalBatch();
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
    var res = await fetch(apiUrl('/queue/batch/' + _globalBatchId + '/status'));
    // Over the rate limit: skip this tick and keep the bar, the next tick asks again.
    if (res.status === 429) return;
    if (!res.ok) throw new Error('API error: ' + res.status);
    var data = await res.json();

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
  _announceGlobalBatch();
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
  // focusin as well as mouseover: the tooltip also shows on keyboard focus (:focus-visible in style.css).
  function placeTip(e) {
    var el = e.target.closest ? e.target.closest('[data-tooltip]') : null;
    if (!el || !sidebar.contains(el)) return;
    var rect = el.getBoundingClientRect();
    el.style.setProperty('--sidebar-tip-y', (rect.top + rect.height / 2) + 'px');
  }
  sidebar.addEventListener('mouseover', placeTip);
  sidebar.addEventListener('focusin', placeTip);
}

/* The "More" nav group. The server renders it open when the current page is one of its children;
   otherwise the remembered preference (written only by an explicit toggle) decides. Any stored value
   other than '1' counts as collapsed. */
var SIDEBAR_MORE_KEY = 'sova.sidebar.moreOpen';

function setSidebarMoreOpen(open) {
  var btn = document.getElementById('sidebar-more-toggle');
  var group = document.getElementById('sidebar-more-group');
  if (!btn || !group) return;
  group.classList.toggle('hidden', !open);
  btn.setAttribute('aria-expanded', open ? 'true' : 'false');
}

function initSidebarMore() {
  var btn = document.getElementById('sidebar-more-toggle');
  if (!btn) return;
  if (btn.dataset.activeChild !== 'true') {
    setSidebarMoreOpen(safeStorageGet(SIDEBAR_MORE_KEY) === '1');
  }
  btn.addEventListener('click', function () {
    var open = btn.getAttribute('aria-expanded') !== 'true';
    setSidebarMoreOpen(open);
    safeStorageSet(SIDEBAR_MORE_KEY, open ? '1' : '0');
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
initSidebarMore();
if (document.getElementById('activity-dot')) {
  initBrowserNotifications();
  startSidebarPolling();
} else if (document.getElementById('restart-banner')) {
  // Pages without the sidebar (Fleet home, Setup) still carry the restart banner.
  _pollRestartRequired();
  _restartPollInterval = setInterval(_pollRestartRequired, 15000);
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
   13. RUN ACTIONS (names and colours)
   ============================================================ */

/* Each kind of run's name, running form and colour token come from the server's table
   (work_state.RUN_ACTIONS), emitted once per page as window.SOVA_RUN_ACTIONS. These helpers only
   normalize a stored role, look it up, and turn a colour token into classes. A page without the
   table (home.html and setup.html do not extend base.html) still shows the command or role name. */

var _RUN_ACTION_CLASSES = {
  blue:   { bg: 'bg-accent/20',        text: 'text-accent',        dot: 'bg-accent',        border: 'border-accent/40',        hexKey: 'accent' },
  green:  { bg: 'bg-accent-green/20',  text: 'text-accent-green',  dot: 'bg-accent-green',  border: 'border-accent-green/40',  hexKey: 'green' },
  mauve:  { bg: 'bg-accent-purple/20', text: 'text-accent-purple', dot: 'bg-accent-purple', border: 'border-accent-purple/40', hexKey: 'purple' },
  yellow: { bg: 'bg-accent-yellow/20', text: 'text-accent-yellow', dot: 'bg-accent-yellow', border: 'border-accent-yellow/40', hexKey: 'yellow' },
  teal:   { bg: 'bg-accent-teal/20',   text: 'text-accent-teal',   dot: 'bg-accent-teal',   border: 'border-accent-teal/40',   hexKey: 'teal' },
  gray:   { bg: 'bg-gray-500/20',      text: 'text-gray-400',      dot: 'bg-gray-500',      border: 'border-gray-600',         hexKey: 'muted' },
};

function runActionColors(token) {
  var c = Object.prototype.hasOwnProperty.call(_RUN_ACTION_CLASSES, token) ? _RUN_ACTION_CLASSES[token] : _RUN_ACTION_CLASSES.gray;
  return {
    bg: c.bg, text: c.text, dot: c.dot, border: c.border,
    get hex() { return sovaColor(c.hexKey); },
  };
}

// command:<name> without a leading slash or arguments, as work_state.command_role() does; other roles unchanged.
function _commandRole(role) {
  if (role.indexOf('command:') !== 0) return role;
  var words = role.slice('command:'.length).trim().split(/\s+/);
  return 'command:' + words[0].replace(/^\/+/, '');
}

// The action a run performed: {key, label, running, color}. Mirrors work_state.run_action() over the same table.
function runActionFor(role, pipelineVariant) {
  if (!role) return { key: 'auto', label: 'Auto', running: 'Running', color: 'gray' };
  var table = window.SOVA_RUN_ACTIONS;
  var own = function(obj, k) { return !!obj && Object.prototype.hasOwnProperty.call(obj, k); };
  var normalized = _commandRole(role);
  var key = null;
  if (table) {
    var byVariant = own(table.variants, role) ? table.variants[role] : null;
    if (own(byVariant, pipelineVariant || '')) key = byVariant[pipelineVariant];
    else if (own(table.roles, normalized)) key = table.roles[normalized];
  }
  if (key !== null && own(table.actions, key)) {
    var a = table.actions[key];
    return { key: key, label: a.label, running: a.running, color: a.color };
  }
  // An unlisted command or role named after a registered action takes that action's name and colour.
  var named = function(name, label) {
    if (table && own(table.actions, name)) {
      var reg = table.actions[name];
      return { key: name, label: reg.label, running: reg.running, color: reg.color };
    }
    return { key: name, label: label, running: 'Running', color: 'gray' };
  };
  if (normalized.indexOf('command:') === 0 && normalized !== 'command:') {
    var name = normalized.slice('command:'.length);
    // With the table loaded, an unlisted command reads like the server names it ("Health audit").
    return named(name, table ? name.charAt(0).toUpperCase() + name.slice(1).toLowerCase().replace(/-/g, ' ') : name);
  }
  return named(role, role);
}

function formatRole(role, pipelineVariant) {
  return runActionFor(role, pipelineVariant).label;
}

function roleColor(role, pipelineVariant) {
  return runActionColors(runActionFor(role, pipelineVariant).color);
}

/* ============================================================
   13. STEP PIPELINE BAR
   ============================================================ */

var PIPELINE_STEPS = [
  'sync', 'assess', 'create_worktree', 'capture_baseline', 'develop', 'simplify',
  'self_review', 'commit', 'validate', 'push', 'create_pr',
  'wait_for_external_reviews', 'address_external_findings',
  'monitor_ci', 'address_sonarcloud_findings', 'confidence_score', 'extract_memory', 'handoff_to_reviewer'
];

var STEP_LABELS = {
  sync: 'Sync', assess: 'Assess', create_worktree: 'Worktree',
  capture_baseline: 'Baseline', develop: 'Develop', simplify: 'Simplify', self_review: 'Review',
  commit: 'Commit', validate: 'Validate', push: 'Push', create_pr: 'PR',
  wait_for_external_reviews: 'Ext Reviews', address_external_findings: 'Address Ext',
  monitor_ci: 'CI', address_sonarcloud_findings: 'Sonar', confidence_score: 'Confidence', extract_memory: 'Memory', handoff_to_reviewer: 'Handoff',
  rebase: 'Rebase', address_review: 'Address', resolve_external_reviews: 'Resolve', resolve_review_threads: 'Threads', refresh_pr_body: 'PR Body',
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
  return sovaColor(key);
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
    var label = '';
    if (!compact) {
      var runAction = runActionFor(role, pipelineVariant);
      // An action outside the table runs as the generic "Running": name the command instead.
      label = '<div class="text-xs text-gray-400 mt-1">' +
        escapeHtml(runAction.running === 'Running' ? runAction.label : runAction.running) + '</div>';
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
    steps = ['ensure_worktree', 'rebase', 'address_review', 'rearrange_commits', 'validate', 'push', 'resolve_review_threads', 'refresh_pr_body', 'monitor_ci', 'address_sonarcloud_findings', 'resolve_external_reviews', 'extract_memory', 'handoff_to_reviewer'];
    labels = {ensure_worktree: 'Worktree', rebase: 'Rebase', address_review: 'Address', rearrange_commits: 'Rearrange', validate: 'Validate', push: 'Push', resolve_review_threads: 'Threads', refresh_pr_body: 'PR Body', monitor_ci: 'CI', address_sonarcloud_findings: 'Sonar', resolve_external_reviews: 'Resolve', extract_memory: 'Memory', handoff_to_reviewer: 'Handoff'};
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
   SOVA STATUS CHANNEL (a page's subscription to the shared socket)
   ============================================================ */

/**
 * createSovaStatusChannel(options) -> { stop: function() }
 *
 * A page-level subscription to the tab's shared status socket (subscribeSovaSocket, section 6b), which owns the
 * connection, the reconnect backoff, the liveness watchdog (a socket silent for 20s is closed and reopened) and
 * closing while the tab is hidden. This wrapper adds only an optional auxiliary interval that runs for the
 * channel's whole life.
 *
 * options:
 *   onMessage(msg)         called with the parsed JSON message on every WS push
 *   onOpen()               called every time the socket (re)connects, and when this channel joins a socket that
 *                          is already open: the catch-up fetch for state the socket itself cannot narrate (e.g. a
 *                          run reaching a terminal status the periodic push never carries).
 *   onFallbackPoll()       called at once when the socket goes down, then every pollIntervalMs while it stays down
 *   pollIntervalMs         fallback polling cadence (default 30000)
 *   auxiliaryPollFn()      optional: a second, independent interval started and stopped with this channel,
 *                          regardless of socket state
 *   auxiliaryPollIntervalMs  cadence for auxiliaryPollFn (default 5000)
 */
function createSovaStatusChannel(options) {
  var auxiliaryInterval = null;
  var subscription = subscribeSovaSocket({
    onMessage: options.onMessage,
    onOpen: options.onOpen,
    onFallbackPoll: options.onFallbackPoll,
    pollIntervalMs: options.pollIntervalMs,
    freshnessKey: options.freshnessKey,
    freshnessTypes: options.freshnessTypes
  });
  if (options.auxiliaryPollFn) {
    auxiliaryInterval = setInterval(options.auxiliaryPollFn, options.auxiliaryPollIntervalMs || 5000);
  }
  return {
    stop: function() {
      if (auxiliaryInterval) { clearInterval(auxiliaryInterval); auxiliaryInterval = null; }
      subscription.unsubscribe();
    }
  };
}


/* ============================================================
   Agent output: one renderer and one stream helper (#1052)
   Shared by the Work page log viewer and the run page. sovaOutputRenderer() appends one row per line and
   sovaOutputStream() feeds it from the snapshot, the SSE stream and the polling fallback. Both are plain
   closures: each instance owns its own state, and stop() releases its EventSource, timers and listener.
   ============================================================ */

var _OUTPUT_POLL_BASE_MS = 1000;
var _OUTPUT_POLL_MAX_MS = 5000;
// A backfill still unanswered after this long is aborted: its rejection falls back to polling instead of holding the
// live lines back for as long as the request hangs.
var _OUTPUT_BACKFILL_TIMEOUT_MS = 15000;
// How long a backfilled stream waits for its "connected" event before giving up on SSE for polling.
var _OUTPUT_CONNECT_WAIT_MS = 3000;
// A pane within this many pixels of its bottom counts as following the tail.
var _OUTPUT_TAIL_SLACK_PX = 24;
// A block of same-kind lines longer than this collapses into a summary (see sovaOutputRenderer()).
var _OUTPUT_FOLD_OVER = 20;
// The renderer holds at most this many lines (the server keeps 5000 per run too); it trims in steps of the slack so
// a long run does not redraw the pane on every line.
var _OUTPUT_MAX_ENTRIES = 5000;
var _OUTPUT_TRIM_SLACK = 500;
// A tool call is "[Name] argument" and an errored tool result "[tool error] detail" (sova/ipc/claude_stream.py).
// The line carries no type, so only a known Claude Code tool name, an MCP tool (mcp__server__tool) or the producer's
// "[tool]" fallback counts as a call: agent prose such as "[Note] check the migration" stays visible text.
var _OUTPUT_TOOL_NAMES = [
  'Agent', 'Task', 'Bash', 'BashOutput', 'KillShell', 'Read', 'Write', 'Edit', 'MultiEdit', 'Glob', 'Grep', 'LS',
  'NotebookEdit', 'NotebookRead', 'WebFetch', 'WebSearch', 'TodoWrite', 'TodoRead', 'Skill', 'SlashCommand',
  'ToolSearch', 'ExitPlanMode', 'EnterPlanMode', 'AskUserQuestion', 'Monitor', 'TaskOutput', 'TaskStop',
  'ListMcpResourcesTool', 'ReadMcpResourceTool', 'tool'
];
var _OUTPUT_TOOL_RE = new RegExp('^\\[(?:' + _OUTPUT_TOOL_NAMES.join('|') + '|mcp__[\\w-]+)\\](?: |$)');
var _OUTPUT_ERROR_RE = /^(?:\[tool error\]|\[ERROR\]|ERROR:)/;
var _OUTPUT_RESULT_RE = /^\s*--- Result/;
var _OUTPUT_PREFIX_RE = /^\[(stderr|stdout)\]\s?(.*)$/;
var _OUTPUT_STRUCT_RE = /^(\d{4}-\d{2}-\d{2}T[\d:.]+Z?)\s+\[\s*(\w+)\s*\]\s*(.*)$/;
var _OUTPUT_CTX_RE = /\s([A-Za-z_][\w.]*)=/;
// "[Name] argument": the tool name and what the call is about (_OUTPUT_TOOL_RE has already vouched for the name).
var _OUTPUT_TOOL_PARSE_RE = /^\[([^\]]+)\](?: ([\s\S]*))?$/;
// One key=value pair of structlog's context: a bare word, a quoted string or a bracketed list or dict.
var _OUTPUT_KV_RE = /([A-Za-z_][\w.]*)=("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|\[[^\]]*\]|\{[^}]*\}|\S+)/g;
// A pause between two rows of more than this is drawn as a "quiet for" divider.
var _OUTPUT_GAP_MS = 60000;
// A log line written more than this after the time it carries was held back by its process (an inner run's stderr
// dumped at the end), and one carrying a time more than this ahead of its write time came from a skewed clock: either
// way its own time and its write time describe different moments, so the group is labelled.
var _OUTPUT_REPLAY_MS = 60000;
// A Python traceback's opening line, after the stream prefix is stripped: plain, or an exception group's
// ("  + Exception Group Traceback ..."), and a sub-exception's inside one ("    | Traceback ...").
// The exception line that ends it is found by position, not by name: the first line after the header that, once an
// exception group's "  | " margin is stripped, starts at column 0 with a dotted name followed by a colon or nothing
// ("Exception: boom", "httpx.ReadTimeout: timed out", "StopIteration", "KeyboardInterrupt"). Frame and source lines
// are indented, and a chaining sentence ("During handling of the above exception, ...") has a space after its first
// word, so neither matches. A folded block that holds a traceback summarises with its exception line rather than
// whatever stderr printed after it.
var _OUTPUT_TRACEBACK_RE = /^[\s|+]*(?:Exception Group )?Traceback \(most recent call last\):/;
var _OUTPUT_GROUP_MARGIN_RE = /^\s*[|+] ?/;
var _OUTPUT_EXCEPTION_RE = /^[A-Za-z_][\w.]*(?::|$)/;
// Agent prose is rendered as markdown only when it shows a markdown signal: an inline code span, bold, a list item,
// a heading, a quote or a link target ("]("). Plain text, padded columns and pasted command output stay as written.
var _OUTPUT_MD_RE = /`|\*\*|^\s{0,3}(?:[-*+]|\d+\.)\s+\S|^\s{0,3}#{1,6}\s+\S|^\s{0,3}>\s|\]\(\S/m;

var _OUTPUT_ERROR_LEVELS = ['error', 'critical', 'exception'];

// A SOVA log line is classified after its [stderr]/[stdout] prefix is stripped: structlog writes to stderr, so
// nearly every one carries the prefix. An error-level one is an error row (it stays visible outside any fold);
// only unstructured stderr text, such as a traceback, is a stderr line.
function _outputKind(text) {
  var prefixed = _OUTPUT_PREFIX_RE.exec(text);
  // An error line stays an error row (outside any fold) whether or not the process stream tagged it first.
  if (_OUTPUT_ERROR_RE.test(text) || (prefixed && _OUTPUT_ERROR_RE.test(prefixed[2]))) return 'error';
  if (_OUTPUT_TOOL_RE.test(text)) return 'tool';
  if (_OUTPUT_RESULT_RE.test(text)) return 'result';
  var sm = _OUTPUT_STRUCT_RE.exec(prefixed ? prefixed[2] : text);
  if (sm) return _OUTPUT_ERROR_LEVELS.indexOf(sm[2].toLowerCase()) >= 0 ? 'error' : 'log';
  if (prefixed && prefixed[1] === 'stderr') return 'stderr';
  return 'text';
}

// Whether a line is agent prose to render as markdown: a plain text row (no stream prefix, so not a SOVA log line or
// stderr) that shows a markdown signal. A tool call, result, error or log row never is.
function _outputIsProse(kind, raw) {
  return kind === 'text' && !_OUTPUT_PREFIX_RE.test(raw) && _OUTPUT_MD_RE.test(raw);
}

// The structlog match of a line (timestamp, level, body) after its [stderr]/[stdout] prefix, or null for any other line.
function _outputStruct(text) {
  var pm = _OUTPUT_PREFIX_RE.exec(text);
  return _OUTPUT_STRUCT_RE.exec(pm ? pm[2] : text);
}

// The lowercase level of a structlog line ('' for any other line).
function _outputLevel(text) {
  var sm = _outputStruct(text);
  return sm ? sm[2].toLowerCase() : '';
}

// When a structlog line says it was logged, as epoch milliseconds; null for a line without a time that names its zone
// (the UTC `...Z` form SOVA logs), which cannot be compared with a write time.
function _outputLoggedMs(text) {
  var sm = _outputStruct(text);
  return sm && /[zZ]$/.test(sm[1]) ? _outputTimeMs(sm[1]) : null;
}

// The clock a structlog line says it was logged at, for a row with no write time (a legacy row): a zoned time in the
// viewer's zone, a time with no zone as written. '' for any other line.
function _outputLoggedClock(text) {
  var sm = _outputStruct(text);
  if (!sm) return '';
  var ms = _outputLoggedMs(text);
  if (ms !== null) return _outputClock(ms);
  var hm = /T(\d{2}:\d{2}:\d{2})/.exec(sm[1]);
  return hm ? hm[1] : '';
}

// "45s", "4m 34s", "2h 5m".
function _outputDuration(ms) {
  var secs = Math.round(ms / 1000);
  if (secs < 60) return secs + 's';
  var mins = Math.floor(secs / 60);
  if (mins < 60) return mins + 'm ' + (secs % 60) + 's';
  return Math.floor(mins / 60) + 'h ' + (mins % 60) + 'm';
}

// The one-line form of a row for a fold summary: no stream prefix, and a SOVA log line as "LEVEL event" without its time.
function _outputSummaryText(text) {
  var sm = _outputStruct(text);
  if (sm) return sm[2].toUpperCase() + ' ' + sm[3];
  var pm = _OUTPUT_PREFIX_RE.exec(text);
  return pm ? pm[2] : text;
}

// A tool name's chip colour group.
function _outputToolClass(name) {
  var n = name.toLowerCase();
  if (n === 'bash' || n === 'bashoutput' || n === 'killshell') return 'out-tool-exec';
  if (/^(?:edit|multiedit|write|notebookedit)$/.test(n)) return 'out-tool-write';
  if (/^(?:agent|task|skill|slashcommand|taskoutput|taskstop)$/.test(n)) return 'out-tool-agent';
  if (n.indexOf('mcp__') === 0) return 'out-tool-mcp';
  return 'out-tool-read';
}

// structlog's key=value context as one chip per pair. A context that is not only pairs stays one muted string, so no
// text is lost.
function _outputContextHtml(context) {
  var chips = [];
  var left = context.replace(_OUTPUT_KV_RE, function(m, key, value) {
    chips.push('<span class="out-kv"><span class="out-k">' + escapeHtml(key) + '</span>' + escapeHtml(value) + '</span>');
    return '';
  });
  if (chips.length === 0 || left.trim() !== '') return '<span class="out-ctx">' + escapeHtml(context) + '</span>';
  return chips.join('');
}

// The inner HTML of one line's text. kind is the row's kind, or 'prose' for agent prose (_outputIsProse()), which goes
// through renderMarkdown() as untrusted text (sanitized, with no image or other load on render and links opening a
// new tab; escaped when the helper is missing); a tool call
// is a coloured name chip and its argument; a line that follows structlog's ConsoleRenderer layout (timestamp, level,
// event, then key=value context, prefixed [stderr]/[stdout] by agent_output.py) is a level badge, the event and
// context chips, its own time always dropped from the text: the time column shows when it was written (or, for a row
// with no write time, the logged clock itself), and the badge's tooltip keeps the logged time, a zoned one as a
// clock in the viewer's zone and one that names no zone (which cannot be compared with a write time) as written.
// Every other line is escaped plain text, so a traceback or an agent's padded columns survive untouched.
function _outputTextHtml(raw, kind) {
  if (kind === 'prose' && typeof renderMarkdown === 'function') {
    return '<div class="out-md">' + renderMarkdown(raw, { untrusted: true }) + '</div>';
  }
  var tm = kind === 'tool' ? _OUTPUT_TOOL_PARSE_RE.exec(raw) : null;
  if (tm) {
    return '<span class="out-chip ' + _outputToolClass(tm[1]) + '">' + escapeHtml(tm[1]) + '</span>' +
      (tm[2] ? ' <span class="out-arg">' + escapeHtml(tm[2]) + '</span>' : '');
  }
  var stream = '';
  var rest = raw;
  var pm = _OUTPUT_PREFIX_RE.exec(raw);
  if (pm) { stream = pm[1]; rest = pm[2]; }
  var sm = _OUTPUT_STRUCT_RE.exec(rest);
  if (!sm) return (stream ? '<span class="out-stream">' + stream + '</span> ' : '') + escapeHtml(rest);
  var ts = sm[1], level = sm[2].toLowerCase(), body = sm[3];
  var badge = level === 'warning' ? 'warn' : (level === 'critical' ? 'crit' : level);
  var ctxMatch = _OUTPUT_CTX_RE.exec(body);
  var event = ctxMatch ? body.slice(0, ctxMatch.index).trim() : body.trim();
  var context = ctxMatch ? body.slice(ctxMatch.index).trim() : '';
  var loggedMs = _outputLoggedMs(rest);
  var logged = loggedMs !== null ? _outputClock(loggedMs) : (/[zZ]$/.test(ts) ? '' : ts + ' (no zone)');
  var title = logged ? ' data-tooltip="logged ' + escapeHtml(logged) + '"' : '';
  return '<span class="out-lvl out-lvl-' + escapeHtml(level) + '"' + title + '>' + escapeHtml(badge.toUpperCase()) +
    '</span> ' +
    '<span class="out-event">' + escapeHtml(event) + '</span>' +
    (context ? ' ' + _outputContextHtml(context) : '');
}

// Epoch milliseconds of an ISO time, or null for a missing or unparseable one (a legacy row has no time).
function _outputTimeMs(iso) {
  if (iso === null || iso === undefined) return null;
  var ms = Date.parse(iso);
  return isNaN(ms) ? null : ms;
}

function _outputClock(ms) {
  var d = new Date(ms);
  return [d.getHours(), d.getMinutes(), d.getSeconds()].map(function(n) { return String(n).padStart(2, '0'); }).join(':');
}

// Appends agent output to a pane, one row per line, never rebuilding what is already there:
//  - a row carries its write time only when the second changed since the previous visible row, and the pane shows
//    the time column only once its times show more than one whole second (rows stored before write times were
//    recorded all share the flush time, and a legacy file has none, so such a run shows no column);
//  - a block of consecutive same-kind lines (agent text, tool calls, results, stderr, SOVA log lines) is drawn
//    open; once it passes _OUTPUT_FOLD_OVER lines it collapses once into a summary (count and last line, both
//    updated in place as it grows), and the reader's own toggle is kept after that. An error row ends the block and
//    stays visible outside it;
//  - the pane follows the tail only while the reader is at the bottom, on a redraw too;
//  - a pause of more than _OUTPUT_GAP_MS between rows is drawn as a "quiet for" divider outside any block (a pause
//    inside a same-kind run ends that block and opens a new one, so a folded block never hides a divider). A chain of
//    slow calls a minute or more apart therefore never folds: each call keeps its own row and the divider after it
//    says how long it ran. That is deliberate, as the dividers are the information there, and a chain that slow adds
//    at most two lines a minute, so it cannot flood the pane the way a burst does. A log
//    line whose own time is far from its write time starts a labelled group, so a row's time column is always the
//    write time (a legacy row with none shows the time its log line carries, marked as such);
//  - at most _OUTPUT_MAX_ENTRIES lines are held: older ones are dropped and a marker says how many.
// The renderer keeps the lines it was given, so setTarget() can draw them into a rebuilt pane.
function sovaOutputRenderer() {
  var pane = null;
  var entries = [];
  // Lines dropped off the front of entries (over the cap): the run's line number of entries[0].
  var dropped = 0;
  // A read has landed since the last clear(), even an empty one, so a rebuilt pane is redrawn (its placeholder goes).
  var filled = false;
  var view = freshView();

  function freshView() {
    // lastMs: the write time of the previous row of any kind (a quiet gap is measured from it); replayAt: the write
    // time of the last row of an open replayed-log group, or null; replayEarly: whether that group's lines carry a
    // time ahead of their write time (a skewed clock) rather than behind it.
    return { prevSec: null, minMs: null, maxMs: null, block: null, lastMs: null, replayAt: null, replayEarly: false };
  }

  function atTail() {
    var gap = pane.scrollHeight - pane.scrollTop - pane.clientHeight;
    return !(gap > _OUTPUT_TAIL_SLACK_PX);
  }

  // scope is the block the row joins, or the view itself for a row outside any block. A row inside a folded block
  // is not visible, so it must neither take the clock label for its second nor use up the label of the row after
  // the block: it tracks its own previous second.
  function rowHtml(entry, kind, scope) {
    var sec = entry.ms === null ? null : Math.floor(entry.ms / 1000);
    var label = '';
    var timeAttr = '';
    if (sec !== null) {
      var prev = scope.folded ? scope.prevSec : view.prevSec;
      if (sec !== prev) label = _outputClock(entry.ms);
      scope.prevSec = sec;
      if (!scope.folded) view.prevSec = sec;
    } else {
      // A row with no write time (stored before they were recorded) keeps the time its log line carries, so an old
      // run's SOVA log lines are not left with no clock at all. The column is shown for it, marked as a logged time.
      label = _outputLoggedClock(entry.text);
      if (label) {
        timeAttr = ' data-tooltip="logged time (no write time recorded)"';
        pane.classList.add('out-has-times');
      }
    }
    var prose = _outputIsProse(kind, entry.text);
    var extra = '';
    if (prose) extra = ' out-prose';
    else if (kind === 'log' && _outputLevel(entry.text).indexOf('warn') === 0) extra = ' out-warn';
    return '<div class="out-row out-' + kind + extra + '"><span class="out-time"' + timeAttr + '>' + label + '</span>' +
      '<span class="out-text">' + _outputTextHtml(entry.text, prose ? 'prose' : kind) + '</span></div>';
  }

  // The divider before a row that follows a pause, and the bookkeeping for the next one. It names the length and
  // nothing more: a long build can print nothing for minutes, so the reader judges whether that is a problem. The
  // baseline only moves forward, so a row stamped earlier than the one before it cannot fake a pause after it.
  function gapHtml(entry) {
    if (entry.ms === null) return '';
    var html = '';
    if (view.lastMs !== null && entry.ms - view.lastMs > _OUTPUT_GAP_MS) {
      html = '<div class="out-gap"><span>quiet for ' + _outputDuration(entry.ms - view.lastMs) + '</span></div>';
    }
    view.lastMs = view.lastMs === null ? entry.ms : Math.max(view.lastMs, entry.ms);
    return html;
  }

  // The label that opens a group of log lines whose own time is more than _OUTPUT_REPLAY_MS from their write time:
  // replayed lines logged earlier, or lines from a clock running ahead. The group stays open for the unstructured
  // lines between its log lines (a traceback has no time) and ends at a log line written on time, one off the other
  // way, or one written more than a minute after the last of the group.
  function replayHtml(entry) {
    var logged = _outputLoggedMs(entry.text);
    if (logged === null) return '';
    var off = entry.ms === null ? 0 : entry.ms - logged;
    if (Math.abs(off) <= _OUTPUT_REPLAY_MS) {
      view.replayAt = null;
      return '';
    }
    var early = off < 0;
    var open = view.replayAt !== null && view.replayEarly === early && entry.ms - view.replayAt <= _OUTPUT_REPLAY_MS;
    view.replayAt = entry.ms;
    view.replayEarly = early;
    if (open) return '';
    var times = 'logged ' + _outputClock(logged) + ', written ' + _outputClock(entry.ms) + ' (' +
      _outputDuration(Math.abs(off)) + (early ? ' early)' : ' late)');
    return '<div class="out-cluster">' + (early ? 'Log clock ahead: ' : 'Replayed log: ') + times + '</div>';
  }

  function noteTime(ms) {
    if (ms === null) return;
    view.minMs = view.minMs === null ? ms : Math.min(view.minMs, ms);
    view.maxMs = view.maxMs === null ? ms : Math.max(view.maxMs, ms);
    // Whole seconds, as rowHtml() labels them: 12:00:00.900 and 12:00:01.100 show two different clocks.
    if (Math.floor(view.maxMs / 1000) !== Math.floor(view.minMs / 1000)) pane.classList.add('out-has-times');
  }

  function openBlock(kind) {
    pane.insertAdjacentHTML('beforeend',
      '<details class="out-fold out-fold-' + kind + '" open><summary class="out-fold-summary">' +
      '<span class="out-fold-count"></span><span class="out-fold-last"></span></summary>' +
      '<div class="out-fold-body"></div></details>');
    var el = pane.lastElementChild;
    view.block = {
      kind: kind,
      el: el,
      body: el.querySelector('.out-fold-body'),
      count: el.querySelector('.out-fold-count'),
      last: el.querySelector('.out-fold-last'),
      n: 0,
      folded: false,
      prevSec: null,
      // inTrace: a traceback has opened and its exception line is not seen yet; exception: the latest such line.
      inTrace: false,
      exception: null
    };
    return view.block;
  }

  // Remembers the exception line of the latest traceback in a block, so its summary names the failure even when
  // stderr goes on printing after it (a make error, a cleanup message).
  function noteTraceback(block, line) {
    if (_OUTPUT_TRACEBACK_RE.test(line)) {
      block.inTrace = true;
      return;
    }
    if (!block.inTrace) return;
    var bare = line.replace(_OUTPUT_GROUP_MARGIN_RE, '');
    if (_OUTPUT_EXCEPTION_RE.test(bare)) {
      block.exception = bare;
      block.inTrace = false;
    }
  }

  function summarize(block, line) {
    var noun = block.kind === 'tool' ? (block.n === 1 ? ' tool call' : ' tool calls') : ' lines';
    block.count.textContent = block.n + noun;
    block.last.textContent = ' ' + (block.exception !== null ? block.exception : line);
  }

  function draw(entry) {
    var kind = _outputKind(entry.text);
    noteTime(entry.ms);
    var gap = gapHtml(entry);
    var label = replayHtml(entry);
    if (kind === 'error') {
      view.block = null;
      pane.insertAdjacentHTML('beforeend', gap + label + rowHtml(entry, kind, view));
      return;
    }
    var block = view.block;
    if (block === null || block.kind !== kind || gap + label !== '') {
      // A new block, or a quiet divider or replayed-group label, which ends the open one: they sit outside any block,
      // so a block that folds never hides one.
      if (gap + label !== '') pane.insertAdjacentHTML('beforeend', gap + label);
      block = openBlock(kind);
    }
    block.body.insertAdjacentHTML('beforeend', rowHtml(entry, kind, block));
    block.n += 1;
    var line = _outputSummaryText(entry.text);
    noteTraceback(block, line);
    if (!block.folded && block.n > _OUTPUT_FOLD_OVER) {
      // Collapse once. The row after the block is labelled whatever its second, as the block hides its own rows.
      block.folded = true;
      block.el.open = false;
      block.el.classList.add('out-folded');
      view.prevSec = null;
    }
    if (block.folded) summarize(block, line);
  }

  function drawAll() {
    var follow = atTail();
    var top = pane.scrollTop;
    view = freshView();
    pane.textContent = '';
    pane.classList.remove('out-has-times');
    if (dropped > 0) {
      pane.insertAdjacentHTML('beforeend', '<div class="out-trimmed">' + dropped + ' earlier lines not shown</div>');
    }
    entries.forEach(draw);
    pane.scrollTop = follow ? pane.scrollHeight : top;
  }

  function toEntry(text, time) {
    return { text: text, ms: _outputTimeMs(time) };
  }

  // Replaces everything with a full read of the run; times is parallel to lines and may be missing. A read that
  // only extends what is held draws just the new lines, so a tab return leaves the open folds and the scroll alone.
  function replace(lines, times) {
    var next = lines.map(function(text, i) { return toEntry(text, Array.isArray(times) ? times[i] : null); });
    var cut = Math.max(0, next.length - _OUTPUT_MAX_ENTRIES);
    if (cut > 0) next = next.slice(cut);
    var extend = pane !== null && filled && entries.length > 0 && cut === dropped && next.length >= entries.length &&
      entries.every(function(e, i) { return e.text === next[i].text; });
    var added = extend ? next.slice(entries.length) : null;
    entries = next;
    dropped = cut;
    filled = true;
    if (pane === null) return;
    if (!extend) { drawAll(); return; }
    var follow = atTail();
    added.forEach(draw);
    if (follow) pane.scrollTop = pane.scrollHeight;
  }

  return {
    // Draws the held lines into a pane (null detaches). The lines survive a rebuilt pane, so a page that
    // re-renders its DOM keeps the output of the run it is streaming.
    setTarget: function(el) {
      // The pane it already draws into keeps its rows, open folds and scroll position.
      if (el && el === pane) return;
      pane = el || null;
      if (pane !== null && filled) drawAll();
    },
    append: function(text, time) {
      var entry = toEntry(text, time);
      entries.push(entry);
      filled = true;
      if (entries.length > _OUTPUT_MAX_ENTRIES + _OUTPUT_TRIM_SLACK) {
        var over = entries.length - _OUTPUT_MAX_ENTRIES;
        entries.splice(0, over);
        dropped += over;
        if (pane !== null) drawAll();
        return;
      }
      if (pane === null) return;
      var follow = atTail();
      draw(entry);
      if (follow) pane.scrollTop = pane.scrollHeight;
    },
    replace: replace,
    clear: function() { dropped = 0; replace([], null); filled = false; },
    // Lines held, and lines the run has produced as far as this renderer was told (held plus trimmed).
    count: function() { return entries.length; },
    total: function() { return dropped + entries.length; }
  };
}

// How many leading held lines the backfill already contains: the lines written after the subscription but
// before the snapshot was read. The stream sent every line from connectedSeq on and the snapshot holds every
// line before snapshotSeq, so when both are known the answer is exact, repeated lines included, as long as the
// stream dropped nothing: the server reports a dropped line as a "gap" event, which abandons the stream for a
// full re-read before this arithmetic is trusted. Without them (an agent no longer in memory, or a server that
// sends no seq) it falls back to matching text, which cannot tell an identical line the agent wrote again from
// one it has already seen, so it may drop genuinely new repeated lines. held entries are {text, time}.
function _outputOverlap(snapshot, held, connectedSeq, snapshotSeq) {
  if (connectedSeq !== null && snapshotSeq !== null) {
    return Math.max(0, Math.min(snapshotSeq - connectedSeq, held.length));
  }
  for (var m = Math.min(snapshot.length, held.length); m > 0; m--) {
    var tail = snapshot.length - m;
    var same = true;
    for (var i = 0; i < m && same; i++) same = held[i].text === snapshot[tail + i];
    if (same) return m;
  }
  return 0;
}

// The line count a "connected" event or an output response carries, or null when it carries none
// (a server that predates it, or an agent that is no longer in memory).
function _outputParseSeq(raw) {
  try {
    var seq = JSON.parse(raw).seq;
    return Number.isInteger(seq) ? seq : null;
  } catch (e) {
    return null;
  }
}

// An "output" event is {time, text}; a server that predates the JSON payload sent the text alone.
function _outputParseEvent(raw) {
  try {
    var data = JSON.parse(raw);
    if (data !== null && typeof data === 'object' && typeof data.text === 'string') {
      return { text: data.text, time: data.time === undefined ? null : data.time };
    }
  } catch (e) { /* not JSON: the text itself */ }
  return { text: raw, time: null };
}

// Streams one run's output into a renderer: sovaOutputStream(renderer) -> { start, ensure, stop, runId }.
//  - start(runId, status): a finished run is read once by polling; any other run (paused and awaiting_approval
//    included, they can still write) opens the SSE stream first and backfills from a full read, holding the
//    lines pushed meanwhile, then drops the lines the snapshot already has (_outputOverlap).
//  - ensure(runId, status): start() unless this run is already streaming (or polling after the server ended its
//    stream), or finished and fully read, so a page that rebuilds its DOM re-attaches without reopening the stream. A polling fallback for a run that has since
//    finished ends after its next read instead of polling a run that writes nothing more.
//  - stop(): closes the EventSource, cancels the poll chain and removes the visibility listener.
// A failed stream, a "gap" event, a failed backfill or a late "connected" falls back to polling from line 0, so the
// pane never keeps a log with a silent hole. The stream closes while the tab is hidden and reconciles on return.
// The one self-rescheduling setTimeout chain is poll(): it backs off (1s up to 5s) while a run is quiet, which
// visibilityAwarePoll() (fixed interval) cannot do, so it checks document.hidden itself.
function sovaOutputStream(renderer) {
  var runId = null;
  var transport = null;   // 'sse' or 'poll' while streaming, null otherwise: the tab resumes with the same one
  var source = null;
  var cursor = 0;
  var pollTimer = null;
  var pollGen = 0;
  var backoffMs = _OUTPUT_POLL_BASE_MS;
  var readOnce = false;   // a finished run's poll ends after one successful read
  var loaded = false;     // a full read of this run has been drawn since start()
  var pageStatus = null;  // the status the page last reported for this run (start() or ensure())
  var listening = false;
  var untilGone = false;  // polling on after the server ended the stream: ends once the agent has left memory
  var lastInMemory = null; // whether the previous read of this poll chain found the agent in memory (null: none yet)
  var idleAt = null;      // the status the page reported when the untilGone chain ended: ensure() restarts on a change

  function cancelPolling() {
    pollGen++;
    if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
  }

  function closeSource() {
    if (source) { source.close(); source = null; }
  }

  async function poll() {
    if (runId === null) return;
    // The stream may switch runs, or the chain may be cancelled or restarted, while this fetch is in flight;
    // whichever poll owns the stream now started its own chain, so a stale response neither draws nor reschedules.
    var id = runId;
    var gen = pollGen;
    var isCurrent = function() { return id === runId && gen === pollGen && !document.hidden; };
    // Only a read that started once the run was known to be finished is the last one: a read already in flight
    // when ensure() learns the run ended may have sampled the output before its final lines were written, so it
    // keeps its cursor and the chain schedules one more read from there (retried until it succeeds).
    var finalRead = readOnce;
    var finishedRead = false;
    try {
      var data = await fetchAPI(apiUrl('/agents/' + id + '/output?since=' + cursor));
      if (!isCurrent()) return;
      var inMemory = Number.isInteger(data.seq);
      var left = false;
      if (cursor > 0 && lastInMemory !== null && inMemory !== lastInMemory) {
        // The agent left memory (or came back into it) since the last read, so `since` now names a different
        // numbering: a stored line number is not an index into the in-memory buffer, and an engine-persisted run
        // stores a different set of lines than it streamed. This answer is no continuation of what is drawn, so
        // read the whole run again; replace() then draws only what is new, or redraws.
        left = true;
        cursor = 0;
        data = await fetchAPI(apiUrl('/agents/' + id + '/output?since=0'));
        if (!isCurrent()) return;
        inMemory = Number.isInteger(data.seq);
      }
      lastInMemory = inMemory;
      var lines = data.lines || [];
      var times = Array.isArray(data.times) ? data.times : null;
      // A full read goes through replace(), which caps the lines and draws once; appending a long run line by
      // line would lay the pane out again for every line.
      if (cursor === 0) renderer.replace(lines, times);
      else lines.forEach(function(text, i) { renderer.append(text, times ? times[i] : null); });
      if (lines.length > 0 || left) {
        // In memory the cursor is the run's lifetime line count (seq): the server counts `since` the same way, so it
        // stays valid after the buffer evicts. A count of the lines drawn is not (the buffer holds only the tail).
        // Out of memory it is the stored line count the read returned.
        cursor = inMemory ? data.seq : data.total;
        backoffMs = _OUTPUT_POLL_BASE_MS;
      } else {
        backoffMs = Math.min(backoffMs * 1.5, _OUTPUT_POLL_MAX_MS);
      }
      loaded = true;
      finishedRead = finalRead;
      // After the server ended the stream of a run the page still thinks is live, a read of an agent that is no
      // longer in memory that adds nothing (idle, or the re-read after it left) means nothing more will be written:
      // stop instead of polling for good, and remember the status so ensure() does not restart a run that is still
      // paused or awaiting approval.
      if (untilGone && !inMemory && (lines.length === 0 || left)) {
        finishedRead = true;
        idleAt = pageStatus;
      }
    } catch (e) {
      if (!isCurrent()) return;
      backoffMs = Math.min(backoffMs * 1.5, _OUTPUT_POLL_MAX_MS);
    }
    if (finishedRead) { transport = null; return; }
    if (isCurrent()) pollTimer = setTimeout(poll, backoffMs);
  }

  function startPolling(once, afterServerDone) {
    cancelPolling();
    transport = 'poll';
    readOnce = once;
    untilGone = afterServerDone === true;
    lastInMemory = null;
    idleAt = null;
    backoffMs = _OUTPUT_POLL_BASE_MS;
    poll();
  }

  // Drops the stream and re-reads the run from line 0 by polling.
  function fallBackToPolling(es, id) {
    if (es === source) closeSource();
    if (runId === id) {
      cursor = 0;
      renderer.clear();
      startPolling(false);
    }
  }

  async function reconcile(id, es, resume) {
    var snapshot = null;
    var times = null;
    var snapshotSeq = null;
    var controller = new AbortController();
    var deadline = setTimeout(function() { controller.abort(); }, _OUTPUT_BACKFILL_TIMEOUT_MS);
    try {
      var data = await fetchAPI(apiUrl('/agents/' + id + '/output?since=0'), { signal: controller.signal });
      snapshot = data.lines || [];
      times = data.times;
      snapshotSeq = Number.isInteger(data.seq) ? data.seq : null;
    } catch (e) {
      console.warn('Output backfill failed', e);
    } finally {
      clearTimeout(deadline);
    }
    // Switched runs, stopped, or hid the tab again: this stream is no longer the live one.
    if (id !== runId || es !== source) return;
    if (snapshot === null) {
      // The lines written while the tab was hidden are unknown, and appending the held ones to the old content
      // would hide the gap. Re-read the whole run instead.
      fallBackToPolling(es, id);
      return;
    }
    renderer.replace(snapshot, times);
    loaded = true;
    var held = resume.held.slice(_outputOverlap(snapshot, resume.held, resume.connectedSeq, snapshotSeq));
    resume.holding = false;
    resume.held = [];
    held.forEach(function(h) { renderer.append(h.text, h.time); });
    if (resume.done) endStream();
  }

  // The server ended the stream. A run the page still considers live (a paused one, or one whose status push has not
  // landed yet) keeps being read by polling until the agent has left memory, so ensure() has no reason to restart
  // (clear and re-read) it meanwhile; when the chain ends on a status that is not finished, idleAt remembers it and
  // ensure() restarts only once the status changes. A finished run is fully read. The read starts from line 0: the
  // in-memory buffer holds only the last 5000 lines, so no count of the lines drawn is a valid `since` (poll() counts
  // it in the run's lifetime lines while the agent is in memory, and in stored lines after). replace() draws only the
  // lines the held ones do not already show.
  function endStream() {
    source = null;
    transport = null;
    if (runId !== null && !sovaOutputFinished(pageStatus)) {
      cursor = 0;
      startPolling(false, true);
    }
  }

  // resume is the state of a backfilled stream: lines pushed while the backfill is in flight are held in
  // resume.held instead of drawn (see reconcile()).
  function connect(id, resume) {
    closeSource();
    var es = new EventSource(apiUrl('/agents/' + id + '/output/stream'));
    source = es;
    transport = 'sse';

    es.addEventListener('output', function(e) {
      if (runId !== id) return;
      var line = _outputParseEvent(e.data);
      if (resume.holding) { resume.held.push(line); return; }
      renderer.append(line.text, line.time);
    });

    // The server subscribes before it sends "connected" and reports in it how many lines the run had buffered by
    // then (seq): everything written from here on is pushed to this stream, everything before it is in the backfill.
    es.addEventListener('connected', function(e) {
      // A late "connected" after the wait timer already gave the stream up has nothing left to reconcile.
      if (!resume.holding) return;
      clearTimeout(resume.connectTimer);
      resume.connectedSeq = _outputParseSeq(e.data);
      reconcile(id, es, resume);
    });

    // The server dropped a line for this subscriber (its queue was full), so the stream has a hole the reader
    // cannot locate: the sequence arithmetic would be off by the missing lines. Re-read the whole run instead.
    es.addEventListener('gap', function() {
      if (runId !== id || es !== source) return;
      fallBackToPolling(es, id);
    });

    es.addEventListener('done', function() {
      if (resume.holding) {
        // The run ended while the backfill is in flight. This stays the live stream until reconcile() has drawn
        // the backfill and the held lines, and retires it then.
        resume.done = true;
        es.close();
        return;
      }
      if (es === source) { es.close(); endStream(); }
    });

    es.onerror = function() { fallBackToPolling(es, id); };

    // Every line is held until "connected" arrives, and onerror does not fire while a socket stays open, so a
    // proxy that buffers the first event would leave the pane empty for good. Past the wait the stream is given
    // up for polling, which reads the run from line 0 and needs no subscription point to be exact.
    resume.connectTimer = setTimeout(function() {
      if (resume.holding && resume.connectedSeq === null && es === source && runId === id) {
        fallBackToPolling(es, id);
      }
    }, _OUTPUT_CONNECT_WAIT_MS);
  }

  // The SSE stream does not replay output, so the lines produced before it connected come from a full fetch. The
  // stream is opened first and holds its lines back until that fetch lands: connecting only afterwards would lose
  // whatever the agent wrote in between.
  function openStream(id) {
    backoffMs = _OUTPUT_POLL_BASE_MS;
    connect(id, { holding: true, held: [], connectedSeq: null, done: false, connectTimer: null });
  }

  function onVisibility() {
    if (runId === null) return;
    if (document.hidden) {
      closeSource();
      cancelPolling();
    } else if (transport === 'poll') {
      // The agent may have left memory while the tab was hidden, which changes what `since` means, and the chain's
      // memory of which numbering the cursor is in is reset by startPolling(). Read the whole run again: replace()
      // draws only what is new, or redraws.
      cursor = 0;
      startPolling(readOnce, untilGone);
    } else if (transport === 'sse') {
      openStream(runId);
    }
  }

  function stop() {
    runId = null;
    pageStatus = null;
    transport = null;
    idleAt = null;
    cursor = 0;
    loaded = false;
    cancelPolling();
    closeSource();
    if (listening) { document.removeEventListener('visibilitychange', onVisibility); listening = false; }
  }

  function start(id, runStatus) {
    stop();
    runId = id;
    pageStatus = runStatus;
    renderer.clear();
    document.addEventListener('visibilitychange', onVisibility);
    listening = true;
    if (sovaOutputFinished(runStatus)) startPolling(true);
    else openStream(id);
  }

  return {
    start: start,
    stop: stop,
    ensure: function(id, runStatus) {
      if (runId === id) pageStatus = runStatus;
      // A run that failed over to polling while live is now finished: its next read is the last one.
      if (runId === id && transport === 'poll' && sovaOutputFinished(runStatus)) { readOnce = true; return; }
      if (runId === id && (transport !== null || (loaded && sovaOutputFinished(runStatus)))) return;
      // The poll chain ended on an agent that has left memory while the run is paused or awaiting approval: nothing
      // more is written until its status changes, so a page re-render must not clear and re-read the pane.
      if (runId === id && loaded && idleAt !== null && idleAt === runStatus) return;
      start(id, runStatus);
    },
    runId: function() { return runId; }
  };
}
// end of the agent output helpers
