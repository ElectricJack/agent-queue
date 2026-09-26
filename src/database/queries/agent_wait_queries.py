"""Agent wait registration, producer snapshots, version CAS and result outbox.

Session -> held task -> wait is the lock order, matching claim/release. The
reconciler reads candidates without locks then acquires that same order. A
terminal row with no result_message_id is durable outbox intent and is retried.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from sqlalchemy import and_, case, func, insert, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.agent_waits import (
    MAX_DIGEST_BYTES,
    TERMINAL_TASK_STATUSES,
    ProducerObservation,
    WaitError,
    WaitResolution,
    resolve_wait,
    job_wait_deadline,
)
from src.database.tables import (
    agent_waits as waits,
    agents,
    archived_tasks,
    messages,
    jobs,
    projects,
    sessions,
    task_completion_records,
    tasks,
)

logger = logging.getLogger(__name__)


def _bounded_digest(digest: dict) -> dict:
    if len(json.dumps(digest, ensure_ascii=False).encode()) <= MAX_DIGEST_BYTES:
        return digest
    return {"reason": "result_available", "digest_truncated": True}


class AgentWaitQueriesMixin:
    async def _wait_owner(
        self,
        conn,
        *,
        session_id: str,
        instance_token: str,
        project_id: str,
        claim_epoch: int | None,
        elevated: bool,
    ) -> dict[str, Any]:
        session = (
            (
                await conn.execute(
                    select(sessions).where(sessions.c.id == session_id).with_for_update()
                )
            )
            .mappings()
            .first()
        )
        if (
            not session
            or not instance_token
            or session["instance_token"] != instance_token
            or session["state"] not in ("starting", "running")
            or session["desired_state"] != "running"
            or session["project_id"] not in (project_id, None)
        ):
            raise WaitError("out_of_scope", "a live matching session instance is required")
        if elevated and session["lifecycle"] == "named" and session["profile_id"] == "supervisor":
            return dict(
                project_id=project_id,
                owner_kind="supervisor",
                owner_id=f"supervisor-{session['project_id'] or 'global'}",
                session_id=session_id,
                session_instance_token=instance_token,
                claim_epoch=0,
            )
        if session["project_id"] != project_id or session["lifecycle"] not in ("task", "pool"):
            raise WaitError("out_of_scope", "waits require a held task or named supervisor")
        task = (
            (
                await conn.execute(
                    select(tasks).where(tasks.c.id == session["task_id"]).with_for_update(read=True)
                )
            )
            .mappings()
            .first()
        )
        if session["lifecycle"] == "pool" and claim_epoch is None:
            raise WaitError("stale_claim", "claim_epoch is required for pool wait mutations")
        if not task or task["project_id"] != project_id:
            raise WaitError("stale_claim", "session does not hold a task in this project")
        epoch = task["claim_epoch"]
        if (
            (claim_epoch is not None and claim_epoch != epoch)
            or (session["last_claim_epoch"] is not None and session["last_claim_epoch"] != epoch)
            or (session["lifecycle"] == "pool" and session["claim_phase"] != "active")
            or task["status"] != "IN_PROGRESS"
            or not session["agent_id"]
            or task["assigned_agent_id"] != session["agent_id"]
        ):
            raise WaitError("stale_claim", "wait mutation does not match the live claim")
        agent = (
            (await conn.execute(select(agents).where(agents.c.id == session["agent_id"])))
            .mappings()
            .first()
        )
        if not agent or agent["current_task_id"] != task["id"] or agent["state"] != "BUSY":
            raise WaitError("stale_claim", "agent does not hold the task")
        return dict(
            project_id=project_id,
            owner_kind="task",
            owner_id=task["id"],
            session_id=session_id,
            session_instance_token=instance_token,
            claim_epoch=epoch,
        )

    async def _observe_agent_wait(self, conn, row: dict) -> ProducerObservation:
        match = row["match"]
        if row["kind"] == "timer":
            due = match["due_at"]
            return ProducerObservation(
                completed_at=due, result_ref=f"timer:{due}", digest={"due_at": due}
            )
        if row["kind"] == "task":
            task = None
            for table in (tasks, archived_tasks):
                task = (
                    (
                        await conn.execute(
                            select(table)
                            .where(
                                table.c.id == match["task_id"],
                                table.c.project_id == row["project_id"],
                            )
                            .with_for_update(read=True)
                        )
                    )
                    .mappings()
                    .first()
                )
                if task:
                    break
            if not task:
                return ProducerObservation(available=False)
            ref = f"task:{task['id']}"
            if task["status"] not in TERMINAL_TASK_STATUSES:
                return ProducerObservation(result_ref=ref)
            completion = (
                (
                    await conn.execute(
                        select(task_completion_records)
                        .where(task_completion_records.c.task_id == task["id"])
                        .order_by(task_completion_records.c.completed_at.desc())
                        .limit(1)
                    )
                )
                .mappings()
                .first()
            )
            return ProducerObservation(
                completed_at=completion["completed_at"] if completion else task["updated_at"],
                result_ref=ref,
                digest={
                    "task_id": task["id"],
                    "status": task["status"],
                    "outcome": completion["outcome"] if completion else None,
                },
            )
        if row["kind"] == "message":
            # Delivery/read/archive markers do not participate in the predicate.
            own = or_(
                and_(
                    messages.c.to_kind == "session",
                    messages.c.to_id.in_([row["session_id"], row["owner_id"]]),
                ),
                and_(messages.c.to_kind == "task", messages.c.to_id == row["owner_id"])
                if row["owner_kind"] == "task"
                else False,
            )
            message = (
                (
                    await conn.execute(
                        select(messages)
                        .where(
                            messages.c.project_id == row["project_id"],
                            messages.c.thread_id == match["thread_id"],
                            messages.c.created_seq > match["after_seq"],
                            own,
                        )
                        .order_by(messages.c.created_seq)
                        .limit(1)
                    )
                )
                .mappings()
                .first()
            )
            if message:
                return ProducerObservation(
                    completed_at=message["created_at"],
                    result_ref=f"message:{message['id']}",
                    digest={
                        "message_id": message["id"],
                        "created_seq": message["created_seq"],
                        "thread_id": message["thread_id"],
                    },
                )
            exists = await conn.scalar(
                select(messages.c.id)
                .where(
                    messages.c.project_id == row["project_id"],
                    messages.c.thread_id == match["thread_id"],
                )
                .limit(1)
            )
            return ProducerObservation(available=exists is not None)
        if row["kind"] == "job":
            job = (
                (await conn.execute(select(jobs).where(jobs.c.id == match["job_id"])))
                .mappings()
                .first()
            )
            if not job or job["project_id"] != row["project_id"]:
                return ProducerObservation(available=False)
            if job["owner_kind"] == "task" and not await conn.scalar(
                select(tasks.c.id).where(tasks.c.id == job["task_id"])
            ):
                return ProducerObservation(available=False)
            if row["owner_kind"] == "task" and (
                job["owner_kind"] != "task" or job["owner_id"] != row["owner_id"]
            ):
                return ProducerObservation(available=False)
            from src.jobs.policy import TERMINAL
            from src.jobs.result import bounded

            ref = f"job:{job['id']}"
            if job["state"] not in TERMINAL:
                return ProducerObservation(result_ref=ref)
            result = job["result"] or {}
            digest = {
                key: result.get(key)
                for key in (
                    "outcome",
                    "exit_code",
                    "signal",
                    "infra_reason",
                    "summary",
                    "result_hash",
                )
            }
            # Leave room for JSON escaping while preserving the failure-first prefix.
            digest["excerpt"] = bounded(result.get("excerpt", ""), 400)
            digest.update(job_id=job["id"], state=job["state"])
            return ProducerObservation(completed_at=job["ended_at"], result_ref=ref, digest=digest)
        return ProducerObservation(available=False)

    async def _validate_wait_source(self, conn, row: dict) -> None:
        if row["kind"] == "job":
            job = (
                (await conn.execute(select(jobs).where(jobs.c.id == row["match"]["job_id"])))
                .mappings()
                .first()
            )
            if job and (
                job["project_id"] != row["project_id"]
                or (
                    row["owner_kind"] == "task"
                    and (job["owner_kind"] != "task" or job["owner_id"] != row["owner_id"])
                )
            ):
                raise WaitError("out_of_scope", "job is not authorized for this owner")
            return
        if row["kind"] == "task":
            for table in (tasks, archived_tasks):
                project = await conn.scalar(
                    select(table.c.project_id).where(table.c.id == row["match"]["task_id"])
                )
                if project is not None:
                    if project != row["project_id"]:
                        raise WaitError("out_of_scope", "wait target belongs to another project")
                    return
            return  # Missing producer resolves immediately as source_unavailable.
        if row["kind"] == "message":
            participant = or_(
                and_(messages.c.from_kind == "session", messages.c.from_id == row["session_id"]),
                and_(
                    messages.c.to_kind == "session",
                    messages.c.to_id.in_([row["session_id"], row["owner_id"]]),
                ),
                and_(messages.c.to_kind == "task", messages.c.to_id == row["owner_id"])
                if row["owner_kind"] == "task"
                else False,
            )
            exists = await conn.scalar(
                select(messages.c.id)
                .where(
                    messages.c.thread_id == row["match"]["thread_id"],
                    messages.c.project_id == row["project_id"],
                    participant,
                )
                .limit(1)
            )
            if not exists:
                raise WaitError("out_of_scope", "message thread is not authorized for this owner")

    async def register_agent_wait_in_transaction(
        self,
        conn,
        *,
        identity: dict,
        kind: str,
        match: dict,
        deadline_at: float | None,
        idempotency_key: str,
        now: float,
    ) -> dict:
        """Register and snapshot the producer in the caller's transaction."""
        if kind == "job":
            # Preserve requested timeout policy so default-budget replays stay stable
            # as queue time elapses, and cannot alias a differently bounded request.
            match = {
                **match,
                "timeout": None if deadline_at is None else round(deadline_at - now, 5),
            }
        owner = await self._wait_owner(conn, **identity)
        old = (
            (
                await conn.execute(
                    select(waits).where(
                        *(
                            waits.c[key] == owner[key]
                            for key in ("project_id", "owner_kind", "owner_id", "claim_epoch")
                        ),
                        waits.c.idempotency_key == idempotency_key,
                    )
                )
            )
            .mappings()
            .first()
        )
        if old:
            if (
                old["kind"] != kind
                or old["match"] != match
                or (
                    deadline_at is not None
                    and abs((old["deadline_at"] - old["created_at"]) - (deadline_at - now))
                    > 0.00001
                )
            ):
                raise WaitError(
                    "wait.idempotency_conflict", "key already names a different condition"
                )
            return dict(old)
        if deadline_at is None:
            if kind != "job":
                raise WaitError("wait.invalid", "deadline required")
            job = (
                (await conn.execute(select(jobs).where(jobs.c.id == match["job_id"])))
                .mappings()
                .first()
            )
            deadline_at = job_wait_deadline(dict(job), now) if job else now + 300
        if owner["owner_kind"] == "task":
            active = await conn.scalar(
                select(waits.c.id)
                .where(
                    waits.c.owner_kind == "task",
                    waits.c.owner_id == owner["owner_id"],
                    waits.c.claim_epoch == owner["claim_epoch"],
                    waits.c.state == "active",
                )
                .limit(1)
            )
            if active:
                raise WaitError(
                    "wait.already_active", "one blocking wait per task claim is permitted"
                )
        else:
            # Serialise project-wide subscription cap across named supervisors.
            await conn.execute(
                select(projects.c.id).where(projects.c.id == owner["project_id"]).with_for_update()
            )
            count = await conn.scalar(
                select(func.count())
                .select_from(waits)
                .where(
                    waits.c.project_id == owner["project_id"],
                    waits.c.owner_kind == "supervisor",
                    waits.c.state == "active",
                )
            )
            if count >= 100:
                raise WaitError("wait.subscription_limit", "100 active subscriptions per project")
        row = dict(
            owner,
            id=str(uuid.uuid4()),
            kind=kind,
            match=match,
            state="active",
            version=1,
            created_at=now,
            deadline_at=deadline_at,
            idempotency_key=idempotency_key,
        )
        await self._validate_wait_source(conn, row)
        await conn.execute(insert(waits).values(**row))
        observation = await self._observe_agent_wait(conn, row)
        resolution = resolve_wait(observation, deadline=deadline_at, now=now)
        if resolution:
            await self._finish_agent_wait(conn, row, resolution, now=now)
        return dict(
            (await conn.execute(select(waits).where(waits.c.id == row["id"]))).mappings().one()
        )

    async def register_agent_wait(self, **kwargs) -> dict:
        async with self._engine.begin() as conn:
            return await self.register_agent_wait_in_transaction(conn, **kwargs)

    async def _enqueue_wait_result(self, conn, row: dict, now: float) -> None:
        message_id = f"wait:{row['id']}:result"
        # Savepoint keeps the resolved row as outbox intent if insertion fails.
        try:
            async with conn.begin_nested():
                await conn.execute(
                    pg_insert(messages)
                    .values(
                        id=message_id,
                        project_id=row["project_id"],
                        from_kind="system",
                        from_id="agent-waits",
                        to_kind="task" if row["owner_kind"] == "task" else "session",
                        to_id=row["owner_id"],
                        subject=f"Wait {row['state']}",
                        body=json.dumps(
                            {
                                "wait_id": row["id"],
                                "state": row["state"],
                                "result_ref": row.get("result_ref"),
                                "digest": row.get("digest"),
                            }
                        ),
                        body_kind="wait_result",
                        created_at=now,
                    )
                    .on_conflict_do_nothing(index_elements=["id"])
                )
                await conn.execute(
                    update(waits)
                    .where(waits.c.id == row["id"])
                    .values(result_message_id=message_id)
                )
        except Exception:
            logger.exception("Wait %s result remains pending in the durable outbox", row["id"])

    async def _finish_agent_wait(self, conn, row: dict, resolution: WaitResolution, *, now: float):
        changed = (
            (
                await conn.execute(
                    update(waits)
                    .where(
                        waits.c.id == row["id"],
                        waits.c.state == "active",
                        waits.c.version == row["version"],
                    )
                    .values(
                        state=resolution.state,
                        version=row["version"] + 1,
                        resolved_at=now,
                        wait_resumed_at=now,
                        result_ref=resolution.result_ref,
                        digest=_bounded_digest(resolution.digest),
                    )
                    .returning(waits)
                )
            )
            .mappings()
            .first()
        )
        if changed:
            await self._enqueue_wait_result(conn, dict(changed), now)
        return dict(changed) if changed else None

    async def get_agent_wait(self, wait_id: str) -> dict | None:
        async with self._engine.connect() as conn:
            row = (
                (await conn.execute(select(waits).where(waits.c.id == wait_id))).mappings().first()
            )
            return dict(row) if row else None

    async def list_agent_waits(
        self,
        *,
        project_id: str | None,
        owner_kind: str | None = None,
        owner_id: str | None = None,
        limit=100,
        offset=0,
    ) -> list[dict]:
        stmt = select(waits)
        for key, value in (
            ("project_id", project_id),
            ("owner_kind", owner_kind),
            ("owner_id", owner_id),
        ):
            if value is not None:
                stmt = stmt.where(waits.c[key] == value)
        async with self._engine.connect() as conn:
            return [
                dict(row)
                for row in (
                    await conn.execute(
                        stmt.order_by(waits.c.created_at.desc(), waits.c.id)
                        .limit(limit)
                        .offset(offset)
                    )
                ).mappings()
            ]

    async def cancel_agent_wait(self, wait_id: str, *, identity: dict | None, now: float) -> dict:
        async with self._engine.begin() as conn:
            owner = await self._wait_owner(conn, **identity) if identity else None
            row = (
                (await conn.execute(select(waits).where(waits.c.id == wait_id).with_for_update()))
                .mappings()
                .first()
            )
            if not row:
                raise WaitError("not_found", "wait not found")
            if owner and any(
                row[key] != owner[key]
                for key in ("project_id", "owner_kind", "owner_id", "claim_epoch")
            ):
                raise WaitError("stale_claim", "wait does not belong to this live claim")
            if row["state"] == "active":
                await self._finish_agent_wait(
                    conn,
                    dict(row),
                    WaitResolution("cancelled", None, {"reason": "cancelled"}),
                    now=now,
                )
            return dict(
                (await conn.execute(select(waits).where(waits.c.id == wait_id))).mappings().one()
            )

    async def _blocking_wait_current(self, conn, row: dict) -> bool:
        session = (
            (await conn.execute(select(sessions).where(sessions.c.id == row["session_id"])))
            .mappings()
            .first()
        )
        task = (
            (await conn.execute(select(tasks).where(tasks.c.id == row["owner_id"])))
            .mappings()
            .first()
        )
        return bool(
            session
            and task
            and session["instance_token"] == row["session_instance_token"]
            and session["state"] in ("starting", "running")
            and session["desired_state"] == "running"
            and session["task_id"] == row["owner_id"]
            and session["project_id"] == row["project_id"] == task["project_id"]
            and task["claim_epoch"] == row["claim_epoch"]
            and (
                session["last_claim_epoch"] is None
                or session["last_claim_epoch"] == row["claim_epoch"]
            )
            and task["status"] in ("ASSIGNED", "IN_PROGRESS")
            and task["assigned_agent_id"] == session["agent_id"]
            and (session["lifecycle"] != "pool" or session["claim_phase"] == "active")
        )

    async def blocking_wait_for(self, session, claim_epoch: int, now: float) -> dict | None:
        """Shared decision for all lease consumers; never exempts stale claims."""
        session_id = session if isinstance(session, str) else session.id
        async with self._engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        select(waits).where(
                            waits.c.session_id == session_id,
                            waits.c.claim_epoch == claim_epoch,
                            waits.c.owner_kind == "task",
                            waits.c.state == "active",
                            waits.c.deadline_at > now,
                        )
                    )
                )
                .mappings()
                .first()
            )
            if (
                not row
                or (
                    not isinstance(session, str)
                    and session.instance_token != row["session_instance_token"]
                )
                or not await self._blocking_wait_current(conn, dict(row))
            ):
                return None
            observation = await self._observe_agent_wait(conn, dict(row))
            if not observation.available or (
                observation.completed_at is not None and observation.completed_at <= now
            ):
                return None
            return dict(row)

    async def agent_wait_for_claim(self, session, claim_epoch: int) -> dict | None:
        """Latest blocking wait for this live instance and claim, never its predecessor."""
        async with self._engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        select(waits)
                        .where(
                            waits.c.session_id == session.id,
                            waits.c.session_instance_token == session.instance_token,
                            waits.c.owner_kind == "task",
                            waits.c.owner_id == session.task_id,
                            waits.c.claim_epoch == claim_epoch,
                        )
                        .order_by(
                            case((waits.c.state == "active", 0), else_=1),
                            waits.c.wait_resumed_at.desc().nulls_last(),
                            waits.c.created_at.desc(),
                            waits.c.id,
                        )
                        .limit(1)
                    )
                )
                .mappings()
                .first()
            )
            if row and await self._blocking_wait_current(conn, dict(row)):
                return dict(row)
            return None

    async def reconcile_agent_waits(
        self, *, now: float, limit: int = 100, wait_id: str | None = None
    ) -> dict:
        """At most 100 indexed candidates, including pending terminal outbox rows."""
        async with self._engine.connect() as conn:
            candidates = [
                dict(row)
                for row in (
                    await conn.execute(
                        select(waits)
                        .where(
                            or_(
                                waits.c.state == "active",
                                waits.c.result_message_id.is_(None),
                            )
                        )
                        .where(waits.c.id == wait_id if wait_id else True)
                        .order_by(
                            case((waits.c.deadline_at <= now, 0), else_=1),
                            waits.c.checked_at,
                            waits.c.deadline_at,
                            waits.c.created_at,
                            waits.c.id,
                        )
                        .limit(min(max(limit, 1), 100))
                    )
                ).mappings()
            ]
        resolved = retried = 0
        for candidate in candidates:
            async with self._engine.begin() as conn:
                # Acquire the same lock order as registration/cancellation and claims.
                await conn.execute(
                    select(sessions.c.id)
                    .where(sessions.c.id == candidate["session_id"])
                    .with_for_update()
                )
                if candidate["owner_kind"] == "task":
                    await conn.execute(
                        select(tasks.c.id)
                        .where(tasks.c.id == candidate["owner_id"])
                        .with_for_update(read=True)
                    )
                row = (
                    (
                        await conn.execute(
                            select(waits)
                            .where(waits.c.id == candidate["id"])
                            .with_for_update(skip_locked=True)
                        )
                    )
                    .mappings()
                    .first()
                )
                if not row:
                    continue
                row = dict(row)
                if row["state"] != "active":
                    if row["result_message_id"] is None:
                        await self._enqueue_wait_result(conn, row, now)
                        retried += 1
                    continue
                if row["owner_kind"] == "task" and not await self._blocking_wait_current(conn, row):
                    resolution = WaitResolution("cancelled", None, {"reason": "claim_ended"})
                else:
                    observation = await self._observe_agent_wait(conn, row)
                    resolution = resolve_wait(observation, deadline=row["deadline_at"], now=now)
                if resolution and await self._finish_agent_wait(conn, row, resolution, now=now):
                    resolved += 1
                elif resolution is None:
                    await conn.execute(
                        update(waits).where(waits.c.id == row["id"]).values(checked_at=now)
                    )
        return {"scanned": len(candidates), "resolved": resolved, "outbox_retried": retried}
