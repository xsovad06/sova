"""Knowledge management for SOVA agents.

Provides task assessments, per-PR review retrospectives (``retro``) and tech-stack detection.

Allowed imports: sova.db, sova.git, sova.llm, sova.roles, sova.utils (not a layer; chains through it are checked).
"""

from sova.knowledge.personas import detect_persona

__all__ = ["detect_persona"]
