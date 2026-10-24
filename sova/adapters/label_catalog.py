"""SOVA-owned GitHub label taxonomy.

Single source of truth for the type/area/agent/sova labels this project
creates and expects on a repo's issue tracker. Shared by the dashboard
settings page (which reports which of these are missing) and
``sova.adapters.github``'s label auto-create retry, so that retry's
allowlist of labels it is permitted to mint can never drift from what the
settings page itself considers "required". Leaf module: no intra-sova
imports, so the adapter layer can depend on it without pulling in the
dashboard.
"""

from __future__ import annotations

REQUIRED_LABELS: list[dict[str, str]] = [
    # Type labels
    {"name": "type: feature", "color": "a2eeef", "description": "New feature or request"},
    {"name": "type: task", "color": "d4c5f9", "description": "General task"},
    {"name": "type: infra", "color": "0e8a16", "description": "Infrastructure or tooling"},
    {"name": "type: bug", "color": "d73a4a", "description": "Something isn't working"},
    {"name": "type: epic", "color": "bfd4f2", "description": "Multi-issue tracking container"},
    # Priority labels
    {"name": "priority: blocker", "color": "5319e7", "description": "Blocks all other work"},
    {"name": "priority: critical", "color": "b60205", "description": "Critical priority"},
    {"name": "priority: high", "color": "d93f0b", "description": "High priority"},
    {"name": "priority: medium", "color": "fbca04", "description": "Medium priority"},
    {"name": "priority: low", "color": "0e8a16", "description": "Low priority"},
    {"name": "priority: undefined", "color": "cccccc", "description": "No priority set"},
    # Area labels
    {"name": "area: agent", "color": "c5def5", "description": "Agent core and roles"},
    {"name": "area: dashboard", "color": "c5def5", "description": "Web UI"},
    {"name": "area: commands", "color": "c5def5", "description": "Claude Code commands"},
    {"name": "area: personas", "color": "c5def5", "description": "Persona guidance files"},
    {"name": "area: invariants", "color": "c5def5", "description": "Pre-push checks"},
    {"name": "area: knowledge", "color": "c5def5", "description": "Knowledge system"},
    {"name": "area: docs", "color": "c5def5", "description": "Documentation"},
    # Agent state labels
    {"name": "agent:triaged", "color": "ededed", "description": "Triaged by agent"},
    {"name": "agent:researched", "color": "ededed", "description": "Research complete"},
    {"name": "agent:ready", "color": "ededed", "description": "Ready for development"},
    {"name": "agent:in-progress", "color": "ededed", "description": "Agent working"},
    {"name": "agent:in-review", "color": "ededed", "description": "Under review"},
    {"name": "agent:needs-spec", "color": "ededed", "description": "Needs specification"},
    {"name": "agent:needs-research", "color": "ededed", "description": "Needs research"},
    {"name": "agent:human-only", "color": "ededed", "description": "Human intervention required"},
    {"name": "agent:interactive-recommended", "color": "ededed", "description": "Manual start only"},
    # SOVA review verdict labels
    {"name": "sova:approved", "color": "0e8a16", "description": "SOVA review approved"},
    {"name": "sova:revise", "color": "fbca04", "description": "SOVA review requests changes"},
    {"name": "sova:block", "color": "d73a4a", "description": "SOVA review blocks merge"},
]

# Area label vocabulary, derived from the catalog so a planner prompt or
# validator can never list a component the repo's label set doesn't have.
AREA_CHOICES: tuple[str, ...] = tuple(
    label["name"].removeprefix("area: ") for label in REQUIRED_LABELS if label["name"].startswith("area: ")
)

# name -> full catalog entry, so a label-creation path can look up the
# intended color/description for any name this catalog owns instead of
# minting every auto-created label with the same generic grey.
LABEL_CATALOG: dict[str, dict[str, str]] = {label["name"]: label for label in REQUIRED_LABELS}
