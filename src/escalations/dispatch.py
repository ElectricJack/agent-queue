"""Drive the durable escalation outbox: one root, one thread, per incident.

The dispatcher is the only component that talks to a chat platform about an
escalation, and it does so exclusively through rows in ``escalation_deliveries``
(:mod:`src.database.queries.escalation_queries`).  That gives §7 its
guarantees for free:

* **one post per incident** -- the row's unique ``dedup_key`` is derived from
  the incident and its generation, so a replayed event, a second daemon or a
  gateway reconnect all converge on the same row;
* **one owner at a time** -- ``claim_escalation_deliveries`` hands out a lease,
  and an expired lease is reclaimed rather than duplicated;
* **honest ambiguity** -- an external send that may or may not have landed is
  reconciled against the message history using the marker embedded in the text,
  and when ownership still cannot be established the row is recorded ``unknown``
  (attention-needed) instead of being reposted.

Nothing here raises into the caller: :meth:`EscalationDeliveryService.tick` is
called from the orchestrator cycle, and a Discord outage must never stop the
scheduler.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from src.escalations.facts import (
    KIND_ACK,
    KIND_RELAY,
    KIND_RESOLUTION,
    KIND_ROOT,
    OPEN_STATES,
    TERMINAL_STATES,
    EscalationFacts,
    MentionPolicy,
    TransportBinding,
)
from src.escalations.plan import (
    DEFER_SECONDS,
    MAX_ATTEMPTS,
    backoff_for,
    binding_from_deliveries,
    plan_deliveries,
    plan_replacement,
)
from src.escalations.render import (
    marker_for,
    render_ack,
    render_relay,
    render_resolution,
    render_resolved_root,
    render_root,
    render_thread_opener,
    thread_name,
)
from src.escalations.transport import (
    EscalationTransport,
    SendOutcome,
    TransportAmbiguous,
    TransportError,
    TransportMissing,
)

logger = logging.getLogger(__name__)

#: How long a claimed delivery stays leased before another process may take it.
LEASE_SECONDS = 120.0
#: Reconciliation re-derives the whole desired set for every open incident, so
#: it runs on its own slower cadence than the pump.  Nothing is lost by the
#: gap: a new incident's first delivery is enqueued by the next pass and the
#: outbox is what actually orders the work.
RECONCILE_INTERVAL_SECONDS = 30.0
#: A closed incident stops being re-derived once its resolution has had ample
#: time to go out; without this the newest hundred rows would be re-queried
#: for the life of the daemon.
TERMINAL_HORIZON_SECONDS = 3600.0


@dataclass
class TickReport:
    """What one pump did — the shape the dashboard health panel reads."""

    enqueued: int = 0
    sent: int = 0
    retried: int = 0
    unknown: int = 0
    skipped: str | None = None
    replacements: tuple[str, ...] = field(default_factory=tuple)


class EscalationDeliveryService:
    """Reconcile incidents into deliveries, then drive the deliveries out."""

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
        on_status: Callable[[Mapping[str, Any]], Awaitable[None]] | None = None,
        max_attempts: int = MAX_ATTEMPTS,
    ) -> None:
        self.db = db
        self.transport = transport
        self._config = config
        self._lease_owner = lease_owner
        self._base_url = base_url
        self._clock = clock
        self._rate_guard = rate_guard
        self._on_status = on_status
        self._max_attempts = max_attempts
        self._last_reconcile_at = 0.0

    # -- configuration -------------------------------------------------
    @property
    def _discord(self) -> Any:
        return getattr(self._config, "discord", self._config)

    @property
    def _settings(self) -> Any:
        return self._discord.escalation

    @property
    def _channel_id(self) -> str:
        return str(getattr(self._discord, "channel_id", "") or "")

    def _mentions(self) -> MentionPolicy:
        return MentionPolicy.from_config(self._settings)

    # -- public entry points -------------------------------------------
    async def tick(self, *, limit: int = 20) -> TickReport:
        """Reconcile open incidents and push whatever is due, never raising."""
        report = TickReport()
        if not getattr(self._settings, "enabled", False):
            report.skipped = "discord.escalation.enabled is false"
            return report
        now = self._clock()
        if now - self._last_reconcile_at >= RECONCILE_INTERVAL_SECONDS:
            self._last_reconcile_at = now
            try:
                report.enqueued = await self.reconcile_open()
            except Exception:
                logger.warning("escalation delivery reconcile failed", exc_info=True)
        try:
            await self.pump(limit=limit, report=report)
        except Exception:
            logger.warning("escalation delivery pump failed", exc_info=True)
        return report

    async def reconcile_open(self, *, limit: int = 100) -> int:
        """Enqueue every delivery the current incident states imply."""
        states = tuple(OPEN_STATES) + tuple(TERMINAL_STATES)
        incidents = await self.db.list_escalations(states=states, limit=limit)
        horizon = self._clock() - TERMINAL_HORIZON_SECONDS
        created = 0
        for incident in incidents:
            terminal_at = incident.get("terminal_at")
            if terminal_at is not None and float(terminal_at) < horizon:
                continue
            created += len(await self.reconcile(incident))
        return created

    async def reconcile(self, incident: Mapping[str, Any] | str) -> list[dict[str, Any]]:
        """Enqueue the deliveries one incident is missing.  Idempotent."""
        if isinstance(incident, str):
            row = await self.db.get_escalation(incident)
            if row is None:
                return []
            incident = row
        facts = EscalationFacts.from_row(incident)
        deliveries = await self.db.list_escalation_deliveries(facts.id)
        messages = await self.db.list_escalation_messages(facts.id)
        plan = plan_deliveries(facts, deliveries=deliveries, messages=messages)
        now = self._clock()
        created: list[dict[str, Any]] = []
        for planned in plan.deliveries:
            row, was_created = await self.db.enqueue_escalation_delivery(
                facts.id,
                dedup_key=planned.dedup_key,
                kind=planned.kind,
                payload=planned.payload,
                available_at=now,
                escalation_message_id=planned.escalation_message_id,
                priority=planned.priority,
                generation=planned.generation,
            )
            if was_created:
                created.append(row)
        return created

    async def pump(self, *, limit: int = 20, report: TickReport | None = None) -> TickReport:
        """Claim due deliveries and attempt each one exactly once."""
        report = report or TickReport()
        now = self._clock()
        claimed = await self.db.claim_escalation_deliveries(
            lease_owner=self._lease_owner,
            now=now,
            lease_seconds=LEASE_SECONDS,
            limit=limit,
        )
        for row in claimed:
            try:
                await self._deliver_one(row, report)
            except Exception:
                logger.warning(
                    "escalation delivery %s raised; leaving it leased", row["id"], exc_info=True
                )
        return report

    # -- one delivery ---------------------------------------------------
    async def _finish(
        self,
        row: Mapping[str, Any],
        report: TickReport,
        *,
        status: str,
        binding: TransportBinding | None = None,
        receipt_id: str | None = None,
        next_attempt_at: float | None = None,
        last_error: str | None = None,
    ) -> None:
        binding = binding or TransportBinding()
        # The row may already carry a binding this attempt persisted mid-flight
        # (see :meth:`_bind`); finishing must never write it back to NULL.
        def _keep(value: str | None, column: str) -> str | None:
            return value or (str(row.get(column) or "") or None)

        finished = await self.db.finish_escalation_delivery(
            row["id"],
            lease_owner=self._lease_owner,
            status=status,
            now=self._clock(),
            next_attempt_at=next_attempt_at,
            channel_id=_keep(binding.channel_id, "channel_id"),
            root_message_id=_keep(binding.root_message_id, "root_message_id"),
            thread_id=_keep(binding.thread_id, "thread_id"),
            external_receipt_id=receipt_id,
            last_error=last_error,
        )
        if status == "sent":
            report.sent += 1
        elif status == "retry":
            report.retried += 1
        else:
            report.unknown += 1
        if finished is not None and self._on_status is not None:
            try:
                await self._on_status(finished)
            except Exception:
                logger.debug("escalation delivery status hook failed", exc_info=True)

    async def _fail(
        self,
        row: Mapping[str, Any],
        report: TickReport,
        *,
        binding: TransportBinding,
        error: str,
        retryable: bool,
    ) -> None:
        """Bounded backoff, then honest abandonment to ``unknown``."""
        attempts = int(row["attempt_count"])
        if retryable and attempts < self._max_attempts:
            await self._finish(
                row,
                report,
                status="retry",
                binding=binding,
                next_attempt_at=self._clock() + backoff_for(attempts),
                last_error=error,
            )
            return
        await self._finish(row, report, status="unknown", binding=binding, last_error=error)

    async def _request_replacement(
        self,
        facts: EscalationFacts,
        deliveries: Sequence[Mapping[str, Any]],
        report: TickReport,
    ) -> str | None:
        """Record one new root generation after a deleted post or thread."""
        planned = plan_replacement(facts, deliveries)
        if planned is None:
            return None
        row, created = await self.db.enqueue_escalation_delivery(
            facts.id,
            dedup_key=planned.dedup_key,
            kind=planned.kind,
            payload=planned.payload,
            available_at=self._clock(),
            priority=planned.priority,
            generation=planned.generation,
        )
        if created:
            report.enqueued += 1
            report.replacements = (*report.replacements, planned.dedup_key)
        return str(row["id"])

    async def _bind(
        self,
        row: Mapping[str, Any],
        *,
        channel_id: str | None = None,
        root_message_id: str | None = None,
        thread_id: str | None = None,
    ) -> None:
        """Persist a confirmed external ID the moment the platform confirms it.

        A root delivery is several external writes in a row, and §7's restart
        invariant is that a crash between any two of them must not produce a
        second post.  Writing the binding under the still-held lease is what
        makes the next owner of this row see the half that already exists.
        """
        try:
            await self.db.note_escalation_delivery_binding(
                row["id"],
                lease_owner=self._lease_owner,
                now=self._clock(),
                channel_id=channel_id,
                root_message_id=root_message_id,
                thread_id=thread_id,
            )
        except Exception:  # pragma: no cover - persistence is best-effort here
            logger.warning(
                "could not persist the escalation binding for %s", row["id"], exc_info=True
            )

    async def _reconcile_marker(
        self, marker: str, *, channel_id: str, thread_id: str | None
    ) -> SendOutcome | None:
        """Did a previous ambiguous attempt already land this exact message?"""
        try:
            return await self.transport.find_marker(
                channel_id=channel_id, thread_id=thread_id, marker=marker
            )
        except TransportError:
            return None
        except Exception:
            logger.debug("marker reconciliation failed", exc_info=True)
            return None

    async def _deliver_one(self, row: Mapping[str, Any], report: TickReport) -> None:
        channel_id = self._channel_id
        if not channel_id:
            await self._finish(
                row,
                report,
                status="unknown",
                last_error="discord.channel_id is not configured; nothing was sent",
            )
            return
        if self._rate_guard is not None and not self._rate_guard():
            await self._finish(
                row,
                report,
                status="retry",
                next_attempt_at=self._clock() + backoff_for(int(row["attempt_count"])),
                last_error="held by the Discord invalid-request rate guard",
            )
            return

        incident = await self.db.get_escalation(str(row["escalation_id"]))
        if incident is None:
            await self._finish(
                row, report, status="unknown", last_error="escalation record is gone"
            )
            return
        facts = EscalationFacts.from_row(incident)
        deliveries = await self.db.list_escalation_deliveries(facts.id)
        binding = binding_from_deliveries(deliveries)
        kind = str(row["kind"])
        handler = {
            KIND_ROOT: self._deliver_root,
            KIND_ACK: self._deliver_thread_text,
            KIND_RELAY: self._deliver_thread_text,
            KIND_RESOLUTION: self._deliver_resolution,
        }.get(kind)
        if handler is None:
            await self._finish(
                row, report, status="unknown", last_error=f"unknown delivery kind {kind!r}"
            )
            return
        await handler(row, report, facts=facts, binding=binding, deliveries=deliveries)

    # -- kinds ----------------------------------------------------------
    async def _deliver_root(
        self,
        row: Mapping[str, Any],
        report: TickReport,
        *,
        facts: EscalationFacts,
        binding: TransportBinding,
        deliveries: Sequence[Mapping[str, Any]],
    ) -> None:
        channel_id = self._channel_id
        generation = int(row["generation"])
        if binding.has_root and binding.generation > generation:
            await self._finish(
                row,
                report,
                status="unknown",
                last_error=f"superseded by generation {binding.generation}",
            )
            return
        dedup_key = str(row["dedup_key"])
        marker = marker_for(dedup_key)
        replacement = bool((row.get("payload") or {}).get("replacement"))
        content = (
            render_resolved_root(facts, base_url=self._base_url, dedup_key=dedup_key)
            if facts.is_terminal
            else render_root(
                facts,
                mentions=self._mentions(),
                base_url=self._base_url,
                dedup_key=dedup_key,
                replacement=replacement,
            )
        )

        # A previous attempt may have posted the root and then failed to open
        # the thread, or died between the request and its response.  Recover
        # the half that exists before sending anything new.
        root_id = str(row.get("root_message_id") or "") or None
        thread_id = str(row.get("thread_id") or "") or None
        if root_id is None and int(row["attempt_count"]) > 1:
            found = await self._reconcile_marker(marker, channel_id=channel_id, thread_id=None)
            if found is not None:
                root_id = found.root_message_id
                thread_id = thread_id or found.thread_id
                await self._bind(
                    row,
                    channel_id=found.channel_id or channel_id,
                    root_message_id=root_id,
                    thread_id=thread_id,
                )
            elif self._ownership_unproven(row):
                # The previous send may have landed and the history does not
                # say.  Posting again would be the duplicate §7 forbids.
                await self._finish(
                    row,
                    report,
                    status="unknown",
                    binding=TransportBinding(channel_id=channel_id, generation=generation),
                    last_error=(
                        "an earlier send was ambiguous and no matching message was found; "
                        "delivery ownership is unknown and nothing was reposted"
                    ),
                )
                return

        current = TransportBinding(
            channel_id=channel_id,
            root_message_id=root_id,
            thread_id=thread_id,
            generation=generation,
        )
        if root_id is None:
            try:
                outcome = await self.transport.post_root(channel_id=channel_id, content=content)
            except TransportError as exc:
                await self._fail(
                    row, report, binding=current, error=self._describe(exc), retryable=True
                )
                return
            root_id = outcome.root_message_id or outcome.receipt_id
            current = TransportBinding(
                channel_id=outcome.channel_id or channel_id,
                root_message_id=root_id,
                thread_id=thread_id,
                generation=generation,
            )
            # Confirmed: record it before the next external write, so an
            # interruption from here on repairs rather than reposts.
            await self._bind(
                row, channel_id=current.channel_id, root_message_id=root_id, thread_id=thread_id
            )

        # Binding the thread and posting its opener are two separate external
        # writes.  Creating the thread is idempotent — a Discord message owns
        # at most one — but the opener is a message like any other, so it goes
        # through the same reconcile-then-send path as every follow-up.  Only a
        # thread this very call created is provably empty; a pre-existing one
        # may already hold an opener from an attempt that timed out after it
        # landed, and that one must be found rather than repeated.
        opener_may_exist = True
        if thread_id is None:
            try:
                handle = await self.transport.ensure_thread(
                    channel_id=current.channel_id or channel_id,
                    root_message_id=str(root_id),
                    name=thread_name(facts),
                )
            except TransportMissing as exc:
                # The root we just recovered is gone: a replacement generation
                # is the only honest way forward, and never for a closed one.
                replacement_id = await self._request_replacement(facts, deliveries, report)
                await self._finish(
                    row,
                    report,
                    status="unknown",
                    binding=current,
                    last_error=(
                        f"{self._describe(exc)}; "
                        + (
                            f"replacement delivery {replacement_id} recorded"
                            if replacement_id
                            else "no replacement (incident is closed or one is already pending)"
                        )
                    ),
                )
                return
            except TransportError as exc:
                await self._fail(
                    row, report, binding=current, error=self._describe(exc), retryable=True
                )
                return
            thread_id = handle.thread_id
            opener_may_exist = not handle.created
            current = TransportBinding(
                channel_id=current.channel_id or channel_id,
                root_message_id=root_id,
                thread_id=thread_id,
                generation=generation,
            )
            await self._bind(
                row,
                channel_id=current.channel_id,
                root_message_id=root_id,
                thread_id=thread_id,
            )

        opener_key = f"{dedup_key}:thread"
        outcome, error = await self._send_thread_text(
            row,
            report,
            binding=current,
            content=render_thread_opener(facts, base_url=self._base_url, dedup_key=opener_key),
            dedup_key=opener_key,
            reconcile=opener_may_exist,
        )
        if outcome is None and error is None:
            return  # unreconcilable ambiguity, already recorded as unknown
        if error is not None:
            if isinstance(error, TransportMissing):
                # The thread vanished between binding it and writing into it.
                replacement_id = await self._request_replacement(facts, deliveries, report)
                await self._finish(
                    row,
                    report,
                    status="unknown",
                    binding=current,
                    last_error=(
                        f"{self._describe(error)}; "
                        + (
                            f"replacement delivery {replacement_id} recorded"
                            if replacement_id
                            else "no replacement (incident is closed or one is already pending)"
                        )
                    ),
                )
                return
            await self._fail(
                row, report, binding=current, error=self._describe(error), retryable=True
            )
            return

        await self._finish(
            row, report, status="sent", binding=current, receipt_id=outcome.receipt_id
        )

    async def _deliver_thread_text(
        self,
        row: Mapping[str, Any],
        report: TickReport,
        *,
        facts: EscalationFacts,
        binding: TransportBinding,
        deliveries: Sequence[Mapping[str, Any]],
    ) -> None:
        dedup_key = str(row["dedup_key"])
        if not binding.has_thread:
            await self._finish(
                row,
                report,
                status="retry",
                next_attempt_at=self._clock() + DEFER_SECONDS,
                last_error="waiting for the incident's thread",
            )
            return
        if str(row["kind"]) == KIND_ACK:
            content = render_ack(facts, dedup_key=dedup_key)
        else:
            content = render_relay(
                str((row.get("payload") or {}).get("text") or ""), dedup_key=dedup_key
            )
        await self._post_in_thread(
            row,
            report,
            facts=facts,
            binding=binding,
            deliveries=deliveries,
            content=content,
            dedup_key=dedup_key,
        )
        if facts.is_terminal:
            # Posting into an archived thread un-archives it (Discord does this
            # for any unlocked thread, and the transport asks for it
            # explicitly).  A closed incident must not be left looking open
            # because somebody replied late, so restore §7's archived state.
            # Best effort: the guidance is already delivered either way.
            try:
                await self.transport.archive_thread(thread_id=str(binding.thread_id))
            except TransportError as exc:
                logger.debug("re-archive after a late-reply post failed: %s", exc)

    async def _post_in_thread(
        self,
        row: Mapping[str, Any],
        report: TickReport,
        *,
        facts: EscalationFacts,
        binding: TransportBinding,
        deliveries: Sequence[Mapping[str, Any]],
        content: str,
        dedup_key: str,
    ) -> None:
        """Send one follow-up into the incident's thread and record the row."""
        outcome, error = await self._send_thread_text(
            row, report, binding=binding, content=content, dedup_key=dedup_key
        )
        if outcome is not None:
            await self._finish(
                row, report, status="sent", binding=binding, receipt_id=outcome.receipt_id
            )
            return
        if error is None:
            return  # already recorded (unreconcilable ambiguity)
        if isinstance(error, TransportMissing):
            replacement_id = await self._request_replacement(facts, deliveries, report)
            if replacement_id is not None:
                await self._finish(
                    row,
                    report,
                    status="retry",
                    binding=binding,
                    next_attempt_at=self._clock() + DEFER_SECONDS,
                    last_error=f"{self._describe(error)}; replacement {replacement_id} recorded",
                )
            else:
                await self._finish(
                    row,
                    report,
                    status="unknown",
                    binding=binding,
                    last_error=f"{self._describe(error)}; no replacement is allowed",
                )
            return
        await self._fail(row, report, binding=binding, error=self._describe(error), retryable=True)

    async def _send_thread_text(
        self,
        row: Mapping[str, Any],
        report: TickReport,
        *,
        binding: TransportBinding,
        content: str,
        dedup_key: str,
        reconcile: bool = True,
    ) -> tuple[SendOutcome | None, TransportError | None]:
        """Reconcile, then send.  Returns ``(outcome, error)``; never finishes.

        The one case it does finish is the honest dead end: a previous attempt
        was ambiguous and the history does not show it, so the row becomes
        ``unknown`` and both halves of the tuple come back ``None``.

        ``reconcile=False`` is for the one destination that cannot hold an
        earlier copy of this message: a thread the current attempt just
        created.  Searching it would find nothing and turn a retry that has
        provably sent nothing into a spurious ``unknown``.
        """
        marker = marker_for(dedup_key)
        if reconcile and int(row["attempt_count"]) > 1:
            found = await self._reconcile_marker(
                marker, channel_id=self._channel_id, thread_id=binding.thread_id
            )
            if found is not None:
                return found, None
            if self._ownership_unproven(row):
                await self._finish(
                    row,
                    report,
                    status="unknown",
                    binding=binding,
                    last_error=(
                        "an earlier send was ambiguous and no matching message was found; "
                        "delivery ownership is unknown and nothing was reposted"
                    ),
                )
                return None, None
        try:
            return (
                await self.transport.post_thread_message(
                    thread_id=str(binding.thread_id), content=content
                ),
                None,
            )
        except TransportError as exc:
            return None, exc

    async def _deliver_resolution(
        self,
        row: Mapping[str, Any],
        report: TickReport,
        *,
        facts: EscalationFacts,
        binding: TransportBinding,
        deliveries: Sequence[Mapping[str, Any]],
    ) -> None:
        """Post the outcome, edit the root, then archive — strictly in that order."""
        if not facts.is_terminal:
            await self._finish(
                row,
                report,
                status="retry",
                binding=binding,
                next_attempt_at=self._clock() + DEFER_SECONDS,
                last_error="incident is no longer terminal; resolution withheld",
            )
            return
        if not binding.has_root:
            await self._finish(
                row,
                report,
                status="unknown",
                binding=binding,
                last_error="no root message to resolve",
            )
            return
        dedup_key = str(row["dedup_key"])
        receipt: str | None = None
        thread_note: str | None = None
        if binding.has_thread:
            outcome, error = await self._send_thread_text(
                row,
                report,
                binding=binding,
                content=render_resolution(facts, base_url=self._base_url, dedup_key=dedup_key),
                dedup_key=dedup_key,
            )
            if outcome is not None:
                receipt = outcome.receipt_id
            elif error is None:
                return  # ambiguity already recorded on the row
            elif isinstance(error, TransportMissing):
                # The thread is gone.  The outcome still belongs on the root,
                # and a closed incident is never reposted into a new thread.
                thread_note = f"thread unavailable ({self._describe(error)})"
            else:
                await self._fail(
                    row, report, binding=binding, error=self._describe(error), retryable=True
                )
                return

        try:
            await self.transport.edit_root(
                channel_id=str(binding.channel_id or self._channel_id),
                root_message_id=str(binding.root_message_id),
                content=render_resolved_root(
                    facts, base_url=self._base_url, dedup_key=f"{dedup_key}:root"
                ),
            )
        except TransportMissing as exc:
            # The root is gone but the outcome is recorded in the thread and in
            # the database.  A closed incident is never reposted.
            await self._finish(
                row,
                report,
                status="unknown",
                binding=binding,
                last_error=f"{self._describe(exc)}; resolution not shown on the deleted root",
            )
            return
        except TransportError as exc:
            await self._fail(
                row, report, binding=binding, error=self._describe(exc), retryable=True
            )
            return

        if binding.has_thread:
            try:
                await self.transport.archive_thread(thread_id=str(binding.thread_id))
            except TransportError as exc:
                # The resolution is visible; archival is cosmetic.  Record the
                # send and leave the reason on the row.
                logger.debug("thread archive failed: %s", exc)
        await self._finish(
            row,
            report,
            status="sent",
            binding=binding,
            receipt_id=receipt or f"edit:{binding.root_message_id}:{facts.revision}",
            last_error=thread_note,
        )

    @staticmethod
    def _ownership_unproven(row: Mapping[str, Any]) -> bool:
        """Could a previous attempt have sent this without the row recording it?

        Two ways: it ended in :class:`TransportAmbiguous`, or it never ended at
        all -- the row was reclaimed out of ``sending`` because its owner died
        or lost its lease mid-send.  Either way the marker search is the only
        evidence there is, and when it comes back empty the honest answer is
        ``unknown`` rather than a second post.
        """
        if str(row.get("last_error") or "").startswith(TransportAmbiguous.__name__):
            return True
        return bool(row.get("reclaimed"))

    @staticmethod
    def _describe(exc: BaseException) -> str:
        return f"{type(exc).__name__}: {exc}"[:500]
