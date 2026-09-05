"""Read projections over durable hierarchical-integration state."""

from __future__ import annotations

from sqlalchemy import and_, select

from src.database.tables import (
    integration_batches,
    integration_branch_owners,
    integration_repair_operations,
    integration_repair_stages,
    task_integration_checkpoints,
    tasks,
    workspaces,
)


class IntegrationStateQueriesMixin:
    """Integration-state reads; state mutations stay caller-transaction owned."""

    async def get_integration_checkpoint(self, task_id: str) -> dict | None:
        statement = select(task_integration_checkpoints).where(
            task_integration_checkpoints.c.task_id == task_id
        )
        async with self._engine.connect() as conn:
            row = (await conn.execute(statement)).mappings().one_or_none()
        return dict(row) if row is not None else None

    async def get_integration_batch(self, batch_id: str) -> dict | None:
        statement = select(integration_batches).where(integration_batches.c.id == batch_id)
        async with self._engine.connect() as conn:
            row = (await conn.execute(statement)).mappings().one_or_none()
        return dict(row) if row is not None else None

    async def get_integration_operation(self, operation_id: str) -> dict | None:
        statement = select(integration_repair_operations).where(
            integration_repair_operations.c.id == operation_id
        )
        async with self._engine.connect() as conn:
            row = (await conn.execute(statement)).mappings().one_or_none()
        return dict(row) if row is not None else None

    async def get_active_integration_repair_for_task(
        self, repair_task_id: str
    ) -> dict | None:
        """Resolve one repair task's current active, nonterminal operation."""
        statement = (
            select(
                integration_repair_operations,
                integration_repair_stages.c.starting_sha.label("stage_starting_sha"),
                integration_repair_stages.c.current_subject.label("stage_subject"),
                integration_repair_stages.c.writer_kind.label("writer_kind"),
                integration_repair_stages.c.retained_workspace_id.label(
                    "retained_workspace_id"
                ),
                integration_repair_stages.c.retained_handoff.label(
                    "retained_handoff"
                ),
            )
            .select_from(
                integration_repair_operations.join(
                    integration_repair_stages,
                    and_(
                        integration_repair_stages.c.operation_id
                        == integration_repair_operations.c.id,
                        integration_repair_stages.c.ordinal
                        == integration_repair_operations.c.active_stage,
                    ),
                )
            )
            .where(integration_repair_stages.c.repair_task_id == repair_task_id)
            .where(integration_repair_stages.c.writer_kind == "repair_delegate")
            .where(
                integration_repair_stages.c.state.in_(("active", "awaiting_completion"))
            )
            .where(
                integration_repair_operations.c.state.in_(
                    ("active", "escalated")
                )
            )
        )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(statement)).mappings().all()
        return dict(rows[0]) if len(rows) == 1 else None

    async def get_repair_filing_scope(
        self, repair_task_id: str, *, conn=None
    ) -> dict | None:
        """Resolve a delegate's server-owned logical filing scope, including expiry."""

        async def read(connection):
            statement = (
                select(
                    integration_repair_operations,
                    integration_repair_stages.c.ordinal.label("stage_ordinal"),
                    integration_repair_stages.c.state.label("stage_state"),
                    integration_repair_stages.c.writer_kind.label("writer_kind"),
                )
                .select_from(
                    integration_repair_operations.join(
                        integration_repair_stages,
                        integration_repair_stages.c.operation_id
                        == integration_repair_operations.c.id,
                    )
                )
                .where(
                    integration_repair_stages.c.repair_task_id == repair_task_id,
                    integration_repair_stages.c.writer_kind == "repair_delegate",
                )
                .with_for_update()
            )
            rows = (await connection.execute(statement)).mappings().all()
            if len(rows) != 1:
                return None
            row = dict(rows[0])
            delegate = (
                await connection.execute(
                    select(tasks).where(tasks.c.id == repair_task_id)
                )
            ).mappings().one_or_none()
            if delegate is None:
                return None
            if row["target_kind"] == "parent":
                target = (
                    await connection.execute(
                        select(tasks).where(tasks.c.id == row["parent_task_id"])
                    )
                ).mappings().one_or_none()
                if target is None:
                    return None
                target_project_id = target["project_id"]
                repository_id = target["repo_id"]
                branch = target["branch_name"]
                parent_task_id = target["id"]
            elif row["target_kind"] == "batch":
                target = (
                    await connection.execute(
                        select(integration_batches).where(
                            integration_batches.c.id == row["batch_id"]
                        )
                    )
                ).mappings().one_or_none()
                if target is None:
                    return None
                target_project_id = target["project_id"]
                repository_id = target["repository_id"]
                branch = target["integration_branch"]
                parent_task_id = None
            else:
                return None
            if (
                delegate["project_id"] != target_project_id
                or delegate["repo_id"] != repository_id
                or delegate["branch_name"] != branch
            ):
                return None
            owner = (
                await connection.execute(
                    select(integration_branch_owners)
                    .where(
                        integration_branch_owners.c.repository_id == repository_id,
                        integration_branch_owners.c.ref == branch,
                    )
                    .with_for_update()
                )
            ).mappings().one_or_none()
            workspace = (
                await connection.execute(
                    select(workspaces)
                    .where(workspaces.c.locked_by_task_id == repair_task_id)
                    .with_for_update()
                )
            ).mappings().first()
            active = bool(
                int(row["active_stage"]) == int(row["stage_ordinal"])
                and row["state"] in {"active", "escalated"}
                and row["stage_state"] in {"active", "awaiting_completion"}
                and owner is not None
                and owner["owner_id"] == repair_task_id
                and owner["owner_role"] == "repair"
                and owner["handoff_state"] in {"reserved", "attached"}
                and (
                    row["target_kind"] == "batch"
                    or (
                        workspace is not None
                        and workspace["project_id"] == target_project_id
                        and workspace["enabled"]
                    )
                )
            )
            return {
                "operation_id": row["id"],
                "target_kind": row["target_kind"],
                "project_id": target_project_id,
                "repository_id": repository_id,
                "parent_task_id": parent_task_id,
                "active": active,
            }

        if conn is not None:
            return await read(conn)
        async with self._engine.connect() as owned_conn:
            return await read(owned_conn)
