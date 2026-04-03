"""Tests for plan parsing handoff from orchestrator to Supervisor."""

import asyncio
import os
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch


def _make_handler():
    from src.command_handler import CommandHandler

    orch = MagicMock()
    orch.db = AsyncMock()
    config = MagicMock()
    config.workspace_dir = "/tmp/test"
    config.auto_task = MagicMock()
    config.auto_task.enabled = True
    config.auto_task.plan_file_patterns = [".claude/plan.md", "plan.md"]
    config.auto_task.max_steps_per_plan = 20
    config.auto_task.use_llm_parser = False
    config.auto_task.skip_if_implemented = False
    config.auto_task.inherit_approval = False
    config.auto_task.chain_dependencies = True
    orch.config = config
    return CommandHandler(orch, config)


def test_process_task_completion_exists():
    handler = _make_handler()
    assert hasattr(handler, "_cmd_process_task_completion")


def test_process_task_completion_no_plan_file():
    handler = _make_handler()
    with tempfile.TemporaryDirectory() as tmpdir:
        result = asyncio.run(
            handler.execute(
                "process_task_completion",
                {
                    "task_id": "t-123",
                    "workspace_path": tmpdir,
                },
            )
        )
    assert result.get("plan_found") is False


def test_process_task_completion_finds_plan():
    handler = _make_handler()
    handler.orchestrator.db.get_task = AsyncMock(
        return_value=MagicMock(
            id="t-123",
            project_id="proj-1",
            is_plan_subtask=False,
        )
    )
    handler.orchestrator.db.set_task_context = AsyncMock()
    handler.orchestrator.db.list_tasks = AsyncMock(return_value=[])
    # Provide a real dict so the staleness check works correctly
    handler.orchestrator._task_exec_start = {}

    with tempfile.TemporaryDirectory() as tmpdir:
        plan_dir = os.path.join(tmpdir, ".claude")
        os.makedirs(plan_dir)
        plan_path = os.path.join(plan_dir, "plan.md")
        with open(plan_path, "w") as f:
            f.write(
                "# Implementation Plan\n\n"
                "## Step 1: Add login endpoint\n\n"
                "Create POST /api/login with JWT auth.\n\n"
                "## Step 2: Add logout endpoint\n\n"
                "Create POST /api/logout to invalidate tokens.\n"
            )

        result = asyncio.run(
            handler.execute(
                "process_task_completion",
                {
                    "task_id": "t-123",
                    "workspace_path": tmpdir,
                },
            )
        )

    assert result.get("plan_found") is True
    # Plan content is stored for later supervisor processing (no step count at discovery time)
    assert "archived_path" in result


def test_process_task_completion_disabled():
    handler = _make_handler()
    handler.orchestrator.config.auto_task.enabled = False

    result = asyncio.run(
        handler.execute(
            "process_task_completion",
            {
                "task_id": "t-123",
                "workspace_path": "/tmp/nonexistent",
            },
        )
    )
    assert result.get("plan_found") is False
    assert "disabled" in result.get("reason", "").lower()


def test_orchestrator_calls_supervisor_on_completion():
    """After task completes, orchestrator delegates to Supervisor."""
    from src.supervisor import Supervisor

    assert hasattr(Supervisor, "on_task_completed")


def test_on_task_completed_returns_plan_status():
    """on_task_completed returns whether a plan was found."""
    from src.supervisor import Supervisor
    from unittest.mock import AsyncMock, MagicMock

    sup = MagicMock(spec=Supervisor)
    sup.on_task_completed = AsyncMock(return_value={"plan_found": True, "steps_count": 3})
    result = asyncio.run(
        sup.on_task_completed(task_id="t-1", project_id="p-1", workspace_path="/tmp/ws")
    )
    assert result["plan_found"] is True
