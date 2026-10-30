import { actionAttrs, registerActions } from './actions.js';
import { getGithubUser, onGithubUserChange } from './work-items.js';
import { diffSizeColor, diffSizeLabel, formatAge, formatAgeBadge } from './render-utils.js';

// safeStorageGet/Set, not localStorage directly: a throwing storage (blocked cookies, some private modes)
// at module evaluation would fail the whole import graph and leave the page blank.
var _prTrackerCollapsed = safeStorageGet('sova-pr-tracker-collapsed') === 'true';

function togglePrTracker() {
  _prTrackerCollapsed = !_prTrackerCollapsed;
  safeStorageSet('sova-pr-tracker-collapsed', _prTrackerCollapsed);
  var list = document.getElementById('pr-tracker-list');
  var chevron = document.getElementById('pr-tracker-chevron');
  var toggle = document.getElementById('pr-tracker-toggle');
  list.classList.toggle('hidden', _prTrackerCollapsed);
  chevron.style.transform = _prTrackerCollapsed ? 'rotate(-90deg)' : '';
  if (toggle) toggle.setAttribute('aria-expanded', (!_prTrackerCollapsed).toString());
}

// The last good open-PR list. Module-private: renderPrSummary() takes an array, so nothing outside this
// module needs the cache, and a window property would let any script overwrite it.
var _prTrackerData = [];
// null until the first /prs/open response settles, then 'ok' or 'failed'. A redraw that does not come from
// loadPrTracker() itself (the GitHub-user listener, a filter click) must not run before 'ok': it would replace
// the load-failed message, or an empty not-yet-loaded list, with an empty-list message that claims a success
// that never happened.
var _prTrackerLoaded = null;

var _STALE_THRESHOLD_SECONDS = 7 * 86400;
var _lastPrSummaryKey = null;

function renderPrSummary(prs) {
  var strip = document.getElementById('pr-summary-strip');
  try {
    prs = prs || _prTrackerData;
    if (!prs.length) {
      strip.classList.add('hidden');
      _lastPrSummaryKey = null;
      return;
    }

    var total = prs.length;
    var awaitingReview = 0;
    var changesRequested = 0;
    var ciFailing = 0;
    var approved = 0;
    var stale = 0;
    var totalAge = 0;

    for (var i = 0; i < prs.length; i++) {
      var pr = prs[i];
      var age = pr.age_seconds || 0;
      var st = pr.computed_state;
      if (st === 'awaiting_review') awaitingReview++;
      else if (st === 'changes_requested') changesRequested++;
      else if (st === 'approved_ci_green') approved++;
      if (pr.ci_status === 'failed') ciFailing++;
      totalAge += age;
      if (age >= _STALE_THRESHOLD_SECONDS) stale++;
    }

    var ageHours = Math.floor(totalAge / 3600);
    var key = [total, awaitingReview, changesRequested, ciFailing, approved, stale, ageHours].join(',');
    if (key === _lastPrSummaryKey) return;
    _lastPrSummaryKey = key;

    var avgAge = total > 0 ? formatAge(Math.floor(totalAge / total)) : '0d';

    var metrics = [
      { label: 'Open', value: total, color: 'text-gray-300' },
      { label: 'Review', value: awaitingReview, color: 'text-accent' },
      { label: 'Changes', value: changesRequested, color: 'text-accent-peach' },
      { label: 'CI Fail', value: ciFailing, color: 'text-accent-red' },
      { label: 'Ready', value: approved, color: 'text-accent-green' },
      { label: 'Stale', value: stale, color: stale > 0 ? 'text-accent-yellow' : 'text-gray-500' },
    ];

    // grid-cols-6 must match metrics.length (currently 6)
    var html = '<div class="grid grid-cols-' + metrics.length + ' gap-2 px-4 py-2 border-b border-gray-700/30">';
    for (var j = 0; j < metrics.length; j++) {
      var m = metrics[j];
      var val = m.label === 'Stale' && m.value === 0 ? avgAge : String(m.value);
      var sub = m.label === 'Stale' && m.value === 0 ? 'Avg Age' : m.label;
      html += '<div class="text-center">' +
        '<div class="text-sm font-semibold ' + m.color + '">' + escapeHtml(val) + '</div>' +
        '<div class="text-xs text-gray-500">' + escapeHtml(sub) + '</div>' +
      '</div>';
    }
    html += '</div>';

    strip.innerHTML = html;
    strip.classList.remove('hidden');
  } catch (e) {
    console.error('PR summary render failed:', e);
    strip.classList.add('hidden');
  }
}

var _NEEDS_ATTENTION_CI_AGE = 3 * 86400;

var _PR_GROUPS = [
  {
    id: 'needs-attention',
    label: 'Needs Attention',
    badgeClass: 'bg-accent-red/20 text-accent-red',
    defaultExpanded: true,
    matchFn: function(pr) {
      return pr.computed_state === 'changes_requested' ||
        pr.mergeable === 'CONFLICTING' ||
        (pr.ci_status === 'failed' && (pr.age_seconds || 0) >= _NEEDS_ATTENTION_CI_AGE);
    }
  },
  {
    id: 'awaiting-review',
    label: 'In Review',
    badgeClass: 'bg-accent/20 text-accent',
    defaultExpanded: true,
    matchFn: function(pr) {
      return pr.computed_state === 'awaiting_review' ||
        pr.computed_state === 'review_addressed' ||
        pr.computed_state === 'approved';
    }
  },
  {
    id: 'failing-ci',
    label: 'Failing CI',
    badgeClass: 'bg-accent-red/20 text-accent-red',
    defaultExpanded: false,
    matchFn: function(pr) {
      return pr.ci_status === 'failed';
    }
  },
  {
    id: 'stale',
    label: 'Stale',
    badgeClass: 'bg-accent-yellow/20 text-accent-yellow',
    defaultExpanded: false,
    matchFn: function(pr) {
      return (pr.age_seconds || 0) >= _STALE_THRESHOLD_SECONDS;
    }
  },
  {
    id: 'ready-to-merge',
    label: 'Ready to Merge',
    badgeClass: 'bg-accent-green/20 text-accent-green',
    defaultExpanded: true,
    matchFn: function(pr) {
      return pr.computed_state === 'approved_ci_green';
    }
  },
  {
    id: 'draft-in-progress',
    label: 'Draft / In Progress',
    badgeClass: 'bg-gray-600/30 text-gray-400',
    defaultExpanded: false,
    matchFn: function() { return true; }
  }
];

function categorizePrs(prs) {
  var grouped = {};
  for (var i = 0; i < _PR_GROUPS.length; i++) {
    grouped[_PR_GROUPS[i].id] = [];
  }
  for (var j = 0; j < prs.length; j++) {
    var pr = prs[j];
    for (var k = 0; k < _PR_GROUPS.length; k++) {
      if (_PR_GROUPS[k].matchFn(pr)) {
        grouped[_PR_GROUPS[k].id].push(pr);
        break;
      }
    }
  }
  return grouped;
}

function _isPrGroupCollapsed(groupId, defaultExpanded) {
  var stored = safeStorageGet('sova-pr-group-' + groupId + '-collapsed');
  if (stored !== null) return stored === 'true';
  return !defaultExpanded;
}

function togglePrGroup(groupId) {
  var content = document.getElementById('pr-group-' + groupId);
  var arrow = document.getElementById('pr-group-' + groupId + '-arrow');
  var toggle = document.getElementById('pr-group-toggle-' + groupId);
  if (!content) return;
  var isHidden = content.classList.contains('hidden');
  if (isHidden) {
    content.classList.remove('hidden');
    if (arrow) arrow.style.transform = '';
    if (toggle) toggle.setAttribute('aria-expanded', 'true');
  } else {
    content.classList.add('hidden');
    if (arrow) arrow.style.transform = 'rotate(-90deg)';
    if (toggle) toggle.setAttribute('aria-expanded', 'false');
  }
  safeStorageSet('sova-pr-group-' + groupId + '-collapsed', (!isHidden).toString());
}

function renderPrGroups(grouped) {
  var html = '';
  for (var i = 0; i < _PR_GROUPS.length; i++) {
    var group = _PR_GROUPS[i];
    var items = grouped[group.id];
    if (!items || items.length === 0) continue;

    items.sort(function(a, b) { return (b.age_seconds || 0) - (a.age_seconds || 0); });

    var collapsed = _isPrGroupCollapsed(group.id, group.defaultExpanded);
    var arrowStyle = collapsed ? ' style="transform: rotate(-90deg)"' : '';
    // In the class list, not a bare `hidden` attribute: togglePrGroup flips the class, and an attribute
    // would keep a group collapsed on first render hidden after the class is gone.
    var contentHiddenClass = collapsed ? ' hidden' : '';
    var ariaExpanded = collapsed ? 'false' : 'true';

    html += '<div class="border-b border-gray-700/30 last:border-b-0">' +
      '<button id="pr-group-toggle-' + group.id + '"' + actionAttrs('toggle-pr-group', [group.id]) + ' class="w-full flex items-center gap-2 px-4 py-2 hover:bg-surface-hover transition-colors text-left" ' +
        'aria-label="Toggle ' + escapeHtml(group.label) + ' group" ' +
        'aria-expanded="' + ariaExpanded + '" ' +
        'aria-controls="pr-group-' + group.id + '">' +
        '<svg id="pr-group-' + group.id + '-arrow" class="w-3 h-3 text-gray-500 transition-transform shrink-0"' + arrowStyle + ' fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"/></svg>' +
        '<span class="text-xs font-medium text-gray-400">' + escapeHtml(group.label) + '</span>' +
        '<span class="text-xs px-1.5 py-0.5 rounded-full ' + group.badgeClass + '">' + items.length + '</span>' +
      '</button>' +
      '<div id="pr-group-' + group.id + '" class="divide-y divide-gray-700/20' + contentHiddenClass + '">';

    for (var j = 0; j < items.length; j++) {
      html += renderPrRow(items[j]);
    }

    html += '</div></div>';
  }
  return html;
}

function renderPrRow(pr) {
  var stateClass = stateBadgeClasses(pr.state_color);

  var issueRef = pr.linked_issue
    ? '<span class="text-xs text-gray-500">#' + escapeHtml(String(pr.linked_issue)) + '</span>'
    : '';

  var ciDot = '';
  if (pr.ci_status === 'passed') ciDot = '<span class="w-1.5 h-1.5 rounded-full bg-accent-green shrink-0" data-tooltip="CI passed"></span>';
  else if (pr.ci_status === 'failed') ciDot = '<span class="w-1.5 h-1.5 rounded-full bg-accent-red shrink-0" data-tooltip="CI failed"></span>';
  else if (pr.ci_status === 'pending') ciDot = '<span class="w-1.5 h-1.5 rounded-full bg-accent-yellow animate-pulse shrink-0" data-tooltip="CI running"></span>';

  var conflictBadge = '';
  if (pr.mergeable === 'CONFLICTING') {
    conflictBadge = '<span class="px-2 py-0.5 rounded-full text-xs bg-accent-red/20 text-accent-red" data-tooltip="This PR has merge conflicts that must be resolved">Conflicts</span>';
  }

  var authorAvatar = '';
  if (pr.author) {
    authorAvatar = '<div class="flex items-center gap-1.5 shrink-0 w-28">' +
      '<img src="https://avatars.githubusercontent.com/' + encodeURIComponent(pr.author) + '?size=32" ' +
        'alt="" width="20" height="20" loading="lazy" ' +
        'class="rounded-full shrink-0" ' +
        'data-hide-on-error>' +
      '<span class="text-xs text-gray-500 truncate">' + escapeHtml(pr.author) + '</span>' +
    '</div>';
  }

  var prDiffHtml = '';
  if (pr.additions || pr.deletions) {
    prDiffHtml = '<span class="text-xs shrink-0 ' + diffSizeColor(pr.additions, pr.deletions) + '" ' +
      'data-tooltip="+' + (pr.additions || 0) + ' / -' + (pr.deletions || 0) + ' in ' + (pr.changed_files || '?') + ' files">' +
      diffSizeLabel(pr.additions, pr.deletions) + '</span>';
  }

  var prDraftBadge = '';
  if (pr.is_draft) {
    prDraftBadge = '<span class="text-xs px-1.5 py-0.5 rounded bg-gray-600/30 text-gray-400 shrink-0">Draft</span>';
  }

  var prThreadHtml = '';
  if (pr.thread_total > 0) {
    var trClr = pr.thread_resolved >= pr.thread_total ? 'text-accent-green' : 'text-accent-yellow';
    prThreadHtml = '<span class="text-xs shrink-0 ' + trClr + '" ' +
      'data-tooltip="' + pr.thread_resolved + '/' + pr.thread_total + ' review threads resolved">' +
      pr.thread_resolved + '/' + pr.thread_total + '</span>';
  }

  return '<div class="flex items-center gap-3 px-4 py-2.5 hover:bg-surface-hover transition-colors">' +
    authorAvatar +
    '<a href="' + (pr.url && pr.url.startsWith('https://') ? escapeHtml(pr.url) : '#') + '" target="_blank" rel="noopener" class="text-xs font-medium text-accent-lavender hover:underline shrink-0">PR #' + escapeHtml(String(pr.number)) + '</a>' +
    '<span class="text-sm text-gray-300 truncate flex-1">' + escapeHtml(pr.title) + '</span>' +
    issueRef +
    prDiffHtml +
    prDraftBadge +
    prThreadHtml +
    ciDot +
    conflictBadge +
    '<span class="px-2 py-0.5 rounded-full text-xs ' + stateClass + '">' + escapeHtml(pr.state_label) + '</span>' +
    (pr.age_seconds > 0 ? formatAgeBadge(pr.age_seconds, null) : '') +
  '</div>';
}

var _prFilter = safeStorageGet('sova-pr-filter') || 'all';

function _updateFilterToggleUI() {
  document.querySelectorAll('#pr-filter-toggle button').forEach(function(btn) {
    var val = btn.getAttribute('data-pr-filter');
    if (val === _prFilter) {
      btn.className = 'px-2.5 py-1 transition-colors bg-accent/20 text-accent font-medium';
    } else {
      btn.className = 'px-2.5 py-1 transition-colors text-gray-500 hover:text-gray-300';
    }
  });
}

function setPrFilter(value) {
  _prFilter = value;
  safeStorageSet('sova-pr-filter', value);
  _updateFilterToggleUI();
  _renderPrTracker();
}

function _setPrTrackerStale(stale) {
  var marker = document.getElementById('pr-tracker-stale');
  if (marker) marker.classList.toggle('hidden', !stale);
}

async function loadPrTracker() {
  try {
    var res = await fetch(apiUrl('/prs/open'));
    if (!res.ok) throw new Error('HTTP ' + res.status);
    var data = await res.json();
    _prTrackerData = data.prs || [];
    _prTrackerLoaded = 'ok';
    _setPrTrackerStale(false);
    _renderPrTracker();
  } catch (e) {
    // A failed poll keeps the last good list rather than claiming there are no
    // open PRs, and marks it stale so old states and CI dots are not read as
    // current; with nothing loaded yet, say the load failed.
    console.warn('Failed to load open PRs', e);
    if (_prTrackerData.length) {
      _setPrTrackerStale(true);
      return;
    }
    _prTrackerLoaded = 'failed';
    var list = document.getElementById('pr-tracker-list');
    if (list) list.innerHTML = '<p class="text-accent-red text-sm py-4 text-center">Failed to load open PRs</p>';
  }
}

function _renderPrTracker() {
  // Nothing has been read yet, or the read failed: loadPrTracker() owns what the panel says then, and a filter
  // click must not paint an empty-list message over a list that was never fetched.
  if (_prTrackerLoaded !== 'ok') return;
  var all = _prTrackerData;
  var visible = all;
  if (_prFilter === 'mine') {
    if (!getGithubUser()) {
      // Not "No open PRs": nothing can be filtered until the first work-items response names the user,
      // and onGithubUserChange() redraws the panel when it does.
      document.getElementById('pr-tracker-count').textContent = '';
      document.getElementById('pr-tracker-list').innerHTML =
        '<p class="text-gray-500 text-sm py-4 text-center">GitHub user not known yet, so your PRs cannot be filtered</p>';
      renderPrSummary([]);
      return;
    }
    var user = getGithubUser().toLowerCase();
    visible = all.filter(function(pr) {
      return (pr.author || '').toLowerCase() === user;
    });
  }
  var list = document.getElementById('pr-tracker-list');
  var count = document.getElementById('pr-tracker-count');
  count.textContent = visible.length ? '(' + visible.length + ')' : '';
  if (!visible.length) {
    list.innerHTML = '<p class="text-gray-500 text-sm py-4 text-center">No open PRs</p>';
  } else {
    var grouped = categorizePrs(visible);
    list.innerHTML = renderPrGroups(grouped);
  }
  renderPrSummary(visible);
}
// CodeRabbit Quota Widget

async function _loadCrQuota() {
  try {
    var resp = await fetch(apiUrl('/quota/coderabbit'));
    if (!resp.ok) return;
    var data = await resp.json();
    var widget = document.getElementById('cr-quota-widget');
    if (!data.enabled) { widget.classList.add('hidden'); return; }
    widget.classList.remove('hidden');

    var used = data.reviews_in_window || 0;
    var limit = data.reviews_per_hour || 0;
    var pct = limit > 0 ? Math.min(100, Math.round((used / limit) * 100)) : 0;

    document.getElementById('cr-quota-usage').textContent = used + ' / ' + limit + ' reviews';

    var bar = document.getElementById('cr-quota-bar');
    bar.style.width = pct + '%';

    var dot = document.getElementById('cr-quota-dot');
    var statusEl = document.getElementById('cr-quota-status');
    var waitEl = document.getElementById('cr-quota-wait');

    if (data.can_create_pr) {
      dot.className = 'w-2 h-2 rounded-full bg-accent-green';
      bar.className = 'h-1.5 rounded-full transition-all duration-300 bg-accent-green';
      statusEl.textContent = 'Available';
      statusEl.className = 'text-xs text-accent-green';
      waitEl.classList.add('hidden');
    } else {
      dot.className = 'w-2 h-2 rounded-full bg-accent-peach';
      bar.className = 'h-1.5 rounded-full transition-all duration-300 bg-accent-peach';
      statusEl.textContent = 'Rate limited';
      statusEl.className = 'text-xs text-accent-peach';
      if (data.next_available_minutes != null) {
        var mins = Math.ceil(data.next_available_minutes);
        waitEl.textContent = 'Next slot in ~' + mins + ' min';
        waitEl.classList.remove('hidden');
      }
    }
  } catch (e) { /* non-fatal */ }
}

async function _syncCrQuota() {
  try {
    var ctrl = new AbortController();
    var tid = setTimeout(function() { ctrl.abort(); }, 10000);
    await fetch(apiUrl('/quota/coderabbit/sync'), {method: 'POST', signal: ctrl.signal});
    clearTimeout(tid);
  } catch (e) { /* non-fatal: network error or timeout */ }
  await _loadCrQuota();
}

export function init() {
  registerActions({
    'toggle-pr-tracker': function() { togglePrTracker(); },
    'set-pr-filter': function(el, e, value) { setPrFilter(value); },
    'toggle-pr-group': function(el, e, groupId) { togglePrGroup(groupId); },
    'sync-cr-quota': function() { _syncCrQuota(); },
  });

  // The user only matters to the 'mine' filter, and only once the list it filters has actually loaded.
  onGithubUserChange(function() {
    if (_prFilter === 'mine' && _prTrackerLoaded === 'ok') _renderPrTracker();
  });

  _updateFilterToggleUI();

  if (_prTrackerCollapsed) {
    document.getElementById('pr-tracker-list').classList.add('hidden');
    document.getElementById('pr-tracker-chevron').style.transform = 'rotate(-90deg)';
  }

  visibilityAwarePoll(loadPrTracker, 30000);
  visibilityAwarePoll(_syncCrQuota, 300000);
}
