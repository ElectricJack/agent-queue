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

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.database.queries.agent_queries import AgentQueryMixin
    from src.database.queries.archive_queries import ArchiveQueryMixin
    from src.database.queries.ask_queries import AskQueriesMixin
    from src.database.queries.ask_delivery_queries import AskDeliveryQueriesMixin
    from src.database.queries.ask_input_queries import AskInputQueriesMixin
    from src.database.queries.assignment_route_queries import AssignmentRouteQueryMixin
    from src.database.queries.blocked_state import BlockedStateMixin
    from src.database.queries.chat_queries import ChatQueryMixin
    from src.database.queries.claim_queries import ClaimQueryMixin
    from src.database.queries.dashboard_state_queries import DashboardStateQueriesMixin
    from src.database.queries.dependency_queries import DependencyQueryMixin
    from src.database.queries.digest_queries import (
        DigestQueryMixin,
        DigestSupervisorQueriesMixin,
    )
    from src.database.queries.event_queries import EventQueryMixin
    from src.database.queries.gate_queries import GateQueriesMixin
    from src.database.queries.hierarchy_queries import HierarchyQueryMixin
    from src.database.queries.message_queries import MessageQueriesMixin
    from src.database.queries.metrics_queries import MetricsQueryMixin
    from src.database.queries.profile_queries import ProfileQueryMixin
    from src.database.queries.project_queries import ProjectQueryMixin
    from src.database.queries.provider_availability_queries import ProviderAvailabilityQueryMixin
    from src.database.queries.provider_usage_queries import ProviderUsageQueryMixin
    from src.database.queries.repo_queries import RepoQueryMixin
    from src.database.queries.result_queries import ResultQueryMixin
    from src.database.queries.review_queries import ReviewQueriesMixin
    from src.database.queries.task_queries import TaskQueryMixin
    from src.database.queries.task_reroute_queries import TaskRerouteQueryMixin
    from src.database.queries.token_queries import TokenQueryMixin
    from src.database.queries.transcript_queries import TranscriptQueryMixin
    from src.database.queries.workflow_queries import WorkflowQueryMixin
    from src.database.queries.workspace_queries import WorkspaceQueryMixin


def __getattr__(name: str):
    if name == "AgentQueryMixin":
        from src.database.queries.agent_queries import AgentQueryMixin as value
    elif name == "ArchiveQueryMixin":
        from src.database.queries.archive_queries import ArchiveQueryMixin as value
    elif name == "AskQueriesMixin":
        from src.database.queries.ask_queries import AskQueriesMixin as value
    elif name == "AskDeliveryQueriesMixin":
        from src.database.queries.ask_delivery_queries import AskDeliveryQueriesMixin as value
    elif name == "AskInputQueriesMixin":
        from src.database.queries.ask_input_queries import AskInputQueriesMixin as value
    elif name == "AssignmentRouteQueryMixin":
        from src.database.queries.assignment_route_queries import AssignmentRouteQueryMixin as value
    elif name == "BlockedStateMixin":
        from src.database.queries.blocked_state import BlockedStateMixin as value
    elif name == "ChatQueryMixin":
        from src.database.queries.chat_queries import ChatQueryMixin as value
    elif name == "ClaimQueryMixin":
        from src.database.queries.claim_queries import ClaimQueryMixin as value
    elif name == "DashboardStateQueriesMixin":
        from src.database.queries.dashboard_state_queries import DashboardStateQueriesMixin as value
    elif name == "DependencyQueryMixin":
        from src.database.queries.dependency_queries import DependencyQueryMixin as value
    elif name == "DigestQueryMixin":
        from src.database.queries.digest_queries import DigestQueryMixin as value
    elif name == "DigestSupervisorQueriesMixin":
        from src.database.queries.digest_queries import DigestSupervisorQueriesMixin as value
    elif name == "EventQueryMixin":
        from src.database.queries.event_queries import EventQueryMixin as value
    elif name == "GateQueriesMixin":
        from src.database.queries.gate_queries import GateQueriesMixin as value
    elif name == "HierarchyQueryMixin":
        from src.database.queries.hierarchy_queries import HierarchyQueryMixin as value
    elif name == "MessageQueriesMixin":
        from src.database.queries.message_queries import MessageQueriesMixin as value
    elif name == "MetricsQueryMixin":
        from src.database.queries.metrics_queries import MetricsQueryMixin as value
    elif name == "ProfileQueryMixin":
        from src.database.queries.profile_queries import ProfileQueryMixin as value
    elif name == "ProjectQueryMixin":
        from src.database.queries.project_queries import ProjectQueryMixin as value
    elif name == "ProviderAvailabilityQueryMixin":
        from src.database.queries.provider_availability_queries import ProviderAvailabilityQueryMixin as value
    elif name == "ProviderUsageQueryMixin":
        from src.database.queries.provider_usage_queries import ProviderUsageQueryMixin as value
    elif name == "RepoQueryMixin":
        from src.database.queries.repo_queries import RepoQueryMixin as value
    elif name == "ResultQueryMixin":
        from src.database.queries.result_queries import ResultQueryMixin as value
    elif name == "ReviewQueriesMixin":
        from src.database.queries.review_queries import ReviewQueriesMixin as value
    elif name == "TaskQueryMixin":
        from src.database.queries.task_queries import TaskQueryMixin as value
    elif name == "TaskRerouteQueryMixin":
        from src.database.queries.task_reroute_queries import TaskRerouteQueryMixin as value
    elif name == "TokenQueryMixin":
        from src.database.queries.token_queries import TokenQueryMixin as value
    elif name == "TranscriptQueryMixin":
        from src.database.queries.transcript_queries import TranscriptQueryMixin as value
    elif name == "WorkflowQueryMixin":
        from src.database.queries.workflow_queries import WorkflowQueryMixin as value
    elif name == "WorkspaceQueryMixin":
        from src.database.queries.workspace_queries import WorkspaceQueryMixin as value
    else:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))


__all__ = [
    "AgentQueryMixin",
    "ArchiveQueryMixin",
    "AskQueriesMixin",
    "AskDeliveryQueriesMixin",
    "AskInputQueriesMixin",
    "AssignmentRouteQueryMixin",
    "BlockedStateMixin",
    "ChatQueryMixin",
    "ClaimQueryMixin",
    "DashboardStateQueriesMixin",
    "DependencyQueryMixin",
    "DigestQueryMixin",
    "DigestSupervisorQueriesMixin",
    "EventQueryMixin",
    "GateQueriesMixin",
    "HierarchyQueryMixin",
    "MessageQueriesMixin",
    "MetricsQueryMixin",
    "ProfileQueryMixin",
    "ProjectQueryMixin",
    "ProviderAvailabilityQueryMixin",
    "ProviderUsageQueryMixin",
    "RepoQueryMixin",
    "ResultQueryMixin",
    "ReviewQueriesMixin",
    "TaskQueryMixin",
    "TaskRerouteQueryMixin",
    "TokenQueryMixin",
    "TranscriptQueryMixin",
    "WorkflowQueryMixin",
    "WorkspaceQueryMixin",
]
