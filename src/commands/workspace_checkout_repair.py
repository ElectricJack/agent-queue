"""Correct a base registration using its existing slots as object-store proof."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from sqlalchemy import or_, select, update

from src.commands.supervisor_authority import operator_or_global_supervisor
from src.database.tables import integration_branch_owners, projects, sessions, workspaces
from src.git.manager import GitError, repository_urls_match
from src.orchestrator.worktree_manager import WorktreeSlotManager
from src.workspace_registration import path_is_missing


def _refuse(code: str, error: str) -> dict:
    return {"success": False, "error_code": code, "error": error}


async def _identity(git, path: str) -> dict:
    root = await git._arun(["rev-parse", "--show-toplevel"], cwd=path)
    if Path(root).resolve(strict=True) != Path(path).resolve(strict=True):
        raise ValueError(f"{path} is not a checkout root")
    common = Path(await git._arun(
        ["rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=path,
    )).resolve(strict=True)
    objects = (common / "objects").resolve(strict=True)
    stat = objects.stat()
    if not objects.is_dir():
        raise ValueError(f"{objects} is not an object directory")
    origin = await git.aget_remote_url(path)
    resolved_origin = (
        str((Path(path) / origin).resolve())
        if origin and origin.startswith(("./", "../")) else origin
    )
    return {"origin": origin, "resolved_origin": resolved_origin, "common_directory": str(common),
            "objects_directory": str(objects), "objects_device": stat.st_dev,
            "objects_inode": stat.st_ino}


async def repair(handler, args: dict) -> dict:
    operator_id, refusal = await operator_or_global_supervisor(handler.db)
    if refusal:
        return _refuse("global_operator_required", refusal)
    required = {"project_id", "workspace_id", "expected_path", "replacement_path", "reason"}
    if (set(args) - required - {"apply", "expected_proof"} or not required <= set(args)
            or any(not isinstance(args[k], str) or not args[k].strip() for k in required)
            or not isinstance(args.get("apply", False), bool)
            or ("expected_proof" in args and not isinstance(args["expected_proof"], str))):
        return _refuse("invalid_arguments", "Provide project, base, paths and reason")
    apply = args.get("apply", False)
    if apply and not args.get("expected_proof"):
        return _refuse("proof_required", "Apply requires expected_proof from preview")
    expected, replacement = args["expected_path"], args["replacement_path"]
    if not Path(expected).is_absolute() or not Path(replacement).is_absolute():
        return _refuse("invalid_arguments", "Both paths must be absolute")
    try:
        replacement = str(Path(replacement).resolve(strict=True))
    except (OSError, RuntimeError) as exc:
        return _refuse("replacement_unverified", str(exc))
    if Path(expected).resolve() == Path(replacement):
        return _refuse("workspace_checkout_stale", "Replacement must differ from expected path")

    # Admission updates lock these rows too. Never release a retained slot or
    # rewrite its source to accommodate an incorrect registration.
    async with handler.db.immediate() as conn:
        project = (await conn.execute(select(projects).where(
            projects.c.id == args["project_id"],
        ).with_for_update())).mappings().first()
        rows = [dict(row) for row in (await conn.execute(select(workspaces).where(
            workspaces.c.project_id == args["project_id"],
        ).order_by(workspaces.c.id).with_for_update())).mappings()]
        base = next((row for row in rows if row["id"] == args["workspace_id"]), None)
        if project is None or base is None or base["slot_index"] is not None or base["base_workspace_id"]:
            return _refuse("base_not_found", "Workspace must be a base in this project")
        if base["workspace_path"] != expected:
            return _refuse("workspace_checkout_stale", "Expected path no longer matches registration")
        slots = [row for row in rows if row["base_workspace_id"] == base["id"]]
        kind_id = base["kind_id"] or "project-repo"
        kind = await handler.db.resolve_workspace_kind(base["project_id"], kind_id)
        if not slots or kind is None or not kind.is_git_repo or kind.mode != "worktree":
            return _refuse("identity_unverified", "A worktree base with existing slots is required")
        other_paths = (await conn.execute(select(workspaces.c.workspace_path).where(
            workspaces.c.id != base["id"],
        ))).scalars()
        if any(Path(path).resolve() == Path(replacement) for path in other_paths):
            return _refuse("replacement_registered", "Replacement already has a workspace registration")
        if base["locked_by_agent_id"] or base["locked_by_task_id"] or base["job_pin_count"]:
            return _refuse("base_busy", "Base must have no lock or job pin")
        active = [dict(row) for row in (await conn.execute(select(
            sessions.c.id, sessions.c.work_dir, sessions.c.state, sessions.c.claim_phase,
        ).where(sessions.c.work_dir.in_([expected, replacement]),
                sessions.c.ended_at.is_(None)))).mappings()]
        owners = (await conn.execute(select(integration_branch_owners.c.id).where(
            or_(integration_branch_owners.c.workspace_id == base["id"],
                integration_branch_owners.c.confirmed_workspace_id == base["id"]),
            integration_branch_owners.c.handoff_state != "released",
        ))).all()
        if (owners or any(s["state"] in ("starting", "running", "draining") or s["claim_phase"]
                          for s in active)
                or any(Path(path, ".aq", "claim.json").exists() for path in (expected, replacement))):
            return _refuse("base_busy", "Base or replacement retains a session, claim or integration writer")

        git = handler.orchestrator.git
        try:
            new = await _identity(git, replacement)
            if Path(new["common_directory"]) != Path(replacement, ".git").resolve(strict=True):
                raise ValueError("Replacement must be a base clone, not a linked worktree")
            old = None if path_is_missing(expected) else await _identity(git, expected)
            authorized = kind.repo_url or (project["repo_url"] if kind_id == "project-repo" else None)
            origins = [origin for origin in (authorized, old and old["resolved_origin"]) if origin]
            if (not origins or not new["origin"] or (old is not None and not old["origin"])
                    or any(not repository_urls_match(new["resolved_origin"], origin, base=replacement)
                           for origin in origins)):
                raise ValueError("Old/authorized and replacement origins must match")
            evidence = {}
            for slot in slots:
                sentinel = WorktreeSlotManager.read_sentinel(slot["workspace_path"])
                if (slot["slot_index"] is None or (slot["kind_id"] or "project-repo") != kind_id
                        or sentinel is None or sentinel.project_id != base["project_id"]
                        or sentinel.base_workspace_id != base["id"]
                        or sentinel.workspace_id not in ("", slot["id"])
                        or sentinel.slot_index != slot["slot_index"]):
                    raise ValueError(f"Slot {slot['id']} has unproved sentinel identity")
                identity = await _identity(git, slot["workspace_path"])
                if (any(identity[key] != new[key] for key in new
                        if key not in {"origin", "resolved_origin"})
                        or not identity["origin"] or not repository_urls_match(
                            identity["resolved_origin"], new["resolved_origin"],
                            base=slot["workspace_path"],
                        )):
                    raise ValueError(f"Slot {slot['id']} does not share replacement object store and origin")
                evidence[slot["id"]] = {**identity, "sentinel": sentinel.to_dict()}
        except (OSError, RuntimeError, GitError, ValueError) as exc:
            return _refuse("identity_unverified", str(exc))

        after = {**base, "workspace_path": replacement}
        snapshot = {"before": base, "after": after, "slots": slots,
                    "identity": {"old": old, "replacement": new, "slots": evidence},
                    "authorized_origin": authorized, "reason": args["reason"]}
        proof = hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()
        if apply and args["expected_proof"] != proof:
            return {**_refuse("proof_changed", "Repair proof changed; preview again"), "proof": proof}
        event_id = None
        if apply:
            operator_id, refusal = await operator_or_global_supervisor(handler.db)
            if refusal:
                return _refuse("global_operator_required", refusal)
            await conn.execute(update(workspaces).where(workspaces.c.id == base["id"]).values(
                workspace_path=replacement,
            ))
            event_id = await handler.db.log_event(
                "project.workspace_checkout_repaired", project_id=base["project_id"],
                payload=json.dumps({**snapshot, "proof": proof, "operator_id": operator_id}), conn=conn,
            )
        return {"success": True, "project_id": base["project_id"], "workspace_id": base["id"],
                "changed": apply, "preview": not apply, "proof": proof,
                **snapshot, "event_id": event_id}
