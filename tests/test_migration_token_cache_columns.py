"""The token-ledger cache columns and the transcript checkpoint table.

Both land in one revision (``c52f1a4fb6ba``).  What is worth pinning is that
the upgrade is additive — an existing ledger row keeps its total and simply
gains two nulls — and that the downgrade actually removes what it added,
because a migration that only goes one way is not reversible on a box that
has to roll back.
"""

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

PRIOR = "4d6f8a0b2c1e"
REVISION = "c52f1a4fb6ba"

pytestmark = pytest.mark.migration


def _config(path) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{path}")
    return config


def test_upgrade_adds_cache_columns_without_touching_existing_rows(tmp_path):
    path = tmp_path / "cache.db"
    config = _config(path)
    command.upgrade(config, PRIOR)
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO projects (id, name, created_at) VALUES ('p', 'P', 1.0)"))
        conn.execute(
            text(
                """INSERT INTO token_ledger
                (id, project_id, agent_id, task_id, tokens_used, model,
                 input_tokens, output_tokens, timestamp)
                VALUES ('tl', 'p', 'a', 't', 165, 'm', 10, 5, 100.0)"""
            )
        )

    command.upgrade(config, REVISION)

    with engine.connect() as conn:
        row = conn.execute(text("SELECT * FROM token_ledger WHERE id='tl'")).mappings().one()
        # The pre-existing row is untouched: its total already included the
        # cache volume, it just could not say how much of it was cache.
        assert row["tokens_used"] == 165
        assert row["input_tokens"] == 10 and row["output_tokens"] == 5
        assert row["cache_read_tokens"] is None
        assert row["cache_write_tokens"] is None
        # And a new row can carry the breakdown.
        conn.execute(text("SELECT cache_read_tokens FROM token_ledger"))

    with engine.begin() as conn:
        conn.execute(
            text(
                """INSERT INTO token_ledger
                (id, project_id, agent_id, task_id, tokens_used, model,
                 input_tokens, output_tokens, cache_read_tokens,
                 cache_write_tokens, timestamp)
                VALUES ('tl2', 'p', 'a', 't', 165, 'm', 10, 5, 100, 50, 101.0)"""
            )
        )
    with engine.connect() as conn:
        row = conn.execute(text("SELECT * FROM token_ledger WHERE id='tl2'")).mappings().one()
        assert row["cache_read_tokens"] == 100
        assert row["cache_write_tokens"] == 50
        assert (
            row["input_tokens"]
            + row["output_tokens"]
            + row["cache_read_tokens"]
            + row["cache_write_tokens"]
            == row["tokens_used"]
        )
    engine.dispose()


def test_transcript_checkpoints_round_trips(tmp_path):
    path = tmp_path / "checkpoints.db"
    config = _config(path)
    command.upgrade(config, PRIOR)
    engine = create_engine(f"sqlite:///{path}")
    with engine.connect() as conn:
        names = {
            row[0]
            for row in conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
        }
        assert "transcript_checkpoints" not in names

    command.upgrade(config, REVISION)
    with engine.begin() as conn:
        conn.execute(
            text(
                """INSERT INTO transcript_checkpoints
                (transcript_path, byte_offset, last_entry_uuid, session_id, updated_at)
                VALUES ('/t/a.jsonl', 4096, 'u1', 's1', 1.0)"""
            )
        )
    with engine.connect() as conn:
        row = (
            conn.execute(text("SELECT * FROM transcript_checkpoints")).mappings().one()
        )
        assert row["byte_offset"] == 4096 and row["last_entry_uuid"] == "u1"

    command.downgrade(config, PRIOR)
    with engine.connect() as conn:
        names = {
            row[0]
            for row in conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
        }
        assert "transcript_checkpoints" not in names
        columns = {
            row[1]
            for row in conn.execute(text("PRAGMA table_info(token_ledger)"))
        }
        assert "cache_read_tokens" not in columns
        assert "cache_write_tokens" not in columns
    engine.dispose()
