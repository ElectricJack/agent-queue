"""The collaboration revision creates its schema and preserves it on rollback."""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine

from src.database.tables import metadata
from tests.alembic_revisions import previous_revision
from tests.pg_dsn import create_scratch_database

pytestmark = [pytest.mark.migration, pytest.mark.integration]
ROOT = Path(__file__).resolve().parent.parent
REVISION = "a00000000034"
TABLE_NAMES = ("collaboration_threads", "collaboration_members", "collaboration_messages")


def alembic(dsn, *args):
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT,
        env=dict(os.environ, AGENT_QUEUE_DB_URL=dsn),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


async def test_upgrade_named_constraints_rerun_and_retained_downgrade():
    dsn = await create_scratch_database("collaborationmigration")
    alembic(dsn, "upgrade", previous_revision(REVISION))
    engine = create_async_engine(dsn)
    try:
        # The squashed baseline uses live metadata. Remove only these new tables
        # to prove this incremental revision itself creates them.
        async with engine.begin() as conn:
            await conn.run_sync(
                lambda bind: [
                    metadata.tables[name].drop(bind, checkfirst=True)
                    for name in reversed(TABLE_NAMES)
                ]
            )
        alembic(dsn, "upgrade", REVISION)
        revision = importlib.import_module("migrations.versions.a00000000034_collaboration_threads")

        def verify_and_rerun(bind):
            inspector = inspect(bind)
            for name in TABLE_NAMES:
                assert inspector.has_table(name)
                expected = {
                    constraint.name
                    for constraint in metadata.tables[name].constraints
                    if constraint.__class__.__name__ == "CheckConstraint"
                }
                checks = {
                    constraint["name"] for constraint in inspector.get_check_constraints(name)
                }
                assert checks == expected
                expected_unique = {
                    constraint.name
                    for constraint in metadata.tables[name].constraints
                    if constraint.__class__.__name__ == "UniqueConstraint"
                }
                assert {
                    c["name"] for c in inspector.get_unique_constraints(name)
                } == expected_unique
                assert {
                    c["name"]
                    for c in inspector.get_indexes(name)
                    if not c.get("duplicates_constraint")
                } == {index.name for index in metadata.tables[name].indexes}
            with Operations.context(MigrationContext.configure(bind)):
                revision.upgrade()
                revision.upgrade()
                revision.downgrade()
            assert all(inspect(bind).has_table(name) for name in TABLE_NAMES)
            diffs = compare_metadata(MigrationContext.configure(bind), metadata)
            assert not [diff for diff in diffs if any(name in str(diff) for name in TABLE_NAMES)]

        async with engine.begin() as conn:
            await conn.run_sync(verify_and_rerun)
    finally:
        await engine.dispose()
