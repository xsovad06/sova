"""Agent Card generation for SOVA roles."""

from __future__ import annotations

from typing import Any

from sova.roles.dispatcher import BUILTIN_ROLE_NAMES

_ROLE_DESCRIPTIONS: dict[str, str] = {
    "triage": "Assess issues for agent suitability, label, and route to the appropriate pipeline",
    "researcher": "Investigate issues, analyze codebase context, and produce structured specifications",
    "developer": "Implement features and fixes using TDD, self-review, and automated CI validation",
    "reviewer": "Review pull requests for correctness, security, design, and test coverage",
    "planner": "Scan the project and generate structured task breakdowns for epics",
}


def generate_agent_card(endpoint_base: str) -> dict[str, Any]:
    """Generate the top-level A2A Agent Card for the SOVA instance."""
    skills = [
        {
            "id": name,
            "name": name,
            "description": _ROLE_DESCRIPTIONS.get(name, f"SOVA {name} agent"),
        }
        for name in sorted(BUILTIN_ROLE_NAMES)
    ]

    return {
        "name": "sova",
        "description": "Software Orchestration Via Agents: autonomous AI-assisted development",
        "url": endpoint_base,
        "version": "1.0.0",
        "capabilities": {
            "streaming": True,
            "pushNotifications": False,
        },
        "defaultInputModes": ["text"],
        "defaultOutputModes": ["text"],
        "skills": skills,
    }


def generate_role_card(role: str, endpoint_base: str) -> dict[str, Any] | None:
    """Generate an A2A Agent Card for a single SOVA role.

    Returns None if the role is not a known built-in role.
    """
    if role not in BUILTIN_ROLE_NAMES:
        return None

    return {
        "name": f"sova-{role}",
        "description": _ROLE_DESCRIPTIONS.get(role, f"SOVA {role} agent"),
        "url": endpoint_base,
        "version": "1.0.0",
        "capabilities": {
            "streaming": True,
            "pushNotifications": False,
        },
        "defaultInputModes": ["text"],
        "defaultOutputModes": ["text"],
        "skills": [
            {
                "id": role,
                "name": role,
                "description": _ROLE_DESCRIPTIONS.get(role, f"SOVA {role} agent"),
            }
        ],
    }
