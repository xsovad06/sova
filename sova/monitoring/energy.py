"""Energy estimation for agent runs.

Estimates energy consumption (Wh) from CPU usage, run duration, and system TDP.
Uses a simplified model: Energy = TDP * (avg_cpu_percent / 100) * duration_hours.
"""

from __future__ import annotations

import functools
import platform
import subprocess
from dataclasses import dataclass

# TDP values in watts for common development chips
_TDP_TABLE: dict[str, float] = {
    # Apple Silicon
    "apple m1": 10.0,
    "apple m1 pro": 30.0,
    "apple m1 max": 60.0,
    "apple m1 ultra": 120.0,
    "apple m2": 10.0,
    "apple m2 pro": 30.0,
    "apple m2 max": 60.0,
    "apple m2 ultra": 120.0,
    "apple m3": 10.0,
    "apple m3 pro": 30.0,
    "apple m3 max": 60.0,
    "apple m3 ultra": 120.0,
    "apple m4": 10.0,
    "apple m4 pro": 30.0,
    "apple m4 max": 60.0,
    "apple m4 ultra": 120.0,
    # Intel mobile
    "intel core i7": 45.0,
    "intel core i9": 65.0,
    "intel core i5": 35.0,
    # AMD mobile
    "amd ryzen 7": 45.0,
    "amd ryzen 9": 65.0,
    "amd ryzen 5": 35.0,
}

_DEFAULT_TDP = 15.0  # Conservative fallback


@dataclass(frozen=True, slots=True)
class EnergyEstimate:
    """Result of energy estimation for a single agent run."""

    energy_wh: float
    co2_grams: float
    chip_name: str
    tdp_watts: float
    duration_seconds: float
    avg_cpu_percent: float


@functools.lru_cache(maxsize=1)
def detect_chip_tdp() -> tuple[str, float]:
    """Detect the CPU chip name and look up its TDP.

    Returns (chip_name, tdp_watts). Falls back to ("Unknown", 15.0).
    """
    chip = _detect_chip_name()
    if chip == "Unknown":
        return chip, _DEFAULT_TDP

    chip_lower = chip.lower()
    # Sort longest-first so "apple m2 pro" matches before "apple m2"
    for pattern, tdp in sorted(_TDP_TABLE.items(), key=lambda x: len(x[0]), reverse=True):
        if pattern in chip_lower:
            return chip, tdp

    return chip, _DEFAULT_TDP


def estimate_energy(
    avg_cpu_percent: float,
    duration_seconds: float,
    tdp_watts: float | None = None,
    co2_grams_per_kwh: float = 436.0,
) -> EnergyEstimate | None:
    """Estimate energy consumption for an agent run.

    Returns None if duration is zero or negative (immediate failures).
    """
    if duration_seconds <= 0:
        return None

    chip_name, detected_tdp = detect_chip_tdp()
    tdp = tdp_watts if tdp_watts is not None else detected_tdp

    duration_hours = duration_seconds / 3600.0
    cpu_fraction = max(0.0, min(avg_cpu_percent, 100.0 * _cpu_count())) / (100.0 * _cpu_count())
    energy_wh = tdp * cpu_fraction * duration_hours
    co2_grams = energy_wh / 1000.0 * co2_grams_per_kwh

    return EnergyEstimate(
        energy_wh=round(energy_wh, 4),
        co2_grams=round(co2_grams, 4),
        chip_name=chip_name,
        tdp_watts=tdp,
        duration_seconds=duration_seconds,
        avg_cpu_percent=avg_cpu_percent,
    )


@functools.lru_cache(maxsize=1)
def _cpu_count() -> int:
    """Get CPU count, defaulting to 1 if unavailable."""
    try:
        import psutil

        return psutil.cpu_count() or 1
    except (ImportError, NotImplementedError):
        import os

        return os.cpu_count() or 1


def _detect_chip_name() -> str:
    """Detect the CPU chip name from the system."""
    system = platform.system()

    if system == "Darwin":
        try:
            result = subprocess.run(
                ["/usr/sbin/sysctl", "-n", "machdep.cpu.brand_string"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
            # Apple Silicon may not have brand_string; use chip name
            result = subprocess.run(
                ["/usr/sbin/sysctl", "-n", "hw.chip"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if result.returncode == 0 and result.stdout.strip():
                return f"Apple {result.stdout.strip()}"
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            pass

    elif system == "Linux":
        try:
            with open("/proc/cpuinfo") as f:
                for line in f:
                    if line.startswith("model name"):
                        return line.split(":", 1)[1].strip()
        except (FileNotFoundError, OSError):
            pass

    return "Unknown"
