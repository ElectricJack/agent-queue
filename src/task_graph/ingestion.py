"""Read-only ingestion membership shared by task and graph projections."""

from src.database.queries.ingest_batch_queries import task_batch_summaries


async def load_ingestion(db, rows: list[dict]) -> dict[str, dict]:
    """Read only the already-visible task ids; never infer holds from READY."""
    if not rows:
        return {}
    async with db._engine.connect() as conn:
        summaries = await task_batch_summaries(conn, [row["id"] for row in rows])
    return {tid: {"held_batch": batch} for tid, batch in summaries.items()}


async def attach_ingestion(db, rows: list[dict]) -> None:
    fields = await load_ingestion(db, rows)
    for row in rows:
        row.update(fields.get(row["id"], {}))
