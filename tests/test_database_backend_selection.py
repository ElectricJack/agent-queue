"""PostgreSQL selection rejects invalid targets and keeps credentials out of logs."""

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
        line = next(r.getMessage() for r in caplog.records if "database url=" in r.getMessage())
        assert "postgresql+asyncpg://" in line
        assert "localhost:5533/aq" in line

    def test_sqlite_path_is_refused(self, tmp_path):
        with pytest.raises(ValueError, match="must be a PostgreSQL DSN"):
            create_database(_config(str(tmp_path / "aq.db")))

    def test_the_password_is_never_logged(self, caplog):
        with caplog.at_level(logging.INFO, logger="src.database"):
            create_database(_config("postgresql://aq_user:hunter2@db.internal:5432/aq"))
        line = next(r.getMessage() for r in caplog.records if "database url=" in r.getMessage())
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
