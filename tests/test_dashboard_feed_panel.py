"""The activity feed panel is an overlay: it never pushes the page, resizes by drag, and opens at the newest message.

There is no browser here, so the behavior tests run the feed section of `app.js` under node against a stub
DOM; the markup and CSS rules are asserted on the shipped source. A missing `node` skips locally but fails
under CI (the same rule as `test_dashboard_agents_modules.py`).
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


_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');

const src = fs.readFileSync(process.argv[2], 'utf8');
const start = src.indexOf('var _feedLastId');
const end = src.lastIndexOf('/*', src.indexOf('8. PR & ISSUE LINK HELPERS'));
// A renamed declaration or section banner must fail here, not slice some other code and fail confusingly later.
if (start < 0 || end <= start) throw new Error('feed code not found in app.js: start=' + start + ' end=' + end);
const feedCode = src.slice(start, end);

const storage = {};
let storageBlocked = false;
const fetches = [];
const elements = {};
const docListeners = {};
const bodyClasses = new Set();
const state = { dialogs: [], selectors: [] };
const toasts = [];
const dispatched = [];
let notifications = 0;

function dialogStub({ hiddenClass = false, ariaHidden = false, rects = 1 } = {}) {
  return {
    classList: { contains: (c) => hiddenClass && c === 'hidden' },
    getAttribute: (k) => (k === 'aria-hidden' && ariaHidden ? 'true' : null),
    getClientRects: () => new Array(rects),
  };
}

function el(id) {
  const listeners = {};
  const classes = new Set(['hidden']);
  const captured = new Set();
  return {
    id, scrollTop: 0, scrollHeight: 0, clientHeight: 0, listeners, focused: false, attrs: {}, onRender: null,
    _html: '',
    get innerHTML() { return this._html; },
    set innerHTML(v) { this._html = v; if (this.onRender) this.onRender(); },
    contains(other) { return other === this._panelChild; },
    classList: {
      add: (c) => classes.add(c), remove: (c) => classes.delete(c), contains: (c) => classes.has(c),
      toggle: (c, on) => { if (on) classes.add(c); else classes.delete(c); },
    },
    style: { setProperty(k, v) { this.props[k] = v; }, props: {} },
    setAttribute(k, v) { this.attrs[k] = v; },
    addEventListener(t, fn) { listeners[t] = fn; },
    setPointerCapture(pid) { captured.add(pid); },
    hasPointerCapture: (pid) => captured.has(pid),
    releasePointerCapture(pid) { captured.delete(pid); },
    focus() { this.focused = true; },
    getBoundingClientRect: () => ({ right: 1280 }),
  };
}
for (const id of ['feed-panel', 'feed-fab', 'feed-list', 'feed-jump-latest', 'feed-resize-handle', 'feed-badge',
  'launch-modal']) {
  elements[id] = el(id);
}

const ctx = {
  console,
  _sovaModalOpen: false,
  document: {
    getElementById: (id) => elements[id] || null,
    querySelectorAll: (sel) => { state.selectors.push(sel); return state.dialogs; },
    activeElement: null,
    addEventListener: (t, fn) => { docListeners[t] = fn; },
    dispatchEvent: (e) => { dispatched.push(e); },
    body: { classList: { add: (c) => bodyClasses.add(c), remove: (c) => bodyClasses.delete(c) } },
  },
  safeStorageGet: (k) => { if (storageBlocked) return null; return k in storage ? storage[k] : null; },
  safeStorageSet: (k, v) => { storage[k] = v; },
  apiUrl: (p) => p,
  escapeHtml: (s) => String(s),
  showToast: (message, type) => { toasts.push([message, type]); },
  sendBrowserNotification: () => { notifications += 1; },
  CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init && init.detail; } },
  requestAnimationFrame: (fn) => fn(),
  fetch: (url) => new Promise((resolve) => { fetches.push({ url, resolve }); }),
  isFinite, Number, Math,
};
vm.createContext(ctx);
vm.runInContext(feedCode, ctx);

const out = {};

// Stored width: missing, empty, non-numeric and out-of-range values.
out.widths = [null, '', ' ', '0x300', 'abc', 'NaN', 'Infinity', '300', '900', '640'].map((raw) => {
  if (raw === null) delete storage['sova.feed.width']; else storage['sova.feed.width'] = raw;
  return vm.runInContext('_feedStoredWidth()', ctx);
});
delete storage['sova.feed.width'];

// Resize drag.
ctx.initFeedPanel();
const handle = elements['feed-resize-handle'];
const panel = elements['feed-panel'];
const drag = (moves, endEvent) => {
  handle.listeners.pointerdown({ button: 0, pointerId: 1, preventDefault() {} });
  out.selectionSuppressed = bodyClasses.has('feed-resizing');
  for (const x of moves) handle.listeners.pointermove({ clientX: x, pointerId: 1 });
  handle.listeners[endEvent]({ pointerId: 1 });
};
drag([], 'pointerup');
out.clickWrites = 'sova.feed.width' in storage;
drag([1280 - 700], 'pointerup');
out.dragged = { stored: storage['sova.feed.width'], prop: panel.style.props['--feed-panel-width'] };
out.captureReleased = !handle.hasPointerCapture(1);
out.selectionRestored = !bodyClasses.has('feed-resizing');
drag([1280 - 1100], 'pointercancel');
out.clampedMax = storage['sova.feed.width'];
drag([1280 - 100], 'pointerup');
out.clampedMin = storage['sova.feed.width'];
out.cancelRestored = !bodyClasses.has('feed-resizing');
drag([1280 - 600], 'lostpointercapture');
out.lostCaptureStored = storage['sova.feed.width'];
out.lostCaptureRestored = !bodyClasses.has('feed-resizing');

// Opening and closing tell js/feed/activity.js (it marks lines seen and keeps the badge); the briefing loads once.
ctx.toggleFeedPanel();
ctx.toggleFeedPanel();
ctx.toggleFeedPanel();
out.toggleEvents = dispatched.map((e) => [e.type, e.detail.open]);
out.briefingFetches = fetches.filter((f) => f.url === '/feed/briefing').length;
out.panelOpenAfterThree = ctx._feedPanelOpen;

// Escape: ignored when closed or already handled, closes and refocuses the button when open.
const esc = (extra) => docListeners.keydown(Object.assign({ key: 'Escape', defaultPrevented: false }, extra));
ctx.toggleFeedPanel();
out.closedBeforeEscape = !ctx._feedPanelOpen;
esc();
out.escapeWhenClosed = ctx._feedPanelOpen;
ctx.toggleFeedPanel();
esc({ defaultPrevented: true });
out.escapeHandledElsewhere = ctx._feedPanelOpen;
docListeners.keydown({ key: 'a', defaultPrevented: false });
out.otherKey = ctx._feedPanelOpen;
ctx._sovaModalOpen = true;
esc();
out.escapeWithConfirmOpen = ctx._feedPanelOpen;
ctx._sovaModalOpen = false;
elements['launch-modal'].classList.remove('hidden');
esc();
out.escapeWithLaunchModalOpen = ctx._feedPanelOpen;
elements['launch-modal'].classList.add('hidden');
state.selectors = [];
state.dialogs = [dialogStub()];
esc();
out.dialogSelector = state.selectors[state.selectors.length - 1];
out.escapeWithGenericDialogOpen = ctx._feedPanelOpen;
// Closed dialogs that stay in the DOM (aria-hidden, hidden class, not rendered) never claim Escape.
state.dialogs = [dialogStub({ ariaHidden: true }), dialogStub({ hiddenClass: true }), dialogStub({ rects: 0 })];
esc();
out.escapeWithOnlyClosedDialogs = !ctx._feedPanelOpen;
ctx.toggleFeedPanel();
state.dialogs = [];
// Focus in a page input (outside the panel) stays put when Escape closes the panel.
ctx.document.activeElement = { name: 'page-input', tagName: 'INPUT' };
elements['feed-fab'].focused = false;
esc();
out.escapeFromPageInputCloses = !ctx._feedPanelOpen;
out.escapeFromPageInputKeepsFocus = !elements['feed-fab'].focused;
// A focused page button or link (not editable) is not an exception: focus returns to the button.
ctx.toggleFeedPanel();
ctx.document.activeElement = { name: 'page-button', tagName: 'BUTTON' };
elements['feed-fab'].focused = false;
esc();
out.escapeFromPageButtonRefocuses = !ctx._feedPanelOpen && elements['feed-fab'].focused;
// Focus inside the panel (or nowhere) returns to the button.
ctx.toggleFeedPanel();
const inside = { name: 'panel-child' };
panel._panelChild = inside;
ctx.document.activeElement = inside;
esc();
out.escapeFromPanelCloses = !ctx._feedPanelOpen;
out.escapeFromPanelRefocuses = elements['feed-fab'].focused;
ctx.document.activeElement = null;
ctx.toggleFeedPanel();
elements['feed-fab'].focused = false;
esc();
out.escapeClosed = !ctx._feedPanelOpen;
out.fabFocused = elements['feed-fab'].focused;

// Blocked storage: the stored width is simply absent.
storageBlocked = true;
out.blockedStorage = vm.runInContext('_feedStoredWidth()', ctx);
storageBlocked = false;

const flush = () => new Promise((r) => setImmediate(r));
const addEvent = (id, severity, title) => ctx._feedAddEvent({ id, severity, title: title || 'e' + id });

(async () => {
  // A warning or an error toasts and notifies; an info event does neither; an id seen twice toasts once.
  addEvent(1, 'info');
  addEvent(2, 'warning', 'slow');
  addEvent(2, 'warning', 'slow');
  addEvent(3, 'error', 'boom');
  out.toasts = toasts.slice();
  out.notifications = notifications;
  out.lastId = ctx._feedLastId;

  // The remembered ids stay bounded, and the newest are still remembered.
  for (let id = 4; id <= 1500; id++) addEvent(id, 'info');
  addEvent(1501, 'warning', 'late');
  addEvent(1501, 'warning', 'late');
  out.seenBounded = vm.runInContext('_feedSeenCount', ctx) <= 1000;
  out.lateToasts = toasts.filter((t) => t[0] === 'late').length;

  // The gap fill asks for what came after the newest id, toasts the gap, and skips ids it already has.
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
def feed_run(tmp_path_factory: pytest.TempPathFactory) -> dict:
    if shutil.which("node") is None:
        if os.environ.get("CI"):
            pytest.fail("node is required in CI: it runs the feed panel behavior tests")
        pytest.skip("node is required to run the feed panel behavior tests")
    script: Path = tmp_path_factory.mktemp("feed") / "harness.cjs"
    script.write_text(_HARNESS, encoding="utf-8")
    proc = subprocess.run(["node", str(script), str(_APP_JS)], capture_output=True, text=True, timeout=60, check=False)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


class TestFeedPanelBehavior:
    def test_stored_width_is_clamped_or_ignored(self, feed_run: dict) -> None:
        assert feed_run["widths"] == [None, None, None, None, None, None, None, 400, 800, 640]

    def test_blocked_storage_gives_no_stored_width(self, feed_run: dict) -> None:
        assert feed_run["blockedStorage"] is None

    def test_drag_applies_the_width_and_persists_it_once_at_the_end(self, feed_run: dict) -> None:
        assert feed_run["dragged"] == {"stored": "700", "prop": "700px"}
        assert feed_run["captureReleased"]
        assert feed_run["selectionSuppressed"]
        assert feed_run["selectionRestored"]

    def test_drag_clamps_and_pointercancel_ends_it_like_pointerup(self, feed_run: dict) -> None:
        assert feed_run["clampedMax"] == "800"
        assert feed_run["clampedMin"] == "400"
        assert feed_run["cancelRestored"]

    def test_losing_pointer_capture_ends_the_drag_and_persists_the_width(self, feed_run: dict) -> None:
        assert feed_run["lostCaptureStored"] == "600"
        assert feed_run["lostCaptureRestored"]

    def test_a_click_on_the_handle_never_writes_a_width(self, feed_run: dict) -> None:
        assert not feed_run["clickWrites"]

    def test_opening_and_closing_tell_the_activity_module_and_the_briefing_loads_once(self, feed_run: dict) -> None:
        assert feed_run["toggleEvents"] == [
            ["sova:feed-toggle", True],
            ["sova:feed-toggle", False],
            ["sova:feed-toggle", True],
        ]
        assert feed_run["panelOpenAfterThree"] is True
        assert feed_run["briefingFetches"] == 1

    def test_a_warning_or_error_toasts_once_and_an_info_event_never_does(self, feed_run: dict) -> None:
        assert feed_run["toasts"] == [["slow", "warning"], ["boom", "error"]]
        assert feed_run["notifications"] == 2
        assert feed_run["lastId"] == 3

    def test_the_remembered_event_ids_stay_bounded_and_the_newest_are_kept(self, feed_run: dict) -> None:
        assert feed_run["seenBounded"]
        assert feed_run["lateToasts"] == 1

    def test_the_gap_fill_toasts_the_gap_and_only_events_it_did_not_have(self, feed_run: dict) -> None:
        assert feed_run["gapUrl"] == "/feed/history?since_id=1501"
        assert feed_run["gapToasts"] == [
            ["Activity feed gap detected: some events were missed", "warning"],
            ["missed", "error"],
        ]

    def test_escape_closes_only_an_open_unhandled_panel_and_refocuses_the_button(self, feed_run: dict) -> None:
        assert feed_run["closedBeforeEscape"]
        assert feed_run["escapeWhenClosed"] is False
        assert feed_run["escapeHandledElsewhere"] is True
        assert feed_run["otherKey"] is True
        assert feed_run["escapeWithConfirmOpen"] is True
        assert feed_run["escapeWithLaunchModalOpen"] is True
        assert feed_run["escapeWithGenericDialogOpen"] is True
        for needle in ("dialog[open]", '[role="dialog"]', '[aria-modal="true"]'):
            assert needle in feed_run["dialogSelector"]
        assert feed_run["escapeWithOnlyClosedDialogs"]
        assert feed_run["escapeClosed"]
        assert feed_run["fabFocused"]

    def test_escape_leaves_focus_alone_when_it_was_in_a_page_input(self, feed_run: dict) -> None:
        assert feed_run["escapeFromPageInputCloses"]
        assert feed_run["escapeFromPageInputKeepsFocus"]
        assert feed_run["escapeFromPageButtonRefocuses"]
        assert feed_run["escapeFromPanelCloses"]
        assert feed_run["escapeFromPanelRefocuses"]
