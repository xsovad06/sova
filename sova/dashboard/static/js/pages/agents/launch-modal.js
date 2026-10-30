import { registerActions } from './actions.js';
import { loadAgents } from './agent-cards.js';
import { _autoOpenLogsIfIdle } from './log-viewer.js';
import { checkInterrupted } from './live-status.js';
import { loadTaskCards } from './work-items.js';

function quickStart(issue, force) {
  showLaunchModal(issue, null, force || false);
}

async function quickStartRole(issue, role, prNumber) {
  showLaunchModal(issue, role, false, prNumber);
}

// Registered here, not in a module that renders a Re-run button: history, the kanban, the interrupted-runs
// banner and the agent cards all emit it, and none of them may depend on another having initialised.
function rerunAgent(issue, role, prNumber, dismissRunId) {
  showLaunchModal(issue, role === 'auto' ? null : role, false, prNumber || null, dismissRunId || null);
}

// Runs a slash command as an agent (the task cards' and PR rows' primary and secondary actions).
async function runCommand(command, args) {
  if (blockedByConnectivity('running ' + command)) return;
  try {
    var res = await fetch(apiUrl('/agents/command'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ command: command, args: args }),
    });
    if (!res.ok) {
      showToast(await getErrorDetail(res, 'Failed to run command'), 'error');
    } else {
      var result = await res.json();
      if (result.error) {
        showToast(result.error, 'error');
      } else {
        _autoOpenLogsIfIdle(result.run_id);
      }
    }
  } catch (e) {
    showToast('Failed to run command: ' + e.message, 'error');
  }
  loadAgents();
  loadTaskCards();
}

function runCommandWithMergeBlockers(command, args, blockers) {
  if (blockers && blockers.length) {
    var msg = 'This PR is not fully ready to merge:\n\n' + blockers.join('\n') + '\n\nIntegrate anyway?';
    if (!confirm(msg)) return;
  }
  runCommand(command, args);
}

async function _doStartAgent(issue, role, force, prNumber, model, budgetOverride, llmModelOverride, dismissRunId) {
  if (!force && blockedByConnectivity('starting an agent')) return;
  try {
    var payload = { issue: String(issue), role: role, force: force, budget_override: budgetOverride || false };
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
          await _doStartAgent(issue, role, force, prNumber, model, true, llmModelOverride, dismissRunId);
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
  loadTaskCards();
}

async function startPlanner() {
  showLaunchModal('', 'planner', false);
}

// Pre-launch modal

var _launchModalState = null; // {issue, role, force, prNumber}
var _launchModalTrigger = null;
var _launchRequestId = 0; // monotonic counter to discard stale fetch responses

var _COMPLEXITY_COLORS = {
  trivial: 'bg-accent-green/20 text-accent-green',
  simple: 'bg-accent-teal/20 text-accent-teal',
  moderate: 'bg-accent-yellow/20 text-accent-yellow',
  complex: 'bg-accent-peach/20 text-accent-peach',
  epic: 'bg-accent-red/20 text-accent-red',
};
var _BADGE_BASE = 'px-2 py-0.5 rounded-full text-xs ';
var _BADGE_DEFAULT = 'bg-gray-600/30 text-gray-300';

function _setComplexityBadge(el, tier) {
  el.textContent = tier;
  el.className = _BADGE_BASE + (_COMPLEXITY_COLORS[tier] || _BADGE_DEFAULT);
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

// Some browsers never show a title tooltip on a disabled <select>, so the
// reason is also written into the visible help line under it.
function _setLlmOverrideDisabled(sel, reason) {
  sel.disabled = true;
  sel.title = reason;
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
  sel.title = '';
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
  _launchModalTrigger = document.activeElement;
  _prepareLlmOverrideSelect(role);
  // Bumped before the no-issue branch too, so an earlier issue's in-flight
  // complexity fetch can never act on (or overwrite) this launch.
  var requestId = ++_launchRequestId;

  if (!issue) {
    _showModalChrome();
    _populateLaunchDefaults();
    return;
  }

  fetch(apiUrl('/agents/issue/' + encodeURIComponent(issue) + '/complexity'))
    .then(function(res) {
      if (!res.ok) throw new Error('Failed to fetch complexity');
      return res.json();
    })
    .then(function(data) {
      if (requestId !== _launchRequestId) return;
      var confirmMode = data.confirm_model || 'always';
      var shouldPrompt = confirmMode === 'always'
        || (confirmMode === 'complex_only' && _CONFIRM_TIERS[data.complexity]);

      if (!shouldPrompt) {
        _launchModalState = null;
        // Only a non-direct role ever used the suggestion: a developer run
        // would turn it into a pin overriding per-step routing, and the
        // other direct-spawn roles ignore it.
        var autoModel = _runtimeModelMode(role) === 'spawn' ? data.suggested_model : null;
        _doStartAgent(issue, role, force || false, prNumber || null, autoModel, false, null, dismissRunId || null);
        return;
      }

      _showModalChrome();
      _setComplexityBadge(document.getElementById('launch-complexity'), data.complexity);
      _prepareRuntimeModelSelect(role, data.suggested_model_id || data.suggested_model);
      document.getElementById('launch-reason').textContent = data.reason;
      document.getElementById('launch-modal-loading').classList.add('hidden');
      document.getElementById('launch-modal-body').classList.remove('hidden');
    })
    .catch(function() {
      if (requestId !== _launchRequestId) return;
      _showModalChrome();
      _populateLaunchDefaults();
    });
}

function _showModalChrome() {
  var modal = document.getElementById('launch-modal');
  var loading = document.getElementById('launch-modal-loading');
  var body = document.getElementById('launch-modal-body');
  loading.classList.remove('hidden');
  body.classList.add('hidden');
  modal.classList.remove('hidden');
  document.addEventListener('keydown', _launchModalKeydown);
}

function _populateLaunchDefaults() {
  _setComplexityBadge(document.getElementById('launch-complexity'), 'unknown');
  _prepareRuntimeModelSelect(_launchModalState ? _launchModalState.role : null);
  document.getElementById('launch-reason').textContent = 'default (no complexity data)';
  document.getElementById('launch-modal-loading').classList.add('hidden');
  document.getElementById('launch-modal-body').classList.remove('hidden');
}

function hideLaunchModal() {
  document.getElementById('launch-modal').classList.add('hidden');
  document.removeEventListener('keydown', _launchModalKeydown);
  _launchModalState = null;
  if (_launchModalTrigger) {
    _launchModalTrigger.focus();
    _launchModalTrigger = null;
  }
}

function _launchModalKeydown(e) {
  if (e.key === 'Escape') hideLaunchModal();
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
    'quick-start': function(el, e, issue, force) { quickStart(issue, force); },
    'run-command': function(el, e, command, args) { runCommand(command, args); },
    'run-command-with-merge-blockers': function(el, e, command, args, blockers) {
      runCommandWithMergeBlockers(command, args, blockers);
    },
    'quick-start-role': function(el, e, issue, role, prNumber) { quickStartRole(issue, role, prNumber); },
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

  document.getElementById('launch-modal').addEventListener('click', function(e) {
    if (e.target === this) hideLaunchModal();
  });
}
