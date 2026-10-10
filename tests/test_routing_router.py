"""``task_route_plan`` and ``task_route_apply`` through a real handler.

Spec: ``projects/agent-queue/specs/2026-09-28-mandatory-task-routing.md``
§6.1-§6.6.  The planner itself is covered without I/O in
``tests/test_routing_planner.py``; this file covers what needs the
database: the snapshot the command builds, the bound-router refusal, the
advisory lock and the re-selection a burst of applies makes on fresh load.
"""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.commands.contracts import CONTRACTS
from src.commands.routing_commands import NOT_ROUTER
from src.config import AppConfig, DatabaseConfig, DiscordConfig
from src.database import Database
from src.intelligence_classes import IntelligenceClass
from src.models import (
    Agent,
    AgentProfile,
    Project,
    RepoSourceType,
    SessionRecord,
    Task,
    TaskStatus,
    TaskType,
    Workspace,
)
from src.orchestrator import Orchestrator
from src.playbooks.artifact_ref import ArtifactRef
from src.playbooks.definition import granted_aq_commands
from src.playbooks.invocation import _invocation_context, current_invocation
from src.routing.sources import LEGACY, ROUTER, UNROUTED
from src.sessions.harness_parser import Harness
from tests.assignment_routing_helpers import route_source_for
from tests.db_fixtures import lease_dsn
from tests.test_routing_planner import NARROW_NO, SHIPPED_POLICY, routine_policy

ROUTER_ID = "default-assignment-routing"
CLASSES = {
    class_id: IntelligenceClass(class_id, class_id, "", {
        "anthropic": {"model": f"claude-{class_id}"},
        "codex": {"model": f"gpt-{class_id}"},
    })
    for class_id in ("standard-high", "deep-high")
}


def _profiles() -> list[AgentProfile]:
    return [
        AgentProfile(id="standard-high-claude", name="", lifecycle="pool", harness="claude",
                     default_class="standard-high", max_active=2),
        AgentProfile(id="standard-high-codex", name="", lifecycle="pool", harness="codex",
                     default_class="standard-high", max_active=2),
        AgentProfile(id="deep-high-claude", name="", lifecycle="pool", harness="claude",
                     default_class="deep-high", max_active=1),
        AgentProfile(id="deep-high-codex", name="", lifecycle="pool", harness="codex",
                     default_class="deep-high", max_active=1),
        AgentProfile(id="reviewer", name="", harness="claude", default_class="deep-high"),
    ]


@pytest.fixture
async def orch(tmp_path):
    db = Database(lease_dsn("router.db"))
    await db.initialize()
    for profile in _profiles():
        await db.create_profile(profile)
    await db.create_project(Project(id="p", name="Project"))
    cfg = AppConfig(
        discord=DiscordConfig(bot_token="test", guild_id="1"),
        workspace_dir=str(tmp_path / "work"), data_dir=str(tmp_path / "data"),
        database=DatabaseConfig(url=lease_dsn("unused.db")),
    )
    cfg.sessions.enabled = True
    cfg.sessions.provider = "fake"
    cfg.swarm.enabled = True
    orchestrator = Orchestrator(cfg)
    orchestrator.db = db
    orchestrator.git = MagicMock()
    orchestrator.bus.emit = AsyncMock()
    for harness in ("claude", "codex"):
        orchestrator.harness_registry.upsert(Harness(
            id=harness, name=harness, command=harness, model_flag="--model",
        ))
    orchestrator.session_spec_builder._intelligence_classes = dict(CLASSES)
    orchestrator.intelligence_classes.replace(dict(CLASSES))
    yield orchestrator
    await db.close()


@pytest.fixture
def handler(orch):
    from src.commands.handler import CommandHandler

    return CommandHandler(orch, orch.config)


async def _create(db, task_id: str, **kw) -> Task:
    kw.setdefault("status", TaskStatus.READY)
    kw.setdefault("route_source", route_source_for(kw.get("profile_id")))
    await db.create_task(Task(id=task_id, project_id="p", title=task_id, description="d", **kw))
    return await db.get_task(task_id)


def _ref(playbook_id: str) -> ArtifactRef:
    return ArtifactRef(
        playbook_id=playbook_id,
        artifact_sha256="sha256:" + "a" * 64,
        schema_generation=2,
        contract_fingerprint="sha256:" + "b" * 64,
        source_digest="sha256:" + "c" * 64,
        compiler_build="test",
    )


@contextmanager
def _as_playbook(playbook_id: str = ROUTER_ID, commands=("task_route_plan", "task_route_apply")):
    """Run the body as a live command step of *playbook_id*'s artifact."""
    artifact = SimpleNamespace(steps={
        f"step_{index}": SimpleNamespace(command=command, tool_use=None)
        for index, command in enumerate(commands)
    })
    ctx = SimpleNamespace(
        run_id="run-1", dispatch_id="dispatch-1", artifact_ref=_ref(playbook_id),
        artifact=artifact, rule_id="route-task", step_id="apply_a", attempt=1,
    )
    with _invocation_context(ctx) as invocation:
        yield invocation


async def _plan(handler, task_id: str, classification=None) -> dict:
    args = {"task_id": task_id, "policy": SHIPPED_POLICY}
    if classification is not None:
        args["classification"] = classification
    return await handler.execute("task_route_plan", args)


# -- task_route_plan -------------------------------------------------------------


async def test_plan_reads_the_live_catalog_and_writes_nothing(handler, orch):
    await _create(orch.db, "t", task_type=TaskType.RESEARCH)
    plan = await _plan(handler, "t")
    assert plan["success"] and plan["outcome"] == "planned", plan
    assert plan["profile_id"] == "standard-high-codex"  # tie_order: Codex first
    assert [c["profile_id"] for c in plan["candidates"]] == [
        "standard-high-codex", "standard-high-claude",
    ]
    assert plan["policy_sha256"].startswith("sha256:")
    task = await orch.db.get_task("t")
    assert (task.profile_id, task.route_source) == (None, UNROUTED)


async def test_system_filing_without_ordinary_input_keeps_its_routing_origin(handler, orch):
    await _create(orch.db, "repair-looking", task_type=TaskType.BUGFIX,
                  created_by_kind="system")
    task = await orch.db.get_task("repair-looking")
    facts = await handler._routing_task_facts(task, await orch.db.get_project("p"))
    assert facts.created_by_kind == "system"


@pytest.mark.parametrize("task_type", [TaskType.TEST, TaskType.BUGFIX])
async def test_integration_writers_route_like_repairs_off_every_opencode_lane(
    handler, orch, task_type,
):
    from tests.test_routing_planner import NARROW_YES, _rung, _snapshot

    await _create(orch.db, "writer", task_type=task_type,
                  created_by_kind="integration_writer", created_by_id="subject-1")
    task = await orch.db.get_task("writer")
    project = await orch.db.get_project("p")
    facts = await handler._routing_task_facts(task, project)
    assert facts.created_by_kind == "integration_repair"
    fleet = [
        _rung("standard-high", harness)
        for harness in ("codex", "claude", "opencode", "opencode-zen",
                        "opencode-zen-nemotron", "opencode-zen-new-preview")
    ]
    observed = await handler._routing_snapshot(task, project, ("standard-high",))
    snapshot = _snapshot(fleet, busy={
        "standard-high-codex": 2, "standard-high-claude": 2,
    })
    handler._routing_snapshot = AsyncMock(return_value=replace(snapshot, context=observed.context))
    plan = await _plan(handler, "writer", {**NARROW_YES, "task_type": task_type.value})
    assert plan["success"] and plan["outcome"] == "planned", plan
    assert plan["rule"] == f"kinds.{task_type.value}+origins.integration_repair"
    assert {candidate["harness"] for candidate in plan["candidates"]} == {"codex", "claude"}
    assert (await orch.db.get_task("writer")).created_by_kind == "integration_writer"


@pytest.mark.parametrize("stamped", [True, False], ids=["stamped", "record-only"])
async def test_a_source_ci_repair_routes_as_an_integration_repair(handler, orch, stamped):
    from sqlalchemy import insert

    from src.commands.integration_commands import SOURCE_CI_REPAIR_ORIGIN
    from src.database.tables import integration_source_ci
    from src.models import RepoConfig, RepoSourceType

    await _create(orch.db, "source", task_type=TaskType.FEATURE)
    await _create(orch.db, "ci-fix", task_type=TaskType.BUGFIX,
                  created_by_kind=SOURCE_CI_REPAIR_ORIGIN if stamped else None)
    await _create(orch.db, "plain-fix", task_type=TaskType.BUGFIX)
    # Every repair carries its record; one filed before the origin was stamped
    # is known only by it.
    await orch.db.create_repo(RepoConfig(
        id="repo", project_id="p", source_type=RepoSourceType.CLONE, url="/private/repo.git",
    ))
    async with orch.db.immediate() as conn:
        await conn.execute(insert(integration_source_ci).values(
            task_id="source", repository_id="repo", source_base="a" * 40,
            source_head="b" * 40, generation=0, policy_generation=0, state="red",
            evidence={}, repair_task_id="ci-fix", observed_at=1000.0,
        ))

    project = await orch.db.get_project("p")
    facts = await handler._routing_task_facts(await orch.db.get_task("ci-fix"), project)
    assert facts.created_by_kind == "integration_repair"
    plan = await _plan(handler, "ci-fix")
    assert plan["success"] and plan["outcome"] == "planned", plan
    # ``origins.integration_repair`` is ``narrow: false``: no OpenCode lane at all.
    assert plan["rule"] == "kinds.bugfix+origins.integration_repair"

    facts = await handler._routing_task_facts(await orch.db.get_task("plain-fix"), project)
    assert facts.created_by_kind is None


def _context_profile(context, profile_id="standard-high-codex"):
    return next(p for p in context["profiles"] if p["profile_id"] == profile_id)


async def _session(db, session_id, **kw):
    kw.setdefault("state", "running")
    kw.setdefault("started_at", time.time())
    await db.create_session(SessionRecord(
        id=session_id, project_id="p", profile_id="standard-high-codex",
        harness="codex", provider="fake", name=session_id, lifecycle="pool",
        work_dir="/private/work", epoch="test", instance_token="secret-session-token", **kw,
    ))


async def _workspace(orch):
    await orch.db.create_workspace(Workspace(
        id="base", project_id="p", workspace_path="/private/repo",
        source_type=RepoSourceType.CLONE, kind_id="project-repo",
    ))


@pytest.mark.parametrize("state,extra,bucket", [
    ("running", {}, "idle"),
    ("running", {"claim_phase": "preparing"}, "busy"),
    ("starting", {}, "starting"),
    ("draining", {}, "draining"),
    ("running", {"desired_state": "stopped"}, "draining"),
    ("running", {"started_at": 1}, "unresponsive"),
])
async def test_context_counts_supply_without_advertising_starting_or_draining_as_idle(
    handler, orch, state, extra, bucket,
):
    await _create(orch.db, "t", task_type=TaskType.RESEARCH)
    await _workspace(orch)
    await _session(orch.db, "s", state=state, **extra)
    result = await _plan(handler, "t")
    context = result["live_context"]
    profile = _context_profile(context)
    assert profile[bucket] == 1
    assert sum(profile[k] for k in ("idle", "busy", "starting", "draining", "unresponsive")) == 1
    assert profile["idle_claim_capacity"] == (1 if bucket == "idle" else 0)
    assert profile["launch_headroom"] <= 1
    assert context["observational"] is True
    assert context["as_of"] >= context["collected_from"]
    assert context["source"] == "routing_snapshot"
    assert "secret-session-token" not in json.dumps(context)
    assert "/private/" not in json.dumps(context)
    assert "no reservations" in result["live_summary"]


@pytest.mark.parametrize("constraint", ["profile", "project", "global", "workspace", "quarantine"])
async def test_context_headroom_obeys_admission_constraints_and_routes_queued_work(
    handler, orch, constraint,
):
    await _create(orch.db, "t", task_type=TaskType.RESEARCH)
    if constraint != "workspace":
        await _workspace(orch)
    if constraint == "profile":
        await _session(orch.db, "s1", claim_phase="active")
        await _session(orch.db, "s2", claim_phase="preparing")
    elif constraint == "project":
        await orch.db.update_project("p", max_concurrent_agents=1)
        await _session(orch.db, "s1", claim_phase="active")
    elif constraint == "global":
        orch.config.swarm.global_max_active = 1
        await _session(orch.db, "s1", claim_phase="active")
    elif constraint == "quarantine":
        orch._quarantine_pool("p", "standard-high-codex", "secret captured stderr")
    result = await _plan(handler, "t")
    assert result["outcome"] == "planned"
    profile = _context_profile(result["live_context"])
    assert profile["launch_headroom"] == profile["effective_headroom"] == 0
    assert "secret captured stderr" not in json.dumps(result["live_context"])


async def test_local_idle_can_claim_at_project_cap_without_a_free_workspace(handler, orch):
    await _create(orch.db, "t", task_type=TaskType.RESEARCH)
    await orch.db.update_project("p", max_concurrent_agents=1)
    await _session(orch.db, "idle")
    context = (await _plan(handler, "t"))["live_context"]
    profile = _context_profile(context)
    assert profile["launch_headroom"] == 0
    assert profile["idle_claim_capacity"] == profile["effective_headroom"] == 1
    # Another routed task consumes that same observation; it is not free twice.
    await _create(orch.db, "queued", profile_id="standard-high-codex")
    profile = _context_profile((await _plan(handler, "t"))["live_context"])
    assert profile["routed_backlog"] == 1 and profile["effective_headroom"] == 0


async def test_context_counts_pending_launch_once_and_subtracts_workspace_need(handler, orch):
    from src.orchestrator.pools import PoolLaunch

    await _create(orch.db, "t", task_type=TaskType.RESEARCH)
    await _workspace(orch)
    launch = PoolLaunch(project_id="p", profile_id="standard-high-codex", session_id="launch")
    orch._pool_launches = {"launch": launch}
    context = (await _plan(handler, "t"))["live_context"]
    assert _context_profile(context)["starting"] == 1
    assert context["project"]["workspace_capacity"] == orch._project_slot_cap(
        await orch.db.get_project("p")
    ) - 1
    await _session(orch.db, "launch", state="starting")
    context = (await _plan(handler, "t"))["live_context"]
    assert _context_profile(context)["starting"] == 1
    assert context["fleet"]["live_pool_workers"] == 1


async def test_context_counts_task_worker_reservation_before_its_session_exists(handler, orch):
    await orch.db.create_profile(AgentProfile(
        id="push-worker", name="push", harness="codex", lifecycle="task",
        default_class="standard-high",
    ))
    await _create(orch.db, "t", task_type=TaskType.RESEARCH)
    await _create(orch.db, "reserved", profile_id="push-worker", status=TaskStatus.ASSIGNED)
    await orch.db.create_agent(Agent(
        id="reserved-agent", name="worker", profile_id="push-worker", current_task_id="reserved",
    ))
    context = (await _plan(handler, "t"))["live_context"]
    profile = _context_profile(context, "push-worker")
    assert profile["busy"] == 1 and profile["effective_headroom"] == 0
    assert context["project"]["live_workers"] == 1
    # Once durable, count it through the session, not again through the reservation.
    await orch.db.create_session(SessionRecord(
        id="push-session", project_id="p", profile_id="push-worker", harness="codex",
        provider="fake", name="push", lifecycle="task", work_dir="/tmp/push", epoch="test",
        instance_token="test", started_at=time.time(), state="starting",
        agent_id="reserved-agent", task_id="reserved",
    ))
    context = (await _plan(handler, "t"))["live_context"]
    profile = _context_profile(context, "push-worker")
    assert profile["starting"] == 1 and profile["busy"] == 0
    assert context["project"]["live_workers"] == 1


async def test_pool_vault_requirement_does_not_advertise_a_missing_repo_seat(handler, orch):
    await _create(orch.db, "t", task_type=TaskType.RESEARCH)
    await orch.db.add_task_workspace_requirements("t", [("vault", None)])
    await orch.db.create_workspace(Workspace(
        id="vault", project_id="p", workspace_path="/tmp/vault", kind_id="vault",
        source_type=RepoSourceType.LINK,
    ))
    context = (await _plan(handler, "t"))["live_context"]
    assert context["project"]["workspace_capacity"] == 0
    assert _context_profile(context)["launch_headroom"] == 0


async def test_context_preserves_quota_windows_and_unknown_stale_or_reset_readings(handler, orch):
    from src.providers import ProviderUsageSnapshot

    await _create(orch.db, "t", task_type=TaskType.RESEARCH)
    now = time.time()
    for window, used, age, reset in [
        ("5h", 20, 10, now + 100), ("weekly", 90, 20, now + 1000),
        ("stale", 100, 20000, now + 1000), ("reset", 100, 20, now - 1),
    ]:
        await orch.db.record_provider_usage([ProviderUsageSnapshot(
            provider="codex", window=window, used_percent=used, observed_at=now-age,
            resets_at=reset, source="transcript", account_label="private-account",
        )])
    result = await _plan(handler, "t")
    providers = {p["provider"]: p for p in result["live_context"]["providers"]}
    windows = {q["window"]: q for q in providers["codex"]["quota"]}
    assert windows["5h"]["used_percent"] == 20
    assert windows["weekly"]["used_percent"] == 90
    assert windows["stale"]["freshness"] == "stale"
    assert windows["reset"]["freshness"] == "reset"
    assert windows["5h"]["collector"] == "transcript"
    assert windows["weekly"]["age_seconds"] >= 20
    assert providers["claude"]["quota_status"] == "unknown"
    assert providers["claude"]["quota"] == []
    codex_score = next(s for s in result["scores"] if s["profile_id"] == "standard-high-codex")
    assert codex_score["usage_percent"] == 90  # only fresh, unreset observations score
    assert "private-account" not in json.dumps(result["live_context"])


async def test_context_reports_degraded_and_disabled_provider_reason(handler, orch):
    from src.providers.availability import ProviderAvailability

    await _create(orch.db, "t", task_type=TaskType.RESEARCH)
    await _workspace(orch)
    availability = orch.provider_availability
    availability._rows["codex"] = ProviderAvailability(
        provider="codex", state="degraded", reason_code="usage_high", updated_at=time.time(),
    )
    context = (await _plan(handler, "t"))["live_context"]
    provider = next(p for p in context["providers"] if p["provider"] == "codex")
    assert (provider["state"], provider["reason"]) == ("degraded", "usage_high")
    assert provider["age_seconds"] is not None
    availability._rows["codex"] = ProviderAvailability(
        provider="codex", override_state="disabled", override_reason="secret operator note",
        updated_at=time.time(),
    )
    context = (await _plan(handler, "t"))["live_context"]
    assert _context_profile(context)["effective_headroom"] == 0
    provider = next(p for p in context["providers"] if p["provider"] == "codex")
    assert (provider["state"], provider["reason"]) == ("disabled", "operator_disabled")
    assert "secret operator note" not in json.dumps(context)


async def test_plan_rejects_an_invalid_policy_and_an_unknown_task(handler, orch):
    await _create(orch.db, "t")
    bad = await handler.execute("task_route_plan", {"task_id": "t", "policy": "version: 7"})
    assert bad["success"] is False and bad["code"] == "routing.invalid_policy"
    missing = await _plan(handler, "nope")
    assert missing["success"] is False


async def test_plan_reports_a_router_route_as_already_routed(handler, orch):
    await _create(orch.db, "t", task_type=TaskType.RESEARCH)
    await _route(handler, "t")
    again = await _plan(handler, "t")
    assert again["outcome"] == "already_routed"
    assert again["route_source"] == ROUTER


async def test_plan_holds_when_every_candidate_provider_is_out(handler, orch):
    await _create(orch.db, "t", task_type=TaskType.RESEARCH)
    availability = orch.provider_availability
    orch.provider_availability = SimpleNamespace(
        provider_for_profile=availability.provider_for_profile,
        effective_state=lambda key: "exhausted",
        suppresses=lambda key: True,
    )
    held = await _plan(handler, "t")
    assert held["outcome"] == "held"
    assert held["providers"] == ["claude", "codex"]


async def test_benchmark_arm_holds_if_requested_model_mapping_differs(handler, orch):
    await _create(orch.db, "t", task_type=TaskType.RESEARCH)
    await orch.db.add_task_label("t", "benchmark:astra")
    policy = SHIPPED_POLICY + """\
benchmark_arms:
  astra:
    class: standard-high
    harness: codex
    requested_model: another-model
    observed_models: [another-model]
"""
    held = await handler.execute("task_route_plan", {"task_id": "t", "policy": policy})
    assert held["outcome"] == "held"
    assert held["reason"] == "requested_model_unavailable"
    assert held["mapped_model"] == "gpt-standard-high"

    policy = policy.replace("another-model", "gpt-standard-high")
    planned = await handler.execute("task_route_plan", {"task_id": "t", "policy": policy})
    assert planned["outcome"] == "planned", planned
    orch.intelligence_classes.replace({
        **CLASSES,
        "standard-high": IntelligenceClass("standard-high", "", "", {
            "codex": {"model": "mapping-changed"},
        }),
    })
    with _as_playbook():
        stale = await handler.execute("task_route_apply", {"task_id": "t", "plan": planned})
    assert stale["outcome"] == "stale"
    assert "mapping changed" in stale["reason"]


@pytest.mark.parametrize("requested_model", ["gpt-standard-high", "unavailable-model"])
async def test_benchmark_contract_preserves_provenance_for_apply(
    handler, orch, monkeypatch, requested_model
):
    from src.commands.contracts import builtin
    from src.commands.principal import ExecutionPrincipal

    monkeypatch.setattr(builtin, "_handler_provider", lambda: handler)
    await _create(orch.db, "t", task_type=TaskType.RESEARCH)
    await orch.db.add_task_label("t", "benchmark:astra")
    policy = SHIPPED_POLICY + f"""\
benchmark_arms:
  astra:
    class: standard-high
    harness: codex
    requested_model: {requested_model}
    observed_models: [{requested_model}]
"""
    registration = CONTRACTS.get("task_route_plan")
    args = registration.contract.execution.args_model(task_id="t", policy=policy)
    with _as_playbook():
        result = await registration.invoke(args, ExecutionPrincipal.service("playbook-dispatch"))
        assert result.value.benchmark_arm == "astra"
        assert result.value.requested_model == requested_model
        if requested_model == "unavailable-model":
            assert result.outcome == "held"
            assert result.value.mapped_model == "gpt-standard-high"
        else:
            assert result.outcome == "planned"
            assert result.value.observed_models == [requested_model]
            routed = await handler.execute("task_route_apply", {
                "task_id": "t", "plan": result.value.model_dump(exclude_none=True),
            })
            assert routed["outcome"] == "routed", routed
            assert (await orch.db.get_task("t")).route["benchmark_arm"] == "astra"


async def test_plan_honours_exclude_providers_from_the_route_constraints(handler, orch):
    await _create(orch.db, "t", task_type=TaskType.RESEARCH,
                  route={"constraints": {"exclude_providers": ["codex"]}})
    plan = await _plan(handler, "t")
    assert plan["profile_id"] == "standard-high-claude"


async def test_task_facts_carry_the_local_model_gate_inputs(handler, orch):
    from src.models import DepType

    # A task's priority is not a gate input (rev-wise-impact): it never routes.
    await _create(orch.db, "fix", task_type=TaskType.BUGFIX, priority=290)
    await _create(orch.db, "waiter", task_type=TaskType.RESEARCH)
    await orch.db.add_dependency("waiter", "fix", DepType.BLOCKS.value)
    facts = await handler._routing_task_facts(
        await orch.db.get_task("fix"), await orch.db.get_project("p"),
    )
    assert (facts.on_train, facts.blocks_work) == (False, True)
    assert not hasattr(facts, "priority")

    # ``train`` needs an integration repository; the builder reads only the mode.
    train = replace(await orch.db.get_project("p"), hierarchical_integration_mode="train")
    facts = await handler._routing_task_facts(await orch.db.get_task("waiter"), train)
    assert (facts.on_train, facts.blocks_work) == (True, False)


# -- task_route_apply: only the bound router ----------------------------------------


async def test_apply_refuses_a_caller_with_no_playbook_invocation(handler, orch):
    await _create(orch.db, "t", task_type=TaskType.RESEARCH)
    plan = await _plan(handler, "t")
    assert current_invocation() is None
    refused = await handler.execute("task_route_apply", {"task_id": "t", "plan": plan})
    assert refused["success"] is False and refused["code"] == NOT_ROUTER
    assert (await orch.db.get_task("t")).route_source == UNROUTED


async def test_apply_refuses_a_playbook_that_is_not_the_bound_router(handler, orch):
    await _create(orch.db, "t", task_type=TaskType.RESEARCH)
    plan = await _plan(handler, "t")
    with _as_playbook("some-other-playbook"):
        refused = await handler.execute("task_route_apply", {"task_id": "t", "plan": plan})
    assert refused["code"] == NOT_ROUTER
    assert "not the project's bound router" in refused["error"]
    # The bound router's id on an artifact that does not grant the command.
    with _as_playbook(ROUTER_ID, commands=("task_route_plan",)):
        refused = await handler.execute("task_route_apply", {"task_id": "t", "plan": plan})
    assert refused["code"] == NOT_ROUTER
    assert "does not grant" in refused["error"]
    # A project re-bound elsewhere refuses the old router too.
    await orch.db.update_project("p", assignment_playbook_id="project-router")
    with _as_playbook(ROUTER_ID):
        refused = await handler.execute("task_route_apply", {"task_id": "t", "plan": plan})
    assert refused["code"] == NOT_ROUTER
    assert (await orch.db.get_task("t")).route_source == UNROUTED


async def test_the_typed_contract_maps_not_router_to_rejected(handler, orch, monkeypatch):
    from src.commands.contracts import builtin
    from src.commands.principal import ExecutionPrincipal

    monkeypatch.setattr(builtin, "_handler_provider", lambda: handler)
    await _create(orch.db, "t", task_type=TaskType.RESEARCH)
    plan = await _plan(handler, "t")
    registration = CONTRACTS.get("task_route_apply")
    args = registration.contract.execution.args_model(task_id="t", plan=plan)
    result = await registration.invoke(args, ExecutionPrincipal.service("playbook-dispatch"))
    assert result.outcome == "rejected"
    assert result.summary.startswith("task_route_apply refused")


def test_granted_aq_commands_reads_command_steps_and_tool_use() -> None:
    artifact = SimpleNamespace(steps={
        "a": SimpleNamespace(command="task_route_plan", tool_use=None),
        "b": SimpleNamespace(command=None, tool_use=SimpleNamespace(aq_commands=["get_task"])),
    })
    assert granted_aq_commands(artifact) == {"task_route_plan", "get_task"}
    assert granted_aq_commands(None) == frozenset()


# -- task_route_apply: the write ------------------------------------------------------


#: A routine feature classified medium risk. The shipped policy's ``risk``
#: table could raise a feature, so its plan needs the risk answer; a medium
#: floor (``standard-high`` on Claude or Codex) leaves a standard-high feature
#: where the hosted preference puts it.
ROUTINE_MEDIUM = {**NARROW_NO, "task_type": "feature", "risk": "medium",
                  "risk_reason": "touches the scheduler"}


async def _route(handler, task_id: str, classification=None) -> dict:
    plan = await _plan(handler, task_id, classification)
    assert plan["outcome"] == "planned", plan
    with _as_playbook():
        return await handler.execute("task_route_apply", {"task_id": task_id, "plan": plan})


async def test_apply_writes_the_route_resolves_the_gate_and_emits_task_routed(handler, orch):
    await _create(orch.db, "t", task_type=TaskType.RESEARCH, class_hint="standard-high")
    gate_id, _ = await orch.db.create_gate("p", "routing", "route me", waiter_task_ids=["t"])
    applied = await _route(handler, "t")
    assert applied["success"] and applied["outcome"] == "routed", applied
    assert applied["resolved_gate_ids"] == [gate_id]
    assert applied["adjusted_at_apply"] is False

    task = await orch.db.get_task("t")
    assert (task.profile_id, task.intelligence_class) == ("standard-high-codex", "standard-high")
    assert (task.route_source, task.provider_intent) == (ROUTER, "class_only")
    route = task.route
    assert route["rule"] == "kinds.research"
    assert route["hints"] == {"class_hint": "standard-high", "task_type": "research"}
    assert route["run_id"] == "run-1" and route["playbook_id"] == ROUTER_ID
    assert route["policy_sha256"].startswith("sha256:")
    assert {c["profile_id"] for c in route["candidates"]} == {
        "standard-high-codex", "standard-high-claude",
    }
    assert route["scores"] and route["reason"]

    emitted = [c for c in orch.bus.emit.await_args_list if c.args[0] == "task.routed"]
    assert len(emitted) == 1
    payload = emitted[0].args[1]
    assert payload["profile_id"] == "standard-high-codex"
    assert payload["provider"] == "codex" and payload["run_id"] == "run-1"
    from src.event_schemas import EVENT_SCHEMAS

    schema = EVENT_SCHEMAS["task.routed"]
    assert set(schema["required"]) <= set(payload)
    assert set(payload) <= set(schema["required"]) | set(schema["optional"])


async def test_apply_writes_the_classified_kind_and_keeps_constraints(handler, orch):
    await _create(orch.db, "t", route={"constraints": {"exclude_providers": ["claude"]}})
    classification = {
        "task_type": "research", "intelligence_class": "deep-high", "narrow": False,
        "test_verified": False, "independent_verifier": False, "reason": "open question",
    }
    applied = await _route(handler, "t", classification)
    assert applied["profile_id"] == "deep-high-codex"
    task = await orch.db.get_task("t")
    assert task.task_type == TaskType.RESEARCH
    assert task.route["constraints"] == {"exclude_providers": ["claude"]}
    assert task.route["classification"]["intelligence_class"] == "deep-high"


async def test_apply_records_a_class_a_risk_floor_raised(handler, orch):
    from tests.test_routing_planner import RISK_POLICY

    classification = {
        "task_type": "research", "intelligence_class": "standard-high", "narrow": False,
        "test_verified": False, "independent_verifier": False, "reason": "schema change",
        "risk": "very_high", "risk_reason": "migrates the claims table",
    }
    for task_id, risk in (("raised", "very_high"), ("kept", "low")):
        await _create(orch.db, task_id, task_type=TaskType.RESEARCH, class_hint="standard-high")
        plan = await handler.execute("task_route_plan", {
            "task_id": task_id, "policy": RISK_POLICY,
            "classification": {**classification, "risk": risk},
        })
        assert plan["outcome"] == "planned", plan
        with _as_playbook():
            applied = await handler.execute("task_route_apply", {"task_id": task_id, "plan": plan})
        assert applied["outcome"] == "routed", applied

    raised = await orch.db.get_task("raised")
    assert raised.intelligence_class == "deep-high"
    assert raised.route["class_raised_for_risk"] == {
        "from": "standard-high", "to": "deep-high", "risk": "very_high",
    }
    assert raised.route["classification"]["risk"] == "very_high"
    assert raised.route["classification"]["risk_reason"] == "migrates the claims table"

    # No raise: the route record has no such key.
    kept = await orch.db.get_task("kept")
    assert kept.intelligence_class == "standard-high"
    assert "class_raised_for_risk" not in kept.route


async def test_apply_reroutes_a_legacy_route(handler, orch):
    await _create(orch.db, "t", task_type=TaskType.RESEARCH, profile_id="standard-high-claude",
                  intelligence_class="standard-high")
    assert (await orch.db.get_task("t")).route_source == LEGACY
    applied = await _route(handler, "t")
    assert applied["outcome"] == "routed"
    routed = await orch.db.get_task("t")
    assert routed.route_source == ROUTER
    # The cutover keeps the replaced route for audit (spec §11, "Pins").
    assert routed.route["legacy"] == {
        "profile_id": "standard-high-claude",
        "intelligence_class": "standard-high",
        "provider_intent": "class_only",
    }


async def test_apply_is_stale_for_a_claimed_assigned_or_routed_task(handler, orch):
    await _create(orch.db, "t", task_type=TaskType.RESEARCH)
    plan = await _plan(handler, "t")
    await orch.db.create_agent(Agent(id="agent-1", name="a", profile_id="standard-high-codex"))
    await orch.db.update_task("t", assigned_agent_id="agent-1")
    with _as_playbook():
        stale = await handler.execute("task_route_apply", {"task_id": "t", "plan": plan})
    assert (stale["outcome"], stale["reason"]) == ("stale", "the task is assigned")

    await _create(orch.db, "u", task_type=TaskType.RESEARCH)
    plan = await _plan(handler, "u")
    assert (await _route(handler, "u"))["outcome"] == "routed"
    with _as_playbook():
        stale = await handler.execute("task_route_apply", {"task_id": "u", "plan": plan})
    assert stale["outcome"] == "stale" and "already routed" in stale["reason"]

    await _create(orch.db, "v", task_type=TaskType.RESEARCH)
    plan = await _plan(handler, "v")
    await orch.db.update_task("v", status=TaskStatus.COMPLETED)
    with _as_playbook():
        stale = await handler.execute("task_route_apply", {"task_id": "v", "plan": plan})
    assert stale["outcome"] == "stale" and "COMPLETED" in stale["reason"]


async def test_apply_refuses_a_malformed_plan(handler, orch):
    await _create(orch.db, "t", task_type=TaskType.RESEARCH)
    with _as_playbook():
        refused = await handler.execute("task_route_apply", {"task_id": "t", "plan": {}})
    assert refused["success"] is False and refused["code"] == "routing.invalid_plan"


# -- the burst ---------------------------------------------------------------------------


@pytest.mark.parametrize("concurrent", [False, True])
async def test_a_burst_planned_against_one_snapshot_spreads_by_pressure(
    handler, orch, concurrent
):
    ids = [f"t{index}" for index in range(6)]
    for task_id in ids:
        await _create(orch.db, task_id, task_type=TaskType.RESEARCH)
    # Every plan sees the same empty fleet and names the same profile.
    plans = [await _plan(handler, task_id) for task_id in ids]
    assert {plan["profile_id"] for plan in plans} == {"standard-high-codex"}

    async def apply(task_id, plan):
        return await handler.execute("task_route_apply", {"task_id": task_id, "plan": plan})

    with _as_playbook():
        if concurrent:
            results = await asyncio.gather(*(apply(t, p) for t, p in zip(ids, plans)))
        else:
            results = [await apply(t, p) for t, p in zip(ids, plans)]
    assert all(result["outcome"] == "routed" for result in results), results
    routed = [(await orch.db.get_task(task_id)).profile_id for task_id in ids]
    # Applied one at a time on fresh load, they fill both pools evenly.
    assert routed.count("standard-high-codex") == routed.count("standard-high-claude") == 3
    assert any(result["adjusted_at_apply"] for result in results)
    adjusted = next(result for result in results if result["adjusted_at_apply"])
    assert "adjusted at apply" in adjusted["reason"]
    backlog = await orch.db.count_routed_backlog_by_profile()
    assert backlog == {
        "standard-high-codex": {"eligible": 3, "blocked": 0},
        "standard-high-claude": {"eligible": 3, "blocked": 0},
    }
    apply_loads = []
    for task_id in ids:
        context = (await orch.db.get_task(task_id)).route["live_context"]
        apply_loads.append(sum(p["routed_backlog"] for p in context["profiles"]))
        assert context["observational"] is True
    assert sorted(apply_loads) == list(range(6))


async def test_apply_refreshes_eligibility_after_waiting_for_route_lock(handler, orch):
    await _create(orch.db, "t", task_type=TaskType.RESEARCH)
    plan = await _plan(handler, "t")
    assert plan["profile_id"] == "standard-high-codex"
    with _as_playbook():
        async with orch.db.routing_apply_lock():
            applying = asyncio.create_task(handler.execute(
                "task_route_apply", {"task_id": "t", "plan": plan},
            ))
            # Keep the apply waiting while its previously eligible profile is disabled.
            await asyncio.sleep(0.05)
            await orch.db.update_profile("standard-high-codex", enabled=False)
        result = await applying
    assert result["outcome"] == "routed" and result["profile_id"] == "standard-high-claude"
    context = (await orch.db.get_task("t")).route["live_context"]
    assert _context_profile(context)["enabled"] is False


@pytest.mark.parametrize("change", ["provider", "constraints", "preference", "binding"])
async def test_apply_revalidates_live_provider_task_constraints_and_router_binding(
    handler, orch, change,
):
    from src.providers.availability import ProviderAvailability

    await _create(orch.db, "t", task_type=TaskType.RESEARCH)
    plan = await _plan(handler, "t")
    if change == "provider":
        orch.provider_availability._rows["codex"] = ProviderAvailability(
            provider="codex", override_state="disabled", updated_at=time.time(),
        )
    elif change == "constraints":
        await orch.db.update_task("t", route={"constraints": {"exclude_providers": ["codex"]}})
    elif change == "preference":
        await orch.db.update_project("p", preferred_provider="claude")
    with _as_playbook():
        if change == "binding":
            async with orch.db.routing_apply_lock():
                applying = asyncio.create_task(handler.execute(
                    "task_route_apply", {"task_id": "t", "plan": plan},
                ))
                await asyncio.sleep(0.05)
                await orch.db.update_project("p", assignment_playbook_id="new-router")
            result = await applying
            assert result["code"] == NOT_ROUTER
            assert (await orch.db.get_task("t")).route_source == UNROUTED
        else:
            result = await handler.execute("task_route_apply", {"task_id": "t", "plan": plan})
            assert result["outcome"] == "routed"
            assert result["profile_id"] == "standard-high-claude"


async def test_context_summarizes_active_task_kinds_without_task_text(handler, orch):
    await _create(orch.db, "t", task_type=TaskType.RESEARCH)
    await _create(orch.db, "active", profile_id="standard-high-codex",
                  status=TaskStatus.IN_PROGRESS, task_type=TaskType.BUGFIX)
    await _session(orch.db, "busy", task_id="active")
    result = await _plan(handler, "t")
    assert result["live_context"]["active_work"] == {"bugfix": 1}
    assert "active" not in json.dumps(result["live_context"]["profiles"])


# -- the routing lock and the load queries -------------------------------------------------


async def test_the_routing_lock_is_exclusive_and_bounded(orch):
    from src.database.queries.routing_queries import RoutingBusyError

    async with orch.db.routing_apply_lock():
        with pytest.raises(RoutingBusyError):
            async with orch.db.routing_apply_lock(budget_seconds=0.2):
                pass
    async with orch.db.routing_apply_lock(budget_seconds=0.2):
        pass


async def test_backlog_counts_ready_and_assigned_routed_work_fleet_wide(orch):
    await orch.db.create_project(Project(id="q", name="Other"))
    await _create(orch.db, "ready", profile_id="standard-high-codex")
    await _create(orch.db, "assigned", profile_id="standard-high-codex",
                  status=TaskStatus.ASSIGNED)
    await orch.db.create_task(Task(id="elsewhere", project_id="q", title="x", description="d",
                                   status=TaskStatus.READY, profile_id="standard-high-claude",
                                   route_source="legacy"))
    await _create(orch.db, "blocked", profile_id="standard-high-claude",
                  status=TaskStatus.BLOCKED)
    await _create(orch.db, "unrouted")
    assert await orch.db.count_routed_backlog_by_profile() == {
        "standard-high-codex": {"eligible": 1, "blocked": 1},
        "standard-high-claude": {"eligible": 1, "blocked": 0},
    }
    assert await orch.db.count_busy_sessions_by_profile() == {}


async def test_unclaimable_ready_work_is_blocked_not_eligible(orch):
    """A task that fails any claim-frontier rule routes to *blocked*.

    Regression: one unclaimable verifier in a one-slot pool must not block a
    narrow task routed to that pool.  The eligible/blocked split is what
    makes the planner's load admit only work a pool worker could actually
    claim — the blocked ones are evidence, not a gate.
    """
    # dependency-blocked
    await _create(orch.db, "dep", profile_id="standard-high-codex")
    await orch.db.update_task("dep", is_blocked=1)
    # hold-labelled
    await _create(orch.db, "hold", profile_id="standard-high-codex")
    await orch.db.add_task_label("hold", "hold:operator")
    # already-assigned → ASSIGNED
    await _create(
        orch.db, "assigned", profile_id="standard-high-codex", status=TaskStatus.ASSIGNED,
    )
    agent = Agent(id="worker-1", name="w", profile_id="standard-high-codex")
    await orch.db.create_agent(agent)
    await orch.db.update_task("assigned", assigned_agent_id="worker-1")
    # plan-subtask (child of a plan; the frontier excludes plan subtasks)
    await _create(orch.db, "sub", profile_id="standard-high-codex")
    await orch.db.update_task("sub", is_plan_subtask=1)
    # a plain, claimable READY routed task
    await _create(orch.db, "plain", profile_id="standard-high-codex")

    backlog = await orch.db.count_routed_backlog_by_profile()
    counts = backlog["standard-high-codex"]
    assert counts == {"eligible": 1, "blocked": 4}, backlog


async def test_blocked_verifier_does_not_gate_a_narrow_route(orch):
    """The regression that motivated the split.

    A pool with 0 live workers and one routed READY verifier that has been
    assigned but is now blocked on a parent's gate — the verifier must show
    up in *blocked*, not *backlog*, so the planner is free to route an
    eligible narrow task to that pool.
    """
    await _create(
        orch.db, "verifier", profile_id="standard-high-claude", status=TaskStatus.ASSIGNED,
    )
    await orch.db.update_task("verifier", is_blocked=1)
    agent = Agent(id="verifier-w", name="v", profile_id="standard-high-claude")
    await orch.db.create_agent(agent)
    await orch.db.update_task("verifier", assigned_agent_id="verifier-w")

    # The blocked verifier is not eligible load, and is not busy either (no
    # live session), so the one Claude pool slot is free for the next routed
    # task.
    backlog = await orch.db.count_routed_backlog_by_profile()
    assert backlog["standard-high-claude"] == {"eligible": 0, "blocked": 1}, backlog
    assert await orch.db.count_busy_sessions_by_profile() == {}


@pytest.mark.parametrize("constraint", ["backlog", "quarantine", "unavailable"])
async def test_hosted_preference_apply_refreshes_capacity_and_ignores_caller_context(
    handler, orch, constraint,
):
    await _workspace(orch)
    await _create(orch.db, "routine", task_type=TaskType.FEATURE, class_hint="standard-high")
    policy, _digest = routine_policy()
    plan = await handler.execute("task_route_plan", {
        "task_id": "routine", "policy": policy.canonical_json(),
        "classification": ROUTINE_MEDIUM,
    })
    assert plan["profile_id"] == "standard-high-codex"
    assert plan["decision"]["mode"] == "hosted_preference"
    if constraint == "backlog":
        for i in range(2):
            await _create(orch.db, f"queued-{i}", profile_id="standard-high-codex")
    elif constraint == "quarantine":
        orch._quarantine_pool("p", "standard-high-codex", "launch backoff")
    else:
        from src.routing.planner import ProviderFacts
        original = handler._routing_static_facts

        async def unavailable(*args, **kwargs):
            profiles, providers = await original(*args, **kwargs)
            providers["codex"] = ProviderFacts(state="exhausted", launchable=False)
            return profiles, providers

        handler._routing_static_facts = unavailable
    plan["live_context"] = {"profiles": [{"profile_id": "standard-high-codex",
                                          "effective_headroom": 999}]}
    with _as_playbook():
        result = await handler.execute("task_route_apply", {"task_id": "routine", "plan": plan})
    assert result["outcome"] == "routed"
    route = (await orch.db.get_task("routine")).route
    assert (route["profile_id"], route["provider"], route["intelligence_class"]) == (
        "standard-high-claude", "claude", "standard-high",
    )
    assert route["adjusted_at_apply"]
    assert route["decision"]["mode"] == "pressure_fallback"
    assert "bypassed" in route["reason"]
    assert "snapshot age" in route["reason"]
    assert route["decision"]["snapshot_as_of"] == route["live_context"]["as_of"]
    # The apply-time re-plan keeps the plan's classification.
    assert route["classification"]["risk"] == "medium"


async def test_concurrent_routine_routes_fill_codex_headroom_then_fall_back(handler, orch):
    await _workspace(orch)
    policy, _digest = routine_policy()
    for i in range(3):
        await _create(orch.db, f"routine-{i}", task_type=TaskType.FEATURE,
                      class_hint="standard-high")
    plans = [await handler.execute("task_route_plan", {
        "task_id": f"routine-{i}", "policy": policy.canonical_json(),
        "classification": ROUTINE_MEDIUM,
    }) for i in range(3)]
    assert {p["profile_id"] for p in plans} == {"standard-high-codex"}
    with _as_playbook():
        results = await asyncio.gather(*(handler.execute("task_route_apply", {
            "task_id": f"routine-{i}", "plan": plan,
        }) for i, plan in enumerate(plans)))
    assert all(r["outcome"] == "routed" for r in results)
    routes = [(await orch.db.get_task(f"routine-{i}")).route for i in range(3)]
    # The fixture has two Codex profile slots and lazy workspace capacity.
    # Each committed route consumes backlog; the third apply sees saturation.
    assert sum(r["decision"]["mode"] == "hosted_preference" for r in routes) == 2
    assert [r["provider"] for r in routes].count("codex") == 2
    assert [r["provider"] for r in routes].count("claude") == 1


# -- task.route_needed carries the router and the class hint ------------------------------


async def test_route_needed_carries_the_bound_router_and_the_class_hint(orch):
    await _create(orch.db, "t", class_hint="deep-high")
    orch._route_needed_emitted = {}
    assert await orch._emit_route_needed_events() == 1
    payload = next(
        call.args[1] for call in orch.bus.emit.await_args_list
        if call.args[0] == "task.route_needed"
    )
    assert payload["router"] == ROUTER_ID
    assert payload["class_hint"] == "deep-high"


# -- the per-task routing preference (spec §4) ------------------------------------------


def _without_provider(handler, key: str):
    """Make one provider unlaunchable in every snapshot this handler builds."""
    from src.routing.planner import ProviderFacts

    original = handler._routing_static_facts

    async def unavailable(*args, **kwargs):
        profiles, providers = await original(*args, **kwargs)
        providers[key] = ProviderFacts(state="exhausted", launchable=False)
        return profiles, providers

    handler._routing_static_facts = unavailable


async def test_a_strict_preference_routes_to_its_target_and_records_the_decision(handler, orch):
    await _workspace(orch)
    await _create(orch.db, "t", task_type=TaskType.RESEARCH, class_hint="standard-high",
                  prefer_target="claude", prefer_mode="strict")
    plan = await _plan(handler, "t")
    assert plan["profile_id"] == "standard-high-claude"
    assert {c["provider"] for c in plan["candidates"]} == {"claude"}
    applied = await _route(handler, "t")
    assert applied["preference"] == {
        "target": "claude", "mode": "strict", "kind": "harness",
        "honoured": True, "fallback_reason": None,
    }
    route = (await orch.db.get_task("t")).route
    assert route["preference"] == applied["preference"]
    assert route["decision"]["preference"] == applied["preference"]
    assert route["decision"]["mode"] == "task_preference"
    emitted = [c for c in orch.bus.emit.await_args_list if c.args[0] == "task.routed"]
    assert emitted[0].args[1]["preference"] == applied["preference"]


async def test_a_strict_preference_holds_for_an_unavailable_provider_rather_than_falling_back(
    handler, orch,
):
    await _create(orch.db, "t", task_type=TaskType.RESEARCH, class_hint="standard-high",
                  prefer_target="claude", prefer_mode="strict")
    _without_provider(handler, "claude")
    plan = await _plan(handler, "t")
    assert plan["outcome"] == "held"
    assert plan["preference"]["fallback_reason"] == "provider_unavailable"
    assert {c["provider"] for c in plan["candidates"]} == {"claude"}
    assert (await orch.db.get_task("t")).profile_id is None


async def test_a_soft_preference_falls_back_to_another_harness_with_the_reason(handler, orch):
    await _workspace(orch)
    await _create(orch.db, "t", task_type=TaskType.RESEARCH, class_hint="standard-high",
                  prefer_target="claude")
    assert (await orch.db.get_task("t")).prefer_mode is None
    _without_provider(handler, "claude")
    plan = await _plan(handler, "t")
    assert plan["profile_id"] == "standard-high-codex"
    assert plan["preference"] == {
        "target": "claude", "mode": "soft", "kind": "harness",
        "honoured": False, "fallback_reason": "provider_unavailable",
    }
    applied = await _route(handler, "t")
    assert applied["profile_id"] == "standard-high-codex"
    route = (await orch.db.get_task("t")).route
    assert route["preference"]["honoured"] is False
    assert "preferred claude (soft) not honoured" in route["reason"]


async def test_apply_reads_the_preference_from_the_task_not_from_the_plan(handler, orch):
    await _create(orch.db, "t", task_type=TaskType.RESEARCH, class_hint="standard-high")
    plan = await _plan(handler, "t")
    assert {c["provider"] for c in plan["candidates"]} == {"claude", "codex"}
    # The filer tightens the task to Claude after the plan was made.
    assert await orch.db.reset_task_route(
        "t", prefer_target="claude", prefer_mode="strict"
    )
    with _as_playbook():
        applied = await handler.execute("task_route_apply", {"task_id": "t", "plan": plan})
    assert applied["outcome"] == "routed"
    assert applied["profile_id"] == "standard-high-claude"


async def test_apply_refuses_a_plan_that_cannot_serve_a_strict_preference(handler, orch):
    await _create(orch.db, "t", task_type=TaskType.RESEARCH, class_hint="standard-high")
    plan = await _plan(handler, "t")
    await orch.db.reset_task_route("t", prefer_target="deep-high-claude", prefer_mode="strict")
    with _as_playbook():
        applied = await handler.execute("task_route_apply", {"task_id": "t", "plan": plan})
    assert applied["outcome"] == "stale"
    assert "strict preference" in applied["reason"]
    assert (await orch.db.get_task("t")).profile_id is None


async def test_routing_is_unchanged_for_a_task_that_names_no_preference(handler, orch):
    await _create(orch.db, "plain", task_type=TaskType.RESEARCH, class_hint="standard-high")
    plan = await _plan(handler, "plain")
    assert plan["preference"] is None
    assert plan["decision"]["preference"] is None
    assert "preferred " not in plan["reason"]
    await _route(handler, "plain")
    route = (await orch.db.get_task("plain")).route
    assert route["preference"] is None
    assert route["decision"]["preference"] is None
    assert route["decision"]["mode"] in {"hosted_preference", "pressure_fallback"}


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ({"prefer": "gemini"}, "neither an installed harness nor an enabled worker profile"),
        ({"prefer": "reviewer"}, "not a worker route"),
        ({"prefer": "claude", "prefer_mode": "loud"}, "Invalid prefer_mode"),
    ],
)
async def test_filing_refuses_an_unknown_or_unusable_preference(handler, orch, args, expected):
    before = len(await orch.db.list_tasks(project_id="p"))
    result = await handler.execute("create_task", {
        "project_id": "p", "title": "Prefers", "description": "d", **args,
    })
    assert not result.get("success"), result
    assert expected in result["error"]
    assert len(await orch.db.list_tasks(project_id="p")) == before


@pytest.mark.parametrize("flags", [{"read_only": True}, {"lifecycle": "named"}])
async def test_filing_refuses_a_preference_on_a_profile_that_is_never_a_route(
    handler, orch, flags,
):
    await orch.db.create_profile(AgentProfile(
        id="helper-claude", name="helper-claude", harness="claude",
        default_class="standard-high", needs_workspace=False, **flags,
    ))
    result = await handler.execute("create_task", {
        "project_id": "p", "title": "Prefers", "description": "d",
        "prefer": "helper-claude",
    })
    assert not result.get("success")
    assert "not a worker route" in result["error"]
    # Its harness is still a legal preference: only the profile is not.
    harness = await handler.execute("create_task", {
        "project_id": "p", "title": "Prefers", "description": "d", "prefer": "claude",
    })
    assert harness["success"] and harness["prefer_target"] == "claude"


async def test_filing_refuses_a_preference_on_a_disabled_profile(handler, orch):
    await orch.db.update_profile("standard-high-codex", enabled=False)
    before = len(await orch.db.list_tasks(project_id="p"))
    result = await handler.execute("create_task", {
        "project_id": "p", "title": "Prefers", "description": "d",
        "prefer": "standard-high-codex",
    })
    assert not result.get("success")
    assert "is disabled" in result["error"]
    assert len(await orch.db.list_tasks(project_id="p")) == before


async def test_filing_stores_a_valid_preference_and_names_its_harness_kind(handler, orch):
    result = await handler.execute("create_task", {
        "project_id": "p", "title": "Prefers", "description": "d", "prefer": "codex",
    })
    assert result["success"]
    assert (result["prefer_target"], result["prefer_mode"]) == ("codex", "soft")
    task = await orch.db.get_task(result["task_id"])
    assert (task.prefer_target, task.prefer_mode, task.route_source) == ("codex", "soft", "unrouted")
    facts = await handler._routing_task_facts(task, await orch.db.get_project("p"))
    assert (facts.prefer_target, facts.prefer_mode) == ("codex", "soft")
    strict = await handler.execute("create_task", {
        "project_id": "p", "title": "Strict", "description": "d",
        "prefer": "standard-high-claude", "prefer_mode": "strict",
    })
    assert (strict["prefer_target"], strict["prefer_mode"]) == (
        "standard-high-claude", "strict",
    )


async def test_task_route_stores_changes_and_clears_the_preference(handler, orch):
    await _create(orch.db, "t", task_type=TaskType.RESEARCH, class_hint="standard-high",
                  prefer_target="claude")
    stored = await handler.execute("task_route", {
        "task_id": "t", "prefer": "codex", "prefer_mode": "strict",
    })
    assert stored["success"]
    assert (stored["prefer_target"], stored["prefer_mode"]) == ("codex", "strict")
    assert (await orch.db.get_task("t")).route_source == UNROUTED
    # A named target alone keeps the mode: re-routing never relaxes a pin.
    kept = await handler.execute("task_route", {"task_id": "t", "prefer": "claude"})
    assert (kept["prefer_target"], kept["prefer_mode"]) == ("claude", "strict")
    relaxed = await handler.execute("task_route", {"task_id": "t", "prefer_mode": "soft"})
    assert (relaxed["prefer_target"], relaxed["prefer_mode"]) == ("claude", "soft")
    cleared = await handler.execute("task_route", {"task_id": "t", "prefer": ""})
    assert cleared["prefer_target"] is None and cleared["prefer_mode"] is None
    kept = await handler.execute("task_route", {"task_id": "t", "task_type": "research"})
    assert (kept["prefer_target"], kept["prefer_mode"]) == (None, None)
    refused = await handler.execute("task_route", {"task_id": "t", "prefer": "gemini"})
    assert not refused["success"] and "neither an installed harness" in refused["error"]


async def test_a_worker_filing_may_name_a_preference(handler, orch):
    """The preference is an input to the router, so every filer may set it."""
    from src.commands.principal import (
        ExecutionPrincipal,
        PrincipalKind,
        principal_context,
    )
    from src.profiles.capabilities import CapabilityPolicy

    principal = ExecutionPrincipal(
        kind=PrincipalKind.SESSION,
        policy=CapabilityPolicy.from_namespaces(aq_commands=["create_task"]),
        session_id="worker-1", project_id="p",
    )
    with principal_context(principal):
        result = await handler.execute("create_task", {
            "project_id": "p", "title": "Worker prefers", "description": "d",
            "prefer": "claude", "prefer_mode": "strict", "reason": "use astra",
        })
    assert result["success"], result
    task = await orch.db.get_task(result["task_id"])
    assert (task.prefer_target, task.prefer_mode) == ("claude", "strict")
