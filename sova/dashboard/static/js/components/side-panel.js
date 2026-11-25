// The shared side panel: a panel fixed to the right edge of the window, over the page (the activity feed, the Work
// ticket detail). Every panel on it behaves the same:
//
// - Only one is open at a time: opening one closes the other first (reason 'replaced').
// - It closes on Escape (unless a dialog owns the key), on any element inside it marked data-panel-close, and on a
//   click outside it. A click on an overlay over it (a modal, a confirm, a toast, any [role="dialog"]) or on an
//   element marked data-keeps-panel is not outside it, nor is one on the panel's own `keepOpen` selector, nor a
//   press that began inside it (a text selection released outside).
// - Escape and the close button return focus: to `returnFocus()` when the panel names a target, else to what had
//   focus outside the panel when it last opened or switched content (an open() on the open panel). Focus left in a
//   page field outside the panel stays there.
// - A closing panel is `inert` at once: its content stays visible while it slides out, but no Tab or click reaches
//   a ticket that is already gone.
// - A wheel over the panel never scrolls the page behind it: the panel scrolls what can scroll under the pointer,
//   and a wheel over its header or past the end of its list is stopped (`overscroll-behavior: contain` in the CSS
//   covers the momentum of a trackpad at either end).
// - Motion is CSS (`.sova-side-panel` and `[data-open]` in style.css): it slides in with an ease-out curve and out
//   with an ease-in one, and not at all under prefers-reduced-motion.
//
// While a panel is open the body carries `data-side-panel="<name>"`, `data-side-panel-floats="shift|hide"` and the
// panel's width as `--side-panel-width`, which the floating owl button and agents widget read to move left of the
// panel ('shift') or hide behind it ('hide').

var _ALWAYS_KEEPS = '.sova-modal-backdrop, .sova-toast, [role="dialog"], [aria-modal="true"], [data-keeps-panel]';

var _open = null;          // the open panel's record, null for none
var _pressInside = false;  // the current press began inside the open panel
var _bound = false;
var _resizeObserver = null;

function _setBodyState(rec) {
  var body = document.body;
  if (!body) return;
  if (rec) {
    body.setAttribute('data-side-panel', rec.name);
    body.setAttribute('data-side-panel-floats', rec.floats);
    _syncWidth();
  } else {
    body.removeAttribute('data-side-panel');
    body.removeAttribute('data-side-panel-floats');
    if (body.style && body.style.removeProperty) body.style.removeProperty('--side-panel-width');
  }
}

// The open panel's laid-out width (offsetWidth ignores the slide transform), for the floats to move by.
function _syncWidth() {
  var body = document.body;
  if (!_open || !body || !body.style || !body.style.setProperty) return;
  var width = _open.el.offsetWidth;
  if (typeof width === 'number') body.style.setProperty('--side-panel-width', width + 'px');
}

// A dialog owns Escape while it is open. The shared modal (sovaModalOpen) stops the key before it gets here; any
// other dialog does not, so the panel checks for open dialogs itself. A closed dialog can stay in the DOM
// (aria-hidden, off-screen or display:none), so only a rendered one counts.
function _dialogOpen() {
  if (typeof sovaModalIsOpen === 'function' && sovaModalIsOpen()) return true;
  var found = document.querySelectorAll('dialog[open], [role="dialog"], [aria-modal="true"]');
  return Array.prototype.some.call(found, function(el) {
    return !(_open && _open.el.contains(el)) && !(el.classList && el.classList.contains('hidden')) &&
      el.getAttribute('aria-hidden') !== 'true' && el.getClientRects().length > 0;
  });
}

function _editableOutside(rec) {
  var active = document.activeElement;
  return !!active && !rec.el.contains(active) &&
    (active.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(active.tagName || ''));
}

// What has focus outside `el`, the element focus returns to when nothing better is named; null for none (the body
// is where focus lands when its element was removed, not a target).
function _outsideFocus(el) {
  var active = document.activeElement;
  return active && active !== document.body && !el.contains(active) ? active : null;
}

function _setInert(el, on) {
  if (!el || !el.setAttribute) return;
  if (on) el.setAttribute('inert', '');
  else el.removeAttribute('inert');
}

function _focusTarget(rec) {
  var target = rec.returnFocus ? rec.returnFocus() : null;
  if (!target && rec.opener && rec.opener.isConnected !== false) target = rec.opener;
  return target && target.focus ? target : null;
}

function _onKeydown(e) {
  if (e.key !== 'Escape' || e.defaultPrevented || !_open || _dialogOpen()) return;
  _open.api.close('escape');
}

function _onPointerdown(e) {
  _pressInside = !!(_open && e.target && _open.el.contains(e.target));
}

// Capture phase, so the decision is made against the page as it was clicked: a handler that opens another panel
// or redraws the clicked element runs after it.
function _onClick(e) {
  if (!_open) return;
  var rec = _open;
  var target = e.target;
  if (target && target.closest && rec.el.contains(target) && target.closest('[data-panel-close]')) {
    rec.api.close('button');
    return;
  }
  if (_pressInside) return;
  // A target a redraw already removed was inside the panel when it was clicked.
  if (!target || target.isConnected === false || !target.closest) return;
  if (rec.el.contains(target)) return;
  if (target.closest(_ALWAYS_KEEPS)) return;
  if (rec.keepOpen && target.closest(rec.keepOpen)) return;
  rec.api.close('outside');
}

function _bindDocument() {
  if (_bound) return;
  _bound = true;
  document.addEventListener('keydown', _onKeydown);
  document.addEventListener('pointerdown', _onPointerdown, true);
  document.addEventListener('click', _onClick, true);
  if (typeof ResizeObserver === 'function') _resizeObserver = new ResizeObserver(_syncWidth);
  else if (typeof window !== 'undefined' && window.addEventListener) window.addEventListener('resize', _syncWidth);
}

function _overflows(value) { return value === 'auto' || value === 'scroll' || value === 'overlay'; }

// Whether `el` itself would move for this wheel delta: it scrolls on that axis and is not already at that end.
export function scrollsFor(el, dx, dy) {
  var style = typeof getComputedStyle === 'function' ? getComputedStyle(el) : null;
  if (!style) return false;
  if (dy && _overflows(style.overflowY) && el.scrollHeight > el.clientHeight) {
    if (dy < 0 ? el.scrollTop > 0 : el.scrollTop + el.clientHeight < el.scrollHeight - 1) return true;
  }
  if (dx && _overflows(style.overflowX) && el.scrollWidth > el.clientWidth) {
    if (dx < 0 ? el.scrollLeft > 0 : el.scrollLeft + el.clientWidth < el.scrollWidth - 1) return true;
  }
  return false;
}

// A wheel inside the panel that nothing between its target and the panel can take would chain to the page: stop it.
// A pinch (ctrlKey) is the browser's zoom and is left alone.
function _containWheel(panel) {
  return function(e) {
    if (e.ctrlKey) return;
    for (var node = e.target; node && node !== panel.parentNode; node = node.parentNode) {
      if (node.nodeType === 1 && scrollsFor(node, e.deltaX, e.deltaY)) return;
      if (node === panel) break;
    }
    e.preventDefault();
  };
}

// Register `el` as a side panel. Options:
//   name       the body's data-side-panel value while it is open ('feed', 'ticket')
//   floats     'shift' (the owl button and agents widget move left of it) or 'hide' (they hide behind it)
//   keepOpen   a selector for targets outside it whose click must not close it (cards that switch the ticket)
//   trigger    the button that opens it: its aria-expanded follows the panel
//   returnFocus  a function giving the element focus returns to on Escape and the close button (called after
//              onClose, so it can find an element the close redrew)
//   onOpen, onClose(reason)  called after it opened and after it closed ('escape', 'button', 'outside', 'toggle'
//              from toggle(), 'replaced' when another panel opened, or the reason a caller passed to close())
// Returns { open(), close(reason), toggle(), isOpen(), el }.
export function createSidePanel(el, options) {
  var opts = options || {};
  var rec = {
    el: el,
    name: opts.name || 'panel',
    floats: opts.floats === 'hide' ? 'hide' : 'shift',
    keepOpen: opts.keepOpen || '',
    trigger: opts.trigger || null,
    returnFocus: opts.returnFocus || null,
    opener: null,
    api: null,
  };
  _bindDocument();
  if (el && el.addEventListener) el.addEventListener('wheel', _containWheel(el), { passive: false });

  function setTrigger(open) {
    if (rec.trigger && rec.trigger.setAttribute) rec.trigger.setAttribute('aria-expanded', open ? 'true' : 'false');
  }

  var api = {
    el: el,
    isOpen: function() { return _open === rec; },
    open: function() {
      // Open already, it is switching content (another ticket from a card, a chain node or the search): what now
      // has focus outside it opened this content, so focus goes back there if `returnFocus()` names nothing.
      if (_open === rec) {
        var switcher = _outsideFocus(el);
        if (switcher) rec.opener = switcher;
        return;
      }
      if (_open) _open.api.close('replaced');
      rec.opener = _outsideFocus(el);
      _open = rec;
      el.setAttribute('data-open', '');
      el.removeAttribute('aria-hidden');
      _setInert(el, false);
      setTrigger(true);
      _setBodyState(rec);
      if (_resizeObserver) _resizeObserver.observe(el);
      if (opts.onOpen) opts.onOpen();
    },
    close: function(reason) {
      if (_open !== rec) return;
      var why = reason || 'api';
      var refocus = (why === 'escape' || why === 'button') && !_editableOutside(rec);
      _open = null;
      _pressInside = false;
      el.removeAttribute('data-open');
      el.setAttribute('aria-hidden', 'true');
      _setInert(el, true);
      setTrigger(false);
      if (_resizeObserver) _resizeObserver.unobserve(el);
      // Another panel opening sets the body state itself, right after.
      if (why !== 'replaced') _setBodyState(null);
      if (opts.onClose) opts.onClose(why);
      // After onClose: a page that redraws on close (the Work board) has drawn the element focus goes back to.
      var target = refocus ? _focusTarget(rec) : null;
      if (target) target.focus();
    },
    toggle: function() {
      if (_open === rec) api.close('toggle');
      else api.open();
    },
  };
  rec.api = api;
  if (el && el.setAttribute && !el.hasAttribute('data-open')) {
    el.setAttribute('aria-hidden', 'true');
    _setInert(el, true);
  }
  return api;
}
