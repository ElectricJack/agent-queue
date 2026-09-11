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
``soft_delete_agent`` cannot, because it only takes an identity *out* of the
reuse pool, and a hard delete would drop history the task ledger still
references.

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

Every repair writes one ``pool.agent_repaired`` event, so ``aq system
get-recent-events --event-type pool.agent_repaired`` answers "why is this
worker RETIRED?" long after the doctor run has scrolled away.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from src.claim_file import read_claim_file
from src.doctor.models import CheckResult, DoctorCheck, DoctorContext, Severity
from src.doctor.runner import apply_fix
from src.git.manager import GitManager
from src.models import AgentState, ProjectStatus, TaskStatus
from src.orchestrator.worktree_manager import BRANCH_PREFIX
from src.pool_claims import is_live_pool_claim_task_status

OWNER = "swarm-work-model"

# ---------------------------------------------------------------------------
# pools.stale_worktree_checkouts
# ---------------------------------------------------------------------------


async def _find_stale_worktree_checkouts(ctx: DoctorContext) -> list[dict]:
    """Return idle slots pinned to a non-live task branch without a claim.

    Git's worktree inventory wins over sentinels here: git is what rejects
    the next ``switch``. Only registered slot paths are considered, so a
    doctor repair never detaches a human's arbitrary worktree.
    """
    workspaces = await ctx.db.list_workspaces()
    bases = {ws.id: ws for ws in workspaces if not ws.is_slot}
    slots = [ws for ws in workspaces if ws.is_slot and ws.base_workspace_id in bases]
    git = GitManager()
    branches: dict[str, str] = {}
    for base_id in {ws.base_workspace_id for ws in slots}:
        base = bases[base_id]
        if not Path(base.workspace_path).is_dir():
            continue
        try:
            entries = await git.aworktree_list(base.workspace_path)
        except Exception:
            continue
        branches.update(
            {
                str(Path(e["path"]).resolve()): e["branch"]
                for e in entries
                if e.get("path") and e.get("branch")
            }
        )

    stale: list[dict] = []
    for slot in slots:
        path = str(Path(slot.workspace_path).resolve())
        branch = branches.get(path)
        if not branch or not branch.startswith(BRANCH_PREFIX) or read_claim_file(path) is not None:
            continue
        task_id = branch[len(BRANCH_PREFIX):]
        task = await ctx.db.get_task(task_id)
        if task is None or task.status == TaskStatus.IN_PROGRESS:
            continue
        stale.append(
            {
                "workspace_id": slot.id,
                "path": slot.workspace_path,
                "branch": branch,
                "task_id": task_id,
                "task_status": task.status.value,
            }
        )
    return stale


async def _check_stale_worktree_checkouts(ctx: DoctorContext) -> CheckResult:
    if ctx.db is None:
        return _no_db_result("pools.stale_worktree_checkouts")
    stale = await _find_stale_worktree_checkouts(ctx)
    if not stale:
        return CheckResult(
            id="pools.stale_worktree_checkouts",
            severity=Severity.OK,
            detail="no stale slot worktree checkouts",
        )
    return CheckResult(
        id="pools.stale_worktree_checkouts",
        severity=Severity.WARN,
        detail=f"{len(stale)} slot worktree(s) pin a non-live task branch without a claim",
        data={"count": len(stale), "slots": stale},
    )


async def _fix_stale_worktree_checkouts(ctx: DoctorContext) -> CheckResult:
    if ctx.db is None:
        return _no_db_result("pools.stale_worktree_checkouts")
    git = GitManager()
    for stale in await _find_stale_worktree_checkouts(ctx):
        await git._arun(["switch", "--detach"], cwd=stale["path"])
    return await _check_stale_worktree_checkouts(ctx)


async def _pool_profiles(db) -> list:
    """Every global ``lifecycle: pool`` profile, as profile objects.

    ``":" not in p.id`` mirrors ``PoolsMixin._pool_profiles``: a pool is
    identified by a bare agent-type id, and any scoped leftover is not one.
    """
    return [
        p
        for p in await db.list_profiles()
        if ":" not in p.id and getattr(p, "lifecycle", "task") == "pool"
    ]


async def _pool_profile_ids(db) -> set[str]:
    """Agent-type ids of every ``lifecycle: pool`` profile.

    Profiles are global, so the id is already the bare agent-type that
    ``agents.profile_id`` holds — no normalisation needed.
    """
    return {
        p.id
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
        if task is None or not is_live_pool_claim_task_status(task.status):
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

    ``aq system get-recent-events --event-type pool.agent_repaired`` is the
    answer to "why is this worker RETIRED?" long after the doctor run has
    scrolled away.
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
            orchestrator = getattr(ctx.handler, "orchestrator", None)
            preparations = getattr(orchestrator, "claim_preparations", {})
            preparation = preparations.get((s.id, s.task_id, s.last_claim_epoch))
            if preparation is not None and not preparation.done():
                continue
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
    released_count = 0
    for s in bad:
        result = await ctx.db.release_claim(
            s.id,
            task_status=TaskStatus.READY,
            context="doctor_prepare_timeout",
            now=now,
            expected_task_id=s.task_id,
            expected_claim_epoch=s.last_claim_epoch,
            preparation_expired_before=now - 2 * _prepare_timeout(ctx),
            result="prepare_failed",
            needs_attention="prepare_timeout",
            prepare_backoff=True,
        )
        released_count += int(result.released)
    return CheckResult(
        id="pools.preparing_stuck",
        severity=Severity.OK,
        detail=f"released {released_count} claim/prepare session(s) stuck past timeout",
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
# pools.task_lifecycle_shadow (report-only — no fix)
# ---------------------------------------------------------------------------


async def _check_task_lifecycle_shadow(ctx: DoctorContext) -> CheckResult:
    """Expose push profiles that duplicate a durable pool execution route.

    A task-lifecycle profile with the same harness and intelligence class as
    a pool starts an unpooled session.  That session consumes a worktree slot
    while pool supply remains zero, which is operationally misleading even
    though both profile definitions parse and launch correctly.
    """
    if ctx.db is None:
        return _no_db_result("pools.task_lifecycle_shadow")
    profiles = await ctx.db.list_profiles()
    pool_routes: dict[tuple[str, str], list[str]] = {}
    for profile in profiles:
        if getattr(profile, "lifecycle", "task") != "pool":
            continue
        harness = str(getattr(profile, "harness", "") or "").strip()
        default_class = str(getattr(profile, "default_class", "") or "").strip()
        if harness and default_class:
            pool_routes.setdefault((harness, default_class), []).append(profile.id)

    duplicates: list[dict] = []
    duplicate_ids: set[str] = set()
    for profile in profiles:
        if getattr(profile, "lifecycle", "task") == "pool":
            continue
        route = (
            str(getattr(profile, "harness", "") or "").strip(),
            str(getattr(profile, "default_class", "") or "").strip(),
        )
        matches = sorted(pool_routes.get(route, [])) if all(route) else []
        if matches:
            duplicate_ids.add(profile.id)
            duplicates.append(
                {
                    "profile_id": profile.id,
                    "harness": route[0],
                    "default_class": route[1],
                    "pool_profile_ids": matches,
                }
            )

    affected_tasks: list[dict] = []
    for status in (TaskStatus.READY, TaskStatus.IN_PROGRESS):
        for task in await ctx.db.list_tasks(status=status):
            profile_id = task.profile_id
            if profile_id and profile_id not in duplicate_ids:
                continue
            affected_tasks.append(
                {
                    "task_id": task.id,
                    "project_id": task.project_id,
                    "status": task.status.value,
                    "profile_id": profile_id,
                    "reason": "missing_profile" if not profile_id else "duplicate_pool_route",
                    "pool_profile_ids": (
                        next(
                            (row["pool_profile_ids"] for row in duplicates if row["profile_id"] == profile_id),
                            [],
                        )
                    ),
                }
            )

    if not duplicates and not affected_tasks:
        return CheckResult(
            id="pools.task_lifecycle_shadow",
            severity=Severity.OK,
            detail="no task-lifecycle profile shadows a pool route and active tasks are explicitly routed",
            data={"profiles": [], "tasks": [], "count": 0},
        )
    detail = (
        f"{len(duplicates)} task-lifecycle profile(s) duplicate a pool harness/class route; "
        f"{len(affected_tasks)} READY/IN_PROGRESS task(s) have a missing or duplicate profile route"
    )
    return CheckResult(
        id="pools.task_lifecycle_shadow",
        severity=Severity.WARN,
        detail=detail,
        data={
            "count": len(duplicates) + len(affected_tasks),
            "profiles": duplicates[:50],
            "tasks": affected_tasks[:100],
        },
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


# ---------------------------------------------------------------------------
# pools.stranded_feature_branches
# ---------------------------------------------------------------------------

#: Cap on ``gh``/``git`` probes per doctor run.  A repository with hundreds of
#: remote branches is already diagnosed by the first few dozen, and doctor is
#: meant to stay fast and to degrade gracefully offline.
_MAX_BRANCH_PROBES = 40


async def _project_checkouts(ctx: DoctorContext) -> list[tuple[str, str, str]]:
    """``(project_id, checkout_path, default_branch)`` per project, best-effort."""
    out: list[tuple[str, str, str]] = []
    git = GitManager()
    for project in await ctx.db.list_projects():
        checkout = await ctx.db.get_project_workspace_path(project.id)
        if not checkout or not Path(checkout).is_dir():
            continue
        default = project.repo_default_branch
        if not default:
            try:
                default = await git.aget_default_branch(checkout)
            except Exception:
                continue
        out.append((project.id, checkout, default))
    return out


async def _find_stranded_feature_branches(ctx: DoctorContext) -> dict:
    """Remote branches that other work was merged *into* and that never left.

    The failure mode, from Pkg 4: three PRs were merged into
    ``feature/playbook-v2-pkg4-core``, their tasks closed COMPLETED, and
    nobody ever opened ``pkg4-core -> main``.  Every surface said the work
    had shipped; ``main`` did not have it.  Such a branch is identifiable
    without any daemon state at all:

    (a) it is ahead of the default branch,
    (b) pull requests have been **merged into** it, and
    (c) no open pull request delivers it **to** the default branch.

    A second, quieter bucket catches the other half of the same outage —
    branches ahead of the default branch with no open PR to it and no merged
    PRs into them (``feature/playbook-v2-pkg4``, whose plan docs were never
    merged).  ``aq/*`` task branches are excluded from *that* bucket only:
    a slot's task branch is ahead of main by design and would otherwise
    drown the report.

    ``gh`` answering ``None`` (no auth, no network, not installed) is
    "unknown" and is counted, never reported as a finding — an offline
    doctor run must not accuse every branch in the repository.
    """
    git = GitManager()
    stranded: list[dict] = []
    stale: list[dict] = []
    unknown = 0
    probes = 0

    for project_id, checkout, default in await _project_checkouts(ctx):
        try:
            await git._arun(["fetch", "origin", "--prune"], cwd=checkout)
        except Exception:
            # Stale remote-tracking refs still answer the question, just
            # less freshly.  Never fatal: doctor runs offline too.
            pass
        branches = await git.alist_remote_branches(checkout)
        if branches is None:
            continue
        for branch in sorted(branches):
            if branch == default:
                continue
            if probes >= _MAX_BRANCH_PROBES:
                break
            ahead = await git.acount_commits_ahead(
                checkout, f"origin/{branch}", f"origin/{default}"
            )
            if not ahead:
                continue
            probes += 1
            open_to_default = await git.alist_prs(
                checkout, state="open", head=branch, base=default, limit=1
            )
            merged_into = await git.alist_prs(
                checkout, state="merged", base=branch, limit=5
            )
            if open_to_default is None or merged_into is None:
                unknown += 1
                continue
            if open_to_default:
                continue
            finding = {
                "project_id": project_id,
                "branch": branch,
                "ahead": ahead,
                "default_branch": default,
                "merged_prs": [pr.get("url") for pr in merged_into],
                "command": (
                    f"gh pr create --base {default} --head {branch} "
                    f'--title "Merge {branch} into {default}" --body "..."'
                ),
            }
            if merged_into:
                stranded.append(finding)
            elif not branch.startswith(BRANCH_PREFIX):
                stale.append(finding)
    return {"stranded": stranded, "stale": stale, "unknown": unknown}


def _stranded_result(found: dict) -> CheckResult:
    stranded, stale = found["stranded"], found["stale"]
    data = {
        "count": len(stranded),
        "branches": stranded[:50],
        "stale": stale[:50],
        "stale_count": len(stale),
        "unverifiable": found["unknown"],
        "commands": [f["command"] for f in stranded[:50]],
    }
    if stranded:
        names = ", ".join(f["branch"] for f in stranded[:5])
        return CheckResult(
            id="pools.stranded_feature_branches",
            severity=Severity.WARN,
            detail=(
                f"{len(stranded)} branch(es) had PRs merged into them and have no open "
                f"PR to the default branch: {names}. Their work is not on the default "
                "branch — re-run with --fix for the gh command that opens the PR."
            ),
            fixable=True,
            data=data,
        )
    if stale:
        names = ", ".join(f["branch"] for f in stale[:5])
        return CheckResult(
            id="pools.stranded_feature_branches",
            severity=Severity.INFO,
            detail=(
                f"no stranded merge bases; {len(stale)} branch(es) are ahead of the "
                f"default branch with no open PR to it: {names}"
            ),
            data=data,
        )
    return CheckResult(
        id="pools.stranded_feature_branches",
        severity=Severity.OK,
        detail="no feature branch is holding merged work back from the default branch",
        data=data,
    )


async def _check_stranded_feature_branches(ctx: DoctorContext) -> CheckResult:
    if ctx.db is None:
        return _no_db_result("pools.stranded_feature_branches")
    return _stranded_result(await _find_stranded_feature_branches(ctx))


async def _fix_stranded_feature_branches(ctx: DoctorContext) -> CheckResult:
    """Print the ``gh pr create`` command; never open the PR itself.

    ``aq doctor --fix`` with no ``--check`` runs *every* registered fix, and
    the design's fix contract is "idempotent and non-destructive".  Opening a
    pull request is neither idempotent nor undoable from here, and it is
    outward-facing — it notifies reviewers and can trigger CI on somebody
    else's repository.  So this fix produces the exact command and leaves the
    decision with the operator, which the deliverable explicitly allows
    ("opens (or prints the command to open) a base->main PR").
    """
    if ctx.db is None:
        return _no_db_result("pools.stranded_feature_branches")
    found = await _find_stranded_feature_branches(ctx)
    result = _stranded_result(found)
    if found["stranded"]:
        result.detail = (
            f"{len(found['stranded'])} stranded branch(es); run these to open the "
            "missing PRs:\n" + "\n".join(f["command"] for f in found["stranded"][:50])
        )
    return result


# ---------------------------------------------------------------------------
# pools.global_bounds_migration (report-only — no fix, by design)
# ---------------------------------------------------------------------------

#: How many ``pool.bounds_rescoped`` rows to scan for the *first* one per
#: profile.  One row per pool profile per daemon start: a few hundred covers
#: months of restarts, and the check only ever needs the oldest of them.
_MAX_RESCOPE_EVENTS = 500


async def _first_rescope_records(ctx: DoctorContext) -> dict[str, dict]:
    """The **oldest** ``pool.bounds_rescoped`` payload per profile.

    ``after_id=0`` asks :meth:`get_recent_events` for ascending, gapless
    replay from the start of the log, so the first payload seen for a profile
    is the one written at the upgrade — not the one written at the most
    recent restart.  That distinction is the whole self-resolution mechanism
    (see :func:`_check_global_bounds_migration`): the newest record would be
    re-stamped with whatever ``max_active`` is current every time the daemon
    came back, and the check would nag forever.

    A payload that is not JSON, or is JSON of the wrong shape, is skipped
    rather than raising: doctor reporting nothing is better than doctor
    failing on an audit row somebody wrote by hand.
    """
    rows = await ctx.db.get_recent_events(
        limit=_MAX_RESCOPE_EVENTS, event_type="pool.bounds_rescoped", after_id=0
    )
    first: dict[str, dict] = {}
    for row in rows:
        try:
            payload = json.loads(row.get("payload") or "")
        except (TypeError, ValueError):
            continue
        profile_id = payload.get("profile_id") if isinstance(payload, dict) else None
        if not isinstance(profile_id, str) or profile_id in first:
            continue
        payload["timestamp"] = row.get("timestamp")
        first[profile_id] = payload
    return first


async def _check_global_bounds_migration(ctx: DoctorContext) -> CheckResult:
    """Report-only: what a pool's ceiling was before bounds became fleet-wide.

    Global worker pools re-scoped ``min_active``/``max_active`` from "these
    bounds, once per active project" to "these bounds, for the fleet".  With
    five active projects a profile whose ``max_active`` is 4 could really run
    twenty workers, and now runs four.  That reduction is intended and is
    deliberately **not** auto-multiplied away on upgrade — multiplying would
    preserve the very bug the re-scoping removes — but left unannounced it
    reads as a throughput regression, so this check states the arithmetic and
    names the ``max_active`` that would buy the old capacity back.

    **No ``fix``.**  Choosing a fleet size is an operator decision and the
    design says so in as many words.  A ``--fix`` here would quietly restore
    a twenty-worker fleet on a box that may not have twenty workers' worth of
    RAM, on the strength of a number that was never deliberate in the first
    place.

    **How it self-resolves.**  The signal for "an operator has acted" is
    durable and needs no new state: ``_announce_bounds_rescoped`` writes one
    ``pool.bounds_rescoped`` audit row per profile per daemon lifetime
    carrying the ``max_active`` in force at that moment, and this check reads
    the *oldest* such row — the one from the upgrade itself.  If the profile's
    ``max_active`` today differs from the one recorded there, the operator has
    re-scaled the pool since (``aq pool scale`` writes the profile) and has
    therefore made the call, whichever way; the notice is resolved for good.
    Re-affirming the *same* ceiling is not treated as acting on it: the fleet
    is still smaller than it was, which is precisely what the notice says.
    Deliberately keeping the smaller fleet is one ``aq pool scale --profile-id
    <id> --max <n>`` away from silence, and expressing that intent once is a
    fair price for not silently swallowing a 5x capacity change.

    A profile with no such row has never been observed by a daemon running
    this code (``swarm.enabled`` off, or no reconcile tick yet) and is not
    reported — there is nothing to say about a fleet that has not started.
    """
    if ctx.db is None:
        return _no_db_result("pools.global_bounds_migration")
    profiles = {p.id: p for p in await _pool_profiles(ctx.db)}
    if not profiles:
        return CheckResult(
            id="pools.global_bounds_migration",
            severity=Severity.OK,
            detail="no pool profiles configured",
        )
    records = await _first_rescope_records(ctx)
    outstanding: list[dict] = []
    for profile_id, profile in sorted(profiles.items()):
        record = records.get(profile_id)
        if record is None:
            continue
        recorded_max = record.get("effective_max_active")
        previous_max = record.get("previous_effective_max_active")
        if not isinstance(recorded_max, int) or not isinstance(previous_max, int):
            # An unbounded pool (``max_active: null``) had no ceiling to lose.
            continue
        if previous_max <= recorded_max:
            # One eligible project, or none: per-project and fleet-wide
            # bounds meant the same thing here and nothing shrank.
            continue
        if profile.max_active != recorded_max:
            continue  # re-scaled since the upgrade — the operator has decided
        outstanding.append(
            {
                "profile_id": profile_id,
                "eligible_projects": record.get("eligible_projects"),
                "previous_effective_max_active": previous_max,
                "effective_max_active": recorded_max,
                "suggested_max_active": previous_max,
                "command": f"aq pool scale --profile-id {profile_id} --max {previous_max}",
                "rescoped_at": record.get("timestamp"),
            }
        )
    if not outstanding:
        return CheckResult(
            id="pools.global_bounds_migration",
            severity=Severity.OK,
            detail="pool bounds are fleet-wide and no ceiling change is unacknowledged",
            data={"count": 0},
        )
    lead = outstanding[0]
    return CheckResult(
        id="pools.global_bounds_migration",
        severity=Severity.INFO,
        detail=(
            f"{len(outstanding)} pool profile(s) lost effective ceiling when bounds became "
            f"fleet-wide, e.g. {lead['profile_id']}: "
            f"{lead['previous_effective_max_active']} -> {lead['effective_max_active']} worker(s) "
            f"across {lead['eligible_projects']} active project(s). Run "
            f"`{lead['command']}` to keep the old capacity, or scale to whatever size you "
            "actually want — either resolves this notice."
        ),
        data={"count": len(outstanding), "profiles": outstanding[:50]},
    )


# ---------------------------------------------------------------------------
# pools.floor_exceeds_max (report-only — no fix)
# ---------------------------------------------------------------------------


async def _check_floor_exceeds_max(ctx: DoctorContext) -> CheckResult:
    """Report-only: a pool floor that its own ceiling cannot fund.

    The effective floor is ``max(min_active, sum of min_per_project over
    eligible projects)`` — a per-project warm floor the global ``min_active``
    cannot pay for raises the floor rather than being silently ignored.  When
    that number exceeds ``max_active`` the configuration contradicts itself:
    the operator has asked for a resident worker in six projects out of a pool
    that may hold four.

    Nothing breaks.  Sizing clamps ``desired`` to ``max_active`` exactly as it
    always did, so this is a warning and not an error — but two of those six
    projects will never get their warm worker, and nothing anywhere else says
    so.  That silence is what the check exists to end.

    Eligibility here is *configured* eligibility: ACTIVE projects, the same
    set ``_measure_pools`` iterates.  At runtime the reservation sum also
    drops projects that are quarantined or out of workspace capacity, so the
    live floor can be lower than the one reported here — which makes this the
    upper bound, and the contradiction it names the one written in the
    configuration rather than one that depends on the weather.

    No ``fix``: raising ``max_active`` and lowering ``min_per_project`` are
    both defensible repairs and they mean opposite things about how the
    operator wants the box used.
    """
    if ctx.db is None:
        return _no_db_result("pools.floor_exceeds_max")
    profiles = await _pool_profiles(ctx.db)
    if not profiles:
        return CheckResult(
            id="pools.floor_exceeds_max", severity=Severity.OK, detail="no pool profiles configured"
        )
    project_ids = sorted(
        p.id for p in await ctx.db.list_projects() if p.status == ProjectStatus.ACTIVE
    )
    bad: list[dict] = []
    for profile in sorted(profiles, key=lambda p: p.id):
        if profile.max_active is None:
            continue  # unbounded: any floor is fundable
        per_project = getattr(profile, "min_per_project", 0) or 0
        contributors = project_ids if per_project else []
        reserved = per_project * len(contributors)
        floor = max(profile.min_active or 0, reserved)
        if floor <= profile.max_active:
            continue
        bad.append(
            {
                "profile_id": profile.id,
                "effective_min_active": floor,
                "max_active": profile.max_active,
                "min_active": profile.min_active or 0,
                "min_per_project": per_project,
                "projects": contributors[:50],
            }
        )
    if not bad:
        return CheckResult(
            id="pools.floor_exceeds_max",
            severity=Severity.OK,
            detail="every pool floor fits inside its ceiling",
            data={"count": 0},
        )
    parts = []
    for entry in bad[:5]:
        contributors = ", ".join(entry["projects"]) or "none"
        parts.append(
            f"{entry['profile_id']}: floor {entry['effective_min_active']} > max_active "
            f"{entry['max_active']} (min_active {entry['min_active']}, min_per_project "
            f"{entry['min_per_project']} x {len(entry['projects'])} active project(s): "
            f"{contributors})"
        )
    return CheckResult(
        id="pools.floor_exceeds_max",
        severity=Severity.WARN,
        detail=(
            f"{len(bad)} pool profile(s) have a floor their ceiling cannot fund — sizing "
            "clamps to max_active and the surplus warm workers never appear: " + "; ".join(parts)
        ),
        data={"count": len(bad), "profiles": bad[:50]},
    )


# ---------------------------------------------------------------------------
# pools.placement_starved (report-only — no fix)
# ---------------------------------------------------------------------------

#: How long a starvation must hold before it is a finding rather than a tick
#: of weather.  A launch, a workspace release or a 60s quarantine window all
#: clear well inside five minutes; a condition that outlives them is an
#: operator problem.
_STARVED_AFTER = 300.0


def _starvation_state(ctx: DoctorContext):
    """``(state, observing_since)`` from the running orchestrator, or ``(None, None)``.

    Starvation is a scheduling condition, not a fact stored anywhere: sizing
    authorises a start and placement finds no project that can take it, both
    inside one 5-second tick.  Only the orchestrator sees it, so doctor —
    which is a point-in-time read — borrows its observation the same way
    ``sessions.*`` and ``intelligence_classes.*`` borrow theirs, through
    ``ctx.handler.orchestrator``.
    """
    orchestrator = getattr(ctx.handler, "orchestrator", None)
    state = getattr(orchestrator, "_pool_starvation_state", None)
    if state is None:
        return None, None
    return state, getattr(orchestrator, "_pool_starvation_observing_since", None)


async def _check_placement_starved(ctx: DoctorContext) -> CheckResult:
    """Report-only: a pool that wants to grow and has nowhere to put a worker.

    Under fleet-wide sizing this is the failure that replaces "the pool for
    project X is stuck": the sizer authorises N starts for a profile, and
    every candidate project is quarantined, out of workspace capacity, or at
    its own ``max_concurrent_agents``.  Nothing errors — the starts are simply
    dropped, tick after tick — so without this check the only symptom is a
    queue that does not drain.

    ``reasons`` names the blocking predicate per project, straight from
    ``PlacementStarvation``: ``quarantined`` (a failed launch's backoff
    window), ``no_workspace`` (no free slot in that checkout), ``at_cap``
    (the project is already running its allowance of pool sessions).  Each has
    a different repair, which is why the check reports them rather than a
    count.

    **Where "since when" comes from.**  ``_report_pool_starvation`` records a
    first-seen timestamp per profile, in memory, alongside ``_pool_quarantine``
    — a live condition observed by whoever is currently scheduling, not
    durable state.  Two consequences the check is explicit about rather than
    papering over: with no reachable orchestrator (doctor run outside the
    daemon, or before the first reconcile tick) it reports INFO and says it
    cannot see, never OK; and when this daemon has itself been watching for
    less than the threshold it says so, because a starvation that began before
    the restart is indistinguishable from one that began with it.

    No ``fix``: every repair is outside doctor's reach — free a workspace,
    raise a project cap, fix the harness whose failures are quarantining the
    project, or accept the smaller fleet.
    """
    if ctx.db is None:
        return _no_db_result("pools.placement_starved")
    state, observing_since = _starvation_state(ctx)
    if state is None:
        return CheckResult(
            id="pools.placement_starved",
            severity=Severity.INFO,
            detail=(
                "placement starvation is observed by the running orchestrator and is not "
                "visible from here (no daemon reachable, or no pool reconcile tick has run)"
            ),
        )
    now = time.time()
    watched = now - observing_since if observing_since else 0.0
    starved, young = [], []
    for profile_id, entry in sorted(state.items()):
        held = now - entry.get("since", now)
        row = {
            "profile_id": profile_id,
            "wanted": entry.get("wanted", 0),
            "held_seconds": round(held, 1),
            "reasons": dict(sorted((entry.get("reasons") or {}).items())),
        }
        (starved if held >= _STARVED_AFTER else young).append(row)
    if not starved:
        detail = "no pool has been placement-starved for 5 minutes"
        if young:
            detail += f" ({len(young)} starved for less than that)"
        return CheckResult(
            id="pools.placement_starved",
            severity=Severity.OK,
            detail=detail,
            data={"count": 0, "recent": young[:50]},
        )
    parts = []
    for row in starved[:5]:
        why = ", ".join(f"{pid}: {reason}" for pid, reason in row["reasons"].items())
        parts.append(
            f"{row['profile_id']}: {row['wanted']} start(s) unplaced for "
            f"{row['held_seconds']:.0f}s ({why or 'no active project runs this profile'})"
        )
    detail = (
        f"{len(starved)} pool profile(s) have had authorised starts and no eligible project "
        "for over 5 minutes: " + "; ".join(parts)
    )
    if watched and watched < _STARVED_AFTER * 2:
        detail += (
            f" — this daemon has only been observing placement for {watched:.0f}s, so the "
            "condition may be older than the durations shown"
        )
    return CheckResult(
        id="pools.placement_starved",
        severity=Severity.WARN,
        detail=detail,
        data={
            "count": len(starved),
            "profiles": starved[:50],
            "recent": young[:50],
            "observing_seconds": round(watched, 1),
        },
    )


def pool_checks() -> list[DoctorCheck]:
    return [
        DoctorCheck(
            id="pools.stale_worktree_checkouts",
            run=_check_stale_worktree_checkouts,
            fix=_fix_stale_worktree_checkouts,
            owner=OWNER,
        ),
        DoctorCheck(id="pools.stuck", run=_check_pools_stuck, fix=_fix_pools_stuck, owner=OWNER),
        # ``timeout_s`` is well above the 5s default: this one fetches and
        # shells out to ``gh`` once or twice per candidate branch.  Its
        # ``fix`` prints the ``gh pr create`` command rather than running it
        # — see ``_fix_stranded_feature_branches``.
        DoctorCheck(
            id="pools.stranded_feature_branches",
            run=_check_stranded_feature_branches,
            fix=_fix_stranded_feature_branches,
            owner=OWNER,
            timeout_s=60.0,
        ),
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
        # Report-only: either changing a profile lifecycle or rerouting active
        # work is an operator decision; doctor must not disrupt live sessions.
        DoctorCheck(
            id="pools.task_lifecycle_shadow",
            run=_check_task_lifecycle_shadow,
            owner=OWNER,
        ),
        # Report-only: no ``fix`` — a claim/holder mismatch needs a human to
        # decide which side (agent, session, or task) is authoritative.
        DoctorCheck(id="claims.holder_consistency", run=_check_holder_consistency, owner=OWNER),
        # Report-only by design (global-worker-pools §4): picking a fleet size
        # is an operator decision, so this one states the arithmetic and stops.
        DoctorCheck(
            id="pools.global_bounds_migration",
            run=_check_global_bounds_migration,
            owner=OWNER,
        ),
        # Report-only: raising ``max_active`` and lowering ``min_per_project``
        # are both valid repairs and mean opposite things.
        DoctorCheck(id="pools.floor_exceeds_max", run=_check_floor_exceeds_max, owner=OWNER),
        # Report-only: every repair (free a workspace, raise a project cap,
        # fix the harness behind a quarantine) is outside doctor's reach.
        DoctorCheck(id="pools.placement_starved", run=_check_placement_starved, owner=OWNER),
    ]


#: Snapshot for convenience call-sites (tests, ad-hoc scripts) that want the
#: list without building a full :class:`~src.doctor.runner.DoctorRegistry`.
CHECKS = pool_checks()

_BY_ID = {c.id: c for c in CHECKS}


async def run_check(
    db, check_id: str, *, config=None, repair: bool = False, handler=None
) -> CheckResult:
    """Run one pool/claim check directly against *db* (no registry needed).

    ``repair=True`` runs the check's ``fix`` (if any) then re-runs the check,
    mirroring :func:`src.doctor.runner.apply_fix`.  *handler* is threaded
    through for the checks that borrow live orchestrator state
    (``pools.placement_starved``); everything else ignores it.
    """
    check = _BY_ID[check_id]
    ctx = DoctorContext(config=config, db=db, handler=handler)
    if repair and check.fix is not None:
        return await apply_fix(check, ctx)
    return await check.run(ctx)
