"""Durable source evidence shared by periodic resolution and the saved-plan sweep."""

from __future__ import annotations

from typing import Any

OPERATIONAL_SOURCES = frozenset({
    "wait", "continuation", "integration_train_stall", "integration_delivery_stuck",
})


async def operational_source(db, incident) -> dict[str, Any] | None:
    """Return a proven cleared cause, never treating an unreadable source as absent."""
    # Rendering and CLI discovery import this module without the database runtime.
    from sqlalchemy import select

    from src.database.tables import (
        admission_reservations,
        continuations,
        integration_batches,
        wait_escalations,
        wait_records,
    )

    kind = incident.get("source_kind")
    identity = str(incident.get("source_identity") or "")
    project_id = incident["project_id"]
    async with db._engine.connect() as conn:
        if kind == "integration_train_stall" and identity:
            batch = (await conn.execute(select(integration_batches).where(
                integration_batches.c.id == identity,
                integration_batches.c.project_id == project_id,
            ))).mappings().first()
            if batch is None:
                return {"cleared": "batch_removed", "batch_id": identity}
            if (batch["intent"] == "aborted"
                    or batch["lifecycle"] in {"promoted", "aborted", "failed", "empty"}
                    or (batch["lifecycle"] == "cleanup_pending" and batch["final_main_sha"])):
                return {"cleared": "batch_terminal", "batch_id": identity,
                        "batch_lifecycle": batch["lifecycle"], "batch_intent": batch["intent"],
                        "final_main_sha": batch["final_main_sha"]}
        elif kind == "continuation" and identity.startswith("continuation:"):
            parts = identity.split(":", 3)
            if len(parts) != 4:
                return None
            continuation = (await conn.execute(select(continuations).join(
                wait_records, wait_records.c.id == continuations.c.wait_id,
            ).where(
                continuations.c.id == parts[1],
                continuations.c.task_id == incident.get("task_id"),
                wait_records.c.project_id == project_id,
            ))).mappings().first()
            if continuation and (
                str(continuation["suspension_generation"]) != parts[2]
                or continuation["resume_state"] not in {"held", "failed"}
                or (continuation["hold_reason"] and continuation["hold_reason"] != parts[3])
            ):
                return {"cleared": "continuation_released", "continuation_id": parts[1],
                        "resume_state": continuation["resume_state"],
                        "suspension_generation": continuation["suspension_generation"]}
        elif kind == "wait":
            waits = (await conn.execute(select(wait_records).join(
                wait_escalations, wait_escalations.c.wait_id == wait_records.c.id,
            ).where(
                wait_escalations.c.escalation_id == incident["id"],
                wait_records.c.project_id == project_id,
            ))).mappings().all()
            if waits and all(wait["outcome"] != "pending" for wait in waits):
                return {"cleared": "wait_settled", "wait_ids": [w["id"] for w in waits],
                        "wait_outcomes": [w["outcome"] for w in waits]}
            if identity.startswith("branch-admission:"):
                reservation = (await conn.execute(select(admission_reservations).where(
                    admission_reservations.c.id == identity.removeprefix("branch-admission:"),
                    admission_reservations.c.project_id == project_id,
                    admission_reservations.c.task_id == incident.get("task_id"),
                ))).mappings().first()
                if reservation and reservation["state"] in {"consumed", "cancelled"}:
                    return {"cleared": "admission_released", "reservation_id": reservation["id"],
                            "reservation_state": reservation["state"]}
        elif kind == "integration_delivery_stuck":
            projection = (await conn.execute(select(wait_records).where(
                wait_records.c.project_id == project_id,
                wait_records.c.decision_inputs["incident_id"].astext == incident["id"],
                wait_records.c.origin == "integration",
                wait_records.c.decision_inputs["delivery_watchdog_projection"].as_boolean().is_(True),
                wait_records.c.outcome == "satisfied",
            ))).mappings().first()
            if projection:
                inputs = projection["decision_inputs"] or {}
                return {"cleared": "delivery_contained", "wait_id": projection["id"],
                        "task_id": projection["owner_id"], "head": inputs.get("head"),
                        "repository_id": inputs.get("repository_id"),
                        "target_ref": inputs.get("target_ref"),
                        "source_generation": projection["source_generation"]}
    return None
