"""Shared claim-epoch resolution + CLI option for pool-session mutators
(swarm-work-model §10).

Lives in its own module rather than ``agent_surface.py`` or ``tasks.py``:
those two need each other at *import* time (``agent_surface`` attaches
``task_claim``/``task_close``/``task_heartbeat`` onto the ``task`` group
``tasks.py`` defines; ``tasks.py``'s ``task_set`` needs the
``claim_epoch_option`` decorator applied at its own module-load time). A
decorator that both modules apply at definition time cannot itself live in
either one without making whichever module is imported *first* fail
(``ImportError: cannot import name ... from partially initialized module``)
— verified by trying exactly that and running the direct-import tests in
``tests/test_swarm_surface.py``. A third, dependency-free module is the only
way to share this logic without an import-order-dependent circular import.

``agent_surface.py`` re-exports all three names so ``from
src.cli.agent_surface import read_claim_epoch`` (existing call sites,
tests) keeps working unchanged.
"""

from __future__ import annotations

import json
import os

import click


def read_claim_epoch(cwd: str | None = None) -> int | None:
    """Resolve the calling session's current claim epoch (swarm-work-model §10).

    Reads ``.aq/claim.json`` (written server-side by ``task_claim`` at the
    workspace root), falling back to the ``AQ_CLAIM_EPOCH`` env var push
    launches export. Returns ``None`` when neither resolves — the mutator
    commands that use this only send ``claim_epoch`` when it returns a
    value, so a task (non-pool) session's calls are unaffected.

    The search walks *up* from *cwd* to the filesystem root. It accepts a
    claim file only when its task and session identities do not conflict
    with the calling worker's environment. This lets a worker that ``cd``ed
    into a subdirectory retain its current epoch, without inheriting a claim
    from a slot that has been reused by another session.
    """
    base = os.path.abspath(cwd or os.getcwd())
    task_id = os.environ.get("AQ_TASK_ID")
    session_id = os.environ.get("AQ_SESSION_ID")
    seen = set()
    while base not in seen:
        seen.add(base)
        path = os.path.join(base, ".aq", "claim.json")
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            if (
                (task_id and data.get("task_id") not in (None, task_id))
                or (session_id and data.get("session_id") not in (None, session_id))
            ):
                base = os.path.dirname(base)
                continue
            epoch = data.get("claim_epoch")
            if epoch is not None:
                return int(epoch)
            break  # a claim file with no epoch: stop, don't climb past it
        except (FileNotFoundError, NotADirectoryError):
            pass
        except (OSError, ValueError, json.JSONDecodeError):
            break  # unreadable/corrupt claim file — fall through to the env
        base = os.path.dirname(base)
    raw = os.environ.get("AQ_CLAIM_EPOCH")
    if raw is not None:
        try:
            return int(raw)
        except ValueError:
            return None
    return None


def resolve_claim_epoch(explicit: int | None) -> int | None:
    """*explicit* (an already-parsed ``--claim-epoch`` value) if given, else
    whatever :func:`read_claim_epoch` resolves from ``.aq/claim.json`` /
    ``$AQ_CLAIM_EPOCH``. Shared by every mutator command that fences on the
    claim epoch (``aq handoff``, ``aq task close|heartbeat|set``) so there is
    one place that decides the precedence.
    """
    return explicit if explicit is not None else read_claim_epoch()


def claim_epoch_option(fn):
    """Decorator adding the ``--claim-epoch`` option every claim-fenced mutator shares."""
    return click.option(
        "--claim-epoch",
        "claim_epoch",
        type=int,
        default=None,
        help="Claim epoch for a pool session (defaults to .aq/claim.json / $AQ_CLAIM_EPOCH).",
    )(fn)
