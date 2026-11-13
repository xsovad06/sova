/* The Settings page's data: every setting from GET /settings/config/grouped, the section list, and what the
   page shows (a section, a search, or the changed-only list). Pure: no DOM access, so the search and the
   counts are testable on their own. */

let groups = [];
const byKey = new Map();
let leftover = [];
let keyringAvailable = false;
let storedUnreadable = false;
let sections = [];
const sectionOfGroup = new Map();

// What the page shows: the selected section, the search text, and the changed-only filter.
export const view = { section: 'overview', query: '', changedOnly: false };

export function setSections(list) {
  sections = list;
  sectionOfGroup.clear();
  list.forEach((sec) => sec.groups.forEach((gid) => sectionOfGroup.set(gid, sec)));
}

export function getSections() { return sections; }
export function sectionById(id) { return sections.find((s) => s.id === id) || null; }
export function sectionForGroup(gid) { return sectionOfGroup.get(gid) || null; }

export function setPayload(payload) {
  groups = payload.groups || [];
  leftover = payload.leftover || [];
  keyringAvailable = !!payload.keyring_available;
  storedUnreadable = !!payload.stored_unreadable;
  if (storedUnreadable) view.changedOnly = false;
  byKey.clear();
  groups.forEach((g) => g.settings.forEach((s) => byKey.set(s.key, s)));
}

export function setting(key) { return byKey.get(key) || null; }
export function allSettings() { return Array.from(byKey.values()); }
/* The stored rows could not be read, so no setting's "stored" flag can be trusted. */
export function isStoredUnreadable() { return storedUnreadable; }
export function groupById(gid) { return groups.find((g) => g.id === gid) || null; }
export function getLeftover() { return leftover; }
export function dropLeftover(key) { leftover = leftover.filter((row) => row.key !== key); }
export function isKeyringAvailable() { return keyringAvailable; }

/* A setting is listed under "Changed only" when a value is stored for this project. An environment override has
   its own pill and is not a stored row. When the stored rows could not be read, no setting counts as changed. */
export function isChanged(s) { return !storedUnreadable && !!s.stored; }

/* Records a save: the value as stored, which is now a stored row. */
export function recordSaved(key, value) {
  const s = byKey.get(key);
  if (!s) return;
  s.value = value;
  s.stored = true;
  if (s.value_type === 'object') s.stored_rows = Math.max(1, s.stored_rows || 0);
}

/* Records a reset: the value now in effect, and no stored row. */
export function recordReset(key, value) {
  const s = byKey.get(key);
  if (!s) return;
  s.value = value;
  s.stored = false;
  if (s.value_type === 'object') s.stored_rows = 0;
}

/* JSON with object keys sorted, so two objects with the same content compare equal whatever their key order. */
function canonical(value) {
  if (Array.isArray(value)) return value.map(canonical);
  if (value && typeof value === 'object') {
    const out = {};
    Object.keys(value).sort().forEach((k) => { out[k] = canonical(value[k]); });
    return out;
  }
  return value;
}

export function sameValue(a, b) { return JSON.stringify(canonical(a)) === JSON.stringify(canonical(b)); }

export function tokens(query) {
  return String(query || '').trim().toLowerCase().split(/\s+/).filter(Boolean);
}

/* Every search word must appear in the setting's label, key or description. */
export function matches(s, toks) {
  if (!toks.length) return true;
  const hay = ((s.label || '') + ' ' + (s.key || '') + ' ' + (s.description || '')).toLowerCase();
  return toks.every((t) => hay.includes(t));
}

// Runtimes whose AgentRuntime.honours_model_override is False: a model passed to them is dropped, so a
// runtime-model pin has nothing to choose (tests/test_settings_template.py keeps this in step with the runtimes).
const NO_MODEL_CHOICE_RUNTIMES = { codex: true };

/* Keys whose rows are hidden because they do not apply to the active provider or runtime. The stored value is
   kept; the row comes back when the provider or runtime changes. */
export function hiddenKeys() {
  const provider = (setting('llm.provider') || {}).value || 'claude-code';
  const runtime = (setting('agent.runtime') || {}).value || 'claude-code';
  const hidden = new Set();
  if (provider !== 'anthropic') hidden.add('llm.api_key');
  if (runtime !== 'codex') { hidden.add('codex.model'); hidden.add('codex.sandbox'); }
  if (NO_MODEL_CHOICE_RUNTIMES[runtime]) hidden.add('roles.developer_runtime_model');
  return hidden;
}

export function filtering() { return !!view.query.trim() || view.changedOnly; }

/* The settings the current search and changed-only filter keep, in one group. */
export function visibleIn(group) {
  const toks = tokens(view.query);
  return group.settings.filter((s) => matches(s, toks) && (!view.changedOnly || isChanged(s)));
}

/* What search and Changed only list in one group: the settings the filter keeps, minus the ones that do not apply
   to the active provider or runtime. A stored one stays listed (flagged inactive), so Changed only lists every
   stored setting and its Reset stays reachable. */
export function resultsIn(group) {
  const hidden = hiddenKeys();
  return visibleIn(group)
    .filter((s) => !hidden.has(s.key) || s.stored)
    .map((s) => ({ setting: s, inactive: hidden.has(s.key) }));
}

export function totals() {
  let settings = 0;
  let stored = 0;
  let env = 0;
  groups.forEach((g) => g.settings.forEach((s) => {
    settings += 1;
    if (isChanged(s)) stored += 1;
    else if (s.env_var) env += 1;
  }));
  return { settings, groups: groups.length, stored, env, changed: stored };
}

/* Settings in a section, or (while filtering) the ones the filter keeps. */
export function sectionCount(sec) {
  return sec.groups.reduce((n, gid) => {
    const g = groupById(gid);
    if (!g) return n;
    return n + (filtering() ? visibleIn(g).length : g.settings.length);
  }, 0);
}

export function sectionChangedCount(sec) {
  return sec.groups.reduce((n, gid) => {
    const g = groupById(gid);
    return g ? n + g.settings.filter(isChanged).length : n;
  }, 0);
}
