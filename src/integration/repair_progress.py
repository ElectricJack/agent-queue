"""Recover a batch delegate's unpublished progress without accepting it as CI evidence."""

from __future__ import annotations

import os

from sqlalchemy import select

from src.database.tables import (
    integration_batch_members,
    integration_batches,
    integration_candidate_revisions,
    integration_owner_recoveries,
    integration_repair_stages,
    repos,
)
from src.git.manager import GitError, RemoteRefState, is_valid_git_oid
from src.integration.owner_recovery import PRESERVED_AND_RELEASED, RELEASED, recovery_ref_allowed


async def batch_recovery_progress(db, recovery, operation, stage, target) -> dict | None:
    """Read durable stop/preservation evidence and prove its frozen batch lineage.

    The audit survives a crash after release and a sweep that beats dispatch.
    Only terminal predecessors of this operation qualify. Git proof happens
    outside a database transaction; dispatch rechecks the subject before READY.
    """
    if operation["target_kind"] != "batch" or int(stage["ordinal"]) == 0:
        return None
    audit, predecessor = integration_owner_recoveries, integration_repair_stages
    async with db._engine.connect() as conn:
        row = (await conn.execute(
            select(audit, predecessor.c.ordinal.label("previous_ordinal"),
                   predecessor.c.dossier.label("previous_dossier"),
                   predecessor.c.current_subject.label("previous_subject"))
            .join(predecessor, predecessor.c.repair_task_id == audit.c.task_id)
            .where(
                predecessor.c.operation_id == operation["id"],
                predecessor.c.ordinal < stage["ordinal"],
                predecessor.c.state.in_(("failed", "expired")),
                audit.c.repository_id == target.repository_id,
                audit.c.ref == target.branch,
                audit.c.outcome.in_((RELEASED, PRESERVED_AND_RELEASED)),
            )
            .order_by(predecessor.c.ordinal.desc(), audit.c.created_at.desc(), audit.c.id)
            .limit(1)
        )).mappings().one_or_none()
        if row is None or row["outcome"] == RELEASED:
            return None
        batch = (await conn.execute(select(integration_batches).where(
            integration_batches.c.id == operation["batch_id"]
        ))).mappings().one()
        members = (await conn.execute(select(integration_batch_members).where(
            integration_batch_members.c.batch_id == batch["id"]
        ).order_by(integration_batch_members.c.ordinal))).mappings().all()
        repository_url = (await conn.execute(select(repos.c.url).where(
            repos.c.id == target.repository_id
        ))).scalar_one()
        candidate = (await conn.execute(select(integration_candidate_revisions).where(
            integration_candidate_revisions.c.batch_id == batch["id"],
            integration_candidate_revisions.c.revision == batch["current_revision"],
        ))).mappings().one_or_none()
    evidence = dict(row["evidence"] or {})
    sha, ref = evidence.get("preserved_sha"), evidence.get("preserved_ref")
    subject = dict(stage["current_subject"] or {})
    base = subject.get("candidate_sha")
    manifest = dict((stage["dossier"] or {}).get("manifest", {}))
    previous_subject = dict(row["previous_subject"] or {})
    # A subsequent accepted/rebuilt revision has its own authoritative tip.
    # Historical preservation must never rewind it or obstruct green handback.
    if (
        candidate is not None
        and subject.get("revision") == batch["current_revision"]
        and subject.get("revision", -1) > previous_subject.get("revision", -1)
        and base == candidate["head_sha"]
    ):
        return None
    if (
        recovery is None
        or not is_valid_git_oid(sha or "")
        or not is_valid_git_oid(base or "")
        or not recovery_ref_allowed(ref, target.branch, row["owner_row_id"], sha)
        or manifest != (row["previous_dossier"] or {}).get("manifest")
        or manifest.get("batch_id") != batch["id"]
        or manifest.get("source_manifest_digest") != batch["source_manifest_digest"]
        or manifest.get("revision") != batch["current_revision"]
        or subject.get("revision") != batch["current_revision"]
    ):
        raise GitError("preserved batch progress has inconsistent frozen lineage")
    checkout = evidence.get("checkout")
    if not isinstance(checkout, str) or not os.path.isdir(checkout):
        raise GitError("preserved batch progress checkout is unavailable")
    git = recovery.git
    async with recovery._mutex(checkout):
        await git.afetch_origin(checkout, repository_url=repository_url, lock_held=True)
    remote = await git.als_remote_ref(checkout, ref)
    direct = ref == target.branch.removeprefix("refs/heads/")
    published = remote if direct else await git.als_remote_ref(checkout, target.branch)
    contained = (
        published.state is RemoteRefState.PRESENT
        and await git.ais_ancestor(checkout, sha, published.oid, strict=True)
    )
    if not (
        remote.state is RemoteRefState.PRESENT and (remote.oid == sha or (direct and contained))
        or remote.state is RemoteRefState.ABSENT and contained
    ):
        raise GitError("preserved batch progress ref does not match its recorded exact SHA")
    if await git.ais_ancestor(checkout, base, sha, strict=True) is not True:
        raise GitError("preserved batch progress does not descend from its frozen subject")
    commits = (await git._arun(
        ["rev-list", "--first-parent", "--reverse", f"{base}..{sha}"], cwd=checkout
    )).splitlines()
    if commits and (await git._arun(
        ["rev-parse", f"{commits[0]}^1"], cwd=checkout
    )).strip() != base:
        raise GitError("preserved batch progress changed its first-parent repair lineage")
    completed = []
    for member in members:
        if await git.ais_ancestor(checkout, member["reviewed_head_sha"], sha, strict=True):
            completed.append(int(member["ordinal"]))
    return {
        "recovery_id": row["id"],
        "owner_row_id": row["owner_row_id"],
        "previous_task_id": row["task_id"],
        "previous_ordinal": int(row["previous_ordinal"]),
        "ref": ref,
        "sha": sha,
        "base_sha": base,
        "subject": subject,
        "manifest": manifest,
        "repair_commits": commits,
        "completed_member_ordinals": completed,
        "evidence": evidence,
    }
