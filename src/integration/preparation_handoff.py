"""Resume a failed pool preparation's own branch handoff, without stopping it."""

import json

from src.git.manager import GitError
from src.git.process import PREPARE_CLEANUP_HOLD
from src.integration.models import BranchKey, Fence, REQUEUE_INTEGRATION_OWNER_ROLES
from src.integration.ownership import BranchOwnership, BranchOwnershipError
from src.models import TaskStatus


async def resume_preparation_handoff(
    orchestrator, owner, *, session_id, workspace_id, dry_run=False, require_failure=False,
) -> bool:
    """Restore only the exact unactivated claim; the caller holds its task control lock.

    A live pool session is safe here because its claim has never activated. The
    ordinary clean/published checkout proof and fenced self-transfer still run.
    Neither the claim nor the workspace lock is released, so preparation can retry.
    """
    db = orchestrator.db
    task = await db.get_task(owner["owner_id"])
    session = await db.get_session(session_id) if session_id else None
    workspace = await db.get_workspace(workspace_id) if workspace_id else None
    if (
        owner.get("fence") is not None
        or owner["handoff_state"] not in {"attached", "handoff_pending"}
        or owner["owner_role"] not in REQUEUE_INTEGRATION_OWNER_ROLES
        or owner.get("session_id") != session_id
        or owner.get("workspace_id") != workspace_id
        or task is None or session is None or workspace is None
        or session.lifecycle != "pool" or session.claim_phase != "preparing"
        or not session.instance_token
        or session.task_id != task.id or session.last_claim_epoch != task.claim_epoch
        or task.status != TaskStatus.IN_PROGRESS
        or task.assigned_agent_id != session.agent_id
        or task.repo_id != owner["repository_id"]
        or not task.branch_name
        or task.branch_name.removeprefix("refs/heads/") != owner["ref"].removeprefix("refs/heads/")
        or await db.get_task_meta(task.id, PREPARE_CLEANUP_HOLD) is not None
    ):
        return False
    if require_failure:
        failure = await db.get_task_meta(task.id, "prepare_failure")
        expected = {
            "task_id": task.id, "session_id": session.id,
            "instance_token": session.instance_token, "claim_epoch": task.claim_epoch,
            "workspace_id": workspace.id, "workspace_generation": workspace.generation,
            "disposition": "retained",
        }
        if not isinstance(failure, dict) or any(failure.get(k) != v for k, v in expected.items()):
            return False
    async with db.immediate() as conn:
        if await db.lock_claim_workspace(
            conn, session.id, task.id, epoch=task.claim_epoch,
            instance_token=session.instance_token, expected_workspace=workspace,
            allow_retained_preparation=True,
        ) is None:
            return False
    if dry_run:
        from src.orchestrator.workspace_attachments import (
            probe_slot_for_integration_handoff, probe_workspace_for_integration_handoff,
        )

        repository = await db.get_repo(owner["repository_id"])
        if repository is None:
            return False
        kwargs = dict(
            expected_branch=owner["ref"], repository_url=repository.url,
            default_branch=repository.default_branch, allow_published_detached_head=True,
        )
        if workspace.is_slot:
            return await probe_slot_for_integration_handoff(
                db, orchestrator.git, orchestrator._git_mutex, workspace, **kwargs,
            )
        return await probe_workspace_for_integration_handoff(
            orchestrator.git, orchestrator._git_mutex, workspace, **kwargs,
        )

    async def confirm(current):
        # Consume this snapshot only; never adopt a successor attachment.
        if any(current.get(k) != owner.get(k) for k in (
            "id", "owner_id", "owner_role", "fence_token", "session_id", "workspace_id",
        )):
            return False
        return await orchestrator.aconfirm_integration_pool_owner_handoff(
            current, require_preparation=True, expected_preparation=(
                task.claim_epoch, session.instance_token, workspace.generation,
            ),
        )

    ownership = BranchOwnership(db, confirm_handoff=confirm)
    try:
        await ownership.transfer(
            Fence(
                target=BranchKey(repository_id=owner["repository_id"], branch=owner["ref"]),
                owner_id=task.id, token=owner["fence_token"],
            ),
            task.id, owner["owner_role"],
        )
    except BranchOwnershipError:
        return False
    return True


async def recover_preparation_handoffs(orchestrator, owners, *, principal, dry_run):
    """Operator-only adapter: prove a recorded failure under the preparation lock."""
    from src.integration.owner_recovery import RecoveryOutcome

    outcomes = []
    for owner in owners:
        async with orchestrator._task_control_lock(owner["owner_id"]):
            current = await BranchOwnership(orchestrator.db).get_owner(
                BranchKey(repository_id=owner["repository_id"], branch=owner["ref"]),
            )
            evidence = {k: owner.get(k) for k in (
                "fence_token", "session_id", "workspace_id", "handoff_state",
            )}
            same = current is not None and all(current.get(k) == owner.get(k) for k in (
                "id", "owner_id", "owner_role", *evidence,
            ))
            try:
                restored = same and await resume_preparation_handoff(
                    orchestrator, owner, session_id=owner.get("session_id"),
                    workspace_id=owner.get("workspace_id"), dry_run=dry_run,
                    require_failure=True,
                )
            except GitError as exc:
                restored = False
                evidence["detail"] = str(exc)
            if restored:
                evidence["reservation_restored"] = True
                evidence["claim_retained"] = True
                if not dry_run:
                    await orchestrator.db.log_event(
                        "integration.preparation_handoff_restored",
                        project_id=(await orchestrator.db.get_task(owner["owner_id"])).project_id,
                        task_id=owner["owner_id"],
                        payload=json.dumps({**evidence, "principal": principal}),
                    )
                    await orchestrator.db.add_task_comment(
                        owner["owner_id"],
                        f"{principal} restored failed preparation's own branch reservation; "
                        f"session {owner['session_id']} and its claim remain held for retry.",
                        author_kind="supervisor" if principal.startswith("supervisor ") else "user",
                        author_id=principal,
                    )
            else:
                evidence.setdefault("detail", "Exact failed preparation or published checkout unproven")
            outcomes.append(RecoveryOutcome(
                owner["id"], "released" if restored else "not_eligible",
                None if restored else "self_owned_preparation_unproven", evidence, dry_run,
            ))
    return outcomes
