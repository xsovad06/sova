// A priority picker for one issue: a button showing the current level's icon and name that opens a menu of the
// levels (each with its icon, the current one checked). Choosing one saves through PUT /api/issues/{number}/priority,
// shows that it is saving, and shows the API's own error message when the save fails (for example a Jira project
// with no matching level), returning to the stored value.
//
// It holds nothing page-specific: a page passes the issue, its current priority and what to do after a save.
// While its menu is open, or a save is in flight, the picker carries data-open or data-saving, so a page can
// hold a redraw that would replace it; onIdle tells the page when it may redraw (the menu closed without a
// save, or a save ended), and onFailed carries a failed save's message to a picker the page draws afresh.

var LEVELS = ['blocker', 'critical', 'high', 'medium', 'low'];

function _icon(level) {
  var img = document.createElement('img');
  img.src = priorityIconUrl(level);
  img.alt = '';
  img.width = 16;
  img.height = 16;
  return img;
}

function _label(level) {
  return level ? priorityDisplayName(level) : 'No priority';
}

// opts: { issue, value, error, onSaved(value), onFailed(message), onIdle(), iconHtml(name) }. iconHtml, when given, returns a named icon's SVG markup
// (the page's own icon set), which draws the button's chevron and the current level's check. Returns the element.
export function createPriorityPicker(opts) {
  var issue = opts.issue;
  var iconHtml = opts.iconHtml || function() { return ''; };
  var stored = (opts.value || '').toLowerCase();
  var levels = LEVELS.slice();
  if (stored && levels.indexOf(stored) === -1) levels.push(stored);

  var wrap = document.createElement('div');
  wrap.className = 'sova-priority-picker flex flex-col gap-1';

  var row = document.createElement('div');
  row.className = 'relative flex items-center gap-2';

  var button = document.createElement('button');
  button.type = 'button';
  button.className = 'sova-priority-picker-btn';
  button.setAttribute('aria-haspopup', 'listbox');
  button.setAttribute('aria-expanded', 'false');
  button.setAttribute('aria-label', 'Priority of #' + issue);

  var menu = document.createElement('ul');
  menu.className = 'sova-priority-picker-menu';
  menu.setAttribute('role', 'listbox');
  menu.setAttribute('aria-label', 'Priority of #' + issue);
  menu.hidden = true;

  var status = document.createElement('span');
  status.className = 'text-xs text-gray-500';
  status.setAttribute('role', 'status');

  var error = document.createElement('p');
  error.className = 'hidden text-xs text-accent-red';
  error.setAttribute('role', 'alert');
  if (opts.error) {
    error.textContent = opts.error;
    error.classList.remove('hidden');
  }

  function drawButton() {
    button.textContent = '';
    if (stored) button.appendChild(_icon(stored));
    var name = document.createElement('span');
    name.textContent = _label(stored);
    button.appendChild(name);
    var chevron = document.createElement('span');
    chevron.className = 'sova-priority-picker-chevron';
    chevron.setAttribute('aria-hidden', 'true');
    chevron.innerHTML = iconHtml('chevron-down');
    button.appendChild(chevron);
  }

  function drawMenu() {
    menu.textContent = '';
    levels.forEach(function(level) {
      var item = document.createElement('li');
      item.setAttribute('role', 'option');
      item.setAttribute('aria-selected', level === stored ? 'true' : 'false');
      item.dataset.level = level;
      item.tabIndex = -1;
      item.className = 'sova-priority-picker-option';
      item.appendChild(_icon(level));
      var name = document.createElement('span');
      name.textContent = _label(level);
      item.appendChild(name);
      var check = document.createElement('span');
      check.className = 'sova-priority-picker-check';
      check.setAttribute('aria-hidden', 'true');
      if (level === stored) check.innerHTML = iconHtml('check');
      item.appendChild(check);
      menu.appendChild(item);
    });
  }

  function options() { return Array.prototype.slice.call(menu.querySelectorAll('[role="option"]')); }

  function onOutside(e) {
    if (!wrap.contains(e.target)) {
      close(false);
      idle();
    }
  }

  function open() {
    drawMenu();
    menu.hidden = false;
    wrap.dataset.open = 'true';
    button.setAttribute('aria-expanded', 'true');
    var items = options();
    var current = items.filter(function(i) { return i.dataset.level === stored; })[0] || items[0];
    if (current) current.focus();
    document.addEventListener('mousedown', onOutside, true);
  }

  function close(refocus) {
    if (menu.hidden) return;
    menu.hidden = true;
    delete wrap.dataset.open;
    button.setAttribute('aria-expanded', 'false');
    document.removeEventListener('mousedown', onOutside, true);
    if (refocus) button.focus();
  }

  // The picker has nothing in flight: a page holding a redraw for it may apply it now.
  function idle() {
    if (opts.onIdle && !wrap.dataset.open && !wrap.dataset.saving) opts.onIdle();
  }

  async function save(wanted) {
    close(true);
    if (!wanted || wanted === stored) {
      idle();
      return;
    }
    var previous = stored;
    stored = wanted;
    drawButton();
    wrap.dataset.saving = 'true';
    button.disabled = true;
    status.textContent = 'Saving...';
    error.classList.add('hidden');
    try {
      var res = await fetch(apiUrl('/issues/' + encodeURIComponent(issue) + '/priority'), {
        method: 'PUT',
        headers: jsonCsrfHeaders(),
        body: JSON.stringify({ priority: wanted }),
      });
      if (!res.ok) throw new Error(await getErrorDetail(res, 'Could not change the priority of #' + issue));
      status.textContent = 'Saved';
      if (opts.onSaved) opts.onSaved(wanted);
    } catch (e) {
      stored = previous;
      drawButton();
      status.textContent = '';
      error.textContent = (e && e.message) || 'Could not change the priority of #' + issue;
      error.classList.remove('hidden');
      if (opts.onFailed) opts.onFailed(error.textContent);
    } finally {
      button.disabled = false;
      delete wrap.dataset.saving;
      idle();
    }
  }

  button.addEventListener('click', function(e) {
    e.stopPropagation();
    if (menu.hidden) {
      open();
    } else {
      close(true);
      idle();
    }
  });
  button.addEventListener('keydown', function(e) {
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') { e.preventDefault(); open(); }
  });
  menu.addEventListener('click', function(e) {
    e.stopPropagation();
    var item = e.target.closest('[role="option"]');
    if (item) save(item.dataset.level);
  });
  menu.addEventListener('keydown', function(e) {
    var items = options();
    var at = items.indexOf(document.activeElement);
    if (e.key === 'ArrowDown') { e.preventDefault(); items[Math.min(at + 1, items.length - 1)].focus(); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); items[Math.max(at - 1, 0)].focus(); }
    else if (e.key === 'Home') { e.preventDefault(); items[0].focus(); }
    else if (e.key === 'End') { e.preventDefault(); items[items.length - 1].focus(); }
    else if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); if (at >= 0) save(items[at].dataset.level); }
    else if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); close(true); idle(); }
    else if (e.key === 'Tab') { close(false); idle(); }
  });

  drawButton();
  row.appendChild(button);
  row.appendChild(menu);
  row.appendChild(status);
  wrap.appendChild(row);
  wrap.appendChild(error);
  return wrap;
}
