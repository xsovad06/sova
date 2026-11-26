import { registerActions } from '../agents/actions.js';
import { openLog } from '../agents/log-viewer.js';
import { loadBoard, setDragging, showStage, toggleGroup, toggleHidden } from './board.js';
import { isSelected, select } from './selection.js';

// Every button on the Work board, and the drag from Up next into Queued or onto Running.
//
// Each action runs through runTrackedAction() (app.js, the shared running/result helper), keyed by the card's
// issue, so the card shows "Starting...", then the result: a success briefly, an error until dismissed. The
// board reloads after each one; the socket also announces the change to every other tab.

var _NO_FREE_SLOT = 'no_free_slot';

async function _send(method, path, body, fallback) {
  var opts = { method: method, headers: jsonCsrfHeaders() };
  if (body !== undefined) opts.body = JSON.stringify(body);
  var res = await fetch(apiUrl(path), opts);
  if (!res.ok) {
    var err = new Error(await getErrorDetail(res.clone(), fallback));
    err.status = res.status;
    try {
      var data = await res.json();
      err.code = data && data.detail && data.detail.code;
    } catch (_e) { /* no JSON body: no code */ }
    throw err;
  }
  return res.json().catch(function() { return {}; });
}

// Keyed like the card (entryKey): by issue, or by PR for a card with no issue, so a PR-only card shows the
// running state and disables its buttons too, and by run for one with neither (a planner's Stop).
function _track(issue, label, fn, pr, run) {
  return runTrackedAction(actionKey(issue, pr, run), label, fn).then(function(result) {
    loadBoard();
    return result;
  });
}

// Queue an issue at the end of the supervisor queue; an issue already queued is a success, not an error.
// Resolves to its position (1-based) and the queue's length.
export async function queueIssue(issue) {
  var queue;
  try {
    queue = (await _send('POST', '/supervisor/queue', { issue_number: Number(issue) }, 'Could not queue #' + issue)).queue;
  } catch (e) {
    if (e.status !== 409) throw e;
    queue = (await fetchAPI(apiUrl('/supervisor/queue'))).queue;
  }
  queue = queue || [];
  return { position: queue.indexOf(Number(issue)) + 1, length: queue.length };
}

// Start the agent an issue's stage calls for: `role` is its board entry's `start` action's role (the server's
// work_state.start_action()), so a triaged issue gets the researcher and a researched one the developer. With no
// free slot the server refuses with code no_free_slot, and the issue is queued instead; the returned message
// says which happened.
export async function startOrQueue(issue, role) {
  if (!role) throw new Error('No agent runs on #' + issue + ' in its current state');
  try {
    await _send('POST', '/agents/start', { issue: String(issue), role: role }, 'Could not start #' + issue);
    return 'Started the ' + role + ' agent on #' + issue;
  } catch (e) {
    if (e.code !== _NO_FREE_SLOT) throw e;
    var queued = await queueIssue(issue);
    return 'No free slot, so #' + issue + ' is queued at position ' + queued.position +
      '. It starts when a slot frees and the supervisor is on.';
  }
}

function _start(issue, role) {
  return _track(issue, 'Starting', function() { return startOrQueue(issue, role); });
}

function _queue(issue) {
  return _track(issue, 'Queueing', async function() {
    var q = await queueIssue(issue);
    return 'Queued at position ' + q.position + ' of ' + q.length;
  });
}

function _unqueue(issue) {
  return _track(issue, 'Removing from the queue', async function() {
    await _send('DELETE', '/supervisor/queue/' + encodeURIComponent(issue), undefined, 'Could not remove #' + issue + ' from the queue');
    return 'Removed from the queue';
  });
}

function _moveInQueue(issue, delta) {
  return _track(issue, 'Moving', async function() {
    var base = ((await fetchAPI(apiUrl('/supervisor/queue'))).queue || []).slice();
    var queue = base.slice();
    var i = queue.indexOf(Number(issue));
    var j = i + delta;
    if (i === -1) throw new Error('#' + issue + ' is no longer in the queue');
    if (j < 0 || j >= queue.length) return 'Already at the ' + (delta < 0 ? 'top' : 'bottom');
    var tmp = queue[i];
    queue[i] = queue[j];
    queue[j] = tmp;
    // The server rebases the move onto the stored queue, so a concurrent change is kept; it answers with the result.
    var res = await _send('PUT', '/supervisor/queue', { issue_numbers: queue, based_on: base }, 'Could not reorder the queue');
    var pos = ((res && res.queue) || queue).indexOf(Number(issue)) + 1;
    return 'Now at position ' + pos;
  });
}

function _stop(runId, issue, pr) {
  return _track(issue, 'Stopping', async function() {
    await _send('POST', '/agents/' + encodeURIComponent(runId) + '/stop', undefined, 'Could not stop the agent');
    return 'Stopped';
  }, pr, runId);
}

function _startRole(issue, role, prNumber, label) {
  return _track(issue, label, async function() {
    var body = { issue: issue != null ? String(issue) : '', role: role };
    if (prNumber) body.pr_number = Number(prNumber);
    await _send('POST', '/agents/start', body, 'Could not start the ' + role + ' agent');
    return 'Started the ' + role + ' agent';
  }, prNumber);
}

function _runCommand(issue, command, args) {
  return _track(issue, 'Starting /' + command, async function() {
    await _send('POST', '/agents/command', { command: command, args: args }, 'Could not start /' + command);
    return 'Started /' + command + (args.pr ? ' for PR #' + args.pr : '');
  }, args.pr);
}

// A command on the card's PR (/integrate-pr, /address-pr), with its issue when it has one.
function _prCommand(issue, prNumber, command) {
  var args = { pr: Number(prNumber) };
  if (issue != null) args.issue = String(issue);
  return _runCommand(issue, command, args);
}

// One of the work item's own actions (the board entry's `actions`, from work_state on the server): its
// handler and arguments say what to run, so the page needs no table of which state offers what. An action
// carrying merge_blockers (an Integrate on a PR not fully ready) asks first, listing each unmet condition, in
// the warning style (which opens with Cancel focused).
async function _runServerAction(issue, action) {
  var a = (action && action.handler_args) || {};
  var blockers = (action && action.merge_blockers) || [];
  if (blockers.length) {
    var ok = await sovaConfirm('This PR is not fully ready to merge. Run ' + action.label + ' anyway?', {
      title: 'Not ready to merge', details: blockers, confirmText: action.label + ' anyway', confirmClass: 'warning',
    });
    if (!ok) return;
  }
  if (action.handler === 'start_agent') return _startRole(a.issue || issue, a.role, a.pr, 'Starting ' + action.label);
  if (action.handler === 'trigger_rebase') return _rebase(a.issue || issue);
  if (action.handler === 'run_command') {
    var args = {};
    if (a.issue) args.issue = String(a.issue);
    if (a.pr) args.pr = Number(a.pr);
    return _runCommand(issue, a.command, args);
  }
}

function _approve(runId, issue) {
  return _track(issue, 'Approving and starting', async function() {
    await _send('POST', '/agents/' + encodeURIComponent(runId) + '/resume-from-approval', undefined, 'Could not approve the spec');
    return 'Approved; the run continues';
  });
}

function _rebase(issue) {
  // The server runs the rebase in the background and lists it as a running action, so the card keeps
  // showing "Rebasing..." until it ends, through refreshes and reloads; this only reports the request.
  return _track(issue, 'Starting the rebase', async function() {
    var result = await _send('POST', '/work/issue/' + encodeURIComponent(issue) + '/rebase', undefined, 'Could not start the rebase');
    return result.status === 'running' ? 'A rebase is already running' : 'Rebase started';
  });
}

function _setHidden(issue, hidden) {
  return _track(issue, hidden ? 'Hiding' : 'Unhiding', async function() {
    await _send(hidden ? 'POST' : 'DELETE', '/work/hidden/' + encodeURIComponent(issue), undefined,
      hidden ? 'Could not hide #' + issue : 'Could not unhide #' + issue);
    return hidden ? 'Hidden from Up next' : 'Back in Up next';
  });
}

function _copyCommand(issue, command) {
  return runTrackedAction(actionKey(issue, null), 'Copying', async function() {
    if (!navigator.clipboard) throw new Error('This browser cannot copy here. The command is: ' + command);
    await navigator.clipboard.writeText(command);
    return 'Copied: ' + command;
  });
}

// Drag and drop -----------------------------------------------------------------

function _clearDropMarks() {
  document.querySelectorAll('.sova-work-col-drop').forEach(function(c) { c.classList.remove('sova-work-col-drop'); });
}

// The dragged card's Start role (its data-start-role) travels in the drag's own data under this type, so a drop
// that did not start from a card (text dragged in from another window) carries none and starts nothing.
var _DRAG_ROLE_TYPE = 'application/x-sova-start-role';

function _initDrag(host) {
  host.addEventListener('dragstart', function(e) {
    var card = e.target.closest && e.target.closest('[data-drag-issue]');
    if (!card) return;
    setDragging(true);
    e.dataTransfer.setData('text/plain', card.getAttribute('data-drag-issue'));
    e.dataTransfer.setData(_DRAG_ROLE_TYPE, card.getAttribute('data-start-role') || '');
    e.dataTransfer.effectAllowed = 'move';
  });
  host.addEventListener('dragend', function() {
    _clearDropMarks();
    setDragging(false);
  });
  host.addEventListener('dragover', function(e) {
    var col = e.target.closest && e.target.closest('[data-drop]');
    if (!col) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    if (!col.classList.contains('sova-work-col-drop')) {
      _clearDropMarks();
      col.classList.add('sova-work-col-drop');
    }
  });
  host.addEventListener('dragleave', function(e) {
    var col = e.target.closest && e.target.closest('[data-drop]');
    if (col && !col.contains(e.relatedTarget)) col.classList.remove('sova-work-col-drop');
  });
  host.addEventListener('drop', function(e) {
    var col = e.target.closest && e.target.closest('[data-drop]');
    if (!col) return;
    e.preventDefault();
    _clearDropMarks();
    setDragging(false);
    var issue = Number(e.dataTransfer.getData('text/plain'));
    if (!issue) return;
    if (col.getAttribute('data-drop') === 'queued') _queue(issue);
    else if (col.getAttribute('data-drop') === 'running') {
      // Only a drag a card of this page began carries the role type (empty when its stage runs no agent, which
      // _start() then reports on the card); any other drop is ignored.
      if (Array.from(e.dataTransfer.types || []).indexOf(_DRAG_ROLE_TYPE) !== -1) {
        _start(issue, e.dataTransfer.getData(_DRAG_ROLE_TYPE));
      }
    }
  });
}

export function init() {
  registerActions({
    // A second click on the selected card closes its details, as a click outside them does.
    'work-select': function(el, e, key) { select(isSelected(key) ? null : key); },
    'work-start': function(el, e, issue, role) { _start(issue, role); },
    'work-queue': function(el, e, issue) { _queue(issue); },
    'work-unqueue': function(el, e, issue) { _unqueue(issue); },
    'work-queue-move': function(el, e, issue, delta) { _moveInQueue(issue, delta); },
    'work-stop': function(el, e, runId, issue, pr) { _stop(runId, issue, pr); },
    'work-view-log': function(el, e, runId, status) { openLog(runId, status); },
    'work-review': function(el, e, issue, prNumber) { _startRole(issue, 'reviewer', prNumber, 'Starting the review'); },
    'work-integrate': function(el, e, issue, prNumber) { _prCommand(issue, prNumber, 'integrate-pr'); },
    'work-address': function(el, e, issue, prNumber) { _prCommand(issue, prNumber, 'address-pr'); },
    'work-run-action': function(el, e, issue, action) { _runServerAction(issue, action); },
    'work-approve': function(el, e, runId, issue) { _approve(runId, issue); },
    'work-retry': function(el, e, issue, role, prNumber) { _startRole(issue, role, prNumber, 'Retrying'); },
    'work-triage': function(el, e, issue) { _startRole(issue, 'triage', null, 'Starting triage'); },
    'work-rebase': function(el, e, issue) { _rebase(issue); },
    'work-hide': function(el, e, issue) { _setHidden(issue, true); },
    'work-unhide': function(el, e, issue) { _setHidden(issue, false); },
    'work-copy-command': function(el, e, issue, command) { _copyCommand(issue, command); },
    'work-toggle-group': function(el, e, stage) { toggleGroup(stage); },
    'work-toggle-hidden': function() { toggleHidden(); },
    // A stage chip (under 768 px): that stage's column on screen.
    'work-stage': function(el, e, stage) { showStage(stage); },
  });
  var host = document.getElementById('work-board');
  if (host) _initDrag(host);
}
