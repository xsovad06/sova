// The selected issue and the issues related to it, for the highlight on every card and chain node.
//
// Selecting an issue marks its open blockers (everything it waits for, through any number of steps) and its
// dependents (everything waiting for it), and dims the rest. The relations come from the board payload: every
// entry's depends_on and dependents, and each chain issue's depends_on. A second click on the selected card clears the
// selection, and so does every way the drawer closes (drawer.js, on the shared side panel, which owns Escape).

var _selected = null;
var _up = new Set();
var _down = new Set();
var _listeners = [];
var _board = null;

export function getSelected() { return _selected; }

// What a card or the drawer selects: the issue number, or 'pr:N' for a PR with no issue (it has no relations, so
// selecting it highlights only its own card). null for an entry with neither.
export function selectionKey(entry) {
  if (!entry) return null;
  if (entry.number != null) return Number(entry.number);
  var pr = entry.pr_number || (entry.pr && entry.pr.number);
  return pr ? 'pr:' + pr : null;
}

// A key as select() stores it: a number for an issue, the 'pr:N' string for a PR with no issue.
function _normalize(key) {
  if (key == null) return null;
  return typeof key === 'string' && key.indexOf('pr:') === 0 ? key : Number(key);
}

export function isSelected(key) { return _selected !== null && _normalize(key) === _selected; }
export function onSelectionChange(fn) { _listeners.push(fn); }

function _notify() {
  _listeners.forEach(function(fn) {
    try {
      fn(_selected);
    } catch (e) {
      console.error('work selection listener failed', e);
    }
  });
}

function _addEdge(map, from, to) {
  if (from == null || to == null) return;
  if (!map.has(from)) map.set(from, new Set());
  map.get(from).add(to);
}

// waitsFor: issue -> the issues it waits for; waitedOnBy: issue -> the issues waiting for it.
function _edges(board) {
  var waitsFor = new Map();
  var waitedOnBy = new Map();
  function add(entry) {
    if (!entry || entry.number == null) return;
    (entry.depends_on || []).forEach(function(dep) {
      _addEdge(waitsFor, entry.number, dep);
      _addEdge(waitedOnBy, dep, entry.number);
    });
    (entry.dependents || []).forEach(function(dependent) {
      _addEdge(waitedOnBy, entry.number, dependent);
      _addEdge(waitsFor, dependent, entry.number);
    });
  }
  (board.columns || []).forEach(function(col) { (board[col.entries] || []).forEach(add); });
  (board.hidden || []).forEach(add);
  (board.chains || []).forEach(function(chain) { (chain.issues || []).forEach(add); });
  return { waitsFor: waitsFor, waitedOnBy: waitedOnBy };
}

function _walk(start, map) {
  var seen = new Set();
  var stack = [start];
  while (stack.length) {
    var next = map.get(stack.pop());
    if (!next) continue;
    next.forEach(function(n) {
      if (n !== start && !seen.has(n)) {
        seen.add(n);
        stack.push(n);
      }
    });
  }
  return seen;
}

function _recompute() {
  _up = new Set();
  _down = new Set();
  if (_selected === null || !_board) return;
  var edges = _edges(_board);
  _up = _walk(_selected, edges.waitsFor);
  _down = _walk(_selected, edges.waitedOnBy);
}

export function setSelectionBoard(board) {
  _board = board;
  _recompute();
}

export function select(number) {
  _selected = _normalize(number);
  _recompute();
  _notify();
}

export function clearSelection() {
  if (_selected === null) return;
  select(null);
}

// The relation class of a card or node: selected, a blocker, a dependent, or dimmed ('' when nothing is selected).
export function relationClass(number) {
  if (_selected === null) return '';
  if (number === _selected) return 'sova-work-sel';
  if (_up.has(number)) return 'sova-work-up';
  if (_down.has(number)) return 'sova-work-down';
  return 'sova-work-dim';
}

export function relationChip(number) {
  if (_selected === null || number === _selected) return '';
  if (_up.has(number)) {
    return '<span class="px-1.5 rounded text-xs whitespace-nowrap sova-work-chip-up">Blocks #' + _selected + '</span>';
  }
  if (_down.has(number)) {
    return '<span class="px-1.5 rounded text-xs whitespace-nowrap sova-work-chip-down">Waits for #' + _selected + '</span>';
  }
  return '';
}

export function upstreamOf() { return new Set(_up); }
export function downstreamOf() { return new Set(_down); }

// Nothing to bind: Escape belongs to the drawer's side panel. Kept so the page starts every module the same way.
export function init() {}
