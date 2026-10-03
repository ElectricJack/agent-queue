"""Resumable deterministic task mappings; never mutates execution rows."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.database.queries.record_queries import RecordDomainUnavailable
from src.database.tables import archived_tasks, projects, record_backfill_state, records, tasks
from src.records.identity import RecordIntegrityError

SOURCES = {"tasks": tasks, "archived_tasks": archived_tasks}
BATCH_SIZE = 500


async def task_mapping_inventory(db):
    """Counts are projections, including orphan projects and duplicate aliases."""
    async with db.immediate() as conn:
        report = {}
        for source, table in SOURCES.items():
            mapped = select(records.c.record_id).where(records.c.task_id == table.c.id).exists()
            available = select(projects.c.id).where(projects.c.id == table.c.project_id).exists()
            report[source] = dict(
                total=await conn.scalar(select(func.count()).select_from(table)),
                missing=await conn.scalar(select(func.count()).select_from(table).where(~mapped)),
                unavailable_projects=await conn.scalar(
                    select(func.count()).select_from(table).where(~available)
                ),
            )
        report["duplicate_aliases"] = await conn.scalar(
            select(func.count()).select_from(
                tasks.join(archived_tasks, tasks.c.id == archived_tasks.c.id)
            )
        )
        return report


class TaskRecordBackfill:
    def __init__(self, db, *, batch_size=BATCH_SIZE):
        if not 1 <= batch_size <= BATCH_SIZE:
            raise ValueError("task mapping batch size must be 1–500")
        self.db = db
        self.batch_size = batch_size

    async def batch(self, source):
        """The cursor and mappings commit together, including reconciliation.

        Each source has its own lock/cursor. Once its ordered scan ends, an
        anti-join finds unmapped tasks inserted below the cursor or moved from
        the other domain while scanning. Missing projects remain reportable.
        """
        table = SOURCES[source]
        async with self.db.immediate() as conn:
            await conn.execute(
                pg_insert(record_backfill_state).values(source=source).on_conflict_do_nothing()
            )
            state = (
                (
                    await conn.execute(
                        select(record_backfill_state)
                        .where(record_backfill_state.c.source == source)
                        .with_for_update()
                    )
                )
                .mappings()
                .one()
            )
            stmt = select(table.c.id).order_by(table.c.id).limit(self.batch_size)
            if state["cursor"] is not None:
                stmt = stmt.where(table.c.id > state["cursor"])
            ids = list((await conn.execute(stmt)).scalars())
            reconciliation = not ids
            if reconciliation:
                ids = list(
                    (
                        await conn.execute(
                            select(table.c.id)
                            .join(projects, projects.c.id == table.c.project_id)
                            .where(
                                ~select(records.c.record_id)
                                .where(records.c.task_id == table.c.id)
                                .exists()
                            )
                            .order_by(table.c.id)
                            .limit(self.batch_size)
                        )
                    ).scalars()
                )
            inserted = unavailable = 0
            for task_id in ids:
                try:
                    _, created = await self.db.ensure_task_record_on(
                        task_id,
                        actor_id="local-operator:task-mapping-backfill",
                        conn=conn,
                        return_inserted=True,
                    )
                except (RecordDomainUnavailable, LookupError):
                    unavailable += 1
                    continue
                inserted += created
            values = dict(
                scanned=state["scanned"] + len(ids),
                inserted=state["inserted"] + inserted,
                updated_at=datetime.now(UTC),
            )
            if ids and not reconciliation:
                values["cursor"] = ids[-1]
            await conn.execute(
                update(record_backfill_state)
                .where(record_backfill_state.c.source == source)
                .values(**values)
            )
        return dict(
            source=source,
            scanned=len(ids),
            inserted=inserted,
            unavailable=unavailable,
            reconciliation=reconciliation,
            done=not ids,
        )

    async def run(self, *, dry_run=True, max_batches=2):
        if not 1 <= max_batches <= 20:
            raise ValueError("max_batches must be 1–20")
        before = await task_mapping_inventory(self.db)
        if dry_run:
            return {"success": True, "outcome": "read", "dry_run": True, "inventory": before}
        if before["duplicate_aliases"]:
            raise RecordIntegrityError("task alias exists in both live and archive domains")
        async with self.db.immediate() as conn:
            last_runs = dict(
                (
                    await conn.execute(
                        select(record_backfill_state.c.source, record_backfill_state.c.updated_at)
                    )
                ).all()
            )
        # Even repeated one-batch requests alternate sources across restarts.
        sources = sorted(
            SOURCES, key=lambda source: last_runs.get(source, datetime.min.replace(tzinfo=UTC))
        )
        batches, done = [], set()
        for ordinal in range(max_batches):
            source = sources[ordinal % len(sources)]
            if source in done:
                source = next((name for name in SOURCES if name not in done), None)
            if source is None:
                break
            result = await self.batch(source)
            batches.append(result)
            if result["done"]:
                done.add(source)
            if ordinal + 1 < max_batches:
                await asyncio.sleep(0.05)
        return dict(
            success=True,
            outcome="updated",
            dry_run=False,
            batches=batches,
            inventory=await task_mapping_inventory(self.db),
            done=len(done) == len(SOURCES),
        )
