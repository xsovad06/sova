"""SOVA dashboard web UI.

Due to circular import constraints with sova.core, symbols are not re-exported
at the package root. Import from submodules directly::

    from sova.dashboard.app import create_app
"""

from __future__ import annotations

__all__: list[str] = []
