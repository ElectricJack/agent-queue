"""Doctor checks for pools and claims — spec §16.

Mirrors ``tests/test_hierarchy_checks.py``'s style but against the pool/claim
checks in ``src/doctor/pool_checks.py``.  The real ``CheckResult`` dataclass
(``src/doctor/models.py``) has no ``.count``/``.repairable`` attributes —
counts live in ``result.data["count"]`` and repairability is
``result.fixable`` (set on the check itself, or by ``run_check(repair=True)``
after a fix runs) — so assertions below read those instead of the brief's
placeholder names.
"""

from __future__ import annotations

import json
import sys
import time
from uuid import uuid4

import pytest
from sqlalchemy import insert

import src.doctor  # noqa: F401 -- side effect: populates sys.modules
from src.doctor.models import Severity
from src.database.tables import task_session_attempts
from src.models import (
    Agent,
    AgentProfile,
    AgentState,
    Project,
    RepoSourceType,
    SessionRecord,
    Task,
    TaskStatus,
    Workspace,
)
from tests.db_fixtures import lease_dsn

# ``src/doctor/__init__.py`` does ``from src.doctor.pool_checks import
# pool_checks`` (the factory function) to build ``default_registry()`` --
# symmetric with how it imports ``hierarchy_checks``. That rebinds the
# package's own ``pool_checks`` attribute to the *function*, so any
# ``import src.doctor.pool_checks`` (plain or ``as``) resolves through that
# now-shadowed attribute and hands back the function, not the submodule.
# ``sys.modules`` is unaffected by that rebinding -- ``import src.doctor``
# above (which pulls the submodule in as a side effect) then pulling the
# real submodule out of the module cache is the only way to reach ``.CHECKS``
# / ``.run_check`` unambiguously.
pool_checks = sys.modules["src.doctor.pool_checks"]

PROJECT_ID = "proj"


@pytest.fixture
async def db(tmp_path):
    from src.database import Database

    database = Database(lease_dsn("test.db"))
    await database.initialize()
    await database.create_project(Project(id=PROJECT_ID, name="p"))
    await database.create_profile(AgentProfile(id="worker", name="w", lifecycle="pool"))
    yield database
    await database.close()


def test_check_names():
    names = {c.id for c in pool_checks.CHECKS}
    assert names == {
        "pools.stale_worktree_checkouts",
        "pools.stranded_feature_branches",
        "pools.stuck",
        "pools.orphan_agents",
        "pools.preparing_stuck",
        "pools.disabled",
        "pools.task_lifecycle_shadow",
        "claims.holder_consistency",
        "pools.global_bounds_migration",
        "pools.floor_exceeds_max",
        "pools.placement_starved",
        "agents.dangling_current_task",
    }
    assert all(c.owner == "swarm-work-model" for c in pool_checks.CHECKS)


async def test_task_lifecycle_shadow_reports_duplicate_profiles_and_active_tasks(db):
    await db.update_profile("worker", harness="codex", default_class="deep-high")
    await db.create_profile(
        AgentProfile(
            id="legacy-deep-codex", name="legacy", lifecycle="task", harness="codex",
            default_class="deep-high",
        )
    )
    await db.create_task(
        Task(
            id="shadowed", project_id=PROJECT_ID, title="shadowed", description="",
            status=TaskStatus.READY, profile_id="legacy-deep-codex",
        )
    )
    await db.create_task(
        Task(
            id="missing", project_id=PROJECT_ID, title="missing", description="",
            status=TaskStatus.IN_PROGRESS,
        )
    )

    finding = await pool_checks.run_check(db, "pools.task_lifecycle_shadow", config=None)

    assert finding.severity is Severity.WARN
    assert finding.data["profiles"] == [{
        "profile_id": "legacy-deep-codex", "harness": "codex", "default_class": "deep-high",
        "pool_profile_ids": ["worker"],
    }]
    assert {task["task_id"] for task in finding.data["tasks"]} == {"shadowed", "missing"}


async def test_task_lifecycle_shadow_ignores_the_shipped_role_profiles(db):
    """A fresh install runs every stage at some class a pool also runs at.

    ``aq install`` makes every active rung a pool, so ``reviewer`` (claude,
    standard-high), ``spec-ingest`` and ``supervisor`` (claude, deep-high),
    ``triage`` (claude, fast-low) and ``pr-merger`` (codex, deep-high) all
    share a harness/class with a pool.  None of them is a worker route: they
    are single-purpose stages, and the check must stay clean on a box that
    has changed nothing.
    """
    await db.update_profile("worker", harness="claude", default_class="standard-high")
    for pool_id, harness, default_class in (
        ("deep-high-claude", "claude", "deep-high"),
        ("fast-low-claude", "claude", "fast-low"),
        ("deep-high-codex", "codex", "deep-high"),
    ):
        await db.create_profile(
            AgentProfile(
                id=pool_id, name=pool_id, lifecycle="pool", harness=harness,
                default_class=default_class,
            )
        )
    shipped = (
        ("reviewer", "claude", "standard-high", "task", True),
        ("final-reviewer", "claude", "standard-high", "task", True),
        ("spec-ingest", "claude", "deep-high", "task", False),
        ("playbook-compiler", "claude", "fast-low", "task", False),
        ("triage", "claude", "fast-low", "task", False),
        ("pr-merger", "codex", "deep-high", "task", False),
        ("supervisor", "claude", "deep-high", "named", False),
    )
    for profile_id, harness, default_class, lifecycle, read_only in shipped:
        await db.create_profile(
            AgentProfile(
                id=profile_id, name=profile_id, lifecycle=lifecycle, harness=harness,
                default_class=default_class, read_only=read_only,
            )
        )

    finding = await pool_checks.run_check(db, "pools.task_lifecycle_shadow", config=None)

    assert finding.severity is Severity.OK, finding.data
    assert finding.data["profiles"] == []


async def test_task_lifecycle_shadow_reports_only_a_writable_worker_copy(db):
    """The case the check exists for: a hand-authored generic worker at a pool's route.

    An operator's own read-only profile at the same route is a role, not a
    worker, exactly like the shipped reviewer; the writable task-lifecycle
    copy is the one that starts unpooled sessions on the pool's route.
    """
    await db.update_profile("worker", harness="claude", default_class="standard-high")
    await db.create_profile(
        AgentProfile(
            id="auditor", name="auditor", lifecycle="task", harness="claude",
            default_class="standard-high", read_only=True,
        )
    )
    await db.create_profile(
        AgentProfile(
            id="house-worker", name="house", lifecycle="task", harness="claude",
            default_class="standard-high",
        )
    )

    finding = await pool_checks.run_check(db, "pools.task_lifecycle_shadow", config=None)

    assert finding.severity is Severity.WARN
    assert [row["profile_id"] for row in finding.data["profiles"]] == ["house-worker"]


async def _stale_agent(db, agent_id, **kw):
    """A pool-profile agent old enough to be past the in-flight-launch window.

    ``create_agent`` always stamps ``created_at=time.time()`` server-side
    regardless of the dataclass's own field, so the backdate is a second
    write.
    """
    kw.setdefault("state", AgentState.IDLE)
    kw.setdefault("profile_id", "worker")
    await db.create_agent(Agent(id=agent_id, name=agent_id, **kw))
    await db.update_agent(agent_id, created_at=time.time() - 10_000)


async def _lock_workspace_to(db, agent_id, ws_id="ws"):
    await db.create_workspace(
        Workspace(
            id=ws_id,
            project_id=PROJECT_ID,
            workspace_path="/w",
            source_type=RepoSourceType.CLONE,
            kind_id="project-repo",
            locked_by_agent_id=agent_id,
        )
    )


async def test_leaked_workspace_lock_released_and_agent_kept_reusable(db):
    """A rolled-back launch leaves the lock behind, not a bad definition.

    The row itself is a perfectly good idle worker the next
    ``_launch_pool_session`` will reuse -- only the workspace it never gave
    back is wrong, so the fix releases the lock and leaves the row IDLE.
    """
    await _stale_agent(db, "a1")
    await _lock_workspace_to(db, "a1")

    finding = await pool_checks.run_check(db, "pools.orphan_agents", config=None)
    assert finding.data["count"] == 1
    assert finding.data["leaked_workspace"] == ["a1"]
    assert finding.severity is Severity.WARN

    repaired = await pool_checks.run_check(db, "pools.orphan_agents", config=None, repair=True)
    assert repaired.severity is Severity.OK
    assert (await db.get_workspace("ws")).locked_by_agent_id is None
    agent = await db.get_agent("a1")
    assert agent is not None and agent.state is AgentState.IDLE


async def test_idle_spare_is_not_an_orphan(db):
    """An idle, unowned, lock-free pool definition is the reuse pool.

    Flagging it was the false positive that made ``pools.orphan_agents``
    fire on every healthy install with a pool profile.
    """
    await _stale_agent(db, "a1")
    finding = await pool_checks.run_check(db, "pools.orphan_agents", config=None)
    assert finding.severity is Severity.OK
    assert finding.data["count"] == 0
    assert finding.data["spares"] == ["a1"]


async def test_busy_orphan_is_reported_but_never_touched(db):
    """The push-agent row for a profile that has since become ``lifecycle: pool``.

    No pool session will ever adopt it, but it may still own a task, so
    ``--fix`` must leave both the state and the row alone.
    """
    await db.create_task(
        Task(
            id="t1",
            project_id=PROJECT_ID,
            title="t",
            description="d",
            status=TaskStatus.IN_PROGRESS,
        )
    )
    await _stale_agent(db, "a1", state=AgentState.BUSY, current_task_id="t1")

    finding = await pool_checks.run_check(db, "pools.orphan_agents", config=None)
    assert finding.severity is Severity.ERROR
    assert finding.data["stranded"] == ["a1"]

    # ``run_check(repair=True)`` re-runs the check afterwards, so the
    # post-fix result still reports the same stranded row: the fix declined
    # to touch it, which is the point.
    repaired = await pool_checks.run_check(db, "pools.orphan_agents", config=None, repair=True)
    assert repaired.data["stranded"] == ["a1"]
    assert repaired.severity is Severity.ERROR
    agent = await db.get_agent("a1")
    assert agent is not None
    assert (agent.state, agent.current_task_id) == (AgentState.BUSY, "t1")


async def test_unusable_orphan_is_retired_not_deleted(db):
    await _stale_agent(db, "a1", state=AgentState.ERROR)
    await _lock_workspace_to(db, "a1")

    finding = await pool_checks.run_check(db, "pools.orphan_agents", config=None)
    assert finding.data["retirable"] == ["a1"]

    await pool_checks.run_check(db, "pools.orphan_agents", config=None, repair=True)
    agent = await db.get_agent("a1")
    # Retired, not deleted: the ledger keeps its reference and the row keeps
    # its history.
    assert agent is not None and agent.state is AgentState.RETIRED
    assert (await db.get_workspace("ws")).locked_by_agent_id is None
    events = await db.get_recent_events(event_type="pool.agent_repaired")
    assert len(events) == 1 and "a1" in events[0]["payload"]


async def test_fresh_orphan_agent_not_flagged(db):
    """A pool launch creates the agent row before the session row -- a
    freshly created agent with no session yet is a healthy in-flight
    launch, not an orphan (swarm-work-model §11's ``_launch_pool_session``
    ordering)."""
    await db.create_agent(
        Agent(id="a1", name="a1", profile_id="worker", state=AgentState.ERROR)
    )
    finding = await pool_checks.run_check(db, "pools.orphan_agents", config=None)
    assert finding.data.get("count", 0) == 0
    assert finding.severity is Severity.OK


async def test_old_orphan_agent_flagged_with_explicit_config(db):
    from src.config import AppConfig

    cfg = AppConfig()
    cfg.swarm.prepare_timeout = 5
    await db.create_agent(Agent(id="a1", name="a1", profile_id="worker", state=AgentState.ERROR))
    await db.update_agent("a1", created_at=time.time() - (2 * cfg.swarm.prepare_timeout) - 1)
    finding = await pool_checks.run_check(db, "pools.orphan_agents", config=cfg)
    assert finding.data["count"] == 1


async def test_stuck_pool_session_released(db):
    await db.create_agent(Agent(id="a1", name="a1", profile_id="worker", state=AgentState.BUSY))
    await db.create_task(
        Task(
            id="t1",
            project_id=PROJECT_ID,
            title="t",
            description="d",
            status=TaskStatus.COMPLETED,
        )
    )
    await db.create_session(
        SessionRecord(
            id="s1",
            project_id=PROJECT_ID,
            profile_id="worker",
            harness="claude",
            provider="fake",
            name="s1",
            lifecycle="pool",
            work_dir="/w",
            epoch="e",
            instance_token="t",
            started_at=time.time(),
            state="running",
            agent_id="a1",
            task_id="t1",
            claim_phase="active",
        )
    )
    finding = await pool_checks.run_check(db, "pools.stuck", config=None)
    assert finding.data["count"] == 1
    assert finding.severity is Severity.ERROR
    await pool_checks.run_check(db, "pools.stuck", config=None, repair=True)
    assert (await db.get_session("s1")).task_id is None
    assert (await db.get_agent("a1")).state == AgentState.IDLE


async def test_preparing_stuck_detected_and_repaired(db):
    from src.config import AppConfig

    cfg = AppConfig()
    cfg.swarm.prepare_timeout = 5
    stale_at = time.time() - (2 * cfg.swarm.prepare_timeout) - 1
    await db.create_agent(Agent(id="a1", name="a1", profile_id="worker", state=AgentState.BUSY))
    await db.create_task(
        Task(id="t1", project_id=PROJECT_ID, title="t", description="d", status=TaskStatus.READY)
    )
    await db.create_session(
        SessionRecord(
            id="s1",
            project_id=PROJECT_ID,
            profile_id="worker",
            harness="claude",
            provider="fake",
            name="s1",
            lifecycle="pool",
            work_dir="/w",
            epoch="e",
            instance_token="t",
            started_at=time.time(),
            state="running",
            agent_id="a1",
            task_id="t1",
            claim_phase="preparing",
            claim_phase_at=stale_at,
        )
    )
    finding = await pool_checks.run_check(db, "pools.preparing_stuck", config=cfg)
    assert finding.data["count"] == 1
    await pool_checks.run_check(db, "pools.preparing_stuck", config=cfg, repair=True)
    assert (await db.get_session("s1")).claim_phase is None


async def test_holder_consistency_reports_mismatch(db):
    await db.create_task(
        Task(
            id="other",
            project_id=PROJECT_ID,
            title="other",
            description="d",
            status=TaskStatus.IN_PROGRESS,
        )
    )
    await db.create_agent(
        Agent(
            id="a1", name="a1", profile_id="worker", state=AgentState.BUSY, current_task_id="other"
        )
    )
    await db.create_task(
        Task(
            id="t1",
            project_id=PROJECT_ID,
            title="t",
            description="d",
            status=TaskStatus.IN_PROGRESS,
            assigned_agent_id="a1",
        )
    )
    finding = await pool_checks.run_check(db, "claims.holder_consistency", config=None)
    assert finding.data["count"] == 1
    check = next(c for c in pool_checks.CHECKS if c.id == "claims.holder_consistency")
    assert check.fix is None


async def test_holder_consistency_flags_duplicate_session_holders(db):
    """Two sessions both pointing at the same task's ``task_id`` is exactly
    the anomaly this check exists to catch -- ``get_session_for_task``
    would silently rank-pick one and hide it, so the check must count
    every session with a matching ``task_id`` rather than ask for "the"
    one."""
    # Circular FK (agent.current_task_id -> t1, t1.assigned_agent_id -> a1):
    # create the agent with no task yet, then the task (agent already
    # exists to satisfy its FK), then point the agent at it.
    await db.create_agent(Agent(id="a1", name="a1", profile_id="worker", state=AgentState.BUSY))
    await db.create_task(
        Task(
            id="t1",
            project_id=PROJECT_ID,
            title="t",
            description="d",
            status=TaskStatus.IN_PROGRESS,
            assigned_agent_id="a1",
        )
    )
    await db.update_agent("a1", current_task_id="t1")
    await db.set_task_meta("t1", "claimed_by_session", "s1")
    for sid in ("s1", "s2"):
        await db.create_session(
            SessionRecord(
                id=sid,
                project_id=PROJECT_ID,
                profile_id="worker",
                harness="claude",
                provider="fake",
                name=sid,
                lifecycle="pool",
                work_dir="/w",
                epoch="e",
                instance_token="t",
                started_at=time.time(),
                state="running",
                agent_id="a1",
                task_id="t1",
                claim_phase="active",
            )
        )
    finding = await pool_checks.run_check(db, "claims.holder_consistency", config=None)
    assert finding.data["count"] == 1


async def test_holder_consistency_ignores_push_launched_holder(db):
    """A ``lifecycle: task`` session holding its task is healthy (I1).

    ``claimed_by_session`` is written only by ``record_holder`` on the claim
    path, so a push-launched session never has one -- which used to make
    this check WARN on every healthy push-launched task.
    """
    await db.create_profile(AgentProfile(id="pusher", name="p", lifecycle="task"))
    await db.create_agent(Agent(id="a1", name="a1", profile_id="pusher", state=AgentState.BUSY))
    await db.create_task(
        Task(
            id="t1",
            project_id=PROJECT_ID,
            title="t",
            description="d",
            status=TaskStatus.IN_PROGRESS,
            assigned_agent_id="a1",
        )
    )
    await db.update_agent("a1", current_task_id="t1")
    await db.create_session(
        SessionRecord(
            id="s1",
            project_id=PROJECT_ID,
            profile_id="pusher",
            harness="claude",
            provider="fake",
            name="s1",
            lifecycle="task",
            work_dir="/w",
            epoch="e",
            instance_token="t",
            started_at=time.time(),
            state="running",
            agent_id="a1",
            task_id="t1",
        )
    )
    finding = await pool_checks.run_check(db, "claims.holder_consistency", config=None)
    assert finding.severity is Severity.OK
    assert finding.data.get("count", 0) == 0


async def test_orphan_agents_matches_any_pool_profile(db):
    """Profiles are global, so ``agents.profile_id`` is the profile id verbatim."""
    from src.config import AppConfig

    await db.create_profile(AgentProfile(id="scoped", name="scoped", lifecycle="pool"))
    cfg = AppConfig()
    cfg.swarm.prepare_timeout = 5
    await db.create_agent(Agent(id="a9", name="a9", profile_id="scoped", state=AgentState.ERROR))
    await db.update_agent("a9", created_at=time.time() - (2 * cfg.swarm.prepare_timeout) - 1)
    finding = await pool_checks.run_check(db, "pools.orphan_agents", config=cfg)
    assert finding.data["count"] == 1
    assert finding.data["retirable"] == ["a9"]


async def test_pools_disabled_warns_when_flag_off(db):
    """I5 / ruling P2-17: pool profiles + ``swarm.enabled=False`` strands work."""
    from src.config import AppConfig

    cfg = AppConfig()
    cfg.swarm.enabled = False
    finding = await pool_checks.run_check(db, "pools.disabled", config=cfg)
    assert finding.severity is Severity.WARN
    assert finding.data["count"] == 1
    assert "worker" in finding.data["profiles"]
    check = next(c for c in pool_checks.CHECKS if c.id == "pools.disabled")
    assert check.fix is None


async def test_pools_disabled_ok_when_flag_on(db):
    from src.config import AppConfig

    cfg = AppConfig()
    cfg.swarm.enabled = True
    finding = await pool_checks.run_check(db, "pools.disabled", config=cfg)
    assert finding.severity is Severity.OK


# ---------------------------------------------------------------------------
# pools.global_bounds_migration
# ---------------------------------------------------------------------------


async def _rescoped(db, profile_id, *, eligible, max_active):
    """Write the audit row ``_announce_bounds_rescoped`` writes at upgrade."""
    await db.log_event(
        "pool.bounds_rescoped",
        payload=json.dumps(
            {
                "profile_id": profile_id,
                "eligible_projects": eligible,
                "effective_max_active": max_active,
                "effective_min_active": 0,
                "previous_effective_max_active": (
                    None if max_active is None else max_active * eligible
                ),
                "previous_effective_min_active": 0,
            },
            sort_keys=True,
        ),
    )


async def test_bounds_migration_reports_the_lost_ceiling(db):
    await db.update_profile("worker", max_active=4)
    await _rescoped(db, "worker", eligible=5, max_active=4)
    finding = await pool_checks.run_check(db, "pools.global_bounds_migration")
    assert finding.severity is Severity.INFO
    assert finding.data["count"] == 1
    entry = finding.data["profiles"][0]
    assert entry["previous_effective_max_active"] == 20
    assert entry["effective_max_active"] == 4
    assert entry["suggested_max_active"] == 20
    assert entry["command"] == "aq pool scale --profile-id worker --max 20"


async def test_bounds_migration_self_resolves_once_max_active_changes(db):
    """Re-scaling the pool *is* the acknowledgement — whichever way it went."""
    await db.update_profile("worker", max_active=4)
    await _rescoped(db, "worker", eligible=5, max_active=4)
    await db.update_profile("worker", max_active=6)
    finding = await pool_checks.run_check(db, "pools.global_bounds_migration")
    assert finding.severity is Severity.OK
    assert finding.data["count"] == 0


async def test_bounds_migration_reads_the_oldest_record_not_the_newest(db):
    """A restart re-announces; the notice must not reset with it.

    ``_announce_bounds_rescoped`` runs once per daemon lifetime, so a box that
    reboots after the operator has re-scaled writes a *second* row carrying
    the new ceiling.  Reading the newest row would compare the new ceiling
    against itself and start nagging again forever.
    """
    await db.update_profile("worker", max_active=4)
    await _rescoped(db, "worker", eligible=5, max_active=4)
    await db.update_profile("worker", max_active=20)
    await _rescoped(db, "worker", eligible=5, max_active=20)
    finding = await pool_checks.run_check(db, "pools.global_bounds_migration")
    assert finding.severity is Severity.OK


async def test_bounds_migration_silent_for_a_single_project_install(db):
    """One eligible project: per-project and fleet-wide bounds meant the same."""
    await db.update_profile("worker", max_active=4)
    await _rescoped(db, "worker", eligible=1, max_active=4)
    finding = await pool_checks.run_check(db, "pools.global_bounds_migration")
    assert finding.severity is Severity.OK


async def test_bounds_migration_silent_without_an_upgrade_record(db):
    """No reconcile tick has ever run — there is nothing to say."""
    await db.update_profile("worker", max_active=4)
    finding = await pool_checks.run_check(db, "pools.global_bounds_migration")
    assert finding.severity is Severity.OK


async def test_bounds_migration_ignores_an_unbounded_pool(db):
    await _rescoped(db, "worker", eligible=5, max_active=None)
    finding = await pool_checks.run_check(db, "pools.global_bounds_migration")
    assert finding.severity is Severity.OK


async def test_bounds_migration_survives_a_junk_payload(db):
    """Doctor reporting nothing beats doctor raising on a hand-written row."""
    await db.log_event("pool.bounds_rescoped", payload="not json")
    finding = await pool_checks.run_check(db, "pools.global_bounds_migration")
    assert finding.severity is Severity.OK


def test_bounds_migration_has_no_fix():
    """§4: choosing a fleet size is an operator decision, explicitly."""
    check = next(c for c in pool_checks.CHECKS if c.id == "pools.global_bounds_migration")
    assert check.fix is None


# ---------------------------------------------------------------------------
# pools.floor_exceeds_max
# ---------------------------------------------------------------------------


async def test_floor_exceeds_max_names_the_numbers_and_the_projects(db):
    await db.create_project(Project(id="second", name="second"))
    await db.create_project(Project(id="third", name="third"))
    await db.update_profile("worker", min_active=1, max_active=2, min_per_project=1)
    finding = await pool_checks.run_check(db, "pools.floor_exceeds_max")
    assert finding.severity is Severity.WARN
    assert finding.data["count"] == 1
    entry = finding.data["profiles"][0]
    assert entry["effective_min_active"] == 3
    assert entry["max_active"] == 2
    assert entry["projects"] == ["proj", "second", "third"]
    assert "worker" in finding.detail and "min_per_project 1" in finding.detail


async def test_floor_exceeds_max_ok_when_the_ceiling_funds_it(db):
    await db.create_project(Project(id="second", name="second"))
    await db.update_profile("worker", min_active=1, max_active=4, min_per_project=1)
    finding = await pool_checks.run_check(db, "pools.floor_exceeds_max")
    assert finding.severity is Severity.OK


async def test_floor_exceeds_max_counts_only_active_projects(db):
    """Eligibility matches ``_measure_pools``: ACTIVE projects, nothing else."""
    from src.models import ProjectStatus

    await db.create_project(Project(id="second", name="second"))
    await db.create_project(Project(id="paused", name="paused"))
    await db.update_project("paused", status=ProjectStatus.PAUSED)
    await db.update_profile("worker", max_active=2, min_per_project=1)
    finding = await pool_checks.run_check(db, "pools.floor_exceeds_max")
    assert finding.severity is Severity.OK


async def test_floor_exceeds_max_catches_a_bare_min_active(db):
    """``min_per_project`` is not required — ``min_active`` alone can overshoot."""
    await db.update_profile("worker", min_active=5, max_active=2)
    finding = await pool_checks.run_check(db, "pools.floor_exceeds_max")
    assert finding.severity is Severity.WARN
    assert finding.data["profiles"][0]["effective_min_active"] == 5


async def test_floor_exceeds_max_ignores_an_unbounded_pool(db):
    await db.update_profile("worker", min_active=9, max_active=None, min_per_project=3)
    finding = await pool_checks.run_check(db, "pools.floor_exceeds_max")
    assert finding.severity is Severity.OK


def test_floor_exceeds_max_has_no_fix():
    check = next(c for c in pool_checks.CHECKS if c.id == "pools.floor_exceeds_max")
    assert check.fix is None


# ---------------------------------------------------------------------------
# pools.placement_starved
# ---------------------------------------------------------------------------


class _FakeHandler:
    """The one thing the check reaches for: ``handler.orchestrator``."""

    def __init__(self, orchestrator):
        self.orchestrator = orchestrator


class _FakeOrchestrator:
    def __init__(self, state, observing_since):
        self._pool_starvation_state = state
        self._pool_starvation_observing_since = observing_since


def _starving(db, state, observing_since):
    return pool_checks.run_check(
        db,
        "pools.placement_starved",
        handler=_FakeHandler(_FakeOrchestrator(state, observing_since)),
    )


async def test_placement_starved_warns_past_five_minutes_with_reasons(db):
    now = time.time()
    state = {
        "worker": {
            "since": now - 900,
            "wanted": 2,
            "reasons": {"proj": "no_workspace", "second": "quarantined"},
        }
    }
    finding = await _starving(db, state, now - 3600)
    assert finding.severity is Severity.WARN
    assert finding.data["count"] == 1
    entry = finding.data["profiles"][0]
    assert entry["wanted"] == 2
    assert entry["reasons"] == {"proj": "no_workspace", "second": "quarantined"}
    assert "proj: no_workspace" in finding.detail
    assert "second: quarantined" in finding.detail


async def test_placement_starved_ok_below_the_threshold(db):
    now = time.time()
    state = {"worker": {"since": now - 30, "wanted": 1, "reasons": {"proj": "at_cap"}}}
    finding = await _starving(db, state, now - 3600)
    assert finding.severity is Severity.OK
    assert finding.data["count"] == 0
    assert finding.data["recent"][0]["profile_id"] == "worker"


async def test_placement_starved_ok_when_nothing_is_starved(db):
    finding = await _starving(db, {}, time.time() - 3600)
    assert finding.severity is Severity.OK
    assert finding.data["count"] == 0


async def test_placement_starved_says_so_when_the_daemon_is_young(db):
    """Honest about not knowing: a young daemon cannot see a five-minute history."""
    now = time.time()
    state = {"worker": {"since": now - 320, "wanted": 1, "reasons": {"proj": "at_cap"}}}
    finding = await _starving(db, state, now - 330)
    assert finding.severity is Severity.WARN
    assert "only been observing placement" in finding.detail


async def test_placement_starved_info_without_a_reachable_orchestrator(db):
    """INFO, never OK: doctor must not report health it cannot observe."""
    finding = await pool_checks.run_check(db, "pools.placement_starved")
    assert finding.severity is Severity.INFO
    assert "not visible from here" in finding.detail


def test_placement_starved_has_no_fix():
    check = next(c for c in pool_checks.CHECKS if c.id == "pools.placement_starved")
    assert check.fix is None


# ---------------------------------------------------------------------------
# agents.dangling_current_task
# ---------------------------------------------------------------------------


async def _live_session_and_attempt(db, agent_id, task_id, *, now):
    await db.create_session(
        SessionRecord(
            id=f"s-{agent_id}",
            project_id=PROJECT_ID,
            profile_id="worker",
            harness="claude",
            provider="tmux",
            name=f"s-{agent_id}",
            lifecycle="pool",
            work_dir="/w",
            epoch="e",
            instance_token=agent_id,
            started_at=now - 60,
            task_id=task_id,
            state="running",
            last_activity=now - 10,
        )
    )
    async with db._engine.begin() as conn:
        await conn.execute(
            insert(task_session_attempts).values(
                id=uuid4().hex,
                session_id=f"s-{agent_id}",
                task_id=task_id,
                project_id=PROJECT_ID,
                agent_id=agent_id,
                agent_name=agent_id,
                profile_id="worker",
                name="worker",
                lifecycle="pool",
                model="claude-opus-5",
                harness="claude",
                provider="tmux",
                state="running",
                work_dir="/w",
                started_at=now - 60,
                session_started_at=now - 60,
                ended_at=None,
            )
        )


async def test_dangling_current_task_ok_when_clean(db):
    await db.create_task(
        Task(
            id="t1", project_id=PROJECT_ID, title="t", description="",
            status=TaskStatus.IN_PROGRESS,
        )
    )
    await _stale_agent(db, "a1", state=AgentState.BUSY, current_task_id="t1")

    finding = await pool_checks.run_check(db, "agents.dangling_current_task", config=None)
    assert finding.severity is Severity.OK
    assert finding.data.get("count", 0) == 0


async def test_dangling_current_task_warns_on_completed_task(db):
    await db.create_task(
        Task(
            id="t1", project_id=PROJECT_ID, title="t", description="",
            status=TaskStatus.COMPLETED,
        )
    )
    await _stale_agent(db, "a1", state=AgentState.IDLE, current_task_id="t1")

    finding = await pool_checks.run_check(db, "agents.dangling_current_task", config=None)
    assert finding.severity is Severity.WARN
    assert finding.data["agents"] == [
        {"agent_id": "a1", "task_id": "t1", "task_status": "COMPLETED"}
    ]


async def test_dangling_current_task_skipped_with_a_live_attempt(db):
    """A BUSY agent whose task went COMPLETED but is still finishing its
    write-up (a live attempt) is not dangling -- only the state-transition
    race, not a mid-flight worker."""
    now = time.time()
    await db.create_task(
        Task(
            id="t1", project_id=PROJECT_ID, title="t", description="",
            status=TaskStatus.COMPLETED,
        )
    )
    await _stale_agent(db, "a1", state=AgentState.BUSY, current_task_id="t1")
    await _live_session_and_attempt(db, "a1", "t1", now=now)

    finding = await pool_checks.run_check(db, "agents.dangling_current_task", config=None)
    assert finding.severity is Severity.OK
    assert finding.data.get("count", 0) == 0


async def test_dangling_current_task_fix_clears_the_agent(db):
    await db.create_task(
        Task(
            id="t1", project_id=PROJECT_ID, title="t", description="",
            status=TaskStatus.FAILED,
        )
    )
    await _stale_agent(db, "a1", state=AgentState.BUSY, current_task_id="t1")

    repaired = await pool_checks.run_check(
        db, "agents.dangling_current_task", config=None, repair=True
    )
    assert repaired.severity is Severity.OK
    assert repaired.data.get("count", 0) == 0

    agent = await db.get_agent("a1")
    assert agent is not None
    assert agent.state is AgentState.IDLE
    assert agent.current_task_id is None


@pytest.mark.parametrize(
    "state", [AgentState.ERROR, AgentState.RETIRED, AgentState.PAUSED]
)
async def test_dangling_current_task_fix_keeps_a_non_busy_state(db, state):
    """The fix clears the pointer; it must not also un-retire the agent.

    Production sets ERROR/RETIRED without clearing ``current_task_id``, and
    RETIRED is what gates workspace reclaim -- rewriting it to IDLE would
    hand a retired agent's workspace back to the scheduler.
    """
    await db.create_task(
        Task(
            id="t1", project_id=PROJECT_ID, title="t", description="",
            status=TaskStatus.FAILED,
        )
    )
    await _stale_agent(db, "a1", state=state, current_task_id="t1")

    repaired = await pool_checks.run_check(
        db, "agents.dangling_current_task", config=None, repair=True
    )
    assert repaired.severity is Severity.OK

    agent = await db.get_agent("a1")
    assert agent is not None
    assert agent.state is state
    assert agent.current_task_id is None


async def test_reset_stale_busy_agent_keeps_the_state_when_asked(db):
    """The reconciler's own rescue is unchanged; only the doctor opts out."""
    from src.orchestrator.agent_reconciler import reset_stale_busy_agent

    await db.create_task(
        Task(
            id="t1", project_id=PROJECT_ID, title="t", description="",
            status=TaskStatus.FAILED,
        )
    )
    await _stale_agent(db, "a1", state=AgentState.ERROR, current_task_id="t1")

    await reset_stale_busy_agent(db, "a1", reset_state=False)
    agent = await db.get_agent("a1")
    assert agent.state is AgentState.ERROR
    assert agent.current_task_id is None

    await db.update_agent("a1", state=AgentState.BUSY, current_task_id="t1")
    await reset_stale_busy_agent(db, "a1")
    agent = await db.get_agent("a1")
    assert agent.state is AgentState.IDLE
    assert agent.current_task_id is None


def test_dangling_current_task_is_fixable():
    check = next(c for c in pool_checks.CHECKS if c.id == "agents.dangling_current_task")
    assert check.fix is not None
