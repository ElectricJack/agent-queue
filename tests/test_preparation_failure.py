"""The bounded, scrubbed description every prepare-failure surface records."""

from __future__ import annotations

import asyncio

import pytest

from src.git.manager import GitError
from src.llm.providers.errors import ProviderUnavailableError
from src.preparation_failure import (
    MAX_MESSAGE_CHARS,
    MAX_REASON_CHARS,
    PreparationFailure,
    describe_preparation_failure,
    safe_message,
)


def raised(exc: BaseException, cause: BaseException | None = None) -> BaseException:
    """``exc`` with a real traceback (and ``cause``), as preparation sees it."""
    try:
        if cause is not None:
            raise exc from cause
        raise exc
    except BaseException as caught:  # noqa: BLE001
        return caught


@pytest.mark.parametrize("exc", [TimeoutError(), ValueError("   \n\t "), OSError()])
def test_textless_exception_names_type_and_stage_never_blank(exc):
    failure = describe_preparation_failure(raised(exc), "slot_reset")
    name = type(exc).__qualname__
    assert failure.reason.startswith(f"slot_reset: {name} (no message) at ")
    assert "test_preparation_failure.py:" in failure.reason
    assert failure.error_type == name and failure.stage == "slot_reset"
    assert failure.causes == ()


def test_unraised_textless_exception_has_no_frame_to_guess():
    failure = describe_preparation_failure(TimeoutError(), "stack_fetch")
    assert failure.reason == "stack_fetch: TimeoutError (no message)"
    # Nothing about a missing directory or a lock owner is inferred.
    for guess in ("index.lock", "No such file", "held by", "missing"):
        assert guess not in failure.reason


async def test_asyncio_timeout_reports_the_cancelled_operation():
    async def reset():
        async with asyncio.timeout(0.01):
            await asyncio.sleep(5)

    with pytest.raises(TimeoutError) as caught:
        await reset()
    failure = describe_preparation_failure(caught.value, "slot_reset")
    assert failure.reason.startswith("slot_reset: TimeoutError (no message) at ")
    assert "; caused by CancelledError (no message) at " in failure.reason
    assert failure.causes[0].startswith("caused by CancelledError")
    assert failure.error_type == "TimeoutError"


def test_nested_git_failure_keeps_each_link_in_order():
    inner = raised(TimeoutError())
    middle = raised(GitError("git fetch origin failed"), inner)
    outer = raised(RuntimeError("could not reset slot"), middle)
    failure = describe_preparation_failure(outer, "slot_reset")
    assert failure.reason.startswith(
        "slot_reset: RuntimeError: could not reset slot; "
        "caused by GitError: git fetch origin failed; "
        "caused by TimeoutError (no message) at "
    )
    assert failure.error_type == "RuntimeError"
    assert len(failure.causes) == 2


def test_implicit_context_is_reported_as_during_unless_suppressed():
    try:
        try:
            raise ProviderUnavailableError("provider claude unavailable")
        except ProviderUnavailableError:
            raise KeyError("workspace")  # the implicit context is the subject
    except KeyError as exc:
        failure = describe_preparation_failure(exc, "workspace_lookup")
    assert failure.reason == (
        "workspace_lookup: KeyError: 'workspace'; "
        "during ProviderUnavailableError: provider claude unavailable"
    )
    try:
        try:
            raise OSError("first")
        except OSError:
            raise ValueError("second") from None
    except ValueError as exc:
        assert describe_preparation_failure(exc, "s").reason == "s: ValueError: second"


def test_cause_chain_is_capped_and_cycle_safe():
    a, b = ValueError("a"), ValueError("b")
    a.__cause__, b.__cause__ = b, a
    assert describe_preparation_failure(a, "s").reason == "s: ValueError: a; caused by ValueError: b"
    deep = ValueError("0")
    current = deep
    for index in range(1, 10):
        current.__cause__ = ValueError(str(index))
        current = current.__cause__
    assert len(describe_preparation_failure(deep, "s").causes) == 3


@pytest.mark.parametrize(
    "text, secret",
    [
        ("clone https://x-access-token:ghs_abcdefghijklmnop1234@github.com/o/r", "ghs_abcdef"),
        ("token ghp_abcdefghijklmnopqrstuvwxyz0123456789 rejected", "ghp_abcdef"),
        ("Authorization: Bearer abcdefghijklmnop.qrstuv", "abcdefghijklmnop"),
        ("api_key=supersecretvalue123 refused", "supersecretvalue123"),
        ("password: hunter22hunter", "hunter22hunter"),
        ("key sk-ant-abcdefghijklmnopqrstuvwx rejected", "sk-ant-abcdefghij"),
        (
            (
                "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXk\n"
                "-----END OPENSSH PRIVATE KEY-----"
            ),
            "b3BlbnNzaC1rZXk",
        ),
    ],
)
def test_secrets_never_reach_the_reason(text, secret):
    failure = describe_preparation_failure(
        raised(RuntimeError("outer"), raised(GitError(text))), "stack_fetch"
    )
    assert secret not in failure.reason
    assert secret not in " ".join(failure.causes)
    assert "***" in failure.reason


def test_messages_and_reason_are_bounded():
    assert len(safe_message(ValueError("x" * 10_000))) == MAX_MESSAGE_CHARS
    exc = ValueError("y" * 10_000)
    for _ in range(3):
        exc = raised(ValueError("z" * 10_000), exc)
    failure = describe_preparation_failure(exc, "s" * 3000)
    assert len(failure.reason) <= MAX_REASON_CHARS
    assert failure.reason.endswith("...")


def test_broken_str_falls_back_to_type_without_failing():
    class Opaque(Exception):
        def __str__(self):
            raise RuntimeError("no")

    failure = describe_preparation_failure(Opaque(), "claim_file")
    assert failure.reason == (
        "claim_file: test_broken_str_falls_back_to_type_without_failing.<locals>.Opaque"
        " (no message)"
    )


def test_stage_is_normalized_and_description_is_deterministic():
    exc = raised(TimeoutError())
    first = describe_preparation_failure(exc, " stack fetch ")
    assert first == describe_preparation_failure(exc, "stack fetch")
    assert first.stage == "stack_fetch"
    assert describe_preparation_failure(exc, "").stage == "unknown_stage"
    assert isinstance(first, PreparationFailure)
    assert first.record() == {
        "reason": first.reason, "error_type": "TimeoutError",
        "stage": "stack_fetch", "causes": [],
    }
    assert first.event_fields() == {
        "reason": first.reason, "error_type": "TimeoutError", "stage": "stack_fetch",
    }
