"""Fenced local operator transition to existing disabled integration semantics."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from contextlib import asynccontextmanager
from functools import wraps

from sqlalchemy import select, text, update

from src.database import tables as t
from src.integration.engine import RootEngineOwnership, lock_key
from src.integration.lock import PublishWithdrawn


def digest(plan):
    return hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(",", ":"))
                          .encode()).hexdigest()


@asynccontextmanager
async def enabled_train_write(db, repository_id):
    """Hold the shared side of off's fence through the bounded remote write.

    This guard belongs only to integration writers. Worker source publication
    remains governed by its own claim and branch authority.
    """
    async with db.immediate() as conn:
        await conn.execute(text("SELECT pg_advisory_xact_lock_shared(:key)"),
                           {"key": lock_key(repository_id)})
        enabled = await conn.scalar(select(t.projects.c.id).where(
            t.projects.c.integration_repository_id == repository_id,
            t.projects.c.status == "ACTIVE",
            t.projects.c.hierarchical_integration_mode.in_(("train", "hierarchy", "development")),
        ))
        if enabled is None:
            raise PublishWithdrawn("automatic integration is off")
        yield


def train_write(method):
    @wraps(method)
    async def fenced(self, repo, *args, **kwargs):
        async with enabled_train_write(self.locks.db, repo.repository_id):
            return await method(self, repo, *args, **kwargs)
    return fenced


def fence_disable(method):
    """Existing project configuration must serialize disabled mode with writers too."""
    @wraps(method)
    async def fenced(self, project_id, **kwargs):
        if kwargs.get("updates", {}).get("hierarchical_integration_mode") != "disabled":
            return await method(self, project_id, **kwargs)
        project = await self.db.get_project(project_id)
        if project is None or not project.integration_repository_id:
            return await method(self, project_id, **kwargs)
        async with asyncio.timeout(60):
            async with RootEngineOwnership(self.db).exclusion(project.integration_repository_id):
                return await method(self, project_id, **kwargs)
    return fenced


class ManualIntegration:
    def __init__(self, db, *, clock=time.time):
        self.db, self.clock = db, clock

    async def _binding_on(self, conn, project_id):
        await self.db.lock_hierarchy_project(conn, project_id)
        project = (await conn.execute(select(t.projects).where(t.projects.c.id == project_id)
                                     .with_for_update())).mappings().one_or_none()
        if project is None or not project["integration_repository_id"]:
            raise ValueError("project has no designated integration repository")
        repo = (await conn.execute(select(t.repos).where(
            t.repos.c.id == project["integration_repository_id"],
            t.repos.c.project_id == project_id,
        ).with_for_update())).mappings().one_or_none()
        if repo is None or not repo["url"] or not repo["default_branch"]:
            raise ValueError("designated repository binding is incomplete")
        return project, repo

    @staticmethod
    def _binding(project, repo):
        return {
            "project_id": project["id"], "repository_id": repo["id"],
            "repository_url": repo["url"],
            "target_ref": "refs/heads/" + repo["default_branch"].removeprefix("refs/heads/"),
            "generation": project["hierarchical_integration_generation"],
        }

    async def run(self, request, *, operator_id):
        project = await self.db.get_project(request.project_id)
        if project is None or not project.integration_repository_id:
            raise ValueError("project has no designated integration repository")
        # Same order as root engine transfers: repository, hierarchy, project.
        async with asyncio.timeout(60):
            async with RootEngineOwnership(self.db).exclusion(
                project.integration_repository_id
            ) as conn:
                current, repo = await self._binding_on(conn, request.project_id)
                if repo["id"] != project.integration_repository_id:
                    raise ValueError("repository changed; preview again")
                plan = {
                    **self._binding(current, repo), "action": "off", "reason": request.reason,
                    "effective_mode": current["hierarchical_integration_mode"],
                    "desired_mode": current["hierarchical_integration_desired_mode"],
                    "draining": current["hierarchical_integration_draining"],
                }
                result = self._result(request, plan, "disabled")
                if request.dry_run:
                    return result
                self._exact_plan(request, result["plan_digest"])
                now = self.clock()
                await conn.execute(update(t.projects).where(t.projects.c.id == request.project_id)
                                   .values(hierarchical_integration_mode="disabled",
                                           hierarchical_integration_desired_mode="disabled",
                                           hierarchical_integration_draining=False,
                                           hierarchical_integration_generation=plan["generation"] + 1))
                await conn.execute(update(t.project_integration_schedules).where(
                    t.project_integration_schedules.c.project_id == request.project_id,
                ).values(enabled=False, updated_at=now))
                await self._audit(conn, "integration.operator_off", result, operator_id)
                return result

    @staticmethod
    def _exact_plan(request, actual):
        if request.expected_plan_digest != actual:
            raise ValueError("plan changed or digest missing; preview again")

    @staticmethod
    def _result(request, plan, applied):
        plan = json.loads(json.dumps(plan))
        return {"outcome": "preview" if request.dry_run else applied,
                "project_id": request.project_id, "dry_run": request.dry_run,
                "plan": plan, "plan_digest": digest(plan)}

    async def _audit(self, conn, event, result, operator_id):
        await self.db.log_event(event, project_id=result["project_id"],
                                payload=json.dumps(result | {"principal": operator_id}), conn=conn)
