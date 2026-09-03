"""Shared fixtures for statement-count/latency perf tests (spec §15).

``any_db`` parametrises over SQLite (always) and Postgres (only when
``POSTGRES_TEST_DSN`` is set) so the same budget assertions run against both
backends — SQLite proves the statement count; Postgres additionally proves
the CAS semantics under a real second connection/backend.

``perf_strict`` is the gate every *wall-clock* budget in this package takes:
statement counts are deterministic, latencies are not, and a box running
several agents (or an ``-n auto`` CI job) turns a real budget into a coin
flip.  See its docstring.
"""

from __future__ import annotations

import os

import pytest

from tests.pg_dsn import ensure_worker_postgres_dsn

#: Per-xdist-worker DSN (tests/pg_dsn.py) -- this suite and
#: tests/test_claim_queries.py / tests/test_database_postgresql.py each get
#: their own Postgres database so concurrent ``reset_for_tests()`` truncates
#: under ``-n auto`` don't race each other.
POSTGRES_TEST_DSN = ensure_worker_postgres_dsn()


@pytest.fixture(params=["sqlite", "postgres"])
async def any_db(request, tmp_path):
    if request.param == "postgres":
        if not POSTGRES_TEST_DSN:
            pytest.skip("POSTGRES_TEST_DSN not set")
        from src.database.adapters.postgresql import PostgreSQLDatabaseAdapter

        db = PostgreSQLDatabaseAdapter(POSTGRES_TEST_DSN)
        await db.initialize()
        await db.reset_for_tests()
    else:
        from src.database import Database

        db = Database(str(tmp_path / "perf.db"))
        await db.initialize()
    yield db
    await db.close()


@pytest.fixture
def perf_strict() -> None:
    """Skip a wall-clock latency budget unless ``AQ_PERF_STRICT=1``.

    Statement-count budgets are deterministic and always run.  Latency
    budgets are not: they measure the machine as much as the query, so
    under ``pytest -n auto`` — or on a developer box running several
    agents — they fail on load rather than on a regression.  pyproject's
    ``perf`` marker description states the ruling; this fixture is where
    it is enforced, so every latency assertion opts in the same way
    instead of hand-rolling the check.

    Take it as the *first* parameter of the test, ahead of any seeding
    fixture, so an un-strict run skips before paying for the seed rather
    than after.

    To run the budgets, do it deliberately and serially on a quiet box::

        AQ_PERF_STRICT=1 aq test -m perf -p no:xdist -s tests/perf
    """
    if os.environ.get("AQ_PERF_STRICT") != "1":
        pytest.skip("AQ_PERF_STRICT not set — wall-clock budgets need a quiet box")
