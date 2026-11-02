"""Tests for sova.adapters.milestone: the shared milestone phase parser."""

from __future__ import annotations

import pytest

from sova.adapters.milestone import UNPHASED, extract_phase_order


class TestExtractPhaseOrder:
    @pytest.mark.parametrize(
        ("milestone", "expected"),
        [
            ("Phase 1: Ship It", 1),
            ("Phase 7", 7),
            ("P3: v0.1", 3),
            ("  Phase 3: v0.1", 3),
            ("phase 2", 2),
            ("Phase0", 0),
        ],
    )
    def test_parses_phase_number(self, milestone: str, expected: int) -> None:
        assert extract_phase_order(milestone) == expected

    @pytest.mark.parametrize("milestone", ["", None, "Beta Release", "   "])
    def test_missing_or_phaseless_milestone_is_unphased(self, milestone: str | None) -> None:
        assert extract_phase_order(milestone) == UNPHASED

    def test_phase_ordering(self) -> None:
        assert extract_phase_order("Phase 1: Ship It") < extract_phase_order("Phase 4: Bank")
        assert extract_phase_order("Phase 4: Bank") < extract_phase_order("Phase 7: Scale")

    def test_phase_zero_sorts_before_phase_one(self) -> None:
        assert extract_phase_order("Phase 0: Bootstrap") < extract_phase_order("Phase 1: Ship It")

    def test_phase_zero_is_not_unphased(self) -> None:
        assert extract_phase_order("Phase 0") == 0
        assert extract_phase_order("Phase 0") != UNPHASED

    def test_every_real_phase_sorts_before_unphased(self) -> None:
        assert extract_phase_order("Phase 12") < UNPHASED
