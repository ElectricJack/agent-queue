"""``advance_workflow_stage`` — the stage-history write path.

This command had no functional test at all, which is how
``stages=json.dumps(stages)`` survived with no ``import json`` in
``src/commands/workflow_commands.py``: every call raised ``NameError:
name 'json' is not defined`` at the point of the update, after the
workflow row had already been read.  ``ruff``'s F821 is what surfaced it.

``test_advance_stage_serialises_history`` is the regression guard — it
fails with a ``NameError`` on the unfixed module.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.commands.handler import CommandHandler
from src.config import AppConfig, DiscordConfig
from src.database import Database
from src.models import PlaybookRun, Project
from src.orchestrator import Orchestrator

PROJECT_ID = "proj"
WORKFLOW_ID = "wf-1"


@pytest.fixture
async def handler(tmp_path):
    db = Database(str(tmp_path / "wf.db"))
    await db.initialize()
    await db.create_project(Project(id=PROJECT_ID, name="p"))
    # workflows.playbook_run_id is a FK to playbook_runs.run_id.
    await db.create_playbook_run(
        PlaybookRun(
            run_id="run-1",
            playbook_id="pb",
            playbook_version=1,
            trigger_event="{}",
            status="running",
            started_at=1.0,
        )
    )
    cfg = AppConfig(
        discord=DiscordConfig(bot_token="t", guild_id="1"),
        workspace_dir=str(tmp_path / "ws"),
        database_path=str(tmp_path / "wf.db"),
        data_dir=str(tmp_path / "data"),
    )
    orch = Orchestrator(cfg)
    orch.db = db
    orch.git = MagicMock()
    yield CommandHandler(orch, cfg)
    await db.close()


async def _make_workflow(handler, current_stage="plan"):
    res = await handler._cmd_create_workflow(
        {
            "workflow_id": WORKFLOW_ID,
            "playbook_id": "pb",
            "playbook_run_id": "run-1",
            "project_id": PROJECT_ID,
            "current_stage": current_stage,
        }
    )
    assert res.get("success"), res
    return res


async def test_advance_stage_serialises_history(handler):
    """The regression guard: this raised NameError before `import json`."""
    await _make_workflow(handler)

    res = await handler._cmd_advance_workflow_stage(
        {"workflow_id": WORKFLOW_ID, "stage_name": "build", "task_ids": ["t1", "t2"]}
    )

    assert res["success"] is True
    assert res["previous_stage"] == "plan"
    assert res["current_stage"] == "build"
    assert res["new_task_ids"] == ["t1", "t2"]

    # The history really was written, not just reported.
    workflow = await handler.db.get_workflow(WORKFLOW_ID)
    assert workflow.current_stage == "build"
    names = [s["name"] for s in workflow.stages]
    assert names[-1] == "build"
    assert workflow.stages[-1]["status"] == "active"
    assert workflow.task_ids == ["t1", "t2"]


async def test_advance_stage_closes_out_the_previous_stage(handler):
    await _make_workflow(handler)
    await handler._cmd_advance_workflow_stage(
        {"workflow_id": WORKFLOW_ID, "stage_name": "plan", "task_ids": []}
    )

    await handler._cmd_advance_workflow_stage(
        {"workflow_id": WORKFLOW_ID, "stage_name": "build"}
    )

    workflow = await handler.db.get_workflow(WORKFLOW_ID)
    plan = next(s for s in workflow.stages if s["name"] == "plan")
    assert plan["status"] == "completed"
    assert plan["completed_at"] is not None


async def test_advance_stage_accepts_comma_separated_task_ids(handler):
    await _make_workflow(handler)

    res = await handler._cmd_advance_workflow_stage(
        {"workflow_id": WORKFLOW_ID, "stage_name": "build", "task_ids": "t1, t2 ,t3"}
    )

    assert res["new_task_ids"] == ["t1", "t2", "t3"]


@pytest.mark.parametrize(
    "args,expected",
    [
        ({}, "workflow_id is required"),
        ({"workflow_id": WORKFLOW_ID}, "stage_name is required"),
        ({"workflow_id": "nope", "stage_name": "x"}, "Workflow 'nope' not found"),
    ],
)
async def test_advance_stage_validation(handler, args, expected):
    await _make_workflow(handler)
    res = await handler._cmd_advance_workflow_stage(args)
    assert res["error"] == expected
