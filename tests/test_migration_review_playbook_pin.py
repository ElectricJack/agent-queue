"""The review-playbook-pin revision adds its columns once and keeps them on rollback."""

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
REVISION = "a00000000036"
TABLE = "doc_review_revisions"
COLUMNS = ("playbook", "playbook_artifact")


def alembic(dsn, *args):
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT,
        env=dict(os.environ, AGENT_QUEUE_DB_URL=dsn),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


async def test_upgrade_adds_the_pin_columns_reruns_and_keeps_them_on_downgrade():
    dsn = await create_scratch_database("reviewplaybookpin")
    alembic(dsn, "upgrade", previous_revision(REVISION))
    engine = create_async_engine(dsn)
    try:
        # The squashed baseline uses live metadata. Drop only these columns to
        # prove the incremental revision itself adds them.
        async with engine.begin() as conn:
            for column in COLUMNS:
                await conn.exec_driver_sql(f"ALTER TABLE {TABLE} DROP COLUMN IF EXISTS {column}")
        alembic(dsn, "upgrade", REVISION)
        revision = importlib.import_module("migrations.versions.a00000000036_review_playbook_pin")

        def verify_and_rerun(bind):
            columns = {column["name"]: column for column in inspect(bind).get_columns(TABLE)}
            for name in COLUMNS:
                assert columns[name]["nullable"] is True
            with Operations.context(MigrationContext.configure(bind)):
                revision.upgrade()
                revision.upgrade()
                revision.downgrade()
            assert set(COLUMNS) <= {column["name"] for column in inspect(bind).get_columns(TABLE)}
            diffs = compare_metadata(MigrationContext.configure(bind), metadata)
            assert not [diff for diff in diffs if TABLE in str(diff)]

        async with engine.begin() as conn:
            await conn.run_sync(verify_and_rerun)
    finally:
        await engine.dispose()
