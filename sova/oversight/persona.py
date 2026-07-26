"""Operations persona: user-maintained guidance loaded on every oversight wake cycle.

The persona file lives at ``~/.config/sova/operations_persona.md`` and is read
fresh each cycle (no caching) so edits take effect immediately.
"""

from __future__ import annotations

import platform
from pathlib import Path

from sova.utils.logging import get_logger

log = get_logger(component="oversight.persona")

PERSONA_DIR = Path.home() / ".config" / "sova"
PERSONA_FILENAME = "operations_persona.md"

DEFAULT_PERSONA_TEMPLATE = """\
# Operations Persona

> This file defines your operational philosophy for the SOVA oversight agent.
> Edit it to shape how the agent prioritizes, escalates, and communicates.
> Changes take effect on the next wake cycle (no restart needed).

## Decision Priorities

- Prioritize issues by dependency order, then by priority label
- Prefer unblocking downstream work over starting new independent tasks
- Respect budget limits: do not spawn agents when per-issue budget is near exhaustion

## Escalation Policy

- Flag any issue that has failed the same step 3+ times
- Alert on CI failures that persist across 2+ retry cycles
- Notify when agent slot capacity is fully consumed for extended periods

## Communication Style

- Be concise: findings should be actionable, not verbose
- Use structured format: observation, impact, recommendation
- Group related observations rather than reporting each individually
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
        path.write_text(DEFAULT_PERSONA_TEMPLATE, encoding="utf-8")
        log.info("persona.created", path=str(path))
    except OSError:
        log.warning("persona.create_failed", path=str(path), exc_info=True)

    return path


def load_persona(persona_path_override: str = "") -> str:
    """Load the persona content, falling back to the default template.

    Returns ``DEFAULT_PERSONA_TEMPLATE`` when the file is missing, unreadable,
    or empty/whitespace-only.
    """
    path = _get_persona_path(persona_path_override)
    try:
        content = path.read_text(encoding="utf-8")
        if not content.strip():
            return DEFAULT_PERSONA_TEMPLATE
        return content
    except FileNotFoundError:
        log.debug("persona.not_found", path=str(path))
        return DEFAULT_PERSONA_TEMPLATE
    except OSError:
        log.warning("persona.load_failed", path=str(path), exc_info=True)
        return DEFAULT_PERSONA_TEMPLATE


def get_persona_info(persona_path_override: str = "") -> dict:
    """Return persona metadata for the dashboard settings API.

    Derives ``exists`` from whether the file was actually read successfully,
    avoiding a TOCTOU race between ``is_file()`` and ``load_persona()``.
    """
    path = _get_persona_path(persona_path_override)
    try:
        raw = path.read_text(encoding="utf-8")
        file_existed = True
        content = raw if raw.strip() else DEFAULT_PERSONA_TEMPLATE
    except FileNotFoundError:
        file_existed = False
        content = DEFAULT_PERSONA_TEMPLATE
    except OSError:
        file_existed = False
        content = DEFAULT_PERSONA_TEMPLATE
    is_default = content == DEFAULT_PERSONA_TEMPLATE

    return {
        "path": str(path),
        "exists": file_existed,
        "is_default": is_default,
        "content": content,
    }


def get_open_command() -> str | None:
    """Return the OS command to open a file in the default editor, or None."""
    system = platform.system()
    if system == "Darwin":
        return "open"
    if system == "Linux":
        return "xdg-open"
    if system == "Windows":
        return "start"
    return None
