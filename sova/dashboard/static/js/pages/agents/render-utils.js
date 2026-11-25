// Pure render helpers for the PR tracker and the Work board's cards.
//
// A leaf module on purpose: it imports nothing from the other page modules.
//
// Stateless, so it exports no init() (unlike the page modules): there is nothing to start, bind or poll.

export function formatAge(seconds) {
  if (seconds < 3600) return Math.floor(seconds / 60) + 'm';
  if (seconds < 86400) return Math.floor(seconds / 3600) + 'h';
  return Math.floor(seconds / 86400) + 'd';
}

// The state colour token (badgeHtml()'s `color`) for a PR or issue of this age in days.
function ageBadgeColor(days) {
  if (days > 14) return 'red';
  if (days > 7) return 'peach';
  if (days > 3) return 'yellow';
  if (days >= 1) return 'gray';
  return 'green';
}

var _ageDateFmt = { year: 'numeric', month: 'short', day: 'numeric' };

export function formatAgeBadge(seconds, createdAt) {
  if (!seconds || seconds <= 0) return '';
  var days = seconds / 86400;
  var color = ageBadgeColor(days);
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
  return badgeHtml(formatAge(seconds), { color: color, className: 'shrink-0', attrs: tooltip ? { 'data-tooltip': tooltip } : {} });
}

export function diffSizeLabel(additions, deletions) {
  var total = (additions || 0) + (deletions || 0);
  if (total === 0) return '';
  if (total <= 50) return 'S';
  if (total <= 200) return 'M';
  if (total <= 500) return 'L';
  if (total <= 1000) return 'XL';
  return 'XXL';
}

export function diffSizeColor(additions, deletions) {
  var total = (additions || 0) + (deletions || 0);
  if (total <= 50) return 'text-accent-green';
  if (total <= 200) return 'text-accent';
  if (total <= 500) return 'text-accent-yellow';
  return 'text-accent-red';
}
