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


from src.database.engine import (
    create_postgres_engine,
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
from src.database.queries.integration_state_queries import IntegrationStateQueriesMixin
from src.database.queries.integration_schedule_queries import IntegrationScheduleQueriesMixin
from src.database.queries.integration_delivery_queries import IntegrationDeliveryQueriesMixin
from src.database.queries.layout_queries import LayoutQueryMixin
from src.database.queries.merge_slot_queries import MergeSlotQueriesMixin
from src.database.queries.message_queries import MessageQueriesMixin
from src.database.queries.metrics_queries import MetricsQueryMixin
from src.database.queries.profile_queries import ProfileQueryMixin
from src.database.queries.project_queries import ProjectQueryMixin
from src.database.queries.repo_queries import RepoQueryMixin
from src.database.queries.result_queries import ResultQueryMixin
from src.database.queries.session_queries import SessionQueryMixin
from src.database.queries.subagent_queries import SubagentQueriesMixin
from src.database.queries.task_session_queries import TaskSessionQueryMixin
from src.database.queries.task_comment_queries import TaskCommentQueriesMixin
from src.database.queries.task_queries import TaskQueryMixin
from src.database.queries.task_recovery_queries import TaskRecoveryQueryMixin
from src.database.queries.task_requirements_queries import TaskRequirementsQueryMixin
from src.database.queries.token_queries import TokenQueryMixin
from src.database.queries.transaction_queries import TransactionQueryMixin
from src.database.queries.transcript_queries import TranscriptQueryMixin
from src.database.queries.playbook_artifact_queries import PlaybookArtifactQueryMixin
from src.database.queries.playbook_run_queries import PlaybookRunQueryMixin
from src.database.queries.plugin_queries import PluginQueryMixin
from src.database.queries.workflow_queries import WorkflowQueryMixin
from src.database.queries.workspace_kinds_queries import WorkspaceKindQueryMixin
from src.database.queries.workspace_queries import WorkspaceQueryMixin

logger = logging.getLogger(__name__)


class PostgreSQLDatabaseAdapter(
    IntegrationDeliveryQueriesMixin,
    IntegrationScheduleQueriesMixin,
    IntegrationStateQueriesMixin,
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
    PluginQueryMixin,
    PlaybookArtifactQueryMixin,
    PlaybookRunQueryMixin,
    WorkflowQueryMixin,
    ApiSessionTokenQueriesMixin,
    TransactionQueryMixin,
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

    async def reset_for_tests(self) -> None:
        """Wipe every row for a clean slate between test modules.

        ``initialize()`` already ran the migrations (idempotently, via
        ``run_schema_setup``); dropping the schema and re-running Alembic's
        full chain per parametrized perf test would dwarf the seeding cost
        it's supposed to measure, so this truncates every table instead
        (same statement ``tests/test_database_postgresql.py``'s per-test
        teardown uses) and leaves the schema — and ``alembic_version`` —
        untouched.

        Refuses unless this adapter is plausibly a test target: its DSN must
        equal ``POSTGRES_TEST_DSN``, or ``AQ_ALLOW_DB_RESET=1`` is set. A
        truncate-everything call reachable against a production DSN by
        accident is a data-loss bug waiting to happen.
        """
        import os

        from sqlalchemy import text

        if (
            self._dsn != os.environ.get("POSTGRES_TEST_DSN")
            and os.environ.get("AQ_ALLOW_DB_RESET") != "1"
        ):
            raise RuntimeError("reset_for_tests refused: not the configured POSTGRES_TEST_DSN")
        if self._engine is None:
            return
        async with self._engine.begin() as conn:
            await conn.execute(
                text(
                    "DO $$ DECLARE r RECORD; BEGIN "
                    "FOR r IN (SELECT tablename FROM pg_tables "
                    "WHERE schemaname = 'public' AND tablename != 'alembic_version') LOOP "
                    "EXECUTE 'TRUNCATE TABLE ' || quote_ident(r.tablename) || ' CASCADE'; "
                    "END LOOP; END $$;"
                )
            )

    # --- Atomic Operations ---
    # Multi-table writes that must succeed or fail together.

    async def assign_task_to_agent(self, task_id: str, agent_id: str) -> bool:
        return await self._assign_task_to_agent(task_id, agent_id)
