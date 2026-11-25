"""The activity feed panel and the shared side panel it sits on (#1563).

The feed is an overlay that never pushes the page and resizes by drag; like the Work ticket detail it is a shared
side panel (`static/js/components/side-panel.js`): one open at a time, closed by Escape, its close button or a click
outside, focus returned, a wheel over it never scrolling the page, motion in CSS. There is no browser here, so the
behavior tests run the modules under node against `tests/fixtures/fake_dom.cjs` (the panel) and the feed section of
`app.js` against a stub (toasts and the gap fill); the markup and CSS rules are asserted on the shipped source. A
missing `node` skips locally but fails under CI (the same rule as `test_dashboard_agents_modules.py`).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.conftest import DASHBOARD_DIR

_STATIC = DASHBOARD_DIR / "static"
_APP_JS = _STATIC / "app.js"
_SIDE_PANEL_JS = _STATIC / "js" / "components" / "side-panel.js"
_FEED_PANEL_JS = _STATIC / "js" / "feed" / "panel.js"
_FAKE_DOM = Path(__file__).parent / "fixtures" / "fake_dom.cjs"
_STYLE_CSS = (_STATIC / "style.css").read_text(encoding="utf-8")
_BASE_HTML = (DASHBOARD_DIR / "templates" / "base.html").read_text(encoding="utf-8")


def _css_rule(selector: str) -> str:
    """The body of the first top-level rule whose selector is exactly `selector`."""
    match = re.search(rf"(?m)^{re.escape(selector)}\s*\{{(.*?)\}}", _STYLE_CSS, re.DOTALL)
    assert match, f"no rule for {selector}"
    return match.group(1)


class TestFeedPanelMarkupAndStyle:
    def test_every_dialog_in_the_templates_can_be_told_closed(self) -> None:
        """Escape treats a `role="dialog"` or `aria-modal` element as open unless it is hidden by the
        `hidden` class or `aria-hidden="true"`, so a template that renders one closed another way would
        block Escape on its page."""
        for path in (DASHBOARD_DIR / "templates").glob("*.html"):
            text = path.read_text(encoding="utf-8")
            for tag in re.findall(r"<[a-z][^>]*(?:role=\"dialog\"|aria-modal=\"true\")[^>]*>", text):
                closed = 'aria-hidden="true"' in tag or re.search(r'class="[^"]*\bhidden\b', tag)
                assert closed, f"{path.name}: a dialog with no hidden class or aria-hidden: {tag[:120]}"

    def test_panel_overlays_instead_of_pushing_the_page(self) -> None:
        for source in (_APP_JS.read_text(encoding="utf-8"), _STYLE_CSS, _BASE_HTML):
            assert "feed-panel-push" not in source

    def test_panel_width_comes_from_a_custom_property_capped_at_the_viewport(self) -> None:
        rule = _css_rule(".feed-panel")
        assert "width: var(--feed-panel-width, 560px)" in rule
        assert "max-width: 100%" in rule
        assert "100vw" not in rule

    def test_message_list_does_not_animate_its_scroll(self) -> None:
        assert "scroll-behavior" not in _css_rule(".feed-chat-body")

    def test_narrow_viewport_is_full_width_without_a_handle(self) -> None:
        media = _STYLE_CSS[_STYLE_CSS.index("@media (max-width: 768px) {\n  .feed-panel {") :]
        media = media[: media.index("\n}\n")]
        assert "width: 100%" in media
        assert "100vw" not in media
        assert ".feed-resize-handle { display: none; }" in media

    def test_resize_handle_is_an_accessible_vertical_separator(self) -> None:
        tag = re.search(r'<div id="feed-resize-handle"[^>]*>', _BASE_HTML)
        assert tag, "base.html has no feed resize handle"
        assert 'role="separator"' in tag.group(0)
        assert 'aria-orientation="vertical"' in tag.group(0)
        assert "aria-label=" in tag.group(0)

    def test_dragging_suppresses_text_selection(self) -> None:
        rule = re.search(r"body\.feed-resizing \{(.*?)\}", _STYLE_CSS, re.DOTALL).group(1)
        assert "user-select: none" in rule
        assert "-webkit-user-select: none" in rule

    def test_feed_markup_is_on_the_side_panel_with_no_inline_handler(self) -> None:
        panel = re.search(r'<aside id="feed-panel"[^>]*>', _BASE_HTML).group(0)
        assert "sova-side-panel" in panel
        assert not re.search(r"\shidden(\s|>|=)", panel), "a closed panel waits off screen (CSS), so it can slide in"
        assert 'aria-hidden="true"' in panel, "closed for assistive tech before the deferred module runs"
        assert re.search(r"\sinert(\s|>)", panel), "and out of the tab order"
        assert 'id="feed-list" class="sova-side-panel-body' in _BASE_HTML
        assert (
            "onclick" not in _BASE_HTML[_BASE_HTML.index("<!-- Activity feed") : _BASE_HTML.index("<!-- Main content")]
        )
        assert "toggleFeedPanel" not in _BASE_HTML
        assert "data-panel-close" in _BASE_HTML

    def test_the_feed_state_lives_in_modules_not_app_js_globals(self) -> None:
        app = _APP_JS.read_text(encoding="utf-8")
        for name in ("toggleFeedPanel", "_feedPanelOpen", "initFeedPanel", "_FEED_WIDTH_KEY", "_feedDialogOpen"):
            assert name not in app, name
        assert "sova.feed.width" in _FEED_PANEL_JS.read_text(encoding="utf-8")
        assert "import('/static/js/feed/panel.js')" in _BASE_HTML

    def test_the_owl_button_and_agents_widget_are_floats_under_the_panel_layer(self) -> None:
        assert re.search(r'id="feed-fab" class="[^"]*\bsova-float\b', _BASE_HTML)
        assert re.search(r'id="resource-widget" class="[^"]*\bsova-float\b', _BASE_HTML)
        assert "z-index: var(--z-float)" in _css_rule(".feed-fab")
        assert "z-index: var(--z-panel)" in _css_rule(".sova-side-panel")
        tokens = {name: int(value) for name, value in re.findall(r"--z-([a-z]+):\s*(\d+);", _STYLE_CSS)}
        assert tokens["float"] < tokens["scrim"] < tokens["panel"] < tokens["nav"] < tokens["tooltip"]
        assert tokens["tooltip"] < tokens["toast"] < tokens["modal"]

    def test_floats_move_left_of_a_shift_panel_and_hide_behind_a_hide_panel(self) -> None:
        shift = _css_rule('body[data-side-panel-floats="shift"] .sova-float')
        assert "translate: calc(-1 * var(--side-panel-width, 0px)) 0" in shift
        hide = _css_rule('body[data-side-panel-floats="hide"] .sova-float')
        assert "visibility: hidden" in hide and "pointer-events: none" in hide

    def test_the_panel_slides_in_ease_out_and_out_ease_in_under_300ms(self) -> None:
        tokens = dict(
            re.findall(r"(--ease-in|--ease-out|--motion-panel-in|--motion-panel-out):\s*([^;]+);", _STYLE_CSS)
        )
        assert tokens["--ease-out"] == "cubic-bezier(0.16, 1, 0.3, 1)"
        assert tokens["--ease-in"] == "cubic-bezier(0.4, 0, 1, 1)"
        assert all(int(tokens[k].rstrip("ms")) < 300 for k in ("--motion-panel-in", "--motion-panel-out"))
        closed = _css_rule(".sova-side-panel")
        opened = _css_rule(".sova-side-panel[data-open]")
        assert "transform var(--motion-panel-out) var(--ease-in)" in closed
        assert "transform var(--motion-panel-in) var(--ease-out)" in opened
        assert "visibility: hidden" in closed and "visibility: visible" in opened
        reduced = _STYLE_CSS[_STYLE_CSS.index("@media (prefers-reduced-motion: reduce) {\n  .sova-side-panel") :]
        assert "transition: none" in reduced[: reduced.index("\n}\n")]

    def test_panels_contain_their_own_scroll(self) -> None:
        assert "overscroll-behavior: contain" in _css_rule(".sova-side-panel")
        body = _css_rule(".sova-side-panel-body")
        assert "overscroll-behavior: contain" in body and "overflow-y: auto" in body and "min-height: 0" in body

    def test_the_owl_is_never_clipped(self) -> None:
        owl = _css_rule(".sova-owl")
        assert "overflow" not in owl
        img = _css_rule(".sova-owl img")
        assert "object-fit: contain" in img and "border-radius" not in img and "90%" in img
        assert "feed-avatar" not in _STYLE_CSS and "feed-fab-owl" not in _STYLE_CSS
        assert _BASE_HTML.count('owl_avatar("') == 2, "the feed header and the owl button use the shared macro"
        assert "owlAvatarHtml('sm')" in _FEED_PANEL_JS.read_text(encoding="utf-8"), "the briefing card does too"
        for source in (_BASE_HTML, _APP_JS.read_text(encoding="utf-8"), _FEED_PANEL_JS.read_text(encoding="utf-8")):
            assert "object-fit: cover" not in source


def _require_node(reason: str) -> None:
    if shutil.which("node") is not None:
        return
    if os.environ.get("CI"):
        pytest.fail(f"node is required in CI: {reason}")
    pytest.skip(reason)


# The feed section of app.js: a toast for each live warning or error, and the gap fill after a reconnect.
_TOAST_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');

const src = fs.readFileSync(process.argv[2], 'utf8');
const start = src.indexOf('var _feedLastId');
const end = src.lastIndexOf('/*', src.indexOf('8. PR & ISSUE LINK HELPERS'));
// A renamed declaration or section banner must fail here, not slice some other code and fail confusingly later.
if (start < 0 || end <= start) throw new Error('feed code not found in app.js: start=' + start + ' end=' + end);
const fetches = [];
const toasts = [];
let notifications = 0;
const ctx = {
  console,
  apiUrl: (p) => p,
  showToast: (message, type) => { toasts.push([message, type]); },
  sendBrowserNotification: () => { notifications += 1; },
  fetch: (url) => new Promise((resolve) => { fetches.push({ url, resolve }); }),
  Number, Object,
};
vm.createContext(ctx);
vm.runInContext(src.slice(start, end), ctx);
const out = {};
const flush = () => new Promise((r) => setImmediate(r));
const addEvent = (id, severity, title) => ctx._feedAddEvent({ id, severity, title: title || 'e' + id });

(async () => {
  addEvent(1, 'info');
  addEvent(2, 'warning', 'slow');
  addEvent(2, 'warning', 'slow');
  addEvent(3, 'error', 'boom');
  out.toasts = toasts.slice();
  out.notifications = notifications;
  out.lastId = ctx._feedLastId;
  for (let id = 4; id <= 1500; id++) addEvent(id, 'info');
  addEvent(1501, 'warning', 'late');
  addEvent(1501, 'warning', 'late');
  out.seenBounded = vm.runInContext('_feedSeenCount', ctx) <= 1000;
  out.lateToasts = toasts.filter((t) => t[0] === 'late').length;
  fetches.length = 0;
  ctx._feedGapFill();
  out.gapUrl = fetches[0].url;
  fetches[0].resolve({ json: () => ({ gap_detected: true, events: [{ id: 1501, severity: 'warning', title: 'late' },
    { id: 1502, severity: 'error', title: 'missed' }] }) });
  await flush();
  out.gapToasts = toasts.slice(-2);
  console.log(JSON.stringify(out));
})();
"""


@pytest.fixture(scope="module")
def toast_run(tmp_path_factory: pytest.TempPathFactory) -> dict:
    _require_node("node is required to run the feed toast tests")
    script: Path = tmp_path_factory.mktemp("feed") / "toasts.cjs"
    script.write_text(_TOAST_HARNESS, encoding="utf-8")
    proc = subprocess.run(["node", str(script), str(_APP_JS)], capture_output=True, text=True, timeout=60, check=False)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


class TestFeedToasts:
    def test_a_warning_or_error_toasts_once_and_an_info_event_never_does(self, toast_run: dict) -> None:
        assert toast_run["toasts"] == [["slow", "warning"], ["boom", "error"]]
        assert toast_run["notifications"] == 2
        assert toast_run["lastId"] == 3

    def test_the_remembered_event_ids_stay_bounded_and_the_newest_are_kept(self, toast_run: dict) -> None:
        assert toast_run["seenBounded"]
        assert toast_run["lateToasts"] == 1

    def test_the_gap_fill_toasts_the_gap_and_only_events_it_did_not_have(self, toast_run: dict) -> None:
        assert toast_run["gapUrl"] == "/feed/history?since_id=1501"
        assert toast_run["gapToasts"] == [
            ["Activity feed gap detected: some events were missed", "warning"],
            ["missed", "error"],
        ]


# The shared side panel and the feed panel module, imported as ES modules over the fake DOM.
_PANEL_HARNESS = r"""
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';
import fs from 'node:fs';
import vm from 'node:vm';

const [, , sidePanelPath, feedPanelPath, appJsPath, fakeDomPath] = process.argv;
const require = createRequire(import.meta.url);
const { Document, key, clickOn } = require(fakeDomPath);

const doc = new Document();
globalThis.document = doc;
globalThis.window = globalThis;
if (typeof globalThis.CustomEvent !== 'function') {
  globalThis.CustomEvent = class { constructor(type, init) { this.type = type; this.detail = init && init.detail; } };
}
doc.dispatchEvent = (e) => { (doc.listeners || []).filter((l) => l.type === e.type).forEach((l) => l.fn(e)); };
doc.body.style.setProperty = function(k, v) { this[k] = v; };
doc.body.style.removeProperty = function(k) { delete this[k]; };
const appSrc = fs.readFileSync(appJsPath, 'utf8');
function real(name) {
  const start = appSrc.indexOf('function ' + name + '(');
  return appSrc.slice(start, appSrc.indexOf('\n}\n', start) + 2);
}
vm.runInThisContext(real('escapeHtml') + 'globalThis.escapeHtml = escapeHtml;');
const owlSizes = appSrc.indexOf('var SOVA_OWL_SIZES');
vm.runInThisContext(appSrc.slice(owlSizes, appSrc.indexOf('\n', owlSizes)) +
  real('owlAvatarHtml') + 'globalThis.SOVA_OWL_SIZES = SOVA_OWL_SIZES; globalThis.owlAvatarHtml = owlAvatarHtml;');
const storage = {};
let storageBlocked = false;
globalThis.safeStorageGet = (k) => (storageBlocked ? null : (k in storage ? storage[k] : null));
globalThis.safeStorageSet = (k, v) => { storage[k] = v; };
globalThis.apiUrl = (p) => p;
const fetches = [];
let briefing = null;
globalThis.fetch = (url) => { fetches.push(url); return Promise.resolve({ json: () => briefing }); };
let sharedModalOpen = false;
globalThis.sovaModalIsOpen = () => sharedModalOpen;
// Scroll boxes for the wheel: an element is a vertical scroller when it carries data-scroll.
globalThis.getComputedStyle = (el) => ({
  overflowY: el.hasAttribute('data-scroll') ? 'auto' : 'visible', overflowX: 'visible',
});

doc.body.innerHTML =
  '<input id="page-input"><button id="page-button">Page</button><p id="outside">Board</p>' +
  '<div id="feed-scrim" class="feed-scrim"></div>' +
  '<aside id="feed-panel" class="sova-side-panel feed-panel">' +
    '<div id="feed-resize-handle"></div>' +
    '<div id="feed-head"><button type="button" id="feed-close" data-panel-close>Close</button></div>' +
    '<div id="feed-list" data-scroll><div id="feed-briefing-slot"></div><button id="feed-line">Line</button></div>' +
  '</aside>' +
  '<button type="button" id="feed-fab">Owl</button>' +
  '<aside id="ticket" class="sova-side-panel"><button id="ticket-btn">Act</button></aside>' +
  '<button id="card" data-action="work-select">Card</button>' +
  '<div id="dialog" role="dialog"></div>' +
  '<div class="sova-toast"><button id="toast-btn">x</button></div>' +
  '<div data-keeps-panel><button id="kept">Kept</button></div>';
const $ = (id) => doc.getElementById(id);
const panelEl = $('feed-panel');
const handle = $('feed-resize-handle');
const captured = new Set();
Object.assign(handle, {
  setPointerCapture: (id) => captured.add(id), hasPointerCapture: (id) => captured.has(id),
  releasePointerCapture: (id) => captured.delete(id),
});
panelEl.style.setProperty = function(k, v) { this[k] = v; };
panelEl.getBoundingClientRect = () => ({ right: 1280 });
panelEl.offsetWidth = 560;
$('ticket').offsetWidth = 416;
$('dialog').hidden = true;

const sidePanel = await import(pathToFileURL(sidePanelPath).href);
const feedPanel = await import(pathToFileURL(feedPanelPath).href);
const out = {};
const toggles = [];
doc.addEventListener('sova:feed-toggle', (e) => toggles.push(e.detail.open));
const flush = () => new Promise((r) => setImmediate(r));
const isOpen = () => feedPanel.isOpen();

// Stored width: missing, empty, non-numeric and out-of-range values.
out.widths = [null, '', ' ', '0x300', 'abc', 'NaN', 'Infinity', '300', '900', '640'].map((raw) => {
  if (raw === null) delete storage['sova.feed.width']; else storage['sova.feed.width'] = raw;
  return feedPanel.storedWidth();
});
storageBlocked = true;
out.blockedStorage = feedPanel.storedWidth();
storageBlocked = false;
storage['sova.feed.width'] = '640';
feedPanel.init();
out.restoredWidth = panelEl.style['--feed-panel-width'];
delete storage['sova.feed.width'];
out.closedAtStart = [isOpen(), panelEl.getAttribute('aria-hidden')];

// Resize drag.
const fire = (type, ev) => handle.listeners.filter((l) => l.type === type)
  .forEach((l) => l.fn(Object.assign({ preventDefault() {} }, ev)));
const drag = (moves, endEvent) => {
  fire('pointerdown', { button: 0, pointerId: 1 });
  out.selectionSuppressed = doc.body.classList.contains('feed-resizing');
  for (const x of moves) fire('pointermove', { clientX: x, pointerId: 1 });
  fire(endEvent, { pointerId: 1 });
};
drag([], 'pointerup');
out.clickWrites = 'sova.feed.width' in storage;
drag([1280 - 700], 'pointerup');
out.dragged = { stored: storage['sova.feed.width'], prop: panelEl.style['--feed-panel-width'] };
out.captureReleased = !handle.hasPointerCapture(1);
out.selectionRestored = !doc.body.classList.contains('feed-resizing');
drag([1280 - 1100], 'pointercancel');
out.clampedMax = storage['sova.feed.width'];
drag([1280 - 100], 'pointerup');
out.clampedMin = storage['sova.feed.width'];
drag([1280 - 600], 'lostpointercapture');
out.lostCaptureStored = storage['sova.feed.width'];

// The owl button opens it: the body names it, the floats hide, the button is expanded, the briefing loads once.
briefing = {
  attention_items: [{ title: '<b>PR</b>', source_url: 'javascript:alert(1)' }], informational_items: [], schedule: [],
};
$('feed-fab').focus();
clickOn($('feed-fab'));
await flush();
out.opened = {
  open: isOpen(), panel: doc.body.getAttribute('data-side-panel'),
  floats: doc.body.getAttribute('data-side-panel-floats'),
  width: doc.body.style['--side-panel-width'], expanded: $('feed-fab').getAttribute('aria-expanded'),
  dataOpen: panelEl.hasAttribute('data-open'), ariaHidden: panelEl.getAttribute('aria-hidden') };
out.briefingHtml = feedPanel.briefingHtml(briefing);
out.briefingDrawn = $('feed-briefing-slot').querySelectorAll('.sova-owl').length;
// Inside the panel: a click and a press that began inside it keep it open.
clickOn($('feed-line'));
out.insideKeeps = isOpen();
clickOn($('outside'), $('feed-line'));
out.pressBeganInsideKeeps = isOpen();
// Overlays over it keep it open.
for (const id of ['toast-btn', 'kept']) { clickOn($(id)); out['keeps_' + id] = isOpen(); }
// The close button closes it and returns focus to the owl button.
$('feed-line').focus();
clickOn($('feed-close'));
out.closeButton = { open: isOpen(), focus: doc.activeElement.id, panel: doc.body.getAttribute('data-side-panel'),
  width: doc.body.style['--side-panel-width'] ?? null, expanded: $('feed-fab').getAttribute('aria-expanded'),
  ariaHidden: panelEl.getAttribute('aria-hidden') };
// A click outside (the scrim, the page) closes it, and focus stays where the click put it.
clickOn($('feed-fab'));
clickOn($('feed-scrim'));
out.scrimCloses = !isOpen();
clickOn($('feed-fab'));
clickOn($('outside'));
out.outsideCloses = !isOpen();
out.briefingFetches = fetches.filter((u) => u === '/feed/briefing').length;
out.toggles = toggles.slice();

// Escape: closes an open panel unless a dialog or an earlier handler has the key; focus goes back to the button.
const openFeed = () => { if (!isOpen()) clickOn($('feed-fab')); };
openFeed();
key(doc, 'Escape', { defaultPrevented: true });
out.escapeHandledElsewhere = isOpen();
key(doc, 'a');
out.otherKey = isOpen();
sharedModalOpen = true;
key(doc, 'Escape');
out.escapeWithModalOpen = isOpen();
sharedModalOpen = false;
$('dialog').hidden = false;
key(doc, 'Escape');
out.escapeWithDialogOpen = isOpen();
$('dialog').hidden = true;
$('feed-line').focus();
key(doc, 'Escape');
out.escapeFromPanel = { open: isOpen(), focus: doc.activeElement.id };
openFeed();
$('page-input').focus();
key(doc, 'Escape');
out.escapeFromPageInput = { open: isOpen(), focus: doc.activeElement.id };
openFeed();
$('page-button').focus();
key(doc, 'Escape');
out.escapeFromPageButton = { open: isOpen(), focus: doc.activeElement.id };
key(doc, 'Escape');
out.escapeWhenClosed = isOpen();

// One open at a time: a second panel replaces the feed, and the feed replaces it.
const ticketCloses = [];
const ticket = sidePanel.createSidePanel($('ticket'), {
  name: 'ticket', floats: 'shift', keepOpen: '[data-action="work-select"]',
  returnFocus: () => $('card'),
  onClose: (reason) => ticketCloses.push(reason),
});
openFeed();
ticket.open();
out.ticketReplacesFeed = { feed: isOpen(), ticket: ticket.isOpen(), panel: doc.body.getAttribute('data-side-panel'),
  floats: doc.body.getAttribute('data-side-panel-floats'), width: doc.body.style['--side-panel-width'],
  lastToggle: toggles[toggles.length - 1] };
clickOn($('card'));
out.keepOpenTarget = ticket.isOpen();
clickOn($('feed-fab'));
out.feedReplacesTicket = { feed: isOpen(), ticket: ticket.isOpen(), panel: doc.body.getAttribute('data-side-panel'),
  reasons: ticketCloses.slice() };
key(doc, 'Escape');
ticket.open();
$('ticket-btn').focus();
key(doc, 'Escape');
out.ticketEscape = { open: ticket.isOpen(), focus: doc.activeElement.id, reasons: ticketCloses.slice(),
  panel: doc.body.getAttribute('data-side-panel') };
ticket.open();
ticket.close('deselected');
out.apiClose = { reasons: ticketCloses.slice(), focus: doc.activeElement.id };

// Wheel: stopped over the header and at either end of the list, let through while the list can still scroll.
const list = $('feed-list');
Object.assign(list, { scrollHeight: 1000, clientHeight: 400, scrollTop: 0 });
const wheelListener = panelEl.listeners.find((l) => l.type === 'wheel');
out.wheelPassive = wheelListener ? wheelListener.passive === undefined : false;
const wheel = (target, deltaY, extra) => {
  const ev = Object.assign({ target, deltaX: 0, deltaY, ctrlKey: false, defaultPrevented: false,
    preventDefault() { this.defaultPrevented = true; } }, extra);
  wheelListener.fn(ev);
  return ev.defaultPrevented;
};
out.wheel = {
  header: wheel($('feed-head'), 100),
  listTopUp: wheel($('feed-line'), -100),
  listTopDown: wheel($('feed-line'), 100),
};
list.scrollTop = 300;
out.wheel.listMiddle = [wheel($('feed-line'), 100), wheel($('feed-line'), -100)];
list.scrollTop = 600;
out.wheel.listEndDown = wheel($('feed-line'), 100);
out.wheel.listEndUp = wheel($('feed-line'), -100);
out.wheel.pinch = wheel($('feed-head'), 100, { ctrlKey: true });
list.scrollTop = 0;
out.scrollsFor = sidePanel.scrollsFor(list, 0, -10);

console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def panel_run(tmp_path_factory: pytest.TempPathFactory) -> dict:
    _require_node("node is required to run the side panel behavior tests")
    script: Path = tmp_path_factory.mktemp("panel") / "harness.mjs"
    script.write_text(_PANEL_HARNESS, encoding="utf-8")
    proc = subprocess.run(
        ["node", str(script), str(_SIDE_PANEL_JS), str(_FEED_PANEL_JS), str(_APP_JS), str(_FAKE_DOM)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


class TestFeedPanelWidth:
    def test_stored_width_is_clamped_or_ignored(self, panel_run: dict) -> None:
        assert panel_run["widths"] == [None, None, None, None, None, None, None, 400, 800, 640]
        assert panel_run["blockedStorage"] is None
        assert panel_run["restoredWidth"] == "640px"

    def test_drag_applies_the_width_and_persists_it_once_at_the_end(self, panel_run: dict) -> None:
        assert panel_run["dragged"] == {"stored": "700", "prop": "700px"}
        assert panel_run["captureReleased"]
        assert panel_run["selectionSuppressed"]
        assert panel_run["selectionRestored"]

    def test_drag_clamps_and_a_cancel_or_lost_capture_ends_it(self, panel_run: dict) -> None:
        assert panel_run["clampedMax"] == "800"
        assert panel_run["clampedMin"] == "400"
        assert panel_run["lostCaptureStored"] == "600"

    def test_a_click_on_the_handle_never_writes_a_width(self, panel_run: dict) -> None:
        assert not panel_run["clickWrites"]


class TestFeedPanelOpenClose:
    def test_it_starts_closed_and_hidden_from_assistive_tech(self, panel_run: dict) -> None:
        assert panel_run["closedAtStart"] == [False, "true"]

    def test_the_owl_button_opens_it_and_the_body_names_it(self, panel_run: dict) -> None:
        assert panel_run["opened"] == {
            "open": True,
            "panel": "feed",
            "floats": "hide",
            "width": "560px",
            "expanded": "true",
            "dataOpen": True,
            "ariaHidden": None,
        }

    def test_the_briefing_loads_once_with_an_uncropped_owl_and_escaped_items(self, panel_run: dict) -> None:
        assert panel_run["briefingFetches"] == 1
        assert panel_run["briefingDrawn"] == 1
        html = panel_run["briefingHtml"]
        assert (
            '<span class="sova-owl sova-owl-sm" aria-hidden="true"><img src="/static/owl-avatar.png" alt=""></span>'
            in html
        )
        assert "<b>" not in html and "javascript:" not in html

    def test_clicks_inside_and_on_overlays_keep_it_open(self, panel_run: dict) -> None:
        assert panel_run["insideKeeps"]
        assert panel_run["pressBeganInsideKeeps"], "a text selection released outside is not a click outside"
        assert panel_run["keeps_toast-btn"]
        assert panel_run["keeps_kept"], "an element marked data-keeps-panel keeps it open"

    def test_the_close_button_closes_it_and_returns_focus(self, panel_run: dict) -> None:
        assert panel_run["closeButton"] == {
            "open": False,
            "focus": "feed-fab",
            "panel": None,
            "width": None,
            "expanded": "false",
            "ariaHidden": "true",
        }

    def test_a_click_outside_or_on_the_scrim_closes_it(self, panel_run: dict) -> None:
        assert panel_run["scrimCloses"]
        assert panel_run["outsideCloses"]

    def test_opening_and_closing_tell_the_activity_module(self, panel_run: dict) -> None:
        toggles = panel_run["toggles"]
        assert toggles[:2] == [True, False]
        assert all(a != b for a, b in zip(toggles, toggles[1:])), "open and close alternate"


class TestSidePanelEscape:
    def test_escape_closes_only_an_open_unclaimed_panel(self, panel_run: dict) -> None:
        assert panel_run["escapeHandledElsewhere"] is True
        assert panel_run["otherKey"] is True
        assert panel_run["escapeWithModalOpen"] is True
        assert panel_run["escapeWithDialogOpen"] is True
        assert panel_run["escapeWhenClosed"] is False

    def test_escape_returns_focus_unless_it_was_in_a_page_field(self, panel_run: dict) -> None:
        assert panel_run["escapeFromPanel"] == {"open": False, "focus": "feed-fab"}
        assert panel_run["escapeFromPageInput"] == {"open": False, "focus": "page-input"}
        assert panel_run["escapeFromPageButton"] == {"open": False, "focus": "feed-fab"}


class TestOnePanelAtATime:
    def test_opening_another_panel_closes_the_feed(self, panel_run: dict) -> None:
        assert panel_run["ticketReplacesFeed"] == {
            "feed": False,
            "ticket": True,
            "panel": "ticket",
            "floats": "shift",
            "width": "416px",
            "lastToggle": False,
        }

    def test_a_keep_open_target_does_not_close_it_and_the_owl_button_swaps_it_for_the_feed(
        self, panel_run: dict
    ) -> None:
        """The owl button is outside the ticket: its click closes the ticket first (capture phase), then opens the
        feed, so the body is left naming the feed."""
        assert panel_run["keepOpenTarget"] is True
        assert panel_run["feedReplacesTicket"] == {
            "feed": True,
            "ticket": False,
            "panel": "feed",
            "reasons": ["outside"],
        }

    def test_escape_on_the_ticket_returns_focus_to_its_card(self, panel_run: dict) -> None:
        assert panel_run["ticketEscape"] == {
            "open": False,
            "focus": "card",
            "reasons": ["outside", "escape"],
            "panel": None,
        }

    def test_a_close_from_code_reports_its_reason_and_moves_no_focus(self, panel_run: dict) -> None:
        assert panel_run["apiClose"] == {"reasons": ["outside", "escape", "deselected"], "focus": "card"}


class TestSidePanelWheel:
    def test_a_wheel_the_panel_cannot_take_is_stopped(self, panel_run: dict) -> None:
        wheel = panel_run["wheel"]
        assert wheel["header"] is True, "nothing scrolls under the header: the page must not"
        assert wheel["listTopUp"] is True, "up at the top of the list"
        assert wheel["listEndDown"] is True, "down at the end of the list"
        assert wheel["pinch"] is False, "a pinch is the browser's zoom"

    def test_a_wheel_the_list_can_take_is_left_to_the_browser(self, panel_run: dict) -> None:
        wheel = panel_run["wheel"]
        assert wheel["listTopDown"] is False
        assert wheel["listMiddle"] == [False, False]
        assert wheel["listEndUp"] is False
        assert panel_run["scrollsFor"] is False, "a list at the top cannot scroll further up"
