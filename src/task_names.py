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

# Depth cap on hierarchical child ids (work-graph §7).  A child of a
# depth-``_MAX_HIERARCHY_DEPTH`` task falls back to a fresh root id and
# gets a ``discovered-from`` edge to its notional parent, with a warning.
_MAX_HIERARCHY_DEPTH = 3


def _hierarchy_depth(task_id: str) -> int:
    """Number of dot-separated segments in *task_id* (depth of the id)."""
    return task_id.count(".") + 1


async def _next_child_ordinal(db, parent_id: str) -> int:
    """Return the next sibling ordinal for children of *parent_id*.

    Children carry ids of the form ``{parent}.{n}``; ordinals fill upward
    from the max existing sibling to avoid reusing gaps left by deletes.
    """
    # ``get_subtasks`` filters by ``parent_task_id``; we only need the
    # numeric suffix of each child id under the dot form.
    try:
        children = await db.get_subtasks(parent_id)
    except Exception:
        children = []
    max_ord = 0
    prefix = f"{parent_id}."
    for child in children:
        cid = child.id if hasattr(child, "id") else child
        if not isinstance(cid, str) or not cid.startswith(prefix):
            continue
        tail = cid[len(prefix):]
        # Only pure-integer segments count as ordinals under this scheme.
        seg = tail.split(".", 1)[0]
        try:
            n = int(seg)
        except ValueError:
            continue
        if n > max_ord:
            max_ord = n
    return max_ord + 1


async def _fresh_root_id(db) -> str:
    """Generate a fresh adjective-noun root id, colliding-safe against the DB."""
    for _ in range(_MAX_RETRIES):
        name = f"{random.choice(ADJECTIVES)}-{random.choice(NOUNS)}"
        existing = await db.get_task(name)
        if not existing:
            return name
    while True:
        name = f"{random.choice(ADJECTIVES)}-{random.choice(NOUNS)}-{random.randint(10, 99)}"
        existing = await db.get_task(name)
        if not existing:
            return name


async def child_task_id(db, parent_id: str) -> tuple[str, bool]:
    """Return ``(id, capped)`` for a child of *parent_id* (work-graph §7).

    * ``capped=False`` — hierarchical child ``f"{parent_id}.{n}"`` where
      ``n`` is the next sibling ordinal.
    * ``capped=True`` — parent is at ``_MAX_HIERARCHY_DEPTH`` so we return
      a fresh root id; the caller is expected to add a ``discovered-from``
      edge to *parent_id* so provenance survives.

    ``_cmd_create_task`` is the sole caller that needs the ``capped`` flag
    (it has the DB handle to add the edge).  ``generate_task_id`` remains
    the shorthand for callers that don't care about the fallback signal.
    """
    if _hierarchy_depth(parent_id) >= _MAX_HIERARCHY_DEPTH:
        import logging as _logging

        _logging.getLogger(__name__).warning(
            "child_task_id: parent '%s' at depth %d hits cap %d — "
            "falling back to root id (caller should add a "
            "'discovered-from' edge)",
            parent_id,
            _hierarchy_depth(parent_id),
            _MAX_HIERARCHY_DEPTH,
        )
        return (await _fresh_root_id(db), True)

    n = await _next_child_ordinal(db, parent_id)
    candidate = f"{parent_id}.{n}"
    existing = await db.get_task(candidate)
    while existing is not None:
        n += 1
        candidate = f"{parent_id}.{n}"
        existing = await db.get_task(candidate)
    return (candidate, False)


async def generate_task_id(db, parent_id: str | None = None) -> str:
    """Generate a unique task ID, checking the DB for collisions.

    When *parent_id* is set, returns a hierarchical child id
    ``f"{parent_id}.{n}"`` (work-graph §7).  At depth cap the helper
    falls back to a fresh root id and logs a warning — callers that need
    to know about the fallback should use :func:`child_task_id` instead,
    which returns ``(id, capped)`` so it can wire the ``discovered-from``
    edge.

    Otherwise (root id) returns the classic adjective-noun form.
    """
    if parent_id is not None:
        cid, _capped = await child_task_id(db, parent_id)
        return cid
    return await _fresh_root_id(db)
