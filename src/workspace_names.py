"""Human-friendly workspace ID generation using adjective-noun combinations.

Workspace IDs like "ws-iron-tower" or "ws-mossy-den" replace opaque hex hashes
(``ws-a1b2c3d4``) so operators can reference workspaces by name in Discord.
Memorable names make it obvious which workspace an agent is using during
conversation, especially when multiple workspaces exist per project.

Word pools are deliberately distinct from task_names.py (which uses
nature/adventure-themed words) to avoid confusing workspace IDs with task IDs
at a glance.  Workspace words lean toward buildings, materials, and places —
evoking a "location where work happens".

The pool of ~928 combinations (29 adjectives x 32 nouns) is sufficient for
most workloads.  On collision, a two-digit numeric suffix is appended as a
fallback.
"""

from __future__ import annotations

import random

# Adjectives — tactile, material, and structural descriptors that pair
# naturally with workshop/location nouns.  Distinct from task_names.py's
# speed/cognition-themed adjectives.
ADJECTIVES = [
    "iron",
    "mossy",
    "dusty",
    "stone",
    "copper",
    "rusty",
    "sunny",
    "foggy",
    "sandy",
    "golden",
    "silver",
    "frosty",
    "warm",
    "quiet",
    "hidden",
    "narrow",
    "deep",
    "steep",
    "outer",
    "inner",
    "upper",
    "lower",
    "north",
    "south",
    "east",
    "west",
    "coastal",
    "hollow",
    "ancient",
]

# Nouns — workshops, shelters, and small places that evoke "a workspace".
# No overlap with task_names.py nouns (falcon, horizon, cascade, etc.).
NOUNS = [
    "tower",
    "den",
    "shed",
    "loft",
    "barn",
    "mill",
    "dock",
    "camp",
    "post",
    "well",
    "arch",
    "gate",
    "hall",
    "keep",
    "yard",
    "bay",
    "pier",
    "hut",
    "spire",
    "lodge",
    "burrow",
    "cabin",
    "depot",
    "annex",
    "wharf",
    "chapel",
    "cellar",
    "turret",
    "alcove",
    "dome",
    "plaza",
    "court",
]

_MAX_RETRIES = 10


async def generate_workspace_id(db) -> str:
    """Generate a unique ``ws-{adjective}-{noun}`` workspace ID.

    Checks existing workspaces via ``db.get_workspace()`` to avoid collisions.
    Tries up to ``_MAX_RETRIES`` random combinations.  If all collide (very
    unlikely with ~928 base combinations), appends a random two-digit suffix
    to guarantee uniqueness.
    """
    for _ in range(_MAX_RETRIES):
        name = f"ws-{random.choice(ADJECTIVES)}-{random.choice(NOUNS)}"
        existing = await db.get_workspace(name)
        if not existing:
            return name

    # Fallback: append a random 2-digit suffix
    while True:
        name = f"ws-{random.choice(ADJECTIVES)}-{random.choice(NOUNS)}-{random.randint(10, 99)}"
        existing = await db.get_workspace(name)
        if not existing:
            return name
