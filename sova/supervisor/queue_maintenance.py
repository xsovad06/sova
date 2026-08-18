"""Deterministic queue maintenance for the supervisor daemon.

Runs once per poll cycle (after quota sync, before progression) to:
1. Remove done/deleted issues from the queue
2. Discover ready issues (RESEARCHED state) and append them
3. Persist the updated queue to sova.toml

The LLM planner may further prune or reorder after this step.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from sova.adapters.base import Task, TaskState
from sova.utils.logging import get_logger

if TYPE_CHECKING:
    from sova.adapters.base import TaskAdapter
    from sova.config.models import SupervisorConfig

log = get_logger(component="supervisor.queue_maintenance")

_DONE_STATES = frozenset({TaskState.DONE, TaskState.HUMAN_ONLY})
_READY_STATES = frozenset({TaskState.RESEARCHED})


@dataclass(frozen=True, slots=True)
class QueueMaintenanceResult:
    """Summary of a single maintenance pass."""

    previous: tuple[int, ...]
    current: tuple[int, ...]
    removed: tuple[int, ...] = field(default_factory=tuple)
    added: tuple[int, ...] = field(default_factory=tuple)
    changed: bool = False


async def maintain_queue(
    adapter: TaskAdapter,
    config: SupervisorConfig,
    project_dir: Path,
) -> QueueMaintenanceResult:
    """Run deterministic queue maintenance: prune done, discover ready, persist.

    Skips the entire pass and returns unchanged if GitHub API calls fail
    (rate limiting, network errors). Never writes a partial queue.
    """
    previous = tuple(config.task_queue)

    try:
        tasks = await adapter.list_tasks()
    except Exception:
        log.warning("queue_maintenance.list_tasks_failed", exc_info=True)
        return QueueMaintenanceResult(previous=previous, current=previous)

    task_map: dict[int, TaskState] = {}
    task_objects: dict[int, Task] = {}
    for task in tasks:
        try:
            issue_id = int(task.id)
            task_map[issue_id] = task.state
            task_objects[issue_id] = task
        except (ValueError, TypeError):
            continue

    pruned = _prune_done(list(previous), task_map)
    pruned_set = set(pruned)
    removed = tuple(i for i in previous if i not in pruned_set)

    added_issues = _discover_ready(pruned, task_map, config.max_queue_size, task_objects)

    current = pruned + list(added_issues)
    changed = tuple(current) != previous

    if changed:
        if _save_queue_to_toml(project_dir, current):
            config.task_queue = current
            log.info(
                "queue_maintenance.updated",
                removed=list(removed),
                added=list(added_issues),
                queue_size=len(current),
            )
        else:
            current = list(previous)
            changed = False
            log.warning("queue_maintenance.persist_failed_reverting")
    else:
        log.debug("queue_maintenance.no_changes", queue_size=len(current))

    return QueueMaintenanceResult(
        previous=previous,
        current=tuple(current),
        removed=removed,
        added=added_issues,
        changed=changed,
    )


def _prune_done(queue: list[int], task_map: dict[int, TaskState]) -> list[int]:
    """Remove issues that are done or no longer exist on the tracker."""
    result: list[int] = []
    for issue_id in queue:
        state = task_map.get(issue_id)
        if state is None:
            log.info("queue_maintenance.pruned_missing", issue=issue_id)
            continue
        if state in _DONE_STATES:
            log.info("queue_maintenance.pruned_done", issue=issue_id, state=state.value)
            continue
        result.append(issue_id)
    return result


_PRIORITY_RANK: dict[str, int] = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def _extract_priority(labels: list[str]) -> str:
    for label in labels:
        lower = label.lower().strip()
        if lower.startswith("priority:"):
            value = lower[len("priority:") :].strip()
            if value in _PRIORITY_RANK:
                return value
    return ""


def _discover_ready(
    current_queue: list[int],
    task_map: dict[int, TaskState],
    max_queue_size: int,
    task_objects: dict[int, Task] | None = None,
) -> tuple[int, ...]:
    """Find issues in RESEARCHED state not already in the queue.

    Sorts by priority (critical > high > medium > low > unlabelled),
    then by issue number (lowest first) as a tie-breaker.
    """
    existing = set(current_queue)
    if max_queue_size > 0:
        capacity = max(0, max_queue_size - len(current_queue))
    else:
        capacity = None

    if capacity == 0:
        return ()

    candidates: list[int] = []
    for issue_id, state in task_map.items():
        if state in _READY_STATES and issue_id not in existing:
            candidates.append(issue_id)

    def _sort_key(issue_id: int) -> tuple[int, int]:
        if task_objects and issue_id in task_objects:
            pri = _extract_priority(task_objects[issue_id].labels)
        else:
            pri = ""
        rank = _PRIORITY_RANK.get(pri, len(_PRIORITY_RANK))
        return (rank, issue_id)

    candidates.sort(key=_sort_key)

    if capacity is not None:
        candidates = candidates[:capacity]

    return tuple(candidates)


def _save_queue_to_toml(project_dir: Path, queue: list[int]) -> bool:
    """Persist task_queue to sova.toml using atomic temp-file-then-rename.

    Returns True on success, False on failure. Callers should only update
    in-memory config after a True return.
    """
    import tempfile

    import tomlkit

    toml_path = project_dir / "sova.toml"
    try:
        doc = tomlkit.parse(toml_path.read_text()) if toml_path.exists() else tomlkit.document()
    except FileNotFoundError:
        doc = tomlkit.document()
    except Exception:
        log.warning("queue_maintenance.toml_read_failed", exc_info=True)
        return False

    if "supervisor" not in doc:
        doc["supervisor"] = tomlkit.table()
    doc["supervisor"]["task_queue"] = queue

    try:
        fd, tmp_path_str = tempfile.mkstemp(dir=project_dir, prefix=".sova-queue-", suffix=".toml.tmp")
        tmp = Path(tmp_path_str)
        try:
            with open(fd, "w") as f:
                f.write(tomlkit.dumps(doc))
            tmp.replace(toml_path)
            return True
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
    except Exception:
        log.warning("queue_maintenance.toml_write_failed", exc_info=True)
        return False


def apply_planner_queue_changes(
    config: SupervisorConfig,
    project_dir: Path,
    *,
    removals: list[int] | None = None,
    reorder: list[int] | None = None,
) -> list[int]:
    """Apply LLM planner queue suggestions (prune/reorder only, never add).

    Returns the updated queue. Persists to sova.toml if changed.
    """
    current = list(config.task_queue)
    changed = False

    if removals:
        before_len = len(current)
        remove_set = set(removals)
        current = [i for i in current if i not in remove_set]
        if len(current) != before_len:
            changed = True
            removed = remove_set & set(config.task_queue)
            log.info("queue_maintenance.planner_removed", removed=sorted(removed))

    if reorder:
        remove_set = set(removals) if removals else set()
        normalized = [i for i in reorder if i not in remove_set]
        if len(normalized) != len(set(normalized)):
            log.warning("queue_maintenance.planner_reorder_has_duplicates", reorder=reorder)
            normalized = list(dict.fromkeys(normalized))
        reorder_set = set(normalized)
        current_set = set(current)
        if reorder_set == current_set:
            current = normalized
            changed = True
            log.info("queue_maintenance.planner_reordered", new_order=current)
        elif reorder_set <= current_set:
            reordered_subset = [i for i in normalized if i in current_set]
            remainder = [i for i in current if i not in reorder_set]
            current = reordered_subset + remainder
            changed = True
            log.info("queue_maintenance.planner_partial_reorder", new_order=current)

    if changed:
        if _save_queue_to_toml(project_dir, current):
            config.task_queue = current
        else:
            return list(config.task_queue)

    return current
