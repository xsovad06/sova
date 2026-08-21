"""Integration tests for YAML workflow execution."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sova.adapters.base import TaskState
from sova.core.context import ExecutionContext
from sova.core.yaml_workflow import discover_yaml_workflows, parse_yaml_workflow
from sova.roles.custom import CustomRole


@pytest.mark.asyncio
class TestYAMLWorkflowIntegration:
    """Test end-to-end YAML workflow execution."""

    async def test_yaml_workflow_parse_and_create_role(self):
        """Parse YAML workflow and create CustomRole from it."""
        with TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)

            yaml_content = """
name: test-integration-workflow
description: Integration test workflow
steps:
  - name: step1
    command: test
    prompt_template: "Test step"
"""
            workflow_file = tmpdir_path / "test.yaml"
            workflow_file.write_text(yaml_content)

            workflow_def = parse_yaml_workflow(workflow_file)
            role = CustomRole(workflow_def)

            assert role.name == "test-integration-workflow"
            assert role.description == "Integration test workflow"

    async def test_yaml_workflow_executes_via_custom_role(self):
        """YAML workflow can be executed through CustomRole."""
        with TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)

            yaml_content = """
name: simple-exec-test
description: Simple execution test
steps:
  - name: single_step
    command: test
    prompt_template: "Run test"
"""
            workflow_file = tmpdir_path / "simple.yaml"
            workflow_file.write_text(yaml_content)

            workflow_def = parse_yaml_workflow(workflow_file)
            role = CustomRole(workflow_def)

            # Mock the start_command function used by DAGExecutor
            mock_result = {"status": "success", "message": "Test completed", "cost_usd": 0}

            with patch("sova.core.dag._get_start_command") as mock_get_start:
                mock_start = AsyncMock(return_value=mock_result)
                mock_get_start.return_value = mock_start

                # Create minimal execution context
                mock_adapter = AsyncMock()
                mock_adapter.get_task.return_value = MagicMock(state=TaskState.RESEARCHED)
                mock_config = MagicMock()
                ctx = ExecutionContext(
                    project_dir=tmpdir_path,
                    config=mock_config,
                    issue_number=1,
                    adapter=mock_adapter,
                    force=True,
                )

                result = await role.execute(ctx)

            assert result.success is True
            assert "1 nodes executed" in result.summary

    async def test_discover_yaml_workflows(self):
        """YAML workflow discovery finds all valid workflows."""
        with TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            workflow_dir = tmpdir_path / "workflows"
            workflow_dir.mkdir()

            yaml_content1 = """
name: workflow-one
description: First workflow
steps:
  - name: step1
    command: test
    prompt_template: "Test"
"""
            yaml_content2 = """
name: workflow-two
description: Second workflow
steps:
  - name: step1
    command: test
    prompt_template: "Test"
"""
            (workflow_dir / "one.yaml").write_text(yaml_content1)
            (workflow_dir / "two.yml").write_text(yaml_content2)

            workflows = discover_yaml_workflows([workflow_dir])

            assert len(workflows) == 2
            names = {w.name for w in workflows}
            assert "workflow-one" in names
            assert "workflow-two" in names
