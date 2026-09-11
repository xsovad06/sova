"""Tests for the configurable step pipeline registry (sova/core/steps/__init__.py)."""

from __future__ import annotations

import pytest

from sova.core.steps import (
    STEP_REGISTRY,
    build_configured_pipeline,
    build_pipeline,
    get_address_review_step_names,
    get_developer_step_names,
    get_planner_step_names,
    get_researcher_step_names,
    validate_pipeline,
)
from sova.core.steps.base import BaseStep


def test_step_registry_contains_all_default_pipeline_steps() -> None:
    """Every step referenced by the built-in pipelines is registered."""
    all_default_names = (
        get_developer_step_names()
        + get_address_review_step_names()
        + get_researcher_step_names()
        + get_planner_step_names()
    )
    for name in all_default_names:
        assert name in STEP_REGISTRY


def test_step_registry_maps_to_base_step_subclasses() -> None:
    for step_cls in STEP_REGISTRY.values():
        assert issubclass(step_cls, BaseStep)


def test_build_pipeline_instantiates_steps_in_order() -> None:
    steps = build_pipeline(["sync", "assess", "commit"])
    assert [s.name for s in steps] == ["sync", "assess", "commit"]
    assert all(isinstance(s, BaseStep) for s in steps)


def test_build_pipeline_raises_on_unknown_step_name() -> None:
    with pytest.raises(ValueError, match="Unknown pipeline step"):
        build_pipeline(["sync", "not_a_real_step"])


def test_build_pipeline_error_lists_available_steps() -> None:
    with pytest.raises(ValueError, match="commit"):
        build_pipeline(["bogus"])


def test_build_pipeline_raises_on_duplicate_step_name() -> None:
    with pytest.raises(ValueError, match="Duplicate pipeline step"):
        build_pipeline(["sync", "assess", "commit", "commit", "push"])


def test_validate_pipeline_no_warnings_for_default_developer_pipeline() -> None:
    assert validate_pipeline(get_developer_step_names()) == []


def test_validate_pipeline_no_warnings_for_default_address_review_pipeline() -> None:
    assert validate_pipeline(get_address_review_step_names()) == []


def test_validate_pipeline_warns_on_commit_after_push() -> None:
    warnings = validate_pipeline(["push", "commit"])
    assert any("commit" in w and "push" in w for w in warnings)


def test_validate_pipeline_warns_on_push_after_create_pr() -> None:
    warnings = validate_pipeline(["create_pr", "push"])
    assert any("push" in w and "create_pr" in w for w in warnings)


def test_validate_pipeline_warns_on_develop_before_create_worktree() -> None:
    warnings = validate_pipeline(["develop", "create_worktree"])
    assert any("create_worktree" in w and "develop" in w for w in warnings)


def test_validate_pipeline_ignores_constraints_with_missing_steps() -> None:
    """Constraint pairs are only checked when both steps are present."""
    assert validate_pipeline(["sync", "assess"]) == []


def test_build_configured_pipeline_falls_back_to_defaults_when_empty() -> None:
    defaults = get_developer_step_names()
    steps = build_configured_pipeline(defaults, [])
    assert [s.name for s in steps] == defaults


def test_build_configured_pipeline_uses_configured_names_when_present() -> None:
    steps = build_configured_pipeline(get_developer_step_names(), ["sync", "commit"])
    assert [s.name for s in steps] == ["sync", "commit"]


def test_build_configured_pipeline_raises_on_unknown_configured_step() -> None:
    with pytest.raises(ValueError, match="Unknown pipeline step"):
        build_configured_pipeline(get_developer_step_names(), ["sync", "totally_bogus"])


def test_build_configured_pipeline_raises_on_duplicate_configured_step() -> None:
    with pytest.raises(ValueError, match="Duplicate pipeline step"):
        build_configured_pipeline(get_developer_step_names(), ["sync", "commit", "commit"])
