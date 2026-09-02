"""Explicit execution requirements must survive assignment to the global flock."""
from __future__ import annotations

from dataclasses import replace

import pytest

from src.intelligence_classes import IntelligenceClass
from src.models import Agent, AgentProfile, AgentState, Project, Task, TaskStatus
from src.scheduler import Scheduler, SchedulerState
from src.assignment_routing import EffectiveAssignmentRoute


def routing_profiles():
    return {
        "worker-deep": AgentProfile(
            id="worker-deep", name="Deep", harness="claude", default_class="deep-high",
        ),
        "worker-deep-codex": AgentProfile(
            id="worker-deep-codex", name="Deep Codex", harness="codex",
            default_class="deep-high",
        ),
        "triage": AgentProfile(
            id="triage", name="Triage", harness="codex", default_class="fast-low",
        ),
    }


def routing_classes():
    return {
        "deep-high": IntelligenceClass("deep-high", "Deep high", "", {
            "anthropic": {"model": "claude-fable-5", "thinking": "high"},
            "openai": {"model": "api-deep", "reasoning_effort": "high"},
            "codex": {"model": "gpt-5.6-sol", "reasoning_effort": "high"},
        }),
        "fast-low": IntelligenceClass("fast-low", "Fast low", "", {
            "anthropic": {"model": "claude-sonnet-5", "thinking": "low"},
            "openai": {"model": "api-fast", "reasoning_effort": "low"},
            "codex": {"model": "gpt-5.6-luna", "reasoning_effort": "low"},
        }),
    }


def workers():
    return [
        Agent(
            id="triage", name="Triage Codex", profile_id="triage",
            harness="codex", intelligence_class="fast-low",
        ),
        Agent(
            id="deep-claude", name="DeepWork Claude", profile_id="worker-deep",
            harness="claude", intelligence_class="deep-high",
        ),
        Agent(
            id="sol", name="DeepWork Codex", profile_id="worker-deep-codex",
            harness="codex", intelligence_class="deep-high",
        ),
    ]


def requested_task(**changes):
    return replace(Task(
        id="task", project_id="p", title="Use Codex Sol", description="",
        status=TaskStatus.READY, profile_id="worker-deep-codex",
        intelligence_class="deep-high", created_at=1,
    ), **changes)


def routing_state(task=None, agents=None, profiles=None):
    state = SchedulerState(
        projects=[Project(id="p", name="Project")],
        tasks=[task or requested_task()],
        agents=workers() if agents is None else agents,
        project_token_usage={}, project_active_agent_counts={},
        tasks_completed_in_window={}, now=1000, affinity_wait_seconds=10,
    )
    # Assigned after construction so these regressions exercise the old
    # scheduler as real misassignments rather than failing on a new argument.
    state.profiles = routing_profiles() if profiles is None else profiles
    state.intelligence_classes = routing_classes()
    return state


@pytest.mark.parametrize("reverse", [False, True])
def test_explicit_codex_deep_task_uses_sol_regardless_of_roster_order(reverse):
    roster = workers()
    if reverse:
        roster.reverse()
    actions = Scheduler.schedule(routing_state(agents=roster))
    assert [(action.task_id, action.agent_id) for action in actions] == [("task", "sol")]


@pytest.mark.parametrize("preferred", [None, "sol"])
def test_busy_sol_never_falls_back_to_fast_or_wrong_provider_after_affinity_timeout(preferred):
    roster = workers()
    roster[-1].state = AgentState.BUSY
    roster[-1].current_task_id = "other"
    task = requested_task(affinity_agent_id=preferred)
    assert Scheduler.schedule(routing_state(task, roster)) == []


def test_worker_occupied_by_interactive_terminal_is_not_replaced_by_triage():
    # The orchestrator removes idle identities with a live terminal from
    # its scheduler snapshot; the remaining roster must not take this work.
    assert Scheduler.schedule(routing_state(agents=workers()[:-1])) == []


def test_explicit_task_class_rejects_low_worker_without_a_task_profile():
    task = requested_task(profile_id=None)
    actions = Scheduler.schedule(routing_state(task))
    assert actions[0].agent_id != "triage"


def test_profile_default_class_prevents_downgrade_when_task_class_is_absent():
    task = requested_task(intelligence_class=None)
    actions = Scheduler.schedule(routing_state(task))
    assert [action.agent_id for action in actions] == ["sol"]


def test_provider_pin_filters_a_class_compatible_worker():
    task = requested_task(profile_id=None)
    state = routing_state(task, [workers()[1], workers()[2]])
    state.assignment_routes = {
        task.id: EffectiveAssignmentRoute(
            task.id, "deep-high", "openai", "playbook", "hash", "run"
        )
    }
    assert [action.agent_id for action in Scheduler.schedule(state)] == ["sol"]


def test_worker_profile_default_class_is_respected_without_agent_override():
    triage = replace(workers()[0], intelligence_class=None)
    assert Scheduler.schedule(routing_state(agents=[triage])) == []


@pytest.mark.parametrize("agent_index", [1, 2])
def test_shipped_generic_deep_profile_can_use_each_matching_provider(agent_index):
    task = requested_task(profile_id="worker-deep")
    agent = workers()[agent_index]
    actions = Scheduler.schedule(routing_state(task, [agent]))
    assert [action.agent_id for action in actions] == [agent.id]


def test_copy_of_generic_profile_with_codex_harness_still_requires_codex():
    profiles = routing_profiles()
    profiles["worker-deep-codex"].tags = ["worker", "generic"]
    assert Scheduler.schedule(routing_state(agents=[workers()[1]], profiles=profiles)) == []


def test_modified_shipped_generic_harness_is_an_explicit_provider_choice():
    profiles = routing_profiles()
    profiles["worker-deep"].harness = "codex"
    task = requested_task(profile_id="worker-deep")
    assert Scheduler.schedule(routing_state(task, [workers()[1]], profiles)) == []


def test_fixed_luna_override_cannot_satisfy_deep_class_even_with_matching_class_label():
    sol = replace(workers()[-1], model="gpt-5.6-luna")
    assert Scheduler.schedule(routing_state(agents=[sol])) == []


def test_explicit_profile_model_cannot_be_replaced_by_worker_model():
    profiles = routing_profiles()
    profiles["worker-deep-codex"].default_class = ""
    profiles["worker-deep-codex"].model = "gpt-5.6-sol"
    task = requested_task(intelligence_class=None)
    agent = replace(workers()[-1], model="gpt-5.6-luna", intelligence_class=None)
    assert Scheduler.schedule(routing_state(task, [agent], profiles)) == []


def test_legacy_task_without_execution_requirements_still_uses_global_flock():
    task = requested_task(profile_id=None, intelligence_class=None)
    actions = Scheduler.schedule(routing_state(task))
    assert [action.agent_id for action in actions] == ["triage"]


def test_ready_task_with_unresolved_routing_gate_is_not_dispatched():
    task = requested_task(profile_id=None, intelligence_class=None, is_blocked=True)
    assert Scheduler.schedule(routing_state(task)) == []


def test_helper_does_not_modify_worker_or_profile_and_reports_mismatch():
    from src.agents.routing import task_agent_mismatch

    profiles = routing_profiles()
    worker = workers()[0]
    before_worker = replace(worker)
    before_profile = replace(profiles["worker-deep-codex"])
    reason = task_agent_mismatch(
        requested_task(), worker, task_profile=profiles["worker-deep-codex"],
        agent_profile=profiles["triage"], intelligence_classes=routing_classes(),
    )
    assert reason and "class" in reason
    assert worker == before_worker
    assert profiles["worker-deep-codex"] == before_profile


def test_unknown_requested_class_waits_instead_of_using_a_fallback_model():
    from src.agents.routing import task_agent_mismatch

    profiles = routing_profiles()
    worker = replace(workers()[-1], intelligence_class="removed-class")
    reason = task_agent_mismatch(
        requested_task(intelligence_class="removed-class"), worker,
        task_profile=profiles["worker-deep-codex"],
        agent_profile=profiles["worker-deep-codex"],
        intelligence_classes=routing_classes(),
    )
    assert reason and "removed-class" in reason


def test_worker_profile_fallback_yields_to_task_class_when_worker_has_no_class():
    profiles = routing_profiles()
    profiles["triage"].default_class = ""
    profiles["triage"].model = "gpt-5.6-luna"
    agent = replace(workers()[0], intelligence_class=None)
    actions = Scheduler.schedule(routing_state(agents=[agent], profiles=profiles))
    assert [action.agent_id for action in actions] == [agent.id]
    assert profiles["triage"].model == "gpt-5.6-luna"


def test_unconfigured_worker_can_inherit_required_class():
    profiles = routing_profiles()
    profiles["triage"].default_class = ""
    profiles["triage"].model = ""
    agent = replace(workers()[0], intelligence_class=None)
    actions = Scheduler.schedule(routing_state(agents=[agent], profiles=profiles))
    assert [action.agent_id for action in actions] == [agent.id]


def test_shipped_triage_capability_runs_on_the_configured_codex_triage_worker():
    profiles = routing_profiles()
    profiles["triage"].harness = "claude"
    profiles["triage"].model = "claude-sonnet-4-6"
    task = requested_task(profile_id="triage", intelligence_class=None)
    actions = Scheduler.schedule(routing_state(task, [workers()[0]], profiles))
    assert [action.agent_id for action in actions] == ["triage"]


def test_requested_class_model_wins_over_explicit_profile_fallback_model():
    profiles = routing_profiles()
    profiles["worker-deep-codex"].model = "old-fallback-model"
    actions = Scheduler.schedule(routing_state(agents=[workers()[-1]], profiles=profiles))
    assert [action.agent_id for action in actions] == ["sol"]


@pytest.fixture
async def routing_db(tmp_path):
    from src.database import Database
    from src.models import RepoSourceType, Workspace

    db = Database(str(tmp_path / "routing.db"))
    await db.initialize()
    for profile in routing_profiles().values():
        await db.create_profile(profile)
    await db.create_project(Project(id="p", name="Project", max_concurrent_agents=3))
    await db.create_workspace(Workspace(
        id="ws", project_id="p", workspace_path=str(tmp_path / "workspace"),
        source_type=RepoSourceType.LINK, enabled=True,
    ))
    await db.create_task(requested_task())
    yield db
    await db.close()


async def test_reconciler_does_not_count_incompatible_idle_worker_as_supply(routing_db):
    from src.orchestrator.agent_reconciler import AgentReconciler

    await routing_db.create_agent(workers()[0])
    report = await AgentReconciler(routing_db).reconcile()
    assert report.created == [("p", "worker-deep-codex")]
    assert (await routing_db.get_agent("triage")).intelligence_class == "fast-low"


async def test_reconciler_respects_manually_sized_roster_when_only_triage_is_idle(routing_db):
    from src.orchestrator.agent_reconciler import AgentReconciler

    await routing_db.create_agent(workers()[0])
    await routing_db.create_agent(workers()[-1])
    assert await routing_db.soft_delete_agent("sol")
    report = await AgentReconciler(routing_db).reconcile()
    assert report.created == []
    assert [agent.id for agent in await routing_db.list_agents()] == ["triage"]
    assert any("roster was manually sized" in reason for _, reason in report.skipped)


async def test_reconciler_does_not_create_supply_for_a_blocked_ready_task(routing_db):
    from src.orchestrator.agent_reconciler import AgentReconciler

    await routing_db.create_gate("p", "routing", "route this task", waiter_task_ids=["task"])
    assert (await routing_db.get_task("task")).is_blocked
    report = await AgentReconciler(routing_db).reconcile()
    assert report.created == []
    assert await routing_db.list_agents() == []


async def test_reconciler_does_not_create_supply_for_unrouted_snapshot(routing_db):
    from src.orchestrator.agent_reconciler import AgentReconciler

    report = await AgentReconciler(routing_db).reconcile(ready_tasks=[])
    assert report.created == []
    assert await routing_db.list_agents() == []


async def test_reconciler_reuses_matching_worker_without_changing_definition(routing_db):
    from src.orchestrator.agent_reconciler import AgentReconciler

    for agent in workers():
        await routing_db.create_agent(agent)
    before = await routing_db.get_agent("sol")
    assert (await AgentReconciler(routing_db).reconcile()).created == []
    assert await routing_db.get_agent("sol") == before


async def test_prelaunch_recheck_rejects_task_without_effective_route(routing_db, tmp_path):
    from src.config import AppConfig, DiscordConfig
    from src.orchestrator import Orchestrator

    agent = workers()[-1]
    await routing_db.create_agent(agent)
    task = replace(await routing_db.get_task("task"), intelligence_class=None)
    config = AppConfig(
        discord=DiscordConfig(bot_token="test", guild_id="1"),
        database_path=str(tmp_path / "unused.db"),
        data_dir=str(tmp_path / "data"),
        workspace_dir=str(tmp_path / "work"),
    )
    orch = Orchestrator(config)
    orch.db = routing_db
    orch.session_spec_builder._intelligence_classes = routing_classes()

    assert await orch._check_agent_routing(task, agent) == "awaiting intelligence route"


@pytest.mark.parametrize("interactive", [False, True])
async def test_orchestrator_snapshot_enforces_matching_and_live_terminal_ownership(
    routing_db, tmp_path, interactive,
):
    from unittest.mock import AsyncMock
    from src.config import AppConfig, DiscordConfig
    from src.models import SessionRecord
    from src.orchestrator import Orchestrator
    from src.orchestrator.agent_reconciler import ReconcileReport

    for agent in workers():
        await routing_db.create_agent(agent)
    if interactive:
        await routing_db.create_session(SessionRecord(
            id="terminal", project_id=None, profile_id="worker-deep-codex",
            harness="codex", provider="tmux", name="interactive-sol", lifecycle="named",
            state="running", work_dir="/tmp", epoch="e", instance_token="test", started_at=1,
            agent_id="sol",
        ))
    config = AppConfig(
        discord=DiscordConfig(bot_token="test", guild_id="1"),
        database_path=str(tmp_path / "unused.db"), data_dir=str(tmp_path / "data"),
        workspace_dir=str(tmp_path / "work"),
    )
    orch = Orchestrator(config)
    orch.db = routing_db
    orch._agent_reconciler.reconcile = AsyncMock(return_value=ReconcileReport())
    orch.session_spec_builder._intelligence_classes = routing_classes()
    actions = await orch._schedule()
    assert [action.agent_id for action in actions] == ([] if interactive else ["sol"])


def test_unrouted_task_is_never_reserved_and_reports_the_route_as_the_blocker():
    from src.explain import build_capacity_reasons

    state = routing_state(requested_task(profile_id=None, intelligence_class=None))
    state.assignment_routes = {}

    assert Scheduler.schedule(state) == []
    reasons = build_capacity_reasons(state.tasks[0], state, {"p": 1}, {"p": 3})
    assert [reason["code"] for reason in reasons] == ["awaiting_intelligence_route"]


def test_fresh_route_reserves_a_worker_at_the_class_it_names():
    state = routing_state(requested_task(profile_id=None, intelligence_class=None))
    state.assignment_routes = {
        "task": EffectiveAssignmentRoute("task", "fast-low", None, "playbook")
    }

    assert [action.agent_id for action in Scheduler.schedule(state)] == ["triage"]


async def test_orchestrator_leaves_an_unrouted_task_and_its_workers_alone(routing_db, tmp_path):
    """No reserve → no BUSY→IDLE flip and no 60 s launch-failure backoff."""
    from unittest.mock import AsyncMock
    from src.config import AppConfig, DiscordConfig
    from src.orchestrator import Orchestrator
    from src.orchestrator.agent_reconciler import ReconcileReport

    for agent in workers():
        await routing_db.create_agent(agent)
    await routing_db.update_task("task", intelligence_class=None, profile_id=None)
    config = AppConfig(
        discord=DiscordConfig(bot_token="test", guild_id="1"),
        database_path=str(tmp_path / "unused.db"), data_dir=str(tmp_path / "data"),
        workspace_dir=str(tmp_path / "work"),
    )
    orch = Orchestrator(config)
    orch.db = routing_db
    orch._agent_reconciler.reconcile = AsyncMock(return_value=ReconcileReport())
    orch.session_spec_builder._intelligence_classes = routing_classes()

    assert await orch._schedule() == []
    assert all(agent.state == AgentState.IDLE for agent in await routing_db.list_agents())
    assert (await routing_db.get_task("task")).status == TaskStatus.READY


def test_idle_count_uses_global_workers_and_ignores_disabled_supervisor_and_busy():
    from src.orchestrator.core import _idle_by_project

    roster = workers()
    roster[0].enabled = False
    roster[1].role = "supervisor"
    state = routing_state(agents=roster)
    state.projects.append(Project(id="q", name="Other"))
    assert _idle_by_project(state) == {"p": 1, "q": 1}


def test_explain_reports_incompatible_workers_instead_of_fake_project_ownership():
    from src.explain import build_capacity_reasons

    state = routing_state(agents=workers()[:-1])
    reasons = build_capacity_reasons(state.tasks[0], state, {"p": 1}, {"p": 2})
    assert any(reason["code"] == "no_compatible_agent" for reason in reasons)
    assert any("no compatible worker" in reason["detail"] for reason in reasons)
    assert all(reason["code"] != "no_idle_agent" for reason in reasons)


def test_explain_reports_global_compatible_worker_cooldown():
    from src.explain import build_capacity_reasons

    state = routing_state(agents=[workers()[-1]])
    state.provider_cooldowns["worker-deep-codex"] = state.now + 100
    reasons = build_capacity_reasons(state.tasks[0], state, {"p": 1}, {"p": 1})
    assert any(reason["code"] == "rate_limited" for reason in reasons)


def test_classified_codex_project_default_binds_provider_without_task_profile():
    state = routing_state(requested_task(profile_id=None))
    state.projects[0].default_profile_id = "worker-deep-codex"
    actions = Scheduler.schedule(state)
    assert [action.agent_id for action in actions] == ["sol"]


def test_classified_codex_project_default_waits_when_only_claude_is_available():
    state = routing_state(requested_task(profile_id=None), [workers()[1]])
    state.projects[0].default_profile_id = "worker-deep-codex"
    assert Scheduler.schedule(state) == []


def test_unclassified_harness_only_project_default_keeps_legacy_global_routing():
    profiles = routing_profiles()
    profiles["legacy"] = AgentProfile(id="legacy", name="Legacy", harness="claude")
    state = routing_state(requested_task(profile_id=None, intelligence_class=None), profiles=profiles)
    state.projects[0].default_profile_id = "legacy"
    assert [action.agent_id for action in Scheduler.schedule(state)] == ["triage"]


def test_project_default_fixed_model_is_binding_without_a_class():
    profiles = routing_profiles()
    profiles["fixed-sol"] = AgentProfile(
        id="fixed-sol", name="Fixed Sol", harness="codex", model="gpt-5.6-sol",
    )
    state = routing_state(
        requested_task(profile_id=None, intelligence_class=None), profiles=profiles,
    )
    state.projects[0].default_profile_id = "fixed-sol"
    assert [action.agent_id for action in Scheduler.schedule(state)] == ["sol"]


def test_inherited_model_does_not_restore_another_providers_fallback():
    from types import SimpleNamespace
    from src.agents.configuration import apply_agent_overrides
    from src.sessions.harness_parser import Harness
    from src.sessions.spec import SessionSpecBuilder

    profile = AgentProfile(
        id="personal", name="Personal", harness="claude", model="claude-sonnet-4-6",
    )
    agent = Agent(id="personal", name="Personal Codex", profile_id="personal", harness="codex")
    effective = apply_agent_overrides(profile, agent, agent_profile=profile)
    builder = SessionSpecBuilder(SimpleNamespace())
    harness = Harness(id="codex", command="codex", model_flag="--model")
    assert builder._resolve_model(effective, harness, None) == ""
    assert profile.model == "claude-sonnet-4-6"


def test_matching_discards_worker_profile_fallback_from_another_harness():
    profiles = routing_profiles()
    profiles["worker-deep-codex"].default_class = ""
    profiles["worker-deep-codex"].model = "gpt-5.6-sol"
    profiles["personal"] = AgentProfile(
        id="personal", name="Personal", harness="claude", model="claude-sonnet-4-6",
    )
    agent = Agent(id="personal", name="Personal Codex", profile_id="personal", harness="codex")
    state = routing_state(
        requested_task(intelligence_class=None), [agent], profiles,
    )
    assert [action.agent_id for action in Scheduler.schedule(state)] == ["personal"]
