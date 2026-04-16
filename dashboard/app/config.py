"""Configuration — per-project path resolution using contextvars.

Supports multi-project mode: middleware sets the project context per-request,
and all path attributes resolve dynamically via __getattr__.

Usage in services/routers:
    from app import config
    costs_file = config.COSTS_FILE   # resolves per-request
"""

import contextvars
import json
import os
from pathlib import Path

# ── Project context (set per-request by middleware) ──────────────────────────

_project_data_dir: contextvars.ContextVar[Path | None] = contextvars.ContextVar(
    "project_data_dir", default=None
)


def set_project_context(data_dir: Path):
    """Set the active project for the current async context."""
    _project_data_dir.set(data_dir)


def _default_data_dir() -> Path:
    env = os.environ.get("AGENT_DATA_DIR")
    if env:
        return Path(env)
    cwd = Path.cwd()
    for parent in [cwd, *cwd.parents]:
        candidate = parent / ".claude"
        if candidate.is_dir():
            return candidate
    return cwd / ".claude"


_DEFAULT_DATA_DIR = _default_data_dir()


# ── Dynamic attribute resolution ────────────────────────────────────────────

def _get_data_dir() -> Path:
    return _project_data_dir.get() or _DEFAULT_DATA_DIR


def __getattr__(name: str):
    dd = _get_data_dir()
    mem = dd / "agent-memory"
    scripts = dd / "scripts"

    attrs = {
        "DATA_DIR": dd,
        "MEMORY_DIR": mem,
        "SCRIPTS_DIR": scripts,
        "WORKTREE_DIR": dd / "worktrees",
        "COSTS_FILE": mem / "costs.jsonl",
        "LOG_FILE": mem / "agent.log",
        "MEMORY_DB": mem / "memory.db",
        "REVIEW_DB": mem / "review-patterns.db",
        "TASK_HISTORY_FILE": mem / "task-history.md",
        "AGENT_CONF": scripts / "gwym-agent.conf",
        "MARKDOWN_FILES": {
            "MEMORY": mem / "MEMORY.md",
            "learnings": mem / "learnings.md",
            "review-feedback": mem / "review-feedback.md",
            "common-mistakes": mem / "common-mistakes.md",
        },
        "GITHUB_REPO": _read_github_repo(scripts / "gwym-agent.conf"),
    }

    if name in attrs:
        return attrs[name]
    raise AttributeError(f"module 'app.config' has no attribute {name!r}")


def _read_github_repo(conf_path: Path) -> str:
    if not conf_path.exists():
        return ""
    try:
        for line in conf_path.read_text().splitlines():
            line = line.strip()
            if line.startswith("GITHUB_REPO="):
                val = line.split("=", 1)[1].strip().strip('"').strip("'")
                if val and val != "owner/repo":
                    return val
    except OSError:
        pass
    return ""


# ── Project Registry ────────────────────────────────────────────────────────

REGISTRY_DIR = Path.home() / ".config" / "pak"
REGISTRY_FILE = REGISTRY_DIR / "projects.json"


def _load_registry() -> dict[str, str]:
    """Load project registry: {slug: path}."""
    if REGISTRY_FILE.exists():
        try:
            return json.loads(REGISTRY_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_registry(registry: dict[str, str]):
    REGISTRY_DIR.mkdir(parents=True, exist_ok=True)
    REGISTRY_FILE.write_text(json.dumps(registry, indent=2))


def register_project(path: str, slug: str = "") -> str:
    """Register a project. Returns the slug."""
    p = Path(path).expanduser().resolve()
    if not slug:
        slug = p.name.lower().replace(" ", "-").replace("_", "-")
    registry = _load_registry()
    registry[slug] = str(p)
    _save_registry(registry)
    return slug


def unregister_project(slug: str):
    registry = _load_registry()
    registry.pop(slug, None)
    _save_registry(registry)


def list_projects() -> dict[str, str]:
    return _load_registry()


def get_project_path(slug: str) -> Path | None:
    registry = _load_registry()
    path = registry.get(slug)
    return Path(path) if path else None
