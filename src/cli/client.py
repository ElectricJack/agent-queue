"""Async database client for CLI operations.

Thin wrapper around ``src.database.Database`` that handles config
loading, database path resolution, and provides convenient methods
for CLI commands.  All reads go directly to the database; writes
use the same models and state machine the rest of AgentQueue uses.
"""

from __future__ import annotations

import os
from typing import Any

from src.database import Database
from src.models import (
    Agent,
    AgentState,
    Hook,
    HookRun,
    Project,
    ProjectStatus,
    Task,
    TaskStatus,
    TaskType,
    VerificationType,
    Workspace,
)


def _default_db_path() -> str:
    """Resolve the database path from config or well-known defaults."""
    # 1. Explicit env var
    env_path = os.environ.get("AGENT_QUEUE_DB")
    if env_path:
        return env_path

    # 2. Try loading from config YAML
    config_dir = os.path.expanduser("~/.agent-queue")
    config_file = os.path.join(config_dir, "config.yaml")
    if os.path.exists(config_file):
        try:
            import yaml
            with open(config_file) as f:
                cfg = yaml.safe_load(f) or {}
            db_path = cfg.get("database", {}).get("path")
            if db_path:
                return os.path.expanduser(db_path)
        except Exception:
            pass

    # 3. Well-known default
    return os.path.join(config_dir, "agent-queue.db")


class CLIClient:
    """Lightweight async client for CLI database operations.

    Usage::

        async with CLIClient() as client:
            tasks = await client.list_tasks(project_id="myproj")
    """

    def __init__(self, db_path: str | None = None):
        self._db_path = db_path or _default_db_path()
        self._db: Database | None = None

    async def connect(self) -> None:
        """Open the database connection."""
        if not os.path.exists(self._db_path):
            raise FileNotFoundError(
                f"Database not found at {self._db_path}. "
                "Is AgentQueue running? Set AGENT_QUEUE_DB to override."
            )
        self._db = Database(self._db_path)
        await self._db.initialize()

    async def close(self) -> None:
        """Close the database connection."""
        if self._db:
            await self._db.close()
            self._db = None

    async def __aenter__(self) -> CLIClient:
        await self.connect()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    @property
    def db(self) -> Database:
        assert self._db is not None, "CLIClient not connected"
        return self._db

    # ----- Tasks -----

    async def list_tasks(
        self,
        project_id: str | None = None,
        status: TaskStatus | None = None,
        active_only: bool = False,
    ) -> list[Task]:
        if active_only:
            return await self.db.list_active_tasks(project_id=project_id)
        return await self.db.list_tasks(project_id=project_id, status=status)

    async def get_task(self, task_id: str) -> Task | None:
        return await self.db.get_task(task_id)

    async def search_tasks(self, query: str, project_id: str | None = None) -> list[Task]:
        """Search tasks by title/description substring match."""
        all_tasks = await self.db.list_tasks(project_id=project_id)
        q = query.lower()
        return [
            t for t in all_tasks
            if q in t.title.lower() or q in t.description.lower()
        ]

    async def create_task(
        self,
        project_id: str,
        title: str,
        description: str,
        priority: int = 100,
        task_type: str | None = None,
        requires_approval: bool = False,
    ) -> Task:
        # Import and use generate_task_id with db for collision checking
        from src.task_names import generate_task_id
        task_id = await generate_task_id(self.db)
        task = Task(
            id=task_id,
            project_id=project_id,
            title=title,
            description=description,
            priority=priority,
            status=TaskStatus.DEFINED,
            task_type=TaskType(task_type) if task_type else None,
            requires_approval=requires_approval,
        )
        await self.db.create_task(task)
        return task

    async def update_task_status(self, task_id: str, new_status: TaskStatus) -> None:
        await self.db.transition_task(task_id, new_status, context="cli")

    async def stop_task(self, task_id: str) -> None:
        await self.db.transition_task(task_id, TaskStatus.FAILED, context="cli:stop")

    async def restart_task(self, task_id: str) -> None:
        await self.db.update_task(task_id, retry_count=0)
        await self.db.transition_task(task_id, TaskStatus.READY, context="cli:restart")

    async def approve_task(self, task_id: str) -> None:
        await self.db.transition_task(
            task_id, TaskStatus.ASSIGNED, context="cli:approve"
        )

    async def get_task_tree(self, task_id: str) -> dict | None:
        return await self.db.get_task_tree(task_id)

    async def get_task_dependencies(self, task_id: str) -> list[str]:
        deps = await self.db.get_dependencies(task_id)
        return list(deps)

    async def get_task_dependents(self, task_id: str) -> list[str]:
        deps = await self.db.get_dependents(task_id)
        return list(deps)

    async def count_tasks_by_status(
        self, project_id: str | None = None
    ) -> dict[str, int]:
        return await self.db.count_tasks_by_status(project_id=project_id)

    # ----- Projects -----

    async def list_projects(
        self, status: ProjectStatus | None = None
    ) -> list[Project]:
        return await self.db.list_projects(status=status)

    async def get_project(self, project_id: str) -> Project | None:
        return await self.db.get_project(project_id)

    # ----- Agents -----

    async def list_agents(self) -> list[Agent]:
        return await self.db.list_agents()

    async def get_agent(self, agent_id: str) -> Agent | None:
        return await self.db.get_agent(agent_id)

    # ----- Hooks -----

    async def list_hooks(
        self, project_id: str | None = None, enabled: bool | None = None
    ) -> list[Hook]:
        return await self.db.list_hooks(project_id=project_id, enabled=enabled)

    async def get_hook(self, hook_id: str) -> Hook | None:
        return await self.db.get_hook(hook_id)

    async def list_hook_runs(self, hook_id: str, limit: int = 20) -> list[HookRun]:
        return await self.db.list_hook_runs(hook_id, limit=limit)

    # ----- Workspaces -----

    async def list_workspaces(self, project_id: str | None = None) -> list[Workspace]:
        if project_id:
            return await self.db.list_workspaces(project_id)
        # Aggregate across all projects
        projects = await self.list_projects()
        all_ws: list[Workspace] = []
        for p in projects:
            all_ws.extend(await self.db.list_workspaces(p.id))
        return all_ws

    # ----- Plugins -----

    async def list_plugins(self, status: str | None = None) -> list[dict]:
        return await self.db.list_plugins(status=status)

    async def get_plugin(self, plugin_id: str) -> dict | None:
        return await self.db.get_plugin(plugin_id)

    async def create_plugin(self, **kwargs) -> None:
        await self.db.create_plugin(**kwargs)

    async def update_plugin(self, plugin_id: str, **kwargs) -> None:
        await self.db.update_plugin(plugin_id, **kwargs)

    async def delete_plugin(self, plugin_id: str) -> None:
        await self.db.delete_plugin(plugin_id)

    async def delete_plugin_data_all(self, plugin_id: str) -> None:
        await self.db.delete_plugin_data_all(plugin_id)
