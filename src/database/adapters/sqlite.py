"""SQLite database adapter using SQLAlchemy Core.

Composes all domain query mixins into a single class that implements the
:class:`~src.database.base.DatabaseBackend` protocol using SQLAlchemy's
async engine with the aiosqlite driver.

Usage::

    db = SQLiteDatabaseAdapter("data/queue.db")
    await db.initialize()
    ...
    await db.close()
"""

from __future__ import annotations

import logging

from src.database.engine import (
    create_sqlite_engine,
    run_schema_setup,
    run_startup_data_migrations,
)
from src.database.queries.agent_queries import AgentQueryMixin
from src.database.queries.agent_question_queries import AgentQuestionQueriesMixin
from src.database.queries.api_session_token_queries import ApiSessionTokenQueriesMixin
from src.database.queries.archive_queries import ArchiveQueryMixin
from src.database.queries.assignment_route_queries import AssignmentRouteQueryMixin
from src.database.queries.blocked_state import BlockedStateMixin
from src.database.queries.chat_queries import ChatQueryMixin
from src.database.queries.claim_queries import ClaimQueryMixin
from src.database.queries.dependency_queries import DependencyQueryMixin
from src.database.queries.event_queries import EventQueryMixin
from src.database.queries.gate_queries import GateQueriesMixin
from src.database.queries.hierarchy_queries import HierarchyQueryMixin
from src.database.queries.layout_queries import LayoutQueryMixin
from src.database.queries.merge_slot_queries import MergeSlotQueriesMixin
from src.database.queries.message_queries import MessageQueriesMixin
from src.database.queries.metrics_queries import MetricsQueryMixin
from src.database.queries.onboarding_queries import OnboardingQueryMixin
from src.database.queries.playbook_artifact_queries import PlaybookArtifactQueryMixin
from src.database.queries.playbook_run_queries import PlaybookRunQueryMixin
from src.database.queries.plugin_queries import PluginQueryMixin
from src.database.queries.profile_queries import ProfileQueryMixin
from src.database.queries.project_queries import ProjectQueryMixin
from src.database.queries.repo_queries import RepoQueryMixin
from src.database.queries.result_queries import ResultQueryMixin
from src.database.queries.session_queries import SessionQueryMixin
from src.database.queries.subagent_queries import SubagentQueriesMixin
from src.database.queries.task_comment_queries import TaskCommentQueriesMixin
from src.database.queries.task_queries import TaskQueryMixin
from src.database.queries.task_recovery_queries import TaskRecoveryQueryMixin
from src.database.queries.task_requirements_queries import TaskRequirementsQueryMixin
from src.database.queries.task_session_queries import TaskSessionQueryMixin
from src.database.queries.token_queries import TokenQueryMixin
from src.database.queries.transaction_queries import TransactionQueryMixin
from src.database.queries.transcript_queries import TranscriptQueryMixin
from src.database.queries.workflow_queries import WorkflowQueryMixin
from src.database.queries.workspace_kinds_queries import WorkspaceKindQueryMixin
from src.database.queries.workspace_queries import WorkspaceQueryMixin

logger = logging.getLogger(__name__)


class SQLiteDatabaseAdapter(
    HierarchyQueryMixin,
    LayoutQueryMixin,
    AssignmentRouteQueryMixin,
    ClaimQueryMixin,
    ProjectQueryMixin,
    ProfileQueryMixin,
    RepoQueryMixin,
    TaskQueryMixin,
    TaskRecoveryQueryMixin,
    TaskCommentQueriesMixin,
    DependencyQueryMixin,
    BlockedStateMixin,
    GateQueriesMixin,
    AgentQueryMixin,
    AgentQuestionQueriesMixin,
    WorkspaceQueryMixin,
    WorkspaceKindQueryMixin,
    TaskRequirementsQueryMixin,
    SessionQueryMixin,
    SubagentQueriesMixin,
    TaskSessionQueryMixin,
    TokenQueryMixin,
    TranscriptQueryMixin,
    ResultQueryMixin,
    EventQueryMixin,
    ArchiveQueryMixin,
    ChatQueryMixin,
    MergeSlotQueriesMixin,
    MessageQueriesMixin,
    MetricsQueryMixin,
    OnboardingQueryMixin,
    PluginQueryMixin,
    PlaybookArtifactQueryMixin,
    PlaybookRunQueryMixin,
    WorkflowQueryMixin,
    ApiSessionTokenQueriesMixin,
    TransactionQueryMixin,
):
    """Async SQLite persistence layer using SQLAlchemy Core.

    All database access in the system goes through this class.  It owns the
    engine lifecycle, schema creation, migrations, and provides typed
    CRUD methods that accept and return domain dataclasses from
    :mod:`src.models`.

    The connection uses WAL journal mode and has foreign keys enabled, so
    referential integrity is enforced at the database level.
    """

    def __init__(self, path: str):
        self._path = path
        self._engine = None

    async def initialize(self) -> None:
        """Create tables, run migrations, and prepare the engine."""
        self._engine = create_sqlite_engine(self._path)
        await run_schema_setup(self._engine)
        await run_startup_data_migrations(self._engine)

    async def close(self) -> None:
        """Gracefully shut down the database engine."""
        if self._engine:
            await self._engine.dispose()

    async def reset_for_tests(self) -> None:
        """Wipe every row for a clean slate between test modules.

        Kept for parity with :meth:`PostgreSQLDatabaseAdapter.reset_for_tests`
        (``tests/test_database_modular.py::TestAdapterParity`` requires both
        adapters to expose the same public surface).  A fresh SQLite test
        normally just points at a new ``tmp_path`` file instead, so this is
        mostly for a caller sharing one adapter instance across cases (e.g.
        ``tests/perf/conftest.py``'s ``any_db``, if ever adapted to reuse a
        single SQLite file rather than a fresh one per parametrization).

        Refuses unless this adapter's file is plausibly a test target: the
        path must be under ``tempfile.gettempdir()``, or ``AQ_ALLOW_DB_RESET=1``
        is set. A truncate-everything call reachable against a real database
        file by accident is a data-loss bug waiting to happen.

        The ``PRAGMA foreign_keys`` toggles run on their own connection,
        outside ``engine.begin()``'s transaction — SQLite's docs are explicit
        that this pragma is a no-op when set inside a transaction, so doing
        it there (the first cut of this method did) silently left FK
        enforcement untouched around the deletes.
        """
        import os
        import tempfile

        if not str(self._path).startswith(tempfile.gettempdir()) and os.environ.get(
            "AQ_ALLOW_DB_RESET"
        ) != "1":
            raise RuntimeError(
                f"reset_for_tests refused: {self._path!r} is not under "
                f"{tempfile.gettempdir()!r} (set AQ_ALLOW_DB_RESET=1 to override)"
            )
        if self._engine is None:
            return
        from sqlalchemy import text

        # AUTOCOMMIT: SQLite's docs are explicit that ``PRAGMA foreign_keys``
        # is a no-op when set inside a transaction -- SQLAlchemy's async
        # connections auto-begin one on first execute otherwise, so the
        # pragma would silently do nothing and leave FK enforcement (and the
        # deletes below, which then fail on FK violations, or "succeed"
        # without ever having disabled the check) in an unpredictable state.
        autocommit = self._engine.execution_options(isolation_level="AUTOCOMMIT")
        async with autocommit.connect() as conn:
            await conn.execute(text("PRAGMA foreign_keys=OFF"))
            rows = await conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table' AND name != 'alembic_version'")
            )
            tables = [r[0] for r in rows.fetchall() if not r[0].startswith("sqlite_")]
            for table in tables:
                await conn.execute(text(f'DELETE FROM "{table}"'))
            await conn.execute(text("PRAGMA foreign_keys=ON"))

    # --- Atomic Operations ---
    # Multi-table writes that must succeed or fail together.

    async def assign_task_to_agent(self, task_id: str, agent_id: str) -> bool:
        return await self._assign_task_to_agent(task_id, agent_id)
