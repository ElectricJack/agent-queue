"""Durable root engine ownership, fenced against every root mutation.

Shared repository guards span remote operations; transfers take an exclusive
guard. A separate nonblocking lock serializes actual main publishers. Row locks are
insufficient: an activation could otherwise commit between a preflight and its
push. Ownership is durable in subject rows; no process-local switch grants it.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import time
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
from typing import Any

from sqlalchemy import select, text, update

from src.database.tables import (
    integration_branch_owners,
    integration_candidate_ref_mutations,
    integration_promotion_intents,
    integration_repair_operations,
    integration_subjects,
)
from src.integration.models import BranchKey
from src.integration.ownership import BranchOwnership
from src.integration.subjects import (
    AdmissionPredicate,
    Decision,
    EjectArgs,
    Subject,
    SubjectEngine,
    SubjectKind,
)


class EngineRefused(RuntimeError):
    """A different engine owns the repository, or the visit became stale."""


_scope: ContextVar[tuple[Any, str, SubjectEngine, asyncio.Task | None, Any] | None] = ContextVar(
    "root_engine_scope", default=None
)
_admission: ContextVar[AdmissionPredicate | None] = ContextVar("root_admission", default=None)
_ejection: ContextVar[RootPolicyEjection | None] = ContextVar("root_policy_ejection", default=None)


@dataclass(frozen=True)
class RootPolicyEjection:
    """Exact committed decision, usable only inside its owning root operation."""

    subject: Subject
    decision: Decision
    journal_seq: int

    async def validate_on(self, db, conn, batch, *, task_id: str, reason: str) -> dict:
        request = self.decision.request
        if current_policy_ejection(db, batch["id"], task_id, reason) is not self:
            raise EngineRefused("policy ejection escaped its root operation")
        current = await db.lock_integration_subject_on(conn, self.subject.id)
        current_subject = Subject.from_row(current) if current is not None else None
        identity_fields = (
            "id", "version", "project_id", "repository_id", "kind", "engine", "phase",
            "policy", "batch_id", "target_ref", "head_sha", "base_sha", "generation",
        )
        if current is None or (
            current["engine"] != SubjectEngine.RECONCILER.value
            or any(
                getattr(current_subject, field) != getattr(self.subject, field)
                for field in identity_fields
            )
            or (batch["project_id"], batch["repository_id"], batch["current_revision"])
            != (self.subject.project_id, self.subject.repository_id, self.subject.generation)
        ):
            raise EngineRefused("policy ejection subject/batch identity changed")
        from src.database.tables import integration_subject_journal

        journal = (
            (
                await conn.execute(
                    select(integration_subject_journal).where(
                        integration_subject_journal.c.seq == self.journal_seq,
                        integration_subject_journal.c.subject_id == self.subject.id,
                    )
                )
            )
            .mappings()
            .first()
        )
        if journal is None or _ejection_decision(self.subject, request, journal) != self.decision:
            raise EngineRefused("policy ejection decision changed")
        return {
            "subject_id": self.subject.id,
            "subject_version": self.subject.version,
            "rule": self.decision.rule,
            "policy_artifact_sha256": self.subject.policy.artifact_sha256,
            "playbook_id": self.subject.policy.playbook_id,
            "decision_seq": self.journal_seq,
            "facts_digest": self.decision.facts_digest,
        }


def _ejection_decision(subject, request, journal) -> Decision:
    from pydantic import ValidationError

    try:
        decision = Decision.model_validate(journal["payload"].get("decision", {}))
    except ValidationError as exc:
        raise EngineRefused("invalid policy ejection decision") from exc
    if (
        not isinstance(request, EjectArgs)
        or decision.request != request
        or (decision.subject_id, decision.subject_version, decision.policy)
        != (subject.id, subject.version, subject.policy)
        or (
            journal["subject_id"], journal["subject_version"], journal["mode"],
            journal["entry_kind"], journal["primitive"], journal["rule"],
            journal["facts_digest"], journal["policy_artifact_sha256"],
            journal["head_sha"], journal["generation"], journal["phase"],
        ) != (
            subject.id, subject.version, "active", "decision", "eject", decision.rule,
            decision.facts_digest, subject.policy.artifact_sha256,
            subject.head_sha, subject.generation, subject.phase.value,
        )
    ):
        raise EngineRefused("policy ejection decision identity mismatch")
    return decision


@contextmanager
def root_policy_ejection(subject, request, journal):
    """Bind server-derived authority; request arguments cannot create this scope."""
    authority = RootPolicyEjection(
        subject, _ejection_decision(subject, request, journal), journal["seq"]
    )
    token = _ejection.set(authority)
    try:
        yield authority
    finally:
        _ejection.reset(token)


def current_policy_ejection(db, batch_id, task_id, reason) -> RootPolicyEjection | None:
    from src.commands.principal import PrincipalKind, current_principal

    authority, scope, principal = _ejection.get(), _scope.get(), current_principal()
    if (
        authority is None
        or scope is None
        or scope[0] is not db
        or scope[1] != authority.subject.repository_id
        or scope[2] is not SubjectEngine.RECONCILER
        or scope[3] is not asyncio.current_task()
        or principal is None
        or principal.kind is not PrincipalKind.SERVICE
        or principal.service_name != "root-reconciler"
        or authority.subject.batch_id != batch_id
        or not isinstance(authority.decision.request, EjectArgs)
        or authority.decision.request.member_task_id != task_id
        or authority.decision.request.reason != reason
    ):
        return None
    return authority


@contextmanager
def root_admission(predicate: AdmissionPredicate):
    token = _admission.set(predicate)
    try:
        yield
    finally:
        _admission.reset(token)


def current_admission() -> AdmissionPredicate | None:
    scope = _scope.get()
    if scope and scope[2] is SubjectEngine.RECONCILER and scope[3] is asyncio.current_task():
        return _admission.get()
    return None


def lock_key(repository_id: str, *, publisher: bool = False) -> int:
    return int.from_bytes(
        hashlib.sha256(
            (("aq-root-publisher:" if publisher else "aq-root-engine:") + repository_id).encode()
        ).digest()[:8],
        "big",
        signed=True,
    )


class RootEngineOwnership:
    def __init__(self, db, *, clock=time.time):
        self.db = db
        self.clock = clock

    @asynccontextmanager
    async def exclusion(self, repository_id: str):
        async with self.db.immediate() as conn:
            await conn.execute(
                text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key(repository_id)}
            )
            yield conn

    async def _publisher(self, conn, repository_id):
        if not await conn.scalar(
            text("SELECT pg_try_advisory_lock(:key)"),
            {"key": lock_key(repository_id, publisher=True)},
        ):
            raise EngineRefused("another repository publisher is active")

    @asynccontextmanager
    async def operation(
        self, repository_id: str, *, subject: Subject | None = None, publisher=False
    ):
        scope = _scope.get()
        if scope and scope[0] is self.db and scope[1] == repository_id:
            # Context is inherited by spawned tasks, but the exclusion is not.
            if scope[3] is not asyncio.current_task():
                raise EngineRefused("root mutation escaped its repository exclusion")
            if subject is not None:
                current = await self.db.get_integration_subject(subject.id)
                if (
                    scope[2] is not SubjectEngine.RECONCILER
                    or current is None
                    or current["engine"] != "reconciler"
                    or current["version"] != subject.version
                ):
                    raise EngineRefused("root subject engine/version changed")
            if publisher:
                await self._publisher(scope[4], repository_id)
            yield
            return
        # This dedicated session owns advisory locks across short domain
        # commits. Keep it in autocommit so network awaits never retain a
        # transaction or its snapshot. Process death releases session locks.
        async with self.db._engine.connect() as conn:
            conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
            try:
                # Cleanup also covers cancellation while acquisition returns:
                # the server may already have granted the session lock.
                await conn.execute(
                    text("SELECT pg_advisory_lock_shared(:key)"),
                    {"key": lock_key(repository_id)},
                )
                async with self._operation(conn, repository_id, subject, publisher):
                    yield
            finally:
                # Only this scope uses the dedicated connection. Nested
                # publishers may acquire its lock more than once.
                try:
                    await conn.execute(text("SELECT pg_advisory_unlock_all()"))
                except BaseException:
                    await conn.invalidate()
                    raise

    @asynccontextmanager
    async def _operation(self, conn, repository_id, subject, publisher):
        engine = SubjectEngine.RECONCILER if subject else SubjectEngine.LEGACY
        rows = (
            (
                await conn.execute(
                    select(integration_subjects).where(
                        integration_subjects.c.repository_id == repository_id,
                        integration_subjects.c.kind == SubjectKind.ROOT_BATCH.value,
                        integration_subjects.c.engine == SubjectEngine.RECONCILER.value,
                    )
                )
            )
            .mappings()
            .all()
        )
        if subject is None and rows:
            raise EngineRefused("repository root publisher belongs to the reconciler")
        if subject is not None:
            current = next((row for row in rows if row["id"] == subject.id), None)
            if current is None or current["version"] != subject.version:
                raise EngineRefused("root subject engine/version changed")
            if subject.kind is not SubjectKind.ROOT_BATCH or not subject.is_live:
                raise EngineRefused("not a live root subject")
        token = _scope.set((self.db, repository_id, engine, asyncio.current_task(), conn))
        try:
            if publisher:
                await self._publisher(conn, repository_id)
            yield
        finally:
            _scope.reset(token)

    async def transfer(
        self,
        repository_id: str,
        *,
        engine: SubjectEngine,
        expected_versions: dict[str, int],
        reason: str,
        evidence: tuple[str, ...] = (),
        operator_id: str | None = None,
        dry_run: bool = False,
    ) -> dict:
        """Transfer the entire repository's roots, with exact versions and audit.

        A cutover needs explicit operator evidence. Disabling the feature does
        not erase ownership: rollback is this same serialized transfer to legacy.
        Neither transfer touches Git nor removes any journal or reservation.
        """
        engine = SubjectEngine(engine)
        if not dry_run and (not reason.strip() or not expected_versions):
            raise EngineRefused("transfer needs a reason and exact subject versions")
        if not dry_run and engine is SubjectEngine.RECONCILER and not evidence:
            raise EngineRefused("cutover requires shadow/scenario/approval evidence")
        async with self.exclusion(repository_id) as conn:
            rows = (
                (
                    await conn.execute(
                        select(integration_subjects)
                        .where(
                            integration_subjects.c.repository_id == repository_id,
                            integration_subjects.c.kind == SubjectKind.ROOT_BATCH.value,
                        )
                        .with_for_update()
                    )
                )
                .mappings()
                .all()
            )
            if dry_run:
                return {
                    "outcome": "preview",
                    "repository_id": repository_id,
                    "engine": engine.value,
                    "subject_ids": sorted(row["id"] for row in rows),
                    "expected_versions": {row["id"]: row["version"] for row in rows},
                    "current_engines": {row["id"]: row["engine"] for row in rows},
                }
            if {row["id"]: row["version"] for row in rows} != expected_versions:
                raise EngineRefused("repository root subject set/version changed")
            # Development delivery uses the same remote main, with its journal
            # in operation events rather than root promotion-intent rows.
            from src.integration.development import operation_rows_on

            repository = await self.db.get_repo(repository_id)
            project_ids = {row["project_id"] for row in rows}
            if repository:
                project_ids.add(repository.project_id)
            development = await operation_rows_on(conn, sorted(project_ids))
            if any(
                row["repository_id"] == repository_id and row["state"] == "publishing"
                for row in development
            ):
                raise EngineRefused("unresolved development publication prevents engine transfer")
            # A prewrite can survive a daemon crash. Drain it under its original
            # owner before transferring; a timeout is never proof of no push.
            for table, predicate in (
                (
                    integration_promotion_intents,
                    integration_promotion_intents.c.state.not_in(
                        ["committed", "conflict", "superseded"]
                    ),
                ),
                (
                    integration_candidate_ref_mutations,
                    integration_candidate_ref_mutations.c.state == "reserved",
                ),
            ):
                pending = (
                    await conn.execute(
                        select(table.c.id)
                        .where(
                            table.c.repository_id == repository_id,
                            predicate,
                        )
                        .limit(1)
                    )
                ).first()
                if pending:
                    raise EngineRefused("unresolved remote write prevents engine transfer")
            if engine is SubjectEngine.RECONCILER:
                if repository is None:
                    raise EngineRefused("repository does not exist")
                ref = "refs/heads/" + repository.default_branch.removeprefix("refs/heads/")
                if any(row["target_ref"] not in {None, ref} for row in rows):
                    raise EngineRefused("root subject names another publication target")
                await BranchOwnership(self.db).acquire(
                    BranchKey(repository_id=repository_id, branch=ref),
                    "root-reconciler:" + repository_id,
                    "publisher",
                    conn=conn,
                )
            now = self.clock()
            for row in rows:
                subject = Subject.from_row(row)
                await self.db.append_integration_subject_journal_on(
                    conn,
                    {
                        "subject_id": subject.id,
                        "entry_kind": "action",
                        "idempotency_key": f"engine:{subject.version}:{engine.value}",
                        "visit_id": f"engine:{subject.version}",
                        "mode": "active",
                        "policy_artifact_sha256": subject.policy.artifact_sha256,
                        "subject_version": subject.version,
                        "phase": subject.phase.value,
                        "head_sha": subject.head_sha,
                        "generation": subject.generation,
                        "primitive": "record_decision",
                        "outcome": "recorded",
                        "payload": {
                            "from": subject.engine.value,
                            "to": engine.value,
                            "reason": reason,
                            "command": "integration_engine_transfer",
                            "evidence": list(evidence),
                            "operator_id": operator_id,
                        },
                        "recorded_at": now,
                    },
                )
                values = {"engine": engine.value}
                if subject.is_live and subject.schedule.gate_id is None:
                    values.update(next_due_at=now, due_set_at=now, wait_reason=None)
                changed = await self.db.update_integration_subject_on(
                    conn,
                    subject_id=subject.id,
                    expected_version=subject.version,
                    values=values,
                    now=now,
                )
                if changed is None:
                    raise EngineRefused("subject changed during engine transfer")
            # Keep the row and monotonic token for future activation. The lock
            # excludes an in-flight publisher before releasing its reservation.
            if engine is SubjectEngine.LEGACY:
                await conn.execute(
                    update(integration_branch_owners)
                    .where(
                        integration_branch_owners.c.repository_id == repository_id,
                        integration_branch_owners.c.owner_id == "root-reconciler:" + repository_id,
                        integration_branch_owners.c.owner_role == "publisher",
                        integration_branch_owners.c.handoff_state == "reserved",
                        integration_branch_owners.c.session_id.is_(None),
                        integration_branch_owners.c.workspace_id.is_(None),
                    )
                    .values(handoff_state="released", updated_at=now)
                )
        return {
            "outcome": "transferred",
            "repository_id": repository_id,
            "engine": engine.value,
            "subject_ids": sorted(expected_versions),
        }


def root_engine_guard(
    resource: str, *, outcome="wait", result_model=None, refusal=None, publisher=False,
    aborted_pr_cleanup=False,
):
    """Cover legacy service entry points, including autonomous/restart callers.

    An active adapter enters the exclusion before calling CommandHandler; its
    nested service uses that same authority. Caller arguments cannot grant it.
    Parent operations have their own ownership and are outside this root cutover.
    """

    def decorate(method):
        signature = inspect.signature(method)

        @wraps(method)
        async def guarded(self, *args, **kwargs):
            db = self.db
            # Small test/service doubles do not implement subject persistence.
            if not callable(getattr(type(db), "get_integration_subject", None)):
                return await method(self, *args, **kwargs)
            bound = signature.bind(self, *args, **kwargs).arguments
            identity = bound.get(resource + "_id")
            if resource == "candidate":
                identity = bound["row"]["batch_id"]
            batch = None
            if resource == "project":
                project = await db.get_project(identity)
                repository_id = project.integration_repository_id if project else None
            else:
                if resource in {"operation", "intent"}:
                    table = (
                        integration_repair_operations
                        if resource == "operation"
                        else integration_promotion_intents
                    )
                    async with db._engine.connect() as conn:
                        row = (
                            (await conn.execute(select(table).where(table.c.id == identity)))
                            .mappings()
                            .first()
                        )
                    identity = row.get("batch_id", row.get("root_batch_id")) if row else None
                if identity:
                    batch = await db.get_integration_batch(identity)
                repository_id = batch["repository_id"] if batch else None
            if repository_id is None:
                return await method(self, *args, **kwargs)
            # Retiring a terminal batch's audit PR does not mutate source refs,
            # candidate construction or publication. A repository's new root
            # owner must not strand this independent, immutable cleanup work.
            if (aborted_pr_cleanup and batch is not None and batch["lifecycle"] == "aborted"
                    and bound.get("kind", "audit_pr") == "audit_pr"):
                return await method(self, *args, **kwargs)
            try:
                async with RootEngineOwnership(db).operation(repository_id, publisher=publisher):
                    return await method(self, *args, **kwargs)
            except EngineRefused as exc:
                values = {"outcome": outcome, **(refusal or {})}
                if result_model:
                    fields = result_model.model_fields
                    values.update({key: value for key, value in bound.items() if key in fields})
                    values["batch_id"] = identity
                    if "revision" in fields:
                        values["revision"] = bound.get(
                            "revision", bound.get("expected_revision", batch["current_revision"])
                        )
                    if "reason" in fields:
                        values["reason"] = str(exc)
                    if "attempts" in fields:
                        values["attempts"] = 0
                    return result_model(**values)
                values["reason"] = str(exc)
                if resource == "project":
                    values.update(
                        project_id=bound["project_id"],
                        request_id=bound.get("request_id"),
                        batch_id=None,
                    )
                return values

        return guarded

    return decorate
