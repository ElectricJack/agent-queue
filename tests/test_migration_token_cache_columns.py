"""Token cache accounting and transcript checkpoints in the current schema."""

from sqlalchemy import text

from src.database import Database
from tests.db_fixtures import lease_dsn


async def test_baseline_cache_columns_and_transcript_checkpoint():
    database = Database(lease_dsn("token-cache"))
    await database.initialize()
    try:
        async with database._engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO projects (id, name, created_at) VALUES ('p', 'P', 1)")
            )
            await conn.execute(
                text(
                    "INSERT INTO token_ledger "
                    "(id, project_id, agent_id, task_id, tokens_used, model, "
                    "input_tokens, output_tokens, timestamp) "
                    "VALUES ('tl', 'p', 'a', 't', 165, 'm', 10, 5, 100)"
                )
            )
            row = (
                (await conn.execute(text("SELECT * FROM token_ledger WHERE id='tl'")))
                .mappings()
                .one()
            )
            assert row["tokens_used"] == 165
            assert row["cache_read_tokens"] is None
            assert row["cache_write_tokens"] is None
            await conn.execute(
                text(
                    "UPDATE token_ledger SET cache_read_tokens=100, cache_write_tokens=50 "
                    "WHERE id='tl'"
                )
            )
            row = (
                (await conn.execute(text("SELECT * FROM token_ledger WHERE id='tl'")))
                .mappings()
                .one()
            )
            assert (
                sum(
                    row[key]
                    for key in (
                        "input_tokens",
                        "output_tokens",
                        "cache_read_tokens",
                        "cache_write_tokens",
                    )
                )
                == row["tokens_used"]
            )
            await conn.execute(
                text(
                    "INSERT INTO transcript_checkpoints "
                    "(transcript_path, byte_offset, last_entry_uuid, session_id, updated_at) "
                    "VALUES ('/t/a.jsonl', 4096, 'u1', 's1', 1)"
                )
            )
            checkpoint = (
                await conn.execute(
                    text("SELECT byte_offset, last_entry_uuid FROM transcript_checkpoints")
                )
            ).one()
            assert tuple(checkpoint) == (4096, "u1")
    finally:
        await database.close()
