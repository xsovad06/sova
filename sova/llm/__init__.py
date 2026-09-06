"""LLM interaction layer for SOVA."""

from __future__ import annotations

from sova.llm.client import (
    get_provider,
    invoke,
    invoke_batch,
    invoke_command,
    invoke_streaming,
    reset_provider,
    resolve_model,
    set_provider,
)
from sova.llm.complexity import ComplexityTier, assess_complexity
from sova.llm.cost import record_cost
from sova.llm.errors import (
    BillingError,
    LLMError,
    LLMInvocationError,
    LLMTimeoutError,
    ModelUnavailableError,
    ProviderUnavailableError,
    RateLimitError,
    classify_error,
    classify_exception,
    is_fallback_eligible,
    resolve_error_category,
)
from sova.llm.guard import PromptInjectionError, ScanResult, scan_prompt
from sova.llm.models import BatchRequest, BatchResult, BatchTimeoutError, LLMResult, StreamEvent
from sova.llm.provider import LLMProvider, create_provider
from sova.llm.routing import TASK_TYPE_KEYS, route_model

__all__ = [
    "BatchRequest",
    "BatchResult",
    "BatchTimeoutError",
    "BillingError",
    "ComplexityTier",
    "LLMError",
    "LLMInvocationError",
    "LLMProvider",
    "LLMResult",
    "LLMTimeoutError",
    "ModelUnavailableError",
    "PromptInjectionError",
    "ProviderUnavailableError",
    "RateLimitError",
    "ScanResult",
    "StreamEvent",
    "TASK_TYPE_KEYS",
    "assess_complexity",
    "classify_error",
    "classify_exception",
    "create_provider",
    "get_provider",
    "invoke",
    "invoke_batch",
    "invoke_command",
    "invoke_streaming",
    "is_fallback_eligible",
    "record_cost",
    "reset_provider",
    "resolve_error_category",
    "resolve_model",
    "route_model",
    "scan_prompt",
    "set_provider",
]
