"""Persona detection and loading for target projects.

Detects tech stack from marker files and loads persona guidance documents.
"""

from __future__ import annotations

from pathlib import Path

from sova.utils.logging import get_logger

log = get_logger(component="knowledge.personas")


def _has_file(project_dir: Path, name: str) -> bool:
    return (project_dir / name).is_file()


def _requirements_mention(project_dir: Path, keyword: str) -> bool:
    """Check if any requirements-style file mentions a keyword."""
    for name in ("requirements.txt", "requirements-dev.txt", "pyproject.toml", "setup.py", "setup.cfg"):
        path = project_dir / name
        if path.is_file():
            try:
                if keyword in path.read_text(encoding="utf-8").lower():
                    return True
            except OSError:
                continue
    return False


def detect_persona(project_dir: Path) -> str | None:
    """Scan project directory for marker files and detect tech stack.

    Returns the persona name (e.g., "django", "node", "python") or None.
    """
    # Django: manage.py + "django" in requirements
    if _has_file(project_dir, "manage.py") and _requirements_mention(project_dir, "django"):
        return "django"

    # Node / frontend
    if _has_file(project_dir, "package.json"):
        return "node"

    # Go
    if _has_file(project_dir, "go.mod"):
        return "go"

    # Rust
    if _has_file(project_dir, "Cargo.toml"):
        return "rust"

    # Python (generic -- after Django check)
    if _has_file(project_dir, "pyproject.toml") or _has_file(project_dir, "setup.py"):
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
