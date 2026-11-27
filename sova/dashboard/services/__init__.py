"""Flat service modules behind the dashboard routers and the supervisor, one concern per module.

Allowed imports: sova.dashboard and every layer below it (sova.supervisor, sova.roles, sova.core, sova.work,
  sova.llm, sova.git, sova.ipc, sova.adapters, sova.db, sova.config, sova.utils).
Outside the layers contract (import-linter does not check these): sova.agents, sova.awareness, sova.commands,
  sova.knowledge, sova.monitoring, sova.oversight.
"""
