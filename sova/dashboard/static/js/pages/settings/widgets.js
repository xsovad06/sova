/* The Settings page's status widgets, each beside the settings it belongs to: SOVA Installation (command sync) and
   the health checks in Overview, personas under Project, GitHub labels under Task Source, invariants under
   Validation, the operations persona under Oversight, and the connection tests under LLM Provider and Agent.
   A widget loads the first time its section is shown and again only when its own button asks; a setting save
   never reloads one. */

const loaders = {
  installation: loadInstallationStatus,
  health: loadHealthChecks,
  capacity: loadCapacity,
  personas: loadPersonas,
  labels: loadLabelsStatus,
  invariants: loadInvariants,
  'operations-persona': loadPersonaPreview,
  'llm-tools': renderLlmTools,
  'runtime-tools': renderRuntimeTools,
};

function widget(name) { return document.querySelector('[data-widget="' + name + '"]'); }

/* Loads every widget inside `root` that has not loaded yet. A widget whose load failed is marked
   data-widget-failed by failed(), and is loaded again the next time its section is shown. */
export function loadWidgets(root) {
  root.querySelectorAll('[data-widget]').forEach((el) => {
    if (el.dataset.widgetLoaded) return;
    const load = loaders[el.dataset.widget];
    if (!load) return;
    begin(el);
    // A synchronous loader that throws must not skip the widgets after it, and a rejected one must not surface
    // as an unhandled rejection: either way the widget is marked failed and loads again on the next visit.
    Promise.resolve().then(() => load(el)).catch((e) => {
      console.error('Failed to load widget ' + el.dataset.widget + ':', e);
      el.dataset.widgetFailed = '1';
    }).finally(() => {
      if (el.dataset.widgetFailed) delete el.dataset.widgetLoaded;
    });
  });
}

/* Marks a widget as loaded and not failed. Every loader calls it when it starts, so a widget reloaded through its
   own button after a failure is not loaded a second time on the next visit. */
function begin(el) {
  el.dataset.widgetLoaded = '1';
  delete el.dataset.widgetFailed;
}

/* A status line's dot is drawn by CSS from data-state: ok, warn, err or muted. */
function setState(el, kind) { el.dataset.state = kind; }

function failed(el, text) {
  el.dataset.widgetFailed = '1';
  setState(el, 'muted');
  el.innerHTML = `<span class="sova-settings-status-muted">${escapeHtml(text)}</span>`;
}

/* --- Overview: commands in sync (SOVA Installation) ------------------------------------------------------- */

let installationRun = 0;

async function loadInstallationStatus(el) {
  el = el || widget('installation');
  if (!el) return;
  begin(el);
  const run = ++installationRun;  // a slower earlier check must not overwrite a newer one
  el.innerHTML = '<div class="sova-settings-sync">Checking against this SOVA version...</div>';
  try {
    const data = await fetchAPI(apiUrl('/settings/installation/status'));
    if (run !== installationRun) return;
    if (data.has_updates) {
      el.innerHTML = '<div class="sova-settings-sync sova-settings-sync-warn" data-state="warn">' +
        escapeHtml(data.total_updates) + ' update' + (data.total_updates !== 1 ? 's' : '') + ' available</div>' +
        '<p class="sova-settings-note">Commands or guidelines in this project differ from the installed SOVA version.</p>' +
        '<div class="sova-settings-actions"><button type="button" class="sova-btn sova-btn-primary" data-act="install-review">Review changes</button>' +
        '<button type="button" class="sova-btn sova-btn-success" data-act="install-sync">Sync all</button>' +
        '<span class="sova-settings-status-muted" data-part="sync-status"></span></div>';
    } else {
      el.innerHTML = '<div class="sova-settings-sync sova-settings-sync-ok" data-state="ok">Commands in sync</div>' +
        '<p class="sova-settings-note">Commands and guidelines match the installed SOVA version.</p>' +
        '<div class="sova-settings-actions"><button type="button" class="sova-btn sova-btn-primary" data-act="install-recheck">Check again</button></div>';
    }
  } catch (e) {
    if (run !== installationRun) return;
    console.error('Failed to load installation status:', e);
    el.dataset.widgetFailed = '1';
    el.innerHTML = '<p class="sova-settings-note">Could not check whether commands are in sync.</p>' +
      '<div class="sova-settings-actions"><button type="button" class="sova-btn sova-btn-primary" data-act="install-recheck">Check again</button></div>';
  }
}

async function syncInstallation(btn) {
  const el = widget('installation');
  const status = el && el.querySelector('[data-part="sync-status"]');
  const run = ++installationRun;  // a check started while the sync runs must not be overwritten by its result
  btn.disabled = true;
  btn.textContent = 'Syncing...';
  try {
    const res = await fetch(apiUrl('/setup/commands/sync'), { method: 'POST', headers: jsonCsrfHeaders() });
    if (!res.ok) {
      if (status) status.textContent = 'Not synced. ' + await getErrorDetail(res, 'Sync failed');
      btn.disabled = false;
      btn.textContent = 'Sync all';
      return;
    }
    const data = await res.json();
    if (run !== installationRun) return;
    const parts = [];
    if (data.commands && data.commands.updated > 0) parts.push(data.commands.updated + ' command' + (data.commands.updated !== 1 ? 's' : '') + ' updated');
    if (data.guidelines && data.guidelines.updated > 0) parts.push(data.guidelines.updated + ' guideline' + (data.guidelines.updated !== 1 ? 's' : '') + ' updated');
    const conflicts = [].concat((data.commands && data.commands.conflicts) || [], (data.guidelines && data.guidelines.conflicts) || []);
    el.innerHTML = '<div class="sova-settings-sync sova-settings-sync-ok" data-state="ok">Sync complete</div>' +
      '<p class="sova-settings-note">' + escapeHtml(parts.length ? parts.join(', ') : 'Already up to date') + '</p>' +
      conflicts.map((f) => `<p class="sova-settings-note sova-settings-note-warn">${escapeHtml(f)}: changed in this project, skipped</p>`).join('') +
      '<div class="sova-settings-actions"><button type="button" class="sova-btn sova-btn-primary" data-act="install-recheck">Check again</button></div>';
  } catch (e) {
    if (status) status.textContent = 'Not synced. ' + e.message;
    btn.disabled = false;
    btn.textContent = 'Sync all';
  }
}

/* --- Review changes modal (SOVA Installation) ------------------------------------------------------------- */

const STATUS_META = {
  conflict: { label: 'Conflict', badgeClass: 'bg-accent-red/20 text-accent-red', checkable: true, defaultChecked: false },
  local_modified: { label: 'Locally modified', badgeClass: 'bg-accent-yellow/20 text-accent-yellow', checkable: false, defaultChecked: false },
  upstream_only: { label: 'Update available', badgeClass: 'bg-accent/20 text-accent', checkable: true, defaultChecked: true },
  new: { label: 'New', badgeClass: 'bg-accent-green/20 text-accent-green', checkable: true, defaultChecked: true },
  removed: { label: 'Removed upstream', badgeClass: 'bg-gray-600/30 text-gray-400', checkable: false, defaultChecked: false },
  local_only: { label: 'Local only', badgeClass: 'bg-gray-600/30 text-gray-400', checkable: false, defaultChecked: false },
};

const LINE_DIFF_MAX_CELLS = 250000;

function diffMiddleLines(oldLines, newLines) {
  const m = oldLines.length;
  const n = newLines.length;
  if (m === 0 && n === 0) return [];
  if (m === 0) return newLines.map((t) => ({ type: 'add', text: t }));
  if (n === 0) return oldLines.map((t) => ({ type: 'remove', text: t }));
  if (m * n > LINE_DIFF_MAX_CELLS) {
    return oldLines.map((t) => ({ type: 'remove', text: t })).concat(newLines.map((t) => ({ type: 'add', text: t })));
  }
  const dp = [];
  for (let i = 0; i <= m; i++) dp.push(new Int32Array(n + 1));
  for (let i = m - 1; i >= 0; i--) {
    for (let j = n - 1; j >= 0; j--) {
      dp[i][j] = oldLines[i] === newLines[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
    }
  }
  const rows = [];
  let i = 0;
  let j = 0;
  while (i < m && j < n) {
    if (oldLines[i] === newLines[j]) { rows.push({ type: 'context', text: oldLines[i] }); i++; j++; }
    else if (dp[i + 1][j] >= dp[i][j + 1]) { rows.push({ type: 'remove', text: oldLines[i] }); i++; }
    else { rows.push({ type: 'add', text: newLines[j] }); j++; }
  }
  while (i < m) { rows.push({ type: 'remove', text: oldLines[i] }); i++; }
  while (j < n) { rows.push({ type: 'add', text: newLines[j] }); j++; }
  return rows;
}

function computeLineDiff(oldText, newText) {
  const oldLines = oldText == null ? [] : oldText.split('\n');
  const newLines = newText == null ? [] : newText.split('\n');
  let start = 0;
  const maxStart = Math.min(oldLines.length, newLines.length);
  while (start < maxStart && oldLines[start] === newLines[start]) start++;
  let oldEnd = oldLines.length;
  let newEnd = newLines.length;
  while (oldEnd > start && newEnd > start && oldLines[oldEnd - 1] === newLines[newEnd - 1]) { oldEnd--; newEnd--; }
  let rows = [];
  for (let i = 0; i < start; i++) rows.push({ type: 'context', text: oldLines[i] });
  rows = rows.concat(diffMiddleLines(oldLines.slice(start, oldEnd), newLines.slice(start, newEnd)));
  for (let i = oldEnd; i < oldLines.length; i++) rows.push({ type: 'context', text: oldLines[i] });
  return rows;
}

function renderDiffRows(oldText, newText) {
  if (oldText == null && newText == null) return '<div class="text-xs text-gray-500 px-2 py-1">Content unavailable</div>';
  if (oldText === '' && newText === '') return '<div class="text-xs text-gray-500 px-2 py-1">Empty file</div>';
  return computeLineDiff(oldText, newText).map((row) => {
    const prefix = row.type === 'add' ? '+' : row.type === 'remove' ? '-' : ' ';
    const cls = row.type === 'add' ? 'bg-accent-green/10 text-accent-green'
      : row.type === 'remove' ? 'bg-accent-red/10 text-accent-red' : 'text-gray-500';
    return '<div class="' + cls + ' px-2 whitespace-pre-wrap break-words">' + escapeHtml(prefix + ' ' + row.text) + '</div>';
  }).join('');
}

function fileDiffSectionHtml(file) {
  const meta = STATUS_META[file.status] || { label: file.status, badgeClass: 'bg-gray-600/30 text-gray-400', checkable: false, defaultChecked: false };
  const checkbox = meta.checkable
    ? '<input type="checkbox" class="review-file-checkbox" data-filename="' + escapeHtml(file.filename) + '" data-category="' + escapeHtml(file.category) + '" ' +
      'aria-label="' + escapeHtml('Sync ' + file.filename) + '"' + (meta.defaultChecked ? ' checked' : '') + '>'
    : '<span class="inline-block w-4"></span>';
  let body;
  if (file.status === 'removed') body = '<div class="text-xs text-gray-500 px-2 py-1">Removed from the canonical source: the copy in this project is left as it is</div>';
  else if (file.status === 'local_only') {
    body = '<div class="text-xs text-gray-500 px-2 py-1">' + (file.local_content === null ? 'File was removed from disk locally' : 'Not managed by SOVA: left as it is') + '</div>';
  } else body = renderDiffRows(file.local_content, file.canonical_content);
  return '<div class="border border-gray-700/30 rounded-lg overflow-hidden">' +
    '<div class="flex items-center gap-2 px-3 py-2 bg-surface-hover/20">' + checkbox +
      '<span class="text-sm font-mono text-gray-200 truncate">' + escapeHtml(file.filename) + '</span>' +
      '<span class="text-xs px-1.5 py-0.5 rounded ' + meta.badgeClass + '">' + escapeHtml(meta.label) + '</span>' +
      '<span class="text-xs text-gray-500 ml-auto">' + escapeHtml(file.category) + '</span></div>' +
    '<div class="max-h-56 overflow-y-auto font-mono text-xs py-1" tabindex="0" role="region" aria-label="' + escapeHtml('Diff for ' + file.filename) + '">' + body + '</div>' +
  '</div>';
}

const review = { trigger: null, requestId: 0 };

function focusables(modal) {
  return Array.from(modal.querySelectorAll('button:not([disabled]), input:not([disabled]):not([type="hidden"]), [tabindex="0"]'));
}

async function openReviewChanges() {
  const modal = document.getElementById('review-changes-modal');
  const body = document.getElementById('review-changes-body');
  if (!modal || !body) return;
  review.trigger = document.activeElement;
  const requestId = ++review.requestId;
  body.innerHTML = '<p class="text-gray-500 text-sm">Loading...</p>';
  modal.classList.add('sova-modal-visible');
  const first = focusables(modal)[0];
  if (first) first.focus();
  try {
    const data = await fetchAPI(apiUrl('/settings/installation/diff'));
    if (requestId !== review.requestId) return;
    if (!data.source_available) body.innerHTML = '<p class="text-gray-500 text-sm">Canonical command source not found (non-editable install).</p>';
    else if (!data.files || !data.files.length) body.innerHTML = '<p class="text-gray-500 text-sm">No changes to review.</p>';
    else body.innerHTML = data.files.map(fileDiffSectionHtml).join('');
  } catch (e) {
    if (requestId !== review.requestId) return;
    body.innerHTML = '<p class="text-gray-500 text-sm">Failed to load changes: ' + escapeHtml(e.message) + '</p>';
  }
}

function closeReviewChanges() {
  const modal = document.getElementById('review-changes-modal');
  if (modal) modal.classList.remove('sova-modal-visible');
  review.requestId++;
  if (review.trigger && document.contains(review.trigger)) review.trigger.focus();
  review.trigger = null;
}

async function applySelectedChanges(btn) {
  const modal = document.getElementById('review-changes-modal');
  const filenames = [];
  const guidelineFilenames = [];
  modal.querySelectorAll('.review-file-checkbox:checked').forEach((cb) => {
    (cb.dataset.category === 'guideline' ? guidelineFilenames : filenames).push(cb.dataset.filename);
  });
  if (!filenames.length && !guidelineFilenames.length) {
    showToast('Select at least one file to sync', 'warning');
    return;
  }
  btn.disabled = true;
  btn.textContent = 'Applying...';
  try {
    const res = await fetch(apiUrl('/setup/commands/sync'), {
      method: 'POST', headers: jsonCsrfHeaders(),
      body: JSON.stringify({ filenames, guideline_filenames: guidelineFilenames }),
    });
    if (!res.ok) {
      showToast(await getErrorDetail(res, 'Failed to apply changes'), 'error');
      return;
    }
    const data = await res.json();
    const total = ((data.commands && data.commands.updated) || 0) + ((data.guidelines && data.guidelines.updated) || 0);
    showToast(total + ' file' + (total !== 1 ? 's' : '') + ' synced', 'success');
    closeReviewChanges();
    loadInstallationStatus();
  } catch (e) {
    showToast('Failed to apply changes: ' + e.message, 'error');
  } finally {
    btn.disabled = false;
    btn.textContent = 'Apply selected';
  }
}

function trapReviewFocus(e) {
  const modal = document.getElementById('review-changes-modal');
  if (!modal || !modal.classList.contains('sova-modal-visible')) return;
  if (e.key === 'Escape') { closeReviewChanges(); return; }
  if (e.key !== 'Tab') return;
  const items = focusables(modal);
  if (!items.length) { e.preventDefault(); return; }
  const first = items[0];
  const last = items[items.length - 1];
  if (!modal.contains(document.activeElement) || items.indexOf(document.activeElement) === -1) { e.preventDefault(); first.focus(); }
  else if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
  else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
}

/* --- Overview: health checks -------------------------------------------------------------------------------- */

/* The health panel. #1121 adds the sign-in control of a failing check that needs a login to its row here. */
let healthRun = 0;

export async function loadHealthChecks(el) {
  el = el || widget('health');
  if (!el) return;
  begin(el);
  const run = ++healthRun;  // a slower earlier run must not overwrite a newer one
  const summary = document.querySelector('[data-part="health-summary"]');
  if (summary) summary.textContent = 'Running...';
  try {
    const data = await fetchAPI(apiUrl('/health/checks'));
    if (run !== healthRun) return;
    const items = data.checks || [];
    const passing = items.filter((c) => c.passed).length;
    if (summary) {
      summary.textContent = items.length ? passing + ' of ' + items.length + ' passing' : 'No checks reported';
      summary.className = 'sova-settings-status-muted ' + (items.length && passing === items.length ? 'text-accent-green' : (items.length ? 'text-accent-yellow' : ''));
    }
    // The tooltip sits on the row: the detail is cut to one line, and a clipped box would clip its tooltip too.
    el.innerHTML = items.length ? `<div class="sova-settings-health">${items.map((check) => `
      <div class="sova-settings-health-row" data-state="${escapeHtml(check.passed ? 'ok' : (check.required ? 'err' : 'warn'))}"
           ${check.detail ? `data-tooltip="${escapeHtml(check.detail)}" data-tooltip-wide` : ''}>
        <span class="sova-settings-health-name">${escapeHtml(check.name)}</span>
        ${check.required ? '' : '<span class="sova-settings-status-muted">optional</span>'}
        ${check.detail ? `<span class="sova-settings-health-detail">${escapeHtml(check.detail)}</span>` : ''}
      </div>`).join('')}</div>` : '<p class="sova-settings-note">No checks reported</p>';
  } catch (e) {
    if (run !== healthRun) return;
    console.error('Failed to load health checks:', e);
    el.dataset.widgetFailed = '1';
    if (summary) summary.textContent = '';
    el.innerHTML = '<p class="sova-settings-note">Failed to run health checks</p>';
  }
}

/* --- Overview: capacity recommendation beside the agent limit --------------------------------------------- */

async function loadCapacity(el) {
  begin(el);
  try {
    const rec = await fetchAPI(apiUrl('/resources/capacity'));
    if (!rec) { el.textContent = ''; return; }
    if (rec.confidence === 'insufficient') {
      el.innerHTML = '<span class="sova-settings-status-muted">' + escapeHtml(rec.reason || 'Not enough runs yet to recommend a limit.') + '</span>';
      return;
    }
    el.innerHTML = `<span>Recommended: <strong class="text-accent-purple">${escapeHtml(rec.recommended_max)}</strong>
      (${escapeHtml(rec.confidence)} confidence)</span>
      ${rec.reason ? `<span class="sova-settings-status-muted">${escapeHtml(rec.reason)}</span>` : ''}`;
  } catch (_e) {
    el.textContent = '';
    el.dataset.widgetFailed = '1';
  }
}

/* --- Project: personas ----------------------------------------------------------------------------------- */

async function loadPersonas(el) {
  try {
    const data = await fetchAPI(apiUrl('/settings/personas'));
    const personas = data.personas || [];
    setState(el, data.detected ? 'ok' : 'muted');
    el.innerHTML = `<span class="sova-settings-status-title">${data.detected ? `Persona: ${escapeHtml(data.detected)} detected` : 'No persona detected'}</span>
      ${personas.length ? `<span class="sova-settings-status-muted">Available:</span><span class="sova-settings-tags">${personas.map((p) =>
        `<span class="sova-settings-tag${p.name === data.detected ? ' sova-settings-tag-on' : ''}">${escapeHtml(p.name)}</span>`).join('')}</span>`
        : '<span class="sova-settings-status-muted">No persona files found</span>'}`;
  } catch (e) {
    console.error('Failed to load personas:', e);
    failed(el, 'Could not load personas');
  }
}

/* --- Task Source: GitHub labels -------------------------------------------------------------------------- */

const labelsState = { loading: false, reload: false };

async function loadLabelsStatus(el) {
  el = el || widget('labels');
  if (!el) return;
  // A reload asked for while one is in flight (after creating labels) runs once that one finishes, so the
  // widget never keeps the answer that predates the change.
  if (labelsState.loading) { labelsState.reload = true; return; }
  labelsState.loading = true;
  labelsState.reload = false;
  try {
    const data = await fetchAPI(apiUrl('/settings/labels/audit'));
    if (data.error) {
      setState(el, 'warn');
      el.innerHTML = '<span class="sova-settings-status-title">Labels not checked</span>' +
        '<span class="sova-settings-status-muted">' + escapeHtml(data.error) + '. Ensure the GitHub CLI is installed and signed in (<code>gh auth login</code>).</span>';
    } else if (data.missing && data.missing.length) {
      setState(el, 'warn');
      el.innerHTML = `<span class="sova-settings-status-title">${escapeHtml(data.missing.length)} tracker label${data.missing.length !== 1 ? 's' : ''} missing</span>
        <span class="sova-settings-tags">${data.missing.map((l) => `<span class="sova-settings-tag">${escapeHtml(l.name)}</span>`).join('')}</span>
        <button type="button" class="sova-btn sova-btn-primary" data-act="labels-create">Create ${escapeHtml(data.missing.length)} missing label${data.missing.length !== 1 ? 's' : ''}</button>`;
    } else {
      setState(el, 'ok');
      el.innerHTML = '<span class="sova-settings-status-title">All ' + escapeHtml(data.total_required) + ' tracker labels exist</span>' +
        '<button type="button" class="sova-setting-link" data-act="labels-recheck">Check again</button>';
    }
  } catch (e) {
    console.error('Failed to load labels status:', e);
    failed(el, 'Could not check the tracker labels');
  } finally {
    labelsState.loading = false;
    if (labelsState.reload) loadLabelsStatus(el);
  }
}

async function createMissingLabels(btn) {
  btn.disabled = true;
  btn.textContent = 'Creating...';
  try {
    const res = await fetch(apiUrl('/settings/labels/create'), { method: 'POST', headers: jsonCsrfHeaders() });
    if (!res.ok) {
      showToast(await getErrorDetail(res, 'Failed to create labels'), 'error');
      btn.disabled = false;
      btn.textContent = 'Retry';
      return;
    }
    const data = await res.json();
    const message = data.created + ' label' + (data.created !== 1 ? 's' : '') + ' created';
    if (data.errors && data.errors.length) {
      showToast(message + ', but ' + (data.errors.length + (data.errors_truncated || 0)) + ' failed (see console)', 'warning');
      console.error('Label creation errors:', data.errors);
    } else {
      showToast(message, 'success');
    }
    loadLabelsStatus();
  } catch (e) {
    showToast('Failed to create labels: ' + e.message, 'error');
    btn.disabled = false;
    btn.textContent = 'Retry';
  }
}

/* --- Validation: invariants ------------------------------------------------------------------------------ */

async function loadInvariants(el) {
  try {
    const data = await fetchAPI(apiUrl('/settings/invariants'));
    const list = data.invariants || [];
    if (!list.length) { failed(el, 'No invariant checks found'); return; }
    const notExec = list.filter((i) => !i.executable).length;
    setState(el, notExec ? 'warn' : 'ok');
    el.innerHTML = `<span class="sova-settings-status-title">${escapeHtml(list.length)} invariant check${list.length !== 1 ? 's' : ''}</span>
      <span class="sova-settings-status-muted">${notExec ? `${escapeHtml(notExec)} not executable` : 'all executable'}</span>
      <span class="sova-settings-tags">${list.map((i) =>
        `<span class="sova-settings-tag font-mono${i.executable ? '' : ' sova-settings-tag-warn'}">${escapeHtml(i.name)}</span>`).join('')}</span>`;
  } catch (e) {
    console.error('Failed to load invariants:', e);
    failed(el, 'Could not list the invariant checks');
  }
}

/* --- Oversight: operations persona ----------------------------------------------------------------------- */

async function loadPersonaPreview(el) {
  try {
    const data = await fetchAPI(apiUrl('/settings/persona'));
    el.innerHTML = '<div class="sova-settings-status-line" data-state="muted"><span class="sova-settings-status-title">Operations persona</span>' +
        '<span class="sova-settings-status-muted font-mono">' + escapeHtml(data.path) + '</span>' +
        (data.is_default ? '<span class="sova-settings-status-muted">still the default text</span>' : '') +
        '<button type="button" class="sova-btn sova-btn-primary sova-settings-push" data-act="persona-open">Open in editor</button></div>' +
      '<pre class="sova-settings-pre">' + escapeHtml(data.content) + '</pre>';
  } catch (e) {
    console.error('Failed to load persona:', e);
    failed(el, 'Could not load the operations persona');
  }
}

async function openPersonaEditor() {
  try {
    const res = await fetch(apiUrl('/settings/persona/open'), { method: 'POST', headers: jsonCsrfHeaders() });
    if (!res.ok) showToast(await getErrorDetail(res, 'Failed to open the editor'), 'error');
  } catch (e) {
    showToast('Failed to open the editor: ' + e.message, 'error');
  }
}

/* --- LLM Provider and Agent: connection tests ------------------------------------------------------------ */

function renderLlmTools(el) {
  el.innerHTML = '<button type="button" class="sova-btn sova-btn-primary" data-act="llm-test">Test connection</button>' +
    '<span class="sova-settings-status-muted" data-part="test-result"></span>' +
    '<span class="sova-settings-status-muted sova-settings-push">Projected monthly cost: <span class="font-mono" data-part="cost">loading...</span></span>';
  loadCostProjection(el.querySelector('[data-part="cost"]'));
}

function renderRuntimeTools(el) {
  el.innerHTML = '<button type="button" class="sova-btn sova-btn-primary" data-act="runtime-test">Test runtime</button>' +
    '<span class="sova-settings-status-muted" data-part="test-result"></span>';
}

async function loadCostProjection(el) {
  try {
    const data = await fetchAPI(apiUrl('/settings/llm/cost-projection'));
    el.textContent = data.insufficient_data
      ? 'not enough data yet'
      : '$' + parseFloat(data.projected_monthly_usd || 0).toFixed(2) + '/mo (from $' + parseFloat(data.window_total_usd || 0).toFixed(2) +
        ' over ' + data.observed_days + ' active day' + (data.observed_days !== 1 ? 's' : '') + ')';
  } catch (_e) {
    el.textContent = 'unavailable';
    const owner = el.closest('[data-widget]');
    if (owner) owner.dataset.widgetFailed = '1';
  }
}

async function testConnection(btn, url, okText, failText) {
  const result = btn.parentElement.querySelector('[data-part="test-result"]');
  result.textContent = 'Testing...';
  result.className = 'sova-settings-status-muted';
  btn.disabled = true;  // two quick clicks must not race, a slow failure replacing a fast success
  try {
    const res = await fetch(apiUrl(url), { method: 'POST', headers: jsonCsrfHeaders() });
    if (!res.ok) {
      result.textContent = 'Failed: ' + await getErrorDetail(res, failText);
      result.className = 'text-xs text-accent-red';
      return;
    }
    const data = await res.json();
    if (data.ok) {
      result.textContent = okText + (data.detail ? ' (' + data.detail + ')' : '');
      result.className = 'text-xs text-accent-green';
    } else {
      result.textContent = 'Failed: ' + (data.detail || failText);
      result.className = 'text-xs text-accent-red';
    }
  } catch (e) {
    result.textContent = 'Failed: ' + e.message;
    result.className = 'text-xs text-accent-red';
  } finally {
    btn.disabled = false;
  }
}

/* --- Events ---------------------------------------------------------------------------------------------- */

export function handleClick(target) {
  const btn = target.closest('[data-act]');
  if (!btn) return false;
  const act = btn.dataset.act;
  if (act === 'install-recheck') loadInstallationStatus();
  else if (act === 'install-sync') syncInstallation(btn);
  else if (act === 'install-review') openReviewChanges();
  else if (act === 'review-close') closeReviewChanges();
  else if (act === 'review-apply') applySelectedChanges(btn);
  else if (act === 'health-run') loadHealthChecks();
  else if (act === 'labels-create') createMissingLabels(btn);
  else if (act === 'labels-recheck') loadLabelsStatus();
  else if (act === 'persona-open') openPersonaEditor();
  else if (act === 'llm-test') testConnection(btn, '/settings/llm/test-connection', 'Connected', 'provider unavailable');
  else if (act === 'runtime-test') testConnection(btn, '/settings/agent-runtime/test-connection', 'Available', 'runtime unavailable');
  else return false;
  return true;
}

let initialised = false;

export function init() {
  if (initialised) return;  // a second init would register the keydown handler twice
  initialised = true;
  const modal = document.getElementById('review-changes-modal');
  if (modal) modal.addEventListener('click', (e) => { if (e.target === modal) closeReviewChanges(); });
  document.addEventListener('keydown', trapReviewFocus);
}
