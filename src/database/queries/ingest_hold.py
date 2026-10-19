"""The ingestion hold is independent of task status, routing and ordinary gates."""

from src.database.tables import tasks


def not_ingest_held(task_table=tasks):
    """Admit only tasks released from their ingestion batch, including aliases."""
    return task_table.c.held_batch_id.is_(None)


class IngestHeld(RuntimeError):
    """A hold acquired during preparation prevents claim activation."""

    def __init__(self):
        super().__init__("ingest_held")
