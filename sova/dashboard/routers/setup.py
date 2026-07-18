"""Setup router -- project onboarding, scanning, and installation."""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from sova.config.registry import register_project
from sova.dashboard.project_context import get_project_dir
from sova.dashboard.services import setup_service
from sova.utils.logging import get_logger

log = get_logger(component="dashboard.setup")

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
    # Jira-specific fields
    jira_base_url: str = ""
    jira_email: str = ""
    jira_project_key: str = ""
    jira_component: str = ""
    jira_status_mapping: dict[str, str] | None = None
    jira_track_agent_work: bool = False


class JiraTestRequest(BaseModel):
    base_url: str
    email: str
    api_token: str

    def __repr_args__(self) -> list[tuple[str, object]]:
        return [(k, v) for k, v in super().__repr_args__() if k != "api_token"]


class JiraProjectsRequest(BaseModel):
    base_url: str
    email: str
    api_token: str


class JiraStatusesRequest(BaseModel):
    base_url: str
    email: str
    api_token: str
    project_key: str


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
        jira_base_url=req.jira_base_url,
        jira_email=req.jira_email,
        jira_project_key=req.jira_project_key,
        jira_component=req.jira_component,
        jira_status_mapping=req.jira_status_mapping,
        jira_track_agent_work=req.jira_track_agent_work,
    )
    toml_content = setup_service.generate_sova_toml(toml_cfg)

    toml_file = project / "sova.toml"
    toml_file.write_text(toml_content)

    slug = register_project(project)

    # Best-effort: create any missing agent:* labels now so the first triage
    # doesn't fail due to missing labels. Failure is non-fatal here.
    labels_result = await setup_service.ensure_agent_labels(project)
    if created := labels_result.get("created"):
        log.info("setup.agent_labels_created", count=len(created), labels=created)

    return {"status": "ok", "config_path": str(toml_file), "slug": slug, "labels": labels_result}


@router.post("/setup/commands/sync")
async def sync_commands() -> dict[str, object]:
    """Sync canonical SOVA commands and guidelines into the active project."""
    from sova.commands.catalog import get_canonical_dir, get_guidelines_dir
    from sova.commands.distribution import UpdateResult, update_commands, update_guidelines
    from sova.commands.manifest import read_manifest
    from sova.config.loader import load_config

    project_dir = get_project_dir()
    if not project_dir or not project_dir.is_dir():
        raise HTTPException(status_code=400, detail="No active project")

    canonical_dir = get_canonical_dir()
    try:
        cfg = load_config(project_dir)
    except (FileNotFoundError, ValueError, KeyError) as e:
        raise HTTPException(status_code=400, detail=f"Failed to load project config: {e}") from e

    commands_dir = project_dir / ".claude" / "commands"
    commands_dir.mkdir(parents=True, exist_ok=True)

    cmd_result = await asyncio.to_thread(update_commands, canonical_dir, commands_dir, cfg)

    # Only sync guidelines if they were previously installed (manifest exists).
    # Without this guard, syncing installs SOVA-framework-specific templates
    # into projects that never opted into managed guidelines.
    rules_dir = project_dir / ".claude" / "rules"
    if rules_dir.is_dir() and read_manifest(rules_dir) is not None:
        guidelines_dir = get_guidelines_dir()
        guide_result = await asyncio.to_thread(update_guidelines, guidelines_dir, rules_dir, cfg)
    else:
        guide_result = UpdateResult()

    return {
        "status": "ok",
        # Backward-compatible top-level fields (commands totals)
        "updated": cmd_result.updated,
        "skipped": cmd_result.skipped,
        "conflicts": cmd_result.conflicts,
        # Structured per-category results
        "commands": {
            "updated": cmd_result.updated,
            "skipped": cmd_result.skipped,
            "conflicts": cmd_result.conflicts,
        },
        "guidelines": {
            "updated": guide_result.updated,
            "skipped": guide_result.skipped,
            "conflicts": guide_result.conflicts,
        },
    }


class CreateMilestonesRequest(BaseModel):
    project_path: str
    titles: list[str] | None = None


@router.post("/setup/milestones/create", responses={404: {"description": "Project directory not found"}})
async def create_milestones(req: CreateMilestonesRequest):
    """Create default phase milestones on the tracker."""
    project = Path(req.project_path).expanduser().resolve()
    if not project.is_dir():
        raise HTTPException(status_code=404, detail=f"Directory not found: {project}")

    return await setup_service.create_starter_milestones(project, titles=req.titles)


class EnsureLabelsRequest(BaseModel):
    project_path: str


@router.post("/setup/labels/create", responses={404: {"description": "Project directory not found"}})
async def create_agent_labels(req: EnsureLabelsRequest) -> dict:
    """Create any missing agent:* labels on the GitHub repo."""
    project = Path(req.project_path).expanduser().resolve()
    if not project.is_dir():
        raise HTTPException(status_code=404, detail=f"Directory not found: {project}")

    return await setup_service.ensure_agent_labels(project)


@router.post("/setup/jira/test")
async def test_jira_connection(req: JiraTestRequest):
    """Test Jira connection credentials."""
    return await setup_service.test_jira_connection(req.base_url, req.email, req.api_token)


@router.post("/setup/jira/projects")
async def discover_jira_projects(req: JiraProjectsRequest):
    """List accessible Jira projects."""
    return await setup_service.discover_jira_projects(req.base_url, req.email, req.api_token)


@router.post("/setup/jira/statuses")
async def discover_jira_statuses(req: JiraStatusesRequest):
    """Discover workflow statuses for a Jira project."""
    return await setup_service.discover_jira_statuses(req.base_url, req.email, req.api_token, req.project_key)
