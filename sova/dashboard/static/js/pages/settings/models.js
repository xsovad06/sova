/* The Models view, "Which model runs where" (#1463, anchor #models, first in Models and agents).

   Renders GET /settings/models/map: the default model and fallbacks, one block per role with its model, its
   fallbacks and its steps, the writing tasks, the issue-size models and the other model settings. Which model a
   step runs on, and why, comes from the server (model_map_service.build_model_map()); this module only shows it.

   Writes: a role model, the default model, a fallback list and the other model settings save through rows.js
   (saveRow, resetRow), so their rows elsewhere on the page stay in step; a step's model writes or deletes its own
   llm.routing.<step> row (POST /settings/models/step); Pin and Pin all replace short model names with the exact
   ID each runs as (POST /settings/models/pin). Every write re-reads the map, since one setting can change what
   many steps run on. */

import * as state from './state.js';
import * as rows from './rows.js';

let map = null;
let llmFamilies = null;
let runtimeFamilies = null;
const modelNames = new Map();
// Per-row messages, kept across re-renders: row id -> { text, kind }.
const messages = new Map();
// Model dropdowns whose "Custom model ID..." box is open, by target.
const customOpen = new Set();
let refreshTimer = null;
let loading = null;

function host() { return document.querySelector('[data-part="models-view"]'); }
function attr(value) { return escapeHtml(String(value == null ? '' : value)); }

export function isLoaded() { return map !== null; }

/* Settings whose change can move a step to another model: the Models view reloads after a save of one. */
const AFFECTING_KEYS = ['agent.model', 'agent.fallback_models', 'agent.runtime', 'confidence.model', 'oversight.analysis_model'];
export function affects(key) {
  return /^(roles|llm|review\.panel)\./.test(key) || AFFECTING_KEYS.indexOf(key) !== -1;
}

function rememberNames(byFamily) {
  Object.values(byFamily || {}).forEach((list) => list.forEach((m) => modelNames.set(m.id, m.display_name || m.id)));
}

/* Fetches the map and renders it, then renders again once the model lists arrive: enumerating the provider's
   models can take seconds, and until then each dropdown offers the stored value and "Custom model ID...".
   Concurrent calls share one load. */
export function load() {
  if (loading) return loading;
  loading = (async () => {
    try {
      const lists = Promise.all([rows.fetchModelOptions('llm'), rows.fetchModelOptions('runtime')]);
      const res = await fetch(apiUrl('/settings/models/map'));
      if (!res.ok) throw new Error(await getErrorDetail(res, 'Could not load the model map'));
      map = await res.json();
      rows.setShortNames(map.short_names);
      render();
      const [llm, runtime] = await lists;
      if (llm !== llmFamilies || runtime !== runtimeFamilies) {
        llmFamilies = llm;
        runtimeFamilies = runtime;
        rememberNames(llm);
        rememberNames(runtime);
        render();
      }
    } catch (e) {
      const el = host();
      if (el) el.innerHTML = '<p class="sova-settings-note sova-settings-note-warn">' + escapeHtml(e.message) + '</p>';
    } finally {
      loading = null;
    }
  })();
  return loading;
}

/* Reloads soon after a save, once for a burst of saves (Pin all saves several keys). */
export function scheduleRefresh() {
  if (!map) return;
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(load, 60);
}

/* --- Text ----------------------------------------------------------------------------------------------- */

function nameOf(id) {
  if (!id) return 'Provider default';
  return modelNames.get(id) || id;
}

function chainText(list) {
  return list && list.length ? 'Then ' + list.map(nameOf).join(', then ') : 'No fallback';
}

const SOURCE_CLASS = { route: 'sova-mm-src-route', pin: 'sova-mm-src-pin' };

/* The model an outcome names, as text: one model, or the range a developer run picks from by issue size. */
function modelText(entry) {
  if (entry.source !== 'size' || !entry.by_size) return nameOf(entry.runs_as);
  const names = [];
  entry.by_size.forEach((t) => { if (names.indexOf(nameOf(t.runs_as)) === -1) names.push(nameOf(t.runs_as)); });
  return names.join(' to ');
}

/* The model a step runs on, where it comes from, and its fallbacks. */
function effectiveHtml(entry) {
  const tip = entry.source === 'size' && entry.by_size ? entry.by_size.map((t) => t.tier + ': ' + nameOf(t.runs_as)).join(', ') : '';
  const lines = ['<span class="sova-mm-model"' + (tip ? ' data-tooltip="' + attr(tip) + '" data-tooltip-wide' : '') + '>' + escapeHtml(modelText(entry)) + '</span>',
    '<span class="sova-mm-src ' + (SOURCE_CLASS[entry.source] || '') + '">' + escapeHtml(entry.why) + '</span>'];
  if (entry.later_attempts) lines.push('<span class="sova-mm-src">Later fix attempts: ' + escapeHtml(modelText(entry.later_attempts)) + '</span>');
  if (entry.address_review) lines.push('<span class="sova-mm-src">Address-review runs: ' + escapeHtml(nameOf(entry.address_review.runs_as)) + '</span>');
  if (entry.dimension_models) {
    lines.push('<span class="sova-mm-src">' + escapeHtml(Object.entries(entry.dimension_models).map(([d, m]) => d + ': ' + nameOf(m)).join(', ')) + '</span>');
  }
  if (entry.source === 'size' && entry.by_size && entry.by_size[0].fallbacks) {
    // Each issue size skips the fallback equal to the model it picked, so the chain is listed per size.
    const perSize = entry.by_size.map((t) => t.tier + ': ' + chainText(t.fallbacks).toLowerCase()).join('; ');
    lines.push('<span class="sova-mm-src" data-tooltip="' + attr(perSize) + '" data-tooltip-wide>Fallbacks by issue size, skipping the model picked</span>');
  } else if (entry.fallbacks) lines.push('<span class="sova-mm-src">' + escapeHtml(chainText(entry.fallbacks)) + '</span>');
  if (entry.note) lines.push('<span class="sova-mm-src">' + escapeHtml(entry.note) + '</span>');
  return '<div class="sova-mm-eff">' + lines.join('') + '</div>';
}

function msgHtml(rowId) {
  const m = messages.get(rowId);
  const kind = m && m.kind ? ' sova-setting-msg-' + m.kind : '';
  return '<div class="sova-setting-msg' + kind + '" data-part="mm-msg" role="status" aria-live="polite">' + (m ? escapeHtml(m.text) : '') + '</div>';
}

function setMessage(rowId, text, kind) {
  if (text) messages.set(rowId, { text, kind });
  else messages.delete(rowId);
  const row = document.querySelector('[data-mm-row="' + rowId.replace(/["\\]/g, '\\$&') + '"]');
  const msg = row && row.querySelector('[data-part="mm-msg"]');
  if (msg) msg.outerHTML = msgHtml(rowId);
  if (kind === 'ok') {
    setTimeout(() => { if ((messages.get(rowId) || {}).text === text) setMessage(rowId, '', null); }, 3000);
  }
}

/* --- Short names ------------------------------------------------------------------------------------------ */

function shortName(key) { return (map.short_names || []).find((item) => item.key === key) || null; }

function shortPill(key) {
  const item = shortName(key);
  if (!item) return '';
  const pill = '<span class="sova-setting-pill sova-setting-pill-short" data-tooltip="' +
    attr('Stored as the short name ' + JSON.stringify(item.value) + '. A new release can move it to a model your account lacks.') +
    '" data-tooltip-wide>Short name</span>';
  if (item.env_var) return pill;
  if (!item.pin_to) return pill + '<span class="sova-settings-status-muted">Pick a model: the name matches several</span>';
  const to = Array.isArray(item.pin_to) ? item.pin_to.map(nameOf).join(', ') : nameOf(item.pin_to);
  return pill + '<button type="button" class="sova-setting-link" data-mm="pin" data-key="' + attr(key) + '">Pin to ' + escapeHtml(to) + '</button>';
}

function pinAllHtml() {
  const items = (map.short_names || []).filter((item) => !item.env_var);
  if (!items.length) return '';
  const pinnable = items.filter((item) => item.pin_to);
  return '<div class="sova-mm-pinall" data-mm-row="pin-all"><div class="sova-mm-pinall-text"><span class="sova-mm-pinall-title">' +
    items.length + (items.length === 1 ? ' setting uses a short model name' : ' settings use a short model name') + '</span> instead of an exact model: ' +
    items.map((item) => '<code>' + escapeHtml(item.key) + '</code>').join(', ') + '. Pin writes the exact model each runs as today.</div>' +
    (pinnable.length ? '<button type="button" class="sova-btn sova-btn-warning" data-mm="pin-all">Pin all</button>' : '') + msgHtml('pin-all') + '</div>';
}

/* --- Controls --------------------------------------------------------------------------------------------- */

// Model dropdowns to build after the HTML is in place: placeholder id -> spec.
let pendingSelects = [];

/* A placeholder for a model dropdown. *target* is "key:<setting>" or "step:<routing key>". */
function selectSlot(target, current, firstLabel, label, layer) {
  const key = target.indexOf('key:') === 0 ? target.slice(4) : null;
  const s = key ? state.setting(key) : state.setting('llm.routing');
  if (s && s.env_var) {
    return '<span class="sova-setting-readonly" data-tooltip="' + attr(s.env_var + ' sets this value') + '">' + escapeHtml(current || firstLabel || '') + '</span>';
  }
  pendingSelects.push({ target, current: current || '', firstLabel, label, layer: layer || 'llm' });
  const custom = customOpen.has(target)
    ? '<div class="sova-setting-custom"><input type="text" class="sova-input" data-mm-custom="' + attr(target) + '" placeholder="Model ID, e.g. ollama/qwen3:32b" ' +
      'aria-label="' + attr('Custom model ID for ' + label) + '" spellcheck="false" autocomplete="off">' +
      '<button type="button" class="sova-btn sova-btn-primary" data-mm="custom-save" data-target="' + attr(target) + '">Use</button>' +
      '<button type="button" class="sova-setting-link" data-mm="custom-cancel" data-target="' + attr(target) + '">Cancel</button></div>'
    : '';
  return '<span data-mm-slot="' + (pendingSelects.length - 1) + '"></span>' + custom;
}

function mountSelects(root) {
  root.querySelectorAll('[data-mm-slot]').forEach((slot) => {
    const spec = pendingSelects[Number(slot.dataset.mmSlot)];
    const byFamily = spec.layer === 'runtime' ? runtimeFamilies : llmFamilies;
    const select = rows.buildModelSelect({
      current: spec.current, byFamily, label: spec.label, firstLabel: spec.firstLabel, dataset: { mm: 'model', target: spec.target },
    });
    // The default model always names a model: no empty choice.
    if (spec.firstLabel === null) select.remove(0);
    if (customOpen.has(spec.target)) select.value = rows.CUSTOM_MODEL;
    slot.replaceWith(select);
  });
  pendingSelects = [];
}

/* An ordered fallback list with Up, Down and Remove, and an Add picker. Every change saves the list. */
function fallbackEditor(key, list, primary) {
  const s = state.setting(key);
  if (s && s.env_var) {
    return '<span class="sova-setting-readonly">' + escapeHtml(list.join(', ') || 'none') + ' (set by ' + escapeHtml(s.env_var) + ')</span>';
  }
  const items = list.map((model, i) => '<li class="sova-mm-fb-item"><span class="sova-mm-fb-ix">' + (i + 1) + '</span>' +
    '<span class="sova-mm-fb-name">' + escapeHtml(nameOf(model)) +
    (model === primary ? ' <span class="sova-setting-pill" data-tooltip="Already tried first, so this entry is skipped">same as the model</span>' : '') + '</span>' +
    '<button type="button" class="sova-btn" data-mm="fb-up" data-key="' + attr(key) + '" data-i="' + i + '"' + (i === 0 ? ' disabled' : '') + ' aria-label="' + attr('Move up ' + model) + '">Up</button>' +
    '<button type="button" class="sova-btn" data-mm="fb-down" data-key="' + attr(key) + '" data-i="' + i + '"' + (i === list.length - 1 ? ' disabled' : '') + ' aria-label="' + attr('Move down ' + model) + '">Down</button>' +
    '<button type="button" class="sova-btn" data-mm="fb-del" data-key="' + attr(key) + '" data-i="' + i + '" aria-label="' + attr('Remove ' + model) + '">Remove</button></li>').join('');
  const addable = [];
  Object.values(llmFamilies || {}).forEach((group) => group.forEach((m) => { if (list.indexOf(m.id) === -1) addable.push(m); }));
  const add = addable.length
    ? '<select class="sova-input" data-mm="fb-add" data-key="' + attr(key) + '" aria-label="Add a fallback model"><option value="">Add a fallback...</option>' +
      addable.map((m) => '<option value="' + attr(m.id) + '">' + escapeHtml(m.display_name || m.id) + '</option>').join('') + '</select>'
    : '';
  return (items ? '<ol class="sova-mm-fb">' + items + '</ol>' : '<p class="sova-settings-note">No fallbacks: a billing or rate-limit error stops the call.</p>') + add;
}

/* --- Blocks ----------------------------------------------------------------------------------------------- */

function stepRow(step, firstLabel) {
  const rowId = 'step:' + step.step;
  const short = shortName('llm.routing.' + step.step);
  return '<div class="sova-mm-grid sova-mm-step" data-mm-row="' + attr(rowId) + '">' +
    '<div><div class="sova-mm-step-name">' + escapeHtml(step.step) + (short ? ' ' + shortPill('llm.routing.' + step.step) : '') + '</div>' +
    '<div class="sova-mm-desc">' + escapeHtml(step.what) + '</div></div>' +
    '<div>' + selectSlot(rowId, step.route, firstLabel, 'Model for ' + step.step) + msgHtml(rowId) + '</div>' +
    effectiveHtml(step) + '</div>';
}

function roleBlock(role) {
  const fb = role.fallbacks;
  const modelRow = 'key:' + role.model_key;
  const fbRow = 'fb:' + fb.key;
  const fbSetting = state.setting(fb.key);
  const fbLocked = fbSetting && fbSetting.env_var;
  const head = '<div class="sova-mm-grid sova-mm-role-head" data-mm-row="' + attr(modelRow) + '">' +
    '<div><div class="sova-mm-role-name">' + escapeHtml(role.label) + ' ' + shortPill(role.model_key) + '</div>' +
    '<div class="sova-mm-desc">' + escapeHtml(role.what) + '</div><div class="sova-setting-key">' + escapeHtml(role.model_key) + '</div></div>' +
    '<div>' + selectSlot(modelRow, role.model, role.unset_label, role.label + ' model') + msgHtml(modelRow) + '</div>' +
    '<div data-mm-row="' + attr(fbRow) + '"><select class="sova-input" data-mm="fb-mode" data-key="' + attr(fb.key) + '" aria-label="' + attr(role.label + ' fallbacks') + '"' + (fbLocked ? ' disabled' : '') + '>' +
      '<option value="default"' + (fb.own ? '' : ' selected') + '>Default fallbacks</option>' +
      '<option value="own"' + (fb.own ? ' selected' : '') + '>Own fallbacks</option></select>' +
      '<div class="sova-mm-desc">' + escapeHtml(fb.models.length ? fb.models.map(nameOf).join(', then ') : 'None') + '</div>' + msgHtml(fbRow) + '</div></div>';
  const own = fb.own
    ? '<div class="sova-mm-sub" data-mm-row="' + attr('list:' + fb.key) + '"><div class="sova-mm-desc">' + escapeHtml(role.label) +
      ' fallbacks, tried in order (<code>' + escapeHtml(fb.key) + '</code>)</div>' + fallbackEditor(fb.key, fb.models, null) + msgHtml('list:' + fb.key) + '</div>'
    : '';
  let pin = '';
  if (role.pin) {
    const pinRow = 'key:' + role.pin.key;
    pin = role.pin.applies
      ? '<div class="sova-mm-sub sova-mm-pin" data-mm-row="' + attr(pinRow) + '"><div><span class="sova-mm-pin-title">Pin for code-editing steps</span> ' +
        '(<code>' + escapeHtml(role.pin.key) + '</code>) outranks every setting below for the steps that edit code.</div>' +
        '<div>' + selectSlot(pinRow, role.pin.model, 'No pin', 'Developer pin', 'runtime') + msgHtml(pinRow) + '</div></div>'
      : '<div class="sova-mm-sub"><span class="sova-settings-note">The ' + escapeHtml(map.runtime.name) + ' runtime runs its own model, so a pin for code-editing steps has no effect.</span></div>';
  }
  const firstLabel = 'Use the ' + role.label.toLowerCase() + ' model';
  return '<div class="sova-mm-role" id="models-role-' + attr(role.id) + '">' + head + own + pin +
    role.steps.map((step) => stepRow(step, firstLabel)).join('') + '</div>';
}

function writingBlock() {
  return '<div class="sova-mm-role"><div class="sova-mm-grid sova-mm-role-head"><div><div class="sova-mm-role-name">Writing and other tasks</div>' +
    '<div class="sova-mm-desc">Short text jobs. Each runs on the model shown unless set here.</div></div></div>' +
    map.writing.map((step) => stepRow(step, 'No setting for this step')).join('') + '</div>';
}

function tiersBlock() {
  const cells = map.tiers.map((t) => {
    const rowId = 'step:' + t.tier;
    return '<div class="sova-mm-tier" data-mm-row="' + attr(rowId) + '"><span class="sova-mm-tier-name">' + escapeHtml(t.tier) + '</span>' +
      selectSlot(rowId, t.route, 'Built in: ' + t.built_in, 'Model for ' + t.tier + ' issues') +
      '<span class="sova-mm-src">Runs as ' + escapeHtml(nameOf(t.runs_as)) + '</span>' + msgHtml(rowId) + '</div>';
  }).join('');
  return '<div class="sova-mm-role"><div class="sova-mm-grid sova-mm-role-head"><div><div class="sova-mm-role-name">Developer model by issue size</div>' +
    '<div class="sova-mm-desc">Used by a developer run when no developer model is set. <code>llm.routing.&lt;size&gt;</code></div></div></div>' +
    '<div class="sova-mm-tiers">' + cells + '</div></div>';
}

function otherBlock() {
  return '<div class="sova-mm-role">' + map.other.map((o) => {
    const rowId = 'key:' + o.key;
    return '<div class="sova-mm-grid sova-mm-step" data-mm-row="' + attr(rowId) + '"><div><div class="sova-mm-role-name">' + escapeHtml(o.label) + ' ' + shortPill(o.key) + '</div>' +
      '<div class="sova-setting-key">' + escapeHtml(o.key) + '</div></div>' +
      '<div>' + selectSlot(rowId, o.model, 'Not set: the provider default', o.label + ' model') + msgHtml(rowId) + '</div>' +
      '<div class="sova-mm-eff"><span class="sova-mm-model">' + escapeHtml(nameOf(o.runs_as)) + '</span></div></div>';
  }).join('') + '</div>';
}

function catalogLine() {
  const n = Object.values(llmFamilies || {}).reduce((acc, list) => acc + list.length, 0);
  const text = llmFamilies ? n + ' models available from the provider' : 'The model list is unavailable: pick "Custom model ID..." to type one';
  return '<div class="sova-mm-catalog"><span>' + escapeHtml(text) + '</span><button type="button" class="sova-setting-link" data-mm="refresh">Refresh list</button></div>';
}

function render() {
  const el = host();
  if (!el || !map) return;
  pendingSelects = [];
  const y = window.scrollY;
  const routingEnv = (state.setting('llm.routing') || {}).env_var;
  // Every value in these blocks is escaped where it is interpolated, by the helpers above.
  const html =
    '<p class="sova-mm-intro">A step runs on the first of: the developer pin (code-editing steps only), the model set for that step, ' +
    'its role\'s model, then the default model. When that model hits a billing or rate-limit error, the role\'s fallbacks are tried in order.</p>' +
    (routingEnv ? '<p class="sova-settings-note sova-settings-note-warn">' + escapeHtml(routingEnv) + ' sets the step models, so they cannot be changed here.</p>' : '') +
    catalogLine() +
    '<div class="sova-mm-top">' +
      '<div class="sova-card"><div class="sova-card-body sova-mm-field" data-mm-row="key:agent.model"><span class="sova-settings-eyebrow">Default model ' + shortPill('agent.model') + '</span>' +
        selectSlot('key:agent.model', map.default.model, null, 'Default model') +
        '<span class="sova-settings-note">Used by every step with nothing more specific. <code>agent.model</code></span>' + msgHtml('key:agent.model') + '</div></div>' +
      '<div class="sova-card"><div class="sova-card-body sova-mm-field" data-mm-row="list:agent.fallback_models"><span class="sova-settings-eyebrow">Default fallbacks, tried in order</span>' +
        fallbackEditor('agent.fallback_models', map.fallbacks.models, map.default.runs_as) +
        '<span class="sova-settings-note">Used by every role without its own fallbacks, and by calls made outside a role. <code>agent.fallback_models</code></span>' +
        msgHtml('list:agent.fallback_models') + '</div></div>' +
    '</div>' +
    pinAllHtml() +
    '<div class="sova-mm-grid sova-mm-colhead"><span>Role and step</span><span>Model set here</span><span>Runs on, and fallbacks</span></div>' +
    map.roles.map(roleBlock).join('') + writingBlock() + tiersBlock() + otherBlock();
  el.innerHTML = html;
  mountSelects(el);
  const count = document.querySelector('[data-part="models-count"]');
  if (count) {
    const steps = map.roles.reduce((n, r) => n + r.steps.length, 0) + map.writing.length;
    count.textContent = map.roles.length + ' roles, ' + steps + ' steps';
  }
  window.scrollTo(0, y);
}

/* --- Writes ----------------------------------------------------------------------------------------------- */

function routingTable() {
  const s = state.setting('llm.routing');
  return Object.assign({}, (s && s.value) || {});
}

/* Records a routing change in the page's data and re-renders the llm.routing row, which reloads the map. */
function recordRouting(next) {
  const s = state.setting('llm.routing');
  if (!s) { scheduleRefresh(); return; }
  s.value = next;
  s.stored_rows = Object.keys(next).length;
  s.stored = s.stored_rows > 0;
  rows.renderKey('llm.routing');
}

async function post(path, body) {
  let res;
  try {
    res = await fetch(apiUrl(path), { method: 'POST', headers: jsonCsrfHeaders(), body: JSON.stringify(body) });
  } catch (_e) {
    return { ok: false, message: 'Could not reach the dashboard' };
  }
  if (!res.ok) return { ok: false, message: await getErrorDetail(res, 'Not saved') };
  const data = await res.json().catch(() => ({}));
  if (data.error) return { ok: false, message: String(data.error) };
  return { ok: true, data };
}

async function setStepModel(step, model) {
  const rowId = 'step:' + step;
  setMessage(rowId, 'Saving...', 'muted');
  const result = await post('/settings/models/step', { step, model });
  if (!result.ok) {
    setMessage(rowId, 'Not saved. ' + result.message, 'err');
    // Put the dropdown back on the stored value, but keep a custom ID the user typed so it can be corrected.
    if (!customOpen.has(rowId)) render();
    return;
  }
  customOpen.delete(rowId);
  const next = routingTable();
  if (model) next[step] = model;
  else delete next[step];
  setMessage(rowId, model ? 'Saved' : 'Removed the setting for this step', 'ok');
  recordRouting(next);
}

async function saveKey(key, raw, rowId) {
  setMessage(rowId, 'Saving...', 'muted');
  const result = await rows.saveRow(key, raw);
  if (!result.ok) {
    setMessage(rowId, 'Not saved. ' + result.message, 'err');
    if (!customOpen.has('key:' + key)) render();
    return;
  }
  customOpen.delete('key:' + key);
  setMessage(rowId, result.warning || result.lifecycleError ? 'Saved. ' + (result.warning || result.lifecycleError) : 'Saved', result.warning || result.lifecycleError ? 'warn' : 'ok');
}

async function resetKey(key, rowId) {
  setMessage(rowId, 'Saving...', 'muted');
  const result = await rows.resetRow(key);
  setMessage(rowId, result.ok ? 'Uses the default fallbacks again' : 'Not saved. ' + result.message, result.ok ? 'ok' : 'err');
  if (!result.ok) render();
}

function applyModel(target, value) {
  if (target.indexOf('step:') === 0) return setStepModel(target.slice(5), value);
  const key = target.slice(4);
  return saveKey(key, value, target);
}

/* The Models view row a setting's messages show on. */
function rowOfKey(key) { return key.indexOf('llm.routing.') === 0 ? 'step:' + key.slice(12) : 'key:' + key; }

async function pin(keys) {
  const rowId = keys ? rowOfKey(keys[0]) : 'pin-all';
  setMessage(rowId, 'Pinning...', 'muted');
  const result = await post('/settings/models/pin', keys ? { keys } : {});
  if (!result.ok) {
    setMessage(rowId, 'Not pinned. ' + result.message, 'err');
    return;
  }
  const pinned = result.data.pinned || {};
  const routing = routingTable();
  let routingChanged = false;
  Object.entries(pinned).forEach(([key, value]) => {
    if (key.indexOf('llm.routing.') === 0) {
      routing[key.slice(12)] = value;
      routingChanged = true;
    } else {
      state.recordSaved(key, value);
      rows.renderKey(key);
    }
  });
  if (routingChanged) recordRouting(routing);
  const skipped = result.data.skipped || [];
  const n = Object.keys(pinned).length;
  const text = 'Pinned ' + n + (n === 1 ? ' setting' : ' settings') + (skipped.length ? '. Left alone: ' + skipped.map((s) => s.key + ' (' + s.reason + ')').join(', ') : '');
  setMessage(rowId, text, skipped.length ? 'warn' : 'ok');
  scheduleRefresh();
}

function currentList(key) {
  if (key === 'agent.fallback_models') return map.fallbacks.models.slice();
  const role = map.roles.find((r) => r.fallbacks.key === key);
  return role ? role.fallbacks.models.slice() : [];
}

function changeList(key, act, index, value) {
  const list = currentList(key);
  if (act === 'fb-up' && index > 0) list.splice(index - 1, 0, list.splice(index, 1)[0]);
  else if (act === 'fb-down' && index < list.length - 1) list.splice(index + 1, 0, list.splice(index, 1)[0]);
  else if (act === 'fb-del') list.splice(index, 1);
  else if (act === 'fb-add' && value && list.indexOf(value) === -1) list.push(value);
  else return;
  saveKey(key, JSON.stringify(list), 'list:' + key);
}

async function refreshList() {
  try {
    await fetch(apiUrl('/models/available?layer=llm&refresh=true'));
  } catch (_e) { /* the next load reports an unavailable list */ }
  rows.forgetModelOptions('llm');
  modelNames.clear();
  await load();
}

/* --- Events, delegated from the page ---------------------------------------------------------------------- */

export function handleClick(target) {
  const btn = target.closest('[data-mm]');
  if (!btn || btn.tagName === 'SELECT' || !btn.closest('[data-part="models-view"]')) return false;
  const act = btn.dataset.mm;
  if (act === 'pin') pin([btn.dataset.key]);
  else if (act === 'pin-all') pin(null);
  else if (act === 'refresh') refreshList();
  else if (act === 'custom-save') {
    const input = document.querySelector('[data-mm-custom="' + btn.dataset.target.replace(/["\\]/g, '\\$&') + '"]');
    const value = input ? input.value.trim() : '';
    if (value) applyModel(btn.dataset.target, value);
  } else if (act === 'custom-cancel') {
    customOpen.delete(btn.dataset.target);
    render();
  } else if (act.indexOf('fb-') === 0) changeList(btn.dataset.key, act, Number(btn.dataset.i));
  else return false;
  return true;
}

export function handleChange(target) {
  const act = target.dataset && target.dataset.mm;
  if (!act) return false;
  if (act === 'model') {
    if (target.value === rows.CUSTOM_MODEL) {
      customOpen.add(target.dataset.target);
      render();
      const input = document.querySelector('[data-mm-custom="' + target.dataset.target.replace(/["\\]/g, '\\$&') + '"]');
      if (input) input.focus();
    } else {
      applyModel(target.dataset.target, target.value);
    }
    return true;
  }
  if (act === 'fb-mode') {
    const key = target.dataset.key;
    if (target.value === 'own') saveKey(key, JSON.stringify(map.fallbacks.models), 'fb:' + key);
    else resetKey(key, 'fb:' + key);
    return true;
  }
  if (act === 'fb-add') {
    changeList(target.dataset.key, 'fb-add', -1, target.value);
    return true;
  }
  return false;
}

export function handleKeydown(e) {
  const t = e.target;
  if (!t.dataset || !t.dataset.mmCustom) return false;
  if (e.key === 'Enter' && t.value.trim()) { e.preventDefault(); applyModel(t.dataset.mmCustom, t.value.trim()); return true; }
  if (e.key === 'Escape') { e.preventDefault(); customOpen.delete(t.dataset.mmCustom); render(); return true; }
  return false;
}
