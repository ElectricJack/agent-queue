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
commit the generated merge revision. Duplicate revision IDs must instead
be resolved while preserving the deployed revision's meaning; Alembic's
duplicate-ID warning is an error even when the graph has only one head.
"""

from __future__ import annotations

import pathlib

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

ROOT = pathlib.Path(__file__).resolve().parent.parent


@pytest.mark.filterwarnings("error:Revision .* is present more than once:UserWarning")
def test_alembic_chain_is_single_headed():
    script = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
    heads = script.get_heads()
    assert len(heads) == 1, (
        "The alembic chain must stay single-headed; `alembic upgrade head` fails "
        f"with MultipleHeads otherwise. Heads: {sorted(heads)}. Join them with "
        f'`alembic merge -m "<why>" {" ".join(sorted(heads))}` and commit the '
        "generated revision."
    )


#: Revisions an operator database may already be stamped with, and the parent
#: each was deployed on.  A sibling that later lands on the same parent joins
#: it through a merge revision (``alembic merge``); re-chaining a deployed
#: revision onto the sibling would make every database stamped with it skip
#: the sibling for good.  ``a00000000050`` was deployed on ``a00000000048``
#: while the live candidate carried its own ``a00000000049``
#: (docs/superpowers/specs/2026-10-01-later-repair-stage-constraints-design.md).
DEPLOYED_PARENTS = {"a00000000050": "a00000000048"}


def test_deployed_revisions_keep_their_parent():
    script = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
    for revision, parent in DEPLOYED_PARENTS.items():
        down = script.get_revision(revision).down_revision
        assert down == parent, (
            f"{revision} is deployed on {parent} but now revises {down!r}. Restore "
            f"down_revision = {parent!r} and join the heads with "
            '`alembic merge -m "<why>" <head-a> <head-b>` instead of re-chaining.'
        )
