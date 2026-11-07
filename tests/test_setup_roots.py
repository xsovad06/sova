"""Tests for the setup wizard's project-root allowlist (`dashboard.setup_roots`)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from sova.dashboard.routers import setup as setup_router
from sova.db.session import close_db, init_db
from tests.conftest import csrf_request_headers


@pytest.fixture(autouse=True)
async def setup_db():
    os.environ["SOVA_DATABASE_URL"] = "sqlite+aiosqlite://"
    await init_db(run_migrations=False)
    yield
    await close_db()
    os.environ.pop("SOVA_DATABASE_URL", None)


@pytest.fixture
def roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """An allowed root and a sibling directory outside it."""
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    monkeypatch.setenv("SOVA_DASHBOARD_SETUP_ROOTS", json.dumps([str(allowed)]))
    return allowed, outside


@pytest.fixture
async def client(roots):
    from sova.dashboard.app import create_app

    app = create_app(multi_project=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8111") as ac:
        ac.headers.update(csrf_request_headers(ac))
        yield ac


ENDPOINTS = [
    ("/api/setup/browse", "path"),
    ("/api/setup/scan", "project_path"),
    ("/api/setup/install", "project_path"),
    ("/api/setup/configure", "project_path"),
    ("/api/setup/milestones/create", "project_path"),
]


@pytest.mark.parametrize(("url", "field"), ENDPOINTS)
async def test_path_outside_roots_is_403(client: AsyncClient, roots, url: str, field: str) -> None:
    resp = await client.post(url, json={field: str(roots[1])})
    assert resp.status_code == 403
    assert str(roots[0]) not in resp.json()["detail"]


@pytest.mark.parametrize(("url", "field"), ENDPOINTS)
async def test_nonexistent_path_outside_roots_is_403_not_404(client: AsyncClient, roots, url: str, field: str) -> None:
    """The root check precedes the existence check, so 403 never reveals whether a path exists."""
    resp = await client.post(url, json={field: str(roots[1] / "missing")})
    assert resp.status_code == 403


@pytest.mark.parametrize("url", ["/api/setup/install", "/api/setup/configure", "/api/setup/milestones/create"])
async def test_nonexistent_path_under_root_keeps_404(client: AsyncClient, roots, url: str) -> None:
    resp = await client.post(url, json={"project_path": str(roots[0] / "missing")})
    assert resp.status_code == 404
    assert "Directory not found" in resp.json()["detail"]


async def test_dotdot_cannot_escape_root(client: AsyncClient, roots) -> None:
    resp = await client.post("/api/setup/scan", json={"project_path": f"{roots[0]}/../outside"})
    assert resp.status_code == 403


async def test_symlink_inside_root_pointing_outside_is_403(client: AsyncClient, roots) -> None:
    link = roots[0] / "escape"
    link.symlink_to(roots[1], target_is_directory=True)
    resp = await client.post("/api/setup/scan", json={"project_path": str(link)})
    assert resp.status_code == 403


async def test_root_configured_as_symlink_allows_its_target(
    tmp_path: Path, roots, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = roots[1]
    (target / "proj").mkdir()
    link_root = tmp_path / "link-root"
    link_root.symlink_to(target, target_is_directory=True)
    monkeypatch.setenv("SOVA_DASHBOARD_SETUP_ROOTS", json.dumps([str(link_root)]))
    assert setup_router._resolve_allowed_path(str(link_root / "proj")) == (target / "proj").resolve()
    assert setup_router._resolve_allowed_path(str(target / "proj")) == (target / "proj").resolve()


async def test_path_inside_root_is_allowed(client: AsyncClient, roots) -> None:
    (roots[0] / "proj").mkdir()
    (roots[0] / "proj" / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
    resp = await client.post("/api/setup/scan", json={"project_path": str(roots[0] / "proj")})
    assert resp.status_code == 200
    assert resp.json()["project_name"] == "proj"


async def test_root_itself_is_allowed(client: AsyncClient, roots) -> None:
    resp = await client.post("/api/setup/browse", json={"path": str(roots[0])})
    assert resp.status_code == 200
    assert resp.json()["current"] == str(roots[0].resolve())


async def test_sibling_with_shared_prefix_is_not_inside_root(client: AsyncClient, roots, tmp_path: Path) -> None:
    """`/x/allowed-evil` shares a string prefix with `/x/allowed` but is not inside it."""
    evil = tmp_path / "allowed-evil"
    evil.mkdir()
    resp = await client.post("/api/setup/scan", json={"project_path": str(evil)})
    assert resp.status_code == 403


async def test_empty_browse_path_starts_at_first_root(client: AsyncClient, roots) -> None:
    resp = await client.post("/api/setup/browse", json={"path": ""})
    assert resp.status_code == 200
    assert resp.json()["current"] == str(roots[0].resolve())


async def test_empty_browse_path_with_missing_root_lists_nothing_outside(
    client: AsyncClient, roots, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A configured root that does not exist yet must not make browse list its parent directory."""
    missing = roots[0] / "not-created-yet"
    (roots[0] / "sibling-of-missing").mkdir()
    monkeypatch.setenv("SOVA_DASHBOARD_SETUP_ROOTS", json.dumps([str(missing)]))
    monkeypatch.setattr(setup_router, "get_default_project_dir", lambda: None)
    resp = await client.post("/api/setup/browse", json={"path": ""})
    assert resp.status_code == 404
    assert "sibling-of-missing" not in resp.text
    resp = await client.post("/api/setup/browse", json={"path": str(missing)})
    assert resp.status_code == 404
    assert "sibling-of-missing" not in resp.text


async def test_empty_browse_path_skips_missing_first_root(
    client: AsyncClient, roots, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = roots[0] / "not-created-yet"
    existing = roots[0] / "real"
    existing.mkdir()
    monkeypatch.setenv("SOVA_DASHBOARD_SETUP_ROOTS", json.dumps([str(missing), str(existing)]))
    monkeypatch.setattr(setup_router, "get_default_project_dir", lambda: None)
    resp = await client.post("/api/setup/browse", json={"path": ""})
    assert resp.status_code == 200
    assert resp.json()["current"] == str(existing.resolve())


async def test_browse_of_file_root_does_not_list_its_parent(
    client: AsyncClient, roots, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A root that is a file would make the service fall back to its parent; the router refuses."""
    file_root = roots[0] / "afile"
    file_root.write_text("x", encoding="utf-8")
    (roots[0] / "other-dir").mkdir()
    monkeypatch.setenv("SOVA_DASHBOARD_SETUP_ROOTS", json.dumps([str(file_root)]))
    monkeypatch.setattr(setup_router, "get_default_project_dir", lambda: None)
    resp = await client.post("/api/setup/browse", json={"path": str(file_root)})
    assert resp.status_code == 404
    assert "other-dir" not in resp.text


async def test_empty_scan_path_is_checked_like_any_other(
    client: AsyncClient, roots, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty path resolves to the working directory, which must itself be inside a root."""
    monkeypatch.chdir(roots[1])
    resp = await client.post("/api/setup/scan", json={})
    assert resp.status_code == 403
    monkeypatch.chdir(roots[0])
    resp = await client.post("/api/setup/scan", json={})
    assert resp.status_code == 200


async def test_relative_path_resolves_against_cwd_then_is_checked(
    client: AsyncClient, roots, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(roots[0])
    assert (await client.post("/api/setup/scan", json={"project_path": "."})).status_code == 200
    monkeypatch.chdir(roots[1])
    assert (await client.post("/api/setup/scan", json={"project_path": "."})).status_code == 403


async def test_tilde_is_expanded_before_the_check(client: AsyncClient, roots, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(roots[1]))
    resp = await client.post("/api/setup/scan", json={"project_path": "~"})
    assert resp.status_code == 403


async def test_browse_at_root_hides_parent_entry(client: AsyncClient, roots) -> None:
    (roots[0] / "child").mkdir()
    resp = await client.post("/api/setup/browse", json={"path": str(roots[0])})
    names = [e["name"] for e in resp.json()["entries"]]
    assert ".." not in names
    assert "child" in names


async def test_browse_below_root_keeps_parent_entry(client: AsyncClient, roots) -> None:
    (roots[0] / "child").mkdir()
    resp = await client.post("/api/setup/browse", json={"path": str(roots[0] / "child")})
    assert ".." in [e["name"] for e in resp.json()["entries"]]


async def test_project_dir_is_always_allowed(tmp_path: Path, roots, monkeypatch: pytest.MonkeyPatch) -> None:
    """The project the dashboard serves stays usable even when it is outside every configured root."""
    from sova.dashboard.app import create_app

    project = roots[1]
    # create_app(project_dir=...) sets a process-wide default; restore it afterwards.
    monkeypatch.setattr("sova.dashboard.services.agent_pool._default_project_dir", None)
    app = create_app(project_dir=project)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8111") as ac:
        ac.headers.update(csrf_request_headers(ac))
        resp = await ac.post("/api/setup/scan", json={"project_path": str(project)})
    assert resp.status_code == 200


def test_blank_root_entries_are_ignored(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A blank root would resolve to the working directory and silently allow it."""
    monkeypatch.setenv("SOVA_DASHBOARD_SETUP_ROOTS", json.dumps(["", "  ", str(tmp_path)]))
    monkeypatch.setattr(setup_router, "get_default_project_dir", lambda: None)
    assert setup_router._allowed_roots() == [tmp_path.resolve()]


def test_unloadable_config_fails_closed_to_project_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def boom(*_a, **_k):
        raise RuntimeError("bad config")

    monkeypatch.setattr(setup_router, "load_config", boom)
    monkeypatch.setattr(setup_router, "get_default_project_dir", lambda: tmp_path)
    assert setup_router._allowed_roots() == [tmp_path.resolve()]


def test_no_project_in_scope_uses_env_roots_not_cwd(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Without a project, roots come from the env or default, never from load_config(cwd)."""

    def boom(*_a, **_k):
        raise AssertionError("load_config must not be called without a project")

    monkeypatch.setattr(setup_router, "load_config", boom)
    monkeypatch.setattr(setup_router, "get_default_project_dir", lambda: None)
    monkeypatch.setenv("SOVA_DASHBOARD_SETUP_ROOTS", json.dumps([str(tmp_path)]))
    assert setup_router._allowed_roots() == [tmp_path.resolve()]
    monkeypatch.delenv("SOVA_DASHBOARD_SETUP_ROOTS")
    assert setup_router._allowed_roots() == [Path("~").expanduser().resolve()]


async def test_browse_reports_the_containing_root(client: AsyncClient, roots) -> None:
    (roots[0] / "child").mkdir()
    resp = await client.post("/api/setup/browse", json={"path": str(roots[0] / "child")})
    assert resp.json()["root"] == str(roots[0].resolve())


def test_default_root_is_home() -> None:
    from sova.config.models import DashboardConfig

    assert DashboardConfig().setup_roots == ["~"]


async def test_empty_browse_path_with_no_roots_is_403(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """With no allowed root at all (no config roots, no project dir), an empty path has nowhere to start."""
    monkeypatch.setattr(setup_router, "_allowed_roots", lambda: [])
    resp = await client.post("/api/setup/browse", json={"path": ""})
    assert resp.status_code == 403
    assert resp.json()["detail"] == setup_router._OUTSIDE_ROOTS_DETAIL


def test_unresolvable_root_entry_is_dropped_not_fatal(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A `~nosuchuser` root is skipped; the remaining roots still apply."""
    monkeypatch.setenv("SOVA_DASHBOARD_SETUP_ROOTS", json.dumps(["~nosuchuser_zz9/projects", str(tmp_path)]))
    monkeypatch.setattr(setup_router, "get_default_project_dir", lambda: None)
    assert setup_router._allowed_roots() == [tmp_path.resolve()]


@pytest.mark.parametrize("bad_path", ["~nosuchuser_zz9/x", "/tmp/with\x00nul"])
@pytest.mark.parametrize(("url", "field"), ENDPOINTS)
async def test_unresolvable_client_path_is_403_not_500(
    client: AsyncClient, url: str, field: str, bad_path: str
) -> None:
    resp = await client.post(url, json={field: bad_path})
    assert resp.status_code == 403
    assert resp.json()["detail"] == setup_router._OUTSIDE_ROOTS_DETAIL
