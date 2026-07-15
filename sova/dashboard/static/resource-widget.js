/* SOVA Dashboard -- Resource monitoring floating widget */

(function () {
  'use strict';

  var POLL_INTERVAL = 5000;
  var CROSS_PROJECT_POLL_INTERVAL = 10000;
  var MAX_BACKOFF = 30000;
  var STALE_THRESHOLD = 30000; // 30s before showing stale indicator
  var HISTORY_MAX = 60; // 5 minutes at 5s intervals
  var STORAGE_KEY = 'sova-resource-widget-expanded';

  var cpuHistory = [];
  var memHistory = [];
  var consecutiveErrors = 0;
  var lastSuccessTime = Date.now();
  var crossProjectData = null;
  var prevCrossProjectJSON = '';

  function init() {
    var widget = document.getElementById('resource-widget');
    if (!widget) return;

    var toggle = document.getElementById('resource-widget-toggle');
    var panel = document.getElementById('resource-widget-panel');
    if (toggle && panel) {
      toggle.addEventListener('click', function () {
        var nowHidden = panel.classList.toggle('hidden');
        localStorage.setItem(STORAGE_KEY, nowHidden ? '0' : '1');
        syncToggleState(!nowHidden);
      });
      // Restore state
      if (localStorage.getItem(STORAGE_KEY) === '1') {
        panel.classList.remove('hidden');
        syncToggleState(true);
      }
    }

    // Listen for cross-tab sync
    window.addEventListener('storage', function (e) {
      if (e.key === STORAGE_KEY && panel) {
        var show = e.newValue === '1';
        panel.classList.toggle('hidden', !show);
        syncToggleState(show);
      }
    });

    schedulePoll();
    scheduleCrossProjectPoll(true);
  }

  function getBackoffDelay() {
    if (consecutiveErrors === 0) return POLL_INTERVAL;
    return Math.min(POLL_INTERVAL * Math.pow(2, consecutiveErrors), MAX_BACKOFF);
  }

  function schedulePoll() {
    setTimeout(async function () {
      if (document.hidden) {
        schedulePoll();
        return;
      }
      await poll();
      schedulePoll();
    }, getBackoffDelay());
  }

  function syncToggleState(expanded) {
    var toggle = document.getElementById('resource-widget-toggle');
    if (!toggle) return;
    var chevron = toggle.querySelector('.resource-widget-chevron');
    if (chevron) {
      chevron.style.transform = expanded ? 'rotate(180deg)' : '';
    }
  }

  async function poll() {
    var widget = document.getElementById('resource-widget');
    if (!widget) return;

    try {
      var data = await fetchAPI(apiUrl('/resources/system/metrics'));
    } catch (_e) {
      consecutiveErrors++;
      updateStaleIndicator(widget);
      return;
    }

    consecutiveErrors = 0;
    lastSuccessTime = Date.now();

    if (!data.available) {
      widget.style.display = 'none';
      return;
    }
    widget.style.display = '';
    updateStaleIndicator(widget);

    updateIndicator(data);
    updatePanel(data);
    updateSparklines(data);
  }

  function updateStaleIndicator(widget) {
    var isStale = (Date.now() - lastSuccessTime) > STALE_THRESHOLD;
    var panel = document.getElementById('resource-widget-panel');
    if (panel) {
      panel.classList.toggle('opacity-50', isStale);
    }
    var dot = document.getElementById('resource-widget-dot');
    if (dot) {
      dot.classList.toggle('opacity-50', isStale);
    }
  }

  function updateIndicator(data) {
    var dot = document.getElementById('resource-widget-dot');
    var label = document.getElementById('resource-widget-label');
    if (!dot || !label) return;

    var cpu = data.system.cpu_percent;
    var agentCount = data.agent_slots.used;

    // Color: green < 60%, yellow 60-85%, red > 85%
    dot.className = 'resource-widget-dot ' + cpuDotClass(cpu);
    label.textContent = agentCount + ' agent' + (agentCount !== 1 ? 's' : '');
  }

  function cpuDotClass(cpu) {
    if (cpu == null || cpu < 60) return 'resource-dot-green';
    if (cpu < 85) return 'resource-dot-yellow';
    return 'resource-dot-red';
  }

  function updatePanel(data) {
    var sys = data.system;

    setText('rw-cpu-val', sys.cpu_percent != null ? sys.cpu_percent.toFixed(1) + '%' : '--');
    setText('rw-cpu-cores', sys.cpu_count != null ? sys.cpu_count + ' cores' : '');

    if (sys.memory_used_bytes != null && sys.memory_total_bytes != null) {
      setText('rw-mem-val', fmtBytes(sys.memory_used_bytes) + ' / ' + fmtBytes(sys.memory_total_bytes));
      setText('rw-mem-pct', sys.memory_percent != null ? sys.memory_percent.toFixed(1) + '%' : '');
    } else {
      setText('rw-mem-val', '--');
      setText('rw-mem-pct', '');
    }

    var loadRow = document.getElementById('rw-load-row');
    if (loadRow) {
      if (sys.load_avg) {
        loadRow.classList.remove('hidden');
        setText('rw-load-val', sys.load_avg.map(function (v) { return v.toFixed(2); }).join(', '));
      } else {
        loadRow.classList.add('hidden');
      }
    }

    setText('rw-slots-val', data.agent_slots.used + ' / ' + data.agent_slots.max);

    // Agent table
    var tbody = document.getElementById('rw-agent-tbody');
    if (tbody) {
      if (data.agents.length === 0) {
        tbody.innerHTML = '<tr><td colspan="4" class="text-center text-gray-500 py-2 text-xs">No agents running</td></tr>';
      } else {
        tbody.innerHTML = data.agents.map(function (a) {
          var cpu = a.cpu_percent != null ? a.cpu_percent.toFixed(1) + '%' : '--';
          var mem = a.memory_rss_bytes != null ? fmtBytes(a.memory_rss_bytes) : '--';
          return '<tr class="border-t border-gray-700/30">' +
            '<td class="py-1.5 px-2 text-xs">#' + escapeHtml(String(a.issue)) + '</td>' +
            '<td class="py-1.5 px-2 text-xs text-gray-400">' + escapeHtml(a.role) + '</td>' +
            '<td class="py-1.5 px-2 text-xs text-right">' + cpu + '</td>' +
            '<td class="py-1.5 px-2 text-xs text-right">' + mem + '</td>' +
            '</tr>';
        }).join('');
      }
    }
  }

  function updateSparklines(data) {
    var cpu = data.system.cpu_percent;
    var memPct = data.system.memory_percent;

    cpuHistory.push(cpu);
    memHistory.push(memPct);
    if (cpuHistory.length > HISTORY_MAX) cpuHistory.shift();
    if (memHistory.length > HISTORY_MAX) memHistory.shift();

    // Skip canvas draw when panel is collapsed
    var panel = document.getElementById('resource-widget-panel');
    if (panel && panel.classList.contains('hidden')) return;

    drawSparkline('rw-spark-cpu', cpuHistory, 'rgb(137, 180, 250)', 'rgba(137, 180, 250, 0.15)');
    drawSparkline('rw-spark-mem', memHistory, 'rgb(166, 227, 161)', 'rgba(166, 227, 161, 0.15)');
  }

  function drawSparkline(canvasId, values, strokeColor, fillColor) {
    var canvas = document.getElementById(canvasId);
    if (!canvas) return;
    var ctx = canvas.getContext('2d');
    var w = canvas.width;
    var h = canvas.height;
    var dpr = window.devicePixelRatio || 1;

    // Handle high-DPI displays
    if (canvas.dataset.dprSet !== '1') {
      canvas.width = w * dpr;
      canvas.height = h * dpr;
      canvas.style.width = w + 'px';
      canvas.style.height = h + 'px';
      ctx.scale(dpr, dpr);
      canvas.dataset.dprSet = '1';
    } else {
      w = canvas.width / dpr;
      h = canvas.height / dpr;
    }

    ctx.clearRect(0, 0, w, h);
    if (values.length < 2) return;

    var max = 100; // percentage scale
    var stepX = w / (HISTORY_MAX - 1);
    var offsetX = (HISTORY_MAX - values.length) * stepX;

    // Build segments of non-null consecutive points
    var segments = [];
    var seg = [];
    for (var i = 0; i < values.length; i++) {
      if (values[i] != null) {
        seg.push({ x: offsetX + i * stepX, y: h - (values[i] / max) * h });
      } else {
        if (seg.length >= 2) segments.push(seg);
        seg = [];
      }
    }
    if (seg.length >= 2) segments.push(seg);

    for (var s = 0; s < segments.length; s++) {
      var pts = segments[s];
      ctx.beginPath();
      ctx.moveTo(pts[0].x, pts[0].y);
      for (var j = 1; j < pts.length; j++) {
        ctx.lineTo(pts[j].x, pts[j].y);
      }
      ctx.strokeStyle = strokeColor;
      ctx.lineWidth = 1.5;
      ctx.stroke();

      // Fill area
      ctx.lineTo(pts[pts.length - 1].x, h);
      ctx.lineTo(pts[0].x, h);
      ctx.closePath();
      ctx.fillStyle = fillColor;
      ctx.fill();
    }
  }

  function scheduleCrossProjectPoll(immediate) {
    var delay = immediate ? 0 : CROSS_PROJECT_POLL_INTERVAL;
    setTimeout(async function () {
      if (!document.hidden) {
        await pollCrossProject();
      }
      scheduleCrossProjectPoll(false);
    }, delay);
  }

  async function pollCrossProject() {
    try {
      crossProjectData = await fetchAPI(apiUrl('/resources/cross-project'));
    } catch (_e) {
      crossProjectData = null;
    }
    updateMachineSection();
  }

  function updateMachineSection() {
    var section = document.getElementById('rw-machine-section');
    var content = document.getElementById('rw-machine-content');
    if (!section || !content) return;

    if (!crossProjectData || !crossProjectData.other_projects || crossProjectData.other_projects.length === 0) {
      section.classList.add('hidden');
      content.innerHTML = '';
      prevCrossProjectJSON = '';
      return;
    }

    var json = JSON.stringify(crossProjectData);
    if (json === prevCrossProjectJSON) return;
    prevCrossProjectJSON = json;

    section.classList.remove('hidden');
    var totals = crossProjectData.machine_totals;
    var others = crossProjectData.other_projects;

    var html = '<div class="flex items-center justify-between">' +
      '<span class="text-xs text-gray-500">Projects</span>' +
      '<span class="text-xs text-gray-300">' + totals.project_count + ' active</span>' +
      '</div>' +
      '<div class="flex items-center justify-between">' +
      '<span class="text-xs text-gray-500">Total agents</span>' +
      '<span class="text-xs text-gray-300">' + totals.total_agents_used + ' / ' + totals.total_agents_max + '</span>' +
      '</div>' +
      '<div class="flex items-center justify-between">' +
      '<span class="text-xs text-gray-500">Agent CPU</span>' +
      '<span class="text-xs text-gray-300">' + totals.total_agent_cpu_percent.toFixed(1) + '%</span>' +
      '</div>' +
      '<div class="flex items-center justify-between">' +
      '<span class="text-xs text-gray-500">Agent memory</span>' +
      '<span class="text-xs text-gray-300">' + fmtBytes(totals.total_agent_memory_bytes) + '</span>' +
      '</div>';

    html += '<div class="mt-1.5 pt-1.5 border-t border-gray-700/30">';
    for (var i = 0; i < others.length; i++) {
      var p = others[i];
      var slots = p.agent_slots || {};
      var agentCount = slots.used || 0;
      html += '<div class="flex items-center justify-between py-0.5">' +
        '<span class="text-xs text-gray-400 truncate max-w-[180px]">' + escapeHtml(p.project_name) + '</span>' +
        '<span class="text-xs text-gray-500">' + agentCount + ' agent' + (agentCount !== 1 ? 's' : '') + '</span>' +
        '</div>';
    }
    html += '</div>';

    content.innerHTML = html;
  }

  function setText(id, text) {
    var el = document.getElementById(id);
    if (el) el.textContent = text;
  }

  // Uses global formatBytes(bytes, compact) from app.js
  function fmtBytes(bytes) {
    return formatBytes(bytes, false);
  }

  // Initialize when DOM is ready
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
