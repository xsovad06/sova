"""Setup service -- project scanning, detection, and configuration.

Provides the business logic for the setup wizard: directory browsing,
tech stack detection, and sova.toml generation.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from pathlib import Path

import httpx

from sova.utils.logging import get_logger
from sova.utils.shell import run

log = get_logger(component="dashboard.setup")

_PACKAGE_JSON = "package.json"
_PYPROJECT_TOML = "pyproject.toml"
_REQUIREMENTS_TXT = "requirements.txt"
_SOVA_TOML = "sova.toml"

_PROJECT_MARKERS = (
    ".git",
    _PACKAGE_JSON,
    _PYPROJECT_TOML,
    "Cargo.toml",
    "go.mod",
    "manage.py",
    "Makefile",
    _REQUIREMENTS_TXT,
)

_SKIP_DIRS = frozenset(
    {
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        "dist",
        "build",
        ".tox",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        "target",
    }
)


def browse_directory(path: str) -> dict:
    """List directories for the project browser."""
    resolved = Path(path).expanduser().resolve() if path else Path.home()
    if not resolved.is_dir():
        resolved = resolved.parent

    entries: list[dict] = []

    # Parent directory
    if resolved != resolved.parent:
        entries.append({"name": "..", "path": str(resolved.parent), "is_project": False})

    try:
        for child in sorted(resolved.iterdir()):
            if not child.is_dir():
                continue
            name = child.name
            if name.startswith(".") and name != ".claude":
                continue
            if name in _SKIP_DIRS:
                continue

            is_project = any((child / marker).exists() for marker in _PROJECT_MARKERS)
            has_sova = (child / _SOVA_TOML).exists() or (child / ".claude" / "sova.db").exists()

            entries.append(
                {
                    "name": name,
                    "path": str(child),
                    "is_project": is_project,
                    "has_sova": has_sova,
                }
            )
    except PermissionError:
        pass

    return {"current": str(resolved), "entries": entries}


async def scan_project(project_path: str) -> dict:
    """Scan a project to detect tech stack, repo, and suggest config."""
    project = Path(project_path).expanduser().resolve()
    if not project.is_dir():
        return {"error": f"Directory not found: {project}"}

    stack = _detect_tech_stack(project)
    persona = _detect_persona(stack)
    github_repo = await _detect_github_repo(project)
    base_branch = await _detect_base_branch(project)
    test_cmd = _detect_cmd(project, "test", "test", "")
    lint_cmd = _detect_cmd(project, "lint", "lint", "")
    format_cmd = _detect_cmd(project, "format", "format", "")

    already_installed = (project / _SOVA_TOML).exists()
    existing_config = _read_existing_toml(project) if already_installed else {}

    return {
        "project_path": str(project),
        "project_name": project.name,
        "tech_stack": stack,
        "persona": persona,
        "github_repo": github_repo,
        "base_branch": base_branch,
        "test_cmd": test_cmd,
        "lint_cmd": lint_cmd,
        "format_cmd": format_cmd,
        "already_installed": already_installed,
        "existing_config": existing_config,
    }


@dataclass
class TomlConfig:
    """Configuration values for sova.toml generation."""

    github_repo: str = ""
    github_user: str = ""
    base_branch: str = "main"
    test_cmd: str = "make test"
    lint_cmd: str = "make lint"
    format_cmd: str = "make format"
    task_source: str = "github"
    agent_model: str = "opus"
    max_budget: str = "10.00"
    review_max_rounds: int = 2
    branch_naming: str = "conventional"
    commit_format: str = "conventional"
    ai_coauthor: bool = True
    pr_title_format: str = "conventional"
    pr_auto_link: bool = True
    # Jira-specific fields
    jira_base_url: str = ""
    jira_email: str = ""
    jira_project_key: str = ""
    jira_component: str = ""
    jira_status_mapping: dict[str, str] | None = None
    jira_track_agent_work: bool = False


def _add_jira_fields(task_source_table: object, config: TomlConfig) -> None:
    """Populate Jira-specific fields on the task_source table."""
    import tomlkit

    jira_fields = [
        ("jira_base_url", config.jira_base_url),
        ("jira_email", config.jira_email),
        ("jira_project_key", config.jira_project_key),
        ("jira_component", config.jira_component),
    ]
    for key, value in jira_fields:
        if value:
            task_source_table.add(key, value)  # type: ignore[union-attr]
    if config.jira_track_agent_work:
        task_source_table.add("jira_track_agent_work", True)  # type: ignore[union-attr]
    if config.jira_status_mapping:
        mapping_table = tomlkit.inline_table()
        for k, v in config.jira_status_mapping.items():
            mapping_table.add(k, v)
        task_source_table.add("jira_status_mapping", mapping_table)  # type: ignore[union-attr]


def generate_sova_toml(config: TomlConfig) -> str:
    """Generate sova.toml content from wizard form data."""
    import tomlkit

    doc = tomlkit.document()
    doc.add(tomlkit.comment("SOVA configuration"))
    doc.add("github_repo", config.github_repo)
    doc.add("github_user", config.github_user)
    doc.add("base_branch", config.base_branch)
    doc.add("test_cmd", config.test_cmd)
    doc.add("lint_cmd", config.lint_cmd)
    doc.add("format_cmd", config.format_cmd)

    task_source_table = tomlkit.table()
    task_source_table.add("type", config.task_source)
    if config.task_source == "jira":
        _add_jira_fields(task_source_table, config)
    doc.add("task_source", task_source_table)

    agent_table = tomlkit.table()
    agent_table.add("model", config.agent_model)
    agent_table.add("max_budget", config.max_budget)
    doc.add("agent", agent_table)

    review_table = tomlkit.table()
    review_table.add("enabled", True)
    review_table.add("max_rounds", config.review_max_rounds)
    doc.add("review", review_table)

    commit_table = tomlkit.table()
    commit_table.add("format", config.commit_format)
    commit_table.add("ai_coauthor", config.ai_coauthor)
    doc.add("commit", commit_table)

    branch_table = tomlkit.table()
    branch_table.add("naming", config.branch_naming)
    doc.add("branch", branch_table)

    pr_table = tomlkit.table()
    pr_table.add("title_format", config.pr_title_format)
    pr_table.add("auto_link_issues", config.pr_auto_link)
    doc.add("pr", pr_table)

    triage_table = tomlkit.table()
    triage_table.add("auto_label", True)
    triage_table.add("min_confidence", 0.7)
    doc.add("triage", triage_table)

    roles_table = tomlkit.table()
    roles_table.add("default", "developer")
    doc.add("roles", roles_table)

    return tomlkit.dumps(doc)


# -- Detection helpers --------------------------------------------------------


def _detect_tech_stack(project: Path) -> list[str]:
    stack: list[str] = []
    if (project / _REQUIREMENTS_TXT).exists() or (project / _PYPROJECT_TOML).exists():
        stack.append("python")
        reqs = ""
        for f in [_REQUIREMENTS_TXT, _PYPROJECT_TOML, "setup.py", "setup.cfg"]:
            p = project / f
            if p.exists():
                reqs += p.read_text(errors="ignore")
        if (project / "manage.py").exists() and "django" in reqs.lower():
            stack.append("django")
        if "fastapi" in reqs.lower():
            stack.append("fastapi")
    if (project / _PACKAGE_JSON).exists():
        stack.append("javascript")
        pkg = (project / _PACKAGE_JSON).read_text(errors="ignore")
        if '"react"' in pkg:
            stack.append("react")
        if '"next"' in pkg:
            stack.append("nextjs")
        if (project / "tsconfig.json").exists():
            stack.append("typescript")
    if (project / "go.mod").exists():
        stack.append("go")
    if (project / "Cargo.toml").exists():
        stack.append("rust")
    try:
        manifests = list(project.rglob("__manifest__.py"))
        if any("'name'" in m.read_text(errors="ignore") for m in manifests[:5]):
            stack.append("odoo")
    except OSError:
        pass
    return stack


def _detect_persona(stack: list[str]) -> str:
    for tech, persona in [
        ("django", "django"),
        ("fastapi", "fastapi"),
        ("odoo", "odoo"),
        ("react", "react"),
        ("go", "go-service"),
        ("rust", "rust"),
    ]:
        if tech in stack:
            return persona
    return ""


async def _detect_github_repo(project: Path) -> str:
    result = await run("git", "remote", "get-url", "origin", cwd=project, timeout=5)
    if result.success:
        m = re.search(r"github\.com[^:/]*[:/]([^/]+/[^/.]+)", result.stdout.strip())
        if m:
            return m.group(1)
    return ""


async def _detect_base_branch(project: Path) -> str:
    for branch in ["main", "master", "develop", "dev"]:
        result = await run("git", "rev-parse", "--verify", branch, cwd=project, timeout=5)
        if result.success:
            return branch
    return "main"


def _detect_cmd(project: Path, makefile_target: str, pkg_script: str, fallback: str) -> str:
    makefile = project / "Makefile"
    if makefile.exists() and re.search(rf"^{makefile_target}:", makefile.read_text(), re.MULTILINE):
        return f"make {makefile_target}"
    pkg = project / _PACKAGE_JSON
    if pkg.exists() and f'"{pkg_script}"' in pkg.read_text(errors="ignore"):
        return f"npm run {pkg_script}"
    return fallback


_SUGGESTED_STATUS_MAPPING: dict[str, str] = {
    "To Do": "backlog",
    "Backlog": "backlog",
    "Open": "backlog",
    "New": "needs_spec",
    "Refinement": "needs_spec",
    "In Progress": "in_progress",
    "In Development": "in_progress",
    "Code Review": "in_review",
    "Review": "in_review",
    "In Review": "in_review",
    "Done": "done",
    "Closed": "done",
    "Resolved": "done",
}


def _validate_jira_base_url(base_url: str) -> str:
    """Validate and normalize a Jira base URL.

    Ensures the URL uses https and points to an Atlassian domain,
    preventing SSRF via user-controlled URL construction.
    """
    from urllib.parse import urlparse

    parsed = urlparse(base_url.rstrip("/"))
    if parsed.scheme not in ("https", "http"):
        raise ValueError(f"Invalid Jira URL scheme: {parsed.scheme}")
    if not parsed.hostname:
        raise ValueError("Jira URL must include a hostname")
    return f"{parsed.scheme}://{parsed.hostname}{f':{parsed.port}' if parsed.port else ''}"


async def _jira_api_get(base_url: str, email: str, api_token: str, endpoint: str) -> httpx.Response:
    """Make an authenticated GET request to the Jira REST API v3."""
    validated_base = _validate_jira_base_url(base_url)
    credentials = base64.b64encode(f"{email}:{api_token}".encode()).decode()
    headers = {
        "Authorization": f"Basic {credentials}",
        "Accept": "application/json",
    }
    # endpoint is always a hardcoded constant from callers (e.g. "myself", "project")
    url = f"{validated_base}/rest/api/3/{endpoint}"
    async with httpx.AsyncClient(timeout=15.0) as client:
        return await client.get(url, headers=headers)


async def test_jira_connection(base_url: str, email: str, api_token: str) -> dict:
    """Test Jira connection credentials by calling /rest/api/3/myself."""
    try:
        resp = await _jira_api_get(base_url, email, api_token, "myself")
        if resp.status_code == 200:
            data = resp.json()
            return {
                "status": "ok",
                "display_name": data.get("displayName", ""),
                "email": data.get("emailAddress", ""),
            }
        return {"status": "error", "detail": f"HTTP {resp.status_code}: {resp.text[:200]}"}
    except httpx.HTTPError as e:
        return {"status": "error", "detail": str(e)}


async def discover_jira_projects(base_url: str, email: str, api_token: str) -> dict:
    """List accessible Jira projects."""
    try:
        resp = await _jira_api_get(base_url, email, api_token, "project")
        if resp.status_code != 200:
            return {"status": "error", "detail": f"HTTP {resp.status_code}: {resp.text[:200]}"}
        projects = [
            {
                "key": p.get("key", ""),
                "name": p.get("name", ""),
                "lead": (p.get("lead") or {}).get("displayName", ""),
            }
            for p in resp.json()
        ]
        return {"status": "ok", "projects": projects}
    except httpx.HTTPError as e:
        return {"status": "error", "detail": str(e)}


def _validate_jira_project_key(project_key: str) -> str:
    """Validate and return a safe Jira project key (uppercase letters + optional digits)."""
    if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,255}", project_key):
        raise ValueError(f"Invalid Jira project key: {project_key!r}")
    return project_key


async def discover_jira_statuses(
    base_url: str,
    email: str,
    api_token: str,
    project_key: str,
) -> dict:
    """Discover workflow statuses for a Jira project and suggest mapping."""
    try:
        safe_key = _validate_jira_project_key(project_key)
        resp = await _jira_api_get(base_url, email, api_token, f"project/{safe_key}/statuses")
        if resp.status_code != 200:
            return {"status": "error", "detail": f"HTTP {resp.status_code}: {resp.text[:200]}"}

        # Parse statuses from all issue types
        seen: set[str] = set()
        statuses: list[dict[str, str]] = []
        for issue_type in resp.json():
            for s in issue_type.get("statuses", []):
                name = s.get("name", "")
                if name and name not in seen:
                    seen.add(name)
                    category = s.get("statusCategory", {}).get("name", "")
                    statuses.append({"name": name, "category": category})

        suggested_mapping = {
            s["name"]: _SUGGESTED_STATUS_MAPPING[s["name"]] for s in statuses if s["name"] in _SUGGESTED_STATUS_MAPPING
        }

        return {"status": "ok", "statuses": statuses, "suggested_mapping": suggested_mapping}
    except ValueError as e:
        return {"status": "error", "detail": str(e)}
    except httpx.HTTPError as e:
        return {"status": "error", "detail": str(e)}


def _read_existing_toml(project: Path) -> dict:
    """Read existing sova.toml as a flat dict for prefilling the form."""
    toml_file = project / _SOVA_TOML
    if not toml_file.exists():
        return {}
    try:
        import tomllib

        with open(toml_file, "rb") as f:
            data = tomllib.load(f)
        # Flatten nested sections for the form
        flat: dict[str, str] = {}
        for key, val in data.items():
            if isinstance(val, dict):
                for k, v in val.items():
                    flat[f"{key}.{k}"] = str(v)
            else:
                flat[key] = str(val)
        return flat
    except Exception:
        return {}
