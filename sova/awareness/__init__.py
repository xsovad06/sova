"""Awareness subsystem: provider registry and factory."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sova.awareness.base import AwarenessItem, AwarenessProvider, ItemCategory
from sova.utils.logging import get_logger

if TYPE_CHECKING:
    from sova.config.models import AwarenessConfig

_log = get_logger(component="awareness")

_PROVIDER_REGISTRY: dict[str, type[AwarenessProvider]] = {}


def register_provider(name: str, cls: type[AwarenessProvider]) -> None:
    """Register a provider class under its config name."""
    _PROVIDER_REGISTRY[name] = cls


def create_providers(config: AwarenessConfig) -> list[AwarenessProvider]:
    """Instantiate providers listed in config.providers.

    Providers not found in the registry are logged and skipped.
    """
    providers: list[AwarenessProvider] = []
    for name in config.providers:
        cls = _PROVIDER_REGISTRY.get(name)
        if cls is None:
            _log.warning("unknown_provider", provider=name, available=list(_PROVIDER_REGISTRY))
            continue
        try:
            providers.append(cls(config))
        except Exception:
            _log.exception("provider_init_failed", provider=name)
    return providers


def _auto_register() -> None:
    """Register built-in providers. Called on first import."""
    # Providers self-register when their modules are imported.
    # Import them here so the registry is populated by the time
    # create_providers() is called.  Each provider module calls
    # register_provider() at module scope.
    #
    # Providers that depend on optional dependencies (e.g., google-api-python-client)
    # catch ImportError and skip registration, so the base package always imports cleanly.
    import importlib

    _BUILTIN_PROVIDERS = (
        "sova.awareness.providers.gmail",
        "sova.awareness.providers.gcal",
        "sova.awareness.providers.reminders",
        "sova.awareness.providers.pr_status",
        "sova.awareness.providers.agent_runs",
    )
    for module_name in _BUILTIN_PROVIDERS:
        try:
            importlib.import_module(module_name)
        except ImportError:
            _log.debug("provider_import_skipped", module=module_name)
        except Exception:
            _log.exception("provider_import_failed", module=module_name)


_auto_register()

__all__ = [
    "AwarenessItem",
    "AwarenessProvider",
    "ItemCategory",
    "create_providers",
    "register_provider",
]
