// Work page header: agents running of the limit (with the shared minus/plus stepper), queued, needs you,
// spend today, CodeRabbit reviews free this hour and the supervisor switch.
//
// The counts come from the board payload (renderHeader, called by board.js after each load). Spend and the
// CodeRabbit quota have their own endpoints, read when the socket (re)opens and when an agent finishes, so
// this module adds no polling of its own. The limit and the switch save through settingStepper() and
// settingSwitch() (app.js), the same path as every other page control.

var _limitControl = null;
var _supervisorOn = null;
var _lastBoard = null;

function _renderAgents(board, limitIsStale) {
  var agents = (board && board.agents) || {};
  var running = agents.running;
  var limit = agents.limit;
  setText('hdr-agents-running', running == null ? '--' : String(running));
  // A save that completed while the board was loading makes the board's limit older than the stepper's.
  if (_limitControl && limit != null && !limitIsStale) _limitControl.sovaSetValue(limit);
  var over = document.getElementById('hdr-agents-over');
  if (over) over.classList.toggle('hidden', !(running != null && limit != null && running > limit));
}

function _renderQueued(board) {
  var queued = (board && board.queued) || [];
  setText('hdr-queued', board ? String(queued.length) : '--');
  var detail = '';
  if (_supervisorOn === true) detail = 'Starts as slots free';
  else if (_supervisorOn === false) detail = queued.length ? 'Waits: supervisor off' : 'Supervisor off';
  setText('hdr-queued-detail', detail);
}

function _renderNeeds(board) {
  var needs = (board && board.needs) || [];
  var yours = (board && board.yours) || [];
  var el = document.getElementById('hdr-needs');
  if (el) {
    el.textContent = board ? String(needs.length) : '--';
    el.classList.toggle('text-accent-peach', needs.length > 0);
  }
  setText('hdr-needs-detail', board ? '+ ' + yours.length + ' for you to do' : '');
}

function _renderSupervisorText() {
  setText('hdr-supervisor-label', _supervisorOn === null ? '' : (_supervisorOn ? 'On' : 'Off'));
  setText('hdr-supervisor-detail', _supervisorOn === null ? '' : (_supervisorOn ? 'Starts queued work' : 'Nothing starts by itself'));
}

// Called by board.js with every board payload (null before the first one). `limitIsStale`: a limit save
// completed while that payload was being fetched, so its limit may predate the save and is not shown.
export function renderHeader(board, limitIsStale) {
  _lastBoard = board;
  _renderAgents(board, limitIsStale);
  _renderQueued(board);
  _renderNeeds(board);
}

async function _loadSpend() {
  try {
    var overview = await fetchAPI(apiUrl('/overview'));
    var costs = overview.costs || {};
    setText('hdr-spend', '$' + parseFloat(costs.today_cost_usd || 0).toFixed(2));
    setText('hdr-spend-detail', '$' + parseFloat(costs.rolling_7d_usd || 0).toFixed(0) + ' in 7 days');
  } catch (e) {
    // Never leave an earlier figure looking current: say it is unknown.
    setText('hdr-spend', '--');
    setText('hdr-spend-detail', 'Could not load spend');
  }
}

async function _loadCodeRabbit() {
  try {
    var q = await fetchAPI(apiUrl('/quota/coderabbit'));
    if (!q.enabled) {
      setText('hdr-coderabbit', '--');
      setText('hdr-coderabbit-of', '');
      setText('hdr-coderabbit-detail', 'Quota tracking off');
      return;
    }
    var per = q.reviews_per_hour || 0;
    var free = Math.max(per - (q.reviews_in_window || 0), 0);
    setText('hdr-coderabbit', String(free));
    setText('hdr-coderabbit-of', 'of ' + per + ' free');
    setText('hdr-coderabbit-detail', free === 0 && q.next_available_minutes != null
      ? 'next in ' + q.next_available_minutes + ' min'
      : 'reviews this hour');
  } catch (e) {
    setText('hdr-coderabbit', '--');
    setText('hdr-coderabbit-of', '');
    setText('hdr-coderabbit-detail', 'Could not load the quota');
  }
}

function _refreshSideFigures() {
  _loadSpend();
  _loadCodeRabbit();
}

export function init() {
  var limitHost = document.getElementById('hdr-agents-limit');
  if (limitHost) {
    _limitControl = settingStepper('max_parallel_agents', {
      label: 'Agent limit',
      min: 1,
      step: 1,
      tooltip: 'Agents allowed to run at once. The next start uses it; lowering it stops no running agent.',
      onSaved: function() { renderHeader(_lastBoard, true); },
    });
    limitHost.appendChild(_limitControl);
  }
  var switchHost = document.getElementById('hdr-supervisor-switch');
  if (switchHost) {
    switchHost.appendChild(settingSwitch('supervisor.enabled', {
      label: 'Supervisor',
      tooltip: 'Turn the supervisor on or off. On, it starts queued issues as slots free.',
      tooltipPos: 'bottom',
      onValue: function(on) {
        _supervisorOn = on;
        _renderSupervisorText();
        _renderQueued(_lastBoard);
      },
    }));
  }
  renderHeader(null);
  subscribeSovaSocket({
    onOpen: _refreshSideFigures,
    onFallbackPoll: _refreshSideFigures,
    onMessage: function(msg) {
      if (msg.type === 'agent_finished') _refreshSideFigures();
    },
  });
}
