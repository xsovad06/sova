"""Dashboard app: health, CSRF, rate limiting, multi-project, websocket status.

Split from tests/test_dashboard.py; see docs/testing-guidelines.md, "Test File Layout".
"""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from sova.dashboard.app import create_app
from sova.dashboard.security import CSRF_COOKIE_NAME, CSRF_HEADER_NAME, require_same_origin_csrf
from tests.conftest import agents_page_source
from tests.conftest import csrf_request_headers as _csrf_request_headers

# Uses the shared per-worker in-memory DB, emptied before each test
# (see ``tests/conftest.py:reset_db_rows``). A test here that leaves a
# background task touching the DB when its event loop closes (the real
# ``start_agent()`` calls in ``TestDuplicateAgentPrevention``) can invalidate the
# shared connection; the next test's sweep recreates the schema.
pytestmark = pytest.mark.usefixtures("reset_db_rows")


# ---------------------------------------------------------------------------
# Health / smoke tests
# ---------------------------------------------------------------------------


class TestDashboardHealth:
    async def test_healthz_returns_ok(self, client: AsyncClient) -> None:
        resp = await client.get("/healthz")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["checks"]["db"] == "ok"
        assert body["checks"]["disk"] == "ok"

    async def test_connectivity_endpoint_reports_healthy(self, client: AsyncClient) -> None:
        resp = await client.get("/api/health/connectivity")
        assert resp.status_code == 200
        assert resp.json()["is_down"] is False

    async def test_connectivity_endpoint_reports_outage(self, client: AsyncClient) -> None:
        from unittest.mock import patch

        from sova.supervisor.network_health import ConnectivityStatus

        status = ConnectivityStatus(
            is_down=True,
            down_for_seconds=42.6,
            healthy_for_seconds=0.0,
            consecutive_failures=2,
            last_source="probe",
        )
        with patch("sova.supervisor.network_health.get_connectivity_status", return_value=status):
            resp = await client.get("/api/health/connectivity")
        # 200 even while down: the banner's own fetch failing is a different
        # condition (the dashboard being unreachable) and must not be conflated
        # with the network being down.
        assert resp.status_code == 200
        body = resp.json()
        assert body["is_down"] is True
        assert body["down_seconds"] == 43
        assert body["last_source"] == "probe"

    async def test_healthz_db_failure_returns_503(self, client: AsyncClient) -> None:
        from unittest.mock import AsyncMock, patch

        with patch("sova.dashboard.app._check_db_health", new=AsyncMock(return_value="fail")):
            resp = await client.get("/healthz")
        assert resp.status_code == 503
        body = resp.json()
        assert body["status"] == "unhealthy"
        assert body["checks"]["db"] == "fail"

    async def test_healthz_disk_failure_returns_503(self, client: AsyncClient) -> None:
        from unittest.mock import patch

        with patch("sova.dashboard.app._check_disk_health", return_value="fail"):
            resp = await client.get("/healthz")
        assert resp.status_code == 503
        assert resp.json()["checks"]["disk"] == "fail"

    async def test_healthz_skips_scheduler_when_no_pid_file(self, client: AsyncClient) -> None:
        resp = await client.get("/healthz")
        assert resp.status_code == 200
        assert "scheduler" not in resp.json()["checks"]

    async def test_healthz_includes_scheduler_when_pid_file_present(self, tmp_path: Path) -> None:
        from sova.dashboard.app import _check_scheduler_health

        pid_file = tmp_path / ".claude" / "sova-server.pid"
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        pid_file.write_text(str(os.getpid()))
        assert _check_scheduler_health(tmp_path) == "ok"

    @pytest.mark.parametrize(
        "pid_content",
        [
            pytest.param("999999", id="dead-pid"),
            pytest.param("not-a-pid", id="invalid-pid-content"),
            # int("0") parses fine, but os.kill(0, 0) probes the caller's own
            # process group, not a real scheduler process: reject it explicitly.
            pytest.param("0", id="non-positive-pid"),
        ],
    )
    async def test_check_scheduler_health_bad_pid_reports_fail(self, tmp_path: Path, pid_content: str) -> None:
        from sova.dashboard.app import _check_scheduler_health

        pid_file = tmp_path / ".claude" / "sova-server.pid"
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        pid_file.write_text(pid_content)
        assert _check_scheduler_health(tmp_path) == "fail"

    async def test_healthz_scheduler_check_is_idempotent(self, tmp_path: Path) -> None:
        # A dead scheduler must keep reporting "fail" across repeated probes and
        # must not delete the PID file (which would make the check flip to skip).
        from sova.dashboard.app import _check_scheduler_health

        pid_file = tmp_path / ".claude" / "sova-server.pid"
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        pid_file.write_text("999999")
        assert _check_scheduler_health(tmp_path) == "fail"
        assert pid_file.exists()
        assert _check_scheduler_health(tmp_path) == "fail"
        assert pid_file.exists()

    async def test_check_db_health_returns_fail_on_error(self) -> None:
        from unittest.mock import patch

        from sova.dashboard.app import _check_db_health

        with patch("sova.dashboard.app.get_session", side_effect=RuntimeError("boom")):
            assert await _check_db_health() == "fail"

    async def test_check_disk_health_returns_fail_when_low(self, tmp_path: Path) -> None:
        from sova.dashboard.app import _check_disk_health

        # An unreachably high floor forces the low-space branch.
        assert _check_disk_health(tmp_path, min_free_mb=10**12) == "fail"

    async def test_check_disk_health_returns_fail_on_oserror(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        from sova.dashboard.app import _check_disk_health

        with patch("shutil.disk_usage", side_effect=OSError("no such device")):
            assert _check_disk_health(tmp_path) == "fail"

    async def test_check_scheduler_health_permission_error_reports_ok(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        from sova.dashboard.app import _check_scheduler_health

        pid_file = tmp_path / ".claude" / "sova-server.pid"
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        pid_file.write_text("12345")
        # A PermissionError from os.kill means the process exists (owned by another user).
        with patch("os.kill", side_effect=PermissionError):
            assert _check_scheduler_health(tmp_path) == "ok"

    async def test_healthz_includes_scheduler_status_in_endpoint(self, client: AsyncClient) -> None:
        from unittest.mock import patch

        with patch("sova.dashboard.app._check_scheduler_health", return_value="ok"):
            resp = await client.get("/healthz")
        assert resp.status_code == 200
        assert resp.json()["checks"]["scheduler"] == "ok"

    async def test_phone_page_is_standalone_and_unscoped(self, client: AsyncClient) -> None:
        """/phone has no sidebar and leaves the API prefix to its unscoped default (the single project)."""
        resp = await client.get("/phone")
        assert resp.status_code == 200
        assert 'window.SOVA_PROJECT_SLUG = "";' in resp.text
        assert 'id="phone-waiting"' in resp.text
        assert 'id="phone-running"' in resp.text
        assert 'id="phone-start-form"' in resp.text
        assert 'class="sidebar' not in resp.text
        assert 'href="/work"' in resp.text
        assert "window.SOVA_RUN_ACTIONS = {" in resp.text
        assert re.search(r"<h1[^>]*>\s*SOVA\s*</h1>", resp.text)
        assert "sova_csrf=" in resp.headers["set-cookie"]

    @pytest.mark.parametrize(
        ("path", "status"),
        [
            pytest.param("/", 307, id="root"),
            pytest.param("/dashboard", 301, id="dashboard"),
            pytest.param("/agents", 301, id="agents"),
            pytest.param("/overview", 301, id="overview"),
            pytest.param("/control", 301, id="control"),
            pytest.param("/runs", 301, id="runs"),
            pytest.param("/tasks", 301, id="tasks"),
            pytest.param("/logs", 301, id="logs"),
            pytest.param("/memory", 301, id="memory"),
        ],
    )
    async def test_redirects_to_work(self, client: AsyncClient, path: str, status: int) -> None:
        resp = await client.get(path, follow_redirects=False)
        assert resp.status_code == status
        assert resp.headers["location"] == "/work"

    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            pytest.param("/work", "Work", id="work"),
            pytest.param("/work/1", "Run #1", id="work-detail-with-agents-sidebar"),
            pytest.param("/costs", "Cost", id="costs"),
        ],
    )
    async def test_page_loads(self, client: AsyncClient, path: str, expected: str) -> None:
        resp = await client.get(path)
        assert resp.status_code == 200
        assert expected in resp.text

    @pytest.mark.parametrize(
        ("path", "more_open", "highlighted"),
        [
            pytest.param("/work", False, "/work", id="visible-page-leaves-more-collapsed"),
            pytest.param("/supervisor", False, "/supervisor", id="supervisor-stays-visible"),
            pytest.param("/specs", True, "/specs", id="child-page-opens-more"),
            pytest.param("/connections", True, "/connections", id="connections-lives-in-more"),
            pytest.param("/queue", True, "/queue", id="queue-lives-in-more"),
            pytest.param("/settings", False, "/settings", id="settings-stays-visible"),
        ],
    )
    async def test_sidebar_more_group(self, client: AsyncClient, path: str, more_open: bool, highlighted: str) -> None:
        html = (await client.get(path)).text
        nav = html[html.index('class="sidebar-nav-scroll') :]
        visible, toggle = nav.split('id="sidebar-more-toggle"', 1)
        assert ('aria-expanded="true"' in toggle.split(">", 1)[0]) is more_open
        group = toggle[toggle.index('id="sidebar-more-group"') :]
        group_tag, group_body = group.split(">", 1)
        assert ("hidden" in group_tag) is (not more_open)
        more, after = group_body.split('<div class="sidebar-divider', 1)
        visible_pages = ["/work", "/supervisor", "/oversight", "/costs"]
        assert all(f'href="{href}"' in visible for href in visible_pages)
        more_pages = [
            "/queue",
            "/specs",
            "/pr-metrics",
            "/reliability",
            "/roles",
            "/commands",
            "/dependency-health",
            "/fleet",
        ]
        assert all(f'href="{href}"' in more for href in [*more_pages, "/connections", "/setup"])
        assert not any(f'href="{href}"' in visible for href in more_pages)
        assert 'href="/settings"' in after.split("</nav>", 1)[0]
        active = [seg for seg in html.split("<a ")[1:] if "bg-sidebar-active" in seg.split(">", 1)[0]]
        assert [seg.split('"')[1] for seg in active] == [highlighted]

    async def test_sidebar_has_retired_pages_removed(self, client: AsyncClient) -> None:
        html = (await client.get("/work")).text
        assert 'href="/logs"' not in html
        assert 'href="/memory"' not in html
        assert (await client.get("/api/logs")).status_code == 404

    async def test_lifecycle_renders_with_agents_sidebar(self, client: AsyncClient) -> None:
        resp = await client.get("/lifecycle/42")
        assert resp.status_code == 200
        assert "Issue " in resp.text and "#42" in resp.text

    async def test_costs_page_has_per_section_error_handling(self, client: AsyncClient) -> None:
        resp = await client.get("/costs")
        assert resp.status_code == 200
        body = resp.text
        assert "function loadSummary()" in body
        assert "function loadEnergy()" in body
        assert "function loadDailyChart()" in body
        assert "function loadCostTable(" in body
        # renderSectionError() is the shared helper from app.js, so the page must call it from the
        # sections that fail, not merely mention it. Its single definition and its load order before this
        # inline script are guarded by TestSharedHelpersHaveOneDefinition in test_dashboard_agents_modules.py.
        assert "function renderSectionError(" not in body
        daily_chart = body[body.index("async function loadDailyChart()") : body.index("async function loadCostTable(")]
        assert "renderSectionError('daily-chart'," in daily_chart
        cost_table = body[body.index("async function loadCostTable(") :]
        assert "renderSectionError(containerId," in cost_table.split("\n}\n")[0]
        assert "function setElementError(" in body
        assert "} catch (err)" in body
        # Verify energy error handler clears detail elements
        assert "cost-energy-detail" in body
        assert "cost-co2-detail" in body

    @pytest.mark.parametrize(
        "asset",
        [
            pytest.param("/static/style.css", id="css"),
            pytest.param("/static/app.js", id="js"),
        ],
    )
    async def test_static_asset_returns_200(self, client: AsyncClient, asset: str) -> None:
        resp = await client.get(asset)
        assert resp.status_code == 200

    @pytest.mark.parametrize(
        "asset",
        ["/static/style.css", "/static/app.js", "/static/js/pages/work/board.js"],
    )
    async def test_static_assets_revalidate_on_every_use(self, client: AsyncClient, asset: str) -> None:
        """A cached asset must never outlive a deploy: `max-age` let a fresh page pair with pre-deploy JS.

        The page GET issues the CSRF cookie, but the `fetch()` patch that echoes it lives in app.js.
        With `max-age=3600` a browser reused the old app.js (no patch) for up to an hour after a
        restart, so every mutating call failed with "Missing or invalid CSRF token".
        """
        resp = await client.get(asset)
        assert resp.status_code == 200
        assert resp.headers.get("cache-control") == "no-cache"
        assert "max-age" not in resp.headers["cache-control"]

    async def test_static_asset_revalidation_is_a_cheap_304(self, client: AsyncClient) -> None:
        """`no-cache` costs one conditional request per asset, answered without a body when unchanged."""
        first = await client.get("/static/app.js")
        etag = first.headers["etag"]
        resp = await client.get("/static/app.js", headers={"if-none-match": etag})
        assert resp.status_code == 304
        assert resp.content == b""
        assert resp.headers.get("cache-control") == "no-cache"

    async def test_static_cache_header_absent_on_404(self, client: AsyncClient) -> None:
        resp = await client.get("/static/nonexistent-file.xyz")
        assert resp.status_code == 404
        assert "max-age" not in resp.headers.get("cache-control", "")

    async def test_status_badge_has_step_label_mapping(self, client: AsyncClient) -> None:
        resp = await client.get("/static/app.js")
        js = resp.text
        assert "_STEP_LABELS" in js
        assert "_WAITING_STEPS" in js
        assert "_computeBadgeLabel" in js
        assert "_getBadgeColors" in js
        for step in ["monitor_ci", "wait_for_external_reviews", "handoff_to_reviewer", "handoff_to_user"]:
            assert step in js, f"waiting step {step} missing from _WAITING_STEPS"

    async def test_status_badge_five_state_colors(self, client: AsyncClient) -> None:
        resp = await client.get("/static/app.js")
        js = resp.text
        assert "done:" in js
        # Done uses blue (accent) not green
        done_idx = js.index("done:")
        done_section = js[done_idx : done_idx + 120]
        assert "bg-accent'" in done_section or "bg-accent," in done_section or "'bg-accent'" in done_section
        # Running states use green
        running_idx = js.index("running:")
        running_section = js[running_idx : running_idx + 120]
        assert "bg-accent-green" in running_section
        # CI monitoring uses peach (waiting) via _WAITING_COLOR constant
        assert "_WAITING_COLOR" in js
        ci_idx = js.index("ci_monitoring:")
        ci_section = js[ci_idx : ci_idx + 120]
        assert "_WAITING_COLOR" in ci_section

    async def test_awaiting_approval_is_terminal_with_color(self, client: AsyncClient) -> None:
        resp = await client.get("/static/app.js")
        js = resp.text
        # awaiting_approval must be in _STATUS_TERMINAL
        terminal_idx = js.index("_STATUS_TERMINAL")
        terminal_section = js[terminal_idx : terminal_idx + 200]
        assert "awaiting_approval" in terminal_section
        # awaiting_approval must have a color mapping in STATUS_COLORS
        colors_idx = js.index("STATUS_COLORS")
        colors_section = js[colors_idx : js.index("};", colors_idx)]
        assert "awaiting_approval" in colors_section

    async def test_badge_label_terminal_states_capitalized(self, client: AsyncClient) -> None:
        """Verify _computeBadgeLabel logic: terminal states produce capitalized labels."""
        resp = await client.get("/static/app.js")
        js = resp.text
        # Extract terminal statuses from _STATUS_TERMINAL
        import re

        terminal_match = re.search(r"_STATUS_TERMINAL\s*=\s*\{([^}]+)\}", js)
        assert terminal_match, "_STATUS_TERMINAL not found"
        terminal_keys = re.findall(r"(\w+)\s*:", terminal_match.group(1))
        assert len(terminal_keys) >= 5, f"Expected at least 5 terminal states, got {terminal_keys}"
        for status in ["done", "failed", "interrupted", "rejected", "paused"]:
            assert status in terminal_keys, f"{status} missing from _STATUS_TERMINAL"

    async def test_badge_label_waiting_steps_use_waiting_prefix(self, client: AsyncClient) -> None:
        """Verify waiting steps are in _WAITING_STEPS and use peach color."""
        resp = await client.get("/static/app.js")
        js = resp.text
        import re

        waiting_match = re.search(r"_WAITING_STEPS\s*=\s*\{([^}]+)\}", js)
        assert waiting_match, "_WAITING_STEPS not found"
        waiting_keys = re.findall(r"(\w+)\s*:", waiting_match.group(1))
        expected_waiting = ["monitor_ci", "wait_for_external_reviews", "handoff_to_reviewer", "handoff_to_user"]
        for step in expected_waiting:
            assert step in waiting_keys, f"{step} missing from _WAITING_STEPS"
        # Verify _computeBadgeLabel uses 'Waiting: ' prefix for waiting steps
        assert "'Waiting: '" in js or '"Waiting: "' in js

    async def test_badge_label_running_steps_use_running_prefix(self, client: AsyncClient) -> None:
        """Verify non-waiting, non-terminal steps produce 'Running: {label}' badges."""
        resp = await client.get("/static/app.js")
        js = resp.text
        assert "'Running: '" in js or '"Running: "' in js
        # Verify non-waiting steps like 'develop' have explicit labels
        import re

        labels_match = re.search(r"_STEP_LABELS\s*=\s*\{([^}]+)\}", js)
        assert labels_match
        # develop should map to 'Developing', not be in _WAITING_STEPS
        assert "develop:" in labels_match.group(1)
        assert "'Developing'" in labels_match.group(1) or '"Developing"' in labels_match.group(1)

    async def test_badge_fallback_label_capitalizes_first_letter(self, client: AsyncClient) -> None:
        """Verify unmapped steps get capitalized fallback labels."""
        resp = await client.get("/static/app.js")
        js = resp.text
        # The fallback path should capitalize: rawLabel.charAt(0).toUpperCase()
        assert "charAt(0).toUpperCase()" in js

    async def test_badge_non_terminal_states_use_green_not_blue(self, client: AsyncClient) -> None:
        """Verify transitional states (researched, pr_created) use green, not blue."""
        resp = await client.get("/static/app.js")
        js = resp.text
        import re

        colors_match = re.search(r"STATUS_COLORS\s*=\s*\{(.+?)\n\};", js, re.DOTALL)
        assert colors_match
        colors_block = colors_match.group(1)
        # researched and pr_created should use accent-green (running), not accent (blue/completed)
        for status in ["researched", "pr_created"]:
            idx = colors_block.index(f"{status}:")
            section = colors_block[idx : idx + 120]
            assert "bg-accent-green" in section, f"{status} should use green (running) color, not blue"

    async def test_ws_notifications_cover_all_terminal_states(self, client: AsyncClient) -> None:
        """Verify the agent_finished notification handler fires for all terminal states except
        paused (#1051: get_all_agent_statuses() only ever selects non-terminal TaskRuns, so a
        terminal-state transition can only be observed from the agent_finished payload, not from
        a status_update broadcast).
        """
        resp = await client.get("/work")
        assert resp.status_code == 200
        html = agents_page_source()
        # Should use _STATUS_TERMINAL lookup instead of hardcoded done/failed check
        assert "_STATUS_TERMINAL[msg.status]" in html, "Notification check should use _STATUS_TERMINAL lookup"
        # Should exclude paused (user-initiated, not worth notifying)
        assert "!== 'paused'" in html or '!== "paused"' in html

    async def test_agents_page_has_ws_notification_tracking(self, client: AsyncClient) -> None:
        resp = await client.get("/work")
        assert resp.status_code == 200
        html = agents_page_source()
        assert "msg.status" in html
        assert "sendBrowserNotification" in html

    async def test_supervisor_page_loads(self, client: AsyncClient, tmp_path: Path) -> None:
        from dataclasses import dataclass
        from unittest.mock import patch

        @dataclass
        class FakeCfg:
            github_repo: str = "owner/repo"

        with (
            patch("sova.config.context.get_project_dir", return_value=str(tmp_path / "proj")),
            patch("sova.dashboard.app.load_config", return_value=FakeCfg()),
            patch("sova.dashboard.services.setup_service.load_config", return_value=FakeCfg()),
            # The nav reads awareness_service's binding; awareness off keeps the page's briefing link out of it.
            patch(
                "sova.dashboard.services.awareness_service.load_config",
                return_value=MagicMock(awareness=MagicMock(enabled=False)),
            ),
        ):
            resp = await client.get("/supervisor")
        assert resp.status_code == 200

    async def test_supervisor_page_falls_back_on_config_error(self, client: AsyncClient) -> None:
        from unittest.mock import patch

        with (
            patch("sova.dashboard.app.load_config", side_effect=RuntimeError("no config")),
            patch("sova.dashboard.services.setup_service.load_config", side_effect=RuntimeError("no config")),
            patch("sova.dashboard.services.awareness_service.load_config", side_effect=RuntimeError("no config")),
        ):
            resp = await client.get("/supervisor")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Navigation definition (sova/dashboard/navigation.py)
# ---------------------------------------------------------------------------


def _sidebar_hrefs(html: str) -> list[str]:
    """The page links of the desktop sidebar's nav list, in order."""
    nav = html[html.index('class="sidebar-nav-scroll') :]
    nav = nav[: nav.index("<!-- Collapse toggle -->")]
    return re.findall(r'<a href="([^"]+)"', nav)


class TestNavigationDefinition:
    def test_every_page_is_listed_once(self) -> None:
        from sova.dashboard.navigation import NAV_PAGES

        ids = [p.id for p in NAV_PAGES]
        paths = [p.path for p in NAV_PAGES]
        assert len(ids) == len(set(ids))
        assert len(paths) == len(set(paths))

    def test_prefix_applies_to_project_pages_only(self) -> None:
        from sova.dashboard.navigation import nav_links

        hrefs = {link.id: link.href for link in nav_links("/p/alpha", awareness_enabled=True)}
        assert hrefs["work"] == "/p/alpha/work"
        assert hrefs["phone"] == "/p/alpha/phone"
        assert hrefs["setup"] == "/setup"

    @pytest.mark.parametrize("awareness", [True, False])
    def test_briefing_is_listed_only_with_awareness(self, awareness: bool) -> None:
        from sova.dashboard.navigation import nav_links

        assert ("briefing" in {link.id for link in nav_links("", awareness)}) is awareness

    async def test_the_sidebar_lists_the_definition_in_order(self, client: AsyncClient) -> None:
        from sova.dashboard.navigation import NAV_PAGES

        expected = [
            p.path
            for place in ("main", "more", "bottom")
            for p in NAV_PAGES
            if p.sidebar == place and not p.needs_awareness
        ]
        assert _sidebar_hrefs((await client.get("/work")).text) == expected


class TestHomeScreen:
    async def test_the_manifest_opens_the_home_page_standalone(self, client: AsyncClient) -> None:
        resp = await client.get("/manifest.webmanifest")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/manifest+json")
        manifest = resp.json()
        assert manifest["start_url"] == "/phone"
        assert manifest["display"] == "standalone"
        assert manifest["scope"] == "/"
        assert {icon["sizes"] for icon in manifest["icons"]} == {"192x192", "512x512"}
        for icon in manifest["icons"]:
            assert (await client.get(icon["src"])).status_code == 200

    async def test_a_page_links_the_manifest_and_the_apple_web_app_tags(self, client: AsyncClient) -> None:
        html = (await client.get("/work")).text
        assert '<link rel="manifest" href="/manifest.webmanifest">' in html
        assert "viewport-fit=cover" in html
        assert "user-scalable=no" not in html and "maximum-scale" not in html, "pinch zoom stays on"
        assert '<meta name="theme-color" content="#1e1e2e">' in html
        assert '<meta name="apple-mobile-web-app-capable" content="yes">' in html
        assert '<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">' in html


# ---------------------------------------------------------------------------
# Auto-handoff orchestration
# ---------------------------------------------------------------------------


class TestRemovedMemoryAPI:
    @pytest.mark.parametrize(
        ("method", "path"),
        [("get", "/api/memory"), ("post", "/api/memory/export"), ("post", "/api/memory/import")],
    )
    async def test_memory_routes_are_gone(self, client: AsyncClient, method: str, path: str) -> None:
        resp = await getattr(client, method)(path)

        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Router-level CSRF coverage (#1204): every mutating route is guarded by
# construction, not by a per-route opt-in that a new route can miss.
# ---------------------------------------------------------------------------


class TestRouterLevelCsrfCoverage:
    """Every router _register_api_routers() installs must guard its mutating
    routes with require_same_origin_csrf by construction (#1204): the previous
    per-route opt-in is exactly how most of them went unguarded. mcp.router is
    the sole exemption (its own bearer-token scheme already authenticates every
    call); /api/telemetry/ingest is exempt structurally, since it is registered
    by a separate call and never passes through this function at all.
    """

    _MUTATING = frozenset({"POST", "PUT", "PATCH", "DELETE"})

    @staticmethod
    def _effective_routes(app) -> list:
        """Every endpoint route the app serves, with its include-time dependencies.

        FastAPI 0.156 stopped copying an included router's routes onto
        ``app.routes``: they sit behind one wrapper per ``include_router`` call,
        reachable through ``fastapi.routing.iter_route_contexts()``. Older
        releases flattened them into ``APIRoute`` objects directly.
        """
        from fastapi import routing

        if hasattr(routing, "iter_route_contexts"):
            return list(routing.iter_route_contexts(app.routes))
        return [route for route in app.routes if isinstance(route, routing.APIRoute)]

    @staticmethod
    def _unguarded_mutating_routes(app, *, exempt: frozenset[str] = frozenset()) -> list[str]:
        from sova.dashboard.security import require_same_origin_csrf

        unguarded = []
        mutating = 0
        for route in TestRouterLevelCsrfCoverage._effective_routes(app):
            if not (TestRouterLevelCsrfCoverage._MUTATING & (route.methods or set())):
                continue
            mutating += 1
            if route.path in exempt:
                continue
            guarded = any(dep.call is require_same_origin_csrf for dep in route.dependant.dependencies)
            if not guarded:
                unguarded.append(route.path)
        # An empty walk would pass every "nothing unguarded" assertion: FastAPI
        # 0.156 hid the routes from the old walk and these tests stayed green.
        assert mutating > 0, "found no mutating routes to check; the route walk no longer sees them"
        return unguarded

    def test_single_api_prefix_guards_every_mutating_route(self) -> None:
        from fastapi import FastAPI

        from sova.dashboard.app import _register_api_routers

        app = FastAPI()
        _register_api_routers(app, prefix="/api")
        assert self._unguarded_mutating_routes(app, exempt=frozenset({"/api/mcp"})) == []

    def test_project_scoped_api_prefix_guards_every_mutating_route(self) -> None:
        """Edge case: a router registered under the multi-project /p/{slug}/api
        prefix must get the dependency too, not just the default /api prefix."""
        from fastapi import FastAPI

        from sova.dashboard.app import _register_api_routers

        app = FastAPI()
        _register_api_routers(app, prefix="/p/{slug}/api")
        assert self._unguarded_mutating_routes(app, exempt=frozenset({"/p/{slug}/api/mcp"})) == []

    def test_exemption_set_is_exactly_mcp(self) -> None:
        """/api/telemetry/ingest never reaches this function's routes at all
        (registered separately), so the one real exemption here is /mcp."""
        from fastapi import FastAPI

        from sova.dashboard.app import _register_api_routers

        app = FastAPI()
        _register_api_routers(app, prefix="/api")
        assert self._unguarded_mutating_routes(app) == ["/api/mcp"]

    def test_full_app_guards_fleet_manager_and_project_admin_routes(self) -> None:
        """fleet_manager.router and the ad-hoc /api/projects/* routes are not
        registered via _register_api_routers, so they need their own check."""
        app = create_app(multi_project=True)
        unguarded = [
            path
            for path in self._unguarded_mutating_routes(app)
            if path.startswith("/api/projects/") or path.startswith("/api/fleet")
        ]
        assert unguarded == []


# ---------------------------------------------------------------------------
# Multi-project mode
# ---------------------------------------------------------------------------


class TestMultiProject:
    @pytest.fixture
    async def multi_client(self, tmp_path):
        """Create a multi-project dashboard with two registered projects."""
        from sova.config import registry

        # Use isolated registry
        reg_file = tmp_path / "registry" / "projects.json"
        import sova.config.registry as reg_mod

        orig_file = reg_mod._REGISTRY_FILE
        orig_dir = reg_mod._REGISTRY_DIR
        reg_mod._REGISTRY_FILE = reg_file
        reg_mod._REGISTRY_DIR = tmp_path / "registry"

        # Create two project dirs
        p1 = tmp_path / "project-alpha"
        p1.mkdir()
        (p1 / ".claude").mkdir()
        p2 = tmp_path / "project-beta"
        p2.mkdir()
        (p2 / ".claude").mkdir()

        registry.register_project(p1, slug="alpha")
        registry.register_project(p2, slug="beta")

        app = create_app(multi_project=True)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            ac.headers.update(_csrf_request_headers(ac))
            yield ac

        reg_mod._REGISTRY_FILE = orig_file
        reg_mod._REGISTRY_DIR = orig_dir

    async def test_home_shows_project_list(self, multi_client: AsyncClient) -> None:
        resp = await multi_client.get("/")
        assert resp.status_code == 200
        assert "alpha" in resp.text
        assert "beta" in resp.text

    @pytest.mark.parametrize(
        ("path", "status"),
        [
            pytest.param("/p/alpha", 307, id="project-root"),
            pytest.param("/p/alpha/dashboard", 301, id="dashboard"),
            pytest.param("/p/alpha/overview", 301, id="overview"),
            pytest.param("/p/alpha/agents", 301, id="agents"),
            pytest.param("/p/alpha/tasks", 301, id="tasks"),
            pytest.param("/p/alpha/runs", 301, id="runs"),
            pytest.param("/p/alpha/logs", 301, id="logs"),
            pytest.param("/p/alpha/memory", 301, id="memory"),
        ],
    )
    async def test_project_redirects_to_work(self, multi_client: AsyncClient, path: str, status: int) -> None:
        resp = await multi_client.get(path, follow_redirects=False)
        assert resp.status_code == status
        assert resp.headers["location"] == "/p/alpha/work"

    async def test_project_api(self, multi_client: AsyncClient) -> None:
        resp = await multi_client.get("/p/alpha/api/overview")
        assert resp.status_code == 200
        data = resp.json()
        assert "runs" in data

    async def test_healthz_checks_all_registered_project_dbs(self, multi_client: AsyncClient) -> None:
        resp = await multi_client.get("/healthz")
        assert resp.status_code == 200
        checks = resp.json()["checks"]
        assert checks["db_alpha"] == "ok"
        assert checks["db_beta"] == "ok"

    async def test_healthz_fails_when_one_project_db_is_down(self, multi_client: AsyncClient) -> None:
        from unittest.mock import AsyncMock, patch

        async def fake_check_db_health(project_dir=None, timeout=2.0):
            return "fail" if project_dir is not None and project_dir.name == "project-beta" else "ok"

        with patch("sova.dashboard.app._check_db_health", new=AsyncMock(side_effect=fake_check_db_health)):
            resp = await multi_client.get("/healthz")
        assert resp.status_code == 503
        checks = resp.json()["checks"]
        assert checks["db_alpha"] == "ok"
        assert checks["db_beta"] == "fail"

    async def test_projects_api_list(self, multi_client: AsyncClient) -> None:
        resp = await multi_client.get("/api/projects")
        assert resp.status_code == 200
        data = resp.json()
        assert "alpha" in data["projects"]
        assert "beta" in data["projects"]

    async def test_fallback_api_still_works(self, multi_client: AsyncClient) -> None:
        resp = await multi_client.get("/api/overview")
        assert resp.status_code == 200

    async def test_startup_skips_project_with_unloadable_config(self, tmp_path) -> None:
        """One registered project with a broken config must not abort startup for the rest.

        load_config() raises for llm.provider="ollama" without an explicit
        llm.model. The multi-project startup loop must skip that project
        (via _try_load_config) rather than letting the exception propagate
        out of the lifespan context manager and fail ASGI startup entirely.

        httpx's ASGITransport never drives the ASGI lifespan protocol, so a
        regular client-fixture test would pass even with the bug (the buggy
        loop would simply never run). Enter the FastAPI lifespan context
        manager directly to actually exercise the startup loop.

        setup_logging() is patched out: it calls structlog.configure()
        globally, and leaving that in place after this test would corrupt
        caplog-based assertions in every test module that runs afterward in
        the same session.
        """
        from unittest.mock import patch

        from structlog.testing import capture_logs

        from sova.config import registry

        reg_file = tmp_path / "registry" / "projects.json"
        import sova.config.registry as reg_mod

        orig_file = reg_mod._REGISTRY_FILE
        orig_dir = reg_mod._REGISTRY_DIR
        reg_mod._REGISTRY_FILE = reg_file
        reg_mod._REGISTRY_DIR = tmp_path / "registry"

        try:
            good = tmp_path / "project-good"
            good.mkdir()
            (good / ".claude").mkdir()

            broken = tmp_path / "project-broken"
            broken.mkdir()
            (broken / ".claude").mkdir()
            (broken / "sova.toml").write_text('[llm]\nprovider = "ollama"\n')

            registry.register_project(good, slug="good")
            registry.register_project(broken, slug="broken")

            app = create_app(multi_project=True)
            with capture_logs() as logs, patch("sova.utils.logging.setup_logging"):
                async with app.router.lifespan_context(app):
                    pass

            skipped = {e["event"] for e in logs if e["log_level"] == "warning" and e.get("project") == str(broken)}
            assert "supervisor.config_load_failed" in skipped
            assert not [e for e in logs if e.get("project") == str(good) and e["log_level"] == "warning"]
        finally:
            reg_mod._REGISTRY_FILE = orig_file
            reg_mod._REGISTRY_DIR = orig_dir

    async def test_startup_cleans_up_old_resources_per_project(self, tmp_path) -> None:
        """Multi-project startup extends resource-sample retention to every registered project.

        ``cleanup_old_resources()`` existed only as a tested, uncalled
        function (issue #1208): it must actually run during startup GC, once
        per registered project, not just for the single-project path.
        """
        from sova.config import registry

        reg_file = tmp_path / "registry" / "projects.json"
        import sova.config.registry as reg_mod

        orig_file = reg_mod._REGISTRY_FILE
        orig_dir = reg_mod._REGISTRY_DIR
        reg_mod._REGISTRY_FILE = reg_file
        reg_mod._REGISTRY_DIR = tmp_path / "registry"

        try:
            good = tmp_path / "project-good"
            good.mkdir()
            (good / ".claude").mkdir()
            registry.register_project(good, slug="good")

            app = create_app(multi_project=True)
            with (
                patch("sova.utils.logging.setup_logging"),
                patch("sova.monitoring.writer.cleanup_old_resources", new=AsyncMock(return_value=0)) as mock_cleanup,
            ):
                async with app.router.lifespan_context(app):
                    pass

            mock_cleanup.assert_any_call(good)
        finally:
            reg_mod._REGISTRY_FILE = orig_file
            reg_mod._REGISTRY_DIR = orig_dir

    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            pytest.param("/setup", "Project Setup", id="setup"),
            pytest.param("/p/alpha/work/1", "Run #1", id="project-work-detail"),
            # Supervisor page must not 500 in multi-project mode (github_repo context var).
            pytest.param("/p/alpha/supervisor", "Supervisor", id="project-supervisor"),
        ],
    )
    async def test_page_loads(self, multi_client: AsyncClient, path: str, expected: str) -> None:
        resp = await multi_client.get(path)
        assert resp.status_code == 200
        assert expected in resp.text

    async def test_project_phone_page_scopes_the_api_to_its_project(self, multi_client: AsyncClient) -> None:
        """A missing slug would silently fall back to the unscoped /api, which belongs to the primary project."""
        resp = await multi_client.get("/p/alpha/phone")
        assert resp.status_code == 200
        assert 'window.SOVA_PROJECT_SLUG = "alpha";' in resp.text
        assert (
            'window.SOVA_API_PREFIX = window.SOVA_PROJECT_SLUG ? "/p/" + window.SOVA_PROJECT_SLUG + "/api"' in resp.text
        )
        assert 'href="/p/alpha/work"' in resp.text
        assert "window.SOVA_RUN_ACTIONS = {" in resp.text
        assert "sova_csrf=" in resp.headers["set-cookie"]

    async def test_a_project_sidebar_carries_its_prefix(self, multi_client: AsyncClient) -> None:
        hrefs = _sidebar_hrefs((await multi_client.get("/p/alpha/work")).text)
        assert "/p/alpha/work" in hrefs
        assert "/setup" in hrefs
        assert not any(h in ("/work", "/settings") for h in hrefs)

    async def test_a_project_manifest_starts_on_its_own_home_page(self, multi_client: AsyncClient) -> None:
        manifest = (await multi_client.get("/p/alpha/manifest.webmanifest")).json()
        assert manifest["start_url"] == "/p/alpha/phone"
        assert manifest["id"] == "/p/alpha/phone"
        assert manifest["name"] == "SOVA project-alpha"
        html = (await multi_client.get("/p/alpha/work")).text
        assert '<link rel="manifest" href="/p/alpha/manifest.webmanifest">' in html

    async def test_project_lifecycle_renders(self, multi_client: AsyncClient) -> None:
        resp = await multi_client.get("/p/alpha/lifecycle/42")
        assert resp.status_code == 200
        assert "Issue " in resp.text and "#42" in resp.text

    async def test_uninstall_api_accepts_json_body(self, multi_client: AsyncClient) -> None:
        from unittest.mock import Mock, patch

        with patch("sova.cli.commands.project._uninstall", new_callable=Mock) as mock_uninstall:
            mock_uninstall.return_value = []
            resp = await multi_client.post(
                "/api/projects/uninstall",
                json={"slug": "alpha", "remove_files": True, "remove_commands": True, "remove_memory": True},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["removed"] is True
        assert data["files_cleaned"] is True
        mock_uninstall.assert_called_once()
        kwargs = mock_uninstall.call_args.kwargs
        assert kwargs["remove_commands"] is True
        assert kwargs["remove_rules"] is False
        assert kwargs["remove_memory"] is True
        assert kwargs["remove_config"] is False

    async def test_uninstall_unknown_slug_unregisters(self, multi_client: AsyncClient) -> None:
        from unittest.mock import Mock, patch

        with patch("sova.cli.commands.project._uninstall", new_callable=Mock) as mock_uninstall:
            resp = await multi_client.post(
                "/api/projects/uninstall",
                json={"slug": "nonexistent", "remove_files": True},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["removed"] is False
        assert data["files_cleaned"] is False
        mock_uninstall.assert_not_called()

    async def test_try_load_config_returns_none_on_broken_toml(self, tmp_path):
        """_try_load_config returns None when the project has invalid config."""
        from sova.dashboard.app import _try_load_config

        p = tmp_path / "broken-project"
        p.mkdir()
        (p / "sova.toml").write_text("INVALID TOML {{{{")

        result = _try_load_config(p)
        assert result is None

    async def test_try_load_config_returns_config_on_valid_project(self, tmp_path):
        """_try_load_config returns a ProjectConfig for valid projects."""
        from sova.dashboard.app import _try_load_config

        p = tmp_path / "valid-project"
        p.mkdir()
        (p / "sova.toml").write_text('[project]\ngithub_repo = "user/repo"\n')

        result = _try_load_config(p)
        assert result is not None
        assert result.github_repo == "user/repo"

    async def test_collect_supervisor_configs_skips_broken(self, tmp_path):
        """_collect_supervisor_configs skips projects with broken config."""
        from sova.dashboard.app import _collect_supervisor_configs

        broken = tmp_path / "broken"
        broken.mkdir()
        (broken / "sova.toml").write_text("INVALID {{{{")

        result = _collect_supervisor_configs({"broken": str(broken)})
        assert result == []

    async def test_collect_supervisor_configs_skips_nonexistent(self, tmp_path):
        """_collect_supervisor_configs skips directories that don't exist."""
        from sova.dashboard.app import _collect_supervisor_configs

        result = _collect_supervisor_configs({"gone": str(tmp_path / "nonexistent")})
        assert result == []

    async def test_collect_supervisor_configs_skips_disabled(self, tmp_path):
        """_collect_supervisor_configs skips projects with supervisor disabled."""
        from sova.dashboard.app import _collect_supervisor_configs

        p = tmp_path / "disabled-sv"
        p.mkdir()
        (p / "sova.toml").write_text('[project]\ngithub_repo = "u/r"\n\n[supervisor]\nenabled = false\n')

        result = _collect_supervisor_configs({"proj": str(p)})
        assert result == []

    async def test_collect_supervisor_configs_returns_enabled(self, tmp_path):
        """_collect_supervisor_configs returns projects with supervisor enabled."""
        from sova.dashboard.app import _collect_supervisor_configs

        p = tmp_path / "enabled-sv"
        p.mkdir()
        (p / "sova.toml").write_text('[project]\ngithub_repo = "u/r"\n\n[supervisor]\nenabled = true\n')

        result = _collect_supervisor_configs({"proj": str(p)})
        assert len(result) == 1
        assert result[0][0] == p
        assert result[0][1].supervisor.enabled is True


# ---------------------------------------------------------------------------
# WebSocket agent status
# ---------------------------------------------------------------------------


def _ws_headers(app: FastAPI) -> dict[str, str]:
    """Origin header a browser on the dashboard's own address would send."""
    return {"origin": sorted(app.state.allowed_origins)[0]}


def _close_coroutine_return(task: MagicMock):
    """Stand-in for ``asyncio.create_task`` that closes the coroutine it is handed and returns ``task``.

    A bare ``return_value`` drops the coroutine unawaited, and its "never awaited" warning surfaces as an
    unraisable exception in whichever later test the garbage collector happens to run during.
    """

    def _create_task(coro, *args, **kwargs):
        coro.close()
        return task

    return _create_task


class TestWebSocketAgentStatus:
    @pytest.fixture(autouse=True)
    def _no_sidebar_builds(self):
        """These tests drive the status tick; the activity and handoff builders open a project DB of their own, and the
        work-items refresh would call the tracker."""
        from sova.dashboard.routers import agents as agents_router

        with (
            patch.object(agents_router, "_build_activity_message", AsyncMock(return_value={"type": "activity"})),
            patch.object(agents_router, "_build_handoff_message", AsyncMock(return_value={"type": "handoff"})),
            patch.object(agents_router, "_refresh_work_items_snapshot", AsyncMock()),
        ):
            yield

    def test_websocket_connect_and_receive_status_update(self) -> None:
        """WebSocket endpoint accepts connection and sends status_update with runs."""
        from starlette.testclient import TestClient

        app = create_app(multi_project=False)
        client = TestClient(app)
        with client.websocket_connect("/api/ws/agents/status", headers=_ws_headers(app)) as ws:
            data = ws.receive_json()
            assert data["type"] == "status_update"
            assert isinstance(data["runs"], list)

    def test_websocket_client_disconnect(self) -> None:
        """Verify connection is removed from manager after client disconnects."""
        from starlette.testclient import TestClient

        from sova.dashboard.routers.agents import _ws_manager

        app = create_app(multi_project=False)
        client = TestClient(app)
        before = len(_ws_manager.active_connections)
        with client.websocket_connect("/api/ws/agents/status", headers=_ws_headers(app)) as ws:
            ws.receive_json()
        # After disconnect, connection count should be back to baseline
        assert len(_ws_manager.active_connections) == before

    def test_websocket_sequential_clients(self) -> None:
        """Sequential clients can each connect and receive updates."""
        from starlette.testclient import TestClient

        app = create_app(multi_project=False)
        # Use separate TestClient instances to avoid threading deadlock
        # when nesting websocket_connect context managers on the same client.
        client1 = TestClient(app)
        client2 = TestClient(app)
        with client1.websocket_connect("/api/ws/agents/status", headers=_ws_headers(app)) as ws1:
            d1 = ws1.receive_json()
            assert d1["type"] == "status_update"
        with client2.websocket_connect("/api/ws/agents/status", headers=_ws_headers(app)) as ws2:
            d2 = ws2.receive_json()
            assert d2["type"] == "status_update"

    def test_websocket_error_handling(self) -> None:
        """When get_all_agent_statuses raises, endpoint sends a distinct status_unavailable message, not empty runs."""
        from unittest.mock import AsyncMock, patch

        from starlette.testclient import TestClient

        app = create_app(multi_project=False)
        client = TestClient(app)
        with (
            patch(
                "sova.dashboard.services.agent_status.get_all_agent_statuses",
                new_callable=AsyncMock,
                side_effect=RuntimeError("DB failure"),
            ),
            client.websocket_connect("/api/ws/agents/status", headers=_ws_headers(app)) as ws,
        ):
            data = ws.receive_json()
            assert data["type"] == "status_unavailable"
            assert "runs" not in data

    def test_websocket_rejects_cross_origin(self) -> None:
        """A handshake from a foreign Origin is closed with 1008 before accept."""
        from starlette.testclient import TestClient
        from starlette.websockets import WebSocketDisconnect

        app = create_app(multi_project=False)
        client = TestClient(app)
        with (
            pytest.raises(WebSocketDisconnect) as exc_info,
            client.websocket_connect("/api/ws/agents/status", headers={"origin": "https://evil.example"}),
        ):
            pass
        assert exc_info.value.code == 1008

    @pytest.mark.parametrize("origin", [None, "null", "http://localhost:1"])
    def test_websocket_rejects_missing_null_or_wrong_port_origin(self, origin: str | None) -> None:
        from starlette.testclient import TestClient
        from starlette.websockets import WebSocketDisconnect

        app = create_app(multi_project=False)
        client = TestClient(app)
        headers = {} if origin is None else {"origin": origin}
        with (
            pytest.raises(WebSocketDisconnect) as exc_info,
            client.websocket_connect("/api/ws/agents/status", headers=headers),
        ):
            pass
        assert exc_info.value.code == 1008

    def test_websocket_fails_closed_when_allowed_origins_unset(self) -> None:
        from starlette.testclient import TestClient
        from starlette.websockets import WebSocketDisconnect

        app = create_app(multi_project=False)
        origin = sorted(app.state.allowed_origins)[0]
        del app.state.allowed_origins
        client = TestClient(app)
        with (
            pytest.raises(WebSocketDisconnect) as exc_info,
            client.websocket_connect("/api/ws/agents/status", headers={"origin": origin}),
        ):
            pass
        assert exc_info.value.code == 1008

    def test_websocket_fails_closed_on_non_loopback_bind_without_secret(self) -> None:
        from starlette.testclient import TestClient
        from starlette.websockets import WebSocketDisconnect

        app = create_app(multi_project=False, host="0.0.0.0")
        client = TestClient(app)
        with (
            pytest.raises(WebSocketDisconnect) as exc_info,
            client.websocket_connect("/api/ws/agents/status", headers=_ws_headers(app)),
        ):
            pass
        assert exc_info.value.code == 1008

    def test_websocket_non_loopback_bind_with_secret_allows_only_configured_host(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from starlette.testclient import TestClient
        from starlette.websockets import WebSocketDisconnect

        monkeypatch.setenv("SOVA_DASHBOARD_CSRF_SECRET", "x" * 32)
        app = create_app(multi_project=False, host="192.168.1.10", port=8111)
        assert app.state.csrf_fail_closed is False
        client = TestClient(app)
        with client.websocket_connect("/api/ws/agents/status", headers={"origin": "http://192.168.1.10:8111"}) as ws:
            assert ws.receive_json()["type"] == "status_update"
        with (
            pytest.raises(WebSocketDisconnect) as exc_info,
            client.websocket_connect("/api/ws/agents/status", headers={"origin": "http://127.0.0.1:8111"}),
        ):
            pass
        assert exc_info.value.code == 1008

    def test_websocket_accepts_ipv6_loopback_origin(self) -> None:
        from starlette.testclient import TestClient

        app = create_app(multi_project=False, host="::1", port=8111)
        assert "http://[::1]:8111" in app.state.allowed_origins
        client = TestClient(app)
        with client.websocket_connect("/api/ws/agents/status", headers={"origin": "http://[::1]:8111"}) as ws:
            assert ws.receive_json()["type"] == "status_update"

    def test_websocket_multi_project_isolation(self) -> None:
        """ConnectionManager groups connections by project_dir for isolation."""
        import asyncio
        from pathlib import Path
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.routers.agents import _ConnectionManager

        mgr = _ConnectionManager()

        ws_a = MagicMock()
        ws_a.send_json = AsyncMock()
        ws_b = MagicMock()
        ws_b.send_json = AsyncMock()

        dir_a = Path("/project-a")
        dir_b = Path("/project-b")

        # Patch create_task to avoid needing a running event loop
        dummy_task = MagicMock()
        dummy_task.done.return_value = False
        with patch("asyncio.create_task", side_effect=_close_coroutine_return(dummy_task)):
            mgr.connect(ws_a, dir_a)
            mgr.connect(ws_b, dir_b)

        # Each project group has exactly one connection
        assert len(mgr._groups.get(dir_a, [])) == 1
        assert len(mgr._groups.get(dir_b, [])) == 1

        # Broadcast to project A should only reach ws_a
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(mgr._broadcast({"type": "test"}, dir_a))
        finally:
            loop.close()
        ws_a.send_json.assert_awaited_once_with({"type": "test"})
        ws_b.send_json.assert_not_awaited()

        # Disconnect from project A should not affect project B
        mgr.disconnect(ws_a, dir_a)
        assert len(mgr._groups.get(dir_a, [])) == 0
        assert len(mgr._groups.get(dir_b, [])) == 1

        mgr.disconnect(ws_b, dir_b)
        assert len(mgr._groups.get(dir_b, [])) == 0

    async def test_agent_finished_reaches_subscriber_despite_unresolved_project_dir(self, tmp_path: Path) -> None:
        """ws_agent_status() registers under get_project_dir() or get_default_project_dir()
        (resolved), but a broadcaster can reach the same project through a different,
        unresolved-but-equivalent form (e.g. AgentState.project_dir's default_factory=Path.cwd,
        or any caller that never called .resolve()). Before _normalize_project_key(), the two
        sides landed in different _ConnectionManager groups and broadcast_event() silently
        dropped agent_finished (and graph_invalidated) for every subscriber (#1051 review,
        verified empirically against a real single-project deployment).
        """
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.routers.agents import _ConnectionManager
        from sova.dashboard.services.agent_finalize import _announce_agent_finished
        from sova.dashboard.services.agent_pool import AgentState

        mgr = _ConnectionManager()
        resolved_dir = (tmp_path / "sova-test-project-1051-ws").resolve()
        unresolved_equivalent = resolved_dir / "sub" / ".."
        assert unresolved_equivalent != resolved_dir
        assert unresolved_equivalent.resolve() == resolved_dir

        ws = MagicMock()
        ws.send_json = AsyncMock()
        dummy_task = MagicMock()
        dummy_task.done.return_value = False
        with patch("asyncio.create_task", side_effect=_close_coroutine_return(dummy_task)):
            mgr.connect(ws, resolved_dir)

        mock_process = MagicMock()
        agent = AgentState(
            run_id=9001,
            issue="900",
            role="developer",
            process=mock_process,
            project_dir=unresolved_equivalent,
        )

        with patch("sova.dashboard.routers.agents._ws_manager", mgr):
            await _announce_agent_finished(agent, status_changed=True, status="done")

        sent = [c.args[0] for c in ws.send_json.await_args_list]
        assert sent[0]["type"] == "agent_finished"
        assert sent[0]["run_id"] == 9001
        assert sent[0]["status"] == "done"
        assert sent[1] == {"type": "graph_invalidated"}

    @staticmethod
    def _slug_socket(slug: str | None) -> MagicMock:
        ws = MagicMock()
        ws.path_params = {} if slug is None else {"slug": slug}
        return ws

    def test_ws_slug_project_dir_resolves_a_registered_slug(self, tmp_path: Path) -> None:
        from sova.dashboard.routers.agents import _ws_slug_project_dir

        with patch("sova.config.registry.get_project_path", return_value=tmp_path) as lookup:
            assert _ws_slug_project_dir(self._slug_socket("proj-a")) == tmp_path
        lookup.assert_called_once_with("proj-a")

    @pytest.mark.parametrize("slug", [None, "", "../etc", "Bad_Slug"])
    def test_ws_slug_project_dir_ignores_a_missing_or_unsafe_slug(self, slug: str | None) -> None:
        from sova.dashboard.routers.agents import _ws_slug_project_dir

        with patch("sova.config.registry.get_project_path") as lookup:
            assert _ws_slug_project_dir(self._slug_socket(slug)) is None
        lookup.assert_not_called()

    def test_ws_slug_project_dir_ignores_an_unknown_slug(self) -> None:
        from sova.dashboard.routers.agents import _ws_slug_project_dir

        with patch("sova.config.registry.get_project_path", return_value=None):
            assert _ws_slug_project_dir(self._slug_socket("nope")) is None

    def test_ws_slug_project_dir_ignores_a_slug_whose_dir_is_gone(self, tmp_path: Path) -> None:
        from sova.dashboard.routers.agents import _ws_slug_project_dir

        with patch("sova.config.registry.get_project_path", return_value=tmp_path / "deleted"):
            assert _ws_slug_project_dir(self._slug_socket("gone")) is None

    @pytest.mark.parametrize("slug_resolves", [True, False])
    async def test_ws_agent_status_registers_under_the_slug_project(self, tmp_path: Path, slug_resolves: bool) -> None:
        """A /p/{slug}/api socket subscribes to its own project, not the default one the run-detail page never sees."""
        from fastapi import WebSocketDisconnect

        from sova.dashboard.routers import agents as agents_router

        slug_dir = tmp_path / "slug-project"
        slug_dir.mkdir()
        default_dir = tmp_path / "default-project"
        ws = self._slug_socket("proj-a")
        ws.accept = AsyncMock()
        ws.receive_text = AsyncMock(side_effect=WebSocketDisconnect())
        mgr = MagicMock()
        with (
            patch.object(agents_router, "validate_websocket_origin", return_value=True),
            patch.object(agents_router, "_ws_manager", mgr),
            patch("sova.config.registry.get_project_path", return_value=slug_dir if slug_resolves else None),
            patch("sova.config.context.resolve_project_dir_or_none", return_value=default_dir),
        ):
            await agents_router.ws_agent_status(ws)
        expected = slug_dir if slug_resolves else default_dir
        mgr.connect.assert_called_once_with(ws, expected, "proj-a" if slug_resolves else None)
        mgr.disconnect.assert_called_once_with(ws, expected)


class TestSingleProjectStartupResourceCleanup:
    """Single-project startup must also run cleanup_old_resources (issue #1208)."""

    async def test_startup_cleans_up_old_resources(self, tmp_path: Path) -> None:
        """ASGITransport never drives the lifespan protocol, so enter it directly."""
        (tmp_path / ".claude").mkdir()
        app = create_app(project_dir=tmp_path)
        with (
            patch("sova.utils.logging.setup_logging"),
            patch("sova.monitoring.writer.cleanup_old_resources", new=AsyncMock(return_value=0)) as mock_cleanup,
        ):
            async with app.router.lifespan_context(app):
                pass

        mock_cleanup.assert_any_call(tmp_path.resolve())


class TestRateLimiting:
    """Test rate limiting middleware."""

    async def test_requests_within_limit_pass(self, tmp_path: Path) -> None:
        """Test that requests within the rate limit are allowed."""
        os.environ["SOVA_DASHBOARD_RATE_LIMIT_PER_MINUTE"] = "3"
        try:
            app = create_app(project_dir=tmp_path)
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                # Make 3 requests (at the limit)
                for i in range(3):
                    response = await client.get("/healthz")
                    assert response.status_code == 200, f"Request {i + 1} failed"
        finally:
            os.environ.pop("SOVA_DASHBOARD_RATE_LIMIT_PER_MINUTE", None)

    async def test_requests_over_limit_get_429(self, tmp_path: Path) -> None:
        """Test that requests exceeding the rate limit get 429."""
        os.environ["SOVA_DASHBOARD_RATE_LIMIT_PER_MINUTE"] = "3"
        try:
            app = create_app(project_dir=tmp_path)
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                # Make 3 requests (at the limit)
                for _ in range(3):
                    response = await client.get("/healthz")
                    assert response.status_code == 200

                # The 4th request should be rate limited
                response = await client.get("/healthz")
                assert response.status_code == 429
                assert "X-RateLimit-Limit" in response.headers
        finally:
            os.environ.pop("SOVA_DASHBOARD_RATE_LIMIT_PER_MINUTE", None)

    async def test_static_files_exempt_from_rate_limit(self, tmp_path: Path) -> None:
        """Test that static file requests are exempt from rate limiting."""
        os.environ["SOVA_DASHBOARD_RATE_LIMIT_PER_MINUTE"] = "3"
        try:
            app = create_app(project_dir=tmp_path)
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                # Make many requests to static files (should all pass)
                for _ in range(10):
                    response = await client.get("/static/style.css")
                    # 404 is fine, we're just testing that rate limiting doesn't apply
                    assert response.status_code in {200, 404}
        finally:
            os.environ.pop("SOVA_DASHBOARD_RATE_LIMIT_PER_MINUTE", None)

    async def test_stream_endpoints_exempt_from_rate_limit(self, tmp_path: Path) -> None:
        """Test that streaming endpoints are exempt from rate limiting."""
        os.environ["SOVA_DASHBOARD_RATE_LIMIT_PER_MINUTE"] = "3"
        try:
            app = create_app(project_dir=tmp_path)
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                # Exhaust the rate limit on a normal endpoint first
                for _ in range(3):
                    await client.get("/healthz")
                # Verify a normal endpoint IS rate limited now
                response = await client.get("/healthz")
                assert response.status_code == 429

                # Paths ending with "/stream" are exempt (key_func returns None).
                # Use a path that ends with /stream but doesn't match a real
                # route to avoid SSE hang; the middleware decides exemption
                # before the route handler runs.
                for _ in range(5):
                    response = await client.get("/api/exempt/stream")
                    assert response.status_code != 429
        finally:
            os.environ.pop("SOVA_DASHBOARD_RATE_LIMIT_PER_MINUTE", None)

    async def test_rate_limit_disabled_when_zero(self, tmp_path: Path) -> None:
        """Test that rate limiting can be disabled by setting it to 0."""
        os.environ["SOVA_DASHBOARD_RATE_LIMIT_PER_MINUTE"] = "0"
        try:
            app = create_app(project_dir=tmp_path)
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                # Make many requests (should all pass when disabled)
                for _ in range(20):
                    response = await client.get("/healthz")
                    assert response.status_code == 200
        finally:
            os.environ.pop("SOVA_DASHBOARD_RATE_LIMIT_PER_MINUTE", None)


# ---------------------------------------------------------------------------
# CSRF/origin guard (sova/dashboard/security.py, issue #931)
# ---------------------------------------------------------------------------


class TestDashboardCsrfGuard:
    @pytest.mark.parametrize(
        ("host", "is_loopback", "fail_closed"),
        [
            pytest.param("127.0.0.1", True, False, id="loopback-bind-is-not-fail-closed"),
            pytest.param("0.0.0.0", False, True, id="nonloopback-bind-is-fail-closed"),
        ],
    )
    async def test_bind_without_secret(self, tmp_path: Path, host: str, is_loopback: bool, fail_closed: bool) -> None:
        app = create_app(project_dir=tmp_path, host=host, port=8111)
        assert app.state.is_loopback_bind is is_loopback
        assert app.state.csrf_fail_closed is fail_closed

    @pytest.mark.parametrize(
        ("host", "fail_closed"),
        [
            pytest.param("192.168.1.5", False, id="nonloopback-bind-is-not-fail-closed"),
            # A wildcard bind has no origin a real browser could ever send, so a configured secret must not
            # unlock it (issue #1020 review).
            pytest.param("0.0.0.0", True, id="wildcard-bind-is-still-fail-closed"),
            pytest.param("::", True, id="ipv6-wildcard-bind-is-still-fail-closed"),
        ],
    )
    async def test_nonloopback_bind_with_secret(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host: str, fail_closed: bool
    ) -> None:
        monkeypatch.setenv("SOVA_DASHBOARD_CSRF_SECRET", "test-secret")
        app = create_app(project_dir=tmp_path, host=host, port=8111)
        assert app.state.is_loopback_bind is False
        assert app.state.csrf_fail_closed is fail_closed

    async def test_port_env_beats_toml_port(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / "sova.toml").write_text("[dashboard]\nport = 8111\n")
        monkeypatch.setenv("SOVA_DASHBOARD_PORT", "9123")
        app = create_app(project_dir=tmp_path, host="127.0.0.1")
        assert "http://127.0.0.1:9123" in app.state.allowed_origins

    async def test_invalid_port_env_is_a_config_error(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A non-numeric SOVA_DASHBOARD_PORT fails like any invalid setting, TOML port or not.

        It used to be silently ignored whenever sova.toml set [dashboard] port, because the
        TOML value outranked the env var.
        """
        (tmp_path / "sova.toml").write_text("[dashboard]\nport = 8111\n")
        monkeypatch.setenv("SOVA_DASHBOARD_PORT", "not-a-port")
        with pytest.raises(SystemExit):
            create_app(project_dir=tmp_path, host="127.0.0.1")

    async def test_default_bind_matches_127_0_0_1_and_localhost_origins(self, tmp_path: Path) -> None:
        app = create_app(project_dir=tmp_path, port=8111)
        assert "http://127.0.0.1:8111" in app.state.allowed_origins
        assert "http://localhost:8111" in app.state.allowed_origins

    @staticmethod
    def _mount_guarded_route(app) -> None:
        from fastapi import Depends

        @app.post("/__test/guarded", dependencies=[Depends(require_same_origin_csrf)])
        async def _guarded() -> dict:
            return {"ok": True}

    async def test_guarded_route_rejects_cross_origin_request(self, tmp_path: Path) -> None:
        app = create_app(project_dir=tmp_path, host="127.0.0.1", port=8111)
        self._mount_guarded_route(app)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            ac.cookies.set(CSRF_COOKIE_NAME, "token")
            response = await ac.post(
                "/__test/guarded",
                headers={"Origin": "http://evil.example.com", CSRF_HEADER_NAME: "token"},
            )
            assert response.status_code == 403

    async def test_guarded_route_accepts_valid_same_origin_request(self, tmp_path: Path) -> None:
        app = create_app(project_dir=tmp_path, host="127.0.0.1", port=8111)
        self._mount_guarded_route(app)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            ac.cookies.set(CSRF_COOKIE_NAME, "matching-token")
            response = await ac.post(
                "/__test/guarded",
                headers={"Origin": "http://127.0.0.1:8111", CSRF_HEADER_NAME: "matching-token"},
            )
            assert response.status_code == 200
            assert response.json() == {"ok": True}

    async def test_guarded_route_nonloopback_without_secret_always_rejects(self, tmp_path: Path) -> None:
        app = create_app(project_dir=tmp_path, host="0.0.0.0", port=8111)
        self._mount_guarded_route(app)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            ac.cookies.set(CSRF_COOKIE_NAME, "matching-token")
            response = await ac.post(
                "/__test/guarded",
                headers={"Origin": "http://0.0.0.0:8111", CSRF_HEADER_NAME: "matching-token"},
            )
            assert response.status_code == 403

    async def test_guarded_route_wildcard_bind_with_secret_still_rejects(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Even with a matching cookie/header and a configured secret, a wildcard
        bind must reject: there is no real origin it could ever match (issue
        #1020 review)."""
        monkeypatch.setenv("SOVA_DASHBOARD_CSRF_SECRET", "test-secret")
        app = create_app(project_dir=tmp_path, host="0.0.0.0", port=8111)
        self._mount_guarded_route(app)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            ac.cookies.set(CSRF_COOKIE_NAME, "matching-token")
            response = await ac.post(
                "/__test/guarded",
                headers={"Origin": "http://0.0.0.0:8111", CSRF_HEADER_NAME: "matching-token"},
            )
            assert response.status_code == 403


# ---------------------------------------------------------------------------
# Health service / API (#1119)
# ---------------------------------------------------------------------------


@pytest.fixture
def _clear_health_cache():
    from sova.dashboard.services.health_service import _health_checks_cache

    _health_checks_cache.clear()
    yield
    _health_checks_cache.clear()


@pytest.mark.usefixtures("_clear_health_cache")
class TestHealthService:
    async def test_returns_serialized_checks(self, tmp_path: Path) -> None:
        from sova.dashboard.services import health_service
        from sova.utils.diagnostics import DiagnosticCheck

        checks = [
            DiagnosticCheck("git", True, "git version 2.43.0", True),
            DiagnosticCheck("rtk", False, "not found", False),
        ]
        with patch.object(health_service, "run_all_checks", new=AsyncMock(return_value=checks)):
            result = await health_service.get_health_checks(tmp_path)

        assert result["cached"] is False
        assert result["checks"] == [
            {"name": "git", "passed": True, "detail": "git version 2.43.0", "required": True},
            {"name": "rtk", "passed": False, "detail": "not found", "required": False},
        ]
        assert "checked_at" in result

    async def test_a_raising_batch_becomes_one_failed_row_not_an_exception(self, tmp_path: Path) -> None:
        """A failure anywhere in run_all_checks() must not 500 the caller or
        blank the panel: it is reported as a single failed check row."""
        from sova.dashboard.services import health_service

        with patch.object(health_service, "run_all_checks", new=AsyncMock(side_effect=RuntimeError("boom"))):
            result = await health_service.get_health_checks(tmp_path)

        assert len(result["checks"]) == 1
        assert result["checks"][0]["passed"] is False
        assert "boom" in result["checks"][0]["detail"]

    async def test_cache_hit_returns_cached_true(self, tmp_path: Path) -> None:
        from sova.dashboard.services import health_service
        from sova.utils.diagnostics import DiagnosticCheck

        mock_run = AsyncMock(return_value=[DiagnosticCheck("git", True, "ok", True)])
        with patch.object(health_service, "run_all_checks", new=mock_run):
            first = await health_service.get_health_checks(tmp_path)
            second = await health_service.get_health_checks(tmp_path)

        assert first["cached"] is False
        assert second["cached"] is True
        assert second["checked_at"] == first["checked_at"]
        assert mock_run.call_count == 1

    async def test_concurrent_uncached_requests_share_one_probe(self, tmp_path: Path) -> None:
        """Single-flight lock: two concurrent calls racing a cold cache must
        not each run run_all_checks(), mirroring setup_service's auth-status cache."""
        from sova.dashboard.services import health_service
        from sova.utils.diagnostics import DiagnosticCheck

        probe_count = 0

        async def _slow_checks(project_dir: Path):
            nonlocal probe_count
            probe_count += 1
            await asyncio.sleep(0.05)
            return [DiagnosticCheck("git", True, "ok", True)]

        with patch.object(health_service, "run_all_checks", new=_slow_checks):
            first, second = await asyncio.gather(
                health_service.get_health_checks(tmp_path),
                health_service.get_health_checks(tmp_path),
            )

        assert probe_count == 1
        assert first["checks"] == second["checks"]


@pytest.mark.usefixtures("_clear_health_cache")
class TestHealthChecksAPI:
    async def test_checks_endpoint_returns_serialized_results(self, client: AsyncClient) -> None:
        from sova.dashboard.services import health_service
        from sova.utils.diagnostics import DiagnosticCheck

        checks = [DiagnosticCheck("git", True, "git version 2.43.0", True)]
        with patch.object(health_service, "run_all_checks", new=AsyncMock(return_value=checks)):
            resp = await client.get("/api/health/checks")

        assert resp.status_code == 200
        data = resp.json()
        assert data["checks"] == [{"name": "git", "passed": True, "detail": "git version 2.43.0", "required": True}]
        assert data["cached"] is False

    async def test_checks_endpoint_never_500s_on_internal_failure(self, client: AsyncClient) -> None:
        from sova.dashboard.services import health_service

        with patch.object(health_service, "run_all_checks", new=AsyncMock(side_effect=RuntimeError("boom"))):
            resp = await client.get("/api/health/checks")

        assert resp.status_code == 200
        data = resp.json()
        assert len(data["checks"]) == 1
        assert data["checks"][0]["passed"] is False

    async def test_project_endpoint_reports_the_served_project_dir(self, client: AsyncClient) -> None:
        # The MCP operator client (sova/mcp/operator.py) reads this key to confirm it reached the project it
        # means before sending a project-scoped call; renaming or dropping it breaks that check.
        resp = await client.get("/api/health/project")

        assert resp.status_code == 200
        data = resp.json()
        assert list(data) == ["project_dir"]
        assert Path(data["project_dir"]).is_absolute()
