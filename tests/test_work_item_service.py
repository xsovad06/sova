"""Tests for unified work item state service."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sova.dashboard.services.work_item_service import (
    WorkItemState,
    _append_standalone_pr_items,
    _apply_sova_verdict,
    _attach_integration_gates,
    _build_pr_item,
    _build_task_item,
    _extract_handoff_summary,
    _find_integrate_action,
    _format_pr_details,
    _format_running_agent,
    _get_actions,
    _index_handoffs,
    _index_prs_by_issue,
    _index_running_agents,
    _sort_items,
    compute_work_item_state,
    get_work_items,
)


def _state(**kwargs: object) -> WorkItemState:
    defaults: dict[str, object] = {
        "task_state": None,
        "pr_data": None,
        "handoff": None,
        "running_agent": None,
    }
    defaults.update(kwargs)
    return compute_work_item_state(**defaults)  # type: ignore[arg-type]


class TestComputeWorkItemState:
    """Priority cascade: running > handoff > PR > label."""

    # -- Priority 1: Running agent --

    def test_running_agent_overrides_everything(self) -> None:
        assert (
            _state(
                task_state="in_review",
                pr_data={"computed_state": "approved_ci_green", "state": "OPEN"},
                handoff={"status": "awaiting_action", "next_actions": []},
                running_agent={"run_id": 1, "role": "developer"},
            )
            == WorkItemState.AGENT_RUNNING
        )

    def test_running_agent_alone(self) -> None:
        assert _state(running_agent={"run_id": 1, "role": "triage"}) == WorkItemState.AGENT_RUNNING

    # -- Priority 2: Handoff --

    def test_handoff_awaiting_action(self) -> None:
        assert (
            _state(
                handoff={"status": "awaiting_action", "next_actions": [{"id": "integrate"}]},
            )
            == WorkItemState.HANDOFF_PENDING
        )

    def test_handoff_spec_review(self) -> None:
        assert (
            _state(
                handoff={"status": "awaiting_action", "next_actions": [{"id": "approve-spec"}]},
            )
            == WorkItemState.SPEC_REVIEW
        )

    def test_handoff_spec_review_command_key(self) -> None:
        """Spec actions using 'command' key (not 'id') are recognized."""
        assert (
            _state(
                handoff={"status": "awaiting_action", "next_actions": [{"command": "approve-spec"}]},
            )
            == WorkItemState.SPEC_REVIEW
        )

    def test_handoff_completed_ignored(self) -> None:
        assert (
            _state(
                task_state="triaged",
                handoff={"status": "completed", "next_actions": [{"id": "integrate"}]},
            )
            == WorkItemState.TRIAGED
        )

    def test_handoff_overrides_pr(self) -> None:
        assert (
            _state(
                pr_data={"computed_state": "approved_ci_green", "state": "OPEN"},
                handoff={"status": "awaiting_action", "next_actions": [{"id": "integrate"}]},
            )
            == WorkItemState.HANDOFF_PENDING
        )

    # -- Priority 3: PR state --

    def test_pr_ready_to_merge(self) -> None:
        assert (
            _state(
                pr_data={"computed_state": "approved_ci_green", "state": "OPEN"},
            )
            == WorkItemState.PR_READY_TO_MERGE
        )

    def test_pr_ci_running(self) -> None:
        assert (
            _state(
                pr_data={"computed_state": "ci_running", "state": "OPEN"},
            )
            == WorkItemState.PR_CI_RUNNING
        )

    def test_pr_ci_failed(self) -> None:
        assert (
            _state(
                pr_data={"computed_state": "ci_failed", "state": "OPEN"},
            )
            == WorkItemState.PR_CI_FAILED
        )

    def test_pr_changes_requested(self) -> None:
        assert (
            _state(
                pr_data={"computed_state": "changes_requested", "state": "OPEN"},
            )
            == WorkItemState.PR_CHANGES_REQUESTED
        )

    def test_pr_awaiting_review(self) -> None:
        assert (
            _state(
                pr_data={"computed_state": "awaiting_review", "state": "OPEN"},
            )
            == WorkItemState.PR_AWAITING_REVIEW
        )

    def test_pr_review_addressed(self) -> None:
        assert (
            _state(
                pr_data={"computed_state": "review_addressed", "state": "OPEN"},
            )
            == WorkItemState.PR_REVIEW_ADDRESSED
        )

    def test_pr_approved(self) -> None:
        assert (
            _state(
                pr_data={"computed_state": "approved", "state": "OPEN"},
            )
            == WorkItemState.PR_APPROVED
        )

    def test_pr_draft(self) -> None:
        assert (
            _state(
                pr_data={"computed_state": "draft", "state": "OPEN"},
            )
            == WorkItemState.PR_DRAFT
        )

    def test_pr_merged(self) -> None:
        assert (
            _state(
                pr_data={"computed_state": "approved_ci_green", "state": "MERGED"},
            )
            == WorkItemState.MERGED
        )

    def test_pr_overrides_label(self) -> None:
        assert (
            _state(
                task_state="in_review",
                pr_data={"computed_state": "approved_ci_green", "state": "OPEN"},
            )
            == WorkItemState.PR_READY_TO_MERGE
        )

    # -- Priority 4: GitHub label state --

    def test_label_backlog(self) -> None:
        assert _state(task_state="backlog") == WorkItemState.BACKLOG

    def test_label_triaged(self) -> None:
        assert _state(task_state="triaged") == WorkItemState.TRIAGED

    def test_label_researched(self) -> None:
        assert _state(task_state="researched") == WorkItemState.RESEARCHED

    def test_label_in_progress(self) -> None:
        assert _state(task_state="in_progress") == WorkItemState.IN_PROGRESS

    def test_label_in_review_no_pr(self) -> None:
        assert _state(task_state="in_review") == WorkItemState.PR_AWAITING_REVIEW

    def test_label_needs_spec(self) -> None:
        assert _state(task_state="needs_spec") == WorkItemState.NEEDS_SPEC

    def test_label_human_only(self) -> None:
        assert _state(task_state="human_only") == WorkItemState.HUMAN_ONLY

    def test_label_done(self) -> None:
        assert _state(task_state="done") == WorkItemState.DONE

    def test_unknown_label_defaults_backlog(self) -> None:
        assert _state(task_state="unknown_state") == WorkItemState.BACKLOG

    # -- SOVA verdict integration --

    def test_pr_approved_no_sova_review_yields_sova_pending(self) -> None:
        verdict = {"has_sova_review": False, "verdict": None, "finding_count": 0, "reviewed_at": None}
        assert (
            _state(
                pr_data={"computed_state": "approved", "state": "OPEN"},
                sova_verdict=verdict,
            )
            == WorkItemState.PR_SOVA_PENDING
        )

    def test_pr_approved_with_sova_approve_stays_approved(self) -> None:
        verdict = {"has_sova_review": True, "verdict": "approve", "finding_count": 0, "reviewed_at": None}
        assert (
            _state(
                pr_data={"computed_state": "approved", "state": "OPEN"},
                sova_verdict=verdict,
            )
            == WorkItemState.PR_APPROVED
        )

    def test_pr_approved_with_sova_revise_yields_changes_requested(self) -> None:
        verdict = {"has_sova_review": True, "verdict": "revise", "finding_count": 2, "reviewed_at": None}
        assert (
            _state(
                pr_data={"computed_state": "approved", "state": "OPEN"},
                sova_verdict=verdict,
            )
            == WorkItemState.PR_CHANGES_REQUESTED
        )

    # -- Edge cases --

    def test_no_inputs_defaults_backlog(self) -> None:
        assert _state() == WorkItemState.BACKLOG

    def test_handoff_action_field_fallback(self) -> None:
        assert (
            _state(
                handoff={"status": "awaiting_action", "next_actions": [{"action": "approve-spec"}]},
            )
            == WorkItemState.SPEC_REVIEW
        )


class TestGetActions:
    def test_backlog_has_triage(self) -> None:
        primary, _ = _get_actions(WorkItemState.BACKLOG, issue_number="42", pr_number=None)
        assert primary is not None
        assert primary["id"] == "triage"
        assert primary["handler"] == "start_agent"

    def test_researched_has_develop(self) -> None:
        primary, _ = _get_actions(WorkItemState.RESEARCHED, issue_number="42", pr_number=None)
        assert primary["id"] == "develop"

    def test_pr_ready_to_merge_with_issue_has_integrate(self) -> None:
        primary, secondary = _get_actions(WorkItemState.PR_READY_TO_MERGE, issue_number="42", pr_number=123)
        assert primary["id"] == "integrate"
        assert primary["handler_args"]["command"] == "integrate-pr"
        assert len(secondary) == 2
        assert secondary[0]["id"] == "review_pr"
        assert secondary[1]["id"] == "address_pr"

    def test_pr_ready_to_merge_standalone_has_integrate(self) -> None:
        primary, secondary = _get_actions(WorkItemState.PR_READY_TO_MERGE, issue_number=None, pr_number=123)
        assert primary["id"] == "integrate"
        assert primary["handler_args"]["command"] == "integrate-pr"
        assert primary["handler_args"]["pr"] == 123
        assert len(secondary) == 2
        assert secondary[0]["id"] == "review_pr"
        assert secondary[1]["id"] == "address_pr"

    def test_pr_ci_running_has_review(self) -> None:
        primary, _ = _get_actions(WorkItemState.PR_CI_RUNNING, issue_number=None, pr_number=99)
        assert primary["id"] == "review_pr"
        assert primary["style"] == "neutral"

    def test_pr_draft_has_review(self) -> None:
        primary, _ = _get_actions(WorkItemState.PR_DRAFT, issue_number=None, pr_number=99)
        assert primary["id"] == "review_pr"

    def test_pr_awaiting_review_has_review(self) -> None:
        primary, _ = _get_actions(WorkItemState.PR_AWAITING_REVIEW, issue_number="42", pr_number=123)
        assert primary["id"] == "review_pr"

    def test_pr_sova_pending_has_address_pr_command(self) -> None:
        """PR_SOVA_PENDING primary is the address-pr command (not the developer agent)."""
        primary, secondary = _get_actions(WorkItemState.PR_SOVA_PENDING, issue_number="42", pr_number=123)
        assert primary is not None
        assert primary["id"] == "address_pr"
        assert primary["handler"] == "run_command"
        assert primary["handler_args"]["command"] == "address-pr"
        assert primary["style"] == "warning"
        review_ids = [a["id"] for a in secondary]
        assert "review_pr" in review_ids
        assert "integrate" in review_ids

    def test_pr_changes_requested_has_address(self) -> None:
        primary, _ = _get_actions(WorkItemState.PR_CHANGES_REQUESTED, issue_number="42", pr_number=123)
        assert primary["id"] == "address_review"
        assert primary["handler"] == "start_agent"

    def test_pr_changes_requested_has_integrate_in_secondary(self) -> None:
        """PR_CHANGES_REQUESTED should always expose integrate-pr in the secondary menu
        so users can manually override SOVA verdict when the PR is actually ready."""
        _, secondary = _get_actions(WorkItemState.PR_CHANGES_REQUESTED, issue_number="42", pr_number=123)
        secondary_ids = [a["id"] for a in secondary]
        assert "integrate" in secondary_ids, f"integrate missing from secondary actions: {secondary_ids}"
        assert "review_pr" in secondary_ids

    def test_agent_running_has_no_action(self) -> None:
        primary, secondary = _get_actions(WorkItemState.AGENT_RUNNING, issue_number="42", pr_number=None)
        assert primary is None
        assert secondary == []

    def test_done_has_no_action(self) -> None:
        primary, _ = _get_actions(WorkItemState.DONE, issue_number="42", pr_number=None)
        assert primary is None

    def test_standalone_pr_actions(self) -> None:
        primary, _ = _get_actions(WorkItemState.PR_CI_FAILED, issue_number=None, pr_number=99)
        assert primary["id"] == "address_pr"
        assert primary["handler_args"]["pr"] == 99

    def test_standalone_pr_omits_empty_issue(self) -> None:
        primary, _ = _get_actions(WorkItemState.PR_CI_FAILED, issue_number=None, pr_number=99)
        assert "issue" not in primary["handler_args"]

    def test_standalone_agent_omits_empty_issue(self) -> None:
        primary, _ = _get_actions(WorkItemState.PR_CHANGES_REQUESTED, issue_number=None, pr_number=99)
        assert "issue" not in primary["handler_args"]
        assert primary["handler_args"]["pr"] == 99

    def test_issue_linked_cmd_includes_issue(self) -> None:
        primary, _ = _get_actions(WorkItemState.PR_CI_FAILED, issue_number="42", pr_number=99)
        assert primary["handler_args"]["issue"] == "42"
        assert primary["handler_args"]["pr"] == 99


class TestIndexHelpers:
    def test_index_running_agents(self) -> None:
        data = {
            "agents": [
                {"issue": "42", "run_id": 1, "role": "developer"},
                {"issue": "99", "run_id": 2, "role": "reviewer"},
                {"issue": "", "run_id": 3, "role": "triage"},
            ]
        }
        idx = _index_running_agents(data)
        assert "42" in idx
        assert "99" in idx
        assert "" not in idx

    def test_index_running_agents_pr_fallback(self) -> None:
        """Agents with pr_number but no issue are indexed under pr:<number>."""
        data = {"agents": [{"issue": "", "pr_number": 55, "run_id": 4, "role": "developer"}]}
        idx = _index_running_agents(data)
        assert "pr:55" in idx
        assert idx["pr:55"]["run_id"] == 4

    def test_index_running_agents_non_numeric_issue_uses_pr_key(self) -> None:
        """Non-numeric issue (command name fallback) should index under pr:<number>."""
        data = {"agents": [{"issue": "address-pr", "pr_number": 243, "run_id": 5, "role": "command:address-pr"}]}
        idx = _index_running_agents(data)
        assert "address-pr" not in idx
        assert "pr:243" in idx
        assert idx["pr:243"]["run_id"] == 5

    def test_index_running_agents_numeric_issue_with_pr(self) -> None:
        """Numeric issue with pr_number should index under both keys."""
        data = {"agents": [{"issue": "198", "pr_number": 248, "run_id": 6, "role": "command:address-pr"}]}
        idx = _index_running_agents(data)
        assert "198" in idx
        assert "pr:248" in idx

    def test_index_handoffs_filters_completed(self) -> None:
        handoffs = [
            {"issue": "42", "status": "awaiting_action", "next_actions": []},
            {"issue": "99", "status": "completed", "next_actions": []},
        ]
        idx = _index_handoffs(handoffs)
        assert "42" in idx
        assert "99" not in idx

    def test_index_handoffs_pr_key_for_standalone(self) -> None:
        handoffs = [
            {"issue": "", "pr_number": 200, "status": "awaiting_action", "next_actions": []},
        ]
        idx = _index_handoffs(handoffs)
        assert "pr:200" in idx
        assert idx["pr:200"]["pr_number"] == 200

    def test_index_handoffs_non_numeric_issue_uses_pr_key(self) -> None:
        """Non-numeric issue (command name fallback) should index under pr:<number>."""
        handoffs = [
            {"issue": "address-pr", "pr_number": 243, "status": "awaiting_action", "next_actions": []},
        ]
        idx = _index_handoffs(handoffs)
        assert "address-pr" not in idx
        assert "pr:243" in idx

    def test_index_prs_by_issue(self) -> None:
        prs = [
            {"number": 100, "linked_issue": 42},
            {"number": 101, "linked_issue": None},
        ]
        idx = _index_prs_by_issue(prs)
        assert "42" in idx
        assert idx["42"]["number"] == 100
        assert len(idx) == 1


class TestSortItems:
    def test_running_first(self) -> None:
        items = [
            {"state": "triaged", "priority": 2},
            {"state": "agent_running", "priority": 99},
        ]
        _sort_items(items)
        assert items[0]["state"] == "agent_running"

    def test_handoff_before_normal(self) -> None:
        items = [
            {"state": "researched", "priority": 0},
            {"state": "handoff_pending", "priority": 99},
        ]
        _sort_items(items)
        assert items[0]["state"] == "handoff_pending"

    def test_ready_to_merge_before_triaged(self) -> None:
        items = [
            {"state": "triaged", "priority": 2},
            {"state": "pr_ready_to_merge", "priority": -1},
        ]
        _sort_items(items)
        assert items[0]["state"] == "pr_ready_to_merge"

    def test_sova_pending_sorts_before_normal(self) -> None:
        items = [
            {"state": "triaged", "priority": 2},
            {"state": "pr_sova_pending", "priority": 99},
        ]
        _sort_items(items)
        assert items[0]["state"] == "pr_sova_pending"


class TestApplySovaVerdict:
    """Unit tests for _apply_sova_verdict() covering all override paths."""

    def _no_review(self) -> dict:
        return {"has_sova_review": False, "verdict": None, "finding_count": 0, "reviewed_at": None}

    def _review(self, verdict: str) -> dict:
        return {"has_sova_review": True, "verdict": verdict, "finding_count": 1, "reviewed_at": None}

    # -- sova_verdict=None: pass-through --

    def test_none_verdict_leaves_state_unchanged(self) -> None:
        assert _apply_sova_verdict(WorkItemState.PR_APPROVED, None) == WorkItemState.PR_APPROVED

    def test_none_verdict_leaves_awaiting_unchanged(self) -> None:
        assert _apply_sova_verdict(WorkItemState.PR_AWAITING_REVIEW, None) == WorkItemState.PR_AWAITING_REVIEW

    # -- No SOVA review, integrate-bound states → PR_SOVA_PENDING --

    def test_no_review_approved_yields_sova_pending(self) -> None:
        assert _apply_sova_verdict(WorkItemState.PR_APPROVED, self._no_review()) == WorkItemState.PR_SOVA_PENDING

    def test_no_review_ready_to_merge_yields_sova_pending(self) -> None:
        assert _apply_sova_verdict(WorkItemState.PR_READY_TO_MERGE, self._no_review()) == WorkItemState.PR_SOVA_PENDING

    def test_no_review_awaiting_review_unchanged(self) -> None:
        """PR_AWAITING_REVIEW is not in _INTEGRATE_STATES, so no downgrade."""
        assert (
            _apply_sova_verdict(WorkItemState.PR_AWAITING_REVIEW, self._no_review()) == WorkItemState.PR_AWAITING_REVIEW
        )

    def test_no_review_changes_requested_unchanged(self) -> None:
        """Existing CHANGES_REQUESTED is already actionable; SOVA pending doesn't override it."""
        assert (
            _apply_sova_verdict(WorkItemState.PR_CHANGES_REQUESTED, self._no_review())
            == WorkItemState.PR_CHANGES_REQUESTED
        )

    # -- SOVA reviewed with approve: pass-through --

    def test_approved_verdict_leaves_approved_unchanged(self) -> None:
        assert _apply_sova_verdict(WorkItemState.PR_APPROVED, self._review("approve")) == WorkItemState.PR_APPROVED

    def test_approved_verdict_leaves_ready_to_merge_unchanged(self) -> None:
        assert (
            _apply_sova_verdict(WorkItemState.PR_READY_TO_MERGE, self._review("approve"))
            == WorkItemState.PR_READY_TO_MERGE
        )

    # -- SOVA reviewed with revise/block: downgrade overrideable states --

    def test_revise_verdict_on_approved_yields_changes_requested(self) -> None:
        assert (
            _apply_sova_verdict(WorkItemState.PR_APPROVED, self._review("revise")) == WorkItemState.PR_CHANGES_REQUESTED
        )

    def test_block_verdict_on_ready_to_merge_yields_changes_requested(self) -> None:
        assert (
            _apply_sova_verdict(WorkItemState.PR_READY_TO_MERGE, self._review("block"))
            == WorkItemState.PR_CHANGES_REQUESTED
        )

    def test_revise_verdict_on_awaiting_review_yields_changes_requested(self) -> None:
        assert (
            _apply_sova_verdict(WorkItemState.PR_AWAITING_REVIEW, self._review("revise"))
            == WorkItemState.PR_CHANGES_REQUESTED
        )

    def test_revise_verdict_leaves_ci_failed_unchanged(self) -> None:
        """CI_FAILED is not in _VERDICT_OVERRIDEABLE; existing fix action should not be clobbered."""
        assert _apply_sova_verdict(WorkItemState.PR_CI_FAILED, self._review("revise")) == WorkItemState.PR_CI_FAILED

    # -- SOVA approved but GitHub has no formal approval (self-review posted as COMMENT) --

    def test_approved_verdict_on_awaiting_review_upgrades_to_approved(self) -> None:
        """SOVA approves but GitHub reviewDecision is empty (owner self-review posts as COMMENT).
        The state should upgrade so "Integrate PR" is shown instead of "Review"."""
        assert (
            _apply_sova_verdict(WorkItemState.PR_AWAITING_REVIEW, self._review("approve")) == WorkItemState.PR_APPROVED
        )

    def test_approved_verdict_does_not_affect_changes_requested(self) -> None:
        """If another reviewer requested changes, SOVA approve should not override it."""
        assert (
            _apply_sova_verdict(WorkItemState.PR_CHANGES_REQUESTED, self._review("approve"))
            == WorkItemState.PR_CHANGES_REQUESTED
        )

    def test_approved_verdict_does_not_affect_ci_failed(self) -> None:
        """CI failure takes priority; SOVA approve should not change the state."""
        assert _apply_sova_verdict(WorkItemState.PR_CI_FAILED, self._review("approve")) == WorkItemState.PR_CI_FAILED


class TestBuildTaskItem:
    def test_basic_task(self) -> None:
        task = {"issue": "42", "title": "Fix bug", "state": "triaged", "labels": [], "priority": 2}
        item = _build_task_item(task, pr_data=None, running=None, handoff=None)
        assert item["issue_number"] == "42"
        assert item["state"] == "triaged"
        assert item["primary_action"]["id"] == "research"

    def test_task_with_pr(self) -> None:
        task = {"issue": "42", "title": "Fix bug", "state": "in_review", "labels": [], "priority": -1}
        pr = {"number": 100, "computed_state": "approved_ci_green", "state": "OPEN"}
        item = _build_task_item(task, pr_data=pr, running=None, handoff=None)
        assert item["state"] == "pr_ready_to_merge"
        assert item["pr_details"]["number"] == 100
        assert item["pr_number"] == 100

    def test_task_with_running_agent(self) -> None:
        task = {"issue": "42", "title": "Fix bug", "state": "in_progress", "labels": [], "priority": 1}
        running = {"run_id": 5, "role": "developer", "elapsed_seconds": 60}
        item = _build_task_item(task, pr_data=None, running=running, handoff=None)
        assert item["state"] == "agent_running"
        assert item["running_agent"]["role_label"] == "Developing"

    def test_task_with_handoff(self) -> None:
        task = {"issue": "42", "title": "Fix bug", "state": "in_review", "labels": [], "priority": -1}
        handoff = {
            "status": "awaiting_action",
            "summary": "Review done",
            "next_actions": [{"id": "integrate", "label": "Integrate PR"}],
        }
        item = _build_task_item(task, pr_data=None, running=None, handoff=handoff)
        assert item["state"] == "handoff_pending"
        assert item["handoff_actions"] == [{"id": "integrate", "label": "Integrate PR"}]
        assert item["handoff_summary"] == "Review done"

    def test_pr_number_from_last_run(self) -> None:
        task = {
            "issue": "42",
            "title": "Fix bug",
            "state": "in_review",
            "labels": [],
            "priority": -1,
            "last_run": {"pr_number": 55, "status": "done"},
        }
        item = _build_task_item(task, pr_data=None, running=None, handoff=None)
        assert item["pr_number"] == 55

    def test_last_failed_flag(self) -> None:
        task = {
            "issue": "42",
            "title": "Fix bug",
            "state": "triaged",
            "labels": [],
            "priority": 2,
            "last_run": {"status": "failed"},
        }
        item = _build_task_item(task, pr_data=None, running=None, handoff=None)
        assert item["last_failed"] is True


class TestBuildPrItem:
    def test_standalone_pr(self) -> None:
        pr = {"number": 200, "title": "Quick fix", "computed_state": "awaiting_review", "state": "OPEN"}
        item = _build_pr_item(pr, running=None, handoff=None, issue_num=None)
        assert item["issue_number"] is None
        assert item["pr_number"] == 200
        assert item["state"] == "pr_awaiting_review"
        assert item["pr_details"]["number"] == 200

    def test_pr_with_linked_issue(self) -> None:
        pr = {"number": 200, "title": "Quick fix", "computed_state": "ci_failed", "state": "OPEN"}
        item = _build_pr_item(pr, running=None, handoff=None, issue_num="10")
        assert item["issue_number"] == "10"
        assert item["state"] == "pr_ci_failed"
        assert item["primary_action"]["id"] == "address_pr"

    def test_pr_with_handoff_propagates_actions(self) -> None:
        pr = {"number": 200, "title": "Quick fix", "computed_state": "awaiting_review", "state": "OPEN"}
        handoff = {
            "status": "awaiting_action",
            "summary": "Review complete",
            "next_actions": [{"id": "integrate", "label": "Integrate PR"}],
        }
        item = _build_pr_item(pr, running=None, handoff=handoff, issue_num=None)
        assert item["state"] == "handoff_pending"
        assert item["handoff_actions"] == [{"id": "integrate", "label": "Integrate PR"}]
        assert item["handoff_summary"] == "Review complete"

    def test_merged_pr(self) -> None:
        pr = {"number": 200, "title": "Done", "computed_state": "approved_ci_green", "state": "MERGED"}
        item = _build_pr_item(pr, running=None, handoff=None, issue_num=None)
        assert item["state"] == "merged"


class TestFormatHelpers:
    def test_format_running_agent(self) -> None:
        result = _format_running_agent({"run_id": 1, "role": "developer", "elapsed_seconds": 120})
        assert result["role_label"] == "Developing"
        assert result["run_id"] == 1

    def test_format_running_agent_unknown_role(self) -> None:
        result = _format_running_agent({"run_id": 1, "role": "custom", "elapsed_seconds": 0})
        assert result["role_label"] == "Running"

    def test_format_pr_details(self) -> None:
        result = _format_pr_details({"number": 42, "computed_state": "approved", "ci_status": "passed"})
        assert result["number"] == 42
        assert result["computed_state"] == "approved"

    def test_extract_handoff_summary(self) -> None:
        h = {"status": "awaiting_action", "summary": "All good"}
        assert _extract_handoff_summary(h, WorkItemState.HANDOFF_PENDING) == "All good"

    def test_extract_handoff_summary_wrong_state(self) -> None:
        h = {"status": "awaiting_action", "summary": "All good"}
        assert _extract_handoff_summary(h, WorkItemState.TRIAGED) == ""

    def test_extract_handoff_summary_none(self) -> None:
        assert _extract_handoff_summary(None, WorkItemState.HANDOFF_PENDING) == ""


class TestGetWorkItems:
    """Integration tests for get_work_items() assembly logic."""

    @pytest.fixture()
    def _mock_sources(self):
        """Patch _fetch_all_sources, _fetch_sova_verdicts, and _get_project_agents."""
        with (
            patch(
                "sova.dashboard.services.work_item_service._fetch_all_sources",
                new_callable=AsyncMock,
            ) as mock_fetch,
            patch(
                "sova.dashboard.services.work_item_service._fetch_sova_verdicts",
                new_callable=AsyncMock,
            ) as mock_verdicts,
            patch(
                "sova.dashboard.services.agent_pool._get_project_agents",
            ) as mock_pa,
        ):
            mock_pa.return_value = MagicMock(max_concurrent=3)
            # Default: match production behaviour; return explicit no-review for every issue.
            # Tests that want specific verdicts can set mock_verdicts.return_value directly.
            mock_verdicts.side_effect = lambda prs_by_issue, **_: {
                issue: {
                    "has_sova_review": False,
                    "verdict": None,
                    "finding_count": 0,
                    "reviewed_at": None,
                }
                for issue in prs_by_issue
            }
            yield mock_fetch, mock_pa, mock_verdicts

    @pytest.mark.asyncio()
    async def test_basic_assembly(self, _mock_sources) -> None:
        mock_fetch, *_ = _mock_sources
        queue = [{"issue": "42", "title": "Bug", "state": "triaged", "labels": [], "priority": 2}]
        mock_fetch.return_value = (queue, [], [], {"agents": [], "completed": []})

        result = await get_work_items()

        assert len(result["items"]) == 1
        assert result["items"][0]["issue_number"] == "42"
        assert result["items"][0]["state"] == "triaged"
        assert result["running_count"] == 0
        assert result["slots_available"] == 3

    @pytest.mark.asyncio()
    async def test_task_with_linked_pr_deduplication(self, _mock_sources) -> None:
        mock_fetch, _, mock_verdicts = _mock_sources
        queue = [{"issue": "42", "title": "Bug", "state": "in_review", "labels": [], "priority": -1}]
        prs = [{"number": 100, "linked_issue": 42, "computed_state": "approved", "state": "OPEN", "title": "Fix"}]
        mock_fetch.return_value = (queue, prs, [], {"agents": [], "completed": []})
        mock_verdicts.side_effect = None
        mock_verdicts.return_value = {
            "42": {"has_sova_review": True, "verdict": "approve", "finding_count": 0, "reviewed_at": None}
        }

        result = await get_work_items()

        # PR should be merged into the task item, not duplicated
        assert len(result["items"]) == 1
        assert result["items"][0]["pr_number"] == 100
        assert result["items"][0]["state"] == "pr_approved"

    @pytest.mark.asyncio()
    async def test_standalone_pr_appears(self, _mock_sources) -> None:
        mock_fetch, *_ = _mock_sources
        prs = [{"number": 200, "linked_issue": None, "computed_state": "ci_running", "state": "OPEN", "title": "Quick"}]
        mock_fetch.return_value = ([], prs, [], {"agents": [], "completed": []})

        result = await get_work_items()

        assert len(result["items"]) == 1
        assert result["items"][0]["pr_number"] == 200
        assert result["items"][0]["issue_number"] is None
        assert result["items"][0]["state"] == "pr_ci_running"

    @pytest.mark.asyncio()
    async def test_running_agent_counted(self, _mock_sources) -> None:
        mock_fetch, *_ = _mock_sources
        queue = [{"issue": "42", "title": "Bug", "state": "in_progress", "labels": [], "priority": 1}]
        agents = {"agents": [{"issue": "42", "run_id": 5, "role": "developer", "elapsed_seconds": 60}], "completed": []}
        mock_fetch.return_value = (queue, [], [], agents)

        result = await get_work_items()

        assert result["running_count"] == 1
        assert result["slots_available"] == 2
        assert result["items"][0]["state"] == "agent_running"

    @pytest.mark.asyncio()
    async def test_handoff_attached_to_task(self, _mock_sources) -> None:
        mock_fetch, *_ = _mock_sources
        queue = [{"issue": "42", "title": "Bug", "state": "in_review", "labels": [], "priority": -1}]
        handoffs = [
            {"issue": "42", "status": "awaiting_action", "summary": "Done", "next_actions": [{"id": "integrate"}]},
        ]
        mock_fetch.return_value = (queue, [], handoffs, {"agents": [], "completed": []})

        result = await get_work_items()

        assert result["items"][0]["state"] == "handoff_pending"
        assert result["items"][0]["handoff_actions"] == [{"id": "integrate"}]

    @pytest.mark.asyncio()
    async def test_project_dir_sets_slug(self, _mock_sources) -> None:
        mock_fetch, mock_pa, _ = _mock_sources
        mock_fetch.return_value = ([], [], [], {"agents": [], "completed": []})

        from pathlib import Path

        await get_work_items(project_dir=Path("/tmp/my-project"))

        mock_fetch.assert_called_once()
        call_kwargs = mock_fetch.call_args[1]
        assert call_kwargs["slug"] == "my-project"
        mock_pa.assert_called_once_with("my-project")

    @pytest.mark.asyncio()
    async def test_no_project_agents_defaults_max_3(self, _mock_sources) -> None:
        mock_fetch, mock_pa, _ = _mock_sources
        mock_pa.return_value = None
        mock_fetch.return_value = ([], [], [], {"agents": [], "completed": []})

        result = await get_work_items()

        assert result["max_concurrent"] == 3

    @pytest.mark.asyncio()
    async def test_pr_with_issue_not_in_queue(self, _mock_sources) -> None:
        """PR linked to issue that's NOT in queue -- appears as PR item with issue context."""
        mock_fetch, _, mock_verdicts = _mock_sources
        prs = [
            {"number": 300, "linked_issue": 99, "computed_state": "approved_ci_green", "state": "OPEN", "title": "Fix"},
        ]
        mock_fetch.return_value = ([], prs, [], {"agents": [], "completed": []})
        mock_verdicts.side_effect = None
        mock_verdicts.return_value = {
            "99": {"has_sova_review": True, "verdict": "approve", "finding_count": 0, "reviewed_at": None}
        }

        result = await get_work_items()

        assert len(result["items"]) == 1
        assert result["items"][0]["issue_number"] == "99"
        assert result["items"][0]["state"] == "pr_ready_to_merge"

    @pytest.mark.asyncio()
    async def test_sorting_applied(self, _mock_sources) -> None:
        mock_fetch, *_ = _mock_sources
        queue = [
            {"issue": "1", "title": "Low", "state": "triaged", "labels": [], "priority": 99},
            {"issue": "2", "title": "High", "state": "in_progress", "labels": [], "priority": 1},
        ]
        agents = {"agents": [{"issue": "2", "run_id": 1, "role": "developer", "elapsed_seconds": 0}], "completed": []}
        mock_fetch.return_value = (queue, [], [], agents)

        result = await get_work_items()

        # Running agent should sort first
        assert result["items"][0]["issue_number"] == "2"
        assert result["items"][0]["state"] == "agent_running"


class TestAppendStandalonePrItems:
    def test_adds_unlinked_pr(self) -> None:
        items: list[dict] = []
        prs = [{"number": 100, "linked_issue": None, "computed_state": "awaiting_review", "state": "OPEN"}]
        _append_standalone_pr_items(items, prs, set(), {}, {})
        assert len(items) == 1
        assert items[0]["pr_number"] == 100
        assert items[0]["issue_number"] is None

    def test_skips_already_linked(self) -> None:
        items: list[dict] = []
        prs = [{"number": 100, "linked_issue": 42, "computed_state": "approved", "state": "OPEN"}]
        _append_standalone_pr_items(items, prs, {"42"}, {}, {})
        assert len(items) == 0

    def test_adds_linked_pr_not_in_queue(self) -> None:
        items: list[dict] = []
        prs = [{"number": 100, "linked_issue": 42, "computed_state": "ci_failed", "state": "OPEN"}]
        _append_standalone_pr_items(items, prs, set(), {}, {})
        assert len(items) == 1
        assert items[0]["issue_number"] == "42"

    def test_unlinked_pr_uses_pr_number_key_for_verdict(self) -> None:
        """Unlinked PRs look up verdict by 'pr:{number}' key, not by issue."""
        items: list[dict] = []
        prs = [{"number": 200, "linked_issue": None, "computed_state": "approved", "state": "OPEN"}]
        verdicts = {"pr:200": {"has_sova_review": True, "verdict": "approve", "finding_count": 0, "reviewed_at": None}}
        _append_standalone_pr_items(items, prs, set(), {}, {}, verdicts_by_issue=verdicts)
        assert len(items) == 1
        assert items[0]["state"] == "pr_approved"

    def test_unlinked_pr_with_no_review_verdict_shows_sova_pending(self) -> None:
        """Unlinked approved PR with has_sova_review=False shows pr_sova_pending (not pr_awaiting_review)."""
        items: list[dict] = []
        prs = [{"number": 300, "linked_issue": None, "computed_state": "approved", "state": "OPEN"}]
        verdicts = {"pr:300": {"has_sova_review": False, "verdict": None, "finding_count": 0, "reviewed_at": None}}
        _append_standalone_pr_items(items, prs, set(), {}, {}, verdicts_by_issue=verdicts)
        assert len(items) == 1
        assert items[0]["state"] == "pr_sova_pending"


class TestFindIntegrateAction:
    def test_finds_in_primary(self) -> None:
        item = {"primary_action": {"id": "integrate", "label": "Integrate"}, "secondary_actions": []}
        assert _find_integrate_action(item) is not None
        assert _find_integrate_action(item)["id"] == "integrate"

    def test_finds_in_secondary(self) -> None:
        item = {
            "primary_action": {"id": "review_pr", "label": "Review"},
            "secondary_actions": [{"id": "integrate", "label": "Integrate"}],
        }
        assert _find_integrate_action(item) is not None
        assert _find_integrate_action(item)["id"] == "integrate"

    def test_returns_none_when_absent(self) -> None:
        item = {
            "primary_action": {"id": "review_pr", "label": "Review"},
            "secondary_actions": [{"id": "address_pr", "label": "Address"}],
        }
        assert _find_integrate_action(item) is None

    def test_returns_none_for_no_actions(self) -> None:
        item = {"primary_action": None, "secondary_actions": []}
        assert _find_integrate_action(item) is None


# ---------------------------------------------------------------------------
# _attach_integration_gates
# ---------------------------------------------------------------------------


class TestAttachIntegrationGates:
    @pytest.mark.asyncio
    async def test_gate_check_failure_sets_failed_result(self, monkeypatch) -> None:
        """When check_integration_gates raises, gate_result should fail-closed."""
        from sova.config.models import IntegrationGatesConfig, ProjectConfig

        cfg = ProjectConfig(
            github_repo="owner/repo",
            github_user="testuser",
            integration_gates=IntegrationGatesConfig(ci_passed=True),
        )
        item = {
            "issue_number": "42",
            "pr_details": {"number": 1, "ci_status": "passed"},
            "primary_action": {"id": "integrate", "label": "Integrate PR"},
            "secondary_actions": [],
        }

        async def _boom(**kwargs):
            raise RuntimeError("gate explosion")

        monkeypatch.setattr(
            "sova.dashboard.services.pr_service.check_integration_gates",
            _boom,
        )
        await _attach_integration_gates([item], {}, cfg)
        action = _find_integrate_action(item)
        assert action is not None
        assert action["gate_result"]["passed"] is False

    @pytest.mark.asyncio
    async def test_skips_when_config_none(self) -> None:
        """When config is None, gates are not attached."""
        item = {
            "issue_number": "42",
            "primary_action": {"id": "integrate", "label": "Integrate PR"},
            "secondary_actions": [],
        }
        await _attach_integration_gates([item], {}, None)
        action = _find_integrate_action(item)
        assert "gate_result" not in action


class TestGetWorkItemsConfigLoadFailure:
    @pytest.mark.asyncio
    async def test_config_load_failure_logs_warning(self, monkeypatch, tmp_path) -> None:
        """Config load failure should log a warning, not silently swallow."""
        monkeypatch.setattr(
            "sova.dashboard.services.work_item_service._fetch_all_sources",
            AsyncMock(return_value=([], [], [], {})),
        )
        monkeypatch.setattr(
            "sova.dashboard.services.agent_pool._get_project_agents",
            MagicMock(return_value=None),
        )

        def _boom(_path):
            raise RuntimeError("config broken")

        monkeypatch.setattr("sova.config.loader.load_config", _boom)

        with patch("sova.dashboard.services.work_item_service.log") as mock_log:
            result = await get_work_items(project_dir=tmp_path)
            mock_log.warning.assert_called_once()
            assert "config_load_failed" in str(mock_log.warning.call_args)

        assert result["items"] == []


class TestFetchSovaVerdicts:
    """Direct tests for _fetch_sova_verdicts covering the unlinked PR path."""

    @pytest.mark.asyncio()
    async def test_fetches_verdicts_for_linked_prs(self) -> None:
        from sova.dashboard.services.work_item_service import _fetch_sova_verdicts

        mock_verdict = {"has_sova_review": False, "verdict": None, "finding_count": 0, "reviewed_at": None}

        with patch(
            "sova.dashboard.services.agent_recovery.get_sova_review_verdict",
            new_callable=AsyncMock,
            return_value=mock_verdict,
        ) as mock_call:
            result = await _fetch_sova_verdicts({"42": {"number": 100}})

        assert "42" in result
        assert result["42"]["has_sova_review"] is False
        mock_call.assert_called_once_with("42", pr_number=100, project_dir=None)

    @pytest.mark.asyncio()
    async def test_fetches_verdicts_for_unlinked_prs(self) -> None:
        from sova.dashboard.services.work_item_service import _fetch_sova_verdicts

        mock_verdict = {"has_sova_review": True, "verdict": "approve", "finding_count": 0, "reviewed_at": None}

        with patch(
            "sova.dashboard.services.agent_recovery.get_sova_review_verdict",
            new_callable=AsyncMock,
            return_value=mock_verdict,
        ) as mock_call:
            result = await _fetch_sova_verdicts(
                {},
                unlinked_prs=[{"number": 200}],
            )

        assert "pr:200" in result
        assert result["pr:200"]["has_sova_review"] is True
        assert result["pr:200"]["verdict"] == "approve"
        mock_call.assert_called_once_with(None, pr_number=200, project_dir=None)

    @pytest.mark.asyncio()
    async def test_unlinked_pr_without_number_is_skipped(self) -> None:
        from sova.dashboard.services.work_item_service import _fetch_sova_verdicts

        with patch(
            "sova.dashboard.services.agent_recovery.get_sova_review_verdict",
            new_callable=AsyncMock,
        ) as mock_verdict:
            result = await _fetch_sova_verdicts({}, unlinked_prs=[{"number": None}])

        assert result == {}
        mock_verdict.assert_not_called()
