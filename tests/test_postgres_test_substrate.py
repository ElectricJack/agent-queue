"""Parallel test workers must not destroy one another's databases."""

import asyncio
import uuid

import pytest

from tests import db_fixtures
from tests.db_fixtures import LeasePool, lease_dsn


def test_separate_test_runs_have_distinct_lease_names():
    dsn = lease_dsn("base")
    first = LeasePool(dsn, "gw0")
    second = LeasePool(dsn, "gw0")
    assert first._name(0) != second._name(0)


async def test_clone_refuses_to_drop_an_existing_unowned_database(monkeypatch):
    executed: list[str] = []

    class Connection:
        async def execute(self, statement):
            executed.append(statement)

        async def close(self):
            return None

    async def _connect(_dsn):
        return Connection()

    async def _exists(_conn, _name):
        return True

    monkeypatch.setattr(db_fixtures, "ensure_template", lambda _dsn: _async_value("template"))
    monkeypatch.setattr(db_fixtures, "_connect_admin", _connect)
    monkeypatch.setattr(db_fixtures, "_database_exists", _exists)

    with pytest.raises(RuntimeError, match="not owned by this lease pool"):
        await db_fixtures.clone_database("postgresql://u:p@h/postgres", "foreign")

    assert executed == []


async def test_template_creation_coordinates_across_worker_databases(monkeypatch):
    name = "aq_tmpl_race_" + uuid.uuid4().hex[:12]
    monkeypatch.setattr(db_fixtures, "template_name", lambda: name)
    monkeypatch.setattr(db_fixtures, "_TEMPLATE_READY", None)
    first, second = lease_dsn("first"), lease_dsn("second")
    try:
        results = await asyncio.gather(
            db_fixtures.ensure_template(first),
            db_fixtures.ensure_template(second),
        )
        assert results == [name, name]
    finally:
        conn = await db_fixtures._connect_admin(first)
        try:
            await conn.execute(
                "UPDATE pg_database SET datistemplate = false WHERE datname = $1", name
            )
        finally:
            await conn.close()
        await db_fixtures.drop_database(first, name)


async def _async_value(value):
    return value
