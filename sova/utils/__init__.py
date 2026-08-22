"""Utility functions for SOVA."""

from __future__ import annotations

from sova.utils.formatting import branch_name, decimal_to_json, iso_utc, slugify, truncate
from sova.utils.gh import get_active_gh_user, resolve_gh_env, resolve_linked_issue
from sova.utils.logging import get_logger, setup_logging
from sova.utils.markdown import extract_section, strip_code_fences, strip_preamble
from sova.utils.rtk import inject_rtk_hook, is_rtk_available, remove_rtk_hook
from sova.utils.shell import GitIdentityResult, ShellResult, subprocess_error

__all__ = [
    "GitIdentityResult",
    "ShellResult",
    "branch_name",
    "decimal_to_json",
    "extract_section",
    "get_active_gh_user",
    "get_logger",
    "inject_rtk_hook",
    "is_rtk_available",
    "iso_utc",
    "remove_rtk_hook",
    "resolve_gh_env",
    "resolve_linked_issue",
    "setup_logging",
    "slugify",
    "strip_code_fences",
    "strip_preamble",
    "subprocess_error",
    "truncate",
]
