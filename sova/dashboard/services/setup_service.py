"""Setup service -- project scanning, detection, and configuration.

Provides the business logic for the setup wizard: directory browsing,
tech stack detection, and sova.toml generation.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import httpx

from sova.utils.env import configured_passthrough, scrub_agent_env
from sova.utils.formatting import decimal_to_json
from sova.utils.logging import get_logger
from sova.utils.shell import run

if TYPE_CHECKING:
    from sova.ipc.runtime import AgentRuntime
    from sova.llm.backends import Backend
    from sova.llm.provider import LLMProvider

log = get_logger(component="dashboard.setup")

_PACKAGE_JSON = "package.json"
_PYPROJECT_TOML = "pyproject.toml"
_REQUIREMENTS_TXT = "requirements.txt"
_SOVA_TOML = "sova.toml"

# TTL matches the provider-status-widget.js poll interval so a poll never
# re-probes the CLI more than once per interval.
_AUTH_STATUS_CACHE_TTL = 60.0
_auth_status_cache: dict[str, tuple[float, dict]] = {}
# project dir -> single-flight lock, mirroring coderabbit_quota.py's per-repo
# lock so concurrent uncached requests for the same project share one probe
# instead of each spawning duplicate `claude --version` / `claude auth status` calls.
_auth_status_locks: dict[str, asyncio.Lock] = {}

# Allowlist, not a denylist: the account payload is whatever `claude auth
# status --json` happens to print, so a field a future CLI version adds (a
# token, a session id) would otherwise be served verbatim by an endpoint that
# is explicitly forbidden from exposing secret material. Only these six fields
# are part of the verified contract in issue #933.
_ACCOUNT_FIELDS = ("email", "authMethod", "apiProvider", "orgId", "orgName", "subscriptionType")

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
        "review": {"enabled": True},
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
    except Exception as e:  # noqa: BLE001 (connection test reports any failure to the user)
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
    except Exception as e:  # noqa: BLE001 (adapter raises AdapterError/ValueError too; stay fail-open)
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
        except Exception as e:  # noqa: BLE001 (adapter raises AdapterError/ValueError too; stay fail-open)
            log.warning("setup.milestone_create_failed", title=title, exc_info=True)
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
    except (OSError, ValueError):
        log.warning("setup.toml_read_failed", toml_file=str(toml_file), exc_info=True)
        return {}


async def get_auth_status(project_dir: Path) -> dict:
    """Assemble the read-only LLM provider auth status for the dashboard widget.

    Cached per project directory for ``_AUTH_STATUS_CACHE_TTL`` seconds so the
    CLI auth probe isn't re-run on every widget poll. A ``create_provider``
    ``ValueError`` (unknown/misconfigured provider type) is a routine
    misconfigured state, reported here rather than raised, mirroring
    ``sova/cli/commands/doctor.py:_check_llm_provider``. Any other exception
    (e.g. config load failure) propagates so the router can turn it into a
    503 (a genuine service failure, not a status to report).
    """
    from sova.config.loader import load_config
    from sova.llm.provider import create_provider
    from sova.utils.env import PROVIDER_ROUTING_VARS

    cache_key = str(project_dir.resolve())
    now = time.monotonic()
    cached = _auth_status_cache.get(cache_key)
    if cached and (now - cached[0]) < _AUTH_STATUS_CACHE_TTL:
        return {**cached[1], "cached": True}

    lock = _auth_status_locks.setdefault(cache_key, asyncio.Lock())
    async with lock:
        # Recheck after acquiring: another task may have refreshed while we waited
        now = time.monotonic()
        cached = _auth_status_cache.get(cache_key)
        if cached and (now - cached[0]) < _AUTH_STATUS_CACHE_TTL:
            return {**cached[1], "cached": True}

        cfg = await asyncio.to_thread(load_config, project_dir)
        result = {
            "provider": cfg.llm.provider,
            "authenticated": False,
            "detail": "",
            "account": None,
            "api_key_configured": bool(cfg.llm.api_key),
            "routing_warnings": sorted(PROVIDER_ROUTING_VARS & os.environ.keys()),
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }

        try:
            provider = create_provider(cfg.llm, project_dir)
        except ValueError as exc:
            result["detail"] = str(exc)
        else:
            result["authenticated"], result["detail"] = await provider.check_available()
            get_auth_details = getattr(provider, "get_auth_details", None)
            account = await get_auth_details() if get_auth_details is not None else None
            if get_auth_details is not None:
                # check_available() fails open on an unreadable/unknown auth
                # probe so agent spawning isn't blocked; the widget must not
                # report that same fail-open case as a confirmed login. Only
                # a get_auth_details() payload counts as authenticated here.
                result["authenticated"] = account is not None
            if account is not None:
                account = {k: account[k] for k in _ACCOUNT_FIELDS if k in account}
            result["account"] = account

        _auth_status_cache[cache_key] = (time.monotonic(), result)
        return {**result, "cached": False}


def _invalidate_auth_status_cache(project_dir: Path) -> None:
    """Drop the cached /auth/status result so the next read re-probes immediately.

    Called after a reconnect completes or a candidate is activated: both
    change the real answer right away, and waiting out ``_AUTH_STATUS_CACHE_TTL``
    would show stale state on the page that just caused the change.
    """
    _auth_status_cache.pop(str(project_dir.resolve()), None)


# Connections page: provider/runtime catalog ---------------------------------

# Display order matches the dashboard's "AI provider for SOVA calls" list.
_LLM_PROVIDER_LABELS: dict[str, str] = {
    "claude-code": "Claude Code (Anthropic subscription)",
    "anthropic": "Anthropic API",
    "openai": "OpenAI",
    "ollama": "Ollama (local)",
    "vertex": "Google Vertex AI",
    "litellm": "LiteLLM (generic routing)",
    "hybrid": "Hybrid (generic routing)",
}

# These three have no shared default model (see LLMConfig's cross-field
# validator): an inactive candidate catalog entry can't construct a real
# LLMConfig for them without one, so the catalog check bypasses LLMConfig
# entirely and talks to the provider class directly (see _check_llm_candidate).
# Must match sova/config/models.py:_VENDOR_MODEL_EXAMPLES exactly, as
# _LLM_PROVIDER_LABELS/_RUNTIME_LABELS above must match the LLMConfig.provider
# and AgentConfig.runtime Literals (all three pinned by
# tests/test_dashboard.py::TestConnectionsAPI::test_catalog_tables_match_config).
_LLM_MODEL_REQUIRED = frozenset({"openai", "ollama", "vertex"})

_RUNTIME_LABELS: dict[str, str] = {
    "claude-code": "Claude Code",
    "aider": "Aider",
    "codex": "Codex",
}

# Placeholder passed to LiteLLMProvider for an inactive model-required
# candidate's catalog readiness check: check_available() for openai/ollama/
# vertex never reads self.model (it checks vendor credentials/connectivity
# instead), so this value is never actually sent anywhere.
_CATALOG_PLACEHOLDER_MODEL = "unconfigured"

# Hard upper bound on how long a `claude auth login` subprocess may run
# before it is killed and reported as timed out (no browser callback).
_RECONNECT_TIMEOUT = 300.0
_CANCEL_GRACE_PERIOD = 2.0


def _build_llm_candidate_provider(
    provider_id: str, cfg, project_dir: Path, *, model: str = "", api_base: str = ""
) -> LLMProvider:
    """Construct the provider class for *provider_id* without going through LLMConfig.

    ``create_provider(cfg.llm)`` takes the project's single, already-validated
    ``LLMConfig``, which only ever describes the *active* provider. A catalog
    entry for every other candidate needs the same ``check_available()``
    contract without that validated object (openai/ollama/vertex require a
    non-empty model at the ``LLMConfig`` level, which an inactive candidate
    may not have), so this builds the provider class directly instead.

    The resolved API key is scoped to ``(project_dir, provider_id)``, not the
    project's currently-active provider: this lets an operator probe a
    not-yet-active candidate's own previously-saved credential (issue #1148).

    The legacy unscoped keyring entry and the plaintext database value are
    both artifacts of whichever provider was active at the time they were
    written (``cfg.llm.api_key`` is a single, unscoped database row, not
    keyed by provider). Both are therefore only passed as fallbacks when
    *provider_id* is the project's currently-active provider; an inactive
    candidate (e.g. probing "openai" while "anthropic" is active) must
    resolve purely from its own scoped keyring entry, never fall through to
    a different vendor's leftover legacy/database credential (CodeRabbit,
    issue #1148).
    """
    from sova.llm.keyring_store import resolve_secret, scoped_secret_name

    is_active = provider_id == cfg.llm.provider
    legacy_name = "llm.api_key" if is_active else None
    db_value = cfg.llm.api_key if is_active else None

    if provider_id == "claude-code":
        from sova.llm.providers.claude_code import ClaudeCodeProvider

        return ClaudeCodeProvider()

    if provider_id == "anthropic":
        from sova.llm.providers.anthropic_api import AnthropicAPIProvider

        scoped_name = scoped_secret_name("llm.api_key", project_dir, provider_id)
        api_key = resolve_secret(scoped_name, db_value, legacy_name=legacy_name)
        return AnthropicAPIProvider(model=model, api_key=api_key)

    from sova.llm.litellm_provider import LiteLLMProvider

    scoped_name = scoped_secret_name("llm.api_key", project_dir, provider_id)
    api_key = resolve_secret(scoped_name, db_value, legacy_name=legacy_name) if provider_id == "openai" else ""
    return LiteLLMProvider(
        model=model or "claude-sonnet-4-6", api_base=api_base or None, vendor=provider_id, api_key=api_key or None
    )


async def _check_llm_candidate(provider_id: str, cfg, project_dir: Path) -> dict:
    """Run check_available() for one llm.provider catalog candidate.

    Never raises: any construction or probe failure is reported as
    unavailable rather than breaking the whole catalog response.
    """
    is_active = provider_id == cfg.llm.provider
    model = cfg.llm.model if is_active and cfg.llm.model else _CATALOG_PLACEHOLDER_MODEL
    api_base = cfg.llm.api_base if is_active else ""
    try:
        provider = _build_llm_candidate_provider(provider_id, cfg, project_dir, model=model, api_base=api_base)
        available, detail = await provider.check_available()
    except Exception as exc:  # noqa: BLE001 (a broken candidate reports unavailable, never breaks the catalog)
        log.info("setup.connections.llm_candidate_check_failed", provider=provider_id, exc_info=True)
        return {"available": False, "detail": str(exc)}
    return {"available": available, "detail": detail}


async def _check_runtime_candidate(runtime_id: str, cfg) -> dict:
    """Run check_available() for one agent.runtime catalog candidate. Never raises."""
    from sova.ipc.runtime import create_runtime

    try:
        runtime = create_runtime(runtime_id, codex=cfg.codex)
        available, detail = await runtime.check_available()
    except Exception as exc:  # noqa: BLE001 (a broken candidate reports unavailable, never breaks the catalog)
        log.info("setup.connections.runtime_candidate_check_failed", runtime=runtime_id, exc_info=True)
        return {"available": False, "detail": str(exc)}
    return {"available": available, "detail": detail}


def _claude_cli_backend() -> Backend:
    """Return the backend a spawned Claude CLI child would actually route to.

    Detected from that child's scrubbed environment, not this (parent) process's
    raw ``os.environ``, for the same reason ``sova/llm/client.py:resolve_alias``
    does it: ``sova.ipc.runtime``/``sova.llm.providers.claude_code`` strip
    ``CLAUDE_CODE_USE_VERTEX``/``CLAUDE_CODE_USE_BEDROCK`` unless
    ``agent.env_passthrough`` opts them back in, so a routing var set in the
    server's own environment but never forwarded is not actual routing. Reading
    the raw environment here would make the Connections page hide the
    ``claude auth login`` button and tell the operator to refresh Google ADC on
    a deployment whose CLI is really firstParty (CodeRabbit, PR #1106).

    ``routing_env_vars_present()`` is a raw-``os.environ`` pre-check so the
    config load behind ``configured_passthrough()`` is only paid when one of the
    two vars is actually set.
    """
    from sova.llm.backends import detect_backend, routing_env_vars_present

    env = scrub_agent_env(passthrough=configured_passthrough()) if routing_env_vars_present() else None
    return detect_backend(SimpleNamespace(provider="claude-code"), env=env)


async def get_connection_catalog(project_dir: Path, *, is_loopback_bind: bool = True) -> dict:
    """List every supported LLM provider and agent runtime with its readiness state.

    Backs the Connections page. Every candidate is probed concurrently (each
    ``check_available()`` is an independent CLI/network call), and a failing
    candidate never prevents the others from reporting.

    ``is_loopback_bind`` feeds ``reconnect_availability()`` so the page can
    suppress/replace the `claude auth login` button when this dashboard
    session cannot complete it. Defaults to ``True`` (today's assumed-local
    behavior) for any caller that does not thread the dashboard's own bind
    signal through.
    """
    from sova.config.loader import load_config
    from sova.llm import keyring_store

    cfg = await asyncio.to_thread(load_config, project_dir)

    llm_ids = list(_LLM_PROVIDER_LABELS)
    runtime_ids = list(_RUNTIME_LABELS)
    llm_checks, runtime_checks = await asyncio.gather(
        asyncio.gather(*(_check_llm_candidate(pid, cfg, project_dir) for pid in llm_ids)),
        asyncio.gather(*(_check_runtime_candidate(rid, cfg) for rid in runtime_ids)),
    )

    # Vertex/Bedrock CLI routing is a property of the environment, not of
    # whichever llm.provider happens to be active, so it is reported on the
    # claude-code candidate regardless of whether that candidate is active.
    routing_backend = str(_claude_cli_backend())

    llm_candidates = []
    for provider_id, check in zip(llm_ids, llm_checks, strict=True):
        is_active = provider_id == cfg.llm.provider
        entry = {
            "id": provider_id,
            "label": _LLM_PROVIDER_LABELS[provider_id],
            "active": is_active,
            "requires_model": provider_id in _LLM_MODEL_REQUIRED,
            "configured_model": cfg.llm.model if is_active else "",
            # Prefilled into the page's api base input so a Validate click on the
            # active candidate probes the host it is really configured against,
            # rather than silently falling back to the vendor default.
            "configured_api_base": cfg.llm.api_base if is_active else "",
            "available": check["available"],
            "detail": check["detail"],
        }
        if provider_id == "claude-code":
            entry["routing_backend"] = routing_backend
        llm_candidates.append(entry)

    runtime_candidates = [
        {
            "id": runtime_id,
            "label": _RUNTIME_LABELS[runtime_id],
            "active": runtime_id == cfg.agent.runtime,
            "available": check["available"],
            "detail": check["detail"],
        }
        for runtime_id, check in zip(runtime_ids, runtime_checks, strict=True)
    ]

    return {
        "llm_providers": llm_candidates,
        "runtimes": runtime_candidates,
        "api_key_configured": bool(cfg.llm.api_key),
        "keyring_available": keyring_store.is_keyring_available(),
        "reconnect": reconnect_availability(is_loopback_bind=is_loopback_bind),
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


# Connections page: validate / activate / test -------------------------------


async def _build_llm_candidate_for_request(
    project_dir: Path, provider: str, model: str, api_base: str
) -> tuple[LLMProvider | None, dict | None]:
    """Turn a (provider, model, api_base) request triple into a probe-ready provider.

    Shared by validate_llm_candidate and test_llm_candidate: both reject the
    same empty-model case for a model-required provider and report a
    construction failure rather than raising. Returns ``(provider, None)`` on
    success and ``(None, error)`` otherwise; *model* and *api_base* must
    already be stripped.
    """
    if provider in _LLM_MODEL_REQUIRED and not model:
        return None, {"ok": False, "reason": "missing_model", "detail": f"'{provider}' requires an explicit model"}

    from sova.config.loader import load_config

    cfg = await asyncio.to_thread(load_config, project_dir)
    try:
        return _build_llm_candidate_provider(provider, cfg, project_dir, model=model, api_base=api_base), None
    except Exception as exc:  # noqa: BLE001 (construction failure is reported, not raised, to the caller)
        return None, {"ok": False, "reason": "create_failed", "detail": str(exc)}


async def _probe_availability(candidate: LLMProvider | AgentRuntime) -> dict:
    """Run check_available() on a candidate, in the validate endpoints' result shape.

    Shared by the LLM and runtime validate paths, which distinguish the same
    two failure modes: the probe itself blew up (check_failed) versus the probe
    answered that the candidate is not usable (not_authenticated).
    """
    try:
        available, detail = await candidate.check_available()
    except Exception as exc:  # noqa: BLE001 (probe failure is reported, not raised, to the caller)
        return {"ok": False, "reason": "check_failed", "detail": str(exc)}
    if not available:
        return {"ok": False, "reason": "not_authenticated", "detail": detail}
    return {"ok": True, "detail": detail}


async def validate_llm_candidate(project_dir: Path, provider: str, *, model: str = "", api_base: str = "") -> dict:
    """Free readiness probe for a candidate llm.provider, never persisting anything.

    Returns ``{"ok": True, "detail": ...}`` or ``{"ok": False, "reason": ..., "detail": ...}``.
    ``reason`` is one of: unknown_provider, missing_model, create_failed,
    check_failed, not_authenticated.
    """
    if provider not in _LLM_PROVIDER_LABELS:
        return {"ok": False, "reason": "unknown_provider", "detail": f"Unknown provider: {provider!r}"}

    model = model.strip()
    api_base = api_base.strip()
    candidate, error = await _build_llm_candidate_for_request(project_dir, provider, model, api_base)
    if error is not None:
        return error

    result = await _probe_availability(candidate)
    if not result["ok"] or provider not in _LLM_MODEL_REQUIRED:
        return result

    hint = await _unknown_model_hint(candidate, provider, model)
    if hint:
        result["detail"] = f"{result['detail']}; {hint}" if result["detail"] else hint
    return result


def _model_id_variants(model: str) -> set[str]:
    """Spell *model* every way an enumeration source may report the same model.

    The configured ``llm.model`` and the enumerated catalog do not always speak
    the same dialect: litellm's Vertex AI IDs carry a ``vertex_ai/`` prefix the
    Vertex publisher catalog does not (the same normalization
    ``sova/llm/client.py:resolve_alias`` applies), and Ollama's tag list always
    spells an untagged pull as ``<name>:latest``.
    """
    bare = model.removeprefix("vertex_ai/")
    return {model, bare, f"{model}:latest", f"{bare}:latest"}


async def _unknown_model_hint(candidate: LLMProvider, provider: str, model: str) -> str:
    """Return a hint when *model* is absent from the vendor's enumerated catalog, else "".

    Advisory, never a rejection. ``list_available_models()`` is best-effort
    discovery that falls back to ``CURATED_MODELS`` (four Anthropic IDs) when
    no vendor source is configured at all, which is the normal case for plain
    OpenAI with no ``api_base``. Treating that answer as authoritative rejected
    every documented openai/ollama/vertex config outright
    (``llm.model="gpt-5"`` is not in the curated Anthropic list), so an
    operator could never activate the three providers that require a model.
    Entries sourced ``"curated"`` are therefore dropped, which turns the
    fallback into "nothing to compare against" rather than a wrong answer.
    """
    try:
        models = await candidate.list_available_models()
    except Exception:  # noqa: BLE001 (enumeration is best-effort; a failure here yields no hint)
        return ""
    known_ids = {m.id for m in models if m.source != "curated"}
    if not known_ids or _model_id_variants(model) & known_ids:
        return ""
    return f"'{model}' was not in the enumerated catalog for {provider}, so it may be a typo"


async def activate_llm_candidate(project_dir: Path, provider: str, *, model: str = "", api_base: str = "") -> dict:
    """Persist *provider* as llm.provider, but only after it re-validates clean.

    All three keys are written with exactly what the card submitted, including
    an empty model or api_base: skipping an empty value would leave the old
    vendor's pin behind, so switching from openai (``llm.model="gpt-5"``) to
    anthropic would build an ``AnthropicAPIProvider(model="gpt-5")`` that fails
    on every call. Both readers of ``llm.model`` fall back when it is empty
    (``cost_service`` to ``agent.model``, ``reviewer`` to its own default), so
    clearing it is safe.

    All three keys are written in a single, all-or-nothing DB transaction via
    ``settings_service.update_config_many()`` instead of three sequential
    ``update_config()`` calls: the combined resulting config is validated
    once, so there is no write-order dependency between a model-required
    target needing its model set first and a clear of llm.model needing the
    provider switched first, and a failure partway through the write can
    never leave a partial mix of the old provider plus the new model
    persisted (issue #1148).
    """
    validation = await validate_llm_candidate(project_dir, provider, model=model, api_base=api_base)
    if not validation["ok"]:
        return {"status": "error", **validation}

    from sova.dashboard.services import settings_service

    model = model.strip()
    api_base = api_base.strip()
    result = await settings_service.update_config_many(
        project_dir, {"llm.provider": provider, "llm.model": model, "llm.api_base": api_base}
    )
    if "error" in result:
        return {"status": "error", "reason": "persist_failed", "detail": result["error"]}

    # Unlike POST /settings/config (which dispatches to _reload_all_configs()
    # on every write), this path writes straight through update_config_many()
    # and must reload the in-process provider itself: otherwise the running
    # server keeps serving LLM calls through the old provider until an
    # unrelated settings write happens to touch the llm.* prefix.
    from sova.config.loader import load_config
    from sova.llm.client import reload_provider_async

    cfg = await asyncio.to_thread(load_config, project_dir)
    await reload_provider_async(cfg, project_dir)

    _invalidate_auth_status_cache(project_dir)
    return {"status": "ok", "provider": provider}


async def test_llm_candidate(project_dir: Path, provider: str, *, model: str = "", api_base: str = "") -> dict:
    """Run one real, billable invoke() call against a candidate LLM provider.

    Distinct from validate_llm_candidate (a free check_available() probe):
    this is the one action in the Connections page explicitly labeled as
    spending money, never triggered implicitly by validate or activate.

    Rejects an unknown provider for the same reason validate_llm_candidate
    does, and more urgently: without the check, an id outside the catalog falls
    through ``_build_llm_candidate_provider``'s LiteLLM branch and reaches a
    real, billable invoke() against the caller-supplied api_base.
    """
    if provider not in _LLM_PROVIDER_LABELS:
        return {"ok": False, "reason": "unknown_provider", "detail": f"Unknown provider: {provider!r}"}

    model = model.strip()
    api_base = api_base.strip()
    candidate, error = await _build_llm_candidate_for_request(project_dir, provider, model, api_base)
    if error is not None:
        return error

    try:
        result = await candidate.invoke("Reply with exactly one word: pong", max_tokens=8, timeout=30.0)
    except Exception as exc:  # noqa: BLE001 (invocation failure is reported, not raised, to the caller)
        return {"ok": False, "reason": "invoke_failed", "detail": str(exc)}

    return {
        "ok": True,
        "text": result.text.strip(),
        "model": result.model,
        # decimal_to_json() at the JSON boundary: the cost stays a Decimal
        # through the invoke path and is serialized as a string for display,
        # never as a float (see sova/utils/formatting.py).
        "cost_usd": decimal_to_json(result.cost_usd),
    }


async def validate_runtime_candidate(project_dir: Path, runtime: str) -> dict:
    """Free readiness probe for a candidate agent.runtime, never persisting anything."""
    if runtime not in _RUNTIME_LABELS:
        return {"ok": False, "reason": "unknown_runtime", "detail": f"Unknown runtime: {runtime!r}"}

    from sova.config.loader import load_config
    from sova.ipc.runtime import create_runtime

    cfg = await asyncio.to_thread(load_config, project_dir)

    try:
        candidate = create_runtime(runtime, codex=cfg.codex)
    except Exception as exc:  # noqa: BLE001 (construction failure is reported, not raised, to the caller)
        return {"ok": False, "reason": "create_failed", "detail": str(exc)}

    return await _probe_availability(candidate)


async def activate_runtime_candidate(project_dir: Path, runtime: str) -> dict:
    """Persist *runtime* as agent.runtime, but only after it re-validates clean."""
    validation = await validate_runtime_candidate(project_dir, runtime)
    if not validation["ok"]:
        return {"status": "error", **validation}

    from sova.dashboard.services import settings_service

    result = await settings_service.update_config(project_dir, key="agent.runtime", value=runtime)
    if "error" in result:
        return {"status": "error", "reason": "persist_failed", "detail": result["error"]}

    # Mirrors activate_llm_candidate(): this path writes straight through
    # update_config() rather than POST /settings/config, so it must reload
    # the in-process runtime itself.
    from sova.config.loader import load_config
    from sova.ipc.runtime import reload_runtime

    cfg = await asyncio.to_thread(load_config, project_dir)
    reload_runtime(cfg)

    _invalidate_auth_status_cache(project_dir)
    return {"status": "ok", "runtime": runtime}


def _is_headless_environment() -> bool:
    """Return True when this server process has no display to open a browser on.

    POSIX convention only: a GUI session sets ``DISPLAY`` (X11) or
    ``WAYLAND_DISPLAY`` (Wayland), and ``BROWSER`` is an explicit operator
    override for either. Always False on Windows/macOS, where that
    convention does not apply and no equivalent headless signal is checked.
    A separate function (rather than inlined in ``reconnect_availability``)
    so tests can patch this one signal directly, the same way
    ``_claude_cli_backend`` is mocked for the routing-backend signal.
    """
    import os
    import sys

    if sys.platform in ("win32", "darwin"):
        return False
    return not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY") or os.environ.get("BROWSER"))


def reconnect_availability(*, is_loopback_bind: bool) -> dict:
    """Determine whether the CLI-owned `claude auth login` reconnect flow can run.

    Combines three independent signals (issue #1148, design decision 4): the
    dashboard's own loopback-only bind (the same ``app.state.is_loopback_bind``
    signal ``sova/dashboard/security.py`` uses to distinguish local from
    remote access), whether this server process itself has any browser to
    open (POSIX with neither ``DISPLAY`` nor ``WAYLAND_DISPLAY`` set and no
    ``BROWSER`` override; always assumed available on non-POSIX, where that
    convention does not apply), and the absence of ``CLAUDE_CODE_USE_VERTEX``/
    ``CLAUDE_CODE_USE_BEDROCK`` routing on the Claude CLI child this process
    would actually spawn (``_claude_cli_backend()``, already environment-aware
    rather than reading this process's raw ``os.environ``). A browser-based
    `claude auth login` cannot complete from a non-loopback dashboard session
    (the browser opens on the server's own machine, not the operator's) or a
    headless server process (an SSH session or container bound to loopback
    still has no display to open a browser on), and it is not how a Vertex-
    or Bedrock-routed CLI authenticates at all.

    Returns ``{"can_reconnect": bool, "reason": "" | "non_loopback" |
    "headless" | "vertex_routed" | "bedrock_routed", "detail": str}`` so the
    frontend can suppress the login button and show accurate guidance
    instead of a button that can never succeed.
    """
    from sova.llm.backends import Backend

    if not is_loopback_bind:
        return {
            "can_reconnect": False,
            "reason": "non_loopback",
            "detail": "The dashboard is not bound to loopback; reconnect cannot be completed from this session.",
        }

    if _is_headless_environment():
        return {
            "can_reconnect": False,
            "reason": "headless",
            "detail": "This server process has no display to open a browser on; reconnect cannot run here.",
        }

    backend = _claude_cli_backend()
    if backend == Backend.VERTEX:
        return {
            "can_reconnect": False,
            "reason": "vertex_routed",
            "detail": (
                "This Claude CLI is routed through Vertex AI; set up Application Default Credentials "
                "instead of `claude auth login`."
            ),
        }
    if backend == Backend.BEDROCK:
        return {
            "can_reconnect": False,
            "reason": "bedrock_routed",
            "detail": (
                "This Claude CLI is routed through Bedrock; configure AWS credentials instead of `claude auth login`."
            ),
        }
    return {"can_reconnect": True, "reason": "", "detail": ""}


# Connections page: CLI-owned `claude auth login` reconnect ------------------


@dataclass
class _ReconnectSession:
    process: asyncio.subprocess.Process
    started_at: float = field(default_factory=time.monotonic)
    status: str = "running"  # running | completed | failed | timeout | cancelled
    detail: str = ""
    # Strong reference to the _watch_reconnect task. asyncio only holds a weak
    # one, so without this the watcher can be garbage-collected mid-wait,
    # leaving status pinned at "running" forever: get_reconnect_status() would
    # never report an outcome and start_reconnect()'s single-flight check would
    # reject every later reconnect for this project.
    watcher: asyncio.Task | None = None


# Keyed by resolved project dir string. A session is present for as long as a
# reconnect has ever been started for that project (including after it
# finished), so get_reconnect_status() can report the terminal outcome once
# instead of going back to "idle" the instant the subprocess exits.
_reconnect_sessions: dict[str, _ReconnectSession] = {}
# Guards the check-then-insert in start_reconnect() so two concurrent calls
# for the same project can never both pass the "no session running" check and
# each spawn a `claude auth login` subprocess. Held only for that brief
# check-and-insert, not for the subprocess's lifetime.
_reconnect_start_lock = asyncio.Lock()


async def _watch_reconnect(session: _ReconnectSession, project_dir: Path) -> None:
    """Wait for the reconnect subprocess, applying the hard timeout.

    Runs as a background task so start_reconnect() can return immediately;
    get_reconnect_status() polls `session.status` rather than this task.
    """
    try:
        await asyncio.wait_for(session.process.wait(), timeout=_RECONNECT_TIMEOUT)
    except TimeoutError:
        if session.status != "cancelled":
            session.status = "timeout"
            session.detail = "Reconnect timed out waiting for browser login"
        with contextlib.suppress(ProcessLookupError):
            session.process.kill()
        await session.process.wait()
        return

    if session.status == "cancelled":
        return
    if session.process.returncode == 0:
        session.status = "completed"
        _invalidate_auth_status_cache(project_dir)
    else:
        session.status = "failed"
        session.detail = f"claude auth login exited with code {session.process.returncode}"


async def start_reconnect(project_dir: Path, *, is_loopback_bind: bool = True) -> dict:
    """Start a CLI-owned `claude auth login` subprocess for local reconnect.

    Single-flight per project: a second call while one is already running is
    rejected rather than spawning a duplicate subprocess. The login token is
    never read by SOVA; only the subprocess's exit code is observed, and the
    real post-login state is re-derived afterward via get_auth_status()'s own
    `claude auth status --json` probe.

    Rejected up front, before ever spawning a subprocess, when
    ``reconnect_availability()`` reports this session cannot complete the
    flow (a non-loopback dashboard bind, or a Vertex/Bedrock-routed CLI):
    see that function's docstring for why a browser-based login cannot
    succeed in either case (issue #1148).
    """
    availability = reconnect_availability(is_loopback_bind=is_loopback_bind)
    if not availability["can_reconnect"]:
        return {"status": "error", "reason": availability["reason"], "detail": availability["detail"]}

    claude_path = shutil.which("claude")
    if not claude_path:
        return {"status": "error", "reason": "cli_missing", "detail": "claude CLI not found"}

    cache_key = str(project_dir.resolve())
    async with _reconnect_start_lock:
        existing = _reconnect_sessions.get(cache_key)
        if existing is not None and existing.status == "running":
            return {"status": "error", "reason": "already_in_progress", "detail": "A reconnect is already in progress"}

        process = await asyncio.create_subprocess_exec(
            claude_path,
            "auth",
            "login",
            env=scrub_agent_env(passthrough=configured_passthrough()),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        session = _ReconnectSession(process=process)
        _reconnect_sessions[cache_key] = session
        session.watcher = asyncio.create_task(_watch_reconnect(session, project_dir))

    return {"status": "ok"}


async def get_reconnect_status(project_dir: Path) -> dict:
    """Report the current/last reconnect session's status for this project."""
    cache_key = str(project_dir.resolve())
    session = _reconnect_sessions.get(cache_key)
    if session is None:
        return {"status": "idle"}
    return {
        "status": session.status,
        "detail": session.detail,
        "elapsed_seconds": round(time.monotonic() - session.started_at, 1),
    }


async def cancel_reconnect(project_dir: Path) -> dict:
    """Terminate an in-flight reconnect subprocess so it never outlives the page."""
    cache_key = str(project_dir.resolve())
    session = _reconnect_sessions.get(cache_key)
    if session is None or session.status != "running":
        return {"status": "error", "detail": "No reconnect in progress"}

    session.status = "cancelled"
    with contextlib.suppress(ProcessLookupError):
        session.process.terminate()
    try:
        await asyncio.wait_for(session.process.wait(), timeout=_CANCEL_GRACE_PERIOD)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            session.process.kill()
    return {"status": "ok"}
