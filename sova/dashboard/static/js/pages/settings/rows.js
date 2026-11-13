/* One setting row: its label, badges, control, default and Reset, and its message line. A row is any element with
   data-key and data-ctx="row" (in a section or in the search and changed-only results); an Overview copy has
   data-ctx="overview". Saving or resetting a setting re-renders only the elements bound to that key, never the
   page or its widgets.

   Switches and the two steppers are app.js's shared controls (settingSwitch, settingStepper, mounted with
   mountSettingControls), which save themselves and keep every control of the same key in step. Every other
   control saves through app.js's saveSetting(). */

import {
  view, setting, allSettings, recordSaved, recordReset, sameValue, tokens, isKeyringAvailable, dropLeftover,
} from './state.js';

// Settings shown as a stepper, here and in the Overview.
const STEPPERS = {
  max_parallel_agents: { min: 1, step: 1, prefix: '' },
  'agent.max_issue_budget': { min: 5, step: 5, prefix: '$' },
};

const LIST_HINT = 'Comma-separated, e.g. gmail, gcal';
const CUSTOM_MODEL = '__custom__';

// Called after a key's rows re-render, so the page can update its counts and anything that depends on the key.
const hooks = { afterRender: () => {} };
export function onKeyRendered(fn) { hooks.afterRender = fn; }

function attr(value) { return escapeHtml(String(value == null ? '' : value)); }

function attrSelector(name, value) { return '[' + name + '="' + String(value).replace(/["\\]/g, '\\$&') + '"]'; }

function elementsFor(key) { return Array.from(document.querySelectorAll(attrSelector('data-key', key))); }

/* Highlights the search words in already-plain text. */
function highlight(text, toks) {
  const plain = String(text == null ? '' : text);
  if (!toks.length) return escapeHtml(plain);
  const pattern = new RegExp('(' + toks.map((t) => t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|') + ')', 'gi');
  return plain.split(pattern).map((part, i) => (i % 2 ? '<mark class="sova-settings-mark">' + escapeHtml(part) + '</mark>' : escapeHtml(part))).join('');
}

/* A value as one line of text, for defaults and read-only values. */
export function formatValue(value, s) {
  if (s && s.value_type === 'secret') return value ? 'set (hidden)' : 'not set';
  if (typeof value === 'boolean') return value ? 'on' : 'off';
  if (Array.isArray(value)) return value.length ? value.join(', ') : 'empty list';
  if (value && typeof value === 'object') {
    const n = Object.keys(value).length;
    return n ? n + (n === 1 ? ' entry' : ' entries') : 'no entries';
  }
  if (value === '' || value === null || value === undefined) return 'empty';
  return String(value);
}

function isShared(s) {
  if (s.env_var) return false;
  return s.value_type === 'boolean' || !!STEPPERS[s.key];
}

function isModelField(s) { return !!s.options_source; }

/* Text the input shows for a value: a list as comma-separated entries, or as a JSON array when an entry holds a
   comma itself (a regex such as \d{1,3}, a glob such as {a,b}), which splitting on commas would cut apart. */
function inputText(s) {
  if (Array.isArray(s.value)) {
    return s.value.some((v) => String(v).indexOf(',') !== -1) ? JSON.stringify(s.value) : s.value.join(', ');
  }
  if (s.value === null || s.value === undefined) return '';
  return String(s.value);
}

export function badgesHtml(s) {
  const pills = [];
  if (s.env_var) {
    pills.push('<span class="sova-setting-pill sova-setting-pill-env" data-tooltip="' + attr(s.env_var + ' sets this value') + '">Environment</span>');
  } else if (s.stored) {
    pills.push(sameValue(s.value, s.default)
      ? '<span class="sova-setting-pill" data-tooltip="Stored for this project, but equal to the default">Stored, same as default</span>'
      : '<span class="sova-setting-pill sova-setting-pill-changed" data-tooltip="Stored for this project">Changed</span>');
  }
  if (s.requires_restart) pills.push('<span class="sova-setting-pill" data-tooltip="Takes effect after the server restarts">Restart</span>');
  return pills.join('');
}

function infoHtml(s, toks, inactive) {
  return '<div class="sova-setting-title"><span class="sova-setting-label">' + highlight(s.label, toks) + '</span>' +
    '<span class="sova-setting-badges" data-part="badges">' + badgesHtml(s) + '</span></div>' +
    '<div class="sova-setting-key">' + highlight(s.key, toks) + '</div>' +
    (s.description ? '<div class="sova-setting-desc">' + highlight(s.description, toks) + '</div>' : '') +
    (inactive ? '<div class="sova-setting-desc">Not used by the active provider or runtime. The stored value is kept.</div>' : '');
}

function metaHtml(s) {
  const parts = [];
  if (s.env_var) {
    parts.push('<span>Set by <code>' + escapeHtml(s.env_var) + '</code> in the server environment. Change it there; it has no Reset.</span>');
  } else if (s.stored) {
    const rows = s.value_type === 'object' && s.stored_rows > 1 ? ' (' + s.stored_rows + ' stored rows)' : '';
    parts.push('<span>Default: <code>' + escapeHtml(formatValue(s.default, s)) + '</code>' + rows + '</span>' +
      '<button type="button" class="sova-setting-link" data-act="reset">Reset to default</button>');
  }
  if (s.value_type === 'object') parts.push('<span>Read-only here.</span>');
  return parts.join('');
}

function editButtons() {
  return '<span class="sova-setting-edit-btns"><button type="button" class="sova-btn sova-btn-primary" data-act="save">Save</button>' +
    '<button type="button" class="sova-setting-link" data-act="cancel">Cancel</button></span>';
}

function textControl(s) {
  const list = s.value_type === 'list';
  const placeholder = list ? LIST_HINT : (isModelField(s) ? 'Model ID' : '');
  return '<input type="text" class="sova-input" data-act="text" value="' + attr(inputText(s)) + '"' +
    (placeholder ? ' placeholder="' + attr(placeholder) + '"' : '') +
    (s.value_type === 'number' ? ' inputmode="decimal"' : '') +
    ' aria-label="' + attr(s.label) + '" spellcheck="false" autocomplete="off">' + editButtons();
}

function objectControl(s) {
  const entries = Object.entries(s.value || {});
  if (!entries.length) return '<span class="sova-setting-readonly">No entries</span>';
  return '<table class="sova-setting-object"><tbody>' + entries.map(([k, v]) =>
    '<tr><td>' + escapeHtml(k) + '</td><td>' + escapeHtml(typeof v === 'object' ? JSON.stringify(v) : v) + '</td></tr>').join('') +
    '</tbody></table>';
}

// Secrets being replaced: their row shows a password field instead of the masked value.
const replacingSecrets = new Set();

function secretControl(s) {
  if (replacingSecrets.has(s.key)) {
    return '<input type="password" class="sova-input" data-act="text" placeholder="New value" aria-label="' + attr(s.label) + '" autocomplete="new-password">' +
      '<span class="sova-setting-edit-btns sova-setting-edit-btns-on"><button type="button" class="sova-btn sova-btn-primary" data-act="save">Save</button>' +
      '<button type="button" class="sova-setting-link" data-act="cancel">Cancel</button></span>';
  }
  const keychain = s.keyring_capable && s.secret_location === 'database' && isKeyringAvailable()
    ? '<button type="button" class="sova-setting-link" data-act="secret-keychain">Move to keychain</button>' : '';
  return '<span class="sova-setting-readonly">' + (s.value ? 'Set (hidden)' : 'Not set') + '</span>' +
    '<button type="button" class="sova-btn sova-btn-primary" data-act="secret-replace">' + (s.value ? 'Replace' : 'Set') + '</button>' + keychain;
}

export function controlHtml(s) {
  if (s.env_var) {
    return '<span class="sova-setting-readonly">' + escapeHtml(formatValue(s.value, s)) + '</span>';
  }
  if (s.value_type === 'boolean') {
    return '<span class="flex" data-setting-switch="' + attr(s.key) + '" data-label="' + attr(s.label) + '"></span>';
  }
  const stepper = STEPPERS[s.key];
  if (stepper) {
    return '<span class="flex" data-setting-stepper="' + attr(s.key) + '" data-label="' + attr(s.label) + '" data-min="' + stepper.min +
      '" data-step="' + stepper.step + '"' + (stepper.prefix ? ' data-prefix="' + attr(stepper.prefix) + '"' : '') + '></span>';
  }
  if (s.options && s.options.length) {
    return '<select class="sova-input" data-act="select" aria-label="' + attr(s.label) + '">' +
      s.options.map((o) => '<option value="' + attr(o) + '"' + (o === s.value ? ' selected' : '') + '>' + escapeHtml(o) + '</option>').join('') +
      '</select>';
  }
  if (s.value_type === 'secret') return secretControl(s);
  if (s.value_type === 'object') return objectControl(s);
  return textControl(s);
}

export function rowHtml(s, opts) {
  const hidden = opts && opts.hidden ? ' hidden' : '';
  return '<div class="sova-setting-row" data-key="' + attr(s.key) + '" data-ctx="row"' + hidden + '>' +
    '<div class="sova-setting-info" data-part="info">' + infoHtml(s, (opts && opts.toks) || [], !!(opts && opts.inactive)) + '</div>' +
    '<div class="sova-setting-side">' +
      '<div class="sova-setting-control" data-part="control">' + controlHtml(s) + '</div>' +
      '<div class="sova-setting-meta" data-part="meta">' + metaHtml(s) + '</div>' +
      '<div class="sova-setting-msg" data-part="msg" role="status" aria-live="polite"></div>' +
    '</div></div>';
}

/* --- Messages and flashes, on every element of a key --------------------------------------------------- */

const MSG_CLASSES = ['sova-setting-msg-ok', 'sova-setting-msg-warn', 'sova-setting-msg-err', 'sova-setting-msg-muted'];

export function setMsg(key, text, kind) {
  elementsFor(key).forEach((el) => {
    const msg = el.querySelector('[data-part="msg"]');
    if (!msg) return;
    MSG_CLASSES.forEach((c) => msg.classList.remove(c));
    if (kind) msg.classList.add('sova-setting-msg-' + kind);
    msg.textContent = text;
  });
  if (kind === 'ok') {
    setTimeout(() => {
      elementsFor(key).forEach((el) => {
        const msg = el.querySelector('[data-part="msg"]');
        if (msg && msg.textContent === text) msg.textContent = '';
      });
    }, 3000);
  }
}

function flash(key) {
  elementsFor(key).forEach((el) => {
    el.classList.add('sova-setting-flash');
    setTimeout(() => el.classList.remove('sova-setting-flash'), 900);
  });
}

function setPending(key, pending) {
  elementsFor(key).forEach((el) => el.classList.toggle('sova-setting-busy', pending));
}

/* --- Mounting controls ------------------------------------------------------------------------------------ */

/* Builds the shared controls in `root` with the values the page already has, so they need no load of their own,
   and reports their saves and failures on the rows of their key. */
export function mountControls(root) {
  const overrides = {};
  root.querySelectorAll('[data-setting-switch], [data-setting-stepper]').forEach((host) => {
    const key = host.dataset.settingSwitch || host.dataset.settingStepper;
    const s = setting(key);
    if (!s) return;
    overrides[key] = {
      value: s.value,
      onSaved: (value, result) => {
        recordSaved(key, value);
        renderKey(key);
        reportSaved(key, s, result);
      },
      onError: (message) => setMsg(key, 'Not saved. ' + message, 'err'),
    };
  });
  mountSettingControls(root, overrides);
  root.querySelectorAll('[data-ctx="row"]').forEach(upgradeModelControl);
  if (root.dataset && root.dataset.ctx === 'row') upgradeModelControl(root);
}

/* --- Re-rendering one key --------------------------------------------------------------------------------- */

function renderRow(el, s, withControl) {
  const inResults = typeof el.closest === 'function' && el.closest('#settings-results');
  const info = el.querySelector('[data-part="info"]');
  if (info) info.innerHTML = infoHtml(s, inResults ? tokens(view.query) : []);
  const meta = el.querySelector('[data-part="meta"]');
  if (meta) meta.innerHTML = metaHtml(s);
  if (!withControl) return;
  const control = el.querySelector('[data-part="control"]');
  if (control) control.innerHTML = controlHtml(s);
  el.classList.remove('sova-setting-dirty');
  mountControls(el);
}

/* Re-renders the rows and Overview copies of one key from the page's data. A shared control is told the new
   value instead of being rebuilt. */
export function renderKey(key) {
  const s = setting(key);
  if (!s) return;
  const shared = isShared(s);
  elementsFor(key).forEach((el) => {
    if (el.dataset.ctx === 'row') renderRow(el, s, !shared);
    else if (el.dataset.ctx === 'overview') {
      const dot = el.querySelector('[data-part="dot"]');
      if (dot) dot.hidden = !(s.stored && !sameValue(s.value, s.default));
    }
  });
  if (shared) {
    document.querySelectorAll(attrSelector('data-setting-key', key)).forEach((c) => {
      if (typeof c.sovaSetValue === 'function') c.sovaSetValue(s.value);
    });
  }
  hooks.afterRender(key);
}

/* --- Saving and resetting --------------------------------------------------------------------------------- */

const modelOptions = {};

/* A provider or runtime change changes which models exist: drop that model list and rebuild every model field
   fed from it, so no dropdown keeps offering the old provider's models. A field the user is editing (dirty) is
   left alone, so a typed value is not discarded; it is rebuilt on its next Cancel or save. */
const MODEL_SOURCE_OF_KEY = (key) => (key.indexOf('llm.') === 0 ? 'llm' : (key === 'agent.runtime' ? 'runtime' : ''));

function refreshModelFields(key) {
  const source = MODEL_SOURCE_OF_KEY(key);
  if (!source) return;
  delete modelOptions[source];
  allSettings().forEach((s) => {
    if (s.options_source !== source || s.key === key) return;
    const editing = elementsFor(s.key).some((el) => el.classList && el.classList.contains('sova-setting-dirty'));
    if (!editing) renderKey(s.key);
  });
}

/* The message line after a successful save: a lifecycle error or warning, a restart note, or plain "Saved".
   Shared by text rows and the shared switch and stepper, so no kind of control reports less than another. */
function reportSaved(key, s, result) {
  const restart = (result && result.restartRequired) || (s && s.requires_restart);
  const note = result && (result.lifecycleError || result.warning);
  if (note) setMsg(key, 'Saved. ' + note, 'warn');
  else setMsg(key, restart ? 'Saved. Restart the server to apply it.' : 'Saved', restart ? 'warn' : 'ok');
}

/* Saves one key and re-renders only its rows. On failure the row keeps what the user typed and shows the
   server's message, whatever status it came with. Resolves to saveSetting()'s result. */
export async function saveRow(key, raw) {
  const current = setting(key);
  // An empty secret would delete the stored credential; Reset to default is the deliberate way to remove one.
  if (current && current.value_type === 'secret' && !String(raw).trim()) {
    setMsg(key, 'Not saved. Enter a value, or use Reset to default to remove the stored one.', 'err');
    return { ok: false, message: 'empty secret' };
  }
  setPending(key, true);
  setMsg(key, 'Saving...', 'muted');
  const result = await saveSetting(key, raw);
  setPending(key, false);
  if (!result.ok) {
    setMsg(key, 'Not saved. ' + result.message, 'err');
    return result;
  }
  replacingSecrets.delete(key);
  recordSaved(key, result.value);
  refreshModelFields(key);
  const s = setting(key);
  // A secret may now live in the keychain or in the database: read where, so "Move to keychain" stays right.
  if (s && s.value_type === 'secret') await refreshSecretLocation(s);
  renderKey(key);
  flash(key);
  reportSaved(key, s, result);
  return result;
}

async function refreshSecretLocation(s) {
  try {
    const data = await fetchAPI(apiUrl('/settings/config/grouped'));
    (data.groups || []).forEach((g) => g.settings.forEach((entry) => {
      if (entry.key === s.key) s.secret_location = entry.secret_location;
    }));
  } catch (_e) { /* the save succeeded; the keychain action may be stale until the next load */ }
}

async function postReset(key) {
  let res;
  try {
    res = await fetch(apiUrl('/settings/config/reset'), {
      method: 'POST', headers: jsonCsrfHeaders(), body: JSON.stringify({ key }),
    });
  } catch (_e) {
    return { ok: false, message: 'Could not reach the dashboard to reset ' + key };
  }
  if (!res.ok) return { ok: false, message: await getErrorDetail(res, 'Could not reset ' + key) };
  const body = await res.json().catch(() => ({}));
  return { ok: true, body };
}

/* Deletes the stored value of one key, so its default applies again, and re-renders only its rows. */
export async function resetRow(key) {
  setPending(key, true);
  const result = await postReset(key);
  setPending(key, false);
  if (!result.ok) {
    setMsg(key, 'Not reset. ' + result.message, 'err');
    return result;
  }
  replacingSecrets.delete(key);
  recordReset(key, result.body.value);
  refreshModelFields(key);
  renderKey(key);
  flash(key);
  const note = result.body.warning || result.body.lifecycle_error || (result.body.restart_required ? 'Restart the server to apply it.' : '');
  setMsg(key, 'Reset. The default applies again.' + (note ? ' ' + note : ''), note ? 'warn' : 'ok');
  return result;
}

/* Removes a stored row no setting reads any more. */
export async function removeLeftover(rowEl) {
  const key = rowEl.dataset.leftover;
  const result = await postReset(key);
  if (!result.ok) {
    const msg = rowEl.querySelector('[data-part="msg"]');
    if (msg) { msg.textContent = 'Not removed. ' + result.message; msg.classList.add('sova-setting-msg-err'); }
    return;
  }
  dropLeftover(key);
  rowEl.remove();
  showToast('Removed the stored value of ' + key, 'success');
  hooks.afterRender(key);
}

async function moveSecretToKeychain(key) {
  try {
    const res = await fetch(apiUrl('/settings/config/migrate-secret'), {
      method: 'POST', headers: jsonCsrfHeaders(), body: JSON.stringify({ key }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || data.error) {
      setMsg(key, 'Not moved. ' + (data.detail || data.error || 'Failed to move the secret'), 'err');
      return;
    }
    const s = setting(key);
    if (s) s.secret_location = 'keyring';
    renderKey(key);
    setMsg(key, 'Moved to the OS keychain', 'ok');
  } catch (e) {
    setMsg(key, 'Not moved. ' + e.message, 'err');
  }
}

/* --- Model fields: a grouped dropdown once the model list is known ---------------------------------------- */

function fetchModelOptions(source) {
  if (!modelOptions[source]) {
    modelOptions[source] = fetchAPI(apiUrl('/models/available?layer=' + encodeURIComponent(source)))
      .then((data) => {
        const byFamily = groupModelsByFamily(data.models);
        if (!Object.keys(byFamily).length) throw new Error('no models enumerated');
        // A "curated" answer is the server's fallback while the provider is unreachable, not this deployment's
        // models: offering it would show the wrong provider's models, so the field stays a text box.
        if (data.source === 'curated') throw new Error('curated fallback');
        return byFamily;
      })
      .catch(() => { delete modelOptions[source]; return null; });
  }
  return modelOptions[source];
}

function modelSelect(s, byFamily) {
  const select = document.createElement('select');
  select.className = 'sova-input';
  select.dataset.act = 'model-select';
  select.setAttribute('aria-label', s.label);
  const current = s.value ? String(s.value) : '';
  const none = document.createElement('option');
  none.value = '';
  none.textContent = current ? 'Not set (clear)' : 'Not set';
  if (!current) none.selected = true;
  select.appendChild(none);
  const matched = appendModelOptgroups(select, byFamily, current);
  if (current && !matched) {
    const group = document.createElement('optgroup');
    group.label = 'Stored value';
    const opt = document.createElement('option');
    opt.value = current;
    opt.textContent = current;
    opt.selected = true;
    group.appendChild(opt);
    select.appendChild(group);
  }
  const custom = document.createElement('option');
  custom.value = CUSTOM_MODEL;
  custom.textContent = 'Custom model ID...';
  select.appendChild(custom);
  return select;
}

/* A single model field starts as a text box, always usable, and becomes a grouped dropdown once the model list
   arrives; a list of models (fallbacks) keeps its text box and gains an ordered list with an Add picker. A
   failed or curated model list leaves the text box. */
function upgradeModelControl(row) {
  const s = setting(row.dataset.key);
  if (!s || s.env_var || !isModelField(s)) return;
  const control = row.querySelector('[data-part="control"]');
  if (!control) return;
  if (s.value_type === 'list') renderFallbackOrder(row);
  fetchModelOptions(s.options_source).then((byFamily) => {
    if (!byFamily || !row.isConnected || row.classList.contains('sova-setting-dirty')) return;
    if (s.value_type === 'list') {
      addFallbackPicker(row, byFamily);
      return;
    }
    const input = control.querySelector('[data-act="text"]');
    if (!input) return;
    const select = modelSelect(s, byFamily);
    input.replaceWith(select);
    const btns = control.querySelector('.sova-setting-edit-btns');
    if (btns) btns.remove();
    // A custom ID is typed in a text box that appears below the dropdown.
    control.appendChild(document.createRange().createContextualFragment(
      '<div class="sova-setting-custom" hidden><input type="text" class="sova-input" data-act="text" placeholder="Model ID, e.g. ollama/qwen3:32b" ' +
      'aria-label="' + attr('Custom model ID for ' + s.label) + '" spellcheck="false" autocomplete="off">' + editButtons() + '</div>'));
  });
}

/* Mirrors the server's _cast_list(): a JSON array, else comma-separated entries. */
function parseList(text) {
  const raw = String(text || '').trim();
  if (raw.charAt(0) === '[') {
    try {
      const parsed = JSON.parse(raw);
      if (Array.isArray(parsed)) return parsed.map((x) => String(x).trim()).filter(Boolean);
    } catch (_e) { /* not JSON: split on commas */ }
  }
  return raw.split(',').map((x) => x.trim()).filter(Boolean);
}

/* The fallback list, tried top to bottom, with Up, Down and Remove per entry. Each action rewrites the text
   box, which is what Save sends. */
function renderFallbackOrder(row) {
  const control = row.querySelector('[data-part="control"]');
  const input = control && control.querySelector('[data-act="text"]');
  if (!input) return;
  let list = control.querySelector('.sova-setting-order');
  if (!list) {
    list = document.createElement('ol');
    list.className = 'sova-setting-order';
    list.setAttribute('aria-label', 'Order tried, top to bottom');
    control.appendChild(list);
  }
  const parts = parseList(input.value);
  list.innerHTML = parts.map((model, i) => `<li><span class="sova-setting-order-name">${escapeHtml(i + 1)}. ${escapeHtml(model)}</span>
    <button type="button" class="sova-btn" data-act="order-up" data-index="${escapeHtml(i)}"${i === 0 ? ' disabled' : ''} aria-label="${escapeHtml('Move up ' + model)}">Up</button>
    <button type="button" class="sova-btn" data-act="order-down" data-index="${escapeHtml(i)}"${i === parts.length - 1 ? ' disabled' : ''} aria-label="${escapeHtml('Move down ' + model)}">Down</button>
    <button type="button" class="sova-btn" data-act="order-remove" data-index="${escapeHtml(i)}" aria-label="${escapeHtml('Remove ' + model)}">Remove</button></li>`).join('');
}

function addFallbackPicker(row, byFamily) {
  const control = row.querySelector('[data-part="control"]');
  if (!control || control.querySelector('[data-act="order-pick"]')) return;
  const wrap = document.createElement('div');
  wrap.className = 'sova-setting-picker';
  const select = document.createElement('select');
  select.className = 'sova-input';
  select.dataset.act = 'order-pick';
  select.setAttribute('aria-label', 'Model to add');
  const first = document.createElement('option');
  first.value = '';
  first.textContent = 'Add a model...';
  select.appendChild(first);
  appendModelOptgroups(select, byFamily);
  const add = document.createElement('button');
  add.type = 'button';
  add.className = 'sova-btn sova-btn-primary';
  add.dataset.act = 'order-add';
  add.textContent = 'Add';
  wrap.appendChild(select);
  wrap.appendChild(add);
  control.appendChild(wrap);
}

function changeOrder(row, act, index) {
  const input = row.querySelector('[data-part="control"] [data-act="text"]');
  if (!input) return;
  const parts = parseList(input.value);
  if (act === 'order-up' && index > 0) parts.splice(index - 1, 0, parts.splice(index, 1)[0]);
  else if (act === 'order-down' && index < parts.length - 1) parts.splice(index + 1, 0, parts.splice(index, 1)[0]);
  else if (act === 'order-remove') parts.splice(index, 1);
  else if (act === 'order-add') {
    const pick = row.querySelector('[data-act="order-pick"]');
    if (!pick || !pick.value) return;
    if (parts.indexOf(pick.value) === -1) parts.push(pick.value);
    pick.value = '';
  }
  input.value = parts.some((p) => p.indexOf(',') !== -1) ? JSON.stringify(parts) : parts.join(', ');
  row.classList.add('sova-setting-dirty');
  renderFallbackOrder(row);
}

/* --- Events, delegated from the page ---------------------------------------------------------------------- */

function rowOf(el) { return el.closest('[data-key]'); }

function textOf(row) {
  const custom = row.querySelector('.sova-setting-custom:not([hidden]) [data-act="text"]');
  const input = custom || row.querySelector('[data-part="control"] [data-act="text"]');
  return input ? input.value : '';
}

function cancelRow(row) {
  const key = row.dataset.key;
  replacingSecrets.delete(key);
  const s = setting(key);
  if (s) renderRow(row, s, true);
  setMsg(key, '', null);
}

/* Handles a click inside a row; returns true when it was one. */
export function handleClick(target) {
  const btn = target.closest('[data-act]');
  if (!btn) return false;
  const leftoverRow = btn.closest('[data-leftover]');
  if (leftoverRow && btn.dataset.act === 'drop-leftover') { removeLeftover(leftoverRow); return true; }
  const row = rowOf(btn);
  if (!row) return false;
  const key = row.dataset.key;
  const act = btn.dataset.act;
  if (act === 'save') saveRow(key, textOf(row));
  else if (act === 'cancel') cancelRow(row);
  else if (act === 'reset') resetRow(key);
  else if (act === 'secret-replace') {
    replacingSecrets.add(key);
    renderKey(key);
    const input = row.querySelector('[data-act="text"]');
    if (input) input.focus();
  } else if (act === 'secret-keychain') moveSecretToKeychain(key);
  else if (act.indexOf('order-') === 0 && act !== 'order-pick') changeOrder(row, act, Number(btn.dataset.index));
  else return false;
  return true;
}

export function handleChange(target) {
  const act = target.dataset && target.dataset.act;
  const row = act && rowOf(target);
  if (!row) return false;
  if (act === 'select') { saveRow(row.dataset.key, target.value); return true; }
  if (act === 'model-select') {
    const custom = row.querySelector('.sova-setting-custom');
    if (target.value === CUSTOM_MODEL) {
      if (custom) {
        custom.hidden = false;
        row.classList.add('sova-setting-dirty');
        const input = custom.querySelector('input');
        if (input) input.focus();
      }
      return true;
    }
    if (custom) custom.hidden = true;
    saveRow(row.dataset.key, target.value);
    return true;
  }
  return false;
}

export function handleInput(target) {
  if (!target.dataset || target.dataset.act !== 'text') return;
  const row = rowOf(target);
  if (!row) return;
  row.classList.add('sova-setting-dirty');
  const s = setting(row.dataset.key);
  if (s && s.value_type === 'list' && isModelField(s)) renderFallbackOrder(row);
}

export function handleKeydown(e) {
  const target = e.target;
  if (!target.dataset || target.dataset.act !== 'text') return;
  const row = rowOf(target);
  if (!row) return;
  if (e.key === 'Enter') { e.preventDefault(); saveRow(row.dataset.key, target.value); }
  else if (e.key === 'Escape') { e.preventDefault(); cancelRow(row); }
}
