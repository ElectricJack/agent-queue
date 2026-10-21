"""The single bounded pump for asks, conversation replies, and routine reports."""

import time

from src.delivery.dispatch import LEASE_SECONDS, OutboundAdapter, dispatch_batch
from src.delivery.message import MessageDelivery
from src.digest.dispatch import DigestTickReport
from src.escalations.plan import MAX_ATTEMPTS


class DeliveryScheduler:
    def __init__(
        self,
        db,
        transport,
        *,
        config,
        lease_owner,
        digest,
        conversation_adapter=None,
        ask_adapter=None,
        links=None,
        clock=time.time,
        rate_guard=None,
        escalation_priority=None,
        include_outbound=True,
        max_attempts=MAX_ATTEMPTS,
    ):
        self.db, self.transport, self.config = db, transport, config
        self.lease_owner, self.digest, self.conversation = lease_owner, digest, conversation_adapter
        self.asks, self.links, self.clock = ask_adapter, links, clock
        self.rate_guard, self.escalation_priority = rate_guard, escalation_priority
        self.include_outbound = include_outbound
        self.delivery = MessageDelivery(transport, clock=clock, max_attempts=max_attempts)
        self._priority_streak = 0

    async def tick(self, *, limit=5):
        if self.asks:
            # The handler owns the state transition boundary, including background work.
            return await self.asks.handler._run_ask_delivery_cycle(self, limit=limit)
        return await self.pump(limit=limit)

    async def pump(self, *, schedule=None, report=None, limit=5):
        schedule = schedule or self.digest.schedule()
        report = report or DigestTickReport()
        if self.asks:
            await self.asks.reconcile()

        async def claim(batch_limit):
            if not self.include_outbound:
                return await self.db.claim_digest_windows(
                    lease_owner=self.lease_owner,
                    now=self.clock(),
                    lease_seconds=LEASE_SECONDS,
                    limit=batch_limit,
                )
            # At most three priority claims before giving routine work first choice.
            priority = self._priority_streak < 3
            rows = await self.db.claim_report_deliveries(
                lease_owner=self.lease_owner,
                now=self.clock(),
                lease_seconds=LEASE_SECONDS,
                limit=batch_limit,
                include_digest=schedule.enabled,
                prefer_priority=priority if self.asks else None,
            )
            if rows:
                self._priority_streak = self._priority_streak + 1 if priority else 0
            return rows

        class DigestAdapter:
            async def deliver(adapter, row):
                await self.digest._deliver_one(schedule, row, report)

        outbound = OutboundAdapter(self.db, self.delivery, lease_owner=self.lease_owner)

        class ScopedOutboundAdapter:
            async def deliver(adapter, row):
                if row["owner_kind"] in {"ask", "conversation"}:
                    if (
                        self.asks
                        and row["owner_kind"] == "conversation"
                        and row["payload"].get("ask_id")
                    ):
                        await self.asks.adopt_reply(row)
                        return
                    target = self.asks if row["owner_kind"] == "ask" else self.conversation
                    if target is not None:
                        await target.deliver(row)
                        return
                    await self.db.finish_outbound_delivery(
                        row["id"],
                        lease_owner=self.lease_owner,
                        status="retry",
                        now=self.clock(),
                        next_attempt_at=self.clock() + 30,
                        last_error=f"{row['owner_kind']} adapter is not bound",
                    )
                    return
                if row["owner_kind"] == "morning" and row["payload"].get("report_id"):
                    from src.reports.delivery import morning_policy, visibility_matches

                    owner = await self.db.get_morning_report(row["owner_id"])
                    if owner is None or not visibility_matches(
                        owner["config_snapshot"], morning_policy(self.config)
                    ):
                        await self.db.finish_outbound_delivery(
                            row["id"],
                            lease_owner=self.lease_owner,
                            status="cancelled",
                            now=self.clock(),
                            last_error="report visibility changed or disabled",
                        )
                        return
                await outbound.deliver(row)

        if self.include_outbound:
            from src.reports.delivery import reconcile_morning_deliveries

            await reconcile_morning_deliveries(
                self.db, self.config, now=self.clock(), links=self.links
            )
        report.deferred = await dispatch_batch(
            now=self.clock(),
            limit=limit,
            claim=claim,
            adapters={"digest": DigestAdapter(), "outbound": ScopedOutboundAdapter()},
            escalation_priority=self.escalation_priority,
            rate_guard=self.rate_guard,
            clock=self.clock,
        )
        return report
