"""Challenger pass: verify and calibrate review findings via a second LLM call.

The challenger receives the full finding set plus the PR diff and verifies
each finding against the actual code. It can remove findings with weak
reasoning, calibrate severity, and merge duplicates. It never generates
new findings.
"""

from __future__ import annotations

import json

from sova.roles._review_comments import ReviewFinding, _extract_json, _safe_severity
from sova.utils.logging import get_logger

log = get_logger(component="role.reviewer.challenger")


def _filter_diff_for_findings(diff: str, findings: list[ReviewFinding]) -> str:
    """Extract only the diff sections for files referenced by findings."""
    referenced_files = {f.file for f in findings}
    sections: list[str] = []
    current: list[str] = []
    current_file: str | None = None

    for line in diff.splitlines(keepends=True):
        if line.startswith("diff --git "):
            if current and current_file in referenced_files:
                sections.append("".join(current))
            current = [line]
            parts = line.split(" b/", 1)
            current_file = parts[1].rstrip() if len(parts) > 1 else None
        else:
            current.append(line)

    if current and current_file in referenced_files:
        sections.append("".join(current))

    return "".join(sections) if sections else diff


def _build_challenger_prompt(findings: list[ReviewFinding], diff: str) -> str:
    """Build the challenger LLM prompt."""
    findings_json = [
        {
            "index": i,
            "file": f.file,
            "line": f.line,
            "severity": f.severity,
            "category": f.category,
            "description": f.description,
            "suggestion": f.suggestion,
        }
        for i, f in enumerate(findings)
    ]

    return f"""You are a senior code review auditor. Your job is to verify review findings \
against the actual code diff. You must be skeptical: only findings with clear \
code evidence survive.

## Findings to Verify
```json
{json.dumps(findings_json, indent=2)}
```

## PR Diff
```
{diff}
```

## Instructions
For each finding, verify it against the actual diff:
1. Is the described issue actually present in the code? Check the exact file and line.
2. Is the severity appropriate? Adjust if the impact is overstated or understated.
3. Are any findings duplicates describing the same underlying issue? Merge them.
4. Does the finding have concrete code evidence, or is it speculative?

Rules:
- REMOVE findings that are speculative, not supported by the diff, or describe \
issues in code that was not changed.
- REMOVE findings where the described bug or issue does not actually exist when \
reading the code carefully.
- DOWNGRADE severity when the impact is overstated (e.g., a style issue scored as a bug).
- MERGE findings that describe the same underlying issue from different angles. \
Use the higher severity and more specific suggestion from the pair.
- NEVER add new findings. You can only keep, modify, or remove existing ones.
- Every removal or downgrade must cite specific code evidence.

## Output Format
Return ONLY a JSON object (no markdown fences, no extra text):
{{
  "adjudicated_findings": [
    {{
      "index": 0,
      "severity": 7,
      "description": "Updated description if needed",
      "suggestion": "Updated suggestion if needed",
      "action": "keep|downgrade|merge",
      "reason": "Why this finding survives or was modified"
    }}
  ],
  "removed_findings": [
    {{
      "index": 1,
      "reason": "Specific code evidence for why this finding is invalid"
    }}
  ]
}}"""


def _apply_challenger_response(
    response_text: str,
    original_findings: list[ReviewFinding],
) -> list[ReviewFinding]:
    """Parse challenger response and return the adjudicated finding list.

    Fails open: returns original findings unchanged on any parse error.
    """
    text = response_text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = _extract_json(text)

    if not isinstance(data, dict) or "adjudicated_findings" not in data:
        log.warning("challenger.malformed_response", text_preview=text[:200])
        return list(original_findings)

    adjudicated = data.get("adjudicated_findings", [])
    if not isinstance(adjudicated, list):
        log.warning("challenger.invalid_adjudicated_type")
        return list(original_findings)

    removed_indices: set[int] = set()
    removed_raw = data.get("removed_findings", [])
    if not isinstance(removed_raw, list):
        log.warning("challenger.invalid_removed_type", type=type(removed_raw).__name__)
        removed_raw = []
    for entry in removed_raw:
        if isinstance(entry, dict) and isinstance(entry.get("index"), int):
            removed_indices.add(entry["index"])
            reason = entry.get("reason", "")
            log.info("challenger.removed", index=entry["index"], reason=reason)

    result: list[ReviewFinding] = []
    seen_indices: set[int] = set()

    for entry in adjudicated:
        if not isinstance(entry, dict):
            continue
        idx = entry.get("index")
        if not isinstance(idx, int) or idx < 0 or idx >= len(original_findings):
            continue
        if idx in removed_indices or idx in seen_indices:
            continue
        seen_indices.add(idx)

        original = original_findings[idx]
        result.append(
            ReviewFinding(
                file=original.file,
                line=original.line,
                severity=_safe_severity(entry.get("severity", original.severity), original.severity),
                category=original.category,
                description=entry.get("description", original.description) or original.description,
                suggestion=entry.get("suggestion", original.suggestion) or original.suggestion,
            )
        )

        action = entry.get("action", "keep")
        if action == "merge":
            merged_reason = entry.get("reason", "")
            log.info("challenger.merged", index=idx, reason=merged_reason)

    if not result and original_findings:
        log.info("challenger.all_removed", original_count=len(original_findings))

    return result
