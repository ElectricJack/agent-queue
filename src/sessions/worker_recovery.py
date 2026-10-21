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
DISPOSITION_KEY = "worker_recovery_disposition"
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
        await db.lock_hierarchy_project(conn, task.project_id)
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


async def pool_recovery_disposition(db, session, task, record: dict) -> dict | None:
    """Observe teardown under the failed claim's fence, without releasing it.

    Teardown can stop the writer yet retain an integration attachment. That is
    a deferred continuation, not a successful retry. A successor or explicit
    task decision always wins over this historical failure observation.
    """
    transition = None
    async with db.immediate() as conn:
        # Owner recovery takes the hierarchy lock before releasing its writer.
        # Match that order before locking the session/task/owner observations.
        await db.lock_hierarchy_project(conn, task.project_id)
        row = (await conn.execute(
            select(sessions).where(sessions.c.id == session.id).with_for_update()
        )).mappings().first()
        current = (await conn.execute(
            select(tasks).where(tasks.c.id == task.id).with_for_update()
        )).mappings().first()
        raw = await conn.scalar(select(task_metadata.c.value).where(
            task_metadata.c.task_id == task.id, task_metadata.c.key == STREAK_KEY,
        ))
        if (
            row is None or current is None or not raw or json.loads(raw) != record
            or current["claim_epoch"] != record["claim_epoch"]
            or row["last_claim_epoch"] != record["claim_epoch"]
            or row["instance_token"] != record["instance_token"]
            or row["desired_state"] != "stopped"
            or current["profile_id"] != task.profile_id
            or current["intelligence_class"] != task.intelligence_class
        ):
            return None
        previous = await conn.scalar(select(task_metadata.c.value).where(
            task_metadata.c.task_id == task.id, task_metadata.c.key == DISPOSITION_KEY,
        ))
        if previous and json.loads(previous).get("failure") == record:
            return None  # Already reported; duplicate observations are silent.
        failure_status = (TaskStatus.BLOCKED if record["count"] >= MAX_SAME_CAUSE
                          else TaskStatus.IN_PROGRESS)
        if (
            row["state"] == "stopped" and row["task_id"] is None
            and current["assigned_agent_id"] is None
            and current["status"] == TaskStatus.READY.value
            and record["count"] < MAX_SAME_CAUSE
        ):
            disposition = "retry_ready"
        elif (
            row["state"] == "stopped" and row["end_reason"] == record["cause"]
            and row["task_id"] == task.id and row["claim_phase"] == "active"
            and current["assigned_agent_id"] == task.assigned_agent_id
            and current["status"] == failure_status.value
        ):
            disposition = "handoff_deferred"
            if current["status"] == TaskStatus.IN_PROGRESS.value:
                attention = f"session_{record['cause']}_handoff_deferred"
                await db._upsert_meta(task.id, "needs_attention", attention, conn=conn)
                transition = await db._apply_transition(
                    conn, task.id, TaskStatus.BLOCKED, context=attention, resume_after=None,
                )
        elif (
            row["state"] == "stopped" and row["task_id"] is None
            and current["assigned_agent_id"] is None
            and current["status"] == TaskStatus.BLOCKED.value
            and record["count"] >= MAX_SAME_CAUSE
        ):
            disposition = "attention_required"
        elif (
            row["state"] in {"starting", "running", "draining"}
            and row["task_id"] == task.id and row["claim_phase"] == "active"
            and current["assigned_agent_id"] == task.assigned_agent_id
            and current["status"] == failure_status.value
        ):
            disposition = "stop_unconfirmed"
        else:
            return None  # No confirmed stop/release, or another decision owns it.
        receipt = {"failure": record, "disposition": disposition,
                   "branch": task.branch_name, "work_dir": session.work_dir}
        await db._upsert_meta(task.id, DISPOSITION_KEY, receipt, conn=conn)
    if transition is not None:
        await db.log_blocked_flips(transition.flipped)
        await db._notify_settled(transition.settled)
        await db._notify_ready(transition.ready)
    return receipt


async def prepare_guarded_recovery(db, session, task, record: dict) -> bool:
    """Reset continuation controls only while this failure still owns the task."""
    from sqlalchemy import delete

    async with db.immediate() as conn:
        row = (await conn.execute(
            select(sessions).where(sessions.c.id == session.id).with_for_update()
        )).mappings().first()
        current = (await conn.execute(
            select(tasks).where(tasks.c.id == task.id).with_for_update()
        )).mappings().first()
        raw = await conn.scalar(select(task_metadata.c.value).where(
            task_metadata.c.task_id == task.id, task_metadata.c.key == STREAK_KEY,
        ))
        expected = (TaskStatus.BLOCKED if record["count"] >= MAX_SAME_CAUSE
                    else TaskStatus.IN_PROGRESS)
        if (
            row is None or current is None or not raw or json.loads(raw) != record
            or current["claim_epoch"] != record["claim_epoch"]
            or current["status"] != expected.value
            or current["assigned_agent_id"] != task.assigned_agent_id
            or current["profile_id"] != task.profile_id
            or current["intelligence_class"] != task.intelligence_class
            or row["task_id"] != task.id
            or row["instance_token"] != record["instance_token"]
            or row["desired_state"] != "running"
            or row["state"] not in {"starting", "running", "draining"}
            or (session.lifecycle == "pool"
                and row["last_claim_epoch"] != record["claim_epoch"])
        ):
            return False
        if record["cause"] == THRASH_CAUSE:
            await conn.execute(delete(task_metadata).where(
                task_metadata.c.task_id == task.id,
                task_metadata.c.key == "session_resume_key",
            ))
        await db._upsert_meta(task.id, "stall_nudges", "0", conn=conn)
        await db._upsert_meta(task.id, "stall_last_action_at", "0", conn=conn)
    return True
