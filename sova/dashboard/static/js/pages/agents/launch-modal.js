import { registerActions } from './actions.js';
import { loadAgents } from './agent-cards.js';
import { _autoOpenLogsIfIdle } from './log-viewer.js';
import { checkInterrupted } from './live-status.js';
import { loadBoard } from '../work/board.js';

// Registered here, not in a module that renders a Re-run button: history, the interrupted-runs
// banner and the agent cards all emit it, and none of them may depend on another having initialised.
function rerunAgent(issue, role, prNumber, dismissRunId) {
  showLaunchModal(issue, role === 'auto' ? null : role, false, prNumber || null, dismissRunId || null);
}

async function _doStartAgent(issue, role, force, prNumber, model, budgetOverride, llmModelOverride, dismissRunId, overrideSuitability) {
  if (!force && blockedByConnectivity('starting an agent')) return;
  try {
    var payload = { issue: String(issue), role: role, force: force, budget_override: budgetOverride || false };
    if (overrideSuitability) payload.override_suitability = true;
    if (prNumber) payload.pr_number = prNumber;
    if (model) payload.model = model;
    if (llmModelOverride) payload.llm_model_override = llmModelOverride;
    var res = await fetch(apiUrl('/agents/start'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (!res.ok) {
      var body = null;
      try { body = await res.json(); } catch (_e) { /* ignore */ }
      var detail = body && body.detail;
      if (res.status === 409 && detail && detail.code === 'budget_exceeded') {
        var spend = Number(detail.total_cost_usd || 0).toFixed(2);
        var limit = Number(detail.max_issue_budget || 0).toFixed(2);
        var confirmed = await sovaConfirm(
          'Issue #' + issue + ' has exceeded its per-issue budget ($' + spend + ' / $' + limit + '). Override and start anyway?',
          { title: 'Budget Exceeded', confirmText: 'Override', confirmClass: 'danger' }
        );
        if (confirmed) {
          await _doStartAgent(issue, role, force, prNumber, model, true, llmModelOverride, dismissRunId, overrideSuitability);
        }
        return;
      }
      if (res.status === 409 && detail && detail.code === 'suitability_blocked') {
        // The retry resends this exact request plus the override, so role, model and PR are kept.
        var humanNote = detail.mode === 'human'
          ? ' The agent:human-only label is replaced once the run moves the issue on.'
          : '';
        var launchAnyway = await sovaConfirm(
          detail.message + humanNote + ' Launch anyway?',
          { title: 'Not meant for an agent', confirmText: 'Launch anyway', confirmClass: 'danger' }
        );
        if (launchAnyway) {
          await _doStartAgent(issue, role, force, prNumber, model, budgetOverride, llmModelOverride, dismissRunId, true);
        }
        return;
      }
      var msg = (detail && (typeof detail === 'string' ? detail : detail.message)) || 'Failed to start agent';
      showToast(msg, 'error');
    } else {
      var result = await res.json();
      if (result.error) { showToast(result.error, 'error'); return; }
      _autoOpenLogsIfIdle(result.run_id);
      var agentLabel = issue ? '#' + issue : (role || 'agent');
      showToast('Agent started: ' + agentLabel, 'success');
      if (Array.isArray(result.warnings)) {
        result.warnings.forEach(function (w) { showToast(w, 'warning'); });
      }
      if (dismissRunId) {
        // The relaunch leaves the originating interrupted run's own status
        // unchanged, so without this it stays in the banner with a Restart
        // button that a second click would reject as a duplicate run.
        // superseded_by_relaunch preserves the original error_message: this
        // dismissal is a side effect of relaunching, not a user decision, so
        // the interruption diagnostic (consumed by get_failure_taxonomy())
        // must survive it.
        try {
          await fetch(apiUrl('/agents/interrupted/dismiss'), {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ run_ids: [dismissRunId], superseded_by_relaunch: true }),
          });
        } catch (e) { /* best-effort */ }
        await checkInterrupted();
      }
    }
  } catch (e) {
    showToast('Failed to start agent: ' + e.message, 'error');
  }
  loadAgents();
  loadBoard();
}

async function startPlanner() {
  showLaunchModal('', 'planner', false);
}

// Pre-launch modal

var _launchModalState = null; // {issue, role, force, prNumber}
var _launchTrigger = null; // the element that opened the launch modal, for focus return
var _launchRequestId = 0; // monotonic counter to discard stale fetch responses

// A complexity tier's own colour pair. Not badgeHtml() state tokens: `simple` is teal, which has no token, so the
// helper keeps its table (only the colour pair changes on the template's `sova-badge` span). Unknown is gray.
var _COMPLEXITY_COLORS = {
  trivial: 'bg-accent-green/20 text-accent-green',
  simple: 'bg-accent-teal/20 text-accent-teal',
  moderate: 'bg-accent-yellow/20 text-accent-yellow',
  complex: 'bg-accent-peach/20 text-accent-peach',
  epic: 'bg-accent-red/20 text-accent-red',
};
var _COMPLEXITY_DEFAULT = 'bg-gray-600/30 text-gray-300';

function _setComplexityBadge(el, tier) {
  el.textContent = tier;
  var known = Object.prototype.hasOwnProperty.call(_COMPLEXITY_COLORS, tier);
  setColorClasses(el, known ? _COMPLEXITY_COLORS[tier] : _COMPLEXITY_DEFAULT);
}

var _CONFIRM_TIERS = { complex: true, epic: true };

var _runtimeModelRequestId = 0;

// Mirrors _prepareLlmOverrideSelect's fail-open discipline, but hides the
// field entirely instead of disabling it: #1039's acceptance criteria call
// for "no selector" when the active runtime does not support choosing a
// model (e.g. Codex, or an unreachable/unauthenticated runtime CLI), not a
// disabled one still showing a value that will be ignored. A one-shot,
// never-persisted override, mirroring #launch-llm-model: only this launch
// call consumes it, nothing is written back to roles.developer_runtime_model.
function _prepareRuntimeModelSelect(role, suggestedModel) {
  var field = document.getElementById('launch-model-field');
  var sel = document.getElementById('launch-model');
  var requestId = ++_runtimeModelRequestId;
  var mode = _runtimeModelMode(role);
  sel.innerHTML = '';
  if (mode === 'ignored') {
    field.style.display = 'none';
    return;
  }
  field.style.display = '';
  // A pin replaces per-step routing for every code-editing step of the run,
  // so it must be a deliberate pick, never the complexity suggestion riding
  // along on a plain Launch click (same rule as #launch-llm-model).
  if (mode === 'pin') suggestedModel = undefined;

  fetchAPI(apiUrl('/models/available?layer=runtime'))
    .then(function(data) {
      if (requestId !== _runtimeModelRequestId) return;
      // A runtime that ignores a caller-supplied model (honours_model_override
      // false, e.g. Codex, whose enumeration can only echo back the single
      // configured codex.model) gets no selector at all rather than one whose
      // value would be dropped. Gated on the capability the payload reports,
      // not on a runtime name hardcoded here: a second runtime with the same
      // capability must not have to be remembered in the client too.
      if (data.honours_model_override === false || data.source === 'unavailable' || !(data.models || []).length) {
        field.style.display = 'none';
        return;
      }
      // Leading "no override" entry, kept selected unless the suggested model
      // is really in this runtime's catalog: the suggestion comes from the
      // complexity endpoint's layer-2 tier routing, resolved to a concrete ID
      // (suggested_model_id), and a backend-specific ID such as a Vertex
      // "@"-pinned one is not in the runtime's catalog. Without it the browser
      // silently selects the catalog's first entry and that unrelated model,
      // not the suggestion the reason line below describes, is what gets
      // submitted and launched.
      var none = document.createElement('option');
      none.value = '';
      none.textContent = 'Use configured model (no override)';
      sel.appendChild(none);
      if (!appendModelOptgroups(sel, groupModelsByFamily(data.models), suggestedModel)) sel.value = '';
    })
    .catch(function() {
      if (requestId !== _runtimeModelRequestId) return;
      field.style.display = 'none';
    });
}

// Mirrors agent_lifecycle.RUNTIME_MODEL_PIN_ROLES: for these direct-spawn
// roles the Model choice travels to `sova run` as a per-run runtime pin for
// its code-editing steps. Every other direct-spawn role (_LLM_OVERRIDE_ROLES)
// has no step that reaches the runtime and ignores the choice server-side,
// so it gets no selector; a non-direct role hands it to runtime.spawn().
var _RUNTIME_MODEL_PIN_ROLES = { developer: true };

function _runtimeModelMode(role) {
  var effectiveRole = role || 'developer';
  if (_RUNTIME_MODEL_PIN_ROLES[effectiveRole]) return 'pin';
  if (_LLM_OVERRIDE_ROLES[effectiveRole]) return 'ignored';
  return 'spawn';
}

// Mirrors agent_lifecycle.LLM_MODEL_OVERRIDE_ROLES. The server rejects an
// override for any other role with a 400 regardless; disabling up front just
// avoids offering a choice that cannot be honored.
var _LLM_OVERRIDE_ROLES = { researcher: true, developer: true, planner: true, reviewer: true };
var _llmOverrideRequestId = 0;
var _llmOverrideDefaultHelp = null;

// A tooltip bubble does not render on a <select> and is hidden on a disabled host, so the reason rides on the
// wrapper around the select and is also written into the visible help line under it.
function _setLlmOverrideDisabled(sel, reason) {
  sel.disabled = true;
  var tip = document.getElementById('launch-llm-model-tip');
  if (tip) tip.setAttribute('data-tooltip', reason);
  sel.classList.add('opacity-50', 'cursor-not-allowed');
  document.getElementById('launch-llm-model-help').textContent = reason + '.';
}

// Always resets to "no override" so a previous launch's choice, or any
// suggested model, can never be submitted without a deliberate pick.
function _prepareLlmOverrideSelect(role) {
  var sel = document.getElementById('launch-llm-model');
  var requestId = ++_llmOverrideRequestId;
  sel.innerHTML = '';
  var none = document.createElement('option');
  none.value = '';
  none.textContent = 'Use configured model (no override)';
  sel.appendChild(none);
  sel.value = '';
  sel.disabled = false;
  var tip = document.getElementById('launch-llm-model-tip');
  if (tip) tip.removeAttribute('data-tooltip');
  sel.classList.remove('opacity-50', 'cursor-not-allowed');
  var help = document.getElementById('launch-llm-model-help');
  if (_llmOverrideDefaultHelp === null) _llmOverrideDefaultHelp = help.textContent;
  help.textContent = _llmOverrideDefaultHelp;

  var effectiveRole = role || 'developer';
  if (!_LLM_OVERRIDE_ROLES[effectiveRole]) {
    _setLlmOverrideDisabled(sel, 'The ' + effectiveRole + ' role cannot honor a per-run internal LLM model override');
    return;
  }

  fetchAPI(apiUrl('/models/available?layer=llm'))
    .then(function(data) {
      if (requestId !== _llmOverrideRequestId) return;
      // A "curated" answer is models_service's fallback while the provider is
      // unauthenticated or misconfigured, not the provider's real catalog.
      if (data.source === 'curated' || !(data.models || []).length) {
        _setLlmOverrideDisabled(sel, 'Model discovery is unavailable for the current provider');
        return;
      }
      appendModelOptgroups(sel, groupModelsByFamily(data.models));
    })
    .catch(function() {
      if (requestId !== _llmOverrideRequestId) return;
      _setLlmOverrideDisabled(sel, 'Model discovery is unavailable for the current provider');
    });
}

export function showLaunchModal(issue, role, force, prNumber, dismissRunId) {
  _launchModalState = {
    issue: issue, role: role, force: force || false, prNumber: prNumber || null,
    dismissRunId: dismissRunId || null,
  };
  _setSuitabilityWarning(null);
  _prepareLlmOverrideSelect(role);
  // The complexity request below can finish after focus has moved, so the trigger is captured now and handed to
  // the shared modal as its return-focus target.
  _launchTrigger = document.activeElement;
  // Bumped before the no-issue branch too, so an earlier issue's in-flight
  // complexity fetch can never act on (or overwrite) this launch.
  var requestId = ++_launchRequestId;

  if (!issue) {
    _showModalChrome();
    _populateLaunchDefaults();
    return;
  }

  // The role lets the server skip the epic/human warning for runs that are never refused (triage, reviewer).
  var complexityUrl = '/agents/issue/' + encodeURIComponent(issue) + '/complexity'
    + (role ? '?role=' + encodeURIComponent(role) : '');
  fetch(apiUrl(complexityUrl))
    .then(function(res) {
      if (!res.ok) throw new Error('Failed to fetch complexity');
      return res.json();
    })
    .then(function(data) {
      if (requestId !== _launchRequestId) return;
      var confirmMode = data.confirm_model || 'always';
      // A suitability warning always opens the modal: the user must see it before the run starts,
      // whatever the model-confirmation setting, since an interactive or on-hold issue is never refused later.
      var shouldPrompt = confirmMode === 'always'
        || (confirmMode === 'complex_only' && _CONFIRM_TIERS[data.complexity])
        || !!data.suitability;

      if (!shouldPrompt) {
        _launchModalState = null;
        // Only a non-direct role ever used the suggestion: a developer run
        // would turn it into a pin overriding per-step routing, and the
        // other direct-spawn roles ignore it.
        var autoModel = _runtimeModelMode(role) === 'spawn' ? data.suggested_model : null;
        _doStartAgent(issue, role, force || false, prNumber || null, autoModel, false, null, dismissRunId || null);
        return;
      }

      _showModalChrome(!!data.suitability);
      _setComplexityBadge(document.getElementById('launch-complexity'), data.complexity);
      _prepareRuntimeModelSelect(role, data.suggested_model_id || data.suggested_model);
      document.getElementById('launch-reason').textContent = data.reason;
      _setSuitabilityWarning(data.suitability);
      document.getElementById('launch-modal-loading').classList.add('hidden');
      document.getElementById('launch-modal-body').classList.remove('hidden');
    })
    .catch(function() {
      if (requestId !== _launchRequestId) return;
      _showModalChrome();
      _populateLaunchDefaults();
    });
}

// Written with textContent: the reasons come from issue content. Cleared on every launch so a previous
// issue's warning never shows for the next one.
function _setSuitabilityWarning(suitability) {
  var el = document.getElementById('launch-suitability');
  if (!suitability) {
    el.textContent = '';
    el.classList.add('hidden');
    return;
  }
  var reasons = (suitability.reasons || []).map(function(r) { return r.source + ': ' + r.value; }).join(', ');
  el.textContent = 'This issue is classified ' + suitability.mode + (reasons ? ' (' + reasons + ')' : '') + '.';
  el.classList.remove('hidden');
}

// Opens on the shared modal (sovaModalOpen in app.js), which owns Escape, the backdrop, the Tab trap and focus
// return. Launch is focused, as a primary confirm's button is; a launch carrying a suitability warning opens on
// Cancel instead, like a warning confirm, so an Enter carried over from the trigger never starts it.
function _showModalChrome(cautious) {
  var loading = document.getElementById('launch-modal-loading');
  var body = document.getElementById('launch-modal-body');
  loading.classList.remove('hidden');
  body.classList.add('hidden');
  sovaModalOpen(document.getElementById('launch-modal'), {
    initialFocus: cautious ? '[data-action="hide-launch-modal"]' : '#launch-confirm-btn',
    returnFocus: _launchTrigger,
    onClose: function() { _launchModalState = null; _launchTrigger = null; },
  });
}

function _populateLaunchDefaults() {
  _setComplexityBadge(document.getElementById('launch-complexity'), 'unknown');
  _prepareRuntimeModelSelect(_launchModalState ? _launchModalState.role : null);
  document.getElementById('launch-reason').textContent = 'default (no complexity data)';
  document.getElementById('launch-modal-loading').classList.add('hidden');
  document.getElementById('launch-modal-body').classList.remove('hidden');
}

function hideLaunchModal() {
  sovaModalClose(document.getElementById('launch-modal'), 'cancel');
}

function confirmLaunch() {
  if (!_launchModalState) return;
  var s = _launchModalState;
  var model = document.getElementById('launch-model').value || null;
  var llmSel = document.getElementById('launch-llm-model');
  var llmModelOverride = llmSel.disabled ? null : (llmSel.value || null);
  hideLaunchModal();
  _doStartAgent(s.issue, s.role, s.force, s.prNumber, model, false, llmModelOverride, s.dismissRunId);
}

export function init() {
  registerActions({
    'rerun': function(el, e) {
      e.preventDefault();
      e.stopPropagation();
      var issue = el.getAttribute('data-issue');
      if (!issue) return;
      var pr = el.getAttribute('data-pr');
      var runId = el.getAttribute('data-run-id');
      rerunAgent(issue, el.getAttribute('data-role'),
        pr ? parseInt(pr, 10) : null, runId ? parseInt(runId, 10) : null);
    },
    'start-planner': function() { startPlanner(); },
    'hide-launch-modal': function() { hideLaunchModal(); },
    'confirm-launch': function() { confirmLaunch(); },
  });
}
