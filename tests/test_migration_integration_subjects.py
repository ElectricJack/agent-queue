"""Revision a00000000057: integration subjects and their journal, additive and replayable.

Covers both upgrade paths a real install takes -- a deployed database at the
previous revision without the tables, and a fresh one whose squashed baseline
already built them from live metadata -- plus downgrade/upgrade replay and the
idempotent re-run of the revision body, on disposable PostgreSQL.
"""

from __future__ import annotations

from importlib import import_module
from pathlib import Path

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from src.database.engine import create_postgres_engine
from src.database.tables import metadata
from tests.alembic_revisions import previous_revision
from tests.pg_dsn import create_scratch_database

pytestmark = pytest.mark.migration

REVISION = "a00000000057"
TABLES = ("integration_subjects", "integration_subject_journal")
TRIGGERS = {
    ("integration_subject_identity_pinned", "integration_subjects"),
    ("integration_subject_journal_append_only", "integration_subject_journal"),
}
ARTIFACT = "sha256:" + "1" * 64


async def _alembic(engine, direction: str, target: str) -> None:
    from alembic import command
    from alembic.config import Config

    def run(conn):
        config = Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))
        config.attributes["connection"] = conn
        getattr(command, direction)(config, target)

    async with engine.begin() as conn:
        await conn.run_sync(run)


def _schema(sync) -> dict:
    inspector = inspect(sync)
    present = set(inspector.get_table_names())
    shape = {"tables": present & set(TABLES)}
    for name in shape["tables"]:
        shape[name] = {
            "checks": {c["name"] for c in inspector.get_check_constraints(name)},
            "uniques": {c["name"] for c in inspector.get_unique_constraints(name)},
            "indexes": {
                i["name"] for i in inspector.get_indexes(name) if not i.get("duplicates_constraint")
            },
        }
    shape["triggers"] = {
        (row[0], row[1])
        for row in sync.execute(
            text(
                "SELECT tgname, relname FROM pg_trigger JOIN pg_class c ON c.oid = tgrelid "
                "WHERE NOT tgisinternal AND relname = ANY(:tables)"
            ),
            {"tables": list(TABLES)},
        )
    }
    return shape


def _expected(name: str) -> dict:
    table = metadata.tables[name]
    kinds = {kind: set() for kind in ("checks", "uniques")}
    for constraint in table.constraints:
        if constraint.__class__.__name__ == "CheckConstraint":
            kinds["checks"].add(constraint.name)
        elif constraint.__class__.__name__ == "UniqueConstraint":
            kinds["uniques"].add(constraint.name)
    return {**kinds, "indexes": {index.name for index in table.indexes}}


def _no_drift(sync) -> list:
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    diffs = compare_metadata(
        MigrationContext.configure(sync, opts={"compare_type": True}), metadata
    )
    return [diff for diff in diffs if any(name in str(diff) for name in TABLES)]


def _assert_current(sync) -> None:
    shape = _schema(sync)
    assert shape["tables"] == set(TABLES)
    for name in TABLES:
        assert shape[name] == _expected(name), name
    assert shape["triggers"] == TRIGGERS
    assert _no_drift(sync) == []


def _seed_artifact(sync) -> None:
    sync.execute(
        text(
            "INSERT INTO playbook_artifacts (artifact_sha256, playbook_id, source_digest, "
            "contract_fingerprint, compiler_build, path, created_at) VALUES "
            "(:sha, 'root-train', 'sha256:s', 'sha256:c', 'b', '/a', 1) ON CONFLICT DO NOTHING"
        ),
        {"sha": ARTIFACT},
    )


_SUBJECT = (
    "INSERT INTO integration_subjects (id, project_id, repository_id, kind, subject_key, phase, "
    "policy_playbook_id, policy_artifact_sha256, next_due_at, due_set_at, max_wait_seconds, "
    "gate_id, created_at, updated_at) VALUES (:id, 'p', 'r', 'root_batch', :id, 'testing', "
    "'root-train', :sha, :due, 100, 60, :gate, 100, 100)"
)


async def _assert_constraints_bind(engine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(_seed_artifact)
        await conn.execute(
            text(_SUBJECT), {"id": "waiting", "sha": ARTIFACT, "due": 160, "gate": None}
        )
        await conn.execute(
            text(_SUBJECT), {"id": "held", "sha": ARTIFACT, "due": None, "gate": "gate-1"}
        )
    for due, gate in ((None, None), (161, None)):
        with pytest.raises(IntegrityError, match="ck_integration_subjects_never_blocked"):
            async with engine.begin() as conn:
                await conn.execute(
                    text(_SUBJECT), {"id": "blocked", "sha": ARTIFACT, "due": due, "gate": gate}
                )
    with pytest.raises(DBAPIError, match="policy artifact is pinned"):
        async with engine.begin() as conn:
            await conn.run_sync(_seed_artifact)
            await conn.execute(
                text(
                    "INSERT INTO playbook_artifacts (artifact_sha256, playbook_id, "
                    "source_digest, contract_fingerprint, compiler_build, path, created_at) "
                    "VALUES ('sha256:new', 'root-train', 's', 'c', 'b', '/n', 1)"
                )
            )
            await conn.execute(
                text(
                    "UPDATE integration_subjects SET policy_artifact_sha256 = 'sha256:new' "
                    "WHERE id = 'waiting'"
                )
            )
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO integration_subject_journal (subject_id, entry_kind, "
                "idempotency_key, mode, policy_artifact_sha256, subject_version, phase, "
                "generation, rule, primitive, facts_digest, recorded_at) VALUES ('waiting', "
                "'decision', 'v1', 'shadow', :sha, 0, 'testing', 0, 'r', 'wait', 'sha256:f', 1)"
            ),
            {"sha": ARTIFACT},
        )
    with pytest.raises(DBAPIError, match="append-only"):
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM integration_subject_journal"))


async def test_upgrade_adds_subjects_to_a_deployed_install_and_replays():
    engine = create_postgres_engine(await create_scratch_database("subjects57"))
    try:
        await _alembic(engine, "upgrade", previous_revision(REVISION))
        # The baseline is built from live metadata; a deployed install at the
        # previous revision never had these tables or their triggers.
        async with engine.begin() as conn:
            await conn.execute(text("DROP TABLE integration_subject_journal"))
            await conn.execute(text("DROP TABLE integration_subjects"))
        await _alembic(engine, "upgrade", REVISION)
        async with engine.connect() as conn:
            await conn.run_sync(_assert_current)
        await _assert_constraints_bind(engine)

        # Replay: down removes exactly what up added, and up again restores it.
        await _alembic(engine, "downgrade", previous_revision(REVISION))
        async with engine.connect() as conn:
            shape = await conn.run_sync(_schema)
            functions = set(
                (
                    await conn.execute(
                        text(
                            "SELECT proname FROM pg_proc WHERE proname IN "
                            "('integration_subject_identity_is_pinned', "
                            "'integration_subject_journal_is_append_only')"
                        )
                    )
                ).scalars()
            )
        assert (shape["tables"], shape["triggers"], functions) == (set(), set(), set())
        await _alembic(engine, "upgrade", REVISION)
        async with engine.connect() as conn:
            await conn.run_sync(_assert_current)

        # The body itself is idempotent: rerunning it over the current schema
        # changes nothing and keeps one copy of each trigger.
        revision = import_module("migrations.versions.a00000000057_integration_subjects")

        def rerun(sync):
            from alembic.migration import MigrationContext
            from alembic.operations import Operations

            with Operations.context(MigrationContext.configure(sync)):
                revision.upgrade()
                revision.upgrade()

        async with engine.begin() as conn:
            await conn.run_sync(rerun)
        async with engine.connect() as conn:
            await conn.run_sync(_assert_current)
    finally:
        await engine.dispose()


async def test_fresh_baseline_gets_the_triggers_from_the_revision():
    """A new install's baseline already has the tables; the revision adds the guards."""
    engine = create_postgres_engine(await create_scratch_database("subjects57fresh"))
    try:
        await _alembic(engine, "upgrade", previous_revision(REVISION))
        async with engine.connect() as conn:
            before = await conn.run_sync(_schema)
        assert before["tables"] == set(TABLES) and before["triggers"] == set()
        await _alembic(engine, "upgrade", "head")
        async with engine.connect() as conn:
            await conn.run_sync(_assert_current)
        await _assert_constraints_bind(engine)
    finally:
        await engine.dispose()
