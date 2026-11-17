"""One HTTP answer for a project config that cannot be loaded, shared by every route that needs it.

The mapping is by cause, so the same failure reads the same on every endpoint:

- a config that fails validation is a 422 naming the first fields (``Invalid configuration: ...``);
- a missing or unparsable ``sova.toml`` (``FileNotFoundError``, ``ValueError``, ``KeyError``) is a 400 with a fixed
  detail, a problem with the project the operator can fix;
- any other expected failure (``OSError``, ``SQLAlchemyError``: a database or I/O failure) is a 503 with a fixed
  detail.

Every one of these is logged with its traceback before it is translated, and none of their text is returned. An
exception outside that list (an ``AttributeError`` or ``TypeError`` from a bug in the loader) is not caught here:
it reaches the catch-all handler in ``create_app()``, which answers a 500 and logs it as unexpected.

``ValidationError`` subclasses ``ValueError`` in Pydantic v2, so the validation branch must stay ahead of the
``ValueError`` one: an empty ``errors()`` list still answers 422, never the 400 for an unparsable file.
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from sova.config.models import ProjectConfig
from sova.utils.logging import get_logger

log = get_logger(component="dashboard.config_errors")

# The failures a config load is expected to raise; anything else is a bug and goes to the catch-all handler.
# A caller that handles a load failure itself catches this tuple, never a bare ``Exception``.
CONFIG_LOAD_ERRORS = (ValidationError, FileNotFoundError, ValueError, KeyError, OSError, SQLAlchemyError)


def validation_detail(exc: Exception) -> str | None:
    """A readable message for a Pydantic ``ValidationError`` (or an error caused by one), else None."""
    cause = exc if isinstance(exc, ValidationError) else exc.__cause__
    if not isinstance(cause, ValidationError):
        return None
    fields = [f"{'.'.join(str(p) for p in err.get('loc', []))}: {err.get('msg', '')}" for err in cause.errors()]
    return f"Invalid configuration: {'; '.join(fields[:3])}" if fields else None


def config_http_error(exc: Exception, *, event: str) -> HTTPException:
    """The ``HTTPException`` for a config load failure *exc*, after logging it under *event*."""
    log.warning(event, exc_info=True)
    if isinstance(exc, ValidationError) or isinstance(exc.__cause__, ValidationError):
        return HTTPException(status_code=422, detail=validation_detail(exc) or "Invalid configuration")
    if isinstance(exc, FileNotFoundError | ValueError | KeyError):
        return HTTPException(status_code=400, detail="Failed to load project config")
    return HTTPException(status_code=503, detail="Failed to load configuration")


def load_config_or_http(loader: Callable[[], ProjectConfig], *, event: str) -> ProjectConfig:
    """Run *loader* (a no-argument config load), turning a failure into ``config_http_error()``."""
    try:
        return loader()
    except CONFIG_LOAD_ERRORS as exc:
        raise config_http_error(exc, event=event) from exc
