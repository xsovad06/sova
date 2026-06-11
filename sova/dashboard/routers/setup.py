"""Setup router -- project onboarding, scanning, and installation."""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from sova.config.registry import register_project
from sova.dashboard.services import setup_service

router = APIRouter(tags=["setup"])


class BrowseRequest(BaseModel):
    path: str = ""


class ScanRequest(BaseModel):
    project_path: str = ""


class InstallRequest(BaseModel):
    project_path: str
    update_only: bool = False


class ConfigureRequest(BaseModel):
    project_path: str
    task_source: str = "github"
    github_repo: str = ""
    github_user: str = ""
    base_branch: str = "main"
    branch_naming: str = "conventional"
    commit_format: str = "conventional"
    agent_model: str = "opus"
    max_budget: str = "10.00"
    review_max_rounds: int = 2
    test_cmd: str = ""
    lint_cmd: str = ""
    format_cmd: str = ""
    ai_coauthor: bool = True
    pr_title_format: str = "conventional"
    pr_auto_link: bool = True


@router.post("/setup/browse")
async def browse_directory(req: BrowseRequest):
    """List directories for the project browser."""
    return await asyncio.to_thread(setup_service.browse_directory, req.path)


@router.post("/setup/scan")
async def scan_project(req: ScanRequest):
    """Scan a project to detect tech stack and suggest configuration."""
    return await setup_service.scan_project(req.project_path)


@router.post(
    "/setup/install",
    responses={
        404: {"description": "Directory not found"},
        500: {"description": "Installation failed"},
    },
)
async def install_project(req: InstallRequest):
    """Run sova install on a project."""
    from sova.cli.commands.project import _install

    project = Path(req.project_path).expanduser().resolve()
    if not project.is_dir():
        raise HTTPException(status_code=404, detail=f"Directory not found: {project}")

    try:
        await _install(path=project, no_dashboard=True, update=req.update_only)
        slug = register_project(project)
        return {"status": "ok", "slug": slug}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/setup/configure", responses={404: {"description": "Directory not found"}})
async def configure_project(req: ConfigureRequest):
    """Generate sova.toml from wizard input and register the project."""
    project = Path(req.project_path).expanduser().resolve()
    if not project.is_dir():
        raise HTTPException(status_code=404, detail=f"Directory not found: {project}")

    toml_cfg = setup_service.TomlConfig(
        github_repo=req.github_repo,
        github_user=req.github_user,
        base_branch=req.base_branch,
        test_cmd=req.test_cmd,
        lint_cmd=req.lint_cmd,
        format_cmd=req.format_cmd,
        task_source=req.task_source,
        agent_model=req.agent_model,
        max_budget=req.max_budget,
        review_max_rounds=req.review_max_rounds,
        branch_naming=req.branch_naming,
        commit_format=req.commit_format,
        ai_coauthor=req.ai_coauthor,
        pr_title_format=req.pr_title_format,
        pr_auto_link=req.pr_auto_link,
    )
    toml_content = setup_service.generate_sova_toml(toml_cfg)

    toml_file = project / "sova.toml"
    toml_file.write_text(toml_content)

    slug = register_project(project)
    return {"status": "ok", "config_path": str(toml_file), "slug": slug}
