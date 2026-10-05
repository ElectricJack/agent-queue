"""Exclusive parent engine authority across short commits and remote actions."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import time
from contextlib import asynccontextmanager
from contextvars import ContextVar
from functools import wraps

from sqlalchemy import select, text

from src.database import tables as t
from src.integration.engine import EngineRefused
from src.integration.subjects import Subject, SubjectEngine, SubjectKind

_scope: ContextVar[tuple | None] = ContextVar("parent_engine_scope", default=None)
_entry_stage: ContextVar[tuple | None] = ContextVar("parent_engine_entry_stage", default=None)


def parent_stage_at_entry(db, operation_id):
    entry = _entry_stage.get()
    if entry and entry[:2] == (db, operation_id) and entry[3] is asyncio.current_task():
        return entry[2]
    return None


def parent_lock_key(task_id):
    return int.from_bytes(
        hashlib.sha256(("aq-parent-engine:" + task_id).encode()).digest()[:8], "big", signed=True
    )


def active_parent_scope(db, task_id):
    scope = _scope.get()
    return bool(
        scope and scope[:3] == (db, task_id, "reconciler") and scope[3] is asyncio.current_task()
    )


async def parent_checkpoint_allowed_on(conn, db, task_id):
    """For transactional lifecycle hooks which cannot open another transaction."""
    if active_parent_scope(db, task_id):
        return True
    await conn.execute(
        text("SELECT pg_advisory_xact_lock_shared(:key)"), {"key": parent_lock_key(task_id)}
    )
    rows = (
        (
            await conn.execute(
                select(t.integration_subjects).where(
                    t.integration_subjects.c.task_id == task_id,
                    t.integration_subjects.c.kind == "parent_episode",
                )
            )
        ).mappings().all()
    )
    return await _unowned_refusal_on(conn, rows) is None


async def _unowned_refusal_on(conn, rows):
    if rows:
        return "parent has a durable subject; reconciler authority is required"
    return None


class ParentEngineOwnership:
    def __init__(self, db, *, clock=time.time):
        self.db, self.clock = db, clock

    @asynccontextmanager
    async def operation(self, task_id, *, subject=None):
        scope = _scope.get()
        if scope and scope[:2] == (self.db, task_id):
            if scope[3] is not asyncio.current_task():
                raise EngineRefused("parent mutation escaped its engine exclusion")
            if subject and scope[2] != "reconciler":
                raise EngineRefused("parent mutation belongs to checkpoint bootstrap")
            yield
            return
        async with self.db._engine.connect() as conn:
            conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
            try:
                await conn.execute(
                    text("SELECT pg_advisory_lock_shared(:key)"), {"key": parent_lock_key(task_id)}
                )
                rows = (
                    (
                        await conn.execute(
                            select(t.integration_subjects).where(
                                t.integration_subjects.c.task_id == task_id,
                                t.integration_subjects.c.kind == "parent_episode",
                            )
                        )
                    )
                    .mappings()
                    .all()
                )
                if subject is None and (refusal := await _unowned_refusal_on(conn, rows)):
                    raise EngineRefused(refusal)
                if subject is not None and (
                    subject.kind is not SubjectKind.PARENT_EPISODE
                    or not subject.is_live
                    or not any(
                        row["id"] == subject.id
                        and row["version"] == subject.version
                        and row["engine"] == "reconciler"
                        for row in rows
                    )
                ):
                    raise EngineRefused("parent subject engine/version changed")
                token = _scope.set(
                    (
                        self.db,
                        task_id,
                        "reconciler" if subject else "bootstrap",
                        asyncio.current_task(),
                    )
                )
                try:
                    yield
                finally:
                    _scope.reset(token)
            finally:
                try:
                    await conn.execute(text("SELECT pg_advisory_unlock_all()"))
                except BaseException:
                    await conn.invalidate()
                    raise

    async def transfer(
        self,
        repository_id,
        *,
        task_id,
        engine,
        expected_versions,
        reason,
        evidence=(),
        operator_id=None,
        dry_run=False,
    ):
        engine = SubjectEngine(engine)
        if not dry_run and (
            not reason.strip()
            or not expected_versions
            or engine is SubjectEngine.RECONCILER
            and not evidence
        ):
            raise EngineRefused("transfer requires exact versions, reason and cutover evidence")
        async with self.db.immediate() as conn:
            await conn.execute(
                text("SELECT pg_advisory_xact_lock(:key)"), {"key": parent_lock_key(task_id)}
            )
            rows = (
                (
                    await conn.execute(
                        select(t.integration_subjects)
                        .where(
                            t.integration_subjects.c.repository_id == repository_id,
                            t.integration_subjects.c.task_id == task_id,
                            t.integration_subjects.c.kind == "parent_episode",
                        )
                        .with_for_update()
                    )
                )
                .mappings()
                .all()
            )
            versions = {row["id"]: row["version"] for row in rows}
            if dry_run:
                return {
                    "outcome": "preview",
                    "repository_id": repository_id,
                    "engine": engine.value,
                    "subject_ids": sorted(versions),
                    "expected_versions": versions,
                    "current_engines": {row["id"]: row["engine"] for row in rows},
                }
            if not rows or versions != expected_versions:
                raise EngineRefused("parent subject set/version changed")
            refs = {row["target_ref"].removeprefix("refs/heads/") for row in rows}
            for table, target, pending in (
                (
                    t.integration_promotion_intents,
                    t.integration_promotion_intents.c.target_branch,
                    t.integration_promotion_intents.c.state.not_in(
                        ["committed", "conflict", "superseded"]
                    ),
                ),
                (
                    t.integration_candidate_ref_mutations,
                    t.integration_candidate_ref_mutations.c.branch,
                    t.integration_candidate_ref_mutations.c.state == "reserved",
                ),
            ):
                if await conn.scalar(
                    select(table.c.id)
                    .where(
                        table.c.repository_id == repository_id,
                        target.in_(refs | {"refs/heads/" + ref for ref in refs}),
                        pending,
                    )
                    .limit(1)
                ):
                    raise EngineRefused("unresolved parent write prevents engine transfer")
            now = self.clock()
            for row in rows:
                subject = Subject.from_row({**row, "engine": "reconciler"})
                await self.db.append_integration_subject_journal_on(
                    conn,
                    {
                        "subject_id": subject.id,
                        "entry_kind": "action",
                        "mode": "active",
                        "idempotency_key": f"engine:{subject.version}:{engine.value}",
                        "policy_artifact_sha256": subject.policy.artifact_sha256,
                        "subject_version": subject.version,
                        "phase": subject.phase.value,
                        "head_sha": subject.head_sha,
                        "generation": subject.generation,
                        "primitive": "record_decision",
                        "outcome": "recorded",
                        "recorded_at": now,
                        "payload": {
                            "from": row["engine"],
                            "to": engine.value,
                            "reason": reason,
                            "evidence": list(evidence),
                            "operator_id": operator_id,
                            "command": "integration_engine_transfer",
                        },
                    },
                )
                values = {"engine": engine.value}
                if subject.is_live and not subject.schedule.gate_id:
                    values.update(next_due_at=now, due_set_at=now, wait_reason=None)
                if (
                    await self.db.update_integration_subject_on(
                        conn,
                        subject_id=subject.id,
                        expected_version=subject.version,
                        values=values,
                        now=now,
                    )
                    is None
                ):
                    raise EngineRefused("parent changed during transfer")
        return {
            "outcome": "transferred",
            "repository_id": repository_id,
            "engine": engine.value,
            "subject_ids": sorted(versions),
        }


def parent_engine_guard(resource="task", *, outcome="waiting", result_model=None, refusal=None):
    """Keep checkpoint bootstrap outside subject authority; nested ports reuse authority."""

    def decorate(method):
        signature = inspect.signature(method)

        @wraps(method)
        async def guarded(self, *args, **kwargs):
            db = self.db
            if not callable(getattr(type(db), "get_integration_subject", None)):
                return await method(self, *args, **kwargs)
            binding = signature.bind(self, *args, **kwargs)
            binding.apply_defaults()
            bound = binding.arguments
            if bound.get("dry_run") is True:
                return await method(self, *args, **kwargs)
            task_id = bound.get("task_id")
            row = None
            if resource in {"intent", "operation"}:
                table = (
                    t.integration_promotion_intents
                    if resource == "intent"
                    else t.integration_repair_operations
                )
                async with db._engine.connect() as conn:
                    row = (
                        (
                            await conn.execute(
                                select(table).where(
                                    table.c.id == bound[resource + "_id"],
                                )
                            )
                        )
                        .mappings()
                        .first()
                    )
                task_id = row.get("target_task_id", row.get("parent_task_id")) if row else None
            elif resource == "request":
                task = await db.get_task(bound["request"].source_task_id)
                task_id = task.parent_task_id if task else None
            elif resource == "row":
                task_id = bound["row"]["task_id"]
            elif resource == "command_target":
                target = bound["args"].get("target") or {}
                branch = str(target.get("branch") or "").removeprefix("refs/heads/")
                async with db._engine.connect() as conn:
                    task_id = await conn.scalar(
                        select(t.task_integration_checkpoints.c.task_id).where(
                            t.task_integration_checkpoints.c.repository_id
                            == target.get("repository_id"),
                            t.task_integration_checkpoints.c.branch.in_(
                                (branch, "refs/heads/" + branch)
                            ),
                            t.task_integration_checkpoints.c.episode_id.is_not(None),
                        )
                    )
            if not task_id:
                return await method(self, *args, **kwargs)
            try:
                async with ParentEngineOwnership(db).operation(task_id):
                    token = None
                    if resource == "operation" and row and row["target_kind"] == "parent":
                        token = _entry_stage.set(
                            (db, bound["operation_id"], row["active_stage"], asyncio.current_task())
                        )
                    try:
                        return await method(self, *args, **kwargs)
                    finally:
                        if token is not None:
                            _entry_stage.reset(token)
            except EngineRefused as exc:
                if result_model:
                    from src.integration.promotion import PromotionTargetMoved

                    raise PromotionTargetMoved(str(exc)) from exc
                result = {"outcome": outcome, "reason": str(exc), **(refusal or {})}
                if resource == "command_target":
                    result.update(success=False, error=str(exc))
                return result

        return guarded

    return decorate
