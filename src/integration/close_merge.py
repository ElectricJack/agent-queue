"""Author-owned merge candidates; only the publisher transports parent writes.

Operation rows describe retained work, not delivery. Every resume observes Git,
current policy and the live claim again, including after an uncertain push.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert

from src.database.tables import integration_close_attempts as attempts
from src.git.manager import GitError
from src.waits.sources import record_source_event


@dataclass(frozen=True)
class ClosePolicy:
    enabled: bool
    author_assist: bool = True
    author_budget_seconds: float = 1200
    digest: str = ""
    required_checks: dict | None = None


class CloseStore:
    def __init__(self, db, clock=time.time):
        self.db, self.clock = db, clock

    async def get(self, operation_id):
        async with self.db._engine.connect() as conn:
            row = (await conn.execute(select(attempts).where(
                attempts.c.id == operation_id))).mappings().first()
        return dict(row) if row else None

    async def proposal(self, *, task, session, source, parent, args):
        key = dict(task_id=task.id, session_id=session.id, claim_epoch=task.claim_epoch,
                   source_oid=source, target_ref="refs/heads/" + parent.branch_name.removeprefix(
                       "refs/heads/"))
        identity = str(uuid.uuid4())
        now = self.clock()
        async with self.db.immediate() as conn:
            await conn.execute(insert(attempts).values(
                **key, id=identity, project_id=task.project_id, repository_id=task.repo_id,
                target_task_id=parent.id, candidate_ref="refs/heads/aq/close/" + identity,
                state="preparing", version=0, attempts=0, evidence={}, close_args=args,
                created_at=now, updated_at=now,
            ).on_conflict_do_nothing(constraint="uq_integration_close_generation"))
            row = (await conn.execute(select(attempts).where(
                *(getattr(attempts.c, k) == v for k, v in key.items())))).mappings().one()
        return dict(row)

    async def retire_previous(self, task_id, session_id, claim_epoch, source=None):
        """A changed source or disabled train settles superseded budget waits."""
        async with self.db._engine.connect() as conn:
            rows = (await conn.execute(select(attempts).where(
                attempts.c.task_id == task_id, attempts.c.session_id == session_id,
                attempts.c.claim_epoch == claim_epoch,
                attempts.c.state.not_in(["finished", "deferred", "cancelled"]),
            ))).mappings().all()
        for row in rows:
            if row["source_oid"] != source:
                await self.save(row, state="cancelled", last_failure="source_or_policy_changed")

    async def save(self, row, **changes):
        async with self.db.immediate() as conn:
            saved = (await conn.execute(update(attempts).where(
                attempts.c.id == row["id"], attempts.c.version == row["version"],
            ).values(**changes, version=row["version"] + 1, updated_at=self.clock())
                .returning(attempts))).mappings().first()
            if saved is None:
                raise GitError("close operation changed; re-observe the same operation")
            await record_source_event(
                conn, source_key="close_merge:" + row["id"], generation=0,
                revision=str(saved["version"]), outcome_ref="close_merge:" + row["id"],
                payload={"state": saved["state"], "project_id": row["project_id"]},
            )
        return dict(saved)

    @asynccontextmanager
    async def visit(self, operation_id):
        # One visit mutates this candidate; no target lease during editing or CI.
        async with self.db._engine.connect() as conn:
            key = int.from_bytes(hashlib.sha256(operation_id.encode()).digest()[:8], "big",
                                 signed=True)
            acquired = await conn.scalar(select(func.pg_try_advisory_lock(key)))
            if not acquired:
                raise GitError("close operation is already being continued")
            try:
                yield
            finally:
                await conn.execute(select(func.pg_advisory_unlock(key)))


class CloseMerge:
    """Ports keep Git, current policy, exact checks and publisher authority explicit."""
    def __init__(self, store, ports):
        self.store, self.ports = store, ports

    async def run(self, operation_id):
        async with self.store.visit(operation_id):
            row = await self.store.get(operation_id)
            if row is None:
                raise GitError("unknown close operation")
            for _ in range(4):
                result = await self._visit(row)
                if result["reason"] not in {"parent_or_policy_changed", "publication_unconfirmed"}:
                    return result
                row = await self.store.get(operation_id)
            return await self.defer(row, "publication_retry_limit")

    async def _visit(self, row):
        p = self.ports
        await p.authorize(row)
        if row["state"] == "cancelled":
            raise GitError("close proposal was superseded; close the current source")
        policy = await p.policy(row)
        if not policy.enabled or not policy.author_assist or row["state"] == "deferred":
            reason = "train_disabled" if not policy.enabled else "publisher_handoff"
            return await self.defer(row, reason)
        # A finished DB row cannot prove a current remote contains the source.
        repo, parent = await p.observe(row)
        if await p.contains(repo, row["source_oid"], parent):
            row = await self.store.save(row, state="finished", last_failure=None)
            return self.result(row, completed=True)
        if row["base_oid"] and row["base_oid"] != parent:
            if not await p.contains(repo, row["base_oid"], parent):
                return await self.defer(row, "parent_rewritten")
            if row["attempts"] >= 3:
                return await self.defer(row, "parent_movement_limit")
            # Preserve committed author fixes and unfinished work. Never reset
            # a checkout with an unresolved merge to a sibling's newer parent.
            if row["candidate_path"]:
                head = await p.resolved(row, repo)
                if head is None:
                    return await self.author(row, policy, "resolve_retained_candidate")
                row = await self.store.save(row, candidate_oid=head)
            row = await self.store.save(row, base_oid=parent, candidate_path=None,
                                        attempts=row["attempts"] + 1, evidence={},
                                        state="preparing")
        if row["base_oid"] is None:
            row = await self.store.save(row, base_oid=parent)
        if not row["candidate_path"]:
            row = await p.prepare(row, repo, self.store)
        head = await p.resolved(row, repo)
        if head is None:
            return await self.author(row, policy, "resolve_merge_and_commit")
        if not (await p.contains(repo, row["base_oid"], head)
                and await p.contains(repo, row["source_oid"], head)):
            return await self.author(row, policy, "candidate_must_retain_both_parents")
        row = await self.store.save(row, candidate_oid=head)
        # Every new committed candidate gets regeneration and fresh checks.
        if row["evidence"].get("regenerated_oid") != head:
            head = await p.regenerate(row, repo, head)
            row = await self.store.save(row, candidate_oid=head, evidence={"regenerated_oid": head})
        evidence = await p.check(row, repo, policy)
        row = await self.store.save(row, candidate_oid=head, policy_digest=policy.digest,
                                    evidence={**row["evidence"], **evidence}, state="checking")
        if evidence["state"] != "green":
            if evidence["state"] == "unavailable":
                return await self.defer(row, "candidate_checks_unavailable")
            if evidence["state"] == "failed":
                return await self.author(row, policy, "fix_candidate_checks")
            return self.result(row, completed=False, reason="candidate_checks_pending")

        async def authorize():
            await p.authorize(row)
            current = await p.policy(row)
            if not current.enabled or not current.author_assist or current.digest != policy.digest:
                return False  # Re-evaluate current requirements on the next visit.
            check = await p.check(row, repo, current)
            return check["state"] == "green"

        row = await self.store.save(row, state="publish_uncertain")
        try:
            await p.publish(repo, row["target_ref"], expected_old_oid=parent,
                            new_oid=head, authorize=authorize)
        except Exception as exc:
            # A lost push reply is resolved by readback, never by a duplicate merge.
            row = await self.store.save(row, last_failure=type(exc).__name__)
        try:
            repo, observed = await p.observe(row)
            if await p.contains(repo, row["source_oid"], observed):
                row = await self.store.save(row, state="finished", last_failure=None)
                return self.result(row, completed=True)
        except (GitError, OSError, TimeoutError):
            return self.result(row, completed=False, reason="publication_unconfirmed")
        return self.result(row, completed=False, reason="parent_or_policy_changed")

    async def author(self, row, policy, reason):
        row = await self.store.save(row, state="needs_author", last_failure=reason)
        if not row["budget_wait_id"]:
            wait_id = await self.ports.budget(row, policy)
            row = await self.store.save(row, budget_wait_id=wait_id)
        return self.result(row, completed=False, reason=reason)

    async def defer(self, row, reason):
        row = await self.store.save(row, state="deferred", last_failure=reason)
        await self.ports.handoff(row, enabled=(await self.ports.policy(row)).enabled)
        return self.result(row, completed=True, reason=reason)

    @staticmethod
    def result(row, *, completed, reason=None):
        resume = "aq integration continue-close --operation-id " + row["id"]
        if row["state"] == "needs_author":
            resume = f"Resolve and commit in {row['candidate_path']}; then {resume}"
        return dict(operation_id=row["id"], state=row["state"], source_oid=row["source_oid"],
                    candidate_oid=row["candidate_oid"], candidate_path=row["candidate_path"],
                    completed=completed, reason=reason, evidence=row["evidence"],
                    budget_wait_id=row["budget_wait_id"],
                    next_step=("source completed; parent delivery pending" if completed
                               and row["state"] == "deferred" else
                               resume))


def policy_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
