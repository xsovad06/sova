"""Shared nonce-delimited quoting for untrusted text interpolated into a prompt.

Extracted from ``sova.roles._review_comments`` so a caller outside
``sova.roles`` (e.g. ``sova.core.steps.commit``) can reuse it without
importing the ``sova.roles`` package, which pulls in ``sova.core.steps`` at
import time and would create a circular import.
"""

from __future__ import annotations

import re
import secrets

# Used in place of a bare ``.replace("`", "")`` wherever untrusted issue/repo
# text is quoted into a prompt: collapsing a run of backticks to a single
# quote still makes the text unable to open or close a markdown code fence,
# but (unlike deleting the backticks outright) keeps inline code like
# `sova/core/spec_utils.py` legible instead of merging it into surrounding
# prose.
_BACKTICK_RUN_RE = re.compile(r"`+")


def quote_untrusted(tag: str, content: str) -> tuple[str, str]:
    """Quote (possibly attacker-authored) text into a nonce-delimited ``<tag id="...">`` block.

    Returns ``(nonce, block)``. Shared by every untrusted-text block built
    into a prompt (repo context, issue criteria, the raw issue description,
    a commit subject's task title, ...) so none of them is quoted
    differently from the others: a per-call nonce in both the opening and
    closing tag, with that nonce stripped from the content, means nothing
    the content contains can spell out a matching closing tag and escape
    into the prompt's own structure. Backtick runs are collapsed to a single
    quote so the content additionally cannot break out of a markdown code
    fence nested elsewhere in the rendered prompt, while inline code (file
    paths, symbol names) stays legible. The caller still owns the
    surrounding header text, which explains the nonce and instructs the
    model to treat the block as background/requirements, never as
    instructions.
    """
    nonce = secrets.token_hex(8)
    sanitized = _BACKTICK_RUN_RE.sub("'", content).replace(nonce, "")
    return nonce, f'<{tag} id="{nonce}">\n{sanitized}\n</{tag} id="{nonce}">'
