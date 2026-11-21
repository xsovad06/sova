// Phone page, "Waiting on you": the handoffs an agent left for a human (GET /handoff) and the supervisor's pending
// plan (GET /supervisor/plan), each with the buttons that answer it.
//
// Every load replaces the whole section, its empty state included. The two sources load independently: one that
// fails shows an error line in its place, never its previous rows (an old list must not pass for the current one),
// while the other still shows, and the failure retries on a timer. A sequence counter drops a slow response that a
// newer load has already overtaken. Handoff files the server could not read, or that the pipeline
// ignores, are reported above the list so "nothing waiting" is never shown while one may be hiding.
//
// Writes always name their item: /handoff/execute and /handoff/clear carry the issue (an empty one would clear
// every handoff) and /supervisor/plan/approve carries issue_numbers (an empty list approves the whole plan).

import { registerActions } from '../agents/actions.js';
import { postJson, setSectionError, setSectionHtml } from './api.js';
import { loadRunning } from './running.js';

var _SECTION = 'phone-waiting';
// A failed read retries on this timer: an open socket with no change to push would otherwise never reload it.
var _RETRY_MS = 15000;
// A write's outcome stays on screen this long after its row is gone.
var _OUTCOME_MS = 8000;
var _VARIANT_BY_STYLE = { approve: 'success', danger: 'danger' };
// Handoff actions the server cannot start: a shell mode is refused after the handoff is already cleared, and a
// dashboard-only one has no command at all.
var _UNSUPPORTED_MODES = ['shell', 'dashboard-only'];

var _seq = 0;
var _last = null;      // {handoff, plan} on screen, each null when its read failed; null before the first load
var _notices = {};     // issue -> the per-decision failures of a partly failed plan approval, whose rows are gone
var _outcomes = {};    // action key -> {text, warn}: what a write did, shown for a while once its row has left the list
var _retry = 0;        // the pending retry timer after a failed read, 0 for none

// "42" for an issue handoff, "pr:7" for one that names only a PR (what /handoff/execute accepts), "" for neither.
function _ref(h) {
  var issue = String(h.issue || '').replace(/^#/, '').trim();
  if (issue) return issue;
  return h.pr_number != null ? 'pr:' + h.pr_number : '';
}

function _refLabel(ref) {
  return ref.indexOf('pr:') === 0 ? 'PR #' + ref.slice(3) : '#' + ref;
}

// An action that merges a PR: its command (or, with no command and no mode, its action or id, as the server
// resolves it) is /integrate-pr. It is confirmed whatever its style, as a danger action is.
function _merges(a) {
  var command = a.command || (a.mode ? '' : (a.action || a.id));
  return String(command || '').trim().replace(/^\//, '').split(/\s+/)[0] === 'integrate-pr';
}

function _button(label, variant, busy, action, args) {
  return sovaBtn(label, {
    variant: variant, size: 'md', className: 'sova-phone-tap', disabled: busy,
    attrs: { 'data-action': action, 'data-args': JSON.stringify(args) },
  });
}

function _handoffRow(h) {
  var ref = _ref(h);
  var key = 'phone-h:' + ref;
  var busy = actionIsRunning(key);
  var role = h.source || '';
  var rc = roleColor(role);
  var buttons = '';
  if (ref) {
    (h.next_actions || []).filter(function(a) { return _UNSUPPORTED_MODES.indexOf(a.mode) === -1; }).forEach(function(a) {
      var variant = Object.prototype.hasOwnProperty.call(_VARIANT_BY_STYLE, a.style) ? _VARIANT_BY_STYLE[a.style] : 'secondary';
      var confirm = a.style === 'danger' || _merges(a);
      buttons += _button(a.label || a.id, variant, busy, 'phone-handoff-run', [ref, a.id, a.label || a.id, confirm]);
    });
  }
  // Only a handoff file with an issue is dismissable: clearing with no issue would clear every handoff, and the
  // handoff synthesized from a PR's review state has no file, so clearing it would report success and change nothing.
  var issue = ref.indexOf('pr:') === 0 || h.source === 'pr-review-state' ? '' : ref;
  if (issue) buttons += _button('Dismiss', 'ghost', busy, 'phone-handoff-dismiss', [issue]);
  return '<li class="sova-card p-3 flex flex-col gap-2 min-w-0">' +
    '<div class="flex flex-wrap items-center gap-2 min-w-0">' +
      '<span class="text-sm font-medium text-gray-200">' + escapeHtml(ref ? _refLabel(ref) : 'Handoff') + '</span>' +
      '<span class="sova-badge ' + rc.bg + ' ' + rc.text + '">' + escapeHtml(formatRole(role)) + '</span>' +
    '</div>' +
    '<p class="text-sm text-gray-300 break-words">' + escapeHtml(h.summary || '') + '</p>' +
    (h.branch ? '<p class="text-xs text-gray-500 truncate">' + escapeHtml(h.branch) + '</p>' : '') +
    (buttons ? '<div class="flex flex-wrap gap-2">' + buttons + '</div>' : '') +
    actionStatusHtml(key) +
  '</li>';
}

function _planRow(d) {
  var key = 'phone-p:' + d.issue_number;
  var busy = actionIsRunning(key);
  var rc = roleColor(d.role || '');
  var args = [d.issue_number, d.action || 'action'];
  return '<li class="sova-card p-3 flex flex-col gap-2 min-w-0">' +
    '<div class="flex flex-wrap items-center gap-2 min-w-0">' +
      '<span class="text-sm font-medium text-gray-200">#' + escapeHtml(d.issue_number) + '</span>' +
      '<span class="text-sm text-gray-300 break-words">' + escapeHtml(d.action || '') + '</span>' +
      (d.role ? '<span class="sova-badge ' + rc.bg + ' ' + rc.text + '">' + escapeHtml(formatRole(d.role)) + '</span>' : '') +
    '</div>' +
    (d.reason ? '<p class="text-sm text-gray-400 break-words">' + escapeHtml(d.reason) + '</p>' : '') +
    '<div class="flex flex-wrap gap-2">' +
      _button('Approve', 'success', busy, 'phone-plan-approve', args) +
      _button('Skip', 'ghost', busy, 'phone-plan-skip', args) +
    '</div>' +
    actionStatusHtml(key) +
  '</li>';
}

function _healthNote(handoff) {
  var unreadable = handoff.unreadable_handoffs || [];
  var invalid = handoff.schema_invalid_handoffs || [];
  if (!unreadable.length && !invalid.length) return '';
  var parts = [];
  if (unreadable.length) parts.push(unreadable.length + ' handoff file(s) could not be read');
  if (invalid.length) parts.push(invalid.length + ' ignored by the pipeline (invalid)');
  return '<p class="text-sm text-accent-yellow break-words" role="status">' + escapeHtml(parts.join('; ')) +
    '. Something may be waiting that is not shown here.</p>';
}

function _noticeHtml() {
  return Object.keys(_notices).map(function(issue) {
    return '<div class="flex items-start gap-2 text-sm text-accent-red min-w-0" role="alert">' +
      '<span class="min-w-0 break-words">' + escapeHtml(_notices[issue]) + '</span>' +
      _button('Dismiss', 'ghost', false, 'phone-notice-dismiss', [issue]) + '</div>';
  }).join('');
}

// A row that was just approved, skipped or dismissed leaves the list on the reload, taking its own status line with
// it, so its outcome (an "Already gone" that ran nothing, above all) is drawn here, outside any row. While the row is
// still listed its own status shows the outcome instead.
function _outcomeHtml(listedKeys) {
  return Object.keys(_outcomes).filter(function(key) { return listedKeys.indexOf(key) === -1; }).map(function(key) {
    var o = _outcomes[key];
    return '<p class="text-sm ' + (o.warn ? 'text-accent-yellow' : 'text-accent-green') + ' break-words" role="status">' +
      escapeHtml(o.text) + '</p>';
  }).join('');
}

function _remember(key, name, result) {
  if (!result || !result.ok) return;
  var outcome = { text: name + ': ' + result.message, warn: /already gone|failures/i.test(result.message) };
  _outcomes[key] = outcome;
  setTimeout(function() {
    if (_outcomes[key] === outcome) { delete _outcomes[key]; _render(); }
  }, _OUTCOME_MS);
  _render();
}

function _sourceError(text) {
  return '<p class="text-sm text-accent-red break-words" role="alert">' + escapeHtml(text) + '</p>';
}

function _render() {
  if (!_last) return;
  var handoffs = _last.handoff
    ? (_last.handoff.handoffs || []).filter(function(h) { return h.status === 'awaiting_action'; }) : [];
  var pending = _last.plan ? _last.plan.pending || [] : [];
  var health = _last.handoff ? _healthNote(_last.handoff) : '';
  var listedKeys = handoffs.map(function(h) { return 'phone-h:' + _ref(h); })
    .concat(pending.map(function(d) { return 'phone-p:' + d.issue_number; }));
  var html = _noticeHtml() + _outcomeHtml(listedKeys) + health;
  if (!_last.handoff) html += _sourceError('Could not load the handoffs. It retries on its own.');
  if (!_last.plan) html += _sourceError('Could not load the supervisor plan. It retries on its own.');
  if (handoffs.length || pending.length) {
    html += '<ul class="flex flex-col gap-3 min-w-0">' +
      handoffs.map(_handoffRow).join('') + pending.map(_planRow).join('') + '</ul>';
  } else if (!health && _last.handoff && _last.plan) {
    html += emptyStateHtml('Nothing is waiting on you');
  }
  setSectionHtml(_SECTION, html);
}

export async function loadWaiting() {
  var seq = ++_seq;
  var settled = await Promise.allSettled([fetchAPI(apiUrl('/handoff')), fetchAPI(apiUrl('/supervisor/plan'))]);
  if (seq !== _seq) return;
  var handoff = settled[0].status === 'fulfilled' ? settled[0].value : null;
  var plan = settled[1].status === 'fulfilled' ? settled[1].value : null;
  if (!handoff && !plan) {
    _last = null;
    setSectionError(_SECTION, 'Could not load what is waiting on you. It retries on its own.');
  } else {
    _last = { handoff: handoff, plan: plan };
    _render();
  }
  if ((!handoff || !plan) && !_retry) _retry = setTimeout(function() { _retry = 0; loadWaiting(); }, _RETRY_MS);
}

// The 502 of a partly failed approval: its plan items are already consumed, so retrying would act on nothing.
function _planFailures(detail) {
  var failed = (detail.results || []).filter(function(r) { return r && r.ok === false; });
  var lines = failed.map(function(r) { return '#' + r.issue_number + ': ' + (r.error || 'failed'); });
  return (detail.message || 'Some approved decisions failed') + (lines.length ? ' (' + lines.join('; ') + ')' : '');
}

async function _afterWrite(key, name, result) {
  // The write changed the snapshot the next read answers from, and may have started an agent.
  await Promise.all([loadWaiting(), loadRunning()]);
  _remember(key, name, result);
}

async function _handoffRun(el, e, ref, actionId, label, confirm) {
  if (confirm && !(await sovaConfirm(label + ' on ' + _refLabel(ref) + '?', {
    title: label, confirmText: label, confirmClass: 'danger',
  }))) return;
  var result = await runTrackedAction('phone-h:' + ref, label, async function() {
    await postJson('/handoff/execute', { action_id: actionId, issue: ref }, 'Could not run ' + label);
    return label + ' started';
  });
  await _afterWrite('phone-h:' + ref, _refLabel(ref), result);
}

async function _handoffDismiss(el, e, issue) {
  if (!(await sovaConfirm('Dismiss the handoff for #' + issue + '? It is archived.', {
    title: 'Dismiss', confirmText: 'Dismiss', confirmClass: 'danger',
  }))) return;
  var result = await runTrackedAction('phone-h:' + issue, 'Dismissing', async function() {
    var res = await postJson('/handoff/clear', { issue: issue }, 'Could not dismiss #' + issue);
    // Nothing cleared: the handoff was already dismissed or executed elsewhere, and the reload drops the row.
    return res.cleared === false ? 'Already gone' : 'Dismissed';
  });
  await _afterWrite('phone-h:' + issue, '#' + issue, result);
}

async function _planApprove(el, e, issue, action) {
  if (!(await sovaConfirm('Approve ' + action + ' on #' + issue + '?', {
    title: 'Approve', confirmText: 'Approve',
  }))) return;
  var result = await runTrackedAction('phone-p:' + issue, 'Approving', async function() {
    var res;
    try {
      res = await postJson('/supervisor/plan/approve', { issue_numbers: [issue] }, 'Could not approve #' + issue);
    } catch (err) {
      if (err.status !== 502 || !err.detail || typeof err.detail !== 'object') throw err;
      _notices[issue] = _planFailures(err.detail);
      return 'Approved, with failures';
    }
    delete _notices[issue];
    // The item left the plan before this request (approved or skipped elsewhere): nothing ran, so do not say it did.
    return res.approved === 0 ? 'Already gone' : 'Approved';
  });
  await _afterWrite('phone-p:' + issue, '#' + issue, result);
}

async function _planSkip(el, e, issue, action) {
  if (!(await sovaConfirm('Skip ' + action + ' on #' + issue + '?', {
    title: 'Skip', confirmText: 'Skip', confirmClass: 'danger',
  }))) return;
  var result = await runTrackedAction('phone-p:' + issue, 'Skipping', async function() {
    try {
      await postJson('/supervisor/plan/skip/' + encodeURIComponent(issue), undefined, 'Could not skip #' + issue);
    } catch (err) {
      // Already gone (approved or skipped elsewhere): the reload below drops the row.
      if (err.status !== 404) throw err;
      return 'Already gone';
    }
    return 'Skipped';
  });
  await _afterWrite('phone-p:' + issue, '#' + issue, result);
}

export function init() {
  registerActions({
    'phone-handoff-run': _handoffRun,
    'phone-handoff-dismiss': _handoffDismiss,
    'phone-plan-approve': _planApprove,
    'phone-plan-skip': _planSkip,
    'phone-notice-dismiss': function(el, e, issue) { delete _notices[issue]; _render(); },
  });
  onActionStateChange(_render);
}
