"""Exact, human-authorized supersession of a retained completion's Subject.

This is an operator control, not a reconciler policy. It cannot merge, approve
gates, cancel a batch or settle delegates. The normal obsolete close owns the
task transition and the subsequent fenced branch retirement.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass

from sqlalchemy import or_, select, text, update

from src.commands.principal import PrincipalKind, TRUSTED_LOCAL, current_principal
from src.commands.supervisor_authority import integration_operator
from src.database import tables as t
from src.integration.delegate_release import live_integration_owner
from src.integration.engine import lock_key
from src.integration.owner_guards import parent_lock_key
from src.integration.provenance import CompletionIdentity
from src.integration.runtime_contracts import Subject, SubjectSchedule, schedule_values
from src.integration.train import TrainTarget
from src.integration.train_sources import project_snapshot


def _digest(value):
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode()).hexdigest()


def _ref(branch):
    return "refs/heads/" + (branch or "").removeprefix("refs/heads/")


@dataclass(frozen=True)
class SupersessionSettlement:
    """A transaction-local receipt for abandoned managed-parent settlement.

    Cannot be supplied through a command argument or reused on another
    connection/task. Ordinary completion still needs verification authority.
    """

    connection: object
    task_id: str
    subject_id: str
    subject_version: int
    journal_seq: int
    proof_sha256: str

    async def valid_on(self, conn, task_id):
        if self.connection is not conn or self.task_id != task_id:
            return False
        subject = (await conn.execute(select(t.integration_subjects).where(
            t.integration_subjects.c.id == self.subject_id,
        ))).mappings().one_or_none()
        journal = (await conn.execute(select(t.integration_subject_journal).where(
            t.integration_subject_journal.c.seq == self.journal_seq,
            t.integration_subject_journal.c.subject_id == self.subject_id,
        ))).mappings().one_or_none()
        metadata = dict((await conn.execute(select(t.task_metadata.c.key,
            t.task_metadata.c.value).where(t.task_metadata.c.task_id == task_id))).all())
        if subject is None or journal is None or "obsolete" not in metadata:
            return False
        payload = journal["payload"]
        marker = json.loads(metadata["obsolete"])
        return (subject["phase"] == "done" and subject["task_id"] == task_id
                and subject["version"] == self.subject_version
                and journal["entry_kind"] == "action" and journal["outcome"] == "recorded"
                and journal["primitive"] == "record_decision"
                and payload.get("proof_sha256") == self.proof_sha256
                and _digest(payload.get("proof")) == self.proof_sha256
                and payload["proof"]["request"]["task_id"] == task_id
                and marker.get("disposition", "obsolete") == "obsolete"
                and marker["reason"] == payload["proof"]["request"]["reason"]
                and json.loads(metadata.get("work_outcome", "null")) == "abandoned")


class _Replay(Exception):
    def __init__(self, result):
        self.result = result


class SupersededCompletion:
    def __init__(self, db, *, closer, confirm_stopped):
        self.db = db
        self.closer = closer
        self.confirm_stopped = confirm_stopped

    async def run(self, request, *, principal):
        label, refusal = await integration_operator(self.db, request.project_id)
        if refusal or label != principal:
            raise ValueError(refusal or "supersession principal changed")
        async with self.db._engine.connect() as conn:
            await self._authority_on(conn, request)
            replay = await self._replay_on(conn, request)
            database = None if replay else await self._capture_on(conn, request)
        if replay is not None:
            if not request.dry_run:
                replay["cleanup"] = await self.closer.cleanup(request.task_id, principal=principal)
            return replay
        retained = await self._retained(request, database)
        await self._stopped(database)
        proof = {"request": self._identity(request), "principal": principal,
                 "database": database, "retained": retained}
        digest = _digest(proof)
        result = {"outcome": "preview", "task_id": request.task_id,
                  "subject_id": request.subject_id, "proof": proof,
                  "proof_sha256": digest, "dry_run": request.dry_run}
        if request.dry_run:
            return result
        if digest != request.expected_proof_sha256:
            raise ValueError("supersession proof changed; preview the exact identities again")

        async def prepare(conn):
            # Try, never wait behind an operation performing a remote write.
            for key in (lock_key(database["subject"]["repository_id"]),
                        parent_lock_key(request.task_id)):
                if not await conn.scalar(text("SELECT pg_try_advisory_xact_lock(:key)"),
                                         {"key": key}):
                    raise ValueError("exact integration writer operation is active; retry later")
            await self.db.lock_hierarchy_project(conn, request.project_id)
            await self._authority_on(conn, request)
            replay = await self._replay_on(conn, request, lock=True)
            if replay is not None:
                raise _Replay(replay)
            current = await self._capture_on(conn, request, lock=True)
            if current != database:
                raise ValueError("supersession identity changed during proof; preview again")
            await self._stopped(current)
            if await self._retained(request, current) != retained:
                raise ValueError("retained source changed during proof; preview again")
            now = time.time()
            subject = Subject.from_row(current["subject"])
            values = {"phase": "done", **schedule_values(SubjectSchedule.close(
                now=now, reason="superseded completion: " + request.reason,
                max_wait_seconds=subject.schedule.max_wait_seconds,
            )), "refusal_streak": subject.schedule.refusal_streak}
            if subject.writer.session_id:
                values.update(writer_status="stopped", writer_stop_proof={
                    "kind": "superseded_completion", "proof_sha256": digest,
                    "session_id": subject.writer.session_id,
                    "instance_token": current["sessions"][subject.writer.session_id][
                        "instance_token"],
                })
            updated = await self.db.update_integration_subject_on(
                conn, subject_id=subject.id, expected_version=request.expected_version,
                values=values, now=now,
            )
            if updated is None:
                raise ValueError("subject version changed during supersession")
            operation = current["operation"]
            if operation and operation["state"] not in ("completed", "cancelled"):
                cancelled = await conn.execute(update(t.integration_repair_operations).where(
                    t.integration_repair_operations.c.id == operation["id"],
                    t.integration_repair_operations.c.episode_id == subject.parent_episode_id,
                    t.integration_repair_operations.c.state == operation["state"],
                    t.integration_repair_operations.c.updated_at == operation["updated_at"],
                ).values(state="cancelled", updated_at=now))
                if cancelled.rowcount != 1:
                    raise ValueError("exact parent operation changed during supersession")
                await conn.execute(update(t.integration_repair_stages).where(
                    t.integration_repair_stages.c.operation_id == operation["id"],
                    t.integration_repair_stages.c.state.in_(("pending", "active")),
                    t.integration_repair_stages.c.repair_task_id.is_(None),
                ).values(state="cancelled", completed_at=now))
            journal, _ = await self.db.append_integration_subject_journal_on(conn, {
                "subject_id": subject.id, "entry_kind": "action",
                "idempotency_key": self._key(request), "visit_id": self._key(request),
                "mode": "active", "policy_artifact_sha256": subject.policy.artifact_sha256,
                "subject_version": subject.version, "phase": subject.phase.value,
                "head_sha": subject.head_sha, "generation": subject.generation,
                "primitive": "record_decision", "outcome": "recorded",
                "payload": {"proof": proof, "proof_sha256": digest}, "recorded_at": now,
            })
            result.update(outcome="superseded", journal_seq=journal["seq"])
            return SupersessionSettlement(
                conn, request.task_id, subject.id, updated["version"], journal["seq"], digest,
            )

        try:
            closed = await self.closer.close(
                request.task_id, reason=request.reason, principal=principal, _prepare_on=prepare,
            )
        except _Replay as replay:
            if not request.dry_run:
                replay.result["cleanup"] = await self.closer.cleanup(
                    request.task_id, principal=principal,
                )
            return replay.result
        result["cleanup"] = closed["cleanup"]
        return result

    @staticmethod
    def _identity(request):
        return request.model_dump(exclude={"dry_run", "expected_proof_sha256"})

    @staticmethod
    def _key(request):
        return f"supersede-completion:{request.expected_version}"

    async def _authority_on(self, conn, request):
        principal = current_principal() or TRUSTED_LOCAL
        if principal.kind is PrincipalKind.LOCAL:
            return
        # This control deliberately admits only operators and named supervisors;
        # setup-worker grants do not authorize interpreting human supersession.
        row = await self._one(conn, t.sessions, t.sessions.c.id == principal.session_id, True)
        if (principal.kind is not PrincipalKind.SESSION or not principal.elevated
                or row is None or row["profile_id"] != "supervisor"
                or row["lifecycle"] != "named" or row["state"] not in (
                    "starting", "running", "draining") or row["desired_state"] != "running"
                or row["project_id"] not in (None, request.project_id)
                or principal.project_id not in (None, request.project_id)
                or (principal.session_instance_token is not None
                    and row["instance_token"] != principal.session_instance_token)):
            raise ValueError("a current in-scope named supervisor or local operator is required")

    @staticmethod
    async def _one(conn, table, where, lock=False):
        statement = select(table).where(where)
        if lock:
            statement = statement.with_for_update()
        row = (await conn.execute(statement)).mappings().one_or_none()
        return dict(row) if row else None

    @staticmethod
    async def _many(conn, table, where, lock=False):
        statement = select(table).where(where).order_by(*table.primary_key.columns)
        if lock:
            statement = statement.with_for_update()
        return [dict(row) for row in (await conn.execute(statement)).mappings()]

    async def _capture_on(self, conn, request, lock=False):
        await self._authority_on(conn, request)
        project = await self._one(conn, t.projects, t.projects.c.id == request.project_id, lock)
        subject = await self._one(conn, t.integration_subjects,
                                  t.integration_subjects.c.id == request.subject_id, lock)
        if (project is None or project["hierarchical_integration_mode"] != "train"
                or subject is None or subject["project_id"] != request.project_id
                or subject["repository_id"] != project["integration_repository_id"]
                or subject["task_id"] != request.task_id
                or subject["kind"] not in ("source", "parent_episode")
                or subject["version"] != request.expected_version
                or subject["phase"] == "done" or subject["batch_id"]
                or subject["head_sha"] != request.expected_head_sha):
            raise ValueError("exact task/Subject/repository/head/version proof changed")
        task = await self._one(conn, t.tasks, t.tasks.c.id == request.task_id, lock)
        repository = await self._one(conn, t.repos, t.repos.c.id == subject["repository_id"], lock)
        if repository is None or repository["project_id"] != request.project_id:
            raise ValueError("source repository belongs to another project or is missing")
        if (task is None or task["project_id"] != request.project_id or task["parent_task_id"]
                or task["repo_id"] != subject["repository_id"]
                or task["claim_epoch"] != request.expected_claim_epoch
                or task["status"] not in ("PAUSED", "BLOCKED", "COMPLETED")
                or task["assigned_agent_id"] or _ref(task["branch_name"]) != subject["target_ref"]):
            raise ValueError("exact root must be explicitly paused or settled with its source branch")
        completions = await self._many(conn, t.task_completion_records,
                                      t.task_completion_records.c.task_id == request.task_id, lock)
        latest = max((row["completed_at"] for row in completions), default=None)
        newest = [row for row in completions if row["completed_at"] == latest]
        if len(newest) != 1:
            raise ValueError("latest stored completion is missing or ambiguous")
        completion = newest[0]
        commits = json.loads(completion["commits"])
        if (completion["id"] != request.completion_id or completion["outcome"] != "pass"
                or completion["work_outcome"] not in (None, "changed")
                or _ref(completion["branch"]) != subject["target_ref"]
                or not commits or commits[-1] != request.expected_head_sha):
            raise ValueError("exact retained passing completion/source does not match")
        reply = await self._one(conn, t.escalation_messages,
                               t.escalation_messages.c.id == request.human_reply_id, lock)
        escalation = await self._one(conn, t.escalations, t.escalations.c.id == (
            reply["escalation_id"] if reply else ""), lock)
        if (reply is None or escalation is None or reply["direction"] != "inbound"
                or not reply["verified_actor"].startswith("human:")
                or not reply["supervisor_message_id"]
                or escalation["project_id"] != request.project_id
                or escalation["task_id"] != request.task_id):
            raise ValueError("original verified human reply for this task's escalation is required")
        checkpoint = await self._one(conn, t.task_integration_checkpoints,
                                    t.task_integration_checkpoints.c.task_id == request.task_id, lock)
        episode = operation = None
        stages = []
        if subject["kind"] == "parent_episode":
            episode = await self._one(conn, t.integration_parent_episodes,
                                     t.integration_parent_episodes.c.id == subject[
                                         "parent_episode_id"], lock)
            if (checkpoint is None or episode is None
                    or episode["parent_task_id"] != request.task_id
                    or episode["repository_id"] != subject["repository_id"]
                    or checkpoint["episode_id"] != episode["id"]
                    or checkpoint["repository_id"] != subject["repository_id"]
                    or _ref(checkpoint["branch"]) != subject["target_ref"]
                    or checkpoint["generation"] != subject["generation"]
                    or checkpoint["checkpoint_sha"] != request.expected_head_sha):
                raise ValueError("current parent checkpoint/episode/source proof changed")
            operation = await self._one(conn, t.integration_repair_operations,
                                       (t.integration_repair_operations.c.parent_task_id
                                        == request.task_id)
                                       & (t.integration_repair_operations.c.episode_id
                                          == episode["id"]), lock)
            if operation is None or operation["episode_id"] != episode["id"]:
                raise ValueError("exact current parent operation is required")
            stages = await self._many(conn, t.integration_repair_stages,
                                     t.integration_repair_stages.c.operation_id == operation["id"],
                                     lock)
            if operation["verifier_task_id"] or any(row["repair_task_id"] for row in stages):
                raise ValueError("operation has delegates; settle them through their owner first")
        elif checkpoint is not None:
            raise ValueError("source has a parent checkpoint; use its exact parent Subject")
        owner = await live_integration_owner(conn, [request.task_id])
        if owner and (operation is None or owner["operation_id"] != operation["id"]
                      or owner["role"] != "parent"):
            raise ValueError("another live integration operation owns this task")
        metadata = await self._many(conn, t.task_metadata,
                                   t.task_metadata.c.task_id == request.task_id, lock)
        if any(row["key"] == "obsolete" for row in metadata):
            raise ValueError("task already has another obsolete disposition")
        origins = await self._many(conn, t.task_branch_origins,
                                  t.task_branch_origins.c.task_id == request.task_id, lock)
        current_origins = [row for row in origins if row["retired_at"] is None]
        if (len(current_origins) != 1 or not current_origins[0]["materialized"]
                or current_origins[0]["repository_id"] != subject["repository_id"]
                or _ref(current_origins[0]["branch_name"]) != subject["target_ref"]):
            raise ValueError("exact materialized source origin is required")
        owners = await self._many(conn, t.integration_branch_owners, or_(
            t.integration_branch_owners.c.owner_id == request.task_id,
            (t.integration_branch_owners.c.repository_id == subject["repository_id"])
            & (t.integration_branch_owners.c.ref.in_((subject["target_ref"],
                subject["target_ref"].removeprefix("refs/heads/")))),
            t.integration_branch_owners.c.owner_id == (operation["id"] if operation else ""),
        ), lock)
        canonical = [row for row in owners if row["repository_id"] == subject["repository_id"]
                     and _ref(row["ref"]) == subject["target_ref"]]
        if (len(canonical) != 1 or canonical[0]["owner_id"] != request.task_id
                or canonical[0]["owner_role"] not in ("worker", "repair")
                or canonical[0]["handoff_state"] not in (
                    "reserved", "attached", "handoff_pending")):
            raise ValueError("exact retained source branch owner is required")
        if checkpoint and checkpoint["branch_owner_id"] != canonical[0]["id"]:
            raise ValueError("parent checkpoint no longer owns this exact source branch")
        if any(row["handoff_state"] != "released" and (
            row["owner_id"] != request.task_id or row["owner_role"] not in ("worker", "repair")
            or row["repository_id"] != subject["repository_id"]
        ) for row in owners):
            raise ValueError("unrelated or collector owner remains; use owning recovery first")
        workspace_ids = {row["workspace_id"] for row in owners if row["workspace_id"]}
        workspaces = await self._many(conn, t.workspaces, or_(
            t.workspaces.c.id.in_(workspace_ids),
            t.workspaces.c.locked_by_task_id == request.task_id,
        ), lock)
        if not workspace_ids.issubset({row["id"] for row in workspaces}):
            raise ValueError("retained writer checkout is missing")
        if any(row["locked_by_task_id"] not in (None, request.task_id) for row in workspaces):
            raise ValueError("retained checkout belongs to another task")
        session_ids = {row["session_id"] for row in owners if row["session_id"]}
        if subject["writer_status"] != "none":
            if (subject["writer_status"] == "unknown"
                    or subject["writer_task_id"] != request.task_id
                    or not subject["writer_session_id"]
                    or canonical[0]["fence_token"] != subject["writer_fence_token"]
                    or canonical[0]["session_id"] != subject["writer_session_id"]):
                raise ValueError("exact Subject writer/session/owner fence proof is required")
            session_ids.add(subject["writer_session_id"])
        for row in metadata:
            if row["key"] == "claimed_by_session":
                holder = json.loads(row["value"])
                if not isinstance(holder, str) or not holder:
                    raise ValueError("claim holder identity is unknown")
                session_ids.add(holder)
        sessions = await self._many(conn, t.sessions, or_(
            t.sessions.c.id.in_(session_ids), t.sessions.c.task_id == request.task_id,
        ), lock)
        if not session_ids.issubset({row["id"] for row in sessions}):
            raise ValueError("attached writer session is missing")
        for row in owners:
            if row["handoff_state"] in ("attached", "handoff_pending") and (
                not row["session_id"] or not row["workspace_id"]):
                raise ValueError("attached owner lacks exact writer or checkout proof")
            if row["handoff_state"] == "reserved" and (
                row["holder"] or row["session_id"] or row["workspace_id"]):
                raise ValueError("reserved owner has unexplained writer attachment")
        blockers = (
            select(t.integration_subjects.c.id).where(
                t.integration_subjects.c.id != subject["id"],
                t.integration_subjects.c.phase != "done", or_(
                    t.integration_subjects.c.task_id == request.task_id,
                    t.integration_subjects.c.writer_task_id == request.task_id,
                    (t.integration_subjects.c.repository_id == subject["repository_id"])
                    & (t.integration_subjects.c.target_ref == subject["target_ref"]))),
            select(t.gates.c.id).join(t.task_gates, t.task_gates.c.gate_id == t.gates.c.id).where(
                t.task_gates.c.task_id == request.task_id, t.gates.c.status == "open"),
            select(t.integration_candidate_ref_mutations.c.id).where(
                t.integration_candidate_ref_mutations.c.repository_id == subject["repository_id"],
                t.integration_candidate_ref_mutations.c.state == "reserved"),
            select(t.integration_repair_operations.c.id).where(
                t.integration_repair_operations.c.parent_task_id == request.task_id,
                t.integration_repair_operations.c.id != (operation["id"] if operation else ""),
                t.integration_repair_operations.c.state.not_in(("completed", "cancelled"))),
            select(t.integration_promotion_intents.c.id).where(
                t.integration_promotion_intents.c.repository_id == subject["repository_id"],
                t.integration_promotion_intents.c.state.not_in(("committed", "conflict",
                                                               "superseded")),
                or_(t.integration_promotion_intents.c.source_task_id == request.task_id,
                    t.integration_promotion_intents.c.target_task_id == request.task_id)),
            select(t.integration_cleanup_items.c.domain_key).where(
                t.integration_cleanup_items.c.repository_id == subject["repository_id"],
                t.integration_cleanup_items.c.target_ref.in_((subject["target_ref"],
                    subject["target_ref"].removeprefix("refs/heads/"))),
                t.integration_cleanup_items.c.state.not_in(("complete", "conflict", "failed"))),
        )
        if subject["gate_id"]:
            raise ValueError("Subject has a gate; supersession cannot waive it")
        for statement in blockers:
            if await conn.scalar(statement.limit(1)):
                raise ValueError("unrelated active Subject, human gate or ref mutation remains")
        return {"project": {key: project[key] for key in (
                    "id", "integration_repository_id", "hierarchical_integration_mode",
                    "hierarchical_integration_generation")},
                "task": task, "subject": subject, "completion": completion,
                "repository": repository,
                "reply": reply, "escalation": {key: escalation[key] for key in (
                    "id", "project_id", "task_id")},
                "checkpoint": checkpoint, "episode": episode, "operation": operation,
                "stages": stages, "metadata": metadata, "origins": origins, "owners": owners,
                "workspaces": workspaces,
                "sessions": {row["id"]: {key: row[key] for key in (
                    "id", "name", "provider", "instance_token", "state", "desired_state",
                    "task_id", "last_claim_epoch", "claim_phase")} for row in sessions}}

    async def _stopped(self, database):
        for session in database["sessions"].values():
            if (session["state"] != "stopped" or session["desired_state"] != "stopped"
                    or not session["instance_token"] or not session["name"]
                    or self.confirm_stopped is None):
                raise ValueError("writer is live or unknown; stop the exact session first")
            try:
                stopped = await self.confirm_stopped(session)
            except Exception as exc:
                raise ValueError("exact writer provider stop proof is unavailable") from exc
            if stopped is not True:
                raise ValueError("exact writer provider cannot confirm it stopped")

    async def _retained(self, request, database):
        subject = database["subject"]
        identity = CompletionIdentity(request.project_id, subject["repository_id"],
                                      request.task_id, request.completion_id)
        snapshot = await project_snapshot(self.db, TrainTarget(
            project_id=request.project_id, repository_id=subject["repository_id"],
            target_ref=subject["target_ref"],
        ))
        if (snapshot is None or snapshot.cached_only or snapshot.observation.error
                or snapshot.target_oid != request.expected_head_sha):
            raise ValueError("fresh exact remote source retention proof is unavailable or changed")
        record = await snapshot.read_completion(identity)
        if (record is None or record["source_oid"] != request.expected_head_sha
                or record["artifact"] is not True):
            raise ValueError("original immutable completion provenance is missing or changed")
        return {"target_ref": subject["target_ref"], "source_oid": snapshot.target_oid,
                "completion": record, "refs": {ref: snapshot.observation.source_heads[ref]
                    for ref in identity.read_refs if ref in snapshot.observation.source_heads}}

    async def _replay_on(self, conn, request, lock=False):
        journal = await self._one(conn, t.integration_subject_journal,
                                 (t.integration_subject_journal.c.subject_id == request.subject_id)
                                 & (t.integration_subject_journal.c.idempotency_key
                                    == self._key(request)), lock)
        if journal is None:
            return None
        payload = journal["payload"]
        if (payload.get("proof", {}).get("request") != self._identity(request)
                or payload.get("proof_sha256") != request.expected_proof_sha256):
            raise ValueError("supersession receipt belongs to different proof or request")
        subject = await self._one(conn, t.integration_subjects,
                                  t.integration_subjects.c.id == request.subject_id, lock)
        task = await self._one(conn, t.tasks, t.tasks.c.id == request.task_id, lock)
        marker = await self._one(conn, t.task_metadata,
                                (t.task_metadata.c.task_id == request.task_id)
                                & (t.task_metadata.c.key == "obsolete"), lock)
        completion = await self._one(conn, t.task_completion_records,
                                    t.task_completion_records.c.id == request.completion_id, lock)
        completions = await self._many(conn, t.task_completion_records,
                                      t.task_completion_records.c.task_id == request.task_id, lock)
        outcome = await self._one(conn, t.task_metadata,
                                 (t.task_metadata.c.task_id == request.task_id)
                                 & (t.task_metadata.c.key == "work_outcome"), lock)
        original = payload["proof"]["database"]
        if (subject is None or subject["phase"] != "done"
                or subject["version"] != request.expected_version + 1
                or subject["head_sha"] != request.expected_head_sha
                or any(subject[key] != original["subject"][key] for key in (
                    "project_id", "repository_id", "task_id", "parent_episode_id",
                    "kind", "generation", "target_ref", "base_sha", "policy_artifact_sha256"))
                or completion != original["completion"]
                or [row["id"] for row in completions if row["completed_at"] >= original[
                    "completion"]["completed_at"]] != [request.completion_id]
                or outcome is None or json.loads(outcome["value"]) != "abandoned"
                or task is None or task["status"] != "COMPLETED" or marker is None
                or task["project_id"] != request.project_id
                or task["branch_name"] != original["task"]["branch_name"]
                or task["claim_epoch"] != request.expected_claim_epoch + (
                    original["task"]["status"] != "COMPLETED")
                or json.loads(marker["value"])["reason"] != request.reason
                or json.loads(marker["value"]).get("disposition", "obsolete") != "obsolete"):
            raise ValueError("supersession receipt no longer matches current settlement")
        return {"outcome": "already_superseded", "task_id": request.task_id,
                "subject_id": request.subject_id, "proof_sha256": payload["proof_sha256"],
                "journal_seq": journal["seq"], "dry_run": request.dry_run}
