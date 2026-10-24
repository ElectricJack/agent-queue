"""Bounded archive-first collection, isolated from the daemon's loop and pool."""

from __future__ import annotations

import asyncio
import base64
import datetime as dt
import fcntl
import gzip
import json
import os
import shutil
import tempfile
from pathlib import Path

from sqlalchemy import and_, delete, func, literal_column, or_, select, tuple_
from sqlalchemy.ext.asyncio import create_async_engine

from src.database import tables as t
from src.playbooks.run_state import TERMINAL_LIFECYCLES

DAY = 86400


def policies(now):
    """Return ordered tables and eligibility predicates; no cached verdicts."""
    terminal = tuple(state.value for state in TERMINAL_LIFECYCLES)
    runs = t.playbook_v2_runs
    parent = runs.alias("retention_parent")
    child = runs.alias("retention_child")
    old = and_(runs.c.lifecycle.in_(terminal), runs.c.completed_at < now - 30 * DAY)
    live_owner = select(runs.c.run_id).where(
        runs.c.run_id == t.playbook_private_values.c.owner_id,
        runs.c.lifecycle.not_in(terminal),
    ).exists()
    old_ids = select(runs.c.run_id).where(old)
    values = t.playbook_private_values
    no_live_parent = ~select(parent.c.run_id).where(
        parent.c.run_id == runs.c.parent_run_id, parent.c.lifecycle.not_in(terminal),
    ).exists()
    no_children = ~select(child.c.run_id).where(child.c.parent_run_id == runs.c.run_id).exists()
    unreferenced = [
        ~select(table.c[column]).where(table.c[column] == runs.c.run_id).exists()
        for table, column in (
            (values, "owner_id"), (t.playbook_step_receipts, "run_id"),
            (t.playbook_waits, "run_id"), (t.task_assignment_routes, "playbook_run_id"),
            (t.workflows, "playbook_run_id"), (t.jobs, "playbook_run_id"),
        )
    ]
    cursor = select(t.wait_listener_cursors.c.last_seq).where(
        t.wait_listener_cursors.c.listener_key == "source",
    ).scalar_subquery()
    return [
        (t.token_ledger, t.token_ledger.c.timestamp < now - 30 * DAY),
        (values, and_(
            ~and_(values.c.owner_kind == "run", live_owner),
            or_(values.c.expires_at < now,
                and_(values.c.owner_kind == "run", values.c.owner_id.in_(old_ids))),
        )),
        (t.playbook_step_receipts, t.playbook_step_receipts.c.run_id.in_(old_ids)),
        (t.playbook_waits, t.playbook_waits.c.run_id.in_(old_ids)),
        (t.task_assignment_routes, t.task_assignment_routes.c.playbook_run_id.in_(old_ids)),
        (runs, and_(old, no_live_parent, no_children, *unreferenced)),
        (t.wait_source_events, and_(
            t.wait_source_events.c.committed_at < now - 14 * DAY,
            t.wait_source_events.c.dispatched_at.is_not(None),
            t.wait_source_events.c.seq <= cursor,
        )),
        (t.events, t.events.c.timestamp < now - 30 * DAY),
    ]


def _encode(value):
    if isinstance(value, bytes):
        return {"$base64": base64.b64encode(value).decode("ascii")}
    raise TypeError(f"Unsupported archive value: {type(value).__name__}")


def append_archive(root, table, rows, now):
    """Append a complete gzip member and fsync it before permitting deletion."""
    root = Path(root)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    date = dt.datetime.fromtimestamp(now, dt.timezone.utc).date().isoformat()
    path = root / f"{table}-archive-{date}.jsonl.gz"
    payload = b"".join(
        (json.dumps(dict(row), default=_encode, separators=(",", ":")) + "\n").encode()
        for row in rows
    )
    compressed = gzip.compress(payload)
    # Replace atomically: a killed gzip append could leave a partial member
    # that makes all subsequent batches unreadable. The separate lock survives
    # rename and also serializes operator runs against periodic collection.
    fd = os.open(str(path) + ".lock", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "ab") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=root, delete=False) as stream:
                temporary = stream.name
                if path.exists():
                    old_fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                    with os.fdopen(old_fd, "rb") as old:
                        shutil.copyfileobj(old, stream)
                stream.write(compressed)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return str(path)


async def sweep(engine, backup_dir, *, now, dry_run=False, batch_size=1000):
    """One bounded pass. Caller must run this on a maintenance worker loop."""
    if not 1 <= batch_size <= 1000:
        raise ValueError("batch_size must be between 1 and 1000")
    counts = {}
    for table, eligible in policies(now):
        key = next(iter(table.primary_key.columns))
        version = literal_column(f"{table.name}.xmin::text")
        statement = (
            select(func.count()).select_from(table).where(eligible) if dry_run else
            select(table, version.label("_archive_xmin")).where(eligible)
            .order_by(key).limit(batch_size)
        )
        async with engine.connect() as conn:
            result = await conn.execute(statement)
            if dry_run:
                counts[table.name] = result.scalar_one()
                continue
            rows = result.mappings().all()
        if not rows:
            counts[table.name] = 0
            continue
        # Neither serialization nor compression nor filesystem I/O holds a DB connection.
        append_archive(backup_dir, table.name, rows, now)
        identities = [(row[key.name], row["_archive_xmin"]) for row in rows]
        statement = delete(table).where(eligible, tuple_(key, version).in_(identities))
        async with engine.begin() as conn:
            if table is t.playbook_v2_runs:
                # Fence concurrent FK children before the fresh DELETE snapshot.
                await conn.execute(select(key).where(key.in_([pair[0] for pair in identities]))
                                   .with_for_update())
            result = await conn.execute(statement)
        counts[table.name] = result.rowcount
    return counts


def run_worker(url, backup_dir, *, now, dry_run=False, batch_size=1000):
    """Own the engine in this thread; never share asyncpg connections across loops."""
    async def run():
        engine = create_async_engine(
            url, pool_size=1, max_overflow=0,
            connect_args={"server_settings": {"statement_timeout": "10000",
                                               "lock_timeout": "1000"}},
        )
        try:
            return await sweep(engine, backup_dir, now=now, dry_run=dry_run,
                               batch_size=batch_size)
        finally:
            await engine.dispose()
    return asyncio.run(run())
