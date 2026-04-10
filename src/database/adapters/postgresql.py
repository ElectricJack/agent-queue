"""PostgreSQL database adapter using SQLAlchemy Core.

Composes all domain query mixins into a single class that implements the
:class:`~src.database.base.DatabaseBackend` protocol using SQLAlchemy's
async engine with the asyncpg driver.

Usage::

    db = PostgreSQLDatabaseAdapter("postgresql://user:pass@localhost/agent_queue")
    await db.initialize()
    ...
    await db.close()
"""

from __future__ import annotations

import logging
import time

from sqlalchemy import insert, select, update

from src.database.engine import (
    create_postgres_engine,
    run_schema_setup,
    run_startup_data_migrations,
)
from src.database.queries.agent_queries import AgentQueryMixin
from src.database.queries.archive_queries import ArchiveQueryMixin
from src.database.queries.chat_queries import ChatQueryMixin
from src.database.queries.dependency_queries import DependencyQueryMixin
from src.database.queries.event_queries import EventQueryMixin
from src.database.queries.profile_queries import ProfileQueryMixin
from src.database.queries.project_queries import ProjectQueryMixin
from src.database.queries.repo_queries import RepoQueryMixin
from src.database.queries.result_queries import ResultQueryMixin
from src.database.queries.task_queries import TaskQueryMixin
from src.database.queries.token_queries import TokenQueryMixin
from src.database.queries.plugin_queries import PluginQueryMixin
from src.database.queries.workspace_queries import WorkspaceQueryMixin
from src.database.tables import agents as agents_t, events as events_t, tasks as tasks_t
from src.models import AgentState, TaskStatus
from src.state_machine import is_valid_status_transition

logger = logging.getLogger(__name__)


class PostgreSQLDatabaseAdapter(
    ProjectQueryMixin,
    ProfileQueryMixin,
    RepoQueryMixin,
    TaskQueryMixin,
    DependencyQueryMixin,
    AgentQueryMixin,
    WorkspaceQueryMixin,
    TokenQueryMixin,
    ResultQueryMixin,
    EventQueryMixin,
    ArchiveQueryMixin,
    ChatQueryMixin,
    PluginQueryMixin,
):
    """Async PostgreSQL persistence layer using SQLAlchemy Core.

    All database access in the system goes through this class.  It owns the
    engine lifecycle, schema creation, migrations, and provides typed
    CRUD methods that accept and return domain dataclasses from
    :mod:`src.models`.

    Connection pooling is managed by SQLAlchemy's default QueuePool.
    """

    def __init__(self, dsn: str, pool_min: int = 2, pool_max: int = 10):
        try:
            import asyncpg  # noqa: F401
        except ImportError:
            raise ImportError(
                "asyncpg is required for PostgreSQL support. "
                "Install it with: pip install agent-queue[postgresql]"
            ) from None
        self._dsn = dsn
        self._pool_min = pool_min
        self._pool_max = pool_max
        self._engine = None

    async def initialize(self) -> None:
        """Create the engine, run migrations, and prepare the database."""
        self._engine = create_postgres_engine(self._dsn, self._pool_min, self._pool_max)
        await run_schema_setup(self._engine)
        await run_startup_data_migrations(self._engine)

    async def close(self) -> None:
        """Gracefully shut down the database engine."""
        if self._engine:
            await self._engine.dispose()

    # --- Atomic Operations ---
    # Multi-table writes that must succeed or fail together.

    async def assign_task_to_agent(self, task_id: str, agent_id: str) -> None:
        """Atomically bind a task to an agent, updating both sides.

        In a single transaction:
        1. Transitions the task from READY to ASSIGNED
        2. Transitions the agent from IDLE to BUSY
        3. Logs a ``task_assigned`` event
        """
        task = await self.get_task(task_id)
        if task and not is_valid_status_transition(task.status, TaskStatus.ASSIGNED):
            logger.warning(
                "Invalid task status transition: %s -> ASSIGNED for task '%s' "
                "(assign_task_to_agent)",
                task.status.value,
                task_id,
            )

        now = time.time()
        async with self._engine.begin() as conn:
            await conn.execute(
                update(tasks_t)
                .where(tasks_t.c.id == task_id)
                .values(
                    status=TaskStatus.ASSIGNED.value,
                    assigned_agent_id=agent_id,
                    updated_at=now,
                )
            )
            await conn.execute(
                update(agents_t)
                .where(agents_t.c.id == agent_id)
                .values(state=AgentState.BUSY.value, current_task_id=task_id)
            )
            await conn.execute(
                insert(events_t).values(
                    event_type="task_assigned",
                    project_id=select(tasks_t.c.project_id)
                    .where(tasks_t.c.id == task_id)
                    .scalar_subquery(),
                    task_id=task_id,
                    agent_id=agent_id,
                    timestamp=now,
                )
            )
