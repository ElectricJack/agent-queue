"""Which delivery path a task or a project actually has.

A ``pull_request`` integration mode needs a repository that can host a pull
request, and AQ opens pull requests only on github.com.  A project whose
origin is a bare repository on disk (``~/.agent-queue/local-remotes/<name>.git``)
or another host has nowhere to open one.  Before this module such a project
inherited ``pull_request`` from ``integration.default_mode``; its workers
pushed their branches and closed ``pass``, and git verification blocked every
one with "Could not authorize PR repository" (agile-flare, stark-vault).

The rules here are shared by the completion pipeline
(``GitOpsMixin._effective_integration_mode``), the task read model, the
``integration.delivery_path`` doctor check and ``task_deliver``:

* a ``pull_request`` mode that only the system default chose resolves to
  ``direct`` on a repository on disk
  (:func:`src.models.resolve_integration_mode_with_source`).  Only there: a
  hosted remote that is not github.com (GitLab, GitHub Enterprise) keeps the
  default rather than have its work land unreviewed, and doctor reports it;
* an explicit ``pull_request`` is kept, and doctor names the project as
  having no working delivery path.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from src.models import (
    resolve_integration_mode_with_source,
)

#: Project ``hierarchical_integration_mode`` values whose own publisher owns
#: delivery; the per-task ``integration_mode`` does not apply to them.
MANAGED_MODES = frozenset({"development", "hierarchy", "train"})


def lacks_pull_request_host(repository_url: str | None) -> bool:
    """True when *repository_url* is set and is not a github.com repository.

    An unset URL is not a verdict: a project without a remote has no pull
    request to fail on, and its callers keep their existing behaviour.
    """
    if not repository_url or not repository_url.strip():
        return False
    from src.projects.github import GitHubError, parse_github_repository

    try:
        parse_github_repository(repository_url)
    except GitHubError:
        return True
    return False


def local_repository_path(repository_url: str | None) -> Path | None:
    """The filesystem path a local repository URL names, else ``None``."""
    url = (repository_url or "").strip()
    if url.startswith("file://"):
        url = url.removeprefix("file://")
    elif not url.startswith(("/", "~")):
        return None
    return Path(url).expanduser()


async def task_repository_url(db: Any, task: Any, project: Any = None) -> str:
    """The repository a task delivers to: its bound repository row, else the project's."""
    if task.repo_id:
        repo = await db.get_repo(task.repo_id)
        return repo.url if repo and repo.project_id == task.project_id else ""
    if project is None:
        project = await db.get_project(task.project_id)
    return (project.repo_url or "") if project else ""


async def effective_integration_mode(
    db: Any, task: Any, *, default_mode: str
) -> tuple[str, str]:
    """``(mode, source)`` for *task*: the single authority for the policy chain."""
    parent_mode: str | None = None
    if task.is_plan_subtask and task.parent_task_id:
        parent = await db.get_task(task.parent_task_id)
        parent_mode = parent.integration_mode if parent else None
    project = await db.get_project(task.project_id)
    repository_url = await task_repository_url(db, task, project)
    return resolve_integration_mode_with_source(
        task.integration_mode,
        parent_task_mode=parent_mode,
        project_mode=project.integration_mode if project else None,
        default_mode=default_mode,
        pull_requests_available=local_repository_path(repository_url) is None,
    )
