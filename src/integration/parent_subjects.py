"""Additive parent episode bridge and read-only receipt/verification facts.

No cutover is performed here. Command owners call ``ensure_on`` in their
transaction. The parent observer that read these facts was retired with the
parent runtime.
Receipt eligibility remains owned by ``ParentEpisodeRecords.readiness_on``.
Reopening failed verification remains owned by keen-stone-14's recovery.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text

from src.database import tables as t
from src.integration.observe import _ref
from src.integration.owner_guards import active_parent_scope, parent_lock_key
from src.playbooks.integration_policy import CompiledIntegrationPolicy
from src.integration.runtime_contracts import (
    SHA_PATTERN,
    PolicyArtifactPin,
    Subject,
    SubjectEngine,
    SubjectFacts,
    SubjectKind,
    SubjectSchedule,
    subject_key,
)


class _Facts(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ParentReceiptFacts(_Facts):
    """Original lineage, with acceptance recorded separately from that lineage."""

    receipt_id: str
    source_task_id: str | None
    episode_id: str | None
    operation_id: str | None
    kind: Literal["code", "noop", "skipped", "ineligible", "failed"]
    source_head_sha: str | None = Field(default=None, pattern=SHA_PATTERN)
    before_sha: str | None = Field(default=None, pattern=SHA_PATTERN)
    target_head_sha: str | None = Field(default=None, pattern=SHA_PATTERN)
    disposition_revision: int | None = None
    created_at: float
    scope: Literal["direct", "carried", "history"]
    selected: bool = False
    acceptance: dict[str, Any] | None = None


class ParentChildFacts(_Facts):
    task_id: str
    status: str
    head_sha: str | None = Field(default=None, pattern=SHA_PATTERN)
    receipt_ids: tuple[str, ...] = ()
    selected_receipt_id: str | None = None
    disposition: str | None = None
    disposition_revision: int | None = None
    pending_collection: bool = False
    blockers: tuple[str, ...] = ()


class ParentVerificationFacts(_Facts):
    verification_id: str | None = None
    episode_id: str
    operation_id: str
    generation: int = Field(ge=0)
    head_sha: str = Field(pattern=SHA_PATTERN)
    required_check_version: str | None = None
    verifier_task_id: str | None = None
    verifier_status: str | None = None
    status: Literal["none", "pending", "passed", "failed", "stale", "unknown"] = "none"
    completion_id: str | None = None
    evidence_ids: tuple[str, ...] = ()


class ParentSubjectFacts(SubjectFacts):
    """Shared subject facts plus parent lineage, ready for a decision binding."""

    parent_episode_id: str | None = None
    parent_operation_id: str | None = None
    collection_state: str | None = None
    collection_generation: int | None = None
    collection_head_sha: str | None = Field(default=None, pattern=SHA_PATTERN)
    collection_checkpoint_head_sha: str | None = Field(default=None, pattern=SHA_PATTERN)
    collection_current: bool = False
    collection_reopened: bool = False
    collection_generation_advanced: bool = False
    readiness: Literal["ready", "waiting", "failed", "unknown"] = "unknown"
    children: tuple[ParentChildFacts, ...] = ()
    receipts: tuple[ParentReceiptFacts, ...] = ()
    verification: ParentVerificationFacts | None = None
    verification_history: tuple[ParentVerificationFacts, ...] = ()
    failed_aggregate_head_sha: str | None = Field(default=None, pattern=SHA_PATTERN)
    failed_aggregate_unchanged: bool = False

    def binding(self) -> dict[str, Any]:
        value = super().binding()
        value["pending_child_count"] = sum(child.pending_collection for child in self.children)
        value["failed_child_count"] = sum(
            child.status in {"FAILED", "BLOCKED"} and child.selected_receipt_id is None
            for child in self.children
        )
        return value


def parent_subject_from_rows(
    parent,
    checkpoint,
    episode,
    operation,
    *,
    policy: PolicyArtifactPin,
    now: float,
    max_wait_seconds: int,
    engine: SubjectEngine = SubjectEngine.RECONCILER,
) -> Subject:
    """Snapshot a legacy episode without resetting any budget or failure evidence."""
    if (
        parent["id"] != checkpoint["task_id"]
        or checkpoint["episode_id"] != episode["id"]
        or parent["id"] != episode["parent_task_id"]
        or parent["repo_id"] != checkpoint["repository_id"]
        or checkpoint["repository_id"] != episode["repository_id"]
        or _ref(parent["branch_name"]) != _ref(checkpoint["branch"])
        or operation["parent_task_id"] != parent["id"]
        or operation["episode_id"] != episode["id"]
        or operation["target_kind"] != "parent"
        or checkpoint["generation"] < episode["generation"]
    ):
        raise ValueError("parent episode identity mismatch")
    phase = "testing" if checkpoint["state"] in {"verifying", "integration_ready"} else "building"
    if (
        checkpoint.get("verified_sha") == checkpoint["checkpoint_sha"]
        and checkpoint.get("verified_generation") == checkpoint["generation"]
        and checkpoint["checkpoint_sha"] is not None
    ):
        phase = "promotable"
    return Subject(
        id="parent-subject-" + uuid.uuid5(uuid.NAMESPACE_URL, episode["id"]).hex,
        project_id=parent["project_id"],
        repository_id=episode["repository_id"],
        kind=SubjectKind.PARENT_EPISODE,
        subject_key=subject_key(
            SubjectKind.PARENT_EPISODE,
            episode["repository_id"],
            parent["id"],
            episode["generation"],
        ),
        parent_episode_id=episode["id"],
        task_id=parent["id"],
        engine=engine,
        phase=phase,
        policy=policy,
        target_ref=_ref(checkpoint["branch"]),
        head_sha=checkpoint["checkpoint_sha"],
        base_sha=episode["pre_collection_checkpoint_sha"],
        generation=checkpoint["generation"],
        schedule=SubjectSchedule.progress(now=now, max_wait_seconds=max_wait_seconds),
        created_at=now,
        updated_at=now,
    )


class ParentSubjectAdapter:
    """Transaction-owned bridge; does not activate, reopen, file or push anything."""

    def __init__(self, db, *, clock: Callable[[], float] = time.time):
        self.db, self.clock = db, clock

    async def ensure_on(
        self,
        conn,
        task_id: str,
        *,
        policy: PolicyArtifactPin,
        max_wait_seconds: int,
        engine: SubjectEngine = SubjectEngine.RECONCILER,
    ) -> tuple[Subject, bool]:
        async def one(table, *conditions, lock=False):
            statement = select(table).where(*conditions)
            if lock:
                statement = statement.with_for_update()
            return (await conn.execute(statement)).mappings().one()

        parent = await one(t.tasks, t.tasks.c.id == task_id)
        checkpoint = await one(
            t.task_integration_checkpoints,
            t.task_integration_checkpoints.c.task_id == task_id,
            lock=True,
        )
        episode = await one(
            t.integration_parent_episodes,
            t.integration_parent_episodes.c.id == checkpoint["episode_id"],
        )
        operation = await one(
            t.integration_repair_operations,
            t.integration_repair_operations.c.parent_task_id == task_id,
            t.integration_repair_operations.c.episode_id == episode["id"],
        )
        subject = parent_subject_from_rows(
            parent,
            checkpoint,
            episode,
            operation,
            policy=policy,
            now=self.clock(),
            max_wait_seconds=max_wait_seconds,
            engine=engine,
        )
        row, created = await self.db.ensure_integration_subject_on(conn, subject.to_row())
        if row.get("parent_episode_id") != episode["id"]:
            raise ValueError("existing subject is not bound to this parent episode")
        return Subject.from_row(row), created


async def ensure_parent_subject_on(db, conn, task_id, loader, *, clock=time.time):
    """The command owner calls this after updating the checkpoint's episode."""
    if loader is None:
        return None
    if not active_parent_scope(db, task_id):
        await conn.execute(
            text("SELECT pg_advisory_xact_lock_shared(:key)"), {"key": parent_lock_key(task_id)}
        )
    await conn.execute(
        select(t.task_integration_checkpoints.c.task_id)
        .where(t.task_integration_checkpoints.c.task_id == task_id)
        .with_for_update()
    )
    operation = (
        (
            await conn.execute(
                select(t.integration_repair_operations)
                .join(
                    t.task_integration_checkpoints,
                    t.task_integration_checkpoints.c.episode_id
                    == t.integration_repair_operations.c.episode_id,
                )
                .where(
                    t.task_integration_checkpoints.c.task_id == task_id,
                    t.integration_repair_operations.c.parent_task_id == task_id,
                )
            )
        )
        .mappings()
        .first()
    )
    if operation is None:
        return None
    artifact = operation["artifact_snapshot"]
    try:
        definition = await asyncio.to_thread(loader, artifact["artifact_sha256"])
    except (OSError, ValueError) as exc:
        logging.getLogger(__name__).warning(
            "Parent %s awaits a pinned artifact: %s", task_id, exc
        )
        return None
    if (
        definition.integration_policy is None
        or SubjectKind.PARENT_EPISODE not in definition.integration_policy.tables
    ):
        return None
    policy = CompiledIntegrationPolicy(definition)
    pin = PolicyArtifactPin(
        playbook_id=artifact["playbook_id"], artifact_sha256=artifact["artifact_sha256"]
    )
    if policy.pin != pin:
        raise ValueError("parent artifact pin does not match loaded definition")
    return await ParentSubjectAdapter(db, clock=clock).ensure_on(
        conn,
        task_id,
        policy=pin,
        max_wait_seconds=policy.policy.max_wait_seconds,
        engine=SubjectEngine.RECONCILER,
    )
