"""Workflow CRUD and coordination operations.

Provides database access for :class:`~src.models.Workflow` records that
track coordination playbook executions.  Follows the same mixin pattern
as other query modules -- expects ``self._engine`` to be set by the
adapter class.

Core operations (Roadmap 7.1.3):
- **create_workflow** -- insert a new workflow
- **get_workflow** -- fetch by ID
- **update_workflow_status** -- transition workflow status
- **add_workflow_task** -- append a task ID to the workflow's task list

See docs/specs/design/agent-coordination.md §6 (Workflow Runtime).
"""

from __future__ import annotations

import json
import logging
import time

from sqlalchemy import and_, delete, insert, select, update

from src.database.tables import workflows
from src.models import Workflow

logger = logging.getLogger(__name__)

# Valid workflow statuses (matches CHECK constraint in tables.py).
_VALID_STATUSES = frozenset({"running", "paused", "completed", "failed"})

# Valid status transitions: current -> set of allowed targets.
_VALID_TRANSITIONS: dict[str, set[str]] = {
    "running": {"paused", "completed", "failed"},
    "paused": {"running", "failed"},
    "completed": set(),  # terminal
    "failed": {"running"},  # allow retry
}


class WorkflowQueryMixin:
    """Query mixin for workflow operations.  Expects ``self._engine``."""

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    async def create_workflow(self, workflow: Workflow) -> None:
        """Insert a new workflow record.

        Parameters
        ----------
        workflow:
            A :class:`Workflow` instance.  ``task_ids`` and
            ``agent_affinity`` are serialised to JSON for storage.
        """
        async with self._engine.begin() as conn:
            await conn.execute(
                insert(workflows).values(
                    workflow_id=workflow.workflow_id,
                    playbook_id=workflow.playbook_id,
                    playbook_run_id=workflow.playbook_run_id,
                    project_id=workflow.project_id,
                    status=workflow.status,
                    current_stage=workflow.current_stage,
                    task_ids=json.dumps(workflow.task_ids),
                    agent_affinity=json.dumps(workflow.agent_affinity),
                    stages=json.dumps(workflow.stages),
                    created_at=workflow.created_at or time.time(),
                    completed_at=workflow.completed_at,
                )
            )

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    async def get_workflow(self, workflow_id: str) -> Workflow | None:
        """Fetch a single workflow by ID.

        Returns ``None`` when no workflow with *workflow_id* exists.
        """
        async with self._engine.begin() as conn:
            result = await conn.execute(
                select(workflows).where(workflows.c.workflow_id == workflow_id)
            )
            row = result.mappings().fetchone()
            if not row:
                return None
            return self._row_to_workflow(row)

    async def list_workflows(
        self,
        project_id: str | None = None,
        playbook_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[Workflow]:
        """List workflows with optional filters, newest first.

        Parameters
        ----------
        project_id:
            Filter to workflows in this project.
        playbook_id:
            Filter to workflows from this playbook.
        status:
            Filter to workflows with this status.
        limit:
            Maximum number of results (default 50).
        """
        stmt = select(workflows).order_by(workflows.c.created_at.desc())
        conditions: list = []
        if project_id:
            conditions.append(workflows.c.project_id == project_id)
        if playbook_id:
            conditions.append(workflows.c.playbook_id == playbook_id)
        if status:
            conditions.append(workflows.c.status == status)
        if conditions:
            stmt = stmt.where(and_(*conditions))
        stmt = stmt.limit(limit)
        async with self._engine.begin() as conn:
            result = await conn.execute(stmt)
            return [self._row_to_workflow(r) for r in result.mappings().fetchall()]

    # ------------------------------------------------------------------
    # Update
    # ------------------------------------------------------------------

    async def update_workflow(self, workflow_id: str, **kwargs) -> None:
        """Update arbitrary workflow fields.

        JSON fields (``task_ids``, ``agent_affinity``) should be passed
        as already-serialised JSON strings.
        """
        async with self._engine.begin() as conn:
            await conn.execute(
                update(workflows)
                .where(workflows.c.workflow_id == workflow_id)
                .values(**kwargs)
            )

    async def update_workflow_status(
        self,
        workflow_id: str,
        new_status: str,
        *,
        completed_at: float | None = None,
    ) -> None:
        """Transition a workflow to a new status.

        Validates the transition against ``_VALID_TRANSITIONS`` and logs
        a warning on invalid transitions (but still applies the update,
        matching the project's convention of log-only validation).

        When *new_status* is ``"completed"`` or ``"failed"`` and no
        explicit *completed_at* is supplied, the current time is used.

        Parameters
        ----------
        workflow_id:
            The workflow to update.
        new_status:
            Target status (must be one of ``running``, ``paused``,
            ``completed``, ``failed``).
        completed_at:
            Optional explicit completion timestamp.  Auto-set for
            terminal statuses when omitted.
        """
        if new_status not in _VALID_STATUSES:
            raise ValueError(
                f"Invalid workflow status {new_status!r}; "
                f"expected one of {sorted(_VALID_STATUSES)}"
            )

        # Fetch current status for transition validation.
        current = await self.get_workflow(workflow_id)
        if current is None:
            logger.warning("update_workflow_status: workflow %r not found", workflow_id)
        elif current.status == new_status:
            return  # no-op
        elif new_status not in _VALID_TRANSITIONS.get(current.status, set()):
            logger.warning(
                "Invalid workflow status transition: %s -> %s for workflow %r",
                current.status,
                new_status,
                workflow_id,
            )

        values: dict = {"status": new_status}
        if new_status in ("completed", "failed") and completed_at is None:
            values["completed_at"] = time.time()
        elif completed_at is not None:
            values["completed_at"] = completed_at

        async with self._engine.begin() as conn:
            await conn.execute(
                update(workflows)
                .where(workflows.c.workflow_id == workflow_id)
                .values(**values)
            )

    async def add_workflow_task(self, workflow_id: str, task_id: str) -> None:
        """Append a task ID to the workflow's ``task_ids`` JSON array.

        This is an atomic read-modify-write: the current list is loaded,
        the new *task_id* is appended (if not already present), and the
        updated JSON is written back in a single transaction.

        Parameters
        ----------
        workflow_id:
            The workflow to modify.
        task_id:
            The task ID to add.
        """
        async with self._engine.begin() as conn:
            result = await conn.execute(
                select(workflows.c.task_ids).where(
                    workflows.c.workflow_id == workflow_id
                )
            )
            row = result.fetchone()
            if row is None:
                logger.warning("add_workflow_task: workflow %r not found", workflow_id)
                return

            current_ids: list[str] = json.loads(row[0])
            if task_id in current_ids:
                return  # already present

            current_ids.append(task_id)
            await conn.execute(
                update(workflows)
                .where(workflows.c.workflow_id == workflow_id)
                .values(task_ids=json.dumps(current_ids))
            )

    # ------------------------------------------------------------------
    # Delete
    # ------------------------------------------------------------------

    async def delete_workflow(self, workflow_id: str) -> None:
        """Delete a workflow record."""
        async with self._engine.begin() as conn:
            await conn.execute(
                delete(workflows).where(workflows.c.workflow_id == workflow_id)
            )

    # ------------------------------------------------------------------
    # Row conversion
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_workflow(row) -> Workflow:
        """Convert a database row mapping to a :class:`Workflow` model."""
        return Workflow(
            workflow_id=row["workflow_id"],
            playbook_id=row["playbook_id"],
            playbook_run_id=row["playbook_run_id"],
            project_id=row["project_id"],
            status=row["status"],
            current_stage=row["current_stage"],
            task_ids=json.loads(row["task_ids"]) if row["task_ids"] else [],
            agent_affinity=json.loads(row["agent_affinity"]) if row["agent_affinity"] else {},
            stages=json.loads(row["stages"]) if row.get("stages") else [],
            created_at=row["created_at"],
            completed_at=row["completed_at"],
        )
