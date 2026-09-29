"""Provider allocation status, preview and apply (provider-worker-allocation-controls, Tasks 6, 8, 9).

``provider_allocation_status`` is the read half of the provider allocation
surface: every ordinary worker profile grouped by the *harness* provider key,
with its pool supply, live sessions, explicit pins, the manual agents that
bulk controls never rewrite, each project's routing preference and the
provider-wide configured ceiling.  Non-worker profiles are diagnostics, and
a project-scoped caller sees other projects' sessions and tasks redacted.
``provider_allocation_preview`` is what one allocation request would change,
with the SHA-256 token apply consumes, and its scope rules.
``provider_allocation_apply`` carries a reviewed token out: stale tokens are
refused, drains never touch busy work unasked, edits match the single-profile
commands and compensate on failure, and one audit event records it.

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
from src.intelligence_classes import IntelligenceClass
from src.models import Agent, AgentProfile, AgentState, SessionRecord, Task, TaskStatus
from src.providers.allocation import aggregate_ceiling
from src.providers.intent import CLASS_ONLY, PINNED, PREFERRED
from src.sessions.harness_parser import Harness
from tests.assignment_routing_helpers import route_source_for

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
            profile_id="standard-high-claude", create_profile=False,
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
                profile_id=profile_id, route_source=route_source_for(profile_id),
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
    assert projects[ALPHA]["assignment_playbook_id"] == "default-assignment-routing"
    assert "default_profile_id" not in projects[ALPHA]
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


# == provider_allocation_apply (plan Task 9) =======================================
#
# Apply takes only a token.  It rebuilds the preview the token was issued for
# and refuses a stale one with the fresh preview; the profile edits go through
# the pool-admin helpers (so the vault and database land as the single-profile
# commands write them) with compensation on failure; the drain never touches
# busy work unless an operator authorizes exactly the busy set; the preference
# re-places only queued class_only work; one audit event records it all.

STANDARD_HIGH = {
    "standard-high": IntelligenceClass(
        "standard-high",
        "Standard high",
        "",
        {"anthropic": {"model": "claude-opus-5"}, "openai": {"model": "gpt-6"}},
    ),
}


@pytest.fixture
def stops(swarm_on):
    """Record pool teardowns instead of stopping processes: the fleet runs none."""
    orch = swarm_on
    calls: list[dict] = []

    async def terminate(session, *, reason, correlation=None, **kwargs):
        calls.append(
            {"session_id": session.id, "task_id": session.task_id, "reason": reason,
             "correlation": correlation}
        )
        await orch.db.update_session(
            session.id, state="stopped", desired_state="stopped", end_reason=reason
        )

    orch._terminate_pool_session = terminate
    return calls


def _collect(orch, *event_types) -> list[dict]:
    seen: list[dict] = []
    for event_type in event_types:
        orch.bus.subscribe(
            event_type,
            lambda payload, event_type=event_type: seen.append(
                {"type": event_type, **payload}
            ),
        )
    return seen


async def _apply(orch, token, **args):
    return await _handler(orch).execute(
        "provider_allocation_apply", {"preview_token": token, **args}
    )


async def _profile_state(orch, profile_id) -> dict:
    profile = await orch.db.get_profile(profile_id)
    return {
        field: getattr(profile, field, None)
        for field in ("lifecycle", "enabled", "min_active", "max_active", "min_per_project",
                      "max_claims_per_session")
    }


async def _allocation_events(orch) -> list[dict]:
    rows = await orch.db.get_recent_events(event_type="provider.allocation_changed")
    return [json.loads(row["payload"]) for row in rows]


# -- the token --------------------------------------------------------------------


async def test_apply_refuses_a_token_it_never_issued(orch):
    refused = await _apply(orch, "0" * 64)
    assert refused["success"] is False
    assert refused["error_code"] == "preview_unknown"
    missing = await _handler(orch).execute("provider_allocation_apply", {})
    assert missing["success"] is False and "preview_token is required" in missing["error"]


async def test_stale_token_is_refused_with_a_fresh_preview(orch, stops):
    preview = await _preview(
        orch, provider="claude", participation="task", allow_pinned_wait=True
    )
    assert preview["success"] is True, preview
    before = await _profile_state(orch, "standard-high-claude")
    # Anything the preview observed changes: here a bound.
    await orch.db.update_profile("standard-high-claude", max_active=3)

    stale = await _apply(orch, preview["preview_token"])
    assert stale["success"] is False
    assert stale["error_code"] == "preview_stale"
    fresh = stale["preview"]
    assert fresh["preview_token"] != preview["preview_token"]
    assert _rows(fresh)["standard-high-claude"]["before"]["max_active"] == 3
    # Nothing was written: the lifecycle and the sessions are untouched.
    assert (await _profile_state(orch, "standard-high-claude")) == {**before, "max_active": 3}
    assert (await orch.db.get_session("sess-alpha-idle")).desired_state != "stopped"
    assert await _allocation_events(orch) == []
    # The stale token is spent; the fresh one is appliable after review.
    again = await _apply(orch, preview["preview_token"])
    assert again["error_code"] == "preview_unknown"
    applied = await _apply(orch, fresh["preview_token"])
    assert applied["success"] is True, applied
    assert applied["status"] == "applied"
    # A token applies once.
    replay = await _apply(orch, fresh["preview_token"])
    assert replay["error_code"] == "preview_unknown"


async def test_the_applied_set_is_the_previewed_set(orch, stops):
    untouched = {
        pid: await _profile_state(orch, pid)
        for pid in ("deep-high-codex", "astra-high-codex", "standard-high-claude")
    }
    preview = await _preview(
        orch, provider="codex", profile_ids=["fast-low-codex", "standard-high-codex"],
        bounds={"min": 0, "max": 1},
    )
    applied = await _apply(orch, preview["preview_token"])
    assert applied["success"] is True, applied
    planned = [row for row in preview["profiles"] if row["changed"]]
    assert [row["profile_id"] for row in applied["profiles"]] == [
        row["profile_id"] for row in planned
    ] == ["fast-low-codex", "standard-high-codex"]
    for row in planned:
        state = await _profile_state(orch, row["profile_id"])
        assert {field: state[field] for field in row["after"]} == row["after"]
    for pid, state in untouched.items():
        assert await _profile_state(orch, pid) == state
    assert all(row["status"] == "applied" for row in applied["profiles"])


# -- drains -----------------------------------------------------------------------


async def test_graceful_drain_never_interrupts_busy_work_or_admits_a_claim(orch, stops):
    preview = await _preview(orch, provider="claude", participation="task",
                             allow_pinned_wait=True)
    applied = await _apply(orch, preview["preview_token"])
    assert applied["success"] is True, applied
    assert stops == []
    for sid in ("sess-alpha-idle", "sess-alpha-busy", "sess-bravo-start"):
        session = await orch.db.get_session(sid)
        assert session.desired_state == "stopped", sid
        assert session.state != "stopped", sid
        # A stopped pool admits no further claim.
        refusal = _handler(orch)._claim_precondition_refusal(
            session, {"project_id": session.project_id}, None, {"next": True}
        )
        assert refusal is not None and refusal["result"] == "drain_requested", sid
    busy = await orch.db.get_task("task-alpha-busy")
    assert busy.status == TaskStatus.IN_PROGRESS and busy.assigned_agent_id == "ag-pool-2"
    drained = {row["session_id"]: row["action"] for row in applied["session_actions"]}
    assert drained == {"sess-alpha-idle": "drain", "sess-alpha-busy": "drain",
                       "sess-bravo-start": "drain"}


async def test_idle_now_stops_idle_workers_now_and_lets_busy_work_finish(orch, stops):
    preview = await _preview(orch, provider="claude", participation="task",
                             drain="idle-now", allow_pinned_wait=True)
    applied = await _apply(orch, preview["preview_token"])
    assert applied["success"] is True, applied
    assert [(call["session_id"], call["reason"]) for call in stops] == [
        ("sess-alpha-idle", "allocation_idle_now")
    ]
    assert stops[0]["correlation"] == {"request_id": applied["request_id"]}
    busy = await orch.db.get_session("sess-alpha-busy")
    assert busy.desired_state == "stopped" and busy.state == "running"
    assert (await orch.db.get_task("task-alpha-busy")).status == TaskStatus.IN_PROGRESS


async def test_lowered_bounds_stop_only_idle_excess_and_only_when_asked(orch, stops):
    graceful = await _preview(orch, provider="claude", profile_ids=["standard-high-claude"],
                              bounds={"min": 0, "max": 1})
    assert (await _apply(orch, graceful["preview_token"]))["success"] is True
    assert stops == []
    await orch.db.update_profile("standard-high-claude", max_active=2)
    idle_now = await _preview(orch, provider="claude", profile_ids=["standard-high-claude"],
                              bounds={"min": 0, "max": 1}, drain="idle-now")
    applied = await _apply(orch, idle_now["preview_token"])
    assert applied["success"] is True, applied
    # ``aq pool scale --now``'s victims, with the allocation request id on them.
    assert [(call["session_id"], call["reason"]) for call in stops] == [
        ("sess-alpha-idle", "scaled")
    ]
    assert stops[0]["correlation"] == {"request_id": applied["request_id"]}


async def test_interrupt_busy_needs_operator_scope_and_exactly_the_busy_set(orch, stops):
    preview = await _preview(orch, provider="claude", participation="task",
                             drain="interrupt-busy", allow_pinned_wait=True)
    token = preview["preview_token"]
    assert preview["busy"] == {"session_ids": ["sess-alpha-busy"],
                               "task_ids": ["task-alpha-busy"]}
    before = await _profile_state(orch, "standard-high-claude")

    # A project admin may not apply an operator's allocation.
    refused = await _apply(orch, token, authorize_busy_interrupt=["sess-alpha-busy"],
                           _scope=_alpha_supervisor())
    assert refused["success"] is False and refused["error"].startswith("out of scope")

    missing = await _apply(orch, token)
    assert missing["error_code"] == "busy_authorization_required"
    assert "sess-alpha-busy" in missing["error"]
    for authorized in (["sess-alpha-idle"], ["sess-alpha-busy", "sess-bravo-busy"], ["nope"]):
        wrong = await _apply(orch, token, authorize_busy_interrupt=authorized)
        assert wrong["error_code"] == "busy_authorization_mismatch", authorized
    # Nothing moved while authorization was refused.
    assert await _profile_state(orch, "standard-high-claude") == before
    assert stops == []

    # The task id names the session that runs it; the set is exact.
    applied = await _apply(orch, token, authorize_busy_interrupt=["task-alpha-busy"])
    assert applied["success"] is True, applied
    assert sorted((call["session_id"], call["reason"]) for call in stops) == [
        ("sess-alpha-busy", "allocation_interrupt"),
        ("sess-alpha-idle", "allocation_idle_now"),
    ]
    actions = {(row["session_id"], row["action"]) for row in applied["session_actions"]}
    assert ("sess-alpha-busy", "interrupt") in actions


async def test_authorizing_an_interrupt_without_interrupt_busy_is_refused(orch, stops):
    preview = await _preview(orch, provider="claude", participation="task",
                             allow_pinned_wait=True)
    refused = await _apply(orch, preview["preview_token"],
                           authorize_busy_interrupt=["sess-alpha-busy"])
    assert refused["error_code"] == "busy_authorization_unexpected"


async def test_pinned_ready_wait_needs_acknowledgement_at_preview_or_apply(orch, stops):
    preview = await _preview(orch, provider="claude", participation="task")
    assert preview["blocked"] is True
    refused = await _apply(orch, preview["preview_token"])
    assert refused["error_code"] == "pinned_wait_unacknowledged"
    assert refused["preview"]["preview_token"] == preview["preview_token"]
    applied = await _apply(orch, preview["preview_token"], allow_pinned_wait=True)
    assert applied["success"] is True, applied
    # The pin is left exactly where it was, and the audit names it.
    pin = await orch.db.get_task("task-alpha-pin")
    assert pin.profile_id == "standard-high-claude" and pin.status == TaskStatus.READY
    assert {row["task_id"] for row in applied["pinned"] if row["waits"]} == {"task-alpha-pin"}


# -- parity with the single-profile commands ---------------------------------------


async def _twins(orch) -> None:
    """Two identical Codex pool profiles, vault-backed as the shipped profiles are."""
    from src.profiles.sync import sync_profile_text_to_db

    for twin in ("twin-a", "twin-b"):
        path = orch.config.data_dir + f"/vault/agent-types/{twin}/profile.md"
        markdown = (
            f"---\nid: {twin}\nname: {twin}\n---\n## Role\nWork.\n## Config\n```json\n"
            '{"harness": "codex", "default_class": "standard-high", "lifecycle": "pool", '
            '"min_active": 1, "max_active": 3, "max_claims_per_session": 2}\n```\n'
        )
        import pathlib

        pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
        pathlib.Path(path).write_text(markdown, encoding="utf-8")
        synced = await sync_profile_text_to_db(markdown, orch.db, source_path=path)
        assert synced.success, synced.errors


def _vault_text(orch, profile_id) -> str:
    import pathlib

    return pathlib.Path(
        orch.config.data_dir + f"/vault/agent-types/{profile_id}/profile.md"
    ).read_text(encoding="utf-8")


def _vault_config(orch, profile_id) -> dict:
    """The ``## Config`` JSON of a vault profile: its meaning, not its formatting."""
    text = _vault_text(orch, profile_id)
    block = text.split("## Config", 1)[1].split("```json", 1)[1].split("```", 1)[0]
    return json.loads(block)


def _without_request_id(events: list[dict], profile_id: str) -> list[dict]:
    return [
        {key: value for key, value in event.items()
         if key not in {"request_id", "event_id", "_event_type", "profile_id"}}
        for event in events
        if event.get("profile_id") == profile_id
    ]


@pytest.mark.parametrize(
    ("request_fields", "command", "command_args"),
    [
        ({"bounds": {"min": 0, "max": 1}}, "pool_scale", {"min": 0, "max": 1}),
        ({"bounds": {"max": None}}, "pool_scale", {"max": None}),
        ({"participation": "task"}, "pool_set_lifecycle", {"lifecycle": "task"}),
    ],
)
async def test_apply_writes_what_the_single_profile_commands_write(orch, stops, request_fields, command, command_args
):
    await _twins(orch)
    events = _collect(orch, "pool.bounds_changed", "pool.lifecycle_changed")
    preview = await _preview(orch, provider="codex", profile_ids=["twin-a"], **request_fields)
    applied = await _apply(orch, preview["preview_token"])
    assert applied["success"] is True, applied
    single = await _handler(orch).execute(command, {"profile_id": "twin-b", **command_args})
    assert single["success"] is True, single

    assert await _profile_state(orch, "twin-a") == await _profile_state(orch, "twin-b")
    assert _vault_text(orch, "twin-a").replace("twin-a", "twin-b") == _vault_text(
        orch, "twin-b"
    )
    assert _without_request_id(events, "twin-a") == _without_request_id(events, "twin-b")
    # Every underlying pool event carries the allocation request id.
    assert {event.get("request_id") for event in events if event["profile_id"] == "twin-a"} == {
        applied["request_id"]
    }


# -- partial failure --------------------------------------------------------------


async def test_a_later_failure_is_compensated_and_reported(orch, stops, monkeypatch):
    from src.commands import pool_admin

    await _twins(orch)
    original = {pid: await _profile_state(orch, pid) for pid in ("twin-a", "twin-b")}
    vault = _vault_config(orch, "twin-a")
    real = pool_admin.set_pool_bounds

    async def flaky(handler, args, *, correlation=None):
        if args["profile_id"] == "twin-b":
            return {"success": False, "error": "vault is read-only"}
        return await real(handler, args, correlation=correlation)

    monkeypatch.setattr(pool_admin, "set_pool_bounds", flaky)
    preview = await _preview(
        orch, provider="codex", profile_ids=["twin-a", "twin-b"], bounds={"min": 0, "max": 1},
    )
    result = await _apply(orch, preview["preview_token"])
    assert result["success"] is False
    assert result["status"] == "rolled_back"
    assert result["error_code"] == "allocation_rolled_back"
    assert "twin-b: vault is read-only" in result["error"]
    rows = {row["profile_id"]: row for row in result["profiles"]}
    assert rows["twin-a"]["status"] == "rolled_back"
    assert rows["twin-a"]["compensated"] is True
    assert rows["twin-b"]["status"] == "failed"
    for pid, state in original.items():
        assert await _profile_state(orch, pid) == state, pid
    assert _vault_config(orch, "twin-a") == vault
    event = (await _allocation_events(orch))[0]
    assert event["status"] == "rolled_back"
    assert {row["profile_id"]: row["status"] for row in event["profiles"]} == {
        "twin-a": "rolled_back", "twin-b": "failed",
    }


async def test_a_failed_compensation_is_partial_never_success(orch, stops, monkeypatch):
    from src.commands import pool_admin

    real = pool_admin.set_pool_bounds

    async def flaky(handler, args, *, correlation=None):
        if args["profile_id"] == "twin-b":
            raise RuntimeError("database went away")
        return await real(handler, args, correlation=correlation)

    async def refuse(handler, before, *, correlation=None):
        return {"success": False, "error": "still read-only"}

    await _twins(orch)
    monkeypatch.setattr(pool_admin, "set_pool_bounds", flaky)
    monkeypatch.setattr(pool_admin, "restore_pool_profile", refuse)
    preview = await _preview(
        orch, provider="codex", profile_ids=["twin-a", "twin-b"], bounds={"min": 0, "max": 1},
    )
    result = await _apply(orch, preview["preview_token"])
    assert result["success"] is False and result["status"] == "partial"
    assert result["error_code"] == "allocation_partial"
    rows = {row["profile_id"]: row for row in result["profiles"]}
    assert rows["twin-a"]["status"] == "rollback_failed"
    assert rows["twin-a"]["compensation_error"] == "still read-only"
    assert "RuntimeError: database went away" in rows["twin-b"]["error"]
    # What did land is on the profile, and the report says so.
    assert (await _profile_state(orch, "twin-a"))["max_active"] == 1


async def test_a_lifecycle_change_is_restored_with_every_pool_key(orch, stops, monkeypatch):
    from src.commands import pool_admin

    await _twins(orch)
    before = await _profile_state(orch, "twin-a")
    vault = _vault_config(orch, "twin-a")
    real = pool_admin.set_pool_lifecycle

    async def flaky(handler, args, *, correlation=None):
        if args["profile_id"] == "twin-b":
            return {"success": False, "error": "boom"}
        return await real(handler, args, correlation=correlation)

    monkeypatch.setattr(pool_admin, "set_pool_lifecycle", flaky)
    preview = await _preview(orch, provider="codex", profile_ids=["twin-a", "twin-b"],
                             participation="task")
    result = await _apply(orch, preview["preview_token"])
    assert result["status"] == "rolled_back", result
    # min/max, the per-session claim cap: everything ``task`` cleared comes back.
    assert await _profile_state(orch, "twin-a") == before
    assert before["max_claims_per_session"] == 2
    assert {key: _vault_config(orch, "twin-a").get(key) for key in vault} == vault


# -- the project preference -------------------------------------------------------


async def test_preference_returns_queued_class_only_router_routes_to_the_router(orch, stops):
    """Mandatory routing §6.8: the preference is a router input, so re-placing
    queued work resets it to ``unrouted`` instead of moving it to a rung."""
    orch.session_spec_builder._intelligence_classes = dict(STANDARD_HIGH)
    constraints = {"constraints": {"exclude_providers": ["gemini"]}}
    for task_id, project_id, status, intent, source in (
        ("task-alpha-free", ALPHA, TaskStatus.READY, CLASS_ONLY, "router"),
        ("task-alpha-defined", ALPHA, TaskStatus.DEFINED, CLASS_ONLY, "router"),
        ("task-alpha-free-busy", ALPHA, TaskStatus.IN_PROGRESS, CLASS_ONLY, "router"),
        ("task-alpha-legacy", ALPHA, TaskStatus.READY, CLASS_ONLY, "legacy"),
        ("task-alpha-override", ALPHA, TaskStatus.READY, CLASS_ONLY, "override"),
    ):
        await orch.db.create_task(
            Task(id=task_id, project_id=project_id, title=task_id, description="d",
                 status=status, profile_id="standard-high-claude", route_source=source,
                 intelligence_class="standard-high", provider_intent=intent,
                 route=constraints)
        )
    before = {
        task_id: (await orch.db.get_task(task_id)).profile_id
        for task_id in ("task-alpha-pin", "task-alpha-pref", "task-alpha-free-busy",
                        "task-bravo-free", "task-alpha-busy", "task-alpha-legacy",
                        "task-alpha-override")
    }
    preview = await _preview(orch, provider="codex",
                             receive_new_work={"project_id": ALPHA, "mode": "prefer"})
    applied = await _apply(orch, preview["preview_token"])
    assert applied["success"] is True, applied
    assert (await orch.db.get_project(ALPHA)).preferred_provider == "codex"
    assert (await orch.db.get_project(BRAVO)).preferred_provider is None
    placement = applied["preference"]["placement"]
    assert sorted(row["task_id"] for row in placement["moved"]) == [
        "task-alpha-defined", "task-alpha-free",
    ]
    assert {row["kind"] for row in placement["moved"]} == {"returned_to_router"}
    assert placement["batch_ids"] == [] and placement["held"] == []
    for task_id in ("task-alpha-free", "task-alpha-defined"):
        task = await orch.db.get_task(task_id)
        assert (task.profile_id, task.route_source, task.intelligence_class) == (
            None, "unrouted", None,
        )
        assert task.route == constraints  # the router re-plans under them
    # Pins, preferred intent, other sources and projects, running work stay put.
    for task_id, profile_id in before.items():
        assert (await orch.db.get_task(task_id)).profile_id == profile_id, task_id
    # No profile and no session changed.
    assert applied["profiles"] == [] and stops == []

    cleared = await _preview(orch, provider="codex",
                             receive_new_work={"project_id": ALPHA, "mode": "clear"})
    applied = await _apply(orch, cleared["preview_token"])
    assert applied["success"] is True, applied
    assert (await orch.db.get_project(ALPHA)).preferred_provider is None
    assert "placement" not in applied["preference"]


async def test_preference_leaves_pins_and_routes_already_on_the_provider(orch, stops):
    orch.session_spec_builder._intelligence_classes = dict(STANDARD_HIGH)
    for task_id, profile_id, intent in (
        ("task-alpha-on-codex", "standard-high-codex", CLASS_ONLY),
        ("task-alpha-lane", "standard-high-claude", PINNED),
    ):
        await orch.db.create_task(
            Task(id=task_id, project_id=ALPHA, title="t", description="d",
                 status=TaskStatus.READY, profile_id=profile_id, route_source="router",
                 intelligence_class="standard-high", provider_intent=intent)
        )
    preview = await _preview(orch, provider="codex",
                             receive_new_work={"project_id": ALPHA, "mode": "prefer"})
    applied = await _apply(orch, preview["preview_token"])
    assert applied["success"] is True, applied
    placement = applied["preference"]["placement"]
    assert placement["moved"] == []
    assert [(row["task_id"], row["kind"]) for row in placement["skipped"]] == [
        ("task-alpha-lane", "provider_intent")
    ]
    assert (await orch.db.get_task("task-alpha-on-codex")).profile_id == "standard-high-codex"
    assert (await orch.db.get_task("task-alpha-lane")).profile_id == "standard-high-claude"


async def test_a_failed_move_leaves_the_preference_and_reports_partial(orch, stops,
                                                                      monkeypatch):
    orch.session_spec_builder._intelligence_classes = dict(STANDARD_HIGH)
    await orch.db.create_task(
        Task(id="task-alpha-free", project_id=ALPHA, title="t", description="d",
             status=TaskStatus.READY, profile_id="standard-high-claude", route_source="router",
             intelligence_class="standard-high", provider_intent=CLASS_ONLY)
    )

    async def broken(*args, **kwargs):
        raise RuntimeError("routing write poisoned")

    monkeypatch.setattr(orch.db, "reset_task_route", broken)
    preview = await _preview(orch, provider="codex",
                             receive_new_work={"project_id": ALPHA, "mode": "prefer"})
    result = await _apply(orch, preview["preview_token"])
    assert result["success"] is False and result["status"] == "partial"
    assert "routing write poisoned" in result["error"]
    assert result["preference"]["applied"] is True
    assert (await orch.db.get_project(ALPHA)).preferred_provider == "codex"
    assert (await orch.db.get_task("task-alpha-free")).profile_id == "standard-high-claude"
    assert (await _allocation_events(orch))[0]["status"] == "partial"


async def test_project_admin_applies_its_own_preference(orch):
    preview = await _preview(
        orch, provider="codex", receive_new_work={"project_id": ALPHA, "mode": "prefer"},
        _scope=_alpha_supervisor(),
    )
    applied = await _apply(orch, preview["preview_token"], _scope=_alpha_supervisor())
    assert applied["success"] is True, applied
    assert applied["actor"] == "session:sup-alpha"
    assert (await orch.db.get_project(ALPHA)).preferred_provider == "codex"


async def test_worker_token_cannot_apply(orch):
    preview = await _preview(
        orch, provider="codex", receive_new_work={"project_id": ALPHA, "mode": "prefer"}
    )
    refused = await _apply(orch, preview["preview_token"], _scope=_worker_scope())
    assert refused["success"] is False and refused["error"].startswith("out of scope")
    assert "preview" not in refused
    worker = RequestScope(kind="session", session_id="s1", task_id="t1", project_id=ALPHA)
    assert "provider_allocation_apply" not in AGENT_COMMAND_SET
    assert check_command_scope("provider_allocation_apply", {}, worker) == (
        "out of scope: provider_allocation_apply"
    )


# -- audit ------------------------------------------------------------------------


async def test_one_audit_event_carries_actor_and_before_and_after(orch, stops):
    from src.event_schemas import validate_payload

    seen = _collect(orch, "provider.allocation_changed", "pool.lifecycle_changed",
                    "pool.session_drained")
    preview = await _preview(orch, provider="claude", profile_ids=["standard-high-claude"],
                             participation="task", drain="idle-now", allow_pinned_wait=True)
    applied = await _apply(orch, preview["preview_token"])
    assert applied["success"] is True, applied

    events = await _allocation_events(orch)
    assert len(events) == 1
    event = events[0]
    assert event["actor"] == "human:local-operator"
    assert event["request_id"] == applied["request_id"]
    assert event["preview_token"] == preview["preview_token"]
    assert event["status"] == "applied"
    [row] = event["profiles"]
    assert row["profile_id"] == "standard-high-claude" and row["status"] == "applied"
    assert row["before"]["lifecycle"] == "pool" and row["after"]["lifecycle"] == "task"
    assert row["before"]["max_active"] == 2 and row["after"]["max_active"] is None
    assert {item["task_id"] for item in event["pinned"]} == {"task-alpha-busy", "task-alpha-pin"}
    assert {item["agent_id"] for item in event["manual_agents"]} == {
        "ag-manual-override", "ag-manual-plain",
    }
    assert {item["session_id"] for item in event["session_actions"]} == {
        "sess-alpha-idle", "sess-alpha-busy", "sess-bravo-start",
    }
    assert validate_payload("provider.allocation_changed", event) == []
    # The bus saw the same event once, and the underlying ones carry its request id.
    assert [item["type"] for item in seen].count("provider.allocation_changed") == 1
    assert {
        item["request_id"] for item in seen if item["type"] == "pool.lifecycle_changed"
    } == {applied["request_id"]}

    status = await _handler(orch).execute("provider_allocation_status", {"provider": "claude"})
    last = _providers(status)["claude"]["last_allocation"]
    assert last["request_id"] == applied["request_id"]
    assert last["status"] == "applied" and last["actor"] == "human:local-operator"


# -- API --------------------------------------------------------------------------


async def test_api_applies_and_returns_refusals_with_their_data(orch, stops):
    handler = _handler(orch)
    first = await handler.execute(
        "provider_allocation_preview",
        {"provider": "claude", "profile_ids": ["standard-high-claude"],
         "bounds": {"min": 0, "max": 1}},
    )
    await orch.db.update_profile("standard-high-claude", max_active=3)
    async with _client(orch) as client:
        # ``_client`` builds its own handler; the registry is the daemon's, so
        # a token issued through one handler applies through another.
        stale = await client.post(
            "/api/providers/allocation/apply", json={"preview_token": first["preview_token"]}
        )
        assert stale.status_code == 409, stale.text
        body = stale.json()
        assert body["error_code"] == "preview_stale"
        fresh = body["preview"]["preview_token"]
        applied = await client.post(
            "/api/providers/allocation/apply", json={"preview_token": fresh}
        )
        replay = await client.post(
            "/api/providers/allocation/apply", json={"preview_token": fresh}
        )
    assert applied.status_code == 200, applied.text
    assert applied.json()["status"] == "applied"
    assert replay.status_code == 409 and replay.json()["error_code"] == "preview_unknown"

