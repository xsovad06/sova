// Phone page, "Running": the agents running now (GET /agents/active, its `agents` list only: the roughly 30 s
// `completed` tail of finished runs is not shown) with a Stop button each.
//
// Every load replaces the whole section, its empty state included; a failed request shows an error in its place,
// never the previous rows, and retries on a timer; a sequence counter drops a response a newer load has overtaken. Stop is keyed by
// run id, so one tap cannot be sent twice. A run the dashboard did not start (`sova run` in a terminal) answers 409
// with a reason, which the row shows until dismissed.

import { registerActions } from '../agents/actions.js';
import { postJson, setSectionError, setSectionHtml } from './api.js';

var _SECTION = 'phone-running';
// A failed read retries on this timer: an open socket with no change to push would otherwise never reload it.
var _RETRY_MS = 15000;

var _seq = 0;
var _agents = null;    // the agents on screen; null while the section shows an error or has not loaded
var _retry = 0;        // the pending retry timer after a failed read, 0 for none

function _row(a) {
  var key = actionKey(null, null, a.run_id);
  var busy = actionIsRunning(key);
  var rc = roleColor(a.role || '', a.pipeline_variant);
  var steps = a.total_steps ? ' (' + a.step_index + '/' + a.total_steps + ')' : '';
  return '<li class="sova-card p-3 flex flex-col gap-2 min-w-0">' +
    '<div class="flex flex-wrap items-center gap-2 min-w-0">' +
      '<span class="text-sm font-medium text-gray-200">' + escapeHtml(a.issue ? '#' + a.issue : 'Run ' + a.run_id) + '</span>' +
      '<span class="sova-badge ' + rc.bg + ' ' + rc.text + '">' + escapeHtml(formatRole(a.role || '', a.pipeline_variant)) + '</span>' +
      statusBadge(a.status || 'running') +
    '</div>' +
    '<p class="text-xs text-gray-500 break-words">' + escapeHtml((a.current_step || 'agent') + steps + ' - ' + formatElapsed(a.elapsed_seconds)) + '</p>' +
    '<div class="flex flex-wrap gap-2">' +
      sovaBtn('Stop', {
        variant: 'danger', size: 'md', className: 'sova-phone-tap', disabled: busy,
        attrs: { 'data-action': 'phone-stop', 'data-args': JSON.stringify([a.run_id, a.issue ? '#' + a.issue : 'run ' + a.run_id]) },
      }) +
    '</div>' +
    actionStatusHtml(key) +
  '</li>';
}

function _render() {
  if (!_agents) return;
  setSectionHtml(_SECTION, _agents.length
    ? '<ul class="flex flex-col gap-3 min-w-0">' + _agents.map(_row).join('') + '</ul>'
    : emptyStateHtml('No agents running'));
}

export async function loadRunning() {
  var seq = ++_seq;
  var data;
  try {
    data = await fetchAPI(apiUrl('/agents/active'));
  } catch (e) {
    if (seq !== _seq) return;
    _agents = null;
    setSectionError(_SECTION, 'Could not load the running agents. It retries on its own.');
    if (!_retry) _retry = setTimeout(function() { _retry = 0; loadRunning(); }, _RETRY_MS);
    return;
  }
  if (seq !== _seq) return;
  _agents = data.agents || [];
  _render();
}

async function _stop(el, e, runId, name) {
  if (!(await sovaConfirm('Stop the agent on ' + name + '?', {
    title: 'Stop', confirmText: 'Stop', confirmClass: 'danger',
  }))) return;
  await runTrackedAction(actionKey(null, null, runId), 'Stopping', async function() {
    await postJson('/agents/' + encodeURIComponent(runId) + '/stop', undefined, 'Could not stop ' + name);
    return 'Stopped';
  });
  await loadRunning();
}

export function init() {
  registerActions({ 'phone-stop': _stop });
  onActionStateChange(_render);
}
