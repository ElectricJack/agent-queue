# tests/test_migration_single_head.py
"""The alembic revision chain must have exactly one head.

Two branches that each add a migration on the same parent are individually
single-headed, so both pass CI; the second head only exists once *main* has
merged them. From then on ``alembic upgrade head`` raises

    alembic.util.exc.CommandError: Multiple head revisions are present ...

inside :mod:`migrations.env`, so every test that builds a database fixture
errors in setup and the real failures underneath are invisible. That is what
happened on 2026-09-03 (revisions ``f4a2c0de0007`` and ``f4f2c0de0007``,
joined by the merge point ``43b61ffc38ec``).

``tests/test_worktree_migration.py`` carried this assertion before, but that
module is ``pytest.mark.migration`` and so is deselected from the default
suite and from ``aq test``'s defaults — nobody saw it until CI's migration
shard ran. This check reads the revision files and nothing else, so it lives
in the default suite, where a worker running the tests for their own change
trips over it immediately.

Fix a second head with ``alembic merge -m "<why>" <head-a> <head-b>``, and
commit the generated merge revision.
"""

from __future__ import annotations

import pathlib

from alembic.config import Config
from alembic.script import ScriptDirectory

ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_alembic_chain_is_single_headed():
    script = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
    heads = script.get_heads()
    assert len(heads) == 1, (
        "The alembic chain must stay single-headed; `alembic upgrade head` fails "
        f"with MultipleHeads otherwise. Heads: {sorted(heads)}. Join them with "
        f'`alembic merge -m "<why>" {" ".join(sorted(heads))}` and commit the '
        "generated revision."
    )
