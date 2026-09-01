"""pools.* / claims.* doctor checks (spec §16, swarm-work-model §9-§11).

Registered by the core registry alongside ``hierarchy_checks`` — same
dataclass (``CheckResult``/``DoctorCheck``) and registration API.  Mirrors
``src/doctor/hierarchy_checks.py``'s shape: a private ``_check_*``/``_fix_*``
pair per check, a factory that returns the list of :class:`DoctorCheck`, plus
a ``CHECKS`` snapshot and a ``run_check`` convenience wrapper used by tests
and any one-off invocation that doesn't want to spin up a full registry.

Pool agent rows — the rule
--------------------------

``_launch_pool_session`` creates one ``agents`` row per pool session and
``_terminate_pool_session`` gives it back.  *Which state* it comes back in is
the whole rule, and both halves matter:

* **Confirmed stop → ``IDLE``.**  Teardown that saw ``provider.stop`` succeed
  returns the definition to the reuse pool, and the next launch draws from
  ``list_agents(state=IDLE)``.  That reuse is what bounds the roster at
  roughly ``max_active`` per pool instead of growing it by one row per
  claimed task — what the agent-flock design means by "pools ... may reuse
  idle definitions after safe termination".
* **Unconfirmed stop → ``RETIRED``.**  ``_terminate_pool_session_locked``
  marks the row ``RETIRED`` *before* stopping the process and clears it back
  to ``IDLE`` only once the stop is confirmed, so a worker whose process may
  still be alive is never handed to a second session.

``RETIRED`` rows are therefore rare, and nothing reaps them automatically:
``soft_delete_agent`` cannot, because ``create_automatic_agent`` refuses to
grow the roster while *any* worker tombstone exists, and a hard delete would
drop history the task ledger still references.

``pools.orphan_agents`` polices the rows that fall outside that loop — a
pool-profile agent with no session row at all, stale by ``2 x prepare_timeout``
so an in-flight launch is never caught.  Four shapes, four verdicts:

* idle, enabled, unowned, holding no workspace — the reuse pool; left alone.
* idle but still holding a workspace lock — a rolled-back launch leaked it;
  the lock is released and the row stays IDLE and reusable.
* busy, or ``current_task_id`` set — reported, never touched.  This is the
  "fixed push-agent row for a profile that has since become ``lifecycle:
  pool``" case: no pool session will ever adopt it, but it may still own a
  task, so retiring or deleting it would strand that task.
* disabled, or ``ERROR``/``PAUSED`` — unusable and unowned; retired, never
  deleted.

Every repair writes one ``pool.agent_repaired`` event, so ``aq events
--event-type pool.agent_repaired`` answers "why is this worker RETIRED?"
long after the doctor run has scrolled away.
"""

from __future__ import annotations

import time

from src.doctor.models import CheckResult, DoctorCheck, DoctorContext, Severity
from src.doctor.runner import apply_fix
from src.models import AgentState, TaskStatus

OWNER = "swarm-work-model"

_LIVE_TASK_STATUSES = (TaskStatus.IN_PROGRESS, TaskStatus.ASSIGNED)


async def _pool_profile_ids(db) -> set[str]:
    """Bare agent-type ids of every ``lifecycle: pool`` profile.

    A project-scoped profile is stored as ``<project>:<agent-type>`` while
    ``agents.profile_id`` holds the bare agent-type id, so the id has to be
    normalised before it can be matched against an agent — the same
    ``rsplit(":", 1)[-1]`` the orchestrator's reaper (``core.py``) and
    ``_reconcile_pools`` (``pools.py``) use.
    """
    return {
        p.id.rsplit(":", 1)[-1]
        for p in await db.list_profiles()
        if getattr(p, "lifecycle", "task") == "pool"
    }


def _no_db_result(check_id: str) -> CheckResult:
    return CheckResult(
        id=check_id,
        severity=Severity.INFO,
        detail="database not initialised — pool state unknown",
    )


# ---------------------------------------------------------------------------
# pools.stuck
# ---------------------------------------------------------------------------


async def _find_stuck_pool_sessions(ctx: DoctorContext):
    sessions = await ctx.db.list_sessions(lifecycle="pool", state="running")
    bad = []
    for s in sessions:
        if not s.task_id:
            continue
        task = await ctx.db.get_task(s.task_id)
        if task is None or task.status not in _LIVE_TASK_STATUSES:
            bad.append((s, task))
    return bad


async def _check_pools_stuck(ctx: DoctorContext) -> CheckResult:
    if ctx.db is None:
        return _no_db_result("pools.stuck")
    bad = await _find_stuck_pool_sessions(ctx)
    if not bad:
        return CheckResult(id="pools.stuck", severity=Severity.OK, detail="no stuck pool sessions")
    return CheckResult(
        id="pools.stuck",
        severity=Severity.ERROR,
        detail=f"{len(bad)} pool session(s) holding a task that is no longer IN_PROGRESS/ASSIGNED",
        data={
            "count": len(bad),
            "sessions": [
                {
                    "session_id": s.id,
                    "task_id": s.task_id,
                    "task_status": t.status.value if t else None,
                }
                for s, t in bad[:50]
            ],
        },
    )


async def _fix_pools_stuck(ctx: DoctorContext) -> CheckResult:
    if ctx.db is None:
        return _no_db_result("pools.stuck")
    bad = await _find_stuck_pool_sessions(ctx)
    now = time.time()
    for s, task in bad:
        task_status = task.status if task is not None else TaskStatus.READY
        await ctx.db.release_claim(
            s.id,
            task_status=task_status,
            context="doctor",
            now=now,
            result="released",
            needs_attention="doctor_stuck_pool_session",
        )
    return CheckResult(
        id="pools.stuck",
        severity=Severity.OK,
        detail=f"released {len(bad)} stuck pool session(s)",
    )


# ---------------------------------------------------------------------------
# pools.orphan_agents
# ---------------------------------------------------------------------------


async def _find_orphan_agents(ctx: DoctorContext):
    """Classify every pool-profile agent row that has no session row at all.

    Returns ``(leaked, stranded, retirable, spares)`` — see the module
    docstring's "pool agent rows" rule for what each bucket means and what
    ``--fix`` is allowed to do to it.

    A pool launch creates the agent row first, then acquires a workspace,
    then writes the session row (``_launch_pool_session``) -- there is a
    real window, seconds wide, where a perfectly healthy in-flight launch
    has an agent with no session yet. Flagging on that window would make
    the check (and ``--fix``) race the launch and act on an agent mid-boot.
    Gate on the same ``2 x prepare_timeout`` staleness ``pools.preparing_stuck``
    uses for its own "this has been mid-flight too long" judgment call.
    """
    pool_profile_ids = await _pool_profile_ids(ctx.db)
    if not pool_profile_ids:
        return [], [], [], []
    threshold = time.time() - 2 * _prepare_timeout(ctx)
    leaked, stranded, retirable, spares = [], [], [], []
    for agent in await ctx.db.list_agents():
        if agent.profile_id not in pool_profile_ids:
            continue
        if (agent.created_at or 0.0) > threshold:
            continue
        # Unfiltered: ``stopped`` rows count. A worker that has actually run
        # keeps its session history, so a normally-drained pool agent is
        # never an orphan here at all. What reaches the buckets below is a
        # row that never got a session row -- a rolled-back launch, or a push
        # agent for a profile that has since become ``lifecycle: pool``.
        if await ctx.db.list_sessions(agent_id=agent.id):
            continue
        if agent.state is AgentState.BUSY or agent.current_task_id:
            # May still own a task.  Retiring or deleting it here would
            # strand that task with no way back; a human decides.
            stranded.append(agent)
        elif not agent.enabled or agent.state in (AgentState.ERROR, AgentState.PAUSED):
            retirable.append(agent)
        elif await ctx.db.get_workspace_for_agent(agent.id) is not None:
            leaked.append(agent)
        else:
            spares.append(agent)
    return leaked, stranded, retirable, spares


def _agent_ids(rows) -> list[str]:
    return [a.id for a in rows[:50]]


async def _check_orphan_agents(ctx: DoctorContext) -> CheckResult:
    if ctx.db is None:
        return _no_db_result("pools.orphan_agents")
    leaked, stranded, retirable, spares = await _find_orphan_agents(ctx)
    data = {
        "count": len(leaked) + len(stranded) + len(retirable),
        "leaked_workspace": _agent_ids(leaked),
        "stranded": _agent_ids(stranded),
        "retirable": _agent_ids(retirable),
        # Reported so an operator can see the reuse pool, never acted on.
        "spares": _agent_ids(spares),
    }
    if not data["count"]:
        return CheckResult(
            id="pools.orphan_agents",
            severity=Severity.OK,
            detail=(
                f"no orphaned pool agents ({len(spares)} idle definition(s) available for reuse)"
            ),
            data=data,
        )
    parts = []
    if leaked:
        parts.append(f"{len(leaked)} holding a workspace lock with no session")
    if retirable:
        parts.append(f"{len(retirable)} unusable and unowned (retirable)")
    if stranded:
        parts.append(f"{len(stranded)} busy or holding a task with no session (needs a human)")
    return CheckResult(
        id="pools.orphan_agents",
        severity=Severity.ERROR if stranded else Severity.WARN,
        detail="pool-profile agent(s) with no session row: " + "; ".join(parts),
        data=data,
    )


async def _fix_orphan_agents(ctx: DoctorContext) -> CheckResult:
    if ctx.db is None:
        return _no_db_result("pools.orphan_agents")
    leaked, stranded, retirable, _spares = await _find_orphan_agents(ctx)
    for agent in leaked:
        # The definition itself is fine and reusable -- only the lock the
        # rolled-back launch failed to give back is wrong.
        await ctx.db.release_workspaces_for_agent(agent.id)
        await _audit(ctx, agent, "workspace_lock_released")
    for agent in retirable:
        await ctx.db.release_workspaces_for_agent(agent.id)
        await ctx.db.update_agent(agent.id, state=AgentState.RETIRED, current_task_id=None)
        await _audit(ctx, agent, "retired_unusable_orphan")
    detail = f"released {len(leaked)} leaked workspace lock(s), retired {len(retirable)} agent(s)"
    if stranded:
        detail += (
            f"; left {len(stranded)} busy/task-holding agent(s) untouched "
            "(retire or reassign them by hand)"
        )
    return CheckResult(
        id="pools.orphan_agents",
        severity=Severity.ERROR if stranded else Severity.OK,
        detail=detail,
        data={
            "released": _agent_ids(leaked),
            "retired": _agent_ids(retirable),
            "skipped_busy": _agent_ids(stranded),
        },
    )


async def _audit(ctx: DoctorContext, agent, reason: str) -> None:
    """One durable row per repaired agent -- never a silent mutation.

    ``aq events --event-type pool.agent_repaired`` is the answer to "why is
    this worker RETIRED?" long after the doctor run has scrolled away.
    """
    try:
        await ctx.db.log_event(
            "pool.agent_repaired",
            agent_id=agent.id,
            payload=f"{reason} {agent.id} ({agent.profile_id})",
        )
    except Exception:  # pragma: no cover - an audit failure must not block the repair
        pass


# ---------------------------------------------------------------------------
# pools.preparing_stuck
# ---------------------------------------------------------------------------


def _prepare_timeout(ctx: DoctorContext) -> int:
    swarm = getattr(ctx.config, "swarm", None)
    return getattr(swarm, "prepare_timeout", 120) if swarm is not None else 120


async def _find_preparing_stuck(ctx: DoctorContext):
    threshold = time.time() - 2 * _prepare_timeout(ctx)
    bad = []
    for phase in ("claiming", "preparing"):
        for s in await ctx.db.list_sessions(lifecycle="pool", claim_phase=phase):
            if (s.claim_phase_at or 0.0) <= threshold:
                bad.append(s)
    return bad


async def _check_preparing_stuck(ctx: DoctorContext) -> CheckResult:
    if ctx.db is None:
        return _no_db_result("pools.preparing_stuck")
    bad = await _find_preparing_stuck(ctx)
    if not bad:
        return CheckResult(
            id="pools.preparing_stuck", severity=Severity.OK, detail="no stuck claim/prepare"
        )
    return CheckResult(
        id="pools.preparing_stuck",
        severity=Severity.ERROR,
        detail=(f"{len(bad)} pool session(s) stuck in claiming/preparing past 2x prepare_timeout"),
        data={"count": len(bad), "sessions": [s.id for s in bad[:50]]},
    )


async def _fix_preparing_stuck(ctx: DoctorContext) -> CheckResult:
    if ctx.db is None:
        return _no_db_result("pools.preparing_stuck")
    bad = await _find_preparing_stuck(ctx)
    now = time.time()
    for s in bad:
        if s.task_id:
            await ctx.db.release_claim(
                s.id,
                task_status=TaskStatus.READY,
                context="doctor_prepare_timeout",
                now=now,
                result="prepare_failed",
                needs_attention="prepare_timeout",
            )
        else:
            await ctx.db.update_session(s.id, claim_phase=None, claim_phase_at=None)
    return CheckResult(
        id="pools.preparing_stuck",
        severity=Severity.OK,
        detail=f"released {len(bad)} claim/prepare session(s) stuck past timeout",
    )


# ---------------------------------------------------------------------------
# pools.disabled (report-only — no fix)
# ---------------------------------------------------------------------------


async def _check_pools_disabled(ctx: DoctorContext) -> CheckResult:
    """Report-only: pool profiles exist but ``swarm.enabled`` is False.

    Ruling P2-17.  The two gates that keep a ``lifecycle: pool`` profile
    from being push-launched are lifecycle-only (they do not consult
    ``swarm.enabled``), while ``_reconcile_pools`` -- the only thing that
    *does* launch pool workers -- is flag-gated.  So with the flag off a
    pool profile's tasks are never pushed and never claimed: they sit in
    READY forever with nothing in the log to say why.  Both gates are
    correct as they stand; what was missing is anyone saying so out loud.
    """
    if ctx.db is None:
        return _no_db_result("pools.disabled")
    swarm = getattr(ctx.config, "swarm", None)
    enabled = getattr(swarm, "enabled", True) if swarm is not None else True
    pool_profile_ids = await _pool_profile_ids(ctx.db)
    if enabled or not pool_profile_ids:
        return CheckResult(
            id="pools.disabled",
            severity=Severity.OK,
            detail=(
                "no pool profiles configured"
                if not pool_profile_ids
                else "swarm.enabled is true"
            ),
        )
    ids = sorted(pool_profile_ids)
    return CheckResult(
        id="pools.disabled",
        severity=Severity.WARN,
        detail=(
            f"{len(ids)} pool profile(s) configured but swarm.enabled is false — "
            "their tasks are never pushed and never claimed (report-only)"
        ),
        data={"count": len(ids), "profiles": ids[:50]},
    )


# ---------------------------------------------------------------------------
# claims.holder_consistency (report-only — no fix)
# ---------------------------------------------------------------------------


async def _check_holder_consistency(ctx: DoctorContext) -> CheckResult:
    """Report-only: ``get_session_for_task`` deliberately hides duplicates.

    It ranks by state/recency and returns its single best guess -- exactly
    the wrong tool here, since "two sessions both think they hold this
    task" is itself the anomaly this check exists to catch. Counts every
    session whose ``task_id`` matches instead of asking for "the" one.

    Scope: **claim-path** holders only.  ``claimed_by_session`` is written
    by ``record_holder``, which only the claim path calls, so a
    push-launched (``lifecycle: task``) session legitimately holds its task
    with no such meta value -- treating that as an anomaly warned on every
    healthy push-launched task.  A task held *only* by non-pool sessions is
    therefore skipped.  A task with no holder session at all is still
    flagged -- an IN_PROGRESS task with an assigned agent and nobody
    holding it is an anomaly whichever path put it there.  Within pool
    holders a missing ``claimed_by_session`` is still an anomaly, not a
    free pass.
    """
    if ctx.db is None:
        return _no_db_result("claims.holder_consistency")
    sessions_by_task: dict[str, list[str]] = {}
    pool_session_ids: set[str] = set()
    for s in await ctx.db.list_sessions():
        if getattr(s, "lifecycle", "task") == "pool":
            pool_session_ids.add(s.id)
        if s.task_id:
            sessions_by_task.setdefault(s.task_id, []).append(s.id)
    bad = []
    for task in await ctx.db.list_tasks(status=TaskStatus.IN_PROGRESS):
        if not task.assigned_agent_id:
            continue
        holders = sessions_by_task.get(task.id, [])
        if holders and not any(h in pool_session_ids for h in holders):
            continue
        agent = await ctx.db.get_agent(task.assigned_agent_id)
        meta = await ctx.db.get_task_meta(task.id, "claimed_by_session")
        ok = (
            agent is not None
            and agent.current_task_id == task.id
            and len(holders) == 1
            and meta is not None
            and meta == holders[0]
        )
        if not ok:
            bad.append(task.id)
    if not bad:
        return CheckResult(
            id="claims.holder_consistency", severity=Severity.OK, detail="holders consistent"
        )
    return CheckResult(
        id="claims.holder_consistency",
        severity=Severity.WARN,
        detail=f"{len(bad)} IN_PROGRESS task(s) with an inconsistent claim holder (report-only)",
        data={"count": len(bad), "tasks": bad[:50]},
    )


def pool_checks() -> list[DoctorCheck]:
    return [
        DoctorCheck(id="pools.stuck", run=_check_pools_stuck, fix=_fix_pools_stuck, owner=OWNER),
        DoctorCheck(
            id="pools.orphan_agents",
            run=_check_orphan_agents,
            fix=_fix_orphan_agents,
            owner=OWNER,
        ),
        DoctorCheck(
            id="pools.preparing_stuck",
            run=_check_preparing_stuck,
            fix=_fix_preparing_stuck,
            owner=OWNER,
        ),
        # Report-only: no ``fix`` — flipping ``swarm.enabled`` is an operator
        # decision, not a repair (they may have disabled it deliberately).
        DoctorCheck(id="pools.disabled", run=_check_pools_disabled, owner=OWNER),
        # Report-only: no ``fix`` — a claim/holder mismatch needs a human to
        # decide which side (agent, session, or task) is authoritative.
        DoctorCheck(id="claims.holder_consistency", run=_check_holder_consistency, owner=OWNER),
    ]


#: Snapshot for convenience call-sites (tests, ad-hoc scripts) that want the
#: list without building a full :class:`~src.doctor.runner.DoctorRegistry`.
CHECKS = pool_checks()

_BY_ID = {c.id: c for c in CHECKS}


async def run_check(db, check_id: str, *, config=None, repair: bool = False) -> CheckResult:
    """Run one pool/claim check directly against *db* (no registry needed).

    ``repair=True`` runs the check's ``fix`` (if any) then re-runs the check,
    mirroring :func:`src.doctor.runner.apply_fix`.
    """
    check = _BY_ID[check_id]
    ctx = DoctorContext(config=config, db=db)
    if repair and check.fix is not None:
        return await apply_fix(check, ctx)
    return await check.run(ctx)
