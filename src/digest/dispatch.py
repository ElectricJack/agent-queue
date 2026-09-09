"""Schedule, reserve and deliver the hourly digest (implementation spec §8).

Everything above this module is pure: :mod:`src.digest.eligibility` decides
whether a window says anything, :mod:`src.digest.render` turns it into one
message, and :mod:`src.digest.schedule` turns settings into a destination, a
configuration generation and grid-aligned window bounds.  This module is the
part that owns *time* and *the outbox*, and it is built so the two halves
cannot corrupt each other:

* **Evaluation** reserves the window row first.  ``digest_windows`` is unique
  on ``(destination, config_generation, window_start, window_end)`` and the
  bounds are snapped to the interval grid, so a second daemon -- or the same
  daemon after a restart mid-cycle -- computes the *same* key and loses the
  insert instead of producing a second digest for the hour.  The evaluation
  then writes either a payload or a ``suppression_reason``: a silent window is
  persisted exactly as durably as a sent one, which is what stops an idle
  installation from rescanning the same hours forever.

* **Delivery** is a separate lease-driven pump over rows that already have a
  payload.  It never evaluates, so a Discord outage cannot delay or distort
  the scheduling half; the windows simply accumulate as ``pending`` and show
  up in ``digest_status``'s delivery health.

Three §7/§8 rules are enforced here rather than trusted to callers:

* escalations go first.  The pump asks how many escalation deliveries are owed
  a send and, if any are, declines to claim at all -- it does not merely lose
  a race for the rate-limit budget, and it spends no attempt establishing that.
* the rate guard is never bypassed.  Same mechanism: when the guard is hot the
  pump does not claim, so a held digest costs nothing.
* an external send is never assumed.  An ambiguous send is reconciled against
  message history through the marker embedded in the text, and a window whose
  ownership still cannot be established is recorded ``unknown`` rather than
  reposted.

Nothing here raises into the orchestrator cycle.
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from src.digest.aggregate import build_digest
from src.digest.facts import CATEGORIES, DigestWindow
from src.digest.render import MAX_CHARS
from src.digest.schedule import DigestSchedule, schedule_for
from src.escalations.plan import MAX_ATTEMPTS, backoff_for
from src.escalations.transport import (
    EscalationTransport,
    SendOutcome,
    TransportAmbiguous,
    TransportError,
    TransportMissing,
    TransportRetryable,
    TransportUnavailable,
)

logger = logging.getLogger(__name__)

#: How long a claimed window stays leased before another process may take it.
LEASE_SECONDS = 120.0

#: Marker prefix embedded in every delivered digest so an ambiguous send can
#: be reconciled against message history.  Distinct from the escalation
#: prefix so neither can ever match the other's post.
MARKER_PREFIX = "aq-dig"

#: The marker is appended as its own line at send time, so the rendered body
#: is built against a budget that leaves room for it and the finished message
#: still honours §8's 1,200-character target.
MARKER_RESERVE = 32

#: How many earlier sent windows are consulted for wording and fact keys that
#: have already been reported.  §8 forbids reposting identical highlight text
#: as if it were new progress.
REPORTED_HISTORY = 20


def marker_for(window_id: str) -> str:
    """Short, stable operation marker for one digest window."""
    fold = hashlib.sha256(window_id.encode("utf-8")).hexdigest()[:16]
    return f"{MARKER_PREFIX}:{fold}"


async def reported_so_far(
    db: Any,
    destination: str,
    *,
    generation: int,
    limit: int = REPORTED_HISTORY,
) -> tuple[frozenset[str], frozenset[str]]:
    """Fact keys and highlight wordings earlier *sent* windows already used.

    Shared by the dispatcher and by ``digest_preview`` so the operator's dry
    run cannot disagree with the message the next window would really send.
    """
    keys: set[str] = set()
    highlights: set[str] = set()
    for window in await db.list_digest_windows(
        destination=destination,
        config_generation=generation,
        statuses=["sent"],
        limit=limit,
    ):
        payload = window.get("payload") or {}
        if isinstance(payload, dict):
            keys.update(payload.get("reported_keys") or [])
            highlights.update(payload.get("reported_highlights") or [])
    return frozenset(keys), frozenset(highlights)


@dataclass
class DigestTickReport:
    """What one tick did — the shape ``digest_status`` health reads."""

    #: Windows this tick reserved and evaluated (sent + suppressed).
    evaluated: int = 0
    suppressed: int = 0
    sent: int = 0
    retried: int = 0
    unknown: int = 0
    expired: int = 0
    catchup: int = 0
    #: Why the whole tick did nothing (digest disabled, nothing configured).
    skipped: str | None = None
    #: Why delivery specifically stood down this tick, leaving the payload
    #: durable and due: escalations first, or the rate guard.
    deferred: str | None = None
    window_id: str | None = None


class DigestScheduleService:
    """Evaluate due windows and drive the ones that have something to say."""

    def __init__(
        self,
        db: Any,
        transport: EscalationTransport,
        *,
        config: Any,
        lease_owner: str,
        base_url: str = "",
        clock: Callable[[], float] = time.time,
        rate_guard: Callable[[], bool] | None = None,
        escalation_priority: Callable[[float], Awaitable[int]] | None = None,
        max_attempts: int = MAX_ATTEMPTS,
    ) -> None:
        self.db = db
        self.transport = transport
        self._config = config
        self._lease_owner = lease_owner
        self._base_url = base_url
        self._clock = clock
        self._rate_guard = rate_guard
        self._escalation_priority = escalation_priority
        self._max_attempts = max_attempts
        # Anchor for a generation nothing has been persisted for yet, keyed by
        # ``(destination, generation)``.  A configuration change lands here as
        # a new key, which is how §9's "starts a new schedule generation at the
        # change time" is honoured without replaying the retired generation.
        self._anchors: dict[tuple[str, int], float] = {}

    # -- configuration -------------------------------------------------
    @property
    def _discord(self) -> Any:
        return getattr(self._config, "discord", self._config)

    @property
    def _settings(self) -> Any:
        return self._discord.digest

    @property
    def _channel_id(self) -> str:
        return str(getattr(self._discord, "channel_id", "") or "")

    def schedule(self) -> DigestSchedule:
        return schedule_for(self._discord)

    # -- public entry point --------------------------------------------
    async def tick(self, *, limit: int = 5) -> DigestTickReport:
        """Evaluate whatever is due and push whatever is ready.  Never raises."""
        report = DigestTickReport()
        schedule = self.schedule()
        if not schedule.enabled:
            report.skipped = "discord.digest.enabled is false"
            return report
        try:
            await self.evaluate(schedule, report)
        except Exception:
            logger.warning("digest window evaluation failed", exc_info=True)
        try:
            await self.pump(schedule, report, limit=limit)
        except Exception:
            logger.warning("digest delivery pump failed", exc_info=True)
        return report

    # -- evaluation ------------------------------------------------------
    async def _anchor(self, schedule: DigestSchedule, now: float) -> float:
        """Where this generation's coverage currently ends."""
        rows = await self.db.list_digest_windows(
            destination=schedule.destination,
            config_generation=schedule.generation,
            limit=1,
        )
        if rows:
            return float(rows[0]["window_end"])
        key = (schedule.destination, schedule.generation)
        anchor = self._anchors.get(key)
        if anchor is None:
            anchor = schedule.boundary_at(now)
            self._anchors[key] = anchor
        return anchor

    async def evaluate(
        self, schedule: DigestSchedule | None = None, report: DigestTickReport | None = None
    ) -> DigestTickReport:
        """Reserve the due window, then persist its output or its silence."""
        schedule = schedule or self.schedule()
        report = report or DigestTickReport()
        now = self._clock()
        anchor = await self._anchor(schedule, now)
        window = schedule.due_window(now, anchor=anchor)
        if window is None:
            return report

        row, created = await self.db.reserve_digest_window(
            destination=schedule.destination,
            config_generation=schedule.generation,
            window_start=window.since,
            window_end=window.until,
            activity_cursor=None,
            due_at=window.until,
            is_catchup=window.catchup,
        )
        if not created and (
            row["payload"] is not None
            or row["suppression_reason"] is not None
            or row["send_status"] != "pending"
        ):
            # Another evaluator already decided this window.  Re-deciding it
            # is exactly the duplicate normal send §8 forbids.
            return report

        window_id = str(row["id"])
        report.window_id = window_id
        result = await self._evaluate_window(schedule, window, now=now)
        payload = (
            {
                "text": result.text,
                "reported_keys": sorted(result.reported_keys),
                "reported_highlights": sorted(result.reported_highlights),
                "completed_count": result.completed_count,
                "active_count": result.active_count,
                "catchup": window.catchup,
            }
            if result.send
            else None
        )
        completed = await self.db.complete_digest_evaluation(
            window_id,
            activity_cursor={
                "evaluated_at": now,
                "window_end": window.until,
                "fact_count": len(result.reported_keys),
            },
            output_hash=result.output_hash if result.send else None,
            payload=payload,
            suppression_reason=None if result.send else (result.reason or "no_activity"),
        )
        if completed is None:
            # Lost the race between reserve and complete; the winner's
            # decision stands.
            return report
        report.evaluated += 1
        if window.catchup:
            report.catchup += 1
        if not result.send:
            report.suppressed += 1
        return report

    async def _evaluate_window(self, schedule: DigestSchedule, window: DigestWindow, *, now: float):
        """Gather the durable evidence for one window and build its message."""
        reported_keys, reported_highlights = await reported_so_far(
            self.db,
            schedule.destination,
            generation=schedule.generation,
        )
        open_escalations = len(
            await self.db.list_escalations(states=["needs_human", "reply_received"], limit=500)
        )
        inputs = await self.db.collect_digest_activity(
            window,
            now=now,
            project_ids=schedule.project_ids or None,
            # A row written after its window closed is picked up by the next
            # evaluation's lookback and deduplicated by fact key, so a late
            # arrival is reported once rather than lost or double-counted.
            lookback_seconds=schedule.interval_seconds,
            open_escalations=open_escalations,
            reported_keys=reported_keys,
            reported_highlights=reported_highlights,
        )
        categories = frozenset(c for c in schedule.categories if c in CATEGORIES)
        return build_digest(
            inputs,
            project_ids=frozenset(schedule.project_ids) if schedule.project_ids else None,
            categories=categories or None,
            dashboard_url=self._base_url,
            max_chars=MAX_CHARS - MARKER_RESERVE,
        )

    # -- delivery --------------------------------------------------------
    async def pump(
        self,
        schedule: DigestSchedule | None = None,
        report: DigestTickReport | None = None,
        *,
        limit: int = 5,
    ) -> DigestTickReport:
        """Claim due windows that carry a payload and send each one once."""
        schedule = schedule or self.schedule()
        report = report or DigestTickReport()
        now = self._clock()

        if self._escalation_priority is not None:
            try:
                owed = await self._escalation_priority(now)
            except Exception:
                logger.debug("escalation priority probe failed", exc_info=True)
                owed = 0
            if owed:
                report.deferred = f"{owed} escalation deliveries take priority"
                return report
        if self._rate_guard is not None and not self._rate_guard():
            report.deferred = "held by the Discord invalid-request rate guard"
            return report

        claimed = await self.db.claim_digest_windows(
            lease_owner=self._lease_owner,
            now=now,
            lease_seconds=LEASE_SECONDS,
            limit=limit,
        )
        for row in claimed:
            try:
                await self._deliver_one(schedule, row, report)
            except Exception:
                logger.warning(
                    "digest window %s raised; leaving it leased", row["id"], exc_info=True
                )
        return report

    async def _finish(
        self,
        row: Mapping[str, Any],
        report: DigestTickReport,
        *,
        status: str,
        receipt_id: str | None = None,
        next_attempt_at: float | None = None,
        last_error: str | None = None,
    ) -> None:
        await self.db.finish_digest_delivery(
            row["id"],
            lease_owner=self._lease_owner,
            status=status,
            now=self._clock(),
            next_attempt_at=next_attempt_at,
            external_receipt_id=receipt_id,
            last_error=last_error,
        )
        if status == "sent":
            report.sent += 1
        elif status == "retry":
            report.retried += 1
        else:
            report.unknown += 1

    async def _fail(
        self,
        row: Mapping[str, Any],
        report: DigestTickReport,
        *,
        error: str,
        retryable: bool,
    ) -> None:
        """Bounded backoff, then honest abandonment rather than a late repost."""
        attempts = int(row["attempt_count"])
        if retryable and attempts < self._max_attempts:
            await self._finish(
                row,
                report,
                status="retry",
                next_attempt_at=self._clock() + backoff_for(attempts),
                last_error=error,
            )
            return
        await self._finish(row, report, status="unknown", last_error=error)

    async def _reconcile_marker(self, marker: str, *, channel_id: str) -> SendOutcome | None:
        try:
            return await self.transport.find_marker(
                channel_id=channel_id, thread_id=None, marker=marker
            )
        except TransportError:
            return None
        except Exception:
            logger.debug("digest marker reconciliation failed", exc_info=True)
            return None

    async def _deliver_one(
        self, schedule: DigestSchedule, row: Mapping[str, Any], report: DigestTickReport
    ) -> None:
        channel_id = self._channel_id
        if not channel_id:
            await self._finish(
                row,
                report,
                status="unknown",
                last_error="discord.channel_id is not configured; nothing was sent",
            )
            return

        payload = row.get("payload") or {}
        text = str(payload.get("text") or "")
        if not text:
            await self._finish(
                row, report, status="unknown", last_error="reserved window carries no message"
            )
            return

        now = self._clock()
        # An hour that could not be delivered inside the catch-up horizon is
        # stale news.  §8 would rather the channel stay silent than receive a
        # summary of last week once the outage clears; the dashboard keeps the
        # history either way.
        age = now - float(row["window_end"])
        if age > schedule.catchup_seconds:
            report.expired += 1
            await self._finish(
                row,
                report,
                status="unknown",
                last_error=(
                    f"window closed {age / 3600.0:.0f}h ago, beyond the "
                    f"{schedule.catchup_seconds / 3600.0:.0f}h catch-up horizon; "
                    "it was not posted"
                ),
            )
            return

        marker = marker_for(str(row["id"]))
        if int(row["attempt_count"]) > 1:
            found = await self._reconcile_marker(marker, channel_id=channel_id)
            if found is not None:
                await self._finish(row, report, status="sent", receipt_id=found.receipt_id)
                return
            if str(row.get("last_error") or "").startswith("ambiguous:"):
                await self._finish(
                    row,
                    report,
                    status="unknown",
                    last_error=(
                        "an earlier send was ambiguous and no matching message was found; "
                        "delivery ownership is unknown and nothing was reposted"
                    ),
                )
                return

        try:
            outcome = await self.transport.post_root(
                channel_id=channel_id, content=f"{text}\n{marker}"
            )
        except TransportAmbiguous as exc:
            found = await self._reconcile_marker(marker, channel_id=channel_id)
            if found is not None:
                await self._finish(row, report, status="sent", receipt_id=found.receipt_id)
                return
            await self._fail(row, report, error=f"ambiguous: {exc}", retryable=True)
            return
        except (TransportRetryable, TransportUnavailable) as exc:
            await self._fail(row, report, error=str(exc), retryable=True)
            return
        except TransportMissing as exc:
            await self._fail(row, report, error=str(exc), retryable=False)
            return
        except TransportError as exc:
            await self._fail(row, report, error=str(exc), retryable=False)
            return
        await self._finish(row, report, status="sent", receipt_id=outcome.receipt_id)


__all__ = [
    "LEASE_SECONDS",
    "MARKER_PREFIX",
    "MARKER_RESERVE",
    "DigestScheduleService",
    "DigestTickReport",
    "marker_for",
    "reported_so_far",
]
