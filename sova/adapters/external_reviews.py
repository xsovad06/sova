"""External review tool integrations (SonarCloud, CodeRabbit).

Fetches findings from external review tools that post on GitHub PRs,
and provides utilities to resolve review threads after fixes.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from urllib.parse import quote

from sova.llm.payload import trim_payload
from sova.utils.logging import get_logger
from sova.utils.markdown import mask_fenced_blocks
from sova.utils.shell import run

log = get_logger(component="adapters.external_reviews")

_CHECK_NAMES: dict[str, str] = {
    "sonarcloud": "SonarCloud Code Analysis",
    "coderabbit": "CodeRabbit",
}

# Shared by both prompt builders below so the two can never describe the commit
# scope rule differently. Worded project-neutrally: this module builds a prompt
# for an agent working inside whatever project SOVA is installed into, which
# may have no invariants/commit-format.sh (or any file by that name) and no
# sova/git or sova/utils directories of its own, so naming this repository's
# own scope list or examples here would point the agent at conventions that
# don't exist in its actual target project.
_COMMIT_SCOPE_GUIDANCE = (
    "<scope> must match this repository's own commit-scope convention: follow any "
    "commitlint config, pre-push hook, or contributor guide it ships, and otherwise "
    "mirror the scope used by recent commits touching the same area"
)


@dataclass
class ExternalFinding:
    """A single finding from an external review tool."""

    source: str
    file_path: str
    line: int | None
    severity: str
    message: str
    tool_id: str = ""


@dataclass
class ExternalCheckStatus:
    """Status of an external review check on a PR."""

    name: str
    completed: bool
    passed: bool


async def get_check_statuses(
    pr_number: int,
    *,
    repo: str,
    tools: list[str],
    github_user: str | None = None,
) -> list[ExternalCheckStatus]:
    """Get the status of external review checks on a PR."""
    from sova.utils.gh import resolve_gh_env

    env = await resolve_gh_env(github_user)
    result = await run(
        "gh",
        "pr",
        "view",
        str(pr_number),
        "--repo",
        repo,
        "--json",
        "statusCheckRollup",
        env=env,
    )
    if not result.success:
        log.warning("external_reviews.check_status_failed", stderr=result.stderr[:200])
        return []

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []

    checks = data.get("statusCheckRollup", [])
    statuses: list[ExternalCheckStatus] = []

    for tool in tools:
        expected_name = _CHECK_NAMES.get(tool, tool)
        for check in checks:
            name = check.get("context") or check.get("name") or ""
            if name == expected_name:
                state = check.get("state") or check.get("status") or ""
                conclusion = check.get("conclusion") or ""
                completed = state.upper() in ("SUCCESS", "FAILURE", "ERROR", "COMPLETED")
                passed = state.upper() == "SUCCESS" or conclusion.upper() == "SUCCESS"
                statuses.append(ExternalCheckStatus(name=expected_name, completed=completed, passed=passed))
                break
        else:
            statuses.append(ExternalCheckStatus(name=expected_name, completed=False, passed=False))

    return statuses


def _get_sonarcloud_auth_header() -> str:
    """Build Authorization header value from SONAR_TOKEN env var. Empty string if unset."""
    token = os.environ.get("SONAR_TOKEN", "")
    return f"Bearer {token}" if token else ""


async def _fetch_sonarcloud_json(url: str, auth_header: str, *, log_context: str) -> dict | None:
    """Execute a curl request to SonarCloud and parse JSON. Returns None on failure.

    Auth is passed via ``-H @-`` (stdin) so the token never appears in process
    argv or debug logs.
    """
    stdin_header = f"Authorization: {auth_header}" if auth_header else ""
    # -H @- reads the header value from stdin, keeping it out of argv/logs.
    auth_args = ["-H", "@-"] if auth_header else []
    result = await run("curl", "-sf", *auth_args, url, timeout=30, stdin=stdin_header)
    if not result.success:
        log.warning("external_reviews.sonarcloud_request_failed", context=log_context, stderr=result.stderr[:200])
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        log.warning("external_reviews.sonarcloud_parse_failed", context=log_context, exc_info=True)
        return None


def _parse_sonarcloud_issues(
    issues: list[dict], source: str, *, default_severity: str = "UNKNOWN"
) -> list[ExternalFinding]:
    """Parse SonarCloud issue dicts into ExternalFinding objects."""
    findings: list[ExternalFinding] = []
    for issue in issues:
        component = issue.get("component", "")
        file_path = component.split(":", 1)[1] if ":" in component else component
        findings.append(
            ExternalFinding(
                source=source,
                file_path=file_path,
                line=issue.get("line"),
                severity=issue.get("severity", default_severity),
                message=issue.get("message", ""),
                tool_id=issue.get("key", ""),
            )
        )
    return findings


async def fetch_sonarcloud_issues(
    project_key: str,
    pr_number: int,
) -> list[ExternalFinding]:
    """Fetch open issues from SonarCloud for a PR."""
    url = (
        f"https://sonarcloud.io/api/issues/search"
        f"?componentKeys={quote(project_key, safe='')}"
        f"&pullRequest={pr_number}"
        f"&resolved=false"
        f"&ps=500"
    )

    data = await _fetch_sonarcloud_json(url, _get_sonarcloud_auth_header(), log_context="sonarcloud_fetch")
    if data is None:
        return []

    findings = _parse_sonarcloud_issues(data.get("issues", []), source="sonarcloud")
    log.info("external_reviews.sonarcloud_fetched", count=len(findings), pr=pr_number)
    return findings


@dataclass
class CoverageReport:
    """Coverage analysis results from SonarCloud for a PR.

    Attributes:
        coverage_pct: Actual coverage percentage on new code.
        required_pct: Quality gate threshold percentage (configurable via
            ``sonarcloud.coverage_threshold`` in SOVA config, default 80.0).
        findings: File/line-level uncovered-code issues from SonarCloud API.
    """

    coverage_pct: Decimal
    required_pct: Decimal
    findings: list[ExternalFinding]


async def fetch_sonarcloud_coverage_issues(
    project_key: str,
    pr_number: int,
    *,
    required_pct: Decimal = Decimal("80.0"),
) -> CoverageReport | None:
    """Fetch coverage-specific data from SonarCloud for a PR.

    Queries the measures API for coverage metrics and the issues API for
    uncovered-line findings. Returns ``None`` when SonarCloud is not
    configured or unreachable.
    """
    if not project_key:
        return None

    auth_header = _get_sonarcloud_auth_header()
    if not auth_header:
        log.warning("external_reviews.sonarcloud_no_token")

    measures_url = (
        f"https://sonarcloud.io/api/measures/component"
        f"?component={quote(project_key, safe='')}"
        f"&pullRequest={pr_number}"
        f"&metricKeys=new_coverage"
    )
    issues_url = (
        f"https://sonarcloud.io/api/issues/search"
        f"?componentKeys={quote(project_key, safe='')}"
        f"&pullRequest={pr_number}"
        f"&resolved=false"
        f"&rules=common-python:InsufficientLineCoverage"
        f"&ps=500"
    )

    measures_data, issues_data = await asyncio.gather(
        _fetch_sonarcloud_json(measures_url, auth_header, log_context="sonarcloud_measures"),
        _fetch_sonarcloud_json(issues_url, auth_header, log_context="sonarcloud_coverage_issues"),
    )

    if measures_data is None:
        log.warning("external_reviews.sonarcloud_measures_unavailable", pr=pr_number)
        return None

    coverage_pct = Decimal("0")
    try:
        for measure in measures_data.get("component", {}).get("measures", []):
            if measure.get("metric") == "new_coverage":
                period = measure.get("period", {})
                coverage_pct = Decimal(str(period.get("value", 0)))
    except (ValueError, InvalidOperation):
        log.warning("external_reviews.sonarcloud_measures_parse_failed", exc_info=True)

    findings = _parse_sonarcloud_issues(
        issues_data.get("issues", []) if issues_data else [],
        source="sonarcloud-coverage",
        default_severity="MAJOR",
    )

    log.info(
        "external_reviews.sonarcloud_coverage",
        coverage_pct=coverage_pct,
        required_pct=required_pct,
        findings=len(findings),
        pr=pr_number,
    )
    return CoverageReport(
        coverage_pct=coverage_pct,
        required_pct=required_pct,
        findings=findings,
    )


def format_coverage_findings_for_prompt(
    report: CoverageReport,
    *,
    diff_files: list[str] | None = None,
) -> str:
    """Format coverage gap data into a test-writing prompt for the LLM agent."""
    lines: list[str] = []
    lines.append(
        f"SonarCloud reports {report.coverage_pct:.1f}% coverage on new code "
        f"(required >= {report.required_pct:.1f}%). "
        f"Write tests to close this coverage gap.\n\n"
    )

    if report.findings:
        lines.append("## Uncovered code locations\n\n")
        for f in report.findings:
            loc = f"{f.file_path}:{f.line}" if f.line else f.file_path
            lines.append(f"- {loc}: {f.message}\n")
        lines.append("\n")

    lines.append(
        "Instructions:\n"
        "1. Run `pytest --cov --cov-report=term-missing` to identify uncovered lines\n"
        "2. For each uncovered code path (especially `except` blocks and error branches):\n"
        "   - Write a pytest test that exercises the path\n"
        "   - Use `unittest.mock.patch` to trigger error conditions\n"
        "   - Name tests descriptively: `test_<function>_<scenario>`\n"
        "3. Place tests in the appropriate `tests/test_*.py` file\n"
        "4. Stage and commit with a message like 'test(<scope>): cover uncovered paths in <module>'. "
        f"{_COMMIT_SCOPE_GUIDANCE}\n"
        "5. Only add tests. Do NOT modify production code unless other findings also require it\n"
    )

    if diff_files:
        lines.append(f"\nFiles changed in this PR: {', '.join(diff_files)}\n")

    return "".join(lines)


_REVIEW_THREADS_QUERY = """
query($owner: String!, $name: String!, $pr: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $pr) {
      reviewThreads(first: 100, after: $cursor) {
        pageInfo {
          hasNextPage
          endCursor
        }
        nodes {
          id
          isResolved
          isOutdated
          path
          line
          comments(first: 1) {
            nodes {
              databaseId
              body
              author { login }
            }
          }
          lastComment: comments(last: 1) {
            nodes {
              body
            }
          }
        }
      }
    }
  }
}
"""

# A guard against a cursor that never advances; 100 threads per page makes this 10,000 threads.
_MAX_THREAD_PAGES = 100


def _review_threads_page_args(owner: str, name: str, pr_number: int, cursor: str | None) -> list[str]:
    """Build the ``gh api graphql`` argv for one page of review threads."""
    args = [
        "gh",
        "api",
        "graphql",
        "-f",
        f"query={_REVIEW_THREADS_QUERY}",
        "-F",
        f"owner={owner}",
        "-F",
        f"name={name}",
        "-F",
        f"pr={pr_number}",
    ]
    if cursor:
        # -f, not -F: -F type-converts its value (numbers, booleans, a leading "@" reads a file),
        # and an opaque cursor must reach GitHub byte for byte.
        args.extend(["-f", f"cursor={cursor}"])
    return args


def _parse_review_threads_page(stdout: str) -> dict | None:
    """Return the ``reviewThreads`` object of one GraphQL page, or None when the page is not a JSON object."""
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        log.warning("external_reviews.threads_parse_failed", exc_info=True)
        return None
    if not isinstance(data, dict):
        log.warning("external_reviews.threads_parse_failed", reason="not a JSON object")
        return None
    pull_request = ((data.get("data") or {}).get("repository") or {}).get("pullRequest")
    review_threads = pull_request.get("reviewThreads") if isinstance(pull_request, dict) else None
    if not isinstance(review_threads, dict):
        # A null repository or pullRequest (a deleted PR, a permission error on a later page) is not an
        # empty page: reading it as one would end pagination and report a partial list as complete.
        log.warning("external_reviews.threads_parse_failed", reason="no pullRequest.reviewThreads object")
        return None
    return review_threads


async def fetch_review_threads(repo: str, pr_number: int, *, github_user: str | None = None) -> list[dict] | None:
    """Fetch every review thread on a PR (resolved, unresolved and outdated), following pagination.

    Returns the raw GraphQL thread nodes, or None when a page could not be fetched or parsed: a partial list
    would let a caller conclude that a thread past the failed page does not exist, so a failure is never
    reported as a shorter list.
    """
    from sova.utils.gh import resolve_gh_env

    owner, name = repo.split("/", 1) if "/" in repo else ("", repo)
    env = await resolve_gh_env(github_user)

    threads: list[dict] = []
    cursor: str | None = None
    for _ in range(_MAX_THREAD_PAGES):
        result = await run(*_review_threads_page_args(owner, name, pr_number, cursor), env=env, timeout=30)
        if not result.success:
            log.warning("external_reviews.threads_fetch_failed", stderr=result.stderr[:200])
            return None
        review_threads = _parse_review_threads_page(result.stdout)
        if review_threads is None:
            return None
        threads.extend(review_threads.get("nodes") or [])
        page_info = review_threads.get("pageInfo") or {}
        cursor = page_info.get("endCursor")
        if not page_info.get("hasNextPage") or not cursor:
            return threads

    log.warning("external_reviews.threads_page_limit", pr=pr_number, pages=_MAX_THREAD_PAGES)
    return None


@dataclass
class _ThreadsResult:
    findings: list[ExternalFinding] = field(default_factory=list)
    thread_ids: list[str] = field(default_factory=list)


_DEFAULT_CODERABBIT_AUTHORS = frozenset({"coderabbitai", "coderabbitai[bot]", "coderabbit[bot]"})
DEFAULT_CODERABBIT_AUTHORS = _DEFAULT_CODERABBIT_AUTHORS

# Across 123 CodeRabbit findings on this repository's PRs a condensed comment ran
# to a median of about 900 characters and a 95th percentile of about 2300, so
# this cap only fires on an outlier. It replaces a raw 500-character slice that
# cut the title off 42% of those findings, because the raw comment leads with
# badge markup and a collapsed log of the scripts CodeRabbit ran.
_CODERABBIT_MESSAGE_LIMIT = 4000

# Collapsed sections holding CodeRabbit's own working rather than the finding:
# the scripts it ran, the learnings it applied, and a restatement of the finding
# for agents. The title, explanation and any suggested diff sit outside them.
_CODERABBIT_NOISE_SUMMARIES = ("static analysis", "analysis chain", "learnings used", "tools", "prompt for ai agents")
# Whole-word match, so a summary such as "Suggested fix using itertools" is not read as the "tools" label.
_CODERABBIT_NOISE_RE = re.compile(
    r"(?<!\w)(?:" + "|".join(re.escape(label) for label in _CODERABBIT_NOISE_SUMMARIES) + r")(?!\w)",
    re.IGNORECASE,
)

_DETAILS_TAG_RE = re.compile(r"<details\b[^>]*>|</details>", re.IGNORECASE)
_SUMMARY_RE = re.compile(r"\s*<summary>(.*?)</summary>", re.DOTALL | re.IGNORECASE)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_LIST_WRAPPER_RE = re.compile(r"</?(?:dl|dd|blockquote)>", re.IGNORECASE)
_MARKUP_RE = re.compile(r"<!--.*?-->|<picture>.*?</picture>|<img[^>]*>", re.DOTALL | re.IGNORECASE)
_INLINE_CODE_RE = re.compile(r"(?<!`)(`+)(?!`)(?:(?!\n[ \t]*\n).)+?(?<!`)\1(?!`)", re.DOTALL)


def _mask_code(text: str) -> str:
    """Return *text* with fenced blocks and inline code spans blanked, keeping offsets.

    Newlines survive, so a match found on the masked copy maps 1:1 onto *text*.
    """
    masked = mask_fenced_blocks(text)
    return _INLINE_CODE_RE.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), masked)


def _sub_outside_code(text: str, pattern: re.Pattern[str], repl: str = "") -> str:
    """Replace every *pattern* match in *text* that sits in neither a code fence nor an inline code span.

    Matches are found on the ``_mask_code()`` copy, which keeps offsets unchanged,
    so code content (a suggested diff of an HTML file, an HTML comment quoted in
    backticks) survives.
    """
    out: list[str] = []
    cursor = 0
    for match in pattern.finditer(_mask_code(text)):
        out.append(text[cursor : match.start()])
        out.append(repl)
        cursor = match.end()
    out.append(text[cursor:])
    return "".join(out)


def _collapse_blank_runs(text: str) -> str:
    """Collapse runs of empty lines to one, except inside code fences.

    ``mask_fenced_blocks()`` leaves an empty line empty, so a fenced one cannot be told from an
    unfenced one on the masked copy. Each empty line is therefore marked first: the mask then
    turns a fenced marker into a space and leaves an unfenced one alone.
    """
    marked = [line or "\x00" for line in text.split("\n")]
    masked = mask_fenced_blocks("\n".join(marked)).split("\n")
    out: list[str] = []
    previous_blank = False
    for line, mark, mask in zip(text.split("\n"), marked, masked, strict=True):
        blank = not line and mask == mark
        if not (blank and previous_blank):
            out.append(line)
        previous_blank = blank
    return "\n".join(out)


def _render_details(inner: str) -> str:
    """Render one ``<details>`` block's inner text: dropped when it is noise, else unwrapped."""
    summary_match = _SUMMARY_RE.match(inner)
    summary = _sub_outside_code(summary_match.group(1), _HTML_TAG_RE).strip() if summary_match else ""
    if _CODERABBIT_NOISE_RE.search(summary):
        return ""
    body = inner[summary_match.end() :] if summary_match else inner
    body = _sub_outside_code(_condense_details(body), _LIST_WRAPPER_RE).strip()
    return f"{summary}:\n{body}" if summary else body


def _condense_details(text: str) -> str:
    """Replace each top-level ``<details>`` block in *text* with its rendering.

    Tracks nesting depth rather than matching with one regex, since a kept block
    can hold another. Tags are found on the code-masked copy, so one inside a
    code fence or an inline code span is content, not structure (a suggested diff
    of an HTML template, a tag quoted in backticks).
    An unclosed block is left verbatim.
    """
    out: list[str] = []
    depth = 0
    cursor = 0
    inner_start = 0
    for match in _DETAILS_TAG_RE.finditer(_mask_code(text)):
        if not match.group(0).startswith("</"):
            if depth == 0:
                out.append(text[cursor : match.start()])
                cursor = match.start()
                inner_start = match.end()
            depth += 1
        elif depth:
            depth -= 1
            if depth == 0:
                out.append(_render_details(text[inner_start : match.start()]))
                cursor = match.end()
    out.append(text[cursor:])
    return "".join(out)


def condense_coderabbit_comment(body: str) -> str:
    """Reduce a CodeRabbit review comment to the text an agent needs to fix it.

    Drops the collapsed noise sections (``_CODERABBIT_NOISE_SUMMARIES``), HTML
    comments and badge images, and unwraps the remaining collapsed sections
    (a suggested fix, a committable suggestion) into plain labelled text. Code
    fences and inline code spans are left untouched, so a suggested diff of a
    file that itself holds HTML comments keeps them. Plain markdown, as a human
    writes it, passes through unchanged apart from blank-line runs outside code.
    """
    text = _sub_outside_code(_condense_details(body), _MARKUP_RE)
    return _collapse_blank_runs(text).strip()


async def _fetch_coderabbit_threads(
    repo: str,
    pr_number: int,
    *,
    authors: set[str] | None = None,
    github_user: str | None = None,
) -> _ThreadsResult:
    """Fetch unresolved review threads from a PR via GraphQL (every page).

    When ``authors`` is None, defaults to CodeRabbit bot accounts.
    """
    threads = await fetch_review_threads(repo, pr_number, github_user=github_user)
    if threads is None:
        return _ThreadsResult()

    from sova.llm.guard import sanitize_external_input

    allowed = authors if authors is not None else _DEFAULT_CODERABBIT_AUTHORS
    out = _ThreadsResult()
    for thread in threads:
        if thread.get("isResolved"):
            continue
        comments = (thread.get("comments") or {}).get("nodes") or []
        if not comments:
            continue
        # A deleted account comes back as ``author: null``, which a .get() default does not cover.
        author = (comments[0].get("author") or {}).get("login", "")
        if author not in allowed:
            continue

        body = condense_coderabbit_comment(comments[0].get("body") or "")
        body = sanitize_external_input(body, source="coderabbit_review")

        out.findings.append(
            ExternalFinding(
                source="coderabbit",
                file_path=thread.get("path", ""),
                line=thread.get("line"),
                severity="MAJOR",
                message=trim_payload(body, _CODERABBIT_MESSAGE_LIMIT, label="coderabbit_finding", keep="head"),
                tool_id=thread.get("id", ""),
            )
        )
        out.thread_ids.append(thread.get("id", ""))

    log.info("external_reviews.coderabbit_fetched", count=len(out.findings), pr=pr_number)
    return out


async def _thread_mutation(query: str, thread_id: str, env: dict[str, str] | None, *fields: str) -> bool:
    """Run one thread GraphQL mutation; True when ``gh`` reports success."""
    args = ["gh", "api", "graphql", "-f", f"query={query}", "-F", f"id={thread_id}"]
    for field_arg in fields:
        args.extend(["-f", field_arg])
    result = await run(*args, env=env, timeout=15)
    if not result.success:
        log.warning("external_reviews.thread_mutation_failed", thread_id=thread_id, stderr=result.stderr[:200])
    return result.success


_RESOLVE_THREAD_MUTATION = (
    "mutation($id: ID!) { resolveReviewThread(input: {threadId: $id}) { thread { isResolved } } }"
)
_REPLY_THREAD_MUTATION = (
    "mutation($id: ID!, $body: String!) "
    "{ addPullRequestReviewThreadReply(input: {pullRequestReviewThreadId: $id, body: $body}) { comment { id } } }"
)


async def reply_to_review_thread(thread_id: str, body: str, *, github_user: str | None = None) -> bool:
    """Reply on a review thread by ID via GraphQL. Returns True when the reply was posted."""
    from sova.utils.gh import resolve_gh_env

    env = await resolve_gh_env(github_user)
    return await _thread_mutation(_REPLY_THREAD_MUTATION, thread_id, env, f"body={body}")


async def resolve_review_thread(thread_id: str, *, github_user: str | None = None) -> bool:
    """Resolve one review thread by ID via GraphQL. Returns True when it was resolved."""
    from sova.utils.gh import resolve_gh_env

    env = await resolve_gh_env(github_user)
    return await _thread_mutation(_RESOLVE_THREAD_MUTATION, thread_id, env)


async def resolve_coderabbit_threads(
    thread_ids: list[str],
    *,
    github_user: str | None = None,
) -> int:
    """Resolve review threads by ID via GraphQL. Returns count of successfully resolved."""
    from sova.utils.gh import resolve_gh_env

    env = await resolve_gh_env(github_user)

    resolved = 0
    for thread_id in thread_ids:
        if await _thread_mutation(_RESOLVE_THREAD_MUTATION, thread_id, env):
            resolved += 1
    return resolved


def format_findings_for_prompt(findings: list[ExternalFinding]) -> str:
    """Format external findings into a structured prompt for the LLM agent."""
    if not findings:
        return ""

    lines = ["The following issues were found by external review tools on this PR:\n"]

    by_source: dict[str, list[ExternalFinding]] = {}
    for f in findings:
        by_source.setdefault(f.source, []).append(f)

    for source, source_findings in sorted(by_source.items()):
        lines.append(f"## {source.upper()} ({len(source_findings)} findings)\n")
        for f in source_findings:
            loc = f"{f.file_path}:{f.line}" if f.line else f.file_path
            lines.append(f"- [{f.severity}] {loc}: {f.message}\n")
        lines.append("")

    lines.append(
        "Fix ALL issues listed above. For each finding:\n"
        "1. Read the file at the indicated location\n"
        "2. Apply the fix\n"
        "3. Stage and commit the changes with a message like "
        "'fix(<scope>): address <tool> finding in <file>'. "
        f"{_COMMIT_SCOPE_GUIDANCE}\n\n"
        "If a finding is a false positive that cannot be fixed, skip it.\n"
        "Do not modify test files unless the finding specifically targets them.\n"
    )
    return "".join(lines)
