import { statePill } from './board.js';
import { boardColumns, showOffBoard } from './drawer.js';
import { select } from './selection.js';

// The issue search above the board: any open issue, on the board or not, by its number or words of its title
// (GET /api/work/search, which reads the dependency graph the board uses). Choosing a result opens the issue's
// details in the drawer and scrolls its card into view. An issue the board does not show opens with the search's
// entry, whose `start` action is the run its stage calls for, so no open issue is out of reach of the page.
//
// Keys: "/" focuses the box, the arrows move through the results, Enter opens one, Escape closes the list.

var _DEBOUNCE_MS = 200;

// The delay before a typed query is searched, for a test to wait on rather than a wall-clock guess.
export function debounceMs() { return _DEBOUNCE_MS; }

var _timer = null;
var _request = 0;     // the latest search's number: a slower, older answer must not replace a newer one
var _results = [];
var _active = -1;     // the highlighted result, -1 for none

function _input() { return document.getElementById('work-search-input'); }
function _list() { return document.getElementById('work-search-results'); }

function _setOpen(open) {
  var input = _input();
  var list = _list();
  if (!input || !list) return;
  list.hidden = !open;
  input.setAttribute('aria-expanded', String(open));
  if (!open) _setActive(-1);
}

function _setActive(i) {
  _active = i;
  var input = _input();
  var list = _list();
  if (!input || !list) return;
  list.querySelectorAll('[data-search-index]').forEach(function(row) {
    var on = Number(row.getAttribute('data-search-index')) === i;
    row.setAttribute('aria-selected', String(on));
    if (on) row.scrollIntoView({ block: 'nearest' });
  });
  if (i >= 0) input.setAttribute('aria-activedescendant', 'work-search-opt-' + i);
  else input.removeAttribute('aria-activedescendant');
}

// Where the issue is: its column on the board, or its state when the board does not show it.
function _where(entry, columns) {
  var col = columns(entry.number);
  if (!col) return statePill(entry) + '<span class="text-xs text-gray-500 whitespace-nowrap">not on the board</span>';
  return '<span class="px-1.5 rounded-full text-xs whitespace-nowrap ' + stateBadgeClasses(col.color) + '">' + escapeHtml(col.label) + '</span>';
}

function _row(entry, i, columns) {
  return '<button type="button" id="work-search-opt-' + i + '" class="sova-work-search-row" role="option" tabindex="-1"' +
    ' aria-selected="' + (i === _active) + '" data-search-index="' + i + '">' +
    '<span class="text-xs font-bold text-accent tabular-nums shrink-0">#' + entry.number + '</span>' +
    '<span class="text-sm text-gray-200 truncate flex-1 min-w-0">' + escapeHtml(entry.title || '') + '</span>' +
    '<span class="flex items-center gap-1 shrink-0">' + _where(entry, columns) + '</span></button>';
}

function _note(text, cls) {
  return '<p class="px-2 py-1.5 text-xs ' + (cls || 'text-gray-500') + '">' + escapeHtml(text) + '</p>';
}

function _render(query, more) {
  var list = _list();
  if (!list) return;
  // One board index for the whole list, not one per row.
  var columns = boardColumns();
  list.innerHTML = _results.length
    ? _results.map(function(entry, i) { return _row(entry, i, columns); }).join('') + (more ? _note(more + ' more match. Add words to narrow it down.') : '')
    : _note('No open issue matches "' + query + '".');
  _setOpen(true);
  _setActive(_results.length ? 0 : -1);
}

async function _search(query) {
  var request = ++_request;
  try {
    var data = await fetchAPI(apiUrl('/work/search?q=' + encodeURIComponent(query)));
    if (request !== _request) return;
    _results = data.results || [];
    _render(query, data.more || 0);
  } catch (e) {
    if (request !== _request) return;
    _results = [];
    var list = _list();
    if (list) list.innerHTML = _note('Could not search the issues: ' + ((e && e.message) || 'unknown error') + '.', 'text-accent-red');
    _setOpen(true);
  }
}

function _clear() {
  clearTimeout(_timer);
  _request++;
  _results = [];
  var input = _input();
  if (input) input.value = '';
  _setOpen(false);
}

function _choose(i) {
  var entry = _results[i];
  if (!entry) return;
  showOffBoard(entry);
  select(entry.number);
  _clear();
  var card = document.querySelector('#work-board [data-action="work-select"][data-args="[' + Number(entry.number) + ']"]');
  if (card) card.scrollIntoView({ block: 'nearest', inline: 'nearest', behavior: 'smooth' });
}

function _onInput() {
  clearTimeout(_timer);
  // Every edit retires the answers to the earlier text: their results are dropped (so Enter cannot open one) and a
  // reply still in flight is refused, until the new text's own search answers.
  _request++;
  _results = [];
  _setOpen(false);
  var query = _input().value.trim();
  if (!query.replace(/^#/, '')) return;
  _timer = setTimeout(function() { _search(query); }, _DEBOUNCE_MS);
}

function _onKeydown(e) {
  var open = !_list().hidden;
  if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
    if (!open || !_results.length) return;
    e.preventDefault();
    var step = e.key === 'ArrowDown' ? 1 : -1;
    _setActive((_active + step + _results.length) % _results.length);
  } else if (e.key === 'Enter') {
    if (!open || _active < 0) return;
    e.preventDefault();
    _choose(_active);
  } else if (e.key === 'Escape') {
    // The list closes first; the next Escape reaches the page and closes the drawer.
    if (!open && !_input().value) return;
    e.stopPropagation();
    _clear();
  }
}

function _typingIn(el) {
  return el && (el.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName));
}

export function init() {
  var input = _input();
  var list = _list();
  if (!input || !list) return;
  input.addEventListener('input', _onInput);
  input.addEventListener('keydown', _onKeydown);
  input.addEventListener('focus', function() { if (_results.length) _setOpen(true); });
  // A press on a result keeps the focus in the box, so the list does not close before the click lands.
  list.addEventListener('mousedown', function(e) { e.preventDefault(); });
  list.addEventListener('click', function(e) {
    var row = e.target.closest && e.target.closest('[data-search-index]');
    if (row) _choose(Number(row.getAttribute('data-search-index')));
  });
  document.addEventListener('click', function(e) {
    var host = document.getElementById('work-search');
    if (host && !list.hidden && !host.contains(e.target)) _setOpen(false);
  });
  document.addEventListener('keydown', function(e) {
    if (e.key !== '/' || e.ctrlKey || e.metaKey || e.altKey || _typingIn(document.activeElement)) return;
    e.preventDefault();
    input.focus();
    input.select();
  });
}
