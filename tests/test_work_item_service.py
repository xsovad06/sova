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
    _format_sova_context,
    _get_actions,
    _index_handoffs,
    _index_prs_by_issue,
    _index_running_agents,
    _is_verdict_stale,
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
        """GitHub-sourced changes_requested maps to PR_EXTERNAL_CHANGES (command path)."""
        assert (
            _state(
                pr_data={"computed_state": "changes_requested", "state": "OPEN"},
            )
            == WorkItemState.PR_EXTERNAL_CHANGES
        )

    def test_pr_sova_changes_from_revise_verdict(self) -> None:
        """SOVA revise verdict on an approved PR → PR_SOVA_CHANGES (agent path)."""
        verdict = {"has_sova_review": True, "verdict": "revise", "finding_count": 2, "reviewed_at": None}
        assert (
            _state(
                pr_data={"computed_state": "approved", "state": "OPEN"},
                sova_verdict=verdict,
            )
            == WorkItemState.PR_SOVA_CHANGES
        )

    def test_pr_sova_changes_overrides_external_changes(self) -> None:
        """SOVA revise verdict upgrades PR_EXTERNAL_CHANGES to PR_SOVA_CHANGES."""
        verdict = {"has_sova_review": True, "verdict": "revise", "finding_count": 1, "reviewed_at": None}
        assert (
            _state(
                pr_data={"computed_state": "changes_requested", "state": "OPEN"},
                sova_verdict=verdict,
            )
            == WorkItemState.PR_SOVA_CHANGES
        )

    def test_external_reviews_disabled_no_sova_pending(self) -> None:
        """With external_reviews_enabled=False, integrate-bound state + no SOVA review → PR_AWAITING_REVIEW."""
        verdict = {"has_sova_review": False, "verdict": None, "finding_count": 0, "reviewed_at": None}
        assert (
            _state(
                pr_data={"computed_state": "approved_ci_green", "state": "OPEN"},
                sova_verdict=verdict,
                external_reviews_enabled=False,
            )
            == WorkItemState.PR_AWAITING_REVIEW
        )

    def test_external_reviews_enabled_yields_sova_pending(self) -> None:
        """With external_reviews_enabled=True (default), no SOVA review → PR_SOVA_PENDING."""
        verdict = {"has_sova_review": False, "verdict": None, "finding_count": 0, "reviewed_at": None}
        assert (
            _state(
                pr_data={"computed_state": "approved_ci_green", "state": "OPEN"},
                sova_verdict=verdict,
                external_reviews_enabled=True,
            )
            == WorkItemState.PR_SOVA_PENDING
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

    def test_pr_approved_with_sova_revise_yields_sova_changes(self) -> None:
        verdict = {"has_sova_review": True, "verdict": "revise", "finding_count": 2, "reviewed_at": None}
        assert (
            _state(
                pr_data={"computed_state": "approved", "state": "OPEN"},
                sova_verdict=verdict,
            )
            == WorkItemState.PR_SOVA_CHANGES
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

    def test_pr_sova_pending_has_review_pr_command(self) -> None:
        """PR_SOVA_PENDING primary is review-pr (prompt user to trigger SOVA review)."""
        primary, secondary = _get_actions(WorkItemState.PR_SOVA_PENDING, issue_number="42", pr_number=123)
        assert primary is not None
        assert primary["id"] == "review_pr"
        assert primary["handler"] == "run_command"
        assert primary["handler_args"]["command"] == "review-pr"
        assert primary["style"] == "warning"
        secondary_ids = [a["id"] for a in secondary]
        assert "address_pr" in secondary_ids
        assert "integrate" in secondary_ids

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

    def test_pr_sova_changes_has_address_agent(self) -> None:
        """PR_SOVA_CHANGES primary is the developer agent (address_review pipeline)."""
        primary, secondary = _get_actions(WorkItemState.PR_SOVA_CHANGES, issue_number="42", pr_number=123)
        assert primary is not None
        assert primary["id"] == "address_review"
        assert primary["handler"] == "start_agent"
        assert primary["handler_args"]["role"] == "developer"
        secondary_ids = [a["id"] for a in secondary]
        assert "integrate" in secondary_ids
        assert "review_pr" in secondary_ids

    def test_pr_external_changes_has_address_pr_command(self) -> None:
        """PR_EXTERNAL_CHANGES primary is the /address-pr command (not developer agent)."""
        primary, secondary = _get_actions(WorkItemState.PR_EXTERNAL_CHANGES, issue_number="42", pr_number=123)
        assert primary is not None
        assert primary["id"] == "address_pr"
        assert primary["handler"] == "run_command"
        assert primary["handler_args"]["command"] == "address-pr"
        secondary_ids = [a["id"] for a in secondary]
        assert "integrate" in secondary_ids
        assert "review_pr" in secondary_ids

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

    def test_sova_changes_sorts_before_normal(self) -> None:
        items = [
            {"state": "triaged", "priority": 2},
            {"state": "pr_sova_changes", "priority": 99},
        ]
        _sort_items(items)
        assert items[0]["state"] == "pr_sova_changes"

    def test_external_changes_sorts_before_normal(self) -> None:
        items = [
            {"state": "triaged", "priority": 2},
            {"state": "pr_external_changes", "priority": 99},
        ]
        _sort_items(items)
        assert items[0]["state"] == "pr_external_changes"


class TestApplySovaVerdict:
    """Unit tests for _apply_sova_verdict() covering all override paths."""

    def _no_review(self) -> dict:
        return {"has_sova_review": False, "verdict": None, "finding_count": 0, "reviewed_at": None}

    def _review(self, verdict: str, reviewed_at: str | None = None) -> dict:
        return {"has_sova_review": True, "verdict": verdict, "finding_count": 1, "reviewed_at": reviewed_at}

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

    def test_no_review_external_changes_unchanged(self) -> None:
        """Existing PR_EXTERNAL_CHANGES is already actionable; SOVA pending doesn't override it."""
        assert (
            _apply_sova_verdict(WorkItemState.PR_EXTERNAL_CHANGES, self._no_review())
            == WorkItemState.PR_EXTERNAL_CHANGES
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

    def test_revise_verdict_on_approved_yields_sova_changes(self) -> None:
        assert _apply_sova_verdict(WorkItemState.PR_APPROVED, self._review("revise")) == WorkItemState.PR_SOVA_CHANGES

    def test_block_verdict_on_ready_to_merge_yields_sova_changes(self) -> None:
        assert (
            _apply_sova_verdict(WorkItemState.PR_READY_TO_MERGE, self._review("block")) == WorkItemState.PR_SOVA_CHANGES
        )

    def test_revise_verdict_on_awaiting_review_yields_sova_changes(self) -> None:
        assert (
            _apply_sova_verdict(WorkItemState.PR_AWAITING_REVIEW, self._review("revise"))
            == WorkItemState.PR_SOVA_CHANGES
        )

    def test_revise_verdict_on_external_changes_yields_sova_changes(self) -> None:
        """SOVA revise overrides external-reviewer-caused PR_EXTERNAL_CHANGES → PR_SOVA_CHANGES."""
        assert (
            _apply_sova_verdict(WorkItemState.PR_EXTERNAL_CHANGES, self._review("revise"))
            == WorkItemState.PR_SOVA_CHANGES
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

    def test_approved_verdict_does_not_affect_external_changes(self) -> None:
        """If an external reviewer requested changes, SOVA approve should not override it."""
        assert (
            _apply_sova_verdict(WorkItemState.PR_EXTERNAL_CHANGES, self._review("approve"))
            == WorkItemState.PR_EXTERNAL_CHANGES
        )

    def test_approved_verdict_does_not_affect_ci_failed(self) -> None:
        """CI failure takes priority; SOVA approve should not change the state."""
        assert _apply_sova_verdict(WorkItemState.PR_CI_FAILED, self._review("approve")) == WorkItemState.PR_CI_FAILED

    # -- Staleness: human approval after SOVA review invalidates revise/block --

    def test_stale_revise_verdict_skips_downgrade(self) -> None:
        """If a human approved on GitHub after the SOVA review, the revise verdict is stale."""
        verdict = self._review("revise", reviewed_at="2026-07-19T10:00:00Z")
        result = _apply_sova_verdict(
            WorkItemState.PR_READY_TO_MERGE,
            verdict,
            latest_approval_at="2026-07-20T12:00:00Z",
        )
        assert result == WorkItemState.PR_READY_TO_MERGE

    def test_fresh_revise_verdict_still_downgrades(self) -> None:
        """If the SOVA review is newer than the last GitHub approval, it still downgrades."""
        verdict = self._review("block", reviewed_at="2026-07-20T14:00:00Z")
        result = _apply_sova_verdict(
            WorkItemState.PR_APPROVED,
            verdict,
            latest_approval_at="2026-07-19T08:00:00Z",
        )
        assert result == WorkItemState.PR_SOVA_CHANGES

    def test_revise_verdict_without_approval_timestamp_still_downgrades(self) -> None:
        """Backward compat: no latest_approval_at means no staleness check."""
        verdict = self._review("revise", reviewed_at="2026-07-19T10:00:00Z")
        result = _apply_sova_verdict(WorkItemState.PR_APPROVED, verdict)
        assert result == WorkItemState.PR_SOVA_CHANGES

    def test_revise_verdict_without_reviewed_at_still_downgrades(self) -> None:
        """If the SOVA verdict has no timestamp, staleness check is skipped."""
        verdict = self._review("revise")
        result = _apply_sova_verdict(
            WorkItemState.PR_APPROVED,
            verdict,
            latest_approval_at="2026-07-20T12:00:00Z",
        )
        assert result == WorkItemState.PR_SOVA_CHANGES

    # -- external_reviews_enabled=False: skip PR_SOVA_PENDING for projects without bot review --

    def test_external_reviews_disabled_approved_yields_awaiting_review(self) -> None:
        """No external reviewers: integrate-bound + no SOVA review → PR_AWAITING_REVIEW (not PR_SOVA_PENDING)."""
        assert (
            _apply_sova_verdict(WorkItemState.PR_APPROVED, self._no_review(), external_reviews_enabled=False)
            == WorkItemState.PR_AWAITING_REVIEW
        )

    def test_external_reviews_disabled_ready_to_merge_yields_awaiting_review(self) -> None:
        assert (
            _apply_sova_verdict(WorkItemState.PR_READY_TO_MERGE, self._no_review(), external_reviews_enabled=False)
            == WorkItemState.PR_AWAITING_REVIEW
        )

    def test_external_reviews_enabled_approved_yields_sova_pending(self) -> None:
        """External reviewers enabled: integrate-bound + no SOVA review → PR_SOVA_PENDING."""
        assert (
            _apply_sova_verdict(WorkItemState.PR_APPROVED, self._no_review(), external_reviews_enabled=True)
            == WorkItemState.PR_SOVA_PENDING
        )

    def test_external_reviews_disabled_does_not_affect_revise_verdict(self) -> None:
        """external_reviews_enabled=False has no effect when SOVA has reviewed with revise."""
        assert (
            _apply_sova_verdict(WorkItemState.PR_APPROVED, self._review("revise"), external_reviews_enabled=False)
            == WorkItemState.PR_SOVA_CHANGES
        )


class TestIsVerdictStale:
    def test_no_approval(self) -> None:
        assert not _is_verdict_stale({"reviewed_at": "2026-07-19T10:00:00Z"}, None)

    def test_no_reviewed_at(self) -> None:
        assert not _is_verdict_stale({"reviewed_at": None}, "2026-07-20T12:00:00Z")

    def test_approval_after_review(self) -> None:
        assert _is_verdict_stale({"reviewed_at": "2026-07-19T10:00:00Z"}, "2026-07-20T12:00:00Z")

    def test_approval_before_review(self) -> None:
        assert not _is_verdict_stale({"reviewed_at": "2026-07-20T14:00:00Z"}, "2026-07-19T08:00:00Z")

    def test_mixed_timezone_formats(self) -> None:
        """GitHub uses 'Z', Python isoformat uses '+00:00'; comparison must still work."""
        assert _is_verdict_stale({"reviewed_at": "2026-07-19T10:00:00+00:00"}, "2026-07-20T12:00:00Z")
        assert not _is_verdict_stale({"reviewed_at": "2026-07-20T14:00:00Z"}, "2026-07-19T08:00:00+00:00")


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

    def test_jira_metadata_fields(self) -> None:
        task = {
            "issue": "42",
            "title": "Fix bug",
            "state": "triaged",
            "labels": [],
            "priority": 2,
            "story_points": 3.0,
            "sprint": "Sprint 5",
            "components": ["RBAC"],
            "jira_status": "In Progress",
            "jira_priority": "High",
            "updated_at": "2026-06-10T08:00:00Z",
        }
        item = _build_task_item(task, pr_data=None, running=None, handoff=None)
        assert item["story_points"] == 3.0
        assert item["sprint"] == "Sprint 5"
        assert item["components"] == ["RBAC"]
        assert item["jira_status"] == "In Progress"
        assert item["jira_priority"] == "High"
        assert item["updated_at"] == "2026-06-10T08:00:00Z"

    def test_jira_metadata_defaults(self) -> None:
        task = {"issue": "42", "title": "Fix bug", "state": "triaged", "labels": [], "priority": 2}
        item = _build_task_item(task, pr_data=None, running=None, handoff=None)
        assert item["story_points"] is None
        assert item["sprint"] == ""
        assert item["components"] == []
        assert item["jira_status"] == ""
        assert item["jira_priority"] == ""
        assert item["updated_at"] == ""


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

    def test_format_pr_details_enriched_fields(self) -> None:
        pr = {
            "number": 42,
            "computed_state": "approved",
            "ci_status": "passed",
            "author": "dev",
            "age_seconds": 3600,
            "is_draft": False,
            "additions": 100,
            "deletions": 20,
            "changed_files": 5,
            "thread_total": 3,
            "thread_resolved": 2,
            "review_logins": ["reviewer1"],
            "assignees": ["dev"],
            "updated_at": "2026-06-02T12:00:00Z",
            "commit_count": 4,
        }
        result = _format_pr_details(pr)
        assert result["author"] == "dev"
        assert result["age_seconds"] == 3600
        assert result["is_draft"] is False
        assert result["additions"] == 100
        assert result["deletions"] == 20
        assert result["changed_files"] == 5
        assert result["thread_total"] == 3
        assert result["thread_resolved"] == 2
        assert result["review_logins"] == ["reviewer1"]
        assert result["assignees"] == ["dev"]
        assert result["updated_at"] == "2026-06-02T12:00:00Z"
        assert result["commit_count"] == 4

    def test_format_pr_details_defaults(self) -> None:
        result = _format_pr_details({"number": 42})
        assert result["author"] == ""
        assert result["age_seconds"] == 0
        assert result["is_draft"] is False
        assert result["additions"] == 0
        assert result["deletions"] == 0
        assert result["changed_files"] == 0
        assert result["thread_total"] == 0
        assert result["thread_resolved"] == 0
        assert result["review_logins"] == []
        assert result["assignees"] == []
        assert result["updated_at"] == ""
        assert result["commit_count"] == 0

    def test_extract_handoff_summary(self) -> None:
        h = {"status": "awaiting_action", "summary": "All good"}
        assert _extract_handoff_summary(h, WorkItemState.HANDOFF_PENDING) == "All good"

    def test_extract_handoff_summary_wrong_state(self) -> None:
        h = {"status": "awaiting_action", "summary": "All good"}
        assert _extract_handoff_summary(h, WorkItemState.TRIAGED) == ""

    def test_format_sova_context_none(self) -> None:
        result = _format_sova_context(None)
        assert result == {"has_sova_review": False, "verdict": None}

    def test_format_sova_context_with_verdict(self) -> None:
        verdict = {"has_sova_review": True, "verdict": "revise", "reviewed_at": "2026-07-20T10:00:00Z"}
        result = _format_sova_context(verdict)
        assert result["has_sova_review"] is True
        assert result["verdict"] == "revise"

    def test_format_sova_context_no_review(self) -> None:
        verdict = {"has_sova_review": False, "verdict": None}
        result = _format_sova_context(verdict)
        assert result["has_sova_review"] is False
        assert result["verdict"] is None


class TestSovaContextInItems:
    def test_task_item_includes_sova_context_without_pr(self) -> None:
        task = {"issue": "42", "title": "Fix bug", "state": "triaged", "labels": [], "priority": 2}
        item = _build_task_item(task, pr_data=None, running=None, handoff=None)
        assert "sova_context" in item
        assert item["sova_context"]["has_sova_review"] is False
        assert item["sova_context"]["verdict"] is None

    def test_task_item_includes_sova_context_with_verdict(self) -> None:
        task = {"issue": "42", "title": "Fix bug", "state": "in_review", "labels": [], "priority": -1}
        pr = {"number": 100, "computed_state": "awaiting_review", "state": "OPEN"}
        verdict = {"has_sova_review": True, "verdict": "revise", "reviewed_at": "2026-07-20T10:00:00Z"}
        item = _build_task_item(task, pr_data=pr, running=None, handoff=None, sova_verdict=verdict)
        assert item["sova_context"]["has_sova_review"] is True
        assert item["sova_context"]["verdict"] == "revise"

    def test_pr_item_includes_sova_context(self) -> None:
        pr = {"number": 200, "title": "Quick fix", "computed_state": "approved_ci_green", "state": "OPEN"}
        verdict = {"has_sova_review": False, "verdict": None}
        item = _build_pr_item(pr, running=None, handoff=None, issue_num=None, sova_verdict=verdict)
        assert "sova_context" in item
        assert item["sova_context"]["has_sova_review"] is False

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
        """Unlinked approved PR with has_sova_review=False shows pr_sova_pending when external reviews enabled."""
        items: list[dict] = []
        prs = [{"number": 300, "linked_issue": None, "computed_state": "approved", "state": "OPEN"}]
        verdicts = {"pr:300": {"has_sova_review": False, "verdict": None, "finding_count": 0, "reviewed_at": None}}
        _append_standalone_pr_items(
            items, prs, set(), {}, {}, verdicts_by_issue=verdicts, external_reviews_enabled=True
        )
        assert len(items) == 1
        assert items[0]["state"] == "pr_sova_pending"

    def test_unlinked_pr_no_review_no_external_reviewers_shows_awaiting_review(self) -> None:
        """No external reviewers: approved PR + no SOVA review → pr_awaiting_review (show Review button)."""
        items: list[dict] = []
        prs = [{"number": 400, "linked_issue": None, "computed_state": "approved", "state": "OPEN"}]
        verdicts = {"pr:400": {"has_sova_review": False, "verdict": None, "finding_count": 0, "reviewed_at": None}}
        _append_standalone_pr_items(
            items, prs, set(), {}, {}, verdicts_by_issue=verdicts, external_reviews_enabled=False
        )
        assert len(items) == 1
        assert items[0]["state"] == "pr_awaiting_review"


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

        with (
            patch(
                "sova.dashboard.services.agent_recovery.get_sova_review_verdict",
                new_callable=AsyncMock,
                return_value=mock_verdict,
            ) as mock_call,
            patch(
                "sova.dashboard.services.work_item_service._fetch_github_review_fallback",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
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


class TestParseSovaReviewFromGithub:
    """_parse_sova_review_from_github detects cross-instance SOVA reviews."""

    def _review(
        self,
        body: str,
        state: str = "APPROVED",
        submitted_at: str = "2026-07-21T10:00:00Z",
        is_bot: bool = False,
    ) -> object:
        """Build a minimal PRReview-like object."""
        from sova.adapters.base import PRReview

        return PRReview(reviewer="dsova06", state=state, body=body, submitted_at=submitted_at, is_bot=is_bot)

    def test_detects_marker_approve(self) -> None:
        from sova.dashboard.services.work_item_service import _parse_sova_review_from_github

        review = self._review("<!-- sova-review: approve -->\n\n## PR Summary\n...")
        result = _parse_sova_review_from_github([review])
        assert result is not None
        assert result["has_sova_review"] is True
        assert result["verdict"] == "approve"

    def test_detects_marker_revise(self) -> None:
        from sova.dashboard.services.work_item_service import _parse_sova_review_from_github

        review = self._review("<!-- sova-review: revise -->\n\n## Review: REVISE")
        result = _parse_sova_review_from_github([review])
        assert result is not None
        assert result["verdict"] == "revise"

    def test_detects_marker_block(self) -> None:
        from sova.dashboard.services.work_item_service import _parse_sova_review_from_github

        review = self._review("<!-- sova-review: block -->\n\n## Review: BLOCK")
        result = _parse_sova_review_from_github([review])
        assert result is not None
        assert result["verdict"] == "block"

    def test_marker_case_insensitive(self) -> None:
        from sova.dashboard.services.work_item_service import _parse_sova_review_from_github

        review = self._review("<!-- SOVA-REVIEW: Approve -->")
        result = _parse_sova_review_from_github([review])
        assert result is not None
        assert result["verdict"] == "approve"

    def test_skips_dismissed_reviews(self) -> None:
        from sova.dashboard.services.work_item_service import _parse_sova_review_from_github

        review = self._review("<!-- sova-review: approve -->", state="DISMISSED")
        result = _parse_sova_review_from_github([review])
        assert result is None

    def test_newest_first_ordering(self) -> None:
        from sova.dashboard.services.work_item_service import _parse_sova_review_from_github

        old = self._review("<!-- sova-review: revise -->", submitted_at="2026-07-20T10:00:00Z")
        new = self._review("<!-- sova-review: approve -->", submitted_at="2026-07-21T10:00:00Z")
        result = _parse_sova_review_from_github([old, new])
        assert result is not None
        assert result["verdict"] == "approve"

    def test_returns_none_when_no_sova_review(self) -> None:
        from sova.dashboard.services.work_item_service import _parse_sova_review_from_github

        review = self._review("LGTM, nice work!")
        result = _parse_sova_review_from_github([review])
        assert result is None

    def test_returns_none_for_empty_list(self) -> None:
        from sova.dashboard.services.work_item_service import _parse_sova_review_from_github

        assert _parse_sova_review_from_github([]) is None

    def test_heuristic_fallback_approve(self) -> None:
        from sova.dashboard.services.work_item_service import _parse_sova_review_from_github

        body = (
            "## PR Summary\nThis PR does X.\n\n"
            "## Findings\n\nNone.\n\n"
            "## Verdict\n\n**Approve.** Clean implementation.\n\n"
            "## What's Done Well\nGood tests."
        )
        review = self._review(body)
        result = _parse_sova_review_from_github([review])
        assert result is not None
        assert result["has_sova_review"] is True
        assert result["verdict"] == "approve"

    def test_heuristic_fallback_request_changes(self) -> None:
        from sova.dashboard.services.work_item_service import _parse_sova_review_from_github

        body = "## PR Summary\nThis PR does X.\n\n## Verdict\n\n**Request changes.** Must fix Y.\n\n"
        review = self._review(body)
        result = _parse_sova_review_from_github([review])
        assert result is not None
        assert result["verdict"] == "revise"

    def test_heuristic_fallback_block(self) -> None:
        from sova.dashboard.services.work_item_service import _parse_sova_review_from_github

        body = "## PR Summary\nX.\n\n## Verdict\n\n**Block.** Critical issue.\n"
        review = self._review(body)
        result = _parse_sova_review_from_github([review])
        assert result is not None
        assert result["verdict"] == "block"

    def test_heuristic_requires_both_sections(self) -> None:
        from sova.dashboard.services.work_item_service import _parse_sova_review_from_github

        # Only ## Verdict, no ## PR Summary -- not a SOVA review
        body = "## Verdict\n\n**Approve.** LGTM."
        review = self._review(body)
        result = _parse_sova_review_from_github([review])
        assert result is None

    def test_heuristic_requires_pr_summary_section(self) -> None:
        from sova.dashboard.services.work_item_service import _parse_sova_review_from_github

        # Only ## PR Summary, no ## Verdict -- not a SOVA review
        body = "## PR Summary\nThis PR does X."
        review = self._review(body)
        result = _parse_sova_review_from_github([review])
        assert result is None

    def test_dismissed_review_skipped_even_when_next_has_no_sova_marker(self) -> None:
        from sova.dashboard.services.work_item_service import _parse_sova_review_from_github

        # The dismissed review has the marker; the non-dismissed one is a plain human review.
        # Expected: None -- the dismissed review is skipped and the human review is not SOVA.
        dismissed = self._review(
            "<!-- sova-review: approve -->", state="DISMISSED", submitted_at="2026-07-21T12:00:00Z"
        )
        human = self._review("LGTM!", state="APPROVED", submitted_at="2026-07-20T10:00:00Z")
        result = _parse_sova_review_from_github([dismissed, human])
        assert result is None

    def test_submitted_at_propagated(self) -> None:
        from sova.dashboard.services.work_item_service import _parse_sova_review_from_github

        ts = "2026-07-21T12:34:56Z"
        review = self._review("<!-- sova-review: approve -->", submitted_at=ts)
        result = _parse_sova_review_from_github([review])
        assert result is not None
        assert result["reviewed_at"] == ts


class TestFetchSovaVerdictsGithubFallback:
    """_fetch_sova_verdicts uses GitHub review fallback when DB has no SOVA review."""

    @pytest.mark.asyncio()
    async def test_github_fallback_used_when_no_db_review(self) -> None:
        from sova.dashboard.services.work_item_service import _fetch_sova_verdicts

        no_review = {"has_sova_review": False, "verdict": None, "finding_count": 0, "reviewed_at": None}
        gh_verdict = {"has_sova_review": True, "verdict": "approve", "finding_count": 0, "reviewed_at": None}
        mock_adapter = MagicMock()

        with (
            patch(
                "sova.dashboard.services.agent_recovery.get_sova_review_verdict",
                new_callable=AsyncMock,
                return_value=no_review,
            ),
            patch("sova.config.loader.load_config", return_value=MagicMock()),
            patch("sova.adapters.create_adapter", return_value=mock_adapter),
            patch(
                "sova.dashboard.services.work_item_service._fetch_github_review_fallback",
                new_callable=AsyncMock,
                return_value=gh_verdict,
            ) as mock_fallback,
        ):
            result = await _fetch_sova_verdicts({"42": {"number": 100}})

        mock_fallback.assert_called_once_with(100, mock_adapter)
        assert result["42"]["has_sova_review"] is True
        assert result["42"]["verdict"] == "approve"

    @pytest.mark.asyncio()
    async def test_github_fallback_not_called_when_db_has_review(self) -> None:
        from sova.dashboard.services.work_item_service import _fetch_sova_verdicts

        db_verdict = {"has_sova_review": True, "verdict": "revise", "finding_count": 2, "reviewed_at": None}

        with (
            patch(
                "sova.dashboard.services.agent_recovery.get_sova_review_verdict",
                new_callable=AsyncMock,
                return_value=db_verdict,
            ),
            patch(
                "sova.dashboard.services.work_item_service._fetch_github_review_fallback",
                new_callable=AsyncMock,
            ) as mock_fallback,
        ):
            result = await _fetch_sova_verdicts({"42": {"number": 100}})

        mock_fallback.assert_not_called()
        assert result["42"]["has_sova_review"] is True
        assert result["42"]["verdict"] == "revise"

    @pytest.mark.asyncio()
    async def test_github_fallback_not_called_when_no_pr_number(self) -> None:
        from sova.dashboard.services.work_item_service import _fetch_sova_verdicts

        no_review = {"has_sova_review": False, "verdict": None, "finding_count": 0, "reviewed_at": None}

        with (
            patch(
                "sova.dashboard.services.agent_recovery.get_sova_review_verdict",
                new_callable=AsyncMock,
                return_value=no_review,
            ),
            patch(
                "sova.dashboard.services.work_item_service._fetch_github_review_fallback",
                new_callable=AsyncMock,
            ) as mock_fallback,
        ):
            result = await _fetch_sova_verdicts({"42": {}})  # no pr number

        mock_fallback.assert_not_called()
        assert result["42"]["has_sova_review"] is False

    @pytest.mark.asyncio()
    async def test_github_fallback_returning_none_preserves_no_review(self) -> None:
        from sova.dashboard.services.work_item_service import _fetch_sova_verdicts

        no_review = {"has_sova_review": False, "verdict": None, "finding_count": 0, "reviewed_at": None}

        with (
            patch(
                "sova.dashboard.services.agent_recovery.get_sova_review_verdict",
                new_callable=AsyncMock,
                return_value=no_review,
            ),
            patch("sova.config.loader.load_config", return_value=MagicMock()),
            patch("sova.adapters.create_adapter", return_value=MagicMock()),
            patch(
                "sova.dashboard.services.work_item_service._fetch_github_review_fallback",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            result = await _fetch_sova_verdicts({"42": {"number": 100}})

        assert result["42"]["has_sova_review"] is False


class TestFetchGithubReviewFallback:
    """Direct tests for _fetch_github_review_fallback."""

    @pytest.mark.asyncio()
    async def test_returns_parsed_verdict_on_success(self) -> None:
        from sova.adapters.base import PRReview
        from sova.dashboard.services.work_item_service import _fetch_github_review_fallback

        review = PRReview(
            reviewer="dsova06",
            state="APPROVED",
            body="<!-- sova-review: approve -->",
            submitted_at="2026-07-21T10:00:00Z",
            is_bot=False,
        )
        mock_adapter = AsyncMock()
        mock_adapter.get_pr_reviews.return_value = [review]

        result = await _fetch_github_review_fallback(100, mock_adapter)

        assert result is not None
        assert result["has_sova_review"] is True
        assert result["verdict"] == "approve"

    @pytest.mark.asyncio()
    async def test_returns_none_when_no_sova_review_found(self) -> None:
        from sova.dashboard.services.work_item_service import _fetch_github_review_fallback

        mock_adapter = AsyncMock()
        mock_adapter.get_pr_reviews.return_value = []

        result = await _fetch_github_review_fallback(100, mock_adapter)

        assert result is None

    @pytest.mark.asyncio()
    async def test_skips_fallback_when_adapter_build_fails(self) -> None:
        """When adapter build fails in _fetch_sova_verdicts, fallback is not called."""
        from sova.dashboard.services.work_item_service import _fetch_sova_verdicts

        no_review = {"has_sova_review": False, "verdict": None, "finding_count": 0, "reviewed_at": None}

        with (
            patch(
                "sova.dashboard.services.agent_recovery.get_sova_review_verdict",
                new_callable=AsyncMock,
                return_value=no_review,
            ),
            patch("sova.config.loader.load_config", side_effect=RuntimeError("config broken")),
            patch(
                "sova.dashboard.services.work_item_service._fetch_github_review_fallback",
                new_callable=AsyncMock,
            ) as mock_fallback,
        ):
            result = await _fetch_sova_verdicts({"42": {"number": 100}})

        mock_fallback.assert_not_called()
        assert result["42"]["has_sova_review"] is False

    @pytest.mark.asyncio()
    async def test_returns_none_on_adapter_api_exception(self) -> None:
        from sova.dashboard.services.work_item_service import _fetch_github_review_fallback

        mock_adapter = AsyncMock()
        mock_adapter.get_pr_reviews.side_effect = RuntimeError("API failure")

        result = await _fetch_github_review_fallback(100, mock_adapter)

        assert result is None


class TestFetchSovaVerdictsExceptionHandling:
    """_fetch_sova_verdicts handles per-item exceptions in fetch_one gracefully."""

    @pytest.mark.asyncio()
    async def test_exception_in_get_sova_review_verdict_returns_no_review(self) -> None:
        from sova.dashboard.services.work_item_service import _fetch_sova_verdicts

        with patch(
            "sova.dashboard.services.agent_recovery.get_sova_review_verdict",
            new_callable=AsyncMock,
            side_effect=RuntimeError("DB error"),
        ):
            result = await _fetch_sova_verdicts({"42": {"number": 100}})

        assert "42" in result
        assert result["42"]["has_sova_review"] is False
        assert result["42"]["verdict"] is None
        assert result["42"]["finding_count"] == 0


class TestHeuristicVerdictRegexScope:
    """Heuristic verdict regex is scoped to the ## Verdict section only."""

    def test_bold_line_in_findings_not_matched_as_verdict(self) -> None:
        """A bold 'Approve' line inside ## Findings must not override the ## Verdict verdict."""
        from sova.adapters.base import PRReview
        from sova.dashboard.services.work_item_service import _parse_sova_review_from_github

        body = (
            "## PR Summary\nThis PR does X.\n\n"
            "## Findings\n\n**Approve this approach but fix the test.**\n\n"
            "## Verdict\n\n**Request changes.** Fix the issue.\n"
        )
        review = PRReview(
            reviewer="dsova06",
            state="CHANGES_REQUESTED",
            body=body,
            submitted_at="2026-07-21T10:00:00Z",
            is_bot=False,
        )
        result = _parse_sova_review_from_github([review])
        assert result is not None
        # Must be "revise" (from ## Verdict), not "approve" (from ## Findings bold line)
        assert result["verdict"] == "revise"
