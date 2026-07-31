"""Oversight analysis: send snapshot to LLM, parse findings, persist to DB.

The ``analyze_snapshot()`` function serializes a snapshot summary, sends it to
the LLM with the operations persona, parses a JSON array of structured
findings, deduplicates against the last 14 days of DB history, and persists
new findings as ``OversightFinding`` rows linked to the current
``OversightRun``.

The entire LLM call is wrapped in non-fatal error handling so a failure sets
ERROR status with a "partial:" error message prefix rather than aborting the
wake cycle.
"""

from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select

from sova.db.models import OversightFinding
from sova.llm.provider import LLMProvider
from sova.utils.logging import get_logger
from sova.utils.markdown import strip_code_fences

log = get_logger(component="oversight.analysis")

_DEFAULT_DEDUP_WINDOW_DAYS = 14
_DEFAULT_ANALYSIS_TIMEOUT = 120
_MAX_DEDUP_TITLES = 5000  # Query limit to prevent unbounded memory usage

_VALID_SEVERITIES = {"info", "warning", "critical"}
_VALID_SCOPES = {"global", "project"}

_DEFAULT_PERSONA = """\
You are an operations analyst reviewing a fleet of software projects managed \
by SOVA (Software Orchestration Via Agents). Identify actionable findings: \
failures, bottlenecks, stale issues, resource pressure, or patterns that need \
human attention. Be concise. Group related observations.
"""

_ANALYSIS_PROMPT_TEMPLATE = """\
{persona}

Analyze the following cross-project health snapshot and return a JSON array of findings.

Each finding must be a JSON object with these fields:
- "title" (string, required): short one-line summary
- "scope" (string, required): "global" for fleet-wide or "project" for project-specific
- "severity" (string): "info", "warning", or "critical" (default: "info")
- "description" (string): detailed observation
- "recommendation" (string): actionable next step
- "confidence" (float 0.0-1.0): how certain you are (default: 0.5)
- "project_slug" (string): relevant project slug, empty for global findings

Return ONLY the JSON array. No markdown, no explanation, no wrapping.
If there are no findings, return an empty array: []

## Snapshot

{snapshot}
"""

_MAX_SNAPSHOT_CHARS = 50_000


def _to_dict(snapshot: Any) -> dict:
    """Convert a snapshot to a dict without mutating the original."""
    if snapshot is None:
        return {}
    if isinstance(snapshot, dict):
        return copy.deepcopy(snapshot)
    if hasattr(snapshot, "to_dict"):
        return snapshot.to_dict()
    return {"raw": str(snapshot)}


def _truncate_projects(data: dict) -> None:
    """Remove open_issues and open_prs from projects to reduce size (in-place)."""
    projects = data.get("projects")
    if not isinstance(projects, list):
        return
    for project in reversed(projects):
        if not isinstance(project, dict):
            continue
        for key in ("open_issues", "open_prs"):
            if key in project:
                project[key] = []


def _serialize_snapshot(snapshot: Any, max_chars: int = _MAX_SNAPSHOT_CHARS) -> str:
    """Serialize a snapshot to a JSON string, truncating if needed.

    Truncation removes per-project failure details (oldest first) to stay
    within the character budget while preserving project-level summaries.
    The original snapshot dict is never mutated.
    """
    data = _to_dict(snapshot)

    text = json.dumps(data, indent=2, default=str)
    if len(text) <= max_chars:
        return text

    _truncate_projects(data)
    text = json.dumps(data, indent=2, default=str)
    if len(text) <= max_chars:
        return text

    return text[:max_chars]


def _build_prompt(persona: str, snapshot_text: str) -> str:
    """Build the LLM analysis prompt from persona and snapshot."""
    effective_persona = persona.strip() if persona.strip() else _DEFAULT_PERSONA
    return _ANALYSIS_PROMPT_TEMPLATE.format(
        persona=effective_persona,
        snapshot=snapshot_text,
    )


def _parse_findings(raw_text: str) -> list[dict]:
    """Parse the LLM response into a list of finding dicts.

    Handles JSON wrapped in markdown code fences.
    """
    text = strip_code_fences(raw_text)
    parsed = json.loads(text)
    if not isinstance(parsed, list):
        raise TypeError(f"Expected JSON array, got {type(parsed).__name__}")
    return parsed


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _finding_from_dict(d: dict, run_id: str) -> OversightFinding | None:
    """Construct an OversightFinding from a parsed dict, or None if invalid."""
    title = d.get("title")
    scope = d.get("scope")
    if not title or not scope:
        log.warning("oversight.analysis.skip_finding", reason="missing title or scope", raw=d)
        return None

    raw_confidence = d.get("confidence", 0.5)
    try:
        confidence = _clamp(float(raw_confidence), 0.0, 1.0)
    except (ValueError, TypeError):
        confidence = 0.5

    # Validate severity with safe fallback
    severity = str(d.get("severity", "info")).lower()[:20]
    if severity not in _VALID_SEVERITIES:
        log.debug("oversight.analysis.invalid_severity", severity=severity, fallback="info")
        severity = "info"

    # Validate scope with safe fallback
    scope_str = str(scope).lower()[:20]
    if scope_str not in _VALID_SCOPES:
        log.debug("oversight.analysis.invalid_scope", scope=scope_str, fallback="project")
        scope_str = "project"

    return OversightFinding(
        run_id=run_id,
        title=str(title)[:300],
        scope=scope_str,
        severity=severity,
        description=str(d.get("description", "")),
        recommendation=str(d.get("recommendation", "")),
        confidence=confidence,
        project_slug=str(d.get("project_slug", ""))[:100],
        dismissed=False,
        github_issue_number=None,
    )


async def _load_recent_titles(session: Any, dedup_window_days: int = _DEFAULT_DEDUP_WINDOW_DAYS) -> set[str]:
    """Load finding titles from the last N days.

    Limits to _MAX_DEDUP_TITLES most recent to prevent unbounded memory usage.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=dedup_window_days)
    stmt = (
        select(OversightFinding.title)
        .where(OversightFinding.created_at >= cutoff)
        .order_by(OversightFinding.created_at.desc())
        .limit(_MAX_DEDUP_TITLES)
    )
    result = await session.execute(stmt)
    return {row[0] for row in result.all()}


async def _persist_findings(
    findings: list[OversightFinding],
    run_id: str,
) -> int:
    """Persist findings to the DB. Returns number persisted."""
    if not findings:
        return 0

    from sova.db.session import get_session

    try:
        async with await get_session() as session:
            async with session.begin():
                session.add_all(findings)
                await session.flush()
        return len(findings)
    except Exception:
        log.error("oversight.analysis.persist_failed", run_id=run_id, count=len(findings), exc_info=True)
        return 0


async def analyze_snapshot(
    snapshot: Any,
    run_id: str,
    persona: str,
    provider: LLMProvider,
    *,
    model: str = "claude-haiku-4-5-20251001",
    dedup_window_days: int = _DEFAULT_DEDUP_WINDOW_DAYS,
    analysis_timeout: int = _DEFAULT_ANALYSIS_TIMEOUT,
) -> tuple[list[OversightFinding], str | None]:
    """Analyze a snapshot via LLM and persist deduplicated findings.

    Args:
        snapshot: The OversightSnapshot (or dict) from the observation phase.
        run_id: The OversightRun.id for this wake cycle.
        persona: The operations persona text (already loaded by caller).
        provider: An LLMProvider instance for the LLM call.
        model: Model identifier to use for analysis.

    Returns:
        Tuple of (list of persisted OversightFinding instances, error message or None).
        On success: (findings, None). On failure: ([], error_message).
    """
    try:
        snapshot_text = _serialize_snapshot(snapshot)
        prompt = _build_prompt(persona, snapshot_text)

        llm_result = await provider.invoke(prompt, model=model, timeout=analysis_timeout)
        raw_text = llm_result.result if hasattr(llm_result, "result") else str(llm_result)

        try:
            parsed = _parse_findings(raw_text)
        except (json.JSONDecodeError, TypeError) as exc:
            log.error("oversight.analysis.parse_failed", run_id=run_id, error=str(exc))
            return ([], "partial: parse failed")

        # Deduplicate against recent history
        from sova.db.session import get_session

        try:
            async with await get_session() as session:
                recent_titles = await _load_recent_titles(session, dedup_window_days)
        except Exception:
            log.warning("oversight.analysis.dedup_query_failed", run_id=run_id, exc_info=True)
            recent_titles = set()

        # Build findings, skipping duplicates and invalid entries
        new_findings: list[OversightFinding] = []
        for entry in parsed:
            if not isinstance(entry, dict):
                continue
            finding = _finding_from_dict(entry, run_id)
            if finding is None:
                continue
            if finding.title in recent_titles:
                log.debug("oversight.analysis.dedup_skip", title=finding.title)
                continue
            new_findings.append(finding)
            recent_titles.add(finding.title)

        persisted = await _persist_findings(new_findings, run_id)

        global_count = sum(1 for f in new_findings if f.scope == "global")
        local_count = len(new_findings) - global_count
        log.info(
            "oversight.analysis.complete",
            run_id=run_id,
            total=persisted,
            global_findings=global_count,
            local_findings=local_count,
        )

        if new_findings and persisted == 0:
            return (new_findings, "partial: persistence failed")

        return (new_findings, None)

    except Exception as exc:
        log.error("oversight.analysis.failed", run_id=run_id, exc_info=True)
        return ([], f"partial: {exc}")
