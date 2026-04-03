"""SQLite database adapter.

Composes all domain query mixins into a single class that implements the
:class:`~src.database.base.DatabaseBackend` protocol using aiosqlite.

This is the primary (and currently only production) adapter.  It preserves
100 % behavioral compatibility with the original monolithic ``Database``
class while benefiting from the modular query organization.

Usage::

    db = SQLiteDatabaseAdapter("data/queue.db")
    await db.initialize()
    ...
    await db.close()
"""

from __future__ import annotations

import logging
import time

import aiosqlite

from src.database.connection import create_sqlite_connection, run_startup_migrations
from src.database.queries.agent_queries import AgentQueryMixin
from src.database.queries.archive_queries import ArchiveQueryMixin
from src.database.queries.chat_queries import ChatQueryMixin
from src.database.queries.dependency_queries import DependencyQueryMixin
from src.database.queries.event_queries import EventQueryMixin
from src.database.queries.hook_queries import HookQueryMixin
from src.database.queries.profile_queries import ProfileQueryMixin
from src.database.queries.project_queries import ProjectQueryMixin
from src.database.queries.repo_queries import RepoQueryMixin
from src.database.queries.result_queries import ResultQueryMixin
from src.database.queries.task_queries import TaskQueryMixin
from src.database.queries.token_queries import TokenQueryMixin
from src.database.queries.plugin_queries import PluginQueryMixin
from src.database.queries.workspace_queries import WorkspaceQueryMixin
from src.models import AgentState, TaskStatus
from src.state_machine import is_valid_status_transition

logger = logging.getLogger(__name__)


class SQLiteDatabaseAdapter(
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
    HookQueryMixin,
    ArchiveQueryMixin,
    ChatQueryMixin,
    PluginQueryMixin,
):
    """Async SQLite persistence layer implementing the repository pattern.

    All database access in the system goes through this class.  It owns the
    connection lifecycle, schema creation, migrations, and provides typed
    CRUD methods that accept and return domain dataclasses from
    :mod:`src.models`.

    The connection uses WAL journal mode and has foreign keys enabled, so
    referential integrity is enforced at the database level.  Row factory is
    set to ``aiosqlite.Row`` for dict-like column access.

    State transitions go through :meth:`transition_task`, which validates
    against the state machine but always applies the update (logging-only
    enforcement) to avoid blocking production on unexpected edge cases.
    """

    def __init__(self, path: str):
        self._path = path
        self._db: aiosqlite.Connection | None = None

    async def initialize(self) -> None:
        """Create tables, run migrations, and prepare the connection."""
        self._db = await create_sqlite_connection(self._path)
        await run_startup_migrations(self._db)

    async def close(self) -> None:
        """Gracefully shut down the database connection."""
        if self._db:
            await self._db.close()

    # --- Atomic Operations ---
    # Multi-table writes that must succeed or fail together.

    async def assign_task_to_agent(self, task_id: str, agent_id: str) -> None:
        """Atomically bind a task to an agent, updating both sides.

        In a single commit:
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
        await self._db.execute(
            "UPDATE tasks SET status = ?, assigned_agent_id = ?, updated_at = ? WHERE id = ?",
            (TaskStatus.ASSIGNED.value, agent_id, now, task_id),
        )
        await self._db.execute(
            "UPDATE agents SET state = ?, current_task_id = ? WHERE id = ?",
            (AgentState.BUSY.value, task_id, agent_id),
        )
        await self._db.execute(
            "INSERT INTO events (event_type, project_id, task_id, agent_id, "
            "timestamp) VALUES (?, (SELECT project_id FROM tasks WHERE id = ?), "
            "?, ?, ?)",
            ("task_assigned", task_id, task_id, agent_id, now),
        )
        await self._db.commit()
