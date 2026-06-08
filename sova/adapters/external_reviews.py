"""External review tool integrations (SonarCloud, CodeRabbit).

Fetches findings from external review tools that post on GitHub PRs,
and provides utilities to resolve review threads after fixes.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
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


async def fetch_sonarcloud_issues(
    project_key: str,
    pr_number: int,
) -> list[ExternalFinding]:
    """Fetch open issues from SonarCloud for a PR."""
    token = os.environ.get("SONAR_TOKEN", "")
    auth_args: list[str] = []
    if token:
        auth_args = ["-H", f"Authorization: Bearer {token}"]

    url = (
        f"https://sonarcloud.io/api/issues/search"
        f"?componentKeys={quote(project_key, safe='')}"
        f"&pullRequest={pr_number}"
        f"&resolved=false"
        f"&ps=500"
    )

    result = await run("curl", "-sf", *auth_args, url, timeout=30)
    if not result.success:
        log.warning("external_reviews.sonarcloud_fetch_failed", stderr=result.stderr[:200])
        return []

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        log.warning("external_reviews.sonarcloud_parse_failed", exc_info=True)
        return []

    findings: list[ExternalFinding] = []
    for issue in data.get("issues", []):
        component = issue.get("component", "")
        file_path = component.split(":", 1)[1] if ":" in component else component
        findings.append(
            ExternalFinding(
                source="sonarcloud",
                file_path=file_path,
                line=issue.get("line"),
                severity=issue.get("severity", "UNKNOWN"),
                message=issue.get("message", ""),
                tool_id=issue.get("key", ""),
            )
        )

    log.info("external_reviews.sonarcloud_fetched", count=len(findings), pr=pr_number)
    return findings


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


async def _fetch_coderabbit_threads(
    repo: str,
    pr_number: int,
    *,
    github_user: str | None = None,
) -> _ThreadsResult:
    """Fetch unresolved CodeRabbit review threads from a PR via GraphQL."""
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

    out = _ThreadsResult()
    for thread in threads:
        if thread.get("isResolved"):
            continue
        comments = thread.get("comments", {}).get("nodes", [])
        if not comments:
            continue
        author = comments[0].get("author", {}).get("login", "")
        if author not in ("coderabbitai", "coderabbitai[bot]", "coderabbit[bot]"):
            continue

        body = comments[0].get("body", "")

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
) -> None:
    """Resolve CodeRabbit review threads by ID via GraphQL."""
    from sova.utils.gh import resolve_gh_env

    env = await resolve_gh_env(github_user)

    mutation = "mutation($id: ID!) { resolveReviewThread(input: {threadId: $id}) { thread { isResolved } } }"

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
        if not result.success:
            log.warning(
                "external_reviews.resolve_thread_failed",
                thread_id=thread_id,
                stderr=result.stderr[:200],
            )


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
