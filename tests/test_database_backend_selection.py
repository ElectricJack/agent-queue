"""``create_database`` says which backend it picked, and never leaks the password.

Backend detection is a prefix match on the DSN scheme, and anything
unrecognized is treated as a SQLite *file path*.  That default is correct —
a bare path is a legal value — but when the URL was meant to be PostgreSQL
it fails silently and expensively: the daemon comes up healthy on an empty
SQLite file while the real database sits untouched.  (That is exactly what
`postgresql+asyncpg://` used to do; see
``tests/test_config.py::test_database_backend_detects_every_postgres_scheme``.)

There is nothing to raise on, so the mitigation is that the answer is in the
log on every start.  These tests hold that line.
"""

from __future__ import annotations

import logging

import pytest

from src.config import AppConfig, DatabaseConfig
from src.database import create_database, redact_dsn


def _config(url: str) -> AppConfig:
    cfg = AppConfig()
    cfg.database = DatabaseConfig(url=url)
    return cfg


class TestBackendLogLine:
    def test_postgres_url_logs_the_postgres_backend(self, caplog):
        with caplog.at_level(logging.INFO, logger="src.database"):
            create_database(_config("postgresql+asyncpg://u:pw@localhost:5533/aq"))
        line = next(r.getMessage() for r in caplog.records if "database backend=" in r.getMessage())
        assert "backend=postgresql" in line
        assert "localhost:5533/aq" in line

    def test_sqlite_path_logs_the_sqlite_backend(self, caplog, tmp_path):
        path = str(tmp_path / "aq.db")
        with caplog.at_level(logging.INFO, logger="src.database"):
            create_database(_config(path))
        line = next(r.getMessage() for r in caplog.records if "database backend=" in r.getMessage())
        assert "backend=sqlite" in line
        assert path in line

    def test_the_password_is_never_logged(self, caplog):
        with caplog.at_level(logging.INFO, logger="src.database"):
            create_database(_config("postgresql://aq_user:hunter2@db.internal:5432/aq"))
        line = next(r.getMessage() for r in caplog.records if "database backend=" in r.getMessage())
        assert "hunter2" not in line
        assert "***" in line
        # The parts an operator actually needs to read stay intact.
        assert "aq_user" in line and "db.internal:5432/aq" in line


class TestRedactDsn:
    @pytest.mark.parametrize(
        "url,expected",
        [
            ("postgresql://u:pw@h/db", "postgresql://u:***@h/db"),
            ("postgresql+asyncpg://u:pw@h:5432/db", "postgresql+asyncpg://u:***@h:5432/db"),
            # No credentials, nothing to redact.
            ("postgresql://h:5432/db", "postgresql://h:5432/db"),
            ("/var/lib/aq/aq.db", "/var/lib/aq/aq.db"),
            ("", ""),
        ],
    )
    def test_redaction(self, url, expected):
        assert redact_dsn(url) == expected
