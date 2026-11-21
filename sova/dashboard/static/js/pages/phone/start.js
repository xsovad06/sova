// Phone page, "Start a run": one issue number field that posts to POST /agents/start with no role, so the server
// picks its default (the developer). The server already refuses an unsuitable, duplicate or over-budget start,
// and its text is shown as it is. Role-aware starts stay on the Work board.
//
// An empty, zero, negative, non-numeric or unsafely large entry is rejected here, with a message and no request.

import { postJson, setSectionHtml } from './api.js';
import { loadRunning } from './running.js';

var _KEY = 'phone-start';
var _invalid = '';     // the message for the last rejected entry, until the next submit or edit

function _render() {
  var btn = document.getElementById('phone-start-btn');
  if (btn) btn.disabled = actionIsRunning(_KEY);
  setSectionHtml('phone-start-status', _invalid
    ? '<p class="text-xs text-accent-red break-words" role="alert">' + escapeHtml(_invalid) + '</p>'
    : actionStatusHtml(_KEY));
}

// The issue as a positive integer string, or '' for anything else ("#42" is accepted, "4x", "0" and "-3" are not,
// nor a number past 2^53, which would round to a different issue).
function _parse(raw) {
  var text = String(raw || '').trim().replace(/^#/, '');
  if (!/^\d+$/.test(text)) return '';
  var value = Number(text);
  return Number.isSafeInteger(value) && value > 0 ? String(value) : '';
}

async function _submit(e) {
  e.preventDefault();
  var input = document.getElementById('phone-start-issue');
  var issue = _parse(input.value);
  if (!issue) {
    _invalid = 'Enter an issue number, like 42.';
    _render();
    return;
  }
  _invalid = '';
  var result = await runTrackedAction(_KEY, 'Starting #' + issue, async function() {
    await postJson('/agents/start', { issue: issue }, 'Could not start #' + issue);
    return 'Started #' + issue;
  });
  if (result.ok) input.value = '';
  await loadRunning();
}

export function init() {
  var form = document.getElementById('phone-start-form');
  if (form) form.addEventListener('submit', _submit);
  var input = document.getElementById('phone-start-issue');
  if (input) input.addEventListener('input', function() { if (_invalid) { _invalid = ''; _render(); } });
  onActionStateChange(_render);
}
