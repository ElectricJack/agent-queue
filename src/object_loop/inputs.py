"""Read bounded, durable inputs for the opt-in object-loop policy."""

from __future__ import annotations

import json
import time

from sqlalchemy import select

from src.commands.contracts.object_loop import ObjectScoreRecordArgs
from src.database.tables import (
    doc_reviews,
    object_loops,
    task_context,
    task_metadata,
    tasks,
)
from src.object_loop.formulas import proposal_valid, start_from_vars

SCORE_PREFIX = "object-score:1\n"


async def read_inputs(db, project_id: str, *, limit: int) -> dict:
    """No inferred approval or score. A malformed packet fails closed."""
    from src.commands.object_loop_commands import _task_settled

    starts, loops = [], []
    async with db._engine.connect() as conn:
        rows = (await conn.execute(select(object_loops).join(
            tasks, tasks.c.id == object_loops.c.epic_task_id,
        ).where(
            object_loops.c.project_id == project_id,
            tasks.c.status != "COMPLETED",
        ).order_by(object_loops.c.object_id).limit(limit + 1))).mappings().all()
        if len(rows) > limit:
            raise ValueError("object-loop input bound exceeded; reduce active objects")
        for row in rows:
            state = row.state
            score = None
            scorer = None
            if state["score_task_id"]:
                scorer = (await conn.execute(select(tasks).where(
                    tasks.c.id == state["score_task_id"],
                    tasks.c.project_id == project_id,
                    tasks.c.parent_task_id == row.epic_task_id,
                    tasks.c.created_by_kind == "object_loop",
                    tasks.c.created_by_id == row.object_id,
                ))).mappings().first()
                if scorer and scorer.status == "COMPLETED":
                    note = (await conn.execute(select(task_context.c.content).where(
                        task_context.c.task_id == scorer.id,
                        task_context.c.type == "note",
                        task_context.c.content.startswith(SCORE_PREFIX, autoescape=True),
                    ).order_by(task_context.c.created_at.desc(), task_context.c.id.desc())
                        .limit(1))).scalar_one_or_none()
                    if note:
                        score = ObjectScoreRecordArgs.model_validate_json(
                            note[len(SCORE_PREFIX):]
                        ).model_dump()
                        if (score["object_id"] != row.object_id
                                or score["project_id"] != project_id
                                or score["score_task_id"] != scorer.id):
                            raise ValueError("score packet belongs to foreign work")
            checkpoint = state.get("checkpoint")
            review_state = None
            if checkpoint:
                review_state = (await conn.execute(select(doc_reviews.c.state).where(
                    doc_reviews.c.id == checkpoint["review_id"],
                    doc_reviews.c.project_id == project_id,
                ))).scalar_one_or_none()
            # A recorded checkpoint retains its packet's finite continuation;
            # a consumed score can never be submitted again at a newer version.
            loops.append({
                "object_id": row.object_id, "project_id": project_id,
                "version": row.version, "state": state,
                "policy_artifact": "sha256:" + state["policy_sha256"],
                "elapsed_seconds": max(0, time.time() - row.created_at),
                "score": score if score and not checkpoint else None,
                "next_variants": score["next_variants"] if score and checkpoint else [],
                "review_state": review_state,
                "scorer_failed": bool(scorer and scorer.status != "COMPLETED"
                                      and _task_settled(scorer)),
            })
        pending = (await conn.execute(select(tasks.c.id, task_metadata.c.value).join(
            task_metadata, task_metadata.c.task_id == tasks.c.id,
        ).where(
            tasks.c.project_id == project_id,
            tasks.c.status.not_in(["COMPLETED", "BLOCKED", "FAILED"]),
            task_metadata.c.key == "formula_vars",
            tasks.c.id.in_(select(task_metadata.c.task_id).where(
                task_metadata.c.key == "formula", task_metadata.c.value == json.dumps("object"),
            )),
            ~select(object_loops.c.object_id).where(
                object_loops.c.epic_task_id == tasks.c.id,
            ).exists(),
        ).order_by(tasks.c.id).limit(limit + 1))).mappings().all()
        if len(pending) + len(rows) > limit:
            raise ValueError("object-loop input bound exceeded; reduce active objects")
        for row in pending:
            # Metadata JSON-encodes the formula_vars string, which is itself
            # the formula provenance's serialized variable mapping.
            variables = json.loads(json.loads(row.value))
            request = start_from_vars(variables, project_id=project_id, epic_task_id=row.id)
            starts.append({
                "request": request,
                "policy_artifact": "sha256:" + request["policy_sha256"],
                "proposal_approved": await proposal_valid(
                    conn, project_id, variables["proposal_sha256"],
                ),
            })
    return {"starts": starts, "loops": loops}
