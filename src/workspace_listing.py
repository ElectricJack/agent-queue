"""Bounded, read-only diagnostics for ``list_workspaces``.

``list_workspaces`` used to probe every row serially with three Git calls
(each allowed the 120s Git timeout) and no overall deadline, so a project with
a stuck checkout could not be listed at all. Worse, its non-strict helpers read
a failed probe as a clean tree on branch ``""``. This module keeps the listing
inside one deadline and makes every gap explicit:

* owner facts (lock task's status and claim fence, the live session holding
  the slot) come from one read-only database transaction under its own bound;
* checkout probes run with bounded concurrency, one lock-free ``git status``
  each, and are cancelled -- killing the child -- when the deadline passes;
* every row says what is known and what is not (``unknown``, ``timeout``,
  ``not_started`` ...). None of these states is evidence that a lock or a
  worker is stale or a workspace is free: those decisions stay with
  ``workspace_reap`` / ``workspace_doctor`` and their own proofs.

Nothing here writes. Spec: ``docs/specs/command-handler.md`` (list_workspaces).
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
from collections import Counter
from collections.abc import Iterable
from typing import Any

from src.database.queries.claim_queries import CLAIM_PHASE_IN_FLIGHT

logger = logging.getLogger(__name__)

DEFAULT_DEADLINE_SECONDS = 10.0
MIN_DEADLINE_SECONDS = 0.5
# Below the CLI's 30s request timeout, so a listing always answers.
MAX_DEADLINE_SECONDS = 20.0
PROBE_CONCURRENCY = 4
OWNER_FACTS_TIMEOUT_SECONDS = 5.0
_ERROR_TEXT_LIMIT = 200

AUTHORITY_NOTE = (
    "Lock and owner fields are read from the database. A missing, timed-out, "
    "skipped or partial diagnostic is not evidence that a lock or worker is "
    "stale or that a workspace is free; reap and reset keep their own proofs."
)

# Probe outcomes that leave a row's checkout state unknown.
INCOMPLETE_GIT_STATES = frozenset({"timeout", "not_started", "error"})


def resolve_deadline(value: Any) -> float:
    """Clamp a requested deadline; ``None`` means the default.

    Raises ``TypeError`` or ``ValueError`` for a non-numeric or non-finite value.
    """
    if value is None:
        return DEFAULT_DEADLINE_SECONDS
    if isinstance(value, bool):
        raise TypeError("deadline_seconds must be a number")
    seconds = float(value)
    if not math.isfinite(seconds):
        raise ValueError("deadline_seconds must be finite")
    return min(MAX_DEADLINE_SECONDS, max(MIN_DEADLINE_SECONDS, seconds))


def _short_error(exc: BaseException) -> str:
    text = str(exc) or type(exc).__name__
    return text if len(text) <= _ERROR_TEXT_LIMIT else text[:_ERROR_TEXT_LIMIT] + "..."


# ---------------------------------------------------------------------------
# Owner facts
# ---------------------------------------------------------------------------


async def read_owner_facts(db, workspaces: list, *, timeout: float) -> dict:
    """Read owner facts and project caps for *workspaces* within *timeout*.

    Returns ``{"status": "complete"|"timeout"|"unavailable", "error": str|None,
    "tasks": {id: row}, "sessions": [row], "caps": {project_id: int}}``.
    A failure leaves the listing intact; rows then report ``unknown`` owners.
    """
    empty: dict = {"tasks": {}, "sessions": [], "caps": {}}

    async def _read() -> dict:
        facts = await db.read_workspace_owner_facts(workspaces)
        caps: dict[str, int] = {}
        for project_id in sorted({ws.project_id for ws in workspaces if ws.is_slot}):
            project = await db.get_project(project_id)
            if project is not None:
                caps[project_id] = project.max_concurrent_agents
        return {
            "tasks": {row["id"]: row for row in facts.get("tasks", [])},
            "sessions": list(facts.get("sessions", [])),
            "caps": caps,
        }

    try:
        facts = await asyncio.wait_for(_read(), timeout=timeout)
    except TimeoutError:
        return {"status": "timeout", "error": f"owner facts exceeded {timeout:g}s", **empty}
    except Exception as exc:  # noqa: BLE001 - reported, never treated as "free"
        logger.warning("list_workspaces: owner facts unavailable: %s", exc)
        return {"status": "unavailable", "error": _short_error(exc), **empty}
    return {"status": "complete", "error": None, **facts}


def _decode_claimed_by(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except ValueError:
            return value
        return decoded if isinstance(decoded, str) else value
    return str(value)


def _session_view(row: dict) -> dict:
    return {
        "id": row.get("id"),
        "agent_id": row.get("agent_id"),
        "task_id": row.get("task_id"),
        "lifecycle": row.get("lifecycle"),
        "state": row.get("state"),
        "claim_phase": row.get("claim_phase"),
        "last_claim_epoch": row.get("last_claim_epoch"),
        "last_activity": row.get("last_activity"),
        "work_dir": row.get("work_dir"),
    }


def _match_sessions(ws, sessions: Iterable[dict]) -> list[dict]:
    """Live sessions pointing at *ws*, best match first.

    A session that both runs in the slot's directory and names the lock's
    agent or task outranks one matching on a single field.
    """
    ranked: list[tuple[int, int, dict]] = []
    for order, row in enumerate(sessions):
        by_dir = bool(ws.workspace_path) and row.get("work_dir") == ws.workspace_path
        by_lock = bool(
            (ws.locked_by_task_id and row.get("task_id") == ws.locked_by_task_id)
            or (ws.locked_by_agent_id and row.get("agent_id") == ws.locked_by_agent_id)
        )
        if not (by_dir or by_lock):
            continue
        # A lock-field match in another project's session is not this slot's owner.
        if not by_dir and row.get("project_id") not in (None, ws.project_id):
            continue
        ranked.append((0 if by_dir and by_lock else 1 if by_lock else 2, order, row))
    ranked.sort(key=lambda item: item[:2])
    return [row for _, _, row in ranked]


def classify_owner(ws, facts: dict) -> dict:
    """The owner identity of *ws* as far as *facts* prove it."""
    task_id = ws.locked_by_task_id
    agent_id = ws.locked_by_agent_id
    lock_state = "task" if task_id else ("agent_only" if agent_id else "unlocked")
    owner: dict[str, Any] = {
        "lock_state": lock_state,
        "agent_id": agent_id,
        "task_id": task_id,
        "locked_at": ws.locked_at,
        "facts": facts["status"],
        "task": None,
        "session": None,
        "sessions_matched": None,
        "fence": None,
        "session_matches_claim": None,
        "classification": "unknown",
    }
    if facts["status"] != "complete":
        return owner

    if task_id:
        row = facts["tasks"].get(task_id)
        owner["task"] = (
            {
                "id": task_id,
                "found": True,
                "status": row.get("status"),
                "claim_epoch": row.get("claim_epoch"),
                "claimed_by_session": _decode_claimed_by(row.get("claimed_by_session")),
            }
            if row is not None
            else {"id": task_id, "found": False}
        )

    matched = _match_sessions(ws, facts["sessions"])
    owner["sessions_matched"] = len(matched)
    session = matched[0] if matched else None
    if session is not None:
        owner["session"] = _session_view(session)

    task = owner["task"]
    if task and task.get("found") and session is not None:
        claimed = task.get("claimed_by_session")
        owner["session_matches_claim"] = None if claimed is None else claimed == session["id"]
        if session.get("task_id") == task_id:
            epoch, seen = task.get("claim_epoch"), session.get("last_claim_epoch")
            owner["fence"] = (
                "unknown" if epoch is None or seen is None
                else "consistent" if epoch == seen else "mismatch"
            )
        else:
            owner["fence"] = "unknown"

    in_flight = session is not None and session.get("claim_phase") in CLAIM_PHASE_IN_FLIGHT
    if lock_state == "unlocked":
        owner["classification"] = "unlocked_session_present" if session else "unlocked"
    elif in_flight:
        owner["classification"] = "preparing"
    elif lock_state == "agent_only":
        owner["classification"] = "agent_held" if session else "unverified"
    elif (
        task and task.get("found") and session is not None
        and session.get("task_id") == task_id and owner["fence"] != "mismatch"
    ):
        owner["classification"] = "held"
    else:
        # The lock names an owner the facts cannot corroborate. Report it;
        # deciding it is stale is reap's job, with its own liveness proof.
        owner["classification"] = "unverified"
    return owner


def beyond_cap(ws, facts: dict) -> bool | None:
    """Whether a slot sits at or past its project's concurrency cap.

    The same test ``workspace_reap all_retired`` uses for a retired slot.
    ``None`` for non-slot rows or when the cap could not be read.
    """
    if not ws.is_slot or ws.slot_index is None:
        return None
    cap = facts["caps"].get(ws.project_id)
    return None if cap is None else ws.slot_index >= cap


# ---------------------------------------------------------------------------
# Checkout probes
# ---------------------------------------------------------------------------


def _probe_result() -> dict:
    return {"git_status": "not_started", "presence": "unknown", "branch": None,
            "dirty": None, "error": None}


async def probe_checkouts(
    git,
    targets: list[tuple[str, str, str | None]],
    *,
    deadline: float,
    concurrency: int = PROBE_CONCURRENCY,
) -> dict[str, dict]:
    """Probe each ``(workspace_id, path, skip_git_reason)`` within *deadline*.

    Presence is a directory check in a worker thread; the checkout state is one
    lock-free ``git status`` (:meth:`GitManager.aread_status_summary`). A
    target with a ``skip_git_reason`` gets only the presence check and reports
    the reason as its ``git_status``. Probes still running at the deadline are
    cancelled -- which kills their Git child -- and report ``timeout``; probes
    that never got a concurrency slot report ``not_started``. Cancelling the
    caller cancels every probe the same way.
    """
    results = {ws_id: _probe_result() for ws_id, _, _ in targets}
    if not targets:
        return results
    semaphore = asyncio.Semaphore(max(1, concurrency))
    git_timeout = max(1, math.ceil(deadline))

    async def _probe(ws_id: str, path: str, skip_reason: str | None) -> None:
        async with semaphore:
            result = results[ws_id]
            result["git_status"] = "running"
            present = await asyncio.to_thread(os.path.isdir, path) if path else False
            result["presence"] = "present" if present else "missing"
            if not present:
                result["git_status"] = "missing"
                return
            if skip_reason is not None:
                result["git_status"] = skip_reason
                return
            try:
                branch, dirty = await git.aread_status_summary(path, timeout=git_timeout)
            except Exception as exc:  # noqa: BLE001 - classified per row
                text = _short_error(exc)
                lowered = text.lower()
                if "not a git repository" in lowered:
                    result["git_status"] = "not_a_checkout"
                elif "working directory does not exist" in lowered:
                    result.update(git_status="missing", presence="missing")
                else:
                    result["git_status"] = "error"
                result["error"] = text
                return
            result.update(git_status="ok", branch=branch, dirty=dirty)

    tasks = {ws_id: asyncio.create_task(_probe(ws_id, path, skip)) for ws_id, path, skip in targets}
    try:
        _, pending = await asyncio.wait(tasks.values(), timeout=deadline)
    finally:
        for task in tasks.values():
            if not task.done():
                task.cancel()
        # Reap the cancelled probes (their Git children are killed on cancel)
        # without letting a wedged thread hold the response past a short grace.
        await asyncio.wait(tasks.values(), timeout=1.0)
    for ws_id, task in tasks.items():
        if task.done() and not task.cancelled() and task.exception() is not None:
            logger.warning("list_workspaces probe of %s failed: %s", ws_id, task.exception())
            results[ws_id].update(git_status="error", error=_short_error(task.exception()))
    for result in results.values():
        if result["git_status"] == "running":
            result["git_status"] = "timeout"
    if pending:
        logger.info(
            "list_workspaces: %d checkout probe(s) unfinished at %.1fs deadline",
            len(pending), deadline,
        )
    return results


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


async def diagnose_workspaces(
    db, git, workspaces: list, *, probe_git: bool, deadline: float
) -> tuple[dict[str, dict], dict]:
    """Owner facts and checkout probes for *workspaces*, inside *deadline*.

    Returns ``(per_row, diagnostics)`` where ``per_row[ws.id]`` carries
    ``owner``, ``presence``, ``git_status``, ``branch``, ``dirty``,
    ``git_error`` and ``beyond_cap``.
    """
    started = time.monotonic()
    facts = await read_owner_facts(
        db, workspaces, timeout=min(OWNER_FACTS_TIMEOUT_SECONDS, deadline / 2)
    )
    owners = {ws.id: classify_owner(ws, facts) for ws in workspaces}

    targets: list[tuple[str, str, str | None]] = []
    for ws in workspaces:
        if git is None or not probe_git:
            skip: str | None = "skipped"
        elif owners[ws.id]["classification"] == "preparing":
            # A writer is resetting this checkout; read nothing from it.
            skip = "skipped_active_writer"
        else:
            skip = None
        targets.append((ws.id, ws.workspace_path, skip))
    remaining = max(0.0, deadline - (time.monotonic() - started))
    probes = await probe_checkouts(git, targets, deadline=remaining)

    per_row: dict[str, dict] = {}
    for ws in workspaces:
        probe = probes[ws.id]
        per_row[ws.id] = {
            "owner": owners[ws.id],
            "presence": probe["presence"],
            "git_status": probe["git_status"],
            "branch": probe["branch"],
            "dirty": probe["dirty"],
            "git_error": probe["error"],
            "beyond_cap": beyond_cap(ws, facts),
        }
    git_counts = Counter(row["git_status"] for row in per_row.values())
    incomplete = sum(git_counts[state] for state in INCOMPLETE_GIT_STATES)
    diagnostics = {
        "complete": facts["status"] == "complete" and incomplete == 0,
        "deadline_seconds": deadline,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "owner_facts": facts["status"],
        "owner_facts_error": facts["error"],
        "git_requested": bool(probe_git and git is not None),
        "git_status_counts": dict(sorted(git_counts.items())),
        "presence_counts": dict(sorted(Counter(r["presence"] for r in per_row.values()).items())),
        "incomplete_rows": incomplete,
        "authority": AUTHORITY_NOTE,
    }
    return per_row, diagnostics
