"""Discord delivery behavior for the durable document-review outbox."""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from src.config import DashboardServerConfig
from src.escalations.transport import (
    TransportAmbiguous,
    TransportRetryable,
    TransportUnavailable,
)
from src.remote_links import DashboardLinkResolver
from src.reviews.notifier import ReviewNotifier


class FakeTransport:
    def __init__(self, outcomes=()):
        self.outcomes = list(outcomes)
        self.posts: list[tuple[str, str]] = []

    async def post_root(self, *, channel_id: str, content: str):
        self.posts.append((channel_id, content))
        outcome = self.outcomes.pop(0) if self.outcomes else None
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeDatabase:
    def __init__(self, reviews: list[dict], revisions: dict[tuple[str, int], dict] | None = None):
        self.reviews = reviews
        self.revisions = revisions or {}
        self.marked: list[tuple[str, int]] = []

    async def reviews_pending_notification(self) -> list[dict]:
        return [
            review
            for review in self.reviews
            if review["notified_revision"] < review["current_revision"]
        ]

    async def get_review_revision(self, review_id: str, revision: int) -> dict | None:
        return self.revisions.get((review_id, revision))

    async def mark_review_notified(self, review_id: str, revision: int) -> None:
        self.marked.append((review_id, revision))
        for review in self.reviews:
            if review["id"] == review_id:
                review["notified_revision"] = max(review["notified_revision"], revision)


def review(**overrides) -> dict:
    result = {
        "id": "rev-bright-harbor",
        "kind": "plan",
        "title": "Review notifications",
        "project_id": "agent-queue",
        "author_task_id": "steady-lantern.5",
        "current_revision": 1,
        "notified_revision": 0,
    }
    result.update(overrides)
    return result


async def test_tick_posts_each_pending_review_once_with_its_deep_link():
    db = FakeDatabase(
        [review(current_revision=2)],
        {("rev-bright-harbor", 2): {"changes_note": "Addressed the delivery feedback."}},
    )
    transport = FakeTransport()
    notifier = ReviewNotifier(db, transport, "123", "https://aq.example.test/")

    assert await notifier.tick() == 1
    assert transport.posts == [
        (
            "123",
            "\n".join(
                [
                    "📄 Revised plan (rev 2): Review notifications",
                    "Project: agent-queue · Author task: steady-lantern.5",
                    "Addressed the delivery feedback.",
                    "https://aq.example.test/reviews/rev-bright-harbor",
                ]
            ),
        )
    ]
    assert db.marked == [("rev-bright-harbor", 2)]

    assert await notifier.tick() == 0
    assert len(transport.posts) == 1


@pytest.mark.parametrize("error", [TransportRetryable("rate limited"), TransportUnavailable("no access")])
async def test_retryable_transport_errors_leave_review_pending(error):
    db = FakeDatabase([review()])
    transport = FakeTransport([error])
    notifier = ReviewNotifier(db, transport, "123", "https://aq.example.test")

    assert await notifier.tick() == 0
    assert db.marked == []

    assert await notifier.tick() == 1
    assert db.marked == [("rev-bright-harbor", 1)]
    assert len(transport.posts) == 2


async def test_ambiguous_transport_result_marks_notified_and_warns(caplog):
    db = FakeDatabase([review()])
    notifier = ReviewNotifier(
        db,
        FakeTransport([TransportAmbiguous("request may have completed")]),
        "123",
        "https://aq.example.test",
    )

    with caplog.at_level(logging.WARNING):
        assert await notifier.tick() == 1

    assert db.marked == [("rev-bright-harbor", 1)]
    assert "outcome was ambiguous" in caplog.text


@pytest.mark.parametrize("transport,channel_id", [(None, "123"), (FakeTransport(), "")])
async def test_unconfigured_discord_marks_reviews_notified(transport, channel_id):
    db = FakeDatabase([review()])
    notifier = ReviewNotifier(db, transport, channel_id, "https://aq.example.test")

    assert await notifier.tick() == 1
    assert db.marked == [("rev-bright-harbor", 1)]
    if transport is not None:
        assert transport.posts == []


async def test_without_an_origin_the_post_carries_a_notice_never_a_loopback_link():
    """A ``127.0.0.1`` link in Discord names the reader's machine (link spec §4.1)."""
    db = FakeDatabase([review()])
    transport = FakeTransport()
    notifier = ReviewNotifier(db, transport, "123")

    assert await notifier.tick() == 1
    content = transport.posts[0][1]
    assert "127.0.0.1" not in content and "/reviews/" not in content
    assert content.endswith("open it on the daemon host).")


async def test_the_link_resolver_supplies_the_review_origin():
    config = SimpleNamespace(
        dashboard_server=DashboardServerConfig(public_url="https://queue.tail1234.ts.net/"),
    )
    db = FakeDatabase([review()])
    transport = FakeTransport()
    notifier = ReviewNotifier(
        db, transport, "123", links=DashboardLinkResolver(lambda: config),
    )

    assert await notifier.tick() == 1
    assert transport.posts[0][1].endswith(
        "https://queue.tail1234.ts.net/reviews/rev-bright-harbor"
    )
