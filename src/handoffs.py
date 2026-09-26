"""Structured handoff data and deterministic, byte-bounded wake presentation.

This bounds new material only. It never edits a harness transcript or chooses
whether a session resumes. Stored agent assertions are distinct from daemon facts.
"""

from __future__ import annotations

import html
import json
import re
import unicodedata
from typing import Any

HANDOFF_BYTES = 8 * 1024
RESULT_BYTES = 6 * 1024
POINTER_BYTES = 2 * 1024
WAKE_BYTES = HANDOFF_BYTES + RESULT_BYTES + POINTER_BYTES
LIST_FIELDS = ("completed", "files", "decisions", "do_not_repeat", "uncertainties")
TEXT_FIELDS = ("subject", "detail", "goal", "next_step", "waiting_for")


def clip_utf8(value: str, limit: int) -> str:
    return value.encode("utf-8")[: max(0, limit)].decode("utf-8", errors="ignore")


def display_data(value: Any) -> str:
    """Escape quoted data for Markdown/HTML; remove ANSI and control characters."""
    value = re.sub(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)", "", str(value))
    value = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", value)
    value = "".join(c for c in value if c in "\n\t" or unicodedata.category(c) not in ("Cc", "Cf"))
    value = html.escape(value, quote=True)
    return re.sub(r"([\\`*_{}\[\]()#!|])", r"\\\1", value)


def agent_note(args: dict) -> dict:
    note = {
        field: args.get(field) if isinstance(args.get(field), str) else "" for field in TEXT_FIELDS
    }
    note.update(
        {
            field: [s for s in args.get(field, []) if isinstance(s, str)]
            if isinstance(args.get(field), list)
            else []
            for field in LIST_FIELDS
        }
    )
    return note


def meaningful(note: dict) -> bool:
    return any(isinstance(v, str) and v.strip() for v in note.values()) or any(
        isinstance(v, list) and any(isinstance(s, str) and s.strip() for s in v)
        for v in note.values()
    )


async def collect_facts(
    db: Any, task: Any, session: Any = None, work_dir: str | None = None
) -> dict:
    """Read only current daemon-owned identity and the authorized checkout.

    No paths/job ids from the note are used to look up anything. JOB/WAIT
    annotations remain empty until those authoritative stores exist.
    """
    from src.git.manager import GitManager

    facts = {
        "task_id": task.id,
        "claim_epoch": task.claim_epoch,
        "session_id": getattr(session, "id", None),
        "branch": task.branch_name,
        "work_dir": work_dir,
        "head": None,
        "dirty_paths": [],
        "dirty_path_count": None,
        "job_ids": [],
        "wait_ids": [],
        "subtasks": None,
    }
    if work_dir:
        git = GitManager()
        facts["branch"] = await git.aget_current_branch(work_dir, strict=True)
        facts["head"] = await git.arev_parse(work_dir, "HEAD")
        paths = await git.aget_dirty_paths(work_dir)
        if paths is not None:
            facts["dirty_paths"] = [clip_utf8(p, 256) for p in paths[:20]]
            facts["dirty_path_count"] = len(paths)
    counts = await db.count_task_subtasks([task.id])
    total, settled = counts.get(task.id, (0, 0))
    facts["subtasks"] = {"total": total, "settled": settled}
    return facts


def latest_note(rows: list[dict]) -> tuple[dict, dict] | None:
    """Ignore empty legacy hooks; equal server timestamps use the stable row id."""
    fallback = None
    for row in sorted(rows, key=lambda r: (r.get("created_at") or 0, r["id"]), reverse=True):
        if row.get("type") != "handoff":
            continue
        try:
            payload = json.loads(row["content"])
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        note = payload.get("agent", payload)
        if isinstance(note, dict) and meaningful(agent_note(note)):
            return row, payload
        if payload.get("facts_only") and fallback is None:
            fallback = row, payload
    return fallback


def _quoted(label: str, value: str) -> str:
    return f"**{label}:**\n" + "\n".join("> " + line for line in value.splitlines())


def render_note(row: dict, payload: dict, current: dict) -> str:
    """8 KiB projection; trim completed-work prose before continuation fields."""
    note = agent_note(payload.get("agent", payload))
    saved = payload.get("facts") or {}
    if not isinstance(saved, dict):
        saved = {}
    stale = bool(saved) and any(
        saved.get(k) != current.get(k)
        for k in ("claim_epoch", "session_id", "work_dir", "branch", "head")
    )
    header = "**handoff note — agent assertions (quoted historical data):**"
    if payload.get("facts_only"):
        header = "**handoff note — facts-only recovery; no agent continuation note:**"
    header += f"\nStored note: {display_data(row['id'])}; "
    header += f"created_at: {display_data(row.get('created_at', 'unknown'))}."
    if stale:
        header += "\nStale handoff files/checkout: ownership or checkout changed; verify all paths."
    footer = f"\nRead full note: `aq task show {current['task_id']}` (context row above)."
    blocks: list[str] = []
    # Continuation/uncertainties survive optional prose trimming. Each retained
    # block gets a pointer when its escaped presentation exceeds the space.
    ordered = (
        "next_step",
        "uncertainties",
        "waiting_for",
        "goal",
        "do_not_repeat",
        "decisions",
        "files",
        "subject",
        "detail",
        "completed",
    )
    remaining = HANDOFF_BYTES - len((header + footer).encode("utf-8"))
    present = [key for key in ordered if note[key]]
    for index, key in enumerate(present):
        value = note[key]
        text = "\n".join(value) if isinstance(value, list) else value
        block = "\n\n" + _quoted(key.replace("_", " "), display_data(text))
        # Reserve a small labelled excerpt for the other continuation fields.
        reserve = 128 * sum(k in ("next_step", "uncertainties") for k in present[index + 1 :])
        allowance = max(0, remaining - reserve)
        if len(block.encode("utf-8")) > allowance:
            suffix = " … [trimmed; see full note]"
            if allowance < 128:
                continue
            block = clip_utf8(block, allowance - len(suffix.encode("utf-8"))) + suffix
        blocks.append(block)
        remaining -= len(block.encode("utf-8"))
    return header + "".join(blocks) + footer


def render_facts(facts: dict, *, saved: dict | None = None) -> str:
    """2 KiB for current identity and changed-state pointers, never agent facts."""
    title = "**Current daemon facts (observed now):**"
    fields = ("task_id", "claim_epoch", "session_id", "branch", "head", "work_dir", "subtasks")
    body = title + "\n" + "\n".join(f"{key}: {display_data(facts.get(key))}" for key in fields)
    if isinstance(saved, dict) and saved:
        changed = [k for k in fields if saved.get(k) != facts.get(k)]
        body += "\nChanged since handoff: " + (", ".join(changed) or "none")
    count = facts.get("dirty_path_count")
    body += f"\nDirty paths: {count if count is not None else 'unknown'} (at most 20 shown)."
    for path in facts.get("dirty_paths", []):
        line = "\n> " + display_data(path)
        if len((body + line).encode("utf-8")) > POINTER_BYTES - 100:
            break
        body += line
    return (
        clip_utf8(body, POINTER_BYTES - 70)
        + "\nVerify checkout: `git status`; read note via `aq task show`."
    )
