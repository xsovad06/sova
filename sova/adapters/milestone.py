"""Milestone phase parsing: single source of truth for a milestone title's phase number.

Leaf module (zero intra-sova imports) so the dashboard queue and the supervisor's
suitability ranker can share one parse without either importing the other's layer.
"""

from __future__ import annotations

import re

# Sort position of an issue whose milestone names no phase: after every real phase.
UNPHASED = 99

_PHASE_RE = re.compile(r"(?:Phase\s*|P)(\d+)", re.IGNORECASE)


def extract_phase_order(milestone: str | None) -> int:
    """Extract the phase number from a milestone title. Lower = earlier phase.

    Supports: 'Phase 1: Ship It', 'Phase 2', 'P1: ...', 'P2'. A missing, empty,
    or phase-less milestone resolves to ``UNPHASED``. ``Phase 0`` is a real phase
    that sorts before ``Phase 1``, so callers test ``!= UNPHASED``, never truthiness.
    """
    if not milestone:
        return UNPHASED
    m = _PHASE_RE.match(milestone.strip())
    return int(m.group(1)) if m else UNPHASED
