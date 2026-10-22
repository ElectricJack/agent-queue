"""Reconcile historical slot bindings without erasing their ownership evidence."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import or_, select

from src.claim_file import read_claim_file
from src.database.tables import integration_branch_owners, job_workspace_pins, jobs, sessions
from src.jobs.policy import TERMINAL

_LIVE_STATES = ("starting", "running", "draining")


async def reconcile_slot_bindings(db, mgr, project_id: str) -> list[dict]:
    """Read existing locked rows; mismatches require owner-fenced recovery.

    Inventory is a hint. Re-read it after filesystem probes and discard changed
    identities. No observation here authorizes a mutation of the row or source.
    """
    findings = []
    for snapshot in await db.list_workspaces(project_id=project_id):
        if not snapshot.is_slot or not (snapshot.locked_by_task_id or snapshot.locked_by_agent_id):
            continue
        ws = await db.get_workspace(snapshot.id)
        if ws is None or _binding(ws) != _binding(snapshot):
            continue
        try:
            detail = await _inspect_binding(db, mgr, ws)
        except Exception as exc:
            detail = f"binding evidence unavailable ({type(exc).__name__}); source preserved"
        fresh = await db.get_workspace(ws.id)
        if fresh is None or _binding(fresh) != _binding(ws):
            continue
        if detail is not None:
            findings.append({
                "kind": "slot_binding_preparing" if detail.startswith("preparation in progress")
                else "slot_binding_retained", "workspace_id": ws.id,
                "generation": ws.generation, "locked_by_task_id": ws.locked_by_task_id,
                "locked_by_agent_id": ws.locked_by_agent_id, "detail": detail,
            })
    return findings


def _binding(ws):
    return (ws.project_id, ws.workspace_path, ws.base_workspace_id, ws.slot_index,
            ws.generation, ws.locked_by_task_id, ws.locked_by_agent_id, ws.enabled)


async def _inspect_binding(db, mgr, ws) -> str | None:
    if not hasattr(db, "_engine"):
        return "registered ownership cannot be verified; binding preserved"
    async with db._engine.connect() as conn:
        holders = (await conn.execute(select(sessions).where(or_(
            sessions.c.work_dir == ws.workspace_path,
            sessions.c.agent_id == ws.locked_by_agent_id if ws.locked_by_agent_id
            else sessions.c.id.is_(None),
        )))).mappings().all()
        owners = (await conn.execute(select(integration_branch_owners).where(
            integration_branch_owners.c.workspace_id == ws.id,
            integration_branch_owners.c.handoff_state.in_(("attached", "handoff_pending")),
        ))).mappings().all()
        pins = (await conn.execute(select(
            job_workspace_pins.c.generation.label("pin_generation"), jobs,
        ).select_from(job_workspace_pins.outerjoin(
            jobs, jobs.c.id == job_workspace_pins.c.job_id,
        )).where(job_workspace_pins.c.workspace_id == ws.id))).mappings().all()
    task = await db.get_task(ws.locked_by_task_id) if ws.locked_by_task_id else None
    detail, session = await _prove_binding(db, mgr, ws, task, holders)
    if owners or pins:
        # A pin or attached owner protects the checkout. It is healthy only
        # when it names exactly the binding proved above; anything else keeps
        # its own fence and needs owner-fenced recovery.
        mismatch = _retention_mismatch(ws, task, session, owners, pins)
        if mismatch is not None:
            return f"integration owner or job pin retained; use its exact recovery fence ({mismatch})"
    return detail


def _retention_mismatch(ws, task, session, owners, pins) -> str | None:
    if session is None:
        return "no unique current session proves the retained holder"
    if task is None:
        return "a taskless pool reservation cannot hold the retained owner or pin"
    branch = (task.branch_name or "").removeprefix("refs/heads/")
    for owner in owners:
        if owner["fence"] is not None or owner["handoff_state"] != "attached":
            return f"owner {owner['id']} is {owner['handoff_state']}, not a live attachment"
        if owner["owner_id"] != task.id or owner["session_id"] != session["id"]:
            return f"owner {owner['id']} belongs to another task or session"
        if (not branch or owner["ref"].removeprefix("refs/heads/") != branch
                or (task.repo_id and owner["repository_id"] != task.repo_id)):
            return f"owner {owner['id']} names another repository or branch"
    for pin in pins:
        if pin["id"] is None:
            return "job pin has no job record"
        if (pin["pin_generation"] != ws.generation or pin["workspace_generation"] != ws.generation
                or pin["workspace_id"] != ws.id or pin["project_id"] != ws.project_id):
            return f"job {pin['id']} pins another workspace generation"
        if (pin["owner_kind"] != "task" or pin["task_id"] != task.id
                or pin["claim_epoch"] != task.claim_epoch
                or pin["submitter_session_id"] != session["id"]):
            return f"job {pin['id']} belongs to another task, claim or session"
        if pin["state"] in TERMINAL or pin["cleanup_blocked"]:
            return f"job {pin['id']} is {pin['state']} but its pin remains"
    return None


async def _prove_binding(db, mgr, ws, task, holders):
    """Return the binding finding and the unique current session it proved."""
    if ws.locked_by_task_id is None and ws.locked_by_agent_id:
        return await _prove_pool_reservation(db, mgr, ws, holders)
    if (task is None or task.project_id != ws.project_id
            or task.assigned_agent_id != ws.locked_by_agent_id):
        return "registered task/agent binding disagrees; source and lock preserved", None
    active = [row for row in holders if row["state"] in _LIVE_STATES]
    exact = [row for row in active if row["project_id"] == ws.project_id
             and row["work_dir"] == ws.workspace_path
             and row["agent_id"] == ws.locked_by_agent_id
             and row["task_id"] == task.id and row["last_claim_epoch"] == task.claim_epoch]
    session = exact[0] if len(exact) == 1 and len(active) == 1 else None
    claim_path = Path(ws.workspace_path, ".aq", "claim.json")
    claim = read_claim_file(ws.workspace_path)
    if claim_path.exists() and claim is None:
        return "claim file is unreadable or malformed; source and lock preserved", None
    if claim is not None and not any(
        claim.get("task_id") == task.id and claim.get("claim_epoch") == task.claim_epoch
        and claim.get("session_id") == row["id"] for row in exact
    ):
        return "foreign or unproved claim retained; no automatic claim retirement", None
    if session is not None and session["claim_phase"] in ("claiming", "preparing"):
        return "preparation in progress; predecessor checkout may still be valid", session
    mismatch = await _source_disagreement(mgr, ws, task.id, task.branch_name, task.branch_name)
    if mismatch is not None:
        return mismatch, session
    if session is None:
        return "no unique current session proves this binding; absence is not quiescence", None
    if claim is None:
        return "active binding has no claim-file proof; source and lock preserved", None
    receipt = await db.get_task_meta(task.id, "claim_workspace")
    expected = {"workspace_id": ws.id, "generation": ws.generation,
                "session_id": session["id"], "instance_token": session["instance_token"],
                "claim_epoch": task.claim_epoch}
    if receipt != expected:
        return "acquisition receipt missing or stale; historical binding preserved", None
    return None, session


async def _prove_pool_reservation(db, mgr, ws, holders):
    """Prove an agent-only lock is one live pool session's taskless reservation.

    A pool session keeps its slot between claims (startup, the first claim,
    draining after a close) with no task lock. That is healthy only while one
    live pool session of exactly this agent and checkout holds no task,
    preparation or failure, nothing names a task, and the source is free.
    """
    active = [row for row in holders if row["state"] in _LIVE_STATES]
    exact = [row for row in active if row["lifecycle"] == "pool"
             and row["project_id"] == ws.project_id
             and row["work_dir"] == ws.workspace_path
             and row["agent_id"] == ws.locked_by_agent_id
             and row["task_id"] is None and row["claim_phase"] in (None, "claiming")]
    if len(exact) != 1 or len(active) != 1:
        return ("no unique taskless pool session proves this agent reservation; "
                "source and lock preserved"), None
    session = exact[0]
    if session["failed_preparation"] is not None:
        return "pool session retains a failed preparation; source and lock preserved", None
    if Path(ws.workspace_path, ".aq", "claim.json").exists():
        return "claim file retained without a task binding; no automatic claim retirement", None
    agent = await db.get_agent(ws.locked_by_agent_id)
    if agent is None or agent.current_task_id is not None:
        return "registered task/agent binding disagrees; source and lock preserved", None
    # A free slot is detached with a sentinel naming no task or branch.
    mismatch = await _source_disagreement(mgr, ws, None, None, "HEAD")
    if mismatch is not None:
        return mismatch, session
    return None, session


async def _source_disagreement(mgr, ws, task_id, branch, checkout) -> str | None:
    """Compare sentinel, repository and checkout with the proved holder.

    ``branch`` is the sentinel's expected branch and ``checkout`` the
    expected ``rev-parse --abbrev-ref HEAD``. Unverifiable evidence is a
    finding, never an empty inventory.
    """
    try:
        sentinel = mgr.read_sentinel(ws.workspace_path)
        if (sentinel is None or sentinel.workspace_id != ws.id
                or sentinel.project_id != ws.project_id
                or sentinel.base_workspace_id != ws.base_workspace_id
                or sentinel.task_id != task_id):
            return "source sentinel disagrees with the active binding; source preserved"
        base = await mgr._base_of(ws)
        if (base is None or base.project_id != ws.project_id
                or await mgr.git.aworktree_base_path(ws.workspace_path)
                != await mgr.git.aworktree_base_path(base.workspace_path)):
            return "checkout repository disagrees with the registered base; source preserved"
        current = await mgr.git.aget_current_branch(ws.workspace_path)
        if current != checkout or sentinel.branch != branch:
            return "checkout branch disagrees with the active binding; source preserved"
        # Process inventory failure cannot be interpreted as an empty inventory.
        await mgr._slot_is_live(ws)
    except Exception as exc:
        reason = f"source or process inventory unverifiable ({type(exc).__name__})"
        return f"{reason}; binding preserved"
    return None
