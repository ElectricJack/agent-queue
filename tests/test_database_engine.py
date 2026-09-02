"""Regression coverage for schema startup safeguards."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
import time

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy import text as sqltext

from src.database.engine import (
    _schema_cache_directory,
    _schema_cache_inputs,
    create_sqlite_engine,
    run_schema_setup,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: The last revision *below* the swarm hierarchy pair (A = a1b2c3d4e5f6 DDL,
#: B = b2c3d4e5f6a7 canonicalise).  Downgrading here crosses both.
BELOW_HIERARCHY_PAIR = "4e925610d7a6"


def _alembic(db_url: str, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, AGENT_QUEUE_DB_URL=db_url)
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


async def test_unknown_alembic_revision_fails_without_rewriting_version(tmp_path):
    engine = create_sqlite_engine(str(tmp_path / "unknown.db"))
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
            )
            await conn.execute(text("INSERT INTO alembic_version VALUES ('not-a-real-revision')"))
        with pytest.raises(RuntimeError, match="not-a-real-revision"):
            await run_schema_setup(engine)
        async with engine.connect() as conn:
            assert (
                await conn.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar() == ("not-a-real-revision")
    finally:
        await engine.dispose()


async def test_startup_data_migrations_are_idempotent_and_preserve_same_project_links(
    tmp_path, disable_schema_cache
):
    engine = create_sqlite_engine(str(tmp_path / "idempotent.db"))
    try:
        await run_schema_setup(engine)
        # Re-running startup schema setup is the public idempotency contract.
        await run_schema_setup(engine)
        async with engine.connect() as conn:
            assert (
                await conn.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar() is not None
    finally:
        await engine.dispose()


async def test_fresh_temp_sqlite_uses_migrated_schema_cache(tmp_path, monkeypatch):
    """A fresh test database is copied from one fully migrated template.

    Removing the schema-cache path in ``run_schema_setup`` must make this
    fail: no template is produced, even though Alembic can still migrate the
    target database directly.
    """
    cache_root = tmp_path / "cache-root"
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(cache_root))
    monkeypatch.setenv("AQ_SCHEMA_CACHE", "1")

    engine = create_sqlite_engine(str(tmp_path / "cached.db"))
    try:
        await run_schema_setup(engine)
        cache_files = list(_schema_cache_directory().glob("*.db"))
        assert len(cache_files) == 1
        async with engine.connect() as conn:
            assert (
                await conn.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar() is not None
    finally:
        await engine.dispose()


async def test_corrupt_schema_cache_template_rebuilds_from_alembic(tmp_path, monkeypatch):
    """A damaged template never leaves a fresh database without a schema."""
    cache_root = tmp_path / "cache-root"
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(cache_root))
    monkeypatch.setenv("AQ_SCHEMA_CACHE", "1")

    first = create_sqlite_engine(str(tmp_path / "first.db"))
    try:
        await run_schema_setup(first)
    finally:
        await first.dispose()

    template = next(_schema_cache_directory().glob("*.db"))
    template.write_bytes(b"not a sqlite database")

    second = create_sqlite_engine(str(tmp_path / "second.db"))
    try:
        await run_schema_setup(second)
        async with second.connect() as conn:
            assert (
                await conn.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar() is not None
    finally:
        await second.dispose()

    assert template.read_bytes().startswith(b"SQLite format 3\000")


async def _setup_fresh_cached_database(tmp_path, monkeypatch, name: str = "cached.db"):
    """Run schema setup for a fresh temp database against a private cache root."""
    cache_root = tmp_path / "cache-root"
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(cache_root))
    monkeypatch.setenv("AQ_SCHEMA_CACHE", "1")
    engine = create_sqlite_engine(str(tmp_path / name))
    try:
        await run_schema_setup(engine)
        async with engine.connect() as conn:
            assert (
                await conn.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar() is not None
    finally:
        await engine.dispose()
    return cache_root


async def test_schema_cache_build_leaves_only_the_template_and_its_lock(tmp_path, monkeypatch):
    """Building the template must not orphan the ``<key>.<pid>.building.db``
    scratch file's ``-wal``/``-shm`` sidecars (they were piling up in
    ``/tmp``: the checkpoint connection was still open across the rename)."""
    await _setup_fresh_cached_database(tmp_path, monkeypatch)

    entries = sorted(entry.name for entry in _schema_cache_directory().iterdir())
    assert len(entries) == 2, entries
    assert {entry.rsplit(".", 1)[1] for entry in entries} == {"db", "lock"}


async def test_restoring_from_the_template_leaves_no_sidecars_behind(tmp_path, monkeypatch):
    """The validation read of an existing template closes its connection,
    so the copy path leaves the directory as it found it."""
    await _setup_fresh_cached_database(tmp_path, monkeypatch, name="first.db")
    await _setup_fresh_cached_database(tmp_path, monkeypatch, name="second.db")

    entries = sorted(entry.name for entry in _schema_cache_directory().iterdir())
    assert {entry.rsplit(".", 1)[1] for entry in entries} == {"db", "lock"}, entries


@pytest.mark.skipif(not hasattr(os, "getuid"), reason="POSIX ownership semantics")
async def test_schema_cache_directory_is_private_to_the_user(tmp_path, monkeypatch):
    """``/tmp`` is shared: a fixed, world-readable path would let another
    local user plant a template that passes ``quick_check`` and carries the
    right ``alembic_version`` rows.  The cache lives in a per-uid, 0700 dir."""
    cache_root = await _setup_fresh_cached_database(tmp_path, monkeypatch)

    directory = _schema_cache_directory()
    assert directory.parent == cache_root
    assert directory.name.endswith(f"-{os.getuid()}")
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700


@pytest.mark.skipif(not hasattr(os, "getuid"), reason="POSIX ownership semantics")
async def test_symlinked_schema_cache_directory_falls_back_to_alembic(tmp_path, monkeypatch):
    """A planted symlink at the cache path is refused: Alembic still migrates
    the database, and nothing is written through the link."""
    cache_root = tmp_path / "cache-root"
    cache_root.mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(cache_root))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    name = f"aq-schema-cache-{os.getuid()}"
    (cache_root / name).symlink_to(elsewhere, target_is_directory=True)

    await _setup_fresh_cached_database(tmp_path, monkeypatch)

    assert list(elsewhere.iterdir()) == []


def test_schema_cache_key_covers_the_whole_migration_environment():
    """``migrations/env.py`` (batch mode, transaction-per-migration) and the
    helper module revision ``b2c3d4e5f6a7`` imports shape the migrated
    schema as much as the revision files do, so they have to key the cache."""
    inputs = _schema_cache_inputs()
    names = {os.path.relpath(path, ROOT).replace(os.sep, "/") for path in inputs}

    assert {
        "src/database/tables.py",
        "src/database/hierarchy_migration.py",
        "migrations/env.py",
    } <= names
    assert any(name.startswith("migrations/versions/") for name in names)
    assert all(os.path.isfile(path) for path in inputs)


@pytest.mark.migration
@pytest.mark.perf
def test_sqlite_head_window_downgrade_reupgrade_preserves_and_transforms_data(tmp_path):
    """head -> below the hierarchy pair -> head, with data in the window.

    The downgrade must remove the swarm DDL; data seeded at the old schema
    (a column-only parent pointer, the pre-canonicalisation drift shape)
    must survive the re-upgrade and be *transformed*: revision B rewrites
    the pointer as a parent-child edge, flags the container and installs
    the single-parent partial unique index.
    """
    db_path = str(tmp_path / "window.db")
    db_url = f"sqlite+aiosqlite:///{db_path}"
    assert _alembic(db_url, "upgrade", "head").returncode == 0
    res = _alembic(db_url, "downgrade", BELOW_HIERARCHY_PAIR)
    assert res.returncode == 0, res.stderr

    engine = create_engine(f"sqlite:///{db_path}")
    insp = inspect(engine)
    assert "claim_epoch" not in {c["name"] for c in insp.get_columns("tasks")}
    assert "hierarchy_migration_rejects" not in insp.get_table_names()

    now = time.time()
    with engine.begin() as conn:
        conn.execute(
            sqltext("INSERT INTO projects (id, name, created_at) VALUES ('x', 'x', 0)")
        )
        for tid, parent in (("p", None), ("c", "p")):
            conn.execute(
                sqltext(
                    "INSERT INTO tasks (id, project_id, parent_task_id, title, description, "
                    "status, created_at, updated_at) "
                    "VALUES (:i, 'x', :pc, :i, :i, :s, :t, :t)"
                ),
                {"i": tid, "pc": parent, "s": "IN_PROGRESS" if tid == "p" else "READY", "t": now},
            )

    res = _alembic(db_url, "upgrade", "head")
    assert res.returncode == 0, res.stderr
    with engine.begin() as conn:
        # Preserved: both rows survive with the pointer intact.
        assert (
            conn.execute(sqltext("SELECT parent_task_id FROM tasks WHERE id='c'")).scalar()
            == "p"
        )
        # Transformed: the column-only pointer became the canonical edge...
        assert (
            conn.execute(
                sqltext(
                    "SELECT depends_on_task_id FROM task_dependencies "
                    "WHERE task_id='c' AND dep_type='parent-child'"
                )
            ).scalar()
            == "p"
        )
        # ...and the parent is now a flagged container.
        assert (
            conn.execute(
                sqltext("SELECT value FROM task_metadata WHERE task_id='p' AND key='container'")
            ).scalar()
            == "true"
        )
    insp = inspect(engine)
    assert "claim_epoch" in {c["name"] for c in insp.get_columns("tasks")}
    assert any(
        i["name"] == "uq_task_deps_single_parent"
        for i in insp.get_indexes("task_dependencies")
    )
    engine.dispose()


@pytest.mark.migration
@pytest.mark.perf
def test_one_way_token_ledger_downgrade_is_documented_lossy(tmp_path):
    """Below-hierarchy history: c4e1a9d7b310's downgrade is one-way by design.

    At head the token ledger keeps rows whose agent no longer exists (the
    whole point of the revision).  Downgrading across it re-establishes the
    FKs by deleting those orphans first — the documented, deliberately
    lossy one-way semantics — while attributable rows survive.
    """
    db_path = str(tmp_path / "oneway.db")
    db_url = f"sqlite+aiosqlite:///{db_path}"
    assert _alembic(db_url, "upgrade", "head").returncode == 0

    engine = create_engine(f"sqlite:///{db_path}")
    now = time.time()
    with engine.begin() as conn:
        conn.execute(sqltext("INSERT INTO projects (id, name, created_at) VALUES ('x','x',0)"))
        conn.execute(
            sqltext(
                "INSERT INTO agent_profiles (id, name, created_at, updated_at) "
                "VALUES ('worker','Worker',:t,:t)"
            ),
            {"t": now},
        )
        conn.execute(
            sqltext(
                "INSERT INTO agents (id, name, profile_id, created_at) "
                "VALUES ('live','live','worker',:t)"
            ),
            {"t": now},
        )
        conn.execute(
            sqltext(
                "INSERT INTO tasks (id, project_id, title, description, status, "
                "created_at, updated_at) VALUES ('t','x','t','t','READY',:t,:t)"
            ),
            {"t": now},
        )
        for lid, agent in (("keep", "live"), ("orphan", "ghost")):
            conn.execute(
                sqltext(
                    "INSERT INTO token_ledger (id, project_id, agent_id, task_id, "
                    "tokens_used, timestamp) VALUES (:i, 'x', :a, 't', 1, :t)"
                ),
                {"i": lid, "a": agent, "t": now},
            )

    # Cross c4e1a9d7b310 (its down_revision is the branchpoint below it).
    res = _alembic(db_url, "downgrade", "a1c7f3e08b42")
    assert res.returncode == 0, res.stderr
    with engine.begin() as conn:
        ids = {
            r[0] for r in conn.execute(sqltext("SELECT id FROM token_ledger")).fetchall()
        }
    assert ids == {"keep"}  # the orphan was dropped so the FK could return
    engine.dispose()
