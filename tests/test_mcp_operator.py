"""Tests for the MCP operator tools: a loopback HTTP client of the running SOVA server."""

from __future__ import annotations

import ast
import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from sova.dashboard.security import CSRF_COOKIE_NAME, CSRF_HEADER_NAME
from sova.mcp import operator
from sova.mcp.operator import MAX_RESPONSE_BYTES, OperatorClient, ServerRefusal, register_operator_tools

Route = tuple[int, Any]


IDENTITY_PATH = "/api/health/project"


class FakeServer:
    """An httpx transport handler that serves canned routes and records every request.

    Unless a test overrides it, the server answers the identity route as a single-project server for the
    test's own project directory (``served_dir``, set per test by ``_serve_the_test_project``).
    """

    served_dir: Path | None = None

    def __init__(self, routes: dict[tuple[str, str], Route | Callable[[httpx.Request], Route]] | None = None) -> None:
        self.routes = routes or {}
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        route = self.routes.get((request.method, request.url.path), (404, {"detail": "Not Found"}))
        if request.url.path == IDENTITY_PATH and (request.method, IDENTITY_PATH) not in self.routes and self.served_dir:
            route = (200, {"project_dir": str(self.served_dir)})
        status, body = route(request) if callable(route) else route
        if isinstance(body, bytes):
            return httpx.Response(status, content=body)
        return httpx.Response(status, json=body)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    def sent(self, method: str, path: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == method and r.url.path == path]


def _tool_server(tmp_path: Path, fake: FakeServer | None = None) -> MCPServer:
    server = MCPServer(name="t")
    register_operator_tools(server, project_dir=tmp_path, transport=(fake or FakeServer()).transport)
    return server


@pytest.fixture(autouse=True)
def _serve_the_test_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(FakeServer, "served_dir", tmp_path)


@pytest.fixture
def project(tmp_path: Path, seed_config: Callable[..., None]) -> Path:
    seed_config(tmp_path)
    return tmp_path


# ---------------------------------------------------------------------------
# OperatorClient
# ---------------------------------------------------------------------------


class TestRequestHeaders:
    async def test_sends_origin_and_matching_csrf_pair(self, project: Path) -> None:
        fake = FakeServer({("POST", "/api/handoff/clear"): (200, {"cleared": 1})})
        await OperatorClient(project, transport=fake.transport).request("POST", "/handoff/clear", body={"issue": "1"})

        request = fake.sent("POST", "/api/handoff/clear")[0]
        token = request.headers[CSRF_HEADER_NAME]
        assert token
        assert request.headers["cookie"] == f"{CSRF_COOKIE_NAME}={token}"
        assert request.headers["origin"] == "http://127.0.0.1:8111"
        assert json.loads(request.content) == {"issue": "1"}

    @pytest.mark.parametrize(
        ("host", "expected_origin"),
        [
            pytest.param("localhost", "http://localhost:8111", id="localhost"),
            pytest.param("::1", "http://[::1]:8111", id="ipv6 literal is bracketed"),
        ],
    )
    async def test_loopback_spellings_are_accepted(
        self, tmp_path: Path, seed_config: Callable[..., None], host: str, expected_origin: str
    ) -> None:
        seed_config(tmp_path, server={"host": host})
        fake = FakeServer({("GET", "/api/health"): (200, {"status": "ok"})})
        await OperatorClient(tmp_path, transport=fake.transport).request("GET", "/health", project_scoped=False)

        assert fake.requests[0].headers["origin"] == expected_origin
        assert str(fake.requests[0].url).startswith(expected_origin)

    @pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.20", "example.com", "::"])
    async def test_non_loopback_host_is_refused_before_any_request(
        self, tmp_path: Path, seed_config: Callable[..., None], host: str
    ) -> None:
        seed_config(tmp_path, server={"host": host}, dashboard={"csrf_secret": "configured"})
        fake = FakeServer()
        client = OperatorClient(tmp_path, transport=fake.transport)
        with pytest.raises(ToolError, match="non-loopback"):
            await client.request("GET", "/health", project_scoped=False)
        assert fake.requests == []


class TestUnreachableServer:
    async def test_error_names_every_endpoint_tried(self, tmp_path: Path, seed_config: Callable[..., None]) -> None:
        seed_config(tmp_path, server={"port": 9001}, dashboard={"port": 9002})

        def refuse(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        client = OperatorClient(tmp_path, transport=httpx.MockTransport(refuse))
        with pytest.raises(ToolError, match=r"127\.0\.0\.1:9001, 127\.0\.0\.1:9002"):
            await client.request("GET", "/health", project_scoped=False)

    async def test_falls_back_to_dashboard_port_on_refusal(
        self, tmp_path: Path, seed_config: Callable[..., None]
    ) -> None:
        seed_config(tmp_path, server={"port": 9001}, dashboard={"port": 9002})
        seen: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.port or 0)
            if request.url.port == 9001:
                raise httpx.ConnectError("refused", request=request)
            if request.url.path == IDENTITY_PATH:
                return httpx.Response(200, json={"project_dir": str(tmp_path)})
            if request.url.path == "/api/health":
                return httpx.Response(200, json={"status": "ok"})
            return httpx.Response(404, json={"detail": "Not Found"})

        client = OperatorClient(tmp_path, transport=httpx.MockTransport(handler))
        assert await client.request("GET", "/health", project_scoped=False) == {"status": "ok"}
        assert seen == [9001, 9002, 9002, 9002]

    async def test_a_refusal_from_the_server_is_not_retried_on_the_other_port(
        self, tmp_path: Path, seed_config: Callable[..., None]
    ) -> None:
        seed_config(tmp_path, server={"port": 9001}, dashboard={"port": 9002})
        fake = FakeServer({("POST", "/api/agents/start"): (409, {"detail": "busy"})})
        client = OperatorClient(tmp_path, transport=fake.transport)
        with pytest.raises(ToolError, match="busy"):
            await client.request("POST", "/agents/start", body={})
        assert {r.url.port for r in fake.requests} == {9001}

    async def test_read_timeout_is_an_error_and_not_retried(
        self, tmp_path: Path, seed_config: Callable[..., None]
    ) -> None:
        seed_config(tmp_path, server={"port": 9001}, dashboard={"port": 9002})
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            raise httpx.ReadTimeout("slow", request=request)

        client = OperatorClient(tmp_path, transport=httpx.MockTransport(handler))
        with pytest.raises(ToolError, match="Timed out"):
            await client.request("POST", "/agents/1/stop")
        assert len(calls) == 1


class TestResponseHandling:
    @pytest.mark.parametrize(
        ("status", "body", "expected"),
        [
            pytest.param(
                409, {"detail": "Agent already running for issue #5"}, "already running for issue #5", id="text"
            ),
            pytest.param(
                409, {"detail": {"code": "budget_exceeded", "message": "Over budget"}}, "Over budget", id="structured"
            ),
            pytest.param(
                422,
                {"detail": [{"loc": ["body", "issue"], "msg": "must be numeric"}]},
                "body.issue: must be numeric",
                id="422",
            ),
            pytest.param(
                500, b"<html>Traceback (most recent call last)</html>", r"HTTP 500\)?: HTTP 500", id="no detail"
            ),
            pytest.param(409, {"detail": 42}, r"HTTP 409\)?: HTTP 409", id="non-string detail"),
            pytest.param(409, {"detail": ""}, r"HTTP 409\)?: HTTP 409", id="empty detail"),
        ],
    )
    async def test_refusal_message_comes_from_detail_only(
        self, project: Path, status: int, body: Any, expected: str
    ) -> None:
        fake = FakeServer({("POST", "/api/agents/start"): (status, body)})
        client = OperatorClient(project, transport=fake.transport)
        with pytest.raises(ToolError, match=expected) as exc_info:
            await client.request("POST", "/agents/start", body={})
        assert "Traceback" not in str(exc_info.value)

    async def test_non_json_success_is_an_error(self, project: Path) -> None:
        fake = FakeServer({("GET", "/api/agents/active"): (200, b"<html>login</html>")})
        client = OperatorClient(project, transport=fake.transport)
        with pytest.raises(ToolError, match="non-JSON"):
            await client.request("GET", "/agents/active")

    async def test_deeply_nested_error_body_falls_back_to_the_bare_status(self, project: Path) -> None:
        fake = FakeServer({("POST", "/api/agents/start"): (409, b"[" * 100_000)})
        client = OperatorClient(project, transport=fake.transport)
        with pytest.raises(ToolError, match=r"HTTP 409\)?: HTTP 409"):
            await client.request("POST", "/agents/start", body={})

    @pytest.mark.parametrize("length", ["abc", "5, 5", "-"])
    async def test_unparseable_content_length_is_ignored_and_the_stream_cap_applies(
        self, project: Path, length: str
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == IDENTITY_PATH:
                return httpx.Response(200, json={"project_dir": str(project)})
            if request.url.path == "/api/agents/active":
                return httpx.Response(200, content=b'{"agents": []}', headers={"content-length": length})
            return httpx.Response(404, json={"detail": "Not Found"})

        client = OperatorClient(project, transport=httpx.MockTransport(handler))
        assert await client.request("GET", "/agents/active") == {"agents": []}

    async def test_oversized_streamed_response_without_a_length_is_an_error(self, project: Path) -> None:
        async def chunks() -> Any:
            yield b"a" * MAX_RESPONSE_BYTES
            yield b"b"

        transport = httpx.MockTransport(lambda request: httpx.Response(200, content=chunks()))
        client = OperatorClient(project, transport=transport)
        with pytest.raises(ToolError, match="too large"):
            await client.request("GET", "/agents/active", project_scoped=False)

    async def test_config_load_failure_is_reported_without_its_detail(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def broken(_project_dir: Path) -> Any:
            raise RuntimeError("secret-bearing detail")

        monkeypatch.setattr(operator, "load_config", broken)
        client = OperatorClient(project, transport=FakeServer().transport)
        with pytest.raises(ToolError, match="Failed to load SOVA config") as exc_info:
            await client.request("GET", "/agents/active")
        assert "secret-bearing" not in str(exc_info.value)

    async def test_other_transport_error_is_an_error_and_not_retried(
        self, tmp_path: Path, seed_config: Callable[..., None]
    ) -> None:
        seed_config(tmp_path, server={"port": 9001}, dashboard={"port": 9002})
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            raise httpx.RemoteProtocolError("dropped", request=request)

        client = OperatorClient(tmp_path, transport=httpx.MockTransport(handler))
        with pytest.raises(ToolError, match="failed"):
            await client.request("POST", "/agents/1/stop", project_scoped=False)
        assert len(calls) == 1

    async def test_oversized_response_is_an_error(self, project: Path) -> None:
        fake = FakeServer({("GET", "/api/agents/active"): (200, b'{"x": "' + b"a" * MAX_RESPONSE_BYTES + b'"}')})
        client = OperatorClient(project, transport=fake.transport)
        with pytest.raises(ToolError, match="too large"):
            await client.request("GET", "/agents/active")


class TestMultiProjectPrefix:
    async def test_registered_project_uses_its_slug_prefix(self, project: Path) -> None:
        fake = FakeServer(
            {
                ("GET", "/api/projects"): (200, {"projects": {"other": "/elsewhere", "mine": str(project)}}),
                ("GET", "/p/mine/api/agents/active"): (200, {"agents": []}),
            }
        )
        assert await OperatorClient(project, transport=fake.transport).request("GET", "/agents/active") == {
            "agents": []
        }

    async def test_unregistered_project_is_an_error(self, project: Path) -> None:
        fake = FakeServer({("GET", "/api/projects"): (200, {"projects": {"other": "/elsewhere"}})})
        client = OperatorClient(project, transport=fake.transport)
        with pytest.raises(ToolError, match="not registered"):
            await client.request("POST", "/agents/1/stop")
        assert fake.sent("POST", "/api/agents/1/stop") == []

    async def test_project_list_refusal_other_than_404_is_an_error(self, project: Path) -> None:
        fake = FakeServer({("GET", "/api/projects"): (500, {"detail": "projects broke"})})
        client = OperatorClient(project, transport=fake.transport)
        with pytest.raises(ToolError, match="projects broke"):
            await client.request("POST", "/agents/1/stop")
        assert fake.sent("POST", "/api/agents/1/stop") == []

    @pytest.mark.parametrize("body", [{"projects": ["mine"]}, ["mine"]], ids=["projects not a dict", "body a list"])
    async def test_malformed_project_list_is_an_error(self, project: Path, body: Any) -> None:
        fake = FakeServer({("GET", "/api/projects"): (200, body)})
        client = OperatorClient(project, transport=fake.transport)
        with pytest.raises(ToolError, match="unexpected project list"):
            await client.request("POST", "/agents/1/stop")

    async def test_global_routes_keep_the_bare_path_on_a_verified_multi_project_server(self, project: Path) -> None:
        fake = FakeServer(
            {
                ("GET", "/api/projects"): (200, {"projects": {"mine": str(project)}}),
                ("GET", "/api/health"): (200, {"status": "ok"}),
            }
        )
        await OperatorClient(project, transport=fake.transport).request("GET", "/health", project_scoped=False)
        assert [r.url.path for r in fake.requests] == ["/api/projects", "/api/health"]


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


class TestToolRegistration:
    async def test_eight_tools_and_only_the_two_reads_are_read_only(self, project: Path) -> None:
        tools = {t.name: t for t in await _tool_server(project).list_tools()}

        assert set(tools) == {
            "sova_status",
            "sova_pending",
            "sova_start_run",
            "sova_stop_run",
            "sova_resume_run",
            "sova_handoff_dismiss",
            "sova_handoff_action",
            "sova_plan_decision",
        }
        read_only = {name for name, t in tools.items() if t.annotations and t.annotations.read_only_hint}
        assert read_only == {"sova_status", "sova_pending"}

    async def test_start_run_does_not_expose_overrides(self, project: Path) -> None:
        tools = {t.name: t for t in await _tool_server(project).list_tools()}
        assert set(tools["sova_start_run"].input_schema["properties"]) == {"issue", "role", "project_dir"}

    def test_operator_module_never_imports_the_in_process_runners(self) -> None:
        tree = ast.parse(Path(operator.__file__).read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
                imported.update(alias.name for alias in node.names)
        assert not [name for name in imported if "control_service" in name or "invoke_command" in name]


class TestStatusTool:
    HEALTH = {"status": "ok", "restart_required": False}

    async def test_returns_health_and_agents(self, project: Path) -> None:
        fake = FakeServer(
            {
                ("GET", "/api/health"): (200, self.HEALTH),
                ("GET", "/api/agents/active"): (200, {"agents": [{"run_id": 3}]}),
            }
        )
        result = await _tool_server(project, fake).call_tool("sova_status", {})
        assert result.structured_content == {"health": self.HEALTH, "agents": {"agents": [{"run_id": 3}]}}

    async def test_degraded_health_passes_through_as_data(self, project: Path) -> None:
        degraded = {"status": "degraded", "restart_required": True}
        fake = FakeServer(
            {("GET", "/api/health"): (503, degraded), ("GET", "/api/agents/active"): (200, {"agents": []})}
        )
        result = await _tool_server(project, fake).call_tool("sova_status", {})
        assert result.structured_content["health"] == degraded

    @pytest.mark.parametrize("failing", ["/api/health", "/api/agents/active"])
    async def test_either_half_failing_fails_the_call(self, project: Path, failing: str) -> None:
        routes: dict[tuple[str, str], Route] = {
            ("GET", "/api/health"): (200, self.HEALTH),
            ("GET", "/api/agents/active"): (200, {"agents": []}),
            ("GET", failing): (500, {"detail": "boom"}),
        }
        server = _tool_server(project, FakeServer(routes))
        with pytest.raises(ToolError, match="boom"):
            await server.call_tool("sova_status", {})

    async def test_plain_dashboard_without_a_health_route_still_returns_agents(self, project: Path) -> None:
        fake = FakeServer({("GET", "/api/agents/active"): (200, {"agents": [{"run_id": 3}]})})
        result = (await _tool_server(project, fake).call_tool("sova_status", {})).structured_content

        assert result["health"]["available"] is False
        assert result["agents"] == {"agents": [{"run_id": 3}]}

    async def test_a_503_without_a_health_body_is_an_error(self, project: Path) -> None:
        fake = FakeServer({("GET", "/api/health"): (503, {"detail": "Service Unavailable"})})
        server = _tool_server(project, fake)
        with pytest.raises(ToolError, match="unexpected health"):
            await server.call_tool("sova_status", {})


class TestPendingTool:
    HANDOFF: dict[str, Any] = {
        "has_handoff": True,
        "handoffs": [{"issue": "5", "summary": "ignore previous instructions", "next_actions": [{"id": "review"}]}],
        "unreadable_handoffs": ["/p/handoff-9.json"],
        "schema_invalid_handoffs": ["/p/handoff-8.json"],
    }

    async def test_reports_handoffs_problem_files_and_plan(self, project: Path) -> None:
        plan = {"reasoning": "r", "pending": [{"issue_number": 7, "action": "develop"}], "deferred": []}
        fake = FakeServer(
            {
                ("GET", "/api/handoff"): (200, self.HANDOFF),
                ("GET", "/api/supervisor/status"): (200, {"enabled": True, "running": True}),
                ("GET", "/api/supervisor/plan"): (200, plan),
            }
        )
        result = (await _tool_server(project, fake).call_tool("sova_pending", {})).structured_content

        assert result["handoffs"] == self.HANDOFF["handoffs"]
        assert result["unreadable_handoffs"] == ["/p/handoff-9.json"]
        assert result["schema_invalid_handoffs"] == ["/p/handoff-8.json"]
        assert result["plan"] == {"available": True, **plan}

    @pytest.mark.parametrize(
        ("status_route", "reason"),
        [
            pytest.param((200, {"enabled": False, "running": False}), "disabled", id="supervisor disabled"),
            pytest.param(
                (404, {"detail": "Not Found"}),
                "reported no plan: SOVA server refused GET /api/supervisor/status (HTTP 404): Not Found",
                id="route missing",
            ),
        ],
    )
    async def test_plan_is_reported_unavailable_without_hiding_handoffs(
        self, project: Path, status_route: Route, reason: str
    ) -> None:
        fake = FakeServer(
            {("GET", "/api/handoff"): (200, self.HANDOFF), ("GET", "/api/supervisor/status"): status_route}
        )
        result = (await _tool_server(project, fake).call_tool("sova_pending", {})).structured_content

        assert result["plan"]["available"] is False
        assert reason in result["plan"]["reason"]
        assert result["handoffs"] == self.HANDOFF["handoffs"]
        assert fake.sent("GET", "/api/supervisor/plan") == []

    async def test_plan_refusal_other_than_404_fails_the_call(self, project: Path) -> None:
        fake = FakeServer(
            {
                ("GET", "/api/handoff"): (200, self.HANDOFF),
                ("GET", "/api/supervisor/status"): (500, {"detail": "supervisor broke"}),
            }
        )
        server = _tool_server(project, fake)
        with pytest.raises(ToolError, match="supervisor broke"):
            await server.call_tool("sova_pending", {})

    @pytest.mark.parametrize("missing", ["handoffs", "unreadable_handoffs", "schema_invalid_handoffs"])
    async def test_a_response_missing_a_field_is_not_read_as_empty(self, project: Path, missing: str) -> None:
        body = {k: v for k, v in self.HANDOFF.items() if k != missing}
        fake = FakeServer({("GET", "/api/handoff"): (200, body)})
        server = _tool_server(project, fake)
        with pytest.raises(ToolError, match="unexpected handoff"):
            await server.call_tool("sova_pending", {})


class TestMutatingTools:
    @pytest.mark.parametrize(
        "answer",
        [
            pytest.param({"status": "started", "pid": 1}, id="no run id"),
            pytest.param({"status": "started", "run_id": None}, id="null run id"),
            pytest.param({"status": "started", "run_id": "12"}, id="run id not an int"),
        ],
    )
    async def test_a_start_without_a_run_id_is_an_error(self, project: Path, answer: dict[str, Any]) -> None:
        fake = FakeServer({("POST", "/api/agents/start"): (200, answer)})
        with pytest.raises(ToolError, match="returned no run id"):
            await _tool_server(project, fake).call_tool("sova_start_run", {"issue": 42})

    @pytest.mark.parametrize(
        ("tool", "args", "method", "path", "body"),
        [
            pytest.param("sova_start_run", {"issue": 42}, "POST", "/api/agents/start", {"issue": "42"}, id="start"),
            pytest.param(
                "sova_start_run",
                {"issue": 42, "role": " researcher "},
                "POST",
                "/api/agents/start",
                {"issue": "42", "role": "researcher"},
                id="start with role",
            ),
            pytest.param("sova_stop_run", {"run_id": 7}, "POST", "/api/agents/7/stop", None, id="stop"),
            pytest.param(
                "sova_resume_run", {"run_id": 7}, "POST", "/api/agents/7/resume-from-approval", None, id="resume"
            ),
            pytest.param(
                "sova_handoff_dismiss", {"issue": "5"}, "POST", "/api/handoff/clear", {"issue": "5"}, id="dismiss"
            ),
            pytest.param(
                "sova_handoff_action",
                {"issue": "5", "action_id": "review"},
                "POST",
                "/api/handoff/execute",
                {"issue": "5", "action_id": "review"},
                id="action",
            ),
            pytest.param(
                "sova_plan_decision",
                {"decision": "approve"},
                "POST",
                "/api/supervisor/plan/approve",
                {"issue_numbers": None},
                id="approve all",
            ),
            pytest.param(
                "sova_plan_decision",
                {"decision": "approve", "issue": 9},
                "POST",
                "/api/supervisor/plan/approve",
                {"issue_numbers": [9]},
                id="approve one",
            ),
            pytest.param(
                "sova_plan_decision",
                {"decision": "skip", "issue": 9},
                "POST",
                "/api/supervisor/plan/skip/9",
                None,
                id="skip",
            ),
        ],
    )
    async def test_sends_one_mutating_request(
        self, project: Path, tool: str, args: dict[str, Any], method: str, path: str, body: dict | None
    ) -> None:
        answer = {"status": "stopped", "cleared": True, "approved": 1, "run_id": 7}
        fake = FakeServer({(method, path): (200, answer)})
        result = await _tool_server(project, fake).call_tool(tool, args)

        assert result.structured_content == answer
        mutating = [r for r in fake.requests if r.method != "GET"]
        assert [(r.method, r.url.path) for r in mutating] == [(method, path)]
        assert (json.loads(mutating[0].content) if mutating[0].content else None) == body

    @pytest.mark.parametrize(
        ("tool", "args", "path", "detail"),
        [
            pytest.param("sova_start_run", {"issue": 5}, "/api/agents/start", "Agent already running", id="duplicate"),
            pytest.param(
                "sova_resume_run",
                {"run_id": 4},
                "/api/agents/4/resume-from-approval",
                "not in awaiting_approval",
                id="resume",
            ),
            pytest.param(
                "sova_handoff_action",
                {"issue": "5", "action_id": "nope"},
                "/api/handoff/execute",
                "Action not found",
                id="unknown action",
            ),
        ],
    )
    async def test_server_refusal_reaches_the_model(
        self, project: Path, tool: str, args: dict[str, Any], path: str, detail: str
    ) -> None:
        fake = FakeServer({("POST", path): (409, {"detail": detail})})
        server = _tool_server(project, fake)
        with pytest.raises(ToolError, match=detail):
            await server.call_tool(tool, args)
        assert len(fake.sent("POST", path)) == 1

    @pytest.mark.parametrize(
        ("tool", "args", "path", "answer", "match"),
        [
            pytest.param(
                "sova_stop_run",
                {"run_id": 99},
                "/api/agents/99/stop",
                {"status": "not_found", "message": "No agent with run_id 99"},
                "No agent with run_id 99",
                id="stop unknown run",
            ),
            pytest.param(
                "sova_handoff_dismiss",
                {"issue": "5"},
                "/api/handoff/clear",
                {"cleared": False},
                "No handoff file for issue '5'",
                id="dismiss without a handoff",
            ),
            pytest.param(
                "sova_plan_decision",
                {"decision": "approve", "issue": 9},
                "/api/supervisor/plan/approve",
                {"approved": 0, "results": []},
                "not in the pending plan",
                id="approve an issue not in the plan",
            ),
            pytest.param(
                "sova_plan_decision",
                {"decision": "approve"},
                "/api/supervisor/plan/approve",
                {"approved": 0, "results": []},
                "no pending plan",
                id="approve all of an empty plan",
            ),
        ],
    )
    async def test_a_200_that_did_nothing_is_not_reported_as_success(
        self, project: Path, tool: str, args: dict[str, Any], path: str, answer: dict[str, Any], match: str
    ) -> None:
        fake = FakeServer({("POST", path): (200, answer)})
        server = _tool_server(project, fake)
        with pytest.raises(ToolError, match=match):
            await server.call_tool(tool, args)

    @pytest.mark.parametrize(
        ("tool", "args", "match"),
        [
            pytest.param("sova_start_run", {"issue": 0}, "Must be positive", id="start issue"),
            pytest.param("sova_stop_run", {"run_id": -1}, "Must be positive", id="stop run"),
            pytest.param("sova_resume_run", {"run_id": 0}, "Must be positive", id="resume run"),
            pytest.param("sova_handoff_dismiss", {"issue": " "}, "issue is required", id="dismiss issue"),
            pytest.param("sova_handoff_action", {"issue": "5", "action_id": ""}, "action_id is required", id="action"),
            pytest.param("sova_plan_decision", {"decision": "skip"}, "skip requires", id="skip without issue"),
            pytest.param("sova_plan_decision", {"decision": "maybe", "issue": 1}, "Invalid decision", id="decision"),
            pytest.param("sova_plan_decision", {"decision": "approve", "issue": -2}, "Invalid issue", id="negative"),
        ],
    )
    async def test_invalid_input_raises_before_any_request(
        self, project: Path, tool: str, args: dict[str, Any], match: str
    ) -> None:
        fake = FakeServer()
        server = _tool_server(project, fake)
        with pytest.raises(ToolError, match=match):
            await server.call_tool(tool, args)
        assert fake.requests == []


class TestProjectDirValidation:
    async def test_unbound_server_validates_the_path_before_loading_config(self, tmp_path: Path) -> None:
        server = MCPServer(name="t")
        register_operator_tools(server, transport=FakeServer().transport)
        with pytest.raises(ToolError, match="Project directory not found"):
            await server.call_tool("sova_status", {"project_dir": str(tmp_path / "missing")})

    async def test_bound_server_rejects_a_path_outside_its_root(self, tmp_path: Path) -> None:
        bound = tmp_path / "bound"
        bound.mkdir()
        server = MCPServer(name="t")
        register_operator_tools(server, project_dir=bound, transport=FakeServer().transport)
        with pytest.raises(ToolError, match="outside the allowed root"):
            await server.call_tool("sova_stop_run", {"run_id": 1, "project_dir": str(tmp_path)})


# ---------------------------------------------------------------------------
# Against the real dashboard app
# ---------------------------------------------------------------------------


@pytest.fixture
def served_app(dashboard_app: Any, project: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """The shared dashboard app, serving ``project`` as its single project."""
    from sova.dashboard.services import agent_pool, control_service, handoff_service

    # Record the module-level project dirs first, so monkeypatch puts them back after the test.
    monkeypatch.setattr(agent_pool, "_default_project_dir", agent_pool._default_project_dir)
    monkeypatch.setattr(handoff_service, "_default_project_dir", handoff_service._default_project_dir)
    pool = agent_pool._get_project_agents(agent_pool._DEFAULT_SLUG)
    monkeypatch.setattr(pool, "project_dir", pool.project_dir)
    monkeypatch.chdir(project)
    control_service.set_project_dir(project.resolve())
    handoff_service.set_project_dir(project.resolve())
    return dashboard_app


def _real_client(app: Any, project: Path) -> OperatorClient:
    return OperatorClient(project, transport=httpx.ASGITransport(app=app))


def _real_tools(app: Any, project: Path) -> MCPServer:
    server = MCPServer(name="t")
    register_operator_tools(server, project_dir=project, transport=httpx.ASGITransport(app=app))
    return server


class TestAgainstDashboardApp:
    """The tools' CSRF pair, Origin and response-shape checks must satisfy the app's real routes."""

    async def test_reads_and_a_dismiss_with_nothing_to_clear(self, served_app: Any, project: Path) -> None:
        server = _real_tools(served_app, project)

        # Past the CSRF guard (a 403 would name it), the route reports there was nothing to clear.
        with pytest.raises(ToolError, match="No handoff file"):
            await server.call_tool("sova_handoff_dismiss", {"issue": "123"})

        pending = (await server.call_tool("sova_pending", {})).structured_content
        assert pending["handoffs"] == []
        assert pending["plan"]["available"] is False

    async def test_the_real_app_reports_the_project_it_serves(self, served_app: Any, project: Path) -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=served_app), base_url="http://127.0.0.1:8111"
        ) as http:
            body = (await http.get(IDENTITY_PATH)).json()
        assert Path(body["project_dir"]) == project.resolve()

    async def test_start_returns_the_run_id_the_real_route_answers_with(
        self, served_app: Any, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from sova.dashboard.services import control_service

        async def started(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {"status": "started", "pid": 4242, "run_id": 321}

        monkeypatch.setattr(control_service, "start_agent", started)
        result = await _real_tools(served_app, project).call_tool("sova_start_run", {"issue": 42})
        assert result.structured_content["run_id"] == 321

    async def test_dismiss_clears_a_real_handoff_file(self, served_app: Any, project: Path) -> None:
        control_dir = project / ".claude" / "agent-control"
        control_dir.mkdir(parents=True)
        (control_dir / "handoff-123.json").write_text(json.dumps({"issue": "123"}), encoding="utf-8")

        result = await _real_tools(served_app, project).call_tool("sova_handoff_dismiss", {"issue": "123"})

        assert result.structured_content == {"cleared": True}
        assert not (control_dir / "handoff-123.json").exists()

    async def test_an_unknown_action_on_an_existing_handoff_is_the_servers_404(
        self, served_app: Any, project: Path
    ) -> None:
        control_dir = project / ".claude" / "agent-control"
        control_dir.mkdir(parents=True)
        handoff = {"issue": "123", "next_actions": [{"id": "review", "label": "Review", "type": "command"}]}
        (control_dir / "handoff-123.json").write_text(json.dumps(handoff), encoding="utf-8")

        with pytest.raises(ToolError, match=r"HTTP 404.*'nope' not found"):
            await _real_tools(served_app, project).call_tool(
                "sova_handoff_action", {"issue": "123", "action_id": "nope"}
            )

        assert (control_dir / "handoff-123.json").exists()

    async def test_stop_of_a_run_the_server_does_not_hold_is_an_error(self, served_app: Any, project: Path) -> None:
        with pytest.raises(ToolError, match="No agent with run_id 987654"):
            await _real_tools(served_app, project).call_tool("sova_stop_run", {"run_id": 987654})

    async def test_stop_of_a_held_run_reports_stopped(self, served_app: Any, project: Path) -> None:
        from types import SimpleNamespace

        from sova.dashboard.services.agent_pool import _get_project_agents

        async def stop(**_kwargs: Any) -> None:
            return None

        pa = _get_project_agents(None)
        held = SimpleNamespace(
            process=SimpleNamespace(pid=4242, stop=stop),
            reader_task=None,
            stderr_task=None,
            resource_flush_task=None,
            run_id=555,
        )
        pa.agents[555] = held
        try:
            result = await _real_tools(served_app, project).call_tool("sova_stop_run", {"run_id": 555})
        finally:
            pa.agents.pop(555, None)

        assert result.structured_content == {"status": "stopped", "pid": 4242, "run_id": 555}

    async def test_plan_approve_and_skip_against_a_real_plan(
        self, served_app: Any, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from sova.config.loader import load_config
        from sova.dashboard.routers import supervisor as supervisor_router
        from sova.dashboard.services.supervisor_service import resolve_project_slug, set_pending_plan
        from sova.supervisor.progression import ProgressionAction, ProgressionDecision

        async def no_execution(decisions: list[Any], _project_dir: Path) -> list[dict]:
            return [{"issue": d.issue_number} for d in decisions]

        monkeypatch.setattr(supervisor_router, "_execute_plan_decisions", no_execution)
        slug = resolve_project_slug(load_config(project).github_repo, None)  # the route passes no dir
        decisions = [ProgressionDecision(issue_number=n, action=ProgressionAction.SPAWN_DEVELOPER) for n in (7, 8, 9)]
        set_pending_plan(decisions, project_slug=slug)
        try:
            server = _real_tools(served_app, project)
            approved = (
                await server.call_tool("sova_plan_decision", {"decision": "approve", "issue": 7})
            ).structured_content
            assert approved["approved"] == 1

            with pytest.raises(ToolError, match="not in the pending plan"):
                await server.call_tool("sova_plan_decision", {"decision": "approve", "issue": 7})

            skipped = (
                await server.call_tool("sova_plan_decision", {"decision": "skip", "issue": 8})
            ).structured_content
            assert skipped == {"skipped": 8}
            with pytest.raises(ToolError, match="not in the pending plan"):
                await server.call_tool("sova_plan_decision", {"decision": "skip", "issue": 8})
        finally:
            set_pending_plan([], project_slug=slug)

    @pytest.mark.parametrize(
        ("path", "body"),
        [
            pytest.param("/agents/start", {"issue": "not-a-number"}, id="start"),
            pytest.param("/agents/987654/stop", None, id="stop"),
            pytest.param("/agents/987654/resume-from-approval", None, id="resume"),
            pytest.param("/handoff/execute", {"issue": "987654", "action_id": "nope"}, id="handoff execute"),
            pytest.param("/handoff/clear", {"issue": "987654"}, id="handoff clear"),
            pytest.param("/supervisor/plan/approve", {"issue_numbers": [987654]}, id="plan approve"),
            pytest.param("/supervisor/plan/skip/987654", None, id="plan skip"),
        ],
    )
    async def test_every_mutating_route_is_csrf_guarded_and_accepts_the_client_pair(
        self, served_app: Any, project: Path, path: str, body: dict[str, Any] | None
    ) -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=served_app), base_url="http://127.0.0.1:8111"
        ) as http:
            bare = await http.post(f"/api{path}", json=body)
        assert bare.status_code == 403

        try:
            await _real_client(served_app, project).request("POST", path, body=body)
        except ServerRefusal as e:
            assert e.status != 403, f"the client's Origin and CSRF pair was rejected by {path}"


# ---------------------------------------------------------------------------
# Project identity, timeouts and response shape
# ---------------------------------------------------------------------------


class TestSingleProjectIdentity:
    async def test_a_wrong_project_on_the_first_port_falls_back_to_the_second(
        self, tmp_path: Path, seed_config: Callable[..., None], tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        seed_config(tmp_path, server={"port": 9001}, dashboard={"port": 9002})
        other = tmp_path_factory.mktemp("other-project")
        sent: list[tuple[int, str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            sent.append((request.url.port or 0, request.method, request.url.path))
            if request.url.path == IDENTITY_PATH:
                served = other if request.url.port == 9001 else tmp_path
                return httpx.Response(200, json={"project_dir": str(served)})
            if request.url.path == "/api/agents/7/stop":
                return httpx.Response(200, json={"status": "stopped"})
            return httpx.Response(404, json={"detail": "Not Found"})

        client = OperatorClient(tmp_path, transport=httpx.MockTransport(handler))
        result = await client.request("POST", "/agents/7/stop")

        assert result == {"status": "stopped"}
        assert [(port, method) for port, method, _ in sent if method == "POST"] == [(9002, "POST")]

    async def test_every_endpoint_serving_another_project_is_named(
        self, tmp_path: Path, seed_config: Callable[..., None], tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        seed_config(tmp_path, server={"port": 9001}, dashboard={"port": 9002})
        other = tmp_path_factory.mktemp("other-project")
        sent: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            sent.append(request.method)
            if request.url.path == IDENTITY_PATH:
                return httpx.Response(200, json={"project_dir": str(other)})
            return httpx.Response(404, json={"detail": "Not Found"})

        client = OperatorClient(tmp_path, transport=httpx.MockTransport(handler))
        with pytest.raises(ToolError, match=r"127\.0\.0\.1:9001 .*other-project.*127\.0\.0\.1:9002 .*other-project"):
            await client.request("POST", "/agents/7/stop")
        assert "POST" not in sent

    async def test_server_for_another_project_is_refused_before_any_mutating_request(
        self, project: Path, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        other = tmp_path_factory.mktemp("other-project")
        fake = FakeServer({("GET", IDENTITY_PATH): (200, {"project_dir": str(other)})})
        server = _tool_server(project, fake)
        with pytest.raises(ToolError, match=r"serves .*other-project.*refusing to act"):
            await server.call_tool("sova_stop_run", {"run_id": 7})
        assert [r for r in fake.requests if r.method != "GET"] == []

    @pytest.mark.parametrize(
        "identity",
        [
            pytest.param((404, {"detail": "Not Found"}), id="no identity route"),
            pytest.param((200, {}), id="no project_dir"),
            pytest.param((200, {"project_dir": ""}), id="empty project_dir"),
            pytest.param((200, ["x"]), id="not an object"),
            pytest.param((500, {"detail": "boom"}), id="error"),
        ],
    )
    async def test_an_unreadable_identity_is_refused_and_names_the_endpoint(
        self, project: Path, identity: Route
    ) -> None:
        fake = FakeServer({("GET", IDENTITY_PATH): identity})
        server = _tool_server(project, fake)
        with pytest.raises(ToolError, match=IDENTITY_PATH):
            await server.call_tool("sova_start_run", {"issue": 42})
        assert [r for r in fake.requests if r.method != "GET"] == []

    @pytest.mark.parametrize(
        "probe",
        [
            pytest.param((403, {"detail": "Forbidden"}), id="403"),
            pytest.param((500, {"detail": "boom"}), id="500"),
            pytest.param((200, b"<html>app</html>"), id="html 200"),
        ],
    )
    async def test_an_unrelated_app_on_the_first_port_falls_back_to_the_second(
        self, tmp_path: Path, seed_config: Callable[..., None], probe: Route
    ) -> None:
        seed_config(tmp_path, server={"port": 9001}, dashboard={"port": 9002})
        sent: list[tuple[int, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            sent.append((request.url.port or 0, request.method))
            if request.url.port == 9001:
                status, body = probe
                if isinstance(body, bytes):
                    return httpx.Response(status, content=body)
                return httpx.Response(status, json=body)
            if request.url.path == IDENTITY_PATH:
                return httpx.Response(200, json={"project_dir": str(tmp_path)})
            if request.url.path == "/api/agents/7/stop":
                return httpx.Response(200, json={"status": "stopped"})
            return httpx.Response(404, json={"detail": "Not Found"})

        result = await OperatorClient(tmp_path, transport=httpx.MockTransport(handler)).request(
            "POST", "/agents/7/stop"
        )

        assert result == {"status": "stopped"}
        assert [port for port, method in sent if method == "POST"] == [9002]

    async def test_an_unrelated_app_everywhere_is_named_in_the_error(
        self, tmp_path: Path, seed_config: Callable[..., None]
    ) -> None:
        seed_config(tmp_path, server={"port": 9001}, dashboard={"port": 9002})
        handler = lambda _request: httpx.Response(200, content=b"<html></html>")  # noqa: E731
        client = OperatorClient(tmp_path, transport=httpx.MockTransport(handler))
        with pytest.raises(ToolError, match=r"127\.0\.0\.1:9001 .*not a SOVA dashboard.*127\.0\.0\.1:9002 "):
            await client.request("POST", "/agents/7/stop")

    async def test_non_json_identity_is_refused_and_falls_through(self, project: Path) -> None:
        fake = FakeServer({("GET", IDENTITY_PATH): (200, b"<html></html>")})
        with pytest.raises(ToolError, match=IDENTITY_PATH):
            await _tool_server(project, fake).call_tool("sova_start_run", {"issue": 42})
        assert [r for r in fake.requests if r.method != "GET"] == []

    async def test_a_server_without_the_identity_route_is_told_to_restart(self, project: Path) -> None:
        fake = FakeServer({("GET", IDENTITY_PATH): (404, {"detail": "Not Found"})})
        with pytest.raises(ToolError, match="restart 'sova server'"):
            await _tool_server(project, fake).call_tool("sova_stop_run", {"run_id": 7})

    async def test_a_rejected_endpoint_is_not_probed_again_by_the_same_client(
        self, project: Path, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        other = tmp_path_factory.mktemp("other-project")
        fake = FakeServer({("GET", IDENTITY_PATH): (200, {"project_dir": str(other)})})
        client = OperatorClient(project, transport=fake.transport)
        for _ in range(2):
            with pytest.raises(ToolError, match="refusing to act"):
                await client.request("GET", "/agents/active")
        assert len(fake.sent("GET", IDENTITY_PATH)) == 1
        assert len(fake.sent("GET", "/api/projects")) == 1

    async def test_the_verified_prefix_is_cached_for_the_client(self, project: Path) -> None:
        fake = FakeServer({("POST", "/api/agents/1/stop"): (200, {"status": "stopped"})})
        client = OperatorClient(project, transport=fake.transport)
        await client.request("POST", "/agents/1/stop")
        await client.request("POST", "/agents/1/stop")
        assert len(fake.sent("GET", IDENTITY_PATH)) == 1
        assert len(fake.sent("GET", "/api/projects")) == 1

    async def test_a_symlinked_project_path_still_matches(self, project: Path, tmp_path_factory: Any) -> None:
        link = tmp_path_factory.mktemp("links") / "link"
        link.symlink_to(project)
        fake = FakeServer({("GET", IDENTITY_PATH): (200, {"project_dir": str(link)})})
        fake.routes[("GET", "/api/agents/active")] = (200, {"agents": []})
        await OperatorClient(project, transport=fake.transport).request("GET", "/agents/active")
        assert fake.sent("GET", "/api/agents/active")

    async def test_global_routes_verify_the_identity_too(self, project: Path) -> None:
        fake = FakeServer({("GET", "/api/health"): (200, {"status": "ok"})})
        await OperatorClient(project, transport=fake.transport).request("GET", "/health", project_scoped=False)
        assert len(fake.sent("GET", IDENTITY_PATH)) == 1

    async def test_status_never_mixes_health_and_agents_from_two_servers(
        self, tmp_path: Path, seed_config: Callable[..., None], tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        # Another project's server holds server.port; this project's dashboard holds dashboard.port.
        seed_config(tmp_path, server={"port": 9001}, dashboard={"port": 9002})
        other = tmp_path_factory.mktemp("other-project")
        health_ports: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            port = request.url.port or 0
            if request.url.path == IDENTITY_PATH:
                return httpx.Response(200, json={"project_dir": str(other if port == 9001 else tmp_path)})
            if request.url.path == "/api/health":
                health_ports.append(port)
                return httpx.Response(200, json={"status": "degraded", "restart_required": port == 9001})
            if request.url.path == "/api/agents/active":
                return httpx.Response(200, json={"agents": []})
            return httpx.Response(404, json={"detail": "Not Found"})

        server = MCPServer(name="t")
        register_operator_tools(server, project_dir=tmp_path, transport=httpx.MockTransport(handler))
        result = (await server.call_tool("sova_status", {})).structured_content

        assert health_ports == [9002]
        assert result["health"] == {"status": "degraded", "restart_required": False}

    async def test_status_fails_when_no_endpoint_serves_the_project(
        self, tmp_path: Path, seed_config: Callable[..., None], tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        seed_config(tmp_path, server={"port": 9001}, dashboard={"port": 9002})
        other = tmp_path_factory.mktemp("other-project")
        fake = FakeServer(
            {
                ("GET", IDENTITY_PATH): (200, {"project_dir": str(other)}),
                ("GET", "/api/health"): (200, {"status": "ok"}),
            }
        )
        server = MCPServer(name="t")
        register_operator_tools(server, project_dir=tmp_path, transport=fake.transport)
        with pytest.raises(ToolError, match="No SOVA server for this project"):
            await server.call_tool("sova_status", {})
        assert fake.sent("GET", "/api/health") == []


class TestMutatingTimeouts:
    @pytest.mark.parametrize(
        "error",
        [
            pytest.param(httpx.ReadTimeout("slow"), id="read timeout"),
            pytest.param(httpx.WriteTimeout("slow"), id="write timeout"),
            pytest.param(httpx.RemoteProtocolError("dropped"), id="dropped connection"),
        ],
    )
    async def test_a_post_that_may_have_been_applied_says_so(self, project: Path, error: httpx.HTTPError) -> None:
        def fail(request: httpx.Request) -> Route:
            raise error.__class__(str(error), request=request)

        fake = FakeServer({("POST", "/api/agents/start"): fail})
        client = OperatorClient(project, transport=fake.transport)
        with pytest.raises(ToolError, match=r"may have taken effect.*before retrying"):
            await client.request("POST", "/agents/start", body={"issue": "1"})

    @pytest.mark.parametrize(
        ("answer", "match"),
        [
            pytest.param(b"<html>login</html>", "non-JSON", id="non-JSON"),
            pytest.param(b"[" * 100_000, "non-JSON", id="deeply nested"),
            pytest.param(b'{"x": "' + b"a" * MAX_RESPONSE_BYTES + b'"}', "too large", id="too large"),
        ],
    )
    async def test_a_malformed_success_to_a_post_says_it_may_have_been_applied(
        self, project: Path, answer: bytes, match: str
    ) -> None:
        fake = FakeServer({("POST", "/api/agents/start"): (200, answer)})
        client = OperatorClient(project, transport=fake.transport)
        with pytest.raises(ToolError, match=rf"{match}.*may have taken effect.*before retrying"):
            await client.request("POST", "/agents/start", body={"issue": "1"})

    async def test_a_malformed_success_to_a_get_does_not_claim_an_action_was_applied(self, project: Path) -> None:
        fake = FakeServer({("GET", "/api/agents/active"): (200, b"<html>login</html>")})
        client = OperatorClient(project, transport=fake.transport)
        with pytest.raises(ToolError, match="non-JSON") as exc_info:
            await client.request("GET", "/agents/active")
        assert "may have" not in str(exc_info.value)

    async def test_a_get_timeout_does_not_claim_an_action_was_applied(self, project: Path) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("slow", request=request)

        client = OperatorClient(project, transport=httpx.MockTransport(handler))
        with pytest.raises(ToolError, match="Timed out") as exc_info:
            await client.request("GET", "/agents/active", project_scoped=False)
        assert "may have" not in str(exc_info.value)

    async def test_a_timeout_before_the_post_goes_out_does_not_claim_an_action_was_applied(self, project: Path) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("slow", request=request)

        client = OperatorClient(project, transport=httpx.MockTransport(handler))
        with pytest.raises(ToolError, match="Timed out") as exc_info:
            await client.request("POST", "/agents/start", body={"issue": "1"})
        assert "may have" not in str(exc_info.value)


class TestReadToolDeadline:
    @pytest.mark.parametrize("tool", ["sova_status", "sova_pending"])
    async def test_a_slow_server_cannot_hold_a_read_tool_past_the_deadline(
        self, project: Path, monkeypatch: pytest.MonkeyPatch, tool: str
    ) -> None:
        monkeypatch.setattr(operator, "READ_TOOL_DEADLINE_S", 0.05)

        async def slow(*_args: Any, **_kwargs: Any) -> Any:
            await asyncio.sleep(5)

        monkeypatch.setattr(OperatorClient, "request", slow)
        server = _tool_server(project, FakeServer())
        with pytest.raises(ToolError, match="did not finish within"):
            await server.call_tool(tool, {})


class TestStatusShape:
    @pytest.mark.parametrize("agents", [b"null", [], {"agents": None}, {"agents": "none"}, {}])
    async def test_a_malformed_agents_response_is_not_read_as_empty(self, project: Path, agents: Any) -> None:
        fake = FakeServer(
            {("GET", "/api/health"): (200, {"status": "ok"}), ("GET", "/api/agents/active"): (200, agents)}
        )
        server = _tool_server(project, fake)
        with pytest.raises(ToolError, match="unexpected active agents|unexpected"):
            await server.call_tool("sova_status", {})


class TestConfigLoading:
    async def test_config_is_loaded_once_per_client_and_off_the_event_loop(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import threading

        real = operator.load_config
        threads: list[int] = []

        def counting(project_dir: Path) -> Any:
            threads.append(threading.get_ident())
            return real(project_dir)

        monkeypatch.setattr(operator, "load_config", counting)
        fake = FakeServer(
            {
                ("GET", "/api/handoff"): (
                    200,
                    {"handoffs": [], "unreadable_handoffs": [], "schema_invalid_handoffs": []},
                ),
                ("GET", "/api/supervisor/status"): (200, {"enabled": False}),
            }
        )
        await _tool_server(project, fake).call_tool("sova_pending", {})

        assert len(threads) == 1
        assert threads[0] != threading.get_ident()

    async def test_validation_error_is_logged_by_field_without_its_input(
        self, project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from pydantic import BaseModel, ValidationError

        class Model(BaseModel):
            token: int

        secret = "super-" + "secret-value"

        def broken(_project_dir: Path) -> Any:
            try:
                Model(token=secret)  # type: ignore[arg-type]
            except ValidationError as e:
                raise e from None

        monkeypatch.setattr(operator, "load_config", broken)
        client = OperatorClient(project, transport=FakeServer().transport)
        with pytest.raises(ToolError, match="Failed to load SOVA config"):
            await client.request("GET", "/health", project_scoped=False)

        captured = capsys.readouterr()
        logged = captured.out + captured.err
        assert "super-secret-value" not in logged
        assert "token" in logged
