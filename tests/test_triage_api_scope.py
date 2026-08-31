"""Triage tokens can route their project's queue without operator authority."""

from __future__ import annotations

import asyncio
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
from src.commands.handler import CommandHandler
from src.config import AppConfig, DiscordConfig
from src.database import Database
from src.models import (
    Agent, AgentProfile, AgentState, PlaybookRun, Project, SessionRecord, Task, TaskStatus,
)
from src.orchestrator import Orchestrator
from src.sessions.harness_parser import Harness
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
    orch.harness_registry.upsert(
        Harness(id="codex", name="Codex", command="codex", model_flag="--model")
    )
    handler = CommandHandler(orch, config)
    for pid in ("p", "other"):
        await db.create_project(Project(id=pid, name=pid))
    for profile_id in ("triage", "coder", "project:p:coder", "project:other:coder"):
        await db.upsert_profile(AgentProfile(
            id=profile_id, name=profile_id, harness="codex", needs_workspace=False,
            default_class="fast-low" if profile_id == "triage" else "deep-high",
        ))
    await db.create_playbook_run(
        PlaybookRun("triage-run", "mandatory-triage", 1, project_id="p", role="triage")
    )
    for worker, role in (("triager", "triage"), ("worker", "coder")):
        await db.create_agent(Agent(
            id=worker, name=worker, profile_id=role,
            role="triage" if worker == "triager" else "worker",
        ))
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
            name=f"s-{worker}", lifecycle="playbook" if worker == "triager" else "task",
            state="running",
            work_dir=str(tmp_path), epoch="test", instance_token=f"instance-{worker}",
            started_at=time.time(),
            playbook_run_id="triage-run" if worker == "triager" else None,
            playbook_node_id="inspect" if worker == "triager" else None,
        ))
    await db.update_playbook_run("triage-run", owner_session_id="s-triager")
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

        principal = await handler._triage_service.authenticate({
            "kind": "session", "session_id": "s-triager", "project_id": "p",
        })
        options = await handler._triage_service.options(principal)
        type_key = options["types"][0]["execution_type_key"]

        yield SimpleNamespace(
            db=db, handler=handler, store=store, tokens=tokens, post=post, result=result,
            gate=gate, foreign_gate=foreign_gate, human_gate=human_gate, type_key=type_key,
            surface=request.param,
        )
    await db.close()


async def test_authenticated_triage_routes_waiting_task(api, monkeypatch):
    # Exercise persistent tokens after a fresh daemon-side store, too.
    monkeypatch.setattr(deps, "_token_store", SessionTokenStore(api.db))
    result = api.result(await api.post("task_route", {
        "task_id": "target", "execution_type_key": api.type_key,
        "expected_revision": 1, "reason": "Matches the requested work",
    }))
    assert result["resolved_gate_ids"] == [api.gate]
    task = await api.db.get_task("target")
    assert (task.profile_id, task.intelligence_class) == ("project:p:coder", "deep-high")
    assert (await api.db.get_gate(api.gate))["status"] == "resolved"
    assert not (await deps._token_store.validate(api.tokens["triager"])).elevated


async def test_authenticated_identical_route_retry_returns_existing_decision(api):
    args = {
        "task_id": "target",
        "execution_type_key": api.type_key,
        "expected_revision": 1,
        "reason": "Matches the requested work",
    }
    first = api.result(await api.post("task_route", args))
    retried = api.result(await api.post("task_route", args))
    assert retried["decision_id"] == first["decision_id"]


async def test_authenticated_options_and_durable_defer_use_live_scope(api):
    options = api.result(await api.post("triage_options"))
    assert options["types"][0]["execution_type_key"] == api.type_key
    deferred = api.result(await api.post("triage_defer", {
        "task_id": "target", "expected_revision": 1, "reason": "No safe match yet",
    }))
    assert deferred["success"] is True
    assert (await api.db.get_gate(api.gate))["status"] == "open"


async def test_legacy_profile_only_route_returns_migration_error(api):
    response = await api.post("task_route", {"task_id": "target", "profile_id": "coder"})
    payload = response.json()
    if api.surface == "execute":
        assert response.status_code == 200 and payload["details"]["code"] == "migration_required"
    else:
        assert response.status_code == 422 and "profile-only" in payload["error"]
    assert (await api.db.get_gate(api.gate))["status"] == "open"


async def test_direct_local_handler_call_cannot_bypass_triage_auth(api):
    result = await api.handler.execute("task_route", {
        "task_id": "target", "execution_type_key": api.type_key,
        "expected_revision": 1, "reason": "Local bypass",
    })
    assert result["success"] is False and result["code"] == "unauthorized"
    assert (await api.db.get_gate(api.gate))["status"] == "open"


async def test_shared_config_editor_lock_precedes_completion_transaction(api):
    lock = api.handler.orchestrator._intelligence_class_edit_lock
    assert api.handler._triage_service.config_lock is lock
    await lock.acquire()
    operation = asyncio.create_task(api.post("task_route", {
        "task_id": "target", "execution_type_key": api.type_key,
        "expected_revision": 1, "reason": "Wait for coherent config",
    }))
    await asyncio.sleep(0)
    assert not operation.done()
    lock.release()
    result = api.result(await operation)
    assert result["success"] is True


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
    ("task_route", {"task_id": "foreign", "execution_type_key": "f" * 64,
                    "expected_revision": 1, "reason": "Wrong project"}),
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
    response = await api.post("task_route", {
        "task_id": "target", "execution_type_key": api.type_key,
        "expected_revision": 1, "reason": "Route",
    })
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
    ("task_route", {"task_id": "target", "execution_type_key": "f" * 64,
                    "expected_revision": 1, "reason": "Impersonation"}),
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


@pytest.mark.parametrize("change", ["stopped", "sleeping", "unlinked", "closed-run", "wrong-owner"])
async def test_stale_or_unassigned_session_cannot_keep_triage_privileges(api, change):
    if change in {"stopped", "sleeping"}:
        await api.db.update_session("s-triager", state=change)
    elif change == "unlinked":
        await api.db.update_session("s-triager", playbook_run_id=None)
    elif change == "closed-run":
        await api.db.update_playbook_run("triage-run", status="completed")
    else:
        await api.db.update_playbook_run("triage-run", owner_session_id=None)
    response = await api.post("task_route", {
        "task_id": "target", "execution_type_key": api.type_key,
        "expected_revision": 1, "reason": "Route",
    })
    assert response.status_code == 403, response.text
    assert (await api.db.get_task("target")).profile_id is None


async def test_request_identity_fields_neither_grant_nor_change_triage_authority(api):
    result = api.result(await api.post("task_route", {
        "task_id": "target", "execution_type_key": api.type_key,
        "expected_revision": 1, "reason": "Authenticated choice",
        "run_id": "fabricated", "session_id": "s-worker", "role": "supervisor",
        "principal": {"project_id": "other", "instance_token": "fabricated"},
    }))
    assert result["success"] is True
    decision = await api.db.get_routing_decision(result["decision_id"])
    assert decision["playbook_run_id"] == "triage-run"
    assert decision["project_id"] == "p"


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
