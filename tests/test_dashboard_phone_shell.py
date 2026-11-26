"""The phone shell under 768 px (#1564): top bar, tab bar, More sheet and full-screen sheets.

There is no browser here, so the markup is asserted on the rendered pages, the layout contract on the shipped
stylesheet, and `static/js/components/phone-shell.js` runs under node against `tests/fixtures/fake_dom.cjs`. The
measured layout at 390 and 1440 px (no sideways scroll, 44 px controls, desktop unchanged) is checked by driving a
real browser before merge; docs/design-system.md, "Phone Shell", describes it.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from httpx import AsyncClient

from sova.dashboard.navigation import NAV_PAGES
from tests.conftest import DASHBOARD_DIR
from tests.dashboard_helpers import PHONE_MEDIA
from tests.dashboard_helpers import css_rule as _rule
from tests.dashboard_helpers import phone_css_block as _phone_block

pytestmark = pytest.mark.usefixtures("reset_db_rows")

_STATIC = DASHBOARD_DIR / "static"
_STYLE_CSS = (_STATIC / "style.css").read_text(encoding="utf-8")
_PHONE_SHELL_JS = _STATIC / "js" / "components" / "phone-shell.js"
_SIDE_PANEL_JS = _STATIC / "js" / "components" / "side-panel.js"
_FAKE_DOM = Path(__file__).parent / "fixtures" / "fake_dom.cjs"
_PHONE_MEDIA = PHONE_MEDIA


def _between(html: str, start: str, end: str) -> str:
    return html[html.index(start) : html.index(end, html.index(start))]


class TestShellMarkup:
    async def test_the_tab_bar_shows_the_tab_pages_with_the_owl_gap_in_the_middle(self, client: AsyncClient) -> None:
        html = (await client.get("/work")).text
        bar = _between(html, '<nav id="phone-tabbar"', "</nav>")
        tabs = [p for p in NAV_PAGES if p.tab]
        assert re.findall(r'<a href="([^"]+)" class="sova-tab"', bar) == [p.path for p in tabs]
        assert [p.label for p in tabs] == ["Home", "Work", "Supervisor", "Settings"]
        gap = bar.index("sova-tabbar-gap")
        assert bar.index('href="/work"') < gap < bar.index('href="/supervisor"')
        assert re.findall(r'class="sova-tab" aria-current="page"', bar) == ['class="sova-tab" aria-current="page"']
        assert '<a href="/work" class="sova-tab" aria-current="page">' in bar

    async def test_the_owl_in_the_bar_is_the_feed_button_through_the_owl_helper(self, client: AsyncClient) -> None:
        html = (await client.get("/work")).text
        fab = _between(html, '<button type="button" id="feed-fab"', "</button>")
        assert 'class="sova-owl sova-owl-lg"' in fab
        assert 'id="feed-badge"' in fab

    async def test_the_more_sheet_lists_every_page_the_bar_does_not(self, client: AsyncClient) -> None:
        html = (await client.get("/costs")).text
        sheet = _between(html, '<aside id="more-sheet"', "</aside>")
        expected = [p.path for p in NAV_PAGES if not p.tab and not p.needs_awareness]
        assert re.findall(r'<a href="([^"]+)" class="sova-more-link"', sheet) == expected
        assert '<a href="/costs" class="sova-more-link" aria-current="page">' in sheet
        assert 'id="more-sheet-provider-slot"' in sheet
        assert 'id="more-sheet-resources-slot"' in sheet
        assert "sova-side-panel" in sheet.split(">", 1)[0]
        assert 'aria-hidden="true" inert' in sheet.split(">", 1)[0], "closed before its module runs"
        assert "Switch project" not in sheet, "a single project has nothing to switch to"
        # The slots hold nothing until phone-shell.js moves a widget in, so the stylesheet's :empty hides a bare one.
        assert '<div id="more-sheet-provider-slot"></div>' in sheet
        assert '<div id="more-sheet-resources-slot"></div>' in sheet

    async def test_the_top_bar_has_its_dot_chip_and_menu(self, client: AsyncClient) -> None:
        html = (await client.get("/work")).text
        top = _between(html, '<header id="phone-topbar"', "</header>")
        assert 'id="topbar-activity-dot"' in top
        assert re.search(r'<a id="needs-you-chip" href="/phone" class="sova-needs-chip" hidden>', top)
        assert re.search(r'id="phone-menu-btn"[^>]*aria-expanded="false" aria-controls="more-sheet"', top)
        assert 'class="sova-owl sova-owl-sm"' in top, "the top bar owl uses the owl helper"
        assert "import('/static/js/components/phone-shell.js')" in html


class TestShellStyle:
    def test_the_shell_is_hidden_on_wider_screens(self) -> None:
        head = _STYLE_CSS[: _STYLE_CSS.index(_PHONE_MEDIA)]
        # The phone-only sheets of single pages (the Supervisor's graph sheet) join the same rule.
        assert re.search(r"\.sova-topbar,\s*\.sova-tabbar,\s*\.sova-more-sheet,?[^{]*\{ display: none; \}", head)
        block = _phone_block()
        assert "display: none" in _rule(block, ".sidebar")
        assert "margin-left: 0" in _rule(block, "main.sidebar-main")

    def test_the_tab_bar_blurs_and_falls_back_to_a_solid_surface(self) -> None:
        bar = _rule(_phone_block(), ".sova-tabbar")
        assert "backdrop-filter: blur(" in bar and "-webkit-backdrop-filter: blur(" in bar
        assert "background: rgb(var(--ctp-base-rgb) / 0.62)" in bar
        fallback = re.search(
            r"@supports not \(\(-webkit-backdrop-filter: blur\(1px\)\) or \(backdrop-filter: blur\(1px\)\)\) \{"
            r"\s*\.sova-tabbar \{ background: var\(--color-surface-card\); \}",
            _STYLE_CSS,
        )
        assert fallback, "a browser without backdrop-filter gets a solid bar"

    def test_safe_areas_and_dynamic_viewport_height(self) -> None:
        block = _phone_block()
        assert "env(safe-area-inset-top" in _rule(block, ".sova-topbar")
        assert "env(safe-area-inset-bottom" in block.split("--phone-bar-bottom:", 1)[1].split(";", 1)[0]
        sheet = _rule(block, ".sova-side-panel")
        assert "height: 100dvh" in sheet
        assert "padding-top: env(safe-area-inset-top" in sheet
        assert "padding-bottom: env(safe-area-inset-bottom" in sheet
        assert not re.search(r"\d+vh\b", re.sub(r"/\*.*?\*/", "", block, flags=re.DOTALL)), "no vh in the shell"
        assert "min-height: 100dvh" in _rule(block, ".min-h-screen")

    def test_the_wide_layout_pads_its_fixed_edges_with_the_safe_area_insets(self) -> None:
        """viewport-fit=cover applies at every width, so a notched phone in landscape or an iPad opened from the home
        screen draws the desktop layout under the notch and the home indicator unless its edges pad themselves.
        env() is 0 without an inset, so a desktop is unchanged. These rules sit outside the phone block."""
        wide = _STYLE_CSS[: _STYLE_CSS.index(_PHONE_MEDIA)]
        expected = {
            ".sidebar": ("inset-top", "inset-bottom", "inset-left"),
            ".sidebar-main": ("inset-left",),
            "main.sidebar-main": ("inset-top", "inset-right", "inset-bottom"),
            ".sidebar-batch-bar": ("inset-left",),
            ".feed-fab": ("inset-bottom", "inset-right"),
            ".resource-widget": ("inset-bottom", "inset-right"),
            "#toast-container": ("inset-top", "inset-right"),
        }
        for selector, insets in expected.items():
            rule = _rule(wide, selector)
            for inset in insets:
                assert f"env(safe-area-{inset}" in rule, f"{selector} ignores safe-area-{inset}"

    def test_a_hidden_needs_you_chip_stays_hidden(self) -> None:
        """The chip sets display, which beats the user agent's [hidden] rule at equal specificity."""
        assert "display: inline-flex" in _rule(_phone_block(), ".sova-needs-chip")
        assert "display: none" in _rule(_phone_block(), ".sova-needs-chip[hidden]")

    def test_the_home_screen_colours_match_the_stylesheet_tokens(self) -> None:
        from sova.dashboard import app as dashboard_app

        def token(name: str) -> str:
            match = re.search(rf"--{name}:\s*(#[0-9a-fA-F]{{6}});", _STYLE_CSS)
            assert match, name
            return match.group(1).lower()

        # The top bar and the manifest's theme colour are the card surface (--color-surface-card), the page behind
        # it the surface (--color-surface); both resolve to these Catppuccin tokens.
        assert re.search(r"--color-surface-card:\s*var\(--ctp-base\);", _STYLE_CSS)
        assert re.search(r"--color-surface:\s*var\(--ctp-mantle\);", _STYLE_CSS)
        assert dashboard_app._APP_THEME_COLOR == token("ctp-base")
        assert dashboard_app._APP_BACKGROUND_COLOR == token("ctp-mantle")

    def test_sheets_rise_from_the_bottom_and_lock_the_page(self) -> None:
        block = _phone_block()
        assert "transform: translateY(100%)" in _rule(block, ".sova-side-panel")
        assert "transform: none" in _rule(block, ".sova-side-panel[data-open]")
        assert "transition" not in _rule(block, ".sova-side-panel"), "the curves stay .sova-side-panel's own"
        assert "overflow: hidden" in _rule(block, "body[data-side-panel]")
        assert "order: 1" in _rule(block, ".sova-work-drawer-actions"), "the ticket's actions pin to the bottom"

    def test_touch_targets_and_16px_fields(self) -> None:
        block = _phone_block()
        assert "min-height: 44px" in _rule(block, ".sova-btn")
        assert "height: 44px" in _rule(block, ".sova-stepper .sova-stepper-btn")
        assert "min-height: 44px" in _rule(block, ".sova-toggle")
        assert "min-height: 44px" in _rule(block, ".sova-input")
        fields = re.search(
            r"\.sova-input,\s*textarea,\s*select,\s*input:not[^{]*\{\s*font-size: 16px !important;", block
        )
        assert fields, "every text field uses 16px text so iOS does not zoom on focus"

    def test_a_tooltip_never_sticks_after_a_tap(self) -> None:
        media = _STYLE_CSS[_STYLE_CSS.index("@media (hover: none) {") :]
        media = media[: media.index("\n}\n")]
        assert "[data-tooltip]:not(:focus-visible)::after { display: none !important; }" in media

    def test_fixed_layers_stay_clear_of_the_tab_bar(self) -> None:
        block = _phone_block()
        tokens = {name: int(value) for name, value in re.findall(r"--z-([a-z]+):\s*(\d+);", _STYLE_CSS)}
        assert tokens["dropdown"] < tokens["bar"] < tokens["float"] < tokens["panel"]
        assert "z-index: var(--z-bar)" in _rule(block, ".sova-tabbar")
        assert "calc(var(--phone-bar-bottom) + 100px)" in _rule(block, "#global-batch-bar"), "above the bar and owl"
        assert "var(--phone-topbar-height)" in _rule(block, "#toast-container")
        assert "display: none !important" in _rule(block, "body > #resource-widget")

    def test_the_agents_widget_stays_visible_inside_the_open_more_sheet(self) -> None:
        """The widget moved into the sheet keeps `.sova-float`, which an open sheet hides (and the 'hide' state of
        the sheet itself too), so a rule must restore it, with more specificity than both hiding rules."""
        block = _phone_block()
        hide = _rule(block, "body[data-side-panel] .sova-float")
        assert "visibility: hidden" in hide
        keep = _rule(block, "body[data-side-panel] .sova-more-sheet .sova-float")
        assert "visibility: visible" in keep and "opacity: 1" in keep and "pointer-events: auto" in keep
        assert "translate: none" in keep
        assert block.index("body[data-side-panel] .sova-more-sheet .sova-float") > block.index(
            "body[data-side-panel] .sova-float {"
        )
        base = (DASHBOARD_DIR / "templates" / "base.html").read_text(encoding="utf-8")
        assert re.search(r'id="resource-widget" class="[^"]*\bsova-float\b', base), "the premise of the rule above"

    def test_the_more_sheet_scrim_loses_its_motion_under_reduced_motion(self) -> None:
        last = _STYLE_CSS[_STYLE_CSS.rindex("@media (prefers-reduced-motion: reduce) {") :]
        assert 'body[data-side-panel="more"] .feed-scrim' in last


_SHELL_HARNESS = r"""
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';

const [, , shellPath, fakeDomPath] = process.argv;
const require = createRequire(import.meta.url);
const { Document, key, clickOn } = require(fakeDomPath);

const doc = new Document();
globalThis.document = doc;
globalThis.window = globalThis;
doc.body.style.setProperty = function(k, v) { this[k] = v; };
doc.body.style.removeProperty = function(k) { delete this[k]; };
globalThis.getComputedStyle = () => ({ overflowY: 'visible', overflowX: 'visible' });
const query = { matches: true, listeners: [], addEventListener(type, fn) { this.listeners.push(fn); } };
globalThis.matchMedia = (q) => { query.media = q; return query; };

doc.body.innerHTML =
  '<nav class="sidebar"><div id="auth-widget">Provider</div></nav>' +
  '<header><button type="button" id="phone-menu-btn">Menu</button></header>' +
  '<aside id="more-sheet" class="sova-side-panel">' +
    '<button type="button" id="more-close" data-panel-close>Close</button>' +
    '<div id="more-sheet-provider-slot"></div><div id="more-sheet-resources-slot"></div>' +
  '</aside>' +
  '<p id="outside">Page</p>' +
  '<div id="resource-widget">Agents</div>';
const $ = (id) => doc.getElementById(id);
const parentOf = (id) => $(id).parentNode.id || $(id).parentNode.tagName;

const shell = await import(pathToFileURL(shellPath).href);
const out = { media: null };
// The fake DOM's insertBefore keeps a node in place when it is its own reference, as the DOM does.
const probe = doc.createElement('div');
doc.body.appendChild(probe);
const before = doc.body.childNodes.length;
doc.body.insertBefore(probe, probe);
out.selfInsert = [before, doc.body.childNodes.length, doc.body.childNodes.filter((n) => n === probe).length];
shell.init();
out.media = query.media;
out.phonePlaces = [parentOf('auth-widget'), parentOf('resource-widget')];
const sheetEl = $('more-sheet');
out.closedAtStart = [shell.isOpen(), sheetEl.getAttribute('aria-hidden'), sheetEl.hasAttribute('inert')];

$('phone-menu-btn').focus();
clickOn($('phone-menu-btn'));
out.opened = [shell.isOpen(), doc.body.getAttribute('data-side-panel'), doc.body.getAttribute('data-side-panel-floats'),
  $('phone-menu-btn').getAttribute('aria-expanded')];
clickOn($('phone-menu-btn'));
out.toggledClosed = shell.isOpen();
clickOn($('phone-menu-btn'));
key(doc, 'Escape');
out.escape = [shell.isOpen(), doc.activeElement && doc.activeElement.id];
clickOn($('phone-menu-btn'));
clickOn($('outside'));
out.outside = shell.isOpen();

// Widening the window puts the widgets back where they were and closes an open sheet.
clickOn($('phone-menu-btn'));
query.matches = false;
query.listeners.forEach((fn) => fn());
out.widePlaces = [parentOf('auth-widget'), parentOf('resource-widget')];
out.wideOrder = doc.body.children.map((el) => el.id || el.tagName);
out.closedWhenWide = shell.isOpen();
query.matches = true;
query.listeners.forEach((fn) => fn());
out.phoneAgain = [parentOf('auth-widget'), parentOf('resource-widget')];
console.log(JSON.stringify(out));
"""


def _require_node(reason: str) -> None:
    if shutil.which("node") is not None:
        return
    if os.environ.get("CI"):
        pytest.fail(f"node is required in CI: {reason}")
    pytest.skip(reason)


@pytest.fixture(scope="module")
def shell_run(tmp_path_factory: pytest.TempPathFactory) -> dict:
    _require_node("node is required to run the phone shell behavior tests")
    script: Path = tmp_path_factory.mktemp("shell") / "harness.mjs"
    script.write_text(_SHELL_HARNESS, encoding="utf-8")
    proc = subprocess.run(
        ["node", str(script), str(_PHONE_SHELL_JS), str(_FAKE_DOM)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


class TestPhoneShellModule:
    def test_it_uses_the_stylesheets_breakpoint(self, shell_run: dict) -> None:
        assert shell_run["media"] == "(max-width: 767.98px)"
        assert _PHONE_MEDIA in _STYLE_CSS

    def test_on_a_phone_the_widgets_move_into_the_more_sheet(self, shell_run: dict) -> None:
        assert shell_run["phonePlaces"] == ["more-sheet-provider-slot", "more-sheet-resources-slot"]

    def test_the_fake_dom_leaves_a_node_inserted_before_itself_in_place(self, shell_run: dict) -> None:
        before, after, copies = shell_run["selfInsert"]
        assert before == after
        assert copies == 1

    def test_the_sheet_starts_closed_and_the_menu_toggles_it(self, shell_run: dict) -> None:
        assert shell_run["closedAtStart"] == [False, "true", True]
        assert shell_run["opened"] == [True, "more", "hide", "true"]
        assert shell_run["toggledClosed"] is False

    def test_escape_and_an_outside_tap_close_it(self, shell_run: dict) -> None:
        assert shell_run["escape"] == [False, "phone-menu-btn"]
        assert shell_run["outside"] is False

    def test_a_wide_window_puts_the_widgets_back_and_closes_the_sheet(self, shell_run: dict) -> None:
        assert shell_run["widePlaces"] == ["NAV", "BODY"]
        order = shell_run["wideOrder"]
        assert order.index("outside") < order.index("resource-widget"), "back in its own place, not appended"
        assert shell_run["closedWhenWide"] is False
        assert shell_run["phoneAgain"] == ["more-sheet-provider-slot", "more-sheet-resources-slot"]


class TestHomeAndWorkOnThePhone:
    """The stylesheet contract for Home and the Work page under 768 px (#1565); the layout itself was measured in
    headless Chrome at 390 px (no sideways scroll) and at 768 and 1440 px (identical to main)."""

    def test_the_work_tiles_are_one_strip_that_scrolls_sideways(self) -> None:
        strip = _rule(_phone_block(), ".sova-work-tiles")
        assert "flex-wrap: nowrap" in strip
        assert "overflow-x: auto" in strip
        assert "flex: 0 0 auto" in _rule(_phone_block(), ".sova-work-tiles > .sova-work-stat")

    def test_the_board_shows_one_stage_per_screen_and_snaps_to_each(self) -> None:
        block = _phone_block()
        assert "scroll-snap-type: x mandatory" in _rule(block, ".sova-work-board")
        assert "grid-auto-columns: 100%" in _rule(block, ".sova-work-track")
        assert "scroll-snap-align: start" in _rule(block, ".sova-work-col")
        assert "scroll-snap-stop: always" in _rule(block, ".sova-work-col")

    def test_the_stage_chips_show_only_on_the_phone_and_are_tap_targets(self) -> None:
        outside = _STYLE_CSS.replace(_phone_block(), "")
        assert re.search(r"(?m)^\.sova-stage-chips \{ display: none; \}", outside)
        assert "display: flex" in _rule(_phone_block(), ".sova-stage-chips")
        assert "min-height: 44px" in _rule(_phone_block(), ".sova-stage-chip")

    def test_the_run_history_stacks_its_rows(self) -> None:
        block = _phone_block()
        assert "display: none" in _rule(block, ".sova-history-runs thead")
        assert "flex-wrap: wrap" in _rule(block, ".sova-history-runs tr")

    def test_the_small_toggle_groups_and_search_results_are_tap_targets(self) -> None:
        block = _phone_block()
        assert "min-height: 44px" in _rule(block, ".sova-segment > button")
        assert "min-height: 44px" in _rule(block, ".sova-work-search-row")

    def test_a_touch_screen_is_not_told_to_drag(self) -> None:
        hover = _STYLE_CSS[_STYLE_CSS.index("@media (hover: none) {") :]
        assert re.search(r"\.sova-drag-hint \{ display: none; \}", hover[: hover.index("\n}\n")])
        html = (DASHBOARD_DIR / "templates" / "work.html").read_text(encoding="utf-8")
        assert '<span class="sova-drag-hint">Drag an Up next card into Queued or Running. </span>' in html

    def test_the_pull_requests_stack_below_768_and_keep_one_line_above(self) -> None:
        source = (_STATIC / "js" / "pages" / "agents" / "pr-tracker.js").read_text(encoding="utf-8")
        assert "flex items-center gap-3 max-md:flex-wrap" in source
        assert "truncate flex-1 max-md:basis-full max-md:order-first" in source


class TestRemainingPagesOnThePhone:
    """The other pages get the shared baseline only (#1566): at 390 px no page scrolls sideways, and a wide table
    scrolls inside its own box. Measured in headless Chrome with a long unbroken string in every table cell; the
    boxes that were missing are max-md: only, so 768 px and wider keep their layout."""

    # Every element a page renders a table into.
    TABLE_BOXES = (
        ("oversight.html", "runs-container"),
        ("fleet.html", "step-failures"),
        ("fleet.html", "error-clusters"),
        ("fleet.html", "cost-breakdown"),
        ("reliability.html", "success-by-role"),
        ("reliability.html", "failure-taxonomy"),
        ("reliability.html", "usage-by-role"),
        ("reliability.html", "usage-by-step"),
        ("pr_metrics.html", "author-table"),
        ("costs.html", "by-routing"),
        ("costs.html", "by-issue"),
        ("costs.html", "by-phase"),
        ("costs.html", "by-model"),
        ("dependency_health.html", "package-table"),
    )

    @pytest.mark.parametrize(("template", "box_id"), TABLE_BOXES)
    def test_each_table_scrolls_inside_its_own_box(self, template: str, box_id: str) -> None:
        html = (DASHBOARD_DIR / "templates" / template).read_text(encoding="utf-8")
        tag = re.search(rf'<div[^>]*\bid="{box_id}"[^>]*>', html)
        assert tag, f"{template} has no #{box_id}"
        class_attr = re.search(r'class="([^"]*)"', tag.group(0))
        assert class_attr, tag.group(0)
        tokens = class_attr.group(1).split()
        assert {"overflow-x-auto", "max-md:overflow-x-auto"} & set(tokens), tag.group(0)

    def test_the_boxes_added_for_the_phone_leave_768_px_and_wider_alone(self) -> None:
        for template, box_id in (("oversight.html", "runs-container"), ("fleet.html", "error-clusters")):
            html = (DASHBOARD_DIR / "templates" / template).read_text(encoding="utf-8")
            assert f'id="{box_id}" class="p-4 max-md:overflow-x-auto"' in html

    def test_long_unbroken_text_wraps(self) -> None:
        block = _phone_block()
        assert "overflow-wrap: anywhere" in _rule(block, "#spend-by-outcome .sova-stat-label")
        assert "overflow-wrap: anywhere" in _rule(block, "main .whitespace-pre-wrap,\n  #drift-files")
