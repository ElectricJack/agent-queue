"""Shared durable operator instructions; no supervisor-local state or text inference."""
from __future__ import annotations

import time
import uuid

from sqlalchemy import and_, insert, or_, select

from src.database import tables as t


async def object_project_on(conn, kind: str, identity: str) -> str:
    table = {"task": t.tasks, "batch": t.integration_batches,
             "operation": t.integration_repair_operations}[kind]
    row = (await conn.execute(select(table).where(table.c.id == identity))).mappings().first()
    if row is None:
        raise ValueError(f"{kind} {identity!r} does not exist")
    if kind == "operation":
        return await object_project_on(
            conn, "batch" if row["batch_id"] else "task",
            row["batch_id"] or row["parent_task_id"],
        )
    return row["project_id"]


async def related_on(conn, kind: str, identity: str) -> set[tuple[str, str]]:
    """Include the owning batch/parent and its members and repair operations."""
    refs = {(kind, identity)}
    if kind == "operation":
        op = (await conn.execute(select(t.integration_repair_operations).where(
            t.integration_repair_operations.c.id == identity,
        ))).mappings().first()
        if op:
            kind, identity = ("batch", op["batch_id"]) if op["batch_id"] else (
                "task", op["parent_task_id"]
            )
            refs.add((kind, identity))
    batch_ids = {identity} if kind == "batch" else set()
    if kind == "task":
        batch_ids.update((await conn.execute(select(t.integration_batch_members.c.batch_id)
            .where(t.integration_batch_members.c.task_id == identity))).scalars())
    refs.update(("batch", value) for value in batch_ids)
    if batch_ids:
        refs.update(("task", value) for value in (await conn.execute(
            select(t.integration_batch_members.c.task_id).where(
                t.integration_batch_members.c.batch_id.in_(batch_ids)
            )
        )).scalars())
    task_ids = [value for key, value in refs if key == "task"]
    refs.update(("operation", value) for value in (await conn.execute(
        select(t.integration_repair_operations.c.id).where(or_(
            t.integration_repair_operations.c.batch_id.in_(batch_ids),
            t.integration_repair_operations.c.parent_task_id.in_(task_ids),
        ))
    )).scalars())
    return refs


async def history_on(conn, project_id: str, refs=None) -> list[dict]:
    table = t.operator_decisions
    query = select(table).where(table.c.project_id == project_id)
    if refs is not None:
        if not refs:
            return []
        query = query.where(or_(*[
            and_(table.c.object_kind == kind, table.c.object_id == identity)
            for kind, identity in refs
        ]))
    rows = [dict(row) for row in (await conn.execute(
        query.order_by(table.c.created_at, table.c.id)
    )).mappings()]
    released = {row["releases"] for row in rows if row["effect"] == "release"}
    return [{**row, "active": row["effect"] == "hold" and row["id"] not in released}
            for row in rows]


class OperatorDecisions:
    def __init__(self, db):
        self.db = db

    async def history(self, kind: str, identity: str) -> list[dict]:
        async with self.db._engine.connect() as conn:
            project = await object_project_on(conn, kind, identity)
            return await history_on(conn, project, await related_on(conn, kind, identity))

    async def holds(self, kind: str, identity: str) -> list[dict]:
        return [row for row in await self.history(kind, identity) if row["active"]]

    async def record(self, values: dict, *, recorded_by: str) -> dict:
        values = dict(values)
        async with self.db._engine.begin() as conn:
            project = await object_project_on(conn, values["object_kind"], values["object_id"])
            # Serialize record/release and idempotency checks across supervisors.
            await conn.execute(select(t.projects.c.id).where(t.projects.c.id == project)
                               .with_for_update())
            table = t.operator_decisions
            old = (await conn.execute(select(table).where(
                table.c.project_id == project,
                table.c.idempotency_key == values["idempotency_key"],
            ))).mappings().first()
            if old:
                if any(old[key] != value for key, value in values.items()):
                    raise ValueError("idempotency key already records a different decision")
                return dict(old)
            release = values.get("releases")
            if values["effect"] == "release":
                history = await history_on(conn, project, {
                    (values["object_kind"], values["object_id"]),
                })
                if not any(row["id"] == release and row["active"] for row in history):
                    raise ValueError("release must name an active hold on this exact object")
            elif release:
                raise ValueError("only a release may name a hold")
            row = {**values, "id": str(uuid.uuid4()), "project_id": project,
                   "created_at": time.time(), "recorded_by": recorded_by}
            await conn.execute(insert(table).values(**row))
            return row


# Diagnostics stay usable while held. Other integration controls cannot override
# instructions by choosing another command or by being a different supervisor.
READ_COMMANDS = frozenset({
    "integration_status", "integration_trust_manifest", "integration_app_verify",
    "integration_delivery_readiness", "delivery_receipts",
})


def decision_control(name: str, args: dict) -> bool:
    """Commands without object identity must reach their normal validation first."""
    return (
        (name.startswith("integration_") or name == "delivery_promote")
        and name not in READ_COMMANDS
        and any(args.get(key) for key in (
            "task_id", "parent_task_id", "batch_id", "operation_id", "source_task_id",
            "member_task_id", "repair_task_id", "operation_key", "subject_id", "project_id",
            "repository_id",
        ))
    )


async def control_refusal(db, name: str, args: dict) -> dict | None:
    if not decision_control(name, args):
        return None
    async with db._engine.connect() as conn:
        refs = set()
        projects = set()
        for field, kind in (("task_id", "task"), ("parent_task_id", "task"),
                            ("batch_id", "batch"), ("operation_id", "operation"),
                            ("source_task_id", "task"), ("member_task_id", "task"),
                            ("repair_task_id", "task"), ("operation_key", "operation")):
            if args.get(field):
                try:
                    project = await object_project_on(conn, kind, args[field])
                except ValueError:
                    continue  # The owning command reports its missing object.
                projects.add(project)
                refs.update(await related_on(conn, kind, args[field]))
        if args.get("subject_id"):
            subject = (await conn.execute(select(t.integration_subjects).where(
                t.integration_subjects.c.id == args["subject_id"],
            ))).mappings().first()
            if subject:
                projects.add(subject["project_id"])
                for key, kind in (("task_id", "task"), ("batch_id", "batch")):
                    if subject.get(key):
                        refs.update(await related_on(conn, kind, subject[key]))
        if args.get("project_id"):
            projects.add(args["project_id"])
        if args.get("repository_id"):
            repository = (await conn.execute(select(t.repos).where(
                t.repos.c.id == args["repository_id"],
            ))).mappings().first()
            if repository:
                projects.add(repository["project_id"])
        from src.commands.principal import current_principal
        principal = current_principal()
        if principal and principal.project_id and projects - {principal.project_id}:
            return {"success": False, "error": "decision control belongs to another project"}
        held = []
        for project in sorted(projects):
            held.extend(row for row in await history_on(conn, project, refs or None)
                        if row["active"])
    if held:
        return {"success": False, "outcome": "operator_decision_hold",
                "error": "Conflicting control blocked by operator decision: " +
                         "; ".join(f"{row['id']}: {row['decision']}" for row in held),
                "operator_decisions": held}
    return None
