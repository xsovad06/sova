"""SOVA dashboard web UI.

Due to circular import constraints with sova.core, symbols are not re-exported
at the package root. Import from submodules directly::

    from sova.dashboard.app import create_app

Allowed imports: sova.supervisor and every layer below it (sova.roles, sova.core, sova.llm, sova.git, sova.ipc,
  sova.adapters, sova.db, sova.config, sova.utils).
Outside the layers contract (import-linter does not check these): sova.a2a, sova.agents, sova.awareness,
  sova.commands, sova.knowledge, sova.monitoring, sova.oversight.
"""

from __future__ import annotations

__all__: list[str] = []
