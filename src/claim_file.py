"""The worktree claim file — the on-disk half of the pull-based claim fence.

``aq task claim`` writes ``.aq/claim.json`` into the worker's work_dir; the
reconciler and the close path read it back to decide whether a claim still
belongs to the task they are cleaning up (swarm-work-model §10).

This module exists as a **leaf** on purpose.  Both
:mod:`src.commands.claim_commands` (the CLI/MCP surface) and
:mod:`src.sessions.reconciler` (the cascade step) need these helpers, and
importing anything from ``src.commands`` pulls in the whole
``CommandHandler`` mixin graph — which imports ``src.sessions.reconciler``
straight back.  Keeping the helpers here, with nothing but stdlib imports,
is what breaks that cycle.  Do not add project imports to this module.
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
