"""Service Protocol facades for internal plugins.

Internal plugins access core systems through typed service interfaces
rather than raw internal objects.  Each Protocol defines a stable
contract; implementation wrappers delegate to the real managers.

Services available to internal plugins via ``ctx.get_service(name)``:

- ``"git"``       — :class:`GitService`
- ``"db"``        — :class:`DatabaseService`
- ``"memory"``    — :class:`MemoryService`
- ``"workspace"`` — :class:`WorkspaceService`
- ``"config"``    — :class:`ConfigService`
"""

from __future__ import annotations

import logging
import os
from typing import Any, Protocol, TYPE_CHECKING, runtime_checkable

if TYPE_CHECKING:
    from src.config import AppConfig
    from src.database import Database
    from src.git.manager import GitManager
    from src.models import Project, Workspace

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Protocols
# ---------------------------------------------------------------------------


@runtime_checkable
class GitService(Protocol):
    """Async git operations on project workspaces."""

    async def status(self, checkout_path: str) -> dict: ...
    async def commit_all(self, checkout_path: str, message: str) -> bool: ...
    async def pull(self, checkout_path: str, branch: str | None = None) -> str: ...
    async def push(
        self, checkout_path: str, branch: str, *, force_with_lease: bool = False
    ) -> str: ...
    async def create_branch(
        self, checkout_path: str, name: str, *, base: str | None = None
    ) -> None: ...
    async def checkout(self, checkout_path: str, branch: str) -> None: ...
    async def merge(
        self, checkout_path: str, source: str, *, target: str | None = None
    ) -> dict: ...
    async def create_pr(
        self,
        checkout_path: str,
        *,
        title: str,
        body: str,
        base: str | None = None,
        head: str | None = None,
        draft: bool = False,
    ) -> dict: ...
    async def log(self, checkout_path: str, limit: int = 10) -> list[dict]: ...
    async def diff(self, checkout_path: str, base: str, head: str | None = None) -> str: ...
    async def changed_files(self, checkout_path: str, base: str) -> list[str]: ...
    async def current_branch(self, checkout_path: str) -> str: ...
    async def list_branches(self, checkout_path: str) -> list[str]: ...
    async def validate_checkout(self, checkout_path: str) -> bool: ...
    def slugify(self, text: str) -> str: ...


@runtime_checkable
class DatabaseService(Protocol):
    """Thin facade over core database queries."""

    async def get_project(self, project_id: str) -> Any: ...
    async def list_projects(self) -> list: ...
    async def list_workspaces(self, project_id: str | None = None) -> list: ...
    async def get_workspace(self, workspace_id: str) -> Any: ...
    async def get_workspace_by_name(self, project_id: str, name: str) -> Any: ...
    async def list_repos(self, project_id: str | None = None) -> list: ...
    async def get_task(self, task_id: str) -> Any: ...
    async def list_profiles(self) -> list: ...
    async def get_profile(self, profile_id: str) -> Any: ...
    async def create_profile(self, **kwargs: Any) -> Any: ...
    async def update_profile(self, profile_id: str, **kwargs: Any) -> None: ...
    async def delete_profile(self, profile_id: str) -> None: ...
    async def get_project_workspace_path(self, project_id: str) -> str | None: ...


@runtime_checkable
class MemoryService(Protocol):
    """Semantic search and memory management."""

    async def search(
        self, project_id: str, workspace: str, query: str, *, top_k: int = 10
    ) -> list[dict]: ...
    async def batch_search(
        self, project_id: str, workspace: str, queries: list[str], *, top_k: int = 10
    ) -> dict[str, list[dict]]: ...
    async def reindex(self, project_id: str, workspace: str) -> int: ...
    async def compact(self, project_id: str, workspace: str) -> dict: ...
    async def stats(self, project_id: str, workspace: str) -> dict: ...
    async def write_memory(
        self, project_id: str, workspace: str, key: str, content: str
    ) -> str | None: ...
    async def read_memory(self, project_id: str, key: str) -> str | None: ...
    async def get_profile(self, project_id: str) -> str | None: ...
    async def promote_note(
        self, project_id: str, note_filename: str, note_content: str, workspace: str
    ) -> str | None: ...
    async def update_profile(self, project_id: str, content: str, workspace: str) -> str | None: ...
    async def regenerate_profile(self, project_id: str, workspace: str) -> str | None: ...
    # Consolidation
    async def run_daily_consolidation(self, project_id: str, workspace_path: str = "") -> dict: ...
    async def run_deep_consolidation(self, project_id: str, workspace_path: str = "") -> dict: ...
    async def bootstrap_consolidation(
        self,
        project_id: str,
        workspace_path: str = "",
        *,
        project_name: str = "",
        repo_url: str = "",
    ) -> dict: ...
    # Factsheet & knowledge
    async def read_factsheet_raw(self, project_id: str) -> str | None: ...
    def parse_factsheet_yaml(self, content: str) -> dict: ...
    async def write_factsheet_raw(
        self, project_id: str, content: str, workspace_path: str = ""
    ) -> str | None: ...
    async def update_factsheet_field(
        self,
        project_id: str,
        dotted_key: str,
        value: Any,
        workspace_path: str = "",
        *,
        project_name: str = "",
        repo_url: str = "",
    ) -> str | None: ...
    async def list_knowledge_topics(self, project_id: str) -> list[dict]: ...
    async def read_knowledge_topic(self, project_id: str, topic: str) -> str | None: ...
    async def search_all_project_factsheets(
        self, project_ids: list[str], query: str = "", field: str = ""
    ) -> list[dict]: ...
    @property
    def notes_inform_profile(self) -> bool: ...


@runtime_checkable
class MemoryV2ServiceProtocol(Protocol):
    """V2 memory operations via memsearch/Milvus with scoped collections.

    Provides semantic search, KV storage, temporal facts, and cross-scope
    tag search.  Wraps the memsearch fork's ``CollectionRouter`` and
    ``MilvusStore``.

    See ``docs/specs/design/memory-plugin.md`` §3.
    """

    @property
    def available(self) -> bool: ...

    # Semantic search
    async def search(
        self,
        project_id: str,
        query: str,
        *,
        scope: str | None = None,
        topic: str | None = None,
        top_k: int = 10,
    ) -> list[dict]: ...
    async def batch_search(
        self,
        project_id: str,
        queries: list[str],
        *,
        scope: str | None = None,
        topic: str | None = None,
        top_k: int = 10,
    ) -> dict[str, list[dict]]: ...
    async def search_by_tag(
        self, tag: str, *, entry_type: str | None = None, topic: str | None = None, limit: int = 10
    ) -> list[dict]: ...

    # KV operations
    async def kv_get(self, project_id: str, namespace: str, key: str) -> dict | None: ...
    async def kv_set(self, project_id: str, namespace: str, key: str, value: str) -> dict: ...
    async def kv_list(self, project_id: str, namespace: str) -> list[dict]: ...

    # Temporal facts
    async def fact_get(
        self, project_id: str, key: str, *, as_of: int | None = None
    ) -> dict | None: ...
    async def fact_set(self, project_id: str, key: str, value: str) -> dict: ...
    async def fact_history(self, project_id: str, key: str) -> list[dict]: ...

    # Stats
    async def stats(self, project_id: str, *, scope: str | None = None) -> dict: ...

    # Lifecycle
    async def initialize(self) -> None: ...
    async def shutdown(self) -> None: ...


@runtime_checkable
class WorkspaceService(Protocol):
    """Path resolution, validation, and workspace helpers."""

    async def resolve_repo_path(
        self, args: dict, active_project_id: str | None = None
    ) -> tuple[str | None, Any, dict | None]: ...
    async def resolve_workspace(
        self, project_id: str, workspace: str | None
    ) -> tuple[Any, dict | None]: ...
    async def validate_path(self, path: str) -> str | None: ...
    def get_notes_dir(self, project_id: str) -> str: ...
    def resolve_note_path(self, notes_dir: str, title: str) -> str | None: ...


@runtime_checkable
class ConfigService(Protocol):
    """Read-only access to application configuration."""

    @property
    def workspace_dir(self) -> str: ...
    @property
    def data_dir(self) -> str: ...


# ---------------------------------------------------------------------------
# Implementations
# ---------------------------------------------------------------------------


class GitServiceImpl:
    """Wraps ``GitManager`` behind the :class:`GitService` protocol.

    Internal plugins may access ``_manager`` for methods not yet
    on the Protocol surface.  External plugins cannot reach this.
    """

    def __init__(self, git_manager: GitManager) -> None:
        self._git = git_manager
        self._manager = git_manager  # escape hatch for internal plugins

    async def status(self, checkout_path: str) -> dict:
        return await self._git.astatus(checkout_path)

    async def commit_all(self, checkout_path: str, message: str) -> bool:
        return await self._git.acommit_all(checkout_path, message)

    async def pull(self, checkout_path: str, branch: str | None = None) -> str:
        return await self._git.apull(checkout_path, branch=branch)

    async def push(self, checkout_path: str, branch: str, *, force_with_lease: bool = False) -> str:
        return await self._git.apush(checkout_path, branch, force_with_lease=force_with_lease)

    async def create_branch(
        self, checkout_path: str, name: str, *, base: str | None = None
    ) -> None:
        await self._git.acreate_branch(checkout_path, name, base=base)

    async def checkout(self, checkout_path: str, branch: str) -> None:
        await self._git.acheckout(checkout_path, branch)

    async def merge(self, checkout_path: str, source: str, *, target: str | None = None) -> dict:
        return await self._git.amerge(checkout_path, source, target=target)

    async def create_pr(
        self,
        checkout_path: str,
        *,
        title: str,
        body: str,
        base: str | None = None,
        head: str | None = None,
        draft: bool = False,
    ) -> dict:
        return await self._git.acreate_pr(
            checkout_path, title=title, body=body, base=base, head=head, draft=draft
        )

    async def log(self, checkout_path: str, limit: int = 10) -> list[dict]:
        return await self._git.alog(checkout_path, limit=limit)

    async def diff(self, checkout_path: str, base: str, head: str | None = None) -> str:
        return await self._git.adiff(checkout_path, base, head=head)

    async def changed_files(self, checkout_path: str, base: str) -> list[str]:
        return await self._git.achanged_files(checkout_path, base)

    async def current_branch(self, checkout_path: str) -> str:
        return await self._git.acurrent_branch(checkout_path)

    async def list_branches(self, checkout_path: str) -> list[str]:
        return await self._git.alist_branches(checkout_path)

    async def validate_checkout(self, checkout_path: str) -> bool:
        return await self._git.avalidate_checkout(checkout_path)

    def slugify(self, text: str) -> str:
        return self._git.slugify(text)


class DatabaseServiceImpl:
    """Wraps ``Database`` behind the :class:`DatabaseService` protocol."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def get_project(self, project_id: str) -> Any:
        return await self._db.get_project(project_id)

    async def list_projects(self) -> list:
        return await self._db.list_projects()

    async def list_workspaces(self, project_id: str | None = None) -> list:
        return await self._db.list_workspaces(project_id=project_id)

    async def get_workspace(self, workspace_id: str) -> Any:
        return await self._db.get_workspace(workspace_id)

    async def get_workspace_by_name(self, project_id: str, name: str) -> Any:
        return await self._db.get_workspace_by_name(project_id, name)

    async def list_repos(self, project_id: str | None = None) -> list:
        return await self._db.list_repos(project_id=project_id)

    async def get_task(self, task_id: str) -> Any:
        return await self._db.get_task(task_id)

    async def list_profiles(self) -> list:
        return await self._db.list_profiles()

    async def get_profile(self, profile_id: str) -> Any:
        return await self._db.get_profile(profile_id)

    async def create_profile(self, **kwargs: Any) -> Any:
        return await self._db.create_profile(**kwargs)

    async def update_profile(self, profile_id: str, **kwargs: Any) -> None:
        await self._db.update_profile(profile_id, **kwargs)

    async def delete_profile(self, profile_id: str) -> None:
        await self._db.delete_profile(profile_id)

    async def get_project_workspace_path(self, project_id: str) -> str | None:
        return await self._db.get_project_workspace_path(project_id)


class MemoryServiceImpl:
    """Wraps ``MemoryManager`` behind the :class:`MemoryService` protocol."""

    def __init__(self, memory_manager: Any) -> None:
        self._mm = memory_manager

    async def search(
        self, project_id: str, workspace: str, query: str, *, top_k: int = 10
    ) -> list[dict]:
        if not self._mm:
            return []
        return await self._mm.search(project_id, workspace, query, top_k=top_k)

    async def batch_search(
        self, project_id: str, workspace: str, queries: list[str], *, top_k: int = 10
    ) -> dict[str, list[dict]]:
        if not self._mm:
            return {q: [] for q in queries}
        return await self._mm.batch_search(project_id, workspace, queries, top_k=top_k)

    async def scoped_search(
        self,
        query: str,
        *,
        project_id: str | None = None,
        agent_type: str | None = None,
        topic: str | None = None,
        top_k: int = 10,
        weights: dict | None = None,
        full: bool = False,
    ) -> list[dict]:
        """Multi-scope weighted search per spec §6."""
        if not self._mm:
            return []
        return await self._mm.scoped_search(
            query,
            project_id=project_id,
            agent_type=agent_type,
            topic=topic,
            top_k=top_k,
            weights=weights,
            full=full,
        )

    async def scoped_batch_search(
        self,
        queries: list[str],
        *,
        project_id: str | None = None,
        agent_type: str | None = None,
        topic: str | None = None,
        top_k: int = 10,
        weights: dict | None = None,
        full: bool = False,
    ) -> dict[str, list[dict]]:
        """Multi-scope weighted batch search per spec §6."""
        if not self._mm:
            return {q: [] for q in queries}
        return await self._mm.scoped_batch_search(
            queries,
            project_id=project_id,
            agent_type=agent_type,
            topic=topic,
            top_k=top_k,
            weights=weights,
            full=full,
        )

    async def write_memory(
        self, project_id: str, workspace: str, key: str, content: str
    ) -> str | None:
        if not self._mm:
            raise RuntimeError(
                "Memory manager is not enabled. "
                "Ensure 'memory.enabled' is set to true in your configuration."
            )
        return await self._mm.write_memory(project_id, workspace, key, content)

    async def read_memory(self, project_id: str, key: str) -> str | None:
        if not self._mm:
            raise RuntimeError(
                "Memory manager is not enabled. "
                "Ensure 'memory.enabled' is set to true in your configuration."
            )
        return await self._mm.read_memory(project_id, key)

    async def reindex(self, project_id: str, workspace: str) -> int:
        if not self._mm:
            return 0
        return await self._mm.reindex(project_id, workspace)

    async def compact(self, project_id: str, workspace: str) -> dict:
        if not self._mm:
            return {"error": "Memory manager not available"}
        return await self._mm.compact(project_id, workspace)

    async def stats(self, project_id: str, workspace: str) -> dict:
        if not self._mm:
            return {"error": "Memory manager not available"}
        return await self._mm.stats(project_id, workspace)

    async def get_profile(self, project_id: str) -> str | None:
        if not self._mm:
            return None
        return await self._mm.get_profile(project_id)

    async def promote_note(
        self, project_id: str, note_filename: str, note_content: str, workspace: str
    ) -> str | None:
        if not self._mm:
            return None
        return await self._mm.promote_note(project_id, note_filename, note_content, workspace)

    async def update_profile(self, project_id: str, content: str, workspace: str) -> str | None:
        if not self._mm:
            return None
        return await self._mm.update_profile(project_id, content, workspace)

    async def regenerate_profile(self, project_id: str, workspace: str) -> str | None:
        if not self._mm:
            return None
        return await self._mm.regenerate_profile(project_id, workspace)

    # --- Consolidation ---

    async def run_daily_consolidation(self, project_id: str, workspace_path: str = "") -> dict:
        if not self._mm:
            return {"error": "Memory manager not available"}
        return await self._mm.run_daily_consolidation(project_id, workspace_path)

    async def run_deep_consolidation(self, project_id: str, workspace_path: str = "") -> dict:
        if not self._mm:
            return {"error": "Memory manager not available"}
        return await self._mm.run_deep_consolidation(project_id, workspace_path)

    async def bootstrap_consolidation(
        self,
        project_id: str,
        workspace_path: str = "",
        *,
        project_name: str = "",
        repo_url: str = "",
    ) -> dict:
        if not self._mm:
            return {"error": "Memory manager not available"}
        return await self._mm.bootstrap_consolidation(
            project_id, workspace_path, project_name=project_name, repo_url=repo_url
        )

    # --- Factsheet & Knowledge ---

    async def read_factsheet_raw(self, project_id: str) -> str | None:
        if not self._mm:
            return None
        return await self._mm.read_factsheet_raw(project_id)

    def parse_factsheet_yaml(self, content: str) -> dict:
        if not self._mm:
            return {}
        return self._mm.parse_factsheet_yaml(content)

    async def write_factsheet_raw(
        self, project_id: str, content: str, workspace_path: str = ""
    ) -> str | None:
        if not self._mm:
            return None
        return await self._mm.write_factsheet_raw(project_id, content, workspace_path)

    async def update_factsheet_field(
        self,
        project_id: str,
        dotted_key: str,
        value: Any,
        workspace_path: str = "",
        *,
        project_name: str = "",
        repo_url: str = "",
    ) -> str | None:
        if not self._mm:
            return None
        return await self._mm.update_factsheet_field(
            project_id,
            dotted_key,
            value,
            workspace_path,
            project_name=project_name,
            repo_url=repo_url,
        )

    async def list_knowledge_topics(self, project_id: str) -> list[dict]:
        if not self._mm:
            return []
        return await self._mm.list_knowledge_topics(project_id)

    async def read_knowledge_topic(self, project_id: str, topic: str) -> str | None:
        if not self._mm:
            return None
        return await self._mm.read_knowledge_topic(project_id, topic)

    async def search_all_project_factsheets(
        self, project_ids: list[str], query: str = "", field: str = ""
    ) -> list[dict]:
        if not self._mm:
            return []
        return await self._mm.search_all_project_factsheets(project_ids, query=query, field=field)

    @property
    def notes_inform_profile(self) -> bool:
        if not self._mm:
            return False
        return getattr(self._mm.config, "notes_inform_profile", False)


class WorkspaceServiceImpl:
    """Path resolution and validation helpers.

    Extracted from ``CommandHandler._resolve_repo_path``,
    ``_validate_path``, ``_get_notes_dir``, and ``_resolve_note_path``.
    """

    def __init__(self, db: Database, git: GitManager, config: AppConfig) -> None:
        self._db = db
        self._git = git
        self._config = config

    async def resolve_repo_path(
        self,
        args: dict,
        active_project_id: str | None = None,
    ) -> tuple[str | None, Any, dict | None]:
        """Resolve git checkout path for a project.

        Returns ``(checkout_path, project, error_dict)``.
        """
        from src.models import RepoSourceType

        project_id = args.get("project_id")
        if not project_id:
            if active_project_id:
                project_id = active_project_id
                args["project_id"] = project_id
            else:
                return None, None, {"error": "project_id is required (no active project set)"}

        project = await self._db.get_project(project_id) if project_id else None
        if project_id and not project:
            return None, None, {"error": f"Project '{project_id}' not found"}

        checkout_path = None
        workspace_param = args.get("workspace")
        if workspace_param and project_id:
            ws, ws_err = await self.resolve_workspace(project_id, workspace_param)
            if ws_err:
                return None, project, ws_err
            if ws:
                checkout_path = ws.workspace_path

        if not checkout_path and project_id:
            workspaces = await self._db.list_workspaces(project_id=project_id)
            if workspaces:
                checkout_path = workspaces[0].workspace_path

        if not checkout_path and project_id:
            repos = await self._db.list_repos(project_id=project_id)
            if repos:
                repo = repos[0]
                if repo.source_type == RepoSourceType.LINK and repo.source_path:
                    checkout_path = repo.source_path
                elif (
                    repo.source_type in (RepoSourceType.CLONE, RepoSourceType.INIT)
                    and repo.checkout_base_path
                ):
                    checkout_path = repo.checkout_base_path

        if not checkout_path:
            if not project:
                return None, None, {"error": "No workspace found and no project context"}
            return (
                None,
                None,
                {
                    "error": f"Project '{project_id}' has no workspaces. "
                    f"Use /add-workspace to create one."
                },
            )

        if not os.path.isdir(checkout_path):
            return None, project, {"error": f"Path not found: {checkout_path}"}
        if not await self._git.avalidate_checkout(checkout_path):
            return None, project, {"error": f"Not a valid git repository: {checkout_path}"}

        return checkout_path, project, None

    async def resolve_workspace(
        self,
        project_id: str,
        workspace: str | None,
    ) -> tuple[Any, dict | None]:
        """Resolve a workspace by ID or name within a project."""
        if not workspace:
            return None, None
        ws = await self._db.get_workspace(workspace)
        if ws:
            if ws.project_id != project_id:
                return None, {"error": f"Workspace '{workspace}' belongs to a different project"}
            return ws, None
        ws = await self._db.get_workspace_by_name(project_id, workspace)
        if ws:
            return ws, None
        return None, {"error": f"Workspace '{workspace}' not found"}

    async def validate_path(self, path: str) -> str | None:
        """Validate that a path resolves within an allowed directory."""
        real = os.path.realpath(path)
        workspace_real = os.path.realpath(self._config.workspace_dir)
        if real.startswith(workspace_real + os.sep) or real == workspace_real:
            return real
        repos = await self._db.list_repos()
        for repo in repos:
            if repo.source_path:
                repo_real = os.path.realpath(repo.source_path)
                if real.startswith(repo_real + os.sep) or real == repo_real:
                    return real
        workspaces = await self._db.list_workspaces()
        for ws in workspaces:
            ws_real = os.path.realpath(ws.workspace_path)
            if real.startswith(ws_real + os.sep) or real == ws_real:
                return real
        return None

    def get_notes_dir(self, project_id: str) -> str:
        """Return the central notes directory for a project.

        Notes live in the vault at ``vault/projects/{project_id}/notes/``.
        """
        return os.path.join(self._config.data_dir, "vault", "projects", project_id, "notes")

    def resolve_note_path(self, notes_dir: str, title: str) -> str | None:
        """Resolve a note file path from a title, filename, or slug.

        Tries, in order:
        1. Exact filename (if ends with .md)
        2. Title + .md
        3. Slugified title + .md
        4. Case-insensitive scan of the notes directory
        """
        # 1. Exact filename
        if title.endswith(".md"):
            fpath = os.path.join(notes_dir, title)
            if os.path.isfile(fpath):
                return fpath
        # 2. Title as-is + .md
        fpath = os.path.join(notes_dir, f"{title}.md")
        if os.path.isfile(fpath):
            return fpath
        # 3. Slugified title
        slug = self._git.slugify(title)
        if slug:
            fpath = os.path.join(notes_dir, f"{slug}.md")
            if os.path.isfile(fpath):
                return fpath
        # 4. Case-insensitive scan — match against filename stem or H1 title
        if os.path.isdir(notes_dir):
            title_lower = title.lower().removesuffix(".md")
            for fname in os.listdir(notes_dir):
                if not fname.endswith(".md"):
                    continue
                fpath = os.path.join(notes_dir, fname)
                stem = fname[:-3].lower()
                if stem == title_lower or stem.replace("-", " ") == title_lower:
                    return fpath
                # Check H1 title inside the file
                try:
                    with open(fpath, "r") as f:
                        first_line = f.readline().strip()
                    if first_line.startswith("# "):
                        h1 = first_line[2:].strip()
                        if h1.lower() == title_lower:
                            return fpath
                except OSError:
                    continue
        return None


class ConfigServiceImpl:
    """Wraps ``AppConfig`` behind the :class:`ConfigService` protocol."""

    def __init__(self, config: AppConfig) -> None:
        self._config = config

    @property
    def workspace_dir(self) -> str:
        return self._config.workspace_dir

    @property
    def data_dir(self) -> str:
        return self._config.data_dir


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def build_internal_services(
    *,
    db: Database,
    git: GitManager,
    config: AppConfig,
    memory_manager: Any = None,
    memory_v2_service: Any = None,
) -> dict[str, Any]:
    """Build the services dict for internal plugin contexts.

    Called by the PluginRegistry during internal plugin loading.

    Parameters
    ----------
    db:
        Database instance.
    git:
        Git manager instance.
    config:
        Application configuration.
    memory_manager:
        Optional v1 MemoryManager instance.
    memory_v2_service:
        Optional v2 MemoryV2Service instance.  When provided, exposed
        as ``"memory_v2"`` for plugins that need v2-specific operations
        (KV, temporal facts, scoped search).
    """
    services: dict[str, Any] = {
        "git": GitServiceImpl(git),
        "db": DatabaseServiceImpl(db),
        "memory": MemoryServiceImpl(memory_manager),
        "workspace": WorkspaceServiceImpl(db, git, config),
        "config": ConfigServiceImpl(config),
    }
    if memory_v2_service is not None:
        services["memory_v2"] = memory_v2_service
    return services
