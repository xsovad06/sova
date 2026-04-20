"""Command distribution and adaptation system for SOVA.

Manages canonical command templates, adapts them to target project tech stacks,
and tracks installed vs local commands via a manifest file.
"""

from sova.commands.catalog import classify, discover, get_canonical_dir
from sova.commands.distribution import diff_commands, install_commands, list_commands, update_commands
from sova.commands.manifest import create_manifest, read_manifest
from sova.commands.templates import build_variables, render_command

__all__ = [
    "build_variables",
    "classify",
    "create_manifest",
    "diff_commands",
    "discover",
    "get_canonical_dir",
    "install_commands",
    "list_commands",
    "read_manifest",
    "render_command",
    "update_commands",
]
