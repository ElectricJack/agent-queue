"""Exact-head CI adapters. The daemon injects its credentialled watcher reader.

The reader must prove the head exists on origin and the check/run exists.
Exact-head identities use stable generation 0; observations change revision.
No network request or transaction is opened here.
"""

import re

from sqlalchemy import or_, select

from src.database.tables import integration_batches as batches
from src.database.tables import integration_candidate_revisions as revisions
from src.database.tables import jobs, repos, tasks
from src.waits.adapters.base import Adapter, fields, foreign, invalid, row, string, unresolved
from src.waits.conditions import Condition, SourceKey


def sha(value):
    value = string(value).lower()
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value):
        invalid("an exact full head SHA is required")
    return value


class RepositoryAdapter(Adapter):
    def __init__(self, reader=None):
        self.reader = reader

    async def authorise(self, conn, cond, scope):
        repo = await row(conn, repos, repos.c.id == cond.parameters["repository_id"])
        if repo is None:
            unresolved()
        if repo["project_id"] != scope.project_id:
            foreign()
        return await super().authorise(conn, cond, scope)

    async def snapshot(self, conn, cond):
        repo = await row(conn, repos, repos.c.id == cond.parameters["repository_id"])
        if repo is None or repo["project_id"] != cond.context["project_id"]:
            return None
        if self.reader is None:
            unresolved("source watcher is unavailable", retry_after=30)
        try:
            snapshot = await self.reader(conn, cond)
        except (TimeoutError, ConnectionError):
            unresolved("source verification is temporarily unavailable", retry_after=30)
        if snapshot is None:
            return None
        if snapshot.generation != 0:
            return None
        facts = snapshot.observation.facts
        if facts.get("repository_id") != cond.parameters["repository_id"]:
            unresolved("watcher did not verify the requested repository")
        if not facts.get("exists_on_origin") and snapshot.observation.state != "unavailable":
            unresolved("watcher did not verify the head on origin", retry_after=30)
        return snapshot


class CIAdapter(RepositoryAdapter):
    def __init__(self, kind, reader=None):
        super().__init__(reader)
        self.kind = kind

    def normalise(self, raw):
        identity = "check_name" if self.kind == "ci_check" else "run_id"
        fields(
            raw, required={"repository_id", "head_sha", identity}, optional={"required_check_set"}
        )
        params = {
            "repository_id": string(raw["repository_id"]),
            "head_sha": sha(raw["head_sha"]),
            identity: string(raw[identity]),
        }
        if "required_check_set" in raw:
            params["required_check_set"] = string(raw["required_check_set"])
        return Condition(self.kind, params, raw.get("generation"))

    async def authorise(self, conn, cond, scope):
        cond = await super().authorise(conn, cond, scope)
        p = cond.parameters
        known_batch = await conn.scalar(
            select(batches.c.id)
            .where(
                batches.c.project_id == scope.project_id,
                batches.c.repository_id == p["repository_id"],
                or_(
                    batches.c.tested_candidate_sha == p["head_sha"],
                    batches.c.base_sha == p["head_sha"],
                    select(revisions.c.batch_id)
                    .where(
                        revisions.c.batch_id == batches.c.id,
                        revisions.c.head_sha == p["head_sha"],
                    )
                    .exists(),
                ),
            )
            .limit(1)
        )
        known_job = await conn.scalar(
            select(jobs.c.id)
            .select_from(jobs.join(tasks, jobs.c.task_id == tasks.c.id))
            .where(
                jobs.c.project_id == scope.project_id,
                tasks.c.repo_id == p["repository_id"],
                jobs.c.input_ref == p["head_sha"],
            )
            .limit(1)
        )
        if not known_batch and not known_job:
            unresolved("head is not known to a job or integration stage")
        return cond

    async def snapshot(self, conn, cond):
        snapshot = await super().snapshot(conn, cond)
        if snapshot:
            facts = snapshot.observation.facts
            identity = "check_name" if self.kind == "ci_check" else "run_id"
            if (
                facts.get("head_sha") != cond.parameters["head_sha"]
                or facts.get(identity) != cond.parameters[identity]
                or (not facts.get("identity_verified")
                    and snapshot.observation.state != "unavailable")
            ):
                unresolved("watcher did not verify the exact check identity")
            if (
                "required_check_set" in cond.parameters
                and facts.get("required_check_set") != cond.parameters["required_check_set"]
            ):
                unresolved("required-check set is not registered")
        return snapshot

    def source_key(self, cond):
        p = cond.parameters
        identity = p["check_name"] if self.kind == "ci_check" else p["run_id"]
        return SourceKey(self.kind, f"{p['repository_id']}:{p['head_sha']}:{identity}", 0)
