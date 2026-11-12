"""Tests for the ntfy notification backend, the per-kind filter and the notify() kind guard."""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from sova.config.models import NotificationConfig
from sova.ipc.notifications import (
    NotificationKind,
    build_item_url,
    notify,
    send_ntfy_notification,
    send_webhook_notification,
)
from sova.llm import keyring_store

SOVA_ROOT = Path(__file__).resolve().parent.parent / "sova"
TOKEN = "tk_secret_value"


def _ntfy_config(**overrides: object) -> NotificationConfig:
    values: dict[str, object] = {"ntfy_url": "https://ntfy.example.com", "ntfy_topic": "sova"}
    values.update(overrides)
    return NotificationConfig(**values)


def _mock_http(response: MagicMock | None = None) -> tuple[MagicMock, MagicMock]:
    """Patch httpx.AsyncClient in the notifications module; returns (client_cls, client)."""
    client = MagicMock()
    client.post = AsyncMock(return_value=response or MagicMock(status_code=200))
    patcher = patch("sova.ipc.notifications.httpx.AsyncClient")
    client_cls = patcher.start()
    client_cls.return_value.__aenter__.return_value = client
    return patcher, client


@pytest.fixture
def http_client():
    patcher, client = _mock_http()
    yield client
    patcher.stop()


@pytest.fixture(autouse=True)
def _no_keyring():
    with patch.object(keyring_store, "get_secret", return_value=None):
        yield


async def _drain() -> None:
    await asyncio.sleep(0)
    await asyncio.sleep(0)


class TestBuildItemUrl:
    @pytest.mark.parametrize(
        ("base", "run_id", "expected"),
        [
            ("https://sova.example.com", 7, "https://sova.example.com/work/7"),
            ("https://sova.example.com/", 7, "https://sova.example.com/work/7"),
            ("https://sova.example.com//", None, "https://sova.example.com/agents"),
            ("https://sova.example.com", 0, "https://sova.example.com/work/0"),
            ("", 7, ""),
            ("   ", None, ""),
        ],
    )
    def test_build_item_url(self, base: str, run_id: int | None, expected: str) -> None:
        assert build_item_url(base, run_id) == expected

    def test_link_paths_are_real_dashboard_pages(self) -> None:
        """A route rename must break this test, not every notification link (a 404 or a redirect hop)."""
        from sova.ipc.notifications import AGENTS_PAGE_PATH, RUN_PAGE_PATH

        source = (SOVA_ROOT / "dashboard" / "app.py").read_text(encoding="utf-8")
        run_route = RUN_PAGE_PATH.format(run_id="{run_id}")
        assert f'@app.get("{run_route}")' in source
        assert f'@app.get("{AGENTS_PAGE_PATH}")' in source
        # The run page must render the run detail template, not redirect (/runs/{id} only redirects).
        run_handler = source.split(f'@app.get("{run_route}")', 1)[1].split("@app.", 1)[0]
        assert "run_detail.html" in run_handler


class TestNtfyDispatch:
    @pytest.mark.parametrize(
        ("kind", "events", "sent"),
        [
            (NotificationKind.NEEDS_HUMAN, ["needs_human", "failed"], True),
            (NotificationKind.FAILED, ["needs_human", "failed"], True),
            (NotificationKind.FINISHED, ["needs_human", "failed"], False),
            (NotificationKind.INFO, ["needs_human", "failed"], False),
            (NotificationKind.FINISHED, ["finished"], True),
            (NotificationKind.NEEDS_HUMAN, [], False),
            ("failed", ["failed"], True),
            ("FAILED", ["failed"], True),
            (" failed ", ["failed"], True),
            (" Needs_Human", ["needs_human"], True),
            ("not-a-kind", ["info"], True),
            ("not-a-kind", ["failed"], False),
        ],
    )
    async def test_kind_filter(self, kind: str, events: list[str], sent: bool) -> None:
        config = _ntfy_config(ntfy_events=events)
        with patch("sova.ipc.notifications.send_ntfy_notification", new_callable=AsyncMock) as mock_ntfy:
            notify(config, "SOVA", "Body", kind=kind)
            await _drain()
        assert mock_ntfy.await_count == (1 if sent else 0)

    @pytest.mark.parametrize(("url", "topic"), [("", "sova"), ("https://ntfy.example.com", ""), ("", "")])
    async def test_skipped_when_url_or_topic_empty(self, url: str, topic: str) -> None:
        config = _ntfy_config(ntfy_url=url, ntfy_topic=topic)
        with patch("sova.ipc.notifications.send_ntfy_notification", new_callable=AsyncMock) as mock_ntfy:
            notify(config, "SOVA", "Body", kind=NotificationKind.FAILED)
            await _drain()
        mock_ntfy.assert_not_awaited()

    async def test_unknown_event_entries_are_ignored_not_raised(self) -> None:
        config = _ntfy_config(ntfy_events=["bogus", "failed"])
        with patch("sova.ipc.notifications.send_ntfy_notification", new_callable=AsyncMock) as mock_ntfy:
            notify(config, "SOVA", "Body", kind=NotificationKind.FAILED)
            await _drain()
        assert mock_ntfy.await_count == 1

    async def test_unknown_event_warns_once_per_process(self) -> None:
        from sova.ipc import notifications

        notifications._warned_unknown_events.clear()
        config = _ntfy_config(ntfy_events=["bogus", "failed"])
        with (
            patch("sova.ipc.notifications.send_ntfy_notification", new_callable=AsyncMock),
            patch("sova.ipc.notifications.log") as mock_log,
        ):
            for _ in range(3):
                notify(config, "SOVA", "Body", kind=NotificationKind.FAILED)
            await _drain()

        unknown_warnings = [c for c in mock_log.warning.call_args_list if c.args[0] == "notify.ntfy_unknown_events"]
        assert len(unknown_warnings) == 1
        assert unknown_warnings[0].kwargs["events"] == ["bogus"]

    async def test_other_channels_receive_every_kind(self) -> None:
        config = _ntfy_config(ntfy_events=["failed"], webhook_url="https://hooks.example.com/sova")
        with (
            patch("sova.ipc.notifications.send_webhook_notification", new_callable=AsyncMock) as mock_webhook,
            patch("sova.ipc.notifications.send_ntfy_notification", new_callable=AsyncMock) as mock_ntfy,
        ):
            notify(config, "SOVA", "Body", kind=NotificationKind.INFO)
            await _drain()
        assert mock_webhook.await_count == 1
        mock_ntfy.assert_not_awaited()

    async def test_each_call_posts_once_without_dedup(self) -> None:
        config = _ntfy_config()
        with patch("sova.ipc.notifications.send_ntfy_notification", new_callable=AsyncMock) as mock_ntfy:
            for _ in range(2):
                notify(config, "SOVA", "Body", kind=NotificationKind.NEEDS_HUMAN, issue="9", run_id=3)
            await _drain()
        assert mock_ntfy.await_count == 2

    async def test_click_url_passed_to_ntfy(self) -> None:
        config = _ntfy_config(link_base_url="https://sova.example.com/")
        with patch("sova.ipc.notifications.send_ntfy_notification", new_callable=AsyncMock) as mock_ntfy:
            notify(config, "SOVA", "Body", kind=NotificationKind.FAILED, run_id=12)
            await _drain()
        assert mock_ntfy.call_args.kwargs["click_url"] == "https://sova.example.com/work/12"


class TestSendNtfy:
    async def test_posts_json_to_server_root(self, http_client: MagicMock) -> None:
        config = _ntfy_config(ntfy_url="https://ntfy.example.com/", ntfy_token=TOKEN)
        await send_ntfy_notification(
            config, "SOVA", "Body", kind=NotificationKind.NEEDS_HUMAN, click_url="https://sova.example.com/agents"
        )

        http_client.post.assert_awaited_once()
        call = http_client.post.call_args
        assert call.args[0] == "https://ntfy.example.com/"
        assert call.kwargs["json"] == {
            "topic": "sova",
            "title": "SOVA",
            "message": "Body",
            "priority": 4,
            "tags": ["raised_hand"],
            "click": "https://sova.example.com/agents",
        }
        assert call.kwargs["headers"] == {"Authorization": f"Bearer {TOKEN}"}
        assert TOKEN not in str(call.kwargs["json"])
        assert TOKEN not in call.args[0]

    async def test_default_priority_and_no_click_without_link(self, http_client: MagicMock) -> None:
        await send_ntfy_notification(_ntfy_config(), "SOVA", "Body", kind=NotificationKind.FAILED)

        payload = http_client.post.call_args.kwargs["json"]
        assert payload["priority"] == 3
        assert payload["tags"] == ["warning"]
        assert "click" not in payload

    async def test_long_message_truncated_to_ntfy_limit(self, http_client: MagicMock) -> None:
        await send_ntfy_notification(_ntfy_config(), "SOVA", "\u00e9" * 5000)

        message = http_client.post.call_args.kwargs["json"]["message"]
        assert len(message.encode("utf-8")) <= 4096
        assert message == "\u00e9" * 2048

    async def test_no_authorization_header_without_token(self, http_client: MagicMock) -> None:
        await send_ntfy_notification(_ntfy_config(), "SOVA", "Body")

        assert http_client.post.call_args.kwargs["headers"] == {}

    async def test_keyring_token_wins_over_database_value(self, http_client: MagicMock, tmp_path: Path) -> None:
        with patch.object(keyring_store, "get_secret", return_value="from-keyring"):
            await send_ntfy_notification(
                _ntfy_config(ntfy_token="keyring:stored"), "SOVA", "Body", project_dir=tmp_path
            )

        assert http_client.post.call_args.kwargs["headers"] == {"Authorization": "Bearer from-keyring"}

    async def test_env_token_overrides_keyring_entry(
        self, http_client: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Rotating a token via the environment must replace a stale keyring entry."""
        monkeypatch.setenv(keyring_store.NTFY_TOKEN_ENV, "from-env")
        with patch.object(keyring_store, "get_secret", return_value="stale-keyring"):
            await send_ntfy_notification(_ntfy_config(ntfy_token="from-env"), "SOVA", "Body", project_dir=tmp_path)

        assert http_client.post.call_args.kwargs["headers"] == {"Authorization": "Bearer from-env"}

    async def test_blank_env_token_falls_back_to_keyring(
        self, http_client: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(keyring_store.NTFY_TOKEN_ENV, "   ")
        with patch.object(keyring_store, "get_secret", return_value="from-keyring"):
            await send_ntfy_notification(_ntfy_config(), "SOVA", "Body", project_dir=tmp_path)

        assert http_client.post.call_args.kwargs["headers"] == {"Authorization": "Bearer from-keyring"}

    async def test_empty_url_or_topic_sends_nothing(self, http_client: MagicMock) -> None:
        await send_ntfy_notification(_ntfy_config(ntfy_topic=""), "SOVA", "Body")

        http_client.post.assert_not_awaited()

    async def test_non_2xx_is_logged_without_token_and_not_raised(self) -> None:
        request = httpx.Request("POST", "https://ntfy.example.com/")
        response = httpx.Response(401, request=request)
        mock_response = MagicMock()
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError("401", request=request, response=response)
        patcher, _client = _mock_http(mock_response)
        try:
            with patch("sova.ipc.notifications.log") as mock_log:
                await send_ntfy_notification(_ntfy_config(ntfy_token=TOKEN), "SOVA", "Body")
        finally:
            patcher.stop()

        mock_log.warning.assert_called_once()
        args, kwargs = mock_log.warning.call_args
        assert kwargs["exc_info"] is True
        assert kwargs["status"] == 401
        assert kwargs["topic"] == "sova"
        assert TOKEN not in str(args) + str(kwargs)

    async def test_network_error_is_logged_not_raised(self, http_client: MagicMock) -> None:
        http_client.post.side_effect = httpx.ConnectError("refused")

        with patch("sova.ipc.notifications.log") as mock_log:
            await send_ntfy_notification(_ntfy_config(), "SOVA", "Body")

        assert mock_log.warning.call_args.kwargs["status"] is None


class TestTokenAbsentFromRenderedLogs:
    """Run the failure branch through the project's real logging config and inspect the rendered output.

    A mocked ``log.warning`` only proves the arguments are clean; ``exc_info=True`` also renders the
    traceback, and httpx/httpcore frames hold the Authorization header in their locals.
    """

    @pytest.fixture(autouse=True)
    def _restore_logging(self):
        yield
        from tests.conftest import configure_test_logging

        configure_test_logging()

    @pytest.mark.parametrize("json_output", [False, True])
    @pytest.mark.parametrize("failure", ["401", "connect_error"])
    async def test_token_never_appears_in_rendered_output(
        self, capsys: pytest.CaptureFixture[str], json_output: bool, failure: str
    ) -> None:
        import structlog

        from sova.utils.logging import setup_logging

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.headers["Authorization"] == f"Bearer {TOKEN}"
            if failure == "connect_error":
                raise httpx.ConnectError("refused", request=request)
            return httpx.Response(401, request=request)

        real_client = httpx.AsyncClient

        def client_with_mock_transport(*args: object, **kwargs: object) -> httpx.AsyncClient:
            return real_client(*args, transport=httpx.MockTransport(handler), **kwargs)

        setup_logging(json_output=json_output, level="DEBUG")
        with (
            patch("sova.ipc.notifications.httpx.AsyncClient", side_effect=client_with_mock_transport),
            patch("sova.ipc.notifications.log", structlog.get_logger()),
        ):
            await send_ntfy_notification(_ntfy_config(ntfy_token=TOKEN), "SOVA", "Body")

        captured = capsys.readouterr()
        rendered = captured.out + captured.err
        assert "notify.ntfy_failed" in rendered
        assert "Traceback" in rendered or "traceback" in rendered or "exception" in rendered
        assert TOKEN not in rendered


class TestWebhookPayload:
    async def test_item_fields_are_additive(self, http_client: MagicMock) -> None:
        item = {"kind": "failed", "issue": "42", "pr_number": 5, "run_id": 3, "url": None}
        await send_webhook_notification("https://hooks.example.com", "", "SOVA", "Body", item=item)

        assert http_client.post.call_args.kwargs["json"] == {"title": "SOVA", "message": "Body", **item}

    async def test_notify_sends_null_url_without_link_base(self) -> None:
        config = NotificationConfig(webhook_url="https://hooks.example.com")
        with patch("sova.ipc.notifications.send_webhook_notification", new_callable=AsyncMock) as mock_webhook:
            notify(config, "SOVA", "Body", kind="bogus", issue=42)
            await _drain()

        assert mock_webhook.call_args.kwargs["item"] == {
            "kind": "info",
            "issue": "42",
            "pr_number": None,
            "run_id": None,
            "url": None,
        }


class TestNtfyConfig:
    def test_defaults(self) -> None:
        assert NotificationConfig().ntfy_events == ["needs_human", "failed"]

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("needs_human,failed", ["needs_human", "failed"]),
            (" failed , finished ", ["failed", "finished"]),
            ('["info"]', ["info"]),
            ("failed", ["failed"]),
        ],
    )
    def test_events_env_override_accepts_comma_text_or_json(
        self, monkeypatch: pytest.MonkeyPatch, raw: str, expected: list[str]
    ) -> None:
        monkeypatch.setenv("SOVA_NOTIFICATION_NTFY_EVENTS", raw)

        assert NotificationConfig().ntfy_events == expected

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (["Failed", " needs_human", "", "  "], ["failed", "needs_human"]),
            (("FINISHED",), ["finished"]),
            ("Failed, NEEDS_HUMAN", ["failed", "needs_human"]),
            ('["Failed", " Info "]', ["failed", "info"]),
            ("[needs_human", ["needs_human"]),
            ("[needs_human, failed", ["needs_human", "failed"]),
            ('[1, "failed"]', ["1", "failed"]),
            ("[1]", ["1"]),
            ("[]", []),
            ("", []),
            (None, []),
            (7, ["7"]),
        ],
    )
    def test_events_are_normalized_and_never_raise(self, raw: object, expected: list[str]) -> None:
        assert NotificationConfig(ntfy_events=raw).ntfy_events == expected

    @pytest.mark.parametrize("raw", ["[needs_human", '[1, "failed"]', "[1]"])
    def test_malformed_env_events_do_not_break_load(self, monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
        monkeypatch.setenv("SOVA_NOTIFICATION_NTFY_EVENTS", raw)

        assert isinstance(NotificationConfig().ntfy_events, list)

    async def test_padded_mixed_case_events_still_fire(self) -> None:
        config = _ntfy_config(ntfy_events=["Failed", " needs_human"])
        with patch("sova.ipc.notifications.send_ntfy_notification", new_callable=AsyncMock) as mock_ntfy:
            notify(config, "SOVA", "Body", kind=NotificationKind.FAILED)
            await _drain()

        mock_ntfy.assert_awaited_once()

    def test_events_accepts_comma_text_as_init_value(self) -> None:
        assert NotificationConfig(ntfy_events="needs_human, failed").ntfy_events == ["needs_human", "failed"]

    def test_token_excluded_from_repr(self) -> None:
        assert TOKEN not in repr(NotificationConfig(ntfy_token=TOKEN))

    def test_token_is_keyring_resolved(self) -> None:
        assert "notification.ntfy_token" in keyring_store.RESOLVED_SECRET_KEYS

    async def test_token_write_goes_to_ntfy_scoped_keyring_entry(self, tmp_path: Path) -> None:
        from sova.config.db_loader import get_setting
        from sova.dashboard.services.settings_service import update_config
        from sova.db.session import get_session

        with (
            patch.object(keyring_store, "is_keyring_available", return_value=True),
            patch.object(keyring_store, "set_secret", return_value=True) as mock_set,
        ):
            result = await update_config(tmp_path, key="notification.ntfy_token", value=TOKEN)

        assert result.get("status") == "ok"
        assert TOKEN not in result["value"]
        expected = keyring_store.scoped_secret_name("notification.ntfy_token", tmp_path, "ntfy")
        mock_set.assert_called_once_with(expected, TOKEN)
        async with await get_session(project_dir=tmp_path) as session:
            assert await get_setting(session, "notification.ntfy_token") == keyring_store.SENTINEL


class TestNtfyEventsSettingsWrite:
    """`sova config set notification.ntfy_events` (update_config) stores a JSON array whichever form it is given."""

    @pytest.mark.parametrize("value", ["needs_human,failed", "needs_human, failed", '["needs_human","failed"]'])
    async def test_list_forms_store_valid_json_and_round_trip(self, tmp_path: Path, value: str) -> None:
        import json

        from sova.config.db_loader import get_setting
        from sova.config.loader import load_config
        from sova.dashboard.services.settings_service import update_config
        from sova.db.session import get_session

        result = await update_config(tmp_path, key="notification.ntfy_events", value=value)

        assert result.get("status") == "ok"
        async with await get_session(project_dir=tmp_path) as session:
            stored = await get_setting(session, "notification.ntfy_events")
        # Read back as the raw stored value: a comma string stored verbatim would not be a list.
        assert (json.loads(stored) if isinstance(stored, str) else stored) == ["needs_human", "failed"]
        assert load_config(tmp_path).notification.ntfy_events == ["needs_human", "failed"]

    async def test_emptying_the_list_disables_ntfy_events(self, tmp_path: Path) -> None:
        from sova.config.loader import load_config
        from sova.dashboard.services.settings_service import update_config

        await update_config(tmp_path, key="notification.ntfy_events", value="failed")
        result = await update_config(tmp_path, key="notification.ntfy_events", value="")

        assert result.get("status") == "ok"
        assert load_config(tmp_path).notification.ntfy_events == []


class TestHandoffKind:
    @pytest.mark.parametrize(
        ("needs_human", "awaiting_approval", "expected"),
        [
            (True, False, "needs_human"),
            (False, False, "info"),
            # The engine's approval notice is the needs_human one; the step's own notice stays info.
            (False, True, "info"),
            (True, True, "info"),
        ],
    )
    async def test_kind(self, tmp_path: Path, needs_human: bool, awaiting_approval: bool, expected: str) -> None:
        from sova.core.steps._handoff_helpers import write_step_handoff

        ctx = MagicMock(
            task_run_id=None,
            project_dir=tmp_path,
            notification_item={},
            addressed_external_findings=[],
            addressed_review_findings=[],
            pr_number=None,
            branch_name="b",
            issue_number="1",
            cost_usd=0,
        )
        with (
            patch("sova.core.steps._handoff_helpers.write_handoff_file"),
            patch("sova.core.steps._handoff_helpers.notify") as mock_notify,
        ):
            await write_step_handoff(
                ctx,
                role="r",
                phase="p",
                summary="s",
                next_action="n",
                actions=[],
                notification_message="m",
                notification_subtitle="t",
                result_summary="rs",
                needs_human=needs_human,
                awaiting_approval=awaiting_approval,
            )

        assert mock_notify.call_args.kwargs["kind"] == expected


_NOTIFICATIONS_MODULE = "sova.ipc.notifications"


def _notify_aliases(tree: ast.AST) -> tuple[set[str], set[str]]:
    """Names bound to ``notify`` and to the notifications module by this file's import statements."""
    function_names: set[str] = set()
    module_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module == _NOTIFICATIONS_MODULE:
                # ``import *`` binds ``notify`` under its own name, so it is treated like a plain import of it.
                function_names.update(a.asname or "notify" for a in node.names if a.name in ("notify", "*"))
            elif node.module == "sova.ipc":
                module_names.update(a.asname or a.name for a in node.names if a.name == "notifications")
        elif isinstance(node, ast.Import):
            # ``import a.b.c`` without an alias binds the full dotted path as the call receiver.
            module_names.update(a.asname or a.name for a in node.names if a.name == _NOTIFICATIONS_MODULE)
    return function_names, module_names


def _dotted_name(node: ast.expr) -> str | None:
    """``a.b.c`` for a Name or an attribute chain of Names, otherwise None."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted_name(node.value)
        return f"{base}.{node.attr}" if base else None
    return None


def _notify_calls_missing_kind_in(source: str, label: str) -> list[str]:
    """Calls to sova.ipc.notifications.notify in *source* (aliases resolved) with no explicit ``kind=``.

    Only files that import ``notify`` are inspected, so an unrelated ``condition.notify()`` is ignored. ``kind``
    must be a named keyword: one hidden inside ``**kwargs`` cannot be checked, so it does not count.
    """
    tree = ast.parse(source)
    function_names, module_names = _notify_aliases(tree)
    missing: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_notify = (isinstance(func, ast.Name) and func.id in function_names) or (
            isinstance(func, ast.Attribute) and func.attr == "notify" and _dotted_name(func.value) in module_names
        )
        if is_notify and not any(kw.arg == "kind" for kw in node.keywords):
            missing.append(f"{label}:{node.lineno}")
    return missing


def _notify_calls_missing_kind() -> list[str]:
    missing: list[str] = []
    for path in sorted(SOVA_ROOT.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        missing.extend(_notify_calls_missing_kind_in(source, str(path.relative_to(SOVA_ROOT.parent))))
    return missing


def test_every_notify_call_passes_kind() -> None:
    """A new notify() call that omits kind would default to info and never reach ntfy: fail it here."""
    assert _notify_calls_missing_kind() == []


class TestNotifyKindGuardChecker:
    """The guard must catch aliases and kwargs-hidden kinds, and ignore unrelated ``.notify()`` calls."""

    @pytest.mark.parametrize(
        "source",
        [
            "from sova.ipc.notifications import notify\nnotify(c, 't', 'm')\n",
            "from sova.ipc.notifications import notify as send\nsend(c, 't', 'm')\n",
            "from sova.ipc import notifications\nnotifications.notify(c, 't', 'm')\n",
            "from sova.ipc import notifications as n\nn.notify(c, 't', 'm')\n",
            "import sova.ipc.notifications as n\nn.notify(c, 't', 'm')\n",
            "from sova.ipc.notifications import notify\nnotify(c, 't', 'm', **extra)\n",
            "import sova.ipc.notifications\nsova.ipc.notifications.notify(c, 't', 'm')\n",
            "from sova.ipc.notifications import *\nnotify(c, 't', 'm')\n",
            "def f():\n    from sova.ipc.notifications import notify\n    notify(c, 't', 'm')\n",
        ],
    )
    def test_flags_call_without_explicit_kind(self, source: str) -> None:
        assert _notify_calls_missing_kind_in(source, "x.py") != []

    @pytest.mark.parametrize(
        "source",
        [
            "from sova.ipc.notifications import notify\nnotify(c, 't', 'm', kind=K.FAILED)\n",
            "from sova.ipc.notifications import notify as send\nsend(c, 't', 'm', kind=K.INFO, **item)\n",
            "import asyncio\ncondition.notify()\ncondition.notify(2)\n",
            "def notify(*a):\n    pass\nnotify(1)\n",
        ],
    )
    def test_accepts_explicit_kind_and_unrelated_notify(self, source: str) -> None:
        assert _notify_calls_missing_kind_in(source, "x.py") == []


class TestExecutionContextNotificationItem:
    def test_notification_item_identifies_the_run(self, tmp_path: Path) -> None:
        from sova.config.models import ProjectConfig
        from sova.core.context import ExecutionContext

        ctx = ExecutionContext(
            project_dir=tmp_path,
            config=ProjectConfig(),
            adapter=AsyncMock(),
            issue_number="42",
            pr_number=7,
            task_run_id=3,
        )

        assert ctx.notification_item == {"issue": "42", "pr_number": 7, "run_id": 3, "project_dir": tmp_path}

    def test_notification_item_empty_issue_becomes_none(self, tmp_path: Path) -> None:
        from sova.config.models import ProjectConfig
        from sova.core.context import ExecutionContext

        ctx = ExecutionContext(project_dir=tmp_path, config=ProjectConfig(), adapter=AsyncMock())

        assert ctx.notification_item == {"issue": None, "pr_number": None, "run_id": None, "project_dir": tmp_path}


class TestBudgetExhaustedNotification:
    def _agent(self, tmp_path: Path, **overrides: object):
        from sova.dashboard.services.agent_pool import AgentState

        values: dict[str, object] = {
            "run_id": 11,
            "issue": "42",
            "role": "developer",
            "process": MagicMock(),
            "project_dir": tmp_path,
            "pr_number": 9,
        }
        values.update(overrides)
        return AgentState(**values)

    def test_notify_budget_exhausted_sends_needs_human_with_item(self, tmp_path: Path) -> None:
        from sova.dashboard.services.agent_handoff import _notify_budget_exhausted

        with (
            patch("sova.config.loader.load_config", return_value=MagicMock(notification=_ntfy_config())),
            patch("sova.ipc.notifications.notify") as mock_notify,
        ):
            _notify_budget_exhausted(self._agent(tmp_path), "42", "budget spent", headline="review budget exhausted")

        kwargs = mock_notify.call_args.kwargs
        assert kwargs["kind"] == NotificationKind.NEEDS_HUMAN
        assert kwargs["subtitle"] == "Review budget exhausted #42"
        assert kwargs["group"] == "sova-42"
        assert (kwargs["issue"], kwargs["pr_number"], kwargs["run_id"]) == ("42", 9, 11)
        assert kwargs["project_dir"] == tmp_path

    def test_notify_budget_exhausted_without_issue_uses_generic_group(self, tmp_path: Path) -> None:
        from sova.dashboard.services.agent_handoff import _notify_budget_exhausted

        with (
            patch("sova.config.loader.load_config", return_value=MagicMock(notification=_ntfy_config())),
            patch("sova.ipc.notifications.notify") as mock_notify,
        ):
            _notify_budget_exhausted(self._agent(tmp_path, issue=""), "", "budget spent", headline="budget unknown")

        kwargs = mock_notify.call_args.kwargs
        assert kwargs["issue"] is None
        assert kwargs["group"] == "sova"
        assert kwargs["subtitle"] == "Budget unknown Agent"

    def test_notify_budget_exhausted_swallows_config_load_failure(self, tmp_path: Path) -> None:
        from sova.dashboard.services.agent_handoff import _notify_budget_exhausted

        with (
            patch("sova.config.loader.load_config", side_effect=RuntimeError("db locked")),
            patch("sova.ipc.notifications.notify") as mock_notify,
        ):
            _notify_budget_exhausted(self._agent(tmp_path), "42", "budget spent", headline="budget exhausted")

        mock_notify.assert_not_called()


class TestFinalizeNotificationKind:
    async def _finalize(
        self,
        tmp_path: Path,
        exit_code: int,
        *,
        notify_side_effect: object = None,
        db_status: str | None = None,
        status_changed: bool = True,
        failure_reason: str = "",
        engine_announced: bool | Exception = False,
    ) -> MagicMock:
        from sova.dashboard.services import agent_finalize
        from sova.dashboard.services.agent_pool import AgentState, ProjectAgents

        agent = AgentState(run_id=5, issue="77", role="developer", process=MagicMock(), project_dir=tmp_path)
        pa = ProjectAgents(project_dir=tmp_path)
        pa.agents[5] = agent
        marker_lookup = (
            AsyncMock(side_effect=engine_announced)
            if isinstance(engine_announced, Exception)
            else AsyncMock(return_value=engine_announced)
        )

        with (
            patch.object(agent_finalize, "_wait_with_terminal_check", new=AsyncMock(return_value=exit_code)),
            patch.object(agent_finalize, "_finalize_task_run", new=AsyncMock(return_value=status_changed)),
            patch.object(
                agent_finalize,
                "_validate_command_outcome",
                new=AsyncMock(return_value=MagicMock(failure_reason=failure_reason, network_blocked=False)),
            ),
            patch.object(agent_finalize, "_validate_pipeline_outcome", new=AsyncMock(return_value="")),
            patch.object(agent_finalize, "_downgrade_to_failed", new=AsyncMock()),
            patch("sova.dashboard.services.agent_resource._finalize_resource_monitoring", new=AsyncMock()),
            patch.object(agent_finalize, "_get_task_run_status", new=AsyncMock(return_value=db_status)),
            patch.object(agent_finalize, "_engine_announced_failure", new=marker_lookup),
            patch.object(agent_finalize, "_announce_agent_finished", new=AsyncMock()),
            patch("sova.dashboard.services.agent_approval._finalize_lifecycle_phase", new=AsyncMock()),
            patch("sova.config.loader.load_config", return_value=MagicMock(notification=_ntfy_config())),
            patch("sova.ipc.notifications.notify", side_effect=notify_side_effect) as mock_notify,
            patch("sova.dashboard.services.agent_handoff._process_auto_handoff", new=AsyncMock()),
        ):
            await asyncio.wait_for(agent_finalize._wait_and_finalize(pa, agent), timeout=10)
        return mock_notify

    async def test_wait_and_finalize_failed_exit_sends_failed_kind(self, tmp_path: Path) -> None:
        mock_notify = await self._finalize(tmp_path, 1)

        mock_notify.assert_called_once()
        kwargs = mock_notify.call_args.kwargs
        assert kwargs["kind"] == NotificationKind.FAILED
        assert kwargs["subtitle"] == "Developer failed #77"
        assert (kwargs["issue"], kwargs["pr_number"], kwargs["run_id"]) == ("77", None, 5)
        assert kwargs["project_dir"] == tmp_path
        assert "Exit code 1" in mock_notify.call_args.args[2]

    async def test_wait_and_finalize_clean_exit_sends_finished_kind(self, tmp_path: Path) -> None:
        mock_notify = await self._finalize(tmp_path, 0)

        kwargs = mock_notify.call_args.kwargs
        assert kwargs["kind"] == NotificationKind.FINISHED
        assert kwargs["subtitle"] == "Developer finished #77"
        assert (kwargs["issue"], kwargs["run_id"], kwargs["project_dir"]) == ("77", 5, tmp_path)

    @pytest.mark.parametrize("exit_code", [0, 1])
    @pytest.mark.parametrize("db_status", ["awaiting_approval", "paused"])
    async def test_run_waiting_on_a_human_sends_info_not_failed_or_finished(
        self, tmp_path: Path, exit_code: int, db_status: str
    ) -> None:
        mock_notify = await self._finalize(tmp_path, exit_code, db_status=db_status, status_changed=False)

        assert mock_notify.call_args.kwargs["kind"] == NotificationKind.INFO

    @pytest.mark.parametrize("exit_code", [0, 1])
    async def test_failed_run_with_engine_failure_notice_on_record_sends_info(
        self, tmp_path: Path, exit_code: int
    ) -> None:
        mock_notify = await self._finalize(
            tmp_path, exit_code, db_status="failed", status_changed=False, engine_announced=True
        )

        assert mock_notify.call_args.kwargs["kind"] == NotificationKind.INFO

    @pytest.mark.parametrize("exit_code", [0, 1])
    async def test_failed_run_without_engine_failure_notice_is_failed(self, tmp_path: Path, exit_code: int) -> None:
        """A run marked failed by a path other than the engine's step failure (setup error, top-level handler)."""
        mock_notify = await self._finalize(
            tmp_path, exit_code, db_status="failed", status_changed=False, engine_announced=False
        )

        assert mock_notify.call_args.kwargs["kind"] == NotificationKind.FAILED

    @pytest.mark.parametrize("exit_code", [1, 143])
    async def test_user_stopped_run_sends_info(self, tmp_path: Path, exit_code: int) -> None:
        mock_notify = await self._finalize(tmp_path, exit_code, db_status="stopped", status_changed=True)

        assert mock_notify.call_args.kwargs["kind"] == NotificationKind.INFO

    async def test_interrupted_run_is_failed(self, tmp_path: Path) -> None:
        mock_notify = await self._finalize(tmp_path, 137, db_status="interrupted", status_changed=True)

        assert mock_notify.call_args.kwargs["kind"] == NotificationKind.FAILED

    async def test_done_written_by_this_finalize_is_finished(self, tmp_path: Path) -> None:
        """A command run with no engine: nothing else announced it, so the exit summary is the FINISHED push."""
        mock_notify = await self._finalize(tmp_path, 0, db_status="done", status_changed=True)

        assert mock_notify.call_args.kwargs["kind"] == NotificationKind.FINISHED

    async def test_done_already_recorded_by_the_engine_is_info(self, tmp_path: Path) -> None:
        """The engine's _finalize sent FINISHED already: a second one would double the push for `finished` opt-ins."""
        mock_notify = await self._finalize(tmp_path, 0, db_status="done", status_changed=False)

        assert mock_notify.call_args.kwargs["kind"] == NotificationKind.INFO

    async def test_marker_lookup_error_is_swallowed_with_the_notification(self, tmp_path: Path) -> None:
        """The marker read only picks a kind: whatever it raises is best-effort and must not abort the finalize."""
        mock_notify = await self._finalize(
            tmp_path, 1, db_status="failed", status_changed=False, engine_announced=RuntimeError("session down")
        )

        mock_notify.assert_not_called()

    async def test_crash_the_engine_could_not_report_stays_failed(self, tmp_path: Path) -> None:
        mock_notify = await self._finalize(tmp_path, 137, db_status="failed", status_changed=True)

        assert mock_notify.call_args.kwargs["kind"] == NotificationKind.FAILED

    async def test_clean_exit_downgraded_by_validation_is_failed(self, tmp_path: Path) -> None:
        mock_notify = await self._finalize(
            tmp_path, 0, db_status="failed", status_changed=False, failure_reason="no push"
        )

        assert mock_notify.call_args.kwargs["kind"] == NotificationKind.FAILED

    async def test_wait_and_finalize_swallows_notify_failure(self, tmp_path: Path) -> None:
        mock_notify = await self._finalize(tmp_path, 1, notify_side_effect=RuntimeError("notifier down"))

        mock_notify.assert_called_once()


class _OutcomeStep:
    """Minimal step whose execute() result is fixed; built lazily so BaseStep imports stay local."""

    @staticmethod
    def make(name: str, result_kwargs: dict[str, object]):
        from sova.core.steps.base import BaseStep, GateCheckResult, StepResult

        class Step(BaseStep):
            async def execute(self, ctx):  # type: ignore[no-untyped-def]
                return StepResult(**result_kwargs)

            async def validate_output(self, ctx):  # type: ignore[no-untyped-def]
                return GateCheckResult(passed=True)

        Step.name = name
        return Step()


class TestWorkflowEngineKinds:
    """The engine's notify() call sites carry the kind that matches what happened to the run."""

    @pytest.fixture
    async def engine_parts(self, tmp_path: Path):
        from sova.config.loader import load_config
        from sova.core.context import ExecutionContext
        from sova.utils.shell import run

        await run("git", "init", cwd=tmp_path)
        (tmp_path / "sova.toml").write_text("github_repo = 'test/repo'\n", encoding="utf-8")
        ctx = ExecutionContext(
            project_dir=tmp_path,
            config=load_config(tmp_path),
            adapter=MagicMock(repo="test/repo"),
            issue_number="42",
            role="developer",
            pr_number=7,
        )
        return ctx, tmp_path

    async def _run(self, ctx, steps):
        from sova.core.workflow import WorkflowEngine

        with patch("sova.core.workflow.notify") as mock_notify:
            result = await WorkflowEngine(steps=steps, ctx=ctx).run()
        return result, mock_notify

    async def test_failed_step_sends_one_failed_notice_and_no_finished(self, engine_parts) -> None:
        ctx, tmp_path = engine_parts
        step = _OutcomeStep.make("boom", {"success": False, "error": "it broke"})

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result, mock_notify = await self._run(ctx, [step])

        assert result.success is False
        assert mock_notify.call_count == 1
        kwargs = mock_notify.call_args.kwargs
        assert kwargs["kind"] is NotificationKind.FAILED
        assert (kwargs["issue"], kwargs["pr_number"]) == ("42", 7)
        assert kwargs["run_id"] == result.task_run_id
        assert kwargs["project_dir"] == tmp_path

    async def test_gate_failure_is_reported_as_failed(self, engine_parts) -> None:
        from sova.core.steps.base import BaseStep, GateCheckResult, StepResult

        ctx, _ = engine_parts

        class GatedStep(BaseStep):
            name = "gated"

            async def execute(self, ctx):  # type: ignore[no-untyped-def]
                return StepResult(success=True, summary="ok")

            async def validate_output(self, ctx):  # type: ignore[no-untyped-def]
                return GateCheckResult(passed=False, reason="nothing changed")

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result, mock_notify = await self._run(ctx, [GatedStep()])

        assert result.success is False
        assert [c.kwargs["kind"] for c in mock_notify.call_args_list] == [NotificationKind.FAILED]

    async def test_approval_pause_sends_only_needs_human(self, engine_parts) -> None:
        ctx, _ = engine_parts
        step = _OutcomeStep.make("approve", {"success": True, "summary": "wait", "awaiting_approval": True})

        result, mock_notify = await self._run(ctx, [step])

        assert result.success is False
        assert [c.kwargs["kind"] for c in mock_notify.call_args_list] == [NotificationKind.NEEDS_HUMAN]
        assert mock_notify.call_args.kwargs["run_id"] == result.task_run_id

    async def test_finalize_runs_only_on_success_and_sends_finished(self, engine_parts) -> None:
        ctx, _ = engine_parts
        step = _OutcomeStep.make("fine", {"success": True, "summary": "done"})

        result, mock_notify = await self._run(ctx, [step])

        assert result.success is True
        assert [c.kwargs["kind"] for c in mock_notify.call_args_list] == [NotificationKind.FINISHED]
        assert mock_notify.call_args.kwargs["issue"] == "42"

    async def test_finalize_is_not_reached_after_a_failure(self, engine_parts) -> None:
        """_finalize() marks the run successful itself, so it must never follow a failed or paused step."""
        from sova.core.workflow import WorkflowEngine

        ctx, _ = engine_parts
        steps = [
            _OutcomeStep.make("boom", {"success": False, "error": "x"}),
            _OutcomeStep.make("never", {"success": True}),
        ]
        with (
            patch("sova.core.workflow.notify"),
            patch.object(WorkflowEngine, "_finalize", new_callable=AsyncMock) as mock_finalize,
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            await WorkflowEngine(steps=steps, ctx=ctx).run()

        mock_finalize.assert_not_awaited()

    async def test_step_failure_leaves_the_announced_marker_the_dashboard_reads(self, engine_parts) -> None:
        from sova.dashboard.services.agent_db import _engine_announced_failure

        ctx, tmp_path = engine_parts
        step = _OutcomeStep.make("boom", {"success": False, "error": "it broke"})

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result, _ = await self._run(ctx, [step])

        assert await _engine_announced_failure(result.task_run_id, tmp_path) is True

    async def test_failure_recorded_without_a_push_has_no_marker(self, engine_parts) -> None:
        """A worktree deleted mid-run marks the run failed with no engine notice, so the dashboard must announce it."""
        from sova.dashboard.services.agent_db import _engine_announced_failure

        ctx, tmp_path = engine_parts
        ctx.worktree_dir = tmp_path / "gone"
        step = _OutcomeStep.make("fine", {"success": True})

        result, mock_notify = await self._run(ctx, [step])

        assert result.success is False
        mock_notify.assert_not_called()
        assert await _engine_announced_failure(result.task_run_id, tmp_path) is False

    async def test_gate_failure_ends_paused_and_leaves_no_marker(self, engine_parts) -> None:
        """A gate failure is announced but ends "paused", so it must not read as an announced failed run."""
        from sova.core.steps.base import BaseStep, GateCheckResult, StepResult
        from sova.dashboard.services.agent_db import _engine_announced_failure

        ctx, tmp_path = engine_parts

        class GatedStep(BaseStep):
            name = "gated"

            async def execute(self, ctx):  # type: ignore[no-untyped-def]
                return StepResult(success=True, summary="ok")

            async def validate_output(self, ctx):  # type: ignore[no-untyped-def]
                return GateCheckResult(passed=False, reason="nothing changed")

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result, _ = await self._run(ctx, [GatedStep()])

        assert await _engine_announced_failure(result.task_run_id, tmp_path) is False

    async def test_only_the_newest_failure_record_counts(self, engine_parts) -> None:
        """An announced failure followed by a later unannounced one: the run's last failure was never pushed."""
        from sova.dashboard.services.agent_db import _engine_announced_failure
        from sova.db.models import FailureRecord
        from sova.db.session import get_session

        ctx, tmp_path = engine_parts
        step = _OutcomeStep.make("boom", {"success": False, "error": "it broke"})
        with patch("asyncio.sleep", new_callable=AsyncMock):
            result, _ = await self._run(ctx, [step])
        assert await _engine_announced_failure(result.task_run_id, tmp_path) is True

        async with await get_session(tmp_path) as session, session.begin():
            session.add(
                FailureRecord(
                    task_run_id=result.task_run_id, step_name="x", failure_type="exception", message="later", context={}
                )
            )

        assert await _engine_announced_failure(result.task_run_id, tmp_path) is False

    async def test_adopting_a_run_for_resume_clears_the_stale_marker(self, engine_parts) -> None:
        """Announced failure, then the same run is resumed and crashes without a push: it must read as unannounced."""
        from sova.core.workflow import WorkflowEngine
        from sova.dashboard.services.agent_db import _engine_announced_failure

        ctx, tmp_path = engine_parts
        step = _OutcomeStep.make("boom", {"success": False, "error": "it broke"})
        with patch("asyncio.sleep", new_callable=AsyncMock):
            result, _ = await self._run(ctx, [step])
        assert await _engine_announced_failure(result.task_run_id, tmp_path) is True

        ctx.task_run_id = result.task_run_id
        engine = WorkflowEngine(steps=[step], ctx=ctx)
        engine._task_run_id = result.task_run_id
        await engine._adopt_task_run()

        assert await _engine_announced_failure(result.task_run_id, tmp_path) is False

    async def test_unknown_run_has_no_marker(self, tmp_path: Path) -> None:
        from sova.dashboard.services.agent_db import _engine_announced_failure

        assert await _engine_announced_failure(999, tmp_path) is False


class TestNeedsHumanHandoffEndToEnd:
    async def test_needs_human_handoff_posts_one_complete_ntfy_message(
        self, http_client: MagicMock, tmp_path: Path
    ) -> None:
        """A real notify() with ntfy and link_base_url set yields exactly one POST carrying all five fields."""
        from sova.core.steps._handoff_helpers import write_step_handoff
        from sova.ipc import notifications

        config = _ntfy_config(link_base_url="https://sova.example.com/", ntfy_token=TOKEN)
        ctx = MagicMock(
            task_run_id=None,
            project_dir=tmp_path,
            config=MagicMock(notification=config),
            notification_group="sova-42",
            notification_item={"issue": "42", "pr_number": 7, "run_id": 3, "project_dir": tmp_path},
            addressed_external_findings=[],
            addressed_review_findings=[],
            pr_number=7,
            branch_name="b",
            issue_number="42",
            cost_usd=0,
        )

        with patch("sova.core.steps._handoff_helpers.write_handoff_file"):
            await write_step_handoff(
                ctx,
                role="developer",
                phase="p",
                summary="s",
                next_action="n",
                actions=[],
                notification_message="needs your review",
                notification_subtitle="Developer needs you",
                result_summary="rs",
                needs_human=True,
            )
        # The token lookup runs in a thread, so wait for the sends rather than yielding a fixed number of ticks.
        await asyncio.gather(*notifications._background_tasks)

        assert http_client.post.await_count == 1
        call = http_client.post.call_args
        assert call.args[0] == "https://ntfy.example.com/"
        payload = call.kwargs["json"]
        assert payload["topic"] == "sova"
        assert payload["title"] == "SOVA"
        assert payload["message"] == f"Developer needs you: {tmp_path.name} | needs your review"
        assert payload["priority"] == 4
        assert payload["click"] == "https://sova.example.com/work/3"
        assert call.kwargs["headers"] == {"Authorization": f"Bearer {TOKEN}"}
