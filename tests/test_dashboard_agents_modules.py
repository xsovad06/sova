"""Source-level guards for the shared dashboard client helpers (#1210).

There is no browser or JS test harness here, so these assert on the shipped source: that app.js
defines each shared helper once, that no page redefines it, and that no inline script calls one
before app.js has loaded. A missing `node` skips locally but fails under CI.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

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
