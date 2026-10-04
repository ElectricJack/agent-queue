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
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from src.agents.configuration import SUPERVISOR_AGENT_ID
from src.config import ReportsConfig
from src.delivery.dispatch import LEASE_SECONDS, OutboundAdapter, dispatch_batch
from src.delivery.message import FrozenMessage, MessageDelivery, operation_marker
from src.digest.aggregate import build_digest
from src.digest.facts import CATEGORIES, DigestWindow
from src.digest.render import MAX_CHARS
from src.digest.schedule import DigestSchedule, provider_facts_enabled, schedule_for
from src.digest.supervisor import (
    SKIP_QUIET_HOURS,
    SKIP_UNCHANGED,
    STUCK_BLOCKED_MINUTES,
    author_deadline,
    bounded_facts,
    decide_window,
    facts_hash,
    fallback_text,
    quiet_at,
    quiet_line,
)
from src.escalations.plan import MAX_ATTEMPTS
from src.escalations.transport import EscalationTransport
from src.remote_links import DashboardLinkSource
from src.reports.hourly import author_skip_reason, build_hourly_brief, local_day_bounds

logger = logging.getLogger(__name__)

#: Marker prefix embedded in every delivered digest so an ambiguous send can
#: be reconciled against message history.  Distinct from the escalation
#: prefix so neither can ever match the other's post.
MARKER_PREFIX = "aq-dig"

#: The invisible marker is appended at send time. Its encoded length is fixed
#: by the prefix and hash width, so reserve that full length in the body budget.
MARKER_RESERVE = len(operation_marker("", prefix=MARKER_PREFIX))

#: How many earlier sent windows are consulted for wording and fact keys that
#: have already been reported.  §8 forbids reposting identical highlight text
#: as if it were new progress.
REPORTED_HISTORY = 20


#: The hourly narrative author's request ids.  The digest author uses the
#: ``report-digest-`` prefix, and the two policies answer to different rules.
HOURLY_REQUEST_PREFIX = "report-hourly-"


def is_hourly_author_request(request_id: object) -> bool:
    """Whether *request_id* is the hourly narrative author's, not the digest's."""
    return str(request_id or "").startswith(HOURLY_REQUEST_PREFIX)


def marker_for(window_id: str) -> str:
    """Short, stable operation marker for one digest window."""
    return operation_marker(window_id, prefix=MARKER_PREFIX)


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
        dashboard_notice: str = "",
        links: DashboardLinkSource | None = None,
        clock: Callable[[], float] = time.time,
        rate_guard: Callable[[], bool] | None = None,
        escalation_priority: Callable[[float], Awaitable[int]] | None = None,
        max_attempts: int = MAX_ATTEMPTS,
        authoring_ready: Callable[[], bool] | None = None,
        event_bus: Any | None = None,
        include_outbound: bool = False,
        conversation_adapter: Any | None = None,
    ) -> None:
        self.db = db
        self.transport = transport
        self._config = config
        self._lease_owner = lease_owner
        # ``links`` (the daemon's DashboardLinkResolver) wins; the fixed
        # ``base_url`` / ``dashboard_notice`` pair serves callers without one.
        self._base_url = base_url
        self._dashboard_notice = dashboard_notice
        self._links = links
        self._clock = clock
        self._rate_guard = rate_guard
        self._escalation_priority = escalation_priority
        self._max_attempts = max_attempts
        self._authoring_ready = authoring_ready
        self._event_bus = event_bus
        self._include_outbound = include_outbound
        self._conversation_adapter = conversation_adapter
        self._delivery = MessageDelivery(transport, clock=clock, max_attempts=max_attempts)
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

    @property
    def _reports(self) -> ReportsConfig:
        return getattr(self._config, "reports", ReportsConfig())

    def schedule(self) -> DigestSchedule:
        return schedule_for(self._discord)

    # -- public entry point --------------------------------------------
    async def tick(self, *, limit: int = 5) -> DigestTickReport:
        """Evaluate whatever is due and push whatever is ready.  Never raises."""
        report = DigestTickReport()
        schedule = self.schedule()
        if not self._reports.hourly.enabled or not (
            self._authoring_ready and self._authoring_ready()
        ):
            try:
                await self.db.cancel_hourly_reports(now=self._clock())
            except Exception:
                logger.warning("hourly report cancellation failed", exc_info=True)
        else:
            try:
                await self.db.invalidate_hourly_visibility(
                    destination=schedule.destination,
                    full_fleet=(
                        self._reports.hourly.full_fleet_visibility and not schedule.project_ids
                    ),
                    now=self._clock(),
                )
            except Exception:
                logger.warning("hourly report visibility check failed", exc_info=True)
        if not schedule.supervisor_authored:
            # Phase P3 rollback is the flag: a held window has no author to
            # post it, so it is released to the deterministic digest rather than
            # sitting silent until a cadence that no longer exists.
            try:
                await self.db.cancel_digest_requests(now=self._clock())
            except Exception:
                logger.warning("digest author release failed", exc_info=True)
        if not schedule.enabled:
            report.skipped = "discord.digest.enabled is false"
            if self._include_outbound:
                try:
                    await self.pump(schedule, report, limit=limit)
                except Exception:
                    logger.warning("outbound delivery pump failed", exc_info=True)
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
        if schedule.supervisor_authored:
            # §4: the same window, held for its author.  The deterministic
            # message is computed here either way because it is the fallback
            # that posts when nobody authors in time.
            return await self._evaluate_authored(schedule, window, window_id, report, now=now)
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
        report_candidate = None
        if payload is not None:
            skip_reason = author_skip_reason(
                self._reports,
                schedule,
                window,
                result,
                now=now,
                playbook_active=bool(self._authoring_ready and self._authoring_ready()),
            )
            deadline = window.until + self._reports.hourly.grace_minutes * 60
            if skip_reason is None and now >= deadline:
                skip_reason = "deadline_elapsed"
            if skip_reason is not None:
                if skip_reason != "feature_off":
                    payload["author_skip_reason"] = skip_reason
            else:
                dashboard_url, dashboard_notice = self._base_url, self._dashboard_notice
                if self._links is not None:
                    link = await self._links.resolve()
                    dashboard_url, dashboard_notice = link.url, link.unavailable_notice
                brief, brief_hash = build_hourly_brief(
                    result,
                    window,
                    destination=schedule.destination,
                    dashboard_url=dashboard_url,
                    dashboard_notice=dashboard_notice,
                )
                day_start, day_end = local_day_bounds(now, self._reports.timezone)
                report_candidate = {
                    "window_id": window_id,
                    "destination": schedule.destination,
                    "visibility": {"full_fleet": True, "project_ids": []},
                    "brief": brief,
                    "brief_hash": brief_hash,
                    "fallback_text": result.text,
                    "author_session_id": "supervisor-global",
                    "deadline": deadline,
                    "daily_cap": self._reports.hourly.max_requests_per_day,
                    "day_start": day_start,
                    "day_end": day_end,
                    "now": now,
                }
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
            report_candidate=report_candidate,
        )
        if completed is None:
            # Lost the race between reserve and complete; the winner's
            # decision stands.
            return report
        if (
            report_candidate is not None
            and float(completed["due_at"]) == report_candidate["deadline"]
            and self._event_bus is not None
        ):
            try:
                await self._event_bus.emit(
                    "digest.window_ready",
                    {"window_id": window_id, "request_id": f"report-hourly-{window_id}"},
                )
            except Exception:
                logger.warning(
                    "digest.window_ready emit failed; request remains durable", exc_info=True
                )
        report.evaluated += 1
        if window.catchup:
            report.catchup += 1
        if not result.send:
            report.suppressed += 1
        return report

    async def _previous_window_state(
        self, schedule: DigestSchedule, window_id: str
    ) -> dict[str, Any]:
        """What the previous window of this generation decided.

        §4.3's two rules are *stateful*: "unchanged since the last window" and
        "three skips in a row" both need the previous window's own record, so
        they are read from the durable cursor of the newest other window rather
        than recomputed from a live query.  A restart therefore resumes the count
        instead of restarting it, and a missed window does not reset it.
        """
        for previous in await self.db.list_digest_windows(
            destination=schedule.destination,
            config_generation=schedule.generation,
            limit=8,
        ):
            if str(previous["id"]) == window_id:
                continue
            cursor = previous.get("activity_cursor") or {}
            if isinstance(cursor, dict) and cursor.get("facts_hash"):
                return cursor
            return {}
        return {}

    async def _evaluate_authored(
        self,
        schedule: DigestSchedule,
        window: DigestWindow,
        window_id: str,
        report: DigestTickReport,
        *,
        now: float,
    ) -> DigestTickReport:
        """§4's window: freeze the facts, decide, and hold it for its author.

        Three outcomes, all durable before this returns:

        * **held** -- one author request, the deterministic message kept as its
          fallback, and ``due_at`` at the fallback deadline, so the window posts
          exactly once whether the supervisor writes or not;
        * **quiet line** -- §4.3's once-a-day "nothing needs you", posted by the
          daemon with no author turn at all;
        * **skipped** -- silence, recorded as durably as a sent window so the
          next evaluation moves on instead of re-deciding it.
        """
        result = await self._evaluate_window(schedule, window, now=now)
        dashboard_url, dashboard_notice = self._base_url, self._dashboard_notice
        if self._links is not None:
            link = await self._links.resolve()
            dashboard_url, dashboard_notice = link.url, link.unavailable_notice
        open_escalations = len(
            await self.db.list_escalations(
                project_ids=list(schedule.project_ids) if schedule.project_ids else None,
                states=["needs_human", "reply_received"],
                limit=500,
            )
        )
        collected = await self.db.collect_digest_supervisor_facts(
            window,
            now=now,
            project_ids=schedule.project_ids or None,
            open_escalations=open_escalations,
            active_tasks=result.active_count,
            stalled_after=STUCK_BLOCKED_MINUTES,
        )
        deadline = author_deadline(window.until, schedule.author_fallback_seconds)
        previous = await self._previous_window_state(schedule, window_id)
        timezone = self._reports.timezone
        brief = bounded_facts(
            window=window,
            landed=collected["landed"],
            stuck=collected["stuck"],
            escalations=collected["escalations"],
            reviews=collected["reviews"],
            sessions_working=collected["sessions_working"],
            sessions_total=collected["sessions_total"],
            active_tasks=collected["active_tasks"],
            open_escalations=collected["open_escalations"],
            previous_facts_hash=previous.get("facts_hash"),
            deadline=deadline,
        )
        this_hash = facts_hash(brief)
        decision = decide_window(
            facts=brief,
            has_activity=result.send,
            previous_facts_hash=previous.get("facts_hash"),
            previous_quiet_line_day=previous.get("quiet_line_day"),
            consecutive_skips=int(previous.get("consecutive_skips") or 0),
            quiet_now=quiet_at(now, timezone, schedule.quiet_hours),
            now=now,
            timezone=timezone,
            quiet_line_after=schedule.quiet_line_after_skips,
        )
        quiet_day = (
            datetime.fromtimestamp(now, ZoneInfo(timezone)).date().isoformat()
            if decision.quiet_line_due
            else previous.get("quiet_line_day")
        )
        cursor = {
            "evaluated_at": now,
            "window_end": window.until,
            "fact_count": len(result.reported_keys),
            "facts_hash": this_hash,
            "consecutive_skips": decision.consecutive_skips,
            "quiet_line_day": quiet_day,
            "decision": decision.action,
        }
        if decision.action in (SKIP_QUIET_HOURS, SKIP_UNCHANGED):
            # Facts are still frozen -- quiet hours suppress the post, never the
            # collection -- so the next window can compare against this one.
            await self.db.complete_digest_evaluation(
                window_id,
                activity_cursor=cursor,
                output_hash=None,
                payload=None,
                suppression_reason=decision.reason,
            )
            report.evaluated += 1
            report.suppressed += 1
            return report
        text = (
            quiet_line(
                collected["sessions_working"],
                base_url=dashboard_url,
                notice=dashboard_notice,
            )
            if decision.quiet_line_due
            else fallback_text(result.text, brief)
        )
        author_candidate = None
        if not decision.quiet_line_due:
            author_candidate = {
                "window_id": window_id,
                "destination": schedule.destination,
                "visibility": {"full_fleet": True, "project_ids": list(schedule.project_ids)},
                "brief": brief,
                "brief_hash": this_hash,
                "fallback_text": text,
                "author_session_id": SUPERVISOR_AGENT_ID,
                "deadline": deadline,
                "now": now,
            }
        completed = await self.db.complete_digest_evaluation(
            window_id,
            activity_cursor=cursor,
            # The window's output hash identifies what will be sent.  A held
            # window keeps the deterministic message's hash until an author
            # replaces it; a quiet line has no deterministic message, so its own
            # text is the output.
            output_hash=(
                result.output_hash
                if result.send
                else hashlib.sha256(text.encode("utf-8")).hexdigest()
            ),
            payload={
                "text": text,
                "reported_keys": sorted(result.reported_keys),
                "reported_highlights": sorted(result.reported_highlights),
                "completed_count": result.completed_count,
                "active_count": result.active_count,
                "catchup": window.catchup,
                "facts_hash": this_hash,
                "quiet_line": decision.quiet_line_due,
            },
            suppression_reason=None,
            author_candidate=author_candidate,
        )
        if completed is None:
            return report
        if author_candidate is not None and self._event_bus is not None:
            try:
                await self._event_bus.emit(
                    "digest.window_ready",
                    {"window_id": window_id, "request_id": f"report-digest-{window_id}"},
                )
            except Exception:
                logger.warning(
                    "digest.window_ready emit failed; request remains durable", exc_info=True
                )
        report.evaluated += 1
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
            provider_facts=provider_facts_enabled(self._config),
        )
        categories = frozenset(c for c in schedule.categories if c in CATEGORIES)
        dashboard_url, dashboard_notice = self._base_url, self._dashboard_notice
        if self._links is not None:
            link = await self._links.resolve()
            dashboard_url, dashboard_notice = link.url, link.unavailable_notice
        return build_digest(
            inputs,
            project_ids=frozenset(schedule.project_ids) if schedule.project_ids else None,
            categories=categories or None,
            dashboard_url=dashboard_url,
            dashboard_notice=dashboard_notice,
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

        async def claim(batch_limit):
            claim_now = self._clock()
            if self._include_outbound:
                return await self.db.claim_report_deliveries(
                    lease_owner=self._lease_owner,
                    now=claim_now,
                    lease_seconds=LEASE_SECONDS,
                    limit=batch_limit,
                    include_digest=schedule.enabled,
                )
            return await self.db.claim_digest_windows(
                lease_owner=self._lease_owner,
                now=claim_now,
                lease_seconds=LEASE_SECONDS,
                limit=batch_limit,
            )

        class DigestAdapter:
            async def deliver(adapter, row):
                await self._deliver_one(schedule, row, report)

        outbound = OutboundAdapter(self.db, self._delivery, lease_owner=self._lease_owner)

        class ScopedOutboundAdapter:
            async def deliver(adapter, row):
                if row["owner_kind"] == "conversation":
                    if self._conversation_adapter is None:
                        await self.db.finish_outbound_delivery(
                            row["id"],
                            lease_owner=self._lease_owner,
                            status="retry",
                            now=self._clock(),
                            next_attempt_at=self._clock() + 30,
                            last_error="conversation adapter is not bound",
                        )
                        return
                    await self._conversation_adapter.deliver(row)
                    return
                if row["owner_kind"] == "morning" and row["payload"].get("report_id"):
                    from src.reports.delivery import morning_policy, visibility_matches

                    owner = await self.db.get_morning_report(row["owner_id"])
                    policy = morning_policy(self._config)
                    if owner is None or not visibility_matches(owner["config_snapshot"], policy):
                        await self.db.finish_outbound_delivery(
                            row["id"],
                            lease_owner=self._lease_owner,
                            status="cancelled",
                            now=self._clock(),
                            last_error="report visibility changed or disabled",
                        )
                        return
                await outbound.deliver(row)

        if self._include_outbound:
            from src.reports.delivery import reconcile_morning_deliveries

            await reconcile_morning_deliveries(
                self.db,
                self._config,
                now=now,
                links=self._links,
            )

        report.deferred = await dispatch_batch(
            now=now,
            limit=limit,
            claim=claim,
            adapters={
                "digest": DigestAdapter(),
                "outbound": ScopedOutboundAdapter(),
            },
            escalation_priority=self._escalation_priority,
            rate_guard=self._rate_guard,
            clock=self._clock,
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

    async def _deliver_one(
        self, schedule: DigestSchedule, row: Mapping[str, Any], report: DigestTickReport
    ) -> None:
        channel_id = str(row["destination"]).removeprefix("discord:")
        if not channel_id or channel_id == "unconfigured":
            await self._finish(
                row,
                report,
                status="unknown",
                last_error="discord.channel_id is not configured; nothing was sent",
            )
            return

        payload = row.get("payload") or {}
        # The hourly narrative author is only allowed on an unfiltered,
        # full-fleet destination, because it hands a whole session the fleet's
        # evidence.  The supervisor-authored digest (2026-10-03 §4) is scoped by
        # the digest's own project selection instead, so its request is exempt --
        # a narrowed digest is narrowed twice over, not twice unrestricted.
        if is_hourly_author_request(payload.get("author_request_id")) and (
            schedule.destination != row["destination"]
            or schedule.project_ids
            or not self._reports.hourly.full_fleet_visibility
        ):
            await self._finish(
                row, report, status="unknown", last_error="authored report visibility changed"
            )
            return
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

        result = await self._delivery.deliver(
            FrozenMessage(
                channel_id=channel_id,
                text=text,
                marker=marker_for(str(row["id"])),
                attempt_count=int(row["attempt_count"]),
                last_error=row.get("last_error"),
                reclaimed=bool(row.get("reclaimed")),
            ),
        )
        await self._finish(
            row,
            report,
            status=result.status,
            receipt_id=result.receipt_id,
            next_attempt_at=result.next_attempt_at,
            last_error=result.last_error,
        )


__all__ = [
    "HOURLY_REQUEST_PREFIX",
    "LEASE_SECONDS",
    "MARKER_PREFIX",
    "MARKER_RESERVE",
    "DigestScheduleService",
    "DigestTickReport",
    "is_hourly_author_request",
    "marker_for",
    "reported_so_far",
]
