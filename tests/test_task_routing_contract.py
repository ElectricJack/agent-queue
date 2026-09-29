"""Routing survives every task-creation boundary before scheduling.

Filing carries hints, never routes (mandatory-routing spec 2026-09-28 §5.1): a
profile named at any filing surface is refused with ``routing.choice_forbidden``,
and the filer's class travels as ``class_hint`` until the router (or the
manual ``task_route``) writes the route.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from src.api.codegen import _make_input_model
from src.api.models.task import CreateTaskResponse, GetTaskResponse, ListTasksResponse
from src.api.models.agent import GetProfileResponse, ListProfilesResponse
from src.commands.handler import CommandHandler
from src.config import DatabaseConfig, AppConfig
from src.database import Database
from src.models import Agent, AgentProfile, Project, Task, TaskStatus
from src.orchestrator import Orchestrator
from src.task_graph import parse_graph
from src.tools.definitions import _ALL_TOOL_DEFINITIONS
from src.vault import ensure_default_intelligence_classes
from tests.db_fixtures import lease_dsn


@pytest.fixture
async def setup(tmp_path):
    db = Database(lease_dsn("routing.db"))
    await db.initialize()
    await db.create_project(Project(id="p", name="Project"))
    await db.create_profile(AgentProfile(
        id="coder", name="Coder", harness="codex", model="gpt-5.6-sol",
        default_class="standard-medium", needs_workspace=False,
    ))
    data_dir = str(tmp_path / "data")
    ensure_default_intelligence_classes(data_dir)
    config = AppConfig(data_dir=data_dir, database=DatabaseConfig(url=lease_dsn("routing.db")))
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


def test_create_root_survives_typed_api_request_model():
    body = request_model("create_task")(title="Cross-cutting work", root=True)
    assert body.model_dump(exclude_none=True)["root"] is True


async def test_unrouted_create_preserves_existing_policy_without_pipeline(setup):
    handler, db = setup
    result = await handler._cmd_create_task({"project_id": "p", "title": "Legacy ready work"})
    assert "error" not in result
    persisted = await db.get_task(result["created"])
    assert persisted.status == TaskStatus.READY
    assert not persisted.is_blocked
    assert await db.get_gates_for_task(persisted.id) == []


@pytest.mark.parametrize("profile_id", [None, "reviewer"])
async def test_agent_task_files_hints_or_a_role_through_real_dispatch(setup, monkeypatch, profile_id):
    from src.commands.contracts import builtin
    from src.commands.contracts.registry import CONTRACTS
    from tests.test_agent_task_executor import agent_task_step, context, parent_principal, run

    handler, db = setup
    caps = {"harness_tools": [], "aq_commands": ["create_task"], "plugin_tools": []}
    await db.update_profile("coder", **caps)
    for name in ("parent", "reviewer"):
        await db.create_profile(AgentProfile(
            id=name, name=name, harness="codex", default_class="standard-high",
            needs_workspace=False, **caps,
        ))
    monkeypatch.setattr(builtin, "_handler_provider", lambda: handler)
    responses = []
    execute = handler.execute

    async def capture(*args, **kwargs):
        response = await execute(*args, **kwargs)
        responses.append(response)
        return response

    monkeypatch.setattr(handler, "execute", capture)
    step = agent_task_step(
        profile_id=profile_id, intelligence_class="deep-high", task_type="design",
        wait_for_completion=False,
    )
    result = await run(step, context(
        CONTRACTS, principal=parent_principal(profile_id="parent", aq_commands={"create_task"}),
        db=db,
    ))
    assert result.outcome == "dispatched", (result.diagnostics, responses)
    task = await db.get_task(result.child_task_id)
    assert task.class_hint == "deep-high"
    assert task.task_type.value == "design"
    assert task.profile_id == profile_id
    if profile_id is None:
        assert task.intelligence_class is None
        assert task.route_source == "unrouted"
        assert task.provider_intent == "class_only"
    else:
        assert task.intelligence_class == "standard-high"
        assert task.route_source == "role"


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


async def test_class_hint_survives_typed_api_and_reads_until_routed(setup):
    handler, db = setup
    profiles = ListProfilesResponse(**await handler._cmd_list_profiles({}))
    assert profiles.profiles[0].harness == "codex"
    profile = GetProfileResponse(**await handler._cmd_get_profile({"profile_id": "coder"}))
    assert profile.harness == "codex"
    model = request_model("create_task")
    # The typed request model no longer offers a route (spec §5.1).
    assert "profile_id" not in model.model_fields
    body = model(project_id="p", title="Use Sol", intelligence_class="deep-high")
    result = await handler._cmd_create_task(body.model_dump(exclude_none=True))
    assert "error" not in result
    task = await db.get_task(result["created"])
    assert (task.profile_id, task.intelligence_class, task.class_hint) == (None, None, "deep-high")
    assert not task.is_blocked
    assert await db.get_gates_for_task(task.id) == []
    created = CreateTaskResponse(**result)
    assert (created.route_source, created.class_hint) == ("unrouted", "deep-high")
    detail = await handler._cmd_get_task({"task_id": task.id})
    assert GetTaskResponse(**detail).class_hint == "deep-high"

    # Once routed, the class reads back through every surface.
    routed = await handler._cmd_task_route(
        {"task_id": task.id, "profile_id": "coder", "intelligence_class": "deep-high"}
    )
    assert routed["success"], routed
    detail = await handler._cmd_get_task({"task_id": task.id})
    assert GetTaskResponse(**detail).intelligence_class == "deep-high"
    listed = await handler._cmd_list_tasks({"project_id": "p"})
    assert ListTasksResponse(**listed).tasks[0].intelligence_class == "deep-high"


async def test_unknown_creation_class_rejected_without_task(setup):
    handler, db = setup
    result = await handler._cmd_create_task({
        "project_id": "p", "title": "No silent fallback", "intelligence_class": "missing-class",
    })
    assert "not found in vault" in result["error"]
    assert await db.list_tasks(project_id="p") == []


async def test_supervisor_omission_is_unrouted_not_the_project_default(setup):
    """The supervisor routes nothing: an omitted profile is left for the
    project's router (mandatory-routing spec §5.2)."""
    handler, db = setup
    await db.create_profile(AgentProfile(
        id="supervisor", name="Supervisor", harness="claude", lifecycle="named",
        needs_workspace=False, harness_tools=[], aq_commands=[], plugin_tools=[],
    ))
    await db.create_profile(AgentProfile(
        id="worker", name="Worker", harness="claude", lifecycle="task",
        needs_workspace=False, harness_tools=[], aq_commands=[], plugin_tools=[],
    ))
    handler._caller_profile_id = "supervisor"
    try:
        result = await handler._cmd_create_task({"project_id": "p", "title": "Delegated"})
    finally:
        handler._caller_profile_id = None
    assert "error" not in result
    task = await db.get_task(result["created"])
    assert (task.profile_id, task.route_source) == (None, "unrouted")


async def test_supervisor_profile_is_rejected_on_creation_edit_route_and_graph(setup):
    handler, db = setup
    await db.create_profile(AgentProfile(
        id="supervisor", name="Supervisor", harness="claude", lifecycle="named",
        needs_workspace=False,
    ))
    # Filing and editing refuse every profile, the supervisor's included.
    created = await handler._cmd_create_task({
        "project_id": "p", "title": "No supervisor", "profile_id": "supervisor",
    })
    assert created["code"] == "routing.choice_forbidden"

    await db.create_task(Task(id="legacy", project_id="p", title="Legacy", description=""))
    edited = await handler._cmd_edit_task({"task_id": "legacy", "profile_id": "supervisor"})
    assert edited["code"] == "routing.choice_forbidden"

    # The manual route still names a profile, and still refuses the supervisor.
    await db.update_task("legacy", intelligence_class="standard-medium")
    routed = await handler._cmd_task_route({"task_id": "legacy", "profile_id": "supervisor"})
    assert "supervisor control-plane" in routed["error"]

    graph = await handler._cmd_create_task_graph({
        "project_id": "p",
        "graph": {"nodes": [{"key": "n", "title": "N", "profile": "supervisor"}]},
    })
    assert graph["code"] == "routing.choice_forbidden"
    assert any(error["rule"] == "routing_choice_forbidden" for error in graph["errors"])
    assert (await db.get_task("legacy")).profile_id is None
    assert [t.id for t in await db.list_tasks(project_id="p")] == ["legacy"]


async def test_a_project_takes_no_default_profile(setup):
    handler, db = setup
    await db.create_profile(AgentProfile(id="supervisor", name="Supervisor", lifecycle="named"))
    # Mandatory routing §5.1, §8: a project has no default profile to set.
    result = await handler.execute(
        "edit_project", {"project_id": "p", "default_profile_id": "supervisor"}
    )
    assert result["code"] == "routing.choice_forbidden"
    assert not hasattr(await db.get_project("p"), "default_profile_id")

    created = await handler._cmd_create_task({"project_id": "p", "title": "No default"})
    assert "error" not in created, created
    task = await db.get_task(created["created"])
    assert (task.profile_id, task.route_source) == (None, "unrouted")


async def test_claim_frontier_skips_legacy_supervisor_route_without_hiding_worker_work(setup):
    _handler, db = setup
    await db.create_profile(AgentProfile(id="supervisor", name="Supervisor", lifecycle="named"))
    await db.create_task(Task(
        id="legacy-supervisor", project_id="p", title="Legacy", description="",
        profile_id="supervisor", route_source="legacy", priority=1, status=TaskStatus.READY,
    ))
    await db.create_task(Task(
        id="normal-worker", project_id="p", title="Normal", description="",
        profile_id="coder", route_source="legacy", priority=2, status=TaskStatus.READY,
    ))
    async with db._engine.begin() as conn:
        selected = await db.select_ready_for_profile(
            conn, project_id="p", profile_id="coder", agent_id="a",
        )
    assert selected == "normal-worker"


async def test_child_keeps_its_creation_class_as_a_hint(setup):
    handler, db = setup
    await db.create_task(Task(id="parent", project_id="p", title="Parent", description="",
                              status=TaskStatus.IN_PROGRESS))
    refused = await handler._cmd_create_task({
        "project_id": "p", "title": "Child", "parent_id": "parent",
        "profile_id": "coder", "intelligence_class": "deep-high",
    })
    assert refused["code"] == "routing.choice_forbidden"
    result = await handler._cmd_create_task({
        "project_id": "p", "title": "Child", "parent_id": "parent",
        "intelligence_class": "deep-high",
    })
    assert "error" not in result
    task = await db.get_task(result["created"])
    assert (task.class_hint, task.profile_id, task.route_source) == ("deep-high", None, "unrouted")


async def test_graph_hints_commit_without_changing_unrouted_policy(setup):
    handler, db = setup
    refused = await handler._cmd_create_task_graph({
        "project_id": "p",
        "graph": {"nodes": [
            {"key": "routed", "title": "Sol work", "profile": "coder",
             "intelligence_class": "deep-high", "acceptance": ["Done"]},
        ]},
    })
    assert refused["code"] == "routing.choice_forbidden"
    assert await db.list_tasks(project_id="p") == []

    result = await handler._cmd_create_task_graph({
        "project_id": "p",
        "graph": {"nodes": [
            {"key": "hinted", "title": "Sol work", "intelligence_class": "deep-high",
             "acceptance": ["Done"]},
            {"key": "unrouted", "title": "Needs routing", "acceptance": ["Done"]},
        ]},
    })
    assert "error" not in result
    hinted, unrouted = [await db.get_task(tid) for tid in result["task_ids"]]
    assert (hinted.profile_id, hinted.class_hint) == (None, "deep-high")
    assert await db.get_gates_for_task(hinted.id) == []
    assert unrouted.profile_id is None
    assert await db.get_gates_for_task(unrouted.id) == []


async def test_graph_rejects_unknown_class_before_any_write(setup):
    handler, db = setup
    result = await handler._cmd_create_task_graph({
        "project_id": "p", "graph": {"nodes": [
            {"key": "bad", "title": "Bad route", "intelligence_class": "missing-class",
             "acceptance": ["Done"]},
        ]},
    })
    assert [e["rule"] for e in result["errors"]] == ["invalid_intelligence_class"]
    assert await db.list_tasks(project_id="p") == []


async def test_graph_command_class_default_applies_only_when_missing(setup):
    handler, db = setup
    refused = await handler._cmd_create_task_graph({
        "project_id": "p", "profile_id": "coder",
        "graph": {"nodes": [{"key": "default", "title": "Sol work", "acceptance": ["Done"]}]},
    })
    assert refused["code"] == "routing.choice_forbidden"
    result = await handler._cmd_create_task_graph({
        "project_id": "p", "intelligence_class": "deep-high",
        "graph": {"nodes": [
            {"key": "default", "title": "Sol work", "acceptance": ["Done"]},
            {"key": "explicit", "title": "Different effort",
             "intelligence_class": "deep-low", "acceptance": ["Done"]},
        ]},
    })
    assert "error" not in result
    first, second = [await db.get_task(tid) for tid in result["task_ids"]]
    assert (first.profile_id, first.class_hint) == (None, "deep-high")
    assert second.class_hint == "deep-low"


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
def test_cli_graph_preserves_the_class_hint(graph_flag):
    from src.cli.app import cli
    import src.cli.tasks  # noqa: F401

    with patch("src.cli.tasks._create_task_graph") as dispatch:
        result = CliRunner().invoke(cli, [
            "task", "create", "--project", "p", graph_flag, "specs/work.md",
            "--intelligence-class", "deep-high",
        ])
    assert result.exit_code == 0, result.output
    assert "profile_id" not in dispatch.call_args.kwargs
    assert dispatch.call_args.kwargs["intelligence_class"] == "deep-high"

    # ``--profile`` is gone: filing names no route (spec §5.1).
    with patch("src.cli.tasks._create_task_graph") as dispatch:
        result = CliRunner().invoke(cli, [
            "task", "create", "--project", "p", graph_flag, "specs/work.md",
            "--profile", "coder",
        ])
    assert result.exit_code == 2, result.output
    assert "No such option" in result.output
    dispatch.assert_not_called()


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


async def test_edit_class_validates_and_persists_as_a_hint(setup):
    handler, db = setup
    await db.create_task(Task(id="t", project_id="p", title="T", description="",
                              status=TaskStatus.READY, profile_id="coder", route_source="legacy",
                              intelligence_class="standard-medium"))
    result = await handler._cmd_edit_task({"task_id": "t", "intelligence_class": "deep-high"})
    assert "error" not in result
    task = await db.get_task("t")
    # The new hint sends the queued task back to its router (spec §5.1).
    assert (task.class_hint, task.profile_id, task.intelligence_class) == ("deep-high", None, None)
    assert task.route_source == "unrouted"
    invalid = await handler._cmd_edit_task({"task_id": "t", "intelligence_class": "missing-class"})
    assert "error" in invalid
    assert (await db.get_task("t")).class_hint == "deep-high"


async def test_edit_cannot_retarget_running_task(setup):
    handler, db = setup
    await db.create_task(Task(id="t", project_id="p", title="T", description="", status=TaskStatus.IN_PROGRESS))
    result = await handler._cmd_edit_task({"task_id": "t", "profile_id": "coder"})
    assert result["code"] == "routing.choice_forbidden"
    result = await handler._cmd_edit_task({"task_id": "t", "intelligence_class": "deep-high"})
    assert "error" in result
    assert "stop" in result["error"].lower()
    task = await db.get_task("t")
    assert (task.profile_id, task.class_hint) == (None, None)


async def test_edit_route_reset_checks_claim_at_write_time(setup):
    handler, db = setup
    await db.create_agent(Agent(id="held", name="Held", profile_id="coder"))
    await db.create_task(Task(id="t", project_id="p", title="T", description="",
                              status=TaskStatus.READY, profile_id="coder", route_source="legacy"))
    reset = db.reset_task_route

    async def claim_first(*args, **kwargs):
        await db.update_task("t", status=TaskStatus.IN_PROGRESS, assigned_agent_id="held")
        return await reset(*args, **kwargs)

    with patch.object(db, "reset_task_route", side_effect=claim_first):
        result = await handler._cmd_edit_task({"task_id": "t", "intelligence_class": "deep-high"})
    assert "error" in result
    assert (await db.get_task("t")).profile_id == "coder"


@pytest.mark.parametrize(
    ("field", "message"),
    [
        ("model", "chooses a route"),
        ("provider", "chooses a route"),
        ("harness", "chooses a route"),
        ("profile", "chooses a route"),
        ("agent_id", "not supported"),
        ("affinity_agent_id", "not supported"),
    ],
)
def test_graph_does_not_silently_drop_unsupported_routing(field, message):
    from src.task_graph import GraphParseError
    with pytest.raises(GraphParseError, match=message):
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
            "--intelligence-class", "deep-high",
        ])
    assert result.exit_code == 0, result.output
    assert captured[0][1]["intelligence_class"] == "deep-high"
    assert "profile_id" not in captured[0][1]

    with patch("src.cli.tasks._get_client", return_value=client):
        result = CliRunner().invoke(cli, [
            "task", "create", "--project", "p", "--title", "Sol work", "--description", "Do work",
            "--profile", "coder",
        ])
    assert result.exit_code == 2, result.output
    assert len(captured) == 1


@pytest.mark.parametrize(
    ("command", "args", "field"),
    [
        ("_cmd_task_route", {"profile_id": "coder", "intelligence_class": "deep-high"},
         "intelligence_class"),
        ("_cmd_edit_task", {"intelligence_class": "deep-high"}, "class_hint"),
    ],
)
async def test_routing_stopped_task_does_not_restart_it(setup, command, args, field):
    handler, db = setup
    await db.create_task(Task(id="t", project_id="p", title="Stopped", description="",
                              status=TaskStatus.BLOCKED))
    result = await getattr(handler, command)({"task_id": "t", **args})
    assert "error" not in result
    task = await db.get_task("t")
    assert task.status == TaskStatus.BLOCKED
    assert getattr(task, field) == "deep-high"


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


async def test_typed_edit_preserves_an_explicit_hint_null(setup):
    from src.api.codegen import _make_route_handler
    handler, db = setup
    await db.create_task(Task(id="t", project_id="p", title="T", description="",
                              class_hint="deep-high"))
    model = request_model("edit_task")
    # A route is the router's: the typed model offers no profile (spec §5.1).
    assert "profile_id" not in model.model_fields
    typed_edit = _make_route_handler("edit_task", model)
    result = await typed_edit(model(task_id="t", intelligence_class=None), ch=handler)
    assert isinstance(result, dict) and result.get("updated") == "t"
    task = await db.get_task("t")
    assert task.class_hint is None


async def test_typed_edit_omitted_routing_fields_do_not_clear(setup):
    from src.api.codegen import _make_route_handler
    handler, db = setup
    await db.create_task(Task(id="t", project_id="p", title="T", description="",
                              profile_id="coder",
                              route_source="legacy", intelligence_class="deep-high"))
    model = request_model("edit_task")
    result = await _make_route_handler("edit_task", model)(model(task_id="t", title="Renamed"), ch=handler)
    assert result["updated"] == "t"
    task = await db.get_task("t")
    assert (task.profile_id, task.intelligence_class) == ("coder", "deep-high")
