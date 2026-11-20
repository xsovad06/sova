"""Core workflow engine and execution context.

Due to circular import constraints with sova.dashboard, symbols are not re-exported
at the package root. Import from submodules directly::

    from sova.core.workflow import WorkflowEngine
    from sova.core.context import ExecutionContext
    from sova.core.state import TaskStatus

Allowed imports: sova.llm, sova.git, sova.ipc, sova.adapters, sova.db, sova.config, sova.utils.
Outside the layers contract (import-linter does not check these): sova.commands.
"""

from __future__ import annotations

__all__: list[str] = []
