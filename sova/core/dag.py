"""DAG executor -- runs command-based workflow graphs.

Topologically sorts DAG nodes and executes them in sequence.
Each node runs a SOVA command via the agent lifecycle layer.
Validates DAG structure (cycles, missing inputs, unreachable nodes).
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from sova.core.context import ExecutionContext
from sova.db.models import StepExecution, WorkflowDefinition
from sova.db.session import get_session
from sova.utils.logging import get_logger

log = get_logger(component="dag")

CommandDispatcher = Callable[..., Awaitable[Any]]


@dataclass
class NodeResult:
    """Outcome of executing a single DAG node."""

    node_id: str
    command: str
    success: bool
    summary: str = ""
    error: str | None = None
    cost_usd: Decimal = Decimal("0")
    duration_ms: int = 0


@dataclass
class DAGResult:
    """Outcome of executing a full DAG."""

    success: bool
    summary: str
    error: str | None = None
    node_results: list[NodeResult] = field(default_factory=list)
    total_cost_usd: Decimal = Decimal("0")


class DAGExecutor:
    """Execute a workflow DAG definition."""

    def __init__(
        self,
        definition: WorkflowDefinition,
        ctx: ExecutionContext,
        command_dispatcher: CommandDispatcher,
    ) -> None:
        if command_dispatcher is None:
            raise ValueError("command_dispatcher is required and cannot be None")
        self.definition = definition
        self.ctx = ctx
        self.graph = definition.graph_json
        self._command_dispatcher = command_dispatcher

        # Pre-build incoming edge index for O(1) lookup in _should_execute
        self._incoming: dict[str, list[dict]] = {}
        for edge in self.graph.get("edges", []):
            self._incoming.setdefault(edge["target"], []).append(edge)

    async def execute(self) -> DAGResult:
        """Topological-sort nodes, execute in order, evaluate edge conditions."""
        errors, sorted_ids = validate_dag(self.graph)
        if errors:
            return DAGResult(success=False, summary="DAG validation failed", error="; ".join(errors))

        nodes = {n["id"]: n for n in self.graph.get("nodes", [])}

        results: list[NodeResult] = []
        total_cost = Decimal("0")
        context_keys: dict[str, str] = {}
        skipped: set[str] = set()

        for node_id in sorted_ids:
            node = nodes[node_id]

            # Check incoming edge conditions
            if not self._should_execute(node_id, context_keys, skipped):
                log.info("dag.node.skipped", node=node_id, command=node.get("command", ""))
                skipped.add(node_id)
                continue

            result = await self._execute_node(node)
            results.append(result)
            total_cost += result.cost_usd

            if not result.success:
                return DAGResult(
                    success=False,
                    summary=f"DAG failed at node {node_id} ({node.get('command', '')})",
                    error=result.error,
                    node_results=results,
                    total_cost_usd=total_cost,
                )

            # Track outputs for condition evaluation
            context_keys[f"{node_id}.done"] = "true"

        return DAGResult(
            success=True,
            summary=f"DAG completed: {len(results)} nodes executed",
            node_results=results,
            total_cost_usd=total_cost,
        )

    async def _execute_node(self, node: dict) -> NodeResult:
        """Run a single command node and record a StepExecution."""
        node_id = node["id"]
        command = node.get("command", "")
        label = node.get("label", command)

        log.info("dag.node.start", node=node_id, command=command)
        start = time.monotonic()
        start_dt = datetime.now(timezone.utc)

        try:
            result = await self._command_dispatcher(
                command=command,
                args=node.get("params"),
            )
            elapsed_ms = int((time.monotonic() - start) * 1000)

            success = result.get("status") != "error" if isinstance(result, dict) else True
            summary = result.get("message", str(result)) if isinstance(result, dict) else str(result)
            raw_cost = result.get("cost_usd") if isinstance(result, dict) else 0
            try:
                cost_usd = Decimal(str(raw_cost)) if raw_cost not in (None, "") else Decimal("0")
            except (InvalidOperation, TypeError, ValueError):
                cost_usd = Decimal("0")

            node_result = NodeResult(
                node_id=node_id,
                command=command,
                success=success,
                summary=summary,
                cost_usd=cost_usd,
                duration_ms=elapsed_ms,
            )
        except Exception as exc:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            node_result = NodeResult(
                node_id=node_id,
                command=command,
                success=False,
                error=str(exc),
                duration_ms=elapsed_ms,
            )

        # Persist StepExecution record
        if self.ctx.task_run_id:
            try:
                async with await get_session() as session:
                    async with session.begin():
                        step = StepExecution(
                            task_run_id=self.ctx.task_run_id,
                            step_name=command or label,
                            status="done" if node_result.success else "failed",
                            cost_usd=node_result.cost_usd,
                            duration_ms=node_result.duration_ms,
                            output_summary=node_result.summary[:500] if node_result.summary else None,
                            error_message=node_result.error,
                            started_at=start_dt,
                            ended_at=datetime.now(timezone.utc),
                        )
                        session.add(step)
            except Exception:
                log.warning("dag.step_record.failed", exc_info=True)

        log.info("dag.node.done", node=node_id, success=node_result.success, ms=elapsed_ms)
        return node_result

    def _should_execute(self, node_id: str, context_keys: dict[str, str], skipped: set[str]) -> bool:
        """Check if all incoming edges pass their conditions.

        Edges from skipped sources are treated as satisfied so that merge nodes
        after conditional branches (if/else -> join) are not blocked forever.
        At least one incoming edge must come from a completed (non-skipped) source.
        """
        incoming = self._incoming.get(node_id, [])

        if not incoming:
            return True  # Entry node

        has_completed_source = False

        for edge in incoming:
            source = edge["source"]

            # If the source was skipped (its own condition didn't match), this
            # edge is automatically satisfied -- don't block the merge node.
            if source in skipped:
                continue

            source_done = f"{source}.done" in context_keys
            condition = edge.get("condition")
            if condition is None:
                # Unconditional edge -- source must have completed
                if not source_done:
                    return False
                has_completed_source = True
            else:
                # Conditional edge -- source must have completed AND condition must pass
                if not source_done:
                    return False
                if not _evaluate_condition(condition, context_keys):
                    return False
                has_completed_source = True

        # At least one non-skipped source must have completed; a node
        # reachable only through skipped branches should not execute.
        return has_completed_source


def validate_dag(graph_json: dict) -> tuple[list[str], list[str]]:
    """Check for cycles, missing inputs, unreachable nodes, empty graphs.

    Returns (errors, sorted_ids) -- errors is empty when valid.
    The sorted_ids list can be reused by the caller to avoid a second sort.
    """
    errors: list[str] = []
    nodes = graph_json.get("nodes", [])
    edges = graph_json.get("edges", [])

    if not nodes:
        errors.append("DAG has no nodes")
        return errors, []

    node_ids = {n["id"] for n in nodes}

    # Validate edges reference existing nodes
    for edge in edges:
        if edge.get("source") not in node_ids:
            errors.append(f"Edge references unknown source node: {edge.get('source')}")
        if edge.get("target") not in node_ids:
            errors.append(f"Edge references unknown target node: {edge.get('target')}")

    # Validate all nodes have a command
    for node in nodes:
        if not node.get("command"):
            errors.append(f"Node {node['id']} has no command")

    # Cycle detection via topological sort (Kahn's algorithm)
    sorted_ids: list[str] = []
    try:
        sorted_ids = _topological_sort(graph_json)
    except ValueError as exc:
        errors.append(str(exc))

    # Disconnected node detection -- reuse directed adjacency as undirected
    if len(nodes) > 1:
        adj_undirected: dict[str, set[str]] = {nid: set() for nid in node_ids}
        for edge in edges:
            src, tgt = edge.get("source"), edge.get("target")
            if src in node_ids and tgt in node_ids:
                adj_undirected[src].add(tgt)
                adj_undirected[tgt].add(src)

        visited: set[str] = set()
        components: list[set[str]] = []
        for start in node_ids:
            if start in visited:
                continue
            component: set[str] = set()
            bfs_queue: deque[str] = deque([start])
            while bfs_queue:
                nid = bfs_queue.popleft()
                if nid in component:
                    continue
                component.add(nid)
                for neighbor in adj_undirected[nid]:
                    if neighbor not in component:
                        bfs_queue.append(neighbor)
            visited |= component
            components.append(component)

        if len(components) > 1:
            largest = max(components, key=len)
            disconnected = node_ids - largest
            errors.append(f"Unreachable nodes: {', '.join(sorted(disconnected))}")

    return errors, sorted_ids


def _topological_sort(graph_json: dict) -> list[str]:
    """Topological sort using Kahn's algorithm. Raises ValueError on cycle."""
    nodes = graph_json.get("nodes", [])
    edges = graph_json.get("edges", [])
    node_ids = {n["id"] for n in nodes}

    in_degree: dict[str, int] = {nid: 0 for nid in node_ids}
    adjacency: dict[str, list[str]] = {nid: [] for nid in node_ids}

    for edge in edges:
        src, tgt = edge["source"], edge["target"]
        if src in node_ids and tgt in node_ids:
            adjacency[src].append(tgt)
            in_degree[tgt] += 1

    queue: deque[str] = deque(nid for nid, deg in in_degree.items() if deg == 0)
    result: list[str] = []

    while queue:
        nid = queue.popleft()
        result.append(nid)
        for neighbor in adjacency[nid]:
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0:
                queue.append(neighbor)

    if len(result) != len(node_ids):
        errors_nodes = node_ids - set(result)
        raise ValueError(f"DAG contains a cycle involving nodes: {', '.join(sorted(errors_nodes))}")

    return result


def _evaluate_condition(condition: str, context_keys: dict[str, str]) -> bool:
    """Evaluate a simple condition string like 'key == value' or 'key != value'."""
    if "!=" in condition:
        parts = condition.split("!=", 1)
        if len(parts) == 2:
            key, expected = parts[0].strip(), parts[1].strip()
            return context_keys.get(key, "") != expected
    elif "==" in condition:
        parts = condition.split("==", 1)
        if len(parts) == 2:
            key, expected = parts[0].strip(), parts[1].strip()
            return context_keys.get(key, "") == expected

    log.warning("dag.condition.unknown_format", condition=condition)
    return False  # Unknown condition format -- fail-safe
