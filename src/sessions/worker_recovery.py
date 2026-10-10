"""Task-scoped failure streaks and bounded continuation evidence.

Session rows are replaced by pool recovery. These observations belong to the
task, and every write is fenced to the session instance and current claim.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path

from sqlalchemy import select

from src.database.tables import sessions, task_metadata, tasks
from src.handoffs import clip_utf8
from src.models import TaskStatus
from src.sessions.transcripts.base import parse_iso_ts

logger = logging.getLogger(__name__)
STREAK_KEY = "worker_failure_streak"
CONTINUATION_KEY = "worker_recovery_note"
THRASH_CAUSE = "context_thrash"
GUARDED_CAUSES = frozenset({THRASH_CAUSE, "stalled", "stuck_timeout"})
MAX_SAME_CAUSE = 2
TAIL_BYTES = 256 * 1024
_THRASH = re.compile(r"^\s*(?:API Error:\s*)?Autocompact is thrashing\s*:", re.I)


def thrash_screen(text: str) -> bool:
    """Match the CLI diagnostic, rather than any mention in scrollback."""
    return any(_THRASH.match(line.lstrip("!✗❌ ")) for line in text.splitlines()[-30:])


def _transcript_thrash(path: Path, session) -> bool:
    with path.open("rb") as stream:
        stream.seek(0, 2)
        start = max(0, stream.tell() - TAIL_BYTES)
        stream.seek(start)
        lines = stream.read(TAIL_BYTES).splitlines(keepends=True)
    if start and lines:
        lines.pop(0)
    for line in reversed(lines):
        if not line.endswith(b"\n"):
            continue
        try:
            raw = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            continue
        if not isinstance(raw, dict) or raw.get("type") != "assistant":
            continue
        # Only the latest assistant record from this launch counts. A later
        # response means the harness recovered; a resumed older error cannot
        # kill a new process. User/tool quotations never count.
        if parse_iso_ts(raw.get("timestamp")) < (session.started_at or 0):
            return False
        if raw.get("isApiErrorMessage") is not True:
            return False
        message = raw.get("message") or {}
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str):
            return thrash_screen(content)
        if isinstance(content, list):
            return any(
                isinstance(block, dict) and block.get("type") == "text"
                and thrash_screen(str(block.get("text") or "")) for block in content
            )
        return False
    return False


async def context_thrashed(session, *, base_dir: Path | None = None) -> bool:
    """Read only the exact Claude session's tail; unknown evidence is false."""
    if session.harness != "claude" or not session.session_key:
        return False
    from src.sessions.transcripts import resolve_reader

    def read():
        try:
            reader = resolve_reader(session.harness, base_dir=base_dir)
            path = reader.resolve_session(session) if reader else None
            return _transcript_thrash(path, session) if path else False
        except (OSError, ValueError, TypeError):
            return False

    return await asyncio.to_thread(read)


async def record_failure(db, session, task, cause: str, *, now: float) -> dict | None:
    """Count once per instance, surviving new claims and daemon restarts."""
    transition = None
    async with db.immediate() as conn:
        live = (await conn.execute(
            select(sessions).where(sessions.c.id == session.id).with_for_update()
        )).mappings().first()
        current = (await conn.execute(
            select(tasks).where(tasks.c.id == task.id).with_for_update()
        )).mappings().first()
        if (
            current is None or live is None
            or current["status"] != TaskStatus.IN_PROGRESS.value
            or current["claim_epoch"] != task.claim_epoch
            or live["task_id"] != task.id
            or live["instance_token"] != session.instance_token
            or live["state"] not in {"starting", "running", "draining"}
            or live["desired_state"] != "running"
            or (session.lifecycle == "pool"
                and live["last_claim_epoch"] != current["claim_epoch"])
        ):
            return None
        raw = await conn.scalar(select(task_metadata.c.value).where(
            task_metadata.c.task_id == task.id, task_metadata.c.key == STREAK_KEY
        ))
        try:
            previous = json.loads(raw) if raw else {}
        except (ValueError, TypeError):
            previous = {}
        if not isinstance(previous, dict):
            previous = {}
        if (previous.get("session_id"), previous.get("instance_token")) == (
            session.id, session.instance_token
        ):
            return previous
        count = previous.get("count", 0) if previous.get("cause") == cause else 0
        count = count if type(count) is int and count >= 0 else 0
        record = {"cause": cause, "count": count + 1, "session_id": session.id,
                  "instance_token": session.instance_token, "claim_epoch": task.claim_epoch,
                  "observed_at": now}
        await db._upsert_meta(task.id, STREAK_KEY, record, conn=conn)
        if cause in GUARDED_CAUSES and record["count"] >= MAX_SAME_CAUSE:
            await db._upsert_meta(task.id, "needs_attention", f"session_{cause}", conn=conn)
            transition = await db._apply_transition(
                conn, task.id, TaskStatus.BLOCKED, context=f"session_{cause}",
                resume_after=None,
            )
    if transition is not None:
        await db.log_blocked_flips(transition.flipped)
        await db._notify_settled(transition.settled)
        await db._notify_ready(transition.ready)
    return record


async def save_continuation(db, session, task, record: dict) -> None:
    """Save observed progress and checkout pointers, without inventing work."""
    from src.handoffs import collect_facts

    note = {**record, "branch": task.branch_name, "work_dir": session.work_dir,
            "harness": session.harness, "session_key": session.session_key,
            "progress": [], "facts": None}
    try:
        facts = await collect_facts(db, task, session, session.work_dir)
        # Keep continuation identifiers and checkout evidence; no transcripts.
        note["facts"] = {key: facts.get(key) for key in (
            "head", "branch", "dirty_paths", "dirty_path_count", "subtasks",
            "job_ids", "wait_ids",
        )}
        for key in ("dirty_paths", "job_ids", "wait_ids"):
            if isinstance(note["facts"].get(key), list):
                note["facts"][key] = note["facts"][key][:5]
    except Exception:
        logger.debug("worker recovery checkout unreadable for %s", task.id, exc_info=True)
    try:
        page = await db.list_task_comments(task.id, limit=5, offset=0, project_id=task.project_id)
        note["progress"] = [
            {"author": f"{c['author_kind']}:{c['author_id']}",
             "body": clip_utf8(c["body"], 700), "created_at": c["created_at"]}
            for c in page.get("comments", []) if c["author_id"] != "session-reconciler"
        ][:2]
    except Exception:
        logger.debug("worker recovery comments unreadable for %s", task.id, exc_info=True)
    async with db.immediate() as conn:
        current = (await conn.execute(select(tasks.c.claim_epoch).where(
            tasks.c.id == task.id
        ).with_for_update())).scalar_one_or_none()
        raw = await conn.scalar(select(task_metadata.c.value).where(
            task_metadata.c.task_id == task.id, task_metadata.c.key == STREAK_KEY,
        ))
        # Gathering git evidence can yield to a newer attempt. Never overwrite
        # that attempt's continuation or resurrect an explicitly cleared streak.
        if current == task.claim_epoch and raw and json.loads(raw) == record:
            await db._upsert_meta(task.id, CONTINUATION_KEY, note, conn=conn)
