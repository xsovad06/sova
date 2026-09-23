"""Tests for unified work item state service."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sova.dashboard.services.work_item_service import (
    PRFacts,
    WorkItemState,
    _append_standalone_pr_items,
    _attach_integration_gates,
    _build_pr_item,
    _build_task_item,
    _extract_handoff_summary,
    _extract_sova_verdict_from_labels,
    _find_integrate_action,
    _format_pr_details,
    _format_running_agent,
    _format_sova_context,
    _get_actions,
    _index_handoffs,
    _index_prs_by_issue,
    _index_running_agents,
    _sort_items,
    clear_verdict_cache,
    compute_work_item_resolution,
    compute_work_item_state,
    describe_reason_chain,
    get_work_items,
    resolve_next_action,
)


def _state(**kwargs: object) -> WorkItemState:
    defaults: dict[str, object] = {
        "task_state": None,
        "pr_data": None,
        "running_agent": None,
    }
    defaults.update(kwargs)
    return compute_work_item_state(**defaults)  # type: ignore[arg-type]


class TestComputeWorkItemState:
    """Priority cascade: running > PR (via resolve_next_action(), #991) > label.

    compute_work_item_state() is now a thin adapter over resolve_next_action():
    it no longer trusts pr_data["computed_state"] for CI/draft/mergeable, only for
    deriving external_changes_requested. See TestResolveNextAction for the full
    golden table over the ladder itself.
    """

    # Priority 1: Running agent

    def test_running_agent_overrides_everything(self) -> None:
        assert (
            _state(
                task_state="in_review",
                pr_data={"computed_state": "approved_ci_green", "state": "OPEN"},
                running_agent={"run_id": 1, "role": "developer"},
            )
            == WorkItemState.AGENT_RUNNING
        )

    def test_running_agent_alone(self) -> None:
        assert _state(running_agent={"run_id": 1, "role": "triage"}) == WorkItemState.AGENT_RUNNING

    # Priority 2: PR state (SOVA-adjusted)

    def test_sova_block_verdict_with_pr(self) -> None:
        """A blocking SOVA verdict overrides PR state to PR_SOVA_CHANGES."""
        assert (
            _state(
                pr_data={"computed_state": "approved_ci_green", "state": "OPEN"},
                sova_verdict={"verdict": "block", "has_sova_review": True, "reviewed_at": "2026-07-24T10:00:00Z"},
            )
            == WorkItemState.PR_SOVA_CHANGES
        )

    def test_sova_revise_verdict_with_pr(self) -> None:
        """A revise SOVA verdict overrides PR state to PR_SOVA_CHANGES."""
        assert (
            _state(
                pr_data={"computed_state": "approved_ci_green", "state": "OPEN"},
                sova_verdict={"verdict": "revise", "has_sova_review": True, "reviewed_at": "2026-07-24T10:00:00Z"},
            )
            == WorkItemState.PR_SOVA_CHANGES
        )

    def test_stale_sova_verdict_falls_to_sova_pending(self) -> None:
        """A verdict anchored to an older commit than the current head is treated as no current
        review (resolve_next_action() rule 8/10), not as a pass-through of the raw PR state."""
        assert (
            _state(
                pr_data={
                    "computed_state": "approved_ci_green",
                    "state": "OPEN",
                    "head_sha": "def456",
                },
                sova_verdict={"verdict": "block", "has_sova_review": True, "review_head_sha": "abc123"},
            )
            == WorkItemState.PR_SOVA_PENDING
        )

    def test_bot_approval_does_not_erase_anchored_revise_verdict(self) -> None:
        """Reproduces the original bug: a revise verdict anchored to SHA X, followed by a
        bot approval, with the PR head SHA still X, must remain revise (not discarded)."""
        assert (
            _state(
                pr_data={
                    "computed_state": "approved_ci_green",
                    "state": "OPEN",
                    "head_sha": "abc123",
                    "latest_approval_at": None,  # bot approvals are excluded upstream
                },
                sova_verdict={"verdict": "revise", "has_sova_review": True, "review_head_sha": "abc123"},
            )
            == WorkItemState.PR_SOVA_CHANGES
        )

    def test_anchored_verdict_stale_once_pr_head_advances(self) -> None:
        """A verdict anchored to an old SHA is reported stale once new commits are pushed."""
        assert (
            _state(
                pr_data={
                    "computed_state": "approved_ci_green",
                    "state": "OPEN",
                    "head_sha": "new_sha_after_push",
                },
                sova_verdict={"verdict": "revise", "has_sova_review": True, "review_head_sha": "old_sha"},
            )
            == WorkItemState.PR_SOVA_PENDING
        )

    # Priority 3: PR state

    def test_pr_ready_to_merge(self) -> None:
        """Reaching PR_READY_TO_MERGE now requires the full rule-12 fact set: an approve verdict
        plus green CI plus a mergeable PR, not just an "approved_ci_green" computed_state."""
        assert (
            _state(
                pr_data={
                    "computed_state": "approved_ci_green",
                    "state": "OPEN",
                    "is_draft": False,
                    "mergeable": "MERGEABLE",
                    "ci_status": "passed",
                    "head_sha": "abc123",
                },
                sova_verdict={"has_sova_review": True, "verdict": "approve", "review_head_sha": None},
            )
            == WorkItemState.PR_READY_TO_MERGE
        )

    def test_pr_ci_running(self) -> None:
        assert (
            _state(
                pr_data={"computed_state": "ci_running", "state": "OPEN", "ci_status": "pending"},
            )
            == WorkItemState.PR_CI_RUNNING
        )

    def test_pr_ci_failed(self) -> None:
        assert (
            _state(
                pr_data={"computed_state": "ci_failed", "state": "OPEN", "ci_status": "failed"},
            )
            == WorkItemState.PR_CI_FAILED
        )

    def test_pr_conflicted(self) -> None:
        """mergeable == CONFLICTING routes to PR_CONFLICTED regardless of computed_state (rule 3).

        _build_pr_facts() reads mergeable directly and ignores computed_state="conflicted"
        for this purpose (see TestResolveNextAction for the full golden table); real PR
        data always sets both consistently since compute_pr_state() derives computed_state
        from mergeable in the first place (pr_service.py).
        """
        assert (
            _state(
                pr_data={"computed_state": "conflicted", "state": "OPEN", "mergeable": "CONFLICTING"},
            )
            == WorkItemState.PR_CONFLICTED
        )

    def test_pr_conflicted_survives_sova_approved_verdict(self) -> None:
        """Conflicted PRs must not be promoted to approved states even with a clean SOVA verdict."""
        verdict = {"has_sova_review": True, "verdict": "approve", "finding_count": 0, "reviewed_at": None}
        assert (
            _state(
                pr_data={
                    "computed_state": "conflicted",
                    "state": "OPEN",
                    "mergeable": "CONFLICTING",
                    "thread_total": 2,
                    "thread_resolved": 2,
                },
                sova_verdict=verdict,
            )
            == WorkItemState.PR_CONFLICTED
        )

    def test_pr_conflicting_never_integrates(self) -> None:
        """mergeable == CONFLICTING routes to PR_CONFLICTED regardless of verdict/CI (rule 3)."""
        assert (
            _state(
                pr_data={
                    "computed_state": "approved_ci_green",
                    "state": "OPEN",
                    "mergeable": "CONFLICTING",
                    "ci_status": "passed",
                },
                sova_verdict={"has_sova_review": True, "verdict": "approve", "review_head_sha": None},
            )
            == WorkItemState.PR_CONFLICTED
        )

    def test_pr_changes_requested(self) -> None:
        """Standing external CHANGES_REQUESTED (with a SOVA approval already on record)
        maps to PR_EXTERNAL_CHANGES (rule 11); without any SOVA verdict, rule 10 fires first."""
        assert (
            _state(
                pr_data={"computed_state": "changes_requested", "state": "OPEN"},
                sova_verdict={"has_sova_review": True, "verdict": "approve", "review_head_sha": None},
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
        """SOVA revise verdict wins over a standing external changes_requested (rule 7 precedes rule 11)."""
        verdict = {"has_sova_review": True, "verdict": "revise", "finding_count": 1, "reviewed_at": None}
        assert (
            _state(
                pr_data={"computed_state": "changes_requested", "state": "OPEN"},
                sova_verdict=verdict,
            )
            == WorkItemState.PR_SOVA_CHANGES
        )

    def test_external_reviews_disabled_no_sova_pending(self) -> None:
        """With external_reviews_enabled=False, no SOVA review → PR_AWAITING_REVIEW, not PR_SOVA_PENDING."""
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

    def test_pr_awaiting_review_without_verdict_yields_sova_pending(self) -> None:
        """No SOVA verdict supplied at all defaults to PR_SOVA_PENDING (rule 10)."""
        assert (
            _state(
                pr_data={"computed_state": "awaiting_review", "state": "OPEN"},
            )
            == WorkItemState.PR_SOVA_PENDING
        )

    def test_pr_review_addressed_needs_completed_address_cycle(self) -> None:
        """PR_REVIEW_ADDRESSED is now driven solely by sova_verdict_addressed (#988, rule 9), not
        GitHub's computed_state: a plain 'review_addressed' with no SOVA verdict at all falls to
        PR_SOVA_PENDING like any other unreviewed PR."""
        assert (
            _state(
                pr_data={"computed_state": "review_addressed", "state": "OPEN"},
            )
            == WorkItemState.PR_SOVA_PENDING
        )

    def test_pr_review_addressed_from_completed_address_cycle(self) -> None:
        """A completed address cycle (verdict == "addressed") routes to PR_REVIEW_ADDRESSED
        regardless of the superseded prior verdict (rule 9)."""
        assert (
            _state(
                pr_data={"computed_state": "approved_ci_green", "state": "OPEN"},
                sova_verdict={"has_sova_review": True, "verdict": "addressed", "review_head_sha": None},
            )
            == WorkItemState.PR_REVIEW_ADDRESSED
        )

    def test_pr_approved_without_ci_confirmation_yields_sova_pending(self) -> None:
        """PR_APPROVED is superseded by PR_READY_TO_MERGE as the sole integrate-bound state
        (rule 12): a PR with no verdict data falls to PR_SOVA_PENDING."""
        assert (
            _state(
                pr_data={"computed_state": "approved", "state": "OPEN"},
            )
            == WorkItemState.PR_SOVA_PENDING
        )

    def test_pr_draft(self) -> None:
        assert (
            _state(
                pr_data={"computed_state": "draft", "state": "OPEN", "is_draft": True},
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
            == WorkItemState.PR_SOVA_PENDING
        )

    # Priority 4: GitHub label state

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

    # SOVA verdict integration

    def test_pr_approved_no_sova_review_yields_sova_pending(self) -> None:
        verdict = {"has_sova_review": False, "verdict": None, "finding_count": 0, "reviewed_at": None}
        assert (
            _state(
                pr_data={"computed_state": "approved", "state": "OPEN"},
                sova_verdict=verdict,
            )
            == WorkItemState.PR_SOVA_PENDING
        )

    def test_pr_approved_with_sova_approve_reaches_ready_to_merge(self) -> None:
        """A SOVA approve verdict plus green CI and mergeable state reaches PR_READY_TO_MERGE
        (rule 12); PR_APPROVED is superseded as the integrate-bound state (#991)."""
        verdict = {"has_sova_review": True, "verdict": "approve", "finding_count": 0, "reviewed_at": None}
        assert (
            _state(
                pr_data={
                    "computed_state": "approved",
                    "state": "OPEN",
                    "ci_status": "passed",
                    "mergeable": "MERGEABLE",
                },
                sova_verdict=verdict,
            )
            == WorkItemState.PR_READY_TO_MERGE
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

    # Edge cases

    def test_no_inputs_defaults_backlog(self) -> None:
        assert _state() == WorkItemState.BACKLOG


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

    def test_pr_conflicted_has_rebase_primary_no_integrate(self) -> None:
        primary, secondary = _get_actions(WorkItemState.PR_CONFLICTED, issue_number="42", pr_number=123)
        assert primary is not None
        assert primary["id"] == "rebase"
        assert primary["handler"] == "trigger_rebase"
        secondary_ids = [a["id"] for a in secondary]
        assert "review_pr" in secondary_ids
        assert "address_pr" in secondary_ids
        all_ids = ([primary["id"]] if primary else []) + secondary_ids
        assert "integrate" not in all_ids
        assert all(a["handler_args"].get("command") != "integrate-pr" for a in secondary)

    def test_pr_conflicted_without_issue_has_no_primary(self) -> None:
        """attempt_auto_rebase() resolves via issue number; no issue means no rebase action."""
        primary, _ = _get_actions(WorkItemState.PR_CONFLICTED, issue_number=None, pr_number=99)
        assert primary is None

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

    def test_spec_review_before_normal(self) -> None:
        items = [
            {"state": "researched", "priority": 0},
            {"state": "spec_review", "priority": 99},
        ]
        _sort_items(items)
        assert items[0]["state"] == "spec_review"

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


def _facts(**overrides: object) -> PRFacts:
    """A fully mergeable, green, approved, thread-clear PRFacts snapshot (rule 12 default).

    Each golden-table row overrides only the fields relevant to the rule it exercises,
    isolating that rule from every other one.
    """
    defaults: dict = {
        "running_agent": False,
        "pr_state": "OPEN",
        "is_draft": False,
        "mergeable": "MERGEABLE",
        "ci_status": "passed",
        "head_sha": "sha-head",
        "sova_verdict": "approve",
        "sova_verdict_sha": "sha-head",
        "sova_verdict_addressed": False,
        "external_changes_requested": False,
        "thread_signal": "clear",
        "external_reviews_enabled": True,
    }
    defaults.update(overrides)
    return PRFacts(**defaults)


class TestResolveNextAction:
    """Golden table for resolve_next_action()'s 13-rule, first-match-wins ladder (#991)."""

    @pytest.mark.parametrize(
        ("scenario", "facts", "expected_state", "expected_action_id"),
        [
            ("agent_running", _facts(running_agent=True), WorkItemState.AGENT_RUNNING, None),
            ("merged", _facts(pr_state="MERGED"), WorkItemState.MERGED, None),
            ("conflicting", _facts(mergeable="CONFLICTING"), WorkItemState.PR_CONFLICTED, "rebase"),
            (
                "conflicting_beats_approve_verdict",
                _facts(mergeable="CONFLICTING", sova_verdict="approve", thread_signal="clear", ci_status="passed"),
                WorkItemState.PR_CONFLICTED,
                "rebase",
            ),
            ("draft", _facts(is_draft=True), WorkItemState.PR_DRAFT, None),
            ("ci_failed", _facts(ci_status="failed"), WorkItemState.PR_CI_FAILED, "address_pr"),
            ("ci_pending", _facts(ci_status="pending"), WorkItemState.PR_CI_RUNNING, None),
            ("ci_running", _facts(ci_status="running"), WorkItemState.PR_CI_RUNNING, None),
            (
                "standing_revise_on_current_head",
                _facts(sova_verdict="revise", sova_verdict_sha="sha-head"),
                WorkItemState.PR_SOVA_CHANGES,
                "address_review",
            ),
            (
                "standing_block_on_current_head",
                _facts(sova_verdict="block", sova_verdict_sha="sha-head"),
                WorkItemState.PR_SOVA_CHANGES,
                "address_review",
            ),
            (
                "verdict_anchored_to_stale_sha",
                _facts(sova_verdict="revise", sova_verdict_sha="old-sha", head_sha="sha-head"),
                WorkItemState.PR_SOVA_PENDING,
                "review_pr",
            ),
            (
                "address_cycle_completed_after_review",
                _facts(sova_verdict="revise", sova_verdict_addressed=True),
                WorkItemState.PR_REVIEW_ADDRESSED,
                "review_pr",
            ),
            (
                "addressed_but_external_changes_requested_routes_to_address_pr",
                _facts(sova_verdict="revise", sova_verdict_addressed=True, external_changes_requested=True),
                WorkItemState.PR_EXTERNAL_CHANGES,
                "address_pr",
            ),
            (
                "addressed_but_unresolved_threads_routes_to_address_pr",
                _facts(sova_verdict="revise", sova_verdict_addressed=True, thread_signal="pending"),
                WorkItemState.PR_EXTERNAL_CHANGES,
                "address_pr",
            ),
            (
                "no_sova_review_but_external_changes_requested_routes_to_address_pr",
                _facts(sova_verdict=None, sova_verdict_sha=None, external_changes_requested=True),
                WorkItemState.PR_EXTERNAL_CHANGES,
                "address_pr",
            ),
            (
                "stale_verdict_with_unresolved_threads_routes_to_address_pr",
                _facts(sova_verdict="revise", sova_verdict_sha="old-sha", head_sha="sha-head", thread_signal="pending"),
                WorkItemState.PR_EXTERNAL_CHANGES,
                "address_pr",
            ),
            (
                "standing_revise_on_current_head_still_beats_external_changes",
                _facts(sova_verdict="revise", sova_verdict_sha="sha-head", external_changes_requested=True),
                WorkItemState.PR_SOVA_CHANGES,
                "address_review",
            ),
            (
                "no_sova_review_at_all",
                _facts(sova_verdict=None, sova_verdict_sha=None),
                WorkItemState.PR_SOVA_PENDING,
                "review_pr",
            ),
            (
                "no_sova_review_external_reviews_disabled",
                _facts(sova_verdict=None, sova_verdict_sha=None, external_reviews_enabled=False),
                WorkItemState.PR_AWAITING_REVIEW,
                "review_pr",
            ),
            (
                "standing_external_changes_requested",
                _facts(external_changes_requested=True),
                WorkItemState.PR_EXTERNAL_CHANGES,
                "address_pr",
            ),
            (
                "unresolved_threads_pending",
                _facts(thread_signal="pending"),
                WorkItemState.PR_EXTERNAL_CHANGES,
                "address_pr",
            ),
            (
                "unresolved_threads_unknown",
                _facts(thread_signal="unknown"),
                WorkItemState.PR_EXTERNAL_CHANGES,
                "address_pr",
            ),
            (
                "approve_clear_green_mergeable_is_the_only_integrate_path",
                _facts(),
                WorkItemState.PR_READY_TO_MERGE,
                "integrate",
            ),
            (
                "approve_but_mergeability_not_yet_computed_is_not_integrate",
                _facts(mergeable="UNKNOWN"),
                WorkItemState.PR_AWAITING_REVIEW,
                "review_pr",
            ),
            (
                "approve_but_no_ci_checks_configured_is_not_integrate",
                _facts(ci_status="none"),
                WorkItemState.PR_AWAITING_REVIEW,
                "review_pr",
            ),
            (
                "post_failed_verdict_falls_to_awaiting_review",
                _facts(sova_verdict="post_failed", sova_verdict_sha=None),
                WorkItemState.PR_AWAITING_REVIEW,
                "review_pr",
            ),
        ],
    )
    def test_golden_table(
        self,
        scenario: str,
        facts: PRFacts,
        expected_state: WorkItemState,
        expected_action_id: str | None,
    ) -> None:
        resolution = resolve_next_action(facts)
        assert resolution.state == expected_state, scenario
        assert resolution.action_id == expected_action_id, scenario

    def test_unknown_thread_signal_never_resolves_like_clear(self) -> None:
        """thread_signal == 'unknown' must never resolve the same way as 'clear' (fail closed)."""
        clear = resolve_next_action(_facts(thread_signal="clear"))
        unknown = resolve_next_action(_facts(thread_signal="unknown"))
        assert clear.state == WorkItemState.PR_READY_TO_MERGE
        assert unknown.state == WorkItemState.PR_EXTERNAL_CHANGES
        assert clear.state != unknown.state

    def test_reason_chain_truncates_at_match(self) -> None:
        """reason_chain lists every rule evaluated up to and including the match, no further."""
        resolution = resolve_next_action(_facts(mergeable="CONFLICTING"))
        assert resolution.reason_chain == ("agent_running", "merged", "conflicting")
        assert resolution.reason_chain[-1] == "conflicting"

    def test_reason_chain_covers_full_ladder_on_default_match(self) -> None:
        """The all-green default only matches the final rule, so every earlier rule is listed."""
        resolution = resolve_next_action(_facts())
        assert resolution.state == WorkItemState.PR_READY_TO_MERGE
        assert resolution.reason_chain[-1] == "ready_to_merge"
        assert len(resolution.reason_chain) == 12

    def test_resolution_is_frozen(self) -> None:
        resolution = resolve_next_action(_facts())
        with pytest.raises(AttributeError):
            resolution.state = WorkItemState.MERGED  # type: ignore[misc]


class TestDescribeReasonChain:
    """Renders resolve_next_action()'s reason_chain identifiers to sentences (#992)."""

    def test_every_ladder_rule_has_a_renderer(self) -> None:
        """Every identifier resolve_next_action() can emit produces a non-identifier sentence."""
        all_rule_ids = (
            "agent_running",
            "merged",
            "conflicting",
            "draft",
            "ci_failed",
            "ci_running",
            "sova_standing_changes",
            "sova_verdict_stale",
            "sova_verdict_addressed",
            "no_sova_review",
            "external_changes_or_unresolved_threads",
            "ready_to_merge",
            "awaiting_review",
        )
        sentences = describe_reason_chain(all_rule_ids, _facts())
        assert len(sentences) == len(all_rule_ids)
        for rule_id, sentence in zip(all_rule_ids, sentences, strict=True):
            assert sentence != rule_id
            assert sentence

    def test_unrecognised_rule_id_renders_as_itself(self) -> None:
        """A future ladder rule with no renderer must not crash; it falls back to the identifier."""
        sentences = describe_reason_chain(("some_future_rule",), _facts())
        assert sentences == ["some_future_rule"]

    def test_external_changes_are_evaluated_before_addressed_and_no_review(self) -> None:
        """A bot CHANGES_REQUESTED posted after an address cycle must not hide behind
        PR_REVIEW_ADDRESSED: the external rule now precedes the addressed/no-review
        routes to review_pr, so the chain ends at the external rule and never
        records either of them."""
        resolution = resolve_next_action(
            _facts(sova_verdict="revise", sova_verdict_addressed=True, external_changes_requested=True)
        )
        assert resolution.reason_chain[-1] == "external_changes_or_unresolved_threads"
        assert "sova_verdict_addressed" not in resolution.reason_chain
        assert "no_sova_review" not in resolution.reason_chain
        # And the standing-changes rule still comes first: a live SOVA revise on the
        # current head wins over an external request, per the PR_SOVA_CHANGES split.
        standing = resolve_next_action(
            _facts(sova_verdict="revise", sova_verdict_sha="sha-head", external_changes_requested=True)
        )
        assert standing.reason_chain[-1] == "sova_standing_changes"

    def test_thread_signal_unknown_never_reads_like_clear(self) -> None:
        clear_sentence = describe_reason_chain(
            ("external_changes_or_unresolved_threads",), _facts(thread_signal="clear")
        )[0]
        unknown_sentence = describe_reason_chain(
            ("external_changes_or_unresolved_threads",), _facts(thread_signal="unknown")
        )[0]
        pending_sentence = describe_reason_chain(
            ("external_changes_or_unresolved_threads",), _facts(thread_signal="pending")
        )[0]
        assert clear_sentence != unknown_sentence
        assert clear_sentence != pending_sentence
        assert unknown_sentence != pending_sentence
        assert "unknown" in unknown_sentence

    def test_stale_comparison_with_missing_sha_reports_unknown_anchor(self) -> None:
        no_verdict_sha = describe_reason_chain(
            ("sova_verdict_stale",), _facts(sova_verdict="revise", sova_verdict_sha=None, head_sha="sha-head")
        )[0]
        no_head_sha = describe_reason_chain(
            ("sova_verdict_stale",), _facts(sova_verdict="revise", sova_verdict_sha="sha-x", head_sha="")
        )[0]
        assert "unknown" in no_verdict_sha
        assert "unknown" in no_head_sha

    def test_stale_comparison_reports_current_vs_stale(self) -> None:
        current = describe_reason_chain(
            ("sova_verdict_stale",), _facts(sova_verdict="revise", sova_verdict_sha="sha-head", head_sha="sha-head")
        )[0]
        stale = describe_reason_chain(
            ("sova_verdict_stale",),
            _facts(sova_verdict="revise", sova_verdict_sha="abc1234full", head_sha="def5678full"),
        )[0]
        no_review = describe_reason_chain(("sova_verdict_stale",), _facts(sova_verdict=None, sova_verdict_sha=None))[0]
        assert "stale" in stale
        assert "abc1234" in stale
        assert "def5678" in stale
        assert "stale" not in current
        assert no_review != current
        assert no_review != stale

    def test_mergeable_unknown_and_ci_status_empty_render_as_unknown(self) -> None:
        conflicting_sentence = describe_reason_chain(("conflicting",), _facts(mergeable="UNKNOWN"))[0]
        ci_failed_sentence = describe_reason_chain(("ci_failed",), _facts(ci_status=""))[0]
        ci_running_sentence = describe_reason_chain(("ci_running",), _facts(ci_status=""))[0]
        assert "unknown" in conflicting_sentence
        assert "unknown" in ci_failed_sentence
        assert "unknown" in ci_running_sentence

    def test_ready_to_merge_non_match_describes_unmet_conditions(self) -> None:
        sentence = describe_reason_chain(("ready_to_merge",), _facts(sova_verdict="revise", ci_status="failed"))[0]
        assert "not ready to merge" in sentence
        assert "verdict" in sentence
        assert "CI status" in sentence

    def test_ready_to_merge_match_describes_ready_state(self) -> None:
        sentence = describe_reason_chain(("ready_to_merge",), _facts())[0]
        assert "ready to merge" in sentence
        assert "not ready" not in sentence


class TestComputeWorkItemResolution:
    """compute_work_item_resolution() returns the full Resolution plus PRFacts (#992)."""

    def test_label_only_item_has_empty_chain_and_no_facts(self) -> None:
        resolution, facts = compute_work_item_resolution(task_state="triaged", pr_data=None, running_agent=None)
        assert resolution.state == WorkItemState.TRIAGED
        assert resolution.reason_chain == ()
        assert facts is None

    def test_no_state_at_all_falls_back_to_backlog_with_empty_chain(self) -> None:
        resolution, facts = compute_work_item_resolution(task_state=None, pr_data=None, running_agent=None)
        assert resolution.state == WorkItemState.BACKLOG
        assert resolution.reason_chain == ()
        assert facts is None

    def test_running_agent_without_pr_data_still_builds_facts_and_matches_state(self) -> None:
        resolution, facts = compute_work_item_resolution(
            task_state="in_progress", pr_data=None, running_agent={"role": "developer"}
        )
        assert resolution.state == WorkItemState.AGENT_RUNNING
        assert resolution.reason_chain == ("agent_running",)
        assert facts is not None
        assert facts.running_agent is True

    def test_running_agent_with_pr_data_flips_running_agent_flag(self) -> None:
        pr_data = {"state": "OPEN", "mergeable": "MERGEABLE", "ci_status": "passed", "head_sha": "x"}
        resolution, facts = compute_work_item_resolution(
            task_state=None, pr_data=pr_data, running_agent={"role": "developer"}
        )
        assert resolution.state == WorkItemState.AGENT_RUNNING
        assert facts is not None
        assert facts.running_agent is True
        assert facts.mergeable == "MERGEABLE"

    def test_pr_data_without_running_agent_matches_resolve_next_action_directly(self) -> None:
        pr_data = {"state": "MERGED"}
        resolution, facts = compute_work_item_resolution(task_state=None, pr_data=pr_data, running_agent=None)
        assert resolution.state == WorkItemState.MERGED
        assert facts is not None
        assert facts.running_agent is False

    def test_matches_compute_work_item_state_for_same_inputs(self) -> None:
        pr_data = {"state": "OPEN", "mergeable": "CONFLICTING"}
        resolution, _facts_result = compute_work_item_resolution(task_state=None, pr_data=pr_data, running_agent=None)
        state_only = compute_work_item_state(task_state=None, pr_data=pr_data, running_agent=None)
        assert resolution.state == state_only


class TestBuildTaskItem:
    def test_basic_task(self) -> None:
        task = {"issue": "42", "title": "Fix bug", "state": "triaged", "labels": [], "priority": 2}
        item = _build_task_item(task, pr_data=None, running=None, handoff=None)
        assert item["issue_number"] == "42"
        assert item["state"] == "triaged"
        assert item["primary_action"]["id"] == "research"

    def test_task_with_pr(self) -> None:
        task = {"issue": "42", "title": "Fix bug", "state": "in_review", "labels": [], "priority": -1}
        pr = {
            "number": 100,
            "computed_state": "approved_ci_green",
            "state": "OPEN",
            "mergeable": "MERGEABLE",
            "ci_status": "passed",
        }
        verdict = {"has_sova_review": True, "verdict": "approve", "review_head_sha": None}
        item = _build_task_item(task, pr_data=pr, running=None, handoff=None, sova_verdict=verdict)
        assert item["state"] == "pr_ready_to_merge"
        assert item["pr_details"]["number"] == 100
        assert item["pr_number"] == 100

    def test_awaiting_approval_run_does_not_override_pr_state(self) -> None:
        # Regression: a stale awaiting_approval run must not set state=spec_review
        # when an open PR exists. PR state takes priority.
        task = {
            "issue": "42",
            "title": "Fix bug",
            "state": "in_review",
            "labels": [],
            "priority": -1,
            "last_run": {"status": "awaiting_approval", "id": 7},
        }
        pr = {"number": 99, "computed_state": "awaiting_review", "state": "OPEN"}
        item = _build_task_item(task, pr_data=pr, running=None, handoff=None)
        assert item["state"] != "spec_review", "PR state must win over stale awaiting_approval run"
        # No SOVA verdict supplied at all: falls to PR_SOVA_PENDING (resolve_next_action() rule 10).
        assert item["state"] == "pr_sova_pending"
        assert item["pr_details"]["number"] == 99

    def test_task_with_running_agent(self) -> None:
        task = {"issue": "42", "title": "Fix bug", "state": "in_progress", "labels": [], "priority": 1}
        running = {"run_id": 5, "role": "developer", "elapsed_seconds": 60}
        item = _build_task_item(task, pr_data=None, running=running, handoff=None)
        assert item["state"] == "agent_running"
        assert item["running_agent"]["role_label"] == "Developing"

    def test_task_with_handoff_uses_label_state(self) -> None:
        """Handoff presence no longer affects computed state; label state applies."""
        task = {"issue": "42", "title": "Fix bug", "state": "in_review", "labels": [], "priority": -1}
        handoff = {
            "status": "awaiting_action",
            "summary": "Review done",
            "next_actions": [{"id": "integrate", "label": "Integrate PR"}],
        }
        item = _build_task_item(task, pr_data=None, running=None, handoff=handoff)
        # State derived from task_state (in_review -> pr_awaiting_review), not from handoff
        assert item["state"] == "pr_awaiting_review"

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
        # No SOVA verdict supplied at all: falls to PR_SOVA_PENDING (resolve_next_action() rule 10).
        assert item["state"] == "pr_sova_pending"
        assert item["pr_details"]["number"] == 200

    def test_pr_with_linked_issue(self) -> None:
        pr = {
            "number": 200,
            "title": "Quick fix",
            "computed_state": "ci_failed",
            "state": "OPEN",
            "ci_status": "failed",
        }
        item = _build_pr_item(pr, running=None, handoff=None, issue_num="10")
        assert item["issue_number"] == "10"
        assert item["state"] == "pr_ci_failed"
        assert item["primary_action"]["id"] == "address_pr"

    def test_pr_with_handoff_uses_pr_state(self) -> None:
        """Handoff presence no longer affects computed state; PR state applies."""
        pr = {"number": 200, "title": "Quick fix", "computed_state": "awaiting_review", "state": "OPEN"}
        handoff = {
            "status": "awaiting_action",
            "summary": "Review complete",
            "next_actions": [{"id": "integrate", "label": "Integrate PR"}],
        }
        item = _build_pr_item(pr, running=None, handoff=handoff, issue_num=None)
        # State derived from PR facts, not from handoff; no verdict supplied → PR_SOVA_PENDING.
        assert item["state"] == "pr_sova_pending"

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
        assert _extract_handoff_summary(h, WorkItemState.SPEC_REVIEW) == "All good"

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
        assert _extract_handoff_summary(None, WorkItemState.SPEC_REVIEW) == ""


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
        prs = [
            {
                "number": 100,
                "linked_issue": 42,
                "computed_state": "approved",
                "state": "OPEN",
                "title": "Fix",
                "mergeable": "MERGEABLE",
                "ci_status": "passed",
            }
        ]
        mock_fetch.return_value = (queue, prs, [], {"agents": [], "completed": []})
        mock_verdicts.side_effect = None
        mock_verdicts.return_value = {
            "42": {"has_sova_review": True, "verdict": "approve", "finding_count": 0, "reviewed_at": None}
        }

        result = await get_work_items()

        # PR should be merged into the task item, not duplicated
        assert len(result["items"]) == 1
        assert result["items"][0]["pr_number"] == 100
        # PR_APPROVED is superseded by PR_READY_TO_MERGE as the sole integrate-bound state (#991).
        assert result["items"][0]["state"] == "pr_ready_to_merge"

    @pytest.mark.asyncio()
    async def test_standalone_pr_appears(self, _mock_sources) -> None:
        mock_fetch, *_ = _mock_sources
        prs = [
            {
                "number": 200,
                "linked_issue": None,
                "computed_state": "ci_running",
                "state": "OPEN",
                "title": "Quick",
                "ci_status": "pending",
            }
        ]
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
        """Handoff no longer affects state but still provides action buttons."""
        mock_fetch, *_ = _mock_sources
        queue = [{"issue": "42", "title": "Bug", "state": "in_review", "labels": [], "priority": -1}]
        handoffs = [
            {"issue": "42", "status": "awaiting_action", "summary": "Done", "next_actions": [{"id": "integrate"}]},
        ]
        mock_fetch.return_value = (queue, [], handoffs, {"agents": [], "completed": []})

        result = await get_work_items()

        # State derived from task_state (in_review), not from handoff
        assert result["items"][0]["state"] == "pr_awaiting_review"

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
        """PR linked to issue that's NOT in queue: appears as PR item with issue context."""
        mock_fetch, _, mock_verdicts = _mock_sources
        prs = [
            {
                "number": 300,
                "linked_issue": 99,
                "computed_state": "approved_ci_green",
                "state": "OPEN",
                "title": "Fix",
                "mergeable": "MERGEABLE",
                "ci_status": "passed",
            },
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
        prs = [
            {
                "number": 200,
                "linked_issue": None,
                "computed_state": "approved",
                "state": "OPEN",
                "mergeable": "MERGEABLE",
                "ci_status": "passed",
            }
        ]
        verdicts = {"pr:200": {"has_sova_review": True, "verdict": "approve", "finding_count": 0, "reviewed_at": None}}
        _append_standalone_pr_items(items, prs, set(), {}, {}, verdicts_by_issue=verdicts)
        assert len(items) == 1
        # PR_APPROVED is superseded by PR_READY_TO_MERGE as the sole integrate-bound state (#991).
        assert items[0]["state"] == "pr_ready_to_merge"

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

    @pytest.mark.asyncio
    async def test_passes_project_dir_through_to_check_integration_gates(self, monkeypatch) -> None:
        """project_dir must reach check_integration_gates for correct multi-project verdict lookup."""
        from pathlib import Path

        from sova.config.models import IntegrationGatesConfig, ProjectConfig

        cfg = ProjectConfig(
            github_repo="owner/repo",
            github_user="testuser",
            integration_gates=IntegrationGatesConfig(sova_reviewed=True),
        )
        item = {
            "issue_number": "42",
            "pr_details": {"number": 1, "ci_status": "passed"},
            "primary_action": {"id": "integrate", "label": "Integrate PR"},
            "secondary_actions": [],
        }
        expected_project_dir = Path("/tmp/some-project")
        captured: dict = {}

        async def _fake_check(**kwargs):
            captured.update(kwargs)
            return {"passed": True, "gates": []}

        monkeypatch.setattr(
            "sova.dashboard.services.pr_service.check_integration_gates",
            _fake_check,
        )
        await _attach_integration_gates([item], {}, cfg, expected_project_dir)
        assert captured["project_dir"] == expected_project_dir

    @pytest.mark.asyncio
    async def test_forwards_resolved_verdict_to_check_integration_gates(self, monkeypatch) -> None:
        """The gate must be handed the same verdict that produced the Integrate action."""
        from sova.config.models import IntegrationGatesConfig, ProjectConfig

        cfg = ProjectConfig(
            github_repo="owner/repo",
            github_user="testuser",
            integration_gates=IntegrationGatesConfig(sova_reviewed=True),
        )
        item = {
            "issue_number": "42",
            "pr_number": 7,
            "pr_details": {"number": 7, "ci_status": "passed"},
            "primary_action": {"id": "integrate", "label": "Integrate PR"},
            "secondary_actions": [],
        }
        verdict = {"has_sova_review": True, "verdict": "approve", "finding_count": 0}
        captured: dict = {}

        async def _fake_check(**kwargs):
            captured.update(kwargs)
            return {"passed": True, "gates": []}

        monkeypatch.setattr(
            "sova.dashboard.services.pr_service.check_integration_gates",
            _fake_check,
        )
        await _attach_integration_gates([item], {}, cfg, None, {"42": verdict})
        assert captured["sova_verdict"] == verdict

    @pytest.mark.asyncio
    async def test_forwards_unlinked_pr_verdict_by_pr_key(self, monkeypatch) -> None:
        """A standalone PR item resolves its verdict under the "pr:{number}" key."""
        from sova.config.models import IntegrationGatesConfig, ProjectConfig

        cfg = ProjectConfig(
            github_repo="owner/repo",
            github_user="testuser",
            integration_gates=IntegrationGatesConfig(sova_reviewed=True),
        )
        item = {
            "issue_number": None,
            "pr_number": 7,
            "pr_details": {"number": 7, "ci_status": "passed"},
            "primary_action": {"id": "integrate", "label": "Integrate PR"},
            "secondary_actions": [],
        }
        verdict = {"has_sova_review": True, "verdict": "approve", "finding_count": 0}
        captured: dict = {}

        async def _fake_check(**kwargs):
            captured.update(kwargs)
            return {"passed": True, "gates": []}

        monkeypatch.setattr(
            "sova.dashboard.services.pr_service.check_integration_gates",
            _fake_check,
        )
        await _attach_integration_gates([item], {}, cfg, None, {"pr:7": verdict})
        assert captured["sova_verdict"] == verdict


class TestVerdictCacheProjectScoping:
    """The verdict cache must not let one project answer for another's PR number."""

    @pytest.mark.asyncio()
    async def test_same_pr_number_in_two_projects_does_not_share_verdict(self) -> None:
        from pathlib import Path as _Path

        from sova.dashboard.services.work_verdict import resolve_sova_verdict

        clear_verdict_cache()
        verdicts = {
            "/tmp/project-a": {"has_sova_review": True, "verdict": "approve", "finding_count": 0},
            "/tmp/project-b": {"has_sova_review": True, "verdict": "revise", "finding_count": 2},
        }

        async def fake_db_verdict(issue_number, *, pr_number=None, project_dir=None):
            return dict(verdicts[str(project_dir)])

        with patch(
            "sova.dashboard.services.agent_recovery.get_sova_review_verdict",
            new=fake_db_verdict,
        ):
            a = await resolve_sova_verdict("10", pr_number=42, project_dir=_Path("/tmp/project-a"))
            b = await resolve_sova_verdict("10", pr_number=42, project_dir=_Path("/tmp/project-b"))

        assert a["verdict"] == "approve"
        assert b["verdict"] == "revise"


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


class TestExtractSovaVerdictFromLabels:
    """Tests for _extract_sova_verdict_from_labels."""

    def test_approved_label(self) -> None:
        result = _extract_sova_verdict_from_labels(["agent:in-review", "sova:approved"])
        assert result is not None
        assert result["has_sova_review"] is True
        assert result["verdict"] == "approve"
        assert result["review_head_sha"] is None

    def test_revise_label(self) -> None:
        result = _extract_sova_verdict_from_labels(["sova:revise", "type:feature"])
        assert result is not None
        assert result["verdict"] == "revise"
        assert result["review_head_sha"] is None

    def test_block_label(self) -> None:
        result = _extract_sova_verdict_from_labels(["sova:block"])
        assert result is not None
        assert result["verdict"] == "block"
        assert result["review_head_sha"] is None

    def test_no_verdict_label(self) -> None:
        result = _extract_sova_verdict_from_labels(["agent:in-review", "type:feature"])
        assert result is None

    def test_empty_labels(self) -> None:
        result = _extract_sova_verdict_from_labels([])
        assert result is None

    def test_unknown_sova_label_ignored(self) -> None:
        result = _extract_sova_verdict_from_labels(["sova:unknown"])
        assert result is None

    def test_multiple_sova_labels_takes_first(self) -> None:
        result = _extract_sova_verdict_from_labels(["sova:approved", "sova:revise"])
        assert result is not None
        assert result["verdict"] == "approve"

    def test_label_verdict_never_stale_unanchored(self) -> None:
        """Label-derived verdicts carry no SHA, so they are never reported stale: the verdict
        is still applied even though the PR head has since advanced."""
        verdict = _extract_sova_verdict_from_labels(["sova:block"])
        assert verdict is not None
        assert verdict["review_head_sha"] is None
        assert (
            _state(
                pr_data={"computed_state": "approved_ci_green", "state": "OPEN", "head_sha": "abc123"},
                sova_verdict=verdict,
            )
            == WorkItemState.PR_SOVA_CHANGES
        )

    def test_label_verdict_not_stale_without_pr_head_sha(self) -> None:
        verdict = _extract_sova_verdict_from_labels(["sova:revise"])
        assert verdict is not None
        assert (
            _state(
                pr_data={"computed_state": "approved_ci_green", "state": "OPEN"},
                sova_verdict=verdict,
            )
            == WorkItemState.PR_SOVA_CHANGES
        )

    def test_label_verdict_overrides_state_despite_pr_advancing(self) -> None:
        """A label-derived block verdict is unanchored, so it still downgrades the state
        even though the PR head has moved on since the verdict was recorded."""
        label_verdict = _extract_sova_verdict_from_labels(["sova:block"])
        assert (
            _state(
                pr_data={
                    "computed_state": "approved_ci_green",
                    "state": "OPEN",
                    "head_sha": "def456",
                },
                sova_verdict=label_verdict,
            )
            == WorkItemState.PR_SOVA_CHANGES
        )


class TestFetchAllSourcesProjectDir:
    """_fetch_all_sources() must thread project_dir into every per-project data source."""

    @pytest.mark.asyncio()
    async def test_safe_prs_passes_project_dir(self, tmp_path) -> None:
        """Regression: safe_prs() used to call list_open_prs_with_state() with no
        project_dir, so it fell back to the request context or Path.cwd() instead of
        the explicit project_dir already in scope, letting a multi-project poll mix
        another project's PRs into this one's verdicts and integration gates."""
        from sova.dashboard.services.work_item_service import _fetch_all_sources

        with (
            patch(
                "sova.dashboard.services.queue_service.get_priority_queue",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch(
                "sova.dashboard.services.pr_service.list_open_prs_with_state",
                new_callable=AsyncMock,
                return_value=[],
            ) as mock_prs,
            patch(
                "sova.dashboard.services.agent_lifecycle.get_unified_agents",
                new_callable=AsyncMock,
                return_value={"agents": [], "completed": []},
            ),
            patch(
                "sova.dashboard.services.handoff_service.get_all_handoffs",
                return_value=[],
            ),
        ):
            await _fetch_all_sources(project_dir=tmp_path, slug=None)

        mock_prs.assert_awaited_once_with(tmp_path)


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
                "sova.dashboard.services.work_verdict._fetch_github_review_fallback",
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

    @pytest.mark.asyncio()
    async def test_label_used_when_db_has_no_review(self) -> None:
        """A sova:* label supplies the verdict when this machine's DB knows nothing."""
        from sova.dashboard.services.work_item_service import _fetch_sova_verdicts

        clear_verdict_cache()
        labels_by_issue = {"42": ["agent:in-review", "sova:approved"]}

        with (
            patch(
                "sova.dashboard.services.agent_recovery.get_sova_review_verdict",
                new_callable=AsyncMock,
                return_value={"has_sova_review": False, "verdict": None},
            ),
            patch(
                "sova.dashboard.services.work_verdict._fetch_github_review_fallback",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            result = await _fetch_sova_verdicts(
                {"42": {"number": 100}},
                labels_by_issue=labels_by_issue,
            )

        assert result["42"]["has_sova_review"] is True
        assert result["42"]["verdict"] == "approve"
        assert result["42"]["review_head_sha"] is None

    @pytest.mark.asyncio()
    async def test_labels_absent_falls_through_to_db(self) -> None:
        """When no sova:* label exists, the DB lookup path runs."""
        from sova.dashboard.services.work_item_service import _fetch_sova_verdicts

        clear_verdict_cache()
        labels_by_issue = {"42": ["agent:in-review"]}
        mock_db_verdict = {"has_sova_review": False, "verdict": None, "finding_count": 0, "reviewed_at": None}

        with (
            patch(
                "sova.dashboard.services.agent_recovery.get_sova_review_verdict",
                new_callable=AsyncMock,
                return_value=mock_db_verdict,
            ) as mock_call,
            patch(
                "sova.dashboard.services.work_verdict._fetch_github_review_fallback",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            result = await _fetch_sova_verdicts(
                {"42": {"number": 100}},
                labels_by_issue=labels_by_issue,
            )

        assert result["42"]["has_sova_review"] is False
        mock_call.assert_called_once()

    @pytest.mark.asyncio()
    async def test_labels_cache_overflow_clears(self) -> None:
        """When the verdict cache exceeds 1000 entries, it is cleared before adding."""
        from sova.dashboard.services.work_item_service import (
            _fetch_sova_verdicts,
            _sova_verdict_cache,
        )

        clear_verdict_cache()
        # Seed the cache with >1000 entries to trigger the overflow path.
        import time

        for i in range(1001):
            _sova_verdict_cache[("", i)] = (time.monotonic(), {"has_sova_review": False})

        labels_by_issue = {"42": ["sova:revise"]}

        with patch(
            "sova.dashboard.services.agent_recovery.get_sova_review_verdict",
            new_callable=AsyncMock,
            return_value={"has_sova_review": False, "verdict": None},
        ):
            result = await _fetch_sova_verdicts(
                {"42": {"number": 9999}},
                labels_by_issue=labels_by_issue,
            )

        assert result["42"]["verdict"] == "revise"
        # Cache was cleared and only the new entry remains.
        assert len(_sova_verdict_cache) == 1
        assert ("", 9999) in _sova_verdict_cache

    @pytest.mark.asyncio()
    async def test_labels_no_pr_number_skips_cache(self) -> None:
        """When pr_number is None, the label verdict is returned without caching."""
        from sova.dashboard.services.work_item_service import (
            _fetch_sova_verdicts,
            _sova_verdict_cache,
        )

        clear_verdict_cache()
        labels_by_issue = {"42": ["sova:approved"]}

        with patch(
            "sova.dashboard.services.agent_recovery.get_sova_review_verdict",
            new_callable=AsyncMock,
            return_value={"has_sova_review": False, "verdict": None},
        ):
            result = await _fetch_sova_verdicts(
                {"42": {"number": None}},
                labels_by_issue=labels_by_issue,
            )

        assert result["42"]["verdict"] == "approve"
        assert len(_sova_verdict_cache) == 0


class TestMergeLabelVerdict:
    """_merge_label_verdict reconciles the local DB record with the sova:* label."""

    def test_no_label_keeps_db_verdict(self) -> None:
        from sova.dashboard.services.work_verdict import _merge_label_verdict

        db = {"has_sova_review": True, "verdict": "approve", "review_head_sha": "abc"}
        assert _merge_label_verdict(db, None) is db

    def test_label_used_when_db_empty(self) -> None:
        from sova.dashboard.services.work_verdict import _merge_label_verdict

        db = {"has_sova_review": False, "verdict": None}
        label = _extract_sova_verdict_from_labels(["sova:revise"])
        assert _merge_label_verdict(db, label)["verdict"] == "revise"

    def test_addressed_supersedes_stale_label(self) -> None:
        """The address cycle never clears the reviewer's label, so the DB must win here."""
        from sova.dashboard.services.work_verdict import _merge_label_verdict

        db = {"has_sova_review": True, "verdict": "addressed", "review_head_sha": None}
        label = _extract_sova_verdict_from_labels(["sova:revise"])
        assert _merge_label_verdict(db, label)["verdict"] == "addressed"

    def test_agreeing_db_wins_to_keep_the_commit_anchor(self) -> None:
        from sova.dashboard.services.work_verdict import _merge_label_verdict

        db = {"has_sova_review": True, "verdict": "revise", "review_head_sha": "abc123"}
        label = _extract_sova_verdict_from_labels(["sova:revise"])
        assert _merge_label_verdict(db, label)["review_head_sha"] == "abc123"

    def test_disagreeing_label_wins_as_cross_machine_source(self) -> None:
        from sova.dashboard.services.work_verdict import _merge_label_verdict

        db = {"has_sova_review": True, "verdict": "approve", "review_head_sha": "abc123"}
        label = _extract_sova_verdict_from_labels(["sova:block"])
        merged = _merge_label_verdict(db, label)
        assert merged["verdict"] == "block"
        assert merged["review_head_sha"] is None


class TestCanonicalVerdictPath:
    """resolve_sova_verdict() is the one assembly path the dashboard and supervisor share."""

    @pytest.mark.asyncio()
    async def test_addressed_db_verdict_beats_label_for_both_callers(self) -> None:
        """Regression: a stale sova:revise label used to route the dashboard to
        address_review while the supervisor saw "addressed" and routed to review_pr."""
        from sova.dashboard.services.work_state import _build_pr_facts
        from sova.dashboard.services.work_verdict import resolve_sova_verdict

        clear_verdict_cache()
        db_verdict = {
            "has_sova_review": True,
            "verdict": "addressed",
            "finding_count": 0,
            "reviewed_at": None,
            "run_status": "done",
            "review_head_sha": None,
        }
        with patch(
            "sova.dashboard.services.agent_recovery.get_sova_review_verdict",
            new_callable=AsyncMock,
            return_value=db_verdict,
        ):
            verdict = await resolve_sova_verdict("42", pr_number=100, issue_labels=["sova:revise"])

        assert verdict["verdict"] == "addressed"
        facts = _build_pr_facts(
            {"state": "OPEN", "ci_status": "passed", "head_sha": "abc", "mergeable": "MERGEABLE"},
            verdict,
            external_reviews_enabled=False,
        )
        assert resolve_next_action(facts).action_id == "review_pr"

    @pytest.mark.asyncio()
    async def test_db_lookup_failure_degrades_to_label(self) -> None:
        from sova.dashboard.services.work_verdict import resolve_sova_verdict

        clear_verdict_cache()
        with patch(
            "sova.dashboard.services.agent_recovery.get_sova_review_verdict",
            new_callable=AsyncMock,
            side_effect=RuntimeError("db down"),
        ):
            verdict = await resolve_sova_verdict("42", pr_number=101, issue_labels=["sova:block"])

        assert verdict["verdict"] == "block"

    @pytest.mark.asyncio()
    async def test_db_lookup_failure_without_label_yields_no_review(self) -> None:
        from sova.dashboard.services.work_verdict import resolve_sova_verdict

        clear_verdict_cache()
        with (
            patch(
                "sova.dashboard.services.agent_recovery.get_sova_review_verdict",
                new_callable=AsyncMock,
                side_effect=RuntimeError("db down"),
            ),
            patch(
                "sova.dashboard.services.work_verdict._fetch_github_review_fallback",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            verdict = await resolve_sova_verdict("42", pr_number=102, issue_labels=[])

        assert verdict["has_sova_review"] is False

    @pytest.mark.asyncio()
    async def test_cached_result_is_reused_by_a_second_caller(self) -> None:
        """The dashboard and supervisor share one cache, so a PR cannot flip
        verdicts between the two within a poll cycle."""
        from sova.dashboard.services.work_verdict import resolve_sova_verdict

        clear_verdict_cache()
        db_verdict = {"has_sova_review": True, "verdict": "approve", "review_head_sha": "abc"}
        with patch(
            "sova.dashboard.services.agent_recovery.get_sova_review_verdict",
            new_callable=AsyncMock,
            return_value=db_verdict,
        ) as mock_db:
            first = await resolve_sova_verdict("42", pr_number=103, issue_labels=[])
            second = await resolve_sova_verdict("42", pr_number=103, issue_labels=[])

        assert first == second
        assert mock_db.call_count == 1

    @pytest.mark.asyncio()
    async def test_cache_hit_still_reconciles_against_current_labels(self) -> None:
        """A cache hit must not bypass label reconciliation: caching the merged verdict
        from the first call must not let a newer sova:revise/sova:block label be ignored
        for the remainder of the positive TTL."""
        from sova.dashboard.services.work_verdict import resolve_sova_verdict

        clear_verdict_cache()
        db_verdict = {"has_sova_review": True, "verdict": "approve", "review_head_sha": "abc"}
        with patch(
            "sova.dashboard.services.agent_recovery.get_sova_review_verdict",
            new_callable=AsyncMock,
            return_value=db_verdict,
        ) as mock_db:
            first = await resolve_sova_verdict("42", pr_number=104, issue_labels=["sova:approved"])
            second = await resolve_sova_verdict("42", pr_number=104, issue_labels=["sova:revise"])

        assert first["verdict"] == "approve"
        assert second["verdict"] == "revise"
        # The DB is not re-queried on the cache-hit path: reconciliation uses the
        # already-cached db_verdict merged against the freshly supplied label.
        assert mock_db.call_count == 1

    @pytest.mark.asyncio()
    async def test_label_only_approve_does_not_survive_a_later_empty_label_lookup(self) -> None:
        """A verdict resolved purely from a sova:* label (no local DB record, the
        cross-instance case) must not freeze into the cache as a standing "approve".
        If a later call's label lookup comes back empty, whether the label was
        actually removed or the lookup itself failed transiently, the result must
        fall back to the unmerged source verdict rather than keep serving the
        stale label-derived approval for the rest of the positive TTL."""
        from sova.dashboard.services.work_verdict import _NO_REVIEW, resolve_sova_verdict

        clear_verdict_cache()
        with (
            patch(
                "sova.dashboard.services.agent_recovery.get_sova_review_verdict",
                new_callable=AsyncMock,
                return_value=dict(_NO_REVIEW),
            ),
            patch(
                "sova.dashboard.services.work_verdict._fetch_github_review_fallback",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            first = await resolve_sova_verdict("42", pr_number=105, issue_labels=["sova:approved"])
            second = await resolve_sova_verdict("42", pr_number=105, issue_labels=[])

        assert first["has_sova_review"] is True
        assert first["verdict"] == "approve"
        assert second["has_sova_review"] is False


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

    def test_detects_marker_with_sha(self) -> None:
        from sova.dashboard.services.work_item_service import _parse_sova_review_from_github

        review = self._review("<!-- sova-review: revise sha=abc1234 -->\n\n## Review: REVISE")
        result = _parse_sova_review_from_github([review])
        assert result is not None
        assert result["verdict"] == "revise"
        assert result["review_head_sha"] == "abc1234"

    def test_marker_without_sha_is_unanchored(self) -> None:
        from sova.dashboard.services.work_item_service import _parse_sova_review_from_github

        review = self._review("<!-- sova-review: approve -->")
        result = _parse_sova_review_from_github([review])
        assert result is not None
        assert result["review_head_sha"] is None

    def test_heuristic_fallback_is_unanchored(self) -> None:
        from sova.dashboard.services.work_item_service import _parse_sova_review_from_github

        body = "## PR Summary\nX.\n\n## Verdict\n\n**Approve.** Clean.\n"
        review = self._review(body)
        result = _parse_sova_review_from_github([review])
        assert result is not None
        assert result["review_head_sha"] is None

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

        # Only ## Verdict, no ## PR Summary (not a SOVA review)
        body = "## Verdict\n\n**Approve.** LGTM."
        review = self._review(body)
        result = _parse_sova_review_from_github([review])
        assert result is None

    def test_heuristic_requires_pr_summary_section(self) -> None:
        from sova.dashboard.services.work_item_service import _parse_sova_review_from_github

        # Only ## PR Summary, no ## Verdict (not a SOVA review)
        body = "## PR Summary\nThis PR does X."
        review = self._review(body)
        result = _parse_sova_review_from_github([review])
        assert result is None

    def test_dismissed_review_skipped_even_when_next_has_no_sova_marker(self) -> None:
        from sova.dashboard.services.work_item_service import _parse_sova_review_from_github

        # The dismissed review has the marker; the non-dismissed one is a plain human review.
        # Expected: None. The dismissed review is skipped and the human review is not SOVA.
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

    def setup_method(self) -> None:
        clear_verdict_cache()

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
                "sova.dashboard.services.work_verdict._fetch_github_review_fallback",
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
                "sova.dashboard.services.work_verdict._fetch_github_review_fallback",
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
                "sova.dashboard.services.work_verdict._fetch_github_review_fallback",
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
                "sova.dashboard.services.work_verdict._fetch_github_review_fallback",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            result = await _fetch_sova_verdicts({"42": {"number": 100}})

        assert result["42"]["has_sova_review"] is False

    @pytest.mark.asyncio()
    async def test_cache_suppresses_second_github_call(self) -> None:
        """After a verdict is cached, the next call returns from cache without API calls."""
        from sova.dashboard.services.work_item_service import _fetch_sova_verdicts

        gh_verdict = {"has_sova_review": True, "verdict": "approve", "finding_count": 0, "reviewed_at": None}
        no_review = {"has_sova_review": False, "verdict": None, "finding_count": 0, "reviewed_at": None}
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
                "sova.dashboard.services.work_verdict._fetch_github_review_fallback",
                new_callable=AsyncMock,
                return_value=gh_verdict,
            ) as mock_fallback,
        ):
            # First call: cache miss, hits GitHub.
            result1 = await _fetch_sova_verdicts({"42": {"number": 100}})
            assert mock_fallback.call_count == 1

            # Second call: cache hit, no additional GitHub API call.
            result2 = await _fetch_sova_verdicts({"42": {"number": 100}})
            assert mock_fallback.call_count == 1

        assert result1["42"]["has_sova_review"] is True
        assert result2["42"]["verdict"] == "approve"


class TestFetchGithubReviewFallback:
    """Direct tests for _fetch_github_review_fallback."""

    def setup_method(self) -> None:
        clear_verdict_cache()

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
                "sova.dashboard.services.work_verdict._fetch_github_review_fallback",
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

    def setup_method(self) -> None:
        clear_verdict_cache()

    @pytest.mark.asyncio()
    async def test_exception_in_get_sova_review_verdict_returns_no_review(self) -> None:
        from sova.dashboard.services.work_item_service import _fetch_sova_verdicts

        # The GitHub fallback is patched out: without it this test built a real
        # adapter from the repo's own config and fetched PR #100's live reviews,
        # which carry a genuine (heuristic-style) SOVA review, so the DB-failure
        # path under test was masked by network state.
        with (
            patch(
                "sova.dashboard.services.agent_recovery.get_sova_review_verdict",
                new_callable=AsyncMock,
                side_effect=RuntimeError("DB error"),
            ),
            patch(
                "sova.dashboard.services.work_verdict._fetch_github_review_fallback",
                new_callable=AsyncMock,
                return_value=None,
            ),
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


class TestApiHealth:
    """Tests for the api_health field in get_work_items response."""

    @pytest.mark.asyncio
    async def test_api_health_rate_limited(self, monkeypatch, tmp_path) -> None:
        """When GitHub quota tracker reports limited, api_health reflects it."""
        from sova.supervisor.github_quota import GitHubQuotaStatus

        monkeypatch.setattr(
            "sova.dashboard.services.work_item_service._fetch_all_sources",
            AsyncMock(return_value=([], [], [], {"agents": [], "completed": []})),
        )
        monkeypatch.setattr(
            "sova.dashboard.services.agent_pool._get_project_agents",
            MagicMock(return_value=MagicMock(max_concurrent=3)),
        )
        monkeypatch.setattr(
            "sova.supervisor.github_quota.get_github_quota_status",
            lambda _user: GitHubQuotaStatus(
                is_limited=True, last_hit_at=100.0, hits_in_window=5, cooldown_remaining_seconds=120.0
            ),
        )

        result = await get_work_items(project_dir=tmp_path)
        assert result["api_health"]["status"] == "rate_limited"
        assert result["api_health"]["cooldown_seconds"] == 120
        assert result["api_health"]["hits"] == 5

    @pytest.mark.asyncio
    async def test_api_health_ok_when_not_limited(self, monkeypatch, tmp_path) -> None:
        """When GitHub quota tracker reports OK, api_health is ok."""
        from sova.supervisor.github_quota import GitHubQuotaStatus

        monkeypatch.setattr(
            "sova.dashboard.services.work_item_service._fetch_all_sources",
            AsyncMock(return_value=([], [], [], {"agents": [], "completed": []})),
        )
        monkeypatch.setattr(
            "sova.dashboard.services.agent_pool._get_project_agents",
            MagicMock(return_value=MagicMock(max_concurrent=3)),
        )
        monkeypatch.setattr(
            "sova.supervisor.github_quota.get_github_quota_status",
            lambda _user: GitHubQuotaStatus(
                is_limited=False, last_hit_at=None, hits_in_window=0, cooldown_remaining_seconds=0.0
            ),
        )

        result = await get_work_items(project_dir=tmp_path)
        assert result["api_health"]["status"] == "ok"

    @pytest.mark.asyncio
    async def test_api_health_exception_contained(self, monkeypatch, tmp_path) -> None:
        """If quota status throws, api_health falls back to ok."""
        monkeypatch.setattr(
            "sova.dashboard.services.work_item_service._fetch_all_sources",
            AsyncMock(return_value=([], [], [], {"agents": [], "completed": []})),
        )
        monkeypatch.setattr(
            "sova.dashboard.services.agent_pool._get_project_agents",
            MagicMock(return_value=MagicMock(max_concurrent=3)),
        )

        def _raise_on_import(*args, **kwargs):
            raise ImportError("no module")

        monkeypatch.setattr(
            "sova.supervisor.github_quota.get_github_quota_status",
            _raise_on_import,
        )

        result = await get_work_items(project_dir=tmp_path)
        assert result["api_health"]["status"] == "ok"


class TestJiraDisplayName:
    """Tests for jira_display_name config field and API exposure."""

    def test_task_source_config_accepts_jira_display_name(self) -> None:
        from sova.config.models import TaskSourceConfig

        cfg = TaskSourceConfig(jira_display_name="Damian Sova")
        assert cfg.jira_display_name == "Damian Sova"

    def test_task_source_config_defaults_empty(self) -> None:
        from sova.config.models import TaskSourceConfig

        cfg = TaskSourceConfig()
        assert cfg.jira_display_name == ""

    def test_settings_meta_registered(self) -> None:
        from sova.dashboard.settings_meta import get_meta

        meta = get_meta("task_source.jira_display_name")
        assert meta is not None
        assert meta.group == "task_source"
        assert "JIRA display name" in meta.label

    @pytest.mark.asyncio
    async def test_get_work_items_returns_jira_display_name(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setattr(
            "sova.dashboard.services.work_item_service._fetch_all_sources",
            AsyncMock(return_value=([], [], [], {"agents": [], "completed": []})),
        )
        monkeypatch.setattr(
            "sova.dashboard.services.agent_pool._get_project_agents",
            MagicMock(return_value=MagicMock(max_concurrent=3)),
        )
        mock_cfg = MagicMock()
        mock_cfg.external_reviews.enabled = False
        mock_cfg.github_user = "dsova06"
        mock_cfg.task_source.jira_display_name = "Damian Sova"
        mock_cfg.integration_gates.ci_passed = False
        mock_cfg.integration_gates.sova_reviewed = False
        mock_cfg.integration_gates.coderabbit_reviewed = False
        mock_cfg.integration_gates.threads_resolved = False
        monkeypatch.setattr("sova.config.loader.load_config", lambda _path: mock_cfg)

        result = await get_work_items(project_dir=tmp_path)
        assert result["jira_display_name"] == "Damian Sova"
        assert result["github_user"] == "dsova06"

    @pytest.mark.asyncio
    async def test_get_work_items_empty_when_config_fails(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setattr(
            "sova.dashboard.services.work_item_service._fetch_all_sources",
            AsyncMock(return_value=([], [], [], {"agents": [], "completed": []})),
        )
        monkeypatch.setattr(
            "sova.dashboard.services.agent_pool._get_project_agents",
            MagicMock(return_value=MagicMock(max_concurrent=3)),
        )
        monkeypatch.setattr(
            "sova.config.loader.load_config",
            MagicMock(side_effect=RuntimeError("config broken")),
        )

        result = await get_work_items(project_dir=tmp_path)
        assert result["jira_display_name"] == ""


class TestParseSovaReviewAddressedMarker:
    """A sova-addressed review newer than the verdict marks the verdict addressed (#1063)."""

    def _review(self, body: str, submitted_at: str, state: str = "COMMENTED") -> object:
        from sova.adapters.base import PRReview

        return PRReview(reviewer="xsovad06", state=state, body=body, submitted_at=submitted_at, is_bot=False)

    def test_addressed_marker_newer_than_verdict_yields_addressed(self) -> None:
        from sova.dashboard.services.work_item_service import _parse_sova_review_from_github

        reviews = [
            self._review("<!-- sova-review: revise -->\n## Review: REVISE", "2026-09-17T21:12:22Z"),
            self._review("<!-- sova-addressed: sha=892372e -->\n## Address Review: Round 1", "2026-09-17T22:16:00Z"),
        ]
        result = _parse_sova_review_from_github(reviews)
        assert result is not None
        assert result["has_sova_review"] is True
        assert result["verdict"] == "addressed"
        assert result["reviewed_at"] == "2026-09-17T21:12:22Z"
        assert result["review_head_sha"] is None

    def test_addressed_marker_older_than_verdict_is_ignored(self) -> None:
        from sova.dashboard.services.work_item_service import _parse_sova_review_from_github

        reviews = [
            self._review("<!-- sova-addressed: sha=abcdef1 -->", "2026-09-17T20:00:00Z"),
            self._review("<!-- sova-review: revise sha=892372e -->", "2026-09-17T21:12:22Z"),
        ]
        result = _parse_sova_review_from_github(reviews)
        assert result is not None
        assert result["verdict"] == "revise"
        assert result["review_head_sha"] == "892372e"

    def test_addressed_marker_alone_is_not_a_review(self) -> None:
        from sova.dashboard.services.work_item_service import _parse_sova_review_from_github

        assert _parse_sova_review_from_github([self._review("<!-- sova-addressed -->", "2026-09-17T22:00:00Z")]) is None

    def test_addressed_applies_to_heuristic_reviews_too(self) -> None:
        from sova.dashboard.services.work_item_service import _parse_sova_review_from_github

        old_style = "## PR Summary\n...\n## Verdict\n**Request changes**: fix it"
        reviews = [
            self._review(old_style, "2026-09-17T21:00:00Z"),
            self._review("<!-- sova-addressed -->", "2026-09-17T22:00:00Z"),
        ]
        result = _parse_sova_review_from_github(reviews)
        assert result is not None
        assert result["verdict"] == "addressed"


class TestResolveSovaVerdictLocalAddressCycle:
    """A GitHub-sourced verdict is superseded by an address cycle recorded in the local DB."""

    def setup_method(self) -> None:
        clear_verdict_cache()

    @pytest.mark.asyncio
    async def test_github_verdict_superseded_by_local_address_cycle(self) -> None:
        from sova.dashboard.services.work_verdict import resolve_sova_verdict

        no_review = {"has_sova_review": False, "verdict": None, "finding_count": 0, "reviewed_at": None}
        gh_verdict = {
            "has_sova_review": True,
            "verdict": "revise",
            "finding_count": 0,
            "reviewed_at": "2026-09-17T21:12:22Z",
            "review_head_sha": None,
        }
        with (
            patch(
                "sova.dashboard.services.agent_recovery.get_sova_review_verdict",
                new_callable=AsyncMock,
                return_value=no_review,
            ),
            patch(
                "sova.dashboard.services.work_verdict._fetch_github_review_fallback",
                new_callable=AsyncMock,
                return_value=gh_verdict,
            ),
            patch(
                "sova.dashboard.services.agent_recovery.has_address_cycle_since",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_since,
        ):
            result = await resolve_sova_verdict("1065", pr_number=1063, fallback_adapter=MagicMock())

        assert result["verdict"] == "addressed"
        assert result["has_sova_review"] is True
        assert result["review_head_sha"] is None
        since = mock_since.call_args.args[0]
        assert since.isoformat() == "2026-09-17T21:12:22+00:00"
        assert mock_since.call_args.kwargs["pr_number"] == 1063

    @pytest.mark.asyncio
    async def test_github_verdict_stands_without_local_address_cycle(self) -> None:
        from sova.dashboard.services.work_verdict import resolve_sova_verdict

        no_review = {"has_sova_review": False, "verdict": None, "finding_count": 0, "reviewed_at": None}
        gh_verdict = {
            "has_sova_review": True,
            "verdict": "revise",
            "finding_count": 0,
            "reviewed_at": "2026-09-17T21:12:22Z",
            "review_head_sha": "cf02928",
        }
        with (
            patch(
                "sova.dashboard.services.agent_recovery.get_sova_review_verdict",
                new_callable=AsyncMock,
                return_value=no_review,
            ),
            patch(
                "sova.dashboard.services.work_verdict._fetch_github_review_fallback",
                new_callable=AsyncMock,
                return_value=gh_verdict,
            ),
            patch(
                "sova.dashboard.services.agent_recovery.has_address_cycle_since",
                new_callable=AsyncMock,
                return_value=False,
            ),
        ):
            result = await resolve_sova_verdict("1065", pr_number=1063, fallback_adapter=MagicMock())

        assert result["verdict"] == "revise"
        assert result["review_head_sha"] == "cf02928"

    @pytest.mark.asyncio
    async def test_github_verdict_without_timestamp_skips_db_check(self) -> None:
        from sova.dashboard.services.work_verdict import resolve_sova_verdict

        no_review = {"has_sova_review": False, "verdict": None, "finding_count": 0, "reviewed_at": None}
        gh_verdict = {"has_sova_review": True, "verdict": "revise", "finding_count": 0, "reviewed_at": None}
        with (
            patch(
                "sova.dashboard.services.agent_recovery.get_sova_review_verdict",
                new_callable=AsyncMock,
                return_value=no_review,
            ),
            patch(
                "sova.dashboard.services.work_verdict._fetch_github_review_fallback",
                new_callable=AsyncMock,
                return_value=gh_verdict,
            ),
            patch(
                "sova.dashboard.services.agent_recovery.has_address_cycle_since",
                new_callable=AsyncMock,
            ) as mock_since,
        ):
            result = await resolve_sova_verdict("1065", pr_number=1063, fallback_adapter=MagicMock())

        mock_since.assert_not_called()
        assert result["verdict"] == "revise"
