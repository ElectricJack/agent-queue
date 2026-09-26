"""Atomic reservation and submission of supervisor-authored report windows."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.database.tables import digest_windows, messages, supervisor_report_requests
from src.database.queries.morning_report_queries import MorningReportQueriesMixin

_HOURLY_LOCK = 801739628126004092
_OPEN_STATES = ("reserved", "requested")


class ReportQueriesMixin(MorningReportQueriesMixin):
    async def collect_morning_report_sources(
        self, *, since: float, until: float, project_ids: tuple[str, ...] | None = None
    ) -> dict[str, Any]:
        from src.reports.queries import read_morning_snapshot

        return await read_morning_snapshot(
            self._engine, since=since, until=until, project_ids=project_ids
        )

    async def reserve_hourly_report_in_transaction(
        self, conn: Any, *, candidate: Mapping[str, Any]
    ) -> tuple[str | None, str | None]:
        """Spend a daily author slot only after serialising all hourly reservations."""
        now = float(candidate["now"])
        await conn.execute(select(func.pg_advisory_xact_lock(_HOURLY_LOCK)))
        outstanding = await conn.scalar(
            select(func.count())
            .select_from(supervisor_report_requests)
            .where(
                supervisor_report_requests.c.kind == "hourly",
                supervisor_report_requests.c.state.in_(_OPEN_STATES),
                supervisor_report_requests.c.deadline > now,
            )
        )
        if outstanding:
            return None, "author_busy"
        used = await conn.scalar(
            select(func.count())
            .select_from(supervisor_report_requests)
            .where(
                supervisor_report_requests.c.kind == "hourly",
                supervisor_report_requests.c.created_at >= candidate["day_start"],
                supervisor_report_requests.c.created_at < candidate["day_end"],
            )
        )
        if int(used or 0) >= int(candidate["daily_cap"]):
            return None, "daily_cap"
        request_id = f"report-hourly-{candidate['window_id']}"
        values = {
            "id": request_id,
            "kind": "hourly",
            "owner_ref": candidate["window_id"],
            "destination": candidate["destination"],
            "visibility": dict(candidate["visibility"]),
            "brief": dict(candidate["brief"]),
            "brief_hash": candidate["brief_hash"],
            "fallback_text": candidate["fallback_text"],
            "author_session_id": candidate["author_session_id"],
            "state": "reserved",
            "deadline": candidate["deadline"],
            "version": 1,
            "created_at": now,
            "updated_at": now,
        }
        inserted = (
            await conn.execute(
                pg_insert(supervisor_report_requests)
                .values(**values)
                .on_conflict_do_nothing(index_elements=["kind", "owner_ref"])
                .returning(supervisor_report_requests.c.id)
            )
        ).scalar_one_or_none()
        if inserted is None:
            return None, "duplicate"
        return request_id, None

    async def get_report_request(self, request_id: str) -> dict[str, Any] | None:
        async with self._engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        select(supervisor_report_requests).where(
                            supervisor_report_requests.c.id == request_id
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
        return dict(row) if row else None

    async def request_report(self, request_id: str, *, now: float) -> dict[str, Any] | None:
        """Queue the same wake message on retries, in the request transaction."""
        async with self.immediate() as conn:
            row = (
                (
                    await conn.execute(
                        select(supervisor_report_requests)
                        .where(supervisor_report_requests.c.id == request_id)
                        .with_for_update()
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None or row["state"] not in _OPEN_STATES or row["deadline"] <= now:
                return None
            message_id = f"msg-{request_id}"
            if row["state"] == "reserved":
                await conn.execute(
                    pg_insert(messages)
                    .values(
                        id=message_id,
                        project_id=None,
                        from_kind="system",
                        from_id="reports",
                        to_kind="session",
                        to_id=row["author_session_id"],
                        subject="Hourly report request",
                        body=(
                            f"Hourly report {request_id} is ready. Read the brief with "
                            f"`aq report brief {request_id}` and submit before the deadline."
                        ),
                        priority=100,
                        created_at=now,
                        archive_after_inject=1,
                        body_kind="report_request",
                    )
                    .on_conflict_do_nothing(index_elements=["id"])
                )
                row = (
                    (
                        await conn.execute(
                            update(supervisor_report_requests)
                            .where(supervisor_report_requests.c.id == request_id)
                            .values(
                                state="requested",
                                request_message_id=message_id,
                                version=supervisor_report_requests.c.version + 1,
                                updated_at=now,
                            )
                            .returning(supervisor_report_requests)
                        )
                    )
                    .mappings()
                    .one()
                )
            return dict(row)

    async def submit_hourly_report(
        self,
        request_id: str,
        *,
        brief_hash: str,
        expected_version: int,
        text: str,
        evidence_refs: list[str],
        source_links: list[str],
        now: float,
    ) -> dict[str, Any] | None:
        """CAS against both request and unclaimed digest window."""
        async with self.immediate() as conn:
            row = (
                (
                    await conn.execute(
                        select(supervisor_report_requests)
                        .where(supervisor_report_requests.c.id == request_id)
                        .with_for_update()
                    )
                )
                .mappings()
                .one_or_none()
            )
            if (
                row is None
                or row["kind"] != "hourly"
                or row["state"] != "requested"
                or row["brief_hash"] != brief_hash
                or row["version"] != expected_version
                or row["deadline"] <= now
            ):
                return None
            window = (
                (
                    await conn.execute(
                        select(digest_windows)
                        .where(digest_windows.c.id == row["owner_ref"])
                        .with_for_update()
                    )
                )
                .mappings()
                .one_or_none()
            )
            if (
                window is None
                or window["send_status"] != "pending"
                or window["lease_owner"] is not None
                or window["destination"] != row["destination"]
            ):
                return None
            payload = dict(window["payload"] or {})
            payload["text"] = text
            payload["author_request_id"] = request_id
            payload["source_links"] = source_links
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            await conn.execute(
                update(digest_windows)
                .where(digest_windows.c.id == row["owner_ref"])
                .values(payload=payload, output_hash=digest, due_at=now, updated_at=now)
            )
            changed = (
                (
                    await conn.execute(
                        update(supervisor_report_requests)
                        .where(supervisor_report_requests.c.id == request_id)
                        .values(
                            state="submitted",
                            version=expected_version + 1,
                            submitted_text=text,
                            submitted_hash=digest,
                            evidence_refs=evidence_refs,
                            source_links=source_links,
                            submitted_at=now,
                            updated_at=now,
                        )
                        .returning(supervisor_report_requests)
                    )
                )
                .mappings()
                .one()
            )
            return dict(changed)

    async def cancel_hourly_reports(self, *, now: float) -> int:
        """Disable authoring and release fallback for every unclaimed window."""
        async with self.immediate() as conn:
            rows = (
                await conn.execute(
                    select(supervisor_report_requests.c.id, supervisor_report_requests.c.owner_ref)
                    .where(
                        supervisor_report_requests.c.kind == "hourly",
                        supervisor_report_requests.c.state.in_(_OPEN_STATES),
                    )
                    .with_for_update(skip_locked=True)
                )
            ).all()
            if not rows:
                return 0
            ids = [row.id for row in rows]
            await conn.execute(
                update(supervisor_report_requests)
                .where(supervisor_report_requests.c.id.in_(ids))
                .values(
                    state="cancelled",
                    skip_reason="authoring_disabled",
                    version=supervisor_report_requests.c.version + 1,
                    updated_at=now,
                )
            )
            await conn.execute(
                update(digest_windows)
                .where(
                    digest_windows.c.id.in_([row.owner_ref for row in rows]),
                    digest_windows.c.send_status == "pending",
                )
                .values(due_at=now, updated_at=now)
            )
            return len(rows)

    async def invalidate_hourly_visibility(
        self, *, destination: str, full_fleet: bool, now: float
    ) -> int:
        """Cancel unsent old-scope payloads before a narrowed channel can see them."""
        async with self.immediate() as conn:
            stmt = (
                select(supervisor_report_requests.c.id, supervisor_report_requests.c.owner_ref)
                .join(
                    digest_windows,
                    digest_windows.c.id == supervisor_report_requests.c.owner_ref,
                )
                .where(
                    supervisor_report_requests.c.kind == "hourly",
                    supervisor_report_requests.c.state.in_(("reserved", "requested", "submitted")),
                    digest_windows.c.send_status == "pending",
                )
                .with_for_update(of=(supervisor_report_requests, digest_windows), skip_locked=True)
            )
            if full_fleet:
                stmt = stmt.where(supervisor_report_requests.c.destination != destination)
            rows = (await conn.execute(stmt)).all()
            if not rows:
                return 0
            ids = [row.id for row in rows]
            owners = [row.owner_ref for row in rows]
            await conn.execute(
                update(supervisor_report_requests)
                .where(supervisor_report_requests.c.id.in_(ids))
                .values(
                    state="cancelled",
                    skip_reason="visibility_changed",
                    version=supervisor_report_requests.c.version + 1,
                    updated_at=now,
                )
            )
            await conn.execute(
                update(digest_windows)
                .where(digest_windows.c.id.in_(owners))
                .values(
                    send_status="unknown",
                    due_at=now,
                    last_error="report visibility changed before delivery",
                    updated_at=now,
                )
            )
            return len(rows)
