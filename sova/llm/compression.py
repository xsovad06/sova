"""Optional Headroom-based prompt compression.

Wraps the optional ``headroom-ai`` package behind an import guard. When the
package is not installed or ``[compression] enabled = false``, ``compress()`` is
an exact identity passthrough. This mirrors the RTK optional-dependency pattern
in ``sova/utils/rtk.py``. Nothing here is wired into the live LLM path yet.
"""

from __future__ import annotations

from sova.config.loader import load_config
from sova.utils.logging import get_logger

try:
    from headroom import compression as headroom_compression  # optional extra (headroom-ai)

    _HEADROOM_AVAILABLE = True
except ImportError:
    _HEADROOM_AVAILABLE = False
    headroom_compression = None

log = get_logger()

# Content-type hints Headroom accepts as strategy values. Unknown content types
# fall back to the "text" strategy rather than raising.
_KNOWN_STRATEGIES = frozenset({"text", "json", "code", "log", "diff"})
_DEFAULT_STRATEGY = "text"


def is_compression_available() -> bool:
    """Return True if the optional ``headroom-ai`` package is importable."""
    return _HEADROOM_AVAILABLE


def compress(text: str, content_type: str = "text") -> str:
    """Compress ``text`` via Headroom when enabled, available, and large enough.

    Returns the input unchanged when the package is missing, compression is
    disabled, the payload is below ``min_chars``, or Headroom raises. Compression
    must never break a caller: all failure paths degrade to an identity passthrough.

    Args:
        text: Input text to compress.
        content_type: Strategy hint, one of "text", "json", "code", "log", "diff".
                      Unknown types fall back to the "text" strategy.

    Returns:
        The compressed text on the happy path, otherwise ``text`` unchanged.
    """
    if not _HEADROOM_AVAILABLE:
        return text

    cfg = load_config()
    if not cfg.compression.enabled:
        return text

    before_chars = len(text)
    if before_chars < cfg.compression.min_chars:
        return text

    strategy = content_type if content_type in _KNOWN_STRATEGIES else _DEFAULT_STRATEGY

    try:
        result = headroom_compression.compress(text, content_type=strategy)
        compressed = result.compressed
    except Exception as exc:
        log.warning("compression.failed", content_type=content_type, error=str(exc), exc_info=True)
        return text

    after_chars = len(compressed)
    log.info(
        "compression.applied",
        content_type=content_type,
        before_chars=before_chars,
        after_chars=after_chars,
        compression_ratio=after_chars / before_chars if before_chars else 1.0,
    )
    return compressed
