"""Parallel test workers must not destroy one another's databases."""

import asyncio
import uuid

from tests import db_fixtures
from tests.db_fixtures import LeasePool, lease_dsn


def test_separate_test_runs_have_distinct_lease_names():
    dsn = lease_dsn("base")
    first = LeasePool(dsn, "gw0")
    second = LeasePool(dsn, "gw0")
    assert first._name(0) != second._name(0)


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
