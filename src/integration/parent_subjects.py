"""Additive parent episode bridge and read-only receipt/verification facts.

No cutover is performed here. Command owners call ``ensure_on`` in their
transaction; the future parent visit uses ``ParentIntegrationObserver``.
Receipt eligibility remains owned by ``ParentEpisodeRecords.readiness_on``.
Reopening failed verification remains owned by keen-stone-14's recovery.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import replace
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text

from src.database import tables as t
from src.integration.failed_verification_recovery import FAILED_AGGREGATE_META_KEY, RECOVERY_EVENT
from src.integration.observe import (
    DatabaseObservationReader,
    IntegrationObserver,
    ObservationRows,
    _latest,
    _operation,
    _ref,
)
from src.integration.records import ParentEpisodeRecords
from src.integration.owner_guards import active_parent_scope, parent_lock_key
from src.playbooks.integration_policy import CompiledIntegrationPolicy
from src.integration.runtime_contracts import (
    SHA_PATTERN,
    HoldFacts,
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


class ParentDatabaseObservationReader(DatabaseObservationReader):
    """One consistent read-only snapshot, including legacy readiness proofs."""

    async def augment_on(self, conn, snapshot: ObservationRows) -> ObservationRows:
        subject = snapshot.subject
        if subject.kind is not SubjectKind.PARENT_EPISODE:
            return snapshot
        if subject.parent_episode_id is None:
            return snapshot
        rows = dict(snapshot.rows)

        async def keep(table, *conditions):
            result = await conn.execute(select(table).where(*conditions))
            rows[table.name] = tuple(dict(row) for row in result.mappings())
            return rows[table.name]

        await keep(
            t.task_delivery_receipts,
            t.task_delivery_receipts.c.target_task_id == subject.task_id,
            t.task_delivery_receipts.c.repository_id == subject.repository_id,
            t.task_delivery_receipts.c.target_branch
            == subject.target_ref.removeprefix("refs/heads/"),
        )
        verifications = await keep(
            t.integration_parent_verifications,
            t.integration_parent_verifications.c.parent_task_id == subject.task_id,
        )
        await keep(
            t.integration_parent_operation_completions,
            t.integration_parent_operation_completions.c.parent_task_id == subject.task_id,
        )
        await keep(
            t.integration_parent_verification_evidence,
            t.integration_parent_verification_evidence.c.verification_id.in_(
                [row["id"] for row in verifications]
            ),
        )
        await keep(
            t.integration_episode_receipt_acceptances,
            t.integration_episode_receipt_acceptances.c.episode_id == subject.parent_episode_id,
        )
        recoveries = await keep(
            t.events,
            t.events.c.task_id == subject.task_id,
            t.events.c.event_type == RECOVERY_EVENT,
        )
        verifier_ids = {
            row["verifier_task_id"]
            for row in snapshot.all("integration_repair_operations")
            if row.get("verifier_task_id")
        }
        for event in recoveries:
            payload = json.loads(event["payload"])
            if payload.get("previous_verifier_task_id"):
                verifier_ids.add(payload["previous_verifier_task_id"])
        await keep(t.task_completion_records, t.task_completion_records.c.task_id.in_(verifier_ids))
        await keep(
            t.archived_tasks,
            t.archived_tasks.c.id.in_(verifier_ids),
            t.archived_tasks.c.project_id == subject.project_id,
        )
        rows["sessions"] = tuple(
            dict(row)
            for row in (
                await conn.execute(
                    select(t.sessions).where(
                        t.sessions.c.project_id == subject.project_id,
                        t.sessions.c.task_id.in_(verifier_ids),
                    )
                )
            ).mappings()
        ) + snapshot.all("sessions")
        task_ids = {subject.task_id, *verifier_ids} | {
            row["id"]
            for row in snapshot.all("tasks")
            if row.get("parent_task_id") == subject.task_id
        }
        links = await keep(t.task_gates, t.task_gates.c.task_id.in_(task_ids))
        gate_ids = {row["gate_id"] for row in links} | {subject.schedule.gate_id}
        await keep(t.gates, t.gates.c.id.in_(gate_ids), t.gates.c.project_id == subject.project_id)
        snapshot = replace(snapshot, rows=rows)
        checkpoint = next(
            (
                row
                for row in snapshot.all("task_integration_checkpoints")
                if row["task_id"] == subject.task_id
            ),
            None,
        )
        operation = _operation(snapshot)
        parent = next((row for row in snapshot.all("tasks") if row["id"] == subject.task_id), None)
        if (
            checkpoint
            and operation
            and parent
            and checkpoint["episode_id"] == subject.parent_episode_id
        ):
            rows["parent_readiness"] = (
                await ParentEpisodeRecords(self.db).readiness_on(
                    conn,
                    parent=dict(parent),
                    project=dict(snapshot.project),
                    checkpoint=dict(checkpoint),
                    operation=dict(operation),
                ),
            )
        return replace(snapshot, rows=rows)


class _SnapshotReader:
    def __init__(self, snapshot):
        self.snapshot = snapshot

    async def read(self, subject_id):
        return self.snapshot if subject_id == self.snapshot.subject.id else None


class ParentIntegrationObserver(IntegrationObserver):
    """Compose shared facts and parent facts from exactly one database snapshot."""

    def __init__(
        self,
        reader,
        git=None,
        *,
        clock=time.time,
        session_probe=None,
        facts_type: type[ParentSubjectFacts] = ParentSubjectFacts,
    ):
        if not issubclass(facts_type, ParentSubjectFacts):
            raise TypeError("parent facts_type must extend ParentSubjectFacts")
        super().__init__(
            reader, git, clock=clock, session_probe=session_probe, facts_type=facts_type
        )

    async def observe_subject(self, subject_id: str, *, include_remote: bool = True):
        snapshot = await self.reader.read(subject_id)
        if snapshot is None:
            return None
        facts = await IntegrationObserver(
            _SnapshotReader(snapshot),
            self.git,
            clock=self.clock,
            session_probe=self.session_probe,
            facts_type=self.facts_type,
        ).observe_subject(subject_id, include_remote=include_remote)
        if snapshot.subject.kind is not SubjectKind.PARENT_EPISODE:
            return facts
        return self.facts_type.model_validate(
            {**facts.model_dump(), **parent_observation(snapshot, facts)}
        )


def parent_observation(snapshot: ObservationRows, facts: SubjectFacts) -> dict[str, Any]:
    """Project legacy identities without rewriting a receipt or inferring success."""
    subject = snapshot.subject
    unknown = list(facts.unknown)
    episode = next(
        (
            row
            for row in snapshot.all("integration_parent_episodes")
            if row["id"] == subject.parent_episode_id
        ),
        None,
    )
    operation = _operation(snapshot) if episode else None
    if not episode or not operation:
        return {"unknown": tuple(sorted({*unknown, "parent_episode_binding_missing"}))}
    checkpoint = next(
        (
            row
            for row in snapshot.all("task_integration_checkpoints")
            if row["task_id"] == subject.task_id
        ),
        {},
    )
    current = checkpoint.get("episode_id") == episode["id"]
    readiness = next(iter(snapshot.all("parent_readiness")), {}) if current else {}
    collection_head = (
        readiness.get("head_sha", checkpoint.get("checkpoint_sha")) if current else None
    )
    # The trusted receipt chain may advance before readiness projects its head
    # into the checkpoint. Once the visit CAS adopts that aggregate, the old
    # checkpoint must not make every later observation stale forever.
    exact = (
        current
        and checkpoint.get("generation") == subject.generation
        and collection_head == subject.head_sha
    )
    if not current:
        unknown.append("parent_episode_superseded")
    elif not exact and (
        checkpoint.get("generation") != subject.generation
        or checkpoint.get("checkpoint_sha") != subject.head_sha
    ):
        unknown.append("parent_checkpoint_moved")
    if current and readiness and collection_head != subject.head_sha:
        unknown.append("parent_collection_head_moved")
        exact = False
    selected = {row["id"] for row in readiness.get("receipts", ())} if exact else set()
    acceptances = {
        row["receipt_id"]: row
        for row in snapshot.all("integration_episode_receipt_acceptances")
        if row["episode_id"] == episode["id"] and row["operation_id"] == operation["id"]
    }
    receipts = []
    for row in sorted(
        snapshot.all("task_delivery_receipts"), key=lambda r: (r["created_at"], r["id"])
    ):
        if (
            row.get("target_task_id") != subject.task_id
            or row.get("repository_id") != subject.repository_id
            or _ref(row["target_branch"]) != subject.target_ref
        ):
            continue
        direct = (
            row["parent_episode_id"] == episode["id"]
            and row["parent_operation_id"] == operation["id"]
        )
        acceptance = acceptances.get(row["id"])
        # A carried receipt is selected only by the existing full ancestry and
        # completion proof; a raw acceptance row is never proof on its own.
        carried = acceptance is not None and row["id"] in selected
        receipts.append(
            ParentReceiptFacts(
                receipt_id=row["id"],
                source_task_id=row["source_task_id"],
                episode_id=row["parent_episode_id"],
                operation_id=row["parent_operation_id"],
                kind=row["disposition"],
                source_head_sha=row.get("reviewed_head_sha"),
                before_sha=row.get("before_sha"),
                target_head_sha=row.get("after_sha"),
                disposition_revision=row.get("disposition_revision"),
                created_at=row["created_at"],
                scope="direct" if direct else "carried" if carried else "history",
                selected=row["id"] in selected,
                acceptance=dict(acceptance) if acceptance else None,
            )
        )
    children = []
    for member in facts.members:
        task = next((row for row in snapshot.all("tasks") if row["id"] == member.task_id), {})
        relevant = [row for row in receipts if row.source_task_id == member.task_id]
        chosen = next((row for row in relevant if row.selected), None)
        disposition = next(
            (
                row
                for row in snapshot.all("integration_child_dispositions")
                if row["child_task_id"] == member.task_id
                and row["parent_episode_id"] == episode["id"]
                and row["parent_operation_id"] == operation["id"]
            ),
            {},
        )
        children.append(
            ParentChildFacts(
                task_id=member.task_id,
                status=task.get("status", "unknown"),
                head_sha=member.head_sha,
                receipt_ids=tuple(row.receipt_id for row in relevant),
                selected_receipt_id=chosen.receipt_id if chosen else None,
                disposition=disposition.get("disposition"),
                disposition_revision=disposition.get("revision"),
                pending_collection=current
                and task.get("status") == "COMPLETED"
                and member.head_sha is not None
                and chosen is None
                and not member.ejected,
                blockers=tuple(
                    sorted(
                        row["reason"]
                        for row in readiness.get("blockers", ())
                        if row["task_id"] == member.task_id
                    )
                ),
            )
        )
    verifications = []
    verification = None
    recovery_payloads = [
        json.loads(row["payload"])
        for row in snapshot.all("events")
        if row.get("event_type") == RECOVERY_EVENT
    ]
    for row in sorted(
        snapshot.all("integration_parent_verifications"), key=lambda r: (r["created_at"], r["id"])
    ):
        evidence_ids = tuple(
            sorted(
                link["evidence_id"]
                for link in snapshot.all("integration_parent_verification_evidence")
                if link["verification_id"] == row["id"]
            )
        )
        completed = any(
            completion["verification_id"] == row["id"]
            and completion["operation_id"] == row["operation_id"]
            and completion["episode_id"] == row["episode_id"]
            for completion in snapshot.all("integration_parent_operation_completions")
        )
        same = row["episode_id"] == episode["id"] and row["operation_id"] == operation["id"]
        active = (
            same
            and exact
            and row["id"] == checkpoint.get("current_verification_id")
            and (
                row["head_sha"] == subject.head_sha
                and row["generation"] == subject.generation
                and row["required_check_version"] == operation["required_check_version"]
            )
        )
        recovery = next(
            (
                payload
                for payload in recovery_payloads
                if payload.get("episode_id") == row["episode_id"]
                and payload.get("operation_id") == row["operation_id"]
                and payload.get("head_sha") == row["head_sha"]
                and payload.get("previous_generation") == row["generation"]
            ),
            {},
        )
        verifier_id = (
            operation.get("verifier_task_id")
            if active
            else recovery.get("previous_verifier_task_id")
        )
        verifier = next(
            (
                task
                for task in (*snapshot.all("tasks"), *snapshot.all("archived_tasks"))
                if task["id"] == verifier_id
            ),
            {},
        )
        completion = _latest(
            (
                c
                for c in snapshot.all("task_completion_records")
                if c["task_id"] == verifier_id
                and c.get("branch")
                and _ref(c["branch"]) == subject.target_ref
                and c["completed_at"] >= row["created_at"]
                and row["head_sha"] in _commits(c)
            ),
            "completed_at",
        )
        status = "passed" if completed else "pending" if active else "stale"
        if completion and completion["outcome"] == "fail" and (active or recovery):
            status = "failed"
        elif active and verifier_id and not verifier:
            status = "unknown"
            unknown.append("verifier_missing:" + verifier_id)
        item = ParentVerificationFacts(
            verification_id=row["id"],
            episode_id=row["episode_id"],
            operation_id=row["operation_id"],
            generation=row["generation"],
            head_sha=row["head_sha"],
            required_check_version=row["required_check_version"],
            verifier_task_id=verifier_id,
            verifier_status=verifier.get("status"),
            status=status,
            completion_id=completion["id"] if completion else None,
            evidence_ids=evidence_ids,
        )
        verifications.append(item)
        if active:
            verification = item
    # Legacy failed verification may precede a durable verification row.
    # Its exact failed completion and the recovery audit remain observable.
    failure_sources = (
        [(operation.get("verifier_task_id"), subject.head_sha, subject.generation, None)]
        if exact
        else []
    )
    failure_sources += [
        (
            payload.get("previous_verifier_task_id"),
            payload.get("head_sha"),
            payload.get("previous_generation"),
            payload.get("failure_completion_id"),
        )
        for payload in recovery_payloads
        if payload.get("episode_id") == episode["id"]
        and payload.get("operation_id") == operation["id"]
    ]
    for verifier_id, head, generation, completion_id in failure_sources:
        if not verifier_id or not head or generation is None:
            continue
        failed = _latest(
            (
                row
                for row in snapshot.all("task_completion_records")
                if row["task_id"] == verifier_id
                and row["outcome"] == "fail"
                and (completion_id is None or row["id"] == completion_id)
                and row.get("branch")
                and _ref(row["branch"]) == subject.target_ref
                and row["completed_at"] >= episode["created_at"]
                and head in _commits(row)
            ),
            "completed_at",
        )
        if not failed or any(item.completion_id == failed["id"] for item in verifications):
            continue
        item = ParentVerificationFacts(
            episode_id=episode["id"],
            operation_id=operation["id"],
            generation=generation,
            head_sha=head,
            required_check_version=operation["required_check_version"],
            verifier_task_id=verifier_id,
            status="failed",
            completion_id=failed["id"],
        )
        verifications.append(item)
        if exact and head == subject.head_sha and generation == subject.generation:
            verification = item
    marker = next(
        (
            row["value"]
            for row in snapshot.all("task_metadata")
            if row["task_id"] == subject.task_id and row["key"] == FAILED_AGGREGATE_META_KEY
        ),
        None,
    )
    failure_head = None
    if marker:
        try:
            value = json.loads(marker)
            if value["episode_id"] == episode["id"] and value["operation_id"] == operation["id"]:
                failure_head = value["head_sha"]
        except (ValueError, TypeError, KeyError):
            unknown.append("failed_aggregate_marker_invalid")
    holds = list(facts.holds)
    for gate in snapshot.all("gates"):
        if gate["status"] == "open" or gate.get("resolution") in {"hold", "reject", "abort"}:
            holds.append(HoldFacts(kind="operator_hold", reason="gate:" + gate["id"]))
    if operation["state"] == "human_required":
        holds.append(HoldFacts(kind="operator_hold", reason="parent_operation_human_required"))
    return {
        "parent_episode_id": episode["id"],
        "parent_operation_id": operation["id"],
        "collection_state": checkpoint.get("state") if current else None,
        "collection_generation": checkpoint.get("generation") if current else None,
        "collection_head_sha": collection_head,
        "collection_checkpoint_head_sha": checkpoint.get("checkpoint_sha") if current else None,
        "collection_current": current,
        "collection_reopened": current
        and (
            failure_head is not None
            or any(
                payload.get("episode_id") == episode["id"]
                and payload.get("operation_id") == operation["id"]
                for payload in recovery_payloads
            )
        ),
        "collection_generation_advanced": current
        and checkpoint.get("generation", 0) > episode["generation"],
        "readiness": readiness.get("outcome", "unknown") if exact else "unknown",
        "children": tuple(children),
        "receipts": tuple(receipts),
        "verification": verification,
        "verification_history": tuple(verifications),
        "failed_aggregate_head_sha": failure_head,
        "failed_aggregate_unchanged": failure_head is not None and failure_head == collection_head,
        "holds": tuple(sorted(set(holds), key=lambda h: (h.kind, h.task_id or "", h.reason))),
        "unknown": tuple(sorted(set(unknown))),
    }


def _commits(completion) -> list[str]:
    try:
        value = json.loads(completion["commits"])
        return value if isinstance(value, list) else []
    except (ValueError, TypeError, KeyError):
        return []


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
