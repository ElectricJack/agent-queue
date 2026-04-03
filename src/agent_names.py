"""Creative agent name generation for personality-rich agent identifiers.

Generates unique, memorable names for agents that give them personality
and character.  Names are designed to be:

- Memorable and distinctive in conversation
- Fun to reference in Discord chat
- Short enough for embed display
- Evocative of an AI assistant personality

Multiple naming strategies provide variety:

1. **Legendary figures** -- mythological, historical, and literary names
   that carry built-in gravitas and personality (e.g. "Atlas", "Athena").
2. **Astronomical** -- space-themed names that feel futuristic and unique
   (e.g. "Nova", "Quasar", "Pulsar").
3. **Compound codenames** -- two-word combinations from a title/role word
   and a distinctive codename (e.g. "Rogue Cipher", "Vanguard Echo").

The combined pool yields thousands of possible names.  On collision with
existing agents, a two-digit numeric suffix is appended as a fallback.
"""

from __future__ import annotations

import random

# ---------------------------------------------------------------------------
# Word pools -- curated for memorability, personality, and brevity
# ---------------------------------------------------------------------------

# Mythological, legendary, and literary figures -- standalone names that
# feel like characters.  Deliberately short (1-3 syllables) for easy
# recall and typing in Discord.
LEGENDARY_NAMES = [
    "Atlas",
    "Athena",
    "Apollo",
    "Artemis",
    "Phoenix",
    "Orion",
    "Hermes",
    "Helios",
    "Icarus",
    "Titan",
    "Prometheus",
    "Odysseus",
    "Merlin",
    "Odin",
    "Loki",
    "Freya",
    "Thor",
    "Raven",
    "Sphinx",
    "Griffin",
    "Valkyrie",
    "Zephyr",
    "Midas",
    "Chronos",
    "Argus",
    "Clio",
    "Delphi",
    "Echo",
    "Iris",
    "Janus",
    "Minerva",
    "Oberon",
    "Puck",
    "Sage",
    "Triton",
    "Vesta",
]

# Astronomical objects and phenomena -- futuristic, evocative, and unique.
ASTRO_NAMES = [
    "Nova",
    "Nebula",
    "Quasar",
    "Pulsar",
    "Cosmos",
    "Vega",
    "Sirius",
    "Rigel",
    "Lyra",
    "Castor",
    "Pollux",
    "Altair",
    "Andromeda",
    "Solaris",
    "Eclipse",
    "Aurora",
    "Comet",
    "Stellar",
    "Zenith",
    "Astra",
    "Celeste",
    "Orbit",
    "Lunar",
    "Equinox",
    "Parallax",
    "Perihelion",
    "Apogee",
    "Umbra",
    "Polaris",
    "Arcturus",
]

# Titles and role words -- paired with codenames to form two-word names.
# These give agents a sense of purpose and rank.
TITLES = [
    "Agent",
    "Operative",
    "Sentinel",
    "Vanguard",
    "Pilot",
    "Scout",
    "Ranger",
    "Cipher",
    "Vector",
    "Proxy",
    "Arbiter",
    "Envoy",
    "Warden",
    "Nexus",
    "Conduit",
    "Catalyst",
    "Oracle",
    "Architect",
    "Marshal",
    "Pathfinder",
]

# Codenames -- punchy single words that pair well with titles.
# Selected for variety across nature, tech, and abstract concepts.
CODENAMES = [
    "Blaze",
    "Drift",
    "Flux",
    "Haze",
    "Bolt",
    "Prism",
    "Storm",
    "Frost",
    "Ember",
    "Wraith",
    "Shade",
    "Glyph",
    "Surge",
    "Crest",
    "Apex",
    "Forge",
    "Rune",
    "Spark",
    "Helix",
    "Shard",
    "Vortex",
    "Flint",
    "Ridge",
    "Quill",
    "Onyx",
    "Cobalt",
    "Thistle",
    "Dusk",
    "Torrent",
    "Basalt",
    "Moss",
    "Bramble",
    "Fable",
    "Lumen",
    "Gossamer",
    "Spire",
]

# Elements and materials -- for a tech/scientific feel
ELEMENT_NAMES = [
    "Carbon",
    "Neon",
    "Argon",
    "Cobalt",
    "Copper",
    "Iron",
    "Chrome",
    "Silicon",
    "Titanium",
    "Zinc",
    "Nickel",
    "Bismuth",
    "Iridium",
    "Osmium",
    "Radium",
    "Cesium",
    "Indigo",
    "Obsidian",
    "Graphite",
    "Marble",
    "Jasper",
    "Amber",
    "Onyx",
    "Beryl",
]

# Nature-inspired evocative names -- organic and poetic
NATURE_NAMES = [
    "Cypress",
    "Aspen",
    "Cedar",
    "Willow",
    "Birch",
    "Juniper",
    "Sequoia",
    "Hawthorn",
    "Rowan",
    "Laurel",
    "Maple",
    "Briar",
    "Thistle",
    "Clover",
    "Fern",
    "Bramble",
    "Sorrel",
    "Yarrow",
    "Wren",
    "Falcon",
    "Osprey",
    "Sparrow",
    "Kestrel",
    "Peregrine",
]

# ---------------------------------------------------------------------------
# Name generation strategies
# ---------------------------------------------------------------------------

_MAX_RETRIES = 20


def _pick_legendary() -> str:
    """Single mythological/legendary name."""
    return random.choice(LEGENDARY_NAMES)


def _pick_astro() -> str:
    """Single astronomical name."""
    return random.choice(ASTRO_NAMES)


def _pick_element() -> str:
    """Single element/material name."""
    return random.choice(ELEMENT_NAMES)


def _pick_nature() -> str:
    """Single nature-inspired name."""
    return random.choice(NATURE_NAMES)


def _pick_compound() -> str:
    """Title + codename combination (e.g. 'Vanguard Flux')."""
    return f"{random.choice(TITLES)} {random.choice(CODENAMES)}"


# Strategies and their relative weights.  Compound names get extra weight
# because the combinatorial space is much larger (~720 vs ~30 each for
# the single-name pools), reducing collision probability.
_STRATEGIES: list[tuple[callable, int]] = [
    (_pick_legendary, 3),
    (_pick_astro, 3),
    (_pick_element, 2),
    (_pick_nature, 2),
    (_pick_compound, 5),
]

_STRATEGY_FNS: list[callable] = []
_STRATEGY_WEIGHTS: list[int] = []
for _fn, _w in _STRATEGIES:
    _STRATEGY_FNS.append(_fn)
    _STRATEGY_WEIGHTS.append(_w)


def generate_agent_name() -> str:
    """Generate a single creative agent name (no uniqueness check).

    Returns a display name like ``"Phoenix"`` or ``"Sentinel Drift"``.
    Useful when you want a name without database access.
    """
    fn = random.choices(_STRATEGY_FNS, weights=_STRATEGY_WEIGHTS, k=1)[0]
    return fn()


async def generate_unique_agent_name(db) -> str:
    """Generate a creative agent name that doesn't collide with existing agents.

    Tries up to ``_MAX_RETRIES`` random names.  If all collide (very unlikely
    given the large combinatorial space), appends a random two-digit suffix.

    Parameters
    ----------
    db:
        Database instance with a ``get_agent(agent_id)`` method.

    Returns
    -------
    str
        A unique display name (e.g. ``"Vanguard Flux"`` or ``"Phoenix"``).
        The caller derives the agent ID via ``.lower().replace(" ", "-")``.
    """
    for _ in range(_MAX_RETRIES):
        name = generate_agent_name()
        agent_id = name.lower().replace(" ", "-")
        existing = await db.get_agent(agent_id)
        if not existing:
            return name

    # Fallback: append a random two-digit suffix
    while True:
        name = generate_agent_name()
        suffix = random.randint(10, 99)
        name_with_suffix = f"{name} {suffix}"
        agent_id = name_with_suffix.lower().replace(" ", "-")
        existing = await db.get_agent(agent_id)
        if not existing:
            return name_with_suffix
