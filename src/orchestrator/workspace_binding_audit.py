"""Reconcile historical slot bindings without erasing their ownership evidence."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import or_, select

from src.claim_file import read_claim_file
from src.database.tables import integration_branch_owners, job_workspace_pins, sessions


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
        retained = await conn.scalar(select(integration_branch_owners.c.owner_id).where(
            integration_branch_owners.c.workspace_id == ws.id,
            integration_branch_owners.c.handoff_state.in_(("attached", "handoff_pending")),
        ).limit(1))
        pinned = await conn.scalar(select(job_workspace_pins.c.workspace_id).where(
            job_workspace_pins.c.workspace_id == ws.id,
        ).limit(1))
    if retained is not None or pinned is not None:
        return "integration owner or job pin retained; use its exact recovery fence"
    task = await db.get_task(ws.locked_by_task_id) if ws.locked_by_task_id else None
    if (task is None or task.project_id != ws.project_id
            or task.assigned_agent_id != ws.locked_by_agent_id):
        return "registered task/agent binding disagrees; source and lock preserved"
    claim_path = Path(ws.workspace_path, ".aq", "claim.json")
    claim = read_claim_file(ws.workspace_path)
    if claim_path.exists() and claim is None:
        return "claim file is unreadable or malformed; source and lock preserved"
    active = [row for row in holders if row["state"] in ("starting", "running", "draining")]
    exact = [row for row in active if row["project_id"] == ws.project_id
             and row["work_dir"] == ws.workspace_path
             and row["agent_id"] == ws.locked_by_agent_id
             and row["task_id"] == task.id and row["last_claim_epoch"] == task.claim_epoch]
    if claim is not None and not any(
        claim.get("task_id") == task.id and claim.get("claim_epoch") == task.claim_epoch
        and claim.get("session_id") == row["id"] for row in exact
    ):
        return "foreign or unproved claim retained; no automatic claim retirement"
    if len(exact) == 1 and len(active) == 1 and exact[0]["claim_phase"] in ("claiming", "preparing"):
        return "preparation in progress; predecessor checkout may still be valid"
    try:
        sentinel = mgr.read_sentinel(ws.workspace_path)
        if (sentinel is None or sentinel.workspace_id != ws.id
                or sentinel.project_id != ws.project_id
                or sentinel.base_workspace_id != ws.base_workspace_id
                or sentinel.task_id != task.id):
            return "source sentinel disagrees with the active binding; source preserved"
        base = await mgr._base_of(ws)
        if (base is None or base.project_id != ws.project_id
                or await mgr.git.aworktree_base_path(ws.workspace_path)
                != await mgr.git.aworktree_base_path(base.workspace_path)):
            return "checkout repository disagrees with the registered base; source preserved"
        branch = await mgr.git.aget_current_branch(ws.workspace_path)
        if branch != task.branch_name or sentinel.branch != task.branch_name:
            return "checkout branch disagrees with the active binding; source preserved"
        # Process inventory failure cannot be interpreted as an empty inventory.
        await mgr._slot_is_live(ws)
    except Exception as exc:
        return f"source or process inventory unverifiable ({type(exc).__name__}); binding preserved"
    if len(exact) != 1 or len(active) != 1:
        return "no unique current session proves this binding; absence is not quiescence"
    if claim is None:
        return "active binding has no claim-file proof; source and lock preserved"
    receipt = await db.get_task_meta(task.id, "claim_workspace")
    expected = {"workspace_id": ws.id, "generation": ws.generation,
                "session_id": exact[0]["id"], "instance_token": exact[0]["instance_token"],
                "claim_epoch": task.claim_epoch}
    if receipt != expected:
        return "acquisition receipt missing or stale; historical binding preserved"
    return None
