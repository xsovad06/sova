"""Shared helpers for inspecting package.json files."""

from __future__ import annotations

import json
from pathlib import Path


def has_dependency(project_dir: Path, package_name: str) -> bool:
    """Check if package.json lists a package in dependencies or devDependencies."""
    path = project_dir / "package.json"
    if not path.is_file():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return False
    if not isinstance(data, dict):
        return False
    for key in ("dependencies", "devDependencies"):
        deps = data.get(key, {})
        if isinstance(deps, dict) and package_name in deps:
            return True
    return False
