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
