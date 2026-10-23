"""Unscheduled, bounded archive/compaction of terminal transcript usage calls.

Exact call-key membership is permanent. Session-only fences cannot protect calls
replayed under another session, and existing hashed keys cannot be turned into
transcript watermarks. Batches retain those keys, not per-call counter rows.
"""

from __future__ import annotations

import asyncio
import fcntl
import gzip
import json
import os
import shutil
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import delete, insert, select
from sqlalchemy.ext.asyncio import AsyncEngine

from src.database.tables import sessions, token_ledger, transcript_usage_calls, transcript_usage_fences

_RETENTION_SECONDS = 30 * 24 * 60 * 60


def _archive(path: Path, rows: list[dict]) -> None:
    """Atomically append a gzip member and durably publish before any deletion.

    Copy-on-replace avoids a crash leaving a partial member in the daily file.
    The sidecar lock serializes concurrent writers across processes. Copying is
    streaming (bounded memory); all of this work runs off the event loop.
    """
    missing = []
    parent = path.parent
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    path.parent.mkdir(parents=True, exist_ok=True)
    # A new backups directory must itself survive a crash, not just its file.
    for created in reversed(missing):
        directory = os.open(created.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    with path.with_suffix(path.suffix + ".lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as output:
                temporary = Path(output.name)
                if path.exists():
                    with path.open("rb") as original:
                        shutil.copyfileobj(original, output)
                with gzip.GzipFile(fileobj=output, mode="wb", mtime=0) as compressed:
                    for row in rows:
                        compressed.write((json.dumps(row, sort_keys=True) + "\n").encode())
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def _eligible(cutoff: float):
    return select(transcript_usage_calls, token_ledger.c.session_id).select_from(
        transcript_usage_calls.join(
            token_ledger, transcript_usage_calls.c.first_ledger_id == token_ledger.c.id
        ).join(sessions, token_ledger.c.session_id == sessions.c.id)
    ).where(
        sessions.c.state == "stopped",
        sessions.c.desired_state == "stopped",
        sessions.c.ended_at < cutoff,
        transcript_usage_calls.c.updated_at < cutoff,
    )


async def archive_transcript_usage_calls(
    engine: AsyncEngine, *, backup_dir: Path | None = None,
    batch_size: int = 500, max_batches: int = 10, now: float | None = None,
) -> dict:
    """Archive snapshots, then atomically fence/delete unchanged eligible calls.

    Each invocation is capped; a later invocation continues remaining work.
    Absent ledger/session evidence is retained. No scheduling is installed here.
    Async database I/O uses short transactions; blocking archive I/O is threaded.
    An archived but changed snapshot is harmless and may be archived again later.
    """
    if not 1 <= batch_size <= 1000 or not 1 <= max_batches <= 100:
        raise ValueError("batch_size must be 1..1000 and max_batches must be 1..100")
    now = time.time() if now is None else now
    cutoff = now - _RETENTION_SECONDS
    day = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")
    backup_dir = backup_dir or Path.home() / ".agent-queue" / "backups"
    path = backup_dir / f"transcript-usage-calls-archive-{day}.jsonl.gz"
    deleted = archived = batches = 0
    for _ in range(max_batches):
        async with engine.connect() as conn:
            snapshots = [dict(row) for row in (await conn.execute(
                _eligible(cutoff).order_by(
                    transcript_usage_calls.c.updated_at, transcript_usage_calls.c.usage_key,
                ).limit(batch_size)
            )).mappings()]
        if not snapshots:
            break
        await asyncio.to_thread(_archive, path, snapshots)
        archived += len(snapshots)
        by_key = {row["usage_key"]: row for row in snapshots}
        async with engine.begin() as conn:
            # SKIP LOCKED bounds contention with ingestion and other collectors.
            # Lock session/ledger evidence too so terminal eligibility cannot
            # change between this recheck and the atomic fence/delete commit.
            current = (await conn.execute(
                _eligible(cutoff).where(transcript_usage_calls.c.usage_key.in_(by_key))
                .order_by(transcript_usage_calls.c.usage_key)
                .with_for_update(skip_locked=True)
            )).mappings().all()
            keys = [row["usage_key"] for row in current
                    if dict(row) == by_key[row["usage_key"]]]
            if keys:
                await conn.execute(insert(transcript_usage_fences).values(
                    id=str(uuid.uuid4()), usage_keys=dict.fromkeys(keys, True),
                    archived_at=now, archive_path=str(path),
                ))
                await conn.execute(delete(transcript_usage_calls).where(
                    transcript_usage_calls.c.usage_key.in_(keys)
                ))
                deleted += len(keys)
        batches += 1
        if not keys:
            break  # avoid repeatedly archiving a contended/changed batch
    return {"archived": archived, "deleted": deleted, "batches": batches, "archive_path": str(path)}
