"""Retiring legacy integration tables (rev-agile-ridge §5.3, §6 phase 4).

The registry ratchet needs no database: every integration table is classified
once, a family's ``code_removed`` flag matches the source, and no revision's
``upgrade()`` drops a legacy table, so retirement stays the explicit operator
step ``aq db retire --apply``.  Retire and restore run on a scratch PostgreSQL
database holding the a00000000064 archive and synthetic legacy tables.  The
``migration``-marked scenarios run the real revision chain: a deployed install,
a fresh baseline, downgrade and replay, and a populated real family retired,
restored and upgraded over again.
"""

from __future__ import annotations

import ast
import hashlib
import os
import re
import time
from importlib import import_module
from pathlib import Path

import pytest
from click.testing import CliRunner
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError

from src.database.engine import create_postgres_engine
from src.database.tables import metadata
from src.integration import table_retirement as retirement
from src.integration.table_retirement import (
    FAMILIES,
    RETAINED_TABLES,
    RetirementFamily,
    RetirementRefused,
)
from tests.alembic_revisions import previous_revision
from tests.pg_dsn import create_scratch_database

REVISION = "a00000000064"
ARCHIVE = ("integration_table_retirements", "integration_retired_rows")
TRIGGERS = {(f"{table}_append_only", table) for table in ARCHIVE}
ROOT = Path(__file__).resolve().parent.parent
RETIRING = tuple(table for family in FAMILIES for table in family.tables)


def _migration():
    return import_module(f"migrations.versions.{REVISION}_integration_table_retirement")


# --------------------------------------------------------------------- registry


def test_every_integration_table_is_classified_exactly_once():
    universe = {name for name in metadata.tables if "integration" in name} | {
        "task_branch_origins",
        "task_delivery_receipts",
    }
    assert len(RETIRING) == len(set(RETIRING)) == 37
    assert not set(RETIRING) & set(RETAINED_TABLES)
    assert set(RETIRING) | set(RETAINED_TABLES) == universe
    assert len({family.name for family in FAMILIES}) == len(FAMILIES)


def test_code_removed_matches_the_source():
    """Flip ``code_removed`` in the change that deletes a family's last user."""
    references = retirement.code_references(RETIRING)
    for family in FAMILIES:
        named = {table: references[table] for table in family.tables if references[table]}
        if family.code_removed:
            assert named == {}, family.name
            assert not set(family.tables) & set(metadata.tables), family.name
        else:
            assert named, f"{family.name}: nothing names its tables; set code_removed=True"


def _upgrade_source(path: Path) -> str:
    source = path.read_text(encoding="utf-8")
    for node in ast.parse(source).body:
        if isinstance(node, ast.FunctionDef) and node.name == "upgrade":
            return ast.get_source_segment(source, node) or ""
    return ""


def test_no_revision_upgrade_drops_a_legacy_integration_table():
    names = "|".join(re.escape(table) for table in RETIRING)
    drop = re.compile(
        rf"drop_table\(\s*['\"]({names})['\"]|DROP\s+TABLE\s+(IF\s+EXISTS\s+)?\"?({names})\b",
        re.IGNORECASE,
    )
    offenders = [
        path.name
        for path in sorted((ROOT / "migrations" / "versions").glob("*.py"))
        if drop.search(_upgrade_source(path))
    ]
    assert offenders == [], "legacy tables retire only through `aq db retire --apply`"


def test_code_references_sees_whole_words_only(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x = 'integration_batches'\n")
    (tmp_path / "src" / "b.py").write_text("x = 'integration_batches_extra'\n")
    found = retirement.code_references(
        ("integration_batches", "integration_cleanup_items"), source_root=tmp_path / "src"
    )
    assert found == {"integration_batches": ("src/a.py",), "integration_cleanup_items": ()}


# ----------------------------------------------------------------------- backup


def _backup(path: Path, body: bytes = b"PGDMP\x01\x0e\x00 dump") -> Path:
    path.write_bytes(body)
    return path


def test_verify_backup_accepts_a_recent_custom_dump(tmp_path):
    path = _backup(tmp_path / "db.dump")
    evidence = retirement.verify_backup(path)
    assert evidence.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert evidence.path == str(path.resolve())


@pytest.mark.parametrize(
    "body, age, reason",
    [
        (None, 0, "does not exist"),
        (b"", 0, "is empty"),
        (b"-- plain SQL dump", 0, "not a pg_dump --format=custom"),
        (b"PGDMP stale", 2 * 24 * 3600, "old"),
    ],
)
def test_verify_backup_refuses(tmp_path, body, age, reason):
    path = tmp_path / "db.dump"
    if body is not None:
        _backup(path, body)
        os.utime(path, (time.time() - age, time.time() - age))
    with pytest.raises(RetirementRefused, match=reason):
        retirement.verify_backup(path)


# ---------------------------------------------------------------------- the CLI


def test_cli_apply_refuses_a_worker_session(tmp_path, monkeypatch):
    from src.cli.app import cli

    monkeypatch.setenv("AQ_DB_SCOPE", "worker")
    backup = _backup(tmp_path / "db.dump")
    result = CliRunner().invoke(
        cli, ["db", "retire", "writers", "--apply", "--backup", str(backup), "--yes"]
    )
    assert result.exit_code == 2
    assert "worker session" in result.output


@pytest.mark.parametrize(
    "args, message",
    [
        (["db", "retire", "--apply", "--backup", "x"], "--apply needs FAMILY and --backup"),
        (["db", "retire", "writers", "--apply"], "--apply needs FAMILY and --backup"),
        (["db", "retire", "nope"], "unknown family 'nope'"),
    ],
)
def test_cli_rejects_incomplete_requests(args, message):
    from src.cli.app import cli

    result = CliRunner().invoke(cli, args)
    assert result.exit_code == 2
    assert message in result.output


# ------------------------------------------------------- retire and restore


FIXTURE = RetirementFamily("fixture", "test", ("legacy_fixture_runs", "legacy_fixture_steps"))
KEEPER = RetirementFamily("keeper", "test", ("legacy_fixture_keeper",))

_FIXTURE_SCHEMA = (
    "CREATE TABLE alembic_version (version_num varchar(32) PRIMARY KEY)",
    f"INSERT INTO alembic_version VALUES ('{REVISION}')",
    (
        "CREATE TABLE legacy_fixture_runs (id bigint GENERATED BY DEFAULT AS IDENTITY "
        "PRIMARY KEY, name text NOT NULL, payload jsonb, score double precision, tags text[], "
        "seen_at timestamptz, blob bytea)"
    ),
    (
        "CREATE TABLE legacy_fixture_steps (id text PRIMARY KEY, "
        "run_id bigint NOT NULL REFERENCES legacy_fixture_runs(id), ordinal integer NOT NULL)"
    ),
    (
        "INSERT INTO legacy_fixture_runs (name, payload, score, tags, seen_at, blob) VALUES "
        "('first', '{\"b\": [1, 2], \"a\": null}', 0.1, ARRAY['x', 'y'], "
        "'2026-10-03 12:00:00+00', '\\x00ff'), "
        "('zweite ü', NULL, NULL, NULL, NULL, NULL), "
        "('first', '{\"b\": [1, 2], \"a\": null}', 1e-300, '{}', NULL, '')"
    ),
    "INSERT INTO legacy_fixture_steps VALUES ('s1', 1, 1), ('s2', 1, 2), ('s3', 2, 1)",
)


def _install_archive(sync) -> None:
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    with Operations.context(MigrationContext.configure(sync)):
        _migration().upgrade()


@pytest.fixture
async def scratch():
    engine = create_postgres_engine(await create_scratch_database("retire64"))
    try:
        async with engine.begin() as conn:
            for statement in _FIXTURE_SCHEMA:
                await conn.execute(text(statement))
            await conn.run_sync(_install_archive)
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
def evidence(tmp_path):
    return retirement.verify_backup(_backup(tmp_path / "db.dump"))


@pytest.fixture
def no_source(tmp_path):
    """A source tree that names no legacy table: the code has been removed."""
    root = tmp_path / "src"
    root.mkdir()
    (root / "unrelated.py").write_text("VALUE = 1\n")
    return root


async def _rows(conn, table: str) -> list[tuple]:
    return list(await conn.execute(text(f"SELECT * FROM {table} ORDER BY 1, 2")))


async def _present(conn, *tables: str) -> set[str]:
    return set(await retirement._present(conn, tables))


async def test_retire_archives_every_row_then_drops_and_restores(scratch, evidence, no_source):
    async with scratch.connect() as conn:
        before = {table: await _rows(conn, table) for table in FIXTURE.tables}
        digests = {table: await retirement._table_digest(conn, table) for table in FIXTURE.tables}
    async with scratch.begin() as conn:
        receipts = await retirement.retire_families(
            conn, [FIXTURE], backup=evidence, retired_by="operator:test", source_root=no_source
        )
    assert {r["table_name"]: (r["row_count"], r["rows_digest"]) for r in receipts} == digests
    assert {r["family"] for r in receipts} == {"fixture"}

    async with scratch.connect() as conn:
        assert await _present(conn, *FIXTURE.tables) == set()
        stored = {
            row.table_name: row
            for row in await conn.execute(text("SELECT * FROM integration_table_retirements"))
        }
        archived = dict(
            (
                await conn.execute(
                    text(
                        "SELECT table_name, count(*) FROM integration_retired_rows "
                        "GROUP BY table_name"
                    )
                )
            ).all()
        )
    assert archived == {"legacy_fixture_runs": 3, "legacy_fixture_steps": 3}
    assert {name: row.backup_sha256 for name, row in stored.items()} == {
        name: evidence.sha256 for name in FIXTURE.tables
    }
    assert {row.schema_revision for row in stored.values()} == {REVISION}
    assert [c["name"] for c in stored["legacy_fixture_steps"].columns] == [
        "id",
        "run_id",
        "ordinal",
    ]

    # The archive is a complete copy: replaying it reproduces every row, even
    # from a session that renders timestamps and bytea differently.
    async with scratch.begin() as conn:
        await conn.execute(text("SET TimeZone = 'Pacific/Auckland'"))
        await conn.execute(text("SET bytea_output = 'escape'"))
        for table in FIXTURE.tables:
            restored = await retirement.restore_retired_table(conn, table)
            assert (restored["rows"], restored["rows_digest"]) == digests[table]
            assert restored["created"] is True
    async with scratch.connect() as conn:
        assert {table: await _rows(conn, table) for table in FIXTURE.tables} == before

    # A restored copy is not retired twice: its receipt already stands.
    with pytest.raises(RetirementRefused, match="present again after its retirement receipt"):
        async with scratch.begin() as conn:
            await retirement.retire_families(
                conn, [FIXTURE], backup=evidence, retired_by="operator:test", source_root=no_source
            )


async def test_retiring_an_already_retired_family_is_a_no_op(scratch, evidence, no_source):
    for expected in (2, 0):
        async with scratch.begin() as conn:
            receipts = await retirement.retire_families(
                conn, [FIXTURE], backup=evidence, retired_by="operator:test", source_root=no_source
            )
        assert len(receipts) == expected


async def _unchanged(scratch) -> None:
    async with scratch.connect() as conn:
        assert await _present(conn, *FIXTURE.tables) == set(FIXTURE.tables)
        assert not await conn.scalar(text("SELECT count(*) FROM integration_table_retirements"))
        assert not await conn.scalar(text("SELECT count(*) FROM integration_retired_rows"))


async def test_refuses_while_source_names_a_table(scratch, evidence, no_source):
    (no_source / "reader.py").write_text("QUERY = 'SELECT * FROM legacy_fixture_steps'\n")
    with pytest.raises(RetirementRefused, match="legacy_fixture_steps: named by 1 source file"):
        async with scratch.begin() as conn:
            await retirement.retire_families(
                conn, [FIXTURE], backup=evidence, retired_by="operator:test", source_root=no_source
            )
    await _unchanged(scratch)


async def test_outside_foreign_key_refuses_until_its_family_retires_alongside(
    scratch, evidence, no_source
):
    async with scratch.begin() as conn:
        await conn.execute(
            text(
                "CREATE TABLE legacy_fixture_keeper (run_id bigint "
                "REFERENCES legacy_fixture_runs(id))"
            )
        )
        await conn.execute(text("INSERT INTO legacy_fixture_keeper VALUES (2)"))
    with pytest.raises(RetirementRefused, match="outside the family: legacy_fixture_keeper"):
        async with scratch.begin() as conn:
            await retirement.retire_families(
                conn, [FIXTURE], backup=evidence, retired_by="operator:test", source_root=no_source
            )
    await _unchanged(scratch)

    async with scratch.begin() as conn:
        receipts = await retirement.retire_families(
            conn,
            [FIXTURE, KEEPER],
            backup=evidence,
            retired_by="operator:test",
            source_root=no_source,
        )
    assert {r["table_name"]: r["family"] for r in receipts} == {
        "legacy_fixture_keeper": "keeper",
        "legacy_fixture_runs": "fixture",
        "legacy_fixture_steps": "fixture",
    }


async def test_refuses_a_table_in_use(scratch, evidence, no_source):
    async with scratch.connect() as holder:
        await holder.execute(text("SELECT 1 FROM legacy_fixture_runs"))  # holds ACCESS SHARE
        with pytest.raises(RetirementRefused, match="in use"):
            async with scratch.begin() as conn:
                await retirement.retire_families(
                    conn,
                    [FIXTURE],
                    backup=evidence,
                    retired_by="operator:test",
                    source_root=no_source,
                )
        await holder.rollback()
    await _unchanged(scratch)


async def test_archive_is_append_only(scratch, evidence, no_source):
    async with scratch.begin() as conn:
        await retirement.retire_families(
            conn, [FIXTURE], backup=evidence, retired_by="operator:test", source_root=no_source
        )
    for statement in (
        "UPDATE integration_table_retirements SET retired_by = 'x'",
        "DELETE FROM integration_table_retirements",
        "UPDATE integration_retired_rows SET row_data = '{}'",
        "DELETE FROM integration_retired_rows",
    ):
        with pytest.raises(DBAPIError, match="append-only"):
            async with scratch.begin() as conn:
                await conn.execute(text(statement))


async def test_restore_refuses_unknown_and_occupied_tables(scratch, evidence, no_source):
    async with scratch.begin() as conn:
        await retirement.retire_families(
            conn, [FIXTURE], backup=evidence, retired_by="operator:test", source_root=no_source
        )
        await conn.execute(text("CREATE TABLE legacy_fixture_steps (id text, run_id bigint)"))
        await conn.execute(text("INSERT INTO legacy_fixture_steps VALUES ('other', 9)"))
    for table, reason in (
        ("legacy_fixture_unknown", "has no retirement receipt"),
        ("legacy_fixture_steps", "exists and is not empty"),
    ):
        with pytest.raises(RetirementRefused, match=reason):
            async with scratch.begin() as conn:
                await retirement.restore_retired_table(conn, table)


async def test_refuses_without_the_archive_tables(evidence, no_source):
    engine = create_postgres_engine(await create_scratch_database("retire64bare"))
    try:
        with pytest.raises(RetirementRefused, match="aq db upgrade"):
            async with engine.begin() as conn:
                await retirement.retire_families(
                    conn,
                    [FIXTURE],
                    backup=evidence,
                    retired_by="operator:test",
                    source_root=no_source,
                )
    finally:
        await engine.dispose()


# ------------------------------------------------------------------ migrations


async def _alembic(engine, direction: str, target: str) -> None:
    from alembic import command
    from alembic.config import Config

    def run(conn):
        config = Config(str(ROOT / "alembic.ini"))
        config.attributes["connection"] = conn
        getattr(command, direction)(config, target)

    async with engine.begin() as conn:
        await conn.run_sync(run)


def _shape(sync) -> dict:
    inspector = inspect(sync)
    present = set(inspector.get_table_names())
    triggers = {
        (row[0], row[1])
        for row in sync.execute(
            text(
                "SELECT tgname, relname FROM pg_trigger JOIN pg_class c ON c.oid = tgrelid "
                "WHERE NOT tgisinternal AND relname = ANY(:tables)"
            ),
            {"tables": list(ARCHIVE)},
        )
    }
    function = sync.scalar(
        text("SELECT count(*) FROM pg_proc WHERE proname = :name"),
        {"name": "integration_table_retirement_is_append_only"},
    )
    return {
        "archive": present & set(ARCHIVE),
        "legacy": present & set(RETIRING),
        "checks": {
            c["name"]
            for name in present & set(ARCHIVE)
            for c in inspector.get_check_constraints(name)
        },
        "triggers": triggers,
        "function": function,
    }


def _no_drift(sync) -> list:
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    diffs = compare_metadata(
        MigrationContext.configure(sync, opts={"compare_type": True}), metadata
    )
    return [diff for diff in diffs if any(name in str(diff) for name in ARCHIVE)]


def _expected_checks() -> set[str]:
    return {
        c.name
        for name in ARCHIVE
        for c in metadata.tables[name].constraints
        if c.__class__.__name__ == "CheckConstraint"
    }


def _assert_current(sync) -> None:
    shape = _shape(sync)
    assert shape["archive"] == set(ARCHIVE)
    assert shape["legacy"] == set(RETIRING), "an upgrade never drops a legacy table"
    assert shape["checks"] == _expected_checks()
    assert shape["triggers"] == TRIGGERS
    assert shape["function"] == 1
    assert _no_drift(sync) == []


_OWNER = (
    "INSERT INTO integration_branch_owners (id, repository_id, ref, owner_id, owner_role, "
    "fence_token, created_at, updated_at) VALUES (:id, 'repo', :ref, 'task-1', 'writer', "
    ":fence, 1.5, 2.25)"
)


async def _seed_writers(engine) -> None:
    async with engine.begin() as conn:
        for index in range(3):
            await conn.execute(
                text(_OWNER), {"id": f"owner-{index}", "ref": f"aq/t{index}", "fence": index}
            )
        await conn.execute(
            text(
                "INSERT INTO project_integration_leases (project_id, repository_id, batch_id, "
                "owner_id, fence_token, heartbeat_at, expires_at) VALUES "
                "('p', 'repo', 'b1', 'o', 4, 10.0, 70.0)"
            )
        )


@pytest.mark.migration
async def test_upgrade_adds_the_archive_to_a_deployed_install_and_replays():
    engine = create_postgres_engine(await create_scratch_database("retire64deployed"))
    try:
        await _alembic(engine, "upgrade", previous_revision(REVISION))
        # The baseline is built from live metadata; a deployed install at the
        # previous revision never had the archive.
        async with engine.begin() as conn:
            await conn.execute(text("DROP TABLE integration_retired_rows"))
            await conn.execute(text("DROP TABLE integration_table_retirements"))
        await _seed_writers(engine)
        await _alembic(engine, "upgrade", "head")
        async with engine.connect() as conn:
            await conn.run_sync(_assert_current)
            assert await conn.scalar(text("SELECT count(*) FROM integration_branch_owners")) == 3

        await _alembic(engine, "downgrade", previous_revision(REVISION))
        async with engine.connect() as conn:
            shape = await conn.run_sync(_shape)
        assert (shape["archive"], shape["triggers"], shape["function"]) == (set(), set(), 0)
        assert shape["legacy"] == set(RETIRING)
        await _alembic(engine, "upgrade", "head")

        # The body is idempotent: rerunning it keeps one copy of each trigger.
        async with engine.begin() as conn:
            await conn.run_sync(_install_archive)
            await conn.run_sync(_install_archive)
        async with engine.connect() as conn:
            await conn.run_sync(_assert_current)
    finally:
        await engine.dispose()


@pytest.mark.migration
async def test_fresh_baseline_gets_the_triggers_from_the_revision():
    engine = create_postgres_engine(await create_scratch_database("retire64fresh"))
    try:
        await _alembic(engine, "upgrade", previous_revision(REVISION))
        async with engine.connect() as conn:
            before = await conn.run_sync(_shape)
        assert before["archive"] == set(ARCHIVE) and before["triggers"] == set()
        await _alembic(engine, "upgrade", "head")
        async with engine.connect() as conn:
            await conn.run_sync(_assert_current)
    finally:
        await engine.dispose()


@pytest.mark.migration
async def test_populated_family_retires_restores_and_survives_replay(tmp_path, no_source):
    engine = create_postgres_engine(await create_scratch_database("retire64populated"))
    writers = retirement.family("writers")
    evidence = retirement.verify_backup(_backup(tmp_path / "db.dump"))
    try:
        await _alembic(engine, "upgrade", "head")
        await _seed_writers(engine)

        # Today's code still uses every family, so nothing is ready.
        async with engine.connect() as conn:
            for family in FAMILIES:
                report = await retirement.family_readiness(conn, family)
                assert any(row.code_references for row in report), family.name
        with pytest.raises(RetirementRefused, match="named by"):
            async with engine.begin() as conn:
                await retirement.retire_families(
                    conn, [writers], backup=evidence, retired_by="operator:test"
                )

        async with engine.connect() as conn:
            owners = await _rows(conn, "integration_branch_owners")
        async with engine.begin() as conn:
            receipts = await retirement.retire_families(
                conn, [writers], backup=evidence, retired_by="operator:test", source_root=no_source
            )
        counts = {r["table_name"]: r["row_count"] for r in receipts}
        assert counts == {
            "integration_branch_owners": 3,
            "integration_owner_recoveries": 0,
            "project_integration_leases": 1,
            "project_integration_schedules": 0,
        }

        # Old rows stay auditable after the drop, and replay into the table.
        async with engine.begin() as conn:
            assert await _present(conn, *writers.tables) == set()
            restored = await retirement.restore_retired_table(conn, "integration_branch_owners")
        assert restored["rows"] == 3
        async with engine.connect() as conn:
            assert await _rows(conn, "integration_branch_owners") == owners

        # Upgrading over a retired family neither recreates nor disturbs it.
        await _alembic(engine, "stamp", previous_revision(REVISION))
        await _alembic(engine, "upgrade", "head")
        async with engine.connect() as conn:
            shape = await conn.run_sync(_shape)
            assert await _present(conn, *writers.tables) == {"integration_branch_owners"}
            retired = await conn.scalar(text("SELECT count(*) FROM integration_table_retirements"))
        assert shape["triggers"] == TRIGGERS and retired == 4
        with pytest.raises(RuntimeError, match="holds retirement receipts"):
            await _alembic(engine, "downgrade", previous_revision(REVISION))
    finally:
        await engine.dispose()
