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


async def generate_task_id(db) -> str:
    """Generate a unique adjective-noun task ID, checking the DB for collisions.

    Tries up to ``_MAX_RETRIES`` random combinations.  If all collide (very
    unlikely with 896 base combinations), appends a random two-digit suffix
    to guarantee uniqueness.
    """
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
