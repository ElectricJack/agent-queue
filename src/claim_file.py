"""The on-disk claim file — pure filesystem helpers, no package imports.

``.aq/claim.json`` inside a worker's work_dir is the swarm claim fence: it
records which task a pool session holds and at which ``claim_epoch``.  Both
``src.commands.claim_commands`` (which writes it) and
``src.sessions.reconciler`` (which reads it during release) need these
helpers, and the reconciler is imported *by* the command package's session
mixin.  Keeping them here — a leaf module that imports nothing from ``src``
— is what stops that from being an import cycle.
"""

from __future__ import annotations

import json
import os

__all__ = [
    "CLAIM_FILE",
    "read_claim_file",
    "remove_claim_file",
    "remove_claim_file_if_matches",
    "write_claim_file",
]

CLAIM_FILE = os.path.join(".aq", "claim.json")


def write_claim_file(work_dir: str, payload: dict) -> str:
    path = os.path.join(work_dir, CLAIM_FILE)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    os.replace(tmp, path)
    return path


def remove_claim_file(work_dir: str) -> None:
    try:
        os.remove(os.path.join(work_dir, CLAIM_FILE))
    except FileNotFoundError:
        pass


def read_claim_file(work_dir: str) -> dict | None:
    """Return a valid claim-file object, or ``None`` when it cannot be read."""
    path = os.path.join(work_dir, CLAIM_FILE)
    try:
        with open(path) as f:
            claim = json.load(f)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    return claim if isinstance(claim, dict) else None


def remove_claim_file_if_matches(work_dir: str, task_id: str, claim_epoch: int | None) -> None:
    """Remove a claim file only when it still belongs to this claim.

    Pool workers can claim again between a terminal close and its delayed
    cleanup, so unconditional removal could erase the successor's fence.
    """
    claim = read_claim_file(work_dir)
    if claim is None:
        return
    if claim.get("task_id") != task_id or claim.get("claim_epoch") != claim_epoch:
        return
    try:
        os.remove(os.path.join(work_dir, CLAIM_FILE))
    except FileNotFoundError:
        pass
