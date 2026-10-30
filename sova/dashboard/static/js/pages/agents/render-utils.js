// Pure render helpers shared by the PR tracker and the work-item cards.
//
// A leaf module on purpose: it imports nothing from the other page modules, so the two modules that
// both need these helpers (pr-tracker.js, work-items.js) no longer import each other for them.
//
// Stateless, so it exports no init() (unlike the page modules): there is nothing to start, bind or poll.

export function formatAge(seconds) {
  if (seconds < 3600) return Math.floor(seconds / 60) + 'm';
  if (seconds < 86400) return Math.floor(seconds / 3600) + 'h';
  return Math.floor(seconds / 86400) + 'd';
}

function ageBadgeClasses(days) {
  if (days > 14) return 'bg-accent-red/20 text-accent-red';
  if (days > 7) return 'bg-accent-peach/20 text-accent-peach';
  if (days > 3) return 'bg-accent-yellow/20 text-accent-yellow';
  if (days >= 1) return 'bg-gray-600/20 text-gray-400';
  return 'bg-accent-green/20 text-accent-green';
}

var _ageDateFmt = { year: 'numeric', month: 'short', day: 'numeric' };

export function formatAgeBadge(seconds, createdAt) {
  if (!seconds || seconds <= 0) return '';
  var days = seconds / 86400;
  var cls = ageBadgeClasses(days);
  var tooltip = '';
  if (createdAt) {
    var d = new Date(createdAt);
    if (!isNaN(d.getTime())) {
      tooltip = 'Created ' + d.toLocaleDateString(undefined, _ageDateFmt);
    } else {
      tooltip = 'Created ' + String(createdAt).split('T')[0];
    }
  }
  if (!tooltip) {
    var approx = new Date(Date.now() - seconds * 1000);
    tooltip = 'Created ' + approx.toLocaleDateString(undefined, _ageDateFmt);
  }
  var tooltipAttr = tooltip ? ' data-tooltip="' + escapeHtml(tooltip) + '"' : '';
  return '<span class="text-xs px-1.5 py-0.5 rounded-full shrink-0 ' + cls + '"' + tooltipAttr + '>' + formatAge(seconds) + '</span>';
}

export function diffSizeLabel(additions, deletions) {
  var total = (additions || 0) + (deletions || 0);
  if (total === 0) return '';
  if (total <= 50) return 'S';
  if (total <= 200) return 'M';
  if (total <= 500) return 'L';
  return 'XL';
}

export function diffSizeColor(additions, deletions) {
  var total = (additions || 0) + (deletions || 0);
  if (total <= 50) return 'text-accent-green';
  if (total <= 200) return 'text-accent';
  if (total <= 500) return 'text-accent-yellow';
  return 'text-accent-red';
}

// The data-hide-on-error attribute is acted on by the capture-phase error listener in actions.js init(),
// which every page module depends on, so a failed GitHub avatar is hidden wherever this markup lands.
export function renderAssigneeAvatar(login) {
  if (!login) return '';
  return '<img src="https://avatars.githubusercontent.com/' + encodeURIComponent(login) + '?size=20" ' +
    'alt="" width="16" height="16" loading="lazy" ' +
    'class="rounded-full shrink-0" data-tooltip="' + escapeHtml(login) + '" ' +
    'data-hide-on-error>';
}

export function renderJiraInitial(name) {
  if (!name) return '';
  var initial = name.charAt(0).toUpperCase();
  return '<span class="w-4 h-4 rounded-full bg-gray-600 flex items-center justify-center text-[9px] text-gray-300 shrink-0" ' +
    'data-tooltip="' + escapeHtml(name) + '">' + escapeHtml(initial) + '</span>';
}
