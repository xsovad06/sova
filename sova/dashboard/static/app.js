/* SOVA Dashboard -- shared JS utilities */

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
      return '<tr class="border-t border-gray-700/30 hover:bg-surface-hover cursor-pointer" onclick="window.location=\'/runs/' + r.id + '\'">' +
        '<td class="p-3 text-accent">#' + escapeHtml(r.issue_number) + '</td>' +
        '<td class="p-3 text-gray-300">' + escapeHtml(r.role) + '</td>' +
        '<td class="p-3"><span class="flex items-center gap-1.5"><span class="w-2 h-2 rounded-full ' + statusDot(r.status) + '"></span><span class="' + statusColor(r.status) + '">' + escapeHtml(r.status) + '</span></span></td>' +
        '<td class="p-3 text-gray-400">' + escapeHtml(r.current_step || '--') + '</td>' +
        '<td class="p-3 text-right text-accent-green">$' + r.total_cost_usd.toFixed(4) + '</td>' +
        '<td class="p-3 text-gray-500 text-xs">' + (r.started_at ? new Date(r.started_at).toLocaleString() : '--') + '</td>' +
        '</tr>';
    }).join('') + '</tbody></table>';
}
