"""Alembic revision lookups for migration tests.

A migration test that proves one revision's own ``upgrade``/``downgrade`` needs
two revision ids: the revision under test, and the revision immediately before
it to start from. Only the first is a real constant — the second is a fact about
the chain, and hard-coding it makes the test silently wrong the moment another
revision is inserted ahead of it. That is exactly how
``tests/test_migration_escalation_state.py`` came to upgrade to ``a00000000005``
while the escalation relations had moved to ``a00000000009``: the pair was
written when the escalation revision *was* the fifth, revisions ``a5``..``a8``
were inserted later, and the constants never moved.

Derive the predecessor from the script directory instead.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


@cache
def previous_revision(revision: str) -> str:
    """The single revision *revision* chains onto.

    Raises if the revision is unknown, is a base revision, or is a merge point —
    none of those give a migration test one unambiguous starting revision.
    """
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(Config(str(_ROOT / "alembic.ini")))
    down = script.get_revision(revision).down_revision
    if isinstance(down, str):
        return down
    if not down:
        raise RuntimeError(f"revision {revision!r} is a base revision with no predecessor")
    raise RuntimeError(
        f"revision {revision!r} is a merge of {sorted(down)!r} and has no single "
        "predecessor to start a migration test from"
    )
