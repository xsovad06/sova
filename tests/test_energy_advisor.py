"""Tests for sova.monitoring.energy and sova.monitoring.advisor."""

from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from sova.monitoring.advisor import CapacityRecommendation, recommend_capacity
from sova.monitoring.energy import EnergyEstimate, _detect_chip_name, detect_chip_tdp, estimate_energy

# ---------------------------------------------------------------------------
# Energy estimation tests
# ---------------------------------------------------------------------------


class TestDetectChipTdp:
    def setup_method(self) -> None:
        detect_chip_tdp.cache_clear()

    def teardown_method(self) -> None:
        detect_chip_tdp.cache_clear()

    @patch("sova.monitoring.energy._detect_chip_name", return_value="Apple M2 Pro")
    def test_known_chip(self, _mock: object) -> None:
        name, tdp = detect_chip_tdp()
        assert name == "Apple M2 Pro"
        assert tdp == 30.0

    @patch("sova.monitoring.energy._detect_chip_name", return_value="Unknown")
    def test_unknown_chip_fallback(self, _mock: object) -> None:
        name, tdp = detect_chip_tdp()
        assert name == "Unknown"
        assert tdp == 15.0

    @patch("sova.monitoring.energy._detect_chip_name", return_value="Some Exotic CPU XYZ")
    def test_unrecognized_chip_uses_default(self, _mock: object) -> None:
        name, tdp = detect_chip_tdp()
        assert name == "Some Exotic CPU XYZ"
        assert tdp == 15.0


class TestEstimateEnergy:
    @patch("sova.monitoring.energy._cpu_count", return_value=8)
    @patch("sova.monitoring.energy.detect_chip_tdp", return_value=("Apple M2", 10.0))
    def test_basic_estimation(self, _tdp: object, _cpu: object) -> None:
        result = estimate_energy(avg_cpu_percent=100.0, duration_seconds=3600.0)
        assert result is not None
        assert isinstance(result, EnergyEstimate)
        # 100% on one core of 8 = 12.5% system utilization, 10W * 0.125 * 1h = 1.25 Wh
        assert result.energy_wh == pytest.approx(1.25, abs=0.01)
        assert result.chip_name == "Apple M2"
        assert result.tdp_watts == 10.0
        # CO2: 1.25 Wh / 1000 * 436 = 0.545 g
        assert result.co2_grams == pytest.approx(0.545, abs=0.01)

    @patch("sova.monitoring.energy._cpu_count", return_value=4)
    @patch("sova.monitoring.energy.detect_chip_tdp", return_value=("Test", 10.0))
    def test_custom_co2_intensity(self, _tdp: object, _cpu: object) -> None:
        result = estimate_energy(avg_cpu_percent=100.0, duration_seconds=3600.0, co2_grams_per_kwh=100.0)
        assert result is not None
        # 100% on 4 cores = 25% utilization, 10W * 0.25 * 1h = 2.5 Wh
        # CO2: 2.5 / 1000 * 100 = 0.25 g
        assert result.co2_grams == pytest.approx(0.25, abs=0.01)

    def test_zero_duration_returns_none(self) -> None:
        result = estimate_energy(avg_cpu_percent=50.0, duration_seconds=0.0)
        assert result is None

    def test_negative_duration_returns_none(self) -> None:
        result = estimate_energy(avg_cpu_percent=50.0, duration_seconds=-10.0)
        assert result is None

    @patch("sova.monitoring.energy._cpu_count", return_value=4)
    @patch("sova.monitoring.energy.detect_chip_tdp", return_value=("Unknown", 15.0))
    def test_tdp_override(self, _tdp: object, _cpu: object) -> None:
        result = estimate_energy(avg_cpu_percent=200.0, duration_seconds=1800.0, tdp_watts=50.0)
        assert result is not None
        assert result.tdp_watts == 50.0
        # 200% on 4 cores = 50% utilization, 50W * 0.5 * 0.5h = 12.5 Wh
        assert result.energy_wh == pytest.approx(12.5, abs=0.01)

    @patch("sova.monitoring.energy._cpu_count", return_value=4)
    @patch("sova.monitoring.energy.detect_chip_tdp", return_value=("Test", 10.0))
    def test_short_run(self, _tdp: object, _cpu: object) -> None:
        result = estimate_energy(avg_cpu_percent=50.0, duration_seconds=60.0)
        assert result is not None
        assert result.duration_seconds == 60.0
        assert result.energy_wh > 0


# ---------------------------------------------------------------------------
# Capacity advisor tests
# ---------------------------------------------------------------------------


def _make_summaries(
    count: int, avg_cpu: float = 50.0, peak_cpu: float = 80.0, peak_mem: int = 500_000_000
) -> list[dict]:
    return [
        {"avg_cpu_percent": avg_cpu, "peak_cpu_percent": peak_cpu, "peak_memory_rss_bytes": peak_mem}
        for _ in range(count)
    ]


class TestRecommendCapacity:
    def test_insufficient_data(self) -> None:
        rec = recommend_capacity(
            summaries=_make_summaries(2),
            current_max=3,
            cpu_count=8,
            total_memory_bytes=16_000_000_000,
            current_cpu_percent=20.0,
            current_memory_percent=40.0,
        )
        assert rec.confidence == "insufficient"
        assert rec.recommended_max == 3  # returns current_max
        assert rec.headroom_cpu_percent is None

    def test_empty_summaries(self) -> None:
        rec = recommend_capacity(
            summaries=[],
            current_max=2,
            cpu_count=4,
            total_memory_bytes=8_000_000_000,
            current_cpu_percent=10.0,
            current_memory_percent=30.0,
        )
        assert rec.confidence == "insufficient"
        assert rec.recommended_max == 2

    def test_sufficient_data_low_confidence(self) -> None:
        rec = recommend_capacity(
            summaries=_make_summaries(3, avg_cpu=50.0, peak_mem=500_000_000),
            current_max=3,
            cpu_count=8,
            total_memory_bytes=16_000_000_000,
            current_cpu_percent=20.0,
            current_memory_percent=40.0,
        )
        assert rec.confidence == "low"
        assert rec.recommended_max >= 1
        assert rec.headroom_cpu_percent is not None

    def test_high_confidence_with_many_runs(self) -> None:
        now = datetime.now(timezone.utc)
        rec = recommend_capacity(
            summaries=_make_summaries(15, avg_cpu=100.0, peak_mem=1_000_000_000),
            current_max=3,
            cpu_count=8,
            total_memory_bytes=16_000_000_000,
            current_cpu_percent=20.0,
            current_memory_percent=40.0,
            latest_summary_time=now,
        )
        assert rec.confidence == "high"
        assert rec.recommended_max >= 1

    def test_stale_data_reduces_confidence(self) -> None:
        old_time = datetime.now(timezone.utc) - timedelta(hours=30)
        rec = recommend_capacity(
            summaries=_make_summaries(15),
            current_max=3,
            cpu_count=8,
            total_memory_bytes=16_000_000_000,
            current_cpu_percent=20.0,
            current_memory_percent=40.0,
            latest_summary_time=old_time,
        )
        assert rec.confidence == "medium"  # downgraded from high

    def test_cross_project_noted_in_reason(self) -> None:
        rec = recommend_capacity(
            summaries=_make_summaries(5),
            current_max=2,
            cpu_count=4,
            total_memory_bytes=8_000_000_000,
            current_cpu_percent=10.0,
            current_memory_percent=30.0,
            cross_project_cpu_percent=100.0,
        )
        assert "Cross-project CPU" in rec.reason

    def test_no_cross_project_noted(self) -> None:
        rec = recommend_capacity(
            summaries=_make_summaries(5),
            current_max=2,
            cpu_count=4,
            total_memory_bytes=8_000_000_000,
            current_cpu_percent=10.0,
            current_memory_percent=30.0,
            cross_project_cpu_percent=0.0,
        )
        assert "single-project" in rec.reason

    def test_recommended_at_least_one(self) -> None:
        rec = recommend_capacity(
            summaries=_make_summaries(5, avg_cpu=400.0, peak_mem=15_000_000_000),
            current_max=1,
            cpu_count=4,
            total_memory_bytes=16_000_000_000,
            current_cpu_percent=80.0,
            current_memory_percent=90.0,
        )
        assert rec.recommended_max >= 1

    def test_recommended_max_capped_at_2x_current(self) -> None:
        """Never recommend more than 2x current_max in one step."""
        rec = recommend_capacity(
            summaries=_make_summaries(10, avg_cpu=10.0, peak_mem=100_000_000),
            current_max=2,
            cpu_count=16,
            total_memory_bytes=64_000_000_000,
            current_cpu_percent=5.0,
            current_memory_percent=10.0,
        )
        assert rec.recommended_max <= 2 * 2  # 2x current_max

    def test_safety_margin_applied(self) -> None:
        base = recommend_capacity(
            summaries=_make_summaries(5, avg_cpu=50.0),
            current_max=2,
            cpu_count=8,
            total_memory_bytes=16_000_000_000,
            current_cpu_percent=10.0,
            current_memory_percent=30.0,
            safety_margin=0.1,
        )
        strict = recommend_capacity(
            summaries=_make_summaries(5, avg_cpu=50.0),
            current_max=2,
            cpu_count=8,
            total_memory_bytes=16_000_000_000,
            current_cpu_percent=10.0,
            current_memory_percent=30.0,
            safety_margin=0.5,
        )
        assert base.recommended_max >= strict.recommended_max

    def test_medium_confidence_stale_reduces_to_low(self) -> None:
        old_time = datetime.now(timezone.utc) - timedelta(hours=30)
        rec = recommend_capacity(
            summaries=_make_summaries(6),
            current_max=3,
            cpu_count=8,
            total_memory_bytes=16_000_000_000,
            current_cpu_percent=20.0,
            current_memory_percent=40.0,
            latest_summary_time=old_time,
        )
        assert rec.confidence == "low"  # medium -> low due to staleness

    def test_zero_avg_cpu_per_agent(self) -> None:
        """Zero CPU per agent defaults to current_max for CPU-based calculation."""
        rec = recommend_capacity(
            summaries=_make_summaries(5, avg_cpu=0.0, peak_mem=500_000_000),
            current_max=3,
            cpu_count=8,
            total_memory_bytes=16_000_000_000,
            current_cpu_percent=10.0,
            current_memory_percent=30.0,
        )
        assert rec.recommended_max >= 1

    def test_zero_memory_per_agent(self) -> None:
        """Zero memory per agent defaults to current_max for memory-based calculation."""
        rec = recommend_capacity(
            summaries=_make_summaries(5, avg_cpu=50.0, peak_mem=0),
            current_max=3,
            cpu_count=8,
            total_memory_bytes=16_000_000_000,
            current_cpu_percent=10.0,
            current_memory_percent=30.0,
        )
        assert rec.recommended_max >= 1

    def test_naive_latest_time_treated_as_utc(self) -> None:
        """Naive timestamps are treated as UTC without raising."""
        naive_time = datetime.now()
        rec = recommend_capacity(
            summaries=_make_summaries(10),
            current_max=3,
            cpu_count=8,
            total_memory_bytes=16_000_000_000,
            current_cpu_percent=20.0,
            current_memory_percent=40.0,
            latest_summary_time=naive_time,
        )
        assert rec.confidence in ("high", "medium", "low")

    def test_zero_cpu_count_in_headroom(self) -> None:
        """Edge case: zero total_cpu_capacity should not divide by zero."""
        rec = recommend_capacity(
            summaries=_make_summaries(5),
            current_max=1,
            cpu_count=0,
            total_memory_bytes=16_000_000_000,
            current_cpu_percent=0.0,
            current_memory_percent=30.0,
        )
        assert rec.recommended_max >= 1

    def test_zero_total_memory_in_headroom(self) -> None:
        """Edge case: zero total_memory should not divide by zero."""
        rec = recommend_capacity(
            summaries=_make_summaries(5),
            current_max=1,
            cpu_count=8,
            total_memory_bytes=0,
            current_cpu_percent=10.0,
            current_memory_percent=0.0,
        )
        assert rec.recommended_max >= 1


class TestDetectChipName:
    @patch("sova.monitoring.energy.platform.system", return_value="Darwin")
    @patch("sova.monitoring.energy.subprocess.run")
    def test_darwin_brand_string(self, mock_run: MagicMock, _sys: MagicMock) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "Apple M2 Pro\n"
        mock_run.return_value = mock_result
        assert _detect_chip_name() == "Apple M2 Pro"

    @patch("sova.monitoring.energy.platform.system", return_value="Darwin")
    @patch("sova.monitoring.energy.subprocess.run")
    def test_darwin_hw_chip_fallback(self, mock_run: MagicMock, _sys: MagicMock) -> None:
        empty = MagicMock()
        empty.returncode = 1
        empty.stdout = ""
        hw_chip = MagicMock()
        hw_chip.returncode = 0
        hw_chip.stdout = "M4\n"
        mock_run.side_effect = [empty, hw_chip]
        assert _detect_chip_name() == "Apple M4"

    @patch("sova.monitoring.energy.platform.system", return_value="Darwin")
    @patch("sova.monitoring.energy.subprocess.run")
    def test_darwin_both_fail(self, mock_run: MagicMock, _sys: MagicMock) -> None:
        fail = MagicMock()
        fail.returncode = 1
        fail.stdout = ""
        mock_run.return_value = fail
        assert _detect_chip_name() == "Unknown"

    @patch("sova.monitoring.energy.platform.system", return_value="Darwin")
    @patch("sova.monitoring.energy.subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="sysctl", timeout=5))
    def test_darwin_timeout(self, _run: MagicMock, _sys: MagicMock) -> None:
        assert _detect_chip_name() == "Unknown"

    @patch("sova.monitoring.energy.platform.system", return_value="Darwin")
    @patch("sova.monitoring.energy.subprocess.run", side_effect=FileNotFoundError)
    def test_darwin_file_not_found(self, _run: MagicMock, _sys: MagicMock) -> None:
        assert _detect_chip_name() == "Unknown"

    @patch("sova.monitoring.energy.platform.system", return_value="Linux")
    def test_linux_proc_cpuinfo(self, _sys: MagicMock) -> None:
        cpuinfo = "processor\t: 0\nmodel name\t: AMD Ryzen 9 5900X\n"
        with patch("builtins.open", create=True) as mock_open:
            mock_open.return_value.__enter__ = lambda s: iter(cpuinfo.splitlines(True))
            mock_open.return_value.__exit__ = MagicMock(return_value=False)
            assert _detect_chip_name() == "AMD Ryzen 9 5900X"

    @patch("sova.monitoring.energy.platform.system", return_value="Linux")
    def test_linux_no_proc_cpuinfo(self, _sys: MagicMock) -> None:
        with patch("builtins.open", side_effect=FileNotFoundError):
            assert _detect_chip_name() == "Unknown"

    @patch("sova.monitoring.energy.platform.system", return_value="Windows")
    def test_unsupported_platform(self, _sys: MagicMock) -> None:
        assert _detect_chip_name() == "Unknown"


class TestCpuCount:
    def test_psutil_available(self) -> None:
        from sova.monitoring.energy import _cpu_count

        _cpu_count.cache_clear()
        try:
            count = _cpu_count()
            assert count >= 1
        finally:
            _cpu_count.cache_clear()

    def test_psutil_returns_none_fallback(self) -> None:
        from sova.monitoring.energy import _cpu_count

        _cpu_count.cache_clear()
        try:
            with patch("psutil.cpu_count", return_value=None):
                _cpu_count.cache_clear()
                count = _cpu_count()
                assert count == 1  # or fallback
        finally:
            _cpu_count.cache_clear()


class TestCapacityRecommendationDataclass:
    def test_frozen(self) -> None:
        rec = CapacityRecommendation(
            recommended_max=3,
            current_max=2,
            confidence="medium",
            headroom_cpu_percent=15.0,
            headroom_memory_percent=20.0,
            reason="Test.",
        )
        with pytest.raises(AttributeError):
            rec.recommended_max = 5  # type: ignore[misc]


class TestEstimateEnergyEdgeCases:
    @patch("sova.monitoring.energy._cpu_count", return_value=4)
    @patch("sova.monitoring.energy.detect_chip_tdp", return_value=("Test", 10.0))
    def test_zero_cpu_clamped(self, _tdp: object, _cpu: object) -> None:
        result = estimate_energy(avg_cpu_percent=0.0, duration_seconds=3600.0)
        assert result is not None
        assert result.energy_wh == 0.0

    @patch("sova.monitoring.energy._cpu_count", return_value=4)
    @patch("sova.monitoring.energy.detect_chip_tdp", return_value=("Test", 10.0))
    def test_negative_cpu_clamped(self, _tdp: object, _cpu: object) -> None:
        result = estimate_energy(avg_cpu_percent=-50.0, duration_seconds=3600.0)
        assert result is not None
        assert result.energy_wh == 0.0

    @patch("sova.monitoring.energy._cpu_count", return_value=4)
    @patch("sova.monitoring.energy.detect_chip_tdp", return_value=("Test", 10.0))
    def test_very_high_cpu_clamped(self, _tdp: object, _cpu: object) -> None:
        """CPU above 100*cpu_count is clamped."""
        result = estimate_energy(avg_cpu_percent=9999.0, duration_seconds=3600.0)
        assert result is not None
        # At full utilization: 10W * 1.0 * 1h = 10 Wh
        assert result.energy_wh == pytest.approx(10.0, abs=0.01)
