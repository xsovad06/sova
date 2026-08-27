"""JSON parsing utilities for LLM responses.

Handles common LLM response patterns:
- Markdown code fences (```json ... ```)
- Prose before/after the JSON
- JSON embedded in explanatory text
"""

from __future__ import annotations

import json


def extract_json(text: str) -> str:
    """Extract JSON from LLM response, stripping markdown fences and prose.

    Returns the extracted JSON as a string (not parsed). The caller should
    call json.loads() on the result.

    Returns empty string if no JSON is found.
    """
    cleaned = text.strip()
    if not cleaned:
        return ""

    # Strip markdown code fences using string ops (avoids ReDoS-prone regex)
    fence_start = cleaned.find("```")
    if fence_start != -1:
        fence_end = cleaned.find("\n", fence_start)
        if fence_end == -1:
            closing = cleaned.find("```", fence_start + 3)
            inner = cleaned[fence_start + 3 : closing] if closing != -1 else cleaned[fence_start + 3 :]
            for i, ch in enumerate(inner):
                if ch in ("[", "{"):
                    return inner[i:].strip()
            return inner.strip()
        closing = cleaned.find("```", fence_end)
        if closing != -1:
            return cleaned[fence_end + 1 : closing].strip()

    # No fences: find [ or { and use raw_decode to extract the complete
    # JSON value. raw_decode correctly handles brackets inside quoted
    # strings, unlike manual bracket-counting. Try each candidate position
    # so that prose brackets (e.g. "[see docs]") are skipped.
    decoder = json.JSONDecoder()
    last_candidate = -1
    for i, ch in enumerate(cleaned):
        if ch in ("[", "{"):
            try:
                obj, _end = decoder.raw_decode(cleaned, i)
                return json.dumps(obj)
            except json.JSONDecodeError:
                last_candidate = i
                continue

    # All raw_decode attempts failed; return from the last candidate as best-effort
    if last_candidate >= 0:
        return cleaned[last_candidate:]

    return ""
