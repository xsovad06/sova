"""External review tool integrations (SonarCloud, CodeRabbit).

Fetches findings from external review tools that post on GitHub PRs,
and provides utilities to resolve review threads after fixes.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from urllib.parse import quote

from sova.utils.logging import get_logger
from sova.utils.shell import run

log = get_logger(component="adapters.external_reviews")

_CHECK_NAMES: dict[str, str] = {
    "sonarcloud": "SonarCloud Code Analysis",
    "coderabbit": "CodeRabbit",
}


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
            ``sonarcloud.coverage_threshold`` in sova.toml, default 80.0).
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
        "4. Stage and commit with message like 'test: cover uncovered paths in <module>'\n"
        "5. Only add tests -- do NOT modify production code unless other findings also require it\n"
    )

    if diff_files:
        lines.append(f"\nFiles changed in this PR: {', '.join(diff_files)}\n")

    return "".join(lines)


_CODERABBIT_THREADS_QUERY = """
query($owner: String!, $name: String!, $pr: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $pr) {
      reviewThreads(first: 100) {
        nodes {
          id
          isResolved
          path
          line
          comments(first: 1) {
            nodes {
              body
              author { login }
            }
          }
        }
      }
    }
  }
}
"""


@dataclass
class _ThreadsResult:
    findings: list[ExternalFinding] = field(default_factory=list)
    thread_ids: list[str] = field(default_factory=list)


_DEFAULT_CODERABBIT_AUTHORS = frozenset({"coderabbitai", "coderabbitai[bot]", "coderabbit[bot]"})
DEFAULT_CODERABBIT_AUTHORS = _DEFAULT_CODERABBIT_AUTHORS


async def _fetch_coderabbit_threads(
    repo: str,
    pr_number: int,
    *,
    authors: set[str] | None = None,
    github_user: str | None = None,
) -> _ThreadsResult:
    """Fetch unresolved review threads from a PR via GraphQL.

    When ``authors`` is None, defaults to CodeRabbit bot accounts.
    """
    from sova.utils.gh import resolve_gh_env

    owner, name = repo.split("/", 1) if "/" in repo else ("", repo)
    env = await resolve_gh_env(github_user)

    result = await run(
        "gh",
        "api",
        "graphql",
        "-f",
        f"query={_CODERABBIT_THREADS_QUERY}",
        "-F",
        f"owner={owner}",
        "-F",
        f"name={name}",
        "-F",
        f"pr={pr_number}",
        env=env,
        timeout=30,
    )
    if not result.success:
        log.warning("external_reviews.coderabbit_fetch_failed", stderr=result.stderr[:200])
        return _ThreadsResult()

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        log.warning("external_reviews.coderabbit_parse_failed", exc_info=True)
        return _ThreadsResult()

    threads = (
        data.get("data", {}).get("repository", {}).get("pullRequest", {}).get("reviewThreads", {}).get("nodes", [])
    )

    from sova.llm.guard import sanitize_external_input

    allowed = authors if authors is not None else _DEFAULT_CODERABBIT_AUTHORS
    out = _ThreadsResult()
    for thread in threads:
        if thread.get("isResolved"):
            continue
        comments = thread.get("comments", {}).get("nodes", [])
        if not comments:
            continue
        author = comments[0].get("author", {}).get("login", "")
        if author not in allowed:
            continue

        body = sanitize_external_input(comments[0].get("body", ""), source="coderabbit_review")

        out.findings.append(
            ExternalFinding(
                source="coderabbit",
                file_path=thread.get("path", ""),
                line=thread.get("line"),
                severity="MAJOR",
                message=body[:500],
                tool_id=thread.get("id", ""),
            )
        )
        out.thread_ids.append(thread.get("id", ""))

    log.info("external_reviews.coderabbit_fetched", count=len(out.findings), pr=pr_number)
    return out


async def fetch_coderabbit_findings(
    repo: str,
    pr_number: int,
    *,
    github_user: str | None = None,
) -> list[ExternalFinding]:
    """Fetch unresolved CodeRabbit findings for a PR."""
    result = await _fetch_coderabbit_threads(repo, pr_number, github_user=github_user)
    return result.findings


async def resolve_coderabbit_threads(
    thread_ids: list[str],
    *,
    github_user: str | None = None,
) -> int:
    """Resolve review threads by ID via GraphQL. Returns count of successfully resolved."""
    from sova.utils.gh import resolve_gh_env

    env = await resolve_gh_env(github_user)

    mutation = "mutation($id: ID!) { resolveReviewThread(input: {threadId: $id}) { thread { isResolved } } }"

    resolved = 0
    for thread_id in thread_ids:
        result = await run(
            "gh",
            "api",
            "graphql",
            "-f",
            f"query={mutation}",
            "-F",
            f"id={thread_id}",
            env=env,
            timeout=15,
        )
        if result.success:
            resolved += 1
        else:
            log.warning(
                "external_reviews.resolve_thread_failed",
                thread_id=thread_id,
                stderr=result.stderr[:200],
            )
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
        "'fix: address <tool> finding in <file>'\n\n"
        "If a finding is a false positive that cannot be fixed, skip it.\n"
        "Do not modify test files unless the finding specifically targets them.\n"
    )
    return "".join(lines)
