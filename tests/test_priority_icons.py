"""Tests for priority icon static files and rendering."""

from __future__ import annotations

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from sova.dashboard.app import create_app
from sova.db.session import close_db, init_db

_ICON_NAMES = ["blocker", "critical", "high", "medium", "low", "undefined"]


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


class TestPriorityIconMacro:
    """Test the priority_icon Jinja2 macro renders correctly in components."""

    def test_macro_exists_in_components(self) -> None:
        components_path = Path(__file__).parent.parent / "sova" / "dashboard" / "templates" / "_components.html"
        content = components_path.read_text()

        assert "macro priority_icon" in content
        assert "/static/priority/" in content
        assert "critical" in content

    @pytest.mark.parametrize("template", ["supervisor.html", "agents.html", "queue.html"])
    def test_template_uses_shared_priorityIconUrl(self, template: str) -> None:
        """Verify templates call priorityIconUrl (defined in app.js) without redefining it."""
        path = Path(__file__).parent.parent / "sova" / "dashboard" / "templates" / template
        content = path.read_text()
        assert "priorityIconUrl(" in content
        assert "function priorityIconUrl" not in content

    def test_shared_helpers_in_app_js(self) -> None:
        """Verify priorityIconUrl and _extractPriority are defined in app.js."""
        app_js = Path(__file__).parent.parent / "sova" / "dashboard" / "static" / "app.js"
        content = app_js.read_text()
        assert "function priorityIconUrl(" in content
        assert "function _extractPriority(" in content


class TestPriorityIconMacroRendering:
    """Test priority_icon Jinja2 macro renders correct HTML for all inputs."""

    @pytest.fixture
    def jinja_env(self):
        from jinja2 import Environment, FileSystemLoader

        templates_dir = Path(__file__).parent.parent / "sova" / "dashboard" / "templates"
        return Environment(loader=FileSystemLoader(str(templates_dir)))

    @pytest.mark.parametrize(
        "priority,expected_icon,expected_alt",
        [
            (None, "undefined.svg", "Undefined"),
            ("", "undefined.svg", "Undefined"),
            ("critical", "critical.svg", "Critical"),
            ("high", "high.svg", "High"),
            ("medium", "medium.svg", "Medium"),
            ("low", "low.svg", "Low"),
            ("unknown_value", "undefined.svg", "Unknown_value"),
        ],
    )
    def test_macro_renders_correct_icon(self, jinja_env, priority, expected_icon, expected_alt) -> None:
        template = jinja_env.from_string('{%- from "_components.html" import priority_icon -%}{{ priority_icon(p) }}')
        html = template.render(p=priority)
        assert expected_icon in html
        assert f'alt="{expected_alt} priority"' in html


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

    def test_mapping_consistency(self) -> None:
        import re

        app_js = Path(__file__).parent.parent / "sova" / "dashboard" / "static" / "app.js"
        js_content = app_js.read_text()

        components = Path(__file__).parent.parent / "sova" / "dashboard" / "templates" / "_components.html"
        jinja_content = components.read_text()

        js_start = js_content.find("_PRIORITY_ICON_MAP")
        js_map = {}
        for m in re.finditer(r"(\w+):\s*'(\w+)'", js_content[js_start : js_start + 200]):
            js_map[m.group(1)] = m.group(2)

        jinja_start = jinja_content.find("priority_map")
        jinja_map = {}
        for m in re.finditer(r"'(\w+)':\s*'(\w+)'", jinja_content[jinja_start : jinja_start + 200]):
            jinja_map[m.group(1)] = m.group(2)

        assert js_map == jinja_map, f"JS map {js_map} != Jinja2 map {jinja_map}"
