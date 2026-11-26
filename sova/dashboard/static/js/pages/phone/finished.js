// Phone page, "Finished today": the issues (or PRs, or lone runs) with a finished run that started since the viewer's
// own midnight, newest first. It is the run history's grouped read (GET /work/history?group=issue), the same rows the
// Work page's history shows for "Today", each with its title and what its runs cost. A row opens that history on the
// Work page, narrowed to its issue or PR.
//
// Every load replaces the section; a failed read shows an error in its place, never the earlier rows, and retries on
// a timer; a sequence counter drops a response a newer load has overtaken.

import { setSectionError, setSectionHtml } from './api.js';

var _SECTION = 'phone-finished';
var _LIMIT = 10;
var _RETRY_MS = 15000;

var _seq = 0;
var _retry = 0;

function _midnight() {
  var d = new Date();
  d.setHours(0, 0, 0, 0);
  return d.toISOString();
}

function _cost(value) {
  return '$' + parseFloat(value || 0).toFixed(2);
}

function _label(g) {
  if (g.issue_number) return '#' + g.issue_number;
  if (g.pr_number) return 'PR #' + g.pr_number;
  var run = (g.runs || [])[0] || {};
  return run.action_label || run.role || 'Run';
}

function _row(g) {
  var prefix = window.SOVA_PROJECT_SLUG ? '/p/' + window.SOVA_PROJECT_SLUG : '';
  var number = g.issue_number || g.pr_number;
  var inner = '<span class="text-sm font-semibold text-accent tabular-nums shrink-0">' + escapeHtml(_label(g)) + '</span>' +
    '<span class="text-sm text-gray-200 truncate flex-1 min-w-0">' + escapeHtml(g.title || '') + '</span>' +
    '<span class="text-xs text-accent-green tabular-nums shrink-0">' + escapeHtml(_cost(g.total_cost_usd)) + '</span>';
  var cls = 'sova-card sova-phone-row flex items-center gap-2 min-w-0';
  return '<li class="min-w-0">' + (number
    ? '<a class="' + cls + ' hover:bg-surface-hover transition-colors" href="' +
      escapeHtml(prefix + '/work?number=' + encodeURIComponent(number)) + '">' + inner + '</a>'
    : '<div class="' + cls + '">' + inner + '</div>') + '</li>';
}

export async function loadFinished() {
  var seq = ++_seq;
  var data;
  try {
    data = await fetchAPI(apiUrl('/work/history?group=issue&limit=' + _LIMIT + '&since=' + encodeURIComponent(_midnight())));
  } catch (e) {
    if (seq !== _seq) return;
    setText('phone-finished-count', '');
    setSectionError(_SECTION, 'Could not load the runs finished today. It retries on its own.');
    if (!_retry) _retry = setTimeout(function() { _retry = 0; loadFinished(); }, _RETRY_MS);
    return;
  }
  if (seq !== _seq) return;
  var groups = data.groups || [];
  var total = data.total != null ? data.total : groups.length;
  setText('phone-finished-count', total ? String(total) : '');
  var more = total - groups.length;
  setSectionHtml(_SECTION, groups.length
    ? '<ul class="flex flex-col gap-2 min-w-0">' + groups.map(_row).join('') + '</ul>' +
      (more > 0 ? '<p class="text-xs text-gray-500 mt-2">' + escapeHtml(more + ' more in the run history on the Work page') + '</p>' : '')
    : emptyStateHtml('Nothing finished today'));
}
