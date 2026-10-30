"""Sidebar footer account row: the LLM provider status widget lives in the sidebar footer (#1235).

The floating bottom-right toggle is gone. The footer row shows the status dot, the provider name and the
configured GitHub user, with the version beneath it, and the same popover opens upward from it.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from sova.dashboard.services import setup_service
from sova.db.session import close_db, init_db
from tests.conftest import DASHBOARD_DIR
from tests.conftest import csrf_request_headers as _csrf_request_headers

_STATIC = DASHBOARD_DIR / "static"
_WIDGET_JS_PATH = _STATIC / "provider-status-widget.js"
_WIDGET_JS = _WIDGET_JS_PATH.read_text(encoding="utf-8")
_APP_JS = (_STATIC / "app.js").read_text(encoding="utf-8")
_STYLE_CSS = (_STATIC / "style.css").read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
async def setup_db(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    monkeypatch.setenv("SOVA_DATABASE_URL", "sqlite+aiosqlite://")
    await init_db(run_migrations=False)
    yield
    await close_db()


@pytest.fixture(autouse=True)
def clear_github_user_cache() -> Iterator[None]:
    setup_service._github_user_cache.clear()
    yield
    setup_service._github_user_cache.clear()


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    from sova.dashboard.app import create_app

    app = create_app(project_dir=Path.cwd())
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        c.headers.update(_csrf_request_headers(c))
        yield c


def _footer_html(page: str) -> str:
    """The rendered sidebar footer block, from its opening tag to the closing `</nav>`."""
    start = page.index('class="sidebar-footer')
    return page[start : page.index("</nav>", start)]


def _block_after(css: str, selector: str) -> str:
    """The body of the first rule whose selector is exactly `selector`."""
    match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
    assert match, f"no rule for {selector}"
    return match.group(1)


class TestConfiguredGithubUser:
    def test_returns_configured_user(self, tmp_path: Path) -> None:
        with patch("sova.config.loader.load_config", return_value=SimpleNamespace(github_user="xsovad06")):
            assert setup_service.get_github_user(tmp_path) == "xsovad06"

    @pytest.mark.parametrize("raw", ["", "   ", None])
    def test_unset_user_is_empty_string(self, tmp_path: Path, raw: str | None) -> None:
        with patch("sova.config.loader.load_config", return_value=SimpleNamespace(github_user=raw)):
            assert setup_service.get_github_user(tmp_path) == ""

    def test_config_failure_returns_none(self, tmp_path: Path) -> None:
        with patch("sova.config.loader.load_config", side_effect=RuntimeError("bad config")):
            assert setup_service.get_github_user(tmp_path) is None

    def test_config_without_a_github_user_attribute_returns_none(self, tmp_path: Path) -> None:
        with patch("sova.config.loader.load_config", return_value=SimpleNamespace(github_repo="owner/repo")):
            assert setup_service.get_github_user(tmp_path) is None

    def test_successful_read_is_cached_within_the_ttl(self, tmp_path: Path) -> None:
        with patch("sova.config.loader.load_config", return_value=SimpleNamespace(github_user="xsovad06")) as mock_load:
            assert setup_service.get_github_user(tmp_path) == "xsovad06"
            assert setup_service.get_github_user(tmp_path) == "xsovad06"
        mock_load.assert_called_once_with(tmp_path)

    def test_unset_user_is_cached_too(self, tmp_path: Path) -> None:
        with patch("sova.config.loader.load_config", return_value=SimpleNamespace(github_user=None)) as mock_load:
            assert setup_service.get_github_user(tmp_path) == ""
            assert setup_service.get_github_user(tmp_path) == ""
        mock_load.assert_called_once()

    def test_cache_expires_after_the_ttl(self, tmp_path: Path) -> None:
        with (
            patch("sova.config.loader.load_config", return_value=SimpleNamespace(github_user="xsovad06")) as mock_load,
            patch("sova.dashboard.services.setup_service.time.monotonic", side_effect=[1000.0, 1061.0]),
        ):
            setup_service.get_github_user(tmp_path)
            setup_service.get_github_user(tmp_path)
        assert mock_load.call_count == 2

    def test_failure_is_not_cached(self, tmp_path: Path) -> None:
        with patch("sova.config.loader.load_config", side_effect=RuntimeError("bad config")):
            assert setup_service.get_github_user(tmp_path) is None
        with patch("sova.config.loader.load_config", return_value=SimpleNamespace(github_user="xsovad06")):
            assert setup_service.get_github_user(tmp_path) == "xsovad06"

    def test_cache_is_keyed_per_project(self, tmp_path: Path) -> None:
        a, b = tmp_path / "a", tmp_path / "b"
        users = {a: "alice", b: "bob"}
        with patch("sova.config.loader.load_config", side_effect=lambda d: SimpleNamespace(github_user=users[d])):
            assert setup_service.get_github_user(a) == "alice"
            assert setup_service.get_github_user(b) == "bob"

    def test_none_project_dir_falls_back_to_default_project(self, tmp_path: Path) -> None:
        with (
            patch("sova.dashboard.services.agent_pool.get_default_project_dir", return_value=tmp_path),
            patch("sova.config.loader.load_config", return_value=SimpleNamespace(github_user="xsovad06")) as mock_load,
        ):
            assert setup_service.get_github_user(None) == "xsovad06"
        mock_load.assert_called_once_with(tmp_path)


class TestSidebarFooterMarkup:
    async def test_footer_hosts_status_row_and_popover(self, client: AsyncClient) -> None:
        with patch("sova.dashboard.services.setup_service.get_github_user", return_value="xsovad06"):
            resp = await client.get("/agents")
        assert resp.status_code == 200
        footer = _footer_html(resp.text)
        for element_id in (
            "auth-widget-toggle",
            "auth-widget-dot",
            "auth-widget-label",
            "auth-widget-panel",
            "aw-provider-val",
            "aw-account-val",
            "aw-org-val",
            "aw-checked-val",
            "aw-recovery-detail",
            "aw-warnings-cmd",
            "aw-error-section",
        ):
            assert f'id="{element_id}"' in footer, f"{element_id} must live inside the sidebar footer"

    async def test_widget_is_not_rendered_outside_the_sidebar(self, client: AsyncClient) -> None:
        resp = await client.get("/agents")
        assert resp.text.count('id="auth-widget"') == 1
        assert resp.text.index('id="auth-widget"') < resp.text.index("</nav>")

    async def test_footer_shows_github_user_and_version_beneath(self, client: AsyncClient) -> None:
        with patch("sova.dashboard.services.setup_service.get_github_user", return_value="xsovad06"):
            resp = await client.get("/agents")
        footer = _footer_html(resp.text)
        assert 'id="auth-widget-user"' in footer
        assert "xsovad06" in footer
        assert footer.index('id="auth-widget-toggle"') < footer.index("SOVA v0.1.0")
        assert footer.index("SOVA v0.1.0") < footer.index('id="auth-widget-panel"')

    async def test_unset_github_user_omits_the_segment(self, client: AsyncClient) -> None:
        with patch("sova.dashboard.services.setup_service.get_github_user", return_value=""):
            resp = await client.get("/agents")
        footer = _footer_html(resp.text)
        assert 'id="auth-widget-user"' not in footer
        assert "None" not in footer

    async def test_github_user_is_escaped(self, client: AsyncClient) -> None:
        with patch("sova.dashboard.services.setup_service.get_github_user", return_value="<b>x</b>"):
            resp = await client.get("/agents")
        footer = _footer_html(resp.text)
        assert "<b>x</b>" not in footer
        assert "&lt;b&gt;x&lt;/b&gt;" in footer

    async def test_initial_state_matches_the_old_widget_before_first_poll(self, client: AsyncClient) -> None:
        resp = await client.get("/agents")
        footer = _footer_html(resp.text)
        assert 'class="resource-widget-dot resource-dot-green" id="auth-widget-dot"' in footer
        assert re.search(r'id="auth-widget-label"[^>]*>provider<', footer)

    async def test_panel_starts_closed_and_toggle_is_a_labelled_button(self, client: AsyncClient) -> None:
        resp = await client.get("/agents")
        footer = _footer_html(resp.text)
        assert re.search(r'<div id="auth-widget-panel" class="[^"]*\bhidden\b', footer)
        toggle = re.search(r'<button id="auth-widget-toggle"[^>]*>', footer)
        assert toggle, "the account row must be a button"
        assert 'type="button"' in toggle.group(0)
        assert 'aria-expanded="false"' in toggle.group(0)
        assert 'aria-label="Provider auth status"' in toggle.group(0)

    async def test_collapsed_sidebar_hides_text_and_keeps_the_dot(self, client: AsyncClient) -> None:
        """Provider, user and version reuse the existing collapse classes; the dot has none, so it stays visible."""
        resp = await client.get("/agents")
        footer = _footer_html(resp.text)
        dot = re.search(r'<span class="([^"]*)" id="auth-widget-dot"', footer)
        assert dot
        assert "sidebar-label" not in dot.group(1)
        assert "sidebar-hide" not in dot.group(1)
        # the provider and user sit inside a .sidebar-label wrapper (opacity/max-width collapse) ...
        text_wrapper = re.search(r'<span class="sidebar-label">(.*?)</button>', footer, re.S)
        assert text_wrapper
        assert 'id="auth-widget-label"' in text_wrapper.group(1)
        # ... and the version line uses .sidebar-hide so a collapsed footer is one line tall
        assert re.search(r'class="[^"]*\bsidebar-hide\b[^"]*">SOVA v0\.1\.0<', text_wrapper.group(1))

    async def test_manage_connections_link_unchanged(self, client: AsyncClient) -> None:
        resp = await client.get("/agents")
        footer = _footer_html(resp.text)
        assert re.search(r'<a href="/connections"[^>]*>Manage connections', footer)

    async def test_unreadable_github_user_omits_the_segment(self, client: AsyncClient) -> None:
        with patch("sova.dashboard.services.setup_service.get_github_user", return_value=None):
            resp = await client.get("/agents")
        assert resp.status_code == 200
        footer = _footer_html(resp.text)
        assert 'id="auth-widget-user"' not in footer
        assert "None" not in footer

    async def test_collapse_control_and_footer_do_not_shrink(self, client: AsyncClient) -> None:
        resp = await client.get("/agents")
        assert re.search(r'<div id="auth-widget" class="[^"]*\bshrink-0\b', resp.text)
        assert re.search(r'<!-- Collapse toggle -->\s*<div class="[^"]*\bshrink-0\b', resp.text)

    async def test_nav_link_list_is_the_scrolling_region(self, client: AsyncClient) -> None:
        """On a short viewport the link list scrolls; the collapse button and footer stay pinned below it."""
        resp = await client.get("/agents")
        nav = resp.text[resp.text.index("<nav") : resp.text.index("</nav>")]
        wrapper = re.search(r'<div class="([^"]*\bflex-1\b[^"]*)">\s*<a href=', nav)
        assert wrapper, "the nav links must sit in a flex-1 wrapper"
        assert "sidebar-nav-scroll" in wrapper.group(1).split()

    async def test_floating_toggle_markup_is_gone(self, client: AsyncClient) -> None:
        resp = await client.get("/agents")
        assert 'class="auth-widget"' not in resp.text
        assert "resource-widget-chevron" not in _footer_html(resp.text)


class TestFooterStyles:
    def test_floating_bottom_right_positioning_is_removed(self) -> None:
        widget = _block_after(_STYLE_CSS, ".auth-widget")
        assert "position: fixed" not in widget
        assert "right: 6rem" not in widget
        assert "bottom: 3.5rem" not in widget

    def test_footer_row_is_the_positioned_anchor(self) -> None:
        assert "position: relative" in _block_after(_STYLE_CSS, ".auth-widget")

    def test_popover_opens_upward_and_cannot_exceed_the_viewport(self) -> None:
        panel = _block_after(_STYLE_CSS, ".auth-widget-panel")
        assert "position: absolute" in panel
        assert "bottom: calc(100% + " in panel
        # a short viewport must shrink the popover, not clip it past the top edge
        assert re.search(r"max-height:\s*min\(480px,\s*calc\(100vh", panel)

    def test_popover_clears_the_sidebar_stacking_context_siblings(self) -> None:
        assert re.search(r"z-index:\s*\d+", _block_after(_STYLE_CSS, ".auth-widget-panel"))

    def test_nav_list_shrinks_and_scrolls_at_any_viewport_height(self) -> None:
        """One rule scales with the link count; fixed per-height padding breakpoints did not."""
        scroll = _block_after(_STYLE_CSS, ".sidebar-nav-scroll")
        assert "min-height: 0" in scroll
        assert "overflow-y: auto" in scroll
        assert not re.search(r"@media \(max-height: \d+px\)\s*\{\s*\.sidebar \.sidebar-nav-link", _STYLE_CSS)

    def test_collapsed_tooltips_are_fixed_so_the_scrolling_nav_cannot_clip_them(self) -> None:
        rule = _block_after(_STYLE_CSS, 'body[data-sidebar-collapsed="true"] .sidebar [data-tooltip]::after')
        assert "position: fixed" in rule
        assert re.search(r"top:\s*var\(--sidebar-tip-y", rule)
        assert re.search(r"left:\s*calc\(var\(--sidebar-width\)", rule)
        assert "left: 100%" not in rule

    def test_app_js_feeds_each_hovered_sidebar_element_its_tooltip_position(self) -> None:
        body = _APP_JS[_APP_JS.index("function initSidebarTooltips()") : _APP_JS.index("function toggleSidebar()")]
        assert "addEventListener('mouseover'" in body
        assert "getBoundingClientRect()" in body
        assert "setProperty('--sidebar-tip-y'" in body
        assert re.search(r"^initSidebarTooltips\(\);$", _APP_JS, re.MULTILINE)

    def test_footer_toggle_fills_the_row(self) -> None:
        toggle = _block_after(_STYLE_CSS, ".auth-widget-toggle")
        assert "width: 100%" in toggle
        assert "box-shadow" not in toggle  # no floating-chip chrome inside the sidebar


class TestWidgetScript:
    def test_polling_and_status_semantics_are_unchanged(self) -> None:
        assert "POLL_INTERVAL = 60000" in _WIDGET_JS
        assert "visibilityAwarePoll(poll, POLL_INTERVAL)" in _WIDGET_JS
        assert "fetchAPI(apiUrl('/auth/status'))" in _WIDGET_JS
        assert "renderUnavailable()" in _WIDGET_JS
        for token in ("resource-dot-red", "resource-dot-yellow", "resource-dot-green"):
            assert token in _WIDGET_JS

    def test_toggle_flips_the_popover_and_aria_expanded(self) -> None:
        assert "aria-expanded" in _WIDGET_JS
        assert "addEventListener('click'" in _WIDGET_JS

    def test_outside_click_and_escape_dismiss_the_popover(self) -> None:
        assert re.search(r"document\.addEventListener\('click'", _WIDGET_JS)
        assert "contains(" in _WIDGET_JS
        assert re.search(r"Escape", _WIDGET_JS)

    def test_collapse_button_is_covered_by_the_outside_click_handler(self) -> None:
        """The sidebar collapse button lives outside #auth-widget, so the outside-click handler closes the
        popover on collapse or expand; the widget must not stop that click from reaching `document`."""
        assert "stopPropagation" not in _WIDGET_JS
        assert "widget.contains(e.target)" in _WIDGET_JS

    def test_open_state_is_not_persisted_across_page_loads(self) -> None:
        assert "initWidgetToggle" not in _WIDGET_JS
        assert "sova-auth-widget-expanded" not in _WIDGET_JS


# Runs the real provider-status-widget.js in a `vm` sandbox against a small fake DOM, so the tests exercise
# the shipped script rather than a copy of its logic. Each poll step is answered with a payload or "fail"; each
# key step presses Escape with focus inside or outside the widget.
_WIDGET_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const [widgetPath, scenarioJson] = process.argv.slice(2);
const scenario = JSON.parse(scenarioJson);

const elements = {};
const docListeners = {};
const doc = { readyState: 'complete', activeElement: null };

function makeEl(id) {
  const hidden = new Set(id === 'auth-widget-panel' || id.endsWith('-section') ? ['hidden'] : []);
  const attrs = {};
  const listeners = {};
  const el = {
    id, textContent: '', className: '', focusCalls: 0,
    classList: {
      add: (c) => hidden.add(c),
      remove: (c) => hidden.delete(c),
      contains: (c) => hidden.has(c),
      toggle: (c, force) => { if (force) hidden.add(c); else hidden.delete(c); return force; },
    },
    setAttribute: (k, v) => { attrs[k] = String(v); },
    getAttribute: (k) => (k in attrs ? attrs[k] : null),
    addEventListener: (t, fn) => { (listeners[t] = listeners[t] || []).push(fn); },
    click: () => (listeners.click || []).forEach((fn) => fn({ target: el })),
    focus: () => { el.focusCalls += 1; doc.activeElement = el; },
    contains: (n) => n === el,
  };
  return el;
}
doc.getElementById = (id) => (elements[id] = elements[id] || makeEl(id));
doc.addEventListener = (t, fn) => { (docListeners[t] = docListeners[t] || []).push(fn); };

const widget = doc.getElementById('auth-widget');
const toggle = doc.getElementById('auth-widget-toggle');
const panel = doc.getElementById('auth-widget-panel');
widget.contains = (n) => n === widget || n === toggle || n === panel;
const outside = makeEl('somewhere-else');

let pollFn = null;
let nextFetch = null;
const ctx = {
  console, Date, isNaN,
  apiUrl: (p) => p,
  fetchAPI: async () => { if (nextFetch === 'fail') throw new Error('down'); return nextFetch; },
  setText: (id, text) => { doc.getElementById(id).textContent = text; },
  visibilityAwarePoll: (fn) => { pollFn = fn; },
};
ctx.document = doc;
ctx.window = ctx;
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(widgetPath, 'utf8'), ctx);

(async () => {
  const results = [];
  for (const step of scenario.steps) {
    if (step.poll !== undefined) {
      nextFetch = step.poll;
      await pollFn();
      results.push({
        tooltip: toggle.getAttribute('data-tooltip'),
        ariaLabel: toggle.getAttribute('aria-label'),
        label: doc.getElementById('auth-widget-label').textContent,
        dot: doc.getElementById('auth-widget-dot').className,
      });
    } else if (step.open) {
      toggle.click();
      results.push({ hidden: panel.classList.contains('hidden'), expanded: toggle.getAttribute('aria-expanded') });
    } else if (step.escape) {
      doc.activeElement = step.focus === 'inside' ? toggle : outside;
      toggle.focusCalls = 0;
      (docListeners.keydown || []).forEach((fn) => fn({ key: 'Escape' }));
      results.push({
        hidden: panel.classList.contains('hidden'),
        expanded: toggle.getAttribute('aria-expanded'),
        toggleFocusCalls: toggle.focusCalls,
      });
    }
  }
  process.stdout.write(JSON.stringify(results));
})().catch((e) => { process.stderr.write(String(e && e.stack || e)); process.exit(1); });
"""


def _run_widget(tmp_path: Path, steps: list[dict]) -> list[dict]:
    if shutil.which("node") is None:
        if os.environ.get("CI"):
            pytest.fail("node is required in CI to run provider-status-widget.js")
        pytest.skip("node is required to run provider-status-widget.js")
    harness = tmp_path / "widget_harness.js"
    harness.write_text(_WIDGET_HARNESS, encoding="utf-8")
    proc = subprocess.run(
        ["node", str(harness), str(_WIDGET_JS_PATH), json.dumps({"steps": steps})],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


class TestCollapsedStateIsSpelledOut:
    """A collapsed sidebar shows only the dot, so the tooltip and accessible name carry the state in words."""

    def test_authenticated_provider(self, tmp_path: Path) -> None:
        (result,) = _run_widget(tmp_path, [{"poll": {"provider": "claude-code", "authenticated": True}}])
        assert result["tooltip"] == "claude-code: authenticated"
        assert result["ariaLabel"] == "claude-code: authenticated"
        assert "resource-dot-green" in result["dot"]

    def test_unauthenticated_provider(self, tmp_path: Path) -> None:
        (result,) = _run_widget(tmp_path, [{"poll": {"provider": "claude-code", "authenticated": False}}])
        assert result["tooltip"] == "claude-code: not authenticated"
        assert result["ariaLabel"] == "claude-code: not authenticated"
        assert "resource-dot-red" in result["dot"]

    def test_routing_variables_are_named_alongside_the_yellow_dot(self, tmp_path: Path) -> None:
        payload = {"provider": "claude-code", "authenticated": True, "routing_warnings": ["ANTHROPIC_BASE_URL"]}
        (result,) = _run_widget(tmp_path, [{"poll": payload}])
        assert result["tooltip"] == "claude-code: authenticated, routing variables set"
        assert "resource-dot-yellow" in result["dot"]

    def test_failed_poll_reports_the_status_as_unavailable(self, tmp_path: Path) -> None:
        (result,) = _run_widget(tmp_path, [{"poll": "fail"}])
        assert result["tooltip"] == "Provider status unavailable"
        assert result["ariaLabel"] == "Provider status unavailable"
        assert result["label"] == "unavailable"

    def test_each_poll_replaces_the_previous_summary(self, tmp_path: Path) -> None:
        steps = [
            {"poll": {"provider": "claude-code", "authenticated": True}},
            {"poll": "fail"},
            {"poll": {"provider": "anthropic", "authenticated": False}},
        ]
        tooltips = [r["tooltip"] for r in _run_widget(tmp_path, steps)]
        assert tooltips == [
            "claude-code: authenticated",
            "Provider status unavailable",
            "anthropic: not authenticated",
        ]


class TestEscapeDismissal:
    def test_escape_closes_the_popover_and_returns_focus_when_focus_is_in_the_widget(self, tmp_path: Path) -> None:
        opened, closed = _run_widget(tmp_path, [{"open": True}, {"escape": True, "focus": "inside"}])
        assert opened == {"hidden": False, "expanded": "true"}
        assert closed == {"hidden": True, "expanded": "false", "toggleFocusCalls": 1}

    def test_escape_does_not_pull_focus_back_after_the_user_moved_elsewhere(self, tmp_path: Path) -> None:
        """Focus on a modal's opener (or anything outside the widget) must stay there."""
        _, closed = _run_widget(tmp_path, [{"open": True}, {"escape": True, "focus": "outside"}])
        assert closed["hidden"] is True
        assert closed["toggleFocusCalls"] == 0

    def test_escape_while_closed_does_nothing(self, tmp_path: Path) -> None:
        (result,) = _run_widget(tmp_path, [{"escape": True, "focus": "inside"}])
        assert result["hidden"] is True
        assert result["toggleFocusCalls"] == 0
