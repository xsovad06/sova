"""Command distribution and adaptation system for SOVA.

Manages canonical command templates, adapts them to target project tech stacks,
and tracks installed vs local commands via a manifest file.
"""

from sova.commands.catalog import classify, discover, get_canonical_dir, get_guidelines_dir
from sova.commands.distribution import (
    diff_commands,
    diff_skills,
    install_commands,
    install_guidelines,
    list_commands,
    reverse_diff_commands,
    reverse_diff_guidelines,
    reverse_diff_skills,
    update_commands,
    update_guidelines,
)
from sova.commands.manifest import create_manifest, read_manifest
from sova.commands.templates import build_variables, render_command, reverse_render

__all__ = [
    "build_variables",
    "classify",
    "create_manifest",
    "diff_commands",
    "diff_skills",
    "discover",
    "get_canonical_dir",
    "get_guidelines_dir",
    "install_commands",
    "install_guidelines",
    "list_commands",
    "read_manifest",
    "render_command",
    "reverse_diff_commands",
    "reverse_diff_guidelines",
    "reverse_diff_skills",
    "reverse_render",
    "update_commands",
    "update_guidelines",
]
