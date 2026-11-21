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


class ConfigurationError(LLMError):
    """Raised when a static, operator-fixable configuration fact blocks the invocation.

    Distinct from ``BillingError``: nothing about a configuration fault (e.g.
    a runtime with no budget-enforcement capability and no explicit opt-in)
    changes between retries or across a model fallback hop, so classifying it
    as billing or rate-limit would misdirect both the retry loop (which would
    burn attempts re-raising the identical error) and reliability reporting
    (which would log an operator misconfiguration as a billing incident).
    """


class ComponentMisconfiguredError(ValueError):
    """A provider or runtime factory refused its config; the message is author-written and safe to show a user.

    A ``ValueError`` subclass, so existing ``except ValueError`` callers still catch it. The dashboard's Test
    connection buttons pass ``user_message`` through as a 400 detail (what to fix), where any other ``ValueError`` is
    not assumed to be written for a client. Construct it only from author-written text, never from a wrapped
    exception's message: ``user_message`` is the one attribute a route may return.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.user_message = message


class ModelUnavailableError(LLMError):
    """Raised when the requested model is not enabled on the deployment."""


class RateLimitError(LLMError):
    """Raised when the provider throttles or is temporarily overloaded."""


class ProviderUnavailableError(LLMError):
    """Raised when the provider backend cannot be reached or invoked."""


class LLMTimeoutError(LLMError):
    """Raised when an invocation exceeds its time budget."""


class ContentFilteredError(LLMError):
    """Raised when the provider's safety classifier refuses the prompt or the response.

    A block is a verdict of one model's classifier on one prompt, not a fault
    of the prompt itself: the classifiers produce false positives on ordinary
    code (the Claude CLI's own message says so), and another model's
    classifier can accept the same text. That makes it fallback-eligible, but
    resending the identical prompt to the same model is not worth a retry.
    """


class LLMInvocationError(LLMError):
    """Raised for failures that do not match a more specific category."""


_BILLING_PATTERNS: tuple[str, ...] = (
    "budget_exhausted",
    "billing",
    "insufficient_quota",
)

# Scanned before every other category (see _CATEGORIES below): a configuration
# fault message must never be re-classified as billing just because it also
# explains a budget cap by name. Namespaced ("sova_", not a bare
# "configuration_error") because classify_error() is also pointed at arbitrary
# StepResult.error text that can carry a target project's own captured
# test/lint output, and "configuration_error" alone is a generic enough token
# (OAuth/OIDC and various service error codes use it) to appear there and
# falsely cancel every remaining retry for an otherwise-transient failure.
# Only SOVA itself raises this exact token (see CodexRuntime.spawn()).
_CONFIGURATION_PATTERNS: tuple[str, ...] = ("sova_configuration_error",)

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

# Provider wording for a safety-classifier block, kept to the exact error text:
# classify_error() also reads a coding agent's failure detail, which can quote
# the target project's own code and output, so a bare "usage policy",
# "safeguards" or "content_filter" (a plausible identifier) would misfile an
# unrelated failure. "safeguards flagged" is the Claude CLI/API text ("Opus
# 5.5's safeguards flagged this message") and "violating our usage policy" the
# OpenAI moderation rejection ("flagged as potentially violating our usage
# policy"). LiteLLM's own block is matched by class name in _EXCEPTION_NAMES.
_CONTENT_FILTER_PATTERNS: tuple[str, ...] = (
    "safeguards flagged",
    "violating our usage policy",
)

# Scanned terminal-first: a detail mentioning both an exhausted budget and a
# 429 classifies as terminal rather than triggering a pointless fallback.
# ConfigurationError is scanned before BillingError for the same reason.
_CATEGORIES: tuple[tuple[type[LLMError], tuple[str, ...]], ...] = (
    (ConfigurationError, _CONFIGURATION_PATTERNS),
    (BillingError, _BILLING_PATTERNS),
    # Ahead of the transient categories: a block names its cause exactly, while a
    # timeout or rate-limit word in the same detail is incidental.
    (ContentFilteredError, _CONTENT_FILTER_PATTERNS),
    (ModelUnavailableError, _MODEL_UNAVAILABLE_PATTERNS),
    (RateLimitError, _RATE_LIMIT_PATTERNS),
    (ProviderUnavailableError, _PROVIDER_UNAVAILABLE_PATTERNS),
    (LLMTimeoutError, _TIMEOUT_PATTERNS),
)

# A different model cannot fix an exhausted budget, and an unclassified
# failure gives no reason to expect a retry elsewhere to behave differently.
# A content-filter block can: each model runs its own safety classifier.
_FALLBACK_ELIGIBLE: tuple[type[LLMError], ...] = (
    ContentFilteredError,
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
    "ContentPolicyViolationError": ContentFilteredError,
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
    (configuration, then billing, model availability, rate limit, provider
    availability, timeout). Falls back to LLMInvocationError for missing,
    empty, or unmatched input: the contract is "name this failure's category",
    never "was there a failure".
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


def resolve_error_category(exc: object) -> type[LLMError]:
    """Return the category *exc* belongs to.

    A typed error reports its own class. Anything else is classified from its
    message: every provider today still raises a bare RuntimeError
    (sova/llm/providers/*), so type-only matching would file every real
    invocation failure as uncategorized and silently disable the fallback chain.
    Non-exceptions have no message to classify and stay uncategorized.
    """
    if isinstance(exc, LLMError):
        return type(exc)
    if isinstance(exc, Exception):
        return classify_error(str(exc))
    return LLMInvocationError


def is_fallback_eligible(exc: object) -> bool:
    """Return True if retrying the invocation on a fallback model may help."""
    return issubclass(resolve_error_category(exc), _FALLBACK_ELIGIBLE)


def format_fix_llm_failure(exc: BaseException, cycle: int) -> str:
    """Format a fix-loop LLM invocation failure with a distinguishable marker.

    Every step-layer fix loop (develop, validate, monitor_ci) catches its LLM
    invocation with a bare ``except RuntimeError`` and re-stringifies it,
    discarding the exception's type, even though the provider boundary
    already classifies it via classify_exception(). Prefixing "fix_llm_timeout"
    for an LLMTimeoutError keeps a timed-out fix invocation distinguishable
    from a non-timeout fix-LLM failure ("fix_llm_failed") and from the
    unrelated "step_hard_timeout" string produced by the outer step deadline
    (sova/core/workflow.py), so failure-taxonomy consumers can bucket them
    without regex-guessing over free text.
    """
    marker = "fix_llm_timeout" if isinstance(exc, LLMTimeoutError) else "fix_llm_failed"
    return f"{marker} on cycle {cycle}: {exc}"


def is_billing_failure(detail: str | BaseException | None) -> bool:
    """Return True if the failure indicates a billing, rate-limit, or availability failure.

    Accepts either a detail string or a raised exception so the workflow layer
    can classify whichever form it holds without duplicating the category set.
    """
    if isinstance(detail, BaseException):
        return classify_exception(detail) in _BILLING_FAILURE_CATEGORIES
    return classify_error(detail) in _BILLING_FAILURE_CATEGORIES


def is_configuration_failure(detail: str | BaseException | None) -> bool:
    """Return True if the failure is a static configuration fault no retry can fix.

    Accepts either a detail string or a raised exception, mirroring
    ``is_billing_failure()``. The workflow retry loop uses this to stop
    immediately rather than re-raising the identical error once per retry
    attempt or fallback-model hop.
    """
    if isinstance(detail, BaseException):
        return classify_exception(detail) is ConfigurationError
    return classify_error(detail) is ConfigurationError
