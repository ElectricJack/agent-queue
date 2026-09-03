"""The Alembic revision graph is well-formed — checked in the default suite.

This module deliberately carries **no marker**.  The equivalent assertion used
to live only in ``tests/test_worktree_migration.py``, whose module-level
``pytestmark = pytest.mark.migration`` is deselected by ``pyproject.toml``'s
``addopts`` and by ``aq test``'s default marker set, so it ran only in CI's
``migration-and-slow`` shard.  That is how ``f4a2c0de0007`` and
``f4f2c0de0007`` — two revisions that each declared ``e3f2c0de0006`` as parent
and were each single-headed on their own branch — merged into ``main`` and left
it with two heads, after which every test that builds a temporary database
errored in ``migrations/env.py`` with ``MultipleHeads``.

Nothing here touches a database: these are pure reads of the revision files, so
they cost milliseconds and belong in the suite everyone actually runs.
"""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

REPO_ROOT = Path(__file__).resolve().parent.parent
ALEMBIC_INI = REPO_ROOT / "alembic.ini"


def _script_directory() -> ScriptDirectory:
    # ``alembic.ini`` resolves ``script_location`` against ``%(here)s``, so an
    # absolute config path keeps this independent of the working directory.
    return ScriptDirectory.from_config(Config(str(ALEMBIC_INI)))


def test_alembic_chain_is_single_headed():
    heads = _script_directory().get_heads()
    assert len(heads) == 1, (
        f"alembic chain must stay single-headed, got {heads}. Two branches "
        f"landed with the same parent: add a merge revision with "
        f"`alembic merge -m '<what it joins>' {' '.join(heads)}`."
    )
