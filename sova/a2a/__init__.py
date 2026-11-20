"""A2A (Agent-to-Agent) protocol implementation for SOVA.

Allowed imports: sova.core, sova.db, sova.roles (not a layer; chains through it are checked).
"""

from __future__ import annotations

from sova.a2a.agent_card import generate_agent_card, generate_role_card

__all__ = ["generate_agent_card", "generate_role_card"]
