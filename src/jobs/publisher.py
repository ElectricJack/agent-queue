"""Publisher validation through the managed queue on immutable Git snapshots."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

from sqlalchemy import select

from src.database.tables import jobs, workspaces
from src.jobs.adapters import finite_command, validation_evidence
from src.jobs.policy import JobError, TERMINAL
from src.jobs.workspace import mutation_guard
from src.models import RepoSourceType, Workspace


async def publisher_check(service, git, store, project_id, head, command, policy) -> dict:
    preset, args = finite_command(command)
    identity = hashlib.sha256(
        f"{project_id}:{head}:{command}:{policy.timeout_seconds}:{policy.slot_wait_seconds}".encode()
    ).hexdigest()
    owner_id = f"validation:{identity}"
    workspace_id = f"job-snapshot:{identity}"
    snapshot = Path(service.config.data_dir) / "job-snapshots" / identity

    async def cleanup_snapshot():
        workspace = await service.db.get_workspace(workspace_id)
        async with mutation_guard(service.db, workspace):
            if snapshot.exists():
                await git._arun(["worktree", "remove", "--force", str(snapshot)], cwd=str(store))
            await service.db.delete_workspace(workspace_id)

    # Recover the same producer after a daemon interruption. A terminal result
    # is replayed; no checkout or reset touches a running, pinned snapshot.
    async with service.db._engine.connect() as conn:
        existing = (
            (
                await conn.execute(
                    select(jobs)
                    .where(
                        jobs.c.project_id == project_id,
                        jobs.c.owner_kind == "integration",
                        jobs.c.owner_id == owner_id,
                    )
                    .order_by(jobs.c.submitted_at.desc())
                )
            )
            .mappings()
            .first()
        )
    job = dict(existing) if existing else None
    attempt = int(job["idempotency_key"].rsplit(":", 1)[-1]) if job else 0
    if (
        job
        and job["state"] in TERMINAL
        and (
            (job.get("result") or {}).get("outcome") in {"infrastructure", "lost", "cancelled"}
            or (job.get("result") or {}).get("input_stability") != "stable"
        )
        and not await service.db.workspace_has_job_pin(workspace_id)
    ):
        job = None
    if job is None:
        workspace = await service.db.get_workspace(workspace_id)
        if workspace is None:
            await asyncio.to_thread(snapshot.parent.mkdir, parents=True, exist_ok=True)
            # A crash after worktree creation but before the row insert leaves
            # a reusable checkout, never a reason to force-remove it.
            if not snapshot.exists():
                await git._arun(
                    ["worktree", "add", "--detach", str(snapshot), head], cwd=str(store)
                )
            await service.db.create_workspace(
                Workspace(
                    id=workspace_id,
                    project_id=project_id,
                    workspace_path=str(snapshot),
                    source_type=RepoSourceType.LINK,
                    kind_id="integration-snapshot",
                    enabled=False,
                )
            )
        async with service.db._engine.connect() as conn:
            generation = await conn.scalar(
                select(workspaces.c.generation).where(
                    workspaces.c.id == workspace_id,
                )
            )
        try:
            job = await service.submit(
                project_id=project_id,
                task_id=None,
                session_id=None,
                claim_epoch=None,
                workspace_id=workspace_id,
                generation=generation,
                preset=preset,
                args=args,
                idempotency_key=f"{identity}:{attempt + 1}",
                owner_kind="integration",
                owner_id=owner_id,
                input_mode="snapshot",
                input_ref=head,
                trusted_band=1,
                queue_seconds=policy.slot_wait_seconds,
                run_seconds=policy.timeout_seconds,
            )
        except JobError:
            # A definite admission refusal created no producer. Ambiguous
            # failures and cancellation deliberately preserve the checkout.
            if not await service.db.workspace_has_job_pin(workspace_id):
                await cleanup_snapshot()
            raise
    try:
        while job["state"] not in TERMINAL:
            # Integration can be awaited from the cascade itself. Its producer
            # must progress without waiting for a later cascade step to tick.
            await service.tick()
            job = await service.db.get_job(job["id"])
            if job["state"] not in TERMINAL:
                await asyncio.sleep(0.25)
        return validation_evidence(command, job, policy)
    finally:
        # An interrupted publisher detaches; execution and the pin survive.
        # Only verified cleanup authorizes removing its dedicated worktree.
        if job["state"] in TERMINAL and not await service.db.workspace_has_job_pin(workspace_id):
            await cleanup_snapshot()
