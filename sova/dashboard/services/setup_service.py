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

    has_toml = (project / _SOVA_TOML).exists()
    from sova.config.db_loader import _flatten_config_dict, _try_load_from_db

    db_config = _try_load_from_db(project)
    has_db = db_config is not None
    already_installed = has_toml or has_db

    existing_config: dict = {}
    if has_toml:
        existing_config = _read_existing_toml(project)
    if db_config is not None:
        db_flat = _flatten_config_dict(db_config)
        existing_config.update({k: str(v) for k, v in db_flat.items()})

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


def generate_config_dict(config: TomlConfig) -> dict:
    """Generate a config dict from wizard form data for DB storage.

    Maps ``[branch]`` and ``[pr]`` fields to the correct ``ProjectConfig``
    fields: ``commit.branch_naming``, ``commit.pr_title_format``,
    ``commit.ai_coauthor``.
    """
    result: dict = {
        "github_repo": config.github_repo,
        "github_user": config.github_user,
        "base_branch": config.base_branch,
        "test_cmd": config.test_cmd,
        "lint_cmd": config.lint_cmd,
        "format_cmd": config.format_cmd,
        "task_source": {"type": config.task_source},
        "agent": {"model": config.agent_model, "max_budget": config.max_budget},
        "review": {"enabled": True, "max_rounds": config.review_max_rounds},
        "commit": {
            "format": config.commit_format,
            "ai_coauthor": config.ai_coauthor,
            "branch_naming": config.branch_naming,
            "pr_title_format": config.pr_title_format,
            "pr_auto_link_issues": config.pr_auto_link,
        },
        "triage": {"auto_label": True, "min_confidence": 0.7},
        "roles": {"default": "developer"},
    }

    if config.task_source == "jira":
        jira_fields: dict = {}
        for key, value in [
            ("jira_base_url", config.jira_base_url),
            ("jira_email", config.jira_email),
            ("jira_project_key", config.jira_project_key),
            ("jira_component", config.jira_component),
        ]:
            if value:
                jira_fields[key] = value
        if config.jira_track_agent_work:
            jira_fields["jira_track_agent_work"] = True
        if config.jira_status_mapping:
            jira_fields["jira_status_mapping"] = config.jira_status_mapping
        result["task_source"].update(jira_fields)

    return result


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
        try:
            pkg = (project / _PACKAGE_JSON).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            pkg = ""
        if '"react"' in pkg:
            stack.append("react")
        if '"next"' in pkg:
            stack.append("nextjs")
        if (project / "tsconfig.json").exists():
            stack.append("typescript")
        from sova.utils.package_json import has_dependency

        if has_dependency(project, "@patternfly/react-core"):
            stack.append("patternfly")
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
        ("patternfly", "patternfly"),
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

    Ensures the URL uses HTTPS and the hostname is not a private/internal
    address. Rejects localhost, loopback, link-local, and RFC-1918 private
    IP ranges to prevent SSRF.
    """
    import ipaddress
    import socket
    from urllib.parse import urlparse

    parsed = urlparse(base_url.rstrip("/"))
    if parsed.scheme != "https":
        raise ValueError(f"Invalid Jira URL scheme: {parsed.scheme!r} (only https is allowed)")
    hostname = parsed.hostname
    if not hostname:
        raise ValueError("Jira URL must include a hostname")

    # Block well-known internal hostnames
    _blocked = {"localhost", "localhost.localdomain", "127.0.0.1", "::1", "[::1]"}
    if hostname.lower() in _blocked:
        raise ValueError(f"Jira URL must not point to a local address: {hostname!r}")

    # Resolve hostname and reject private/reserved IPs
    try:
        addr = ipaddress.ip_address(hostname)
    except ValueError:
        # Not a literal IP -- resolve DNS
        try:
            resolved = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
            for _family, _type, _proto, _canonname, sockaddr in resolved:
                addr = ipaddress.ip_address(sockaddr[0])
                if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved:
                    raise ValueError(f"Jira URL hostname {hostname!r} resolves to a private/reserved address: {addr}")
        except socket.gaierror:
            # Cannot resolve -- allow (the actual HTTP request will fail)
            pass
    else:
        if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved:
            raise ValueError(f"Jira URL must not point to a private/reserved address: {hostname!r}")

    return f"https://{hostname}{f':{parsed.port}' if parsed.port else ''}"


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
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
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
    except Exception as e:
        log.exception("jira_test.failed")
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


DEFAULT_PHASE_TITLES = [
    "Phase 1: Now",
    "Phase 2: Next",
    "Phase 3: Later",
    "Phase 4: Future",
]

DEFAULT_PHASE_DESCRIPTIONS = [
    "Current sprint work",
    "Backlog - next sprint",
    "Future enhancements",
    "Long-term vision",
]


async def create_starter_milestones(
    project_dir: Path,
    titles: list[str] | None = None,
) -> dict:
    """Create default phase milestones on the tracker, skipping existing ones.

    Returns a dict with created, skipped, and failed lists.
    """
    from sova.adapters import create_adapter
    from sova.config.loader import load_config

    effective_titles = list(DEFAULT_PHASE_TITLES) if titles is None else titles

    try:
        cfg = load_config(project_dir)
    except (FileNotFoundError, ValueError, KeyError) as e:
        return {"status": "error", "detail": f"Failed to load config: {e}"}

    try:
        adapter = create_adapter(cfg)
    except ValueError as e:
        return {"status": "error", "detail": str(e)}

    try:
        existing = await adapter.list_milestones(state="all")
    except Exception as e:
        log.warning("create_starter_milestones.list_failed", exc_info=True)
        return {"status": "error", "detail": f"Failed to list milestones: {e}"}

    existing_titles = {m.title for m in existing}

    created: list[str] = []
    skipped: list[str] = []
    failed: list[dict[str, str]] = []

    for i, title in enumerate(effective_titles):
        if title in existing_titles:
            skipped.append(title)
            continue
        try:
            desc = DEFAULT_PHASE_DESCRIPTIONS[i] if i < len(DEFAULT_PHASE_DESCRIPTIONS) else ""
            await adapter.create_milestone(title=title, description=desc)
            created.append(title)
        except Exception as e:
            failed.append({"title": title, "error": str(e)})

    return {
        "status": "ok",
        "created": created,
        "skipped": skipped,
        "failed": failed,
    }


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
