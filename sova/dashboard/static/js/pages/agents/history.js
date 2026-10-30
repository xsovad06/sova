import { actionAttrs, registerActions } from './actions.js';

// History / Failed tabs

// The tab on screen, so a tab return refreshes the list the user is looking at rather than a hidden one.
var _activeTab = 'history';

function switchTab(tab) {
  _activeTab = tab;
  ['history', 'failed'].forEach(function(t) {
    var panel = document.getElementById('panel-' + t);
    var tabBtn = document.getElementById('tab-' + t);
    if (t === tab) {
      panel.classList.remove('hidden');
      tabBtn.className = 'px-4 py-2.5 text-sm transition-colors border-b-2 border-accent text-white';
    } else {
      panel.classList.add('hidden');
      tabBtn.className = 'px-4 py-2.5 text-sm transition-colors border-b-2 border-transparent text-gray-500 hover:text-gray-300';
    }
  });

  if (tab === 'history') loadHistory();
  if (tab === 'failed') loadFailed();
}

var _PAGE_SIZE = 15;
var historyPage = 0;
var historyTotal = 0;
var failedPage = 0;
var failedTotal = 0;

async function loadHistory() {
  try {
    var role = document.getElementById('filter-role').value;
    var status = document.getElementById('filter-status').value;
    var offset = historyPage * _PAGE_SIZE;
    var url = '/work/history?limit=' + _PAGE_SIZE + '&offset=' + offset;
    if (role) url += '&role=' + role;
    if (status) url += '&status=' + status;

    var data = await fetchAPI(apiUrl(url));
    historyTotal = data.total || 0;
    renderHistoryTable(data.tasks, 'history-table');
    _updatePager('history', historyPage, historyTotal, _PAGE_SIZE);
  } catch (e) {
    console.error('Failed to load history:', e);
  }
}

function historyPrev() { if (historyPage > 0) { historyPage--; loadHistory(); } }
function historyNext() { if ((historyPage + 1) * _PAGE_SIZE < historyTotal) { historyPage++; loadHistory(); } }

async function loadFailed() {
  try {
    var offset = failedPage * _PAGE_SIZE;
    var data = await fetchAPI(apiUrl('/work/history?status=failed&limit=' + _PAGE_SIZE + '&offset=' + offset));
    failedTotal = data.total || 0;
    var countEl = document.getElementById('tab-failed-count');
    countEl.textContent = failedTotal > 0 ? '(' + failedTotal + ')' : '';
    renderHistoryTable(data.tasks, 'failed-table', true);
    _updatePager('failed', failedPage, failedTotal, _PAGE_SIZE);
  } catch (e) {
    console.error('Failed to load failed:', e);
  }
}

function failedPrev() { if (failedPage > 0) { failedPage--; loadFailed(); } }
function failedNext() { if ((failedPage + 1) * _PAGE_SIZE < failedTotal) { failedPage++; loadFailed(); } }

function _updatePager(prefix, page, total, pageSize) {
  var pager = document.getElementById(prefix + '-pager');
  if (total <= pageSize) { pager.classList.add('hidden'); return; }
  pager.classList.remove('hidden');
  var totalPages = Math.ceil(total / pageSize);
  document.getElementById(prefix + '-page-info').textContent = 'Page ' + (page + 1) + ' of ' + totalPages;
  document.getElementById(prefix + '-prev').disabled = page === 0;
  document.getElementById(prefix + '-next').disabled = page + 1 >= totalPages;
}

function renderHistoryTable(tasks, targetId, showError) {
  var el = document.getElementById(targetId);
  if (tasks.length === 0) {
    el.innerHTML = '<div class="bg-surface-card rounded-lg border border-gray-700/50 p-8 text-center">' +
      '<p class="text-gray-500">No runs found</p></div>';
    return;
  }

  var prefix = window.SOVA_PROJECT_SLUG ? '/p/' + window.SOVA_PROJECT_SLUG : '';

  el.innerHTML = '<div class="bg-surface-card rounded-lg border border-gray-700/50 overflow-hidden">' +
    '<table class="w-full text-sm">' +
    '<thead><tr class="text-gray-500 text-xs uppercase border-b border-gray-700/50">' +
      '<th class="text-left p-3">Issue / PR</th>' +
      '<th class="text-left p-3">Role</th>' +
      '<th class="text-left p-3">Status</th>' +
      '<th class="text-left p-3">Steps</th>' +
      '<th class="text-right p-3">Cost</th>' +
      '<th class="text-left p-3">Duration</th>' +
      '<th class="text-left p-3">PR</th>' +
      '<th class="text-left p-3">Date</th>' +
      '<th class="text-center p-3" style="width:5rem"></th>' +
    '</tr></thead><tbody>' +
    tasks.map(function(t) {
      var colors = roleColor(t.role);
      var stepsText = t.total_steps_possible ? (t.steps_completed + '/' + t.total_steps_possible) : '--';
      var canRerun = (t.status === 'failed' || t.status === 'interrupted' || t.status === 'stopped') && t.issue_number;
      var rerunCell = canRerun
        ? '<td class="p-3 text-center"><button class="rerun-btn px-2 py-0.5 rounded text-xs bg-accent-yellow/10 text-accent-yellow hover:bg-accent-yellow/20 transition-colors" data-action="rerun" ' +
          'data-issue="' + escapeHtml(String(t.issue_number)) + '" data-role="' + escapeHtml(t.role || '') + '" data-pr="' + escapeHtml(String(parseInt(t.pr_number, 10) || '')) + '" ' +
          'data-run-id="' + escapeHtml(String(t.id)) + '" ' +
          'data-tooltip="Re-run this task">Re-run</button></td>'
        : '<td class="p-3"></td>';

      return '<tr class="border-t border-gray-700/30 hover:bg-surface-hover cursor-pointer"' + actionAttrs('navigate', [prefix + '/work/' + t.id]) + '>' +
        '<td class="p-3">' + issueOrPrLink(t.issue_number, t.pr_number) + '</td>' +
        '<td class="p-3"><span class="px-1.5 py-0.5 rounded text-xs ' + colors.bg + ' ' + colors.text + '">' + escapeHtml(formatRole(t.role, t.pipeline_variant)) + '</span></td>' +
        '<td class="p-3"><span class="flex items-center gap-1.5"><span class="w-2 h-2 rounded-full ' + statusDot(t.status) + '"></span><span class="' + statusColor(t.status) + '">' + escapeHtml(t.status) + '</span></span></td>' +
        '<td class="p-3 text-gray-400">' + stepsText + '</td>' +
        '<td class="p-3 text-right text-accent-green">$' + parseFloat(t.total_cost_usd || 0).toFixed(4) + '</td>' +
        '<td class="p-3 text-gray-400">' + (t.duration_formatted || '--') + '</td>' +
        '<td class="p-3 text-gray-400">' + prLink(t.pr_number) + '</td>' +
        '<td class="p-3 text-gray-500 text-xs">' + (t.ended_at ? new Date(t.ended_at).toLocaleDateString() : '--') + '</td>' +
        rerunCell +
      '</tr>' +
      (showError && t.error_message ? '<tr class="border-t border-gray-700/10"><td colspan="9" class="px-3 pb-3 pt-1 text-xs text-accent-red">' + escapeHtml(t.error_message) + '</td></tr>' : '');
    }).join('') +
    '</tbody></table></div>';
}

export function init() {
  registerActions({
    'switch-tab': function(el, e, tab) { switchTab(tab); },
    'history-prev': function() { historyPrev(); },
    'history-next': function() { historyNext(); },
    'failed-prev': function() { failedPrev(); },
    'failed-next': function() { failedNext(); },
  });

  ['filter-role', 'filter-status'].forEach(function(id) {
    document.getElementById(id).addEventListener('change', function() {
      historyPage = 0;
      loadHistory();
    });
  });

  loadHistory();

  // The visible list is refreshed when the tab returns, along with the pollers' own catch-up.
  document.addEventListener('visibilitychange', function() {
    if (document.hidden) return;
    if (_activeTab === 'failed') loadFailed();
    else loadHistory();
  });
}
