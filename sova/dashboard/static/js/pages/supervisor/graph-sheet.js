// The Supervisor's dependency graph on a phone (templates/supervisor.html, docs/design-system.md "Phone Shell"): a
// full-screen sheet on the shared side panel, opened from the "Dependency graph" row.
//
// The graph card and the node drawer move into the sheet while the window is phone-sized and back to their places
// when it widens, the way phone-shell.js moves the sidebar's widgets into the More sheet; the page script keeps
// working on them by id. d3's zoom gives the graph its pinch zoom and drag; a tap on a node is its click, which
// highlights the chain and opens the drawer at the bottom of the sheet.

import { createSidePanel } from '../../components/side-panel.js';
import { PHONE_QUERY, trackMove, placeMoves } from '../../components/phone-shell.js';

var _sheet = null;

export function isOpen() { return !!(_sheet && _sheet.isOpen()); }

// `refit` redraws the graph to fit its container: called when the sheet opens and whenever the graph moved, since
// its container then has another size.
export function init(options) {
  var opts = options || {};
  var refit = typeof opts.refit === 'function' ? opts.refit : function() {};
  var sheetEl = document.getElementById('graph-sheet');
  var openBtn = document.getElementById('graph-open-btn');
  if (!sheetEl || !openBtn) return;
  _sheet = createSidePanel(sheetEl, {
    name: 'graph',
    floats: 'hide',
    trigger: openBtn,
    returnFocus: function() { return openBtn; },
    onOpen: refit,
  });
  openBtn.addEventListener('click', function() { _sheet.open(); });

  var moves = [trackMove('graph-card', 'graph-sheet-slot'), trackMove('action-drawer', 'graph-sheet-slot')]
    .filter(Boolean);
  var query = typeof window.matchMedia === 'function' ? window.matchMedia(PHONE_QUERY) : null;
  var placedPhone = null;
  function apply() {
    var phone = !!(query && query.matches);
    // The page shows the graph again on a wide screen, so the sheet has no place there.
    if (!phone && _sheet.isOpen()) _sheet.close('wide');
    if (phone === placedPhone) return;
    placedPhone = phone;
    placeMoves(moves, phone);
    refit();
  }
  apply();
  if (query && query.addEventListener) query.addEventListener('change', apply);
}
