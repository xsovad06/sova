// Work page header: agents running of the limit (with the shared minus/plus stepper), run success and merged
// PRs over 7 days, spend today, CodeRabbit reviews free this hour and the supervisor switch.
//
// The running count comes from the board payload (renderHeader, called by board.js after each load). The
// success, merged and spend figures come from GET /overview/stats and the CodeRabbit quota from its own
// endpoint, both read when the socket (re)opens and when an agent finishes, so this module adds no polling of
// its own. The limit and the switch save through settingStepper() and settingSwitch() (app.js), the same path
// as every other page control.

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

function _renderSupervisorText() {
  setText('hdr-supervisor-label', _supervisorOn === null ? '' : (_supervisorOn ? 'On' : 'Off'));
  setText('hdr-supervisor-detail', _supervisorOn === null ? '' : (_supervisorOn ? 'Starts queued work' : 'Nothing starts by itself'));
}

// Called by board.js with every board payload (null before the first one). `limitIsStale`: a limit save
// completed while that payload was being fetched, so its limit may predate the save and is not shown.
export function renderHeader(board, limitIsStale) {
  _lastBoard = board;
  _renderAgents(board, limitIsStale);
}

var _TONE_CLASSES = { good: 'text-accent-green', bad: 'text-accent-peach' };

// A week-over-week comparison: the words carry the meaning, the colour only repeats it. A missing previous
// figure gives no comparison, and an equal one or a zero one (nothing to measure a change against) stays neutral.
// `higherIsBetter` decides which direction is green. Callers pass figures already rounded as they are shown.
export function trend(current, previous, higherIsBetter, format) {
  if (previous == null || current == null) return { text: '', tone: '' };
  var word = current > previous ? 'up from ' : (current < previous ? 'down from ' : 'same as ');
  var tone = '';
  if (previous !== 0 && current !== previous) tone = ((current > previous) === higherIsBetter) ? 'good' : 'bad';
  return { text: word + format(previous) + ' the week before', tone: tone };
}

function _setTone(id, tone) {
  var el = document.getElementById(id);
  if (!el) return;
  Object.keys(_TONE_CLASSES).forEach(function(key) { el.classList.toggle(_TONE_CLASSES[key], key === tone); });
}

function _setTip(id, lines) {
  var el = document.getElementById(id);
  if (!el) return;
  var text = lines.filter(function(line) { return line; }).join('\n');
  if (text) el.setAttribute('data-tooltip', text);
  else el.removeAttribute('data-tooltip');
}

function _usd(value, digits) {
  var n = parseFloat(value || 0);
  return '$' + n.toLocaleString('en-US', { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

function _pct(rate) {
  return Math.round(rate * 100);
}

function _plural(n, word) {
  return n + ' ' + word + (n === 1 ? '' : 's');
}

function _reasonList(reasons) {
  return Object.keys(reasons || {}).map(function(key) { return key.replace(/_/g, ' ') + ' (' + reasons[key] + ')'; }).join(', ');
}

function _rateText(summary) {
  return summary.success_rate == null ? summary.success + ' of ' + summary.rated : _pct(summary.success_rate) + '%';
}

function _renderSpend(stats) {
  var spend = stats.spend || {};
  var week = Math.round(spend.week_usd || 0);
  var previous = Math.round(spend.previous_week_usd || 0);
  var change = trend(week, previous, false, function(v) { return _usd(v, 0); });
  setText('hdr-spend', _usd(spend.today_usd, 2));
  setText('hdr-spend-detail', _usd(week, 0) + ' in 7 days' + (change.text ? ', ' + change.text : ''));
  _setTone('hdr-spend-detail', change.tone);
  var zone = stats.timezone || {};
  _setTip('hdr-spend-tile', [
    'Today starts at midnight, your time (' + (zone.abbreviation || 'UTC') + ')',
    'Last 30 days: ' + _usd(spend.month_usd, 0) + ', about ' + _usd(spend.month_daily_usd, 0) + ' a day',
    // Database-only, so it shows whatever state GitHub is in, and on a project with no merged tile.
    spend.unfinished_share == null ? '' : 'Spent on runs that failed, were paused or were stopped: ' + _usd(spend.unfinished_usd, 0)
      + ' (' + _pct(spend.unfinished_share) + '%)',
  ]);
}

function _renderSuccess(stats) {
  var outcomes = stats.outcomes || {};
  var week = outcomes.week || {};
  var previous = outcomes.previous_week || {};
  var month = outcomes.month || {};
  var developer = outcomes.developer || {};
  var devWeek = developer.week || {};
  var devPrevious = developer.previous_week || {};
  var counts = week.success + ' of ' + week.rated + ' runs';
  var change = { text: '', tone: '' };
  setText('hdr-success', _rateText(week));
  if (week.success_rate == null) {
    setText('hdr-success-detail', 'too few finished runs for a rate');
  } else if (previous.success_rate == null) {
    setText('hdr-success-detail', counts);
  } else {
    change = trend(_pct(week.success_rate), _pct(previous.success_rate), true, function(v) { return v + '%'; });
    setText('hdr-success-detail', counts + ', ' + change.text);
  }
  _setTone('hdr-success-detail', change.tone);
  var reasons = _reasonList(outcomes.paused_reasons);
  _setTip('hdr-success-tile', [
    'Developer runs: ' + _rateText(devWeek)
      + (devWeek.success_rate == null ? '' : ' (' + devWeek.success + ' of ' + devWeek.rated + ')')
      + (devPrevious.success_rate == null ? '' : ', ' + _pct(devPrevious.success_rate) + '% the week before'),
    'Left out of the rate: ' + week.stopped_by_user + ' stopped by you, ' + week.paused_by_sova + ' paused by SOVA'
      + (reasons ? ': ' + reasons : ''),
    '30 days: ' + _rateText(month),
  ]);
}

function _renderMerged(stats) {
  var merged = stats.merged || {};
  var tile = document.getElementById('hdr-merged-tile');
  // A project with no GitHub repo has no merged PRs to count: the tile goes, and the row reflows. The hidden
  // attribute, not Tailwind's .hidden, which .sova-work-stat's display: flex (style.css loads later) overrides.
  if (tile) tile.hidden = merged.status === 'unsupported';
  if (merged.status === 'unavailable') {
    setText('hdr-merged', 'unknown');
    setText('hdr-merged-detail', 'GitHub could not be reached');
    setText('hdr-merged-cost', 'Cost per PR unknown');
    _setTone('hdr-merged-detail', '');
    _setTone('hdr-merged-cost', '');
    _setTip('hdr-merged-tile', ['GitHub could not be reached, so the merged PRs are not counted here']);
    return;
  }
  if (merged.status !== 'ok') return;
  var change = trend(merged.count, merged.previous_count, true, String);
  setText('hdr-merged', String(merged.count));
  setText('hdr-merged-detail', change.text);
  _setTone('hdr-merged-detail', change.tone);
  var cost = { text: '', tone: '' };
  if (merged.cost_per_pr_usd == null) {
    setText('hdr-merged-cost', 'Cost per PR n/a');
  } else {
    // Rounded to cents before comparing, as shown.
    var current = Math.round(merged.cost_per_pr_usd * 100) / 100;
    var before = merged.previous_cost_per_pr_usd == null ? null : Math.round(merged.previous_cost_per_pr_usd * 100) / 100;
    cost = trend(current, before, false, function(v) { return _usd(v, 2); });
    setText('hdr-merged-cost', _usd(current, 2) + ' per PR' + (cost.text ? ', ' + cost.text : ''));
  }
  _setTone('hdr-merged-cost', cost.tone);
  _setTip('hdr-merged-tile', [
    merged.with_sova_run + ' of the ' + _plural(merged.count, 'PR') + ' merged in the last 7 days had a SOVA run',
    merged.bots_left_out ? _plural(merged.bots_left_out, 'bot PR') + ' left out' : '',
  ]);
}

export function renderStats(stats) {
  _renderSpend(stats);
  _renderSuccess(stats);
  _renderMerged(stats);
}

function _localZone() {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
  } catch (e) {
    return 'UTC';
  }
}

async function _loadStats() {
  try {
    renderStats(await fetchAPI(apiUrl('/overview/stats?tz=' + encodeURIComponent(_localZone()))));
  } catch (e) {
    // Never leave an earlier figure looking current: say it is unknown.
    setText('hdr-spend', '--');
    setText('hdr-spend-detail', 'Could not load spend');
    setText('hdr-success', '--');
    setText('hdr-success-detail', 'Could not load');
    setText('hdr-merged', '--');
    setText('hdr-merged-detail', 'Could not load');
    setText('hdr-merged-cost', '');
    ['hdr-spend-detail', 'hdr-success-detail', 'hdr-merged-detail', 'hdr-merged-cost'].forEach(function(id) { _setTone(id, ''); });
    ['hdr-spend-tile', 'hdr-success-tile', 'hdr-merged-tile'].forEach(function(id) { _setTip(id, []); });
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
  _loadStats();
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
