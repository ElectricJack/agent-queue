"""Offline contract checks for the TypeSafe selection boundary."""

from __future__ import annotations

import asyncio
import logging
import math
import sys
from types import SimpleNamespace

import pytest

from src.test_selection.questions import CRITERIA, Packing, Request
from src.test_selection.typesafe import (
    AreaAnswer,
    FakeTransport,
    JevAdapter,
    SdkTransport,
    TransportError,
    TransportResponse,
    validate_answer,
)


def answer(choice="affected", p=(0.9, 0.05, 0.05), conf=0.8):
    return {
        "type": "choice",
        "choice": choice,
        "probabilities": dict(zip(CRITERIA, p)),
        "confidence": conf,
    }


def packing(*keys: str) -> Packing:
    body = {"type": "choice", "instructions": {"area": "x", "question": "y"}, "criteria": CRITERIA}
    return Packing((Request({"changes": []}, {key: body for key in keys}, 10),), True, None)


AREAS = {
    "area_alpha": "alpha",
    "area_beta": "beta",
    "area_gamma": "gamma",
    "area_delta": "delta",
}
MODEL = "jev-1.13.0"


async def test_disabled_and_over_budget_make_no_call():
    disabled = await JevAdapter(None, model=MODEL).evaluate(
        packing("area_alpha"), area_by_key=AREAS
    )
    transport = FakeTransport()
    budget = await JevAdapter(transport, model=MODEL).evaluate(
        Packing((), False, "fallback_budget"), area_by_key=AREAS
    )
    assert (disabled.status, disabled.reason, disabled.requests) == ("disabled", "jev_disabled", 0)
    assert (budget.status, budget.reason, budget.requests) == ("over_budget", "fallback_budget", 0)
    assert transport.calls == []


async def test_multi_area_answers_are_validated_and_keyed_by_area():
    response = TransportResponse(
        MODEL,
        {"area_alpha": answer(), "area_beta": answer("unknown", (0.1, 0.1, 0.8), 0.95)},
        318,
        34,
    )
    transport = FakeTransport(response)
    result = await JevAdapter(transport, model=MODEL).evaluate(
        packing("area_alpha", "area_beta"), area_by_key=AREAS
    )
    assert result.complete and result.status == "ok" and result.returned_model == MODEL
    assert result.answers["beta"] == AreaAnswer(
        "beta", "unknown", {"affected": 0.1, "unaffected": 0.1, "unknown": 0.8}, 0.95
    )
    assert (result.input_tokens, result.output_tokens, result.requests) == (318, 34, 1)
    assert transport.calls[0]["model"] == MODEL
    assert 0 < transport.calls[0]["timeout"] <= 2.0


async def test_missing_and_extra_answers_fall_back():
    missing = await JevAdapter(
        FakeTransport(TransportResponse(MODEL, {"area_alpha": answer()}, 1, 1)), model=MODEL
    ).evaluate(packing("area_alpha", "area_beta"), area_by_key=AREAS)
    extra = await JevAdapter(
        FakeTransport(
            TransportResponse(MODEL, {"area_alpha": answer(), "area_zeta": answer()}, 1, 1)
        ),
        model=MODEL,
    ).evaluate(packing("area_alpha"), area_by_key=AREAS)
    assert (missing.status, missing.reason, set(missing.answers)) == (
        "partial",
        "jev_missing_answer",
        {"alpha"},
    )
    assert (extra.status, extra.reason) == ("invalid", "jev_invalid:extra")


@pytest.mark.parametrize(
    "raw,detail",
    [
        ({**answer(), "type": "noul"}, "type"),
        (answer("maybe"), "choice"),
        (answer([]), "choice"),
        (answer(p=(0.5, 0.5, 0.5)), "sum"),
        (answer(p=(1.2, -0.1, -0.1)), "probabilities"),
        (answer(p=(math.nan, 0.5, 0.5)), "probabilities"),
        ({**answer(), "probabilities": {"affected": 1.0}}, "probabilities"),
        (answer(conf=1.5), "confidence"),
        (answer(p=(True, 0.0, 0.0)), "probabilities"),
        (answer(conf=float("inf")), "confidence"),
    ],
)
def test_invalid_answers_are_named(raw, detail):
    assert validate_answer(raw) == f"jev_invalid:{detail}"
    assert validate_answer(answer()) is None
    assert validate_answer(answer(p=(0.9995, 0.0003, 0.0003))) is None


async def test_model_drift_discards_answers():
    result = await JevAdapter(
        FakeTransport(TransportResponse("jev-1.14.0", {"area_alpha": answer()}, 1, 1)),
        model=MODEL,
    ).evaluate(packing("area_alpha"), area_by_key=AREAS)
    assert (result.status, result.reason, result.answers, result.returned_model) == (
        "model_drift",
        "jev_model_drift",
        {},
        "jev-1.14.0",
    )


@pytest.mark.parametrize(
    "kind,status,reason",
    [
        ("http_401", "unavailable", "fallback_http_401"),
        ("http_422", "unavailable", "fallback_http_422"),
        ("http_429", "unavailable", "fallback_http_429"),
        ("http_529", "unavailable", "fallback_http_529"),
        ("http_other", "unavailable", "fallback_http_other"),
        ("connection", "unavailable", "fallback_connection"),
        ("timeout", "timeout", "fallback_timeout"),
        ("invalid_response", "invalid", "jev_invalid"),
        ("unconfigured", "unconfigured", "jev_unconfigured"),
    ],
)
async def test_transport_failures_become_fallback_reasons(kind, status, reason):
    result = await JevAdapter(FakeTransport(TransportError(kind)), model=MODEL).evaluate(
        packing("area_alpha"), area_by_key=AREAS
    )
    assert (result.status, result.reason, result.requests) == (status, reason, 1)


async def test_total_deadline_cancels_pending_requests_and_keeps_validated_answers():
    fast = FakeTransport(TransportResponse(MODEL, {"area_alpha": answer()}, 1, 1))

    class OneFastThenSlow:
        def __init__(self):
            self.calls = 0
            self.cancelled = False

        async def system_one(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return await fast.system_one(**kwargs)
            try:
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                self.cancelled = True
                raise

    transport = OneFastThenSlow()
    body = packing("area_alpha").requests[0]
    two = Packing(
        (body, Request(body.state, {"area_beta": body.questions["area_alpha"]}, 10)), True, None
    )
    result = await JevAdapter(transport, model=MODEL, deadline_seconds=0.3, concurrency=1).evaluate(
        two, area_by_key=AREAS
    )
    assert (result.status, result.reason) == ("timeout", "fallback_timeout")
    assert set(result.answers) == {"alpha"} and result.elapsed_ms < 1000
    assert transport.cancelled


async def test_concurrency_is_bounded():
    class CountingTransport:
        active = 0
        maximum = 0

        async def system_one(self, **kwargs):
            self.active += 1
            self.maximum = max(self.maximum, self.active)
            try:
                await asyncio.sleep(0.03)
                return TransportResponse(
                    MODEL, {key: answer() for key in kwargs["questions"]}, 1, 1
                )
            finally:
                self.active -= 1

    body = packing("area_alpha").requests[0]
    requests = tuple(
        Request(body.state, {f"area_{key}": body.questions["area_alpha"]}, 10)
        for key in ("alpha", "beta", "gamma", "delta")
    )
    transport = CountingTransport()
    result = await JevAdapter(transport, model=MODEL, concurrency=2).evaluate(
        Packing(requests, True, None), area_by_key=AREAS
    )
    assert transport.maximum == 2 and result.requests == 4 and result.complete


async def test_cancellation_propagates():
    transport = FakeTransport(TransportResponse(MODEL, {"area_alpha": answer()}, 1, 1), delay=1)
    task = asyncio.create_task(
        JevAdapter(transport, model=MODEL, deadline_seconds=5).evaluate(
            packing("area_alpha"), area_by_key=AREAS
        )
    )
    await asyncio.sleep(0.03)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_state_never_reaches_logs(caplog):
    caplog.set_level(logging.DEBUG)
    body = packing("area_alpha").requests[0]
    secret = Packing(
        (Request({"changes": [{"excerpt": "SUPERSECRETDIFF"}]}, body.questions, 10),),
        True,
        None,
    )
    await JevAdapter(FakeTransport(TransportError("http_429")), model=MODEL).evaluate(
        secret, area_by_key=AREAS
    )
    assert "SUPERSECRETDIFF" not in caplog.text


def _fake_sdk(monkeypatch, *, error_name=None, error_status=None):
    """Exercise SdkTransport's SDK boundary without importing or calling the real SDK."""
    captured = {}

    class TypeSafeError(Exception):
        pass

    class TypeSafeAPIError(TypeSafeError):
        def __init__(self, status=500):
            self.status = status

    class TypeSafeAPIResponseValidationError(TypeSafeAPIError):
        pass

    class TypeSafeAPITimeoutError(TypeSafeError):
        pass

    class TypeSafeAPIConnectionError(TypeSafeError):
        pass

    class TypeSafeAuthenticationError(TypeSafeAPIError):
        pass

    class TypeSafeUnprocessableEntityError(TypeSafeAPIError):
        pass

    class TypeSafeRateLimitError(TypeSafeAPIError):
        pass

    class TypeSafeInternalServerError(TypeSafeAPIError):
        pass

    error_types = {
        cls.__name__: cls
        for cls in (
            TypeSafeAPIError,
            TypeSafeAPIResponseValidationError,
            TypeSafeAPITimeoutError,
            TypeSafeAPIConnectionError,
            TypeSafeAuthenticationError,
            TypeSafeUnprocessableEntityError,
            TypeSafeRateLimitError,
            TypeSafeInternalServerError,
        )
    }
    error = (
        error_types[error_name](error_status)
        if error_name is not None and error_status is not None
        else error_types[error_name]()
        if error_name is not None
        else None
    )

    class RetryPolicy:
        def __init__(self, *, max_retries):
            self.max_retries = max_retries

    class Choice:
        def __init__(self, *, instructions, criteria):
            captured["instructions"] = instructions
            captured["criteria"] = criteria

    class AsyncTypeSafeClient:
        def __init__(self, **kwargs):
            captured["client"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def system_one(self, state, questions, **kwargs):
            captured["call"] = (state, questions, kwargs)
            logging.getLogger("typesafe_sdk").debug("request state: %r", state)
            if error is not None:
                raise error
            return SimpleNamespace(
                model=MODEL,
                answers={"area_alpha": SimpleNamespace(model_dump=lambda **_: answer())},
                usage=SimpleNamespace(input_tokens=12, output_tokens=3),
            )

    sdk = SimpleNamespace(
        TypeSafeError=TypeSafeError,
        TypeSafeAPIError=TypeSafeAPIError,
        TypeSafeAPIResponseValidationError=TypeSafeAPIResponseValidationError,
        TypeSafeAPITimeoutError=TypeSafeAPITimeoutError,
        TypeSafeAPIConnectionError=TypeSafeAPIConnectionError,
        TypeSafeAuthenticationError=TypeSafeAuthenticationError,
        TypeSafeUnprocessableEntityError=TypeSafeUnprocessableEntityError,
        TypeSafeRateLimitError=TypeSafeRateLimitError,
        TypeSafeInternalServerError=TypeSafeInternalServerError,
        RetryPolicy=RetryPolicy,
        Choice=Choice,
        AsyncTypeSafeClient=AsyncTypeSafeClient,
    )
    monkeypatch.setitem(sys.modules, "typesafe_sdk", sdk)
    return sdk, captured


async def test_sdk_transport_uses_choices_no_retries_and_does_not_log_bodies(monkeypatch, caplog):
    _, captured = _fake_sdk(monkeypatch)
    caplog.set_level(logging.DEBUG)
    transport = SdkTransport(api_key="test-key", base_url="https://example.invalid")
    body = packing("area_alpha").requests[0]
    response = await transport.system_one(
        model=MODEL,
        state={"secret": "SUPERSECRETDIFF"},
        questions=body.questions,
        timeout=1.25,
    )
    assert response == TransportResponse(MODEL, {"area_alpha": answer()}, 12, 3)
    assert captured["client"]["retry"].max_retries == 0
    assert captured["client"]["timeout"] == 1.25
    assert captured["call"][2]["retry"].max_retries == 0
    assert captured["call"][2]["timeout"] == 1.25
    assert captured["criteria"] == CRITERIA
    assert logging.getLogger("typesafe_sdk").propagate is False
    assert "SUPERSECRETDIFF" not in caplog.text


async def test_missing_sdk_is_unconfigured(monkeypatch):
    monkeypatch.setitem(sys.modules, "typesafe_sdk", None)
    transport = SdkTransport(api_key="test-key")
    body = packing("area_alpha").requests[0]
    with pytest.raises(TransportError) as caught:
        await transport.system_one(
            model=MODEL, state=body.state, questions=body.questions, timeout=1.0
        )
    assert caught.value.kind == "unconfigured"


@pytest.mark.parametrize(
    "exception_name,status,kind",
    [
        ("TypeSafeAuthenticationError", 401, "http_401"),
        ("TypeSafeUnprocessableEntityError", 422, "http_422"),
        ("TypeSafeRateLimitError", 429, "http_429"),
        ("TypeSafeInternalServerError", 529, "http_529"),
        ("TypeSafeInternalServerError", 500, "http_other"),
        ("TypeSafeAPIError", 403, "http_other"),
        ("TypeSafeAPIResponseValidationError", 200, "invalid_response"),
        ("TypeSafeAPITimeoutError", None, "timeout"),
        ("TypeSafeAPIConnectionError", None, "connection"),
    ],
)
async def test_sdk_exception_mapping(monkeypatch, exception_name, status, kind):
    _fake_sdk(monkeypatch, error_name=exception_name, error_status=status)
    transport = SdkTransport(api_key="test-key")
    body = packing("area_alpha").requests[0]
    with pytest.raises(TransportError) as caught:
        await transport.system_one(
            model=MODEL, state=body.state, questions=body.questions, timeout=1.0
        )
    assert caught.value.kind == kind
