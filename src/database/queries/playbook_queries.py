"""Playbook run CRUD and filtering operations.

Provides database access for :class:`~src.models.PlaybookRun` records that
track playbook graph executions.  Follows the same mixin pattern as other
query modules — expects ``self._engine`` to be set by the adapter class.

See docs/specs/design/playbooks.md §6 (Run Persistence) for the schema.
"""

from __future__ import annotations

import logging

from sqlalchemy import delete, func, insert, select, update

from src.database.tables import playbook_runs
from src.models import PlaybookRun

logger = logging.getLogger(__name__)


class PlaybookQueryMixin:
    """Query mixin for playbook_run operations.  Expects ``self._engine``."""

    async def create_playbook_run(self, run: PlaybookRun) -> None:
        """Insert a new playbook run record."""
        async with self._engine.begin() as conn:
            await conn.execute(
                insert(playbook_runs).values(
                    run_id=run.run_id,
                    playbook_id=run.playbook_id,
                    playbook_version=run.playbook_version,
                    trigger_event=run.trigger_event,
                    status=run.status,
                    current_node=run.current_node,
                    conversation_history=run.conversation_history,
                    node_trace=run.node_trace,
                    tokens_used=run.tokens_used,
                    started_at=run.started_at,
                    completed_at=run.completed_at,
                    error=run.error,
                    pinned_graph=run.pinned_graph,
                )
            )

    async def get_playbook_run(self, run_id: str) -> PlaybookRun | None:
        """Fetch a single playbook run by ID."""
        async with self._engine.begin() as conn:
            result = await conn.execute(
                select(playbook_runs).where(playbook_runs.c.run_id == run_id)
            )
            row = result.mappings().fetchone()
            if not row:
                return None
            return self._row_to_playbook_run(row)

    async def list_playbook_runs(
        self,
        playbook_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[PlaybookRun]:
        """List playbook runs with optional filters, newest first."""
        stmt = select(playbook_runs).order_by(playbook_runs.c.started_at.desc())
        if playbook_id:
            stmt = stmt.where(playbook_runs.c.playbook_id == playbook_id)
        if status:
            stmt = stmt.where(playbook_runs.c.status == status)
        stmt = stmt.limit(limit)
        async with self._engine.begin() as conn:
            result = await conn.execute(stmt)
            return [self._row_to_playbook_run(r) for r in result.mappings().fetchall()]

    async def update_playbook_run(self, run_id: str, **kwargs) -> None:
        """Update arbitrary playbook run fields.

        JSON fields (``conversation_history``, ``node_trace``, ``trigger_event``)
        should be passed as already-serialised JSON strings.
        """
        async with self._engine.begin() as conn:
            await conn.execute(
                update(playbook_runs).where(playbook_runs.c.run_id == run_id).values(**kwargs)
            )

    async def get_daily_playbook_token_usage(self, since: float) -> int:
        """Sum ``tokens_used`` across all playbook runs started at or after *since*.

        Used by the daily playbook token cap to determine whether the global
        ``max_daily_playbook_tokens`` budget has been exhausted.

        Parameters
        ----------
        since:
            Unix timestamp (e.g. midnight today).  Only runs with
            ``started_at >= since`` are included.

        Returns
        -------
        int
            Total tokens used by matching runs (0 when there are none).
        """
        async with self._engine.begin() as conn:
            result = await conn.execute(
                select(func.coalesce(func.sum(playbook_runs.c.tokens_used), 0)).where(
                    playbook_runs.c.started_at >= since
                )
            )
            return int(result.scalar())

    async def delete_playbook_run(self, run_id: str) -> None:
        """Delete a playbook run record."""
        async with self._engine.begin() as conn:
            await conn.execute(delete(playbook_runs).where(playbook_runs.c.run_id == run_id))

    @staticmethod
    def _row_to_playbook_run(row) -> PlaybookRun:
        """Convert a database row to a PlaybookRun model."""
        return PlaybookRun(
            run_id=row["run_id"],
            playbook_id=row["playbook_id"],
            playbook_version=row["playbook_version"],
            trigger_event=row["trigger_event"],
            status=row["status"],
            current_node=row["current_node"],
            conversation_history=row["conversation_history"],
            node_trace=row["node_trace"],
            tokens_used=row["tokens_used"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            error=row["error"],
            pinned_graph=row["pinned_graph"],
        )
