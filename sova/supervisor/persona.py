"""Supervisor persona: user-maintained guidance loaded on every planning cycle.

The persona file lives at ``~/.config/sova/supervisor_persona.md`` and is read
fresh each cycle (no caching) so edits take effect immediately.
"""

from __future__ import annotations

from pathlib import Path

from sova.oversight.persona import get_open_command
from sova.utils.logging import get_logger

__all__ = [
    "DEFAULT_SUPERVISOR_PERSONA",
    "PERSONA_FILENAME",
    "ensure_persona_exists",
    "get_open_command",
    "get_persona_info",
    "load_persona",
]

log = get_logger(component="supervisor.persona")

PERSONA_DIR = Path.home() / ".config" / "sova"
PERSONA_FILENAME = "supervisor_persona.md"

DEFAULT_SUPERVISOR_PERSONA = """\
# Supervisor Persona

## Planning style
Be conservative. Prefer delivering open PRs over starting new work.
When in doubt, wait for human approval rather than acting autonomously.

## Resource thresholds
- CodeRabbit: stop spawning reviewer-triggering work when fewer than 2 reviews/hr remain
- GitHub API: pause all spawning when fewer than 500 requests/hr remain
- CI minutes: warn when fewer than 200 minutes remain this month; stop spawning CI-triggering work below 50

## Working hours
No restrictions. Run whenever issues are ready.

## Priorities
1. Merge approved PRs (no resource cost)
2. Address open review findings
3. Start researchers on high-priority issues
4. Start developers on researched issues
5. Triage backlog issues (lowest priority)
"""


def _get_persona_path(override: str = "") -> Path:
    """Return the persona file path, respecting an explicit override."""
    if override:
        return Path(override).expanduser()
    return PERSONA_DIR / PERSONA_FILENAME


def ensure_persona_exists(persona_path_override: str = "") -> Path:
    """Create the default persona file if it does not already exist.

    Returns the resolved path to the persona file.
    """
    path = _get_persona_path(persona_path_override)
    if path.exists():
        return path

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(DEFAULT_SUPERVISOR_PERSONA, encoding="utf-8")
        log.info("persona.created", path=str(path))
    except OSError:
        log.warning("persona.create_failed", path=str(path), exc_info=True)

    return path


def load_persona(persona_path_override: str = "") -> str:
    """Load the persona content, falling back to the default template.

    Returns ``DEFAULT_SUPERVISOR_PERSONA`` when the file is missing, unreadable,
    or empty/whitespace-only.
    """
    path = _get_persona_path(persona_path_override)
    try:
        content = path.read_text(encoding="utf-8")
        if not content.strip():
            return DEFAULT_SUPERVISOR_PERSONA
        return content
    except FileNotFoundError:
        log.debug("persona.not_found", path=str(path))
        return DEFAULT_SUPERVISOR_PERSONA
    except OSError:
        log.warning("persona.load_failed", path=str(path), exc_info=True)
        return DEFAULT_SUPERVISOR_PERSONA


def get_persona_info(persona_path_override: str = "") -> dict:
    """Return persona metadata for the dashboard supervisor API.

    Derives ``exists`` from whether the file was actually read successfully,
    avoiding a TOCTOU race between ``is_file()`` and ``load_persona()``.
    """
    path = _get_persona_path(persona_path_override)
    try:
        raw = path.read_text(encoding="utf-8")
        file_existed = True
        content = raw if raw.strip() else DEFAULT_SUPERVISOR_PERSONA
    except FileNotFoundError:
        file_existed = False
        content = DEFAULT_SUPERVISOR_PERSONA
    except OSError:
        file_existed = False
        content = DEFAULT_SUPERVISOR_PERSONA
    is_default = content == DEFAULT_SUPERVISOR_PERSONA

    return {
        "path": str(path),
        "exists": file_existed,
        "is_default": is_default,
        "content": content,
    }
