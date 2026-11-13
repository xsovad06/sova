/* The Settings page (#1463): a section list on the left and one section on the right, search across every setting,
   a changed-only list with Reset to default, and an Overview of the everyday settings.

   Sections and their groups are rendered by settings.html from settings_meta.SECTIONS; this module fills each
   group with its rows from GET /settings/config/grouped, shows one section at a time, and follows the URL hash:
   `#<section>` opens a section and `#<group id>` (e.g. #supervisor, #task_source) opens the section holding that
   group and scrolls to it, and `#models` opens Models and agents at the Models view (models.js). */

import * as state from './state.js';
import * as rows from './rows.js';
import * as widgets from './widgets.js';
import * as models from './models.js';

const $ = (id) => document.getElementById(id);

/* --- Settings that only apply to one provider or runtime -------------------------------------------------- */

function applyVisibility() {
  const hidden = state.hiddenKeys();
  document.querySelectorAll('[data-ctx="row"][data-key]').forEach((el) => { el.hidden = hidden.has(el.dataset.key); });
}

/* --- Counts ------------------------------------------------------------------------------------------------- */

function updateCounts() {
  const t = state.totals();
  const unknown = state.isStoredUnreadable();
  $('settings-subtitle').textContent = t.settings + ' settings in ' + t.groups + ' groups. ' +
    (unknown ? 'Stored values could not be read.' : t.stored + ' changed for this project' + (t.env ? ', ' + t.env + ' set by the environment.' : '.'));
  $('settings-changed-count').textContent = unknown ? '?' : String(t.changed);
  $('settings-changed').disabled = unknown;
  const filtering = state.filtering();
  state.getSections().forEach((sec) => {
    if (sec.id === 'overview') return;
    const count = document.querySelector('[data-section-count="' + sec.id + '"]');
    if (count) {
      const n = state.sectionCount(sec);
      count.textContent = String(n);
      count.classList.toggle('sova-settings-nav-hit', filtering && n > 0);
    }
    const summary = document.querySelector('#section-' + sec.id + ' [data-part="section-summary"]');
    if (summary) {
      const n = sec.groups.reduce((acc, gid) => acc + ((state.groupById(gid) || { settings: [] }).settings.length), 0);
      summary.textContent = sec.groups.length + ' groups, ' + n + ' settings' +
        (unknown ? '' : ', ' + state.sectionChangedCount(sec) + ' changed for this project');
    }
  });
}

function updateSupervisorCard() {
  const s = state.setting('supervisor.enabled');
  const card = $('overview-supervisor');
  if (!s || !card) return;
  card.classList.toggle('sova-overview-off', !s.value);
  const label = $('overview-supervisor-state');
  label.textContent = s.value ? 'On' : 'Off';
  label.className = 'sova-overview-state ' + (s.value ? 'sova-overview-state-on' : '');
  const ovs = state.setting('oversight.enabled');
  const ovsLabel = $('overview-oversight-state');
  if (ovs && ovsLabel) {
    ovsLabel.textContent = ovs.value ? 'On' : 'Off';
    ovsLabel.className = 'sova-overview-state ' + (ovs.value ? 'sova-overview-state-on' : '');
  }
}

function afterKeyRendered(key) {
  updateCounts();
  if (models.isLoaded() && models.affects(key)) models.scheduleRefresh();
  if (key === 'llm.provider' || key === 'agent.runtime') applyVisibility();
  if (key === 'supervisor.enabled' || key === 'oversight.enabled') updateSupervisorCard();
}

/* --- Rendering ---------------------------------------------------------------------------------------------- */

function renderGroups() {
  const hidden = state.hiddenKeys();
  document.querySelectorAll('[data-group]').forEach((groupEl) => {
    const g = state.groupById(groupEl.dataset.group);
    const target = groupEl.querySelector('[data-part="group-rows"]');
    const count = groupEl.querySelector('[data-part="group-count"]');
    if (!g) {
      target.innerHTML = '<p class="sova-settings-note">No settings in this group.</p>';
      if (count) count.textContent = '';
      return;
    }
    if (count) count.textContent = g.settings.length + (g.settings.length === 1 ? ' setting' : ' settings');
    target.innerHTML = g.settings.map((s) => rows.rowHtml(s, { hidden: hidden.has(s.key) })).join('');
    rows.mountControls(target);
  });
}

function renderOverview() {
  const overview = $('section-overview');
  overview.querySelectorAll('[data-ctx="overview"][data-key]').forEach((el) => {
    const s = state.setting(el.dataset.key);
    if (!s) return;
    const dot = el.querySelector('[data-part="dot"]');
    if (dot) dot.hidden = !(s.stored && !state.sameValue(s.value, s.default));
    // A value set by the environment cannot be changed here: show it instead of a control.
    if (s.env_var) {
      const host = el.querySelector('[data-setting-switch], [data-setting-stepper]');
      if (host) host.outerHTML = '<span class="sova-setting-readonly" data-tooltip="' + escapeHtml(s.env_var + ' sets this value') + '">' + escapeHtml(rows.formatValue(s.value, s)) + '</span>';
    }
  });
  rows.mountControls(overview);
  updateSupervisorCard();
}

function leftoverHtml() {
  const list = state.getLeftover();
  if (!list.length) return '';
  return '<div class="sova-settings-group"><div class="sova-settings-group-head"><h4>Stored values no setting reads</h4>' +
    '<span class="sova-settings-group-count">' + list.length + '</span></div>' +
    list.map((row) => '<div class="sova-setting-row" data-leftover="' + escapeHtml(row.key) + '">' +
      '<div class="sova-setting-info"><div class="sova-setting-key sova-setting-key-strong">' + escapeHtml(row.key) + '</div>' +
      '<div class="sova-setting-desc">' + (typeof row.value === 'string' ? 'A stored value (hidden)' : 'Stored as <code>' + escapeHtml(JSON.stringify(row.value)) + '</code>') + '. No setting uses this key any more, so it has no effect.</div></div>' +
      '<div class="sova-setting-side"><div class="sova-setting-control"><button type="button" class="sova-btn sova-btn-danger" data-act="drop-leftover">Remove stored value</button></div>' +
      '<div class="sova-setting-msg" data-part="msg" role="status"></div></div></div>').join('') +
    '</div>';
}

/* The search and changed-only results: matching settings from every section, under their section and group. */
function renderResults() {
  const results = $('settings-results');
  const toks = state.tokens(state.view.query);
  let total = 0;
  const blocks = state.getSections().map((sec) => {
    const parts = sec.groups.map((gid) => {
      const g = state.groupById(gid);
      if (!g) return '';
      const kept = state.resultsIn(g);
      if (!kept.length) return '';
      total += kept.length;
      return '<div class="sova-settings-group"><div class="sova-settings-group-head"><h4>' + escapeHtml(g.label) + '</h4>' +
        '<span class="sova-settings-group-count">' + kept.length + (kept.length === 1 ? ' setting' : ' settings') + '</span>' +
        '<a class="sova-settings-anchor" href="#' + escapeHtml(g.id) + '">Open in ' + escapeHtml(sec.label) + '</a></div>' +
        kept.map((k) => rows.rowHtml(k.setting, { toks, inactive: k.inactive })).join('') + '</div>';
    }).join('');
    return parts ? '<div id="results-' + sec.id + '"><div class="sova-settings-crumb">' + escapeHtml(sec.label) + '</div>' + parts + '</div>' : '';
  }).join('');

  const query = state.view.query.trim();
  const t = state.totals();
  let head;
  let sub;
  if (state.view.changedOnly) {
    head = query ? total + ' changed setting' + (total === 1 ? '' : 's') + ' match "' + query + '"'
      : t.stored + ' setting' + (t.stored === 1 ? '' : 's') + ' stored for this project' + (t.env ? ', ' + t.env + ' set by the environment' : '');
    sub = 'Reset to default deletes the stored value, so the default applies again. A value set by an environment variable has no Reset.';
  } else {
    head = total + (total === 1 ? ' setting matches' : ' settings match') + ' "' + query + '"';
    sub = 'Searching names, keys and descriptions in every section.';
  }
  const empty = '<div class="sova-settings-empty"><p class="sova-settings-empty-title">' +
    (state.view.changedOnly && !query ? 'Nothing is stored for this project' : 'No setting matches "' + escapeHtml(query) + '"') + '</p>' +
    '<p class="sova-settings-note">' + (state.view.changedOnly ? 'Every setting uses its default.' : 'Search looks at names, keys and descriptions.') + '</p></div>';
  results.innerHTML = '<header class="sova-settings-section-head"><h3>' + escapeHtml(head) + '</h3><p>' + escapeHtml(sub) + '</p></header>' +
    (blocks || empty) + (state.view.changedOnly && !query ? leftoverHtml() : '');
  rows.mountControls(results);
}

/* --- Showing a section, the results, and the hash --------------------------------------------------------- */

function showSection(id) {
  state.view.section = id;
  document.querySelectorAll('[data-section]').forEach((el) => { el.hidden = el.dataset.section !== id; });
  $('settings-results').hidden = true;
  const sec = $('section-' + id);
  if (sec) {
    widgets.loadWidgets(sec);
    if (sec.querySelector('[data-part="models-view"]') && !models.isLoaded()) models.load();
  }
  updateNav();
}

function updateNav() {
  const filtering = state.filtering();
  document.querySelectorAll('[data-section-link]').forEach((link) => {
    const on = !filtering && link.dataset.sectionLink === state.view.section;
    link.classList.toggle('sova-settings-nav-on', on);
    if (on) link.setAttribute('aria-current', 'page');
    else link.removeAttribute('aria-current');
  });
  document.querySelectorAll('[data-section-sub]').forEach((sub) => {
    sub.hidden = filtering || sub.dataset.sectionSub !== state.view.section;
  });
  $('settings-search-clear').hidden = !state.view.query;
  $('settings-changed').setAttribute('aria-pressed', String(state.view.changedOnly));
  $('settings-nav-hint').textContent = filtering
    ? 'Counts show matches. Pick a section to jump to its results.'
    : 'Every group sits in one section. Overview repeats the settings you use most.';
}

function showResults() {
  document.querySelectorAll('[data-section]').forEach((el) => { el.hidden = true; });
  $('settings-results').hidden = false;
  renderResults();
  updateNav();
  updateCounts();
}

function refreshView() {
  if (state.filtering()) showResults();
  else {
    $('settings-results').innerHTML = '';
    showSection(state.view.section);
    updateCounts();
  }
}

function scrollToEl(el) {
  if (el) el.scrollIntoView({ block: 'start' });
}

/* Opens what the hash names: a section, or the section holding a group and that group. */
function route() {
  let hash = location.hash.slice(1);
  try { hash = decodeURIComponent(hash); } catch (_e) { /* a malformed escape (#100%): use the raw text */ }
  const clearFilters = () => {
    state.view.query = '';
    state.view.changedOnly = false;
    $('settings-search').value = '';
  };
  if (!hash) { clearFilters(); showSection('overview'); updateCounts(); return; }
  const sec = state.sectionById(hash);
  if (sec) { clearFilters(); showSection(sec.id); updateCounts(); window.scrollTo(0, 0); return; }
  const gid = state.sectionForGroup(hash) ? hash : (state.sectionForGroup(hash.replace(/-/g, '_')) ? hash.replace(/-/g, '_') : null);
  if (gid) {
    clearFilters();
    showSection(state.sectionForGroup(gid).id);
    updateCounts();
    scrollToEl($('group-' + gid));
    return;
  }
  // A view that is not a settings group (#models) sits in a section of its own choosing.
  const view = $('group-' + hash);
  const viewSection = view && view.closest('[data-section]');
  if (viewSection) {
    clearFilters();
    showSection(viewSection.dataset.section);
    updateCounts();
    scrollToEl(view);
    return;
  }
  if (!state.filtering()) showSection(state.view.section);
}

function go(hash) {
  // Following the same hash again (a second click on the current section) fires no hashchange.
  if (location.hash === '#' + hash) route();
  else location.hash = hash;
}

/* --- Events ------------------------------------------------------------------------------------------------- */

function onClick(e) {
  // Only the page's own links: the sidebar and banners have href="#" links of their own.
  const link = e.target.closest('[data-section-link], [data-group-link], a[href^="#"]');
  if (link && link.closest('.sova-settings-layout')) {
    const target = link.dataset.sectionLink || link.dataset.groupLink || link.getAttribute('href').slice(1);
    if (link.dataset.sectionLink && state.filtering()) {
      // While filtering, a section jumps to its part of the results.
      e.preventDefault();
      const block = $('results-' + target);
      if (block) { scrollToEl(block); return; }
      // No results there (or Overview): leave the search and open the section.
      go(target);
      return;
    }
    e.preventDefault();
    go(target);
    return;
  }
  if (widgets.handleClick(e.target)) return;
  if (models.handleClick(e.target)) return;
  rows.handleClick(e.target);
}

let searchTimer = null;

function onSearchInput(e) {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => { state.view.query = e.target.value; refreshView(); }, 120);
}

function onKeydown(e) {
  const t = e.target;
  const typing = /^(INPUT|SELECT|TEXTAREA)$/.test(t.tagName);
  if (e.key === '/' && !typing && !e.metaKey && !e.ctrlKey) {
    e.preventDefault();
    $('settings-search').focus();
    return;
  }
  if (t.id === 'settings-search' && e.key === 'Escape') {
    clearTimeout(searchTimer);
    t.value = '';
    state.view.query = '';
    refreshView();
    return;
  }
  if (models.handleKeydown(e)) return;
  rows.handleKeydown(e);
}

function bindEvents() {
  document.addEventListener('click', onClick);
  document.addEventListener('change', (e) => { if (!models.handleChange(e.target)) rows.handleChange(e.target); });
  document.addEventListener('input', (e) => { if (e.target.id !== 'settings-search') rows.handleInput(e.target); });
  document.addEventListener('keydown', onKeydown);
  $('settings-search').addEventListener('input', onSearchInput);
  $('settings-search-clear').addEventListener('click', () => {
    clearTimeout(searchTimer);
    $('settings-search').value = '';
    state.view.query = '';
    refreshView();
    $('settings-search').focus();
  });
  $('settings-changed').addEventListener('click', () => {
    state.view.changedOnly = !state.view.changedOnly;
    refreshView();
    window.scrollTo(0, 0);
  });
  window.addEventListener('hashchange', route);
}

/* --- Loading ------------------------------------------------------------------------------------------------ */

function showLoadError(detail, title) {
  const box = $('settings-error');
  const invalid = String(detail || '').indexOf('Invalid configuration:') === 0;
  box.innerHTML = '<p class="sova-settings-error-title">' + escapeHtml(title || (invalid ? 'Configuration error' : 'Failed to load the settings')) + '</p>' +
    '<p class="font-mono text-xs">' + escapeHtml(detail || '') + '</p>' +
    (invalid ? '<p class="sova-settings-note">Fix the value with <code>sova config set</code> (or the matching SOVA_ variable), then reload.</p>' : '');
  box.hidden = false;
}

async function load() {
  let payload;
  try {
    const res = await fetch(apiUrl('/settings/config/grouped'));
    if (!res.ok) throw new Error(await getErrorDetail(res, 'Failed to fetch configuration'));
    payload = await res.json();
  } catch (e) {
    console.error('Failed to load config:', e);
    showLoadError(e.message);
    return false;
  }
  if (payload.error) showLoadError(payload.error);
  else if (payload.stored_unreadable) {
    showLoadError('"Changed only" and the changed counts are unknown, and no setting offers Reset. Check the project '
      + 'database, then reload.', 'The values stored for this project could not be read');
  }
  state.setPayload(payload);
  renderGroups();
  renderOverview();
  return true;
}

export async function init() {
  const sectionsData = $('settings-sections-data');
  state.setSections(JSON.parse(sectionsData.textContent));
  rows.onKeyRendered(afterKeyRendered);
  widgets.init();
  bindEvents();
  route();
  updateNav();
  await load();
  updateCounts();
  route();
}
