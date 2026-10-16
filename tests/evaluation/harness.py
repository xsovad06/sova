"""Attempt recording and aggregation for the model-evaluation harness.

Pure logic, independent of any provider: ``test_role_step_matrix.py`` wires
this to a real ``LLMProvider`` when run manually with live credentials.
Keeping the two separate means this module's behavior (cost-per-accepted-
result, escalation attribution) is unit-testable with a fake client and never
needs network access or an API key.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Protocol

from sova.llm.models import CostSource, LLMResult, combine_cost_sources
from tests.evaluation.fixtures import EvalFixture


class ModelClient(Protocol):
    async def __call__(self, prompt: str, model_id: str) -> LLMResult: ...


class AcceptFn(Protocol):
    def __call__(self, fixture: EvalFixture, result: LLMResult) -> bool: ...


def accept_on_marker(fixture: EvalFixture, result: LLMResult) -> bool:
    """Default acceptance check: the marker heading, plus any required content.

    A fixture whose own ``accept`` is set bypasses this entirely (see
    ``run_fixture`` below); this is the check used by every other fixture.
    """
    if fixture.accept_marker not in result.text:
        return False
    text_lower = result.text.lower()
    return all(token.lower() in text_lower for token in fixture.required_content)


@dataclass(frozen=True)
class EvalAttempt:
    """One model's attempt at one fixture.

    ``role``/``step`` are always the fixture's own, even for an escalation
    attempt: ``escalated_from_model`` names the model whose rejected attempt
    triggered this one, so a cost report attributes the escalation's spend to
    the step that triggered it rather than inventing a separate
    escalation-model row. Attributing it anywhere else would understate the
    real cost of running that step under this escalation policy, which is the
    figure the evaluation exists to produce.

    ``cost_source`` is carried alongside ``cost_usd`` because a 0.00 figure is
    ambiguous on its own: a subscription-backed Codex turn, a local Ollama
    turn and a provider that simply reported no price all land there. Keeping
    the provenance is what lets a report say "free (subscription)" instead of
    claiming a priced zero.
    """

    role: str
    step: str
    model_id: str
    accepted: bool
    latency_s: float
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal
    cost_source: CostSource = CostSource.UNKNOWN
    escalated_from_model: str | None = None
    # The model id actually served by the provider (``LLMResult.model``),
    # which litellm routinely resolves away from the requested alias. Kept
    # alongside ``model_id`` (the requested id, used for grouping and the
    # escalation policy) so a merged results file can say which concrete
    # model produced a number.
    served_model: str = ""
    # Verbatim response text, so a false accept/reject can be spot-checked
    # after the run ends instead of only by re-paying for the whole matrix.
    response_text: str = ""
    # Set only for a synthetic attempt ``run_matrix``/``run_fixture_with_escalation``
    # records when a provider call raised (e.g. a transient 429 or timeout),
    # so that call's failure is visible in the attempt list rather than
    # aborting the whole (already billed) matrix. ``accepted`` is always
    # False alongside this, so it never contributes a cost to
    # ``cost_per_accepted_result``. Excluded from ``combined_cost_source``'s
    # fold: a call that never completed is a known zero, not an unpriced one,
    # and should not make an otherwise fully-priced group report "unknown".
    error: str | None = None


async def run_fixture(
    fixture: EvalFixture,
    client: ModelClient,
    model_id: str,
    *,
    accept: AcceptFn = accept_on_marker,
) -> EvalAttempt:
    """Run one fixture against one candidate model and record the attempt.

    A fixture's own ``accept`` override (see ``EvalFixture.accept``) takes
    precedence over the ``accept`` passed in here, letting a fixture require
    a stricter check than the default marker/required_content substring test.
    """
    start = time.monotonic()
    result = await client(fixture.prompt, model_id)
    latency_s = time.monotonic() - start
    accept_fn = fixture.accept if fixture.accept is not None else accept
    return EvalAttempt(
        role=fixture.role,
        step=fixture.step,
        model_id=model_id,
        accepted=accept_fn(fixture, result),
        latency_s=latency_s,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        cost_usd=result.cost_usd,
        cost_source=result.cost_source,
        served_model=result.model,
        response_text=result.text,
    )


async def run_fixture_with_escalation(
    fixture: EvalFixture,
    client: ModelClient,
    model_id: str,
    *,
    escalation_model_id: str | None = None,
    accept: AcceptFn = accept_on_marker,
) -> list[EvalAttempt]:
    """Run the primary candidate, then escalate once if it was rejected.

    Returns every attempt made (primary, plus the escalation when one ran),
    so a caller folding these into ``cost_per_accepted_result`` sees the full
    cost of reaching an accepted outcome under this escalation policy.

    A provider exception on the escalation call is caught here, not left to
    propagate: the primary attempt already succeeded and was already billed,
    so letting an escalation failure raise out of this function would discard
    that paid-for record and attribute the whole fixture's failure to the
    wrong model (see ``run_matrix``, which only treats a *primary* failure as
    the fixture failing). The escalation failure is instead recorded as a
    synthetic error attempt tagged with the escalation model id.
    """
    primary = await run_fixture(fixture, client, model_id, accept=accept)
    attempts = [primary]
    if not primary.accepted and escalation_model_id:
        try:
            escalation = await run_fixture(fixture, client, escalation_model_id, accept=accept)
        except Exception as exc:  # noqa: BLE001 (any provider/transport failure becomes a failed attempt)
            attempts.append(
                EvalAttempt(
                    role=fixture.role,
                    step=fixture.step,
                    model_id=escalation_model_id,
                    accepted=False,
                    latency_s=0.0,
                    input_tokens=0,
                    output_tokens=0,
                    cost_usd=Decimal("0"),
                    escalated_from_model=model_id,
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
        else:
            attempts.append(replace(escalation, escalated_from_model=model_id))
    return attempts


def cost_per_accepted_result(attempts: Sequence[EvalAttempt]) -> Decimal | None:
    """(sum of cost across attempts) / (count of accepted outcomes).

    Returns ``None`` when nothing was accepted: a zero would read as "this
    model is free" rather than "no accepted result to divide by" (the
    failure-must-not-look-like-success convention).
    """
    accepted_count = sum(1 for attempt in attempts if attempt.accepted)
    if accepted_count == 0:
        return None
    total_cost = sum((attempt.cost_usd for attempt in attempts), Decimal("0"))
    return total_cost / accepted_count


def combined_cost_source(attempts: Sequence[EvalAttempt]) -> CostSource:
    """Provenance of the summed cost across ``attempts``.

    Folded with the same :func:`sova.llm.models.combine_cost_sources` the cost
    tracker uses, so a total built from even one unpriced attempt reports
    ``UNKNOWN`` rather than presenting a partial sum as a complete price.

    A failed attempt (``error`` set) is excluded from the fold: it never
    completed, so its ``cost_source`` default (``UNKNOWN``) is a known zero,
    not an unpriced one, and should not make an otherwise fully-priced group
    report "unknown".
    """
    source: CostSource | None = None
    for attempt in attempts:
        if attempt.error is not None:
            continue
        source = combine_cost_sources(source, attempt.cost_source)
    return source if source is not None else CostSource.UNKNOWN


def group_by_role_step(attempts: Sequence[EvalAttempt]) -> dict[tuple[str, str], list[EvalAttempt]]:
    """Bucket attempts by (role, step), the grain the results doc reports at."""
    groups: dict[tuple[str, str], list[EvalAttempt]] = {}
    for attempt in attempts:
        groups.setdefault((attempt.role, attempt.step), []).append(attempt)
    return groups


async def run_matrix(
    fixtures: Sequence[EvalFixture],
    client: ModelClient,
    model_id: str,
    *,
    escalation_model_id: str | None = None,
    accept: AcceptFn = accept_on_marker,
    on_attempt: Callable[[EvalAttempt], None] | None = None,
    repeats: int = 1,
) -> list[EvalAttempt]:
    """Run every fixture ``repeats`` times against ``model_id``, escalating on rejection.

    LLM acceptance is nondeterministic, so a single sample per role/step
    cannot rank candidate models on anything but price. ``repeats`` (default
    1) runs each fixture that many times so ``cost_per_accepted_result``
    reflects more than one draw; the per-fixture matrix is still run as
    ``len(fixtures) * repeats`` independent calls, not a single retried call.

    ``on_attempt`` is called with each attempt as soon as it is recorded. A
    live run bills real money per fixture, so a single call's provider error
    (a transient 429, a read timeout) is caught here and recorded as a failed
    ``EvalAttempt`` rather than abandoning every remaining fixture: the
    operator already paid for the fixtures that succeeded and should not have
    to rerun (and re-pay for) them. An escalation call's own failure is
    instead handled inside ``run_fixture_with_escalation``, so it never
    discards an already-billed primary attempt; only a failure of the
    *primary* call surfaces here. This function only re-raises when every
    call failed (a non-empty ``fixtures`` with 100% failures; an empty
    ``fixtures`` never raises, since nothing failed), as a matrix with zero
    usable attempts has nothing left to aggregate.

    A failure inside ``on_attempt`` itself (e.g. an unwritable output path)
    is caught and printed, never propagated: the reporting callback must not
    be able to cost the operator the rest of an already-billed matrix.
    """
    attempts: list[EvalAttempt] = []
    failures = 0
    total_runs = len(fixtures) * repeats
    for fixture in fixtures:
        for _ in range(repeats):
            try:
                fixture_attempts = await run_fixture_with_escalation(
                    fixture,
                    client,
                    model_id,
                    escalation_model_id=escalation_model_id,
                    accept=accept,
                )
            except Exception as exc:  # noqa: BLE001 (any provider/transport failure becomes a failed attempt)
                failures += 1
                fixture_attempts = [
                    EvalAttempt(
                        role=fixture.role,
                        step=fixture.step,
                        model_id=model_id,
                        accepted=False,
                        latency_s=0.0,
                        input_tokens=0,
                        output_tokens=0,
                        cost_usd=Decimal("0"),
                        error=f"{type(exc).__name__}: {exc}",
                    )
                ]
            for attempt in fixture_attempts:
                attempts.append(attempt)
                if on_attempt is not None:
                    try:
                        on_attempt(attempt)
                    except Exception as exc:  # noqa: BLE001 (reporting must never cost the rest of the matrix)
                        print(f"on_attempt callback failed for {attempt.role}/{attempt.step}: {exc}")
    if total_runs and failures == total_runs:
        raise RuntimeError(f"every fixture in the matrix failed ({failures}/{total_runs}); see EvalAttempt.error")
    return attempts


__all__ = [
    "AcceptFn",
    "EvalAttempt",
    "ModelClient",
    "accept_on_marker",
    "combined_cost_source",
    "cost_per_accepted_result",
    "group_by_role_step",
    "run_fixture",
    "run_fixture_with_escalation",
    "run_matrix",
]
