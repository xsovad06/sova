"""Text formatting and slug generation utilities for SOVA."""

from __future__ import annotations

import re
import unicodedata


def slugify(text: str, max_length: int = 50) -> str:
    """Convert text to a URL/branch-safe slug.

    Examples:
        slugify("Add user authentication") -> "add-user-authentication"
        slugify("Fix bug #42: NullPointer!") -> "fix-bug-42-nullpointer"
    """
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = text.strip("-")
    if len(text) > max_length:
        text = text[:max_length].rsplit("-", 1)[0]
    return text


def branch_name(issue_number: int | str, title: str, prefix: str = "feat") -> str:
    """Generate a conventional branch name from an issue.

    Examples:
        branch_name(42, "Add login page") -> "agent/feat/42-add-login-page"
    """
    slug = slugify(title, max_length=40)
    return f"agent/{prefix}/{issue_number}-{slug}"


def truncate(text: str, max_length: int = 200) -> str:
    """Truncate text with ellipsis."""
    if len(text) <= max_length:
        return text
    return text[: max_length - 3] + "..."
