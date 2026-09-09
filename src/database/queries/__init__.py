"""Query mixins for each database domain.

Each mixin class provides the CRUD and query methods for one domain
(projects, tasks, agents, etc.).  The methods are backend-neutral
SQLAlchemy Core: self-contained ones expect ``self._engine`` to be an
initialized :class:`~sqlalchemy.ext.asyncio.AsyncEngine` and open their
own ``engine.begin()`` block, while caller-owned ones take an
:class:`~sqlalchemy.ext.asyncio.AsyncConnection` so the caller controls
the transaction boundary.
:class:`~src.database.adapters.postgresql.PostgreSQLDatabaseAdapter` is
the only adapter; it composes the mixins via multiple inheritance.
"""

from src.database.queries.agent_queries import AgentQueryMixin
from src.database.queries.archive_queries import ArchiveQueryMixin
from src.database.queries.assignment_route_queries import AssignmentRouteQueryMixin
from src.database.queries.blocked_state import BlockedStateMixin
from src.database.queries.chat_queries import ChatQueryMixin
from src.database.queries.claim_queries import ClaimQueryMixin
from src.database.queries.dependency_queries import DependencyQueryMixin
from src.database.queries.digest_queries import DigestQueryMixin
from src.database.queries.event_queries import EventQueryMixin
from src.database.queries.gate_queries import GateQueriesMixin
from src.database.queries.hierarchy_queries import HierarchyQueryMixin
from src.database.queries.message_queries import MessageQueriesMixin
from src.database.queries.metrics_queries import MetricsQueryMixin
from src.database.queries.profile_queries import ProfileQueryMixin
from src.database.queries.project_queries import ProjectQueryMixin
from src.database.queries.provider_usage_queries import ProviderUsageQueryMixin
from src.database.queries.repo_queries import RepoQueryMixin
from src.database.queries.result_queries import ResultQueryMixin
from src.database.queries.task_queries import TaskQueryMixin
from src.database.queries.token_queries import TokenQueryMixin
from src.database.queries.transcript_queries import TranscriptQueryMixin
from src.database.queries.workflow_queries import WorkflowQueryMixin
from src.database.queries.workspace_queries import WorkspaceQueryMixin

__all__ = [
    "AgentQueryMixin",
    "ArchiveQueryMixin",
    "AssignmentRouteQueryMixin",
    "BlockedStateMixin",
    "ChatQueryMixin",
    "ClaimQueryMixin",
    "DependencyQueryMixin",
    "DigestQueryMixin",
    "EventQueryMixin",
    "GateQueriesMixin",
    "HierarchyQueryMixin",
    "MessageQueriesMixin",
    "MetricsQueryMixin",
    "ProfileQueryMixin",
    "ProjectQueryMixin",
    "ProviderUsageQueryMixin",
    "RepoQueryMixin",
    "ResultQueryMixin",
    "TaskQueryMixin",
    "TokenQueryMixin",
    "TranscriptQueryMixin",
    "WorkflowQueryMixin",
    "WorkspaceQueryMixin",
]
