"""Pure collaboration limits, identifiers and pointer bodies."""

from src.collaboration import (
    WAIT_REASONS,
    CollaborationError,
    is_collaboration_thread,
    new_thread_id,
    render_closed,
    render_invite,
)


def test_ids_reserve_the_prefix():
    ids = {new_thread_id() for _ in range(100)}
    assert len(ids) == 100
    assert all(len(value) == 23 and is_collaboration_thread(value) for value in ids)
    assert is_collaboration_thread("collab-guessed")
    assert not is_collaboration_thread(None)
    assert not is_collaboration_thread("ordinary-thread")


def test_error_preserves_code_and_retry_after():
    error = CollaborationError("rate_limited", "Wait", retry_after=12)
    assert isinstance(error, ValueError)
    assert error.code == "collaboration.rate_limited"
    assert error.retry_after == 12
    assert str(error) == "Wait"
    assert CollaborationError("collaboration.closed", "Closed").code == "collaboration.closed"


def test_renderers_and_wait_reasons():
    thread = dict(id="collab-test", goal="Inspect the diff", close_reason="budget_exhausted")
    invite = render_invite(thread, [dict(task_id="one"), dict(task_id="two")])
    assert "aq collaboration accept collab-test" in invite
    assert "aq collaboration show collab-test" in invite
    assert "one" in invite and "two" in invite and "Inspect the diff" in invite
    closed = render_closed(thread)
    assert "budget_exhausted" in closed
    assert "aq collaboration show collab-test" in closed
    assert "\n" not in invite + closed
    assert WAIT_REASONS == ("thread_closed", "peer_failed", "peer_gone", "partner_not_running")
