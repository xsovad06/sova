"""SOVA: an autonomous AI development crew."""

from typing import Any

__all__ = ["__version__"]


def __getattr__(name: str) -> Any:
    """Resolve ``sova.__version__`` lazily (PEP 562).

    Importing ``sova.utils.version`` eagerly here would make every ``import
    sova`` execute ``sova/utils/__init__.py``, which pulls in structlog, rich
    and pygments (~65ms) that the package root itself needs for nothing. It
    would also make any future module-level ``import sova`` inside that
    subtree a circular-import failure rather than a working import.
    """
    if name == "__version__":
        from sova.utils.version import get_version

        return get_version()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
