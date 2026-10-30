import { registerActions } from './actions.js';
import { loadAgents } from './agent-cards.js';

// Storage can throw (blocked site data, sandboxed iframe); a throw here at
// module evaluation would fail every Agents module's import.
function _readStoredView() {
  try {
    return localStorage.getItem('sova-agents-view') === 'kanban' ? 'kanban' : 'list';
  } catch (e) {
    return 'list';
  }
}

var _agentView = _readStoredView();
export function getAgentView() { return _agentView; }

// View Toggle (List / Pipeline Kanban)

var _toggleBase = 'px-3 py-1.5 text-xs font-medium border border-gray-700 transition-colors';
var _toggleActive = 'bg-surface-hover text-white';
var _toggleInactive = 'bg-surface text-gray-500 hover:text-gray-300';

function _applyAgentView(view) {
  _agentView = view;
  try { localStorage.setItem('sova-agents-view', view); } catch (e) { /* storage unavailable */ }

  var listBtn = document.getElementById('view-list');
  var kanbanBtn = document.getElementById('view-kanban');
  var listGrid = document.getElementById('agents-grid');
  var kanbanGrid = document.getElementById('kanban-grid');
  var isKanban = view === 'kanban';

  listGrid.classList.toggle('hidden', isKanban);
  kanbanGrid.classList.toggle('hidden', !isKanban);
  listBtn.className = _toggleBase + ' rounded-l ' + (isKanban ? _toggleInactive : _toggleActive);
  kanbanBtn.className = _toggleBase + ' rounded-r ' + (isKanban ? _toggleActive : _toggleInactive);
}

function switchAgentView(view) {
  _applyAgentView(view);
  if (view === 'kanban') {
    loadKanban();
  } else {
    loadAgents();
  }
}

function _formatStepName(name) {
  if (!name) return '';
  return name.replace(/_/g, ' ').replace(/\b\w/g, function(c) { return c.toUpperCase(); });
}

function _pipelineLabel(pipeline) {
  var labels = { developer: 'Dev', address_review: 'Addr', researcher: 'Res', pending: '', unknown: '' };
  return labels[pipeline] || pipeline;
}

// Kanban drag-and-drop state
var _kanbanDragging = false;
var _kanbanDragRunId = null;
var _kanbanOrderKey = 'sova-kanban-card-order';

function _getKanbanOrder() {
  try {
    return JSON.parse(localStorage.getItem(_kanbanOrderKey)) || {};
  } catch (e) { return {}; }
}

function _saveKanbanOrder(order) {
  try {
    localStorage.setItem(_kanbanOrderKey, JSON.stringify(order));
  } catch (e) {
    console.warn('Failed to save kanban order:', e);
  }
}

function _columnKey(col) {
  return col.name + '--' + (col.pipeline || '');
}

function _applyCardOrder(runs, columnKey, savedOrder) {
  var order = savedOrder[columnKey];
  if (!order || !order.length) return runs;
  var byId = {};
  runs.forEach(function(r) { byId[r.id] = r; });
  var ordered = [];
  order.forEach(function(id) {
    if (byId[id]) { ordered.push(byId[id]); delete byId[id]; }
  });
  Object.keys(byId).forEach(function(id) { ordered.push(byId[id]); });
  return ordered;
}

var _kanbanPlaceholder = null;

function _removePlaceholder() {
  if (_kanbanPlaceholder) { _kanbanPlaceholder.remove(); _kanbanPlaceholder = null; }
}

function _insertPlaceholder(targetCard, clientY) {
  _removePlaceholder();
  var rect = targetCard.getBoundingClientRect();
  var midY = rect.top + rect.height / 2;
  _kanbanPlaceholder = document.createElement('div');
  _kanbanPlaceholder.className = 'kanban-drop-placeholder';
  if (clientY < midY) {
    targetCard.parentNode.insertBefore(_kanbanPlaceholder, targetCard);
  } else {
    targetCard.parentNode.insertBefore(_kanbanPlaceholder, targetCard.nextSibling);
  }
}

function _buildLabelHtml(runLabel) {
  if (!runLabel) return '';
  return '<div class="mb-1" style="max-width:12rem" data-tooltip="' + escapeHtml(runLabel) + '">' +
    '<div class="text-xs text-gray-400 truncate">' + escapeHtml(runLabel) + '</div></div>';
}

async function loadKanban() {
  if (_agentView !== 'kanban') return;
  if (_kanbanDragging) return;
  try {
    var data = await fetchAPI(apiUrl('/agents/kanban'));
    var container = document.getElementById('kanban-columns');
    if (!container) return;

    if (!data.columns || data.columns.length === 0) {
      container.innerHTML = '<p class="text-gray-500 text-sm py-8 text-center w-full">No active runs</p>';
      return;
    }

    var prefix = window.SOVA_PROJECT_SLUG ? '/p/' + window.SOVA_PROJECT_SLUG : '';
    var savedOrder = _getKanbanOrder();

    var columnsHtml = data.columns.map(function(col) {
      var colKey = _columnKey(col);
      var pipelineBadge = col.pipeline && col.pipeline !== 'pending' && col.pipeline !== 'unknown'
        ? '<span class="text-xs text-gray-600 font-normal ml-1">' + escapeHtml(_pipelineLabel(col.pipeline)) + '</span>'
        : '';
      var countBadge = '<span class="inline-flex items-center justify-center min-w-[1.25rem] h-5 px-1.5 text-xs rounded-full bg-accent/20 text-accent">' + col.count + '</span>';

      var sortedRuns = _applyCardOrder(col.runs || [], colKey, savedOrder);
      var cardsHtml = sortedRuns.map(function(run) {
        var rc = roleColor(run.role);

        return '<a href="' + prefix + '/work/' + run.id + '" class="kanban-card block" draggable="true" data-run-id="' + run.id + '">' +
          '<div class="flex items-center justify-between mb-1.5">' +
            '<span class="text-sm font-medium text-gray-200">' + issueOrPrLabel(run.issue_number, run.pr_number) + '</span>' +
            '<span class="w-2 h-2 rounded-full flex-shrink-0 ' + statusDot(run.status) + '"></span>' +
          '</div>' +
          _buildLabelHtml(run.run_label) +
          '<div class="flex items-center gap-1.5 mb-1.5">' +
            '<span class="px-1.5 py-0.5 rounded text-xs font-medium ' + rc.bg + ' ' + rc.text + '">' + escapeHtml(formatRole(run.role, run.pipeline_variant)) + '</span>' +
            '<span class="text-xs text-gray-500">' + escapeHtml(run.status) + '</span>' +
          '</div>' +
          '<div class="flex items-center justify-between text-xs text-gray-500">' +
            '<span>' + (run.status === 'running' ? liveElapsed(run.elapsed_seconds) : formatElapsed(run.elapsed_seconds)) + '</span>' +
            '<span class="text-accent-green">$' + (Number(run.total_cost_usd) || 0).toFixed(2) + '</span>' +
          '</div>' +
        '</a>';
      }).join('');

      var overflowHint = col.count > col.runs.length
        ? '<p class="text-xs text-gray-600 text-center mt-2">+ ' + (col.count - col.runs.length) + ' more</p>'
        : '';

      return '<div class="kanban-column">' +
        '<div class="flex items-center justify-between px-3 py-2 border-b border-gray-700/50">' +
          '<span class="text-xs font-medium text-gray-400 uppercase tracking-wider">' +
            escapeHtml(_formatStepName(col.name)) + pipelineBadge +
          '</span>' +
          countBadge +
        '</div>' +
        '<div class="kanban-card-list p-2 space-y-2" data-column="' + escapeHtml(colKey) + '">' +
          cardsHtml +
          overflowHint +
        '</div>' +
      '</div>';
    }).join('');

    // Recently Failed column
    var failedRuns = data.failed_runs || [];
    var failedColHtml = '';
    var failedCount = failedRuns.length;
    var failedCountBadge = '<span class="inline-flex items-center justify-center min-w-[1.25rem] h-5 px-1.5 text-xs rounded-full bg-accent-red/20 text-accent-red">' + failedCount + '</span>';
    var failedCardsHtml = failedRuns.length
      ? failedRuns.map(function(run) {
          var rc = roleColor(run.role);
          var issueText = (run.issue_number || run.pr_number) ? issueOrPrLabel(run.issue_number, run.pr_number) : escapeHtml(run.role || 'command');

          var rerunHtml = run.issue_number
            ? '<button class="rerun-btn px-2 py-0.5 rounded text-xs bg-accent-yellow/10 text-accent-yellow hover:bg-accent-yellow/20 transition-colors flex-shrink-0" data-action="rerun" ' +
              'data-issue="' + escapeHtml(String(run.issue_number)) + '" data-role="' + escapeHtml(run.role || '') + '" data-pr="' + escapeHtml(String(parseInt(run.pr_number, 10) || '')) + '" ' +
              'data-run-id="' + escapeHtml(String(run.id)) + '">Re-run</button>'
            : '';

          var errorHtml = run.error_message
            ? '<div class="mt-1" style="max-width:12rem" data-tooltip="' + escapeHtml(run.error_message) + '">' +
              '<div class="text-xs text-accent-red/70 truncate">' + escapeHtml(run.error_message) + '</div></div>'
            : '';

          return '<a href="' + prefix + '/work/' + run.id + '" class="kanban-card block border-accent-red/30">' +
            '<div class="flex items-center justify-between mb-1.5">' +
              '<span class="text-sm font-medium text-gray-200">' + issueText + '</span>' +
              '<span class="w-2 h-2 rounded-full flex-shrink-0 ' + statusDot('failed') + '"></span>' +
            '</div>' +
            _buildLabelHtml(run.run_label) +
            '<div class="flex items-center gap-1.5 mb-1.5">' +
              '<span class="px-1.5 py-0.5 rounded text-xs font-medium ' + rc.bg + ' ' + rc.text + '">' + escapeHtml(formatRole(run.role, run.pipeline_variant)) + '</span>' +
              rerunHtml +
            '</div>' +
            errorHtml +
            '<div class="flex items-center justify-between text-xs text-gray-500 mt-1">' +
              '<span>' + (run.duration_formatted || '') + '</span>' +
              '<span class="text-accent-green">$' + (Number(run.total_cost_usd) || 0).toFixed(2) + '</span>' +
            '</div>' +
          '</a>';
        }).join('')
      : '<p class="text-gray-600 text-xs text-center py-4">No failures in the last 24h</p>'; // Keep in sync with get_recent_failed_runs default hours=24

    failedColHtml = '<div class="kanban-column" style="border-color: rgb(var(--ctp-red-rgb) / 0.2)">' +
      '<div class="flex items-center justify-between px-3 py-2 border-b border-accent-red/20">' +
        '<span class="text-xs font-medium text-accent-red/70 uppercase tracking-wider">Recently Failed</span>' +
        failedCountBadge +
      '</div>' +
      '<div class="kanban-card-list p-2 space-y-2">' +
        failedCardsHtml +
      '</div>' +
    '</div>';

    container.innerHTML = columnsHtml + failedColHtml;
  } catch (e) {
    console.error('Failed to load kanban:', e);
    var errContainer = document.getElementById('kanban-columns');
    if (errContainer) errContainer.innerHTML = '<p class="text-red-400 text-sm py-8 text-center w-full">Failed to load pipeline view</p>';
  }
}

// Kanban drag-and-drop handlers (event delegation)
// Minimum px movement before a touch gesture becomes a drag (prevents accidental drags while scrolling)
var _TOUCH_DRAG_THRESHOLD = 10;

function _saveColumnOrder(cardList) {
  var colKey = cardList.getAttribute('data-column');
  if (!colKey) return;
  var cards = cardList.querySelectorAll('.kanban-card[data-run-id]');
  var ids = [];
  cards.forEach(function(c) { ids.push(Number(c.getAttribute('data-run-id'))); });
  var order = _getKanbanOrder();
  order[colKey] = ids;
  _saveKanbanOrder(order);
}

// Stop refreshing during drag to prevent the DOM from being replaced mid-interaction
// (loadKanban returns early while _kanbanDragging is set, so the 5s poll just skips)
function _pauseKanbanPolling() {
  _kanbanDragging = true;
}

// Re-fetch kanban data right after a drag completes; the poll picks up from there
function _resumeKanbanPolling() {
  _kanbanDragging = false;
  loadKanban();
}

function _initDragAndDrop() {
  var container = document.getElementById('kanban-columns');
  if (!container) return;

  container.addEventListener('dragstart', function(e) {
    var card = e.target.closest('.kanban-card[data-run-id]');
    if (!card) return;
    _pauseKanbanPolling();
    _kanbanDragRunId = card.getAttribute('data-run-id');
    card.classList.add('dragging');
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData('text/plain', _kanbanDragRunId);
  });

  // Dragover fires at ~60Hz; throttle DOM updates via requestAnimationFrame
  var _dragRafPending = false;
  container.addEventListener('dragover', function(e) {
    var cardList = e.target.closest('.kanban-card-list');
    if (!cardList) return;
    // Only within-column reorder: never show a drop state over another column
    var dragged = container.querySelector('.kanban-card[data-run-id="' + _kanbanDragRunId + '"]');
    if (!dragged || dragged.closest('.kanban-card-list') !== cardList) return;
    var card = e.target.closest('.kanban-card[data-run-id]');
    if (card && card.getAttribute('data-run-id') === _kanbanDragRunId) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';

    if (_dragRafPending) return;
    var clientY = e.clientY;
    var targetCard = card;
    _dragRafPending = true;
    requestAnimationFrame(function() {
      _dragRafPending = false;
      if (targetCard) {
        _insertPlaceholder(targetCard, clientY);
      } else {
        _removePlaceholder();
      }
    });
  });

  // Remove placeholder when cursor leaves all card lists; use relatedTarget
  // to avoid flicker when moving between child elements within the same list.
  container.addEventListener('dragleave', function(e) {
    if (!e.target.closest('.kanban-card-list')) return;
    if (e.relatedTarget && e.relatedTarget.closest && e.relatedTarget.closest('.kanban-card-list')) return;
    _removePlaceholder();
  });

  container.addEventListener('drop', function(e) {
    e.preventDefault();
    var draggedId = e.dataTransfer.getData('text/plain');
    if (!/^\d+$/.test(draggedId)) { _removePlaceholder(); return; }
    var draggedCard = container.querySelector('.kanban-card[data-run-id="' + draggedId + '"]');
    var targetList = e.target.closest('.kanban-card-list');

    if (!draggedCard || !targetList) {
      _removePlaceholder();
      return;
    }

    // Only allow within-column reorder
    var sourceList = draggedCard.closest('.kanban-card-list');
    if (!sourceList || sourceList !== targetList) {
      _removePlaceholder();
      return;
    }

    if (_kanbanPlaceholder && _kanbanPlaceholder.parentNode === targetList) {
      targetList.insertBefore(draggedCard, _kanbanPlaceholder);
      _removePlaceholder();
      _saveColumnOrder(targetList);
    }
  });

  container.addEventListener('dragend', function(e) {
    _removePlaceholder();
    var card = e.target.closest('.kanban-card[data-run-id]');
    if (card) card.classList.remove('dragging');
    _kanbanDragRunId = null;
    _resumeKanbanPolling();
  });

  // Touch drag-and-drop
  var _touchDragCard = null;
  var _touchClone = null;
  var _touchStartX = 0;
  var _touchStartY = 0;
  var _touchDragging = false;

  container.addEventListener('touchstart', function(e) {
    var card = e.target.closest('.kanban-card[data-run-id]');
    if (!card) return;
    _touchDragCard = card;
    _touchDragging = false;
    var touch = e.touches[0];
    _touchStartX = touch.clientX;
    _touchStartY = touch.clientY;
  }, { passive: true });

  var _touchRafPending = false;
  container.addEventListener('touchmove', function(e) {
    if (!_touchDragCard) return;
    var touch = e.touches[0];
    var dx = touch.clientX - _touchStartX;
    var dy = touch.clientY - _touchStartY;

    if (!_touchDragging) {
      if (Math.sqrt(dx * dx + dy * dy) < _TOUCH_DRAG_THRESHOLD) return;
      _touchDragging = true;
      _pauseKanbanPolling();
      _touchDragCard.classList.add('dragging');
      _touchClone = _touchDragCard.cloneNode(true);
      _touchClone.classList.add('kanban-touch-clone');
      _touchClone.style.width = _touchDragCard.offsetWidth + 'px';
      document.body.appendChild(_touchClone);
    }

    e.preventDefault();
    _touchClone.style.left = touch.clientX + 'px';
    _touchClone.style.top = touch.clientY + 'px';

    if (_touchRafPending) return;
    var clientY = touch.clientY;
    var clientX = touch.clientX;
    var dragCard = _touchDragCard;
    _touchRafPending = true;
    requestAnimationFrame(function() {
      _touchRafPending = false;
      var elem = document.elementFromPoint(clientX, clientY);
      if (elem) {
        var targetCard = elem.closest('.kanban-card[data-run-id]');
        if (targetCard && targetCard !== dragCard) {
          var targetList = targetCard.closest('.kanban-card-list');
          var sourceList = dragCard.closest('.kanban-card-list');
          if (targetList === sourceList) {
            _insertPlaceholder(targetCard, clientY);
          }
        }
      }
    });
  }, { passive: false });

  container.addEventListener('touchend', function(e) {
    if (!_touchDragCard) return;
    if (!_touchDragging) {
      _touchDragCard = null;
      return;
    }

    if (_touchClone) { _touchClone.remove(); _touchClone = null; }
    _touchDragCard.classList.remove('dragging');

    // Use existing placeholder position for drop target
    if (_kanbanPlaceholder) {
      var targetList = _kanbanPlaceholder.closest('.kanban-card-list');
      var sourceList = _touchDragCard.closest('.kanban-card-list');
      if (targetList && targetList === sourceList) {
        targetList.insertBefore(_touchDragCard, _kanbanPlaceholder);
        _saveColumnOrder(targetList);
      }
    }
    _removePlaceholder();

    _touchDragCard = null;
    _touchDragging = false;
    _resumeKanbanPolling();
  });

  // A system gesture or OS interruption fires touchcancel instead of touchend;
  // without this the poll stays paused and the drag clone stays attached.
  container.addEventListener('touchcancel', function() {
    var wasDragging = _touchDragging;
    if (_touchClone) { _touchClone.remove(); _touchClone = null; }
    if (_touchDragCard) _touchDragCard.classList.remove('dragging');
    _removePlaceholder();
    _touchDragCard = null;
    _touchDragging = false;
    if (wasDragging) _resumeKanbanPolling();
  });
}

export function init() {
  registerActions({
    'switch-agent-view': function(el, e, view) { switchAgentView(view); },
  });

  _initDragAndDrop();

  // Show the saved view preference. The poll's immediate first run loads its data, so
  // this applies the chrome only; switchAgentView() would fetch the same data twice.
  if (_agentView === 'kanban') _applyAgentView('kanban');
  visibilityAwarePoll(loadKanban, 5000);
}
