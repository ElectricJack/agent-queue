"""Dependency-light PostgreSQL test configuration diagnostics."""

from __future__ import annotations

import os
from collections.abc import Mapping

_POSTGRES_SETUP = """POSTGRES_TEST_DSN is not set; PostgreSQL is required by this test suite.
Nothing was run. Start the separate disposable test service and set a base DSN:

  docker compose up -d postgres-test
  export POSTGRES_TEST_DSN=postgresql+asyncpg://agent_queue_test:agent_queue_test_dev@localhost:5534/postgres

The test harness creates uniquely named databases under that server and removes only the
databases it owns. Do not point POSTGRES_TEST_DSN at the daemon's :5533 server."""


def postgres_test_dsn_error(environ: Mapping[str, str] | None = None) -> str | None:
    """Actionable setup error when the required PostgreSQL test DSN is absent."""
    values = os.environ if environ is None else environ
    return None if values.get("POSTGRES_TEST_DSN", "").strip() else _POSTGRES_SETUP
