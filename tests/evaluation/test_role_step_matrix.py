"""Opt-in evaluation run against a real LLM provider.

Unlike every other test in this repo, this makes real (billable) requests
against whichever provider ``SOVA_EVAL_PROVIDER`` names. It is gated two
ways, mirroring ``tests/runtime/test_codex_smoke.py``'s own real-money test:
the ``evaluation`` marker alone is not enough, because ``make test-all``
collects this file with no ``-m`` deselection. The ``skipif`` below is what
actually keeps it out of every default run: it requires ``SOVA_EVAL_LIVE=1``
plus a candidate model id and provider credentials.

Run locally with: ``make test-evaluation``, after setting
``SOVA_EVAL_MODEL_ID`` (the candidate under test) and, for a second row,
``SOVA_EVAL_ESCALATION_MODEL_ID``, plus the provider credentials
``SOVA_LLM_API_KEY``/``SOVA_EVAL_PROVIDER`` (default ``openai``) resolve to.
``SOVA_EVAL_MODEL_ID`` doubles as the provider config's own ``model``, which
the vendor provider types require non-empty.

This test only produces attempt records for the role/step fixture matrix; it
does not assert anything about model quality (that is nondeterministic and
provider-dependent) or write the checked-in results doc itself; an operator
aggregates several runs' attempts into that doc by hand, using cost per
accepted result (not token price) as the comparison figure, with an
escalation's cost attributed to the step that triggered it. LLM acceptance is
nondeterministic, so a single sample per role/step cannot rank candidate
models on anything but price: set ``SOVA_EVAL_REPEATS`` (default 1) to run
each fixture that many times per invocation, or merge several separate runs'
``SOVA_EVAL_OUTPUT`` files, before treating cost-per-accepted-result as
meaningful.

Each attempt is reported as soon as it is recorded, and ``make
test-evaluation`` passes ``-s`` so that output is not swallowed by pytest's
capture: a passing test shows nothing otherwise (pytest already shows
captured output for a failing or erroring test, so a provider error partway
through is unaffected by this flag).

Requires the ``litellm`` extra (``pip install "sova[litellm]"``): every
``SOVA_EVAL_PROVIDER`` value routes through ``LiteLLMProvider``.

Every ``SOVA_EVAL_*``/``SOVA_LLM_API_KEY`` value is read once, at import time,
into a module constant (see ``_MODEL_ID`` and friends below), never inside the
test body. ``tests/conftest.py``'s autouse ``_isolate_sova_env`` fixture
deletes every ``SOVA_*`` env var before each test body runs (so a developer's
own exported credentials cannot leak into an unrelated test's assertions);
reading at import time, during collection, happens before that fixture's
per-test setup phase, so the operator's real environment is still visible
then.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import json
import os
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from sova.config.models import LLMConfig
from sova.llm.models import LLMResult
from sova.llm.provider import create_provider
from tests.evaluation.fixtures import FIXTURES
from tests.evaluation.harness import (
    EvalAttempt,
    combined_cost_source,
    cost_per_accepted_result,
    group_by_role_step,
    run_matrix,
)

# Exported as data (not re-derived from source text) so
# tests/test_evaluation_harness.py can assert this coupling without parsing
# this module's source.
DEFAULT_EVAL_PROVIDER = "openai"

# Every SOVA_EVAL_*/SOVA_LLM_API_KEY value is read once here, at import time,
# into a module constant, and the test body below uses only these constants,
# never os.environ directly. tests/conftest.py's autouse _isolate_sova_env
# fixture deletes every SOVA_* env var (SOVA_LLM_API_KEY included) before each
# test body runs, but that fixture's setup phase runs per-test, after this
# module has already been imported during collection, so a read here still
# sees the operator's real environment. A read inside the test body would
# instead always see an empty/absent value (KeyError for a required one).
_MODEL_ID = os.environ.get("SOVA_EVAL_MODEL_ID", "")
_ESCALATION_MODEL_ID = os.environ.get("SOVA_EVAL_ESCALATION_MODEL_ID") or None
_PROVIDER = os.environ.get("SOVA_EVAL_PROVIDER", DEFAULT_EVAL_PROVIDER)
_API_KEY = os.environ.get("SOVA_LLM_API_KEY", "")
# Resolved against the real cwd now, before the autouse _isolate_cwd fixture
# chdirs into a pytest tmp dir: a relative SOVA_EVAL_OUTPUT must land where
# the operator pointed it, not inside a directory that is deleted on teardown.
_OUTPUT_PATH = Path(p).resolve() if (p := os.environ.get("SOVA_EVAL_OUTPUT")) else None

# Per-call timeout passed explicitly into provider.invoke() below, so one hung
# turn fails that one fixture rather than silently eating the whole matrix's
# budget (LiteLLMProvider._DEFAULT_TIMEOUT is itself 300s).
_PER_CALL_TIMEOUT_S = float(os.environ.get("SOVA_EVAL_PER_CALL_TIMEOUT_S", "300"))

# How many times to run each fixture per invocation: a single sample per
# role/step cannot rank candidate models on anything but price, since LLM
# acceptance is nondeterministic. Default 1 keeps a single run's cost and
# runtime unchanged; an operator who wants a meaningful cost-per-accepted-
# result either sets this above 1 or merges several separate runs'
# SOVA_EVAL_OUTPUT files.
_REPEATS = int(os.environ.get("SOVA_EVAL_REPEATS", "1"))

pytestmark = [
    pytest.mark.evaluation,
    # Scaled to the matrix size: up to two calls per fixture (primary plus a
    # possible escalation) per repeat, each capped at _PER_CALL_TIMEOUT_S. A
    # flat 300s (copied from tests/runtime/test_codex_smoke.py, which covers a
    # single turn) left pytest-timeout killing the run mid-matrix after real
    # money had already been billed, discarding the summary the test exists to
    # print. Override with SOVA_EVAL_TIMEOUT_S for a non-default matrix size.
    pytest.mark.timeout(
        int(float(os.environ.get("SOVA_EVAL_TIMEOUT_S", str(2 * _PER_CALL_TIMEOUT_S * len(FIXTURES) * _REPEATS))))
    ),
    pytest.mark.skipif(
        os.environ.get("SOVA_EVAL_LIVE") != "1" or not _MODEL_ID,
        reason="opt-in live evaluation: set SOVA_EVAL_LIVE=1 and SOVA_EVAL_MODEL_ID to run",
    ),
    # create_provider() routes openai/ollama/vertex/litellm through
    # LiteLLMProvider, which raises ImportError when the optional litellm
    # extra isn't installed. Without this, a default dev install errors out
    # here instead of skipping cleanly.
    pytest.mark.skipif(
        importlib.util.find_spec("litellm") is None,
        reason='live evaluation requires the litellm extra: pip install "sova[litellm]"',
    ),
]


async def test_candidate_model_against_fixture_matrix() -> None:
    # model= is required, not just a default: LLMConfig rejects the vendor
    # provider types (openai/ollama/vertex, the default among them) with an
    # empty model, so omitting it raises ValueError at config-validation time
    # before any billable turn runs. The per-fixture invoke() below passes its
    # own model= anyway, so this only supplies the provider's default.
    provider = create_provider(
        LLMConfig(
            provider=_PROVIDER,  # type: ignore[arg-type]
            model=_MODEL_ID,
            api_key=_API_KEY,
        )
    )

    async def client(prompt: str, candidate_model_id: str) -> LLMResult:
        return await provider.invoke(prompt, model=candidate_model_id, timeout=_PER_CALL_TIMEOUT_S)

    # Identifies this run in the optional JSON sink below, so attempts from
    # several runs (different candidate models) can be told apart once
    # merged for the checked-in results doc.
    run_id = str(uuid.uuid4())

    # Validated (and created) before any billable call: the sink is otherwise
    # opened lazily on the first attempt, so a bad path (missing parent, full
    # disk, read-only mount) would only surface after the operator had
    # already paid for that attempt.
    if _OUTPUT_PATH is not None:
        _OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        _OUTPUT_PATH.touch(exist_ok=True)

    def report(attempt: EvalAttempt) -> None:
        origin = f" (escalated from {attempt.escalated_from_model})" if attempt.escalated_from_model else ""
        error = f" error={attempt.error}" if attempt.error else ""
        print(
            f"{attempt.role}/{attempt.step} via {attempt.model_id}{origin}: "
            f"accepted={attempt.accepted} latency={attempt.latency_s:.1f}s "
            f"tokens={attempt.input_tokens}/{attempt.output_tokens} "
            f"cost={attempt.cost_usd} ({attempt.cost_source}){error}",
            flush=True,
        )
        # Durable, machine-readable record of every billed attempt: a kill
        # (timeout, Ctrl-C, provider error) still leaves the attempts already
        # paid for on disk, and several runs' records can be aggregated by
        # script instead of copied by hand from terminal output.
        if _OUTPUT_PATH is not None:
            record = {
                **dataclasses.asdict(attempt),
                "run_id": run_id,
                "timestamp": datetime.now(UTC).isoformat(),
            }
            with open(_OUTPUT_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, default=str) + "\n")

    attempts = await run_matrix(
        FIXTURES,
        client,
        _MODEL_ID,
        escalation_model_id=_ESCALATION_MODEL_ID,
        on_attempt=report,
        repeats=_REPEATS,
    )

    assert len(attempts) >= len(FIXTURES) * _REPEATS
    for attempt in attempts:
        assert attempt.cost_usd >= Decimal("0")

    for (role, step), step_attempts in group_by_role_step(attempts).items():
        cost = cost_per_accepted_result(step_attempts)
        models = ", ".join(sorted({a.model_id for a in step_attempts}))
        print(
            f"{role}/{step}: {len(step_attempts)} attempts across [{models}], "
            f"cost per accepted result: {cost} ({combined_cost_source(step_attempts)})",
            flush=True,
        )
