"""JSON Schema validation with LLM retry for structured step outputs."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import jsonschema
from jsonschema import ValidationError as JsonSchemaValidationError

from sova.utils.json import extract_json
from sova.utils.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from sova.llm.models import LLMResult

logger = get_logger()


class ValidationError(Exception):
    """Raised when schema validation fails after all retries."""

    def __init__(self, message: str, retry_cost: Decimal = Decimal("0")) -> None:
        super().__init__(message)
        self.retry_cost = retry_cost


def _format_validation_error(error: JsonSchemaValidationError) -> str:
    """Format a jsonschema validation error into a readable message."""
    path = ".".join(str(p) for p in error.path) if error.path else "'root'"
    return f"Validation error at {path}: {error.message}"


def _build_retry_prompt(original_prompt: str, validation_error: str, failed_output: str, max_preview: int = 300) -> str:
    """Build a retry prompt with validation error context."""
    truncated = failed_output[:max_preview]
    if len(failed_output) > max_preview:
        truncated += "..."

    return f"""{original_prompt}

VALIDATION ERROR: Your previous response had a validation error:
{validation_error}

Failed output preview:
{truncated}

Please fix the validation error and return valid JSON."""


def _try_parse_and_validate(raw_text: str, schema: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None, bool]:
    """
    Try to extract JSON from raw text and validate against schema.

    Returns (validated_data, error_message, had_json).
    - validated_data: Parsed object if successful, None otherwise
    - error_message: Error description if failed, None if successful
    - had_json: True if JSON was found (even if invalid), False if no JSON at all
    """
    # Extract JSON from LLM response (handles markdown fences, prose)
    json_str = extract_json(raw_text)
    if not json_str:
        return None, "No JSON found in response", False

    # Parse the extracted JSON string
    try:
        extracted = json.loads(json_str)
    except json.JSONDecodeError as e:
        return None, f"Parse error: {e}", True

    # Validate against schema
    try:
        jsonschema.validate(instance=extracted, schema=schema)
        return extracted, None, True
    except JsonSchemaValidationError as e:
        return None, _format_validation_error(e), True


async def validate_step_output(
    raw_text: str,
    schema: dict[str, Any],
    llm_invoke: Callable[[str], Awaitable[LLMResult]],
    original_prompt: str,
    max_retries: int = 2,
) -> tuple[dict[str, Any], Decimal]:
    """
    Validate LLM output against a JSON Schema, retrying on validation errors.

    Args:
        raw_text: Raw LLM response text (may contain markdown fences, prose)
        schema: JSON Schema to validate against
        llm_invoke: Async function to retry LLM call with error context
        original_prompt: Original prompt for context in retry
        max_retries: Maximum retry attempts (default 2)

    Returns:
        Tuple of (validated_data, total_retry_cost)
        - validated_data: Parsed and validated JSON object
        - total_retry_cost: Accumulated cost from retry attempts (Decimal)

    Raises:
        ValidationError: If validation fails after all retries, includes retry_cost
    """
    retry_cost = Decimal("0")

    # Try the initial output first
    result, error, _ = _try_parse_and_validate(raw_text, schema)
    if result is not None:
        return result, retry_cost

    # Validation failed, retry with error context
    logger.info(f"Initial validation failed: {error}. Starting retry loop (max {max_retries} attempts).")

    for attempt in range(1, max_retries + 1):
        logger.info(f"Retry attempt {attempt}/{max_retries}")

        retry_prompt = _build_retry_prompt(original_prompt, error, raw_text)

        try:
            llm_result = await llm_invoke(retry_prompt)
            retry_cost += llm_result.cost_usd

            # Try to validate the retry result
            result, error, _ = _try_parse_and_validate(llm_result.text, schema)
            if result is not None:
                logger.info(f"Validation succeeded on retry attempt {attempt}")
                return result, retry_cost

            logger.warning(f"Retry {attempt} still invalid: {error}")
            raw_text = llm_result.text  # Update for next retry preview

        except Exception as e:
            logger.error(f"Retry {attempt} failed with exception: {e}", exc_info=True)
            # The exception was raised before we could access llm_result, so only count
            # cost if we got a result first
            raise ValidationError(
                f"LLM retry failed: {e}",
                retry_cost=retry_cost,
            ) from e

    # All retries exhausted
    raise ValidationError(
        f"Validation exhausted after {max_retries} retries. Last error: {error}",
        retry_cost=retry_cost,
    )
