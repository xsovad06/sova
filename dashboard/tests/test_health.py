"""Smoke tests -- verify the dashboard starts and serves pages."""

import pytest


@pytest.mark.anyio
async def test_home_redirects(client):
    resp = await client.get("/", follow_redirects=False)
    assert resp.status_code in (200, 302, 307)


@pytest.mark.anyio
async def test_overview_page(client):
    resp = await client.get("/overview")
    assert resp.status_code == 200
    assert "overview" in resp.text.lower() or "Project Automation Kit" in resp.text


@pytest.mark.anyio
async def test_setup_page(client):
    resp = await client.get("/setup")
    assert resp.status_code == 200


@pytest.mark.anyio
async def test_costs_page(client):
    resp = await client.get("/costs")
    assert resp.status_code == 200


@pytest.mark.anyio
async def test_static_files(client):
    resp = await client.get("/static/main.js")
    # Static file should exist or return 404 (not 500)
    assert resp.status_code in (200, 404)
