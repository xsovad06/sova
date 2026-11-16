import { actionAttrs } from '../agents/actions.js';
import { getBoard, iconHtml, onBoard } from './board.js';
import { onSelectionChange, relationClass } from './selection.js';

// "How it hangs together": the dependency chains the board API returns (each a group of open issues that
// wait on one another, with at least one of them on the board), drawn left to right in dependency order:
// an issue sits one step right of the last issue it waits for. Selecting a node selects that issue, as a
// card does. The count of other blocked issues and the link to the dependency graph come last.

var _PREFIX = window.SOVA_PROJECT_SLUG ? '/p/' + window.SOVA_PROJECT_SLUG : '';

function _stageColor(board, stage) {
  var col = (board.columns || []).filter(function(c) { return c.stage === stage; })[0];
  return stateHex(col ? col.color : 'gray');
}

function _stageLabel(board, stage) {
  var col = (board.columns || []).filter(function(c) { return c.stage === stage; })[0];
  return col ? col.label : 'Not on the board';
}

// Each issue's step: 0 when it waits for nothing in the chain, else one more than its deepest dependency.
function _steps(issues) {
  var byNumber = new Map(issues.map(function(i) { return [i.number, i]; }));
  var memo = new Map();
  function step(issue, seen) {
    if (memo.has(issue.number)) return memo.get(issue.number);
    if (seen.has(issue.number)) return 0;  // a cycle: stop rather than recurse forever
    seen.add(issue.number);
    var deepest = -1;
    (issue.depends_on || []).forEach(function(dep) {
      var d = byNumber.get(dep);
      if (d) deepest = Math.max(deepest, step(d, seen));
    });
    memo.set(issue.number, deepest + 1);
    return deepest + 1;
  }
  issues.forEach(function(i) { step(i, new Set()); });
  return memo;
}

function _node(board, issue) {
  var title = issue.title || '';
  return '<button type="button" class="sova-work-node ' + relationClass(issue.number) + '"' +
    actionAttrs('work-select', [issue.number]) +
    ' aria-label="#' + issue.number + ' ' + escapeHtml(title) + ', ' + escapeHtml(_stageLabel(board, issue.stage)) + '">' +
    '<span class="w-1.5 h-1.5 rounded-full shrink-0" style="background:' + _stageColor(board, issue.stage) + '"></span>' +
    '<span class="font-bold text-accent tabular-nums">#' + issue.number + '</span>' +
    '<span class="text-gray-300 truncate min-w-0">' + escapeHtml(title) + '</span></button>';
}

function _chain(board, chain) {
  var issues = chain.issues || [];
  var steps = _steps(issues);
  var columns = [];
  issues.forEach(function(issue) {
    var s = steps.get(issue.number) || 0;
    (columns[s] = columns[s] || []).push(issue);
  });
  var arrow = '<span class="text-gray-500 shrink-0" aria-hidden="true">' + iconHtml('arrow-right') + '</span>';
  return '<div class="flex flex-wrap items-center gap-1.5 px-2 py-1.5 rounded" role="group" aria-label="Dependency chain">' +
    columns.filter(Boolean).map(function(col) {
      return '<div class="flex flex-col gap-1">' + col.map(function(i) { return _node(board, i); }).join('') + '</div>';
    }).join(arrow) + '</div>';
}

function _render() {
  var board = getBoard();
  var body = document.getElementById('work-chains-body');
  if (!board || !body) return;
  var chains = board.chains || [];
  setText('work-chains-count', chains.length + ' chain' + (chains.length === 1 ? '' : 's'));
  var other = board.other_blocked || 0;
  body.innerHTML = (chains.map(function(c) { return _chain(board, c); }).join('') ||
      '<p class="text-xs text-gray-500">Nothing on the board waits on anything else.</p>') +
    '<p class="text-xs text-gray-500 px-2">' +
      (other ? other + ' other blocked issue' + (other === 1 ? ' is' : 's are') + ' further out and not shown. ' : '') +
      'The whole backlog is on the <a class="text-accent hover:underline" href="' + _PREFIX + '/supervisor">dependency graph</a>.' +
    '</p>';
}

export function init() {
  onBoard(_render);
  onSelectionChange(_render);
}
