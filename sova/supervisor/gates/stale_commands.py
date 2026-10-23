"""Stale-commands gate: blocks a command whose installed recipe predates its fix.

Complements the pre-spawn check in ``agent_validation.check_stale_command``:
this one stops the supervisor *deciding* to run an outdated ``/address-pr`` or
``/integrate-pr``, so the operator sees a blocked decision naming the sync
command rather than a run that silently executes the unfixed recipe (#1251).
Only command-carried actions reach it (``_ACTION_TO_COMMAND``); agent roles
run pipeline steps from the installed SOVA package, which cannot be stale
relative to itself.
"""

from __future__ import annotations

from pathlib import Path

from sova.commands.staleness import find_stale_command
from sova.config.models import ProjectConfig
from sova.supervisor.gates import BlockReason
from sova.utils.logging import get_logger

log = get_logger(component="supervisor.gates.stale_commands")


def check_stale_commands_gate(project_dir: Path, command: str, cfg: ProjectConfig) -> BlockReason | None:
    """Block ``command`` when it is safety-critical and its installed copy is stale. Fail-open."""
    try:
        reason = find_stale_command(project_dir, command, cfg)
    except Exception:  # noqa: BLE001 (gates fail open: an unevaluable gate must not block progression)
        log.debug("stale_commands_gate.check_failed", command=command, exc_info=True)
        return None
    if reason is None:
        return None
    return BlockReason(gate="stale_commands", detail=reason)
