"""Pure typed predicates and completion/deadline arbitration."""

import math

import pytest
from pydantic import ValidationError

from src.agent_waits import ProducerObservation, WaitError, deadline_for, resolve_wait, typed_match


@pytest.mark.parametrize("kind,match,command", [
    ("job", {"job_id": "validation"}, "aq job show validation --json"),
    ("task", {"task_id": "review"}, "aq task show review --json"),
    ("message", {"thread_id": "reply", "after_seq": 1}, "recipient and thread cursor"),
    ("timer", {"due_at": 50}, "timer due instant"),
])
def test_expiry_returns_an_action_without_repeating_work(kind, match, command):
    from src.agent_waits import wait_next_step

    result = wait_next_step(dict(
        id="w", owner_kind="task", kind=kind, match=match, state="expired",
        digest={"reason": "deadline_expired"},
    ))
    assert command in result and "once" in result
    assert "producer was not cancelled" in result
    assert "Do not infer success" in result and "submit replacement work" in result


@pytest.mark.parametrize(
    "completed,now,state",
    [
        (99, 120, "satisfied"),
        (100, 120, "satisfied"),
        (101, 120, "expired"),
        (None, 100, "expired"),
        (None, 99, None),
        (101, 99, None),
    ],
)
def test_deadline_arbitration(completed, now, state):
    observation = ProducerObservation(
        completed_at=completed, result_ref="task:t", digest={"outcome": "fail"}
    )
    resolution = resolve_wait(observation, deadline=100, now=now)
    assert (resolution.state if resolution else None) == state
    if state == "satisfied":
        assert resolution.digest["outcome"] == "fail"


def test_missing_source_is_an_explicit_result():
    result = resolve_wait(ProducerObservation(available=False), deadline=100, now=99)
    assert result.state == "satisfied"
    assert result.digest == {"reason": "source_unavailable"}


@pytest.mark.parametrize("seconds", [0, -1, 86401, math.inf, math.nan, True])
def test_deadline_is_positive_finite_and_capped(seconds):
    with pytest.raises(WaitError):
        deadline_for(10, seconds, {})


def test_defaults_and_timer_bound():
    assert deadline_for(10, None, {}) == 7210
    assert deadline_for(10, 86400, {"due_at": 86410}) == 86410
    with pytest.raises(WaitError):
        deadline_for(10, 20, {"due_at": 31})


@pytest.mark.parametrize(
    "kind,ref,seq,due",
    [
        ("task", "t", 1, None),
        ("timer", "t", None, 10),
        ("message", "thread", None, None),
        ("message", "thread", -1, None),
        ("timer", None, None, math.nan),
        ("timer", None, None, None),
        ("event", None, None, None),
    ],
)
def test_predicates_are_typed(kind, ref, seq, due):
    with pytest.raises((WaitError, ValidationError)):
        typed_match(kind, ref, seq, due)


def test_job_predicate_is_typed():
    assert typed_match("job", "j", None, None) == {"job_id": "j"}
    with pytest.raises(WaitError):
        typed_match("job", "j", 1, None)


def test_message_predicate_requires_cursor_with_named_error():
    with pytest.raises(WaitError, match="message waits require after_seq") as error:
        typed_match("message", "thread", None, None)
    assert error.value.code == "wait.invalid"
