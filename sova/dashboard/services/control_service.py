"""Agent control -- backward-compatible re-exports.

Split into focused modules:
- agent_pool.py -- data models, slot management, project-scoped collections
- agent_db.py -- TaskRun/CostRecord CRUD for dashboard-spawned agents
- agent_lifecycle.py: process start/stop/wait, status queries
- agent_resource.py: resource monitoring, background task tracking
- agent_finalize.py: wait-and-finalize, crash recovery, merge queue checks
- agent_approval.py: spec approval/rejection, lifecycle integration
- agent_output.py -- output streaming and stream-json parsing
- agent_recovery.py -- stale run detection and PID checks
- agent_handoff.py -- auto-handoff orchestration
"""

from sova.dashboard.services.agent_db import (
    _create_task_run as _create_task_run,
)
from sova.dashboard.services.agent_db import (
    _fetch_run_states as _fetch_run_states,
)
from sova.dashboard.services.agent_db import (
    _finalize_task_run as _finalize_task_run,
)
from sova.dashboard.services.agent_handoff import _process_auto_handoff as _process_auto_handoff
from sova.dashboard.services.agent_lifecycle import (
    _ADDRESS_REVIEW_ONLY as _ADDRESS_REVIEW_ONLY,
)
from sova.dashboard.services.agent_lifecycle import (
    _RESEARCHER_ONLY as _RESEARCHER_ONLY,
)
from sova.dashboard.services.agent_lifecycle import (
    ADDRESS_REVIEW_PIPELINE as ADDRESS_REVIEW_PIPELINE,
)
from sova.dashboard.services.agent_lifecycle import (
    DEVELOPER_PIPELINE as DEVELOPER_PIPELINE,
)
from sova.dashboard.services.agent_lifecycle import (
    RESEARCHER_PIPELINE as RESEARCHER_PIPELINE,
)
from sova.dashboard.services.agent_lifecycle import (
    _resolve_command_prompt as _resolve_command_prompt,
)
from sova.dashboard.services.agent_lifecycle import (
    _resolve_project_gh_env as _resolve_project_gh_env,
)
from sova.dashboard.services.agent_lifecycle import (
    _strip_frontmatter as _strip_frontmatter,
)
from sova.dashboard.services.agent_lifecycle import (
    _transition_to_in_progress as _transition_to_in_progress,
)
from sova.dashboard.services.agent_lifecycle import (
    _wait_and_finalize as _wait_and_finalize,
)
from sova.dashboard.services.agent_lifecycle import (
    complete_awaiting_approval_by_issue as complete_awaiting_approval_by_issue,
)
from sova.dashboard.services.agent_lifecycle import (
    get_all_agents as get_all_agents,
)
from sova.dashboard.services.agent_lifecycle import (
    get_status as get_status,
)
from sova.dashboard.services.agent_lifecycle import (
    get_step_progress as get_step_progress,
)
from sova.dashboard.services.agent_lifecycle import (
    get_unified_agents as get_unified_agents,
)
from sova.dashboard.services.agent_lifecycle import (
    reject_spec as reject_spec,
)
from sova.dashboard.services.agent_lifecycle import (
    resume_from_approval as resume_from_approval,
)
from sova.dashboard.services.agent_lifecycle import (
    start_agent as start_agent,
)
from sova.dashboard.services.agent_lifecycle import (
    start_command as start_command,
)
from sova.dashboard.services.agent_lifecycle import (
    stop_agent as stop_agent,
)
from sova.dashboard.services.agent_output import (
    _parse_stream_line as _parse_stream_line,
)
from sova.dashboard.services.agent_output import (
    _read_output as _read_output,
)
from sova.dashboard.services.agent_output import (
    _read_stderr as _read_stderr,
)
from sova.dashboard.services.agent_output import (
    get_output as get_output,
)
from sova.dashboard.services.agent_pool import (
    _DEFAULT_SLUG as _DEFAULT_SLUG,
)
from sova.dashboard.services.agent_pool import (
    MAX_RECENTLY_COMPLETED as MAX_RECENTLY_COMPLETED,
)
from sova.dashboard.services.agent_pool import (
    RECENTLY_COMPLETED_TTL as RECENTLY_COMPLETED_TTL,
)
from sova.dashboard.services.agent_pool import (
    AgentState as AgentState,
)
from sova.dashboard.services.agent_pool import (
    CompletedAgent as CompletedAgent,
)
from sova.dashboard.services.agent_pool import (
    ProjectAgents as ProjectAgents,
)
from sova.dashboard.services.agent_pool import (
    _default_project_dir as _default_project_dir,
)
from sova.dashboard.services.agent_pool import (
    _evict_completed_for_issue as _evict_completed_for_issue,
)
from sova.dashboard.services.agent_pool import (
    _get_project_agents as _get_project_agents,
)
from sova.dashboard.services.agent_pool import (
    _projects as _projects,
)
from sova.dashboard.services.agent_pool import (
    _prune_completed as _prune_completed,
)
from sova.dashboard.services.agent_pool import (
    set_project_dir as set_project_dir,
)
from sova.dashboard.services.agent_recovery import (
    _is_process_alive as _is_process_alive,
)
from sova.dashboard.services.agent_recovery import (
    recover_stale_runs as recover_stale_runs,
)
from sova.dashboard.services.output_service import OutputWriter as OutputWriter
from sova.dashboard.services.work_service import get_kanban_columns as get_kanban_columns
from sova.dashboard.services.work_service import get_recent_failed_runs as get_recent_failed_runs
