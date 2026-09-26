"""Transactional idempotency, version CAS and pin ownership for jobs."""

from __future__ import annotations

import time
from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from src.database.tables import jobs, job_outbox, job_workspace_pins, tasks, workspaces
from src.jobs.policy import JobError, TERMINAL, TRANSITIONS


def unpinned_workspace():
    return workspaces.c.job_pin_count == 0


class JobQueriesMixin:
    async def submit_job(
        self,
        values: dict,
        *,
        max_queued=100,
        per_task_queued=10,
        log_budget=2 * 1024**3,
        reservation=64 * 1024**2,
    ) -> dict:
        async with self._engine.begin() as conn:
            # Serialize host-wide quota/reservation and replay before quotas.
            await conn.execute(select(func.pg_advisory_xact_lock(109794, 1)))
            existing = (
                (
                    await conn.execute(
                        select(jobs).where(
                            jobs.c.project_id == values["project_id"],
                            jobs.c.owner_kind == values["owner_kind"],
                            jobs.c.owner_id == values["owner_id"],
                            jobs.c.idempotency_key == values["idempotency_key"],
                        )
                    )
                )
                .mappings()
                .first()
            )
            if existing:
                if existing["request_hash"] != values["request_hash"]:
                    raise JobError("jobs.idempotency_conflict")
                return dict(existing)
            queued = await conn.scalar(
                select(func.count()).select_from(jobs).where(jobs.c.state == "queued")
            )
            owned = await conn.scalar(
                select(func.count())
                .select_from(jobs)
                .where(
                    jobs.c.project_id == values["project_id"],
                    jobs.c.owner_kind == values["owner_kind"],
                    jobs.c.owner_id == values["owner_id"],
                    jobs.c.state == "queued",
                )
            )
            if queued >= max_queued or owned >= per_task_queued:
                raise JobError("jobs.queue_full")
            reserved = await conn.scalar(
                select(func.coalesce(func.sum(jobs.c.output_reservation_bytes), 0))
                .select_from(jobs)
                .where(jobs.c.output_retention.in_(["reserved", "retained"]))
            )
            if reserved + reservation > log_budget:
                raise JobError("jobs.output_capacity")
            if values["owner_kind"] == "task":
                task = (
                    (
                        await conn.execute(
                            select(tasks).where(tasks.c.id == values["task_id"]).with_for_update()
                        )
                    )
                    .mappings()
                    .first()
                )
                if (
                    not task
                    or task["project_id"] != values["project_id"]
                    or task["claim_epoch"] != values["claim_epoch"]
                    or task["status"] != "IN_PROGRESS"
                ):
                    raise JobError("jobs.stale_claim")
            await conn.execute(
                select(func.pg_advisory_xact_lock(109795, func.hashtext(values["workspace_id"])))
            )
            ws = (
                (
                    await conn.execute(
                        select(workspaces)
                        .where(workspaces.c.id == values["workspace_id"])
                        .with_for_update()
                    )
                )
                .mappings()
                .first()
            )
            if not ws or ws["project_id"] != values["project_id"] or not ws["enabled"]:
                raise JobError("jobs.cwd_invalid")
            if ws["generation"] != values["workspace_generation"]:
                raise JobError("jobs.workspace_busy")
            if values["input_mode"] == "live" and ws["locked_by_task_id"] != values["task_id"]:
                raise JobError("jobs.workspace_busy")
            row = (
                (
                    await conn.execute(
                        insert(jobs)
                        .values(**{**values, "output_reservation_bytes": reservation})
                        .returning(jobs)
                    )
                )
                .mappings()
                .one()
            )
            await conn.execute(
                insert(job_workspace_pins).values(
                    job_id=row["id"],
                    workspace_id=ws["id"],
                    generation=ws["generation"],
                    created_at=values["submitted_at"],
                )
            )
            await conn.execute(
                update(workspaces)
                .where(workspaces.c.id == ws["id"])
                .values(job_pin_count=workspaces.c.job_pin_count + 1)
            )
            return dict(row)

    async def get_job(self, job_id: str) -> dict | None:
        async with self._engine.connect() as conn:
            row = (await conn.execute(select(jobs).where(jobs.c.id == job_id))).mappings().first()
            return dict(row) if row else None

    async def list_jobs(
        self, *, project_id=None, task_id=None, active=False, limit=100
    ) -> list[dict]:
        stmt = select(jobs)
        if project_id is not None:
            stmt = stmt.where(jobs.c.project_id == project_id)
        if task_id is not None:
            stmt = stmt.where(jobs.c.task_id == task_id)
        if active:
            stmt = stmt.where(jobs.c.state.not_in(TERMINAL))
        async with self._engine.connect() as conn:
            return [
                dict(r)
                for r in (
                    await conn.execute(stmt.order_by(jobs.c.submitted_at, jobs.c.id).limit(limit))
                ).mappings()
            ]

    async def transition_job(
        self, job_id: str, version: int, state: str, *, cleaned=False, **fields
    ) -> dict | None:
        async with self._engine.begin() as conn:
            row = (
                (await conn.execute(select(jobs).where(jobs.c.id == job_id).with_for_update()))
                .mappings()
                .first()
            )
            if not row or row["state_version"] != version or row["state"] in TERMINAL:
                return None
            if state not in TRANSITIONS[row["state"]]:
                raise ValueError(f"invalid job transition {row['state']} -> {state}")
            if state in TERMINAL:
                fields.setdefault("ended_at", time.time())
                if not fields.get("result"):
                    raise ValueError("terminal job needs immutable result")
            updated = (
                (
                    await conn.execute(
                        update(jobs)
                        .where(jobs.c.id == job_id, jobs.c.state_version == version)
                        .values(state=state, state_version=version + 1, **fields)
                        .returning(jobs)
                    )
                )
                .mappings()
                .one()
            )
            if state in TERMINAL:
                await conn.execute(
                    pg_insert(job_outbox)
                    .values(
                        key=f"job:{job_id}:terminal",
                        job_id=job_id,
                        created_at=fields["ended_at"],
                        payload=fields["result"],
                    )
                    .on_conflict_do_nothing()
                )
                if cleaned:
                    await self._release_job_pin(conn, job_id)
            return dict(updated)

    async def workspace_has_job_pin(self, workspace_id: str) -> bool:
        async with self._engine.connect() as conn:
            return bool(
                await conn.scalar(
                    select(job_workspace_pins.c.job_id)
                    .where(job_workspace_pins.c.workspace_id == workspace_id)
                    .limit(1)
                )
            )

    async def job_cleanup_verified(self, job_id: str) -> None:
        async with self._engine.begin() as conn:
            await self._release_job_pin(conn, job_id)
            await conn.execute(
                update(jobs).where(jobs.c.id == job_id).values(cleanup_blocked=False)
            )

    async def reconcilable_jobs(self, limit=100):
        async with self._engine.connect() as conn:
            rows = await conn.execute(
                select(jobs)
                .where(jobs.c.state.not_in(TERMINAL) | jobs.c.cleanup_blocked.is_(True))
                .order_by(jobs.c.submitted_at, jobs.c.id)
                .limit(limit)
            )
            return [dict(r) for r in rows.mappings()]

    async def mark_job_cleanup_blocked(self, job_id):
        async with self._engine.begin() as conn:
            await conn.execute(update(jobs).where(jobs.c.id == job_id).values(cleanup_blocked=True))

    async def job_output_reservations(self):
        async with self._engine.connect() as conn:
            return await conn.scalar(
                select(func.coalesce(func.sum(jobs.c.output_reservation_bytes), 0))
                .select_from(jobs)
                .where(jobs.c.output_retention.in_(["reserved", "retained"]))
            )

    async def retained_terminal_jobs(self, limit=100, *, result_cutoff=0):
        async with self._engine.connect() as conn:
            rows = await conn.execute(
                select(jobs)
                .where(
                    jobs.c.state.in_(TERMINAL),
                    jobs.c.cleanup_blocked.is_(False),
                    (jobs.c.output_retention != "expired") | (jobs.c.ended_at < result_cutoff),
                )
                .order_by(jobs.c.ended_at, jobs.c.id)
                .limit(limit)
            )
            return [dict(r) for r in rows.mappings()]

    async def expire_job_output(self, job_id):
        async with self._engine.begin() as conn:
            await conn.execute(
                update(jobs)
                .where(jobs.c.id == job_id, jobs.c.state.in_(TERMINAL))
                .values(output_retention="expired")
            )

    async def purge_terminal_job(self, job_id):
        async with self._engine.begin() as conn:
            pinned = await conn.scalar(
                select(job_workspace_pins.c.job_id).where(job_workspace_pins.c.job_id == job_id)
            )
            if pinned:
                return False
            await conn.execute(delete(job_outbox).where(job_outbox.c.job_id == job_id))
            await conn.execute(delete(jobs).where(jobs.c.id == job_id, jobs.c.state.in_(TERMINAL)))
            return True

    async def _release_job_pin(self, conn, job_id):
        workspace_id = await conn.scalar(
            delete(job_workspace_pins)
            .where(job_workspace_pins.c.job_id == job_id)
            .returning(job_workspace_pins.c.workspace_id)
        )
        if workspace_id:
            await conn.execute(
                update(workspaces)
                .where(workspaces.c.id == workspace_id)
                .values(job_pin_count=workspaces.c.job_pin_count - 1)
            )
