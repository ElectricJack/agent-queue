"""Compose ordinary task filing with an exact informational knowledge link."""

import asyncio
import random
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from pydantic import ValidationError

from src.commands.contracts.knowledge import KnowledgeCreateTaskArgs

from src.database.tables import records, task_gates, task_record_link_state, tasks
from src.records.models import RecordError, uuid_value


class FilingReplay(Exception):
    """Roll back a competing filing before returning its committed receipt."""

    def __init__(self, result):
        self.result = result


class FilingPending(Exception):
    """Roll back a preflight receipt; the task transaction will own it."""


async def add_task_link_on(service, conn, task_record, target, revision_id, access, link_type):
    """Internal writer for a newly filed task or an authorized Save finding."""
    await service._lock_source(task_record, conn=conn)
    state = (
        (
            await conn.execute(
                select(task_record_link_state)
                .where(task_record_link_state.c.record_id == task_record["record_id"])
                .with_for_update()
            )
        )
        .mappings()
        .one()
    )
    operations = service._normalize_operations(
        [
            {
                "action": "add",
                "target": f"record:{target['record_id']}",
                "target_revision_id": str(revision_id),
                "link_type": link_type,
            }
        ]
    )
    _, changes = await service._plan_links(task_record, operations, access, conn=conn)
    now, token = datetime.now(UTC), uuid4()
    await service._write_links(task_record, changes, access, None, now, conn=conn)
    await conn.execute(
        update(task_record_link_state)
        .where(task_record_link_state.c.record_id == task_record["record_id"])
        .values(link_token=token, link_sequence=state["link_sequence"] + 1, updated_at=now)
    )
    await conn.execute(
        update(records)
        .where(records.c.record_id == task_record["record_id"])
        .values(updated_at=now)
    )
    result = {
        "success": True,
        "record_id": str(task_record["record_id"]),
        "link_token": str(token),
        "link_id": changes[0]["link_id"],
    }
    await service._outbox(task_record, result, access, "link_create", now, conn=conn)
    return result


async def create_task_from_knowledge(handler, args, principal):
    """Bound the composed request, retrying only rolled-back database conflicts."""
    try:
        args = KnowledgeCreateTaskArgs.model_validate(args).model_dump(exclude_none=True)
    except ValidationError as exc:
        raise RecordError("record.invalid_input", "Invalid task filing fields") from exc
    service = handler._knowledge_service()
    try:
        async with asyncio.timeout(5):
            async with service._request_slots:
                for attempt in range(3):
                    try:
                        return await _file_once(handler, args, principal)
                    except DBAPIError as exc:
                        if getattr(exc.orig, "sqlstate", None) in {"40P01", "40001"}:
                            if attempt == 2:
                                raise RecordError("record.retryable") from exc
                            await asyncio.sleep(random.uniform(0.01, 0.04) * (attempt + 1))
                        elif isinstance(exc, IntegrityError):
                            raise RecordError("record.integrity_conflict") from exc
                        else:
                            raise
    except TimeoutError as exc:
        raise RecordError("record.retryable") from exc


async def _file_once(handler, args, principal):
    """No task conversion, route overrides, or independent link transaction.

    Preflight verifies the source and finds completed receipts without filing.
    Its uncommitted placeholder is rolled back. The existing task filer's
    internal callback rechecks authority and claims the receipt under a row
    lock. A concurrent replay raises out of that transaction, rolling back
    the duplicate task, its quota reservation, edges and gates before return.
    """
    service = handler._knowledge_service()
    project_id = args.get("project_id")
    revision_id = str(uuid_value(args.get("revision_id"), "revision_id"))
    task_fields = {
        key: args[key]
        for key in ("title", "description", "priority", "task_type", "parent_id", "root", "reason")
        if key in args
    }
    allowed = set(task_fields) | {
        "identity",
        "revision_id",
        "project_id",
        "idempotency_key",
        "claim_epoch",
    }
    if set(args) - allowed:
        raise RecordError("record.invalid_input", "Unsupported task filing fields")
    if not isinstance(task_fields.get("title"), str) or not task_fields["title"].strip():
        raise RecordError("record.invalid_input", "Select a task title")
    if (
        not isinstance(task_fields.get("description"), str)
        or not task_fields["description"].strip()
    ):
        raise RecordError("record.invalid_input", "Select task description text")
    request = dict(identity=args.get("identity"), revision_id=revision_id, task=task_fields)
    key, epoch = args.get("idempotency_key"), args.get("claim_epoch")

    async def prepare(conn):
        access = await service._access(
            conn, principal, "knowledge_create_task", project_id, write=True, claim_epoch=epoch
        )
        await service._access(
            conn, principal, "create_task", project_id, write=True, claim_epoch=epoch
        )
        record = await service.resolve_on(args.get("identity"), access, conn=conn)
        if record["kind"] != "knowledge":
            raise RecordError("record.not_found")
        revision = await service._revision(record, revision_id, conn=conn)
        await service._validate_snapshot_access(revision["snapshot"], access, conn=conn)
        receipt, replay = await service._receipt(
            conn, access, "knowledge_create_task", key, request
        )
        if replay:
            raise FilingReplay(replay)
        return access, record, receipt

    async def replay_only():
        try:
            async with handler.db.immediate() as conn:
                await prepare(conn)
                raise FilingPending()
        except FilingReplay as exc:
            return exc.result
        except FilingPending:
            return None

    replay = await replay_only()
    if replay is not None:
        return replay

    result = None

    async def after_create_on(conn, task_id, parent_id):
        nonlocal result
        access, record, receipt = await prepare(conn)
        # The ordinary filer has authorized and inserted this new task. Only
        # this callback can write its first link, even for a task-scoped worker.
        task_record = await handler.db.ensure_task_record_on(
            task_id, actor_id=access.actor_key, conn=conn
        )
        link = await add_task_link_on(
            service, conn, task_record, record, revision_id, access, "motivated_by"
        )
        task = (await conn.execute(select(tasks).where(tasks.c.id == task_id))).mappings().one()
        result = dict(
            success=True,
            outcome="created",
            task_id=task_id,
            record_id=str(record["record_id"]),
            revision_id=revision_id,
            task_record_id=str(task_record["record_id"]),
            link_id=link["link_id"],
            parent_id=parent_id,
            route_source=task["route_source"],
            status=task["status"],
            gate_ids=list(
                (
                    await conn.execute(
                        select(task_gates.c.gate_id)
                        .where(task_gates.c.task_id == task_id)
                        .order_by(task_gates.c.gate_id)
                    )
                ).scalars()
            ),
        )
        await handler.db.finish_record_request_on(receipt, result, conn=conn)

    try:
        filed = await handler._cmd_create_task(
            {**task_fields, "project_id": project_id, "_after_create_on": after_create_on}
        )
    except FilingReplay as exc:
        return exc.result
    if filed.get("error") or filed.get("success") is False:
        # A concurrent winner may have consumed the last filing allowance
        # before this filer reached its callback. Recheck its receipt without
        # bypassing authorization or reserving another task.
        replay = await replay_only()
        return replay if replay is not None else filed
    return result
