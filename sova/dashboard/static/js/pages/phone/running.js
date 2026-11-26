// Phone page, "Running": the agents running now (GET /agents/active, its `agents` list only: the roughly 30 s
// `completed` tail of finished runs is not shown), each with its issue's title (phone/summary.js), its step and its
// place in the pipeline, a progress bar, the time it has run, its cost so far, and Stop.
//
// Every load replaces the whole section, its empty state included; a failed request shows an error in its place,
// never the previous rows, and retries on a timer; a sequence counter drops a response a newer load has overtaken. Stop is keyed by
// run id, so one tap cannot be sent twice. A run the dashboard did not start (`sova run` in a terminal) answers 409
// with a reason, which the row shows until dismissed.

import { registerActions } from '../agents/actions.js';
import { humanStep, stepProgress } from '../agents/render-utils.js';
import { iconHtml, postJson, setSectionError, setSectionHtml } from './api.js';
import { onTitles, titleOf } from './summary.js';

var _SECTION = 'phone-running';
// A failed read retries on this timer: an open socket with no change to push would otherwise never reload it.
var _RETRY_MS = 15000;

var _seq = 0;
var _agents = null;    // the agents on screen; null while the section shows an error or has not loaded
var _retry = 0;        // the pending retry timer after a failed read, 0 for none

function _ref(a) {
  return a.issue ? '#' + a.issue : (a.pr_number ? 'PR #' + a.pr_number : 'Run ' + a.run_id);
}

// "Develop · monitor ci · step 14 of 18 · 48m · $4.27": what runs, where it is, for how long and at what cost.
function _meta(a, progress) {
  return [
    formatRole(a.role || '', a.pipeline_variant),
    humanStep(a.current_step || 'agent'),
    progress ? 'step ' + progress.position + ' of ' + progress.total : '',
    a.elapsed_seconds != null ? formatElapsed(a.elapsed_seconds) : '',
    a.cost_usd != null ? '$' + parseFloat(a.cost_usd || 0).toFixed(2) : '',
  ].filter(Boolean).join(' · ');
}

function _bar(progress) {
  if (!progress) return '';
  return '<div class="sova-phone-progress" role="progressbar" aria-label="Step ' + progress.position + ' of ' + progress.total +
    '" aria-valuemin="0" aria-valuemax="100" aria-valuenow="' + progress.pct + '"><span style="width:' + progress.pct + '%"></span></div>';
}

function _row(a) {
  var key = actionKey(null, null, a.run_id);
  var busy = actionIsRunning(key);
  var ref = _ref(a);
  var title = titleOf(a.issue, a.issue ? null : a.pr_number);
  var progress = stepProgress(a.step_index, a.total_steps);
  return '<li class="sova-card sova-phone-row flex flex-col gap-1 min-w-0">' +
    '<div class="flex items-center gap-3 min-w-0">' +
      '<div class="flex flex-col gap-1 flex-1 min-w-0">' +
        '<div class="flex items-center gap-1.5 min-w-0">' +
          '<span class="text-sm font-semibold text-accent tabular-nums shrink-0">' + escapeHtml(ref) + '</span>' +
          (title ? '<span class="text-sm text-gray-200 truncate min-w-0">' + escapeHtml(title) + '</span>' : '') +
        '</div>' +
        '<p class="text-xs text-gray-400 break-words">' + escapeHtml(_meta(a, progress)) + '</p>' +
        _bar(progress) +
      '</div>' +
      sovaBtn('', {
        variant: 'danger', size: 'icon', className: 'shrink-0', disabled: busy, iconHtml: iconHtml('stop'),
        attrs: { 'aria-label': 'Stop the agent on ' + ref, 'data-action': 'phone-stop', 'data-args': JSON.stringify([a.run_id, ref]) },
      }) +
    '</div>' +
    actionStatusHtml(key) +
  '</li>';
}

function _render() {
  if (!_agents) return;
  setText('phone-running-count', String(_agents.length));
  setSectionHtml(_SECTION, _agents.length
    ? '<ul class="flex flex-col gap-2 min-w-0">' + _agents.map(_row).join('') + '</ul>'
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
    setText('phone-running-count', '');
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
  onTitles(_render);
}
