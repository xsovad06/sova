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
const start = src.indexOf('var _feedEvents');
const end = src.lastIndexOf('/*', src.indexOf('8. PR & ISSUE LINK HELPERS'));
const feedCode = src.slice(start, end);

const storage = {};
let storageBlocked = false;
const fetches = [];
const elements = {};
const docListeners = {};
const bodyClasses = new Set();
const state = { dialogs: [], selectors: [] };

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
    body: { classList: { add: (c) => bodyClasses.add(c), remove: (c) => bodyClasses.delete(c) } },
  },
  safeStorageGet: (k) => { if (storageBlocked) return null; return k in storage ? storage[k] : null; },
  safeStorageSet: (k, v) => { storage[k] = v; },
  apiUrl: (p) => p,
  escapeHtml: (s) => String(s),
  showToast() {},
  sendBrowserNotification() {},
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

// Open-time positioning and the auto-load hold.
const list = elements['feed-list'];
const olderFetches = () => fetches.filter((f) => f.url.includes('before_id=0&')).length;
list.scrollHeight = 2000;
list.clientHeight = 500;
ctx.toggleFeedPanel();
out.openedAtBottom = list.scrollTop;
out.heldOnOpen = ctx._feedAutoLoadHeld;
list.listeners.scroll();
out.programmaticScrollLoads = olderFetches();
list.scrollTop = 1000; list.listeners.scroll();
out.releasedByUserScroll = !ctx._feedAutoLoadHeld;
list.scrollTop = 10; list.listeners.scroll();
out.loadsAfterUserScroll = olderFetches();

// Reopening resets the hold and positions at the newest message again.
ctx.toggleFeedPanel();
list.scrollTop = 0;
ctx.toggleFeedPanel();
out.reopenedAtBottom = list.scrollTop;
out.heldOnReopen = ctx._feedAutoLoadHeld;

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

// Async history paths: fetches are deferreds the scenarios resolve by hand. The stub list is
// `baseHeight + 100 * events` tall and 500 high; "at the bottom" means scrollTop === scrollHeight.
const flush = () => new Promise((r) => setImmediate(r));
const ev = (ids) => ids.map((id) => ({ id, title: 'e' + id, timestamp: 1700000000 + id }));
async function resolveFetch(i, ids, hasMore) {
  fetches[i].resolve({ json: () => ({ events: ev(ids), has_more: hasMore }) });
  await flush();
}
function resetFeed(baseHeight) {
  vm.runInContext(
    '_feedPanelOpen = false; _feedEvents = []; _feedSeenIds = {}; _feedLastId = 0; _feedOldestId = 0;' +
    '_feedHasMoreHistory = true; _feedHistoryLoading = false; _feedHistoryLoaded = false;' +
    '_feedBriefingLoaded = true; _feedAutoLoadHeld = true; _feedLastScrollTop = 0; _feedLastScrollHeight = 0;', ctx);
  panel.classList.add('hidden');
  fetches.length = 0;
  list.scrollTop = 0;
  list.clientHeight = 500;
  list.scrollHeight = baseHeight;
  list.onRender = () => { list.scrollHeight = baseHeight + 100 * ctx._feedEvents.length; };
}

(async () => {
  // History resolving after open, user has not scrolled: positioned at the newest again.
  resetFeed(1000);
  ctx.toggleFeedPanel();
  list.scrollTop = 0;  // render would not follow (not at the bottom), only the held reposition can
  await resolveFetch(0, [1, 2, 3], true);
  out.historyAfterOpen = { top: list.scrollTop, height: list.scrollHeight, last: ctx._feedLastScrollTop };

  // History resolving after the user scrolled up: the position is left alone.
  resetFeed(1000);
  ctx.toggleFeedPanel();
  list.scrollTop = 200; list.listeners.scroll();
  out.releasedBeforeHistory = !ctx._feedAutoLoadHeld;
  await resolveFetch(0, [1, 2, 3], true);
  out.historyAfterScrollUp = { top: list.scrollTop, last: ctx._feedLastScrollTop };

  // Older history resolving restores the seam, keeping the same content in view.
  resetFeed(1000);
  ctx.toggleFeedPanel();
  await resolveFetch(0, [101, 102, 103], true);
  list.scrollTop = 30; list.listeners.scroll();
  out.olderRequested = fetches.length === 2 && fetches[1].url.includes('before_id=101');
  await resolveFetch(1, [98, 99, 100], false);
  out.seam = { top: list.scrollTop, height: list.scrollHeight, last: ctx._feedLastScrollTop };

  // A load still pending when the panel is closed and reopened must not pull the list off the newest.
  resetFeed(1000);
  ctx.toggleFeedPanel();
  await resolveFetch(0, [101, 102, 103], true);
  list.scrollTop = 30; list.listeners.scroll();
  ctx.toggleFeedPanel();
  ctx.toggleFeedPanel();
  out.reopenedWhileLoading = { top: list.scrollTop, height: list.scrollHeight, held: ctx._feedAutoLoadHeld };
  await resolveFetch(1, [98, 99, 100], false);
  out.afterRaceResolve = { top: list.scrollTop, height: list.scrollHeight, held: ctx._feedAutoLoadHeld };
  list.listeners.scroll();  // the next synthetic scroll event is not the user scrolling up
  out.afterRaceScroll = { held: ctx._feedAutoLoadHeld, fetches: fetches.length };

  // The list shrinks while the user has not scrolled (a rewrap): the clamped scrollTop is not a user
  // scroll, so the hold stays and nothing older is fetched, even inside the 40px zone.
  resetFeed(1000);
  ctx.toggleFeedPanel();
  await resolveFetch(0, [101, 102, 103], true);
  list.scrollHeight = 520;
  list.scrollTop = 20;
  list.listeners.scroll();
  out.reflow = { held: ctx._feedAutoLoadHeld, fetches: fetches.length };
  // A real upward scroll afterwards (height unchanged) still releases the hold and loads older history.
  list.scrollTop = 10;
  list.listeners.scroll();
  out.afterReflowUserScroll = { held: ctx._feedAutoLoadHeld, fetches: fetches.length };

  // A freshly opened list too short to scroll stays at 0 and loads no older history.
  resetFeed(0);
  ctx.toggleFeedPanel();
  list.listeners.scroll();
  out.shortList = { top: list.scrollTop, fetches: fetches.length, held: ctx._feedAutoLoadHeld };

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

    def test_open_positions_at_newest_and_holds_auto_load_until_the_user_scrolls_up(self, feed_run: dict) -> None:
        assert feed_run["openedAtBottom"] == 2000
        assert feed_run["heldOnOpen"]
        assert feed_run["programmaticScrollLoads"] == 0
        assert feed_run["releasedByUserScroll"]
        assert feed_run["loadsAfterUserScroll"] == 1

    def test_reopen_positions_at_newest_and_resets_the_hold(self, feed_run: dict) -> None:
        assert feed_run["reopenedAtBottom"] == 2000
        assert feed_run["heldOnReopen"]

    def test_history_resolving_after_open_repositions_at_newest_while_held(self, feed_run: dict) -> None:
        assert feed_run["historyAfterOpen"] == {"top": 1300, "height": 1300, "last": 1300}

    def test_history_resolving_after_the_user_scrolled_up_leaves_the_position(self, feed_run: dict) -> None:
        assert feed_run["releasedBeforeHistory"]
        assert feed_run["historyAfterScrollUp"] == {"top": 200, "last": 200}

    def test_older_history_restores_the_seam_and_records_the_position(self, feed_run: dict) -> None:
        assert feed_run["olderRequested"]
        assert feed_run["seam"] == {"top": 330, "height": 1600, "last": 330}

    def test_older_history_resolving_after_a_reopen_stays_at_newest_and_keeps_the_hold(self, feed_run: dict) -> None:
        assert feed_run["reopenedWhileLoading"] == {"top": 1300, "height": 1300, "held": True}
        assert feed_run["afterRaceResolve"] == {"top": 1600, "height": 1600, "held": True}
        assert feed_run["afterRaceScroll"] == {"held": True, "fetches": 2}

    def test_a_reflow_that_shrinks_the_list_is_not_a_user_scroll(self, feed_run: dict) -> None:
        assert feed_run["reflow"] == {"held": True, "fetches": 1}
        assert feed_run["afterReflowUserScroll"] == {"held": False, "fetches": 2}

    def test_a_freshly_opened_list_too_short_to_scroll_loads_nothing_older(self, feed_run: dict) -> None:
        assert feed_run["shortList"] == {"top": 0, "fetches": 1, "held": True}

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
