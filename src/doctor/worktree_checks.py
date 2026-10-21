"""``worktrees.*`` doctor checks (worktree-execution §5, trust-and-ops §5.5).

``worktrees.orphans`` is a *reserved* check id (see
:data:`src.doctor.models.RESERVED_CHECK_IDS`): doctor ships the placeholder,
and this module — owned by the worktree-execution workstream — claims it at
daemon startup when ``worktrees.enabled`` is on.  With worktrees off the
placeholder stays, which is the honest answer.

Shape mirrors ``src/doctor/pool_checks.py``: a private ``_check_*`` per check,
a factory returning the :class:`DoctorCheck` list, a ``CHECKS`` snapshot and a
``run_check`` wrapper for tests.
"""

from __future__ import annotations

from src.doctor.models import CheckResult, DoctorCheck, DoctorContext, Severity

OWNER = "worktree-execution"


def _no_db_result(check_id: str) -> CheckResult:
    return CheckResult(
        id=check_id,
        severity=Severity.INFO,
        detail="database not initialised — worktree state unknown",
    )


# ---------------------------------------------------------------------------
# worktrees.orphans
# ---------------------------------------------------------------------------


async def _find_orphan_slots(ctx: DoctorContext) -> list[dict]:
    """Slot worktrees whose sentinel names a task that is no longer in ``tasks``.

    A released slot deliberately stays on its last task's branch — the branch
    is the durable artifact (§3.4) — so a slot naming a task that has since
    been deleted keeps ``aq/<task_id>`` checked out with nothing left to
    finish it.  Git then refuses that branch to any other worktree, which is
    one way a live task ends up looping on the no-workspace backoff.

    The sentinel is the only pin worth checking.  ``workspaces.locked_by_task_id``
    carries a foreign key to ``tasks.id`` *and* ``delete_task`` releases every
    lock it holds, so a lock can never outlive its task; the sentinel is a
    file on disk with neither protection.
    """
    from src.orchestrator.worktree_manager import WorktreeSlotManager

    orphans: list[dict] = []
    for ws in await ctx.db.list_workspaces():
        if not ws.is_slot:
            continue
        try:
            sentinel = WorktreeSlotManager.read_sentinel(ws.workspace_path)
        except Exception:
            sentinel = None
        if sentinel is None or not sentinel.task_id:
            continue
        if await ctx.db.get_task(sentinel.task_id) is not None:
            continue
        orphans.append(
            {
                "workspace_id": ws.id,
                "project_id": ws.project_id,
                "path": ws.workspace_path,
                "slot_index": ws.slot_index,
                "task_id": sentinel.task_id,
                "branch": sentinel.branch,
                # Whoever holds the slot now, for the operator's benefit: an
                # unlocked slot can be reset on the spot, a locked one has to
                # wait for its current task.
                "locked_by_task_id": ws.locked_by_task_id,
            }
        )
    return orphans


async def _check_worktrees_orphans(ctx: DoctorContext) -> CheckResult:
    if ctx.db is None:
        return _no_db_result("worktrees.orphans")
    orphans = await _find_orphan_slots(ctx)
    if not orphans:
        return CheckResult(
            id="worktrees.orphans",
            severity=Severity.OK,
            detail="no slot worktrees pinned to a deleted task",
        )
    detail = (
        f"{len(orphans)} slot worktree(s) still on a deleted task's branch: "
        + ", ".join(f"{o['path']} -> {o['branch'] or o['task_id']}" for o in orphans[:5])
    )
    return CheckResult(
        id="worktrees.orphans",
        severity=Severity.WARN,
        detail=detail,
        data={"count": len(orphans), "slots": orphans},
    )


# ---------------------------------------------------------------------------
# worktrees.capacity
# ---------------------------------------------------------------------------


async def find_capacity_shortfalls(db) -> list[dict]:
    """Projects whose enabled worktree slots fall short of their agent cap.

    A disabled slot inside the cap is an agent the pool can never start:
    acquisition skips it and lazy growth counts it as existing, so the
    scheduler just reports "no workspace capacity".  One entry per project
    with at least one such slot, each slot carrying its recorded reason.
    Shared with ``stall.sweep`` (``src/doctor/stall_checks.py``).
    """
    from src.workspace_disable import describe_disabled_slot, disabled_slots_in_cap, slot_cap

    by_project: dict[str, list] = {}
    for ws in await db.list_workspaces():
        if ws.is_slot and ws.project_id:
            by_project.setdefault(ws.project_id, []).append(ws)
    shortfalls: list[dict] = []
    for project_id in sorted(by_project):
        project = await db.get_project(project_id)
        if project is None:
            continue
        disabled = disabled_slots_in_cap(project, by_project[project_id])
        if not disabled:
            continue
        cap = slot_cap(project)
        shortfalls.append(
            {
                "project_id": project_id,
                "cap": cap,
                "enabled_in_cap": cap - len(disabled),
                "disabled_slots": [describe_disabled_slot(ws) for ws in disabled],
            }
        )
    return shortfalls


def describe_shortfall(entry: dict) -> str:
    """One line: ``agent-queue: 2/8 slots enabled (slot-0 unrecorded, ...)``."""
    slots = ", ".join(
        f"slot-{s['slot_index']} {s['reason_code']}" for s in entry["disabled_slots"][:6]
    )
    more = len(entry["disabled_slots"]) - 6
    if more > 0:
        slots += f", +{more} more"
    return (
        f"{entry['project_id']}: at most {entry['enabled_in_cap']}/{entry['cap']} "
        f"slots usable ({slots})"
    )


_CAPACITY_REMEDIATION = (
    "The slot reconciler re-creates missing slots and re-enables clean ones on "
    "its 5-minute pass (claim_failures slots after a 15-minute cooldown). "
    "quarantined_dirty slots need their uncommitted changes salvaged or "
    "discarded; operator slots stay off until `aq project edit-workspace` re-enables them."
)


async def _check_worktrees_capacity(ctx: DoctorContext) -> CheckResult:
    if ctx.db is None:
        return _no_db_result("worktrees.capacity")
    shortfalls = await find_capacity_shortfalls(ctx.db)
    if not shortfalls:
        return CheckResult(
            id="worktrees.capacity",
            severity=Severity.OK,
            detail="no disabled worktree slots inside any project's agent cap",
        )
    return CheckResult(
        id="worktrees.capacity",
        severity=Severity.WARN,
        detail=(
            "enabled slots < cap — disabled slots hold the pool below max-agents: "
            + "; ".join(describe_shortfall(e) for e in shortfalls)
        ),
        data={"projects": shortfalls, "remediation": _CAPACITY_REMEDIATION},
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def worktree_checks() -> list[DoctorCheck]:
    """The worktree-execution checks, for registration at daemon startup."""
    return [
        DoctorCheck(id="worktrees.orphans", run=_check_worktrees_orphans, owner=OWNER),
        DoctorCheck(id="worktrees.capacity", run=_check_worktrees_capacity, owner=OWNER),
    ]


#: Snapshot of the catalog, for tests and one-off invocation.
CHECKS = worktree_checks()

_BY_ID = {c.id: c for c in CHECKS}


async def run_check(db, check_id: str, *, config=None) -> CheckResult:
    """Run one worktree check directly against *db* (no registry needed).

    No ``repair`` argument: none of these checks declare a ``fix``.  The
    reserved-id contract limits ``worktrees.orphans`` to ``git worktree
    prune``, which cannot clear either pin this check reports — releasing a
    lock or resetting a slot off a dead task's branch is an operator call.
    """
    return await _BY_ID[check_id].run(DoctorContext(config=config, db=db))
