"""Explicit supervisor assignment for a fixer working on an existing candidate."""

from __future__ import annotations

import asyncio
import json

from sqlalchemy import select

from src.commands.supervisor_authority import integration_operator
from src.database.tables import agents, integration_batches, projects, sessions, task_metadata, tasks
from src.git.manager import GitError, RemoteRefState
from src.integration.batches import candidate_ref
from src.integration.lock import BranchLock, CRITICAL_SECTION_SECONDS
from src.integration.models import BranchKey
from src.integration.ownership import BranchBusy, StaleFence
from src.models import TaskStatus

ASSIGNMENT_KEY = "integration_batch_repair"


class BatchRepairAssignments:
    def __init__(self, db):
        self.db = db
        self.locks = BranchLock(db)

    async def _validate(self, task, batch_id, *, conn):
        if task is None:
            raise GitError("batch repair task no longer exists")
        batch = (await conn.execute(select(integration_batches).where(
            integration_batches.c.id == batch_id,
        ))).mappings().one_or_none()
        project = await self.db.get_project(task.project_id)
        repo = await self.db.get_repo(task.repo_id) if task.repo_id else None
        if (batch is None or batch["project_id"] != task.project_id
                or batch["repository_id"] != task.repo_id
                or batch["intent"] != "open" or not batch["target_ref"]
                or batch["lifecycle"] == "promoted"
                or project is None or repo is None or repo.project_id != task.project_id
                or project.integration_repository_id != task.repo_id
                or project.hierarchical_integration_mode not in {"train", "hierarchy"}
                or not repo.url or task.status in {TaskStatus.COMPLETED, TaskStatus.FAILED}):
            raise GitError("batch repair assignment requires an open batch in the task repository")
        branch = candidate_ref(batch_id).removeprefix("refs/heads/")
        protected = {repo.default_branch, project.repo_default_branch}
        flow = await conn.scalar(select(projects.c.promotion_flow).where(
            projects.c.id == task.project_id,
        ))
        for step in flow or []:
            if isinstance(step, dict):
                protected.update((step.get("source"), step.get("target")))
        if branch in {ref.removeprefix("refs/heads/") for ref in protected if ref}:
            raise GitError("default and promotion branches cannot be assigned for batch repair")
        return {"batch_id": batch_id, "project_id": task.project_id,
                "repository_id": task.repo_id, "repository_url": repo.url,
                "branch": branch,
                "target_ref": batch["target_ref"]}

    async def assign(self, task, batch_id):
        by, refusal = await integration_operator(self.db, task.project_id)
        if refusal:
            raise GitError(refusal)
        if not isinstance(batch_id, str) or not batch_id.strip():
            raise GitError("repair_batch requires a batch id")
        async with self.db.immediate() as conn:
            await conn.execute(select(integration_batches.c.id).where(
                integration_batches.c.id == batch_id,
            ).with_for_update())
            # Publication and reassignment share this task lock. Do not change
            # its canonical integration checkpoint just to authorize a fixer.
            await conn.execute(select(tasks.c.id).where(
                tasks.c.id == task.id,
            ).with_for_update())
            task = await self.db.get_task(task.id)
            binding = await self._validate(task, batch_id, conn=conn)
            await self.db._upsert_meta(
                task.id, ASSIGNMENT_KEY, {**binding, "assigned_by": by}, conn=conn,
            )
        return binding

    async def get(self, task, *, conn=None):
        if conn is None:
            async with self.db._engine.connect() as connection:
                return await self.get(task, conn=connection)
        raw = await conn.scalar(select(task_metadata.c.value).where(
            task_metadata.c.task_id == task.id, task_metadata.c.key == ASSIGNMENT_KEY,
        ))
        if raw is None:
            return None
        try:
            recorded = json.loads(raw)
            current = await self._validate(task, recorded["batch_id"], conn=conn)
            if any(recorded.get(key) != value for key, value in current.items()):
                raise ValueError("binding changed")
        except (KeyError, TypeError, ValueError) as exc:
            raise GitError("batch repair assignment is invalid") from exc
        return current

    async def publish(self, task_id, binding, *, git, checkout, policy,
                      expected_oid=None, event_bus=None, principal=None, claim_epoch=None):
        """Publish only a descendant of the observed candidate, under its lease."""
        try:
            async with asyncio.timeout(CRITICAL_SECTION_SECONDS), self.db.immediate() as conn:
                # Claim transitions lock the session before the task. Keep
                # the authenticated holder fenced through network publication.
                if principal is not None:
                    await conn.execute(select(sessions.c.id).where(
                        sessions.c.id == principal.session_id,
                    ).with_for_update())
                await conn.execute(select(integration_batches.c.id).where(
                    integration_batches.c.id == binding["batch_id"],
                ).with_for_update())
                await conn.execute(select(tasks.c.id).where(
                    tasks.c.id == task_id,
                ).with_for_update())
                task = await self.db.get_task(task_id)
                if task is None or task.status is not TaskStatus.IN_PROGRESS:
                    raise GitError("batch repair task is no longer active")
                if principal is not None:
                    from src.api.auth import RequestScope
                    from src.api.scope import held_task_for_session
                    from src.commands.principal import matches_session_instance

                    await conn.execute(select(agents.c.id).where(
                        agents.c.id == task.assigned_agent_id,
                    ).with_for_update())
                    held = await held_task_for_session(self.db, RequestScope(
                        kind="session", session_id=principal.session_id,
                        project_id=principal.project_id, task_id=principal.task_id,
                    ))
                    session = await self.db.get_session(principal.session_id)
                    if (held is None or held.id != task_id or held.claim_epoch != claim_epoch
                            or session is None
                            or not matches_session_instance(principal, session.instance_token)):
                        raise GitError("batch repair publication requires the current held claim")
                current = await self.get(task, conn=conn)
                if current != binding:
                    raise GitError("batch repair assignment changed")
                target = BranchKey(repository_id=binding["repository_id"], branch=binding["branch"])
                fence = await self.locks.acquire_on(
                    conn, target, task_id, ttl_seconds=CRITICAL_SECTION_SECONDS, role="repair",
                )
                async with self.locks.exclusion(fence, conn=conn):
                    remote = await git.als_remote_ref(
                        checkout, binding["branch"], repository_url=binding["repository_url"],
                    )
                    if remote.state is not RemoteRefState.PRESENT or not remote.oid:
                        raise GitError("batch repair candidate is missing or could not be observed")
                    if expected_oid not in (None, remote.oid):
                        raise GitError("batch repair candidate moved")
                    tip = await git.arev_parse(checkout, binding["branch"])
                    if not tip or await git.ais_ancestor(
                        checkout, remote.oid, tip, strict=True,
                    ) is not True:
                        raise GitError("batch repair push must fast-forward the current candidate")
                    inherited = await git.als_remote_ref(
                        checkout, binding["target_ref"].removeprefix("refs/heads/"),
                        repository_url=binding["repository_url"],
                    )
                    if inherited.state is not RemoteRefState.PRESENT or not inherited.oid:
                        raise GitError("batch repair target is missing or could not be observed")
                    # The target's published history may include upstream merge
                    # committers; repair-only commits must still match the policy.
                    await git._arun(
                        ["cat-file", "-e", f"{inherited.oid}^{{commit}}"], cwd=checkout,
                    )
                    oid = await git.apush_validated_delivery(
                        checkout, remote.oid, tip, binding["branch"],
                        expected_remote_oid=remote.oid, repository_url=binding["repository_url"],
                        identity_policy=policy, event_bus=event_bus,
                        project_id=binding["project_id"],
                        inherited_oids=(remote.oid, inherited.oid),
                    )
                    if oid != tip:
                        raise GitError("batch repair push did not confirm its immutable tip")
                    await self.locks.release(fence, conn=conn)
                    return oid
        except (BranchBusy, StaleFence, TimeoutError) as exc:
            raise GitError(f"batch repair publication refused: {exc}") from exc


async def wake_pushed_batch(db, train, event):
    """A push is a scheduling hint; the next visit still fetches exact Git truth."""
    project_id, branch = event.get("project_id"), event.get("branch") or ""
    if not project_id or not branch.removeprefix("refs/heads/").startswith("aq/batches/"):
        return
    async with db._engine.connect() as conn:
        rows = (await conn.execute(select(integration_batches).where(
            integration_batches.c.project_id == project_id,
            integration_batches.c.intent == "open",
            integration_batches.c.target_ref.is_not(None),
            integration_batches.c.lifecycle != "promoted",
            integration_batches.c.integration_branch.in_((
                branch.removeprefix("refs/heads/"),
                "refs/heads/" + branch.removeprefix("refs/heads/"),
            )),
        ))).mappings().all()
    for row in rows:
        if candidate_ref(row["id"]).removeprefix("refs/heads/") == branch.removeprefix("refs/heads/"):
            train.wake(project_id, row["repository_id"], row["target_ref"])
