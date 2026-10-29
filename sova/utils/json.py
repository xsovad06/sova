"""JSON parsing utilities for LLM responses.

Handles common LLM response patterns:
- Markdown code fences (```json ... ```)
- Prose before/after the JSON
- JSON embedded in explanatory text
"""

from __future__ import annotations

import json

from sova.utils.markdown import strip_code_fences


def _from_first_bracket(inner: str) -> str:
    """Return *inner* from its first ``[`` or ``{`` (the JSON start), or all of it when it has neither."""
    for i, ch in enumerate(inner):
        if ch in ("[", "{"):
            return inner[i:].strip()
    return inner.strip()


def extract_json(text: str) -> str:
    """Extract JSON from LLM response, stripping markdown fences and prose.

    Returns the extracted JSON as a string (not parsed). The caller should
    call json.loads() on the result.

    Returns empty string if no JSON is found.
    """
    cleaned = text.strip()
    if not cleaned:
        return ""

    # A response that is wrapped in one fence goes through the canonical stripper, so triage's
    # strip_code_fences() and this function agree on every wrapper shape (wide fences, a closing fence
    # glued to the last line, a closing fence of another width). If a fence is left in the result, the
    # fence was not the whole wrapper (prose follows it, or the content holds fences of its own), and
    # the search below decides where the JSON is.
    if cleaned.startswith("```"):
        unwrapped = strip_code_fences(cleaned)
        if unwrapped and "```" not in unwrapped:
            return unwrapped if "\n" in cleaned else _from_first_bracket(unwrapped)

    # A fence in the middle of prose, or prose after the closing fence: find the first fenced block using
    # string ops (avoids ReDoS-prone regex). This is the one place a response is allowed to carry text
    # outside its fence, which strip_code_fences() deliberately does not tolerate.
    fence_start = cleaned.find("```")
    if fence_start != -1:
        fence_end = cleaned.find("\n", fence_start)
        if fence_end == -1:
            closing = cleaned.find("```", fence_start + 3)
            inner = cleaned[fence_start + 3 : closing] if closing != -1 else cleaned[fence_start + 3 :]
            return _from_first_bracket(inner)
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
