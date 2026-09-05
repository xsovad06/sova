"""Tests for the typed LLM error hierarchy and classifier."""

from __future__ import annotations

import pytest

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
    is_billing_failure,
    is_fallback_eligible,
)

_ALL_ERRORS = (
    LLMError,
    BillingError,
    ModelUnavailableError,
    RateLimitError,
    ProviderUnavailableError,
    LLMTimeoutError,
    LLMInvocationError,
)


class TestHierarchy:
    @pytest.mark.parametrize("cls", _ALL_ERRORS)
    def test_subclasses_runtime_error(self, cls: type[Exception]) -> None:
        assert issubclass(cls, RuntimeError)

    @pytest.mark.parametrize("cls", _ALL_ERRORS[1:])
    def test_subclasses_llm_error(self, cls: type[Exception]) -> None:
        assert issubclass(cls, LLMError)

    def test_caught_by_bare_runtime_error(self) -> None:
        with pytest.raises(RuntimeError):
            raise ModelUnavailableError("claude-opus-5 is not available")


class TestClassifyError:
    @pytest.mark.parametrize(
        "detail",
        [
            "budget_exhausted",
            "terminal_reason=budget_exhausted",
            "billing account suspended",
            "insufficient_quota",
        ],
    )
    def test_billing(self, detail: str) -> None:
        assert classify_error(detail) is BillingError

    @pytest.mark.parametrize(
        "detail",
        [
            "The model claude-opus-5 is not available on your vertex deployment",
            "model_not_available: claude-opus-5",
            "not_available",
        ],
    )
    def test_model_unavailable(self, detail: str) -> None:
        assert classify_error(detail) is ModelUnavailableError

    @pytest.mark.parametrize(
        "detail", ["rate_limit exceeded", "API overloaded, try again", "HTTP 429 Too Many Requests"]
    )
    def test_rate_limit(self, detail: str) -> None:
        assert classify_error(detail) is RateLimitError

    @pytest.mark.parametrize(
        "detail",
        [
            "connection refused",
            "connection error to endpoint",
            "claude: command not found",
            "no such file or directory",
        ],
    )
    def test_provider_unavailable(self, detail: str) -> None:
        assert classify_error(detail) is ProviderUnavailableError

    @pytest.mark.parametrize("detail", ["request timed out", "step_hard_timeout", "deadline exceeded"])
    def test_timeout(self, detail: str) -> None:
        assert classify_error(detail) is LLMTimeoutError

    @pytest.mark.parametrize(
        "detail",
        [
            "TypeError: cannot iterate",
            "test assertion failed: expected 3 got 4",
            "ruff check failed with 3 errors",
            "feature not available in free tier",
            "",
        ],
    )
    def test_unknown_defaults_to_invocation(self, detail: str) -> None:
        assert classify_error(detail) is LLMInvocationError

    def test_missing_provider_binary_is_provider_unavailable(self) -> None:
        """The errno-2 text of a failed exec means the provider CLI is absent, not an unknown failure."""
        detail = "FileNotFoundError: [Errno 2] No such file or directory: 'claude'"
        assert classify_error(detail) is ProviderUnavailableError

    def test_none_defaults_to_invocation(self) -> None:
        assert classify_error(None) is LLMInvocationError

    def test_case_insensitive(self) -> None:
        assert classify_error("Model is Not Available on this region") is ModelUnavailableError

    def test_429_requires_leading_space(self) -> None:
        assert classify_error("HTTP 429 Too Many Requests") is RateLimitError
        assert classify_error("failed writing /var/logs/4291/run.txt") is LLMInvocationError

    def test_terminal_first_scan_order(self) -> None:
        assert classify_error("budget_exhausted after HTTP 429 Too Many Requests") is BillingError


class TestIsFallbackEligible:
    @pytest.mark.parametrize("cls", [ModelUnavailableError, RateLimitError, ProviderUnavailableError, LLMTimeoutError])
    def test_eligible(self, cls: type[LLMError]) -> None:
        assert is_fallback_eligible(cls("boom"))

    @pytest.mark.parametrize("cls", [LLMError, BillingError, LLMInvocationError])
    def test_not_eligible(self, cls: type[LLMError]) -> None:
        assert not is_fallback_eligible(cls("boom"))

    def test_non_llm_error_is_not_eligible(self) -> None:
        assert not is_fallback_eligible(RuntimeError("boom"))

    def test_non_exception_is_not_eligible(self) -> None:
        assert not is_fallback_eligible("rate_limit")


class TestIsBillingFailure:
    @pytest.mark.parametrize(
        "detail",
        [
            "budget_exhausted",
            "billing error",
            "rate_limit exceeded",
            "overloaded",
            "insufficient_quota",
            "HTTP 429 Too Many Requests",
            "claude-opus-5 is not available",
            "model_not_available",
            "not_available",
        ],
    )
    def test_legacy_union_matches(self, detail: str) -> None:
        assert is_billing_failure(detail)

    @pytest.mark.parametrize(
        "detail",
        [
            "TypeError: cannot iterate",
            "FileNotFoundError: no such file",
            "step_hard_timeout",
            "connection refused",
            "feature not available in free tier",
            "",
            None,
        ],
    )
    def test_non_billing_categories_excluded(self, detail: str | None) -> None:
        assert not is_billing_failure(detail)


def _sdk_exc(name: str, message: str, **attrs: object) -> Exception:
    """Build a stand-in for an optional-SDK exception with the given class name.

    classify_exception matches on the class name rather than isinstance, so the
    stubs only need the right name. That is the same reason the real anthropic
    and litellm classes never have to be importable here.
    """
    exc = type(name, (Exception,), {})(message)
    for key, value in attrs.items():
        setattr(exc, key, value)
    return exc


class _VendorConnectionError(ConnectionError):
    """Stand-in for a provider exception deriving from a known base class."""


class TestClassifyException:
    @pytest.mark.parametrize(
        ("exc", "expected"),
        [
            (_sdk_exc("RateLimitError", "slow down"), RateLimitError),
            (_sdk_exc("APITimeoutError", "request timed out"), LLMTimeoutError),
            (_sdk_exc("BudgetExceededError", "over cap"), BillingError),
            (_sdk_exc("NotFoundError", "unknown model"), ModelUnavailableError),
            (_sdk_exc("APIConnectionError", "unreachable"), ProviderUnavailableError),
            (TimeoutError("deadline"), LLMTimeoutError),
            (ConnectionRefusedError("no listener"), ProviderUnavailableError),
            (_VendorConnectionError("socket closed"), ProviderUnavailableError),
        ],
    )
    def test_maps_by_exception_name(self, exc: BaseException, expected: type[LLMError]) -> None:
        assert classify_exception(exc) is expected

    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            (402, BillingError),
            (404, ModelUnavailableError),
            (408, LLMTimeoutError),
            (429, RateLimitError),
            (503, ProviderUnavailableError),
        ],
    )
    def test_maps_by_status_code(self, status: int, expected: type[LLMError]) -> None:
        exc = _sdk_exc("APIStatusError", "upstream said no", status_code=status)
        assert classify_exception(exc) is expected

    def test_unmapped_status_code_falls_through_to_message(self) -> None:
        exc = _sdk_exc("APIStatusError", "billing account suspended", status_code=400)
        assert classify_exception(exc) is BillingError

    def test_falls_back_to_message_classification(self) -> None:
        assert classify_exception(RuntimeError("claude-opus-5 is not available")) is ModelUnavailableError

    def test_unknown_exception_is_invocation_error(self) -> None:
        assert classify_exception(ValueError("cannot parse")) is LLMInvocationError

    def test_already_typed_error_keeps_its_class(self) -> None:
        assert classify_exception(BillingError("budget_exhausted")) is BillingError

    def test_exception_name_wins_over_message(self) -> None:
        """A typed rate limit stays a rate limit even when the text mentions billing."""
        assert classify_exception(_sdk_exc("RateLimitError", "billing tier throttle")) is RateLimitError


class TestIsBillingFailureExceptions:
    @pytest.mark.parametrize(
        "exc",
        [
            BillingError("budget_exhausted"),
            ModelUnavailableError("model is not available"),
            RateLimitError("slow down"),
            _sdk_exc("RateLimitError", "slow down"),
            _sdk_exc("APIStatusError", "upstream said no", status_code=429),
        ],
    )
    def test_billing_categories_detected(self, exc: BaseException) -> None:
        assert is_billing_failure(exc)

    @pytest.mark.parametrize(
        "exc",
        [
            LLMInvocationError("cannot parse"),
            ProviderUnavailableError("connection refused"),
            LLMTimeoutError("timed out"),
            ConnectionRefusedError("no listener"),
            RuntimeError("test assertion failed"),
        ],
    )
    def test_non_billing_categories_excluded(self, exc: BaseException) -> None:
        assert not is_billing_failure(exc)
