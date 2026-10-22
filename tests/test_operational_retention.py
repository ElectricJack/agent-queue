"""Archive-first retention on real PostgreSQL, including crash and race fences."""

import gzip
import json

import pytest
from sqlalchemy import insert, select

from src.database import operational_retention as retention
from src.database import tables as t
from tests.test_playbook_run_repository import ARTIFACT, seed_artifact

NOW = 1_800_000_000.0
OLD = NOW - 31 * retention.DAY


@pytest.fixture
async def db(reuse_database):
    database = await reuse_database("operational-retention")
    await seed_artifact(database, ARTIFACT)
    return database


async def seed_runs(db):
    async with db._engine.begin() as conn:
        for name, state, completed, parent in (
            ("old", "completed", OLD, None), ("live", "running", None, None),
            ("recent", "completed", NOW, None), ("pinned", "completed", OLD, None),
            ("child", "running", None, "pinned"),
            ("live-parent-child", "completed", OLD, "live"),
        ):
            await conn.execute(insert(t.playbook_v2_runs).values(
                run_id=name, playbook_id="p", artifact_sha256=ARTIFACT, rule_id="r",
                lifecycle=state, started_at=OLD, updated_at=OLD,
                completed_at=completed, parent_run_id=parent,
            ))
        for name in ("old", "live", "recent"):
            await conn.execute(insert(t.playbook_step_receipts).values(
                receipt_id=name, run_id=name, artifact_sha256=ARTIFACT, rule_id="r",
                step_id="s", step_kind="command", idempotency_key=name,
                outcome="success", started_at=OLD, completed_at=OLD,
            ))
        for name, owner, owner_kind, expiry in (
            ("old", "old", "run", None), ("live", "live", "run", OLD),
            ("recent", "recent", "run", None),
            ("expired", "ns", "namespace", OLD),
            ("namespace", "ns", "namespace", None),
            ("snapshot", "snap", "snapshot", NOW + 100),
        ):
            await conn.execute(insert(t.playbook_private_values).values(
                value_id=name, owner_kind=owner_kind, owner_id=owner, scope_kind="system",
                scope_owner="system", format_version=1, content_type="application/json",
                ciphertext=b"encrypted", key_id="key", nonce=b"0" * 12, digest="digest",
                byte_count=9, expires_at=expiry,
            ))


async def ids(db, table):
    async with db._engine.connect() as conn:
        return set((await conn.execute(select(next(iter(table.primary_key.columns))))).scalars())


async def test_run_eligibility_and_binary_archive(db, tmp_path):
    await seed_runs(db)
    counts = await retention.sweep(db._engine, tmp_path, now=NOW)
    assert counts["playbook_private_values"] == 2
    assert counts["playbook_step_receipts"] == 1
    assert counts["playbook_v2_runs"] == 1
    assert await ids(db, t.playbook_v2_runs) == {
        "live", "recent", "pinned", "child", "live-parent-child",
    }
    assert await ids(db, t.playbook_private_values) == {"live", "recent", "namespace", "snapshot"}
    assert await ids(db, t.playbook_step_receipts) == {"live", "recent"}
    archive = next(tmp_path.glob("playbook_private_values-*.gz"))
    rows = [json.loads(line) for line in gzip.decompress(archive.read_bytes()).splitlines()]
    assert {row["value_id"] for row in rows} == {"old", "expired"}
    assert rows[0]["ciphertext"] == {"$base64": "ZW5jcnlwdGVk"}


async def seed_events(db):
    async with db._engine.begin() as conn:
        for stamp in (OLD, OLD, NOW):
            await conn.execute(insert(t.events).values(event_type="test", timestamp=stamp))
            await conn.execute(insert(t.token_ledger).values(
                id=str(stamp) + str(await conn.scalar(select(t.events.c.id).order_by(t.events.c.id.desc()).limit(1))),
                agent_id="a", task_id="t", tokens_used=1, timestamp=stamp,
            ))
        await conn.execute(insert(t.transcript_usage_calls).values(
            usage_key="keep", input_tokens=1, output_tokens=0,
            cache_read_tokens=0, cache_write_tokens=0, updated_at=OLD,
        ))


async def test_counts_bounds_and_dedupe_preserved(db, tmp_path):
    await seed_events(db)
    counts = await retention.sweep(db._engine, tmp_path, now=NOW, dry_run=True, batch_size=1)
    assert counts["events"] == counts["token_ledger"] == 2
    assert not list(tmp_path.iterdir())
    first = await retention.sweep(db._engine, tmp_path, now=NOW, batch_size=1)
    assert first["events"] == first["token_ledger"] == 1
    second = await retention.sweep(db._engine, tmp_path, now=NOW, batch_size=1)
    assert second["events"] == second["token_ledger"] == 1
    assert len(await ids(db, t.events)) == len(await ids(db, t.token_ledger)) == 1
    assert await ids(db, t.transcript_usage_calls) == {"keep"}
    assert len(gzip.decompress(next(tmp_path.glob("events-*.gz")).read_bytes()).splitlines()) == 2


async def test_archive_failure_never_deletes(db, tmp_path, monkeypatch):
    await seed_events(db)
    def fail(*args):
        raise OSError("disk full")
    monkeypatch.setattr(retention, "append_archive", fail)
    with pytest.raises(OSError, match="disk full"):
        await retention.sweep(db._engine, tmp_path, now=NOW)
    assert len(await ids(db, t.token_ledger)) == 3


async def test_changed_row_survives_archive_delete_gap(db, tmp_path, monkeypatch):
    await seed_events(db)
    original = retention.append_archive
    # Mutate on the same connection immediately before DELETE to model a
    # committed concurrent update after the selected xmin was archived.
    from sqlalchemy import event
    @event.listens_for(db._engine.sync_engine, "before_cursor_execute")
    def change(conn, cursor, statement, parameters, context, many):
        if statement.startswith("DELETE FROM token_ledger"):
            cursor.execute("UPDATE token_ledger SET tokens_used = 2")
    try:
        counts = await retention.sweep(db._engine, tmp_path, now=NOW)
    finally:
        event.remove(db._engine.sync_engine, "before_cursor_execute", change)
    assert counts["token_ledger"] == 0
    assert len(await ids(db, t.token_ledger)) == 3
    assert original is retention.append_archive


async def test_wait_cursor_and_age_protection(db, tmp_path):
    async with db._engine.begin() as conn:
        for seq, age, dispatched in ((1, 15, True), (2, 13, True), (3, 15, False), (4, 15, True)):
            await conn.execute(insert(t.wait_source_events).values(
                seq=seq, id=str(seq), source_key="test", source_generation=0,
                source_revision=str(seq), occurred_at=OLD, committed_at=NOW - age * retention.DAY,
                outcome_ref="test", payload_hash="test", payload={},
                dispatched_at=NOW if dispatched else None,
            ))
    assert (await retention.sweep(db._engine, tmp_path, now=NOW))["wait_source_events"] == 0
    async with db._engine.begin() as conn:
        await conn.execute(insert(t.wait_listener_cursors).values(
            listener_key="source", last_seq=3, updated_at=NOW,
        ))
    assert (await retention.sweep(db._engine, tmp_path, now=NOW))["wait_source_events"] == 1
    assert await ids(db, t.wait_source_events) == {2, 3, 4}


def test_atomic_archive_failure_preserves_previous_members(tmp_path, monkeypatch):
    retention.append_archive(tmp_path, "events", [{"id": 1}], NOW)
    def fail(*args):
        raise OSError("rename failed")
    monkeypatch.setattr(retention.os, "replace", fail)
    with pytest.raises(OSError, match="rename failed"):
        retention.append_archive(tmp_path, "events", [{"id": 2}], NOW)
    assert gzip.decompress(next(tmp_path.glob("*.gz")).read_bytes()) == b'{"id":1}\n'
