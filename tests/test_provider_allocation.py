"""Provider allocation status and preview (provider-worker-allocation-controls, Tasks 6, 8).

``provider_allocation_status`` is the read half of the provider allocation
surface: every ordinary worker profile grouped by the *harness* provider key,
with its pool supply, live sessions, explicit pins, the manual agents that
bulk controls never rewrite, each project's routing preference and the
provider-wide configured ceiling.  Non-worker profiles are diagnostics, and
a project-scoped caller sees other projects' sessions and tasks redacted.
``provider_allocation_preview`` is what one allocation request would change,
with the SHA-256 token apply consumes, and its scope rules.

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


# == provider_allocation_preview (plan Task 8) =====================================
#
# The preview is a pure function of one snapshot and one request: it selects
# the eligible profiles of one provider, reports before and after rows, the
# project-effective limits and the provider-wide ceiling, the sessions the
# request touches (and the busy set an interrupt would need), the pins and
# manual agents it leaves alone, and a SHA-256 token over the request and
# everything it observed.  It writes nothing.


def _global_supervisor() -> dict:
    return {"kind": "session", "session_id": "supervisor-global", "project_id": None,
            "elevated": True}


def _worker_scope() -> dict:
    return {"kind": "session", "session_id": "s1", "task_id": "t1", "project_id": ALPHA,
            "elevated": False}


@pytest.fixture
def swarm_on(orch):
    """``participation: pool`` is refused while the swarm is off, as ``pool set-lifecycle`` is."""
    enabled = orch.config.swarm.enabled
    orch.config.swarm.enabled = True
    yield orch
    orch.config.swarm.enabled = enabled


async def _preview(orch, **request):
    return await _handler(orch).execute("provider_allocation_preview", request)


def _rows(preview) -> dict[str, dict]:
    return {row["profile_id"]: row for row in preview["profiles"]}


def _warnings(preview) -> dict[str, dict]:
    return {row["code"]: row for row in preview["warnings"]}


async def _snapshot(orch):
    from src.providers.allocation import build_allocation_snapshot

    return await build_allocation_snapshot(orch)


# -- selection --------------------------------------------------------------------


async def test_preview_selects_every_eligible_profile_of_the_provider(swarm_on):
    orch = swarm_on
    preview = await _preview(orch, provider="openai", participation="pool")
    assert preview["success"] is True, preview
    # A vendor resolves to the harness provider key, and the canonical request says so.
    assert preview["provider"] == "codex" and preview["request"]["provider"] == "codex"
    assert preview["request"]["profile_ids"] is None
    codex = [
        "astra-high-codex", "astra-low-codex", "deep-high-codex", "deep-low-codex",
        "fast-high-codex", "fast-low-codex", "standard-high-codex",
    ]
    assert preview["selected"] == codex
    rows = _rows(preview)
    assert list(rows) == codex
    assert all(row["selected"] for row in rows.values())
    # Already-pool rungs are unchanged; task rungs join the pool.
    assert rows["standard-high-codex"]["changed"] is False
    assert rows["deep-high-codex"]["changed_fields"] == ["lifecycle"]
    assert rows["deep-high-codex"]["before"]["lifecycle"] == "task"
    assert rows["deep-high-codex"]["after"]["lifecycle"] == "pool"
    # Nothing Claude-side is selected or even listed.
    assert not any(pid.endswith("-claude") for pid in rows)


async def test_preview_narrows_to_named_profiles_of_the_same_provider(swarm_on):
    orch = swarm_on
    preview = await _preview(
        orch, provider="codex", profile_ids=["deep-high-codex", "deep-high-codex"],
        participation="pool",
    )
    assert preview["success"] is True, preview
    assert preview["selected"] == ["deep-high-codex"]
    assert preview["request"]["profile_ids"] == ["deep-high-codex"]
    rows = _rows(preview)
    assert rows["deep-high-codex"]["selected"] is True
    assert rows["deep-low-codex"]["selected"] is False
    assert rows["deep-low-codex"]["changed"] is False


@pytest.mark.parametrize(
    ("profile_ids", "fragment"),
    [
        (["standard-high-claude"], "belongs to provider claude, not codex"),
        (["standard-high-codex", "standard-high-claude"], "belongs to provider claude"),
        (["reviewer"], "not an ordinary worker profile (role)"),
        (["supervisor"], "not an ordinary worker profile (named)"),
        (["mystery-worker"], "not an ordinary worker profile (unknown_provider)"),
        (["nope"], "unknown profile 'nope'"),
        ([], "empty selection"),
        ([" "], "empty selection"),
    ],
)
async def test_preview_refuses_foreign_control_unknown_and_empty_selections(
    orch, profile_ids, fragment
):
    preview = await _preview(
        orch, provider="codex", profile_ids=profile_ids, participation="task"
    )
    assert preview["success"] is False
    assert fragment in preview["error"], preview["error"]


async def test_preview_refuses_an_unknown_provider_and_an_empty_request(orch):
    unknown = await _preview(orch, provider="mystery", participation="pool")
    assert unknown["success"] is False and unknown["error"].startswith("unknown provider")
    nothing = await _preview(orch, provider="codex")
    assert nothing["success"] is False and nothing["error"].startswith("nothing to change")
    missing = await _preview(orch, participation="pool")
    assert missing["success"] is False and "provider is required" in missing["error"]


@pytest.mark.parametrize(
    ("request_fields", "fragment"),
    [
        ({"participation": "named"}, "participation must be pool or task"),
        ({"participation": "pool", "drain": "kill"}, "drain must be one of"),
        ({"bounds": {}}, "pass bounds.min and/or bounds.max"),
        ({"bounds": {"min": -1}}, "min must be >= 0"),
        ({"bounds": {"max": 0}}, "max must be >= 1"),
        ({"bounds": {"min": 3, "max": 2}}, "max must be >= min"),
        ({"bounds": {"min": "one"}}, "bounds.min must be an integer"),
        ({"bounds": {"min": 0, "cap": 2}}, "bounds accepts only min and max"),
        ({"participation": "task", "bounds": {"max": 2}}, "participation task clears them"),
        ({"receive_new_work": {"project_id": ALPHA}}, "mode must be prefer or clear"),
        ({"receive_new_work": {"mode": "prefer"}}, "project_id is required"),
        ({"receive_new_work": {"project_id": "nope", "mode": "prefer"}}, "not found"),
        ({"participation": "pool", "allow_pinned_wait": "yes"}, "must be a boolean"),
    ],
)
async def test_preview_validates_the_request(orch, request_fields, fragment):
    preview = await _preview(orch, provider="codex", **request_fields)
    assert preview["success"] is False
    assert fragment in preview["error"], preview["error"]


async def test_bounds_follow_the_pool_scale_validation_per_profile(orch):
    # ``deep-high-claude`` has no min_active, and ``aq pool scale --max`` alone
    # refuses exactly that; the preview names the profile.
    refused = await _preview(orch, provider="claude", bounds={"max": 1})
    assert refused["success"] is False
    assert refused["error"].startswith("deep-high-claude: min must be >= 0")
    # A named task-lifecycle profile is no pool profile, as for ``pool scale``.
    named = await _preview(
        orch, provider="claude", profile_ids=["fast-low-claude"], bounds={"min": 0, "max": 1}
    )
    assert named["success"] is False and "no pool profile 'fast-low-claude'" in named["error"]


async def test_pool_participation_is_refused_while_the_swarm_is_off(orch):
    assert orch.config.swarm.enabled is False
    preview = await _preview(orch, provider="codex", participation="pool")
    assert preview["success"] is False
    assert "swarm.enabled is false" in preview["error"]
    # Leaving the pool is always allowed.
    assert (await _preview(orch, provider="codex", participation="task"))["success"] is True


# -- rows, limits and the ceiling -------------------------------------------------


async def test_preview_reports_the_ceiling_beside_per_profile_bounds(orch):
    preview = await _preview(orch, provider="claude", bounds={"min": 0, "max": 1})
    assert preview["success"] is True, preview
    rows = _rows(preview)
    rung = rows["standard-high-claude"]
    assert rung["before"] == {
        "lifecycle": "pool", "enabled": True, "min_active": 1, "max_active": 2,
        "min_per_project": None,
    }
    assert rung["after"] == {**rung["before"], "min_active": 0, "max_active": 1}
    assert rung["changed_fields"] == ["min_active", "max_active"]
    assert rows["deep-high-claude"]["after"]["max_active"] == 1
    assert rows["deep-low-claude"]["after"]["max_active"] == 1
    # Bounds are per pool profile: task rungs keep theirs and are named.
    assert rows["fast-low-claude"]["changed"] is False
    assert _warnings(preview)["bounds_skipped"]["subjects"] == [
        "fast-high-claude", "fast-low-claude",
    ]
    # The disabled pool adds nothing to the ceiling before or after.
    assert preview["ceiling"] == {
        "before": {"min_active": 1, "max_active": 3, "unbounded": False, "pool_profiles": 2},
        "after": {"min_active": 0, "max_active": 2, "unbounded": False, "pool_profiles": 2},
    }
    limits = {
        (row["project_id"], row["profile_id"]): row for row in preview["project_limits"]
    }
    assert limits[(ALPHA, "standard-high-claude")] == {
        "project_id": ALPHA,
        "profile_id": "standard-high-claude",
        "max_concurrent_agents": 2,
        "lifecycle_before": "pool",
        "lifecycle_after": "pool",
        "effective_max_before": 2,
        "effective_max_after": 1,
    }
    assert (ALPHA, "fast-low-claude") not in limits


async def test_unbounded_max_makes_the_ceiling_unbounded(orch):
    preview = await _preview(
        orch, provider="codex", profile_ids=["standard-high-codex"],
        bounds={"min": 0, "max": "unbounded"},
    )
    assert preview["success"] is True, preview
    assert preview["request"]["bounds"] == {"min": 0, "max": None}
    assert _rows(preview)["standard-high-codex"]["after"]["max_active"] is None
    limits = {
        (row["project_id"], row["profile_id"]): row for row in preview["project_limits"]
    }
    # The project cap still bounds an unbounded pool.
    assert limits[(BRAVO, "standard-high-codex")]["effective_max_after"] == 2


# -- sessions, the busy set, pins and manual agents --------------------------------


async def test_leaving_the_pool_drains_by_mode_and_names_the_busy_set(orch):
    graceful = await _preview(orch, provider="claude", participation="task")
    assert graceful["success"] is True, graceful
    assert {row["profile_id"] for row in graceful["profiles"] if row["changed"]} == {
        "deep-high-claude", "deep-low-claude", "standard-high-claude",
    }
    actions = {row["session_id"]: row["action"] for row in graceful["sessions"]}
    assert actions == {
        "sess-alpha-busy": "stop_after_task",
        "sess-alpha-idle": "stop",
        "sess-bravo-start": "stop",
    }
    assert graceful["busy"] == {"session_ids": ["sess-alpha-busy"],
                                "task_ids": ["task-alpha-busy"]}
    assert _rows(graceful)["standard-high-claude"]["after"] == {
        "lifecycle": "task", "enabled": True, "min_active": None, "max_active": None,
        "min_per_project": None,
    }
    assert graceful["ceiling"]["after"] == {
        "min_active": 0, "max_active": 0, "unbounded": False, "pool_profiles": 0,
    }

    idle_now = await _preview(orch, provider="claude", participation="task", drain="idle-now")
    actions = {row["session_id"]: row["action"] for row in idle_now["sessions"]}
    assert actions["sess-alpha-idle"] == "terminate"
    assert actions["sess-alpha-busy"] == "stop_after_task"
    assert idle_now["busy"] == graceful["busy"]

    interrupt = await _preview(
        orch, provider="claude", participation="task", drain="interrupt_busy"
    )
    assert interrupt["request"]["drain"] == "interrupt-busy"
    actions = {row["session_id"]: row["action"] for row in interrupt["sessions"]}
    assert actions["sess-alpha-busy"] == "interrupt"
    assert actions["sess-bravo-start"] == "stop"
    assert interrupt["busy"] == graceful["busy"]


async def test_lowering_bounds_terminates_idle_excess_only_when_asked(orch):
    graceful = await _preview(
        orch, provider="claude", profile_ids=["standard-high-claude"],
        bounds={"min": 0, "max": 1},
    )
    assert {row["action"] for row in graceful["sessions"]} == {"none"}
    assert graceful["busy"] == {"session_ids": [], "task_ids": []}

    idle_now = await _preview(
        orch, provider="claude", profile_ids=["standard-high-claude"],
        bounds={"min": 0, "max": 1}, drain="idle-now",
    )
    actions = {row["session_id"]: row["action"] for row in idle_now["sessions"]}
    # ``aq pool scale --now``: alpha runs two against an effective max of one,
    # so its oldest idle worker goes; busy work is never cut short.
    assert actions == {
        "sess-alpha-busy": "none",
        "sess-alpha-idle": "terminate",
        "sess-bravo-start": "none",
    }
    assert idle_now["busy"] == {"session_ids": [], "task_ids": []}


async def test_pinned_ready_work_on_a_drained_profile_blocks_until_acknowledged(orch):
    preview = await _preview(orch, provider="claude", participation="task")
    pinned = {row["task_id"]: row for row in preview["pinned"]}
    assert set(pinned) == {"task-alpha-busy", "task-alpha-pin"}
    assert pinned["task-alpha-pin"]["waits"] is True
    assert pinned["task-alpha-busy"]["waits"] is False
    warning = _warnings(preview)["pinned_ready_wait"]
    assert warning["blocking"] is True and warning["acknowledged"] is False
    assert warning["subjects"] == ["task-alpha-pin"]
    assert preview["blocked"] is True

    acknowledged = await _preview(
        orch, provider="claude", participation="task", allow_pinned_wait=True
    )
    warning = _warnings(acknowledged)["pinned_ready_wait"]
    assert warning["acknowledged"] is True
    assert acknowledged["blocked"] is False
    assert acknowledged["preview_token"] != preview["preview_token"]


async def test_manual_agents_whose_push_eligibility_changes_are_named(orch):
    preview = await _preview(orch, provider="claude", participation="task")
    # Both definitions draw on standard-high-claude, whichever harness they run.
    agents = {row["agent_id"]: row for row in preview["manual_agents"]}
    assert list(agents) == ["ag-manual-override", "ag-manual-plain"]
    assert agents["ag-manual-override"]["provider"] == "codex"
    assert (agents["ag-manual-plain"]["push_before"], agents["ag-manual-plain"]["push_after"]) == (
        False, True,
    )
    warning = _warnings(preview)["manual_agent_push_changes"]
    assert warning["blocking"] is False
    assert warning["subjects"] == ["ag-manual-override", "ag-manual-plain"]

    # A Codex lifecycle change touches no manual agent: none draws on a Codex rung.
    codex = await _preview(orch, provider="codex", participation="task")
    assert codex["manual_agents"] == []
    assert "manual_agent_push_changes" not in _warnings(codex)


async def test_preference_only_preview_touches_no_session(orch):
    preview = await _preview(
        orch, provider="codex", receive_new_work={"project_id": ALPHA, "mode": "prefer"}
    )
    assert preview["success"] is True, preview
    assert preview["required_scope"] == "project_admin"
    assert preview["preference"] == {
        "project_id": ALPHA, "mode": "prefer", "before": None, "after": "codex",
        "changed": True,
    }
    assert not any(row["changed"] for row in preview["profiles"])
    assert (preview["sessions"], preview["pinned"], preview["manual_agents"]) == ([], [], [])
    assert preview["project_limits"] == []
    assert preview["blocked"] is False

    clear = await _preview(
        orch, provider="codex", receive_new_work={"project_id": ALPHA, "mode": "clear"}
    )
    assert clear["preference"]["changed"] is False
    assert "no_change" in _warnings(clear)


# -- the token -------------------------------------------------------------------


async def test_token_is_stable_for_an_identical_snapshot(orch):
    request = {"provider": "claude", "participation": "task", "drain": "idle-now"}
    first = await _preview(orch, **request)
    second = await _preview(orch, **request)
    assert first["success"] is True, first
    assert len(first["preview_token"]) == 64
    assert first["preview_token"] == second["preview_token"]
    # Equivalent spellings of one request are one request.
    alias = await _preview(orch, provider="Anthropic", participation="task", drain="idle_now")
    assert alias["preview_token"] == first["preview_token"]


def _mutations():
    """``(name, mutate)`` pairs; each changes one fingerprinted input of the snapshot."""

    def group(snapshot, provider):
        return next(row for row in snapshot["providers"] if row["provider"] == provider)

    def rung(snapshot, profile_id="standard-high-claude"):
        return next(
            row for row in group(snapshot, "claude")["profiles"]
            if row["profile_id"] == profile_id
        )

    def session(snapshot, session_id):
        return next(row for row in rung(snapshot)["sessions"] if row["session_id"] == session_id)

    def project(snapshot, project_id):
        return next(row for row in snapshot["projects"] if row["project_id"] == project_id)

    return [
        ("profile lifecycle", lambda s: rung(s, "fast-low-claude").update(lifecycle="pool")),
        ("profile max", lambda s: rung(s).update(max_active=4)),
        ("profile min", lambda s: rung(s).update(min_active=0)),
        ("profile enabled", lambda s: rung(s, "deep-low-claude").update(enabled=True)),
        ("session state", lambda s: session(s, "sess-bravo-start").update(
            state="running", activity="idle")),
        ("session task", lambda s: session(s, "sess-alpha-idle").update(
            task_id="task-bravo-free", activity="busy")),
        ("new session", lambda s: rung(s)["sessions"].append(
            {**session(s, "sess-alpha-idle"), "session_id": "sess-alpha-new"})),
        ("project preference", lambda s: project(s, BRAVO).update(preferred_provider="codex")),
        ("project cap", lambda s: project(s, ALPHA).update(max_concurrent_agents=5)),
        ("pinned task", lambda s: rung(s)["pinned"].append(
            {"task_id": "task-new-pin", "project_id": ALPHA, "status": "READY"})),
        ("pinned status", lambda s: rung(s)["pinned"][1].update(status="ASSIGNED")),
        ("manual agent", lambda s: group(s, "claude")["manual_agents"].pop()),
        ("global cap", lambda s: s.update(global_max_active=3)),
    ]


@pytest.mark.parametrize("name", [name for name, _ in _mutations()])
async def test_token_changes_with_every_fingerprinted_input(orch, name):
    import copy

    from src.providers.allocation import plan_allocation_preview

    snapshot = await _snapshot(orch)
    request = {"provider": "claude", "participation": "task"}
    base = plan_allocation_preview(snapshot, request)
    assert base["success"] is True, base
    mutate = dict(_mutations())[name]
    changed = copy.deepcopy(snapshot)
    mutate(changed)
    assert plan_allocation_preview(changed, request)["preview_token"] != base["preview_token"]


async def test_token_ignores_the_clock_and_pool_demand(orch):
    import copy

    from src.providers.allocation import plan_allocation_preview

    snapshot = await _snapshot(orch)
    request = {"provider": "claude", "participation": "task"}
    base = plan_allocation_preview(snapshot, request)["preview_token"]
    drifted = copy.deepcopy(snapshot)
    drifted["now"] += 600
    for provider in drifted["providers"]:
        for row in provider["profiles"]:
            if row["supply"].get("ready") is not None:
                row["supply"]["ready"] += 7
            for session in row["sessions"]:
                if session["idle_seconds"] is not None:
                    session["idle_seconds"] += 600
                if session["activity"] == "idle":
                    # An idle worker whose claim loop looks stalled is the same worker.
                    session["activity"] = "unresponsive"
    assert plan_allocation_preview(drifted, request)["preview_token"] == base


async def test_token_changes_with_the_request(orch):
    from src.providers.allocation import plan_allocation_preview

    snapshot = await _snapshot(orch)
    tokens = {
        plan_allocation_preview(snapshot, request)["preview_token"]
        for request in (
            {"provider": "claude", "participation": "task"},
            {"provider": "claude", "participation": "task", "drain": "idle-now"},
            {"provider": "claude", "participation": "task",
             "profile_ids": ["standard-high-claude"]},
            {"provider": "claude", "bounds": {"min": 0, "max": 1}},
            {"provider": "claude", "bounds": {"min": 0, "max": 2}},
            {"provider": "claude", "receive_new_work": {"project_id": ALPHA, "mode": "prefer"}},
        )
    }
    assert len(tokens) == 6


async def test_preference_only_token_ignores_session_churn(orch):
    import copy

    from src.providers.allocation import plan_allocation_preview

    snapshot = await _snapshot(orch)
    request = {"provider": "claude", "receive_new_work": {"project_id": ALPHA, "mode": "prefer"}}
    base = plan_allocation_preview(snapshot, request)["preview_token"]
    churned = copy.deepcopy(snapshot)
    rung = next(
        row for row in churned["providers"][0]["profiles"]
        if row["profile_id"] == "standard-high-claude"
    )
    rung["sessions"][0].update(task_id="task-bravo-free", activity="busy")
    assert plan_allocation_preview(churned, request)["preview_token"] == base
    for row in churned["projects"]:
        if row["project_id"] == ALPHA:
            row["preferred_provider"] = "claude"
    assert plan_allocation_preview(churned, request)["preview_token"] != base


async def test_preview_writes_nothing(orch):
    before = await orch.db.get_profile("standard-high-claude")
    sessions = await orch.db.list_sessions(lifecycle="pool", live_only=True)
    await _preview(orch, provider="claude", participation="task", drain="interrupt-busy")
    after = await orch.db.get_profile("standard-high-claude")
    assert (after.lifecycle, after.min_active, after.max_active) == (
        before.lifecycle, before.min_active, before.max_active,
    )
    assert [
        (s.id, s.desired_state) for s in await orch.db.list_sessions(lifecycle="pool",
                                                                      live_only=True)
    ] == [(s.id, s.desired_state) for s in sessions]


# -- scope ------------------------------------------------------------------------


async def test_operator_and_global_supervisor_may_preview_anything(orch):
    request = {"provider": "claude", "participation": "task", "drain": "interrupt-busy"}
    local = await _preview(orch, **request)
    assert local["success"] is True, local
    assert local["required_scope"] == "operator"
    global_admin = await _preview(orch, **request, _scope=_global_supervisor())
    assert global_admin["success"] is True, global_admin
    assert global_admin["preview_token"] == local["preview_token"]


@pytest.mark.parametrize(
    ("request_fields", "fragment"),
    [
        ({"participation": "task"}, "requires operator scope"),
        ({"bounds": {"min": 0, "max": 1}}, "requires operator scope"),
        (
            {"receive_new_work": {"project_id": ALPHA, "mode": "prefer"},
             "drain": "interrupt-busy"},
            "interrupt-busy requires operator scope",
        ),
        (
            {"receive_new_work": {"project_id": BRAVO, "mode": "prefer"}},
            "only its own project's preference",
        ),
    ],
)
async def test_project_admin_may_preview_only_its_own_preference(
    orch, request_fields, fragment
):
    refused = await _preview(
        orch, provider="codex", **request_fields, _scope=_alpha_supervisor()
    )
    assert refused["success"] is False
    assert refused["error"].startswith("out of scope"), refused["error"]
    assert fragment in refused["error"]


async def test_project_admin_previews_its_own_preference(orch):
    preview = await _preview(
        orch, provider="codex", receive_new_work={"project_id": ALPHA, "mode": "prefer"},
        project_id=ALPHA, _scope=_alpha_supervisor(),
    )
    assert preview["success"] is True, preview
    assert preview["preference"]["after"] == "codex"


async def test_worker_token_cannot_preview(orch):
    refused = await _preview(
        orch, provider="codex", receive_new_work={"project_id": ALPHA, "mode": "prefer"},
        _scope=_worker_scope(),
    )
    assert refused["success"] is False and refused["error"].startswith("out of scope")
    worker = RequestScope(kind="session", session_id="s1", task_id="t1", project_id=ALPHA)
    assert "provider_allocation_preview" not in AGENT_COMMAND_SET
    assert check_command_scope("provider_allocation_preview", {}, worker) == (
        "out of scope: provider_allocation_preview"
    )


# -- API and contract ---------------------------------------------------------------


async def test_api_previews_and_maps_refusals(orch):
    body = {"provider": "claude", "profile_ids": ["standard-high-claude"],
            "bounds": {"min": 0, "max": None}, "drain": "idle-now"}
    async with _client(orch) as client:
        response = await client.post("/api/providers/allocation/preview", json=body)
        unknown = await client.post(
            "/api/providers/allocation/preview", json={"provider": "nope", "participation": "pool"}
        )
        invalid = await client.post(
            "/api/providers/allocation/preview", json={"provider": "claude"}
        )
    assert response.status_code == 200, response.text
    preview = response.json()
    # An explicit ``max: null`` is an unbounded pool, not an omitted bound.
    assert preview["request"]["bounds"] == {"min": 0, "max": None}
    assert _rows(preview)["standard-high-claude"]["after"]["max_active"] is None
    assert len(preview["preview_token"]) == 64
    assert unknown.status_code == 404
    assert invalid.status_code == 400

    supervisor = RequestScope(kind="session", session_id="sup", project_id=ALPHA, elevated=True)
    async with _client(orch, supervisor) as client:
        refused = await client.post(
            "/api/providers/allocation/preview",
            json={"provider": "claude", "participation": "task"},
        )
        allowed = await client.post(
            "/api/providers/allocation/preview",
            json={"provider": "claude",
                  "receive_new_work": {"project_id": ALPHA, "mode": "prefer"}},
        )
    assert refused.status_code == 403
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["preference"]["after"] == "claude"


async def test_the_preview_contract_is_a_read(orch):
    from src.commands.contracts import CONTRACTS
    from src.commands.contracts.builtin import (
        ProviderAllocationPreviewArgs,
        set_handler_provider,
    )

    set_handler_provider(lambda: _handler(orch))
    try:
        registration = CONTRACTS.get("provider_allocation_preview")
        result = await registration.invoke(
            ProviderAllocationPreviewArgs(
                provider="codex",
                profile_ids=["standard-high-codex"],
                bounds={"min": 0, "max": None},
            ),
            None,
        )
        refused = await registration.invoke(
            ProviderAllocationPreviewArgs(provider="codex"), None
        )
    finally:
        set_handler_provider(None)
    assert registration.contract.execution.side_effect.value == "read"
    assert result.outcome == "previewed", result.summary
    assert result.value.request["bounds"] == {"min": 0, "max": None}
    assert len(result.value.preview_token) == 64
    assert refused.outcome == "rejected"
