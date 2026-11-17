"""GitHub label service: audit a repository's labels against SOVA's required set and create the missing ones.

Failures are returned as ``{"error": <fixed message>, ...}`` rather than raised, so the router can turn them
into one HTTP answer without this module knowing about HTTP. ``gh`` stderr is only ever logged: every message in
a result is one of a few fixed categories (not available, failed to list, failed to parse, permission denied,
invalid color, failed to create), so nothing a tool printed reaches a client. A creation that fails for any label
comes back with an ``errors`` list, which the router answers as a 502.
"""

from __future__ import annotations

import asyncio
import json
import re

from sova.adapters.label_catalog import REQUIRED_LABELS
from sova.config.models import ProjectConfig
from sova.utils import gh, shell
from sova.utils.logging import get_logger

log = get_logger(component="dashboard.service.github_labels")
# ``gh`` and ``shell`` are used as modules (not ``from ... import run``) so a test patching ``sova.utils.shell.run``
# or ``sova.utils.gh.resolve_gh_env`` takes effect here.

# Maximum number of errors to include in label creation response
MAX_LABEL_ERRORS = 10

# Maximum length of individual error messages to prevent unbounded response size
MAX_ERROR_MESSAGE_LENGTH = 200

# Concurrency limit for parallel label creation requests
LABEL_CREATE_CONCURRENCY = 5


async def fetch_existing_labels(cfg: ProjectConfig) -> set[str] | dict:
    """Fetch existing label names from GitHub, returning a set of names or error dict."""
    env = await gh.resolve_gh_env(cfg.github_user)

    try:
        result = await shell.run(
            "gh", "label", "list", "--repo", cfg.github_repo, "--json", "name", "--limit", "500", env=env
        )
    except FileNotFoundError:
        return {"error": "GitHub CLI not available or not authenticated"}

    if not result.success:
        error_msg = result.stderr.strip()
        if "not found" in error_msg or "not installed" in error_msg:
            return {"error": "GitHub CLI not available or not authenticated"}
        log.warning("settings.labels.fetch_failed", stderr=error_msg[:MAX_ERROR_MESSAGE_LENGTH])
        return {"error": "Failed to fetch labels"}

    try:
        existing_labels = json.loads(result.stdout)
    except json.JSONDecodeError:
        log.warning("settings.labels.parse_failed", stdout=result.stdout[:MAX_ERROR_MESSAGE_LENGTH], exc_info=True)
        return {"error": "Failed to parse GitHub CLI output"}

    if not isinstance(existing_labels, list):
        log.warning("settings.labels.unexpected_output", stdout=result.stdout[:MAX_ERROR_MESSAGE_LENGTH])
        return {"error": "Failed to parse GitHub CLI output"}
    return {label["name"] for label in existing_labels if isinstance(label, dict) and label.get("name")}


async def create_single_label(label: dict, cfg: ProjectConfig, env: dict | None) -> tuple[bool, str | None]:
    """Create a single label, returning (success, error_msg)."""
    color = label.get("color", "")
    if not re.match(r"^[0-9a-fA-F]{6}$", color):
        return (False, f"{label['name']}: Invalid color format (expected 6-char hex)")

    try:
        result = await shell.run(
            "gh",
            "label",
            "create",
            label["name"],
            "--repo",
            cfg.github_repo,
            "--color",
            color,
            "--description",
            label.get("description", ""),
            "--force",
            env=env,
        )
    except OSError:
        # gh vanished or could not be spawned mid-batch: this label fails, the others still report their own result.
        log.warning("settings.labels.create_spawn_failed", label=label["name"], exc_info=True)
        return (False, f"{label['name']}: GitHub CLI not available")

    if result.success:
        return (True, None)

    # gh's stderr is tool output, not a message for the client: it goes to the log, the response names a category.
    error_msg = result.stderr.strip()[:MAX_ERROR_MESSAGE_LENGTH]
    log.warning("settings.labels.create_failed", label=label["name"], stderr=error_msg)
    if "403" in error_msg or "permission" in error_msg.lower():
        return (False, f"{label['name']}: Permission denied (requires write access)")
    return (False, f"{label['name']}: Failed to create the label")


def build_label_response(results: list[tuple[bool, str | None]]) -> dict:
    """Build response dict from label creation results."""
    created = sum(1 for success, _ in results if success)
    errors = [err for success, err in results if not success and err]

    response: dict = {"created": created}
    if errors:
        if len(errors) > MAX_LABEL_ERRORS:
            response["errors"] = errors[:MAX_LABEL_ERRORS]
            response["errors_truncated"] = len(errors) - MAX_LABEL_ERRORS
        else:
            response["errors"] = errors

    return response


async def compute_missing_labels(cfg: ProjectConfig) -> dict:
    """Compute missing labels for a given config.

    Returns a dict with either:
    - {"missing": [...], "total_required": N} on success
    - {"error": "...", "missing": []} on failure (a fixed message; the routes turn it into a 502)
    """
    existing_or_error = await fetch_existing_labels(cfg)

    if isinstance(existing_or_error, dict):
        return {**existing_or_error, "missing": []}

    missing = [label for label in REQUIRED_LABELS if label["name"] not in existing_or_error]

    return {"missing": missing, "total_required": len(REQUIRED_LABELS)}


async def create_missing_labels(cfg: ProjectConfig) -> dict:
    """Create every required label the repository lacks.

    Returns ``{"error": ..., "missing": []}`` when the labels cannot be listed, ``{"created": 0, "message": ...}``
    when nothing is missing, else the creation counts and any (capped) errors.
    """
    result = await compute_missing_labels(cfg)
    if "error" in result:
        return result

    missing = result["missing"]
    if not missing:
        return {"created": 0, "message": "All required labels already exist"}

    # Create missing labels in parallel with controlled concurrency
    env = await gh.resolve_gh_env(cfg.github_user)
    semaphore = asyncio.Semaphore(LABEL_CREATE_CONCURRENCY)

    async def create_with_limit(label: dict) -> tuple[bool, str | None]:
        async with semaphore:
            return await create_single_label(label, cfg, env)

    results = await asyncio.gather(*[create_with_limit(label) for label in missing])

    return build_label_response(results)
