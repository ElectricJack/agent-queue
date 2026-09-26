"""Immutable reservation and lease/CAS operations for shared deliveries."""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any

from sqlalchemy import and_, func, literal, or_, select, union_all, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.database.tables import digest_windows, outbound_deliveries, supervisor_report_requests
from src.delivery.dispatch import MAX_BATCH
from src.delivery.message import operation_marker
from src.digest.render import sanitise


def _due(table, state, now):
    return or_(
        and_(state.in_(("pending", "retry")), table.c.due_at <= now),
        and_(state == "sending", table.c.lease_expires_at < now),
    )


class OutboundQueriesMixin:
    async def reserve_outbound_delivery(
        self,
        *,
        owner_kind: str,
        owner_id: str,
        dedup_key: str,
        destination: dict[str, Any],
        payload: dict[str, Any],
        due_at: float,
        now: float,
    ) -> tuple[dict, bool]:
        """Freeze the route/body at reservation; a conflicting replay is refused.

        Called only by domain commands, never an arbitrary Discord-post surface.
        """
        if owner_kind not in ("morning", "conversation") or not owner_id or not dedup_key:
            raise ValueError("a morning/conversation owner and dedup key are required")
        if not all(math.isfinite(value) for value in (due_at, now)):
            raise ValueError("delivery times must be finite")
        if (
            destination.get("transport") != "discord"
            or not destination.get("channel_id")
            or set(destination) - {"transport", "channel_id", "thread_id"}
        ):
            raise ValueError("a frozen Discord channel and optional thread are required")
        text = str(payload.get("text") or "")
        text = "\n".join(filter(None, (sanitise(line) for line in text.splitlines())))
        text = "".join(c for c in text if c == "\n" or ord(c) >= 32 and ord(c) != 127)
        frozen = {**payload, "text": text}
        digest = hashlib.sha256(
            json.dumps(
                {"destination": destination, "payload": frozen},
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        delivery_id = "outbound-" + hashlib.sha256(dedup_key.encode()).hexdigest()[:32]
        marker = operation_marker(delivery_id, prefix="aq-out")
        max_chars = 1500 if owner_kind == "morning" else 2000
        if not text.strip() or len(f"{text}\n{marker}") > max_chars:
            raise ValueError(f"outbound message must fit {max_chars} characters including marker")
        async with self.immediate() as conn:
            inserted = (
                (
                    await conn.execute(
                        pg_insert(outbound_deliveries)
                        .values(
                            id=delivery_id,
                            owner_kind=owner_kind,
                            owner_id=owner_id,
                            dedup_key=dedup_key,
                            destination=destination,
                            payload=frozen,
                            payload_hash=digest,
                            marker=marker,
                            due_at=due_at,
                            created_at=now,
                            updated_at=now,
                        )
                        .on_conflict_do_nothing(index_elements=["dedup_key"])
                        .returning(outbound_deliveries)
                    )
                )
                .mappings()
                .one_or_none()
            )
            row = (
                inserted
                or (
                    await conn.execute(
                        select(outbound_deliveries).where(
                            outbound_deliveries.c.dedup_key == dedup_key
                        )
                    )
                )
                .mappings()
                .one()
            )
            if (row["owner_kind"], row["owner_id"], row["payload_hash"]) != (
                owner_kind,
                owner_id,
                digest,
            ):
                raise ValueError("delivery dedup key already owns a different frozen message")
            return dict(row), inserted is not None

    async def get_outbound_delivery(self, delivery_id: str) -> dict | None:
        async with self._engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        select(outbound_deliveries).where(outbound_deliveries.c.id == delivery_id)
                    )
                )
                .mappings()
                .one_or_none()
            )
        return dict(row) if row else None

    async def list_outbound_deliveries(
        self,
        *,
        owner_kind: str,
        owner_id: str,
        limit: int = 50,
    ) -> list[dict]:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        async with self._engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        select(outbound_deliveries)
                        .where(
                            outbound_deliveries.c.owner_kind == owner_kind,
                            outbound_deliveries.c.owner_id == owner_id,
                        )
                        .order_by(outbound_deliveries.c.created_at, outbound_deliveries.c.id)
                        .limit(limit)
                    )
                )
                .mappings()
                .all()
            )
        return [dict(row) for row in rows]

    async def claim_report_deliveries(
        self,
        *,
        lease_owner: str,
        now: float,
        lease_seconds: float = 120,
        limit: int = 5,
        include_digest: bool = True,
    ) -> list[dict]:
        """Claim oldest eligible messages across tables under one short transaction."""
        if not lease_owner or lease_seconds <= 0 or not 1 <= limit <= MAX_BATCH:
            raise ValueError("invalid delivery lease or batch")
        candidates = [
            select(
                outbound_deliveries.c.id,
                outbound_deliveries.c.due_at,
                literal("outbound").label("outbox"),
            ).where(_due(outbound_deliveries, outbound_deliveries.c.state, now))
        ]
        if include_digest:
            candidates.append(
                select(
                    digest_windows.c.id,
                    digest_windows.c.due_at,
                    literal("digest").label("outbox"),
                ).where(
                    _due(digest_windows, digest_windows.c.send_status, now),
                    digest_windows.c.payload.is_not(None),
                )
            )
        async with self.immediate() as conn:
            # Serializes only claim selection, never transport I/O. A bounded
            # union preserves global ordering without leasing a younger batch.
            await conn.execute(select(func.pg_advisory_xact_lock(801739628126004093)))
            queue = union_all(*candidates).subquery()
            chosen = (
                (
                    await conn.execute(
                        select(queue)
                        .order_by(queue.c.due_at, queue.c.outbox, queue.c.id)
                        .limit(limit)
                    )
                )
                .mappings()
                .all()
            )
            rows = []
            for candidate in chosen:
                is_digest = candidate["outbox"] == "digest"
                if is_digest:
                    # Submission locks request, then window. Match that order
                    # so a submit/pump race chooses a winner without deadlock.
                    await conn.execute(
                        select(supervisor_report_requests.c.id)
                        .where(
                            supervisor_report_requests.c.kind == "hourly",
                            supervisor_report_requests.c.owner_ref == candidate["id"],
                        )
                        .with_for_update()
                    )
                table = digest_windows if is_digest else outbound_deliveries
                state = table.c.send_status if is_digest else table.c.state
                old = (
                    (
                        await conn.execute(
                            select(table)
                            .where(
                                table.c.id == candidate["id"],
                                _due(table, state, now),
                            )
                            .with_for_update(skip_locked=True)
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if old is None:
                    continue
                values = {
                    state.name: "sending",
                    "lease_owner": lease_owner,
                    "lease_expires_at": now + lease_seconds,
                    "attempt_count": table.c.attempt_count + 1,
                    "updated_at": now,
                }
                row = dict(
                    (
                        await conn.execute(
                            update(table)
                            .where(table.c.id == old["id"])
                            .values(**values)
                            .returning(table)
                        )
                    )
                    .mappings()
                    .one()
                )
                row["outbox"] = candidate["outbox"]
                row["reclaimed"] = old[state.name] == "sending"
                rows.append(row)
                if is_digest:
                    await conn.execute(
                        update(supervisor_report_requests)
                        .where(
                            supervisor_report_requests.c.kind == "hourly",
                            supervisor_report_requests.c.owner_ref == old["id"],
                            supervisor_report_requests.c.state.in_(("reserved", "requested")),
                        )
                        .values(
                            state="fallback",
                            skip_reason="deadline_or_pump_claim",
                            version=supervisor_report_requests.c.version + 1,
                            updated_at=now,
                        )
                    )
        return rows

    async def finish_outbound_delivery(
        self,
        delivery_id: str,
        *,
        lease_owner: str,
        status: str,
        now: float,
        external_receipt_id: str | None = None,
        next_attempt_at: float | None = None,
        last_error: str | None = None,
    ) -> dict | None:
        if (
            status not in ("sent", "retry", "unknown")
            or status == "sent"
            and not external_receipt_id
            or status == "retry"
            and next_attempt_at is None
        ):
            raise ValueError("delivery finish requires a receipt or retry time")
        async with self.immediate() as conn:
            row = (
                (
                    await conn.execute(
                        update(outbound_deliveries)
                        .where(
                            outbound_deliveries.c.id == delivery_id,
                            outbound_deliveries.c.state == "sending",
                            outbound_deliveries.c.lease_owner == lease_owner,
                            outbound_deliveries.c.lease_expires_at > now,
                        )
                        .values(
                            state=status,
                            due_at=next_attempt_at if next_attempt_at is not None else now,
                            lease_owner=None,
                            lease_expires_at=None,
                            external_receipt_id=external_receipt_id,
                            receipt_confirmed_at=now if status == "sent" else None,
                            last_error=last_error,
                            updated_at=now,
                        )
                        .returning(outbound_deliveries)
                    )
                )
                .mappings()
                .one_or_none()
            )
        return dict(row) if row else None
