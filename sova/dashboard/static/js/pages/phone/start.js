// Phone page, starting a run: the Work page's own issue search (work/search.js, GET /work/search) finds any open
// issue by its number or words of its title, and choosing one starts the agent its state calls for. That is the
// `start` action the server attaches to every result (sova.work.state.start_action(): the researcher on a triaged issue,
// the developer on a researched one), sent through the Work page's own start (work/actions.js startOrQueue(), which
// queues the issue when no slot is free). The server's refusal (unsuitable, duplicate, over budget) is shown as it is.
//
// Each result names the run a tap starts; an issue whose state runs no agent (in review, done) says so, and choosing
// it sends nothing. A start is confirmed first: a stray tap in the list must not spend an agent run.

import { startOrQueue } from '../work/actions.js';
import { init as initSearch } from '../work/search.js';
import { setSectionHtml } from './api.js';
import { loadRunning } from './running.js';

var _KEY = 'phone-start';
var _notice = '';      // why the last choice started nothing, until the next choice

function _role(entry) {
  return (entry && entry.start && entry.start.handler_args && entry.start.handler_args.role) || '';
}

function _label(entry) {
  return (entry.start && entry.start.label) || formatRole(_role(entry));
}

// The notice and the start's own status show together: a choice that starts nothing must not hide a start's refusal.
function _render() {
  setSectionHtml('phone-start-status', (_notice
    ? '<p class="text-xs text-accent-yellow break-words" role="status">' + escapeHtml(_notice) + '</p>'
    : '') + actionStatusHtml(_KEY));
}

// The right side of a search result: the run a tap starts, in its role's colour, or that none does.
function _describe() {
  return function(entry) {
    var role = _role(entry);
    if (!role) return '<span class="text-xs text-gray-500 whitespace-nowrap">no run in its state</span>';
    var rc = roleColor(role);
    return '<span class="sova-badge ' + rc.bg + ' ' + rc.text + '">' + escapeHtml(_label(entry)) + '</span>';
  };
}

export async function startFromSearch(entry) {
  var role = _role(entry);
  if (!role) {
    _notice = 'No agent runs on #' + entry.number + ' in its current state.';
    _render();
    return;
  }
  _notice = '';
  _render();
  if (actionIsRunning(_KEY)) return;
  var label = _label(entry);
  if (!(await sovaConfirm(label + ' #' + entry.number + '?', {
    title: label, confirmText: label, details: entry.title ? [entry.title] : [],
  }))) return;
  await runTrackedAction(_KEY, 'Starting #' + entry.number, function() { return startOrQueue(entry.number, role); });
  await loadRunning();
}

export function init() {
  initSearch({ prefix: 'phone-search', onChoose: startFromSearch, describe: _describe });
  onActionStateChange(_render);
}
