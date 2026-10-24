"""Tests for priority icon static files and rendering."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from sova.adapters.priority import Priority
from sova.dashboard.app import create_app, priorities_json, priority_icon
from sova.db.session import close_db, init_db

# Derived from the registry, so a new Priority level fails these tests until its icon ships.
_ICON_NAMES = [p.icon for p in Priority]


@pytest.fixture(autouse=True)
async def setup_db(monkeypatch):
    """Initialize an in-memory DB for icon tests."""
    monkeypatch.setenv("SOVA_DATABASE_URL", "sqlite+aiosqlite://")
    await init_db(run_migrations=False)
    yield
    await close_db()


@pytest.fixture
async def client() -> AsyncClient:
    """Create test client."""
    app = create_app(project_dir=None)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


class TestPriorityIcons:
    """Test priority icon static files are served correctly."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("icon", _ICON_NAMES)
    async def test_icon_is_served(self, client: AsyncClient, icon: str) -> None:
        resp = await client.get(f"/static/priority/{icon}.svg")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "image/svg+xml"
        assert b"<svg" in resp.content

    @pytest.mark.asyncio
    async def test_blocker_icon_has_red_color(self, client: AsyncClient) -> None:
        resp = await client.get("/static/priority/blocker.svg")
        assert b"ff5630" in resp.content

    @pytest.mark.asyncio
    async def test_undefined_icon_has_gray_color(self, client: AsyncClient) -> None:
        resp = await client.get("/static/priority/undefined.svg")
        assert b"7a8699" in resp.content


class TestPriorityIconFiles:
    """Test priority icon files exist and have correct structure."""

    def test_all_icon_files_exist(self) -> None:
        icon_dir = Path(__file__).parent.parent / "sova" / "dashboard" / "static" / "priority"
        assert icon_dir.exists()

        for icon in _ICON_NAMES:
            icon_path = icon_dir / f"{icon}.svg"
            assert icon_path.exists(), f"Missing priority icon: {icon}.svg"
            assert icon_path.stat().st_size > 0, f"Empty priority icon: {icon}.svg"

    @pytest.mark.parametrize("icon", ["blocker", "undefined"])
    def test_icon_structure(self, icon: str) -> None:
        icon_path = Path(__file__).parent.parent / "sova" / "dashboard" / "static" / "priority" / f"{icon}.svg"
        content = icon_path.read_text()

        assert "<svg" in content
        assert 'xmlns="http://www.w3.org/2000/svg"' in content
        assert 'viewBox="0 0 16 16"' in content


class TestPriorityIconGlobal:
    """Test the priority_icon Jinja global (registered in app.py) renders correctly."""

    @pytest.mark.parametrize(
        "priority,expected_icon,expected_alt",
        [
            (None, "undefined", "Undefined"),
            ("", "undefined", "Undefined"),
            ("blocker", "blocker", "Blocker"),
            ("critical", "critical", "Critical"),
            ("high", "high", "High"),
            ("medium", "medium", "Medium"),
            ("low", "low", "Low"),
            ("unknown_value", "undefined", "Undefined"),
        ],
    )
    def test_renders_correct_icon(self, priority: str | None, expected_icon: str, expected_alt: str) -> None:
        html = priority_icon(priority)
        assert f"/static/priority/{expected_icon}.svg" in html
        assert f'alt="{expected_alt} priority"' in html

    def test_case_insensitive(self) -> None:
        html = priority_icon("CRITICAL")
        assert "/static/priority/critical.svg" in html

    def test_custom_size(self) -> None:
        html = priority_icon("high", "12")
        assert 'width="12" height="12"' in html

    def test_not_a_macro_import(self) -> None:
        """priority_icon is a registered Jinja global, not a _components.html macro."""
        components_path = Path(__file__).parent.parent / "sova" / "dashboard" / "templates" / "_components.html"
        content = components_path.read_text()
        assert "macro priority_icon" not in content

    @pytest.mark.parametrize("template", ["supervisor.html", "agents.html", "queue.html"])
    def test_template_uses_shared_priorityIconUrl(self, template: str) -> None:
        """Verify templates call priorityIconUrl (defined in app.js) without redefining it."""
        path = Path(__file__).parent.parent / "sova" / "dashboard" / "templates" / template
        content = path.read_text()
        assert "priorityIconUrl(" in content

    @pytest.mark.parametrize("template", ["supervisor.html", "agents.html", "queue.html"])
    def test_template_uses_shared_priorityDisplayName_for_alt_text(self, template: str) -> None:
        """Every client-rendered priority icon's alt text must come from the
        shared priorityDisplayName() helper, so it never disagrees with the
        server-rendered priority_icon() global on the same enum value.
        """
        path = Path(__file__).parent.parent / "sova" / "dashboard" / "templates" / template
        content = path.read_text()
        assert "priorityDisplayName(" in content
        assert "function priorityIconUrl" not in content

    def test_shared_helper_in_app_js(self) -> None:
        """priorityIconUrl is defined in app.js and sourced from window.SOVA_PRIORITIES."""
        app_js = Path(__file__).parent.parent / "sova" / "dashboard" / "static" / "app.js"
        content = app_js.read_text()
        assert "function priorityIconUrl(" in content
        assert "SOVA_PRIORITIES" in content


class TestPrioritiesJson:
    """Test the priorities_json Jinja global that feeds window.SOVA_PRIORITIES."""

    def test_includes_all_six_levels(self) -> None:
        payload = json.loads(priorities_json())
        values = {entry["value"] for entry in payload}
        assert values == {p.value for p in Priority}

    def test_entries_have_expected_shape(self) -> None:
        payload = json.loads(priorities_json())
        for entry in payload:
            assert set(entry) == {"value", "rank", "display_name", "icon"}

    def test_declaration_order_is_rank_order(self) -> None:
        payload = json.loads(priorities_json())
        ranks = [entry["rank"] for entry in payload]
        assert ranks == sorted(ranks)

    def test_base_html_renders_window_sova_priorities(self) -> None:
        base_path = Path(__file__).parent.parent / "sova" / "dashboard" / "templates" / "base.html"
        content = base_path.read_text()
        assert "window.SOVA_PRIORITIES = " in content

    @pytest.mark.asyncio
    async def test_rendered_page_emits_unescaped_json(self, client: AsyncClient) -> None:
        """Jinja autoescape is on for HTML: if priorities_json() ever returned a
        plain str instead of Markup, the page would emit HTML-escaped JSON
        (&#34; instead of ") and silently break window.SOVA_PRIORITIES for every
        page. Render a real page and check the raw response body.
        """
        resp = await client.get("/agents")
        assert resp.status_code == 200
        assert 'window.SOVA_PRIORITIES = [{"value"' in resp.text


class TestUndefinedPriorityRendering:
    """Verify undefined.svg is rendered when priority is missing in JS templates."""

    def test_supervisor_graph_renders_icon_without_priority(self) -> None:
        path = Path(__file__).parent.parent / "sova" / "dashboard" / "templates" / "supervisor.html"
        content = path.read_text()
        idx = content.find("Priority icon at bottom-left")
        assert idx != -1, "Priority icon comment not found in supervisor.html"
        block = content[idx : idx + 400]
        assert "if (n.priority)" not in block, "Supervisor graph still guards icon rendering behind if(n.priority)"

    def test_queue_always_renders_priority_icon(self) -> None:
        path = Path(__file__).parent.parent / "sova" / "dashboard" / "templates" / "queue.html"
        content = path.read_text()
        assert "priority ? '<img" not in content, "Queue template conditionally renders priority icon"

    def test_agents_planner_renders_icon_without_priority(self) -> None:
        path = Path(__file__).parent.parent / "sova" / "dashboard" / "templates" / "agents.html"
        content = path.read_text()
        assert "priorityIconUrl(t.priority)" in content, "Agents template must call priorityIconUrl for planner tasks"
        assert "t.priority ? '<img" not in content, "Agents planner conditionally renders priority icon"
