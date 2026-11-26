"""Tests for shared helpers defined in tests/conftest.py."""

from __future__ import annotations

import asyncio
import inspect

import pytest

from tests.conftest import closing_stand_in


async def _noop() -> None:
    """A coroutine function to build un-awaited coroutine objects from."""


class _Boom(Exception):
    """Distinct exception type for the raise paths."""


class TestClosingStandIn:
    def test_closes_the_coroutine_and_returns_the_result(self) -> None:
        coro = _noop()
        assert closing_stand_in("done")(coro) == "done"
        assert inspect.getcoroutinestate(coro) == inspect.CORO_CLOSED

    def test_closes_the_coroutine_before_raising(self) -> None:
        coro = _noop()
        with pytest.raises(_Boom):
            closing_stand_in(raises=_Boom)(coro)
        assert inspect.getcoroutinestate(coro) == inspect.CORO_CLOSED

    def test_returns_none_by_default(self) -> None:
        coro = _noop()
        assert closing_stand_in()(coro) is None
        coro.close()

    @pytest.mark.parametrize("argument", [42, "text", None])
    def test_tolerates_a_non_coroutine_argument(self, argument: object) -> None:
        assert closing_stand_in("ok")(argument) == "ok"

    def test_tolerates_a_future_argument(self) -> None:
        loop = asyncio.new_event_loop()
        try:
            future = loop.create_future()
            assert closing_stand_in("ok")(future) == "ok"
            assert not future.done()
        finally:
            loop.close()

    def test_ignores_extra_positional_and_keyword_arguments(self) -> None:
        coro = _noop()
        assert closing_stand_in(7)(coro, 1.5, timeout=3) == 7
        assert inspect.getcoroutinestate(coro) == inspect.CORO_CLOSED

    def test_raises_an_exception_class(self) -> None:
        with pytest.raises(_Boom):
            closing_stand_in(raises=_Boom)(42)

    def test_raises_an_exception_instance(self) -> None:
        error = _Boom("x")
        with pytest.raises(_Boom) as caught:
            closing_stand_in(raises=error)(42)
        assert caught.value is error

    def test_an_instance_is_raised_with_a_fresh_traceback_each_call(self) -> None:
        stand_in = closing_stand_in(raises=_Boom("x"))
        tracebacks = []
        for _ in range(2):
            with pytest.raises(_Boom) as caught:
                stand_in(42)
            tracebacks.append(caught.value.__traceback__)
        # The helper drops the stored traceback, so the second raise does not chain onto the first call's frames.
        frames = [[tb.tb_frame.f_code.co_name for tb in _walk(traceback)] for traceback in tracebacks]
        assert frames[0] == frames[1]


def _walk(traceback: object) -> list:
    """Flatten a traceback chain into a list of traceback entries."""
    entries = []
    while traceback is not None:
        entries.append(traceback)
        traceback = traceback.tb_next  # type: ignore[attr-defined]
    return entries
