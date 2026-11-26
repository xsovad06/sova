"""Helpers shared by the files split from the former tests/test_dashboard.py."""

from __future__ import annotations

import functools
import re
from pathlib import Path

_DASHBOARD_DIR = Path(__file__).parent.parent / "sova" / "dashboard"

# httpx merges per-call headers over the client's defaults rather than clearing
# them, so a test proving a route rejects a *missing* CSRF pair must explicitly
# blank out both headers the `client` fixture in tests/conftest.py pre-arms on every request.
NO_CSRF = {"origin": "", "x-csrf-token": ""}

PHONE_MEDIA = "@media (max-width: 767.98px) {"


@functools.cache
def phone_css_block() -> str:
    """The stylesheet's phone block (#1564): from its media query to the matching closing brace."""
    css = (_DASHBOARD_DIR / "static" / "style.css").read_text(encoding="utf-8")
    start = css.index(PHONE_MEDIA)
    depth = 0
    for i in range(start, len(css)):
        if css[i] == "{":
            depth += 1
        elif css[i] == "}":
            depth -= 1
            if depth == 0:
                return css[start : i + 1]
    raise AssertionError("unterminated phone block")


def css_rule(block: str, selector: str) -> str:
    """The body of the rule in `block` whose selector text is exactly `selector`."""
    match = re.search(rf"(?m)^\s*{re.escape(selector)}\s*\{{(.*?)\}}", block, re.DOTALL)
    assert match, f"no rule for {selector}"
    return match.group(1)
