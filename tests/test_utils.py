"""Tests for SOVA utility functions."""

from __future__ import annotations

from sova.utils.formatting import branch_name, slugify, truncate


def test_slugify_basic() -> None:
    assert slugify("Add user authentication") == "add-user-authentication"


def test_slugify_special_chars() -> None:
    assert slugify("Fix bug #42: NullPointer!") == "fix-bug-42-nullpointer"


def test_slugify_max_length() -> None:
    result = slugify("This is a very long title that should be truncated", max_length=20)
    assert len(result) <= 20
    assert not result.endswith("-")


def test_slugify_unicode() -> None:
    assert slugify("Implementar funcionalidad") == "implementar-funcionalidad"


def test_branch_name_default() -> None:
    assert branch_name(42, "Add login page") == "agent/feat/42-add-login-page"


def test_branch_name_fix_prefix() -> None:
    assert branch_name(10, "Fix crash on startup", prefix="fix") == "agent/fix/10-fix-crash-on-startup"


def test_truncate_short() -> None:
    assert truncate("short", 200) == "short"


def test_truncate_long() -> None:
    result = truncate("a" * 300, 200)
    assert len(result) == 200
    assert result.endswith("...")
