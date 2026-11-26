"""The Supervisor page (templates/supervisor.html): its page script and its layout on the phone.

There is no browser here, so the markup is asserted on the rendered page and the layout contract on the shipped
stylesheet; the measured layout at 390 and 1440 px is checked by driving a real browser before merge.
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

from tests.conftest import DASHBOARD_DIR
from tests.dashboard_helpers import css_rule as _rule
from tests.dashboard_helpers import phone_css_block as _phone_block

pytestmark = pytest.mark.usefixtures("reset_db_rows")

_TEMPLATE = (DASHBOARD_DIR / "templates" / "supervisor.html").read_text(encoding="utf-8")


def _page_script() -> str:
    """The page's own classic script block."""
    scripts = _TEMPLATE[_TEMPLATE.index("{% block scripts %}") :]
    start = scripts.index("<script>")
    return scripts[start : scripts.index("</script>", start)]


class TestPageScript:
    def test_every_variable_the_unload_handler_reads_is_declared(self) -> None:
        """The page left its own socket for the shared one (subscribeSovaSocket); a handler still closing the old
        one threw a ReferenceError on every navigation away from the page."""
        script = _page_script()
        handler = script[script.index("addEventListener('beforeunload'") :]
        for name in set(re.findall(r"\b(_[A-Za-z]\w*)\b", handler)):
            assert re.search(rf"\b(?:let|const|var|function)\s+{name}\b", script), f"{name} is never declared"

    async def test_the_page_renders(self, client: AsyncClient) -> None:
        response = await client.get("/supervisor")
        assert response.status_code == 200
        assert 'id="supervisor-switches"' in response.text


# --- The phone (#1566) ------------------------------------------------------------------------------------------

_STATIC = DASHBOARD_DIR / "static"
_STYLE_CSS = (_STATIC / "style.css").read_text(encoding="utf-8")
_GRAPH_SHEET_JS = _STATIC / "js" / "pages" / "supervisor" / "graph-sheet.js"
_FAKE_DOM = Path(__file__).parent / "fixtures" / "fake_dom.cjs"


class TestPhoneMarkup:
    async def test_the_phone_parts_are_hidden_from_768_px(self, client: AsyncClient) -> None:
        html = (await client.get("/supervisor")).text
        for part in (
            'id="supervisor-off-note-phone" class="hidden md:hidden',
            'id="pending-empty" class="md:hidden',
            'id="graph-open-btn" class="sova-sup-graph-entry md:hidden"',
        ):
            assert part in html

    async def test_the_graph_row_opens_the_graph_sheet(self, client: AsyncClient) -> None:
        html = (await client.get("/supervisor")).text
        assert 'aria-controls="graph-sheet"' in html
        sheet = html[html.index('<aside id="graph-sheet"') :]
        sheet = sheet[: sheet.index("</aside>")]
        assert 'class="sova-side-panel sova-graph-sheet"' in sheet
        assert "data-panel-close" in sheet
        assert 'id="graph-sheet-slot"' in sheet
        assert "import { init } from '/static/js/pages/supervisor/graph-sheet.js';" in html

    async def test_the_switches_are_one_column_below_768_px(self, client: AsyncClient) -> None:
        html = (await client.get("/supervisor")).text
        switches = html[html.index('id="supervisor-switches"') : html.index('id="graph-open-btn"')]
        assert "sm:grid-cols-2" not in switches
        assert "md:grid-cols-2" in switches
        assert switches.count('class="sova-sup-switch-row') == 13

    def test_the_off_note_shows_in_both_places(self) -> None:
        assert "['supervisor-off-note', 'supervisor-off-note-phone']" in _page_script()

    def test_a_pending_item_wraps_its_reason_onto_its_own_line(self) -> None:
        script = _page_script()
        assert "truncate max-md:basis-full max-md:whitespace-normal" in script
        assert "shrink-0 max-md:ml-auto" in script


class TestPhoneStyle:
    def test_the_page_leads_with_the_switch_the_note_the_actions_and_the_graph(self) -> None:
        block = _phone_block()
        assert "flex-direction: column" in _rule(block, ".sova-sup-page")
        order = [
            "#supervisor-head",
            "#supervisor-off-note-phone",
            "#pending-empty",
            "#graph-open-btn",
            "#supervisor-switches",
        ]
        values = [int(re.search(r"order: (-?\d+)", _rule(block, sel)).group(1)) for sel in order]
        assert values == sorted(values)
        assert len(set(values)) == len(values)
        assert "order: -3" in _rule(block, "#pending-plan,\n  #pending-empty")
        assert "display: none" in _rule(block, "#pending-plan:not(.hidden) + #pending-empty")

    def test_switch_rows_and_the_graph_row_are_tap_targets(self) -> None:
        block = _phone_block()
        assert "min-height: 44px" in _rule(block, ".sova-sup-switch-row")
        assert "min-height: 56px" in _rule(block, ".sova-sup-graph-entry")
        assert "min-height: 44px" in _rule(block, ".sova-sup-settings-link")

    def test_the_graph_sheet_shows_only_on_the_phone_and_fills_it(self) -> None:
        outside = _STYLE_CSS.replace(_phone_block(), "")
        assert re.search(r"\.sova-graph-sheet \{ display: none; \}", outside)
        block = _phone_block()
        assert "display: flex" in _rule(block, ".sova-side-panel.sova-graph-sheet")
        container = _rule(block, ".sova-graph-sheet #graph-container")
        assert "flex: 1" in container
        assert "height: auto !important" in container
        assert "touch-action: none" in _rule(block, ".sova-graph-sheet #graph-svg")
        assert "overflow-x: auto" in _rule(block, ".sova-graph-sheet #graph-legend")


_GRAPH_HARNESS = r"""
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';

const [, , modulePath, fakeDomPath] = process.argv;
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
  '<div id="supervisor-page"><button type="button" id="graph-open-btn">Graph</button>' +
  '<div id="graph-card">Graph</div><div id="action-drawer">Drawer</div><p id="outside">Page</p></div>' +
  '<aside id="graph-sheet" class="sova-side-panel">' +
    '<button type="button" id="graph-close" data-panel-close>Close</button><div id="graph-sheet-slot"></div>' +
  '</aside>';
const $ = (id) => doc.getElementById(id);
const parentOf = (id) => $(id).parentNode.id;
let refits = 0;

const sheet = await import(pathToFileURL(modulePath).href);
sheet.init({ refit: () => { refits += 1; } });
const out = { media: query.media };
out.phonePlaces = [parentOf('graph-card'), parentOf('action-drawer'), refits];
out.closedAtStart = [sheet.isOpen(), $('graph-sheet').getAttribute('aria-hidden')];
$('graph-open-btn').focus();
clickOn($('graph-open-btn'));
out.opened = [sheet.isOpen(), doc.body.getAttribute('data-side-panel'),
  $('graph-open-btn').getAttribute('aria-expanded'), refits];
key(doc, 'Escape');
out.escape = [sheet.isOpen(), doc.activeElement && doc.activeElement.id];
clickOn($('graph-open-btn'));
query.matches = false;
query.listeners.forEach((fn) => fn());
out.wide = [sheet.isOpen(), parentOf('graph-card'), parentOf('action-drawer'), refits];
out.wideOrder = $('supervisor-page').children.map((el) => el.id);
console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def graph_run(tmp_path_factory: pytest.TempPathFactory) -> dict:
    if shutil.which("node") is None:
        if os.environ.get("CI"):
            pytest.fail("node is required in CI to run the graph sheet module")
        pytest.skip("node is required to run the graph sheet module")
    script: Path = tmp_path_factory.mktemp("graph") / "harness.mjs"
    script.write_text(_GRAPH_HARNESS, encoding="utf-8")
    proc = subprocess.run(
        ["node", str(script), str(_GRAPH_SHEET_JS), str(_FAKE_DOM)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


class TestGraphSheetModule:
    def test_on_a_phone_the_graph_and_its_drawer_live_in_the_sheet(self, graph_run: dict) -> None:
        assert graph_run["media"] == "(max-width: 767.98px)"
        assert graph_run["phonePlaces"] == ["graph-sheet-slot", "graph-sheet-slot", 1]

    def test_the_row_opens_it_and_fits_the_graph(self, graph_run: dict) -> None:
        assert graph_run["closedAtStart"] == [False, "true"]
        assert graph_run["opened"] == [True, "graph", "true", 2]

    def test_escape_closes_it_and_returns_focus_to_the_row(self, graph_run: dict) -> None:
        assert graph_run["escape"] == [False, "graph-open-btn"]

    def test_a_wide_window_closes_it_and_puts_the_graph_back_in_place(self, graph_run: dict) -> None:
        closed, card, drawer, refits = graph_run["wide"]
        assert [closed, card, drawer] == [False, "supervisor-page", "supervisor-page"]
        assert refits == 4, "fitted on open, and again after the move back"
        order = graph_run["wideOrder"]
        places = [order.index(part) for part in ("graph-open-btn", "graph-card", "action-drawer", "outside")]
        assert places == sorted(places)
