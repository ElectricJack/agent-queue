"""Default control-plane cutover from legacy triage assignment."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.commands.handler import CommandHandler
from src.config import AppConfig, DiscordConfig
from src.database import Database
from src.models import AgentProfile, Project, TaskStatus
from src.orchestrator import Orchestrator
from src.playbooks.pipeline_compiler import compile_pipeline
from src.vault import ensure_default_playbooks


PID = "e2e-proj"


@pytest.fixture
async def wired(tmp_path):
    data_dir = str(tmp_path / "data")
    ensure_default_playbooks(data_dir)
    db = Database(str(tmp_path / "e2e.db"))
    await db.initialize()
    await db.create_profile(
        AgentProfile(id="coder", name="Coder", harness="claude", needs_workspace=False)
    )
    await db.create_project(Project(id=PID, name="E2E", default_profile_id="coder"))
    config = AppConfig(
        discord=DiscordConfig(bot_token="t", guild_id="1"),
        workspace_dir=str(tmp_path / "w"),
        database_path=str(tmp_path / "e2e.db"),
        data_dir=data_dir,
    )
    orch = Orchestrator(config)
    orch.db = db
    orch.git = MagicMock()
    handler = CommandHandler(orch, config)
    orch.set_command_handler(handler)
    pipeline = compile_pipeline(
        (
            Path(data_dir)
            / "vault"
            / "system"
            / "playbooks"
            / "default-pipeline.md"
        ).read_text(encoding="utf-8")
    ).playbook
    assert pipeline is not None
    yield db, handler, pipeline
    await db.close()


async def test_task_creation_no_longer_creates_legacy_assignment_work(wired):
    db, handler, pipeline = wired

    result = await handler.execute(
        "create_task",
        {"project_id": PID, "title": "Do a thing", "description": "..."},
    )
    task_id = result["created"]
    task = await db.get_task(task_id)

    assert "task.created" not in pipeline.pipeline_rules
    assert task.status == TaskStatus.READY
    assert task.is_blocked is False
    assert task.profile_id is None
    assert task.intelligence_class is None
    assert await db.get_gates_for_task(task_id) == []
    assert await db.find_task_by_dedup_key(PID, "triage-open") is None


async def test_unrelated_default_pipeline_rules_remain_compiled(wired):
    _db, _handler, pipeline = wired

    assert set(pipeline.pipeline_rules) == {
        "task.completed",
        "spec.approved",
        "proposal.ready",
        "gate.resolved",
    }
