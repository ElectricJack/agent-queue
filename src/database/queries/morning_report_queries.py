"""Durable morning reservations, immutable snapshots and source coverage."""

from __future__ import annotations

import hashlib
import json

from sqlalchemy import cast, delete, func, select, update
from sqlalchemy.dialects.postgresql import JSONB, insert as pg_insert

from src.database.tables import morning_report_coverage as coverage
from src.database.tables import morning_report_facts as facts
from src.database.tables import morning_reports as reports
from src.database.tables import supervisor_report_requests as requests
from src.database.tables import messages
from src.reports.morning import report_window
from src.reports.schedule import SCHEDULE_ID, ZONE_CHANGE_GUARD_SECONDS

_MORNING_LOCK = 801739628126004093
_TERMINAL = ("final", "suppressed", "skipped")


def scope_key(project_ids: list[str]) -> str:
    return hashlib.sha256(json.dumps(sorted(set(project_ids))).encode()).hexdigest()


class MorningReportQueriesMixin:
    async def reserve_morning_author(
        self, report_id: str, *, destination: str, now: float
    ) -> dict | None:
        """One request per frozen report, never per transport attempt."""
        async with self.immediate() as conn:
            row = (
                (
                    await conn.execute(
                        select(reports).where(reports.c.id == report_id).with_for_update()
                    )
                )
                .mappings()
                .one_or_none()
            )
            if (
                not row
                or row["state"] not in ("ready", "authoring")
                or row["author_deadline"] <= now
            ):
                return None
            snapshot = row["config_snapshot"]
            if not snapshot.get("full_fleet_visibility") or snapshot["project_ids"]:
                return None
            request_id = f"report-morning-{report_id}"
            await conn.execute(
                pg_insert(requests)
                .values(
                    id=request_id,
                    kind="morning",
                    owner_ref=report_id,
                    destination=destination,
                    visibility={"full_fleet": True, "project_ids": []},
                    brief=row["brief"],
                    brief_hash=row["brief_hash"],
                    fallback_text=json.dumps(row["fallback"], ensure_ascii=False),
                    author_session_id="supervisor-global",
                    state="reserved",
                    deadline=row["author_deadline"],
                    version=1,
                    created_at=now,
                    updated_at=now,
                )
                .on_conflict_do_nothing(index_elements=["kind", "owner_ref"])
            )
            await conn.execute(
                update(reports).where(reports.c.id == report_id).values(state="authoring")
            )
            request = (
                (await conn.execute(select(requests).where(requests.c.id == request_id)))
                .mappings()
                .one()
            )
            return dict(request)

    async def request_morning_report(
        self, request_id: str, report_id: str, *, now: float
    ) -> dict | None:
        """Lock owner before request, matching submit, deadline and disable."""
        async with self.immediate() as conn:
            owner = (
                (
                    await conn.execute(
                        select(reports).where(reports.c.id == report_id).with_for_update()
                    )
                )
                .mappings()
                .one_or_none()
            )
            row = (
                (
                    await conn.execute(
                        select(requests).where(requests.c.id == request_id).with_for_update()
                    )
                )
                .mappings()
                .one_or_none()
            )
            if (
                not owner
                or owner["state"] != "authoring"
                or not row
                or row["kind"] != "morning"
                or row["owner_ref"] != report_id
                or row["state"] not in ("reserved", "requested")
                or row["deadline"] <= now
            ):
                return None
            if row["state"] == "reserved":
                message_id = f"msg-{request_id}"
                await conn.execute(
                    pg_insert(messages)
                    .values(
                        id=message_id,
                        project_id=None,
                        from_kind="system",
                        from_id="reports",
                        to_kind="session",
                        to_id=row["author_session_id"],
                        subject="Morning report request",
                        body=(
                            f"Morning report {request_id} is ready. Read `aq report brief {request_id}` "
                            f"(all fact pages), then submit a version 1 JSON report with "
                            f"`aq report submit {request_id} --file FILE --brief-hash HASH --expected-version VERSION`. "
                            "Write summary and projects with landed, pending, failures and manual_checks. "
                            "Use only brief project ids and refs; at most 10 manual checks, each tied to landed "
                            "evidence and a known surface in surface_map. Keep unknown shipment pending; "
                            "label inferences and agent-reported verification. Do not run code work, git, "
                            "tests, select a destination/path/URL, or post to Discord. Coverage and delivery "
                            f"are daemon-owned. Deadline (UTC epoch): {row['deadline']}."
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
                            update(requests)
                            .where(requests.c.id == request_id)
                            .values(
                                state="requested",
                                request_message_id=message_id,
                                version=requests.c.version + 1,
                                updated_at=now,
                            )
                            .returning(requests)
                        )
                    )
                    .mappings()
                    .one()
                )
            return dict(row)

    async def submit_morning_report(
        self,
        request_id: str,
        *,
        brief_hash: str,
        expected_version: int,
        report: dict,
        evidence_refs: list[str],
        source_links: list[str],
        now: float,
    ) -> dict | None:
        lookup = await self.get_report_request(request_id)
        if not lookup or lookup["kind"] != "morning":
            return None
        async with self.immediate() as conn:
            owner = (
                (
                    await conn.execute(
                        select(reports).where(reports.c.id == lookup["owner_ref"]).with_for_update()
                    )
                )
                .mappings()
                .one_or_none()
            )
            row = (
                (
                    await conn.execute(
                        select(requests).where(requests.c.id == request_id).with_for_update()
                    )
                )
                .mappings()
                .one_or_none()
            )
            if (
                not owner
                or owner["state"] != "authoring"
                or not row
                or row["state"] != "requested"
                or row["brief_hash"] != brief_hash
                or row["version"] != expected_version
                or row["deadline"] <= now
                or owner["author_deadline"] <= now
                or owner["brief_hash"] != brief_hash
            ):
                return None
            text = json.dumps(report, ensure_ascii=False, sort_keys=True)
            changed = (
                (
                    await conn.execute(
                        update(requests)
                        .where(requests.c.id == request_id)
                        .values(
                            state="submitted",
                            version=expected_version + 1,
                            submitted_text=text,
                            submitted_hash=hashlib.sha256(text.encode()).hexdigest(),
                            evidence_refs=evidence_refs,
                            source_links=source_links,
                            submitted_at=now,
                            updated_at=now,
                        )
                        .returning(requests)
                    )
                )
                .mappings()
                .one()
            )
            await self._finalize_morning_in_transaction(
                conn, dict(owner), now, "final", content=report, reason="authored"
            )
            return dict(changed)

    async def get_morning_report(self, report_id: str) -> dict | None:
        async with self._engine.connect() as conn:
            row = (await conn.execute(select(reports).where(reports.c.id == report_id))).mappings()
            result = row.one_or_none()
            return dict(result) if result else None

    async def list_morning_reports(
        self, *, limit=50, offset=0, states=None, project_id=None
    ) -> list[dict]:
        stmt = select(reports).order_by(reports.c.planned_at.desc(), reports.c.id)
        if states is not None:
            stmt = stmt.where(reports.c.state.in_(states))
        if project_id is not None:
            stmt = stmt.where(
                cast(reports.c.config_snapshot, JSONB).contains({"project_ids": [project_id]})
                | cast(reports.c.brief, JSONB).contains({"projects": [{"id": project_id}]})
            )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt.limit(limit).offset(offset))).mappings().all()
            return [dict(row) for row in rows]

    async def latest_morning_reservation(self) -> dict | None:
        async with self._engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        select(reports)
                        .where(
                            reports.c.schedule_id == SCHEDULE_ID,
                            reports.c.reason.is_distinct_from("late_start"),
                        )
                        .order_by(reports.c.created_at.desc())
                        .limit(1)
                    )
                )
                .mappings()
                .one_or_none()
            )
            return dict(row) if row else None

    async def reserve_morning_report(
        self, *, local_date: str, planned_at: float, config: dict, now: float
    ) -> tuple[dict | None, str]:
        """Serialize the day, zone guard, coverage and replay membership snapshot."""
        async with self.immediate() as conn:
            await conn.execute(select(func.pg_advisory_xact_lock(_MORNING_LOCK)))
            existing = (
                (
                    await conn.execute(
                        select(reports).where(
                            reports.c.schedule_id == SCHEDULE_ID, reports.c.local_date == local_date
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if existing:
                return dict(existing), "existing"
            latest = (
                (
                    await conn.execute(
                        select(reports)
                        .where(
                            reports.c.schedule_id == SCHEDULE_ID,
                            reports.c.reason.is_distinct_from("late_start"),
                        )
                        .order_by(reports.c.created_at.desc())
                        .limit(1)
                    )
                )
                .mappings()
                .one_or_none()
            )
            if (
                latest
                and latest["timezone"] != config["timezone"]
                and (planned_at < latest["created_at"] + ZONE_CHANGE_GUARD_SECONDS)
            ):
                return None, "timezone_guard"
            key = scope_key(config["project_ids"])
            cursors = (
                (
                    await conn.execute(
                        select(coverage).where(
                            coverage.c.schedule_id == SCHEDULE_ID, coverage.c.scope_key == key
                        )
                    )
                )
                .mappings()
                .all()
            )
            since = min((row["covered_until"] for row in cursors), default=None)
            if since is not None and since >= planned_at:
                return None, "covered"
            window = report_window(since, planned_at, config["max_lookback_hours"])
            membership = (
                (
                    await conn.execute(
                        select(facts.c.fact_key)
                        .join(reports, reports.c.id == facts.c.report_id)
                        .where(
                            reports.c.schedule_id == SCHEDULE_ID,
                            reports.c.scope_key == key,
                            reports.c.state.in_(("final", "suppressed")),
                            reports.c.window_end >= planned_at - 72 * 3600,
                        )
                    )
                )
                .scalars()
                .all()
            )
            skipped = now > planned_at + config["late_cutoff_minutes"] * 60
            row = (
                (
                    await conn.execute(
                        pg_insert(reports)
                        .values(
                            id=f"morning-{local_date}",
                            schedule_id=SCHEDULE_ID,
                            local_date=local_date,
                            config_snapshot=config,
                            scope_key=key,
                            timezone=config["timezone"],
                            planned_at=planned_at,
                            window_start=window["since"],
                            window_end=planned_at,
                            build_context={
                                "window": window,
                                "reported_keys": sorted(set(membership)),
                                "previous_heads": {
                                    row["source"].removeprefix("git:"): row["head_sha"]
                                    for row in cursors
                                    if row["head_sha"]
                                },
                            },
                            state="skipped" if skipped else "building",
                            reason="late_start" if skipped else None,
                            author_deadline=now + config["author_deadline_minutes"] * 60,
                            created_at=now,
                            finalized_at=now if skipped else None,
                        )
                        .returning(reports)
                    )
                )
                .mappings()
                .one()
            )
            return dict(row), "reserved"

    async def claim_morning_build(self, report_id: str, *, owner: str, now: float) -> dict | None:
        async with self.immediate() as conn:
            row = (
                (
                    await conn.execute(
                        update(reports)
                        .where(
                            reports.c.id == report_id,
                            reports.c.state.in_(("building", "failed")),
                            (reports.c.lease_expires_at.is_(None))
                            | (reports.c.lease_expires_at <= now),
                        )
                        .values(state="building", lease_owner=owner, lease_expires_at=now + 300)
                        .returning(reports)
                    )
                )
                .mappings()
                .one_or_none()
            )
            return dict(row) if row else None

    async def store_morning_build(
        self, report_id: str, *, owner: str, result: dict, fallback: dict, now: float
    ) -> dict | None:
        """First snapshot wins; retries never overwrite a ready/final report."""
        brief = result["brief"]
        async with self.immediate() as conn:
            row = (
                (
                    await conn.execute(
                        update(reports)
                        .where(
                            reports.c.id == report_id,
                            reports.c.state == "building",
                            reports.c.lease_owner == owner,
                        )
                        .values(
                            state="ready",
                            reason=result["reason"],
                            brief=brief,
                            brief_hash=result["brief_hash"],
                            fallback=fallback,
                            coverage=brief["coverage"],
                            source_cursors=brief["source_cursors"],
                            source_heads=brief["source_heads"],
                            lease_owner=None,
                            lease_expires_at=None,
                        )
                        .returning(reports)
                    )
                )
                .mappings()
                .one_or_none()
            )
            if not row:
                return None
            if brief["facts"]:
                await conn.execute(
                    pg_insert(facts),
                    [
                        {
                            "report_id": report_id,
                            "fact_key": fact["key"],
                            "source": fact["source"],
                            "record_id": fact["record_id"],
                            "project_id": fact["project_id"],
                            "at": fact["at"],
                        }
                        for fact in brief["facts"]
                    ],
                )
            # Healthy quiet days have no author turn or grace delay.
            if result["would_suppress"]:
                return await self._finalize_morning_in_transaction(
                    conn, dict(row), now, "suppressed"
                )
            return dict(row)

    async def fail_morning_build(self, report_id: str, *, owner: str) -> None:
        async with self.immediate() as conn:
            await conn.execute(
                update(reports)
                .where(
                    reports.c.id == report_id,
                    reports.c.state == "building",
                    reports.c.lease_owner == owner,
                )
                .values(
                    state="failed",
                    reason="snapshot_unavailable",
                    lease_owner=None,
                    lease_expires_at=None,
                )
            )

    async def _finalize_morning_in_transaction(
        self,
        conn,
        row: dict,
        now: float,
        state: str,
        *,
        content: dict | None = None,
        reason: str | None = None,
    ) -> dict:
        report_id = row["id"]
        # Only successful sources advance. Seed failed sources at the original
        # window start so a later recovery reaches back over the missing interval.
        sources = dict(row["source_cursors"] or {})
        for project_id in row["source_heads"] or {}:
            sources[f"git:{project_id}"] = row["window_end"]
        for gap in row["coverage"]["gaps"]:
            source = gap["source"]
            if source not in ("brief", "window"):
                sources.setdefault(source, row["window_start"])
        if not row["coverage"]["complete"] and not sources:
            sources["snapshot"] = row["window_start"]
        for source, cursor in sources.items():
            stmt = pg_insert(coverage).values(
                schedule_id=row["schedule_id"],
                scope_key=row["scope_key"],
                source=source,
                covered_until=cursor,
                head_sha=(row["source_heads"] or {}).get(source.removeprefix("git:"))
                if source.startswith("git:")
                else None,
                report_id=report_id,
            )
            await conn.execute(
                stmt.on_conflict_do_update(
                    index_elements=["schedule_id", "scope_key", "source"],
                    set_={
                        "covered_until": stmt.excluded.covered_until,
                        "head_sha": stmt.excluded.head_sha,
                        "report_id": report_id,
                    },
                    where=coverage.c.covered_until < stmt.excluded.covered_until,
                )
            )
        # A complete recovered snapshot releases the conservative recovery anchor.
        if row["coverage"]["complete"]:
            await conn.execute(
                delete(coverage).where(
                    coverage.c.schedule_id == row["schedule_id"],
                    coverage.c.scope_key == row["scope_key"],
                    coverage.c.source == "snapshot",
                )
            )
        changed = (
            (
                await conn.execute(
                    update(reports)
                    .where(reports.c.id == report_id)
                    .values(
                        state=state,
                        report=content if content is not None else row["fallback"],
                        finalized_at=now,
                        reason=reason
                        or ("no_changes" if state == "suppressed" else "author_deadline"),
                    )
                    .returning(reports)
                )
            )
            .mappings()
            .one()
        )
        return dict(changed)

    async def finalize_morning_fallback(self, report_id: str, *, now: float) -> dict | None:
        """Lock/CAS shared with future author submit; terminal content is immutable."""
        async with self.immediate() as conn:
            row = (
                (
                    await conn.execute(
                        select(reports).where(reports.c.id == report_id).with_for_update()
                    )
                )
                .mappings()
                .one_or_none()
            )
            if (
                not row
                or row["state"] not in ("ready", "authoring")
                or row["author_deadline"] > now
            ):
                return None
            await conn.execute(
                update(requests)
                .where(
                    requests.c.kind == "morning",
                    requests.c.owner_ref == report_id,
                    requests.c.state.in_(("reserved", "requested")),
                )
                .values(
                    state="fallback",
                    skip_reason="author_deadline",
                    version=requests.c.version + 1,
                    updated_at=now,
                )
            )
            return await self._finalize_morning_in_transaction(conn, dict(row), now, "final")

    async def cancel_pending_morning_reports(self, *, now: float) -> int:
        async with self.immediate() as conn:
            result = await conn.execute(
                update(reports)
                .where(reports.c.state.not_in(_TERMINAL))
                .values(
                    state="skipped",
                    reason="disabled",
                    finalized_at=now,
                    lease_owner=None,
                    lease_expires_at=None,
                )
            )
            await conn.execute(
                update(requests)
                .where(
                    requests.c.kind == "morning",
                    requests.c.state.in_(("reserved", "requested")),
                )
                .values(
                    state="cancelled",
                    skip_reason="disabled",
                    version=requests.c.version + 1,
                    updated_at=now,
                )
            )
            return result.rowcount

    async def prune_morning_reports(self, *, now: float) -> int:
        async with self.immediate() as conn:
            result = await conn.execute(
                delete(reports).where(
                    reports.c.state.in_(_TERMINAL), reports.c.finalized_at < now - 90 * 86400
                )
            )
            return result.rowcount
