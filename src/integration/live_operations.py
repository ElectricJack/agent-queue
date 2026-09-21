"""Read projections for repair operations that can still own integration work."""

from __future__ import annotations

from typing import Any

from sqlalchemy import or_, select

from src.database.tables import integration_batches, integration_repair_operations, tasks


ACTIVE_OPERATION_STATES = ("active", "escalated", "human_required")


async def live_operations_on(conn: Any, project_id: str) -> list[dict[str, Any]]:
    """Return active repair operations whose parent or batch belongs to a project.

    Parent operations identify their project through ``tasks``; root/batch
    operations identify it through ``integration_batches``.  Keeping that
    mapping in one query prevents status, transition guards, and doctor from
    disagreeing about which historical operation still belongs to a project.
    """
    batches = integration_batches.alias("live_operation_batches")
    parents = tasks.alias("live_operation_parents")
    rows = (
        await conn.execute(
            select(
                integration_repair_operations.c.id,
                integration_repair_operations.c.target_kind,
                integration_repair_operations.c.batch_id,
                integration_repair_operations.c.parent_task_id,
                integration_repair_operations.c.active_stage,
                integration_repair_operations.c.state,
                integration_repair_operations.c.created_at,
                integration_repair_operations.c.updated_at,
            )
            .select_from(
                integration_repair_operations.outerjoin(
                    batches, batches.c.id == integration_repair_operations.c.batch_id
                ).outerjoin(parents, parents.c.id == integration_repair_operations.c.parent_task_id)
            )
            .where(
                integration_repair_operations.c.state.in_(ACTIVE_OPERATION_STATES),
                or_(batches.c.project_id == project_id, parents.c.project_id == project_id),
            )
            .order_by(
                integration_repair_operations.c.created_at,
                integration_repair_operations.c.id,
            )
        )
    ).mappings()
    return [
        {
            "id": row["id"],
            "target": {
                "kind": row["target_kind"],
                "id": row["parent_task_id"] or row["batch_id"],
            },
            "state": row["state"],
            "active_stage": row["active_stage"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        for row in rows
    ]


def cancel_preserving_command(operation_id: str) -> str:
    """Return the safe operator command for ending a legacy operation."""
    return (
        f"aq integration cancel-preserving {operation_id} "
        "--reason 'ending legacy hierarchy operation before development switch'"
    )


def describe_live_operation(operation: dict[str, Any]) -> str:
    """Render an operation with its target and safe terminal command."""
    target = operation["target"]
    return (
        f"{operation['id']} ({target['kind']} {target['id']}; "
        f"{cancel_preserving_command(operation['id'])})"
    )
