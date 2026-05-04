"""Auto-handoff orchestration after agent completion."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sova.utils.logging import get_logger

if TYPE_CHECKING:
    from sova.dashboard.services.agent_pool import AgentState

log = get_logger(component="dashboard.control.handoff")


async def _process_auto_handoff(agent: AgentState) -> None:
    """Check for auto-executable handoff actions after an agent completes.

    Reads the handoff file and auto-triggers the first action marked
    with auto_execute=True. This enables role chaining (e.g., Developer
    hands off to Reviewer automatically after CI passes).
    """
    try:
        from sova.dashboard.services import agent_lifecycle
        from sova.ipc.handoff import read_handoff_file

        handoff = read_handoff_file(agent.project_dir)
        if handoff is None or handoff.status != "awaiting_action":
            return

        for action in handoff.next_actions:
            if not action.auto_execute:
                continue

            log.info(
                "auto_handoff.executing",
                run_id=agent.run_id,
                action_id=action.id,
                mode=action.mode,
                issue=handoff.issue,
            )

            if action.mode == "agent":
                args = action.args or {}
                raw_pr = args.get("pr") or handoff.pr_number
                result = await agent_lifecycle.start_agent(
                    str(args.get("issue", handoff.issue)),
                    role=args.get("role"),
                    pr_number=int(raw_pr) if raw_pr is not None else None,
                    slug=None,
                )
                log.info("auto_handoff.agent_started", result=result)
            elif action.mode == "claude-command":
                cmd = action.command.lstrip("/").split()[0] if action.command else ""
                if cmd:
                    result = await agent_lifecycle.start_command(cmd, action.args, slug=None)
                    log.info("auto_handoff.command_started", result=result)
            else:
                log.warning("auto_handoff.unsupported_mode", mode=action.mode)

            return  # only execute the first auto action

    except Exception:
        log.warning("auto_handoff.failed", run_id=agent.run_id, exc_info=True)
