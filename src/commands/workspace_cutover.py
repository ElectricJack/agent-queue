"""Fence an admission-only base switch; never relocate or delete source."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

from sqlalchemy import or_, select, update

from src.commands.operator_authority import local_or_operator
from src.git.manager import GitError, repository_urls_match
from src.database.tables import integration_branch_owners, sessions, workspaces
from src.workspace_disable import ENABLE_FIELDS, OPERATOR, disable_fields


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


async def _source(git, path: str) -> dict:
    """Observation only: even unknown/dirty old sources are retained in place."""
    try:
        claim = Path(path, ".aq", "claim.json")
        claim_digest = await asyncio.to_thread(
            lambda: hashlib.sha256(claim.read_bytes()).hexdigest() if claim.exists() else None
        )
        return {
            "head": await git.arev_parse(path, "HEAD"),
            "root": await git._arun(["rev-parse", "--show-toplevel"], cwd=path),
            "branch": await git.aget_current_branch(path, strict=True),
            "common_directory": await git.aworktree_base_path(path),
            "origin": await git.aget_remote_url(path),
            "dirty_paths": await git.aget_dirty_paths(path),
            "claim_sha256": claim_digest,
        }
    except (OSError, RuntimeError, GitError) as exc:
        return {"unknown": str(exc)}


async def cutover(handler, args: dict) -> dict:
    if not local_or_operator("workspace_cutover"):
        return {"success": False, "error": "workspace_cutover requires a local operator"}
    project_id, source_id, target_id = (args.get(k) for k in (
        "project_id", "source_id", "target_id"
    ))
    if not all((project_id, source_id, target_id)) or source_id == target_id:
        return {"success": False, "error": "distinct source_id and target_id in a project required"}
    apply = bool(args.get("apply"))
    if apply and not args.get("expected_proof"):
        return {"success": False, "error": "apply requires expected_proof from preview"}

    # Lock the whole registered family during observation/apply. Acquisition's
    # conditional UPDATE rechecks enabled; no claim or pin is cleared here.
    async with handler.db.immediate() as conn:
        rows = [dict(r) for r in (await conn.execute(select(workspaces).where(
            workspaces.c.project_id == project_id,
        ).order_by(workspaces.c.id).with_for_update())).mappings()]
        by_id = {r["id"]: r for r in rows}
        source, target = by_id.get(source_id), by_id.get(target_id)
        if source is None or target is None:
            return {"success": False, "error": "both bases must belong to this project"}
        if (source["slot_index"] is not None or target["slot_index"] is not None
                or (source["kind_id"] or "project-repo") != (target["kind_id"] or "project-repo")
                or not source["enabled"] or target["enabled"]):
            return {"success": False, "error": "need an enabled source and disabled target base of the same kind"}
        kind_id = source["kind_id"] or "project-repo"
        kind = await handler.db.resolve_workspace_kind(project_id, kind_id)
        if (kind is None or not kind.is_git_repo or not kind.writable or not kind.lockable
                or kind.mode not in ("worktree", "exclusive-clone")):
            return {"success": False, "error": "base kind is not a compatible repository"}
        if any(r["id"] != source_id and r["slot_index"] is None and r["enabled"]
               and (r["kind_id"] or "project-repo") == kind_id for r in rows):
            return {"success": False, "error": "another enabled base makes the cutover ambiguous"}
        family = [r for r in rows if r["id"] in (source_id, target_id)
                  or r["base_workspace_id"] in (source_id, target_id)]
        if (any(r["base_workspace_id"] == target_id for r in family)
                or target["locked_by_agent_id"] or target["locked_by_task_id"]
                or target["job_pin_count"]):
            return {"success": False, "error": "target must be idle and have no existing slots or pins"}
        ids = [r["id"] for r in family]
        paths = [r["workspace_path"] for r in family]
        claims = [dict(r) for r in (await conn.execute(select(
            sessions.c.id, sessions.c.work_dir, sessions.c.task_id, sessions.c.last_claim_epoch,
            sessions.c.claim_phase, sessions.c.state, sessions.c.desired_state,
        ).where(sessions.c.work_dir.in_(paths)).order_by(sessions.c.id))).mappings()]
        owners = [dict(r) for r in (await conn.execute(select(integration_branch_owners).where(
            or_(integration_branch_owners.c.workspace_id.in_(ids),
                integration_branch_owners.c.confirmed_workspace_id.in_(ids)),
        ).order_by(integration_branch_owners.c.id))).mappings()]
        if (any(s["work_dir"] == target["workspace_path"]
                and (s["state"] in ("starting", "running", "draining") or s["claim_phase"])
                for s in claims)
                or any(target_id in (o["workspace_id"], o["confirmed_workspace_id"])
                       and o["handoff_state"] != "released" for o in owners)):
            return {"success": False, "error": "target has a retained session or integration writer"}
        evidence = {r["id"]: await _source(handler.orchestrator.git, r["workspace_path"])
                    for r in family}
        new = evidence[target_id]
        if (not new.get("head") or not new.get("common_directory")
                or not new.get("root")
                or Path(new["root"]).resolve() != Path(target["workspace_path"]).resolve()
                or new.get("dirty_paths") != [] or new.get("claim_sha256")):
            return {"success": False, "error": "target repository must be verifiable and clean without a claim"}
        old_origin = evidence[source_id].get("origin")
        if not old_origin or not new.get("origin") or not repository_urls_match(
            new["origin"], old_origin, base=target["workspace_path"]
        ):
            return {"success": False, "error": "source and target origins must prove the same repository"}
        if new["common_directory"] == evidence[source_id].get("common_directory"):
            return {"success": False, "error": "target must have an independent Git common directory"}
        snapshot = {"project_id": project_id, "source_id": source_id, "target_id": target_id,
                    "workspaces": family, "sessions": claims, "owners": owners, "source": evidence}
        proof = _digest(snapshot)
        if apply and args["expected_proof"] != proof:
            return {"success": False, "error": "cutover proof changed; preview again", "proof": proof}
        if apply:
            old_ids = [r["id"] for r in family if r["id"] != target_id]
            await conn.execute(update(workspaces).where(workspaces.c.id.in_(old_ids)).values(
                **disable_fields(OPERATOR, f"cutover to {target_id}; source retained"),
            ))
            await conn.execute(update(workspaces).where(workspaces.c.id == target_id).values(
                **ENABLE_FIELDS,
            ))
        return {"success": True, "applied": apply, "proof": proof, "snapshot": snapshot,
                "retained": [r["id"] for r in family if r["id"] != target_id],
                "retirement": "All source, writers, claims, generations and checkpoints retained. "
                              "This receipt authorizes no source deletion or lock release."}
