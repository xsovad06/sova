"""Persona detection and loading for target projects.

Detects tech stack from marker files and loads persona guidance documents.
"""

from __future__ import annotations

import os
from pathlib import Path

from sova.utils.logging import get_logger

log = get_logger(component="knowledge.personas")

_SETUP_PY = "setup.py"


def _has_file(project_dir: Path, name: str) -> bool:
    return (project_dir / name).is_file()


def _requirements_mention(project_dir: Path, keyword: str) -> bool:
    """Check if any requirements-style file mentions a keyword.

    Scans root-level requirements files, pyproject.toml, and also checks
    nested setup.py files (e.g. app/setup.py in monorepos).
    """
    candidates = ["requirements.txt", "requirements-dev.txt", "pyproject.toml", _SETUP_PY, "setup.cfg"]
    for name in candidates:
        path = project_dir / name
        if path.is_file():
            try:
                if keyword in path.read_text(encoding="utf-8").lower():
                    return True
            except OSError:
                continue
    # Check one level deep for monorepo layouts (e.g. app/setup.py)
    try:
        children = list(project_dir.iterdir())
    except OSError:
        return False
    for child in children:
        if child.is_dir() and not child.name.startswith("."):
            for name in (_SETUP_PY, "requirements.txt"):
                path = child / name
                if path.is_file():
                    try:
                        if keyword in path.read_text(encoding="utf-8").lower():
                            return True
                    except OSError:
                        continue
    return False


_WALK_SKIP = {".git", "node_modules", ".venv", "__pycache__", ".tox", ".mypy_cache", ".nox", ".eggs"}


def _has_odoo_manifest(project_dir: Path) -> bool:
    """Check if the project contains an Odoo-style __manifest__.py."""
    for root, dirs, files in os.walk(project_dir):
        dirs[:] = [d for d in dirs if d not in _WALK_SKIP and not d.startswith(".")]
        if "__manifest__.py" in files:
            manifest = Path(root) / "__manifest__.py"
            try:
                content = manifest.read_text(encoding="utf-8").lower()
                if "installable" in content or "'depends'" in content or '"depends"' in content:
                    return True
            except OSError:
                continue
    return False


def detect_persona(project_dir: Path) -> str | None:
    """Scan project directory for marker files and detect tech stack.

    Returns the persona name (e.g., "django", "fastapi", "python") or None.
    Detection order: specific frameworks first, generic language fallbacks last.
    """
    # Django: manage.py + "django" in requirements
    if _has_file(project_dir, "manage.py") and _requirements_mention(project_dir, "django"):
        return "django"

    # Odoo: __manifest__.py with Odoo-style keys
    if _has_odoo_manifest(project_dir):
        return "odoo"

    # FastAPI: "fastapi" in any requirements file (including nested)
    if _requirements_mention(project_dir, "fastapi"):
        return "fastapi"

    # Node / frontend
    if _has_file(project_dir, "package.json"):
        return "node"

    # Go
    if _has_file(project_dir, "go.mod"):
        return "go"

    # Rust
    if _has_file(project_dir, "Cargo.toml"):
        return "rust"

    # Python (generic -- after framework-specific checks)
    if _has_file(project_dir, "pyproject.toml") or _has_file(project_dir, _SETUP_PY):
        return "python"

    # Ruby
    if _has_file(project_dir, "Gemfile"):
        return "ruby"

    return None


def load_persona(name: str, personas_dir: Path | None = None) -> str:
    """Load persona guidance markdown for a given tech stack.

    Args:
        name: Persona name (e.g., "django", "python").
        personas_dir: Directory containing persona .md files.
            Defaults to the repo's ``personas/`` directory.

    Returns:
        File content as string, or empty string if not found.
    """
    if personas_dir is None:
        # Default: repo root / personas/
        personas_dir = Path(__file__).resolve().parent.parent.parent / "personas"

    path = personas_dir / f"{name}.md"
    if not path.is_file():
        log.debug("persona.not_found", name=name, path=str(path))
        return ""

    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        log.warning("persona.read_error", name=name, path=str(path))
        return ""
