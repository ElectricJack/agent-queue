"""Bounded, content-free cost/ambiguity diagnostics; never inspect a provider."""

from sqlalchemy import func, select

from src.knowledge.extraction_store import ExtractionStore


async def generation_status(db):
    store = ExtractionStore()
    async with db.immediate() as conn:
        jobs = (
            (
                await conn.execute(
                    select(
                        store.jobs.c.scope_key,
                        store.jobs.c.state,
                        store.jobs.c.error_code,
                        func.count().label("count"),
                    )
                    .group_by(store.jobs.c.scope_key, store.jobs.c.state, store.jobs.c.error_code)
                    .order_by(store.jobs.c.scope_key, store.jobs.c.state)
                    .limit(100)
                )
            )
            .mappings()
            .all()
        )
        budgets = (
            (
                await conn.execute(
                    select(store.budgets)
                    .order_by(
                        store.budgets.c.period_start.desc(),
                        store.budgets.c.scope_key,
                        store.budgets.c.feature,
                    )
                    .limit(100)
                )
            )
            .mappings()
            .all()
        )
        unknown = await conn.scalar(
            select(func.count())
            .select_from(store.reservations)
            .where(
                store.reservations.c.state == "unknown",
            )
        )
    return dict(
        success=True,
        jobs=[dict(row) for row in jobs],
        unknown_calls=unknown,
        budgets=[{**dict(row), "period_start": row["period_start"].isoformat()} for row in budgets],
        page_limit=100,
    )
