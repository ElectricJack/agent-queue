"""Triage tokens can route their project's queue without operator authority."""

from __future__ import annotations

import time
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from src.api import dependencies as deps
from src.api.auth import SessionTokenStore
from src.api.codegen import build_category_routers
from src.api.execute import router as execute_router
from src.api.middleware import TokenAuthMiddleware
from src.api.scope import (
    _FINAL_REVIEWER_COMMANDS,
    _REVIEWER_COMMANDS,
    _TRIAGE_COMMANDS,
)
from src.commands.handler import CommandHandler
from src.config import AppConfig, DiscordConfig
from src.database import Database
from src.models import Agent, AgentProfile, AgentState, Project, SessionRecord, Task, TaskStatus
from src.orchestrator import Orchestrator
from src.vault import ensure_default_intelligence_classes


@pytest.fixture(scope="module")
def generated_routers():
    return build_category_routers()


@pytest.fixture(params=["execute", "typed"])
async def api(tmp_path, monkeypatch, request, generated_routers):
    db = Database(str(tmp_path / "triage-auth.db"))
    await db.initialize()
    data_dir = str(tmp_path / "data")
    ensure_default_intelligence_classes(data_dir)
    config = AppConfig(
        discord=DiscordConfig(bot_token="test", guild_id="1"),
        database_path=str(tmp_path / "triage-auth.db"),
        workspace_dir=str(tmp_path / "workspaces"),
        data_dir=data_dir,
    )
    orch = Orchestrator(config)
    orch.db = db
    handler = CommandHandler(orch, config)
    for pid in ("p", "other"):
        await db.create_project(Project(id=pid, name=pid))
    for profile_id in ("triage", "coder", "project:p:coder", "project:other:coder"):
        await db.upsert_profile(AgentProfile(
            id=profile_id, name=profile_id, harness="codex", needs_workspace=False,
            default_class="fast-low" if profile_id == "triage" else "deep-high",
        ))
    for worker, role in (("triager", "triage"), ("worker", "coder")):
        await db.create_agent(Agent(id=worker, name=worker, profile_id=role))
        await db.create_task(Task(
            id=f"{worker}-job", project_id="p", title=worker, description="Assigned work",
            status=TaskStatus.IN_PROGRESS, profile_id=role, assigned_agent_id=worker,
        ))
        await db.update_agent(
            worker, state=AgentState.BUSY, current_task_id=f"{worker}-job",
        )
        await db.create_session(SessionRecord(
            id=f"s-{worker}", task_id=f"{worker}-job", project_id="p",
            agent_id=worker, profile_id=role, harness="codex", provider="fake",
            name=f"s-{worker}", lifecycle="task", state="running",
            work_dir=str(tmp_path), epoch="test", instance_token=f"instance-{worker}",
            started_at=time.time(),
        ))
    for tid, pid in (("target", "p"), ("foreign", "other"), ("human-waiter", "p")):
        await db.create_task(Task(
            id=tid, project_id=pid, title=tid, description="Needs routing",
            status=TaskStatus.DEFINED,
        ))
    gate, _ = await db.create_gate(
        project_id="p", gate_type="routing", title="Route target",
        waiter_task_ids=["target"],
    )
    foreign_gate, _ = await db.create_gate(
        project_id="other", gate_type="routing", title="Route foreign",
        waiter_task_ids=["foreign"],
    )
    human_gate, _ = await db.create_gate(
        project_id="p", gate_type="human", title="Human approval",
        waiter_task_ids=["human-waiter"],
    )
    store = SessionTokenStore(db)
    tokens = {
        worker: await store.mint(
            session_id=f"s-{worker}", task_id=f"{worker}-job", project_id="p",
        )
        for worker in ("triager", "worker")
    }
    monkeypatch.setattr(deps, "_command_handler", handler)
    monkeypatch.setattr(deps, "_orchestrator", orch)
    monkeypatch.setattr(deps, "_token_store", store)
    monkeypatch.setattr(deps, "_require_session_token", True)
    app = FastAPI()
    app.include_router(execute_router)
    paths = {}
    for router in generated_routers:
        app.include_router(router)
        for route in router.routes:
            paths[route.operation_id] = route.path
    app.add_middleware(TokenAuthMiddleware)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        async def post(command, args=None, *, worker="triager"):
            headers = {"Authorization": f"Bearer {tokens[worker]}"}
            if request.param == "execute":
                return await client.post(
                    "/api/execute", headers=headers,
                    json={"command": command, "args": args or {}},
                )
            return await client.post(paths[command], headers=headers, json=args or {})

        def result(response):
            assert response.status_code == 200, response.text
            payload = response.json()
            if request.param == "execute":
                assert payload["ok"], payload
                return payload["result"]
            return payload

        yield SimpleNamespace(
            db=db, store=store, tokens=tokens, post=post, result=result,
            gate=gate, foreign_gate=foreign_gate, human_gate=human_gate,
        )
    await db.close()


@pytest.mark.parametrize("lifecycle", ["task", "pool"])
async def test_authenticated_triage_routes_waiting_task(api, lifecycle, monkeypatch):
    if lifecycle == "pool":
        await api.db.update_session("s-triager", lifecycle="pool")
        api.tokens["triager"] = await api.store.mint(
            session_id="s-triager", task_id=None, project_id="p",
        )
    # Exercise persistent tokens after a fresh daemon-side store, too.
    monkeypatch.setattr(deps, "_token_store", SessionTokenStore(api.db))
    result = api.result(await api.post("task_route", {
        "task_id": "target", "profile_id": "coder", "intelligence_class": "deep-high",
    }))
    assert result["resolved_gate_ids"] == [api.gate]
    task = await api.db.get_task("target")
    assert (task.profile_id, task.intelligence_class) == ("coder", "deep-high")
    assert (await api.db.get_gate(api.gate))["status"] == "resolved"
    assert not (await deps._token_store.validate(api.tokens["triager"])).elevated


async def test_triage_lists_only_its_project_tasks(api):
    result = api.result(await api.post("list_tasks"))
    ids = {task["id"] for task in result["tasks"]}
    assert "target" in ids
    assert "triager-job" in ids
    assert "foreign" not in ids


@pytest.mark.parametrize("command", ["get_task", "task_show"])
async def test_triage_can_read_another_task_in_its_project(api, command):
    result = api.result(await api.post(command, {"task_id": "target"}))
    assert result["id"] == "target"
    assert result["project_id"] == "p"


@pytest.mark.parametrize(
    "command", sorted(_TRIAGE_COMMANDS & _REVIEWER_COMMANDS & _FINAL_REVIEWER_COMMANDS),
)
async def test_triage_keeps_its_carve_out_for_commands_other_roles_also_claim(api, command):
    """Reading a task is a triage, reviewer *and* final-reviewer capability.

    Both review blocks run before the triage one, so a session that is neither
    kind of reviewer must fall through them rather than be refused by them.
    Regression for 54ab3ee1, where the final-reviewer block returned the
    ordinary-scope error outright and pre-empted the triage carve-out below it.
    """
    result = api.result(await api.post(command, {"task_id": "target"}))
    assert result["id"] == "target"


def test_the_shared_read_commands_are_still_shared():
    """Guards the parametrization above from silently going empty."""
    assert _TRIAGE_COMMANDS & _REVIEWER_COMMANDS & _FINAL_REVIEWER_COMMANDS == {
        "get_task", "task_show",
    }


async def test_triage_reads_only_open_routing_gates_in_its_project(api):
    result = api.result(await api.post("gate_list"))
    assert [gate["id"] for gate in result["gates"]] == [api.gate]
    result = api.result(await api.post("gate_show", {"gate_id": api.gate}))
    assert result["gate"]["id"] == api.gate
    assert result["waiters"] == ["target"]


async def test_triage_reads_global_and_own_project_profiles(api):
    result = api.result(await api.post("list_profiles"))
    ids = {profile["id"] for profile in result["profiles"]}
    assert ids == {"triage", "coder", "project:p:coder"}
    assert result["count"] == 3


async def test_triage_reads_intelligence_classes(api):
    result = api.result(await api.post("list_intelligence_classes"))
    assert "deep-high" in {item["id"] for item in result["classes"]}


@pytest.mark.parametrize("command,args", [
    ("get_task", {"task_id": "foreign"}),
    ("task_show", {"task_id": "foreign"}),
    ("task_route", {"task_id": "foreign", "profile_id": "coder"}),
    ("task_route", {"task_id": "target", "profile_id": "project:other:coder"}),
    ("list_tasks", {"project_id": "other"}),
])
async def test_triage_cannot_cross_project_boundary(api, command, args):
    response = await api.post(command, args)
    assert response.status_code == 403, response.text
    assert (await api.db.get_task("foreign")).profile_id is None
    assert (await api.db.get_task("target")).profile_id is None
    assert (await api.db.get_gate(api.foreign_gate))["status"] == "open"


@pytest.mark.parametrize("which", ["foreign_gate", "human_gate"])
async def test_triage_cannot_read_foreign_or_nonrouting_gate(api, which):
    response = await api.post("gate_show", {"gate_id": getattr(api, which)})
    assert response.status_code == 403, response.text


async def test_triage_cannot_route_without_open_routing_gate(api):
    await api.db.resolve_gate(api.gate, resolved_by="test", resolution="Already routed")
    response = await api.post("task_route", {"task_id": "target", "profile_id": "coder"})
    assert response.status_code == 403, response.text
    assert (await api.db.get_task("target")).profile_id is None


@pytest.mark.parametrize("command,args", [
    ("edit_profile", {"profile_id": "coder", "name": "Unauthorized rename"}),
    ("create_profile", {"id": "unauthorized", "name": "Unauthorized"}),
    ("edit_intelligence_class", {
        "class_id": "deep-high", "name": "Unauthorized", "description": "", "mapping": {},
    }),
    ("gate_resolve", {"gate_id": "unused", "resolved_by": "triage"}),
    ("list_projects", {}),
    ("get_status", {}),
])
async def test_triage_does_not_gain_operator_commands(api, command, args):
    response = await api.post(command, args)
    assert response.status_code == 403, response.text
    assert (await api.db.get_profile("coder")).name == "coder"
    assert await api.db.get_profile("unauthorized") is None


@pytest.mark.parametrize("command,args", [
    ("task_route", {"task_id": "target", "profile_id": "coder"}),
    ("list_tasks", {}),
])
async def test_worker_cannot_impersonate_triage_through_request_fields(api, command, args):
    response = await api.post(command, {
        **args,
        "session_id": "s-triager",
        "role": "triage",
        "_scope": {"kind": "local", "elevated": True, "session_id": "s-triager"},
    }, worker="worker")
    assert response.status_code == 403, response.text
    assert (await api.db.get_task("target")).profile_id is None


@pytest.mark.parametrize("change", ["stopped", "sleeping", "wrong-profile", "wrong-agent", "closed-task"])
async def test_stale_or_unassigned_session_cannot_keep_triage_privileges(api, change):
    if change in {"stopped", "sleeping"}:
        await api.db.update_session("s-triager", state=change)
    elif change == "wrong-profile":
        await api.db.update_session("s-triager", profile_id="coder")
    elif change == "wrong-agent":
        await api.db.update_task("triager-job", assigned_agent_id="worker")
    else:
        await api.db.update_task("triager-job", status=TaskStatus.COMPLETED)
    response = await api.post("task_route", {"task_id": "target", "profile_id": "coder"})
    assert response.status_code == 403, response.text
    assert (await api.db.get_task("target")).profile_id is None


async def test_triage_ordinary_mutations_stay_pinned_to_its_own_task(api):
    response = await api.post("task_close", {
        "task_id": "target", "outcome": "done", "notes": "Not my task",
    })
    assert response.status_code == 403, response.text
    assert (await api.db.get_task("target")).status == TaskStatus.DEFINED
    result = api.result(await api.post("task_show", {"task_id": "triager-job"}))
    assert result["id"] == "triager-job"
    result = api.result(await api.post("task_show", {"task_id": "worker-job"}, worker="worker"))
    assert result["id"] == "worker-job"


async def test_triage_typed_and_execute_routes_have_identical_scoped_error_shapes(api):
    """Plan 9: both transports refuse with the same status and error string.

    The fixture parameterizes the transport, so pinning each denial to one
    exact literal proves /api/execute and the generated typed route cannot
    drift apart in status code or message — a CLI retrying against the
    other surface must see the identical contract.
    """
    cases = [
        # Foreign-project task read.
        ("task_show", {"task_id": "foreign"},
         "out of scope: task must belong to this triage project's queue"),
        # Non-routing (human) gate read.
        ("gate_show", {"gate_id": api.human_gate},
         "out of scope: triage may only read its project's open routing gates"),
        # Ordinary mutation stays pinned to the triager's own task.
        ("task_close", {"task_id": "target", "outcome": "done"},
         "out of scope: task_id mismatch"),
    ]
    for command, args, expected_error in cases:
        response = await api.post(command, args)
        assert response.status_code == 403, (command, response.text)
        assert response.json()["error"] == expected_error, command

    # Routing without an open routing gate — the fixture's gate is resolved
    # first so the denial comes from the gate check, again identically.
    await api.db.resolve_gate(api.gate, resolved_by="test", resolution="done")
    response = await api.post("task_route", {"task_id": "target", "profile_id": "coder"})
    assert response.status_code == 403, response.text
    assert response.json()["error"] == (
        "out of scope: triage may only route tasks with an open routing gate"
    )
    assert (await api.db.get_task("target")).profile_id is None
