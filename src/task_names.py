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


async def generate_task_id(db, parent_id: str | None = None) -> str:
    """Generate a unique task ID, checking the DB for collisions.

    When *parent_id* is set, returns a hierarchical child id
    ``f"{parent_id}.{n}"`` (work-graph §7), where ``n`` is the next
    sibling ordinal.  Depth is capped at ``_MAX_HIERARCHY_DEPTH``; at cap
    the helper falls back to a fresh root id and logs a warning — the
    caller (``_cmd_create_task``) is expected to add a
    ``discovered-from`` edge to *parent_id* so provenance survives.

    Otherwise (root id) returns the classic adjective-noun form.
    """
    if parent_id is not None:
        if _hierarchy_depth(parent_id) >= _MAX_HIERARCHY_DEPTH:
            import logging as _logging

            _logging.getLogger(__name__).warning(
                "generate_task_id: parent '%s' at depth %d hits cap %d — "
                "falling back to root id (caller should add a "
                "'discovered-from' edge)",
                parent_id,
                _hierarchy_depth(parent_id),
                _MAX_HIERARCHY_DEPTH,
            )
        else:
            n = await _next_child_ordinal(db, parent_id)
            candidate = f"{parent_id}.{n}"
            existing = await db.get_task(candidate)
            if existing is None:
                return candidate
            # Extremely rare (racing create) — bump until unused.
            while existing is not None:
                n += 1
                candidate = f"{parent_id}.{n}"
                existing = await db.get_task(candidate)
            return candidate

    for _ in range(_MAX_RETRIES):
        name = f"{random.choice(ADJECTIVES)}-{random.choice(NOUNS)}"
        existing = await db.get_task(name)
        if not existing:
            return name

    # Fallback: append a random 2-digit suffix
    while True:
        name = f"{random.choice(ADJECTIVES)}-{random.choice(NOUNS)}-{random.randint(10, 99)}"
        existing = await db.get_task(name)
        if not existing:
            return name
