"""Role-based agent system for SOVA.

Provides the mandatory pipeline: Triage -> Research -> Develop,
with each role enforcing state gates on the tracker.
"""

from sova.roles.base import AgentRole, RoleResult, TaskAssessment
from sova.roles.developer import DeveloperRole
from sova.roles.dispatcher import dispatch, get_role, list_roles, resolve_role_for_state
from sova.roles.researcher import ResearcherRole
from sova.roles.reviewer import ReviewerRole
from sova.roles.triage import TriageRole

__all__ = [
    "AgentRole",
    "DeveloperRole",
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
