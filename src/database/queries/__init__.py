"""Query mixins for each database domain.

Each mixin class provides the CRUD and query methods for one domain
(projects, tasks, agents, etc.).  They expect ``self._db`` to be an
open aiosqlite connection.  The SQLite adapter composes them via
multiple inheritance.
"""

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
from src.database.queries.workflow_queries import WorkflowQueryMixin
from src.database.queries.workspace_queries import WorkspaceQueryMixin

__all__ = [
    "AgentQueryMixin",
    "ArchiveQueryMixin",
    "ChatQueryMixin",
    "DependencyQueryMixin",
    "EventQueryMixin",
    "HookQueryMixin",
    "ProfileQueryMixin",
    "ProjectQueryMixin",
    "RepoQueryMixin",
    "ResultQueryMixin",
    "TaskQueryMixin",
    "TokenQueryMixin",
    "WorkflowQueryMixin",
    "WorkspaceQueryMixin",
]
