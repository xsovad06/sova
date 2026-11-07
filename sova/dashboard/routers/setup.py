"""Setup router: project onboarding, scanning, and installation."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from sova.config.context import get_project_dir, resolve_project_dir_or_none
from sova.config.loader import load_config
from sova.config.models import DashboardConfig
from sova.config.registry import register_project
from sova.dashboard.services import setup_service
from sova.dashboard.services.agent_pool import get_default_project_dir
from sova.utils.logging import get_logger

log = get_logger(component="dashboard.setup")

router = APIRouter(tags=["setup"])


_OUTSIDE_ROOTS_DETAIL = "Path is outside the directories the setup wizard may use"


def _allowed_roots() -> list[Path]:
    """Resolve the directories the setup wizard may touch: `dashboard.setup_roots` plus the project dir.

    The project directory the dashboard serves is always allowed, otherwise a project outside the
    configured roots could never be set up from its own dashboard. A config that fails to load
    leaves only that project directory (fail closed). Blank entries are skipped, since an empty path
    would resolve to the current working directory.

    With no project in scope (the global multi-project hub), `load_config(None)` would read the
    process's working directory, which under a service manager is unrelated to where the operator
    ran `sova config set`. The roots then come from the environment alone
    (`SOVA_DASHBOARD_SETUP_ROOTS`) or the default, never from the cwd.
    """
    project_dir = resolve_project_dir_or_none(default_project_dir=get_default_project_dir())
    try:
        if project_dir is None:
            configured = DashboardConfig().setup_roots
        else:
            configured = load_config(project_dir).dashboard.setup_roots
    except Exception:  # noqa: BLE001 (any config failure must fail closed to the project dir instead of a 500)
        log.warning("setup.roots_config_failed", exc_info=True)
        configured = []
    candidates = [r for r in configured if r.strip()]
    roots: list[Path] = []
    # Each entry is expanded and resolved on its own, so one bad entry (`~nosuchuser/x`, a NUL byte)
    # is logged and dropped instead of failing every setup endpoint.
    for candidate in candidates:
        try:
            resolved = Path(candidate).expanduser().resolve()
        except (RuntimeError, ValueError, OSError):
            log.warning("setup.root_entry_invalid", exc_info=True)
            continue
        if resolved not in roots:
            roots.append(resolved)
    if project_dir is not None:
        resolved_project = project_dir.resolve()
        if resolved_project not in roots:
            roots.append(resolved_project)
    return roots


def _is_within_roots(path: Path, roots: list[Path]) -> bool:
    return any(path.is_relative_to(root) for root in roots)


def _resolve_allowed_path(raw: str, roots: list[Path] | None = None) -> Path:
    """Resolve a client-supplied project path and require it to sit inside an allowed root.

    The root check runs before any existence check, so a 403 versus 404 never reveals whether a
    path outside the roots exists. Both the candidate and the roots are resolved, so symlinks and
    `..` cannot escape. The error text does not name the configured roots. `roots` defaults to
    `_allowed_roots()`; a caller that already resolved them passes them in.
    """
    try:
        resolved = Path(raw).expanduser().resolve()
    except (RuntimeError, ValueError, OSError) as exc:
        # `~nosuchuser/x` (RuntimeError) and an embedded NUL byte (ValueError) are client input errors.
        log.warning("setup.path_unresolvable", exc_info=True)
        raise HTTPException(status_code=403, detail=_OUTSIDE_ROOTS_DETAIL) from exc
    if not _is_within_roots(resolved, roots if roots is not None else _allowed_roots()):
        log.warning("setup.path_outside_roots", path=str(resolved))
        raise HTTPException(status_code=403, detail=_OUTSIDE_ROOTS_DETAIL)
    return resolved


def _first_existing_dir(roots: list[Path]) -> Path:
    """Return the first root that is a directory, or `roots[0]` when none is (the caller then 404s)."""
    return next((r for r in roots if r.is_dir()), roots[0])


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


@router.post(
    "/setup/browse",
    responses={
        403: {"description": "Path is outside the allowed setup roots"},
        404: {"description": "Directory not found"},
    },
)
async def browse_directory(req: BrowseRequest) -> dict:
    """List directories for the project browser."""
    roots = await asyncio.to_thread(_allowed_roots)
    # An empty path starts at the first allowed root that is a directory (the home directory by default).
    # A root that does not exist yet is skipped: `browse_directory` would otherwise list its parent,
    # which sits outside the roots.
    if req.path:
        start = await asyncio.to_thread(_resolve_allowed_path, req.path, roots)
    elif roots:
        start = await asyncio.to_thread(_first_existing_dir, roots)
    else:
        raise HTTPException(status_code=403, detail=_OUTSIDE_ROOTS_DETAIL)
    if not await asyncio.to_thread(start.is_dir):
        raise HTTPException(status_code=404, detail="Directory not found")
    result = await asyncio.to_thread(setup_service.browse_directory, str(start))
    # Defense in depth: never return a listing of a directory outside the roots.
    if not _is_within_roots(Path(result["current"]).resolve(), roots):
        log.warning("setup.browse_result_outside_roots", path=result["current"])
        raise HTTPException(status_code=403, detail=_OUTSIDE_ROOTS_DETAIL)
    # The root that contains the listing: the page renders ancestors above it as plain text.
    current = Path(result["current"]).resolve()
    result["root"] = str(max((r for r in roots if current.is_relative_to(r)), key=lambda r: len(r.parts)))
    # Do not offer a ".." entry that leads out of the roots.
    result["entries"] = [
        e for e in result["entries"] if e["name"] != ".." or _is_within_roots(Path(e["path"]).resolve(), roots)
    ]
    return result


@router.post("/setup/scan", responses={403: {"description": "Path is outside the allowed setup roots"}})
async def scan_project(req: ScanRequest) -> dict:
    """Scan a project to detect tech stack and suggest configuration."""
    roots = await asyncio.to_thread(_allowed_roots)
    project = await asyncio.to_thread(_resolve_allowed_path, req.project_path, roots)
    return await setup_service.scan_project(str(project))


@router.post(
    "/setup/install",
    responses={
        403: {"description": "Path is outside the allowed setup roots"},
        404: {"description": "Directory not found"},
        409: {"description": "Installed, but the project registry is unreadable"},
        500: {"description": "Installation failed"},
    },
)
async def install_project(req: InstallRequest) -> dict:
    """Run sova install on a project."""
    from sova.cli.commands.project import _install

    roots = await asyncio.to_thread(_allowed_roots)
    project = await asyncio.to_thread(_resolve_allowed_path, req.project_path, roots)
    if not project.is_dir():
        raise HTTPException(status_code=404, detail=f"Directory not found: {project}")

    try:
        await _install(path=project, no_dashboard=True, update=req.update_only)
    except Exception as exc:  # noqa: BLE001 (HTTP boundary: any internal failure becomes a 500)
        log.exception("setup.install.error", project=str(project))
        raise HTTPException(status_code=500, detail="Installation failed") from exc
    # Outside the try: a finished install must not be reported as "Installation failed" when only the
    # registration fails. An unreadable registry reaches the client as a 409 through the app's handler.
    slug = register_project(project)
    return {"status": "ok", "slug": slug}


@router.post(
    "/setup/configure",
    responses={
        403: {"description": "Path is outside the allowed setup roots"},
        404: {"description": "Directory not found"},
        422: {"description": "The configured database cannot be read back by load_config()"},
    },
)
async def configure_project(req: ConfigureRequest) -> dict:
    """Save project config to the database and register the project."""
    roots = await asyncio.to_thread(_allowed_roots)
    project = await asyncio.to_thread(_resolve_allowed_path, req.project_path, roots)
    if not project.is_dir():
        raise HTTPException(status_code=404, detail=f"Directory not found: {project}")

    from sova.config.db_loader import config_db_unreadable_reason

    # Refuse before writing: rows load_config() can never read back must not report success.
    reason = config_db_unreadable_reason(project)
    if reason is not None:
        raise HTTPException(status_code=422, detail=f"Configuration not saved: {reason}")

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
    config_dict = setup_service.generate_config_dict(toml_cfg)

    from sova.config.db_loader import save_config_to_db
    from sova.db.session import get_session, init_db_for_project

    await init_db_for_project(project)
    async with await get_session(project_dir=project) as session:
        async with session.begin():
            await save_config_to_db(session, config_dict)

    from sova.config.loader import invalidate_config_cache

    invalidate_config_cache(project)

    slug = register_project(project)
    return {"status": "ok", "config_source": "database", "slug": slug}


_SKILL_FILENAME_RE = re.compile(r"^[^/\\]+/SKILL\.md$")


def _is_safe_sync_filename(name: str) -> bool:
    """Reject path traversal or absolute paths in a client-supplied filename.

    Allows the ``{skill}/SKILL.md`` form already used by the manifest; any
    other path separator or ``..`` component is rejected.
    """
    if not name or ".." in name or name.startswith(("/", "\\")):
        return False
    if "/" in name or "\\" in name:
        return bool(_SKILL_FILENAME_RE.match(name))
    return True


def _validate_sync_filenames(names: list[str] | None, *, field_name: str) -> None:
    if names is None:
        return
    invalid = [n for n in names if not _is_safe_sync_filename(n)]
    if invalid:
        raise HTTPException(status_code=400, detail=f"Invalid {field_name}: {invalid}")


class SyncCommandsRequest(BaseModel):
    filenames: list[str] | None = None
    guideline_filenames: list[str] | None = None


@router.post("/setup/commands/sync")
async def sync_commands(req: SyncCommandsRequest | None = None) -> dict[str, object]:
    """Sync canonical SOVA commands and guidelines into the active project.

    With no body (or an empty body), syncs everything, matching the original
    behaviour. A body naming ``filenames`` and/or ``guideline_filenames``
    restricts the sync to that explicit subset, applying ``force=True`` to
    those files (the caller has already reviewed and accepted the conflict).
    Omitting ``guideline_filenames`` from a present body means "no guideline
    changes", not "all guideline changes": a selective request must name
    what it wants.
    """
    from sova.agents.claude_code import ClaudeCodeAdapter
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

    cmd_filenames = req.filenames if req is not None else None
    guideline_filenames = req.guideline_filenames if req is not None else None
    _validate_sync_filenames(cmd_filenames, field_name="filenames")
    _validate_sync_filenames(guideline_filenames, field_name="guideline_filenames")

    # A present body must name what it wants: selecting only `guideline_filenames`
    # means "skip commands" for the same reason omitting `guideline_filenames` means
    # "skip guidelines" (see docstring). A body with neither field set (no body, or
    # an empty `{}`) is a "sync everything" request, not a selective one.
    no_selection = req is None or (cmd_filenames is None and guideline_filenames is None)
    should_sync_commands = no_selection or cmd_filenames is not None

    claude_adapter = ClaudeCodeAdapter()
    commands_dir = claude_adapter.commands_dir(project_dir)
    if should_sync_commands:
        # A selective sync with an explicit empty `filenames` list is a no-op for
        # commands (see _update_files: an empty allow-list returns immediately
        # without touching target_dir). Skip creating the directory in that case
        # so a pure no-op selective sync has no side effect on a project that
        # never had a commands directory. A full sync (cmd_filenames is None) or
        # a non-empty selection still ensures the directory exists up front.
        if cmd_filenames is None or cmd_filenames:
            commands_dir.mkdir(parents=True, exist_ok=True)

        cmd_result = await asyncio.to_thread(
            update_commands,
            canonical_dir,
            commands_dir,
            cfg,
            force=cmd_filenames is not None,
            filenames=cmd_filenames,
            adapter=claude_adapter,
        )
    else:
        cmd_result = UpdateResult()

    # Only sync guidelines if they were previously installed (manifest exists).
    # Without this guard, syncing installs SOVA-framework-specific templates
    # into projects that never opted into managed guidelines.
    rules_dir = project_dir / ".claude" / "rules"
    sync_guidelines = no_selection or guideline_filenames is not None
    guidelines_synced = sync_guidelines and rules_dir.is_dir() and read_manifest(rules_dir) is not None
    if guidelines_synced:
        guidelines_dir = get_guidelines_dir()
        guide_result = await asyncio.to_thread(
            update_guidelines,
            guidelines_dir,
            rules_dir,
            cfg,
            force=guideline_filenames is not None,
            filenames=guideline_filenames,
        )
    else:
        guide_result = UpdateResult()

    log.info(
        "setup.commands.sync",
        project=str(project_dir),
        commands_synced=should_sync_commands,
        commands_updated=cmd_result.updated,
        commands_skipped=cmd_result.skipped,
        commands_conflicts=cmd_result.conflicts,
        guidelines_synced=guidelines_synced,
        guidelines_updated=guide_result.updated,
        guidelines_skipped=guide_result.skipped,
        guidelines_conflicts=guide_result.conflicts,
    )

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


@router.post(
    "/setup/milestones/create",
    responses={
        403: {"description": "Path is outside the allowed setup roots"},
        404: {"description": "Project directory not found"},
    },
)
async def create_milestones(req: CreateMilestonesRequest) -> dict:
    """Create default phase milestones on the tracker."""
    roots = await asyncio.to_thread(_allowed_roots)
    project = await asyncio.to_thread(_resolve_allowed_path, req.project_path, roots)
    if not project.is_dir():
        raise HTTPException(status_code=404, detail=f"Directory not found: {project}")

    return await setup_service.create_starter_milestones(project, titles=req.titles)


@router.post(
    "/setup/jira/test",
    responses={
        400: {"description": "Invalid Jira credentials or configuration"},
        503: {"description": "Jira server unreachable"},
    },
)
async def test_jira_connection(req: JiraTestRequest) -> dict:
    """Test Jira connection credentials."""
    try:
        return await setup_service.test_jira_connection(req.base_url, req.email, req.api_token)
    except (ValueError, KeyError) as exc:
        log.warning("setup.jira.test.config_error", exc_info=True)
        raise HTTPException(status_code=400, detail="Configuration validation failed") from exc
    except (ConnectionError, TimeoutError, OSError, httpx.HTTPError) as exc:
        log.exception("setup.jira.test.connection_error")
        raise HTTPException(status_code=503, detail="Connection test failed") from exc


@router.post(
    "/setup/jira/projects",
    responses={
        400: {"description": "Invalid Jira credentials"},
        503: {"description": "Jira server unreachable"},
    },
)
async def discover_jira_projects(req: JiraProjectsRequest) -> dict:
    """List accessible Jira projects."""
    try:
        return await setup_service.discover_jira_projects(req.base_url, req.email, req.api_token)
    except (ValueError, KeyError) as exc:
        log.warning("setup.jira.projects.config_error", exc_info=True)
        raise HTTPException(status_code=400, detail="Configuration validation failed") from exc
    except (ConnectionError, TimeoutError, OSError, httpx.HTTPError) as exc:
        log.exception("setup.jira.projects.connection_error")
        raise HTTPException(status_code=503, detail="Failed to discover Jira projects") from exc


@router.post(
    "/setup/jira/statuses",
    responses={
        400: {"description": "Invalid Jira credentials or project key"},
        503: {"description": "Jira server unreachable"},
    },
)
async def discover_jira_statuses(req: JiraStatusesRequest) -> dict:
    """Discover workflow statuses for a Jira project."""
    try:
        return await setup_service.discover_jira_statuses(req.base_url, req.email, req.api_token, req.project_key)
    except (ValueError, KeyError) as exc:
        log.warning("setup.jira.statuses.config_error", exc_info=True)
        raise HTTPException(status_code=400, detail="Configuration validation failed") from exc
    except (ConnectionError, TimeoutError, OSError, httpx.HTTPError) as exc:
        log.exception("setup.jira.statuses.connection_error")
        raise HTTPException(status_code=503, detail="Failed to discover Jira statuses") from exc
