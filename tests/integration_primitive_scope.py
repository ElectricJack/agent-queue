"""Give retained root domain unit tests the durable authority their caller now owns.

These tests exercise one primitive at a time. Runtime and adapter tests separately
prove that policy decisions enter this scope with the exact current subject. This
fixture uses the real ownership guard and never replaces its lock or task checks.
"""

import inspect
from contextlib import asynccontextmanager
from functools import wraps

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from src.database import tables as t
from src.integration.engine import RootEngineOwnership
from src.integration.runtime_contracts import (
    PolicyArtifactPin,
    Subject,
    SubjectKind,
    SubjectPhase,
    SubjectSchedule,
)


@asynccontextmanager
async def primitive_scope(db, repository_id, *, batch=None, request_id=None):
    async with db._engine.connect() as conn:
        repository = (
            (await conn.execute(select(t.repos).where(t.repos.c.id == repository_id)))
            .mappings()
            .one()
        )
        existing_subject = (
            (
                await conn.execute(
                    select(t.integration_subjects).where(
                        t.integration_subjects.c.batch_id == batch["id"],
                        t.integration_subjects.c.kind == "root_batch",
                    )
                )
            ).mappings().first()
            if batch else None
        )
        artifact = (
            (
                await conn.execute(
                    select(t.playbook_artifacts)
                    .order_by(t.playbook_artifacts.c.created_at)
                    .limit(1)
                )
            )
            .mappings()
            .first()
        )
    if existing_subject is not None:
        subject = Subject.from_row(existing_subject)
        async with RootEngineOwnership(db).operation(repository_id, subject=subject):
            yield
        return
    if artifact is None:
        artifact = {
            "artifact_sha256": "sha256:" + "f" * 64,
            "playbook_id": "root-primitive-test",
            "source_digest": "sha256:" + "e" * 64,
            "contract_fingerprint": "sha256:" + "d" * 64,
            "compiler_build": "test",
            "path": "/test/root-primitive.json",
            "created_at": 1.0,
        }
        async with db.immediate() as conn:
            await conn.execute(
                insert(t.playbook_artifacts).values(**artifact).on_conflict_do_nothing()
            )
    key = (
        "root_batch:"
        + repository_id
        + ":"
        + (batch["request_id"] if batch else request_id or "primitive-test")
    )
    subject = Subject(
        id="primitive:" + key,
        project_id=repository["project_id"],
        repository_id=repository_id,
        kind=SubjectKind.ROOT_BATCH,
        subject_key=key,
        phase=SubjectPhase.BUILDING,
        policy=PolicyArtifactPin(
            playbook_id=artifact["playbook_id"],
            artifact_sha256=artifact["artifact_sha256"],
        ),
        batch_id=batch["id"] if batch else None,
        target_ref="refs/heads/"
        + repository["default_branch"].removeprefix("refs/heads/"),
        generation=batch["current_revision"] if batch else 0,
        schedule=SubjectSchedule.progress(now=1, max_wait_seconds=3600),
        created_at=1,
        updated_at=1,
    )
    row, _ = await db.ensure_integration_subject(subject.to_row())
    subject = Subject.from_row(row)
    async with RootEngineOwnership(db).operation(repository_id, subject=subject):
        yield


def authorize_root_primitives(monkeypatch):
    from src.integration import (
        attestation,
        candidate_ci,
        candidates,
        cleanup,
        main_promotion,
        repair,
        scheduler,
    )

    modules = (
        attestation,
        candidate_ci,
        candidates,
        cleanup,
        main_promotion,
        repair,
        scheduler,
    )
    for module in modules:
        for cls in vars(module).values():
            if not isinstance(cls, type) or cls.__module__ != module.__name__:
                continue
            for name, method in tuple(vars(cls).items()):
                if not inspect.iscoroutinefunction(method):
                    continue
                source = inspect.getsource(method)
                if "@root_engine_guard(" not in source:
                    continue
                monkeypatch.setattr(cls, name, _authorized(method))


def _authorized(method):
    signature = inspect.signature(method)

    @wraps(method)
    async def invoke(service, *args, **kwargs):
        db = service.db
        if not callable(getattr(type(db), "get_integration_subject", None)):
            return await method(service, *args, **kwargs)
        bound = signature.bind(service, *args, **kwargs).arguments
        batch_id = bound.get("batch_id") or (bound.get("row") or {}).get("batch_id")
        if not batch_id:
            for field, table in (
                ("operation_id", t.integration_repair_operations),
                ("intent_id", t.integration_promotion_intents),
            ):
                if bound.get(field):
                    async with db._engine.connect() as conn:
                        row = (
                            (
                                await conn.execute(
                                    select(table).where(table.c.id == bound[field])
                                )
                            )
                            .mappings()
                            .first()
                        )
                    batch_id = (
                        row.get("batch_id", row.get("root_batch_id")) if row else None
                    )
        batch = await db.get_integration_batch(batch_id) if batch_id else None
        repository_id = batch["repository_id"] if batch else None
        if repository_id is None and bound.get("project_id"):
            project = await db.get_project(bound["project_id"])
            repository_id = project.integration_repository_id if project else None
        if repository_id is None:
            return await method(service, *args, **kwargs)
        async with primitive_scope(
            db, repository_id, batch=batch, request_id=bound.get("request_id")
        ):
            return await method(service, *args, **kwargs)

    return invoke
