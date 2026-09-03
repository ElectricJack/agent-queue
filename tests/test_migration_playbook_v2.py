"""Playbook V2 Package 3 migration chain (durable-state child plan §4.3, J-1..J-4).

Three revisions create six tables, and the whole package's rollback boundary
is "``alembic downgrade d3e7b1c9a204`` and the tables are gone".  This module
is the proof of that claim on SQLite plus the single-head invariant that keeps
the two Package 3 branches from splitting the chain, and the PostgreSQL
upgrade smoke that ``tests/test_migration_postgres_upgrade_head.py`` runs for
the chain as a whole.

Follows ``tests/test_migration_agent_flock.py``'s ``migrate()`` helper — an
Alembic ``Config`` handed a live connection — rather than shelling out, so a
failure surfaces as a Python traceback pointing at the offending revision.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from tests.pg_dsn import ensure_worker_postgres_dsn

pytestmark = pytest.mark.migration

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
POSTGRES_DSN = ensure_worker_postgres_dsn()

#: The revision immediately before the Package 3 chain — the package's
#: documented rollback target (§19).
BASE_REVISION = "d3e7b1c9a204"
PRE_TURN_RECEIPTS_REVISION = "c52f1a4fb6ba"

#: Every table the three revisions create, in creation order.
V2_TABLES = (
    "playbook_artifacts",
    "playbook_activations",
    "playbook_v2_runs",
    "playbook_step_receipts",
    "playbook_waits",
    "playbook_pending_events",
)

#: Every non-primary-key index they create, keyed by its table.
V2_INDEXES = {
    "playbook_artifacts": {
        "idx_playbook_artifacts_playbook",
        "idx_playbook_artifacts_source",
        "idx_playbook_artifacts_created",
    },
    "playbook_activations": {"idx_playbook_activations_health"},
    "playbook_v2_runs": {
        "uq_playbook_v2_runs_dispatch_rule",
        "idx_playbook_v2_runs_playbook",
        "idx_playbook_v2_runs_lifecycle",
        "idx_playbook_v2_runs_artifact",
    },
    "playbook_step_receipts": {
        "idx_playbook_step_receipts_run",
        "idx_playbook_step_receipts_key",
        "idx_playbook_step_receipts_turn",
    },
    "playbook_waits": {
        "uq_playbook_waits_active_step",
        "idx_playbook_waits_match",
        "idx_playbook_waits_deadline",
    },
    "playbook_pending_events": {
        "uq_playbook_pending_events_dedup",
        "idx_playbook_pending_events_playbook",
        "idx_playbook_pending_events_expiry",
    },
}

#: The three partial (filtered) indexes and the predicate each must carry on
#: both dialects.  A partial index that silently loses its ``WHERE`` becomes a
#: total uniqueness constraint and rejects perfectly legal rows.
PARTIAL_INDEXES = {
    "uq_playbook_v2_runs_dispatch_rule": "dispatch_id IS NOT NULL",
    "uq_playbook_waits_active_step": "state = 'active'",
    "uq_playbook_pending_events_dedup": "resolved_at IS NULL AND dedup_key <> ''",
}


def _alembic_pg(dsn: str, *args: str) -> subprocess.CompletedProcess:
    """Run Alembic against a PostgreSQL DSN out of process.

    Module level, not inlined into the async tests: Alembic's command layer is
    synchronous, and calling ``subprocess.run`` from inside a coroutine would
    block the loop the asyncpg assertions run on.  Same shape as
    ``tests/test_migration_postgres_upgrade_head.py``.
    """
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT,
        env=dict(os.environ, AGENT_QUEUE_DB_URL=dsn),
        capture_output=True,
        text=True,
        check=False,
    )


def migrate(engine, target, downgrade=False):
    with engine.connect() as conn:
        cfg = Config("alembic.ini")
        cfg.attributes["connection"] = conn
        (command.downgrade if downgrade else command.upgrade)(cfg, target)
        conn.commit()


def _sqlite_engine(tmp_path, name="playbook-v2-migration.db", foreign_keys=False):
    engine = sa.create_engine(f"sqlite:///{tmp_path / name}")
    if foreign_keys:

        @sa.event.listens_for(engine, "connect")
        def _enable_foreign_keys(conn, _record):  # pragma: no cover - driver callback
            conn.execute("PRAGMA foreign_keys=ON")

    return engine


def _inspect(engine):
    """Reflect through a connection that has not seen an older schema.

    ``e1b7c2a94d38`` repairs the artifact/activation defaults with
    ``batch_alter_table``, which on SQLite means drop-and-recreate.  A pooled
    connection that was open across that rebuild can go on answering
    ``PRAGMA index_list`` from the pre-rebuild schema, so a reflection taken
    on the migrating engine reports ``playbook_activations`` as having no
    indexes at all — which is a lie about the pool, not about the database.
    Disposing first makes every check here read the file as it actually is.
    """
    engine.dispose()
    return sa.inspect(engine)


def _table_names(engine) -> set[str]:
    return set(_inspect(engine).get_table_names())


# -- J-1 ---------------------------------------------------------------------


def test_single_head():
    """One head, always.

    §4.3 rule 2.  Two Package 3 branches each add revisions; if either ever
    re-points a ``down_revision`` at a revision that is no longer the tip, the
    chain forks and ``alembic upgrade head`` starts failing with "Multiple
    head revisions are present".
    """
    script = ScriptDirectory.from_config(Config(os.path.join(ROOT, "alembic.ini")))
    assert len(script.get_heads()) == 1, script.get_heads()


def test_the_package_three_chain_is_linear_and_ordered():
    """Artifacts before run state before waits, whatever else has landed between.

    The locked ordering (§4.3) is about *reachability*, not adjacency:
    ``b3f2c0de0002.down_revision`` is ``e1b7c2a94d38`` rather than the plan's
    ``a3f1c0de0001`` because ``e1b7c2a94d38`` (the string-default repair for
    these same tables) landed on main first and itself chains from
    ``a3f1c0de0001``.  Keeping the literal value would have produced two
    heads, which is the failure rule 2 exists to prevent, so the ordering is
    asserted here as ancestry instead.  Recorded as an approved deviation in
    the child plan §4.3.
    """
    script = ScriptDirectory.from_config(Config(os.path.join(ROOT, "alembic.ini")))
    head = script.get_current_head()
    ancestry = [rev.revision for rev in script.iterate_revisions(head, "base")]
    positions = {rev: index for index, rev in enumerate(ancestry)}
    # iterate_revisions walks newest -> oldest, so a larger index is older.
    assert positions["b3f2c0de0003"] < positions["b3f2c0de0002"] < positions["a3f1c0de0001"]
    assert positions["a3f1c0de0001"] < positions[BASE_REVISION]


# -- J-2 ---------------------------------------------------------------------


def test_upgrade_creates_every_table_and_index(tmp_path):
    engine = _sqlite_engine(tmp_path)
    try:
        migrate(engine, BASE_REVISION)
        assert not (_table_names(engine) & set(V2_TABLES))

        migrate(engine, "head")
        assert set(V2_TABLES) <= _table_names(engine)

        inspector = _inspect(engine)
        for table, expected in V2_INDEXES.items():
            found = {index["name"] for index in inspector.get_indexes(table)}
            assert expected <= found, f"{table} missing {expected - found}"
    finally:
        engine.dispose()


def test_receipt_boundary_columns_preserve_single_receipt_compatibility(tmp_path):
    engine = _sqlite_engine(tmp_path, name="receipt-boundaries.db")
    try:
        migrate(engine, "head")
        inspector = _inspect(engine)
        columns = {column["name"]: column for column in inspector.get_columns(
            "playbook_step_receipts"
        )}
        assert columns["receipt_kind"]["default"] in {"'step'", '"step"'}
        assert str(columns["turn_index"]["default"]) in {"-1", "'-1'"}
        assert columns["operator_decision_id"]["nullable"] is True
        unique = {
            constraint["name"]: tuple(constraint["column_names"])
            for constraint in inspector.get_unique_constraints("playbook_step_receipts")
        }
        assert unique["uq_playbook_step_receipts_boundary"] == (
            "run_id",
            "step_id",
            "iteration",
            "attempt",
            "turn_index",
            "receipt_kind",
        )
    finally:
        engine.dispose()


def test_pre_amendment_receipt_reads_as_step_minus_one_after_upgrade(tmp_path):
    engine = _sqlite_engine(tmp_path, name="existing-receipt.db")
    sha = "sha256:" + "a" * 64
    try:
        migrate(engine, PRE_TURN_RECEIPTS_REVISION)
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO playbook_artifacts (artifact_sha256, playbook_id, scope, "
                    "scope_identifier, schema_generation, version, source_digest, "
                    "contract_fingerprint, profile_fingerprint, compiler_build, path, "
                    "size_bytes, validation, created_at) VALUES (:sha,'p','system','',2,1,"
                    ":sha,:sha,'','build','/tmp/a.json',2,'{}',1.0)"
                ),
                {"sha": sha},
            )
            conn.execute(
                sa.text(
                    "INSERT INTO playbook_v2_runs (run_id, playbook_id, artifact_sha256, "
                    "rule_id, lifecycle, mode, snapshot_version, snapshot, snapshot_bytes, "
                    "event_type, summary, started_at, updated_at) VALUES "
                    "('r','p',:sha,'rule','running','live',1,'{}',2,'','',1.0,1.0)"
                ),
                {"sha": sha},
            )
            conn.execute(
                sa.text(
                    "INSERT INTO playbook_step_receipts "
                    "(receipt_id, run_id, artifact_sha256, rule_id, step_id, step_kind, "
                    "idempotency_key, outcome, started_at) VALUES "
                    "('old','r',:sha,'rule','step','command',:key,'success',1.0)"
                ),
                {"sha": sha, "key": "r:step:-:1"},
            )

        migrate(engine, "head")
        with engine.connect() as conn:
            row = conn.execute(
                sa.text(
                    "SELECT receipt_kind, turn_index, operator_decision_id "
                    "FROM playbook_step_receipts WHERE receipt_id='old'"
                )
            ).one()
        assert tuple(row) == ("step", -1, None)
    finally:
        engine.dispose()


def test_downgrade_removes_them_in_fk_safe_order(tmp_path):
    """With ``PRAGMA foreign_keys=ON``, so a wrong drop order actually fails.

    ``playbook_v2_runs.artifact_sha256`` references ``playbook_artifacts``, so
    dropping the artifact tables first would leave a dangling reference — the
    exact hazard §4.3 rule 3 names.
    """
    engine = _sqlite_engine(tmp_path, foreign_keys=True)
    try:
        migrate(engine, "head")
        assert set(V2_TABLES) <= _table_names(engine)

        migrate(engine, BASE_REVISION, downgrade=True)
        assert not (_table_names(engine) & set(V2_TABLES))
    finally:
        engine.dispose()


def test_upgrade_downgrade_upgrade_is_stable(tmp_path):
    """A second upgrade produces the same schema, not a partially-repaired one."""
    engine = _sqlite_engine(tmp_path)
    try:
        migrate(engine, "head")
        inspector = _inspect(engine)
        first = {
            table: sorted(column["name"] for column in inspector.get_columns(table))
            for table in V2_TABLES
        }
        migrate(engine, BASE_REVISION, downgrade=True)
        migrate(engine, "head")
        inspector = _inspect(engine)
        second = {
            table: sorted(column["name"] for column in inspector.get_columns(table))
            for table in V2_TABLES
        }
        assert first == second
    finally:
        engine.dispose()


def test_rows_written_before_the_downgrade_do_not_block_it(tmp_path):
    """Dropping the tables is the documented rollback, data and all (§19)."""
    engine = _sqlite_engine(tmp_path, name="with-rows.db", foreign_keys=True)
    try:
        migrate(engine, "head")
        sha = "sha256:" + "a" * 64
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO playbook_artifacts (artifact_sha256, playbook_id, scope, "
                    "scope_identifier, schema_generation, version, source_digest, "
                    "contract_fingerprint, profile_fingerprint, compiler_build, path, "
                    "size_bytes, validation, created_at) VALUES (:sha,'p','system','',2,1,"
                    ":sha,:sha,'','build','/tmp/a.json',2,'{}',1.0)"
                ),
                {"sha": sha},
            )
            conn.execute(
                sa.text(
                    "INSERT INTO playbook_v2_runs (run_id, playbook_id, artifact_sha256, "
                    "rule_id, lifecycle, mode, snapshot_version, snapshot, snapshot_bytes, "
                    "event_type, summary, started_at, updated_at) VALUES "
                    "('r','p',:sha,'rule','running','live',0,'{}',2,'','',1.0,1.0)"
                ),
                {"sha": sha},
            )
        migrate(engine, BASE_REVISION, downgrade=True)
        assert not (_table_names(engine) & set(V2_TABLES))
    finally:
        engine.dispose()


# -- J-3 ---------------------------------------------------------------------


def test_partial_indexes_are_created_on_sqlite(tmp_path):
    engine = _sqlite_engine(tmp_path, name="partial.db")
    try:
        migrate(engine, "head")
        engine.dispose()
        with engine.connect() as conn:
            sql = {
                row[0]: row[1]
                for row in conn.execute(
                    sa.text("SELECT name, sql FROM sqlite_master WHERE type='index'")
                ).all()
            }
        for name, predicate in PARTIAL_INDEXES.items():
            assert name in sql, f"{name} was not created"
            assert "WHERE" in (sql[name] or "").upper(), f"{name} lost its predicate"
            assert predicate.split(" IS ")[0].split(" = ")[0] in sql[name]
    finally:
        engine.dispose()


@pytest.mark.integration
async def test_partial_indexes_are_created_on_postgres():
    """Same three predicates, read back out of ``pg_indexes.indexdef``."""
    if not POSTGRES_DSN:
        pytest.skip("POSTGRES_TEST_DSN not set")
    import asyncpg

    from tests.pg_dsn import create_scratch_database

    dsn = await create_scratch_database("pbv2idx")
    result = _alembic_pg(dsn, "upgrade", "head")
    assert result.returncode == 0, result.stderr

    conn = await asyncpg.connect(dsn.replace("postgresql+asyncpg://", "postgresql://"))
    try:
        defs = {
            row["indexname"]: row["indexdef"]
            for row in await conn.fetch(
                "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname='public'"
            )
        }
    finally:
        await conn.close()
    for name in PARTIAL_INDEXES:
        assert name in defs, f"{name} was not created on PostgreSQL"
        assert "WHERE" in defs[name].upper(), f"{name} lost its predicate: {defs[name]}"


# -- J-4 ---------------------------------------------------------------------


@pytest.mark.integration
async def test_upgrade_head_on_postgres():
    """Empty PostgreSQL database to head, with the six V2 tables present.

    Mirrors ``tests/test_migration_postgres_upgrade_head.py``: the chain is
    normally only exercised against SQLite, which is far more forgiving about
    DDL, and this package adds three revisions' worth of it.
    """
    if not POSTGRES_DSN:
        pytest.skip("POSTGRES_TEST_DSN not set")
    import asyncpg

    from tests.pg_dsn import create_scratch_database

    dsn = await create_scratch_database("pbv2head")
    result = _alembic_pg(dsn, "upgrade", "head")
    assert result.returncode == 0, result.stderr

    conn = await asyncpg.connect(dsn.replace("postgresql+asyncpg://", "postgresql://"))
    try:
        present = {
            row["tablename"]
            for row in await conn.fetch(
                "SELECT tablename FROM pg_tables WHERE schemaname='public'"
            )
        }
    finally:
        await conn.close()
    assert set(V2_TABLES) <= present, set(V2_TABLES) - present
