"""Transactional idempotency, version CAS and pin ownership for jobs."""

from __future__ import annotations

import json
import logging
import time
from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from src.database.tables import (
    agent_waits,
    jobs,
    job_outbox,
    job_workspace_pins,
    messages,
    tasks,
    workspaces,
)
from src.jobs.policy import JobError, TERMINAL, TRANSITIONS

logger = logging.getLogger(__name__)


def unpinned_workspace():
    return workspaces.c.job_pin_count == 0


class JobQueriesMixin:
    async def list_task_job_results(self, task_id: str, *, limit: int = 10) -> list[dict]:
        """Recent terminal results for prime, with no delivery acknowledgement."""
        async with self._engine.connect() as conn:
            return [
                dict(row)
                for row in (
                    await conn.execute(
                        select(jobs)
                        .join(tasks, tasks.c.id == jobs.c.task_id)
                        .where(
                            jobs.c.owner_kind == "task",
                            jobs.c.task_id == task_id,
                            jobs.c.project_id == tasks.c.project_id,
                            jobs.c.state.in_(TERMINAL),
                        )
                        .order_by(jobs.c.ended_at.desc(), jobs.c.id)
                        .limit(min(max(limit, 1), 10))
                    )
                ).mappings()
            ]

    async def reconcile_job_results(self, *, now: float, messaging_enabled: bool) -> None:
        """Dispatch at most 100 terminal intents; a matching owner wait owns the wake.

        Lock the task before the outbox, serializing with wait registration's
        task lock. Message insertion and acknowledgement commit together. A
        failed transaction or disabled messaging leaves intent for the next scan.
        """
        from src.jobs.result import result_digest

        async with self._engine.connect() as conn:
            candidates = (
                (
                    await conn.execute(
                        select(job_outbox.c.key, jobs.c.task_id)
                        .join(jobs)
                        .where(job_outbox.c.delivered_at.is_(None))
                        .order_by(job_outbox.c.created_at, job_outbox.c.key)
                        .limit(100)
                    )
                )
                .mappings()
                .all()
            )
        for candidate in candidates:
            try:
                async with self._engine.begin() as conn:
                    await conn.execute(
                        select(tasks.c.id)
                        .where(tasks.c.id == candidate["task_id"])
                        .with_for_update()
                    )
                    intent = (
                        (
                            await conn.execute(
                                select(job_outbox)
                                .where(
                                    job_outbox.c.key == candidate["key"],
                                    job_outbox.c.delivered_at.is_(None),
                                )
                                .with_for_update(skip_locked=True)
                            )
                        )
                        .mappings()
                        .first()
                    )
                    if not intent:
                        continue
                    job = (
                        (await conn.execute(select(jobs).where(jobs.c.id == intent["job_id"])))
                        .mappings()
                        .one()
                    )
                    owner_exists = await conn.scalar(
                        select(tasks.c.id).where(
                            tasks.c.id == job["task_id"], tasks.c.project_id == job["project_id"]
                        )
                    )
                    waiting = await conn.scalar(
                        select(agent_waits.c.id)
                        .where(
                            agent_waits.c.project_id == job["project_id"],
                            agent_waits.c.owner_kind == "task",
                            agent_waits.c.owner_id == job["owner_id"],
                            agent_waits.c.kind == "job",
                            agent_waits.c.match["job_id"].as_string() == job["id"],
                            agent_waits.c.state.in_(("active", "satisfied")),
                        )
                        .limit(1)
                    )
                    if job["owner_kind"] == "task" and owner_exists and not waiting:
                        if not messaging_enabled:
                            continue
                        await conn.execute(
                            pg_insert(messages)
                            .values(
                                id=intent["key"],
                                project_id=job["project_id"],
                                from_kind="system",
                                from_id="jobs",
                                to_kind="task",
                                to_id=job["owner_id"],
                                subject=f"Job {job['state']}",
                                body_kind="job_result",
                                created_at=now,
                                body=json.dumps(
                                    {
                                        "terminal_key": intent["key"],
                                        "result_ref": f"job:{job['id']}",
                                        "digest": result_digest(dict(job)),
                                    }
                                ),
                            )
                            .on_conflict_do_nothing(index_elements=["id"])
                        )
                    await conn.execute(
                        update(job_outbox)
                        .where(job_outbox.c.key == intent["key"])
                        .values(delivered_at=now)
                    )
            except Exception:
                logger.exception(
                    "Job result %s remains pending in the durable outbox", candidate["key"]
                )

    async def submit_job(
        self,
        values: dict,
        *,
        max_queued=100,
        per_task_queued=10,
        log_budget=2 * 1024**3,
        reservation=64 * 1024**2,
        wait_identity: dict | None = None,
    ) -> dict:
        async with self._engine.begin() as conn:
            if wait_identity:
                # Match registration/reconciliation: session -> task -> producer/workspace.
                owner = await self._wait_owner(conn, **wait_identity)
                if owner["owner_kind"] != "task" or owner["owner_id"] != values["task_id"]:
                    from src.agent_waits import WaitError

                    raise WaitError("out_of_scope", "submit-with-wait requires the held task")
            row = await self.submit_job_in_transaction(
                conn,
                values,
                max_queued=max_queued,
                per_task_queued=per_task_queued,
                log_budget=log_budget,
                reservation=reservation,
            )
            if wait_identity:
                wait = await self.register_agent_wait_in_transaction(
                    conn,
                    identity=wait_identity,
                    kind="job",
                    match={"job_id": row["id"]},
                    deadline_at=None,
                    idempotency_key=f"job:{row['id']}",
                    now=values["submitted_at"],
                )
                return {**row, "wait": wait}
            return row

    async def submit_job_in_transaction(
        self,
        conn,
        values: dict,
        *,
        max_queued=100,
        per_task_queued=10,
        log_budget=2 * 1024**3,
        reservation=64 * 1024**2,
    ) -> dict:
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
