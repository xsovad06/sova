"""Canonical, provider-neutral runtime adapter layer.

Mirrors ``sova.ipc.runtime.AgentRuntime`` (which decides how to spawn a
coding agent process) with ``RuntimeAdapter`` (which decides where that
runtime's workflow commands and skills are installed on disk). See
``sova/agents/base.py`` for the full rationale.
"""

from sova.agents.base import RuntimeAdapter
from sova.agents.claude_code import ClaudeCodeAdapter
from sova.agents.codex import CodexAdapter
from sova.agents.registry import create_runtime_adapter

__all__ = ["RuntimeAdapter", "ClaudeCodeAdapter", "CodexAdapter", "create_runtime_adapter"]
