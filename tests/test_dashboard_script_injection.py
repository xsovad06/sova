"""Untrusted text must reach the dashboard's pages as text, never as markup or code.

Markdown first. marked passes raw inline HTML through untouched, so its output is only
safe to assign to innerHTML after DOMPurify sanitizes it. `_head.html` owns that rule in
two shared helpers, `renderMarkdown()` and `renderMarkdownInline()`, both of which fall
back to escaping the text when either library failed to load from its CDN.

The spec page once called `marked.parseInline()` itself for open-question text, which
comes from an LLM-written spec built from issue content, so a question carrying an
`<img onerror>` reached innerHTML unsanitized. These tests run the real helpers and the
real `renderQuestions()` under node with stand-in libraries, plus a source guard that
keeps every marked call inside the shared helpers.

Then the spec page's issue number, a free-form path segment written into its script.
Quoting it by hand left a backslash free to escape the closing quote; `tojson` emits a
string literal that holds exactly the path value.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import quote

import pytest
from httpx import ASGITransport, AsyncClient

from sova.dashboard.app import create_app

_DASHBOARD = Path(__file__).resolve().parents[1] / "sova" / "dashboard"
_HEAD = _DASHBOARD / "templates" / "_head.html"
_SPEC = _DASHBOARD / "templates" / "spec.html"

_PAYLOAD = "<img src=x onerror=alert(1)>"
_QUESTION = "Should `retry` cover this? " + _PAYLOAD

# Runs _head.html's inline script and spec.html's renderQuestions() in a vm sandbox.
# The stand-in marked passes inline HTML through as the real one does; the stand-in
# DOMPurify records what it was given and strips <img> tags.
_HARNESS = r"""
const vm = require('vm');
const scenario = JSON.parse(process.argv[2]);
const sanitized = [];
const elements = {
  'questions-section': { classList: { add() {}, remove() {} } },
  'questions-list': { innerHTML: '' },
};
const ctx = {
  console: { log() {}, warn() {}, error() {} },
  document: { getElementById: (id) => elements[id] || null },
};
if (scenario.marked) {
  ctx.marked = {
    use() {},
    parse: (text) => '<p>' + text + '</p>',
    parseInline: (text) => text,
  };
}
if (scenario.purify) {
  ctx.DOMPurify = {
    sanitize(html) { sanitized.push(html); return html.replace(/<img\b[^>]*>/gi, ''); },
  };
}
ctx.window = ctx;
vm.createContext(ctx);
vm.runInContext(scenario.head, ctx);
vm.runInContext(scenario.page, ctx);
const results = scenario.calls.map(([name, arg]) => ctx[name](arg));
process.stdout.write(JSON.stringify({
  results, sanitized, listHtml: elements['questions-list'].innerHTML,
}));
"""


def _head_script() -> str:
    """The inline script of _head.html (the CDN tags carry a src and no body)."""
    return re.search(r"<script>(.*?)</script>", _HEAD.read_text(encoding="utf-8"), re.DOTALL).group(1)


def _render_questions_source() -> str:
    """renderQuestions() from spec.html; the rest of that script holds Jinja markup."""
    source = _SPEC.read_text(encoding="utf-8")
    start = source.index("function renderQuestions(questions) {")
    return source[start : source.index("\n}\n", start) + 2]


def _run(tmp_path: Path, *, marked: bool, purify: bool, calls: list[list[object]]) -> dict:
    if shutil.which("node") is None:
        pytest.skip("node is required to run the dashboard template scripts")
    harness = tmp_path / "harness.js"
    harness.write_text(_HARNESS, encoding="utf-8")
    scenario = {
        "head": _head_script(),
        "page": _render_questions_source(),
        "marked": marked,
        "purify": purify,
        "calls": calls,
    }
    proc = subprocess.run(
        ["node", str(harness), json.dumps(scenario)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _render_question(tmp_path: Path, *, marked: bool, purify: bool) -> dict:
    question = {"id": 1, "text": _QUESTION, "answer": ""}
    return _run(tmp_path, marked=marked, purify=purify, calls=[["renderQuestions", [question]]])


class TestSpecQuestionRendering:
    def test_question_markdown_is_sanitized_before_innerhtml(self, tmp_path: Path) -> None:
        out = _render_question(tmp_path, marked=True, purify=True)

        assert any(_PAYLOAD in html for html in out["sanitized"]), "question HTML never reached DOMPurify"
        assert "onerror" not in out["listHtml"]
        assert "Should `retry` cover this?" in out["listHtml"]

    @pytest.mark.parametrize(("marked", "purify"), [(True, False), (False, True)], ids=["no-dompurify", "no-marked"])
    def test_question_text_is_escaped_when_a_library_failed_to_load(
        self, tmp_path: Path, marked: bool, purify: bool
    ) -> None:
        out = _render_question(tmp_path, marked=marked, purify=purify)

        assert "<img" not in out["listHtml"]
        assert "&lt;img src=x onerror=alert(1)&gt;" in out["listHtml"]


class TestSharedMarkdownHelpers:
    @pytest.mark.parametrize("helper", ["renderMarkdown", "renderMarkdownInline"])
    @pytest.mark.parametrize(
        ("marked", "purify"),
        [(True, True), (True, False), (False, True), (False, False)],
        ids=["both-loaded", "no-dompurify", "no-marked", "neither"],
    )
    def test_raw_html_never_survives(self, tmp_path: Path, helper: str, marked: bool, purify: bool) -> None:
        out = _run(tmp_path, marked=marked, purify=purify, calls=[[helper, _QUESTION]])

        assert "<img" not in out["results"][0]


class TestMarkedCallSites:
    def test_only_the_shared_helpers_call_marked(self) -> None:
        """A page calling marked itself skips DOMPurify, which is how spec.html leaked."""
        call = re.compile(r"\bmarked\.(?:parse|parseInline)\(")
        sources = [*sorted((_DASHBOARD / "templates").glob("*.html")), *sorted((_DASHBOARD / "static").rglob("*.js"))]
        offenders = []
        head_calls = 0
        for path in sources:
            if path.name.endswith(".min.js"):
                continue
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                if not call.search(line):
                    continue
                if path == _HEAD:
                    head_calls += 1
                else:
                    offenders.append(f"{path.relative_to(_DASHBOARD)}:{lineno}")

        assert offenders == [], f"call renderMarkdown()/renderMarkdownInline() instead: {offenders}"
        assert head_calls == 2, "expected exactly the two shared helpers in _head.html to call marked"


class TestSpecIssueNumberLiteral:
    @pytest.mark.parametrize("issue_number", ["42", "RHCLOUD-123", "7\\';x", '7"<b>x'])
    async def test_issue_number_is_a_string_literal_holding_the_path_value(self, issue_number: str) -> None:
        app = create_app(multi_project=False)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/spec/" + quote(issue_number, safe=""))

        assert resp.status_code == 200
        line = next(line for line in resp.text.splitlines() if "var _issueNumber = " in line)
        literal = line.split("var _issueNumber = ", 1)[1].strip().removesuffix(";")
        assert json.loads(literal) == issue_number
        assert "<" not in literal
