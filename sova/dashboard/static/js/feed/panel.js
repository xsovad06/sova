// The activity feed panel, on the shared side panel (components/side-panel.js): the floating owl button opens it,
// its width is set by dragging its left edge on desktop and remembered, and the morning briefing card loads the first
// time it opens. js/feed/activity.js draws the body and hears about opening and closing through `sova:feed-toggle`.
//
// While it is open the owl button and the agents widget hide behind it (floats: 'hide'), and a scrim over the page
// (base.html's #feed-scrim) dims what is behind; a click on the scrim is a click outside, which closes it.

import { createSidePanel } from '../components/side-panel.js';

var _WIDTH_KEY = 'sova.feed.width';
var _WIDTH_MIN = 400;
var _WIDTH_MAX = 800;

var _panel = null;
var _briefingLoaded = false;

export function clampWidth(px) {
  return Math.min(_WIDTH_MAX, Math.max(_WIDTH_MIN, Math.round(px)));
}

// A stored width that is missing, unreadable or not a number gives null (the CSS default applies); a number outside
// the allowed range is clamped.
export function storedWidth() {
  var raw = safeStorageGet(_WIDTH_KEY);
  // Plain decimal text only: Number() would turn whitespace into 0 and '0x300' into 768.
  if (raw === null || raw === undefined || !/^\s*\d+(\.\d+)?\s*$/.test(raw)) return null;
  var px = Number(raw);
  return isFinite(px) ? clampWidth(px) : null;
}

function _setWidth(panel, px) {
  panel.style.setProperty('--feed-panel-width', px + 'px');
}

function _bindResize(panel) {
  var handle = document.getElementById('feed-resize-handle');
  if (!handle) return;
  var stored = storedWidth();
  if (stored !== null) _setWidth(panel, stored);

  var dragging = false;
  var dragRight = 0;     // the panel's right edge, which stops short of a classic page scrollbar
  var dragWidth = null;  // set by the first move, so a click on the handle never rewrites the stored width
  function end(e) {
    if (!dragging) return;
    dragging = false;
    if (handle.hasPointerCapture && handle.hasPointerCapture(e.pointerId)) handle.releasePointerCapture(e.pointerId);
    document.body.classList.remove('feed-resizing');
    if (dragWidth !== null) safeStorageSet(_WIDTH_KEY, String(dragWidth));
    dragWidth = null;
  }
  handle.addEventListener('pointerdown', function(e) {
    if (e.button !== 0) return;
    handle.setPointerCapture(e.pointerId);
    document.body.classList.add('feed-resizing');
    dragRight = panel.getBoundingClientRect().right;
    dragging = true;
    e.preventDefault();
  });
  handle.addEventListener('pointermove', function(e) {
    if (!dragging) return;
    dragWidth = clampWidth(dragRight - e.clientX);
    _setWidth(panel, dragWidth);
  });
  handle.addEventListener('pointerup', end);
  handle.addEventListener('pointercancel', end);
  // Capture can also be lost without pointerup (window blur, the handle hidden mid-drag).
  handle.addEventListener('lostpointercapture', end);
}

// The morning briefing card (GET /feed/briefing), or '' when it has nothing. Every interpolation is escaped, and a
// source link is drawn only for an http(s) URL.
export function briefingHtml(b) {
  if (!b) return '';
  var attention = b.attention_items || [];
  var info = b.informational_items || [];
  var schedule = b.schedule || [];
  if (attention.length === 0 && info.length === 0 && schedule.length === 0) return '';

  function itemRow(it, dotColor) {
    var safeLink = escapeHtml(it.title);
    if (it.source_url) {
      try {
        var proto = new URL(it.source_url).protocol;
        if (proto === 'http:' || proto === 'https:') {
          safeLink = '<a href="' + escapeHtml(it.source_url) + '" target="_blank" rel="noopener">' + escapeHtml(it.title) + '</a>';
        }
      } catch (_e) { /* invalid URL, render as plain text */ }
    }
    return '<div class="feed-briefing-item"><span class="feed-briefing-dot" style="background:' + dotColor + '"></span><span>' + safeLink + '</span></div>';
  }

  var sections = '';
  if (attention.length) {
    sections += '<div class="feed-briefing-section-label">Needs attention</div>' +
      attention.map(function(it) { return itemRow(it, 'var(--color-accent-red)'); }).join('');
  }
  if (info.length) {
    sections += '<div class="feed-briefing-section-label">Updates</div>' +
      info.map(function(it) { return itemRow(it, 'var(--color-accent)'); }).join('');
  }
  if (schedule.length) {
    sections += '<div class="feed-briefing-section-label">Schedule</div>' +
      schedule.map(function(it) { return itemRow(it, 'var(--color-accent-yellow)'); }).join('');
  }

  return '<div id="feed-briefing-card" class="feed-briefing">' +
    '<div class="feed-briefing-head">' + owlAvatarHtml('sm') +
      '<span class="feed-briefing-title">Morning briefing</span>' +
    '</div>' + sections + '</div>';
}

function _loadBriefing() {
  if (_briefingLoaded) return;
  _briefingLoaded = true;
  fetch(apiUrl('/feed/briefing')).then(function(r) { return r.json(); }).then(function(b) {
    var slot = document.getElementById('feed-briefing-slot');
    if (slot) slot.innerHTML = briefingHtml(b);
  }).catch(function() { /* no briefing card */ });
}

// js/feed/activity.js marks the attention lines seen on open and keeps the badge.
function _announce(open) {
  document.dispatchEvent(new CustomEvent('sova:feed-toggle', { detail: { open: open } }));
}

export function isOpen() { return !!(_panel && _panel.isOpen()); }

export function init() {
  var el = document.getElementById('feed-panel');
  if (!el) return;
  var fab = document.getElementById('feed-fab');
  _panel = createSidePanel(el, {
    name: 'feed',
    floats: 'hide',
    trigger: fab,
    returnFocus: function() { return fab; },
    onOpen: function() {
      _loadBriefing();
      _announce(true);
    },
    onClose: function() { _announce(false); },
  });
  _bindResize(el);
  if (fab) fab.addEventListener('click', function() { _panel.toggle(); });
}
