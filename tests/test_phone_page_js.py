"""Behavior of the phone page's modules (static/js/pages/phone/, #1454), run under node.

The tests run the shipped app.js and the shipped page modules against a small fake DOM and a routed fake fetch
(tests/fixtures/phone_page_harness.mjs). A scenario drives the page the way a tap does: through the real click
dispatcher (agents/actions.js), with the buttons found in the markup each section renders.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

_DASHBOARD = Path(__file__).parent.parent / "sova" / "dashboard"
_APP_JS = _DASHBOARD / "static" / "app.js"
_PAGES = _DASHBOARD / "static" / "js" / "pages"
_HARNESS = Path(__file__).parent / "fixtures" / "phone_page_harness.mjs"


def _run(script: str, routes: dict, **extra: object) -> dict:
    if shutil.which("node") is None:
        if os.environ.get("CI"):
            pytest.fail("node is required in CI to run the phone page modules")
        pytest.skip("node is required to run the phone page modules")
    scenario = {"script": script, "routes": routes, **extra}
    proc = subprocess.run(
        ["node", str(_HARNESS), str(_APP_JS), str(_PAGES), json.dumps(scenario)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _handoff(**extra: object) -> dict:
    base = {
        "id": "h1",
        "source": "developer",
        "status": "awaiting_action",
        "issue": "42",
        "pr_number": 7,
        "branch": "feat/issue-42",
        "summary": "PR opened, review it",
        "next_actions": [
            {"id": "review", "label": "Review", "style": "approve", "mode": "agent"},
            {"id": "drop", "label": "Drop it", "style": "danger", "mode": "claude-command"},
            {"id": "sh", "label": "Shell thing", "style": "neutral", "mode": "shell"},
        ],
    }
    return {**base, **extra}


def _waiting_routes(handoffs: list | None = None, pending: list | None = None, **handoff_extra: object) -> dict:
    handoffs = [] if handoffs is None else handoffs
    return {
        "GET /handoff": {"body": {"has_handoff": bool(handoffs), "handoffs": handoffs, **handoff_extra}},
        "GET /supervisor/plan": {"body": {"reasoning": None, "pending": pending or [], "deferred": []}},
        "GET /agents/active": {"body": {"agents": [], "completed": []}},
    }


def _assert_csrf_on_writes_only(requests: list) -> None:
    """Every POST carries the dashboard's CSRF header (the harness cookie is `sova_csrf=tok`); a GET needs none."""
    posts = [r for r in requests if r["method"] == "POST"]
    assert posts
    for r in posts:
        assert r["headers"].get("x-csrf-token") == "tok", r["path"]
    for r in requests:
        if r["method"] == "GET":
            assert "x-csrf-token" not in r["headers"], r["path"]


_PLAN_ITEM = {"issue_number": 12, "action": "start", "role": "developer", "reason": "Ready and unblocked"}
_AGENT = {
    "run_id": 5,
    "issue": "42",
    "role": "developer",
    "status": "running",
    "current_step": "develop",
    "step_index": 5,
    "total_steps": 17,
    "elapsed_seconds": 75,
}


class TestWiring:
    def test_init_loads_every_section_and_a_socket_message_reloads_only_its_section(self) -> None:
        out = _run(
            """
            phone.page.init();
            await settle();
            const first = requests.splice(0).map((r) => r.method + ' ' + r.path).sort();
            const options = sockets[0];
            options.onMessage({ type: 'handoff' });
            await settle();
            const afterHandoff = requests.splice(0).map((r) => r.path).sort();
            options.onMessage({ type: 'activity' });
            await settle();
            const afterActivity = requests.splice(0).map((r) => r.path).sort();
            options.onMessage({ type: 'status_update' });
            await settle();
            const afterStatus = requests.length;
            options.onOpen();
            await settle();
            const hooks = [typeof options.onOpen, typeof options.onFallbackPoll];
            return { first, afterHandoff, afterActivity, afterStatus, hooks, reloadedOnOpen: requests.length };
            """,
            _waiting_routes(),
        )["result"]
        assert out["first"] == ["GET /agents/active", "GET /handoff", "GET /supervisor/plan"]
        assert out["afterHandoff"] == ["/handoff", "/supervisor/plan"]
        assert out["afterActivity"] == ["/agents/active"]
        assert out["afterStatus"] == 0
        assert out["hooks"] == ["function", "function"]
        assert out["reloadedOnOpen"] == 3

    def test_the_fallback_poll_reloads_every_section_and_clears_what_is_gone(self) -> None:
        out = _run(
            """
            phone.page.init();
            await settle();
            const before = [html('phone-waiting'), html('phone-running')];
            requests.length = 0;
            sockets[0].onFallbackPoll();
            await settle();
            const polled = requests.map((r) => r.method + ' ' + r.path).sort();
            return { before, polled, after: [html('phone-waiting'), html('phone-running')] };
            """,
            {
                "GET /handoff": [{"body": {"handoffs": [_handoff()]}}, {"body": {"handoffs": []}}],
                "GET /supervisor/plan": [{"body": {"pending": [_PLAN_ITEM]}}, {"body": {"pending": []}}],
                "GET /agents/active": [{"body": {"agents": [_AGENT]}}, {"body": {"agents": []}}],
            },
        )["result"]
        assert out["polled"] == ["GET /agents/active", "GET /handoff", "GET /supervisor/plan"]
        assert "Review" in out["before"][0]
        assert "Stop" in out["before"][1]
        assert "Nothing is waiting on you" in out["after"][0]
        assert "No agents running" in out["after"][1]

    def test_a_visible_page_reloads_on_a_timer_because_a_plan_change_sends_no_message(self) -> None:
        out = _run(
            """
            phone.page.init();
            await settle();
            const timer = intervals[0];
            requests.length = 0;
            timer.fn();
            await settle();
            const visible = requests.map((r) => r.path).sort();
            return { count: intervals.length, ms: timer.ms, visible };
            """,
            _waiting_routes(),
        )["result"]
        assert out["count"] == 1
        assert out["ms"] > 0
        assert out["visible"] == ["/agents/active", "/handoff", "/supervisor/plan"]

    def test_a_hidden_page_skips_the_timer_and_a_visible_one_reloads_once(self) -> None:
        out = _run(
            """
            phone.page.init();
            await settle();
            setHidden(true);
            requests.length = 0;
            intervals[0].fn();
            await settle();
            const whileHidden = requests.length;
            setHidden(false);
            intervals[0].fn();
            await settle();
            return { whileHidden, afterVisible: requests.map((r) => r.path).sort() };
            """,
            _waiting_routes(),
        )["result"]
        assert out["whileHidden"] == 0
        assert out["afterVisible"] == ["/agents/active", "/handoff", "/supervisor/plan"]

    def test_an_unchanged_poll_leaves_every_section_untouched(self) -> None:
        """A rewrite with the same markup would drop focus, re-announce alerts and swallow a tap mid-swap."""
        out = _run(
            """
            phone.page.init();
            await settle();
            const ids = ['phone-waiting', 'phone-running'];
            const before = ids.map((id) => byId(id).renders);
            sockets[0].onFallbackPoll();
            await settle();
            sockets[0].onFallbackPoll();
            await settle();
            return { before, after: ids.map((id) => byId(id).renders) };
            """,
            {
                "GET /handoff": {"body": {"handoffs": [_handoff()]}},
                "GET /supervisor/plan": {"body": {"pending": [_PLAN_ITEM]}},
                "GET /agents/active": {"body": {"agents": [_AGENT]}},
            },
        )["result"]
        assert out["before"] == [1, 1]
        assert out["after"] == out["before"]

    def test_a_changed_section_is_still_redrawn_after_an_unchanged_one(self) -> None:
        out = _run(
            """
            phone.page.init();
            await settle();
            sockets[0].onFallbackPoll();
            await settle();
            const same = byId('phone-running').renders;
            sockets[0].onFallbackPoll();
            await settle();
            return { same, after: byId('phone-running').renders, text: html('phone-running') };
            """,
            {
                **_waiting_routes(),
                "GET /agents/active": [
                    {"body": {"agents": [_AGENT]}},
                    {"body": {"agents": [_AGENT]}},
                    {"body": {"agents": []}},
                ],
            },
        )["result"]
        assert out["after"] == out["same"] + 1
        assert "No agents running" in out["text"]

    def test_requests_use_the_project_scoped_prefix(self) -> None:
        out = _run(
            """
            phone.page.init();
            await settle();
            return null;
            """,
            {
                "GET /handoff": {"body": {"handoffs": []}},
                "GET /supervisor/plan": {"body": {"pending": []}},
                "GET /agents/active": {"body": {"agents": []}},
            },
            prefix="/p/alpha/api",
        )
        assert {r["path"] for r in out["requests"]} == {"/handoff", "/supervisor/plan", "/agents/active"}


class TestWaiting:
    def test_a_handoff_row_shows_its_runnable_actions_and_dismiss(self) -> None:
        out = _run(
            """
            await phone.waiting.loadWaiting();
            return { html: html('phone-waiting'), buttons: buttons('phone-waiting') };
            """,
            _waiting_routes([_handoff(summary="<b>x</b> & more")]),
        )["result"]
        assert "&lt;b&gt;x&lt;/b&gt; &amp; more" in out["html"]
        assert "feat/issue-42" in out["html"]
        assert [(b["label"], b["action"], b["args"]) for b in out["buttons"]] == [
            ("Review", "phone-handoff-run", ["42", "review", "Review", False]),
            ("Drop it", "phone-handoff-run", ["42", "drop", "Drop it", True]),
            ("Dismiss", "phone-handoff-dismiss", ["42"]),
        ]

    def test_an_empty_answer_clears_the_rows_and_says_nothing_is_waiting(self) -> None:
        out = _run(
            """
            await phone.waiting.loadWaiting();
            const before = html('phone-waiting');
            await phone.waiting.loadWaiting();
            return { before, after: html('phone-waiting') };
            """,
            {
                **_waiting_routes(),
                "GET /handoff": [
                    {"body": {"handoffs": [_handoff()]}},
                    {"body": {"handoffs": []}},
                ],
                "GET /supervisor/plan": [
                    {"body": {"pending": [_PLAN_ITEM]}},
                    {"body": {"pending": []}},
                ],
            },
        )["result"]
        assert "Review" in out["before"]
        assert "Approve" in out["before"]
        assert "Nothing is waiting on you" in out["after"]
        assert "Review" not in out["after"]
        assert "Approve" not in out["after"]

    @pytest.mark.parametrize(
        "extra",
        [
            pytest.param({"unreadable_handoffs": ["/p/.claude/agent-control/handoff-9.json"]}, id="unreadable"),
            pytest.param({"schema_invalid_handoffs": ["/p/.claude/agent-control/handoff-9.json"]}, id="schema-invalid"),
        ],
    )
    def test_a_handoff_file_that_cannot_be_used_is_never_shown_as_nothing_waiting(self, extra: dict) -> None:
        out = _run(
            "await phone.waiting.loadWaiting(); return html('phone-waiting');",
            _waiting_routes([], **extra),
        )["result"]
        assert "Something may be waiting that is not shown here" in out
        assert "Nothing is waiting on you" not in out

    def test_a_failed_read_replaces_the_old_rows_with_an_error(self) -> None:
        out = _run(
            """
            await phone.waiting.loadWaiting();
            await phone.waiting.loadWaiting();
            return html('phone-waiting');
            """,
            {
                **_waiting_routes(),
                "GET /handoff": [{"body": {"handoffs": [_handoff()]}}, {"status": 500, "body": {"detail": "boom"}}],
            },
        )["result"]
        assert "Could not load the handoffs" in out
        assert "Review" not in out

    def test_both_sources_failing_replaces_the_section_with_one_error(self) -> None:
        out = _run(
            "await phone.waiting.loadWaiting(); return html('phone-waiting');",
            {
                "GET /handoff": {"status": 500, "body": {}},
                "GET /supervisor/plan": {"status": 500, "body": {}},
            },
        )["result"]
        assert "Could not load what is waiting on you" in out
        assert "Nothing is waiting on you" not in out

    def test_a_failing_plan_source_does_not_hide_the_handoffs(self) -> None:
        out = _run(
            """
            const timers = [];
            globalThis.setTimeout = (fn, ms) => { timers.push({ fn, ms }); return timers.length; };
            await phone.waiting.loadWaiting();
            return { html: html('phone-waiting'), scheduled: timers.length };
            """,
            {
                **_waiting_routes([_handoff()]),
                "GET /supervisor/plan": {"status": 500, "body": {"detail": "boom"}},
            },
        )["result"]
        assert "Review" in out["html"]
        assert "Could not load the supervisor plan" in out["html"]
        assert "Nothing is waiting on you" not in out["html"]
        assert out["scheduled"] == 1

    def test_a_source_that_recovers_shows_no_error_and_schedules_no_retry(self) -> None:
        out = _run(
            """
            const timers = [];
            globalThis.setTimeout = (fn, ms) => { timers.push({ fn, ms }); return timers.length; };
            await phone.waiting.loadWaiting();
            return { html: html('phone-waiting'), scheduled: timers.length };
            """,
            _waiting_routes([_handoff()]),
        )["result"]
        assert "Could not load" not in out["html"]
        assert out["scheduled"] == 0

    def test_a_failing_source_never_shows_its_old_rows(self) -> None:
        out = _run(
            """
            await phone.waiting.loadWaiting();
            await phone.waiting.loadWaiting();
            return html('phone-waiting');
            """,
            {
                **_waiting_routes(),
                "GET /handoff": {"body": {"handoffs": [_handoff()]}},
                "GET /supervisor/plan": [{"body": {"pending": [_PLAN_ITEM]}}, {"status": 500, "body": {}}],
            },
        )["result"]
        assert "Review" in out
        assert "Approve" not in out
        assert "Could not load the supervisor plan" in out

    def test_a_slow_response_never_overwrites_a_newer_one(self) -> None:
        out = _run(
            """
            const slow = phone.waiting.loadWaiting();
            await new Promise((r) => setTimeout(r, 5));
            await phone.waiting.loadWaiting();
            await slow;
            await settle();
            return html('phone-waiting');
            """,
            {
                **_waiting_routes(),
                "GET /handoff": [
                    {"body": {"handoffs": [_handoff(summary="stale answer")]}, "delay_ms": 80},
                    {"body": {"handoffs": [_handoff(summary="fresh answer")]}},
                ],
            },
        )["result"]
        assert "fresh answer" in out
        assert "stale answer" not in out

    def test_a_handoff_action_posts_its_issue_and_reloads(self) -> None:
        out = _run(
            """
            phone.page.init();
            await settle();
            requests.length = 0;
            await tap('phone-handoff-run', ['42', 'review', 'Review', false]);
            return requests.map((r) => r.method + ' ' + r.path);
            """,
            {**_waiting_routes([_handoff()]), "POST /handoff/execute": {"body": {"status": "started"}}},
        )
        sent = [r for r in out["requests"] if r["path"] == "/handoff/execute"]
        assert [r["body"] for r in sent] == [{"action_id": "review", "issue": "42"}]
        assert {"GET /handoff", "GET /supervisor/plan", "GET /agents/active"} <= set(out["result"])
        assert out["confirms"] == []
        _assert_csrf_on_writes_only(out["requests"])

    def test_a_pr_only_handoff_is_addressed_by_pr_and_has_no_dismiss(self) -> None:
        out = _run(
            """
            await phone.waiting.loadWaiting();
            return buttons('phone-waiting').map((b) => [b.label, b.args]);
            """,
            _waiting_routes([_handoff(issue="", source="pr-review-state")]),
        )["result"]
        assert out[0] == ["Review", ["pr:7", "review", "Review", False]]
        assert all(label != "Dismiss" for label, _ in out)

    def test_the_handoff_synthesized_from_a_pr_state_has_no_dismiss(self) -> None:
        """It has no file, so /handoff/clear would answer success and the row would come straight back."""
        out = _run(
            """
            await phone.waiting.loadWaiting();
            return buttons('phone-waiting').map((b) => [b.label, b.args]);
            """,
            _waiting_routes([_handoff(issue=42, source="pr-review-state")]),
        )["result"]
        assert out[0] == ["Review", ["42", "review", "Review", False]]
        assert all(label != "Dismiss" for label, _ in out)

    @pytest.mark.parametrize("confirmed", [True, False])
    def test_a_danger_action_acts_only_when_confirmed(self, confirmed: bool) -> None:
        out = _run(
            """
            phone.page.init();
            await settle();
            requests.length = 0;
            await tap('phone-handoff-run', ['42', 'drop', 'Drop it', true]);
            """,
            {**_waiting_routes([_handoff()]), "POST /handoff/execute": {"body": {}}},
            confirm=confirmed,
        )
        assert len(out["confirms"]) == 1
        assert any(r["path"] == "/handoff/execute" for r in out["requests"]) is confirmed

    @pytest.mark.parametrize(("cleared", "shown"), [(True, "Dismissed"), (False, "Already gone")])
    def test_dismiss_clears_only_its_own_issue(self, cleared: bool, shown: str) -> None:
        out = _run(
            """
            phone.page.init();
            await settle();
            requests.length = 0;
            await tap('phone-handoff-dismiss', ['42']);
            return html('phone-waiting');
            """,
            {**_waiting_routes([_handoff()]), "POST /handoff/clear": {"body": {"cleared": cleared}}},
        )
        assert [r["body"] for r in out["requests"] if r["path"] == "/handoff/clear"] == [{"issue": "42"}]
        assert shown in out["result"]

    def test_a_failed_action_shows_the_server_text_on_its_row(self) -> None:
        out = _run(
            """
            phone.page.init();
            await settle();
            await tap('phone-handoff-run', ['42', 'review', 'Review', false]);
            return html('phone-waiting');
            """,
            {
                **_waiting_routes([_handoff()]),
                "POST /handoff/execute": {"status": 409, "body": {"detail": "Agent already running for #42"}},
            },
        )["result"]
        assert "Agent already running for #42" in out

    @pytest.mark.parametrize(
        "action",
        [
            pytest.param(
                {
                    "id": "integrate",
                    "label": "Integrate PR",
                    "style": "approve",
                    "mode": "claude-command",
                    "command": "/integrate-pr 7",
                },
                id="command",
            ),
            pytest.param({"id": "integrate-pr", "label": "Integrate", "style": "neutral"}, id="id-with-no-mode"),
        ],
    )
    def test_a_merge_action_is_confirmed_whatever_its_style(self, action: dict) -> None:
        out = _run(
            """
            phone.page.init();
            await settle();
            const shown = buttons('phone-waiting')[0];
            requests.length = 0;
            await tap(shown.action, shown.args);
            return shown.args;
            """,
            {**_waiting_routes([_handoff(next_actions=[action])]), "POST /handoff/execute": {"body": {}}},
            confirm=False,
        )
        assert out["result"][3] is True
        assert len(out["confirms"]) == 1
        assert all(r["path"] != "/handoff/execute" for r in out["requests"])

    def test_an_agent_action_named_like_a_merge_needs_no_confirmation(self) -> None:
        """An agent-mode action never runs /integrate-pr, so its id alone does not require confirmation."""
        action = {"id": "integrate-pr", "label": "Review", "style": "approve", "mode": "agent"}
        out = _run(
            "await phone.waiting.loadWaiting(); return buttons('phone-waiting')[0].args;",
            _waiting_routes([_handoff(next_actions=[action])]),
        )["result"]
        assert out[3] is False

    def test_a_failed_read_reloads_on_its_own(self) -> None:
        out = _run(
            """
            const timers = [];
            globalThis.setTimeout = (fn, ms) => { timers.push({ fn, ms }); return timers.length; };
            await phone.waiting.loadWaiting();
            await phone.waiting.loadWaiting();
            const scheduled = timers.length;
            requests.length = 0;
            timers[0].fn();
            await settle();
            const reloaded = requests.map((r) => r.path);
            return { scheduled, delay: timers[0].ms, html: html('phone-waiting'), reloaded };
            """,
            {
                **_waiting_routes(),
                "GET /handoff": [{"status": 500, "body": {}}, {"status": 500, "body": {}}, {"body": {"handoffs": []}}],
            },
        )["result"]
        assert out["scheduled"] == 1
        assert out["delay"] > 0
        assert "/handoff" in out["reloaded"]
        assert "Nothing is waiting on you" in out["html"]


class TestPlan:
    def test_a_decision_row_approves_and_skips_only_its_own_issue(self) -> None:
        out = _run(
            """
            phone.page.init();
            await settle();
            const buttonsShown = buttons('phone-waiting').map((b) => [b.label, b.action, b.args]);
            requests.length = 0;
            await tap('phone-plan-approve', [12, 'start']);
            await tap('phone-plan-skip', [12, 'start']);
            return buttonsShown;
            """,
            {
                **_waiting_routes([], [_PLAN_ITEM]),
                "POST /supervisor/plan/approve": {"body": {"approved": 1, "results": []}},
                "POST /supervisor/plan/skip/12": {"body": {"skipped": 12}},
            },
        )
        assert out["result"] == [
            ["Approve", "phone-plan-approve", [12, "start"]],
            ["Skip", "phone-plan-skip", [12, "start"]],
        ]
        posts = [(r["path"], r.get("body")) for r in out["requests"] if r["method"] == "POST"]
        assert posts == [("/supervisor/plan/approve", {"issue_numbers": [12]}), ("/supervisor/plan/skip/12", None)]
        _assert_csrf_on_writes_only(out["requests"])

    def test_skip_is_confirmed_and_a_declined_answer_sends_nothing(self) -> None:
        out = _run(
            """
            phone.page.init();
            await settle();
            requests.length = 0;
            await tap('phone-plan-skip', [12, 'start']);
            return null;
            """,
            {**_waiting_routes([], [_PLAN_ITEM]), "POST /supervisor/plan/skip/12": {"body": {"skipped": 12}}},
            confirm=False,
        )
        assert [c["title"] for c in out["confirms"]] == ["Skip"]
        assert "start" in out["confirms"][0]["message"]
        assert "#12" in out["confirms"][0]["message"]
        assert all(r["method"] != "POST" for r in out["requests"])

    def test_approving_one_item_keeps_another_items_failure_notice(self) -> None:
        failure = {"ok": False, "issue_number": 12, "error": "x"}
        detail = {"message": "Some approved decisions failed", "results": [failure]}
        other = {**_PLAN_ITEM, "issue_number": 13}
        out = _run(
            """
            phone.page.init();
            await settle();
            await tap('phone-plan-approve', [12, 'start']);
            await tap('phone-plan-approve', [13, 'start']);
            const kept = html('phone-waiting');
            await tap('phone-notice-dismiss', [12]);
            return { kept, dismissed: html('phone-waiting') };
            """,
            {
                **_waiting_routes(),
                "GET /supervisor/plan": [
                    {"body": {"pending": [_PLAN_ITEM, other]}},
                    {"body": {"pending": [other]}},
                    {"body": {"pending": []}},
                ],
                "POST /supervisor/plan/approve": [
                    {"status": 502, "body": {"detail": detail}},
                    {"body": {"approved": 1, "results": []}},
                ],
            },
        )["result"]
        assert "Some approved decisions failed" in out["kept"]
        assert "Some approved decisions failed" not in out["dismissed"]

    def test_an_approval_that_ran_nothing_does_not_say_approved(self) -> None:
        """The item left the plan elsewhere first: the server answers 200 with approved 0."""
        out = _run(
            """
            phone.page.init();
            await settle();
            await tap('phone-plan-approve', [12, 'start']);
            return html('phone-waiting');
            """,
            {
                **_waiting_routes([], [_PLAN_ITEM]),
                "POST /supervisor/plan/approve": {"body": {"approved": 0, "results": []}},
            },
        )["result"]
        assert "Already gone" in out
        assert "Approved" not in out

    @pytest.mark.parametrize(
        ("script_tap", "route", "text"),
        [
            (
                "phone-plan-approve",
                {"POST /supervisor/plan/approve": {"body": {"approved": 0, "results": []}}},
                "Already gone",
            ),
            (
                "phone-plan-approve",
                {"POST /supervisor/plan/approve": {"body": {"approved": 1, "results": []}}},
                "Approved",
            ),
            (
                "phone-plan-skip",
                {"POST /supervisor/plan/skip/12": {"status": 404, "body": {"detail": "gone"}}},
                "Already gone",
            ),
            ("phone-plan-skip", {"POST /supervisor/plan/skip/12": {"body": {}}}, "Skipped"),
        ],
    )
    def test_the_outcome_of_a_decision_stays_visible_after_its_row_leaves(
        self, script_tap: str, route: dict, text: str
    ) -> None:
        """The reload drops the row and its status line, so the outcome is shown outside the row."""
        out = _run(
            f"""
            phone.page.init();
            await settle();
            await tap('{script_tap}', [12, 'start']);
            return html('phone-waiting');
            """,
            {
                **_waiting_routes(),
                "GET /supervisor/plan": [{"body": {"pending": [_PLAN_ITEM]}}, {"body": {"pending": []}}],
                **route,
            },
        )["result"]
        assert f"#12: {text}" in out
        assert "Nothing is waiting on you" in out

    def test_a_dismissed_handoff_that_was_already_gone_says_so_after_the_row_leaves(self) -> None:
        out = _run(
            """
            phone.page.init();
            await settle();
            await tap('phone-handoff-dismiss', ['42']);
            return html('phone-waiting');
            """,
            {
                **_waiting_routes(),
                "GET /handoff": [{"body": {"handoffs": [_handoff()]}}, {"body": {"handoffs": []}}],
                "POST /handoff/clear": {"body": {"cleared": False}},
            },
        )["result"]
        assert "#42: Already gone" in out

    def test_a_partly_failed_approval_lists_each_failure_and_is_not_retried(self) -> None:
        detail = {
            "message": "Some approved decisions failed to execute",
            "results": [
                {"ok": True, "issue_number": 11},
                {"ok": False, "issue_number": 12, "error": "Decision failed"},
            ],
        }
        out = _run(
            """
            phone.page.init();
            await settle();
            await tap('phone-plan-approve', [12, 'start']);
            return html('phone-waiting');
            """,
            {
                **_waiting_routes(),
                "GET /supervisor/plan": [{"body": {"pending": [_PLAN_ITEM]}}, {"body": {"pending": []}}],
                "POST /supervisor/plan/approve": {"status": 502, "body": {"detail": detail}},
            },
        )
        assert "Some approved decisions failed to execute" in out["result"]
        assert "#12: Decision failed" in out["result"]
        assert "#11" not in out["result"]
        assert "[object Object]" not in out["result"]
        assert [r["path"] for r in out["requests"]].count("/supervisor/plan/approve") == 1

    def test_skipping_an_item_that_is_already_gone_reloads_without_an_error(self) -> None:
        out = _run(
            """
            phone.page.init();
            await settle();
            await tap('phone-plan-skip', [12, 'start']);
            return html('phone-waiting');
            """,
            {
                **_waiting_routes(),
                "GET /supervisor/plan": [{"body": {"pending": [_PLAN_ITEM]}}, {"body": {"pending": []}}],
                "POST /supervisor/plan/skip/12": {
                    "status": 404,
                    "body": {"detail": "Issue #12 is not in the pending plan"},
                },
            },
        )["result"]
        assert "Nothing is waiting on you" in out
        assert "not in the pending plan" not in out


class TestRunning:
    def test_only_active_agents_are_listed_and_each_has_stop(self) -> None:
        out = _run(
            """
            await phone.running.loadRunning();
            return { html: html('phone-running'), buttons: buttons('phone-running') };
            """,
            {
                "GET /agents/active": {
                    "body": {"agents": [_AGENT], "completed": [{**_AGENT, "run_id": 6, "issue": "99"}]}
                }
            },
        )["result"]
        assert "#42" in out["html"]
        assert "#99" not in out["html"]
        assert "develop (5/17)" in out["html"]
        assert "1m 15s" in out["html"]
        assert [(b["label"], b["args"]) for b in out["buttons"]] == [("Stop", [5, "#42"])]

    def test_no_agents_clears_the_rows(self) -> None:
        out = _run(
            "await phone.running.loadRunning(); await phone.running.loadRunning(); return html('phone-running');",
            {"GET /agents/active": [{"body": {"agents": [_AGENT]}}, {"body": {"agents": []}}]},
        )["result"]
        assert "No agents running" in out
        assert "Stop" not in out

    def test_a_slow_response_never_brings_back_a_finished_agent(self) -> None:
        out = _run(
            """
            const slow = phone.running.loadRunning();
            await new Promise((r) => setTimeout(r, 5));
            await phone.running.loadRunning();
            await slow;
            await settle();
            return html('phone-running');
            """,
            {
                "GET /agents/active": [
                    {"body": {"agents": [_AGENT]}, "delay_ms": 80},
                    {"body": {"agents": []}},
                ]
            },
        )["result"]
        assert "No agents running" in out
        assert "Stop" not in out

    def test_a_failed_read_shows_an_error_not_the_old_rows(self) -> None:
        out = _run(
            "await phone.running.loadRunning(); await phone.running.loadRunning(); return html('phone-running');",
            {"GET /agents/active": [{"body": {"agents": [_AGENT]}}, {"status": 503, "body": {}}]},
        )["result"]
        assert "Could not load the running agents" in out
        assert "Stop" not in out

    def test_a_failed_read_reloads_on_its_own(self) -> None:
        out = _run(
            """
            const timers = [];
            globalThis.setTimeout = (fn, ms) => { timers.push({ fn, ms }); return timers.length; };
            await phone.running.loadRunning();
            await phone.running.loadRunning();
            const scheduled = timers.length;
            timers[0].fn();
            await settle();
            return { scheduled, html: html('phone-running') };
            """,
            {
                "GET /agents/active": [
                    {"status": 503, "body": {}},
                    {"status": 503, "body": {}},
                    {"body": {"agents": [_AGENT]}},
                ]
            },
        )["result"]
        assert out["scheduled"] == 1
        assert "#42" in out["html"]
        assert "Could not load the running agents" not in out["html"]

    def test_stop_posts_the_run_id_once_and_a_refusal_is_shown_inline(self) -> None:
        reason = "Run 5 was started outside the dashboard, so the dashboard cannot stop it."
        out = _run(
            """
            phone.page.init();
            await settle();
            await tap('phone-stop', [5, '#42']);
            return html('phone-running');
            """,
            {
                **_waiting_routes(),
                "GET /agents/active": {"body": {"agents": [_AGENT]}},
                "POST /agents/5/stop": {"status": 409, "body": {"detail": reason}},
            },
        )
        assert out["result"].count(reason) == 1
        assert [r["path"] for r in out["requests"]].count("/agents/5/stop") == 1
        assert len(out["confirms"]) == 1
        _assert_csrf_on_writes_only(out["requests"])

    def test_a_declined_confirmation_sends_nothing(self) -> None:
        out = _run(
            "phone.page.init(); await settle(); requests.length = 0; await tap('phone-stop', [5, '#42']);",
            {
                **_waiting_routes(),
                "GET /agents/active": {"body": {"agents": [_AGENT]}},
                "POST /agents/5/stop": {"body": {}},
            },
            confirm=False,
        )
        assert len(out["confirms"]) == 1
        assert out["requests"] == []


class TestStart:
    @pytest.mark.parametrize("entry", ["", "   ", "0", "-3", "4x", "abc", "1.5", "9007199254740993", "1" + "0" * 21])
    def test_an_invalid_entry_is_rejected_inline_with_no_request(self, entry: str) -> None:
        out = _run(
            f"""
            phone.start.init();
            await submitStart({json.dumps(entry)});
            return html('phone-start-status');
            """,
            {},
        )
        assert "Enter an issue number" in out["result"]
        assert out["requests"] == []

    @pytest.mark.parametrize(("entry", "issue"), [("42", "42"), (" #42 ", "42"), ("007", "7")])
    def test_a_valid_entry_starts_the_issue_with_the_default_role(self, entry: str, issue: str) -> None:
        out = _run(
            f"""
            phone.start.init();
            await submitStart({json.dumps(entry)});
            return {{ status: html('phone-start-status'), field: byId('phone-start-issue').value }};
            """,
            {"POST /agents/start": {"body": {"status": "started"}}, "GET /agents/active": {"body": {"agents": []}}},
        )
        starts = [r for r in out["requests"] if r["path"] == "/agents/start"]
        assert [r["body"] for r in starts] == [{"issue": issue}]
        assert f"Started #{issue}" in out["result"]["status"]
        assert out["result"]["field"] == ""
        assert any(r["path"] == "/agents/active" for r in out["requests"])
        _assert_csrf_on_writes_only(out["requests"])

    def test_a_refusal_is_shown_verbatim_and_keeps_the_entry(self) -> None:
        out = _run(
            """
            phone.start.init();
            await submitStart('42');
            return { status: html('phone-start-status'), field: byId('phone-start-issue').value };
            """,
            {
                "POST /agents/start": {
                    "status": 409,
                    "body": {"detail": {"code": "budget_exceeded", "message": "Over budget"}},
                },
                "GET /agents/active": {"body": {"agents": []}},
            },
        )["result"]
        assert "Over budget" in out["status"]
        assert out["field"] == "42"
