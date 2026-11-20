"""Supervisor subsystem: cross-cutting orchestration and resource management.

Allowed imports: sova.roles and every layer below it (sova.core, sova.llm, sova.git, sova.ipc, sova.adapters, sova.db,
  sova.config, sova.utils).
Outside the layers contract (import-linter does not check these): sova.commands, sova.knowledge, sova.oversight.
"""
