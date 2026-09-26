"""Deliver the document-review Discord outbox (document-review spec §9)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from src.escalations.transport import (
    TransportAmbiguous,
    TransportRetryable,
    TransportUnavailable,
)
from src.remote_links import DashboardLink, DashboardLinkSource

logger = logging.getLogger(__name__)


class ReviewNotifier:
    """Post each submitted review revision once without blocking authoring.

    The review row's ``notified_revision`` is the durable outbox cursor.  A
    retryable or unavailable Discord send deliberately leaves it unchanged;
    an ambiguous send advances it because a duplicate review announcement is
    worse than a possible miss.
    """

    def __init__(
        self,
        db: Any,
        transport: Any | None,
        channel_id: str,
        base_url: str = "",
        *,
        links: DashboardLinkSource | None = None,
    ) -> None:
        self.db = db
        self.transport = transport
        self.channel_id = str(channel_id or "")
        # ``links`` (the daemon's DashboardLinkResolver) wins.  There is no
        # loopback default: a ``127.0.0.1`` link in Discord names the reader's
        # own machine, so without an origin the post carries the notice.
        self.base_url = (base_url or "").strip().rstrip("/")
        self._links = links

    async def tick(self) -> int:
        """Deliver pending review revisions and return those handled this tick."""
        try:
            reviews = await self.db.reviews_pending_notification()
        except Exception:
            logger.exception("Review notification outbox query failed")
            return 0

        handled = 0
        for review in reviews:
            try:
                revision = int(review["current_revision"])
                review_id = str(review["id"])
                if self.transport is None or not self.channel_id:
                    await self.db.mark_review_notified(review_id, revision)
                    handled += 1
                    continue

                await self.transport.post_root(
                    channel_id=self.channel_id,
                    content=await self._content(review, revision),
                )
                await self.db.mark_review_notified(review_id, revision)
                handled += 1
            except TransportAmbiguous:
                logger.warning(
                    "Review %s notification outcome was ambiguous; marking revision %s notified "
                    "to avoid a duplicate",
                    review.get("id"),
                    review.get("current_revision"),
                    exc_info=True,
                )
                try:
                    await self.db.mark_review_notified(
                        str(review["id"]), int(review["current_revision"])
                    )
                    handled += 1
                except Exception:
                    logger.exception("Failed to mark ambiguous review notification as handled")
            except (TransportRetryable, TransportUnavailable):
                logger.warning(
                    "Review %s notification failed transiently; it will retry on the next tick",
                    review.get("id"),
                    exc_info=True,
                )
            except Exception:
                logger.exception("Review %s notification failed", review.get("id"))
        return handled

    async def run(self, interval: float = 30.0) -> None:
        """Keep polling the durable outbox until the daemon cancels this task."""
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Review notification tick escaped its error guard")
            await asyncio.sleep(interval)

    async def _content(self, review: dict, revision: int) -> str:
        changes_note = await self._changes_note(review, revision)
        kind = review["kind"]
        title = review["title"]
        if revision == 1:
            heading = f"📄 {kind} for review: {title}"
        else:
            heading = f"📄 Revised {kind} (rev {revision}): {title}"
        lines = [
            heading,
            f"Project: {review['project_id']} · Author task: {review.get('author_task_id') or '—'}",
        ]
        if changes_note:
            lines.append(str(changes_note))
        link = await self._links.resolve() if self._links is not None else DashboardLink(
            url=self.base_url
        )
        if link.url:
            lines.append(f"{link.url}/reviews/{review['id']}")
        else:
            lines.append(link.unavailable_notice)
        return "\n".join(lines)

    async def _changes_note(self, review: dict, revision: int) -> str | None:
        """Read the note stored with the revision, without widening the outbox API."""
        if "changes_note" in review:
            return review.get("changes_note")
        if revision == 1:
            return None
        get_revision = getattr(self.db, "get_review_revision", None)
        if get_revision is None:
            return None
        current = await get_revision(str(review["id"]), revision)
        return current.get("changes_note") if current is not None else None
