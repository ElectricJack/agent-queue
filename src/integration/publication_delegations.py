"""Explicit, single-use publication authority; ordinary worker scope stays narrow."""

from __future__ import annotations

import asyncio
import time

from sqlalchemy import select, update

from src.commands.principal import PrincipalKind, current_principal, matches_session_instance
from src.commands.supervisor_authority import integration_operator
from src.database import tables as t
from src.git.manager import GitError
from src.integration.lock import CRITICAL_SECTION_SECONDS, BranchLock, managed
from src.integration.models import BranchKey, Fence
from src.integration.ownership import BranchBusy, StaleFence


class DelegationRefused(GitError):
    """No authority was obtained; the grant may not be used for publication."""


HOLDER = "publication-delegation:"


class PublicationDelegations:
    """Each grant lives on its target's owner lease row (``delegation`` JSONB).

    The lease holder ``publication-delegation:<id>`` names the grant; the binding
    and its terminal audit stay readable until the next acquisition of the ref.
    """

    def __init__(self, db, *, clock=time.time, active=None):
        self.db, self.clock = db, clock
        self.locks = BranchLock(db, clock=clock)
        self.active = active

    async def _authority(self, project_id):
        by, refusal = await integration_operator(self.db, project_id)
        if refusal:
            raise DelegationRefused(refusal)
        principal = current_principal()
        if principal is not None and principal.kind is PrincipalKind.SESSION:
            session = await self.db.get_session(principal.session_id)
            if session is None or not matches_session_instance(principal, session.instance_token):
                raise DelegationRefused("supervisor session instance is stale")
        return by

    @staticmethod
    def _target(row):
        return BranchKey(repository_id=row["repository_id"], branch=row["target_branch"])

    @classmethod
    def _fence(cls, row):
        return Fence(target=cls._target(row), owner_id=HOLDER + row["id"],
                     token=row["owner_fence"])

    @staticmethod
    def _view(owner):
        if owner is None or owner["delegation"] is None:
            return None
        return {**owner["delegation"], "repository_id": owner["repository_id"],
                "target_branch": owner["ref"]}

    @classmethod
    async def _grant(cls, conn, delegation_id, *, lock=False):
        owners = t.integration_branch_owners
        query = select(owners).where(
            owners.c.owner_id == HOLDER + delegation_id, owners.c.delegation.is_not(None),
        ).order_by(owners.c.updated_at.desc()).limit(1)
        if lock:
            query = query.with_for_update()
        return cls._view((await conn.execute(query)).mappings().first())

    @staticmethod
    async def _record(conn, row, **changes):
        owners = t.integration_branch_owners
        binding = {k: v for k, v in {**row, **changes}.items()
                   if k not in {"repository_id", "target_branch"}}
        await conn.execute(update(owners).where(
            owners.c.repository_id == row["repository_id"],
            owners.c.owner_id == HOLDER + row["id"],
        ).values(delegation=binding))
        return {**row, **changes}

    @staticmethod
    async def _row(conn, table, row_id, *, lock=False):
        query = select(table).where(table.c.id == row_id)
        if lock:
            query = query.with_for_update()
        return (await conn.execute(query)).mappings().one_or_none()

    async def _claim(self, conn, row, *, lock=False):
        # Match the claim protocol's session -> task ordering, then fence the agent.
        session = await self._row(conn, t.sessions, row["session_id"], lock=lock)
        task = await self._row(conn, t.tasks, row["task_id"], lock=lock)
        if (session is None or task is None
                or session["task_id"] != task["id"]
                or session["project_id"] != row["project_id"]
                or task["project_id"] != row["project_id"]
                or session["lifecycle"] not in {"task", "pool"}
                or session["state"] not in {"starting", "running"}
                or session["desired_state"] != "running"
                or task["status"] != "IN_PROGRESS"
                or not session["agent_id"]
                or task["assigned_agent_id"] != session["agent_id"]
                or task["claim_epoch"] != row["claim_epoch"]
                or (session["lifecycle"] == "pool"
                    and session["last_claim_epoch"] != row["claim_epoch"])
                or (session["last_claim_epoch"] is not None
                    and session["last_claim_epoch"] != row["claim_epoch"])
                or not session["instance_token"]):
            raise DelegationRefused("delegation requires the current live held claim")
        agent = await self._row(conn, t.agents, session["agent_id"], lock=lock)
        if (agent is None or not agent["enabled"] or agent["deleted_at"] is not None
                or agent["state"] != "BUSY" or agent["current_task_id"] != task["id"]):
            raise DelegationRefused("delegation agent no longer holds the task")
        if ("session_instance_token" in row
                and session["instance_token"] != row["session_instance_token"]):
            raise DelegationRefused("delegation session instance is stale")
        if "source_branch" in row and task["branch_name"] != row["source_branch"]:
            raise DelegationRefused("delegation source branch changed")
        return session, task

    async def _repository(self, conn, row, task):
        project = (await conn.execute(select(t.projects).where(
            t.projects.c.id == row["project_id"],
        ).with_for_update(read=True))).mappings().one_or_none()
        repo = (await conn.execute(select(t.repos).where(
            t.repos.c.id == row["repository_id"],
        ).with_for_update(read=True))).mappings().one_or_none()
        if (project is None or repo is None
                or project["hierarchical_integration_mode"] not in {"hierarchy", "train"}
                or project["integration_repository_id"] != repo["id"]
                or repo["project_id"] != project["id"] or not repo["url"]
                or task["repo_id"] not in (None, repo["id"])
                or ("repository_url" in row and row["repository_url"] != repo["url"])):
            raise DelegationRefused("delegation repository binding changed or is not authorized")
        protected = {"main", "dev", "staging", repo["default_branch"],
                     project["repo_default_branch"]}
        for step in project["promotion_flow"] or []:
            if isinstance(step, dict):
                protected.update((step.get("source"), step.get("target")))
        if row["target_branch"] in {ref.removeprefix("refs/heads/") for ref in protected if ref}:
            raise DelegationRefused("default and promotion branches cannot be delegated")
        return repo

    async def issue(self, request):
        by = await self._authority(request.project_id)
        binding = request.model_dump()
        async with asyncio.timeout(CRITICAL_SECTION_SECONDS), self.db.immediate() as conn:
            session, task = await self._claim(conn, binding, lock=True)
            repo = await self._repository(conn, binding, task)
            if not task["branch_name"] or request.target_branch in {
                task["branch_name"], f"aq/{task['id']}",
            }:
                raise DelegationRefused("use ordinary publication for the held task branch")
            target = self._target(binding)
            owner = await self.locks.lock_on(conn, target)
            prior = await self._grant(conn, request.id)
            if prior is not None:
                keys = ("project_id", "repository_id", "task_id", "session_id", "claim_epoch",
                        "target_branch", "expected_target_oid", "expected_owner_fence", "reason",
                        "ttl_seconds")
                if (any(prior[key] != binding[key] for key in keys)
                        or prior["repository_id"] != binding["repository_id"]
                        or prior["issued_by"] != by):
                    raise DelegationRefused("delegation id already records another request")
                return prior
            if owner is not None and not managed(owner):
                raise DelegationRefused("legacy target owner requires managed owner recovery")
            if (owner["fence"] if owner else 0) != request.expected_owner_fence:
                raise DelegationRefused("target owner fence changed")
            if owner and owner["holder"] and owner["expires_at"] > self.clock():
                raise DelegationRefused("target has another unexpired integration writer")
            fence = await self.locks.acquire_on(
                conn, target, HOLDER + request.id,
                ttl_seconds=request.ttl_seconds, role="integration",
            )
            now = self.clock()
            row = {key: binding[key] for key in (
                "id", "project_id", "repository_id", "task_id", "session_id", "claim_epoch",
                "target_branch", "expected_target_oid", "expected_owner_fence", "reason",
                "ttl_seconds",
            )}
            row.update(repository_url=repo["url"], source_branch=task["branch_name"],
                       session_instance_token=session["instance_token"], owner_fence=fence.token,
                       issued_by=by, created_at=now, expires_at=now + request.ttl_seconds,
                       revoked_at=None, revoked_by=None, revoke_reason=None,
                       published_at=None, published_oid=None)
            return await self._record(conn, row)

    async def revoke(self, request):
        by = await self._authority(request.project_id)
        async with asyncio.timeout(CRITICAL_SECTION_SECONDS), self.db.immediate() as conn:
            row = await self._grant(conn, request.id)
            if row is None or row["project_id"] != request.project_id:
                raise DelegationRefused("delegation not found in project")
            # Same target lock order as issue/publication. No claim is needed
            # to revoke an expired or reassigned attempt's remaining authority.
            await self.locks.lock_on(conn, self._target(row))
            row = await self._grant(conn, request.id, lock=True)
            if row is None:
                raise DelegationRefused("delegation was superseded by a later owner")
            if row["revoked_at"] is None and row["published_at"] is None:
                row = await self._record(conn, row, revoked_at=self.clock(), revoked_by=by,
                                         revoke_reason=request.reason)
                await self.locks.release(self._fence(row), conn=conn)
            return row

    async def _validate(self, conn, row, identity, branch, expected_oid, *, owner=None):
        if (row is None or row["project_id"] != identity.project_id
                or row["session_id"] != identity.session_id
                or identity.task_id not in (None, row["task_id"])
                or not matches_session_instance(identity, row["session_instance_token"])
                or branch != row["target_branch"]
                or expected_oid not in (None, row["expected_target_oid"])
                or row["revoked_at"] is not None or row["published_at"] is not None
                or row["expires_at"] <= self.clock()):
            raise DelegationRefused("publication delegation is stale, mismatched or revoked")
        _, task = await self._claim(conn, row)
        await self._repository(conn, row, task)
        if owner is None:
            owner = (await conn.execute(select(t.integration_branch_owners).where(
                t.integration_branch_owners.c.repository_id == row["repository_id"],
                t.integration_branch_owners.c.ref == row["target_branch"],
            ))).mappings().one_or_none()
        self.locks.require_current(owner, self._fence(row))

    async def validate(self, delegation_id, identity, branch, expected_oid=None):
        async with asyncio.timeout(CRITICAL_SECTION_SECONDS), self.db._engine.connect() as conn:
            row = await self._grant(conn, delegation_id)
            await self._validate(conn, row, identity, branch, expected_oid)
            return row

    async def publish(self, delegation_id, identity, branch, *, expected_oid=None,
                      git, checkout_path, policy, inherited_oids=(), event_bus=None):
        # Advisory dispatch validation never establishes authority for this write.
        async with asyncio.timeout(CRITICAL_SECTION_SECONDS), self.db.immediate() as conn:
            row = await self._grant(conn, delegation_id)
            if row is None:
                raise DelegationRefused("publication delegation not found")
            await self._claim(conn, row, lock=True)
            async with self.locks.exclusion(self._fence(row), conn=conn) as owner:
                row = await self._grant(conn, delegation_id, lock=True)
                await self._validate(conn, row, identity, branch, expected_oid, owner=owner)
                if self.active is None or not self.active():
                    raise DelegationRefused("publication delegation requires Git-first active mode")
                tip = await git.arev_parse(checkout_path, row["source_branch"])
                if not tip or await git.ais_ancestor(
                    checkout_path, row["expected_target_oid"], tip, strict=True,
                ) is not True:
                    raise DelegationRefused("delegated publication must fast-forward the issued target")
                oid = await git.apush_validated_delivery(
                    checkout_path, row["expected_target_oid"], tip, row["target_branch"],
                    expected_remote_oid=row["expected_target_oid"],
                    repository_url=row["repository_url"], event_bus=event_bus,
                    project_id=row["project_id"], identity_policy=policy,
                    inherited_oids=inherited_oids,
                )
                if oid != tip:
                    raise DelegationRefused("publication did not confirm the selected immutable tip")
                await self._record(conn, row, published_at=self.clock(), published_oid=oid)
                await self.locks.release(self._fence(row), conn=conn)
                return oid


DELEGATION_ERRORS = (DelegationRefused, BranchBusy, StaleFence, TimeoutError)
