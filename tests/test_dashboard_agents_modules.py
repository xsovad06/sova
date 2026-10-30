"""Source-level guards for the shared client helpers and the shared state display (#1210).

There is no browser or JS test harness here, so these assert on the shipped source: that app.js
defines each shared helper once, that no inline script calls one before app.js has loaded, and
that state labels and colors come from `work_state.py` instead of per-page tables. A missing
`node` skips locally but fails under CI.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sova.adapters.base import Task, TaskState
from sova.dashboard.services import pr_service, work_state
from sova.dashboard.services.work_state import WorkItemState, state_color, state_label

DASHBOARD_DIR = Path(__file__).resolve().parents[1] / "sova" / "dashboard"
_STATIC = DASHBOARD_DIR / "static"
_TEMPLATES = DASHBOARD_DIR / "templates"
_APP_JS = (_STATIC / "app.js").read_text(encoding="utf-8")

_APP_JS_HELPER_CALL = re.compile(r"\b(?:escapeHtml|timeAgo|esc|statusBadge|setText|renderSectionError)\(")


def _require_node(reason: str) -> None:
    if shutil.which("node") is not None:
        return
    if os.environ.get("CI"):
        pytest.fail(f"node is required in CI: {reason}")
    pytest.skip(reason)


class TestStateDisplayHasOneSource:
    def test_work_item_states_all_have_a_label_and_a_token(self) -> None:
        for state in WorkItemState:
            assert state_label(state) == work_state._STATE_LABELS[state]
            assert state_color(state) == work_state._STATE_COLORS[state]

    def test_pr_and_tracker_states_resolve(self) -> None:
        assert state_label("approved_ci_green") == "Ready to Merge"
        assert state_color("approved_ci_green") == "green-strong"
        assert state_label("in_review") == "In Review"
        assert state_color("in_review") == "green"
        assert state_label("on_qa") == "On QA"
        assert state_color("on_qa") == "peach"

    def test_ready_to_merge_is_one_color_on_every_surface(self) -> None:
        """The PR tracker row (a PR state) and the task card's PR badge (a work item state) share a shade."""
        assert state_color("approved_ci_green") == state_color(WorkItemState.PR_READY_TO_MERGE)
        assert state_label("approved_ci_green") == state_label(WorkItemState.PR_READY_TO_MERGE)

    def test_approved_and_ready_to_merge_stay_apart(self) -> None:
        """An approved PR with CI not green must not look like one that can be merged."""
        assert state_color("approved") != state_color("approved_ci_green")
        assert (state_label("approved"), state_label("approved_ci_green")) == ("Approved", "Ready to Merge")

    @staticmethod
    def _canvas_hex(tokens: list[str]) -> list[str]:
        """stateHex() for each token, with SOVA_COLORS built by app.js's own initColors() from style.css values."""
        _require_node("node is required to run stateHex")
        css = (_STATIC / "style.css").read_text(encoding="utf-8")
        variables = dict(re.findall(r"(--ctp-[\w-]+):\s*(#[0-9a-fA-F]{6})", css))
        init = _APP_JS[
            _APP_JS.index("function initColors(") : _APP_JS.index("\n}\n", _APP_JS.index("function initColors("))
        ]
        hex_keys = _APP_JS[
            _APP_JS.index("var _STATE_HEX_KEYS") : _APP_JS.index("\n}\n", _APP_JS.index("function stateHex("))
        ]
        script = (
            f"globalThis.window = globalThis; const vars = {json.dumps(variables)};"
            "\nfunction getCSSVar(name) { return vars[name] || ''; }"
            f"\n{init}\n}}\n{hex_keys}\n}}\ninitColors();"
            f"\nconsole.log(JSON.stringify({json.dumps(tokens)}.map(stateHex)));"
        )
        proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30, check=False)
        assert proc.returncode == 0, proc.stderr
        return json.loads(proc.stdout)

    def test_states_that_must_be_told_apart_on_the_graph_get_different_canvas_colors(self) -> None:
        """The graph and its legend draw tokens as hex, so two tokens sharing one hex merge two states."""
        in_review, done = (state_color(s) for s in ("in_review", "done"))
        approved, ready = (state_color(s) for s in ("approved", "approved_ci_green"))
        first = self._canvas_hex([in_review, done])
        second = self._canvas_hex([approved, ready])
        assert first[0] != first[1], "a node still in review must not look finished"
        assert second[0] != second[1], "an approved PR must not look mergeable"

    def test_every_color_token_work_state_emits_has_its_own_canvas_hex(self) -> None:
        tokens = sorted(
            {
                *work_state._STATE_COLORS.values(),
                *(color for _, color in work_state._PR_STATE_META.values()),
                *(color for _, color in work_state._TASK_STATE_META.values()),
            }
        )
        hexes = self._canvas_hex(tokens)
        assert all(re.fullmatch(r"#[0-9a-fA-F]{6}", h) for h in hexes), dict(zip(tokens, hexes))
        assert len(set(hexes)) == len(tokens), dict(zip(tokens, hexes))

    def test_lifecycle_neighbours_do_not_share_a_color(self) -> None:
        assert len({state_color("in_progress"), state_color("in_review"), state_color("on_qa")}) == 3

    def test_no_label_is_drawn_in_two_colors(self) -> None:
        """ "In Review" is the tracker state and "Review Addressed" the PR state: one label, one color."""
        by_label: dict[str, set[str]] = {}
        for state in (*WorkItemState, *work_state._PR_STATE_META, *work_state._TASK_STATE_META):
            value = state.value if isinstance(state, WorkItemState) else state
            by_label.setdefault(state_label(value), set()).add(state_color(value))
        assert {label: colors for label, colors in by_label.items() if len(colors) > 1} == {}

    @pytest.mark.parametrize(
        ("pr_state", "work_item_state"),
        [
            ("draft", WorkItemState.PR_DRAFT),
            ("conflicted", WorkItemState.PR_CONFLICTED),
            ("ci_running", WorkItemState.PR_CI_RUNNING),
            ("ci_failed", WorkItemState.PR_CI_FAILED),
            ("changes_requested", WorkItemState.PR_EXTERNAL_CHANGES),
            ("approved_ci_green", WorkItemState.PR_READY_TO_MERGE),
            ("review_addressed", WorkItemState.PR_REVIEW_ADDRESSED),
            ("awaiting_review", WorkItemState.PR_AWAITING_REVIEW),
        ],
    )
    def test_pr_state_color_is_read_from_its_work_item_state(
        self, pr_state: str, work_item_state: WorkItemState
    ) -> None:
        assert state_color(pr_state) == state_color(work_item_state)

    @pytest.mark.parametrize(("pr_state", "work_item_state"), sorted(work_state._PR_STATE_WORK_ITEM.items()))
    def test_pr_state_label_is_read_from_its_work_item_state(
        self, pr_state: str, work_item_state: WorkItemState
    ) -> None:
        """The PR row and the task card showing that PR say the same thing."""
        assert state_label(pr_state) == state_label(work_item_state)

    def test_every_computed_pr_state_is_registered(self) -> None:
        for member in pr_service.ComputedPRState:
            assert member.value in work_state._PR_STATE_META, member

    def test_every_tracker_state_is_registered(self) -> None:
        for member in TaskState:
            assert work_state._state_meta(member.value) is not None, member

    def test_tracker_table_holds_exactly_the_states_work_item_state_lacks(self) -> None:
        """A tracker-only entry for a state the adapters never emit would read as if it flowed through the API."""
        lacking = {member.value for member in TaskState} - {member.value for member in WorkItemState}
        assert set(work_state._TASK_STATE_META) == lacking

    def test_unknown_state_is_visible_not_blank(self) -> None:
        assert state_label("dependency_graph_pseudo_state") == "Dependency Graph Pseudo State"
        assert state_color("dependency_graph_pseudo_state") == "gray"
        assert state_label("") == "Unknown"
        assert state_color("") == "gray"

    def test_unregistered_state_is_logged_once(self) -> None:
        import structlog

        work_state._reported_unknown_states.discard("brand_new_state")
        with structlog.testing.capture_logs() as logs:
            for _ in range(3):
                state_label("brand_new_state")
                state_color("brand_new_state")
        events = [entry for entry in logs if entry["event"] == "work_state.unregistered_state"]
        assert [entry["state"] for entry in events] == ["brand_new_state"]

    def test_empty_state_is_not_reported_as_unregistered(self) -> None:
        import structlog

        with structlog.testing.capture_logs() as logs:
            state_label("")
            state_color("")
        assert not [entry for entry in logs if entry["event"] == "work_state.unregistered_state"]

    def test_every_emitted_token_is_a_client_badge_class(self) -> None:
        match = re.search(r"var STATE_BADGE_CLASSES = \{(.*?)\n\};", _APP_JS, re.DOTALL)
        assert match
        client_tokens = set(re.findall(r"^\s*'([\w-]+)':", match.group(1), re.MULTILINE))
        emitted = set(work_state._STATE_COLORS.values())
        emitted |= {color for _, color in work_state._PR_STATE_META.values()}
        emitted |= {color for _, color in work_state._TASK_STATE_META.values()}
        emitted.add(work_state._FALLBACK_COLOR)
        assert emitted <= client_tokens, sorted(emitted - client_tokens)

    def test_every_token_resolves_to_a_hex_key(self) -> None:
        match = re.search(r"var _STATE_HEX_KEYS = \{(.*?)\n\};", _APP_JS, re.DOTALL)
        assert match
        hex_tokens = set(re.findall(r"'([\w-]+)':", match.group(1)))
        badge = re.search(r"var STATE_BADGE_CLASSES = \{(.*?)\n\};", _APP_JS, re.DOTALL)
        assert badge
        assert set(re.findall(r"^\s*'([\w-]+)':", badge.group(1), re.MULTILINE)) <= hex_tokens

    def test_pr_service_keeps_no_label_table_of_its_own(self) -> None:
        assert not hasattr(pr_service, "_STATE_LABELS")

    def test_client_keeps_no_state_color_tables(self) -> None:
        for name in ("PR_STATE_COLORS", "ISSUE_STATE_COLORS", "prStateBadgeColor", "issueStateColor"):
            assert name not in _APP_JS
        supervisor = (_TEMPLATES / "supervisor.html").read_text(encoding="utf-8")
        assert "function stateColor(" not in supervisor
        assert "function prStateColor(" not in supervisor
        assert "stateLabels" not in (_TEMPLATES / "queue.html").read_text(encoding="utf-8")


class TestStatePayloads:
    def test_pr_row_carries_label_and_color(self) -> None:
        computed = pr_service.ComputedPRState.CI_FAILED.value
        assert state_label(computed) == "CI Failed"
        assert state_color(computed) == "red"

    def test_graph_nodes_are_enriched(self) -> None:
        from sova.dashboard.routers.dependencies import _enrich_with_state_display

        graph = {
            "nodes": [
                {"id": 1, "state": "in_progress", "pr_state": "conflicted"},
                {"id": 2, "state": "closed"},
                {"id": 3, "state": "never_seen_before"},
            ]
        }
        _enrich_with_state_display(graph)
        first, second, third = graph["nodes"]
        assert (first["state_label"], first["state_color"]) == ("In Progress", "blue")
        assert (second["state_label"], second["state_color"]) == ("Closed", "gray")
        assert (third["state_label"], third["state_color"]) == ("Never Seen Before", "gray")

    def test_triaged_node_is_not_drawn_in_an_awaiting_color(self) -> None:
        """Graph dots come from the work item palette, and the legend no longer hard-codes a second meaning."""
        from sova.dashboard.routers.dependencies import _enrich_with_state_display

        graph = {"nodes": [{"id": 1, "state": "triaged"}, {"id": 2, "state": "researched"}]}
        _enrich_with_state_display(graph)
        triaged, researched = graph["nodes"]
        assert (triaged["state_label"], triaged["state_color"]) == ("Triaged", "yellow")
        assert (researched["state_label"], researched["state_color"]) == ("Researched", "purple")

    async def test_graph_pr_label_and_color_come_from_the_same_state(self) -> None:
        """A review_addressed PR reads "Review Addressed" in lavender, as on the Agents page, not the collapsed blue."""
        from sova.dashboard.routers.dependencies import _fetch_pr_map

        prs = [
            {
                "number": 42,
                "url": "u",
                "computed_state": "review_addressed",
                "state_label": "Stale Label",
                "linked_issues": [10],
            },
            {"number": 43, "url": "u", "computed_state": "approved", "linked_issues": [20]},
        ]
        with patch("sova.dashboard.services.pr_service.list_open_prs_with_state", return_value=prs):
            result = await _fetch_pr_map()

        assert result[10]["pr_state"] == "awaiting_review"
        assert result[10]["pr_state_label"] == state_label("review_addressed") == "Review Addressed"
        assert result[10]["pr_state_color"] == state_color("review_addressed") == "lavender"
        assert result[20]["pr_state_label"] == "Approved"
        assert result[20]["pr_state_color"] == state_color("approved")

    async def test_task_issue_list_carries_label_and_color(self) -> None:
        from sova.dashboard.routers import tasks as tasks_router

        adapter = MagicMock()
        adapter.list_tasks = AsyncMock(
            return_value=[
                Task(id="7", title="t", state=TaskState.TRIAGED),
                Task(id="8", title="u", state=TaskState.IN_REVIEW),
            ]
        )
        with (
            patch.dict(tasks_router._issues_cache, clear=True),
            patch("sova.config.loader.load_config"),
            patch("sova.adapters.create_adapter", return_value=adapter),
        ):
            payload = await tasks_router.list_issues()

        triaged, in_review = payload["issues"]
        assert (triaged["state"], triaged["state_label"], triaged["state_color"]) == ("triaged", "Triaged", "yellow")
        assert (in_review["state_label"], in_review["state_color"]) == ("In Review", "green")

    def test_work_item_pr_details_carry_a_color(self) -> None:
        from sova.dashboard.services.work_item_service import _format_pr_details

        details = _format_pr_details({"number": 7, "computed_state": "approved", "state_label": "Approved"})
        assert details["state_color"] == "green"

    def test_work_item_pr_details_without_a_computed_state_show_no_label(self) -> None:
        """"Unknown" beside a PR with no state would read as a real state; the pre-existing blank is kept."""
        from sova.dashboard.services.work_item_service import _format_pr_details

        details = _format_pr_details({"number": 7})
        assert (details["computed_state"], details["state_label"], details["state_color"]) == ("", "", "")

    def test_work_item_pr_details_label_and_color_share_one_source(self) -> None:
        """A PR dict whose own state_label is missing or stale cannot pair a blank label with a valid color."""
        from sova.dashboard.services.work_item_service import _format_pr_details

        for pr in (
            {"number": 7, "computed_state": "review_addressed"},
            {"number": 7, "computed_state": "review_addressed", "state_label": "Stale"},
        ):
            details = _format_pr_details(pr)
            assert details["state_label"] == "Review Addressed"
            assert details["state_color"] == "lavender"


class TestSharedHelpersHaveOneDefinition:
    @pytest.mark.parametrize("name", ["escapeHtml", "timeAgo", "setText", "renderSectionError", "statusBadge"])
    def test_defined_once_in_app_js_and_nowhere_else(self, name: str) -> None:
        assert len(re.findall(rf"^function {name}\(", _APP_JS, re.MULTILINE)) == 1
        for path in [*_TEMPLATES.glob("*.html"), *_STATIC.glob("*.js"), *(_STATIC / "js").rglob("*.js")]:
            if path.name == "app.js":
                continue
            source = path.read_text(encoding="utf-8")
            assert not re.search(rf"function {name}\(", source), f"{path.name} redefines {name}"

    def test_esc_alias_is_declared_once_in_app_js(self) -> None:
        assert len(re.findall(r"^var esc = escapeHtml;$", _APP_JS, re.MULTILINE)) == 1
        for path in [*_TEMPLATES.glob("*.html"), *_STATIC.glob("*.js"), *(_STATIC / "js").rglob("*.js")]:
            if path.name == "app.js":
                continue
            source = path.read_text(encoding="utf-8")
            assert not re.search(r"\b(?:const|let|var)\s+esc\b", source), f"{path.name} redeclares esc"
            assert not re.search(r"function\s+esc\b", source), f"{path.name} redefines esc"

    @pytest.mark.parametrize(
        "name", ["esc", "escapeHtml", "timeAgo", "setText", "renderSectionError", "statusBadge", "safeStorageGet"]
    )
    def test_no_script_redeclares_an_app_js_global_lexically(self, name: str) -> None:
        """app.js is a classic script, so its top-level names are globals. A `const`/`let`/`class` of the same
        name in another classic script or inline template script is a redeclaration SyntaxError that stops that
        whole script, and `node --check` looks at one file at a time so it never sees the clash."""
        pattern = re.compile(rf"(?:^|[;{{}}\s])(?:const|let|class)\s+{name}\b")
        for path in [*_TEMPLATES.glob("*.html"), *_STATIC.glob("*.js"), *(_STATIC / "js").rglob("*.js")]:
            if path.name == "app.js":
                continue
            assert not pattern.search(path.read_text(encoding="utf-8")), f"{path.name} redeclares {name}"

    def test_time_ago_reads_a_future_timestamp_as_just_now(self) -> None:
        """Clock skew between server and browser: queue.html's former private copy showed nothing here."""
        _require_node("node is required to run the dashboard helper")
        start = _APP_JS.index("function timeAgo(")
        fn = _APP_JS[start : _APP_JS.index("\n}\n", start) + 2]
        script = (
            fn + "\nconst future = new Date(Date.now() + 60000).toISOString();"
            "\nconsole.log(JSON.stringify([timeAgo(future), timeAgo(future, '-'), timeAgo(null, '-'), timeAgo('x')]));"
        )
        proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30, check=False)
        assert proc.returncode == 0, proc.stderr
        assert json.loads(proc.stdout) == ["just now", "just now", "-", ""]

    def test_status_badge_returns_escaped_html_with_a_neutral_fallback(self) -> None:
        _require_node("node is required to run the dashboard helper")
        start = _APP_JS.index("var STATUS_BADGE_CLASSES")
        end = _APP_JS.index("function setText(")
        script = (
            _APP_JS[
                _APP_JS.index("function escapeHtml(") : _APP_JS.index("\n}\n", _APP_JS.index("function escapeHtml("))
                + 2
            ]
            + _APP_JS[start:end]
            + "\nconsole.log(JSON.stringify([statusBadge('done'), statusBadge('<b>'), statusBadge('x', {x: 'c1'}),"
            " statusBadge('constructor')]));"
        )
        proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30, check=False)
        assert proc.returncode == 0, proc.stderr
        done, unknown, mapped, inherited = json.loads(proc.stdout)
        assert "bg-accent-green/15 text-accent-green" in done
        assert unknown == '<span class="text-xs px-2 py-0.5 rounded bg-gray-700/30 text-gray-400">&lt;b&gt;</span>'
        assert 'rounded c1"' in mapped
        assert "bg-gray-700/30" in inherited, "an Object.prototype name must not pick up an inherited class"

    def test_spec_page_status_setter_is_not_a_second_status_badge(self) -> None:
        spec = (_TEMPLATES / "spec.html").read_text(encoding="utf-8")
        assert "function applySpecStatusBadge(" in spec
        assert "statusBadge(" not in spec.replace("applySpecStatusBadge(", "")

    def test_head_partial_no_longer_defines_escape_html(self) -> None:
        assert "function escapeHtml" not in (_TEMPLATES / "_head.html").read_text(encoding="utf-8")

    def test_every_page_with_the_head_partial_loads_app_js(self) -> None:
        for path in _TEMPLATES.glob("*.html"):
            source = path.read_text(encoding="utf-8")
            if '{% include "_head.html" %}' in source:
                assert "/static/app.js" in source, path.name

    @pytest.mark.parametrize(
        "path", sorted(p for p in _TEMPLATES.glob("*.html") if not p.name.startswith("_")), ids=lambda p: p.name
    )
    def test_no_inline_script_uses_an_app_js_helper_before_app_js_runs(self, path: Path) -> None:
        """escapeHtml and friends live in app.js, which loads last: an inline script ahead of it would
        call an undefined function (a page extending base.html is safe only inside its scripts block).
        Partials are skipped: `_head.html` only references escapeHtml inside a function body that runs
        after every script has loaded."""
        source = path.read_text(encoding="utf-8")
        if "{% extends" in source:
            marker = source.find("{% block scripts %}")
            early = source if marker == -1 else source[:marker]
        else:
            app = source.find("/static/app.js")
            early = source if app == -1 else source[:app]
        for script in re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", early, re.DOTALL):
            assert not _APP_JS_HELPER_CALL.search(script), (
                f"{path.name}: inline script calls an app.js helper too early"
            )

    def test_escape_html_escapes_quotes_and_tolerates_non_strings(self) -> None:
        _require_node("node is required to run the dashboard helper")
        start = _APP_JS.index("function escapeHtml(")
        fn = _APP_JS[start : _APP_JS.index("\n}\n", start) + 2]
        script = fn + '\nconsole.log(JSON.stringify([escapeHtml(`<a href="x">\'`), escapeHtml(0), escapeHtml(null)]));'
        proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30, check=False)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == '["&lt;a href=&quot;x&quot;&gt;&#39;","0",""]'


class TestSupervisorGraphLegend:
    """The graph legend is built from the labels and tokens the API attaches to nodes, not a second palette."""

    _SOURCE = (_TEMPLATES / "supervisor.html").read_text(encoding="utf-8")

    def _legend_block(self) -> str:
        start = self._SOURCE.index("Dependency Graph <span")
        return self._SOURCE[start : self._SOURCE.index('id="hide-done-btn"', start)]

    def test_state_entries_are_rendered_from_node_tokens(self) -> None:
        assert 'id="graph-state-legend"' in self._legend_block()
        assert "function renderStateLegend(" in self._SOURCE
        assert "stateHex(token)" in self._SOURCE
        assert "renderStateLegend(visibleNodes);" in self._SOURCE

    def test_graph_key_includes_the_display_fields_the_renderer_reads(self) -> None:
        """pr_state is collapsed (review_addressed reads awaiting_review, approved reads approved_ci_green),
        but the dot and the drawer draw the raw state's color and label, so the key must change with them."""
        key = self._SOURCE[self._SOURCE.index("function _computeGraphKey(") :]
        key = key[: key.index("\n}\n")]
        for field in ("n.pr_state_color", "n.pr_state_label", "n.state_color"):
            assert field in key, field

    def test_graph_key_changes_when_only_the_raw_pr_state_changes(self) -> None:
        _require_node("node is required to run the graph key")
        start = self._SOURCE.index("function _computeGraphKey(")
        fn = self._SOURCE[start : self._SOURCE.index("\n}\n", start) + 2]
        node = {"id": 1, "state": "in_review", "state_color": "green", "pr_state": "awaiting_review"}
        before = {**node, "pr_state_color": "blue", "pr_state_label": "Awaiting Review"}
        after = {**node, "pr_state_color": "lavender", "pr_state_label": "Review Addressed"}
        script = (
            fn + f"\nconst a = {json.dumps({'nodes': [before], 'edges': []})};"
            f"\nconst b = {json.dumps({'nodes': [after], 'edges': []})};"
            "\nconsole.log(JSON.stringify([_computeGraphKey(a) === _computeGraphKey(b),"
            " _computeGraphKey(a) === _computeGraphKey(a)]));"
        )
        proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30, check=False)
        assert proc.returncode == 0, proc.stderr
        assert json.loads(proc.stdout) == [False, True]

    def test_legend_does_not_hard_code_a_state_meaning(self) -> None:
        """A static "Awaiting" yellow dot is what made an ordinary triaged node read as waiting for approval."""
        assert "Awaiting" not in self._legend_block()

    def test_legend_has_no_hex_color_literal(self) -> None:
        legend = self._legend_block()
        assert not re.search(r"#[0-9a-fA-F]{6}", legend)
        assert 'style="background:#' not in legend

    def test_node_renderer_reads_state_colors_from_the_shared_palette(self) -> None:
        """Human, Interactive, Queued and the run-status dots are drawn in colors the legend also shows, so they
        come from stateHex()/SOVA_COLORS like the legend's. Priority colors and the milestone lane palette are
        not state colors and keep their literals; a state hex anywhere else fails here."""
        state_hexes = ("#f38ba8", "#fab387", "#a6e3a1", "#74c7ec")
        not_state = re.compile(r"^\s*(?:blocker|critical|high|medium|low|undefined):\s*\{ color: |^\s*\{ bg: '#")
        offenders = [
            line.strip()
            for line in self._SOURCE.splitlines()
            if any(h in line.lower() for h in state_hexes) and not not_state.match(line)
        ]
        assert offenders == []

    def test_pulse_keyframes_use_theme_variables(self) -> None:
        keyframes = self._SOURCE[self._SOURCE.index("@keyframes graph-node-pulse") :]
        keyframes = keyframes[: keyframes.index("}\n.graph-node-highlight")]
        assert not re.search(r"#[0-9a-fA-F]{6}", keyframes)
