"""Provider allocation status (provider-worker-allocation-controls, plan Task 6).

``provider_allocation_status`` is the read half of the provider allocation
surface: every ordinary worker profile grouped by the *harness* provider key,
with its pool supply, live sessions, explicit pins, the manual agents that
bulk controls never rewrite, each project's routing preference and the
provider-wide configured ceiling.  Non-worker profiles are diagnostics, and
a project-scoped caller sees other projects' sessions and tasks redacted.

The fixture is the spec's mixed fleet on real PostgreSQL: Claude and Codex
worker rungs at two classes, pool and task lifecycles, idle / busy /
starting / draining sessions in two projects, pins on both providers,
unpinned tasks, and manual agents with and without overrides.  No LLM.
"""

from __future__ import annotations

import json
import time

import pytest

from src.api.auth import LOCAL_SCOPE, RequestScope
from src.api.scope import AGENT_COMMAND_SET, check_command_scope
from src.models import Agent, AgentProfile, AgentState, SessionRecord, Task, TaskStatus
from src.providers.allocation import aggregate_ceiling
from src.providers.intent import CLASS_ONLY, PINNED, PREFERRED
from src.sessions.harness_parser import Harness

ALPHA = "alpha"
BRAVO = "bravo"


@pytest.fixture
async def orch(tmp_path):
    from tests.session_dispatch_helpers import create_session_project, make_session_orch

    orch = await make_session_orch(tmp_path)
    orch.harness_registry.upsert(
        Harness(id="codex", name="codex", command="codex", prompt_mode="arg",
                process_names=("codex",))
    )

    async def probe(provider, timeout):
        return "cannot_tell"

    orch.provider_availability._probe_impl = probe
    # ``initialize`` seeds every shipped rung as a task-lifecycle profile;
    # these are the pools the fleet runs.
    for pid, fields in (
        ("standard-high-claude", {"lifecycle": "pool", "min_active": 1, "max_active": 2}),
        ("deep-high-claude", {"lifecycle": "pool", "max_active": 1}),
        ("deep-low-claude", {"lifecycle": "pool", "max_active": 5, "enabled": False}),
        ("standard-high-codex", {"lifecycle": "pool", "max_active": 3}),
        ("fast-low-codex", {"lifecycle": "pool", "max_active": None}),
    ):
        await orch.db.update_profile(pid, **fields)
    await orch.db.create_profile(
        AgentProfile(
            id="mystery-worker", name="mystery-worker", harness="mystery",
            default_class="standard-high", lifecycle="pool", max_active=4,
        )
    )
    await orch.db.create_profile(
        AgentProfile(id="classless-worker", name="classless-worker", harness="claude")
    )
    for project_id in (ALPHA, BRAVO):
        await create_session_project(
            orch, project_id=project_id,
            default_profile_id="standard-high-claude", create_profile=False,
        )
    await _fleet(orch)
    yield orch
    await orch.wait_for_running_tasks(timeout=5)
    await orch.provider_availability.close()
    await orch.db.close()


async def _fleet(orch) -> None:
    db = orch.db
    now = time.time()
    for agent_id, profile_id, fields in (
        ("ag-pool-1", "standard-high-claude", {}),
        ("ag-pool-2", "standard-high-claude", {"state": AgentState.BUSY}),
        ("ag-pool-3", "standard-high-claude", {"state": AgentState.BUSY}),
        ("ag-pool-4", "standard-high-codex", {"state": AgentState.BUSY}),
        ("ag-pool-5", "standard-high-codex", {"state": AgentState.BUSY}),
        ("ag-manual-plain", "standard-high-claude", {}),
        (
            "ag-manual-override",
            "standard-high-claude",
            {
                "harness": "codex",
                "intelligence_class": "deep-high",
                "model": "gpt-6",
                "state": AgentState.BUSY,
            },
        ),
        ("ag-manual-disabled", "standard-high-claude", {"enabled": False}),
    ):
        await db.create_agent(Agent(id=agent_id, name=agent_id, profile_id=profile_id, **fields))

    for task_id, project_id, profile_id, status, intent, agent_id in (
        ("task-alpha-busy", ALPHA, "standard-high-claude", TaskStatus.IN_PROGRESS, PINNED,
         "ag-pool-2"),
        ("task-alpha-pin", ALPHA, "standard-high-claude", TaskStatus.READY, PINNED, None),
        ("task-alpha-pref", ALPHA, "standard-high-claude", TaskStatus.READY, PREFERRED, None),
        ("task-alpha-done", ALPHA, "standard-high-claude", TaskStatus.COMPLETED, PINNED, None),
        ("task-alpha-task", ALPHA, "deep-high-codex", TaskStatus.IN_PROGRESS, CLASS_ONLY, None),
        ("task-alpha-codex-pin", ALPHA, "standard-high-codex", TaskStatus.ASSIGNED, PINNED,
         None),
        ("task-bravo-busy", BRAVO, "standard-high-codex", TaskStatus.IN_PROGRESS, PREFERRED,
         "ag-pool-5"),
        ("task-bravo-pin", BRAVO, "standard-high-codex", TaskStatus.READY, PINNED, None),
        ("task-bravo-free", BRAVO, "standard-high-claude", TaskStatus.READY, CLASS_ONLY, None),
        ("task-bravo-manual", BRAVO, "deep-high-codex", TaskStatus.IN_PROGRESS, CLASS_ONLY,
         "ag-manual-override"),
    ):
        await db.create_task(
            Task(
                id=task_id,
                project_id=project_id,
                title=f"title of {task_id}",
                description="d",
                status=status,
                profile_id=profile_id,
                provider_intent=intent,
                assigned_agent_id=agent_id,
            )
        )
    await db.update_agent("ag-manual-override", current_task_id="task-bravo-manual")

    def session(sid, project_id, profile_id, **fields):
        # ``provider`` is the session *transport*: never the allocation key.
        base = {
            "id": sid, "project_id": project_id, "profile_id": profile_id, "harness": "claude",
            "provider": "tmux", "name": sid, "lifecycle": "pool", "work_dir": f"/wd/{sid}",
            "epoch": "e", "instance_token": "t", "started_at": now - 60,
            "last_activity": now - 10, "state": "running",
        }
        return SessionRecord(**{**base, **fields})

    for record in (
        session("sess-alpha-idle", ALPHA, "standard-high-claude", agent_id="ag-pool-1"),
        session(
            "sess-alpha-busy", ALPHA, "standard-high-claude", agent_id="ag-pool-2",
            task_id="task-alpha-busy", claim_phase="active", claim_phase_at=now - 5,
        ),
        session("sess-alpha-old", ALPHA, "standard-high-claude", state="stopped",
                ended_at=now - 30),
        session("sess-bravo-start", BRAVO, "standard-high-claude", agent_id="ag-pool-3",
                state="starting"),
        session("sess-bravo-drain", BRAVO, "standard-high-codex", harness="codex",
                agent_id="ag-pool-4", state="draining"),
        session(
            "sess-bravo-busy", BRAVO, "standard-high-codex", harness="codex",
            agent_id="ag-pool-5", task_id="task-bravo-busy", claim_phase="active",
            claim_phase_at=now - 5,
        ),
        session("sess-alpha-task", ALPHA, "deep-high-codex", harness="codex", lifecycle="task",
                task_id="task-alpha-task"),
    ):
        await db.create_session(record)


def _handler(orch):
    from src.commands.handler import CommandHandler

    return CommandHandler(orch, orch.config)


def _alpha_supervisor() -> dict:
    return {"kind": "session", "session_id": "sup-alpha", "project_id": ALPHA, "elevated": True}


def _providers(result) -> dict[str, dict]:
    return {row["provider"]: row for row in result["providers"]}


def _profiles(provider_row) -> dict[str, dict]:
    return {row["profile_id"]: row for row in provider_row["profiles"]}


def _stable(value):
    """*value* without the clock: ``now`` and every ``idle_seconds``."""
    if isinstance(value, dict):
        return {
            key: _stable(item) for key, item in value.items()
            if key not in {"now", "idle_seconds"}
        }
    if isinstance(value, list):
        return [_stable(item) for item in value]
    return value


# -- grouping -------------------------------------------------------------------


async def test_groups_ordinary_workers_by_harness_provider_key(orch):
    result = await _handler(orch).execute("provider_allocation_status", {})
    assert result["success"] is True, result
    providers = _providers(result)
    # Sessions record transport ``tmux``; grouping follows the profile's harness.
    assert list(providers) == ["claude", "codex"]
    claude, codex = providers["claude"], providers["codex"]
    assert claude["vendor"] == "anthropic" and codex["vendor"] == "openai"
    assert claude["harnesses"] == ["claude"] and codex["harnesses"] == ["codex"]
    assert list(_profiles(claude)) == [
        "deep-high-claude",
        "deep-low-claude",
        "fast-high-claude",
        "fast-low-claude",
        "standard-high-claude",
    ]
    assert list(_profiles(codex)) == [
        "astra-high-codex",
        "astra-low-codex",
        "deep-high-codex",
        "deep-low-codex",
        "fast-high-codex",
        "fast-low-codex",
        "standard-high-codex",
    ]
    rung = _profiles(claude)["standard-high-claude"]
    assert {key: rung[key] for key in (
        "harness", "lifecycle", "enabled", "intelligence_class", "min_active", "max_active",
    )} == {
        "harness": "claude",
        "lifecycle": "pool",
        "enabled": True,
        "intelligence_class": "standard-high",
        "min_active": 1,
        "max_active": 2,
    }
    assert _profiles(claude)["deep-low-claude"]["enabled"] is False
    assert _profiles(codex)["deep-high-codex"]["lifecycle"] == "task"


async def test_non_workers_are_diagnostics_never_selected(orch):
    result = await _handler(orch).execute("provider_allocation_status", {})
    diagnostics = {
        row["id"]: row["reason"] for row in result["diagnostics"] if row["kind"] == "profile"
    }
    assert diagnostics["supervisor"] == "named"
    for role in ("reviewer", "final-reviewer", "triage", "spec-ingest", "playbook-compiler",
                 "planner", "pr-merger"):
        assert diagnostics[role] == "role", role
    assert diagnostics["classless-worker"] == "malformed"
    assert diagnostics["mystery-worker"] == "unknown_provider"
    selected = {
        profile["profile_id"]
        for provider in result["providers"]
        for profile in provider["profiles"]
    }
    assert selected.isdisjoint(diagnostics)


# -- counts ---------------------------------------------------------------------


async def test_supply_sessions_and_ceiling_are_exact(orch):
    result = await _handler(orch).execute("provider_allocation_status", {})
    claude, codex = _providers(result)["claude"], _providers(result)["codex"]

    rung = _profiles(claude)["standard-high-claude"]
    assert rung["supply"] == {
        "ready": 3, "idle": 1, "busy": 1, "starting": 1, "draining": 0, "unresponsive": 0,
    }
    assert [(row["project_id"], row["ready"], row["idle"], row["busy"], row["starting"])
            for row in rung["projects"]] == [(ALPHA, 2, 1, 1, 0), (BRAVO, 1, 0, 0, 1)]
    sessions = {row["session_id"]: row for row in rung["sessions"]}
    # The stopped session is history, not supply.
    assert list(sessions) == ["sess-alpha-busy", "sess-alpha-idle", "sess-bravo-start"]
    assert sessions["sess-alpha-idle"]["activity"] == "idle"
    assert sessions["sess-alpha-idle"]["idle_seconds"] == pytest.approx(10, abs=5)
    assert sessions["sess-alpha-busy"]["activity"] == "busy"
    assert sessions["sess-alpha-busy"]["task_id"] == "task-alpha-busy"
    assert sessions["sess-alpha-busy"]["task_title"] == "title of task-alpha-busy"
    assert sessions["sess-alpha-busy"]["idle_seconds"] is None
    assert sessions["sess-bravo-start"]["activity"] == "starting"

    codex_rung = _profiles(codex)["standard-high-codex"]
    assert codex_rung["supply"] == {
        "ready": 1, "idle": 0, "busy": 1, "starting": 0, "draining": 1, "unresponsive": 0,
    }
    # A task-lifecycle profile has live sessions but no pool demand.
    task_profile = _profiles(codex)["deep-high-codex"]
    assert task_profile["supply"]["ready"] is None
    assert task_profile["supply"]["busy"] == 1
    assert [row["session_id"] for row in task_profile["sessions"]] == ["sess-alpha-task"]
    assert task_profile["sessions"][0]["lifecycle"] == "task"

    assert claude["supply"] == {
        "ready": 3, "idle": 1, "busy": 1, "starting": 1, "draining": 0, "unresponsive": 0,
    }
    assert codex["supply"] == {
        "ready": 1, "idle": 0, "busy": 2, "starting": 0, "draining": 1, "unresponsive": 0,
    }
    # Enabled pools only: the disabled deep-low-claude pool (max 5) adds nothing,
    # and one unbounded Codex pool makes the Codex ceiling unbounded.
    assert claude["ceiling"] == {
        "min_active": 1, "max_active": 3, "unbounded": False, "pool_profiles": 2,
    }
    assert codex["ceiling"] == {
        "min_active": 0, "max_active": None, "unbounded": True, "pool_profiles": 2,
    }


async def test_pins_and_preferred_tasks_per_profile(orch):
    result = await _handler(orch).execute("provider_allocation_status", {})
    claude, codex = _providers(result)["claude"], _providers(result)["codex"]

    rung = _profiles(claude)["standard-high-claude"]
    assert rung["pinned"] == {
        "count": 2,
        "by_status": {"IN_PROGRESS": 1, "READY": 1},
        "task_ids": ["task-alpha-busy", "task-alpha-pin"],
    }
    assert rung["preferred"] == {
        "count": 1, "by_status": {"READY": 1}, "task_ids": ["task-alpha-pref"],
    }
    codex_rung = _profiles(codex)["standard-high-codex"]
    assert codex_rung["pinned"] == {
        "count": 2,
        "by_status": {"ASSIGNED": 1, "READY": 1},
        "task_ids": ["task-alpha-codex-pin", "task-bravo-pin"],
    }
    assert codex_rung["preferred"]["task_ids"] == ["task-bravo-busy"]
    # ``class_only`` and finished work are not pins.
    assert _profiles(codex)["deep-high-codex"]["pinned"]["count"] == 0
    assert (claude["pinned_tasks"], claude["preferred_tasks"]) == (2, 1)
    assert (codex["pinned_tasks"], codex["preferred_tasks"]) == (2, 1)


async def test_manual_agents_are_grouped_by_effective_provider(orch):
    result = await _handler(orch).execute("provider_allocation_status", {})
    claude, codex = _providers(result)["claude"], _providers(result)["codex"]
    # Agents owned by a live pool session are pool workers; a disabled
    # definition and the supervisor are not manual workers.
    assert [row["agent_id"] for row in claude["manual_agents"]] == ["ag-manual-plain"]
    assert [row["agent_id"] for row in codex["manual_agents"]] == ["ag-manual-override"]
    plain = claude["manual_agents"][0]
    assert plain["has_overrides"] is False
    assert (plain["effective_harness"], plain["effective_class"]) == ("claude", "standard-high")
    override = codex["manual_agents"][0]
    assert override["profile_id"] == "standard-high-claude"
    assert {key: override[key] for key in ("harness", "intelligence_class", "model")} == {
        "harness": "codex", "intelligence_class": "deep-high", "model": "gpt-6",
    }
    assert override["has_overrides"] is True
    assert (override["effective_harness"], override["effective_class"]) == ("codex", "deep-high")
    assert override["current_task_id"] == "task-bravo-manual"
    assert override["current_project_id"] == BRAVO
    assert override["redacted"] is False


async def test_projects_carry_preference_and_limits(orch):
    result = await _handler(orch).execute("provider_allocation_status", {})
    projects = {row["project_id"]: row for row in result["projects"]}
    assert list(projects) == [ALPHA, BRAVO]
    assert projects[ALPHA]["preferred_provider"] is None
    assert projects[ALPHA]["default_profile_id"] == "standard-high-claude"
    assert projects[ALPHA]["max_concurrent_agents"] == 2
    assert result["redacted"] is False and result["project_id"] is None


async def test_status_is_deterministic(orch):
    handler = _handler(orch)
    first = await handler.execute("provider_allocation_status", {})
    second = await handler.execute("provider_allocation_status", {})
    assert _stable(first) == _stable(second)


async def test_last_allocation_event_per_provider(orch):
    result = await _handler(orch).execute("provider_allocation_status", {})
    assert [row["last_allocation"] for row in result["providers"]] == [None, None]

    await orch.db.log_event(
        "provider.allocation_changed",
        payload=json.dumps({"provider": "codex", "request_id": "req-1", "status": "applied"}),
    )
    result = await _handler(orch).execute("provider_allocation_status", {})
    providers = _providers(result)
    assert providers["claude"]["last_allocation"] is None
    last = providers["codex"]["last_allocation"]
    assert (last["request_id"], last["status"]) == ("req-1", "applied")
    assert last["event_id"] > 0 and last["at"] > 0


# -- filters and scope ------------------------------------------------------------


async def test_provider_filter_accepts_a_vendor_alias(orch):
    handler = _handler(orch)
    one = await handler.execute("provider_allocation_status", {"provider": "openai"})
    assert [row["provider"] for row in one["providers"]] == ["codex"]
    missing = await handler.execute("provider_allocation_status", {"provider": "nope"})
    assert missing["success"] is False
    assert missing["error"].startswith("unknown provider")


async def test_project_view_filter_for_the_operator(orch):
    result = await _handler(orch).execute("provider_allocation_status", {"project_id": ALPHA})
    assert result["project_id"] == ALPHA and result["redacted"] is False
    assert BRAVO not in json.dumps(result)
    rung = _profiles(_providers(result)["claude"])["standard-high-claude"]
    # Pool identity stays fleet-wide; only the per-project detail narrows.
    assert rung["supply"]["starting"] == 1
    assert [row["project_id"] for row in rung["projects"]] == [ALPHA]
    missing = await _handler(orch).execute("provider_allocation_status", {"project_id": "nope"})
    assert missing["success"] is False


async def test_project_scoped_caller_sees_other_projects_redacted(orch):
    result = await _handler(orch).execute(
        "provider_allocation_status", {"_scope": _alpha_supervisor()}
    )
    assert result["success"] is True, result
    assert result["redacted"] is True and result["project_id"] == ALPHA
    # No other project's id, session, task or title survives anywhere.
    assert BRAVO not in json.dumps(result)
    assert [row["project_id"] for row in result["projects"]] == [ALPHA]

    claude, codex = _providers(result)["claude"], _providers(result)["codex"]
    rung = _profiles(claude)["standard-high-claude"]
    assert rung["supply"]["starting"] == 1  # fleet-wide counts remain
    assert [row["session_id"] for row in rung["sessions"]] == [
        "sess-alpha-busy", "sess-alpha-idle",
    ]
    assert rung["hidden"] == {"projects": 1, "sessions": 1, "tasks": 0}
    codex_rung = _profiles(codex)["standard-high-codex"]
    assert codex_rung["sessions"] == []
    assert codex_rung["pinned"]["count"] == 2
    assert codex_rung["pinned"]["task_ids"] == ["task-alpha-codex-pin"]
    assert codex_rung["preferred"] == {"count": 1, "by_status": {"IN_PROGRESS": 1},
                                       "task_ids": []}
    assert codex_rung["hidden"] == {"projects": 1, "sessions": 2, "tasks": 2}
    override = codex["manual_agents"][0]
    assert override["redacted"] is True
    assert (override["current_task_id"], override["current_project_id"]) == (None, None)


async def test_project_scoped_caller_cannot_view_another_project(orch):
    result = await _handler(orch).execute(
        "provider_allocation_status", {"project_id": BRAVO, "_scope": _alpha_supervisor()}
    )
    assert result["success"] is False
    assert result["error"].startswith("out of scope")


def test_request_scope_is_project_readable():
    assert "provider_allocation_status" not in AGENT_COMMAND_SET
    worker = RequestScope(kind="session", session_id="s1", task_id="t1", project_id=ALPHA)
    assert check_command_scope("provider_allocation_status", {}, worker) == (
        "out of scope: provider_allocation_status"
    )
    supervisor = RequestScope(kind="session", session_id="sup", project_id=ALPHA, elevated=True)
    args: dict = {}
    assert check_command_scope("provider_allocation_status", args, supervisor) is None
    assert args == {"project_id": ALPHA}
    assert "project_id mismatch" in check_command_scope(
        "provider_allocation_status", {"project_id": BRAVO}, supervisor
    )
    assert check_command_scope("provider_allocation_status", {}, LOCAL_SCOPE) is None


# -- the typed API ----------------------------------------------------------------


def _client(orch, scope: RequestScope | None = None):
    from fastapi import FastAPI, Request
    from httpx import ASGITransport, AsyncClient

    from src.api.providers import build_providers_router

    handler = _handler(orch)
    app = FastAPI()
    if scope is not None:

        @app.middleware("http")
        async def _scoped(request: Request, call_next):
            request.state.scope = scope
            return await call_next(request)

    app.include_router(
        build_providers_router(db=orch.db, config=orch.config, command_handler=handler)
    )
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


async def test_api_serves_the_snapshot(orch):
    async with _client(orch) as client:
        response = await client.get("/api/providers/allocation", params={"provider": "codex"})
        missing = await client.get("/api/providers/allocation", params={"provider": "nope"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert [row["provider"] for row in body["providers"]] == ["codex"]
    rung = _profiles(body["providers"][0])["standard-high-codex"]
    assert rung["pinned"]["count"] == 2
    assert missing.status_code == 404


async def test_api_redacts_for_a_project_scope_and_refuses_a_worker(orch):
    supervisor = RequestScope(kind="session", session_id="sup", project_id=BRAVO, elevated=True)
    async with _client(orch, supervisor) as client:
        scoped = await client.get("/api/providers/allocation")
    assert scoped.status_code == 200, scoped.text
    assert scoped.json()["redacted"] is True
    assert ALPHA not in scoped.text

    worker = RequestScope(kind="session", session_id="s1", task_id="t1", project_id=BRAVO)
    async with _client(orch, worker) as client:
        refused = await client.get("/api/providers/allocation")
    assert refused.status_code == 403


async def test_the_contract_reads_the_same_snapshot(orch):
    from src.commands.contracts import CONTRACTS
    from src.commands.contracts.builtin import (
        ProviderAllocationStatusArgs,
        set_handler_provider,
    )

    set_handler_provider(lambda: _handler(orch))
    try:
        registration = CONTRACTS.get("provider_allocation_status")
        result = await registration.invoke(ProviderAllocationStatusArgs(provider="claude"), None)
        refused = await registration.invoke(ProviderAllocationStatusArgs(provider="nope"), None)
    finally:
        set_handler_provider(None)
    assert registration.contract.execution.side_effect.value == "read"
    assert result.outcome == "read"
    assert [row["provider"] for row in result.value.providers] == ["claude"]
    assert refused.outcome == "rejected"


# -- the pure ceiling -------------------------------------------------------------


def test_aggregate_ceiling_counts_enabled_pools_only():
    rows = [
        {"lifecycle": "pool", "enabled": True, "min_active": 1, "max_active": 2},
        {"lifecycle": "pool", "enabled": True, "min_active": 0, "max_active": 4},
        {"lifecycle": "pool", "enabled": False, "min_active": 3, "max_active": 9},
        {"lifecycle": "task", "enabled": True, "min_active": None, "max_active": None},
    ]
    assert aggregate_ceiling(rows) == {
        "min_active": 1, "max_active": 6, "unbounded": False, "pool_profiles": 2,
    }
    rows.append({"lifecycle": "pool", "enabled": True, "min_active": None, "max_active": None})
    assert aggregate_ceiling(rows) == {
        "min_active": 1, "max_active": None, "unbounded": True, "pool_profiles": 3,
    }
    assert aggregate_ceiling([]) == {
        "min_active": 0, "max_active": 0, "unbounded": False, "pool_profiles": 0,
    }
