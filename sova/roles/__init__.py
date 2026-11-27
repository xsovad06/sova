"""Role-based agent system for SOVA.

Provides the mandatory pipeline: Triage -> Research -> Develop,
with each role enforcing state gates on the tracker.

Allowed imports: sova.core, its sibling sova.work, and every layer below them (sova.llm, sova.git, sova.ipc,
  sova.adapters, sova.db, sova.config, sova.utils).
"""

from sova.roles.base import AgentRole, RoleResult, TaskAssessment
from sova.roles.developer import DeveloperRole
from sova.roles.dispatcher import dispatch, get_role, list_roles, resolve_role_for_state
from sova.roles.planner import PlannerRole
from sova.roles.researcher import ResearcherRole
from sova.roles.reviewer import ReviewerRole
from sova.roles.triage import TriageRole

__all__ = [
    "AgentRole",
    "DeveloperRole",
    "PlannerRole",
    "ResearcherRole",
    "ReviewerRole",
    "RoleResult",
    "TaskAssessment",
    "TriageRole",
    "dispatch",
    "get_role",
    "list_roles",
    "resolve_role_for_state",
]
