import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.playbooks.pipeline_runner import PipelineRunner


@pytest.fixture
def handler():
    h = MagicMock()
    h.execute = AsyncMock(side_effect=lambda name, args: {"success": True, "task_id": f"t-{name}"})
    return h


@pytest.fixture
def graph():
    return {
        "id": "pl",
        "version": 1,
        "kind": "pipeline",
        "role": "default-pipeline",
        "nodes": {
            "a": {
                "entry": True,
                "kind": "action",
                "command": "gate_create",
                "args": {"project_id": "{{event.project_id}}", "gate_type": "routing", "title": "x"},
                "on_success": "b",
            },
            "b": {
                "kind": "action",
                "command": "ensure_task",
                "args": {"project_id": "{{event.project_id}}", "dedup_key": "triage-open", "title": "t"},
                "output": {"as": "triage"},
                "on_success": "done",
            },
            "done": {"terminal": True},
        },
    }


async def test_walks_success_chain(handler, graph):
    r = PipelineRunner(graph, event={"project_id": "P1", "task_id": "T1"}, handler=handler)
    result = await r.run()
    assert result.status == "completed"
    calls = [c.args for c in handler.execute.await_args_list]
    assert calls[0][0] == "gate_create"
    assert calls[0][1]["project_id"] == "P1"
    assert calls[1][0] == "ensure_task"


async def test_takes_on_failure_branch(graph):
    h = MagicMock()
    h.execute = AsyncMock(return_value={"success": False, "error": "boom"})
    graph["nodes"]["a"]["on_failure"] = "done"
    r = PipelineRunner(graph, event={"project_id": "P", "task_id": "T"}, handler=h)
    result = await r.run()
    assert result.status == "completed"
    assert h.execute.await_count == 1  # 'b' never called


async def test_output_reference_in_next_node(graph):
    h = MagicMock()
    async def fake(name, args):
        if name == "ensure_task":
            return {"success": True, "task_id": "t-42"}
        return {"success": True, "used": args.get("depends_on")}
    h.execute = AsyncMock(side_effect=fake)
    graph["nodes"]["done"] = {
        "kind": "action",
        "command": "add_dependency",
        "args": {"task_id": "downstream", "depends_on": "{{outputs.triage.task_id}}"},
        "on_success": "end",
    }
    graph["nodes"]["end"] = {"terminal": True}
    r = PipelineRunner(graph, event={"project_id": "P", "task_id": "T"}, handler=h)
    await r.run()
    # third call is add_dependency; its depends_on came from ensure_task's task_id.
    third = h.execute.await_args_list[2]
    assert third.args[0] == "add_dependency"
    assert third.args[1]["depends_on"] == "t-42"


async def test_missing_target_fails(graph):
    graph["nodes"]["a"]["on_success"] = "does-not-exist"
    h = MagicMock()
    h.execute = AsyncMock(return_value={"success": True})
    r = PipelineRunner(graph, event={"project_id": "P", "task_id": "T"}, handler=h)
    result = await r.run()
    assert result.status == "failed"
