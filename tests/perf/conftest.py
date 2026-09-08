"""Shared fixtures for statement-count/latency perf tests (spec §15).

``any_db`` parametrises over SQLite (always) and Postgres (only when
``POSTGRES_TEST_DSN`` is set) so the same budget assertions run against both
backends — SQLite proves the statement count; Postgres additionally proves
the CAS semantics under a real second connection/backend.

``perf_strict`` (defined in ``tests/conftest.py``) is the gate every
*wall-clock* budget in this package takes: statement counts are
deterministic, latencies are not, and a box running several agents (or an
``-n auto`` CI job) turns a real budget into a coin flip.  See its
docstring.
"""

from __future__ import annotations

import pytest

from tests.pg_dsn import ensure_worker_postgres_dsn
from tests.db_fixtures import lease_dsn

#: Per-xdist-worker DSN (tests/pg_dsn.py) -- this suite and
#: tests/test_claim_queries.py / tests/test_database_postgresql.py each get
#: their own Postgres database so concurrent ``reset_for_tests()`` truncates
#: under ``-n auto`` don't race each other.
POSTGRES_TEST_DSN = ensure_worker_postgres_dsn()


@pytest.fixture
async def any_db(request, tmp_path):
    from src.database import Database

    db = Database(lease_dsn("perf.db"))
    await db.initialize()
    yield db
    await db.close()


#: ``perf_strict`` is defined in ``tests/conftest.py`` -- wall-clock budgets
#: are not confined to this package, so the gate lives at the root where every
#: suite can take it.  It is still the gate every latency budget here takes.
