"""Launch suppression against an unavailable provider (provider-failover D11, D13).

The 2026-09-20 replay: every Codex launch dies on the ``login-required``
startup dialog.  The acceptance bound is **two launches** (D3): after that
the provider is ``unauthenticated``, the push scheduler and pool sizing stop
launching against it, the startup deaths arm no pool-key quarantine, a
queued task explains itself with ``provider_hold`` and an idle pool worker
is drained on its next claim.  Fake session provider, injected auth probe,
real PostgreSQL -- no CLI, no LLM.
"""

from __future__ import annotations

import time
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.commands.handler import CommandHandler
from src.config import AppConfig, DatabaseConfig, DiscordConfig
from src.database import Database
from src.intelligence_classes import IntelligenceClass
from src.models import AgentProfile, Project, RepoSourceType, Task, TaskStatus, Workspace
from src.orchestrator import Orchestrator
from src.providers.availability import DEGRADED, UNAUTHENTICATED
from src.sessions.harness_parser import Harness
from tests.db_fixtures import lease_dsn
from tests.session_dispatch_helpers import (
    create_session_profile,
    create_session_project,
    drain_running_tasks,
    fake_provider,
    make_session_orch,
)

BOTH_VENDORS = {
    "standard-medium": IntelligenceClass(
        "standard-medium",
        "Standard",
        "",
        {"anthropic": {"model": "claude-sonnet-5"}, "openai": {"model": "gpt-6"}},
    ),
}


class Probe:
    def __init__(self, answer="cannot_tell"):
        self.answer = answer
        self.calls = 0

    async def __call__(self, provider, timeout):
        self.calls += 1
        return self.answer


def codex_harness() -> Harness:
    return Harness(
        id="codex", name="codex", command="codex", prompt_mode="arg", process_names=("codex",)
    )


async def _cycle(orch: Orchestrator) -> None:
    await orch.run_one_cycle()
    await drain_running_tasks(orch)
    await orch.provider_availability.wait_for_probes()


async def _startup_exits(orch: Orchestrator) -> int:
    return sum(1 for row in await orch.db.list_sessions() if row.end_reason == "startup_exit")


# -- push path ---------------------------------------------------------------------


@pytest.fixture
async def push_orch(tmp_path):
    orch = await make_session_orch(tmp_path)
    orch.session_spec_builder._intelligence_classes = dict(BOTH_VENDORS)
    orch.harness_registry.upsert(codex_harness())
    orch.provider_availability._probe_impl = Probe("cannot_tell")
    await create_session_project(orch)
    await create_session_profile(orch, "std-codex", harness="codex")
    for i in range(4):
        await orch.db.create_task(
            Task(
                id=f"t{i}",
                project_id="p-1",
                title=f"fix {i}",
                description="d",
                status=TaskStatus.READY,
                profile_id="std-codex",
                intelligence_class="standard-medium",
            )
        )
    yield orch
    await orch.wait_for_running_tasks(timeout=5)
    await orch.provider_availability.close()
    await orch.db.close()


async def test_login_required_on_every_codex_launch_trips_within_two_and_stops(push_orch):
    orch = push_orch
    fake = fake_provider(orch)
    fake.script_startup_dialog("codex", "login-required")  # no signal: name-map fallback

    for _ in range(8):
        await _cycle(orch)

    availability = orch.provider_availability
    assert availability.effective_state("codex") == UNAUTHENTICATED
    # The acceptance bound: at most two launches died before the trip, and
    # the count of startup_exit session rows then stopped growing.
    assert len(fake.dialog_deaths) <= 2
    assert await _startup_exits(orch) == len(fake.dialog_deaths)
    for _ in range(3):
        await _cycle(orch)
    assert await _startup_exits(orch) == len(fake.dialog_deaths)
    assert fake.starts == []

    # The rest of the queue is held, not failed: READY, explained.
    handler = CommandHandler(orch, orch.config)
    held = [t for t in await orch.db.list_tasks(project_id="p-1") if t.status == TaskStatus.READY]
    assert held
    explained = await handler._cmd_explain_task({"task_id": held[0].id})
    assert "provider_hold" in explained["reason_codes"]
    hold = explained["provider_hold"]
    assert hold["provider"] == "codex" and hold["state"] == UNAUTHENTICATED
    assert hold["kind"] in {"no_equivalent_rung", "failover_inactive"}
    assert "codex login" in hold["remediation"]


async def test_one_probe_confirmation_trips_on_the_first_launch(push_orch):
    orch = push_orch
    orch.provider_availability._probe_impl = Probe("not_authenticated")
    fake = fake_provider(orch)
    fake.script_startup_dialog("codex", "login-required", signal="auth")
    for _ in range(6):
        await _cycle(orch)
    assert orch.provider_availability.effective_state("codex") == UNAUTHENTICATED
    assert len(fake.dialog_deaths) == 1


async def test_observe_mode_records_the_state_but_keeps_launching(push_orch):
    orch = push_orch
    orch.config.provider_failover.mode = "observe"
    fake = fake_provider(orch)
    fake.script_startup_dialog("codex", "login-required")
    for _ in range(8):
        await _cycle(orch)
        # Paused launches come back quickly in this test.
        for task in await orch.db.list_tasks(project_id="p-1"):
            if task.status == TaskStatus.PAUSED:
                await orch.db.transition_task(task.id, TaskStatus.READY, context="test")
    assert orch.provider_availability.is_unavailable("codex")
    assert len(fake.dialog_deaths) > 2


async def test_state_survives_a_daemon_restart(push_orch, tmp_path):
    orch = push_orch
    fake = fake_provider(orch)
    fake.script_startup_dialog("codex", "login-required")
    for _ in range(6):
        await _cycle(orch)
    assert orch.provider_availability.effective_state("codex") == UNAUTHENTICATED

    from src.providers.availability_service import ProviderAvailabilityService

    reborn = ProviderAvailabilityService(
        db=orch.db, config_getter=lambda: orch.config, harness_registry=orch.harness_registry,
        probe=Probe("cannot_tell"),
    )
    await reborn.load()
    assert reborn.suppresses("codex")


async def test_a_pre_launch_refusal_does_not_notify_a_launch_failure(push_orch):
    orch = push_orch
    await orch.provider_availability.set_state(
        "codex", "disabled", by="human:cli", reason="rotating", until=time.time() + 600
    )
    orch._emit_text_notify = AsyncMock()
    task = await orch.db.get_task("t0")
    action = MagicMock(task_id="t0", agent_id="a-x", project_id="p-1")
    await orch._fail_session_launch(action, task, "refused", backoff=30, notify=False)
    orch._emit_text_notify.assert_not_awaited()
    paused = await orch.db.get_task("t0")
    assert paused.status == TaskStatus.PAUSED
    assert paused.resume_after <= time.time() + 31


async def test_a_killed_push_canary_lets_the_next_launch_be_the_canary(push_orch):
    orch = push_orch
    availability = orch.provider_availability
    availability._probe_impl = Probe("not_authenticated")
    await availability.record(
        "codex", "startup_dialog", "auth", detail={"dialog": "login-required"}
    )
    await availability.wait_for_probes()
    await availability.set_state("codex", "auto", by="human:cli")
    assert availability.row("codex").reason_code == "recovering"
    availability._probe_impl = Probe("authenticated")

    async def task_launches():
        return [row for row in await orch.db.list_sessions() if row.lifecycle == "task"]

    await _cycle(orch)
    (canary,) = await task_launches()
    assert canary.state == "running"

    await orch.db.update_session(
        canary.id, state="stopped", desired_state="stopped",
        ended_at=time.time(), end_reason="killed",
    )
    # The refused launches were held briefly (D13); bring them straight back.
    for task in await orch.db.list_tasks(project_id="p-1"):
        if task.status == TaskStatus.PAUSED:
            await orch.db.transition_task(task.id, TaskStatus.READY, context="test")
    # One workspace: the reconciler frees it after this cycle has scheduled.
    for _ in range(2):
        await _cycle(orch)
    assert len(await task_launches()) == 2


async def test_a_push_canary_that_never_starts_frees_the_next_launch(push_orch, monkeypatch):
    """A launch refused after admission never ran; it is not a canary in flight."""
    orch = push_orch
    availability = orch.provider_availability
    availability._probe_impl = Probe("not_authenticated")
    await availability.record(
        "codex", "startup_dialog", "auth", detail={"dialog": "login-required"}
    )
    await availability.wait_for_probes()
    await availability.set_state("codex", "auto", by="human:cli")
    availability._probe_impl = Probe("authenticated")
    fake = fake_provider(orch)

    from src.orchestrator import execution

    refusals = ["refusing to run an agent in the base checkout"]

    async def refuse_once(*_args, **_kwargs):
        return refusals.pop() if refusals else None

    monkeypatch.setattr(execution, "base_checkout_refusal", refuse_once)
    await _cycle(orch)
    assert not refusals
    assert not [row for row in await orch.db.list_sessions() if row.lifecycle == "task"]

    for task in await orch.db.list_tasks(project_id="p-1"):
        if task.status == TaskStatus.PAUSED:
            await orch.db.transition_task(task.id, TaskStatus.READY, context="test")
    await _cycle(orch)
    assert [row for row in await orch.db.list_sessions() if row.lifecycle == "task"]
    assert [spec for spec in fake.starts if spec.session_name.startswith("s-")]


# -- pool path -------------------------------------------------------------------------

POOL_PROJECT = "proj"


@pytest.fixture
async def pool_db(tmp_path):
    database = Database(lease_dsn("pool.db"))
    await database.initialize()
    kind = await database.resolve_workspace_kind("__system__", "project-repo")
    await database.upsert_workspace_kind(replace(kind, mode="exclusive-clone"))
    await database.create_project(Project(id=POOL_PROJECT, name="p"))
    await database.create_profile(
        AgentProfile(
            id="worker", name="w", lifecycle="pool", min_active=0, max_active=2,
            harness="codex", default_class="standard-medium",
        )
    )
    for i in range(3):
        await database.create_workspace(
            Workspace(
                id=f"ws{i}",
                project_id=POOL_PROJECT,
                workspace_path=str(tmp_path / f"ws{i}"),
                source_type=RepoSourceType.LINK,
                kind_id="project-repo",
            )
        )
    for i in range(3):
        await database.create_task(
            Task(
                id=f"t{i}", project_id=POOL_PROJECT, title="t", description="d",
                status=TaskStatus.READY, profile_id="worker",
            )
        )
    yield database
    await database.close()


@pytest.fixture
async def pool_orch(pool_db, tmp_path):
    cfg = AppConfig(
        discord=DiscordConfig(bot_token="t", guild_id="1"),
        workspace_dir=str(tmp_path / "ws"),
        database=DatabaseConfig(url=lease_dsn("pool.db")),
        data_dir=str(tmp_path / "data"),
    )
    cfg.sessions.enabled = True
    cfg.sessions.provider = "fake"
    cfg.swarm.enabled = True
    cfg.swarm.max_starts_per_tick = 5
    cfg.worktrees.enabled = False
    o = Orchestrator(cfg)
    o.session_spec_builder._intelligence_classes = dict(BOTH_VENDORS)
    o.db = pool_db
    o._agent_reconciler._db = pool_db
    o.git = MagicMock()
    o._ensure_control_files_excluded = AsyncMock(return_value=True)
    o.bus.emit = AsyncMock()
    o.harness_registry.upsert(codex_harness())
    o.provider_availability._probe_impl = Probe("cannot_tell")
    yield o
    await o.wait_for_pool_launches(cancel=True)
    await o.provider_availability.close()


async def _pool_round(orch: Orchestrator) -> None:
    await orch._reconcile_pools()
    await orch.wait_for_pool_launches()
    await orch.provider_availability.wait_for_probes()


async def test_pool_startup_dialogs_arm_no_key_quarantine_and_size_the_pool_to_zero(pool_orch):
    orch = pool_orch
    fake = fake_provider(orch)
    fake.script_startup_dialog("codex", "login-required", signal="auth")

    for _ in range(4):
        await _pool_round(orch)

    assert orch.provider_availability.effective_state("codex") == UNAUTHENTICATED
    assert len(fake.dialog_deaths) <= 2
    # The deaths were the provider's, not the (project, profile) key's.
    assert orch._pool_quarantine == {}
    measurement = await orch._measure_pools()
    assert all(bounds == (0, 0) for bounds in measurement.bounds.values())
    deaths = len(fake.dialog_deaths)
    await _pool_round(orch)
    assert len(fake.dialog_deaths) == deaths

    # ``aq pool status`` says why the pool is empty -- not placement_starved.
    status = await CommandHandler(orch, orch.config)._cmd_pool_status({})
    row = next(pool for pool in status["pools"] if pool["profile_id"] == "worker")
    assert row["desired"] == 0
    assert row["provider_unavailable"]["provider"] == "codex"
    assert row["provider_unavailable"]["state"] == UNAUTHENTICATED


async def test_a_generic_startup_death_still_quarantines_the_key(pool_orch):
    """Only provider-attributed deaths are exempt; a broken checkout is not."""
    orch = pool_orch
    fake = fake_provider(orch)

    original_start = fake.start

    async def dying_start(spec):
        from src.sessions.provider import SessionDiedDuringStartup

        raise SessionDiedDuringStartup(spec.session_name, detail="process died before start")

    fake.start = dying_start
    try:
        await _pool_round(orch)
    finally:
        fake.start = original_start
    assert orch._pool_quarantine
    assert orch.provider_availability.effective_state("codex") != UNAUTHENTICATED


async def test_a_recovering_provider_admits_one_pool_canary(pool_orch):
    orch = pool_orch
    fake = fake_provider(orch)
    fake.script_startup_dialog("codex", "login-required", signal="auth")
    for _ in range(3):
        await _pool_round(orch)
    assert orch.provider_availability.effective_state("codex") == UNAUTHENTICATED

    fake.clear_startup_dialog("codex")
    orch.provider_availability._probe_impl = Probe("authenticated")
    await orch.provider_availability.recheck("codex")
    row = orch.provider_availability.row("codex")
    assert (row.state, row.reason_code) == (DEGRADED, "recovering")

    await _pool_round(orch)
    # Demand is three tasks and max_active two, but probation admits one.
    assert len(fake.starts) == 1


async def test_a_killed_pool_canary_lets_the_next_launch_be_the_canary(pool_orch):
    """The canary died before any evidence; the provider must not wait 10 minutes."""
    orch = pool_orch
    fake = fake_provider(orch)
    fake.script_startup_dialog("codex", "login-required", signal="auth")
    for _ in range(3):
        await _pool_round(orch)
    fake.clear_startup_dialog("codex")
    orch.provider_availability._probe_impl = Probe("authenticated")
    await orch.provider_availability.recheck("codex")
    await _pool_round(orch)
    assert len(fake.starts) == 1
    (canary,) = [row for row in await orch.db.list_sessions() if row.state == "running"]

    await _pool_round(orch)
    assert len(fake.starts) == 1  # still in flight

    # Killed (``aq session kill``, a claim-loop reap) before its first
    # authenticated call: no launch evidence either way.
    await orch.db.update_session(
        canary.id, state="stopped", desired_state="stopped",
        ended_at=time.time(), end_reason="killed",
    )
    await orch.provider_availability.tick()
    await _pool_round(orch)
    assert len(fake.starts) == 2
    row = orch.provider_availability.row("codex")
    assert (row.state, row.reason_code) == (DEGRADED, "recovering")


# -- claim admission -----------------------------------------------------------------------


async def test_an_idle_pool_worker_is_drained_on_its_next_claim(pool_orch, pool_db):
    from src.models import SessionRecord

    orch = pool_orch
    await pool_db.create_session(
        SessionRecord(
            id="sess-1", project_id=POOL_PROJECT, profile_id="worker", harness="codex",
            provider="fake", name="p-worker--proj--x", lifecycle="pool", state="running",
            work_dir="/tmp", epoch="e", instance_token="i", started_at=time.time(),
        )
    )
    handler = CommandHandler(orch, orch.config)
    await orch.provider_availability.set_state(
        "codex", "disabled", by="human:cli", reason="rotating", until=time.time() + 600
    )
    handler._current_scope = {"kind": "session", "session_id": "sess-1", "project_id": POOL_PROJECT}
    result = await handler._cmd_task_claim({"next": True})
    assert result["result"] == "drain_requested"
    assert "codex" in (result.get("reason") or result.get("error") or str(result))
