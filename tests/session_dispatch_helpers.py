"""Test-only helpers for driving the orchestrator's *session* dispatch path.

With the runtime subsystem removed, ``_execute_task`` no longer consults an
injected adapter factory: it requires ``sessions.enabled`` plus a profile
carrying a ``harness`` (``_is_session_routed``) and raises otherwise.  Suites
that need a task to actually leave READY build their orchestrator with these
helpers and the ``fake`` session provider, then assert on what a session
launch actually produces:

* the ``sessions`` row (``db.get_session_for_task``) — profile, harness,
  provider, state;
* the :class:`~src.sessions.provider.SessionSpec` the provider received
  (``fake_provider(orch).starts``) — argv, env markers, bootstrap prompt;
* the prime document (``render_prime``) — the startup prompt the agent gets
  back from ``aq prime``, which is where L0/L1/L2 now live.

The ``session_orch`` fixture in ``tests/conftest.py`` wraps
:func:`make_session_orch`; it is the same pattern ``tests/test_orchestrator.py``
uses for its scheduling tests, shared here so the suites ported off the
deleted adapter factory do not each grow their own copy.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from unittest.mock import AsyncMock

from src.config import AppConfig
from src.intelligence_classes import IntelligenceClass
from src.models import AgentProfile, Project, RepoSourceType, Workspace
from src.orchestrator import Orchestrator
from src.sessions.harness_parser import Harness
from tests.assignment_routing_helpers import install_already_routed
from tests.git_mock_helpers import stub_repo_root_identity

__all__ = [
    "SESSION_CLASSES",
    "create_session_profile",
    "create_session_project",
    "drain_running_tasks",
    "fake_provider",
    "make_session_orch",
    "prime_bodies",
    "render_prime",
    "write_vault_profile",
]

SESSION_CLASSES = {
    "standard-medium": IntelligenceClass(
        "standard-medium", "Standard", "", {"anthropic": {"model": "claude-sonnet-5"}}
    ),
}


async def drain_running_tasks(orch: Orchestrator) -> None:
    """Await every background ``_execute_task_safe`` the cycle launched."""
    if orch._running_tasks:
        await asyncio.gather(*orch._running_tasks.values(), return_exceptions=True)
        orch._running_tasks.clear()


async def make_session_orch(tmp_path) -> Orchestrator:
    """An initialized orchestrator that dispatches the way production does."""
    config = AppConfig(
        database_path=str(tmp_path / "test.db"),
        workspace_dir=str(tmp_path / "workspaces"),
        data_dir=str(tmp_path / "data"),
    )
    # The workspaces these suites create are bare directories, not clones;
    # the worktrees P6 default would try to provision them as slots.
    config.worktrees.enabled = False
    config.sessions.enabled = True
    config.sessions.provider = "fake"
    orch = Orchestrator(config)
    await orch.initialize()
    orch.session_spec_builder._intelligence_classes = dict(SESSION_CLASSES)
    # Branch setup is not what these suites assert on.
    orch.git = AsyncMock()
    # ``_prepare_workspace`` writes the managed excludes at whatever path this
    # returns.  Left as a bare AsyncMock it answers with a MagicMock, and the
    # write lands in a stray ``AsyncMock/`` directory under the CWD.
    orch.git.aget_git_path = AsyncMock(
        side_effect=lambda cwd, path: os.path.join(cwd, ".git", path)
    )
    # ``_prepare_workspace`` also proves the checkout it is about to hand off is
    # the repository *root*.  Left as a bare AsyncMock that identity query
    # answers with a MagicMock, git setup fails closed and no session is
    # launched at all -- which reads here as "session was never launched".
    stub_repo_root_identity(orch.git)
    orch.harness_registry.upsert(
        Harness(
            id="claude",
            name="claude",
            command="claude",
            prompt_mode="arg",
            session_id_flag="--session-id",
            process_names=("claude",),
        )
    )
    install_already_routed(orch)
    return orch


async def create_session_profile(orch: Orchestrator, profile_id: str, **fields) -> AgentProfile:
    """A profile the session path can route: it carries a harness and a class."""
    fields.setdefault("name", profile_id)
    fields.setdefault("harness", "claude")
    fields.setdefault("default_class", "standard-medium")
    profile = AgentProfile(id=profile_id, **fields)
    await orch.db.create_profile(profile)
    return profile


async def create_session_project(
    orch: Orchestrator,
    *,
    project_id: str = "p-1",
    default_profile_id: str | None = "claude",
    create_profile: bool = True,
) -> str:
    """A project (defaulting to *default_profile_id*) with one bound workspace.

    Returns the workspace path.  When *create_profile* is true the default
    profile is created first so the project's FK is satisfied; pass
    ``create_profile=False`` when the test registers profiles itself.
    """
    if default_profile_id and create_profile:
        await create_session_profile(orch, default_profile_id)
    await orch.db.create_project(
        Project(id=project_id, name="test-project", default_profile_id=default_profile_id)
    )
    path = os.path.join(orch.config.workspace_dir, project_id)
    os.makedirs(path, exist_ok=True)
    await orch.db.create_workspace(
        Workspace(
            id=f"ws-{project_id}",
            project_id=project_id,
            workspace_path=path,
            source_type=RepoSourceType.LINK,
            kind_id="project-repo",
        )
    )
    return path


def fake_provider(orch: Orchestrator):
    """The one ``FakeProvider`` instance the orchestrator launches through.

    ``SessionProviderRegistry.create`` caches instances per name, so this is
    the same object ``_launch_session_for_task_locked`` called ``start`` on.
    """
    return orch.session_providers.create("fake", orch.config)


def write_vault_profile(config: AppConfig, profile_id: str, content: str) -> Path:
    """Write ``vault/agent-types/<profile_id>/profile.md`` — prime's L0 source."""
    path = Path(config.vault_agent_types) / profile_id / "profile.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


async def render_prime(orch: Orchestrator, task_id: str):
    """Render the dispatched task's prime document — what ``aq prime`` returns."""
    from src.prime import PrimeRenderer

    return await PrimeRenderer(orch.db, orch.config).render_for_task(task_id)


def prime_bodies(doc) -> dict[str, str]:
    """``{section_key: body}`` for a rendered prime document."""
    return {section.key: section.body for section in doc.sections}
