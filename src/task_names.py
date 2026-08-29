"""Human-friendly task ID generation using adjective-noun combinations.

Task IDs like "swift-falcon" or "bold-summit" are used everywhere instead of
UUIDs because operators interact with tasks primarily through Discord chat.
Memorable names are easier to type, discuss, and recall than hex strings —
especially when managing dozens of concurrent tasks across projects.

The pool of ~896 combinations (28 adjectives x 32 nouns) is sufficient for
most workloads. On collision, a two-digit numeric suffix is appended as a
fallback.
"""

from __future__ import annotations

import logging
import random

ADJECTIVES = [
    "swift",
    "bright",
    "calm",
    "bold",
    "keen",
    "wise",
    "fair",
    "sharp",
    "clear",
    "eager",
    "fresh",
    "grand",
    "prime",
    "quick",
    "smart",
    "sound",
    "solid",
    "stark",
    "steady",
    "noble",
    "crisp",
    "fleet",
    "nimble",
    "brisk",
    "vivid",
    "agile",
    "amber",
    "azure",
]

NOUNS = [
    "falcon",
    "horizon",
    "cascade",
    "ember",
    "summit",
    "ridge",
    "beacon",
    "current",
    "delta",
    "forge",
    "glacier",
    "harbor",
    "impact",
    "journey",
    "lantern",
    "meadow",
    "nexus",
    "orbit",
    "pinnacle",
    "quest",
    "rapids",
    "stone",
    "torrent",
    "vault",
    "willow",
    "zenith",
    "apex",
    "bridge",
    "crest",
    "dune",
    "flare",
    "grove",
]

_MAX_RETRIES = 10

#: Naming depth cap (swarm-work-model §4): a parent whose id already has this
#: many dot-segments mints *root* ids for its children (plus a
#: ``discovered-from`` edge, added by the caller).  Naming depth never blocks
#: a structural operation; structural depth is enforced by the query layer.
MAX_NAMING_DEPTH = 3

#: Structural depth cap — the live ``parent-child`` chain length, root = 1.
MAX_STRUCTURAL_DEPTH = 3


def naming_depth(task_id: str) -> int:
    """Number of dot-separated segments in *task_id*."""
    return task_id.count(".") + 1


async def fresh_root_id(conn) -> str:
    """A fresh adjective-noun root id, collision-checked on *conn*."""
    from sqlalchemy import select

    from src.database.tables import tasks

    async def _exists(name: str) -> bool:
        row = (await conn.execute(select(tasks.c.id).where(tasks.c.id == name))).fetchone()
        return row is not None

    for _ in range(_MAX_RETRIES):
        name = f"{random.choice(ADJECTIVES)}-{random.choice(NOUNS)}"
        if not await _exists(name):
            return name
    while True:
        name = f"{random.choice(ADJECTIVES)}-{random.choice(NOUNS)}-{random.randint(10, 99)}"
        if not await _exists(name):
            return name


async def reserve_child_ordinal(conn, parent_id: str) -> int:
    """Atomically take the next child ordinal from the parent row (spec §6).

    ``UPDATE … RETURNING`` on both dialects (SQLite ≥ 3.35 supports
    RETURNING; the bundled library is newer).  The row update is the
    serialisation point: two concurrent reservations cannot return the same
    number because the second UPDATE sees the first's increment.  Ordinals
    are never reused — deletes leave gaps on purpose (an id must never be
    re-minted).
    """
    from sqlalchemy import update

    from src.database.tables import tasks

    stmt = (
        update(tasks)
        .where(tasks.c.id == parent_id)
        .values(next_child_ordinal=tasks.c.next_child_ordinal + 1)
        .returning(tasks.c.next_child_ordinal)
    )
    row = (await conn.execute(stmt)).fetchone()
    if row is None:
        raise KeyError(parent_id)
    return int(row[0]) - 1


async def child_task_id(conn, parent_id: str) -> tuple[str, bool]:
    """Return ``(id, capped)`` for a new child of *parent_id* (spec §6).

    ``capped=False`` — ``f"{parent_id}.{n}"`` with *n* reserved atomically.
    ``capped=True``  — the parent is at :data:`MAX_NAMING_DEPTH`; a fresh
    root id is returned and the caller adds a ``discovered-from`` edge so
    provenance survives without extending the dotted chain.
    """
    if naming_depth(parent_id) >= MAX_NAMING_DEPTH:
        logging.getLogger(__name__).info(
            "child_task_id: parent '%s' at naming depth cap — minting a root id", parent_id
        )
        return (await fresh_root_id(conn), True)
    n = await reserve_child_ordinal(conn, parent_id)
    return (f"{parent_id}.{n}", False)


async def generate_task_id(db, parent_id: str | None = None) -> str:
    """Generate a unique task id.

    With *parent_id* this opens its own transaction and delegates to
    :func:`child_task_id`; callers that already hold a connection (the
    hierarchy mixin, the graph creator) call that directly instead.
    """
    async with db._engine.begin() as conn:
        if parent_id is not None:
            cid, _capped = await child_task_id(conn, parent_id)
            return cid
        return await fresh_root_id(conn)
