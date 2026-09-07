"""SQLite batch rebuilds must not silently retire an earlier revision's triggers.

Alembic's SQLite batch mode copies a table and drops the original, and SQLite
drops the original's triggers with it.  ``migrations/sqlite_triggers.py``
restores them; this walk catches the next revision that forgets to.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

pytestmark = pytest.mark.migration

ROOT = Path(__file__).resolve().parents[1]


def _triggers(db_path: Path) -> set[str]:
    with sqlite3.connect(db_path) as conn:
        return {name for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")}


def test_every_revision_keeps_the_sqlite_triggers_it_does_not_name(tmp_path):
    db_path = tmp_path / "triggers.db"
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db_path}")
    script = ScriptDirectory.from_config(config)
    # ``walk_revisions`` yields head first; apply base first.
    chain = list(reversed(list(script.walk_revisions("base", "heads"))))

    silent_losses: list[str] = []
    before: set[str] = set()
    for revision in chain:
        command.upgrade(config, revision.revision)
        after = _triggers(db_path)
        source = Path(revision.path).read_text()
        silent_losses.extend(
            f"{revision.revision}: {name}" for name in sorted(before - after) if name not in source
        )
        before = after

    assert not silent_losses, (
        "a batch rebuild dropped triggers the revision never mentions; wrap it in "
        "preserve_sqlite_triggers() or retire them by name:\n" + "\n".join(silent_losses)
    )
    assert before, "head should carry the integration invariant triggers"

    command.downgrade(config, "base")
    assert _triggers(db_path) == set()
