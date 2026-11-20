"""FastAPI routers mounted under ``/api``: they translate HTTP into service calls and hold no logic.

Allowed imports: sova.dashboard and every layer below it (sova.supervisor, sova.roles, sova.core, sova.llm, sova.git,
  sova.ipc, sova.adapters, sova.db, sova.config, sova.utils).
Outside the layers contract (import-linter does not check these): sova.a2a, sova.agents, sova.commands,
  sova.oversight.
"""
