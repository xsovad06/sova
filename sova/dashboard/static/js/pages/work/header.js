// Work page header: agents running of the limit (with the shared minus/plus stepper), run success and merged
// PRs over 7 days, spend today, CodeRabbit reviews available this hour (only on a project that uses CodeRabbit)
// and the supervisor switch.
//
// The running count comes from the board payload (renderHeader, called by board.js after each load). The
// success, merged and spend figures come from GET /overview/stats and the CodeRabbit quota from its own
// endpoint, both read when the socket (re)opens and when an agent finishes, so this module adds no polling of
// its own: the quota endpoint refreshes a stale count in the background. The limit and the two switches save
// through settingStepper() and settingSwitch() (app.js), the same path as every other page control.

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

// A week-over-week comparison: the words carry the meaning, the colour only repeats it. A missing previous
// figure gives no comparison (no text, no tone), and an equal one or a zero one (nothing to measure a change
// against) stays neutral. `higherIsBetter` decides which direction is green. Callers pass figures already rounded
// as they are shown. Without `suffix` the phrase names the direction ("up from $10.29"); with one it is the
// previous figure and the suffix ("93% the week before").
export function trend(current, previous, higherIsBetter, format, suffix) {
  if (previous == null || current == null) return { text: '', tone: '' };
  var word = suffix ? '' : (current > previous ? 'up from ' : (current < previous ? 'down from ' : 'same as '));
  var tone = '';
  if (previous !== 0 && current !== previous) tone = ((current > previous) === higherIsBetter) ? 'good' : 'bad';
  return { text: word + format(previous) + (suffix || ''), tone: tone };
}

var _TONE_CLASSES = { good: 'sova-tone-good', bad: 'sova-tone-bad' };

function _setTone(id, tone) {
  var el = document.getElementById(id);
  if (!el) return;
  Object.keys(_TONE_CLASSES).forEach(function(key) { el.classList.toggle(_TONE_CLASSES[key], key === tone); });
}

// A line of text and, after a comma, a comparison phrase: only the phrase sits in the toned `<id>-trend` span, so
// the counts and amounts beside it stay neutral. No comparison leaves no comma and an empty span.
function _setLine(id, text, change) {
  var cmp = change || { text: '', tone: '' };
  setText(id + '-text', text && cmp.text ? text + ', ' : text);
  setText(id + '-trend', cmp.text);
  _setTone(id + '-trend', cmp.tone);
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
  // No prior week is no comparison, not a comparison with $0.
  var previous = spend.previous_week_usd == null ? null : Math.round(spend.previous_week_usd);
  setText('hdr-spend', _usd(spend.today_usd, 2));
  _setLine('hdr-spend-detail', _usd(week, 0) + ' in 7 days',
    trend(week, previous, false, function(v) { return _usd(v, 0); }));
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
  setText('hdr-success', week.success_rate == null ? '--' : _pct(week.success_rate) + '%');
  setText('hdr-success-count', week.success + ' of ' + week.rated + ' runs');
  if (week.success_rate == null) {
    _setLine('hdr-success-detail', 'too few finished runs for a rate');
  } else if (previous.success_rate == null) {
    _setLine('hdr-success-detail', '');
  } else {
    _setLine('hdr-success-detail', '',
      trend(_pct(week.success_rate), _pct(previous.success_rate), true, function(v) { return v + '%'; }, ' the week before'));
  }
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
    _setLine('hdr-merged-count', '');
    _setLine('hdr-merged-detail', 'GitHub could not be reached');
    _setTip('hdr-merged-tile', ['GitHub could not be reached, so the merged PRs are not counted here']);
    return;
  }
  if (merged.status !== 'ok') return;
  setText('hdr-merged', String(merged.count));
  _setLine('hdr-merged-count', merged.count === 1 ? 'PR' : 'PRs',
    trend(merged.count, merged.previous_count, true, String, ' the week before'));
  if (merged.cost_per_pr_usd == null) {
    _setLine('hdr-merged-detail', 'Cost per PR n/a');
  } else {
    // Rounded to cents before comparing, as shown.
    var current = Math.round(merged.cost_per_pr_usd * 100) / 100;
    var before = merged.previous_cost_per_pr_usd == null ? null : Math.round(merged.previous_cost_per_pr_usd * 100) / 100;
    _setLine('hdr-merged-detail', _usd(current, 2) + ' per PR',
      trend(current, before, false, function(v) { return _usd(v, 2); }));
  }
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
    _setLine('hdr-spend-detail', 'Could not load spend');
    setText('hdr-success', '--');
    setText('hdr-success-count', '');
    _setLine('hdr-success-detail', 'Could not load');
    setText('hdr-merged', '--');
    _setLine('hdr-merged-count', '');
    _setLine('hdr-merged-detail', 'Could not load');
    ['hdr-spend-tile', 'hdr-success-tile', 'hdr-merged-tile'].forEach(function(id) { _setTip(id, []); });
  }
}

function _crTooltip(q, limit) {
  var stars = q.stars;
  // oss grows with the stars, so "from 10 stars" is a floor there, not the limit.
  var fromTen = 'From 10 stars: auto-review and '
    + (q.limit_scales_with_stars ? 'one an hour per 10 stars, up to 10' : q.limit_from_10_stars + ' an hour');
  var perHour = limit === 0 ? 'with no limit set' : limit + ' an hour';
  var rule;
  if (q.limit_is_explicit) {
    // The operator's own figure wins over the star rule, so state it and keep the rule only as context.
    var context = q.auto_review_eligible ? 'CodeRabbit reviews every PR by itself'
      : (stars == null ? 'The star count is unknown' : 'The repo has ' + stars + ' stars') + '. Below 10 stars CodeRabbit does not auto-review';
    rule = 'Limit set in settings: ' + (limit === 0 ? 'no limit' : limit + ' an hour') + '. ' + context;
  } else if (q.auto_review_eligible && !(stars != null && stars >= 10)) {
    rule = 'Your CodeRabbit plan reviews every PR by itself, ' + perHour;
  } else if (q.auto_review_eligible) {
    // The limit in force, not the plan's 10-star figure: on oss it grows with the stars.
    rule = 'The repo has ' + stars + ' stars, so CodeRabbit reviews every PR by itself, ' + perHour;
  } else if (stars == null) {
    rule = 'The star count is unknown, so the rule for fewer than 10 stars applies: no auto-review, one '
      + '@coderabbitai review an hour. ' + fromTen;
  } else {
    rule = 'The repo has ' + stars + ' stars. Below 10: no auto-review, one @coderabbitai review an hour. ' + fromTen;
  }
  return [rule, 'Ask on new PRs posts @coderabbitai review on each PR SOVA opens.'];
}

// Remaining capacity, never consumption: a full bar means reviews are available, an empty one means used up, and
// a limit of 0 (unlimited) is always full. A project that lists CodeRabbit but tracks no quota keeps the tile
// and its switch; one that does not list it has no tile.
function _renderCodeRabbit(q) {
  var tile = document.getElementById('hdr-coderabbit-tile');
  if (tile) tile.hidden = !q.configured;
  if (!q.configured) return;
  var number = document.getElementById('hdr-coderabbit');
  var bar = document.getElementById('hdr-coderabbit-bar');
  if (number) number.hidden = !q.enabled;
  if (bar) bar.hidden = !q.enabled;
  if (!q.enabled) {
    setText('hdr-coderabbit-of', 'Not tracked, SOVA does not wait for a free review');
    _setTip('hdr-coderabbit-tile', ['Ask on new PRs posts @coderabbitai review on each PR SOVA opens.']);
    return;
  }
  var limit = q.reviews_per_hour || 0;
  var left = limit === 0 ? 0 : Math.max(limit - (q.reviews_in_window || 0), 0);
  var usedUp = limit !== 0 && left === 0;
  setText('hdr-coderabbit', limit === 0 ? 'No limit' : String(left));
  if (limit === 0) {
    setText('hdr-coderabbit-of', '');
  } else if (usedUp) {
    setText('hdr-coderabbit-of', 'of ' + limit + (q.next_available_minutes == null ? '' : ', next in ' + Math.ceil(q.next_available_minutes) + ' min'));
  } else {
    setText('hdr-coderabbit-of', 'of ' + limit + ' available' + (q.auto_review_eligible ? ', auto-review' : ' this hour'));
  }
  if (number) number.classList.toggle('text-accent-peach', usedUp);
  if (bar) {
    bar.classList.toggle('is-used-up', usedUp);
    bar.firstElementChild.style.width = (limit === 0 ? 100 : Math.round(100 * left / limit)) + '%';
  }
  _setTip('hdr-coderabbit-tile', _crTooltip(q, limit));
}

// The quota endpoint answers from the stored count and refreshes a stale one in the background, with no completion
// event, so a reply marked "refreshing" is read again shortly after. Bounded, so a sync that keeps failing stops
// being asked about after a few reads.
var _CR_REREAD_MS = 8000;
var _CR_MAX_REREADS = 3;
var _crRereads = 0;
var _crRereadTimer = null;

async function _loadCodeRabbit() {
  try {
    var q = await fetchAPI(apiUrl('/quota/coderabbit'));
    _renderCodeRabbit(q);
    if (_crRereadTimer !== null) { clearTimeout(_crRereadTimer); _crRereadTimer = null; }
    if (!q.refreshing) {
      _crRereads = 0;
    } else if (_crRereads < _CR_MAX_REREADS) {
      _crRereads += 1;
      _crRereadTimer = setTimeout(function() { _crRereadTimer = null; _loadCodeRabbit(); }, _CR_REREAD_MS);
    }
  } catch (e) {
    // The tile keeps its visibility, but an earlier figure, bar and rule must not stay up as if they were current.
    var number = document.getElementById('hdr-coderabbit');
    if (number) number.classList.toggle('text-accent-peach', false);
    var bar = document.getElementById('hdr-coderabbit-bar');
    if (bar) bar.hidden = true;
    setText('hdr-coderabbit', '--');
    setText('hdr-coderabbit-of', 'Could not load the quota');
    _setTip('hdr-coderabbit-tile', []);
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
  var triggerHost = document.getElementById('hdr-coderabbit-switch');
  if (triggerHost) {
    triggerHost.appendChild(settingSwitch('external_reviews.coderabbit.trigger_review', {
      label: 'Ask on new PRs',
      tooltip: 'Posts @coderabbitai review on each PR SOVA opens. Runs started after a change use the new setting.',
      tooltipPos: 'bottom',
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
