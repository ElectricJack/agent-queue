"""Async TypeSafe boundary for smart test selection.

Only :class:`SdkTransport` talks to TypeSafe. Its optional dependency is loaded
when constructed, and all routine tests use :class:`FakeTransport`. An operator
may run a separate live smoke with
``TYPESAFE_API_KEY=… python -m src.test_selection.typesafe --smoke``. It sends
one fixed question with no repository state and prints only model and usage.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import math
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from src.test_selection import reasons
from src.test_selection.questions import CRITERIA, Packing, Request


@dataclass(frozen=True)
class TransportResponse:
    model: str
    answers: dict[str, dict[str, Any]]
    input_tokens: int | None
    output_tokens: int | None


class TransportError(Exception):
    """Sanitized transport failure. ``detail`` never contains a response body."""

    def __init__(self, kind: str, detail: str = "") -> None:
        super().__init__(kind)
        self.kind = kind
        self.detail = detail


class TypeSafeTransport(Protocol):
    async def system_one(
        self, *, model: str, state: dict, questions: dict[str, dict], timeout: float
    ) -> TransportResponse: ...


class SdkTransport:
    """Daemon-owned TypeSafe SDK transport with retries and body logging disabled."""

    def __init__(self, *, api_key: str, base_url: str | None = None) -> None:
        self._api_key = api_key
        self._base_url = base_url
        sdk_logger = logging.getLogger("typesafe_sdk")
        sdk_logger.setLevel(logging.WARNING)
        sdk_logger.propagate = False
        try:
            import typesafe_sdk
        except ImportError:
            self._sdk = None
        else:
            self._sdk = typesafe_sdk
            # The SDK reads TYPESAFE_LOG_LEVEL during import. Keep body logging
            # suppressed even when the daemon environment enables SDK debug.
            sdk_logger.setLevel(logging.WARNING)
            sdk_logger.propagate = False

    async def system_one(
        self, *, model: str, state: dict, questions: dict[str, dict], timeout: float
    ) -> TransportResponse:
        sdk = self._sdk
        if sdk is None:
            raise TransportError("unconfigured", "typesafe-sdk not installed")
        try:
            sdk_questions = {
                key: sdk.Choice(instructions=body["instructions"], criteria=body["criteria"])
                for key, body in questions.items()
            }
            retry = sdk.RetryPolicy(max_retries=0)
            async with sdk.AsyncTypeSafeClient(
                api_key=self._api_key,
                base_url=self._base_url,
                retry=retry,
                timeout=timeout,
            ) as client:
                response = await client.system_one(
                    state, sdk_questions, model=model, timeout=timeout, retry=retry
                )
            answers = {
                key: value.model_dump(mode="python") for key, value in response.answers.items()
            }
            return TransportResponse(
                response.model,
                answers,
                response.usage.input_tokens,
                response.usage.output_tokens,
            )
        except sdk.TypeSafeAPIResponseValidationError as exc:
            raise TransportError("invalid_response") from exc
        except sdk.TypeSafeAPITimeoutError as exc:
            raise TransportError("timeout") from exc
        except sdk.TypeSafeAPIConnectionError as exc:
            raise TransportError("connection") from exc
        except sdk.TypeSafeAuthenticationError as exc:
            raise TransportError("http_401", "401") from exc
        except sdk.TypeSafeUnprocessableEntityError as exc:
            raise TransportError("http_422", "422") from exc
        except sdk.TypeSafeRateLimitError as exc:
            raise TransportError("http_429", "429") from exc
        except sdk.TypeSafeInternalServerError as exc:
            kind = "http_529" if exc.status == 529 else "http_other"
            raise TransportError(kind, str(exc.status)) from exc
        except sdk.TypeSafeAPIError as exc:
            raise TransportError("http_other", str(exc.status)) from exc
        except sdk.TypeSafeError as exc:
            raise TransportError("unconfigured") from exc
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise TransportError("invalid_response") from exc


class FakeTransport:
    """Scripted offline transport. Each call records its arguments in memory."""

    def __init__(self, *responses: TransportResponse | TransportError, delay: float = 0.0) -> None:
        self._responses = iter(responses)
        self.delay = delay
        self.calls: list[dict[str, Any]] = []

    async def system_one(
        self, *, model: str, state: dict, questions: dict[str, dict], timeout: float
    ) -> TransportResponse:
        self.calls.append(
            {"model": model, "state": state, "questions": questions, "timeout": timeout}
        )
        try:
            scripted = next(self._responses)
        except StopIteration as exc:
            raise TransportError("invalid_response") from exc
        if self.delay:
            await asyncio.sleep(self.delay)
        if isinstance(scripted, TransportError):
            raise scripted
        return scripted


@dataclass(frozen=True)
class AreaAnswer:
    area_id: str
    choice: str
    probabilities: dict[str, float]
    confidence: float


def validate_answer(raw: dict, *, tolerance: float = 1e-3) -> str | None:
    """Validate the complete three-option Choice shape and numeric bounds."""
    if not isinstance(raw, dict) or raw.get("type") != "choice":
        return reasons.with_detail(reasons.JEV_INVALID, "type")
    if not isinstance(raw.get("choice"), str) or raw["choice"] not in CRITERIA:
        return reasons.with_detail(reasons.JEV_INVALID, "choice")
    probabilities = raw.get("probabilities")
    if not isinstance(probabilities, dict) or set(probabilities) != set(CRITERIA):
        return reasons.with_detail(reasons.JEV_INVALID, "probabilities")
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0 <= value <= 1
        for value in probabilities.values()
    ):
        return reasons.with_detail(reasons.JEV_INVALID, "probabilities")
    if not math.isclose(sum(probabilities.values()), 1.0, abs_tol=tolerance, rel_tol=0):
        return reasons.with_detail(reasons.JEV_INVALID, "sum")
    confidence = raw.get("confidence")
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not math.isfinite(confidence)
        or not 0 <= confidence <= 1
    ):
        return reasons.with_detail(reasons.JEV_INVALID, "confidence")
    return None


@dataclass(frozen=True)
class JevResult:
    status: str
    answers: dict[str, AreaAnswer]
    requested_model: str
    returned_model: str | None
    reason: str | None
    input_tokens: int
    output_tokens: int
    requests: int
    elapsed_ms: int

    @property
    def complete(self) -> bool:
        return self.status == "ok"


_ERRORS: dict[str, tuple[str, str]] = {
    "http_401": ("unavailable", reasons.FALLBACK_HTTP_401),
    "http_422": ("unavailable", reasons.FALLBACK_HTTP_422),
    "http_429": ("unavailable", reasons.FALLBACK_HTTP_429),
    "http_529": ("unavailable", reasons.FALLBACK_HTTP_529),
    "http_other": ("unavailable", reasons.FALLBACK_HTTP_OTHER),
    "connection": ("unavailable", reasons.FALLBACK_CONNECTION),
    "timeout": ("timeout", reasons.FALLBACK_TIMEOUT),
    "invalid_response": ("invalid", reasons.JEV_INVALID),
    "unconfigured": ("unconfigured", reasons.JEV_UNCONFIGURED),
}


class JevAdapter:
    def __init__(
        self,
        transport: TypeSafeTransport | None,
        *,
        model: str,
        deadline_seconds: float = 2.0,
        concurrency: int = 2,
        tolerance: float = 1e-3,
        clock=time.monotonic,
    ) -> None:
        if deadline_seconds <= 0 or not math.isfinite(deadline_seconds):
            raise ValueError("deadline_seconds must be finite and positive")
        if concurrency < 1:
            raise ValueError("concurrency must be positive")
        if tolerance < 0 or not math.isfinite(tolerance):
            raise ValueError("tolerance must be finite and nonnegative")
        self._transport = transport
        self._model = model
        self._deadline_seconds = deadline_seconds
        self._concurrency = concurrency
        self._tolerance = tolerance
        self._clock = clock

    async def evaluate(self, packing: Packing, *, area_by_key: Mapping[str, str]) -> JevResult:
        started = self._clock()
        answers: dict[str, AreaAnswer] = {}
        returned_model: str | None = None
        input_tokens = output_tokens = requests = 0

        def result(status: str, reason: str | None) -> JevResult:
            return JevResult(
                status,
                answers,
                self._model,
                returned_model,
                reason,
                input_tokens,
                output_tokens,
                requests,
                max(0, int((self._clock() - started) * 1000)),
            )

        if self._transport is None:
            return result("disabled", reasons.JEV_DISABLED)
        if not packing.complete:
            return result("over_budget", packing.reason or reasons.FALLBACK_BUDGET)
        if not packing.requests:
            return result("ok", None)

        semaphore = asyncio.Semaphore(self._concurrency)
        outcomes: list[TransportResponse | TransportError | None] = [None] * len(packing.requests)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._deadline_seconds

        async def call(index: int, request: Request) -> None:
            nonlocal requests
            async with semaphore:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    return
                requests += 1
                try:
                    outcomes[index] = await self._transport.system_one(
                        model=self._model,
                        state=request.state,
                        questions=request.questions,
                        timeout=remaining,
                    )
                except TransportError as exc:
                    outcomes[index] = exc
                except TimeoutError:
                    outcomes[index] = TransportError("timeout")
                except ConnectionError:
                    outcomes[index] = TransportError("connection")
                except Exception:
                    # A malformed or unexpectedly failing transport is never
                    # evidence that can narrow the test set.
                    outcomes[index] = TransportError("invalid_response")

        timed_out = False
        try:
            async with asyncio.timeout(self._deadline_seconds):
                await asyncio.gather(
                    *(call(index, request) for index, request in enumerate(packing.requests))
                )
        except TimeoutError:
            timed_out = True

        first_error: tuple[str, str] | None = None
        invalid_reason: str | None = None
        drift = False
        expected_keys: set[str] = set()
        for request, outcome in zip(packing.requests, outcomes):
            if expected_keys.intersection(request.questions):
                invalid_reason = reasons.with_detail(reasons.JEV_INVALID, "extra")
            expected_keys.update(request.questions)
            if isinstance(outcome, TransportError):
                first_error = first_error or _ERRORS.get(
                    outcome.kind, ("unavailable", reasons.FALLBACK_HTTP_OTHER)
                )
                continue
            if outcome is None:
                continue
            if not isinstance(outcome, TransportResponse) or not isinstance(outcome.model, str):
                invalid_reason = reasons.JEV_INVALID
                continue
            if outcome.model != self._model:
                if not drift:
                    returned_model = outcome.model
                drift = True
                continue
            if returned_model is None:
                returned_model = outcome.model
            if not isinstance(outcome.answers, dict):
                invalid_reason = reasons.JEV_INVALID
                continue
            if not set(outcome.answers) <= set(request.questions):
                invalid_reason = reasons.with_detail(reasons.JEV_INVALID, "extra")
            for key, raw in outcome.answers.items():
                if key not in request.questions:
                    continue
                area_id = area_by_key.get(key)
                if area_id is None or area_id in answers:
                    invalid_reason = reasons.with_detail(reasons.JEV_INVALID, "extra")
                    continue
                reason = validate_answer(raw, tolerance=self._tolerance)
                if reason is not None:
                    invalid_reason = invalid_reason or reason
                    continue
                answers[area_id] = AreaAnswer(
                    area_id,
                    raw["choice"],
                    {name: float(raw["probabilities"][name]) for name in CRITERIA},
                    float(raw["confidence"]),
                )
            for count in (outcome.input_tokens, outcome.output_tokens):
                if count is not None and (
                    isinstance(count, bool) or not isinstance(count, int) or count < 0
                ):
                    invalid_reason = reasons.JEV_INVALID
            if invalid_reason is None:
                input_tokens += outcome.input_tokens or 0
                output_tokens += outcome.output_tokens or 0

        if drift:
            answers.clear()
            return result("model_drift", reasons.JEV_MODEL_DRIFT)
        if invalid_reason is not None:
            return result("invalid", invalid_reason)
        if timed_out:
            return result("timeout", reasons.FALLBACK_TIMEOUT)
        if first_error is not None:
            return result(*first_error)
        if len(answers) != len(expected_keys):
            return result("partial", reasons.JEV_MISSING_ANSWER)
        return result("ok", None)


async def _smoke(model: str) -> int:
    transport = SdkTransport(api_key=os.environ.get("TYPESAFE_API_KEY", ""))
    try:
        response = await transport.system_one(
            model=model,
            state={"change": "A small code change may affect a test area."},
            questions={
                "area_smoke": {
                    "type": "choice",
                    "instructions": "Could this change affect tests?",
                    "criteria": CRITERIA,
                }
            },
            timeout=2.0,
        )
    except TransportError as exc:
        print(f"TypeSafe smoke failed: {exc.kind}")
        return 1
    print(
        f"model={response.model} input_tokens={response.input_tokens} "
        f"output_tokens={response.output_tokens}"
    )
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Optional, operator-run TypeSafe live smoke")
    parser.add_argument("--smoke", action="store_true", required=True)
    parser.add_argument("--model", default="jev-1.13.0")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_smoke(args.model)))
