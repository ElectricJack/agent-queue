"""Use retained author fixes as assembly input without changing source identity."""
from dataclasses import replace

from sqlalchemy import select

from src.database.tables import integration_close_attempts as attempts


async def assembly_inputs(db, gitops, repo, batch, members):
    result = []
    for member in members:
        async with db._engine.connect() as conn:
            op = (await conn.execute(select(attempts).where(
                attempts.c.project_id == batch.project_id,
                attempts.c.repository_id == batch.repository_id,
                attempts.c.target_ref == batch.target_ref,
                attempts.c.task_id == member.task_id,
                attempts.c.source_oid == member.source_sha,
                attempts.c.state == "deferred",
                attempts.c.candidate_oid.is_not(None),
            ).order_by(attempts.c.updated_at.desc()).limit(1))).mappings().first()
        if op:
            # Author worktrees can share the primary clone's objects, while the
            # publisher uses its own retained store. Pin exact M there before
            # assembly; neither the source nor the parent branch is rewritten.
            if op["candidate_path"]:
                await gitops.run(repo, "fetch", "--no-tags", "--no-write-fetch-head",
                                 op["candidate_path"],
                                 f"{op['candidate_oid']}:{op['candidate_ref']}")
            await gitops.exact(repo, op["candidate_oid"])
            if await gitops.is_ancestor(repo, member.source_sha, op["candidate_oid"]):
                # Batch membership/withholding/delivery evidence still names S.
                # Existing publisher regeneration and checks now run on M + target.
                member = replace(member, source_sha=op["candidate_oid"])
        result.append(member)
    return tuple(result)
