"""PostgreSQL snapshot persistence, serialized logical quotas and stable scans."""

from __future__ import annotations

import hashlib
import time
from contextlib import asynccontextmanager

from sqlalchemy import case, delete, func, insert, select, text

from src.database import tables as t
from src.observations.snapshots import MAX_IDENTITIES, Limits, SnapshotError, encoded

# One transaction-scoped lock serializes admission and cleanup across processes.
# Quotas need a host-wide lock, not a Python lock or a stale repeatable-read sum.
_QUOTA_LOCK = 71612509234871


@asynccontextmanager
async def consistent_read(engine):
    async with engine.connect() as connection:
        connection = await connection.execution_options(isolation_level="REPEATABLE READ")
        async with connection.begin():
            yield connection


async def bounded_rows(conn, query, *, limit=MAX_IDENTITIES):
    """Bound retained source data as it arrives, not after fetching a whole table."""
    rows, byte_count = [], 0
    async with conn.stream(query.limit(limit + 1)) as stream:
        async for row in stream.mappings():
            byte_count += len(encoded(dict(row)))
            if len(rows) >= limit:
                raise SnapshotError("snapshot.too_many_rows")
            if byte_count > Limits().per_run:
                raise SnapshotError("snapshot.source_oversize")
            rows.append(row)
    return rows


async def check_quotas(conn, row, limits, *, additional=0):
    table = t.observation_snapshots
    # Expired rows still occupy storage until the bounded collector removes
    # them. Otherwise admission could outrun cleanup and exceed the host cap.
    usage = (await conn.execute(select(
        func.coalesce(func.sum(table.c.byte_count), 0),
        func.coalesce(func.sum(case((
            (table.c.scope_kind == row["scope_kind"])
            & (table.c.scope_owner == row["scope_owner"]), table.c.byte_count), else_=0)), 0),
        func.coalesce(func.sum(case((table.c.run_id == row["run_id"], table.c.byte_count),
                                    else_=0)), 0),
    ))).one()
    if (usage[0] + additional > limits.host or usage[1] + additional > limits.per_scope
            or (row["run_id"] and usage[2] + additional > limits.per_run)):
        raise SnapshotError("snapshot.quota_exceeded")


async def store_snapshot(engine, row, pages, batch, limits):
    async with engine.begin() as conn:
        await conn.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _QUOTA_LOCK})
        await check_quotas(conn, row, limits, additional=row["byte_count"])
        await conn.execute(insert(t.observation_snapshots).values(**row))
        await batch.persist(conn)
        if pages:
            await conn.execute(insert(t.observation_snapshot_pages), pages)


async def load_manifest(engine, snapshot_id, *, limits=Limits()):
    async with engine.connect() as conn:
        row = (await conn.execute(select(t.observation_snapshots).where(
            t.observation_snapshots.c.snapshot_id == snapshot_id,
        ))).mappings().first()
        if row is None:
            raise SnapshotError("snapshot.unavailable")
        await check_quotas(conn, row, limits)
    return row


async def load_page(engine, snapshot_id, token, limits):
    async with engine.connect() as conn:
        row = (await conn.execute(select(t.observation_snapshots).where(
            t.observation_snapshots.c.snapshot_id == snapshot_id,
        ))).mappings().first()
        if row is None:
            raise SnapshotError("snapshot.unavailable")
        if row["expires_at"] <= time.time():
            raise SnapshotError("snapshot.expired")
        await check_quotas(conn, row, limits)
        page = (await conn.execute(select(t.observation_snapshot_pages).where(
            t.observation_snapshot_pages.c.snapshot_id == snapshot_id,
            t.observation_snapshot_pages.c.token_hash == hashlib.sha256(token.encode()).hexdigest(),
        ))).mappings().first()
        if page is None:
            raise SnapshotError("snapshot.page_unavailable")
        count = await conn.scalar(select(func.count()).select_from(t.observation_snapshot_pages)
                                  .where(t.observation_snapshot_pages.c.snapshot_id == snapshot_id))
        if count != row["page_count"]:
            raise SnapshotError("snapshot.page_unavailable")
    return row, page


async def cleanup_expired(engine, *, limit=100):
    """Bounded background maintenance; never evict an unexpired observation."""
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("cleanup limit must be between 1 and 100")
    async with engine.begin() as conn:
        await conn.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _QUOTA_LOCK})
        ids = list((await conn.execute(select(t.observation_snapshots.c.snapshot_id)
                    .where(t.observation_snapshots.c.expires_at <= time.time())
                    .order_by(t.observation_snapshots.c.expires_at).limit(limit)
                    .with_for_update(skip_locked=True))).scalars())
        if ids:
            await conn.execute(delete(t.observation_snapshots)
                               .where(t.observation_snapshots.c.snapshot_id.in_(ids)))
            await conn.execute(delete(t.playbook_private_values).where(
                t.playbook_private_values.c.owner_kind == "snapshot",
                t.playbook_private_values.c.owner_id.in_(ids),
            ))
    return len(ids)


async def project_rows(engine, project_id=None):
    workspace = select(t.workspaces.c.workspace_path).where(
        t.workspaces.c.project_id == t.projects.c.id,
    ).order_by(case((t.workspaces.c.source_type == "clone", 0), else_=1), t.workspaces.c.id
               ).limit(1).scalar_subquery()
    fields = ("id", "name", "status", "credit_weight", "max_concurrent_agents",
              "supervisor_enabled", "repo_url", "assignment_playbook_id")
    query = select(*(t.projects.c[name] for name in fields), workspace.label("workspace"))
    if project_id:
        query = query.where(t.projects.c.id == project_id)
    async with consistent_read(engine) as conn:
        rows = await bounded_rows(conn, query.order_by(t.projects.c.id))
    return [dict(row) for row in rows]


async def task_rows(handler, args):
    """SQL filtering and bounded materialization without the legacy flat cap."""
    from src.database.queries.task_queries import apply_label_filters
    from src.models import BLOCKING_DEP_TYPES

    fields = ("id", "project_id", "title", "status", "priority", "profile_id",
              "intelligence_class", "provider_intent", "rerouted_from", "parent_task_id",
              "is_plan_subtask", "task_type", "pr_url", "integration_mode", "created_at",
              "updated_at")
    query = select(*(t.tasks.c[name] for name in fields),
                   t.tasks.c.assigned_agent_id.label("assigned_agent"))
    if args.get("project_id"):
        query = query.where(t.tasks.c.project_id == args["project_id"])
    if args.get("status"):
        query = query.where(t.tasks.c.status == args["status"])
    elif args.get("completed_only"):
        query = query.where(t.tasks.c.status == "COMPLETED")
    elif not args.get("include_completed") and not args.get("show_all"):
        query = query.where(t.tasks.c.status != "COMPLETED")
    query = apply_label_filters(query, labels=args.get("labels"), any_label=args.get("any_label"))
    query = query.order_by(t.tasks.c.priority, t.tasks.c.created_at, t.tasks.c.id)
    async with consistent_read(handler.db._engine) as conn:
        rows = await bounded_rows(conn, query)
        result = [dict(row, is_plan_subtask=bool(row["is_plan_subtask"])) for row in rows]
        if args.get("show_dependencies"):
            # Both directions and upstream statuses belong to this same read.
            ids = [row["id"] for row in result]
            other = t.tasks.alias("other")
            deps = t.task_dependencies
            for output, local, remote in (
                ("depends_on", deps.c.task_id, deps.c.depends_on_task_id),
                ("blocks", deps.c.depends_on_task_id, deps.c.task_id),
            ):
                edges = await bounded_rows(conn, select(local.label("local"), other.c.id,
                                                       other.c.project_id,
                                                       other.c.status, other.c.title)
                                           .join(other, other.c.id == remote)
                                           .where(local.in_(ids),
                                                  deps.c.dep_type.in_(sorted(BLOCKING_DEP_TYPES)))
                                           .distinct().order_by(local, other.c.id))
                grouped = {}
                for edge in edges:
                    if args.get("project_id") and edge["project_id"] != args["project_id"]:
                        raise SnapshotError("snapshot.source_scope_mismatch")
                    grouped.setdefault(edge["local"], []).append(
                        {key: edge[key] for key in ("id", "status", "title")})
                for row in result:
                    row[output] = grouped.get(row["id"], [])
    return result
