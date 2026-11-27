"""Work domain logic shared by the pipeline, the supervisor and the dashboard: what state each issue and PR is in.

- ``state``: the work-item state resolver and the one table of state labels and colours.
- ``verdict``: parses SOVA's review verdicts and thread markers from a PR's review history.
- ``pr_state``: computes each open PR's state (CI, reviews, linked issues) behind a shared per-repo cache.
- ``feed``: the activity feed event bus (emit, persist, subscribe); the dashboard owns the read and query side.

Siblings of ``sova.core`` in the layers contract: ``state`` needs ``sova.core.state.TaskStatus``, and core steps emit
feed events. Nothing here imports the dashboard, the supervisor or the roles.

Allowed imports: sova.core, sova.llm, sova.git, sova.ipc, sova.adapters, sova.db, sova.config, sova.utils.
"""

from __future__ import annotations

__all__: list[str] = []
