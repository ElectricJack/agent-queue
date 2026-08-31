"""``ensure_worker_postgres_dsn`` must be idempotent (I4).

Each call used to re-derive from the *already rewritten* ``POSTGRES_TEST_DSN``
and append another ``_master``/``_gwN`` suffix, so the three modules that
import it disagreed about which database they were using and
``reset_for_tests`` refused the mismatch.
"""

from __future__ import annotations

import tests.pg_dsn as pg_dsn


def test_repeated_calls_return_the_same_dsn(monkeypatch):
    monkeypatch.setattr(pg_dsn, "_CACHED_DSN", pg_dsn._UNSET)
    monkeypatch.setenv("POSTGRES_TEST_DSN", "postgresql://u:p@h:5432/aqtest")
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw3")
    created: list[tuple[str, str]] = []

    async def _fake_create(base_dsn, target_db):
        created.append((base_dsn, target_db))

    monkeypatch.setattr(pg_dsn, "_create_database_if_missing", _fake_create)

    first = pg_dsn.ensure_worker_postgres_dsn()
    second = pg_dsn.ensure_worker_postgres_dsn()
    third = pg_dsn.ensure_worker_postgres_dsn()

    assert first == "postgresql://u:p@h:5432/aqtest_gw3"
    assert first == second == third
    # The env var the adapter's own guard reads agrees with the return value.
    import os

    assert os.environ["POSTGRES_TEST_DSN"] == first
    # The database is created once, not once per caller.
    assert created == [("postgresql://u:p@h:5432/aqtest", "aqtest_gw3")]


def test_unset_dsn_is_cached_as_none(monkeypatch):
    monkeypatch.setattr(pg_dsn, "_CACHED_DSN", pg_dsn._UNSET)
    monkeypatch.delenv("POSTGRES_TEST_DSN", raising=False)
    monkeypatch.delenv("AQ_REQUIRE_POSTGRES_TESTS", raising=False)
    assert pg_dsn.ensure_worker_postgres_dsn() is None
    assert pg_dsn.ensure_worker_postgres_dsn() is None


def test_missing_dsn_fails_loudly_when_postgres_tests_are_required(monkeypatch):
    """CI sets ``AQ_REQUIRE_POSTGRES_TESTS=1`` next to its Postgres service:
    losing the DSN there must abort collection, not skip every PG arm."""
    import pytest

    monkeypatch.setattr(pg_dsn, "_CACHED_DSN", pg_dsn._UNSET)
    monkeypatch.delenv("POSTGRES_TEST_DSN", raising=False)
    monkeypatch.setenv("AQ_REQUIRE_POSTGRES_TESTS", "1")
    with pytest.raises(RuntimeError, match="silently skip"):
        pg_dsn.ensure_worker_postgres_dsn()
