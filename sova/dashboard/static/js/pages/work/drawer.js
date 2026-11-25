import { createSidePanel } from '../../components/side-panel.js';
import { actionAttrs } from '../agents/actions.js';
import {
  actionSignature, cardParts, ciHtml, entryKey, getBoard, holdBoardScroll, iconHtml, lastRunHtml, loadBoard, onBoard, onBoardRendered, prFlags, prPill,
  queuePosition, sizeHtml, statePill, threadsHtml,
} from './board.js';
import { createPriorityPicker } from './priority-picker.js';
import { clearSelection, downstreamOf, getSelected, onSelectionChange, selectionKey } from './selection.js';

// The issue drawer (the ticket detail): why an issue is where it is, what it waits for, what it unblocks, its PR and
// last run, its buttons (the same as on its card, then the work item's other actions from the server) and the
// priority picker. It is a shared side panel (components/side-panel.js), open while a card or chain node is
// selected. It closes with Escape, its close button, a click outside it (a card, the search and the log viewer are
// not outside it), a second click on the selected card, or the activity feed opening, and each of these also clears
// the highlight.

// Every issue the board knows, with the stage it is in: column entries first, then hidden ones, then chain
// members (which carry less detail and are used only when the issue is nowhere else). A column entry for a PR
// with no issue is keyed 'pr:N' (selectionKey()), the key its card selects by.
function _index(board) {
  var byNumber = new Map();
  (board.columns || []).forEach(function(col) {
    (board[col.entries] || []).forEach(function(entry) {
      var key = selectionKey(entry);
      if (key != null && !byNumber.has(key)) byNumber.set(key, { entry: entry, stage: col.stage });
    });
  });
  (board.hidden || []).forEach(function(entry) {
    if (entry.number != null && !byNumber.has(entry.number)) byNumber.set(entry.number, { entry: entry, stage: 'hidden' });
  });
  (board.chains || []).forEach(function(chain) {
    (chain.issues || []).forEach(function(entry) {
      if (!byNumber.has(entry.number)) byNumber.set(entry.number, { entry: entry, stage: entry.stage || '', chain: true });
    });
  });
  return byNumber;
}

// The issue last opened from the search, as the search's entry: it stands in for a card while the board does not
// show the issue (a start puts it in Running), and a board entry always wins over it. It is dropped when another
// issue is selected or a column of the board shows this one, and re-read from the search on every board reload
// while the issue stays off the board, so its `start` never outlives the state it was offered for.
var _offBoard = null;

async function _refreshOffBoard() {
  var held = _offBoard;
  var board = getBoard();
  if (!held || !board || getSelected() !== held.number) return;
  var known = _index(board).get(held.number);
  if (known && !known.chain) return;
  try {
    var data = await fetchAPI(apiUrl('/work/search?q=' + encodeURIComponent(held.number)));
    if (_offBoard !== held) return;
    // An issue the search no longer returns (closed) leaves only what the board knows of it.
    _offBoard = (data.results || []).filter(function(r) { return r.number === held.number; })[0] || null;
    _renderUnlessBusy();
  } catch (_e) { /* keep what is drawn: the next board reload tries again */ }
}

export function showOffBoard(entry) { _offBoard = entry || null; }

// An issue the board does not show is drawn from the search's entry, which a board reload never refreshes, so a
// saved priority is written into it. Returns that entry, or null when the saved issue is not the searched one.
export function applySavedPriority(number, saved) {
  if (!_offBoard || _offBoard.number !== number) return null;
  _offBoard.priority = saved;
  return _offBoard;
}

// What the drawer draws for an issue: a board entry (a column or Hidden) first. A chain entry carries no `start` or
// `actions`, so the search's entry for the same issue replaces it (keeping the chain's stage); with no board
// record at all the search's entry stands alone.
export function resolveEntry(index, number, offBoard) {
  var known = index.get(number) || null;
  var searched = offBoard && offBoard.number === number ? offBoard : null;
  if (!searched) return known;
  if (!known) return { entry: searched, stage: '' };
  return known.chain ? { entry: searched, stage: known.stage } : known;
}

function _column(board, stage) {
  return (board.columns || []).filter(function(c) { return c.stage === stage; })[0] || null;
}

// A lookup of the column an issue sits in on the current board (its label and color token), null when the board
// does not show it. A hidden issue reads as Hidden. The board is indexed once per call, so a caller with many
// issues to place (the search results) builds one lookup and asks it for each.
export function boardColumns() {
  var board = getBoard();
  var index = board ? _index(board) : null;
  return function(number) {
    var known = index ? index.get(number) : null;
    if (!known) return null;
    if (known.stage === 'hidden') return { label: 'Hidden', color: 'gray' };
    return _column(board, known.stage);
  };
}

function _pill(board, stage) {
  if (stage === 'hidden') return '<span class="px-2 py-0.5 rounded-full text-xs bg-gray-600/30 text-gray-400">Hidden</span>';
  var col = _column(board, stage);
  if (!col) return '';
  return '<span class="px-2 py-0.5 rounded-full text-xs whitespace-nowrap ' + stateBadgeClasses(col.color) + '">' + escapeHtml(col.label) + '</span>';
}

// The ranking's reasons (`priority:high`, `unblocks:2`, `state:researched`) as words.
function _whyWords(why) {
  return (why || []).map(function(w) {
    var parts = String(w).split(':');
    var key = parts[0];
    var value = parts.slice(1).join(':');
    if (key === 'priority') return priorityDisplayName(value) + ' priority';
    if (key === 'unblocks') return 'finishing it unblocks ' + value + ' other issue' + (value === '1' ? '' : 's');
    if (key === 'state') return 'it is ' + value.replace(/_/g, ' ');
    return String(w).replace(/_/g, ' ');
  });
}

function _why(entry, stage) {
  if (stage === 'next') {
    var words = _whyWords(entry.why);
    return 'Rank ' + entry.rank + ' in Up next' + (words.length ? ': ' + words.join(', ') : '') + '.';
  }
  if (stage === 'queued') {
    var pos = queuePosition(entry.number);
    return 'Queued at position ' + pos[0] + ' of ' + pos[1] + '. The supervisor starts it when a slot frees, while it is on.';
  }
  if (stage === 'running') {
    // A one-step run (a command) has no step worth naming.
    var steps = entry.total_steps > 1 && entry.step_index != null;
    var step = steps && entry.step_name ? ' is at ' + entry.step_name.replace(/_/g, ' ') : ' is running';
    var of = steps ? ', step ' + (entry.step_index + 1) + ' of ' + entry.total_steps : '';
    return (entry.role ? formatRole(entry.role) : 'An agent') + step + of + '.';
  }
  if (stage === 'review') return entry.pr_number
    ? 'PR #' + entry.pr_number + ' is open and waits on CI or a review.'
    : 'The tracker marks this issue in review, but no open PR is linked.';
  if (stage === 'needs') return (entry.reason || 'It needs you') + (entry.pr_number ? ' (PR #' + entry.pr_number + ').' : '.');
  if (stage === 'yours') return 'Agents skip it: ' + (entry.reason || 'no agent label yet') + '.';
  if (stage === 'blocked') return 'It waits for ' + (entry.depends_on || []).map(function(n) { return '#' + n; }).join(', ') + ' to close.';
  if (stage === 'done') return 'Merged today' + (entry.pr_number ? ' with PR #' + entry.pr_number : '') + '.';
  if (stage === 'hidden') return 'Hidden from Up next. Unhide brings it back.';
  return entry.state_label ? 'Its state is ' + entry.state_label + '. It is not on the board.' : 'It is not on the board.';
}

function _depRow(board, index, number) {
  var known = index.get(number);
  var title = known ? known.entry.title : '';
  var stage = known ? known.stage : '';
  return '<button type="button" class="flex items-center gap-2 w-full text-left px-1.5 py-1 rounded hover:bg-surface-hover transition-colors min-w-0"' +
    actionAttrs('work-select', [number]) + '>' +
    '<span class="text-xs font-bold text-accent tabular-nums shrink-0">#' + number + '</span>' +
    '<span class="text-sm text-gray-300 truncate flex-1 min-w-0">' + escapeHtml(title || 'Not on the board') + '</span>' +
    (stage ? _pill(board, stage) : '') + '</button>';
}

function _section(label, inner, count) {
  return '<section class="flex flex-col gap-1.5"><h4 class="flex items-center gap-1.5 text-xs uppercase tracking-wider text-gray-500">' +
    escapeHtml(label) + (count || '') + '</h4>' + inner + '</section>';
}

// A count beside a section heading: colored when there is something to count, muted at zero.
function _count(n, cls) {
  return '<span class="px-1.5 rounded-full text-xs normal-case tracking-normal tabular-nums ' +
    (n > 0 ? cls : 'bg-surface-hover text-gray-500') + '">' + n + '</span>';
}

function _tag(text, cls) {
  return '<span class="px-1.5 rounded-full text-xs whitespace-nowrap ' + (cls || 'bg-surface-hover text-gray-300') + '">' + escapeHtml(text) + '</span>';
}

var _ACTION_BUTTON = { primary: 'sova-btn-primary', success: 'sova-btn-success', warning: 'sova-btn-warning', danger: 'sova-btn-danger' };

// The work item's own actions (from the server, for its state) that its card buttons do not already offer:
// for a PR whose review budget ran out, Review PR and Address PR beside Integrate.
// With no card buttons (an issue off the board, or a card whose stage offers none), they are its only buttons: no
// "Also", and the first is primary.
function _moreActions(entry, parts, busy) {
  var covered = parts.covers || [];
  var actions = (entry.actions || []).filter(function(a) { return covered.indexOf(actionSignature(a)) === -1; });
  if (!actions.length) return '';
  var only = !parts.buttons;
  return '<div class="flex flex-wrap items-center gap-1">' +
    (only ? '' : '<span class="text-xs text-gray-500 mr-1">Also</span>') +
    actions.map(function(a, i) {
      var variant = only && i === 0 ? _ACTION_BUTTON.primary : _ACTION_BUTTON[a.style];
      var cls = variant ? 'sova-btn ' + variant : 'sova-btn sova-work-btn-quiet bg-surface-hover text-gray-300 hover:text-white';
      return '<button type="button" class="' + cls + '"' + actionAttrs('work-run-action', [entry.number, a]) + (busy ? ' disabled' : '') + '>' +
        iconHtml('play') + escapeHtml(a.label) + '</button>';
    }).join('') + '</div>';
}

function _kv(rows) {
  var html = rows.filter(function(r) { return r[1]; }).map(function(r) {
    return '<dt class="text-gray-500">' + escapeHtml(r[0]) + '</dt><dd class="text-gray-300 min-w-0">' + r[1] + '</dd>';
  }).join('');
  return html ? '<dl class="grid grid-cols-[7rem_1fr] gap-x-3 gap-y-1 text-sm">' + html + '</dl>' : '';
}

function _render() {
  _redrawPending = false;
  var el = document.getElementById('work-drawer');
  if (!el) return;
  var board = getBoard();
  var number = getSelected();
  _setBoardRoom(number !== null && !!board);
  if (number === null || !board) {
    // The content stays while the panel slides out; the next selection replaces it.
    if (_panel) _panel.close('deselected');
    return;
  }
  var index = _index(board);
  if (_offBoard) {
    var held = index.get(_offBoard.number);
    if (_offBoard.number !== number || (held && !held.chain)) _offBoard = null;
  }
  var known = resolveEntry(index, number, _offBoard);
  // A selection the board no longer shows keeps its key's shape: an issue number, or a PR with no issue.
  var entry = known ? known.entry
    : typeof number === 'string' ? { number: null, pr_number: Number(number.slice(3)), title: '' } : { number: number, title: '' };
  var stage = known ? known.stage : '';
  var parts = known ? cardParts(entry, stage) : { buttons: '' };
  var waits = entry.depends_on || [];
  var direct = entry.dependents || [];
  var total = downstreamOf().size;
  var pr = entry.pr;
  var last = entry.last_run;

  var busy = actionIsRunning(entryKey(entry));
  var col = _column(board, stage);
  // A PR with no issue (selected as 'pr:N') has no priority, relations or milestone: the drawer shows its PR only.
  var prOnly = typeof number === 'string';
  var ref = prOnly ? 'PR #' + number.slice(3) : (entry.jira_key || '#' + number);
  var link = entry.url || (prOnly && pr && /^https:\/\//.test(pr.url || '') ? pr.url : '');
  // Empty for a PR with no issue and no run yet, which then shows no Context heading at all.
  var context = _kv([
    ['Milestone', prOnly ? '' : (entry.milestone ? _tag(entry.milestone) : '<span class="text-gray-500">None</span>')],
    ['Agent fit', entry.mode ? _tag(entry.mode.replace(/_/g, ' ')) : ''],
    ['Last run', last ? '<span class="flex flex-wrap items-center gap-1">' + lastRunHtml(entry, true) +
      '<a class="text-accent hover:underline" href="' + (window.SOVA_PROJECT_SLUG ? '/p/' + window.SOVA_PROJECT_SLUG : '') + '/work/' + last.run_id + '">details</a></span>' : ''],
  ]);
  var html = '<div class="flex flex-col gap-2 px-4 pt-4 pb-3 border-b border-gray-700/50">' +
      '<div class="flex flex-wrap items-center gap-1.5">' +
        (!prOnly && entry.priority != null && stage !== 'done' ? '<img src="' + priorityIconUrl(entry.priority) + '" alt="' + escapeHtml(priorityDisplayName(entry.priority)) + ' priority" width="16" height="16">' : '') +
        '<span class="text-sm font-bold text-accent tabular-nums">' + escapeHtml(ref) + '</span>' + _pill(board, stage) + statePill(entry) +
        '<span class="ml-auto flex items-center gap-1">' +
          (link ? '<a href="' + escapeHtml(link) + '" target="_blank" rel="noopener" class="p-1 rounded text-gray-400 hover:text-gray-200 hover:bg-surface-hover transition-colors" aria-label="Open ' + escapeHtml(ref) + '">' + iconHtml('external-link') + '</a>' : '') +
          '<button type="button" id="work-drawer-close" data-panel-close class="sova-btn sova-btn-ghost sova-btn-icon sova-work-btn-quiet" aria-label="Close the details">' + iconHtml('close') + '</button>' +
        '</span>' +
      '</div>' +
      '<h3 class="text-base font-semibold text-white leading-snug">' + escapeHtml(entry.title || (prOnly ? ref : 'Issue #' + number)) + '</h3>' +
      actionStatusHtml(entryKey(entry)) +
      (parts.buttons ? '<div class="flex flex-wrap items-center gap-0.5">' + parts.buttons + '</div>' : '') +
      _moreActions(entry, parts, busy) +
    '</div>' +
    '<div class="sova-side-panel-body sova-work-drawer-body flex flex-col gap-4 px-4 py-3">' +
      _section('Why it is here', '<p class="flex items-start gap-2 text-sm text-gray-300">' +
        '<span class="w-2 h-2 mt-1.5 rounded-full shrink-0" style="background:' + stateHex(col ? col.color : 'gray') + '"></span>' +
        '<span>' + escapeHtml(_why(entry, stage)) + '</span></p>') +
      (prOnly ? '' : _section('Priority', '<div id="work-drawer-priority"></div>') +
        _section('Waits for',
          waits.map(function(n) { return _depRow(board, index, n); }).join('') || '<p class="text-xs text-gray-500">Nothing open.</p>',
          _count(waits.length, 'sova-work-chip-up')) +
        _section('Unblocks',
          direct.map(function(n) { return _depRow(board, index, n); }).join('') || '<p class="text-xs text-gray-500">Nothing waits for it.</p>',
          _count(direct.length, 'sova-work-chip-unblocks') +
            (total > direct.length ? '<span class="normal-case tracking-normal text-gray-500">' + total + ' in total</span>' : ''))) +
      (pr ? _section('Pull request', _kv([
        ['PR', '<span class="flex flex-wrap items-center gap-1">' + prPill(pr) +
          (pr.state_label ? _tag(pr.state_label, stateBadgeClasses(pr.state_color)) : '') + prFlags(pr, { stateShown: !!pr.state_label, end: true }) + '</span>'],
        ['CI', ciHtml(pr.ci_state) || '<span class="text-gray-500">No checks reported</span>'],
        ['Threads', threadsHtml(pr, true)],
        ['Size', sizeHtml(pr, true)],
      ])) : '') +
      (context ? _section('Context', context) : '') +
    '</div>';
  el.innerHTML = html;
  _focusKey = number;
  if (_panel) _panel.open();
  var host = document.getElementById('work-drawer-priority');
  if (host) {
    host.appendChild(createPriorityPicker({
      issue: number,
      value: entry.priority,
      iconHtml: iconHtml,
      error: _priorityError && _priorityError.issue === number ? _priorityError.message : '',
      onSaved: function(saved) {
        _priorityError = null;
        applySavedPriority(number, saved);
        loadBoard();
      },
      onFailed: function(message) { _priorityError = { issue: number, message: message }; },
      onIdle: _replayRedraw,
    }));
  }
}

// A redraw replaces the picker, so a board refresh must not land while its menu is open or its save runs. The
// redraw is held (_redrawPending) and applied when the picker reports idle, so the drawer never keeps old state.
var _redrawPending = false;
// The last failed priority save, drawn into the next picker: a redraw after the failure must not hide it.
var _priorityError = null;

function _isOpen() { return !!(_panel && _panel.isOpen()); }

function _renderUnlessBusy() {
  var el = document.getElementById('work-drawer');
  if (el && _isOpen() && el.querySelector('.sova-priority-picker[data-open], .sova-priority-picker[data-saving]')) {
    _redrawPending = true;
    return;
  }
  _render();
}

function _replayRedraw() {
  if (!_redrawPending) return;
  _redrawPending = false;
  _renderUnlessBusy();
}

// The drawer lies over the right of the board without narrowing it, so a card selected in a right-hand column
// would open the drawer on top of itself. The board scrolls sideways inside its own box: scroll it, as far as it
// can go, until the selected card clears the drawer's left edge. While the drawer is open the board's track has a
// drawer's width of extra scroll room (sova-work-board-room), so even the last column can be brought clear.
//
// The reveal never depends on listener order or timing. It is tried after the board redraws its cards and after
// the drawer draws itself, whichever runs first. A selection is handled once its card was measured with the drawer
// showing; if no card can be measured at a redraw with the drawer showing (an issue only a chain or the search
// shows, or a card in a collapsed group), the selection is handled all the same, so a reload or an expanded group
// minutes later never scrolls the board on its own. _revealedKey is the selection already handled.
var _revealedKey = null;

var _REVEAL_NOT_READY = 0;  // no selection, or the drawer is not showing yet: try again at the next redraw
var _REVEAL_NO_CARD = 1;    // the drawer shows but no column card could be measured
var _REVEAL_DONE = 2;       // the card was measured (and scrolled, or found clear already)

function _revealSelectedCard() {
  var key = getSelected();
  var drawer = document.getElementById('work-drawer');
  var host = document.getElementById('work-board');
  if (key === null || !drawer || !_isOpen() || !host) return _REVEAL_NOT_READY;
  var args = JSON.stringify([key]);
  // Only the column card: a chain node or a collapsed group's card selects the same issue, and a hidden one
  // measures as an empty box.
  var card = Array.prototype.find.call(host.querySelectorAll('[data-work-card]'), function(el) {
    return el.isConnected !== false && el.getAttribute('data-args') === args && el.getBoundingClientRect().width > 0;
  });
  if (!card) return _REVEAL_NO_CARD;
  var cardBox = card.getBoundingClientRect();
  var hostBox = host.getBoundingClientRect();
  // Where the drawer's left edge settles: it may still be sliding in, and its box would measure mid-slide.
  var limit = Math.min(hostBox.right, document.documentElement.clientWidth - drawer.offsetWidth) - 16;
  // A drawer covering most of the window (a narrow screen) leaves no room to show the card beside it.
  if (cardBox.right <= limit || limit - hostBox.left < cardBox.width) return _REVEAL_DONE;
  // Never ask for more than the board can still scroll, so no animation starts that goes nowhere.
  var delta = Math.min(cardBox.right - limit, host.scrollWidth - host.clientWidth - host.scrollLeft);
  if (!(delta > 0)) return _REVEAL_DONE;
  var reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  if (!reduce) holdBoardScroll(host.scrollLeft + delta, key);
  host.scrollBy({ left: delta, behavior: reduce ? 'auto' : 'smooth' });
  return _REVEAL_DONE;
}

// The selection the board last redrew its cards for: the reveal waits for it, and a drawer that shows and finds no
// card for a selection the board has drawn has none to show, whichever of the two ran first.
var _drawnKey = null;

function _revealIfNew() {
  var key = getSelected();
  if (key === null) {
    _revealedKey = null;
    return;
  }
  if (key === _revealedKey) return;
  // Measure only once the board has drawn this selection: ahead of its redraw the cards are the old ones, and the
  // redraw restoring its own scroll position would turn the smooth scroll into a jump.
  if (_drawnKey !== key) return;
  var result = _revealSelectedCard();
  if (result === _REVEAL_DONE || (result === _REVEAL_NO_CARD && _drawnKey === key)) _revealedKey = key;
}

// The board keeps a drawer's width of scroll room past its last column while the drawer shows.
function _setBoardRoom(open) {
  var host = document.getElementById('work-board');
  if (host && host.classList) host.classList.toggle('sova-work-board-room', open);
}

// The card focus returns to when Escape or the close button closes the drawer: the one the last drawn issue selects.
// It is looked up after the close, once the board has redrawn its cards without the highlight.
var _focusKey = null;

function _selectedCard() {
  var host = document.getElementById('work-board');
  if (_focusKey === null || !host) return null;
  var args = JSON.stringify([_focusKey]);
  return Array.prototype.find.call(host.querySelectorAll('[data-work-card]'), function(card) {
    return card.getAttribute('data-args') === args;
  }) || null;
}

var _panel = null;

export function init() {
  var el = document.getElementById('work-drawer');
  if (el) {
    _panel = createSidePanel(el, {
      name: 'ticket',
      floats: 'shift',
      // A card or chain node switches the ticket (or, the selected one, closes it through the selection); the search
      // opens one; the log viewer is what the drawer's View log opened.
      keepOpen: '[data-action="work-select"], #work-search, #log-viewer',
      returnFocus: _selectedCard,
      onClose: function(reason) { if (reason !== 'deselected') clearSelection(); },
    });
  }
  onSelectionChange(_render);
  onSelectionChange(_revealIfNew);
  onBoardRendered(function() {
    _drawnKey = getSelected();
    _revealIfNew();
  });
  onBoard(function() {
    _refreshOffBoard();
    _renderUnlessBusy();
  });
  onActionStateChange(_renderUnlessBusy);
}
