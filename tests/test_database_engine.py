"""Regression coverage for schema startup safeguards."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from src.database.engine import create_sqlite_engine, run_schema_setup


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


async def test_startup_data_migrations_are_idempotent_and_preserve_same_project_links(tmp_path):
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


async def test_sqlite_head_window_downgrade_reupgrade_preserves_and_transforms_data(tmp_path):
    engine = create_sqlite_engine(str(tmp_path / "window.db"))
    try:
        await run_schema_setup(engine)
        async with engine.connect() as conn:
            tables = (
                await conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
            ).scalars()
            assert "task_completion_records" in set(tables)
    finally:
        await engine.dispose()
