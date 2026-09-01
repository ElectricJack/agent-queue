"""End-to-end assignment playbook route followed by deterministic scheduling."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.assignment_routing import assignment_input_hash
from src.config import AppConfig, DiscordConfig
from src.database import Database
from src.intelligence_classes import IntelligenceClass
from src.llm import LLMRunResult
from src.models import (
    Agent,
    AgentProfile,
    AgentState,
    Project,
    RepoSourceType,
    Task,
    TaskStatus,
    Workspace,
)
from src.orchestrator import Orchestrator
from src.playbooks.manager import PlaybookManager
from src.playbooks.services import PlaybookServices


ASSIGNMENT_PLAYBOOK = """---
id: default-assignment-routing
kind: assignment-routing
role: assignment-routing
scope: system
triggers: [assignment.route.requested]
---
Choose the least expensive reliable intelligence class for every task.
"""


@pytest.mark.asyncio
async def test_route_then_existing_scheduler_selects_concrete_agent(tmp_path):
    db = Database(str(tmp_path / "routing-e2e.db"))
    await db.initialize()
    try:
        await db.create_profile(
            AgentProfile(id="coder", name="Coder", harness="claude", lifecycle="task")
        )
        await db.create_project(
            Project(id="project", name="Project", default_profile_id="coder")
        )
        await db.create_agent(
            Agent(
                id="existing-worker",
                name="Existing worker",
                profile_id="coder",
                harness="claude",
                state=AgentState.IDLE,
            )
        )
        await db.create_workspace(
            Workspace(
                id="workspace",
                project_id="project",
                workspace_path=str(tmp_path / "checkout"),
                source_type=RepoSourceType.LINK,
                kind_id="project-repo",
            )
        )
        await db.create_task(
            Task(
                id="task",
                project_id="project",
                title="Small fix",
                description="Make the localized change",
                status=TaskStatus.READY,
            )
        )

        config = AppConfig(
            discord=DiscordConfig(bot_token="t", guild_id="1"),
            workspace_dir=str(tmp_path / "workspaces"),
            database_path=str(tmp_path / "routing-e2e.db"),
            data_dir=str(tmp_path / "data"),
        )
        orch = Orchestrator(config)
        orch.db = db
        orch._agent_reconciler._db = db
        orch.git = MagicMock()
        orch.session_spec_builder._intelligence_classes = {
            "fast-low": IntelligenceClass(
                "fast-low",
                "Fast",
                "",
                {"anthropic": {"model": "claude-haiku"}},
            )
        }
        manager = PlaybookManager(config=None)
        assert (await manager.compile_playbook(ASSIGNMENT_PLAYBOOK)).success
        orch.playbook_manager = manager

        services = PlaybookServices.for_tests(MagicMock())
        services.llm = MagicMock()
        services.llm.config = SimpleNamespace(max_tokens=1024)
        services.llm.complete = AsyncMock()
        task = await db.get_task("task")
        services.llm.run_tools = AsyncMock(return_value=LLMRunResult(
            text=json.dumps({"decisions": [{
                "task_id": task.id,
                "input_hash": assignment_input_hash(task),
                "intelligence_class": "fast-low",
                "provider": None,
                "reason": "A small localized change fits the fast class.",
            }]}),
            transcript=[],
            turns=1,
            stopped_by="done",
        ))
        services.handler.set_caller_profile = MagicMock()
        services.handler.set_active_project = MagicMock()
        orch.playbook_services = lambda: services

        routed = await orch.assignment_routing.reconcile()
        actions = await orch._schedule()

        assert routed["task"].intelligence_class == "fast-low"
        assert (await db.get_task("task")).profile_id is None
        assert [(action.task_id, action.agent_id) for action in actions] == [
            ("task", "existing-worker")
        ]
        services.llm.run_tools.assert_awaited_once()
    finally:
        await db.close()
