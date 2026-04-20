"""Setup service -- project scanning, detection, and configuration.

Provides the business logic for the setup wizard: directory browsing,
tech stack detection, and sova.toml generation.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from sova.utils.logging import get_logger

log = get_logger(component="dashboard.setup")

_PROJECT_MARKERS = (
    ".git",
    "package.json",
    "pyproject.toml",
    "Cargo.toml",
    "go.mod",
    "manage.py",
    "Makefile",
    "requirements.txt",
)

_SKIP_DIRS = frozenset({
    "node_modules", "__pycache__", ".venv", "venv", "dist", "build",
    ".tox", ".mypy_cache", ".ruff_cache", ".pytest_cache", "target",
})


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
            has_sova = (child / "sova.toml").exists() or (child / ".claude" / "sova.db").exists()

            entries.append({
                "name": name,
                "path": str(child),
                "is_project": is_project,
                "has_sova": has_sova,
            })
    except PermissionError:
        pass

    return {"current": str(resolved), "entries": entries}


def scan_project(project_path: str) -> dict:
    """Scan a project to detect tech stack, repo, and suggest config."""
    project = Path(project_path).expanduser().resolve()
    if not project.is_dir():
        return {"error": f"Directory not found: {project}"}

    stack = _detect_tech_stack(project)
    persona = _detect_persona(stack)
    github_repo = _detect_github_repo(project)
    base_branch = _detect_base_branch(project)
    test_cmd = _detect_cmd(project, "test", "test", "")
    lint_cmd = _detect_cmd(project, "lint", "lint", "")
    format_cmd = _detect_cmd(project, "format", "format", "")

    already_installed = (project / "sova.toml").exists()
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


def generate_sova_toml(
    *,
    github_repo: str = "",
    github_user: str = "",
    base_branch: str = "main",
    test_cmd: str = "make test",
    lint_cmd: str = "make lint",
    format_cmd: str = "make format",
    task_source: str = "github",
    agent_model: str = "opus",
    max_budget: str = "10.00",
    review_max_rounds: int = 2,
    branch_naming: str = "conventional",
    commit_format: str = "conventional",
    no_ai_coauthor: bool = False,
    pr_title_format: str = "conventional",
    pr_auto_link: bool = True,
) -> str:
    """Generate sova.toml content from wizard form data."""
    return f"""# SOVA configuration
github_repo = "{github_repo}"
github_user = "{github_user}"
base_branch = "{base_branch}"
test_cmd = "{test_cmd}"
lint_cmd = "{lint_cmd}"
format_cmd = "{format_cmd}"

[task_source]
type = "{task_source}"

[agent]
model = "{agent_model}"
max_budget = "{max_budget}"

[review]
enabled = true
max_rounds = {review_max_rounds}

[commit]
format = "{commit_format}"
no_ai_coauthor = {"true" if no_ai_coauthor else "false"}

[branch]
naming = "{branch_naming}"

[pr]
title_format = "{pr_title_format}"
auto_link_issues = {"true" if pr_auto_link else "false"}

[triage]
auto_label = true
min_confidence = 0.7

[roles]
default = "developer"
"""


# -- Detection helpers --------------------------------------------------------


def _detect_tech_stack(project: Path) -> list[str]:
    stack: list[str] = []
    if (project / "requirements.txt").exists() or (project / "pyproject.toml").exists():
        stack.append("python")
        reqs = ""
        for f in ["requirements.txt", "pyproject.toml", "setup.py", "setup.cfg"]:
            p = project / f
            if p.exists():
                reqs += p.read_text(errors="ignore")
        if (project / "manage.py").exists() and "django" in reqs.lower():
            stack.append("django")
        if "fastapi" in reqs.lower():
            stack.append("fastapi")
    if (project / "package.json").exists():
        stack.append("javascript")
        pkg = (project / "package.json").read_text(errors="ignore")
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


def _detect_github_repo(project: Path) -> str:
    try:
        r = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=5, cwd=str(project),
        )
        if r.returncode == 0:
            m = re.search(r"github\.com[^:/]*[:/]([^/]+/[^/.]+)", r.stdout.strip())
            if m:
                return m.group(1)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return ""


def _detect_base_branch(project: Path) -> str:
    for branch in ["main", "master", "develop", "dev"]:
        try:
            r = subprocess.run(
                ["git", "rev-parse", "--verify", branch],
                capture_output=True, cwd=str(project), timeout=5,
            )
            if r.returncode == 0:
                return branch
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
    return "main"


def _detect_cmd(project: Path, makefile_target: str, pkg_script: str, fallback: str) -> str:
    makefile = project / "Makefile"
    if makefile.exists() and re.search(rf"^{makefile_target}:", makefile.read_text(), re.MULTILINE):
        return f"make {makefile_target}"
    pkg = project / "package.json"
    if pkg.exists() and f'"{pkg_script}"' in pkg.read_text(errors="ignore"):
        return f"npm run {pkg_script}"
    return fallback


def _read_existing_toml(project: Path) -> dict:
    """Read existing sova.toml as a flat dict for prefilling the form."""
    toml_file = project / "sova.toml"
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
