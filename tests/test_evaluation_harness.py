"""Unit tests for tests/evaluation/harness.py: pure logic, no network access.

These exercise the attempt-recording and aggregation behavior the live
evaluation run (tests/evaluation/test_role_step_matrix.py) depends on, using
a fake client so they run in the default suite at zero cost.
"""

from __future__ import annotations

import importlib
from decimal import Decimal

import pytest
from pydantic import ValidationError

from sova.config.models import _VENDOR_MODEL_EXAMPLES, LLMConfig
from sova.llm.models import CostSource, LLMResult
from tests.evaluation.fixtures import EvalFixture
from tests.evaluation.harness import (
    EvalAttempt,
    accept_on_marker,
    combined_cost_source,
    cost_per_accepted_result,
    group_by_role_step,
    run_fixture,
    run_fixture_with_escalation,
    run_matrix,
)

FIXTURE = EvalFixture(
    role="researcher",
    step="research",
    prompt="investigate something",
    accept_marker="## Research",
)


def _result(text: str, cost: str, cost_source: CostSource = CostSource.PRICED, model: str = "irrelevant") -> LLMResult:
    return LLMResult(
        text=text,
        model=model,
        cost_usd=Decimal(cost),
        cost_source=cost_source,
        input_tokens=10,
        output_tokens=5,
    )


def _client(responses: dict[str, LLMResult]):
    async def client(prompt: str, model_id: str) -> LLMResult:
        return responses[model_id]

    return client


class TestAcceptOnMarker:
    def test_marker_present(self) -> None:
        assert accept_on_marker(FIXTURE, _result("## Research\nfindings", "0.01"))

    def test_marker_absent(self) -> None:
        assert not accept_on_marker(FIXTURE, _result("no heading here", "0.01"))

    def test_required_content_must_all_be_present(self) -> None:
        fixture = EvalFixture(
            role="developer",
            step="validate",
            prompt="x",
            accept_marker="## Fix",
            required_content=("StopIteration",),
        )

        assert accept_on_marker(fixture, _result("## Fix\nraises StopIteration on empty input", "0.01"))
        assert not accept_on_marker(fixture, _result("## Fix\nreturns early instead", "0.01"))

    def test_required_content_check_is_case_insensitive(self) -> None:
        fixture = EvalFixture(
            role="triage",
            step="triage",
            prompt="x",
            accept_marker="## Triage",
            required_content=("bug",),
        )

        assert accept_on_marker(fixture, _result("## Triage\nThis is a Bug, priority high", "0.01"))

    def test_marker_present_but_required_content_missing_is_rejected(self) -> None:
        fixture = EvalFixture(
            role="reviewer",
            step="self_review",
            prompt="x",
            accept_marker="## Findings",
            required_content=("ZeroDivisionError",),
        )

        # correctly formatted but names the wrong (or no) bug: must not pass
        # just because the heading is present.
        assert not accept_on_marker(fixture, _result("## Findings\nnone", "0.01"))


class TestFixtureAcceptOverride:
    def test_fixture_accept_override_replaces_the_default_check(self) -> None:
        fixture = EvalFixture(
            role="developer",
            step="develop",
            prompt="x",
            accept_marker="def clamp",
            accept=lambda f, result: "def clamp" in result.text and result.text.count("assert") >= 3,
        )

        three_asserts = "def clamp(v, lo, hi): ...\nassert 1\nassert 2\nassert 3"
        one_assert = "def clamp(v, lo, hi): ...\nassert 1"

        assert fixture.accept(fixture, _result(three_asserts, "0.01"))
        assert not fixture.accept(fixture, _result(one_assert, "0.01"))

    async def test_run_fixture_uses_the_fixture_accept_override(self) -> None:
        fixture = EvalFixture(
            role="developer",
            step="develop",
            prompt="x",
            accept_marker="def clamp",
            accept=lambda f, result: result.text.count("assert") >= 3,
        )
        client = _client({"model-a": _result("def clamp(...): ...\nassert\nassert", "0.01")})

        attempt = await run_fixture(fixture, client, "model-a")

        # the default marker-only check would accept this (the marker is
        # present); the override requires 3+ asserts and rejects it.
        assert not attempt.accepted


class TestRunFixture:
    async def test_records_attempt_fields(self) -> None:
        client = _client({"model-a": _result("## Research\nfindings", "0.05", model="model-a-snapshot-3")})

        attempt = await run_fixture(FIXTURE, client, "model-a")

        assert attempt.role == "researcher"
        assert attempt.step == "research"
        assert attempt.model_id == "model-a"
        assert attempt.accepted
        assert attempt.cost_usd == Decimal("0.05")
        assert attempt.input_tokens == 10
        assert attempt.output_tokens == 5
        assert attempt.escalated_from_model is None
        assert attempt.latency_s >= 0
        assert attempt.cost_source == CostSource.PRICED
        # requested id (used for grouping/escalation) stays "model-a"; the
        # served snapshot and response text are recorded separately so a
        # merged results file can say which concrete model answered.
        assert attempt.served_model == "model-a-snapshot-3"
        assert attempt.response_text == "## Research\nfindings"


class TestRunFixtureWithEscalation:
    async def test_skips_escalation_when_primary_accepted(self) -> None:
        client = _client(
            {
                "model-a": _result("## Research\nfindings", "0.05"),
                "model-b": _result("## Research\nfindings", "0.50"),
            }
        )

        attempts = await run_fixture_with_escalation(FIXTURE, client, "model-a", escalation_model_id="model-b")

        assert len(attempts) == 1
        assert attempts[0].model_id == "model-a"

    async def test_escalates_and_tags_origin_when_primary_rejected(self) -> None:
        client = _client(
            {
                "model-a": _result("no heading", "0.05"),
                "model-b": _result("## Research\nfindings", "0.50"),
            }
        )

        attempts = await run_fixture_with_escalation(FIXTURE, client, "model-a", escalation_model_id="model-b")

        assert len(attempts) == 2
        assert attempts[0].model_id == "model-a"
        assert not attempts[0].accepted
        assert attempts[1].model_id == "model-b"
        assert attempts[1].accepted
        assert attempts[1].escalated_from_model == "model-a"
        # the escalation attempt still belongs to the originating role/step,
        # not a separate "escalation" row, so its cost lands against the step
        # whose rejection paid for it
        assert attempts[1].role == FIXTURE.role
        assert attempts[1].step == FIXTURE.step

    async def test_no_escalation_configured_stops_at_primary(self) -> None:
        client = _client({"model-a": _result("no heading", "0.05")})

        attempts = await run_fixture_with_escalation(FIXTURE, client, "model-a", escalation_model_id=None)

        assert len(attempts) == 1
        assert not attempts[0].accepted

    async def test_escalation_failure_keeps_the_already_billed_primary_attempt(self) -> None:
        # The primary call already succeeded (and was already billed) before
        # the escalation raises; discarding it here would understate cost and
        # misattribute the failure to the primary model.
        async def client(prompt: str, model_id: str) -> LLMResult:
            if model_id == "model-a":
                return _result("no heading", "0.05")
            raise RuntimeError("escalation model rejected the request")

        attempts = await run_fixture_with_escalation(FIXTURE, client, "model-a", escalation_model_id="model-b")

        assert len(attempts) == 2
        primary, escalation = attempts
        assert primary.model_id == "model-a"
        assert not primary.accepted
        assert primary.error is None
        assert primary.cost_usd == Decimal("0.05")
        assert escalation.model_id == "model-b"
        assert escalation.escalated_from_model == "model-a"
        assert not escalation.accepted
        assert escalation.error == "RuntimeError: escalation model rejected the request"
        assert escalation.cost_usd == Decimal("0")
        # the primary's already-billed cost must survive the escalation failure
        assert cost_per_accepted_result(attempts) is None
        assert sum((a.cost_usd for a in attempts), Decimal("0")) == Decimal("0.05")


class TestCostPerAcceptedResult:
    async def test_normal_case(self) -> None:
        client = _client({"model-a": _result("## Research\nfindings", "0.10")})
        attempt = await run_fixture(FIXTURE, client, "model-a")

        assert cost_per_accepted_result([attempt]) == Decimal("0.10")

    async def test_no_accepted_returns_none(self) -> None:
        client = _client({"model-a": _result("no heading", "0.10")})
        attempt = await run_fixture(FIXTURE, client, "model-a")

        assert cost_per_accepted_result([attempt]) is None

    async def test_escalation_cost_counts_against_originating_step(self) -> None:
        client = _client(
            {
                "model-a": _result("no heading", "0.05"),
                "model-b": _result("## Research\nfindings", "0.50"),
            }
        )

        attempts = await run_fixture_with_escalation(FIXTURE, client, "model-a", escalation_model_id="model-b")

        # one accepted outcome, but it cost both the rejected attempt and the
        # escalation to get there
        assert cost_per_accepted_result(attempts) == Decimal("0.55")


class TestCombinedCostSource:
    async def test_all_priced_stays_priced(self) -> None:
        client = _client({"model-a": _result("## Research\nfindings", "0.10")})
        attempt = await run_fixture(FIXTURE, client, "model-a")

        assert combined_cost_source([attempt]) == CostSource.PRICED

    async def test_subscription_run_is_not_a_priced_zero(self) -> None:
        # A subscription-backed turn reports 0.00; without the provenance that
        # is indistinguishable from a priced free call, and the cost report
        # would claim this model costs nothing per accepted result.
        client = _client({"model-a": _result("## Research\nfindings", "0", CostSource.SUBSCRIPTION)})
        attempt = await run_fixture(FIXTURE, client, "model-a")

        assert cost_per_accepted_result([attempt]) == Decimal("0")
        assert combined_cost_source([attempt]) == CostSource.SUBSCRIPTION

    async def test_one_unpriced_attempt_taints_the_total(self) -> None:
        client = _client(
            {
                "model-a": _result("no heading", "0", CostSource.UNKNOWN),
                "model-b": _result("## Research\nfindings", "0.50"),
            }
        )
        attempts = await run_fixture_with_escalation(FIXTURE, client, "model-a", escalation_model_id="model-b")

        assert combined_cost_source(attempts) == CostSource.UNKNOWN

    def test_no_attempts_is_unknown(self) -> None:
        assert combined_cost_source([]) == CostSource.UNKNOWN

    async def test_a_failed_attempt_does_not_taint_an_otherwise_priced_group(self) -> None:
        # A failed call never completed: it is a known zero, not an unpriced
        # one, and must not make an otherwise fully-priced group report
        # "unknown" the way a genuinely unpriced attempt does (see above).
        client = _client({"model-a": _result("## Research\nfindings", "0.10")})
        priced = await run_fixture(FIXTURE, client, "model-a")
        failed = EvalAttempt(
            role=FIXTURE.role,
            step=FIXTURE.step,
            model_id="model-a",
            accepted=False,
            latency_s=0.0,
            input_tokens=0,
            output_tokens=0,
            cost_usd=Decimal("0"),
            error="RuntimeError: boom",
        )

        assert combined_cost_source([priced, failed]) == CostSource.PRICED


class TestGroupByRoleStep:
    async def test_groups_distinct_role_step_pairs(self) -> None:
        other_fixture = EvalFixture(role="developer", step="develop", prompt="x", accept_marker="y")
        client = _client(
            {
                "model-a": _result("## Research\nfindings", "0.01"),
            }
        )
        attempt_a = await run_fixture(FIXTURE, client, "model-a")
        attempt_b = await run_fixture(other_fixture, _client({"model-a": _result("y", "0.02")}), "model-a")

        groups = group_by_role_step([attempt_a, attempt_b])

        assert set(groups) == {("researcher", "research"), ("developer", "develop")}
        assert groups[("researcher", "research")] == [attempt_a]


class TestRunMatrix:
    async def test_runs_every_fixture_once(self) -> None:
        fixtures = [FIXTURE]
        client = _client({"model-a": _result("## Research\nfindings", "0.01")})

        attempts = await run_matrix(fixtures, client, "model-a")

        assert len(attempts) == 1
        assert attempts[0].role == "researcher"

    async def test_on_attempt_fires_per_attempt(self) -> None:
        client = _client({"model-a": _result("## Research\nfindings", "0.01")})
        seen: list[str] = []

        await run_matrix([FIXTURE, FIXTURE], client, "model-a", on_attempt=lambda a: seen.append(a.model_id))

        assert seen == ["model-a", "model-a"]

    async def test_continues_past_a_provider_error_and_records_it_as_a_failed_attempt(self) -> None:
        # A live run bills per fixture: a failure on one must not discard the
        # record of another, already-paid-for attempt, nor abort the run.
        other = EvalFixture(role="developer", step="develop", prompt="x", accept_marker="y")

        async def client(prompt: str, model_id: str):
            if prompt == "x":
                raise RuntimeError("provider rejected the request")
            return _result("## Research\nfindings", "0.01")

        seen: list[str] = []
        attempts = await run_matrix([FIXTURE, other], client, "model-a", on_attempt=lambda a: seen.append(a.step))

        assert seen == ["research", "develop"]
        assert len(attempts) == 2
        failed = attempts[1]
        assert failed.role == "developer"
        assert failed.step == "develop"
        assert not failed.accepted
        assert failed.error == "RuntimeError: provider rejected the request"
        # a failed attempt never contributes a cost to cost_per_accepted_result
        assert failed.cost_usd == Decimal("0")

    async def test_reraises_when_every_fixture_fails(self) -> None:
        async def client(prompt: str, model_id: str):
            raise RuntimeError("provider is down")

        with pytest.raises(RuntimeError, match="every fixture"):
            await run_matrix([FIXTURE], client, "model-a")

    async def test_empty_fixture_list_returns_empty_without_raising(self) -> None:
        # 0 failures out of 0 fixtures must not satisfy "every fixture failed":
        # a caller re-running only the rows a killed run never reached can
        # legitimately pass an empty list.
        async def client(prompt: str, model_id: str):
            raise AssertionError("must not be called")

        assert await run_matrix([], client, "model-a") == []

    async def test_on_attempt_failure_does_not_abort_the_matrix(self) -> None:
        # A live run bills per fixture; a broken reporting callback (e.g. an
        # unwritable output path) must not cost the operator the remaining,
        # already-billed fixtures.
        client = _client({"model-a": _result("## Research\nfindings", "0.01")})

        def broken_report(attempt) -> None:
            raise OSError("disk full")

        attempts = await run_matrix([FIXTURE, FIXTURE], client, "model-a", on_attempt=broken_report)

        assert len(attempts) == 2

    async def test_repeats_runs_each_fixture_that_many_times(self) -> None:
        client = _client({"model-a": _result("## Research\nfindings", "0.01")})

        attempts = await run_matrix([FIXTURE], client, "model-a", repeats=3)

        assert len(attempts) == 3
        assert all(a.role == "researcher" for a in attempts)

    async def test_reraises_only_when_every_repeated_run_fails(self) -> None:
        async def client(prompt: str, model_id: str):
            raise RuntimeError("provider is down")

        # 2 of 2*2=4 runs failing must not trip "every run failed"; only a
        # full 4/4 failure should.
        with pytest.raises(RuntimeError, match="every fixture"):
            await run_matrix([FIXTURE], client, "model-a", repeats=2)

    async def test_escalation_failure_does_not_count_as_a_fixture_failure(self) -> None:
        # Only the *primary* call failing should count toward "every fixture
        # failed": an escalation failure is already captured as its own
        # attempt by run_fixture_with_escalation and must not also trip the
        # matrix-wide re-raise.
        async def client(prompt: str, model_id: str) -> LLMResult:
            if model_id == "model-a":
                return _result("no heading", "0.05")
            raise RuntimeError("escalation model rejected the request")

        attempts = await run_matrix([FIXTURE], client, "model-a", escalation_model_id="model-b")

        assert len(attempts) == 2
        assert attempts[0].error is None
        assert attempts[1].error == "RuntimeError: escalation model rejected the request"


class TestLiveRunProviderConfig:
    """Guards the live run's own LLMConfig shape without making a billable call.

    ``tests/evaluation/test_role_step_matrix.py`` only ever runs with
    ``SOVA_EVAL_LIVE=1`` and real credentials, so a config-validation mistake
    there is invisible to CI and surfaces only when an operator spends money.
    These rebuild the same config the live test builds and assert it validates.
    """

    def _config(self, provider: str, model: str) -> LLMConfig:
        return LLMConfig(provider=provider, model=model, api_key="")  # type: ignore[arg-type]

    @pytest.mark.parametrize("provider", ["openai", "ollama", "vertex"])
    def test_vendor_providers_validate_with_the_candidate_model_id(self, provider: str) -> None:
        cfg = self._config(provider, "candidate-model-id")

        assert cfg.model == "candidate-model-id"

    @pytest.mark.parametrize("provider", ["openai", "ollama", "vertex"])
    def test_vendor_providers_reject_an_empty_model(self, provider: str) -> None:
        # Why the live test must pass model=: these provider types fail fast at
        # config-validation time rather than guessing a paid default.
        with pytest.raises(ValidationError, match="requires an explicit llm.model"):
            self._config(provider, "")

    def test_default_eval_provider_is_one_that_requires_a_model(self) -> None:
        # Pins the coupling: if SOVA_EVAL_PROVIDER's default ever changes to a
        # provider with no model requirement, the parametrized guards above
        # stop covering the path the live test actually takes. Asserts on the
        # exported constant (data), not source text, so a reformat or rename
        # elsewhere in the module can't break this for the wrong reason.
        from tests.evaluation.test_role_step_matrix import DEFAULT_EVAL_PROVIDER

        assert DEFAULT_EVAL_PROVIDER in _VENDOR_MODEL_EXAMPLES


class TestLiveTestEnvConstantsSurviveIsolation:
    """Regression guard for tests/conftest.py's autouse env-stripping fixture.

    ``test_role_step_matrix.py`` used to read ``os.environ["SOVA_EVAL_MODEL_ID"]``
    (and friends) directly inside the test body, which always raised
    ``KeyError``: the autouse ``_isolate_sova_env`` fixture deletes every
    ``SOVA_*`` env var before any test body runs. The fix reads each value once
    into a module constant at import time, during collection, before that
    fixture's per-test setup phase. Reloading the module after setting these
    env vars simulates a fresh import and proves the constants actually come
    from the environment, so a regression back to an in-body ``os.environ``
    read would be caught here rather than only when an operator with real
    credentials runs ``make test-evaluation``.
    """

    _EVAL_ENV_KEYS = (
        "SOVA_EVAL_MODEL_ID",
        "SOVA_EVAL_ESCALATION_MODEL_ID",
        "SOVA_EVAL_PROVIDER",
        "SOVA_LLM_API_KEY",
        "SOVA_EVAL_OUTPUT",
    )

    def test_module_constants_reflect_env_present_at_import(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import tests.evaluation.test_role_step_matrix as mod

        monkeypatch.setenv("SOVA_EVAL_MODEL_ID", "candidate-model")
        monkeypatch.setenv("SOVA_EVAL_ESCALATION_MODEL_ID", "escalation-model")
        monkeypatch.setenv("SOVA_EVAL_PROVIDER", "ollama")
        monkeypatch.setenv("SOVA_LLM_API_KEY", "secret-key")
        monkeypatch.setenv("SOVA_EVAL_OUTPUT", "attempts.jsonl")
        try:
            importlib.reload(mod)
            assert mod._MODEL_ID == "candidate-model"
            assert mod._ESCALATION_MODEL_ID == "escalation-model"
            assert mod._PROVIDER == "ollama"
            assert mod._API_KEY == "secret-key"
            assert mod._OUTPUT_PATH is not None
            assert mod._OUTPUT_PATH.name == "attempts.jsonl"
        finally:
            # Module objects are cached process-wide; leaving the reloaded
            # (env-present) constants in place would leak into whichever test
            # imports this module next, so restore the normal (env-absent)
            # state before returning. monkeypatch is a single function-scoped
            # fixture instance shared with the autouse _isolate_sova_env,
            # _isolate_cwd, and _isolate_home_config fixtures, so a bare
            # monkeypatch.undo() here would also tear down their patches
            # (real cwd, real HOME) for the rest of this test's body. Instead,
            # clear only the five keys this test itself set, leaving the
            # isolation fixtures' own patches intact.
            for key in self._EVAL_ENV_KEYS:
                monkeypatch.delenv(key, raising=False)
            importlib.reload(mod)
            assert mod._MODEL_ID == ""

    def test_absent_env_yields_empty_defaults_not_a_crash(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import tests.evaluation.test_role_step_matrix as mod

        for key in self._EVAL_ENV_KEYS:
            monkeypatch.delenv(key, raising=False)
        try:
            importlib.reload(mod)
            assert mod._MODEL_ID == ""
            assert mod._ESCALATION_MODEL_ID is None
            assert mod._PROVIDER == mod.DEFAULT_EVAL_PROVIDER
            assert mod._API_KEY == ""
            assert mod._OUTPUT_PATH is None
        finally:
            # Same reasoning as above: monkeypatch.undo() here would also
            # restore whatever the autouse _isolate_sova_env fixture's own
            # delenv calls are tracking for teardown, re-exposing the
            # operator's real environment to this reload.
            for key in self._EVAL_ENV_KEYS:
                monkeypatch.delenv(key, raising=False)
            importlib.reload(mod)
            assert mod._MODEL_ID == ""
