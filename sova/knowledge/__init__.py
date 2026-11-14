"""Knowledge management for SOVA agents.

Provides task assessments, per-PR review retrospectives (``retro``) and tech-stack detection.
"""

from sova.knowledge.personas import detect_persona

__all__ = ["detect_persona"]
