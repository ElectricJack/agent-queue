# tests/test_db_immediate.py
"""``Database.immediate()`` — the write-locked transaction context (spec §7).

The cascade-delete and subtree-abandon commands read a guard condition and
then act on it in one transaction; on SQLite that needs ``BEGIN IMMEDIATE``,
not the default deferred transaction.
"""

from __future__ import annotations

import sqlite3
import time

import pytest
from sqlalchemy import insert, select

from src.database import Database
from src.database.tables import projects as projects_t


@pytest.fixture
async def db(tmp_path):
    d = Database(str(tmp_path / "imm.db"))
    await d.initialize()
    yield d
    await d.close()


async def _project_ids(db) -> set[str]:
    async with db._engine.connect() as conn:
        rows = await conn.execute(select(projects_t.c.id))
        return {r[0] for r in rows}


class TestImmediate:
    async def test_exists_on_the_adapter(self, db):
        # The two call sites used to fall back to ``_engine.begin`` via
        # getattr because this method did not exist.
        assert callable(getattr(db, "immediate", None))

    async def test_write_commits_and_is_visible_after_the_block(self, db):
        async with db.immediate() as conn:
            await conn.execute(
                insert(projects_t).values(id="p-ok", name="ok", created_at=time.time())
            )
        assert "p-ok" in await _project_ids(db)

    async def test_exception_rolls_back(self, db):
        with pytest.raises(RuntimeError):
            async with db.immediate() as conn:
                await conn.execute(
                    insert(projects_t).values(id="p-bad", name="bad", created_at=time.time())
                )
                raise RuntimeError("boom")
        assert "p-bad" not in await _project_ids(db)

    async def test_holds_the_sqlite_write_lock(self, db, tmp_path):
        """A foreign connection must not be able to write while the block is open.

        In-process this cannot be shown: the SQLite engine uses ``StaticPool``,
        so every checkout is the *same* DBAPI connection.  A separate
        ``sqlite3`` connection to the same file is a genuine second writer.
        """
        path = str(tmp_path / "imm.db")
        async with db.immediate() as conn:
            await conn.execute(
                insert(projects_t).values(id="p-lock", name="lock", created_at=time.time())
            )
            other = sqlite3.connect(path, timeout=0.5)
            try:
                with pytest.raises(sqlite3.OperationalError, match="locked"):
                    other.execute(
                        "INSERT INTO projects (id, name, created_at) VALUES (?, ?, ?)",
                        ("p-other", "other", time.time()),
                    )
                    other.commit()
            finally:
                other.close()
        assert "p-lock" in await _project_ids(db)
