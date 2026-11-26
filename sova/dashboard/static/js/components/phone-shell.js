// The phone shell under 768 px (base.html, docs/design-system.md "Phone Shell"): the top bar's menu button opens the
// More sheet, a shared side panel (side-panel.js) that rises from the bottom, so it closes like the feed and the
// ticket detail (Escape, its close button, a tap on the scrim) and only one sheet is open at a time.
//
// The sidebar is hidden on a phone, so its account row (the provider status) and the floating agents widget (the
// system resources) move into the More sheet while the window is phone-sized and back when it widens. Their own
// scripts (provider-status-widget.js, resource-widget.js) keep working: a moved element keeps its listeners and id.

import { createSidePanel } from './side-panel.js';

// The same breakpoint as the `@media (max-width: 767.98px)` phone block in style.css.
export var PHONE_QUERY = '(max-width: 767.98px)';

var _sheet = null;
var _moves = [];

// An element that moves into a sheet on a phone: where it lives on a wide screen (a marker left in its place) and
// its slot in the sheet. Null when either is missing. The Supervisor's graph sheet moves its graph the same way.
export function trackMove(id, slotId) {
  var el = document.getElementById(id);
  var slot = document.getElementById(slotId);
  if (!el || !slot || !el.parentNode) return null;
  var marker = document.createComment(' ' + id + ' lives here on a wide screen ');
  el.parentNode.insertBefore(marker, el);
  return { el: el, slot: slot, marker: marker };
}

// Puts each tracked element in its sheet slot on a phone, and back in its own place on a wide screen.
export function placeMoves(moves, phone) {
  moves.forEach(function(m) {
    if (phone) {
      if (m.el.parentNode !== m.slot) m.slot.appendChild(m.el);
    } else if (m.el.previousSibling !== m.marker) {
      m.marker.parentNode.insertBefore(m.el, m.marker.nextSibling);
    }
  });
}

export function isOpen() { return !!(_sheet && _sheet.isOpen()); }

export function init() {
  var sheetEl = document.getElementById('more-sheet');
  var menu = document.getElementById('phone-menu-btn');
  if (!sheetEl || !menu) return;
  _sheet = createSidePanel(sheetEl, {
    name: 'more',
    floats: 'hide',
    trigger: menu,
    returnFocus: function() { return menu; },
  });
  menu.addEventListener('click', function() { _sheet.toggle(); });

  _moves = [trackMove('auth-widget', 'more-sheet-provider-slot'), trackMove('resource-widget', 'more-sheet-resources-slot')]
    .filter(Boolean);
  var query = typeof window.matchMedia === 'function' ? window.matchMedia(PHONE_QUERY) : null;
  function apply() {
    var phone = !!(query && query.matches);
    placeMoves(_moves, phone);
    // A window widened past the phone breakpoint shows the sidebar, which lists every page: the sheet has no place.
    if (!phone && _sheet.isOpen()) _sheet.close('wide');
  }
  apply();
  if (query && query.addEventListener) query.addEventListener('change', apply);
}
