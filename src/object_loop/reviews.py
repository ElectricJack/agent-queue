"""The review audience follows the stored object experiment, not worker prose."""

from __future__ import annotations

import re

from sqlalchemy import select

from src.database.tables import object_loops


async def experiment_context(db, task_id: str | None) -> dict | None:
    """Include unmarked probe children filed beneath an experiment task.

    Hierarchy depth is capped at three. A visited set also bounds corrupt
    ancestry without an unbounded lookup.
    """
    seen: set[str] = set()
    while task_id and task_id not in seen and len(seen) < 3:
        seen.add(task_id)
        marker = await db.get_task_meta(task_id, "object_experiment")
        if isinstance(marker, dict) and marker.get("object_id"):
            # Only the marked finalizer itself may submit the human result.
            return {**marker, "final_result": marker.get("purpose") == "finalize"
                    and len(seen) == 1}
        task = await db.get_task(task_id)
        task_id = task.parent_task_id if task else None
    return None


async def result_text_error(db, experiment: dict, text: str) -> str | None:
    """Reject known internal vocabulary in human prose; keep image URLs usable."""
    prose = re.sub(r"(?<=\]\()[^)]+(?=\))", "", text)
    if re.search(r"\b(?:receipts?|incumbent|plateau|r\d+)\b|\b[0-9a-f]{64}\b", prose, re.I):
        return "Explain the result in plain English without internal terms or hashes."
    async with db._engine.connect() as conn:
        state = (await conn.execute(select(object_loops.c.state).where(
            object_loops.c.object_id == experiment["object_id"],
        ))).scalar_one_or_none()
    identifiers = [experiment["object_id"]] if re.search(r"[-_]", experiment["object_id"]) else []
    if state:
        identifiers.append(state["attempt_id"])
    if any(re.search(rf"(?<![\w-]){re.escape(value)}(?![\w-])", prose) for value in identifiers):
        return "Use the object's ordinary name without attempt or round IDs."
    return None
