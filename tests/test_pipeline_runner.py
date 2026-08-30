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


# ---------------------------------------------------------------------------
# for_each tests
# ---------------------------------------------------------------------------

def _make_foreach_graph(source: str) -> dict:
    """Graph with a for_each node that writes items to output and then exits."""
    return {
        "id": "fe",
        "version": 1,
        "kind": "pipeline",
        "role": "default-pipeline",
        "nodes": {
            "loop": {
                "entry": True,
                "kind": "action",
                "command": "do_item",
                "for_each": {"source": source, "as": "item"},
                "args": {"id": "{{outputs.item}}"},
                "on_success": "done",
                "on_failure": "done",
            },
            "done": {"terminal": True},
        },
    }


async def test_for_each_calls_handler_per_item():
    """for_each over a list output calls handler once per item with loop var substituted."""
    graph = {
        "id": "fe2",
        "version": 1,
        "kind": "pipeline",
        "role": "default-pipeline",
        "nodes": {
            "seed": {
                "entry": True,
                "kind": "action",
                "command": "get_tasks",
                "args": {},
                "output": {"as": "x"},
                "on_success": "loop",
            },
            "loop": {
                "kind": "action",
                "command": "do_item",
                "for_each": {"source": "outputs.x.tasks", "as": "item"},
                "args": {"id": "{{outputs.item}}"},
                "on_success": "done",
            },
            "done": {"terminal": True},
        },
    }

    h = MagicMock()

    async def fake(name, args):
        if name == "get_tasks":
            return {"success": True, "tasks": ["t1", "t2", "t3"]}
        return {"success": True}

    h.execute = AsyncMock(side_effect=fake)
    r = PipelineRunner(graph, event={}, handler=h)
    result = await r.run()
    assert result.status == "completed"
    do_calls = [c for c in h.execute.await_args_list if c.args[0] == "do_item"]
    assert len(do_calls) == 3
    ids = [c.args[1]["id"] for c in do_calls]
    assert ids == ["t1", "t2", "t3"]


async def test_for_each_empty_list_routes_on_success():
    """for_each with an empty list produces zero iterations and routes to on_success."""
    graph = _make_foreach_graph("event.items")
    h = MagicMock()
    h.execute = AsyncMock(return_value={"success": True})
    r = PipelineRunner(graph, event={"items": []}, handler=h)
    result = await r.run()
    assert result.status == "completed"
    # do_item must never have been called
    assert h.execute.await_count == 0


async def test_for_each_non_list_source_fails():
    """for_each whose source resolves to a non-list causes the run to fail."""
    graph = _make_foreach_graph("event.items")
    h = MagicMock()
    h.execute = AsyncMock(return_value={"success": True})
    r = PipelineRunner(graph, event={"items": "not-a-list"}, handler=h)
    result = await r.run()
    assert result.status == "failed"
    assert "not a list" in result.error


async def test_handler_exception_ends_run_failed():
    """A handler that raises an exception causes the run to end as failed without crash."""
    graph = {
        "id": "ex",
        "version": 1,
        "kind": "pipeline",
        "role": "default-pipeline",
        "nodes": {
            "a": {
                "entry": True,
                "kind": "action",
                "command": "boom",
                "args": {},
                "on_success": "done",
            },
            "done": {"terminal": True},
        },
    }
    h = MagicMock()
    h.execute = AsyncMock(side_effect=RuntimeError("kaboom"))
    r = PipelineRunner(graph, event={}, handler=h)
    result = await r.run()
    assert result.status == "failed"
    assert "kaboom" in result.error


async def test_nested_event_path_substitution():
    """{{event.a.b}} resolves through nested dicts."""
    graph = {
        "id": "nest",
        "version": 1,
        "kind": "pipeline",
        "role": "default-pipeline",
        "nodes": {
            "a": {
                "entry": True,
                "kind": "action",
                "command": "cmd",
                "args": {"val": "{{event.a.b}}"},
                "on_success": "done",
            },
            "done": {"terminal": True},
        },
    }
    captured = {}
    h = MagicMock()

    async def fake(name, args):
        captured.update(args)
        return {"success": True}

    h.execute = AsyncMock(side_effect=fake)
    r = PipelineRunner(graph, event={"a": {"b": "deep_value"}}, handler=h)
    await r.run()
    assert captured["val"] == "deep_value"


async def test_whole_value_reference_preserves_type():
    """A whole-string {{...}} placeholder returns the raw value, not a string."""
    graph = {
        "id": "wv",
        "version": 1,
        "kind": "pipeline",
        "role": "default-pipeline",
        "nodes": {
            "a": {
                "entry": True,
                "kind": "action",
                "command": "cmd",
                "args": {"items": "{{event.list}}", "meta": "{{event.meta}}"},
                "on_success": "done",
            },
            "done": {"terminal": True},
        },
    }
    captured = {}
    h = MagicMock()

    async def fake(name, args):
        captured.update(args)
        return {"success": True}

    h.execute = AsyncMock(side_effect=fake)
    event = {"list": [1, 2, 3], "meta": {"key": "v"}}
    r = PipelineRunner(graph, event=event, handler=h)
    await r.run()
    assert captured["items"] == [1, 2, 3]
    assert captured["meta"] == {"key": "v"}


async def test_result_without_success_key_routes_on_success():
    """A result dict without a 'success' key is treated as success (fix 1)."""
    graph = {
        "id": "nsk",
        "version": 1,
        "kind": "pipeline",
        "role": "default-pipeline",
        "nodes": {
            "a": {
                "entry": True,
                "kind": "action",
                "command": "cmd",
                "args": {},
                "on_success": "done",
                "on_failure": "fail_node",
            },
            "done": {"terminal": True},
            "fail_node": {"terminal": True, "kind": "failure"},
        },
    }
    h = MagicMock()
    # Result has no "success" key at all
    h.execute = AsyncMock(return_value={"task_id": "t-99"})
    r = PipelineRunner(graph, event={}, handler=h)
    result = await r.run()
    assert result.status == "completed"
