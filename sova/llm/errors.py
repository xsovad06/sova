"""Typed error hierarchy for LLM invocation failures.

Every class subclasses RuntimeError so existing `except RuntimeError` catches
in the step layer (e.g. sova/core/steps/create_pr.py) keep working once
providers start raising these types.

This module is a leaf: it imports nothing from sova so any layer can use it.
"""

from __future__ import annotations


class LLMError(RuntimeError):
    """Base class for typed LLM invocation failures."""


class BillingError(LLMError):
    """Raised when a budget, billing, or quota limit blocks the invocation."""


class ModelUnavailableError(LLMError):
    """Raised when the requested model is not enabled on the deployment."""


class RateLimitError(LLMError):
    """Raised when the provider throttles or is temporarily overloaded."""


class ProviderUnavailableError(LLMError):
    """Raised when the provider backend cannot be reached or invoked."""


class LLMTimeoutError(LLMError):
    """Raised when an invocation exceeds its time budget."""


class LLMInvocationError(LLMError):
    """Raised for failures that do not match a more specific category."""


_BILLING_PATTERNS: tuple[str, ...] = (
    "budget_exhausted",
    "billing",
    "insufficient_quota",
)

# "is not available" covers Vertex AI rejections where the requested model
# version is not enabled on the deployment (e.g. "claude-opus-5 is not
# available on your vertex deployment"). It is deliberately narrower than
# "not available" to avoid false positives on generic unavailability messages
# such as "feature not available in free tier". "not_available" also covers
# the "model_not_available" error code by substring.
_MODEL_UNAVAILABLE_PATTERNS: tuple[str, ...] = (
    "is not available",
    "not_available",
)

# The leading space in " 429" avoids false positives on file paths and port
# numbers that happen to contain those digits.
_RATE_LIMIT_PATTERNS: tuple[str, ...] = (
    "rate_limit",
    "overloaded",
    " 429",
)

# "no such file or directory" is the errno-2 text a failed exec of a missing
# provider binary produces (FileNotFoundError: [Errno 2] No such file or
# directory: 'claude'). It is scoped to invocation-failure details, not
# arbitrary step errors: nothing here feeds is_billing_failure, so a step
# error carrying that text still classifies as non-billing exactly as before.
_PROVIDER_UNAVAILABLE_PATTERNS: tuple[str, ...] = (
    "connection refused",
    "connection error",
    "command not found",
    "no such file or directory",
)

_TIMEOUT_PATTERNS: tuple[str, ...] = (
    "timed out",
    "timeout",
    "deadline exceeded",
)

# Scanned terminal-first: a detail mentioning both an exhausted budget and a
# 429 classifies as terminal rather than triggering a pointless fallback.
_CATEGORIES: tuple[tuple[type[LLMError], tuple[str, ...]], ...] = (
    (BillingError, _BILLING_PATTERNS),
    (ModelUnavailableError, _MODEL_UNAVAILABLE_PATTERNS),
    (RateLimitError, _RATE_LIMIT_PATTERNS),
    (ProviderUnavailableError, _PROVIDER_UNAVAILABLE_PATTERNS),
    (LLMTimeoutError, _TIMEOUT_PATTERNS),
)

# A different model cannot fix an exhausted budget, and an unclassified
# failure gives no reason to expect a retry elsewhere to behave differently.
_FALLBACK_ELIGIBLE: tuple[type[LLMError], ...] = (
    ModelUnavailableError,
    RateLimitError,
    ProviderUnavailableError,
    LLMTimeoutError,
)

# Categories the legacy workflow.py billing check treated as one boolean.
_BILLING_FAILURE_CATEGORIES: tuple[type[LLMError], ...] = (
    BillingError,
    ModelUnavailableError,
    RateLimitError,
)

# SDK exception class names mapped onto the hierarchy. Matched by name rather
# than isinstance because anthropic and litellm are optional extras: importing
# their exception classes here would break partial installs, and LiteLLM
# re-raises upstream provider exceptions that are not anthropic classes at all.
_EXCEPTION_NAMES: dict[str, type[LLMError]] = {
    "APIConnectionError": ProviderUnavailableError,
    "APITimeoutError": LLMTimeoutError,
    "BudgetExceededError": BillingError,
    "ConnectError": ProviderUnavailableError,
    "ConnectionError": ProviderUnavailableError,
    "InternalServerError": ProviderUnavailableError,
    "NotFoundError": ModelUnavailableError,
    "RateLimitError": RateLimitError,
    "ServiceUnavailableError": ProviderUnavailableError,
    "Timeout": LLMTimeoutError,
    "TimeoutError": LLMTimeoutError,
}

# HTTP status codes for SDK exceptions whose class name is not in the table
# above (e.g. a bare anthropic APIStatusError).
_STATUS_CODES: dict[int, type[LLMError]] = {
    402: BillingError,
    404: ModelUnavailableError,
    408: LLMTimeoutError,
    429: RateLimitError,
}


def classify_error(detail: str | None) -> type[LLMError]:
    """Return the error class matching a failure detail string.

    Matching is case-insensitive substring containment, scanned terminal-first
    (billing, then model availability, rate limit, provider availability,
    timeout). Falls back to LLMInvocationError for missing, empty, or unmatched
    input: the contract is "name this failure's category", never "was there a
    failure".
    """
    if not detail:
        return LLMInvocationError
    lower = detail.lower()
    for error_cls, patterns in _CATEGORIES:
        if any(p in lower for p in patterns):
            return error_cls
    return LLMInvocationError


def classify_exception(exc: BaseException) -> type[LLMError]:
    """Return the error class matching a raised provider exception.

    Resolution order: an already-typed LLMError keeps its own class, then the
    exception's class name (walking the MRO so SDK subclasses resolve), then an
    HTTP status_code attribute, then classify_error on the stringified message.
    """
    if isinstance(exc, LLMError):
        return type(exc)

    for cls in type(exc).__mro__:
        mapped = _EXCEPTION_NAMES.get(cls.__name__)
        if mapped is not None:
            return mapped

    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        if status in _STATUS_CODES:
            return _STATUS_CODES[status]
        if status >= 500:
            return ProviderUnavailableError

    return classify_error(str(exc))


def is_fallback_eligible(exc: object) -> bool:
    """Return True if retrying the invocation on a fallback model may help."""
    return isinstance(exc, _FALLBACK_ELIGIBLE)


def is_billing_failure(detail: str | BaseException | None) -> bool:
    """Return True if the failure indicates a billing, rate-limit, or availability failure.

    Accepts either a detail string or a raised exception so the workflow layer
    can classify whichever form it holds without duplicating the category set.
    """
    if isinstance(detail, BaseException):
        return classify_exception(detail) in _BILLING_FAILURE_CATEGORIES
    return classify_error(detail) in _BILLING_FAILURE_CATEGORIES
