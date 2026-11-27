"""Conversations API (issue #1544): the feed panel's conversation tabs over HTTP, with a fake runtime."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient

from sova.dashboard.services import conversation_service as svc
from tests.conversation_helpers import FakeProcess, FakeRuntime, drop_live_turns

pytestmark = pytest.mark.usefixtures("reset_db_rows")


@pytest.fixture(autouse=True)
async def _runtime(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[dict[str, Any]]:
    state: dict[str, Any] = {"runtime": FakeRuntime(continues=True)}

    async def runtime_for(_project_dir: Path | None) -> Any:
        return state["runtime"]

    monkeypatch.setattr(svc, "runtime_for_async", runtime_for)
    await drop_live_turns()
    yield state
    await drop_live_turns()


async def _settle() -> None:
    for _ in range(300):
        if not svc._turns:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("turn never finished")


class TestConversationsAPI:
    async def test_create_post_list_close_reopen_delete(self, client: AsyncClient) -> None:
        created = (await client.post("/api/conversations", json={"message": "What runs now?"})).json()
        cid = created["conversation"]["id"]
        assert created["conversation"]["title"] == "What runs now?"
        assert created["turn"]["user_message"]["content"] == "What runs now?"
        await _settle()

        follow = await client.post(f"/api/conversations/{cid}/messages", json={"text": "And next?"})
        assert follow.status_code == 200
        await _settle()
        detail = (await client.get(f"/api/conversations/{cid}")).json()
        assert [m["role"] for m in detail["messages"]] == ["user", "assistant", "user", "assistant"]
        assert detail["messages"][-1]["status"] == "done"

        assert (await client.post(f"/api/conversations/{cid}/close")).json()["status"] == "closed"
        listing = (await client.get("/api/conversations")).json()
        assert listing["open"] == []
        assert [c["id"] for c in listing["past"]] == [cid]

        assert (await client.post(f"/api/conversations/{cid}/reopen")).json()["status"] == "open"
        assert [c["id"] for c in (await client.get("/api/conversations")).json()["open"]] == [cid]

        assert (await client.delete(f"/api/conversations/{cid}")).json() == {"deleted": True}
        assert (await client.delete(f"/api/conversations/{cid}")).json() == {"deleted": False}
        assert (await client.get(f"/api/conversations/{cid}")).status_code == 404

    async def test_an_issue_bound_conversation_opens_without_a_turn(self, client: AsyncClient) -> None:
        created = (await client.post("/api/conversations", json={"issue": "#42"})).json()
        assert created["turn"] is None
        assert created["conversation"]["issue_number"] == "42"

    async def test_a_fourth_turn_is_refused_and_the_message_is_not_stored(
        self, client: AsyncClient, _runtime: dict[str, Any]
    ) -> None:
        hold = asyncio.Event()
        _runtime["runtime"] = FakeRuntime(continues=False, script=lambda n: FakeProcess([], hold=hold))
        ids = []
        for i in range(3):
            response = await client.post("/api/conversations", json={"message": f"q{i}"})
            ids.append(response.json()["conversation"]["id"])
        target = (await client.post("/api/conversations", json={})).json()["conversation"]["id"]

        try:
            refused = await client.post(f"/api/conversations/{target}/messages", json={"text": "fourth"})
            assert refused.status_code == 409
            assert "conversation.max_concurrent" in refused.json()["detail"]
            assert (await client.get(f"/api/conversations/{target}")).json()["messages"] == []
            # A refused first message leaves no conversation behind either.
            assert (await client.post("/api/conversations", json={"message": "fifth"})).status_code == 409
            assert len((await client.get("/api/conversations")).json()["open"]) == 4

            stopped = await client.post(f"/api/conversations/{ids[0]}/stop")
            assert stopped.json() == {"stopped": True}
            assert (await client.get(f"/api/conversations/{ids[0]}")).json()["messages"][-1]["status"] == "stopped"
        finally:
            hold.set()
            await _settle()

    @pytest.mark.parametrize(
        ("method", "path", "body"),
        [
            ("GET", "/api/conversations/999", None),
            ("POST", "/api/conversations/999/messages", {"text": "hi"}),
            ("POST", "/api/conversations/999/close", None),
            ("POST", "/api/conversations/999/reopen", None),
        ],
    )
    async def test_an_unknown_conversation_is_404(self, client: AsyncClient, method: str, path: str, body: Any) -> None:
        response = await client.request(method, path, json=body)
        assert response.status_code == 404

    async def test_turn_cost_reaches_the_costs_totals(self, client: AsyncClient, _runtime: dict[str, Any]) -> None:
        _runtime["runtime"] = FakeRuntime(
            continues=False, script=lambda n: FakeProcess([json.dumps({"result": {"cost": "1.5"}})])
        )
        await client.post("/api/conversations", json={"message": "cost?"})
        await _settle()

        summary = (await client.get("/api/costs/summary")).json()
        assert float(summary["total_cost_usd"]) == 1.5
        assert float(summary["conversation_cost_usd"]) == 1.5
        assert (await client.get("/api/runs")).json().get("runs", []) == []

    @pytest.mark.parametrize(
        "query",
        ["", "issue=%20", "issue=../x", "issue=1%202", "issue=5&run_id=1", "issue=--web", "issue=%23-5", "issue=-"],
    )
    async def test_explain_refuses_a_malformed_or_ambiguous_target(self, client: AsyncClient, query: str) -> None:
        response = await client.get(f"/api/issues/explain?{query}")
        assert response.status_code == 400

    @pytest.mark.parametrize("issue", ["not an issue", "../x", "1 2", "--web", "#-5", "-"])
    async def test_an_issue_that_is_no_issue_key_is_refused_before_any_row(
        self, client: AsyncClient, issue: str
    ) -> None:
        response = await client.post("/api/conversations", json={"issue": issue})

        assert response.status_code == 422
        assert (await client.get("/api/conversations")).json()["open"] == []

    async def test_a_tracker_key_and_a_blank_issue_are_accepted(self, client: AsyncClient) -> None:
        keyed = (await client.post("/api/conversations", json={"issue": "PROJ-123"})).json()["conversation"]
        blank = (await client.post("/api/conversations", json={"issue": "  "})).json()["conversation"]

        assert keyed["issue_number"] == "PROJ-123"
        assert blank["issue_number"] is None

    async def test_a_blank_message_is_400_on_both_routes(self, client: AsyncClient) -> None:
        assert (await client.post("/api/conversations", json={"message": "   "})).status_code == 400
        cid = (await client.post("/api/conversations", json={})).json()["conversation"]["id"]
        assert (await client.post(f"/api/conversations/{cid}/messages", json={"text": "   "})).status_code == 400

    async def test_a_first_turn_that_breaks_unexpectedly_leaves_no_empty_tab(
        self, client_no_raise: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = client_no_raise

        async def broken(*_a: Any, **_kw: Any) -> dict:
            raise RuntimeError("database went away")

        monkeypatch.setattr(svc, "start_turn", broken)
        response = await client.post("/api/conversations", json={"message": "hello"})

        assert response.status_code == 500
        assert (await client.get("/api/conversations")).json()["open"] == []

    def test_the_message_routes_document_400_and_404(self) -> None:
        from sova.dashboard.routers.conversations import router

        documented = {
            (route.path, tuple(sorted(route.responses))) for route in router.routes if "POST" in route.methods
        }
        assert ("/conversations", (400, 409)) in documented
        assert ("/conversations/{conversation_id}/messages", (400, 404, 409)) in documented
