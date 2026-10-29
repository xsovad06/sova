"""Shared helpers for inspecting package.json files."""

from __future__ import annotations

from pathlib import Path

from sova.utils.files import read_json_or_none


def has_dependency(project_dir: Path, package_name: str) -> bool:
    """Check if package.json lists a package in dependencies or devDependencies."""
    data = read_json_or_none(project_dir / "package.json")
    if not isinstance(data, dict):
        return False
    for key in ("dependencies", "devDependencies"):
        deps = data.get(key, {})
        if isinstance(deps, dict) and package_name in deps:
            return True
    return False
