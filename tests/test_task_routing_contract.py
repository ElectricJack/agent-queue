"""Explicit routing survives every task-creation boundary before scheduling."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from src.api.codegen import _make_input_model
from src.api.models.task import CreateTaskResponse, GetTaskResponse, ListTasksResponse
from src.api.models.agent import GetProfileResponse, ListProfilesResponse
from src.commands.handler import CommandHandler
from src.config import AppConfig
from src.database import Database
from src.models import Agent, AgentProfile, Project, Task, TaskStatus
from src.orchestrator import Orchestrator
from src.task_graph import parse_graph
from src.tools.definitions import _ALL_TOOL_DEFINITIONS
from src.vault import ensure_default_intelligence_classes


@pytest.fixture
async def setup(tmp_path):
    db = Database(str(tmp_path / "routing.db"))
    await db.initialize()
    await db.create_project(Project(id="p", name="Project"))
    await db.create_profile(AgentProfile(
        id="coder", name="Coder", harness="codex", model="gpt-5.6-sol",
        default_class="standard-medium", needs_workspace=False,
    ))
    data_dir = str(tmp_path / "data")
    ensure_default_intelligence_classes(data_dir)
    config = AppConfig(data_dir=data_dir, database_path=str(tmp_path / "routing.db"))
    orch = Orchestrator(config)
    orch.db = db
    orch.git = MagicMock()
    orch._emit_notify = AsyncMock()
    handler = CommandHandler(orch, config)
    yield handler, db
    await db.close()


def request_model(command):
    schema = next(t["input_schema"] for t in _ALL_TOOL_DEFINITIONS if t["name"] == command)
    return _make_input_model(command, schema)


async def test_unrouted_create_preserves_existing_policy_without_pipeline(setup):
    handler, db = setup
    result = await handler._cmd_create_task({"project_id": "p", "title": "Legacy ready work"})
    assert "error" not in result
    persisted = await db.get_task(result["created"])
    assert persisted.status == TaskStatus.READY
    assert not persisted.is_blocked
    assert await db.get_gates_for_task(persisted.id) == []


async def test_creation_pipeline_gate_is_present_before_return(setup):
    handler, db = setup

    async def attach_gate(event, task, **_extras):
        if event == "task.created":
            await db.create_gate("p", "routing", "Choose worker", waiter_task_ids=[task.id])

    handler.orchestrator._emit_task_event = AsyncMock(side_effect=attach_gate)
    result = await handler._cmd_create_task({"project_id": "p", "title": "Needs routing"})
    assert "error" not in result
    persisted = await db.get_task(result["created"])
    assert persisted.status == TaskStatus.READY
    assert persisted.is_blocked
    assert await db.get_ready_frontier("p") == []


async def test_routed_creation_preserves_class_through_typed_api_and_reads(setup):
    handler, db = setup
    profiles = ListProfilesResponse(**await handler._cmd_list_profiles({}))
    assert profiles.profiles[0].harness == "codex"
    profile = GetProfileResponse(**await handler._cmd_get_profile({"profile_id": "coder"}))
    assert profile.harness == "codex"
    body = request_model("create_task")(
        project_id="p", title="Use Sol", profile_id="coder", intelligence_class="deep-high",
    )
    result = await handler._cmd_create_task(body.model_dump(exclude_none=True))
    assert "error" not in result
    task = await db.get_task(result["created"])
    assert task.intelligence_class == "deep-high"
    assert not task.is_blocked
    assert await db.get_gates_for_task(task.id) == []
    assert CreateTaskResponse(**result).intelligence_class == "deep-high"
    detail = await handler._cmd_get_task({"task_id": task.id})
    assert GetTaskResponse(**detail).intelligence_class == "deep-high"
    listed = await handler._cmd_list_tasks({"project_id": "p"})
    assert ListTasksResponse(**listed).tasks[0].intelligence_class == "deep-high"


async def test_unknown_creation_class_rejected_without_task(setup):
    handler, db = setup
    result = await handler._cmd_create_task({
        "project_id": "p", "title": "No silent fallback", "profile_id": "coder",
        "intelligence_class": "missing-class",
    })
    assert "error" in result
    assert await db.list_tasks(project_id="p") == []


async def test_routed_child_keeps_creation_class(setup):
    handler, db = setup
    await db.create_task(Task(id="parent", project_id="p", title="Parent", description="",
                              status=TaskStatus.IN_PROGRESS))
    result = await handler._cmd_create_task({
        "project_id": "p", "title": "Child", "parent_id": "parent",
        "profile_id": "coder", "intelligence_class": "deep-high",
    })
    assert "error" not in result
    task = await db.get_task(result["created"])
    assert task.intelligence_class == "deep-high"
    assert task.profile_id == "coder"


async def test_graph_routes_commit_without_changing_unrouted_policy(setup):
    handler, db = setup
    result = await handler._cmd_create_task_graph({
        "project_id": "p",
        "graph": {"nodes": [
            {"key": "routed", "title": "Sol work", "profile": "coder",
             "intelligence_class": "deep-high", "acceptance": ["Done"]},
            {"key": "unrouted", "title": "Needs routing", "acceptance": ["Done"]},
        ]},
    })
    assert "error" not in result
    routed, unrouted = [await db.get_task(tid) for tid in result["task_ids"]]
    assert routed.intelligence_class == "deep-high"
    assert await db.get_gates_for_task(routed.id) == []
    assert unrouted.profile_id is None
    assert await db.get_gates_for_task(unrouted.id) == []


async def test_graph_rejects_unknown_class_before_any_write(setup):
    handler, db = setup
    result = await handler._cmd_create_task_graph({
        "project_id": "p", "graph": {"defaults": {"profile": "coder"}, "nodes": [
            {"key": "bad", "title": "Bad route", "intelligence_class": "missing-class",
             "acceptance": ["Done"]},
        ]},
    })
    assert "error" in result
    assert await db.list_tasks(project_id="p") == []


async def test_graph_command_routing_defaults_apply_only_when_missing(setup):
    handler, db = setup
    result = await handler._cmd_create_task_graph({
        "project_id": "p", "profile_id": "coder", "intelligence_class": "deep-high",
        "graph": {"nodes": [
            {"key": "default", "title": "Sol work", "acceptance": ["Done"]},
            {"key": "explicit", "title": "Different effort", "profile": "coder",
             "intelligence_class": "deep-low", "acceptance": ["Done"]},
        ]},
    })
    assert "error" not in result
    first, second = [await db.get_task(tid) for tid in result["task_ids"]]
    assert (first.profile_id, first.intelligence_class) == ("coder", "deep-high")
    assert second.intelligence_class == "deep-low"


def test_graph_class_defaults_are_preserved():
    graph = parse_graph({"defaults": {"intelligence_class": "deep-high"}, "nodes": [
        {"key": "a", "title": "A"},
        {"key": "b", "title": "B", "intelligence_class": "deep-low"},
    ]})
    assert [node.intelligence_class for node in graph.nodes] == ["deep-high", "deep-low"]
    assert graph.nodes[0].to_dict()["intelligence_class"] == "deep-high"


async def test_route_omission_keeps_existing_class(setup):
    handler, db = setup
    await db.create_task(Task(id="t", project_id="p", title="T", description="",
                              intelligence_class="deep-high"))
    result = await handler._cmd_task_route({"task_id": "t", "profile_id": "coder"})
    assert result["success"]
    assert (await db.get_task("t")).intelligence_class == "deep-high"


@pytest.mark.parametrize("status,assigned", [(TaskStatus.IN_PROGRESS, False),
                                             (TaskStatus.READY, True)])
async def test_route_rejects_running_or_claimed_task(setup, status, assigned):
    handler, db = setup
    await db.create_agent(Agent(id="held", name="Held", profile_id="coder"))
    await db.create_task(Task(id="t", project_id="p", title="T", description="", status=status,
                              assigned_agent_id="held" if assigned else None))
    result = await handler._cmd_task_route({
        "task_id": "t", "profile_id": "coder", "intelligence_class": "deep-high",
    })
    assert result["success"] is False
    assert "stop" in result["error"].lower()
    assert (await db.get_task("t")).profile_id is None


@pytest.mark.parametrize("graph_flag", ["--graph", "--from-spec"])
def test_cli_graph_preserves_routing_flags(graph_flag):
    from src.cli.app import cli
    import src.cli.tasks  # noqa: F401

    with patch("src.cli.tasks._create_task_graph") as dispatch:
        result = CliRunner().invoke(cli, [
            "task", "create", "--project", "p", graph_flag, "specs/work.md",
            "--profile", "coder", "--intelligence-class", "deep-high",
        ])
    assert result.exit_code == 0, result.output
    assert dispatch.call_args.kwargs["profile_id"] == "coder"
    assert dispatch.call_args.kwargs["intelligence_class"] == "deep-high"


async def test_routing_update_rechecks_claim_after_command_read(setup):
    handler, db = setup
    await db.create_agent(Agent(id="held", name="Held", profile_id="coder"))
    await db.create_task(Task(id="t", project_id="p", title="T", description=""))
    update = db.update_task_routing

    async def claim_first(*args, **kwargs):
        await db.update_task("t", status=TaskStatus.IN_PROGRESS, assigned_agent_id="held")
        return await update(*args, **kwargs)

    with patch.object(db, "update_task_routing", side_effect=claim_first):
        result = await handler._cmd_task_route({"task_id": "t", "profile_id": "coder"})
    assert result["success"] is False
    assert (await db.get_task("t")).profile_id is None


async def test_edit_class_validates_and_persists(setup):
    handler, db = setup
    await db.create_task(Task(id="t", project_id="p", title="T", description="", profile_id="coder"))
    result = await handler._cmd_edit_task({"task_id": "t", "intelligence_class": "deep-high"})
    assert "error" not in result
    assert (await db.get_task("t")).intelligence_class == "deep-high"
    invalid = await handler._cmd_edit_task({"task_id": "t", "intelligence_class": "missing-class"})
    assert "error" in invalid
    assert (await db.get_task("t")).intelligence_class == "deep-high"


async def test_edit_profile_cannot_retarget_running_task(setup):
    handler, db = setup
    await db.create_task(Task(id="t", project_id="p", title="T", description="", status=TaskStatus.IN_PROGRESS))
    result = await handler._cmd_edit_task({"task_id": "t", "profile_id": "coder"})
    assert "error" in result
    assert "stop" in result["error"].lower()
    assert (await db.get_task("t")).profile_id is None


async def test_edit_routing_checks_claim_at_write_time(setup):
    handler, db = setup
    await db.create_agent(Agent(id="held", name="Held", profile_id="coder"))
    await db.create_task(Task(id="t", project_id="p", title="T", description=""))
    update = db.update_task_routing

    async def claim_first(*args, **kwargs):
        await db.update_task("t", status=TaskStatus.IN_PROGRESS, assigned_agent_id="held")
        return await update(*args, **kwargs)

    with patch.object(db, "update_task_routing", side_effect=claim_first):
        result = await handler._cmd_edit_task({"task_id": "t", "profile_id": "coder", "intelligence_class": "deep-high"})
    assert "error" in result
    assert (await db.get_task("t")).profile_id is None


@pytest.mark.parametrize("field", ["model", "provider", "harness", "agent_id", "affinity_agent_id"])
def test_graph_does_not_silently_drop_unsupported_routing(field):
    from src.task_graph import GraphParseError
    with pytest.raises(GraphParseError, match="not supported"):
        parse_graph({"nodes": [{"key": "a", "title": "A", field: "requested"}]})


def test_cli_single_task_preserves_class():
    from src.cli.app import cli
    import src.cli.tasks  # noqa: F401

    captured = []
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock()

    async def execute(command, args):
        captured.append((command, args))
        return {"created": "t", "title": args["title"]}

    client.execute = AsyncMock(side_effect=execute)
    with patch("src.cli.tasks._get_client", return_value=client):
        result = CliRunner().invoke(cli, [
            "task", "create", "--project", "p", "--title", "Sol work", "--description", "Do work",
            "--profile", "coder", "--intelligence-class", "deep-high",
        ])
    assert result.exit_code == 0, result.output
    assert captured[0][1]["intelligence_class"] == "deep-high"


@pytest.mark.parametrize("command", ["_cmd_task_route", "_cmd_edit_task"])
async def test_routing_stopped_task_does_not_restart_it(setup, command):
    handler, db = setup
    await db.create_task(Task(id="t", project_id="p", title="Stopped", description="",
                              status=TaskStatus.BLOCKED))
    result = await getattr(handler, command)({
        "task_id": "t", "profile_id": "coder", "intelligence_class": "deep-high",
    })
    assert "error" not in result
    task = await db.get_task("t")
    assert task.status == TaskStatus.BLOCKED
    assert task.intelligence_class == "deep-high"


async def test_routing_rejects_active_session_even_without_legacy_agent_assignment(setup):
    from src.models import SessionRecord
    handler, db = setup
    await db.create_task(Task(id="t", project_id="p", title="T", description=""))
    await db.create_session(SessionRecord(
        id="s", project_id="p", profile_id="coder", harness="codex", provider="openai",
        name="session", lifecycle="task", work_dir="/tmp/test", epoch="test",
        instance_token="test", started_at=1, task_id="t", state="starting",
    ))
    result = await handler._cmd_task_route({"task_id": "t", "profile_id": "coder"})
    assert result["success"] is False
    assert (await db.get_task("t")).profile_id is None


@pytest.mark.parametrize("field", ["profile_id", "intelligence_class"])
async def test_typed_edit_preserves_explicit_routing_null_only(setup, field):
    from src.api.codegen import _make_route_handler
    handler, db = setup
    await db.create_task(Task(id="t", project_id="p", title="T", description="",
                              profile_id="coder", intelligence_class="deep-high"))
    model = request_model("edit_task")
    typed_edit = _make_route_handler("edit_task", model)
    result = await typed_edit(model(task_id="t", **{field: None}), ch=handler)
    assert isinstance(result, dict) and result.get("updated") == "t"
    task = await db.get_task("t")
    assert getattr(task, field) is None
    other = "profile_id" if field == "intelligence_class" else "intelligence_class"
    assert getattr(task, other) == ("coder" if other == "profile_id" else "deep-high")


async def test_typed_edit_omitted_routing_fields_do_not_clear(setup):
    from src.api.codegen import _make_route_handler
    handler, db = setup
    await db.create_task(Task(id="t", project_id="p", title="T", description="",
                              profile_id="coder", intelligence_class="deep-high"))
    model = request_model("edit_task")
    result = await _make_route_handler("edit_task", model)(model(task_id="t", title="Renamed"), ch=handler)
    assert result["updated"] == "t"
    task = await db.get_task("t")
    assert (task.profile_id, task.intelligence_class) == ("coder", "deep-high")
