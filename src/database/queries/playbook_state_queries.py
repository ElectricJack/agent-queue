"""State transaction primitives. Lock namespace before journal/entry/accounting.

D2 joins these helpers and message insertion using the same SQL connection.
There are no process-local locks and no automatic tombstone/journal deletion.
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from sqlalchemy import func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.database import tables as t


async def transaction_time(conn) -> float:
    return float(await conn.scalar(select(func.extract("epoch", func.transaction_timestamp()))))


async def namespace_row(conn, namespace, *, lock=False):
    query = select(t.playbook_state_namespaces).where(
        t.playbook_state_namespaces.c.namespace_id == namespace.identifier)
    if lock:
        query = query.with_for_update()
    return (await conn.execute(query)).mappings().first()


@asynccontextmanager
async def locked_namespace(engine, namespace, *, provision=False):
    from src.playbooks.state import StateError

    async with engine.begin() as conn:
        row = await namespace_row(conn, namespace)
        if row is None:
            if not provision:
                table = t.playbook_state_namespaces
                prior_version = await conn.scalar(select(table.c.state_schema_version).where(
                    table.c.scope_kind == namespace.scope_kind,
                    table.c.scope_owner == namespace.scope_owner,
                    table.c.playbook_id == namespace.playbook_id,
                ).limit(1))
                if namespace.state_schema_version != 1 or prior_version is not None:
                    raise StateError("state.migration_required")
            now = await transaction_time(conn)
            await conn.execute(pg_insert(t.playbook_state_namespaces).values(
                namespace_id=namespace.identifier, **namespace.as_dict(),
                created_at=now, updated_at=now,
            ).on_conflict_do_nothing())
        row = await namespace_row(conn, namespace, lock=True)
        yield conn, row


async def entry_row(conn, namespace, space, key, *, lock=False):
    query = select(t.playbook_state_entries).where(
        t.playbook_state_entries.c.namespace_id == namespace.identifier,
        t.playbook_state_entries.c.record_space == space,
        t.playbook_state_entries.c.key == key,
    )
    if lock:
        query = query.with_for_update()
    return (await conn.execute(query)).mappings().first()


async def operation_row(conn, namespace, operation_key):
    return (await conn.execute(select(t.playbook_state_operations).where(
        t.playbook_state_operations.c.namespace_id == namespace.identifier,
        t.playbook_state_operations.c.operation_key == operation_key,
    ))).mappings().first()


async def write_entry(conn, namespace, values):
    table = t.playbook_state_entries
    row = {"namespace_id": namespace.identifier, **values}
    await conn.execute(pg_insert(table).values(**row).on_conflict_do_update(
        index_elements=[table.c.namespace_id, table.c.record_space, table.c.key],
        set_={key: value for key, value in values.items()
              if key not in {"record_space", "key", "created_at"}},
    ))


async def commit_operation(conn, namespace, locked, *, operation_key, request_digest,
                           result_reference, result, journal_bytes, value_bytes, key_count,
                           limits, now, replay_floor=None):
    """Validate all accounting before commit; callers' earlier inserts roll back on refusal."""
    from src.playbooks.state import StateError

    total_journal = locked["journal_bytes"] + journal_bytes
    if (key_count > limits.keys or value_bytes > limits.namespace_bytes
            or total_journal > limits.journal_bytes):
        raise StateError("state.quota_exceeded")
    await conn.execute(insert(t.playbook_state_operations).values(
        namespace_id=namespace.identifier, operation_key=operation_key,
        request_digest=request_digest, result_reference=result_reference,
        byte_count=journal_bytes, revision=result["revision"],
        message_id=result.get("message_id"), committed_at=now,
    ))
    accounting = dict(revision=locked["revision"] + 1, value_bytes=value_bytes,
                      key_count=key_count, journal_bytes=total_journal, updated_at=now)
    if replay_floor is not None:
        accounting["replay_floor"] = max(locked["replay_floor"] or replay_floor, replay_floor)
    await conn.execute(update(t.playbook_state_namespaces).where(
        t.playbook_state_namespaces.c.namespace_id == namespace.identifier).values(**accounting))


async def snapshot_rows(engine, namespace, *, record_space, prefix):
    """Read pre-scan revisions and encrypted references from one stable transaction."""
    from src.database.queries.observation_snapshot_queries import consistent_read

    async with consistent_read(engine) as conn:
        locked = await namespace_row(conn, namespace)
        now = await transaction_time(conn)
        query = select(t.playbook_state_entries).where(
            t.playbook_state_entries.c.namespace_id == namespace.identifier,
            t.playbook_state_entries.c.record_space == record_space,
            t.playbook_state_entries.c.key.startswith(prefix, autoescape=True),
        ).order_by(t.playbook_state_entries.c.key)
        rows = [dict(row) for row in (await conn.execute(query.limit(10_001))).mappings()]
    return rows, locked["revision"] if locked else 0, now


def check_observation_floor(namespace_row, entry, observed_at):
    """D2 must apply this before accepting observation evidence, including cleared rows."""
    from src.playbooks.state import StateError

    floors = [row.get("replay_floor") for row in (namespace_row, entry or {})]
    if any(floor is not None and observed_at <= floor for floor in floors):
        raise StateError("state.retired_observation")
