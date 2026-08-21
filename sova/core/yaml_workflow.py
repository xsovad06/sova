"""YAML workflow parser and validator.

Parses YAML workflow definitions into WorkflowDefinition models.
Validates structure, detects cycles, and converts to graph_json format.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator

from sova.core.dag import validate_dag
from sova.db.models import WorkflowDefinition
from sova.utils.logging import get_logger

log = get_logger(component="yaml_workflow")

# Built-in role names that cannot be used for custom workflows
_BUILTIN_ROLES = frozenset({"developer", "researcher", "reviewer", "triage", "planner"})


class YAMLWorkflowError(Exception):
    """Raised when YAML workflow parsing or validation fails."""


class YAMLStep(BaseModel):
    """A single step in a YAML workflow."""

    name: str = Field(..., description="Unique step identifier")
    command: str = Field(..., description="SOVA command to execute")
    prompt_template: str = Field(default="", description="Prompt template for the command")
    depends_on: list[str] = Field(default_factory=list, description="List of step names this depends on")
    condition: str | None = Field(default=None, description="Jinja2 condition for execution")
    timeout: int | None = Field(default=None, description="Timeout in seconds")
    model: str | None = Field(default=None, description="LLM model to use")


class YAMLWorkflow(BaseModel):
    """YAML workflow schema."""

    name: str = Field(..., description="Workflow name (unique identifier)")
    description: str = Field(default="", description="Workflow description")
    steps: list[YAMLStep] = Field(..., min_length=1, description="Workflow steps")

    @field_validator("name")
    @classmethod
    def validate_name_not_builtin(cls, v: str) -> str:
        """Ensure workflow name doesn't conflict with built-in roles."""
        if v in _BUILTIN_ROLES:
            raise ValueError(f"Workflow name '{v}' conflicts with built-in role")
        return v

    @field_validator("steps")
    @classmethod
    def validate_unique_step_names(cls, v: list[YAMLStep]) -> list[YAMLStep]:
        """Ensure step names are unique."""
        names = [step.name for step in v]
        if len(names) != len(set(names)):
            duplicates = [name for name in names if names.count(name) > 1]
            raise ValueError(f"Duplicate step names: {', '.join(set(duplicates))}")
        return v


def parse_yaml_workflow(path: Path) -> WorkflowDefinition:
    """Parse a YAML workflow file into a WorkflowDefinition.

    Args:
        path: Path to YAML workflow file

    Returns:
        WorkflowDefinition ready for execution

    Raises:
        YAMLWorkflowError: On parse errors, validation failures, or cycles
    """
    try:
        content = path.read_text()
    except (OSError, UnicodeDecodeError) as exc:
        raise YAMLWorkflowError(f"Failed to read {path}: {exc}") from exc

    try:
        raw_data = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        raise YAMLWorkflowError(f"YAML syntax error in {path}: {exc}") from exc

    if not isinstance(raw_data, dict):
        raise YAMLWorkflowError(f"YAML file {path} must contain a dictionary at root")

    try:
        workflow = YAMLWorkflow.model_validate(raw_data)
    except Exception as exc:
        raise YAMLWorkflowError(f"Validation failed for {path}: {exc}") from exc

    graph_json = _convert_to_graph_json(workflow)

    errors, _ = validate_dag(graph_json)
    if errors:
        raise YAMLWorkflowError(f"DAG validation failed for {path}: {'; '.join(errors)}")

    return WorkflowDefinition(
        name=workflow.name,
        description=workflow.description,
        graph_json=graph_json,
        input_states=[],
        output_state="",
        version=1,
        is_builtin=False,
    )


def _convert_to_graph_json(workflow: YAMLWorkflow) -> dict[str, Any]:
    """Convert YAML workflow to graph_json format (nodes + edges)."""
    nodes = []
    edges = []

    # Build step name to index mapping for validation
    step_names = {step.name for step in workflow.steps}

    for idx, step in enumerate(workflow.steps):
        # Validate dependencies exist
        for dep in step.depends_on:
            if dep not in step_names:
                raise YAMLWorkflowError(f"Step '{step.name}' depends on unknown step '{dep}'")

        # Build node
        params: dict[str, Any] = {}
        if step.timeout is not None:
            params["timeout"] = step.timeout
        if step.model is not None:
            params["model"] = step.model
        if step.prompt_template:
            params["prompt"] = step.prompt_template

        node = {
            "id": step.name,
            "command": step.command,
            "label": step.name,
            "position": {"x": 100 + (idx * 200), "y": 100},
        }
        if params:
            node["params"] = params

        nodes.append(node)

        # Build edges for dependencies
        for dep in step.depends_on:
            edge = {
                "id": f"{dep}_to_{step.name}",
                "source": dep,
                "target": step.name,
            }
            if step.condition:
                edge["condition"] = step.condition
            edges.append(edge)

    return {"nodes": nodes, "edges": edges}


def discover_yaml_workflows(directories: list[Path]) -> list[WorkflowDefinition]:
    """Discover all valid YAML workflows from given directories.

    Args:
        directories: List of directories to scan for .yaml/.yml files

    Returns:
        List of parsed WorkflowDefinition objects (invalid files are skipped)
    """
    workflows = []

    for directory in directories:
        if not directory.exists() or not directory.is_dir():
            log.debug("yaml.discover.skip", path=str(directory), reason="not a directory")
            continue

        for file_path in directory.glob("*.yaml"):
            try:
                workflow = parse_yaml_workflow(file_path)
                workflows.append(workflow)
                log.info("yaml.discover.found", path=str(file_path), name=workflow.name)
            except YAMLWorkflowError as exc:
                log.warning("yaml.discover.invalid", path=str(file_path), error=str(exc))
            except Exception:
                log.warning("yaml.discover.error", path=str(file_path), exc_info=True)

        for file_path in directory.glob("*.yml"):
            try:
                workflow = parse_yaml_workflow(file_path)
                workflows.append(workflow)
                log.info("yaml.discover.found", path=str(file_path), name=workflow.name)
            except YAMLWorkflowError as exc:
                log.warning("yaml.discover.invalid", path=str(file_path), error=str(exc))
            except Exception:
                log.warning("yaml.discover.error", path=str(file_path), exc_info=True)

    return workflows
