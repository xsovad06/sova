"""Tests for YAML workflow parsing and execution."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from sova.core.yaml_workflow import (
    YAMLWorkflowError,
    discover_yaml_workflows,
    parse_yaml_workflow,
)


@pytest.fixture
def temp_workflow_dir():
    """Create a temporary directory for workflow YAML files."""
    with TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


class TestYAMLWorkflowParsing:
    """Test YAML workflow parsing and validation."""

    def test_parse_valid_simple_workflow(self, temp_workflow_dir):
        """Parse a minimal valid YAML workflow."""
        yaml_content = """
name: simple-workflow
description: A simple test workflow
steps:
  - name: step1
    command: test
    prompt_template: "Test prompt"
"""
        workflow_file = temp_workflow_dir / "simple.yaml"
        workflow_file.write_text(yaml_content)

        result = parse_yaml_workflow(workflow_file)

        assert result.name == "simple-workflow"
        assert result.description == "A simple test workflow"
        assert len(result.graph_json["nodes"]) == 1
        assert result.graph_json["nodes"][0]["command"] == "test"

    def test_parse_workflow_with_dependencies(self, temp_workflow_dir):
        """Parse a workflow with step dependencies."""
        yaml_content = """
name: multi-step
description: Multi-step workflow
steps:
  - name: research
    command: research
    prompt_template: "Research topic"
  - name: write
    command: develop
    prompt_template: "Write docs"
    depends_on: [research]
  - name: review
    command: review
    depends_on: [write]
"""
        workflow_file = temp_workflow_dir / "multi.yaml"
        workflow_file.write_text(yaml_content)

        result = parse_yaml_workflow(workflow_file)

        assert len(result.graph_json["nodes"]) == 3
        assert len(result.graph_json["edges"]) == 2

        # Check edges connect correctly
        edges = result.graph_json["edges"]
        edge_pairs = [(e["source"], e["target"]) for e in edges]
        assert ("research", "write") in edge_pairs
        assert ("write", "review") in edge_pairs

    def test_parse_workflow_with_jinja2_conditions(self, temp_workflow_dir):
        """Parse a workflow with Jinja2 condition syntax."""
        yaml_content = """
name: conditional-workflow
description: Workflow with conditions
steps:
  - name: check
    command: test
    prompt_template: "Check something"
  - name: action
    command: develop
    prompt_template: "Take action"
    depends_on: [check]
    condition: "{{ check.status == 'completed' }}"
"""
        workflow_file = temp_workflow_dir / "conditional.yaml"
        workflow_file.write_text(yaml_content)

        result = parse_yaml_workflow(workflow_file)

        edges = result.graph_json["edges"]
        assert len(edges) == 1
        assert edges[0]["condition"] == "{{ check.status == 'completed' }}"

    def test_parse_workflow_with_timeout(self, temp_workflow_dir):
        """Parse a workflow step with timeout."""
        yaml_content = """
name: timeout-workflow
description: Workflow with timeout
steps:
  - name: slow_task
    command: research
    prompt_template: "Slow task"
    timeout: 900
"""
        workflow_file = temp_workflow_dir / "timeout.yaml"
        workflow_file.write_text(yaml_content)

        result = parse_yaml_workflow(workflow_file)

        node = result.graph_json["nodes"][0]
        assert node.get("params", {}).get("timeout") == 900

    def test_parse_workflow_missing_required_fields(self, temp_workflow_dir):
        """Reject YAML missing required fields."""
        yaml_content = """
description: Missing name field
steps:
  - name: step1
    command: test
"""
        workflow_file = temp_workflow_dir / "invalid.yaml"
        workflow_file.write_text(yaml_content)

        with pytest.raises(YAMLWorkflowError, match="name"):
            parse_yaml_workflow(workflow_file)

    def test_parse_workflow_empty_steps(self, temp_workflow_dir):
        """Reject YAML with empty steps list."""
        yaml_content = """
name: empty-workflow
description: No steps
steps: []
"""
        workflow_file = temp_workflow_dir / "empty.yaml"
        workflow_file.write_text(yaml_content)

        with pytest.raises(YAMLWorkflowError, match="at least 1 item"):
            parse_yaml_workflow(workflow_file)

    def test_parse_workflow_circular_dependency(self, temp_workflow_dir):
        """Reject YAML with circular dependencies."""
        yaml_content = """
name: circular
description: Circular dependency
steps:
  - name: a
    command: test
    depends_on: [b]
  - name: b
    command: test
    depends_on: [a]
"""
        workflow_file = temp_workflow_dir / "circular.yaml"
        workflow_file.write_text(yaml_content)

        with pytest.raises(YAMLWorkflowError, match="cycle"):
            parse_yaml_workflow(workflow_file)

    def test_parse_workflow_undefined_dependency(self, temp_workflow_dir):
        """Reject YAML with undefined dependency."""
        yaml_content = """
name: undefined-dep
description: Undefined dependency
steps:
  - name: step1
    command: test
    depends_on: [nonexistent]
"""
        workflow_file = temp_workflow_dir / "undefined.yaml"
        workflow_file.write_text(yaml_content)

        with pytest.raises(YAMLWorkflowError, match="unknown"):
            parse_yaml_workflow(workflow_file)

    def test_parse_workflow_invalid_yaml_syntax(self, temp_workflow_dir):
        """Reject malformed YAML with clear error."""
        yaml_content = """
name: broken
description: [this is
  not valid: yaml
"""
        workflow_file = temp_workflow_dir / "broken.yaml"
        workflow_file.write_text(yaml_content)

        with pytest.raises(YAMLWorkflowError, match="YAML syntax"):
            parse_yaml_workflow(workflow_file)


class TestYAMLWorkflowDiscovery:
    """Test YAML workflow file discovery."""

    def test_discover_workflows_from_single_dir(self, temp_workflow_dir):
        """Discover workflows from a directory."""
        (temp_workflow_dir / "workflow1.yaml").write_text("""
name: workflow1
description: First workflow
steps:
  - name: step1
    command: test
    prompt_template: "Test"
""")
        (temp_workflow_dir / "workflow2.yml").write_text("""
name: workflow2
description: Second workflow
steps:
  - name: step1
    command: test
    prompt_template: "Test"
""")

        workflows = discover_yaml_workflows([temp_workflow_dir])

        assert len(workflows) == 2
        names = {w.name for w in workflows}
        assert "workflow1" in names
        assert "workflow2" in names

    def test_discover_workflows_ignores_invalid_files(self, temp_workflow_dir):
        """Invalid YAML files are logged and skipped, not raised."""
        (temp_workflow_dir / "valid.yaml").write_text("""
name: valid
description: Valid workflow
steps:
  - name: step1
    command: test
    prompt_template: "Test"
""")
        (temp_workflow_dir / "invalid.yaml").write_text("""
name: [broken
""")

        workflows = discover_yaml_workflows([temp_workflow_dir])

        assert len(workflows) == 1
        assert workflows[0].name == "valid"

    def test_discover_workflows_empty_directory(self, temp_workflow_dir):
        """Empty directory returns empty list."""
        workflows = discover_yaml_workflows([temp_workflow_dir])
        assert workflows == []

    def test_discover_workflows_nonexistent_directory(self):
        """Nonexistent directory returns empty list, does not raise."""
        workflows = discover_yaml_workflows([Path("/nonexistent/path")])
        assert workflows == []


class TestJinja2ConditionEvaluation:
    """Test Jinja2 condition evaluation in DAG executor."""

    def test_evaluate_jinja2_condition_simple_equality(self):
        """Evaluate simple Jinja2 equality condition."""
        from sova.core.dag import _evaluate_condition

        context = {"step1.status": "completed"}
        assert _evaluate_condition("{{ step1.status == 'completed' }}", context) is True
        assert _evaluate_condition("{{ step1.status == 'failed' }}", context) is False

    def test_evaluate_jinja2_condition_with_done_key(self):
        """Evaluate Jinja2 condition with .done pattern."""
        from sova.core.dag import _evaluate_condition

        context = {"step1.done": "true"}
        assert _evaluate_condition("{{ step1.done == 'true' }}", context) is True

    def test_evaluate_legacy_condition_format(self):
        """Legacy key == value format still works."""
        from sova.core.dag import _evaluate_condition

        context = {"step1.done": "true"}
        assert _evaluate_condition("step1.done == true", context) is True

    def test_evaluate_jinja2_condition_complex_expression(self):
        """Evaluate complex Jinja2 expressions."""
        from sova.core.dag import _evaluate_condition

        context = {"step1.count": "5", "step2.done": "true"}
        assert _evaluate_condition("{{ step1.count|int > 3 and step2.done == 'true' }}", context) is True
        assert _evaluate_condition("{{ step1.count|int > 10 }}", context) is False

    def test_evaluate_jinja2_sandbox_prevents_code_injection(self):
        """Jinja2 sandbox prevents code execution."""
        from sova.core.dag import _evaluate_condition

        context = {"x": "1"}
        # Sandboxed environment should prevent __import__ and similar
        result = _evaluate_condition("{{ x.__class__.__bases__ }}", context)
        # Should fail safely, not execute arbitrary code
        assert result is False


class TestWorkflowNameConflicts:
    """Test workflow name conflict handling."""

    def test_reject_workflow_name_conflicting_with_builtin(self, temp_workflow_dir):
        """Reject workflow names that conflict with built-in roles."""
        yaml_content = """
name: developer
description: Should be rejected
steps:
  - name: step1
    command: test
    prompt_template: "Test"
"""
        workflow_file = temp_workflow_dir / "conflict.yaml"
        workflow_file.write_text(yaml_content)

        with pytest.raises(YAMLWorkflowError, match="conflicts with built-in role"):
            parse_yaml_workflow(workflow_file)
