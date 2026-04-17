"""Setup router — project onboarding, scanning, and installation."""

import os
import re
import subprocess
from datetime import date
from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel

from app import config

router = APIRouter(tags=["setup"])

# Resolve PAK_ROOT from env or by walking up from this file
PAK_ROOT = Path(os.environ.get("PAK_ROOT", Path(__file__).resolve().parents[3]))
INSTALL_SCRIPT = PAK_ROOT / "agent" / "install.sh"


class SetupRequest(BaseModel):
    project_path: str
    task_source: str = "github"
    github_repo: str = ""
    base_branch: str = "main"
    branch_naming: str = "conventional"
    commit_format: str = "conventional"
    agent_model: str = "opus"
    max_budget: str = "10.00"
    review_max_rounds: int = 2
    test_cmd: str = ""
    lint_cmd: str = ""
    format_cmd: str = ""
    no_ai_coauthor: bool = False
    pr_title_format: str = "conventional"
    pr_auto_link: bool = True


class ScanRequest(BaseModel):
    project_path: str = ""


class InstallRequest(BaseModel):
    project_path: str
    no_dashboard: bool = True


class BrowseRequest(BaseModel):
    path: str = ""


# ── Helpers ──────────────────────────────────────────────────────────────────


def _resolve_project(path: str) -> Path:
    return Path(path).expanduser().resolve()


def _read_conf(conf_path: Path) -> dict:
    config = {}
    if conf_path.exists():
        for line in conf_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, val = line.split("=", 1)
                config[key.strip()] = val.strip().strip('"').strip("'")
    return config


def _detect_tech_stack(project: Path) -> list[str]:
    stack = []
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
        if "celery" in reqs.lower():
            stack.append("celery")
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
    # Odoo
    try:
        manifests = list(project.rglob("__manifest__.py"))
        if any("'name'" in m.read_text(errors="ignore") for m in manifests[:5]):
            stack.append("odoo")
    except OSError:
        pass
    if (project / ".github" / "workflows").is_dir():
        stack.append("github-actions")
    return stack


def _detect_persona(stack: list[str]) -> str:
    if "django" in stack:
        return "django"
    if "fastapi" in stack:
        return "fastapi"
    if "odoo" in stack:
        return "odoo"
    if "react" in stack:
        return "react"
    if "go" in stack:
        return "go-service"
    if "rust" in stack:
        return "rust"
    if "javascript" in stack:
        return "frontend"
    return ""


def _detect_github_repo(project: Path) -> str:
    try:
        r = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=str(project),
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
        r = subprocess.run(
            ["git", "rev-parse", "--verify", branch],
            capture_output=True,
            cwd=str(project),
            timeout=5,
        )
        if r.returncode == 0:
            return branch
    return "main"


def _detect_cmd(project: Path, makefile_target: str, pkg_script: str, fallback: str) -> str:
    makefile = project / "Makefile"
    if makefile.exists() and re.search(rf"^{makefile_target}:", makefile.read_text(), re.MULTILINE):
        return f"make {makefile_target}"
    pkg = project / "package.json"
    if pkg.exists() and f'"{pkg_script}"' in pkg.read_text(errors="ignore"):
        return f"npm run {pkg_script}"
    return fallback


# ── Endpoints ────────────────────────────────────────────────────────────────


@router.get("/setup/status")
async def setup_status(project_path: str = ""):
    """Get setup status for a project (or the current dashboard project)."""
    if project_path:
        project = _resolve_project(project_path)
        conf_file = project / ".claude" / "scripts" / "pak-agent.conf"
    else:
        conf_file = config.SCRIPTS_DIR / "pak-agent.conf"
        project = config.DATA_DIR.parent

    conf_data = _read_conf(conf_file)
    has_agent = (project / ".claude" / "scripts" / "pak-agent.sh").exists()

    return {
        "installed": conf_file.exists(),
        "has_agent": has_agent,
        "config": conf_data,
        "project_path": str(project),
        "data_dir": str(project / ".claude"),
    }


@router.post("/setup/browse")
async def browse_directory(req: BrowseRequest):
    """List directories for the project browser."""
    path = Path(req.path).expanduser().resolve() if req.path else Path.home()

    if not path.is_dir():
        path = path.parent

    # Build entries: parent + subdirectories
    entries = []

    # Parent directory
    if path != path.parent:
        entries.append(
            {
                "name": "..",
                "path": str(path.parent),
                "is_project": False,
            }
        )

    # Subdirectories (sorted, skip hidden except .claude)
    try:
        for child in sorted(path.iterdir()):
            if not child.is_dir():
                continue
            name = child.name
            # Skip hidden dirs, node_modules, venvs, etc.
            if name.startswith(".") and name != ".claude":
                continue
            if name in ("node_modules", "__pycache__", ".venv", "venv", "dist", "build"):
                continue

            # Detect if this looks like a project (has git, package.json, etc.)
            is_project = any(
                (child / marker).exists()
                for marker in [
                    ".git",
                    "package.json",
                    "pyproject.toml",
                    "Cargo.toml",
                    "go.mod",
                    "manage.py",
                    "Makefile",
                    "requirements.txt",
                ]
            )

            # Check if PAK is already installed
            has_pak = (child / ".claude" / "scripts" / "pak-agent.sh").exists()

            entries.append(
                {
                    "name": name,
                    "path": str(child),
                    "is_project": is_project,
                    "has_pak": has_pak,
                }
            )
    except PermissionError:
        pass

    return {
        "current": str(path),
        "entries": entries,
    }


@router.post("/setup/scan")
async def scan_project(req: ScanRequest):
    """Scan a project to detect tech stack and suggest configuration."""
    project = _resolve_project(req.project_path) if req.project_path else config.DATA_DIR.parent

    if not project.is_dir():
        return {"error": f"Directory not found: {project}"}

    stack = _detect_tech_stack(project)
    persona = _detect_persona(stack)
    github_repo = _detect_github_repo(project)
    base_branch = _detect_base_branch(project)
    test_cmd = _detect_cmd(project, "test", "test", "")
    lint_cmd = _detect_cmd(project, "lint", "lint", "")
    format_cmd = _detect_cmd(project, "format", "format", "")

    # Check existing install
    conf_file = project / ".claude" / "scripts" / "pak-agent.conf"
    existing_config = _read_conf(conf_file)

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
        "already_installed": conf_file.exists(),
        "existing_config": existing_config,
    }


@router.post("/setup/install")
async def install_project(req: InstallRequest):
    """Run pak install on a project."""
    project = _resolve_project(req.project_path)

    if not project.is_dir():
        return {"error": f"Directory not found: {project}"}

    if not INSTALL_SCRIPT.exists():
        return {"error": f"Install script not found: {INSTALL_SCRIPT}"}

    args = [str(INSTALL_SCRIPT)]
    if req.no_dashboard:
        args.append("--no-dashboard")

    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(project),
        )
        # Auto-register on successful install
        slug = ""
        if result.returncode == 0:
            slug = config.register_project(str(project))
        return {
            "status": "ok" if result.returncode == 0 else "error",
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "slug": slug,
        }
    except subprocess.TimeoutExpired:
        return {"status": "error", "error": "Installation timed out (30s)"}


@router.post("/setup/configure")
async def configure_project(req: SetupRequest):
    """Generate project configuration from wizard input."""
    project = _resolve_project(req.project_path)
    conf_dir = project / ".claude" / "scripts"
    conf_file = conf_dir / "pak-agent.conf"
    conf_dir.mkdir(parents=True, exist_ok=True)

    conf_content = f"""# Project Automation Kit — Configuration
# Generated by dashboard setup wizard on {date.today()}

# Task Source
TASK_SOURCE="{req.task_source}"
TASK_SOURCE_CONFIG=""

# Project Settings
GITHUB_REPO="{req.github_repo}"
GITHUB_USER=""
TEST_CMD="{req.test_cmd}"
LINT_CMD="{req.lint_cmd}"
FORMAT_CMD="{req.format_cmd}"
BASE_BRANCH="{req.base_branch}"

# Agent Settings
AGENT_MODEL="{req.agent_model}"
MAX_BUDGET="{req.max_budget}"
REVIEW_ENABLED="true"
REVIEW_MAX_ROUNDS={req.review_max_rounds}

# Commit & PR Settings
NO_AI_COAUTHOR="{"true" if req.no_ai_coauthor else "false"}"
COMMIT_FORMAT="{req.commit_format}"
PR_TITLE_FORMAT="{req.pr_title_format}"
PR_AUTO_LINK_ISSUES="{"true" if req.pr_auto_link else "false"}"
BRANCH_NAMING="{req.branch_naming}"

# Defaults
PERSONA_MAP=""
ISSUE_MILESTONE=""
ISSUE_LABELS=""
SKIP_MANUAL_TEST="true"
AUTO_APPROVE_FIXES="false"
CI_POLL_INTERVAL=60
CI_MAX_WAIT=600
FLAKY_CHECKS=""
SCANNER_GITHUB_CHECK="true"
WATCH_INTERVAL_ACTIVE=300
WATCH_INTERVAL_IDLE=1800
WATCH_AUTO_SELECT_ISSUES="true"
WATCH_VETO_SECONDS=30
WORKTREE_COPY_FILES=".env,.env.local"
WORKTREE_TTL_DONE_DAYS=3
WORKTREE_TTL_PAUSED_DAYS=7
SHARED_KNOWLEDGE_DIR="$HOME/.claude/shared-knowledge"
INVARIANTS_DIR=""
MAX_PARALLEL_AGENTS=2
COMMIT_AUTHOR=""
SLACK_CHANNEL=""
"""
    conf_file.write_text(conf_content)

    # Auto-register project in the dashboard registry
    slug = config.register_project(str(project))

    return {"status": "ok", "config_path": str(conf_file), "slug": slug}
